# SPDX-License-Identifier: Apache-2.0
"""Fold an absorbed uid's contact record into the canonical one (issue athenaeum#2184).

When a merge absorbs a person page — ``dedupe persons --apply`` unlinks it, a
``fold-into-existing`` approve tombstones it — the absorbed uid's record on the
excluded contacts surface used to stay behind, still listing the absorbed
person's addresses under the absorbed uid. Every such address then resolved to
two uids, which kept raising ``email-handle-ambiguous`` questions, and no
sanctioned write path could reach the record afterwards.

This module holds the fold primitive and the merge hook; the repair reuses it:

1. **The merge hook** — :func:`fold_absorbed_contact_records`. The merge paths
   call it after they absorb a page. Each of the absorbed uid's records is
   folded onto the canonical uid's record: addresses the canonical record does
   not list are added, with per-value provenance in ``field_sources`` (the same
   list shape the corrections applier writes); addresses it already lists are
   dropped; per-address facts (bounce closes, usage classifications) travel
   with the addresses they describe. The absorbed record is then retired with
   :func:`athenaeum.pii.retire_contact_record` — renamed out of the
   ``*.md`` glob and logged in a ledger, never deleted.

2. **The repair** — :mod:`athenaeum.contact_record_repair`, reached through
   ``athenaeum repair --contact-records``. It finds what earlier merges left
   behind:

   - records whose uid has no live page because a recorded merge absorbed it
     (tombstone ``folded_into``, a canonical page's ``merged_from``, or a
     ``_merge_provenance.jsonl`` fold record), folded exactly as in (1);
   - uids claimed by two records — a slug-only filename next to a
     ``<uid>-``-prefixed one, the shape a filename migration left. The slug
     copy is retired only when every value it holds (every address field,
     plus per-address facts) is already on the prefixed copy. A copy holding
     anything more is reported, never retired.

   Records whose uid has no page and no merge record are reported, never
   touched. Everything is counts only.

Why not the corrections applier itself: ``process_correction_record`` is gated
by the deployment's ``librarian.corrections.fields`` allowlist, which is empty
by default, and it resolves a uid's record first-match-wins — so on a store
with a duplicate pair it can land a value on the very copy being retired. The
fold writes the corrections applier's per-value provenance shape directly
instead, on a record it chose explicitly.

Nothing here rebuilds the recall index: the excluded surface is not in it.

Layering: L3. Imports :mod:`athenaeum.pii` (L3; surface layout and
retirement), :mod:`athenaeum.models` (L1) and :mod:`athenaeum.atomic_io`
(L0). The merge paths that call it (:mod:`athenaeum.dedupe`,
:mod:`athenaeum.pending_merges`) are L4; the repair, which needs
:mod:`athenaeum.pending_merges`' target resolver, lives in
:mod:`athenaeum.contact_record_repair` so this module never imports upward.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum import pii
from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter, render_frontmatter

log = logging.getLogger(__name__)

#: Every value-bearing field a contact record carries: ``emails``,
#: ``phones``, ``former_emails``, ``alt_emails``.
FOLD_FIELDS: tuple[str, ...] = pii.CONTACT_DATA_FIELDS

#: Per-ADDRESS fact lists that travel with the address they describe.
_PER_IDENTIFIER_FACT_FIELDS: tuple[str, ...] = (
    pii.IDENTIFIER_VALIDITY_FIELD,
    pii.CONTACT_CLASSIFICATION_FIELD,
)

#: Provenance stamped on a folded value that carried no ``field_sources``
#: entry of its own on the absorbed record.
FOLD_SOURCE = "script:contact-record-fold"

#: ``reason`` strings written to the retirement ledger.
REASON_MERGE_FOLD = "merge-fold"
REASON_DUPLICATE = "duplicate-record"


# ---------------------------------------------------------------------------
# Value comparison and merge
# ---------------------------------------------------------------------------


def _value_key(field_name: str, value: Any) -> str:
    """Comparison key for one value: digits for a phone, casefold otherwise."""
    text = str(value).strip()
    if field_name == "phones":
        digits = re.sub(r"\D", "", text)
        return digits or text.lower()
    return pii.normalize_identifier(text)


def _field_values(meta: Mapping[str, Any], field_name: str) -> list[str]:
    raw = meta.get(field_name)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if isinstance(item, str | int) and str(item).strip()]


def missing_values(source: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, list[str]]:
    """Per field, the values *source* lists that *target* does not (in source order)."""
    out: dict[str, list[str]] = {}
    for field_name in FOLD_FIELDS:
        have = {_value_key(field_name, v) for v in _field_values(target, field_name)}
        missing: list[str] = []
        for value in _field_values(source, field_name):
            key = _value_key(field_name, value)
            if key and key not in have:
                missing.append(value)
                have.add(key)
        if missing:
            out[field_name] = missing
    return out


def _fact_entries(meta: Mapping[str, Any], fact_field: str) -> list[dict[str, Any]]:
    raw = meta.get(fact_field)
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _fact_identifier(entry: Mapping[str, Any]) -> str:
    return pii.normalize_identifier(str(entry.get("identifier", "") or ""))


def _missing_facts(
    source: Mapping[str, Any], target: Mapping[str, Any], fact_field: str
) -> list[dict[str, Any]]:
    """Fact entries on *source* for an address *target* holds no entry for."""
    have = {_fact_identifier(e) for e in _fact_entries(target, fact_field)}
    return [e for e in _fact_entries(source, fact_field) if _fact_identifier(e) not in have]


def _facts_not_on(
    source: Mapping[str, Any], target: Mapping[str, Any], fact_field: str
) -> list[dict[str, Any]]:
    """Fact entries on *source* with no IDENTICAL entry on *target* (subset test)."""
    target_entries = _fact_entries(target, fact_field)
    out = []
    for entry in _fact_entries(source, fact_field):
        key = _fact_identifier(entry)
        if not any(
            _fact_identifier(other) == key
            and {k: v for k, v in other.items() if k != "identifier"}
            == {k: v for k, v in entry.items() if k != "identifier"}
            for other in target_entries
        ):
            out.append(entry)
    return out


def holds_nothing_more(copy: Mapping[str, Any], keep: Mapping[str, Any]) -> bool:
    """True when every value and per-address fact on *copy* is already on *keep*.

    The retire-safe test for a duplicate record: retiring *copy* then loses
    nothing any reader could have seen.
    """
    if missing_values(copy, keep):
        return False
    return not any(_facts_not_on(copy, keep, f) for f in _PER_IDENTIFIER_FACT_FIELDS)


def merge_contact_values(
    canonical: Mapping[str, Any],
    absorbed: Mapping[str, Any],
    *,
    source: Any = FOLD_SOURCE,
) -> tuple[dict[str, Any], dict[str, int], int]:
    """Fold *absorbed*'s values onto a copy of *canonical*.

    Returns ``(merged_meta, values_added_per_field, facts_carried)``. A value
    the canonical record already lists is dropped. An added value keeps the
    absorbed record's own ``field_sources`` provenance when it had one, else
    gets *source*; either way the entry is written in the corrections
    applier's list shape, ``{"value": ..., "source": ...}``.
    """
    merged = dict(canonical)
    added: dict[str, int] = {}
    missing = missing_values(absorbed, canonical)
    absorbed_fs = absorbed.get("field_sources")
    absorbed_fs = absorbed_fs if isinstance(absorbed_fs, dict) else {}
    canonical_fs = canonical.get("field_sources")
    fs = dict(canonical_fs) if isinstance(canonical_fs, dict) else {}
    for field_name, values in missing.items():
        merged[field_name] = [*_field_values(canonical, field_name), *values]
        entries = [e for e in (fs.get(field_name) or []) if isinstance(e, dict)]
        origin = [e for e in (absorbed_fs.get(field_name) or []) if isinstance(e, dict)]
        for value in values:
            key = _value_key(field_name, value)
            prior = next(
                (e for e in origin if _value_key(field_name, e.get("value", "")) == key),
                None,
            )
            prior_source = prior.get("source") if prior else None
            entries.append({"value": value, "source": prior_source or source})
        fs[field_name] = entries
        added[field_name] = len(values)
    if added:
        merged["field_sources"] = fs
    facts = 0
    for fact_field in _PER_IDENTIFIER_FACT_FIELDS:
        extra = _missing_facts(absorbed, canonical, fact_field)
        if extra:
            merged[fact_field] = [*_fact_entries(canonical, fact_field), *extra]
            facts += len(extra)
    return merged, added, facts


# ---------------------------------------------------------------------------
# The fold primitive
# ---------------------------------------------------------------------------


@dataclass
class ContactFoldSummary:
    """Counts from folding one or more absorbed records. Never carries a value."""

    records_folded: int = 0
    values_added: dict[str, int] = field(default_factory=dict)
    facts_carried: int = 0
    canonical_records_minted: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, other: ContactFoldSummary) -> None:
        self.records_folded += other.records_folded
        for key, count in other.values_added.items():
            self.values_added[key] = self.values_added.get(key, 0) + count
        self.facts_carried += other.facts_carried
        self.canonical_records_minted += other.canonical_records_minted
        self.errors.extend(other.errors)

    @property
    def values_added_total(self) -> int:
        return sum(self.values_added.values())


def uid_prefixed(path: Path, uid: str) -> bool:
    stem = path.stem
    return stem == uid or stem.startswith(f"{uid}-")


def preferred_record(paths: Iterable[Path], uid: str) -> Path | None:
    """The record a fold should land on: the ``<uid>``-named one first, else scan order."""
    existing = [p for p in paths if p.exists()]
    for path in existing:
        if uid_prefixed(path, uid):
            return path
    return existing[0] if existing else None


def fold_contact_record(
    contacts_root: Path,
    absorbed_path: Path,
    *,
    canonical_uid: str,
    index: pii.ExcludedRecordIndex,
    reason: str = REASON_MERGE_FOLD,
    dry_run: bool = False,
) -> ContactFoldSummary:
    """Fold one absorbed record onto *canonical_uid*'s record, then retire it.

    The canonical record is the ``<uid>``-named record for *canonical_uid* when
    there is one, else the first in scan order. When the canonical uid has no
    record at all and the absorbed one carries something to move, a record is
    minted at :func:`athenaeum.pii.default_uid_record_path`. Under *dry_run*
    nothing is written and the summary describes what would be.
    """
    root = Path(contacts_root)
    summary = ContactFoldSummary()
    absorbed_text = absorbed_path.read_text(encoding="utf-8")
    absorbed_meta, _ = parse_frontmatter(absorbed_text)
    absorbed_meta = absorbed_meta if isinstance(absorbed_meta, dict) else {}

    canonical_path = preferred_record(index.all_by_uid(canonical_uid), canonical_uid)
    minted = canonical_path is None
    if canonical_path is None:
        canonical_path = pii.default_uid_record_path(root, canonical_uid)
        canonical_meta: dict[str, Any] = {}
        canonical_body = (
            "Contact record minted when a merge folded an absorbed record into "
            "this entity (issue athenaeum#2184). This record is outside the "
            "corpus: not embedded, recalled, or merge-eligible.\n"
        )
    else:
        meta, canonical_body = parse_frontmatter(canonical_path.read_text(encoding="utf-8"))
        canonical_meta = meta if isinstance(meta, dict) else {}

    merged, added, facts = merge_contact_values(canonical_meta, absorbed_meta)
    if minted:
        merged["uid"] = canonical_uid
        merged[pii.PII_FLAG] = True
    changed = bool(added or facts)

    summary.records_folded = 1
    summary.values_added = added
    summary.facts_carried = facts
    summary.canonical_records_minted = int(minted and changed)
    if dry_run:
        return summary

    if changed:
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(canonical_path, render_frontmatter(merged) + "\n" + canonical_body)
        index.register(canonical_path)
    pii.retire_contact_record(
        root,
        absorbed_path,
        folded_into=canonical_uid,
        reason=reason,
        canonical_record=canonical_path if changed or not minted else None,
    )
    return summary


def fold_absorbed_contact_records(
    contacts_root: Path,
    absorbed_to_canonical: Iterable[tuple[str, str]],
    *,
    index: pii.ExcludedRecordIndex | None = None,
    dry_run: bool = False,
) -> ContactFoldSummary:
    """The merge hook: fold every record of each absorbed uid onto its canonical.

    *absorbed_to_canonical* is ``(absorbed_uid, canonical_uid)`` pairs, in the
    order the merge executed them. Idempotent: a uid whose records were
    already folded has none left to find. A missing contacts surface is a
    no-op. One failing record is reported in ``errors`` and does not stop the
    rest — the merge that called this has already happened.
    """
    summary = ContactFoldSummary()
    root = Path(contacts_root)
    if not root.is_dir():
        return summary
    idx = index if index is not None else pii.ExcludedRecordIndex(root)
    for absorbed_uid, canonical_uid in absorbed_to_canonical:
        absorbed = str(absorbed_uid or "").strip()
        canonical = str(canonical_uid or "").strip()
        if not absorbed or not canonical or absorbed == canonical:
            continue
        for path in [p for p in idx.all_by_uid(absorbed) if p.exists()]:
            try:
                summary.add(
                    fold_contact_record(
                        root,
                        path,
                        canonical_uid=canonical,
                        index=idx,
                        dry_run=dry_run,
                    )
                )
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                summary.errors.append(f"{type(exc).__name__} folding a record of an absorbed uid")
                log.warning(
                    "contact-record fold failed for one absorbed record (%s): %s",
                    type(exc).__name__,
                    exc,
                )
    return summary


def resolve_contacts_root(knowledge_root: Path, config: dict[str, Any] | None) -> Path | None:
    """The configured contacts surface, or ``None`` when ``pii`` is not excluded.

    :func:`athenaeum.pii.contacts_surface_root` resolves an UNMAPPED ``pii``
    class to the wiki root itself (documented there as a no-op convenience).
    A fold that scanned the wiki as if it were the contacts surface would
    retire wiki pages, so every caller here goes through this gate instead.
    """
    if not pii.is_pii_class_excluded(config):
        return None
    return pii.contacts_surface_root(Path(knowledge_root), config)


def fold_contact_records_after_merge(
    knowledge_root: Path,
    absorbed_to_canonical: Iterable[tuple[str, str]],
    *,
    config: dict[str, Any] | None = None,
    absorbed_page_types: Mapping[str, str] | None = None,
) -> ContactFoldSummary:
    """Best-effort wrapper the merge paths call: resolve the surface, fold, never raise.

    *absorbed_page_types* maps an absorbed uid to its page's ``type:``. When
    given, only uids whose page class joins the ``pii`` surface
    (:func:`athenaeum.pii.surface_class_for_page_class`) are folded, and a
    merge that absorbed none — a concept fold — returns before scanning the
    contacts surface at all.

    Resolves the contacts surface through :func:`resolve_contacts_root`
    (loading the knowledge root's config when *config* is ``None``), so no
    merge path hand-crafts a surface path, and a deployment that has not
    mapped ``pii`` to an excluded surface is a no-op. A failure is logged and returned in
    ``errors``; it never fails the merge, whose page writes already happened.
    """
    pairs = list(absorbed_to_canonical)
    if not pairs:
        return ContactFoldSummary()
    try:
        if config is None:
            from athenaeum.config import load_config

            config = load_config(knowledge_root)
        if absorbed_page_types is not None:
            pairs = [
                (absorbed, canonical)
                for absorbed, canonical in pairs
                if pii.surface_class_for_page_class(
                    absorbed_page_types.get(absorbed, ""), config
                )
                == pii.PII_ENTITY_CLASS
            ]
            if not pairs:
                return ContactFoldSummary()
        root = resolve_contacts_root(Path(knowledge_root), config)
        if root is None:
            return ContactFoldSummary()
        summary = fold_absorbed_contact_records(root, pairs)
    except Exception as exc:  # noqa: BLE001 — must never fail the merge
        log.warning("contact-record fold after merge skipped (%s): %s", type(exc).__name__, exc)
        summary = ContactFoldSummary(errors=[f"{type(exc).__name__}: contact-record fold skipped"])
    if summary.records_folded:
        log.info(
            "contact-record fold: %d record(s) folded, %d value(s) added, %d retired",
            summary.records_folded,
            summary.values_added_total,
            summary.records_folded,
        )
    return summary
