# SPDX-License-Identifier: Apache-2.0
"""First-class parent/child page relation (issue athenaeum#2535, slice R1 of
athenaeum#2525's page-recompile design) — L3 service.

Operator ruling on athenaeum#2525: the parent/sub-page link must be a GENERAL
relation — settable by the recompile pass, by a fold decision, or by hand —
and not only a by-product of splitting. Before this module the only
page-to-page roles the engine wrote into ``related`` were ``split-into``/
``split-from`` (``tiers.py:_build_split_child_pages``), and nothing read
them.

**Shape (AC1).** The child carries a top-level ``parent: <uid>`` key. The
parent carries one ``related`` row per child of the form
``{uid, role: "child", via}``, where ``via`` is one of :data:`VALID_VIA`.

**One writer, one remover (AC2).** :func:`set_relation` sets the relation;
:func:`remove_relation` removes it. Each writes both pages in one
all-or-nothing operation via :func:`_write_both_or_rollback` — if the second
write raises, the first page is restored to its own pre-call bytes before
the exception propagates, mirroring the atomicity contract the retired
reactive split (``tiers.py:_perform_oversize_page_split``, deleted by
issue athenaeum#2550) gave its own children: both new pages are computed
in full (:func:`read_relations`,
:func:`apply_breadcrumb`, :func:`apply_sub_pages_section`) before either
file on disk is touched.

**Invariants (AC3).** :func:`set_relation` refuses, with a reason and no
write: a second (different) parent for a child that already has one; a
cycle; a third nesting level (the proposed parent already has its own
parent, or the proposed child already has children of its own); an
inactive (tombstoned, retired, or superseded — :func:`is_inactive_relation_target`)
parent. A repeat call with the SAME ``(child, parent, via)`` triple is a
byte-for-byte no-op (AC5), not a "second parent" refusal — that check only
fires when the child's EXISTING parent differs from the one being set.

**Legacy read-compat, no migration (AC4).** :func:`read_relations` is the
ONE reader: given either page's frontmatter dict, it normalizes the two
pre-existing split-only roles to this relation's shape — a parent-side
``related`` row with ``role: "split-into"`` reads as a child with
``via: "split"``; a child-side ``related`` row with ``role: "split-from"``
reads as that page's parent with ``via: "split"``. No corpus migration runs
anywhere in this module. A legacy row is rewritten to the new shape on disk
only as a side effect of :func:`set_relation`/:func:`remove_relation`
actually touching that page (both rebuild a page's FULL current child list
from :func:`read_relations`, so every sibling child row on a touched parent
page is re-serialized in the new shape, not just the one being changed) —
exactly the Plan's "rewrite a legacy row to the new shape only when the
writer next touches that page," never a bulk pass.

**Idempotent rendering (AC5).** :func:`apply_breadcrumb` prepends a
first-line breadcrumb to the child's body; :func:`apply_sub_pages_section`
sets/replaces a "Sub-pages" section on the parent's body listing each child
as a wikilink with its one-line ``description:``. Both detect their own
previously-written marker and replace it in place, so a second write of the
same relation reproduces the identical bytes. An attached page's body is
otherwise unchanged — only the delimited region moves.

**CLI / doctor (AC6/AC7).** ``athenaeum relate`` (:mod:`athenaeum._cmd_relate`)
is the hand-operated front door onto :func:`set_relation`/:func:`remove_relation`
(always ``via="manual"``). :func:`count_one_sided_relations` is the read-only
counter ``athenaeum doctor`` (:mod:`athenaeum.doctor`) reports.

**Out of scope (per the issue).** Recall/listing behaviour, the merge-queue
verdict, recompile planning, and any bulk migration of existing rows — none
of those are touched here.

Layering: L3 service. Imports only :mod:`athenaeum.atomic_io` (L0) and
:mod:`athenaeum.models` (L1) — both well below every NAMED caller this
relation is meant to serve: ``tiers.py``'s recompile pass, the merge
resolver (``merge.py``/``wiki_dedupe.py``), and ``cli.py``'s ``_cmd_*``
layer (L5). Placing this module below all three means none of them has to
import another just to reach the relation primitives; it also means this
module itself never imports any of them (no edge exists to check for a
cycle). Nothing above L3 is imported here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import (
    is_tombstone,
    parse_frontmatter,
    parse_superseded_by,
    render_frontmatter,
)

#: The four origins a relation can be set from (issue athenaeum#2535 AC1).
VALID_VIA: frozenset[str] = frozenset({"split", "attach", "fold", "manual"})

#: The new-shape role a parent's ``related`` row carries for a child.
ROLE_CHILD = "child"

#: Legacy roles (issue athenaeum#1248, ``tiers.py:_build_split_child_pages``):
#: the PARENT's row naming a child, and the CHILD's row naming its parent.
#: Read-compat only (AC4) -- nothing writes these roles any more.
_LEGACY_ROLE_PARENT_NAMES_CHILD = "split-into"
_LEGACY_ROLE_CHILD_NAMES_PARENT = "split-from"


class RelationError(Exception):
    """A relation write was refused; nothing was written (issue athenaeum#2535 AC3)."""


@dataclass(frozen=True)
class ParentRef:
    """A child page's reference to its parent, as read by :func:`read_relations`."""

    uid: str
    via: str | None = None


@dataclass(frozen=True)
class ChildRef:
    """A parent page's reference to one child, as read by :func:`read_relations`."""

    uid: str
    via: str


@dataclass(frozen=True)
class PageRelations:
    """Both directions of the relation, read off ONE page's frontmatter."""

    parent: ParentRef | None
    children: tuple[ChildRef, ...]


def read_relations(meta: Mapping[str, object]) -> PageRelations:
    """The ONE reader for the parent/child relation (issue athenaeum#2535 AC4).

    Reads both directions off *meta* (one page's frontmatter dict),
    normalizing the two legacy split-only roles to this relation's shape
    in the SAME place so the translation cannot drift between a
    "what is my parent" read and a "what are my children" read:

    - A top-level ``parent:`` key (new shape) names this page's parent; its
      ``via`` is unknown from the child side alone (only the parent's own
      ``related`` row records ``via`` in the new shape), so it is left
      ``None`` here.
    - Lacking that, a ``related`` row with ``role: "split-from"`` (legacy)
      names this page's parent with ``via="split"``.
    - A ``related`` row with ``role: "child"`` (new shape) names one child,
      with its own recorded ``via``.
    - A ``related`` row with ``role: "split-into"`` (legacy) names one
      child with ``via="split"``.

    Any other ``related`` row (a different role entirely, or a malformed
    entry) is ignored here -- callers that rewrite ``related`` are
    responsible for preserving those rows verbatim (see
    :func:`set_relation`/:func:`remove_relation`).
    """
    parent: ParentRef | None = None
    raw_parent = meta.get("parent")
    if isinstance(raw_parent, str) and raw_parent:
        parent = ParentRef(uid=raw_parent)

    children: list[ChildRef] = []
    raw_related = meta.get("related")
    rows = raw_related if isinstance(raw_related, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        uid = row.get("uid")
        if not isinstance(uid, str) or not uid:
            continue
        role = row.get("role")
        if role == ROLE_CHILD:
            via = row.get("via")
            via_value = via if isinstance(via, str) and via else "manual"
            children.append(ChildRef(uid=uid, via=via_value))
        elif role == _LEGACY_ROLE_PARENT_NAMES_CHILD:
            children.append(ChildRef(uid=uid, via="split"))
        elif role == _LEGACY_ROLE_CHILD_NAMES_PARENT and parent is None:
            parent = ParentRef(uid=uid, via="split")

    return PageRelations(parent=parent, children=tuple(children))


def is_inactive_relation_target(meta: Mapping[str, object]) -> bool:
    """True when *meta* names a tombstoned, retired, or superseded page --
    the three inactive states issue athenaeum#2535 AC3 names for refusing a
    parent attach.

    Deliberately narrower than :func:`athenaeum.models.is_inactive_memory`
    (which also treats a truthy ``deprecated`` flag or a past-``valid_until``
    claim window as inactive): those describe a stale MEMORY claim, not a
    withdrawn PAGE, and the issue names exactly these three page states.
    """
    if is_tombstone(meta):
        return True
    if bool(meta.get("retired")):
        return True
    if parse_superseded_by(meta):
        return True
    return False


# ---------------------------------------------------------------------------
# Wiki-wide lookup helpers (read-only).
# ---------------------------------------------------------------------------


def _discover_wiki_pages(wiki_root: Path) -> list[Path]:
    """Every ``.md`` page directly under *wiki_root*, infra ledgers
    (``_``-prefixed) excluded -- the same shallow-scan convention several
    other domain modules each keep their own copy of (e.g.
    :func:`athenaeum.retire_pages.resolve_uids`)."""
    if not wiki_root.is_dir():
        return []
    return sorted(p for p in wiki_root.glob("*.md") if p.is_file() and not p.name.startswith("_"))


def _index_pages_by_uid(wiki_root: Path) -> dict[str, Path]:
    """uid -> path for every page with a string uid. The first page found
    (sorted-path order) wins a duplicate uid -- an ambiguous uid is an
    existing-corpus problem outside this module's job to adjudicate."""
    index: dict[str, Path] = {}
    for path in _discover_wiki_pages(wiki_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        uid = meta.get("uid")
        if isinstance(uid, str) and uid and uid not in index:
            index[uid] = path
    return index


def _read_meta(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    meta, _ = parse_frontmatter(text)
    return meta


def _ancestor_uids(index: Mapping[str, Path], start_meta: Mapping[str, object]) -> list[str]:
    """Walk UP the parent chain from *start_meta*, returning the visited
    ancestor uids in order. Stops at a missing page, an already-visited uid
    (defensive against pre-existing cyclic data on disk), or a sane depth
    bound -- this relation caps real trees at depth 2, so a genuine chain
    never approaches the bound."""
    chain: list[str] = []
    seen: set[str] = set()
    meta: Mapping[str, object] = start_meta
    for _ in range(8):
        rel = read_relations(meta)
        if rel.parent is None or rel.parent.uid in seen:
            break
        seen.add(rel.parent.uid)
        chain.append(rel.parent.uid)
        path = index.get(rel.parent.uid)
        if path is None:
            break
        meta = _read_meta(path)
    return chain


def _is_child_relation_row(row: object) -> bool:
    """True iff *row* is a parent-side relation row (new shape ``role:
    "child"`` or legacy ``role: "split-into"``) naming ANY child -- the
    exact set of rows :func:`set_relation`/:func:`remove_relation` own and
    fully rebuild from :func:`read_relations`'s own ``children`` list on
    every write, so filtering by uid here would under-strip: a row for a
    DIFFERENT existing child is reconstructed via that rebuild too, and
    keeping its OLD row here as well would duplicate it (issue athenaeum#2535,
    found while adding a regression test: attaching a second child to an
    already-populated parent produced two rows for the first child). A row
    naming a child uid under a different, non-relation role is left
    untouched -- this module only owns rows shaped like this one."""
    return isinstance(row, dict) and row.get("role") in (
        ROLE_CHILD,
        _LEGACY_ROLE_PARENT_NAMES_CHILD,
    )


def _related_rows(meta: Mapping[str, object]) -> list[object]:
    """``meta["related"]`` narrowed to a list (``[]`` for anything else)."""
    raw = meta.get("related")
    return raw if isinstance(raw, list) else []


def _clear_child_parent_pointer(meta: Mapping[str, object], parent_uid: str) -> dict[str, object]:
    """Return a COPY of *meta* with this child's own reference to
    *parent_uid* fully cleared -- both shapes (issue athenaeum#2535 review
    finding: a prior version only popped the new-shape top-level
    ``parent:`` key, leaving a legacy ``split-from`` row -- and therefore
    the relation itself -- intact on a page that had never been touched
    by this module before).

    Clears the top-level ``parent:`` key (new shape) AND any ``related``
    row with ``role: "split-from"`` naming *parent_uid* (legacy shape,
    issue athenaeum#1248). Any OTHER ``related`` row is preserved
    untouched."""
    new_meta = dict(meta)
    new_meta.pop("parent", None)
    rows = _related_rows(meta)
    if rows:
        remaining = [
            row
            for row in rows
            if not (
                isinstance(row, dict)
                and row.get("uid") == parent_uid
                and row.get("role") == _LEGACY_ROLE_CHILD_NAMES_PARENT
            )
        ]
        if remaining:
            new_meta["related"] = remaining
        else:
            new_meta.pop("related", None)
    return new_meta


def _children_display_rows(
    index: Mapping[str, Path], children: list[ChildRef]
) -> list[tuple[str, str, str]]:
    """``(uid, name, description)`` per child, read fresh off disk, for
    :func:`render_sub_pages_block`. Falls back to the uid itself as the
    display name when the child page is missing or carries no ``name:``."""
    rows: list[tuple[str, str, str]] = []
    for child in children:
        name = child.uid
        description = ""
        path = index.get(child.uid)
        if path is not None:
            meta = _read_meta(path)
            name = str(meta.get("name") or child.uid)
            description = str(meta.get("description") or "")
        rows.append((child.uid, name, description))
    return rows


# ---------------------------------------------------------------------------
# Idempotent body rendering (AC5).
# ---------------------------------------------------------------------------

_BREADCRUMB_MARKER = "<!-- athenaeum:page-relations:breadcrumb -->"
_SUBPAGES_BEGIN = "<!-- athenaeum:page-relations:sub-pages:begin -->"
_SUBPAGES_END = "<!-- athenaeum:page-relations:sub-pages:end -->"

#: Anchored to the very start of the body -- a breadcrumb is a FIRST-line
#: fact, never a mid-body match on a coincidentally similar line.
_BREADCRUMB_LINE_RE = re.compile(r"\A" + re.escape(_BREADCRUMB_MARKER) + r"[^\n]*\n\n?")
_SUBPAGES_BLOCK_RE = re.compile(
    re.escape(_SUBPAGES_BEGIN) + r".*?" + re.escape(_SUBPAGES_END) + r"\n?", re.DOTALL
)


def render_breadcrumb(parent_uid: str, parent_name: str) -> str:
    """One marker-prefixed line naming *parent_uid* as this page's parent."""
    return f"{_BREADCRUMB_MARKER} Part of: [[{parent_uid}|{parent_name}]]\n"


def apply_breadcrumb(body: str, parent_uid: str, parent_name: str) -> str:
    """Idempotently set *body*'s first-line breadcrumb (issue athenaeum#2535
    AC5). Detects and replaces a breadcrumb this function previously wrote;
    a second call with the SAME parent reproduces *body* byte-for-byte. The
    rest of *body* -- everything after the breadcrumb and its separating
    blank line -- is never touched."""
    rest = _BREADCRUMB_LINE_RE.sub("", body, count=1)
    return render_breadcrumb(parent_uid, parent_name) + "\n" + rest


def strip_breadcrumb(body: str) -> str:
    """Remove a previously-set breadcrumb (used by :func:`remove_relation`).
    A no-op on a body that never carried one."""
    return _BREADCRUMB_LINE_RE.sub("", body, count=1)


def render_sub_pages_block(children: list[tuple[str, str, str]]) -> str:
    """The marker-delimited "Sub-pages" section for *children*
    (``(uid, name, description)`` triples, issue athenaeum#2535 AC5)."""
    lines = [_SUBPAGES_BEGIN, "", "## Sub-pages", ""]
    for uid, name, description in children:
        suffix = f" — {description}" if description else ""
        lines.append(f"- [[{uid}|{name}]]{suffix}")
    lines.append("")
    lines.append(_SUBPAGES_END)
    return "\n".join(lines) + "\n"


def apply_sub_pages_section(body: str, children: list[tuple[str, str, str]]) -> str:
    """Idempotently set, replace, or remove *body*'s "Sub-pages" section
    (issue athenaeum#2535 AC5). An empty *children* removes the section
    entirely (used when a parent's last child is detached); replacing with
    the SAME *children* reproduces *body* byte-for-byte. Everything outside
    the delimited block is never touched."""
    if not children:
        return _SUBPAGES_BLOCK_RE.sub("", body)
    block = render_sub_pages_block(children)
    if _SUBPAGES_BLOCK_RE.search(body):
        # A callable replacement, not the raw string -- `re.sub` treats a
        # STRING replacement as a template and interprets `\1`/`\g<...>`/a
        # stray trailing backslash in it (issue athenaeum#2535 review
        # finding): a child `name:`/`description:` containing a backslash
        # (a Windows path, a LaTeX fragment) would raise `re.error` or
        # silently mangle the render. A callable's return value is spliced
        # in verbatim, with no escape processing at all.
        return _SUBPAGES_BLOCK_RE.sub(lambda _match: block, body)
    sep = "" if body.endswith("\n\n") else "\n" if body.endswith("\n") else "\n\n"
    return body + sep + block


# ---------------------------------------------------------------------------
# The one writer, the one remover (AC2/AC3).
# ---------------------------------------------------------------------------


def _write_both_or_rollback(path_a: Path, text_a: str, path_b: Path, text_b: str) -> None:
    """Write *path_a* then *path_b*, atomically (:func:`athenaeum.atomic_io.atomic_write_text`).
    If writing *path_b* raises, *path_a* is restored to its own pre-call
    bytes before the exception propagates -- a caller never observes a
    half-applied relation (issue athenaeum#2535 AC2)."""
    original_a = path_a.read_text(encoding="utf-8")
    atomic_write_text(path_a, text_a)
    try:
        atomic_write_text(path_b, text_b)
    except Exception:
        atomic_write_text(path_a, original_a)
        raise


def set_relation(wiki_root: Path, *, child_uid: str, parent_uid: str, via: str) -> None:
    """Set the parent/child relation between *child_uid* and *parent_uid*
    (issue athenaeum#2535 AC1-AC3/AC5), writing both pages in one
    all-or-nothing operation.

    Refuses -- raising :class:`RelationError`, writing nothing -- when:
    *child_uid* already has a DIFFERENT parent; the relation would form a
    cycle; the resulting tree would be three levels deep (the proposed
    parent already has its own parent, or the proposed child already has
    children); or the proposed parent is inactive (tombstoned, retired, or
    superseded). A repeat call with the SAME ``(child_uid, parent_uid, via)``
    triple is a byte-for-byte no-op, not a "second parent" refusal.

    Every OTHER child already recorded on *parent_uid* (new-shape or
    legacy ``split-into``) is re-serialized in the new shape as part of
    this same write -- the Plan's "rewrite a legacy row to the new shape
    only when the writer next touches that page," applied naturally by
    rebuilding the full current child list from :func:`read_relations`
    rather than patching one row in place.
    """
    if via not in VALID_VIA:
        raise ValueError(f"via must be one of {sorted(VALID_VIA)}, got {via!r}")

    index = _index_pages_by_uid(wiki_root)
    child_path = index.get(child_uid)
    parent_path = index.get(parent_uid)
    if child_path is None:
        raise RelationError(f"unknown child uid: {child_uid!r}")
    if parent_path is None:
        raise RelationError(f"unknown parent uid: {parent_uid!r}")

    child_meta = _read_meta(child_path)
    parent_meta = _read_meta(parent_path)
    child_body = parse_frontmatter(child_path.read_text(encoding="utf-8"))[1]
    parent_body = parse_frontmatter(parent_path.read_text(encoding="utf-8"))[1]

    if child_uid == parent_uid or child_uid in _ancestor_uids(index, parent_meta):
        raise RelationError(f"setting {parent_uid} as {child_uid}'s parent would form a cycle")

    if is_inactive_relation_target(parent_meta):
        raise RelationError(
            f"{parent_uid} is inactive (tombstoned, retired, or superseded) "
            "and cannot be a parent"
        )

    existing_child_rel = read_relations(child_meta)
    if existing_child_rel.parent is not None and existing_child_rel.parent.uid != parent_uid:
        raise RelationError(
            f"{child_uid} already has a parent ({existing_child_rel.parent.uid}); "
            "detach it first"
        )

    existing_parent_rel = read_relations(parent_meta)
    if existing_parent_rel.parent is not None:
        raise RelationError(
            f"{parent_uid} already has its own parent "
            f"({existing_parent_rel.parent.uid}); a third nesting level is not allowed"
        )
    if existing_child_rel.children:
        raise RelationError(
            f"{child_uid} already has children of its own; a third nesting level "
            "is not allowed"
        )

    # Everything below is pure computation -- no file on disk is touched
    # until _write_both_or_rollback, mirroring the retired reactive split's
    # (tiers.py:_perform_oversize_page_split, deleted by issue
    # athenaeum#2550) "compute both pages fully before any write"
    # discipline.
    #
    # _clear_child_parent_pointer also drops a pre-existing legacy
    # split-from row naming this same parent -- a child moving from the
    # legacy shape to the new one (e.g. re-affirmed with via="manual")
    # must not keep BOTH the old row and the new top-level key.
    new_child_meta = _clear_child_parent_pointer(child_meta, parent_uid)
    new_child_meta["parent"] = parent_uid

    other_rows = [row for row in _related_rows(parent_meta) if not _is_child_relation_row(row)]
    children_after = [c for c in existing_parent_rel.children if c.uid != child_uid]
    children_after.append(ChildRef(uid=child_uid, via=via))
    children_after.sort(key=lambda c: c.uid)
    new_related = other_rows + [
        {"uid": c.uid, "role": ROLE_CHILD, "via": c.via} for c in children_after
    ]
    new_parent_meta = dict(parent_meta)
    new_parent_meta["related"] = new_related

    parent_name = str(parent_meta.get("name") or parent_uid)
    new_child_body = apply_breadcrumb(child_body, parent_uid, parent_name)

    display_rows = _children_display_rows(index, children_after)
    new_parent_body = apply_sub_pages_section(parent_body, display_rows)

    new_child_text = render_frontmatter(new_child_meta) + "\n" + new_child_body
    new_parent_text = render_frontmatter(new_parent_meta) + "\n" + new_parent_body

    _write_both_or_rollback(child_path, new_child_text, parent_path, new_parent_text)


def remove_relation(wiki_root: Path, *, child_uid: str) -> None:
    """Remove the parent/child relation naming *child_uid* as the child
    (issue athenaeum#2535 AC2), writing both pages in one all-or-nothing
    operation. Raises :class:`RelationError` -- no write -- when
    *child_uid* names no page, or the page has no parent to detach.

    If the parent page named by *child_uid*'s own ``parent:`` key no
    longer exists on disk, only the child's dangling pointer is cleared --
    there is no second page left to keep in sync.
    """
    index = _index_pages_by_uid(wiki_root)
    child_path = index.get(child_uid)
    if child_path is None:
        raise RelationError(f"unknown child uid: {child_uid!r}")

    child_meta = _read_meta(child_path)
    child_body = parse_frontmatter(child_path.read_text(encoding="utf-8"))[1]
    rel = read_relations(child_meta)
    if rel.parent is None:
        raise RelationError(f"{child_uid} has no parent to detach")
    parent_uid = rel.parent.uid

    # _clear_child_parent_pointer drops BOTH the new-shape top-level
    # `parent:` key and a legacy `split-from` row naming this parent --
    # review finding: a prior version only popped the former, leaving a
    # legacy pair half-detached (the child still linked to the parent via
    # its own `related` row, read back by read_relations as still having
    # a parent, even though the parent side was cleared).
    new_child_meta = _clear_child_parent_pointer(child_meta, parent_uid)
    new_child_body = strip_breadcrumb(child_body)
    new_child_text = render_frontmatter(new_child_meta) + "\n" + new_child_body

    parent_path = index.get(parent_uid)
    if parent_path is None:
        atomic_write_text(child_path, new_child_text)
        return

    parent_meta = _read_meta(parent_path)
    parent_body = parse_frontmatter(parent_path.read_text(encoding="utf-8"))[1]

    other_rows = [row for row in _related_rows(parent_meta) if not _is_child_relation_row(row)]
    remaining = [c for c in read_relations(parent_meta).children if c.uid != child_uid]
    new_related = other_rows + [
        {"uid": c.uid, "role": ROLE_CHILD, "via": c.via} for c in remaining
    ]
    new_parent_meta = dict(parent_meta)
    if new_related:
        new_parent_meta["related"] = new_related
    else:
        new_parent_meta.pop("related", None)

    display_rows = _children_display_rows(index, remaining)
    new_parent_body = apply_sub_pages_section(parent_body, display_rows)
    new_parent_text = render_frontmatter(new_parent_meta) + "\n" + new_parent_body

    _write_both_or_rollback(child_path, new_child_text, parent_path, new_parent_text)


# ---------------------------------------------------------------------------
# athenaeum doctor's count (AC7).
# ---------------------------------------------------------------------------


def count_one_sided_relations(wiki_root: Path) -> int:
    """Count pairs where only ONE side records the parent/child relation
    (issue athenaeum#2535 AC7): a ``parent:`` key with no matching
    ``related`` row on the named parent, or the reverse. Read-only --
    counts only, no further diagnosis."""
    index = _index_pages_by_uid(wiki_root)
    metas: dict[str, dict[str, object]] = {uid: _read_meta(path) for uid, path in index.items()}

    pairs: set[tuple[str, str]] = set()
    for uid, meta in metas.items():
        rel = read_relations(meta)
        if rel.parent is not None:
            pairs.add((rel.parent.uid, uid))
        for child in rel.children:
            pairs.add((uid, child.uid))

    one_sided = 0
    for parent_uid, child_uid in pairs:
        child_meta = metas.get(child_uid)
        parent_meta = metas.get(parent_uid)
        child_rel = read_relations(child_meta) if child_meta is not None else None
        parent_rel = read_relations(parent_meta) if parent_meta is not None else None
        child_says_parent = bool(
            child_rel is not None
            and child_rel.parent is not None
            and child_rel.parent.uid == parent_uid
        )
        parent_says_child = bool(
            parent_rel is not None and any(c.uid == child_uid for c in parent_rel.children)
        )
        if child_says_parent != parent_says_child:
            one_sided += 1
    return one_sided


# ---------------------------------------------------------------------------
# Recall/listing read-side helpers (issue athenaeum#2538, slice R2).
# ---------------------------------------------------------------------------


def resolve_page_meta_by_uid(wiki_root: Path, uid: str) -> dict[str, object] | None:
    """Best-effort lookup of one page's frontmatter by its ``uid:`` field.

    A single targeted glob (``<uid>*.md``), never a corpus-wide index build
    (:func:`_index_pages_by_uid` reads EVERY page -- fine for a one-shot
    ``count_one_sided_relations`` sweep, too heavy for a per-hit relation-
    line resolution called from a hot path). Tries both filename shapes a
    wiki page can take -- an exact ``<uid>.md`` stem, or the real writer's
    ``<uid>-<slug>.md`` convention (:meth:`athenaeum.models.WikiEntity.filename`)
    -- and verifies the candidate's OWN ``uid:`` before accepting a prefix
    match (an exact stem match is accepted by construction, mirroring
    :class:`athenaeum.mcp_server._LinkNameIndex`'s identical rule for
    outbound-link targets).

    Returns ``None`` when *wiki_root* is not a directory, *uid* is empty, or
    no candidate resolves -- a dangling relation target (a parent/child uid
    naming a page no longer on disk) renders exactly like an unresolved
    outbound wikilink elsewhere in this codebase: never an error.
    """
    if not uid or not wiki_root.is_dir():
        return None
    try:
        candidates = sorted(wiki_root.glob(f"{uid}*.md"))
    except OSError:
        return None
    for path in candidates:
        if path.stem != uid and not path.stem.startswith(f"{uid}-"):
            continue
        meta = _read_meta(path)
        if path.stem == uid or str(meta.get("uid") or "") == uid:
            return meta
    return None


def is_hidden_split_child(meta: Mapping[str, object]) -> bool:
    """True iff *meta* is a split-created sub-page, hidden from entity
    listings/enumeration by default (issue athenaeum#2538 AC7).

    Reads *meta*'s OWN relation via :func:`read_relations` -- the one
    reader -- and checks only ``via == "split"``. Deliberately narrower
    than "has a parent": a page attached under a parent via ``attach``,
    ``fold``, or ``manual`` is -- per the operator ruling this issue
    implements -- "a real entity of its own type" and stays listed; only a
    split-created sub-page (the corpus's oversize-page-split machinery,
    :func:`athenaeum.tiers._build_split_child_pages`, which writes the
    LEGACY ``split-from`` row :func:`read_relations` normalizes to
    ``via="split"`` directly off the child's own frontmatter -- no second
    read needed) is hidden.
    """
    rel = read_relations(meta)
    return rel.parent is not None and rel.parent.via == "split"
