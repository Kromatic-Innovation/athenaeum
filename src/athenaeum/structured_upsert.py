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
    resolve_structured_upsert,
)
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.schemas import validate_wiki_meta

if TYPE_CHECKING:
    from athenaeum.models import EntityIndex, RawFile

log = logging.getLogger(__name__)

STATUS_APPLIED = "applied"
STATUS_NOOP = "noop"
STATUS_REFUSED = "refused"

#: ``skipped`` entries recorded for a refused raw are ``structured-upsert-<reason>``.
REASON_TYPE_MISMATCH = "type-mismatch"
REASON_INELIGIBLE = "ineligible"
REASON_UNRESOLVED_UID = "unresolved-uid"
REASON_UNAPPLIABLE = "unappliable"

_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
_BULLET_RE = re.compile(r"^[-*+][ \t]+")


@dataclass(frozen=True)
class StructuredUpsertOutcome:
    """What :func:`tier0_structured_upsert` did with a configured-type raw."""

    status: str
    uid: str = ""
    name: str = ""
    reason: str = ""

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


def _ends_in_open_fence(body: str) -> bool:
    in_fence = False
    for line in body.replace("\r\n", "\n").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
    return in_fence


def _split_sections(body: str) -> tuple[str, list[_Section]]:
    """Split *body* into (preamble, H2 sections), ignoring ``##`` inside fences."""
    preamble: list[str] = []
    sections: list[_Section] = []
    in_fence = False
    cur: _Section | None = None
    for line in body.replace("\r\n", "\n").splitlines(keepends=True):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _H2_RE.match(line.rstrip("\n"))
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
    loose = [
        ln for ln in raw_preamble.splitlines() if ln.strip() and not ln.lstrip().startswith("# ")
    ]
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
    """True when the raw ranks below the page on any ordered key."""
    for key, ranks in spec.ordered_keys:
        incoming, current = str(raw.get(key, "")), str(existing.get(key, ""))
        if incoming in ranks and current in ranks and ranks.index(incoming) < ranks.index(current):
            return True
    return False


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
        have = str(merged.get(key, ""))
        current_rank = ranks.index(have) if have in ranks else -1
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

    def refuse(reason: str, detail: str) -> StructuredUpsertOutcome:
        log.warning(
            "  T0 structured-upsert: raw %s (type %s, uid %r) refused: %s; "
            "leaving the raw on disk, not falling through to the LLM tiers",
            raw.ref,
            etype,
            uid,
            detail,
        )
        return StructuredUpsertOutcome(STATUS_REFUSED, uid, name, reason)

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
