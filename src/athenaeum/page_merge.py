# SPDX-License-Identifier: Apache-2.0
"""Pure three-way page merge (athenaeum#2773, slice S3 of the multi-writer sync design).

:func:`merge_page` merges three versions of one page (the common ancestor, the
local side and the remote side) unit by unit, using the rule table in
``docs/design/federation-multi-writer-sync.md`` section 5.1. It returns the
merged page text plus the units it could not settle. It opens no file, runs no
git command and calls no model.

Units: one per frontmatter key (``fm:<key>``), the text before the first H2
(``preamble``), one per H2 section (``section:<title>``), and, inside a
section, lines. A unit that is *contested* (needs a decision) or *held* (needs
a person: ``uid``, ``type``, ``created``, ``access``, ``audience`` and a body
that ends inside an open code fence) carries the REMOTE value in the merged
page, so the function always returns a page that parses and never writes a
conflict marker.

``access`` and ``audience`` decide who may see a page. They are never merged:
no union, no model choice. A difference in either is held.

The type rules come from the same :class:`athenaeum.config.StructuredUpsertSpec`
the tier-0 structured upsert reads, so a type's rule is declared once. The
section and list helpers are shared with that module through
:mod:`athenaeum.page_text`; the two-input ``_merge_body`` and
``_merge_frontmatter`` are NOT reused (only the ancestor tells an addition on
one side from a removal on the other).

Layering: L2, a sibling of :mod:`athenaeum.structured_upsert`. Imports
:mod:`athenaeum.config` (L2), :mod:`athenaeum.models` (L1) and
:mod:`athenaeum.page_text` (L0). It imports no git helper, store, provider or
file API; ``tests/test_page_merge.py`` pins that import list.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from athenaeum.config import StructuredUpsertSpec
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.page_text import (
    as_list,
    ends_in_open_fence,
    norm_line,
    split_sections,
)

# Resolver names: which rule settled a unit.
R_UNCHANGED = "unchanged"  # neither side changed it
R_IDENTICAL = "identical"  # both sides changed it to the same value
R_LOCAL_ONLY = "local-only"  # only local changed it
R_REMOTE_ONLY = "remote-only"  # only remote changed it
R_UPDATED_LATER = "updated-later"
R_SET_MERGE = "set-merge"
R_ORDERED = "ordered"
R_APPEND = "append"
R_LINE_MERGE = "line-merge"
R_HELD = "held"
R_CONTESTED = "contested"
R_DELETED_BOTH = "deleted-both"
R_ERASURE = "erasure"
R_DELETED_ONE_SIDE = "deleted-one-side"  # deleted on one side, untouched on the other
R_ADDED_ONE_SIDE = "added-one-side"
R_DELETE_VS_EDIT = "delete-vs-edit"

#: Page-identity and visibility keys that are never merged: a difference is held.
HELD_KEYS = ("uid", "type", "created", "access", "audience")
#: List-valued keys that are set-merged for every type, on top of ``union_keys``.
_DEFAULT_LIST_KEYS = ("aliases", "tags", "sources")
_REQUIRED_KEYS = ("uid", "type", "name")

REASON_UNCLOSED_FENCE = "unclosed-fence"
REASON_UNPARSEABLE = "unparseable"

_MISSING: Any = object()


@dataclass(frozen=True)
class HeldUnit:
    """A unit that waits for a person, with a reason code naming why."""

    unit: str
    reason: str


@dataclass(frozen=True)
class PageMergeResult:
    """The merged page and what could not be settled.

    ``text`` is ``None`` when the page is deleted. ``resolvers`` maps every
    unit that was merged to the name of the rule that settled it.
    """

    text: str | None
    contested: tuple[str, ...] = ()
    held: tuple[HeldUnit, ...] = ()
    resolvers: Mapping[str, str] = field(default_factory=dict)

    @property
    def deleted(self) -> bool:
        return self.text is None


class _Run:
    """Accumulates per-unit outcomes for one merge."""

    def __init__(self) -> None:
        self.contested: list[str] = []
        self.held: list[HeldUnit] = []
        self.resolvers: dict[str, str] = {}

    def settle(self, unit: str, resolver: str) -> None:
        self.resolvers[unit] = resolver

    def contest(self, unit: str) -> None:
        self.contested.append(unit)
        self.resolvers[unit] = R_CONTESTED

    def hold(self, unit: str, reason: str) -> None:
        self.held.append(HeldUnit(unit, reason))
        self.resolvers[unit] = R_HELD

    def result(self, text: str | None) -> PageMergeResult:
        return PageMergeResult(text, tuple(self.contested), tuple(self.held), dict(self.resolvers))


def merge_page(
    base: str | None,
    local: str | None,
    remote: str | None,
    spec: StructuredUpsertSpec | None = None,
    *,
    erasure: bool = False,
) -> PageMergeResult:
    """Three-way merge of one page. Each version is the page text, or ``None`` when absent.

    *spec* is the page type's :class:`~athenaeum.config.StructuredUpsertSpec`
    (``None`` for a type without one). *erasure* says that a deletion on either
    side is an erasure; the caller reads the erasure mark (slice S16), this
    function only honours the flag.
    """
    run = _Run()

    # --- Whole-page cases: a side is absent. ---
    if local is None and remote is None:
        # Deleted on both sides (or never present): nothing to keep.
        run.settle("page", R_ERASURE if erasure else R_DELETED_BOTH)
        return run.result(None)
    if local is None or remote is None:
        kept = remote if local is None else local
        assert kept is not None
        if erasure:
            # Erasure always wins, even over an edit on the other side.
            run.settle("page", R_ERASURE)
            return run.result(None)
        if base is None:
            # Added on one side only: nothing to merge.
            run.settle("page", R_ADDED_ONE_SIDE)
            return run.result(kept)
        if kept == base:
            # Deleted on one side, untouched on the other: the deletion stands.
            run.settle("page", R_DELETED_ONE_SIDE)
            return run.result(None)
        # Deleted on one side, edited on the other: the page stays with the new
        # fact and the deletion is raised again (operator ruling 4, design
        # section 15), so the page that survives is the merged page.
        run.contest("page")
        run.settle("page", R_DELETE_VS_EDIT)
        return run.result(kept)

    # --- Whole-page shortcuts keep the text byte for byte. ---
    # Identical sides, or one side equal to the ancestor, need no unit merge.
    if local == remote:
        run.settle("page", R_UNCHANGED if local == base else R_IDENTICAL)
        return run.result(local)
    if base is not None and remote == base:
        run.settle("page", R_LOCAL_ONLY)
        return run.result(local)
    if base is not None and local == base:
        run.settle("page", R_REMOTE_ONLY)
        return run.result(remote)

    # --- Unit merge. ---
    l_meta, l_body = parse_frontmatter(local)
    r_meta, r_body = parse_frontmatter(remote)
    if base is None:
        b_meta: dict[str, object] | None = None
        b_body: str | None = None
    else:
        b_meta, b_body = parse_frontmatter(base)
        if not isinstance(b_meta, dict) or not b_meta:
            b_meta, b_body = None, None  # an unparseable ancestor is no ancestor
    if not isinstance(l_meta, dict) or not isinstance(r_meta, dict) or not l_meta or not r_meta:
        # A side without parseable frontmatter has no units to merge. Hold the
        # whole page and keep the remote text.
        run.hold("page", REASON_UNPARSEABLE)
        return run.result(remote)

    meta = _merge_frontmatter(run, b_meta, l_meta, r_meta, spec)
    body = _merge_body(run, b_body, l_body, r_body, spec)
    text = render_frontmatter(meta) + body

    # Last guard: the result must parse and carry its identity keys. If a value
    # does not survive a YAML round trip, fall back to the remote page and hold.
    got, _ = parse_frontmatter(text)
    if not got or any(not str(got.get(k) or "").strip() for k in _REQUIRED_KEYS):
        fallback, _ = parse_frontmatter(remote)
        if fallback and all(str(fallback.get(k) or "").strip() for k in _REQUIRED_KEYS):
            run.hold("page", REASON_UNPARSEABLE)
            return run.result(remote)
    return run.result(text)


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def _item_key(item: object) -> str:
    return json.dumps(item, sort_keys=True, default=str)


def _three_way_set(base: object, local: object, remote: object) -> list[object]:
    """Everything either side has, minus what either side removed.

    Order: the ancestor's surviving items, then remote additions, then local
    additions, duplicates dropped. An ancestor item that one side no longer
    has was removed by that side; keeping it would undo the removal.
    """
    b_items = as_list(None if base is _MISSING else base)
    l_items = as_list(None if local is _MISSING else local)
    r_items = as_list(None if remote is _MISSING else remote)
    b_keys = {_item_key(i) for i in b_items}
    l_keys = {_item_key(i) for i in l_items}
    r_keys = {_item_key(i) for i in r_items}
    out: list[object] = []
    seen: set[str] = set()
    for i in b_items:
        k = _item_key(i)
        if k in l_keys and k in r_keys and k not in seen:
            seen.add(k)
            out.append(i)
    for i in r_items + l_items:
        k = _item_key(i)
        if k not in b_keys and k not in seen:
            seen.add(k)
            out.append(i)
    return out


def _as_date(value: object) -> date | None:
    if isinstance(value, date):  # datetime is a date too
        return value.date() if isinstance(value, datetime) else value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _merge_frontmatter(
    run: _Run,
    base: dict[str, object] | None,
    local: dict[str, object],
    remote: dict[str, object],
    spec: StructuredUpsertSpec | None,
) -> dict[str, object]:
    list_keys = set(_DEFAULT_LIST_KEYS)
    ordered: dict[str, tuple[str, ...]] = {}
    if spec is not None:
        list_keys |= set(spec.union_keys)
        ordered = dict(spec.ordered_keys)
    # audience is never a list-merge key: a union would widen who may see a page.
    list_keys -= set(HELD_KEYS)

    # Key order: the ancestor's, then keys it never had, sorted, so swapping
    # local and remote cannot change the order.
    base_keys = list(base) if base else []
    all_keys = set(local) | set(remote) | set(base_keys)
    order = [k for k in base_keys if k in all_keys]
    order += sorted(k for k in all_keys if k not in set(base_keys))

    merged: dict[str, object] = {}
    for key in order:
        unit = f"fm:{key}"
        b = _MISSING if base is None else base.get(key, _MISSING)
        lv = local.get(key, _MISSING)
        rv = remote.get(key, _MISSING)
        value = _merge_key(run, unit, key, b, lv, rv, base is None, list_keys, ordered)
        if key in _REQUIRED_KEYS and value is _MISSING:
            # uid/type/name must survive in a page that parses, whichever side has it.
            value = lv if lv is not _MISSING else b
        if value is not _MISSING:
            merged[key] = value
    return merged


def _merge_key(
    run: _Run,
    unit: str,
    key: str,
    b: Any,
    lv: Any,
    rv: Any,
    no_ancestor: bool,
    list_keys: set[str],
    ordered: dict[str, tuple[str, ...]],
) -> Any:
    # Rule: both sides agree, so there is nothing to settle.
    if lv == rv:
        run.settle(unit, R_UNCHANGED if lv == b else R_IDENTICAL)
        return rv
    # Rule: uid/type/created/access/audience are never merged. They name the
    # page or decide who may see it, so neither a rule nor a model may pick one.
    # This precedes the one-side rule: an audience change on one side is held
    # when the other side also edited the page. The remote value stays.
    if key in HELD_KEYS:
        run.hold(unit, f"held-{key}")
        return rv
    # Without an ancestor nothing says which side added or removed a value, so
    # no rule may settle a difference: it is contested and the remote stays.
    if no_ancestor:
        run.contest(unit)
        return rv
    # Rule: changed on one side only, that value.
    if lv == b:
        run.settle(unit, R_REMOTE_ONLY)
        return rv
    if rv == b:
        run.settle(unit, R_LOCAL_ONLY)
        return lv
    # From here both sides changed the key, differently.
    # Rule: `updated` takes the later date.
    if key == "updated":
        dl, dr = _as_date(lv), _as_date(rv)
        if dl is not None and dr is not None:
            run.settle(unit, R_UPDATED_LATER)
            return lv if dl > dr else rv
        run.contest(unit)
        return rv
    # Rule: list-valued keys get a three-way set merge (keep what either side
    # has, drop what either side removed).
    if key in list_keys:
        items = _three_way_set(b, lv, rv)
        run.settle(unit, R_SET_MERGE)
        return items if items or lv is not _MISSING or rv is not _MISSING else _MISSING
    # Rule: an ordered key ratchets to the value further along the order.
    if key in ordered:
        ranks = ordered[key]
        sl, sr = str(lv), str(rv)
        if sl in ranks and sr in ranks:
            run.settle(unit, R_ORDERED)
            return sl if ranks.index(sl) > ranks.index(sr) else sr
        run.contest(unit)
        return rv
    # Rule: a set_keys scalar (the spec's overwrite-when-newer scalars) and any
    # other key changed differently on both sides is contested; remote stays.
    run.contest(unit)
    return rv


# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------


@dataclass
class _Unit:
    heading: str
    text: str


def _norm_text(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


_SectionKey = tuple[str, int]  # (casefolded title, 1-based occurrence of that title)


def _unit_name(key: _SectionKey) -> str:
    return f"section:{key[0]}" if key[1] == 1 else f"section:{key[0]}#{key[1]}"


def _units(body: str | None) -> tuple[str, dict[_SectionKey, _Unit]]:
    """(preamble, ordered section units keyed by (casefolded title, occurrence))."""
    if body is None:
        return "", {}
    preamble, sections = split_sections(body)
    out: dict[_SectionKey, _Unit] = {}
    seen: dict[str, int] = {}
    for sec in sections:
        seen[sec.key] = seen.get(sec.key, 0) + 1  # a repeated heading is its own unit
        key = (sec.key, seen[sec.key])
        out[key] = _Unit(_norm_text(sec.heading), _norm_text(sec.text))
    return _norm_text(preamble), out


def _merge_body(
    run: _Run,
    base: str | None,
    local: str,
    remote: str,
    spec: StructuredUpsertSpec | None,
) -> str:
    # Rule: a body that ends inside an unclosed code fence is held whole. Every
    # later heading would sit inside the fence, so no unit boundary is reliable
    # and merging across it could splice one side into the other's fence.
    bodies = [b for b in (base, local, remote) if b is not None]
    if any(ends_in_open_fence(b) for b in bodies):
        run.hold("body", REASON_UNCLOSED_FENCE)
        return remote

    b_pre, b_secs = _units(base)
    l_pre, l_secs = _units(local)
    r_pre, r_secs = _units(remote)
    no_base = base is None
    append = spec.append_sections if spec is not None else frozenset()
    replace = spec.replace_sections if spec is not None else frozenset()

    out: list[str] = []
    # The preamble (the H1) is plain lines.
    out.append(
        _merge_text(
            run, "preamble", None if no_base else b_pre, l_pre, r_pre, no_base, False, False
        )
    )

    keys = list(b_secs)
    keys += [k for k in r_secs if k not in b_secs]
    keys += [k for k in l_secs if k not in b_secs and k not in r_secs]
    for key in keys:
        unit = _unit_name(key)
        b = b_secs.get(key)
        lu = l_secs.get(key)
        ru = r_secs.get(key)
        base_key = key[0]
        text, heading, keep = _merge_section(
            run,
            unit,
            b,
            lu,
            ru,
            no_base,
            base_key in append,
            base_key in replace,
        )
        if keep:
            out.append(heading + text)
    return "".join(out)


def _merge_section(
    run: _Run,
    unit: str,
    b: _Unit | None,
    lu: _Unit | None,
    ru: _Unit | None,
    no_base: bool,
    is_append: bool,
    is_replace: bool,
) -> tuple[str, str, bool]:
    """Merge one section. Returns (text, heading, kept)."""
    if lu is None and ru is None:
        run.settle(unit, R_DELETED_BOTH)
        return "", "", False
    lv = None if lu is None else lu.heading + lu.text
    rv = None if ru is None else ru.heading + ru.text
    bv = None if b is None else b.heading + b.text
    # Rule: agreement, or a change on one side only.
    if lv == rv:
        run.settle(unit, R_UNCHANGED if lv == bv else R_IDENTICAL)
        assert ru is not None
        return ru.text, ru.heading, True
    if not no_base:
        if lv == bv:
            run.settle(unit, R_REMOTE_ONLY)
            return (ru.text, ru.heading, True) if ru else ("", "", False)
        if rv == bv:
            run.settle(unit, R_LOCAL_ONLY)
            return (lu.text, lu.heading, True) if lu else ("", "", False)
    # Both sides changed this section, differently.
    if lu is None or ru is None:
        # One side deleted the section while the other edited it: contested.
        # The surviving text stays; nothing is dropped silently.
        run.contest(unit)
        keep = ru if ru is not None else lu
        assert keep is not None
        return keep.text, keep.heading, True
    # A heading both sides re-worded differently cannot be settled by a rule.
    heading = ru.heading
    heading_clash = False
    if not no_base and b is not None:
        if lu.heading == ru.heading:
            heading = ru.heading
        elif lu.heading == b.heading:
            heading = ru.heading
        elif ru.heading == b.heading:
            heading = lu.heading
        else:
            heading_clash = True
    elif lu.heading != ru.heading:
        heading_clash = True
    if heading_clash:
        run.contest(unit)
        return ru.text, ru.heading, True
    base_text = None if (no_base or b is None) else b.text
    text = _merge_text(run, unit, base_text, lu.text, ru.text, no_base, is_append, is_replace)
    return text, heading, True


def _merge_text(
    run: _Run,
    unit: str,
    base: str | None,
    local: str,
    remote: str,
    no_ancestor: bool,
    is_append: bool,
    is_replace: bool,
) -> str:
    """Merge the text of one unit that both sides have, where it differs."""
    b = "" if base is None else base
    if local == remote:
        run.settle(unit, R_UNCHANGED if local == base else R_IDENTICAL)
        return remote
    if not no_ancestor:
        if local == b:
            run.settle(unit, R_REMOTE_ONLY)
            return remote
        if remote == b:
            run.settle(unit, R_LOCAL_ONLY)
            return local
    # Rule: a replace_sections section changed differently is contested.
    if is_replace:
        run.contest(unit)
        return remote
    # Rule: an append_sections section merges lines: the ancestor's order, then
    # remote additions, then local additions, duplicates dropped. Without an
    # ancestor there is nothing to tell an addition from a removal.
    if is_append and not no_ancestor:
        run.settle(unit, R_APPEND)
        return _append_merge(b, local, remote)
    if no_ancestor:
        run.contest(unit)
        return remote
    # Rule: lines changed in different places both apply; changes that touch
    # the same lines are contested (never written with conflict markers).
    merged = _line_merge(b, local, remote)
    if merged is None:
        run.contest(unit)
        return remote
    run.settle(unit, R_LINE_MERGE)
    return merged


def _append_merge(base: str, local: str, remote: str) -> str:
    def lines(text: str) -> list[str]:
        return [ln.rstrip() for ln in text.splitlines() if ln.strip()]

    b_l, l_l, r_l = lines(base), lines(local), lines(remote)
    l_norm = {norm_line(x) for x in l_l}
    r_norm = {norm_line(x) for x in r_l}
    b_norm = {norm_line(x) for x in b_l}
    out: list[str] = []
    seen: set[str] = set()
    # An ancestor line either side dropped stays dropped.
    for ln in b_l:
        n = norm_line(ln)
        if n in l_norm and n in r_norm and n not in seen:
            seen.add(n)
            out.append(ln)
    for ln in r_l + l_l:
        n = norm_line(ln)
        if n and n not in b_norm and n not in seen:
            seen.add(n)
            out.append(ln)
    return "\n" + "\n".join(out) + "\n" if out else "\n"


Hunk = tuple[int, int, list[str]]  # (start, end) in the ancestor, replacement lines


def _hunks(base: list[str], side: list[str]) -> list[Hunk]:
    sm = difflib.SequenceMatcher(None, base, side, autojunk=False)
    return [(i1, i2, side[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _overlap(a: Hunk, b: Hunk) -> bool:
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    if lo < hi:
        return True
    # An insertion (empty range) touching or inside the other change.
    return (a[0] == a[1] or b[0] == b[1]) and lo <= hi


def _line_merge(base: str, local: str, remote: str) -> str | None:
    """Diff3-style merge; ``None`` when the two sides change the same lines."""
    b = base.splitlines(keepends=True)
    lh = _hunks(b, local.splitlines(keepends=True))
    rh = _hunks(b, remote.splitlines(keepends=True))
    for x in lh:
        for y in rh:
            if _overlap(x, y) and x != y:
                return None
    # Identical hunks on both sides apply once.
    pending = sorted(lh + [y for y in rh if y not in lh], key=lambda h: (h[0], h[1]))
    out: list[str] = []
    pos = 0
    for start, end, repl in pending:
        out += b[pos:start]
        out += repl
        pos = end
    out += b[pos:]
    return "".join(out)
