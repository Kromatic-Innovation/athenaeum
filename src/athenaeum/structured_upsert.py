# SPDX-License-Identifier: Apache-2.0
"""Tier-0 structured upsert for configured entity types (issue athenaeum#2360).

A pre-structured raw whose ``type`` is listed under
``librarian.structured_upsert`` and whose ``uid`` already resolves to a wiki
page is merged onto that page deterministically: no LLM, no model-authored
text. The merge rules are pure config (see
:class:`athenaeum.config.StructuredUpsertSpec`); nothing here knows about any
particular entity type.

Why this exists: every other path that can carry a later update to a known uid
falls short for a page with a fixed H2 layout. ``tier0_passthrough`` declines
an indexed uid, ``tier0_handle_upsert`` only moves source-handle keys, and the
uid-bind path (athenaeum#1982) hands a truncated observation to a Tier-3 merge
that may not touch frontmatter and rewrites body text. This module replaces
named H2 sections, appends new lines to others, unions list keys, sets scalar
keys, and ratchets ordered keys, all under the page's own H1 and identity
frontmatter.

Layering: L2 primitive, a sibling of :mod:`athenaeum.intake`. Imports only
:mod:`athenaeum.config` (L2), :mod:`athenaeum.models` / :mod:`athenaeum.schemas`
(L1) and :mod:`athenaeum.atomic_io` (L0); imported by the L4 librarian and batch.

Lives in its own module (re-exported from :mod:`athenaeum.librarian`) so that
:mod:`athenaeum.batch` can call it without importing the librarian, the same
reason ``tier0_passthrough`` was hoisted into :mod:`athenaeum.intake`.

Contract:

- ``uid``/``type``/``created``/``access`` and the page's preamble (its H1) are
  never modified.
- No delta means no write and no ``updated`` bump: re-applying a raw is a no-op.
- A raw of a configured type that cannot be applied is REFUSED, never passed
  on: the caller records it as skipped and leaves the raw on disk, so it can
  never fall through to the uid-bind path or the LLM tiers (which would
  flatten the structure this exists to preserve).
- A raw whose value for a declared ``librarian.field_groups.<type>`` field
  collides with a different, already-held value is likewise REFUSED (issue
  athenaeum#2197, ``REASON_FIELD_GROUP_MISFIT``) -- but only when NO other
  field in the same group still agrees between the raw and the page; an
  ordinary update to the record already on the page (e.g. the same deal's
  stage moving forward while its name/amount stay put) applies normally
  instead (post-merge review of athenaeum#2197). When a misfit does fire,
  the whole raw is refused, not merged piecemeal, so neither value is
  lost. The outcome's ``field_group_misfits`` names what collided;
  :mod:`athenaeum.schema_amendment_proposals` is what turns that into a
  growing pending-decisions proposal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (
    STRUCTURED_UPSERT_PROTECTED_KEYS,
    StructuredUpsertSpec,
    resolve_page_type_field_groups,
    resolve_structured_upsert,
)
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.schemas import validate_wiki_meta

if TYPE_CHECKING:
    from athenaeum.models import EntityIndex, RawFile

log = logging.getLogger(__name__)

#: Frontmatter keys that are the page's identity: the page's own value always wins.
_IDENTITY_KEYS = STRUCTURED_UPSERT_PROTECTED_KEYS | {"name"}

STATUS_APPLIED = "applied"
STATUS_NOOP = "noop"
STATUS_REFUSED = "refused"

#: ``skipped`` entries recorded for a refused raw are ``structured-upsert-<reason>``.
REASON_TYPE_MISMATCH = "type-mismatch"
REASON_INELIGIBLE = "ineligible"
REASON_UNRESOLVED_UID = "unresolved-uid"
REASON_UNAPPLIABLE = "unappliable"
#: Issue athenaeum#2197: the raw's value for a declared single-valued field
#: group's field differs from a different value the page already holds.
#: Refused (never applied, never partially merged) so the existing page is
#: untouched and the raw stays on disk -- see ``tier0_structured_upsert``'s
#: ``field_group_misfits`` output, which names what to propose.
REASON_FIELD_GROUP_MISFIT = "field-group-misfit"

_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_BULLET_RE = re.compile(r"^[-*+][ \t]+")


@dataclass(frozen=True)
class FieldGroupMisfit:
    """One declared single-valued field-group slot a raw's value collided
    with (issue athenaeum#2197): the page already holds a DIFFERENT, non-empty
    value for *field*, and -- per :func:`_detect_field_group_misfits`'s
    group-level agreement check -- NO other field in the same group still
    matches, so this is read as a different source record's data rather
    than an ordinary update to the record already on the page.

    Pure data -- :func:`athenaeum.schema_amendment_proposals.record_field_group_misfits`
    is what turns a list of these into a growing pending-decisions proposal.
    """

    page_type: str
    field_group: str
    field: str
    existing_value: Any
    incoming_value: Any


@dataclass(frozen=True)
class StructuredUpsertOutcome:
    """What :func:`tier0_structured_upsert` did with a configured-type raw."""

    status: str
    uid: str = ""
    name: str = ""
    reason: str = ""
    #: Non-empty only for a :data:`REASON_FIELD_GROUP_MISFIT` refusal (issue
    #: athenaeum#2197) -- every declared-group field the raw's value collided
    #: with. Empty for every other status/reason, including every
    #: pre-athenaeum#2197 caller's outcome.
    field_group_misfits: tuple[FieldGroupMisfit, ...] = ()

    @property
    def skipped_label(self) -> str:
        return f"structured-upsert-{self.reason}"


@dataclass
class _Section:
    title: str
    heading: str
    text: str

    @property
    def key(self) -> str:
        return self.title.casefold()


def _advance_fence(line: str, state: tuple[str, int] | None) -> tuple[str, int] | None:
    """CommonMark fence tracking: the state after *line*.

    A fence opens on three or more backticks or tildes (a backtick opener's info
    string may not contain a backtick) and closes only on the same character, at
    least as long as the opener, with nothing after it but whitespace.
    """
    m = _FENCE_RE.match(line.rstrip("\n"))
    if m is None:
        return state
    run, rest = m.group(1), m.group(2)
    if state is None:
        if run[0] == "`" and "`" in rest:
            return None
        return (run[0], len(run))
    if run[0] == state[0] and len(run) >= state[1] and not rest.strip():
        return None
    return state


def _ends_in_open_fence(body: str) -> bool:
    state: tuple[str, int] | None = None
    for line in body.replace("\r\n", "\n").splitlines():
        state = _advance_fence(line, state)
    return state is not None


def _split_sections(body: str) -> tuple[str, list[_Section]]:
    """Split *body* into (preamble, H2 sections), ignoring ``##`` inside fences."""
    preamble: list[str] = []
    sections: list[_Section] = []
    fence: tuple[str, int] | None = None
    cur: _Section | None = None
    for line in body.replace("\r\n", "\n").splitlines(keepends=True):
        match = None if fence else _H2_RE.match(line.rstrip("\n"))
        fence = _advance_fence(line, fence)
        if match:
            cur = _Section(title=match.group(1).strip(), heading=line, text="")
            sections.append(cur)
        elif cur is None:
            preamble.append(line)
        else:
            cur.text += line
    return "".join(preamble), sections


def _norm_line(line: str) -> str:
    return _BULLET_RE.sub("", line.strip()).casefold()


def _raw_section_map(sections: list[_Section]) -> dict[str, str]:
    out: dict[str, str] = {}
    for sec in sections:
        content = sec.text.strip()
        if content:
            out[sec.key] = f"{out[sec.key]}\n{content}" if sec.key in out else content
    return out


def _merge_body(
    spec: StructuredUpsertSpec,
    page_body: str,
    raw_body: str,
    allow_replace: bool = True,
) -> str | None:
    """Merged body text, or ``None`` when the body needs no change.

    Raises :class:`ValueError` when the raw carries content nothing would
    place, since dropping it silently would lose it once the raw is consumed:
    non-empty text under an H2 the spec does not configure, non-empty text
    between the H1 and the first H2, or a body that ends inside an unclosed
    code fence (every later heading would be swallowed into the fence).

    *allow_replace* ``False`` leaves existing ``replace_sections`` untouched
    (a lower-ranked raw must not overwrite a higher-ranked page's content).
    """
    if _ends_in_open_fence(raw_body):
        raise ValueError("raw body ends inside an unclosed code fence")
    raw_preamble, raw_sections = _split_sections(raw_body)
    loose = []
    seen_h1 = False
    for ln in raw_preamble.splitlines():
        if not ln.strip():
            continue
        if not seen_h1 and ln.startswith("# "):  # only the first, unindented H1
            seen_h1 = True
        else:
            loose.append(ln)
    if loose:
        raise ValueError("raw has text between the H1 and the first H2 that no section holds")
    raw_map = _raw_section_map(raw_sections)
    canon = {title.casefold(): i for i, title in enumerate(spec.sections)}
    stray = sorted(k for k in raw_map if k not in canon)
    if stray:
        raise ValueError(f"raw has non-empty section(s) not in `sections`: {stray}")

    preamble, page_sections = _split_sections(page_body)
    changed = False
    for idx, title in enumerate(spec.sections):
        key = title.casefold()
        raw_content = raw_map.get(key, "")
        if not raw_content:
            continue
        existing = next((s for s in page_sections if s.key == key), None)
        if existing is None:
            new = _Section(title=title, heading=f"## {title}\n", text=f"\n{raw_content}\n")
            pos = None
            for j, sec in enumerate(page_sections):
                ci = canon.get(sec.key)
                if ci is not None and ci < idx:
                    pos = j + 1
            if pos is None:
                for j, sec in enumerate(page_sections):
                    ci = canon.get(sec.key)
                    if ci is not None and ci > idx:
                        pos = j
                        break
            page_sections.insert(len(page_sections) if pos is None else pos, new)
            changed = True
            continue
        current = existing.text.strip()
        if key in spec.replace_sections:
            if allow_replace and raw_content != current:
                existing.text = f"\n{raw_content}\n"
                changed = True
        elif key in spec.append_sections:
            seen = {_norm_line(ln) for ln in current.splitlines() if ln.strip()}
            fresh: list[str] = []
            for ln in raw_content.splitlines():
                n = _norm_line(ln)
                if n and n not in seen:
                    seen.add(n)
                    fresh.append(ln.rstrip())
            if fresh:
                base = f"{current}\n" if current else ""
                existing.text = f"\n{base}" + "\n".join(fresh) + "\n"
                changed = True
    if not changed:
        return None

    blocks = []
    for sec in page_sections:
        content = sec.text.strip("\n")
        head = sec.heading.rstrip("\n")
        blocks.append(f"{head}\n\n{content}\n" if content else f"{head}\n")
    pre = preamble.rstrip("\n")
    lead = f"{pre}\n\n" if pre.strip() else preamble
    return lead + "\n".join(blocks)


def _as_list(value: object) -> list[object]:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, list) else [value]


def _rank_regressed(
    spec: StructuredUpsertSpec, existing: dict[str, Any], raw: dict[str, Any]
) -> bool:
    """True when the raw ranks below the page on any ordered key.

    A raw value that is missing or not in the rank list counts as the lowest rank
    (when the page holds a ranked value), so it can never replace a ranked page's
    sections.
    """
    for key, ranks in spec.ordered_keys:
        current = str(existing.get(key, ""))
        if current not in ranks:
            continue
        incoming = str(raw.get(key, ""))
        if incoming not in ranks or ranks.index(incoming) < ranks.index(current):
            return True
    return False


def _detect_field_group_misfits(
    type_groups: dict[str, tuple[str, ...]],
    spec: StructuredUpsertSpec,
    page_type: str,
    existing: dict[str, Any],
    raw: dict[str, Any],
) -> list[FieldGroupMisfit]:
    """Declared single-valued field-group conflicts between *raw* and *existing*
    (issue athenaeum#2197; record-identity narrowing from athenaeum#2197's
    post-merge review).

    Only ``spec.set_keys`` are eligible: a field group names scalar,
    single-valued slots (the motivating case is a flat ``crm_*`` deal
    record), not the list-valued ``union_keys`` the ordinary merge already
    unions without conflict. ``type_groups`` empty (no groups declared for
    *page_type*) always returns ``[]`` regardless of field names or values --
    the "no implicit groups" guarantee lives here, not in the caller.

    A field is a CANDIDATE misfit when: it belongs to a declared group, the
    raw carries a non-empty value for it, the page already holds a non-empty
    value for it, and the two differ. Matching or absent-on-either-side
    values are never a candidate -- they are the ordinary "no delta" /
    "first value" cases ``_merge_frontmatter`` already handles correctly.

    A candidate only becomes a REPORTED misfit once per GROUP, not per
    field, because this is where record identity is decided. Neither the
    raw nor the page carries an explicit source-record id this deep in the
    pipeline -- the raw's frontmatter is limited to the identity keys plus
    whatever the spec's ``union_keys``/``set_keys``/``ordered_keys``/
    ``keep_keys`` declare (an undeclared id field is refused upstream in
    :func:`tier0_structured_upsert` as an unknown key), and
    :mod:`athenaeum.registry`'s athenaeum#453 source-handle keys identify a
    wiki ENTITY across adapters, not a sub-record within one page's field
    group. Lacking that signal, this uses the group's OWN other declared
    fields as the identity proxy the design doc already describes them as
    ("a field group names a set of scalar fields that together hold ONE
    record's worth of data"): if ANY other field in the same group has a
    non-empty value on both sides that AGREES, the raw is treated as an
    ordinary update to the record already on the page -- e.g. the same
    deal's ``crm_deal_name``/``crm_deal_amount`` staying put while only
    ``crm_deal_stage`` advances -- and no misfit is reported for ANY field
    in that group, even the ones that differ; they apply normally. Only
    when every comparable field in the group disagrees (no shared anchor)
    is the group reported as a different source record's conflicting data,
    exactly as before.

    This is deliberately the conservative side of the tradeoff: a different
    record that happens to coincidentally share one field's value with the
    page's existing record (vanishingly unlikely for the motivating case --
    two unrelated deals with an identical name AND amount) would wrongly be
    let through rather than flagged. Closing that gap would need an
    explicit per-record identity carried through the raw, which nothing in
    this pipeline provides today.
    """
    if not type_groups:
        return []
    field_to_group = {
        field_name: group_name
        for group_name, fields in type_groups.items()
        for field_name in fields
    }
    # Per group: candidate (key, existing, incoming) diffs, and whether any
    # OTHER field in the same group agrees (non-empty on both sides, equal)
    # -- the signal that this raw updates the record already on the page
    # rather than naming a different one.
    group_diffs: dict[str, list[tuple[str, Any, Any]]] = {}
    group_has_agreement: dict[str, bool] = {}
    for key in spec.set_keys:
        group_name = field_to_group.get(key)
        if group_name is None:
            continue
        incoming_value = raw.get(key)
        if incoming_value in (None, "", [], {}):
            continue
        existing_value = existing.get(key)
        if existing_value in (None, "", [], {}):
            continue
        if existing_value == incoming_value:
            group_has_agreement[group_name] = True
        else:
            group_diffs.setdefault(group_name, []).append((key, existing_value, incoming_value))

    misfits: list[FieldGroupMisfit] = []
    for group_name, diffs in group_diffs.items():
        if group_has_agreement.get(group_name):
            continue
        for key, existing_value, incoming_value in diffs:
            misfits.append(
                FieldGroupMisfit(
                    page_type=page_type,
                    field_group=group_name,
                    field=key,
                    existing_value=existing_value,
                    incoming_value=incoming_value,
                )
            )
    return misfits


def _merge_frontmatter(
    spec: StructuredUpsertSpec,
    existing: dict[str, Any],
    raw: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key in spec.union_keys:
        if key in STRUCTURED_UPSERT_PROTECTED_KEYS:
            continue
        current = _as_list(merged.get(key))
        have = {str(v) for v in current}
        added = []
        for item in _as_list(raw.get(key)):
            if str(item) not in have:
                have.add(str(item))
                added.append(item)
        if added:
            merged[key] = current + added
    for key in spec.set_keys:
        value = raw.get(key)
        if key in STRUCTURED_UPSERT_PROTECTED_KEYS or value in (None, "", [], {}):
            continue
        if merged.get(key) != value:
            merged[key] = value
    for key, ranks in spec.ordered_keys:
        value = raw.get(key)
        if key in STRUCTURED_UPSERT_PROTECTED_KEYS or value is None:
            continue
        if str(value) not in ranks:
            continue
        held = str(merged.get(key, ""))
        current_rank = ranks.index(held) if held in ranks else -1
        if ranks.index(str(value)) > current_rank:
            merged[key] = str(value)
    return merged


def tier0_structured_upsert(
    raw: RawFile,
    index: EntityIndex,
    wiki_root: Path,
    valid_types: list[str],
    config: dict[str, object] | None = None,
    dry_run: bool = False,
) -> StructuredUpsertOutcome | None:
    """Merge a configured-type raw onto the existing page its ``uid`` names.

    Returns ``None`` when this step does not apply (nothing configured, no
    parseable frontmatter, or the raw's ``type`` is not configured): the caller
    carries on exactly as it did before this step existed. Otherwise returns an
    outcome: ``applied`` (page rewritten), ``noop`` (no delta: nothing written,
    ``updated`` untouched), or ``refused`` (logged at WARNING; the caller must
    record it as skipped, leave the raw on disk, and stop).

    *wiki_root* is accepted for signature parity with the sibling tier-0 steps;
    the target path comes from *index*.
    """
    specs = resolve_structured_upsert(config)
    if not specs:
        return None
    meta, raw_body = parse_frontmatter(raw.content)
    if not meta:
        return None
    etype = str(meta.get("type", "") or "").strip()
    spec = specs.get(etype)
    if spec is None:
        return None

    uid = str(meta.get("uid", "") or "").strip()
    name = str(meta.get("name", "") or "").strip()

    def refuse(
        reason: str,
        detail: str,
        *,
        misfits: tuple[FieldGroupMisfit, ...] = (),
    ) -> StructuredUpsertOutcome:
        log.warning(
            "  T0 structured-upsert: raw %s (type %s, uid %r) refused: %s; "
            "leaving the raw on disk, not falling through to the LLM tiers",
            raw.ref,
            etype,
            uid,
            detail,
        )
        return StructuredUpsertOutcome(STATUS_REFUSED, uid, name, reason, misfits)

    if not uid or not name or etype not in valid_types:
        return refuse(REASON_INELIGIBLE, "needs a non-empty uid and name and a schema-valid type")
    page_path = index.get_by_uid(uid)
    if page_path is None or not page_path.exists():
        return refuse(REASON_UNRESOLVED_UID, "uid does not resolve to an existing page")
    existing_meta, existing_body = parse_frontmatter(page_path.read_text(encoding="utf-8"))
    if not existing_meta:
        return refuse(REASON_UNRESOLVED_UID, f"page {page_path.name} has no frontmatter")
    existing_type = str(existing_meta.get("type", "") or "").strip()
    if existing_type != etype:
        return refuse(
            REASON_TYPE_MISMATCH,
            f"page {page_path.name} is type {existing_type!r}, raw is {etype!r}",
        )

    # Every raw frontmatter key must be handled: identity keys (the page's own
    # values win; a differing raw ``name`` is ignored because name and H1 are
    # identity), or a key the spec merges (union/set/ordered) or deliberately
    # ignores (keep). Anything else would be silently lost when the raw is
    # consumed, so refuse it, naming the keys.
    known = (
        _IDENTITY_KEYS
        | set(spec.union_keys)
        | set(spec.set_keys)
        | {k for k, _ in spec.ordered_keys}
        | set(spec.keep_keys)
    )
    unknown = sorted(str(k) for k in meta if k not in known)
    if unknown:
        return refuse(
            REASON_UNAPPLIABLE,
            f"raw carries frontmatter key(s) the config neither merges nor keeps: {unknown}",
        )

    # Issue athenaeum#2197: a declared single-valued field-group conflict is
    # checked BEFORE any merge is computed -- the whole raw is refused
    # (never partially applied) so the page keeps both the existing and the
    # not-yet-adopted incoming value intact, and the raw itself (the only
    # copy of the colliding record) stays on disk rather than being
    # consumed. Types with no `librarian.field_groups` entry get an empty
    # `type_groups`, so this is a complete no-op for every pre-athenaeum#2197
    # deployment and every page type that declares no groups.
    type_groups = resolve_page_type_field_groups(config).get(etype, {})
    misfits = _detect_field_group_misfits(type_groups, spec, etype, existing_meta, meta)
    if misfits:
        return refuse(
            REASON_FIELD_GROUP_MISFIT,
            "declared field group(s) "
            f"{sorted({m.field_group for m in misfits})} already hold a "
            "different value, with no other field in the group still "
            "agreeing -- read as a different source record's data",
            misfits=tuple(misfits),
        )

    raw_name = str(meta.get("name", "") or "").strip()
    page_name = str(existing_meta.get("name", "") or "").strip()
    if raw_name != page_name:
        log.info(
            "  T0 structured-upsert: raw name %r differs from page name %r; keeping the page's",
            raw_name,
            page_name,
        )

    try:
        new_body = _merge_body(
            spec,
            existing_body,
            raw_body,
            allow_replace=not _rank_regressed(spec, existing_meta, meta),
        )
    except ValueError as exc:
        return refuse(REASON_UNAPPLIABLE, str(exc))
    merged_meta = _merge_frontmatter(spec, existing_meta, meta)
    if new_body is None and merged_meta == existing_meta:
        log.info("  T0 structured-upsert: %s (%s): no delta, no-op", name, uid)
        return StructuredUpsertOutcome(STATUS_NOOP, uid, name)

    merged_meta["updated"] = date.today().isoformat()
    try:
        validate_wiki_meta(merged_meta, declared_types=valid_types)
    except ValueError as exc:  # pydantic.ValidationError subclasses ValueError
        return refuse(REASON_UNAPPLIABLE, f"merged frontmatter fails the schema: {exc}")
    if not dry_run:
        atomic_write_text(
            page_path,
            render_frontmatter(merged_meta)
            + "\n"
            + (new_body if new_body is not None else existing_body),
        )
    log.info("  T0 structured-upsert: %s → %s (merged)", name, page_path.name)
    return StructuredUpsertOutcome(STATUS_APPLIED, uid, name)
