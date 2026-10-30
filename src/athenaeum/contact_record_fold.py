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

3. **The page-removal settle** — :func:`settle_removed_uid_records` and its
   best-effort wrapper :func:`settle_removed_uid_records_after_removal`
   (issue athenaeum#2217). Removing a wiki page used to leave its contact
   record live on the excluded surface with nobody pointing at it: the
   merge paths above fold an ABSORBED uid's record, but a page can also go
   away with no absorption at all — a retirement
   (:mod:`athenaeum.retire_pages`), a decay sweep
   (:mod:`athenaeum.decay_sweep`), or an oversize page moved into the
   preserved-log area (:mod:`athenaeum.tiers`). Such a record keeps
   inflating the shared-address count and makes the ownership guard decline
   correct writes. The three removal paths call the wrapper as a one-liner
   right after their own removal succeeds: with a successor uid (a
   retirement that recorded a merge target) each record is folded exactly
   as in (1); with none, each record is retired outright through
   :func:`athenaeum.pii.retire_contact_record` with no ``folded_into`` —
   genuinely not folded into anything, never a sentinel string.

Why not the corrections applier itself: ``process_correction_record`` is gated
by the deployment's ``librarian.corrections.fields`` allowlist, which is empty
by default, and it resolves a uid's record first-match-wins — so on a store
with a duplicate pair it can land a value on the very copy being retired. The
fold writes the corrections applier's per-value provenance shape directly
instead, on a record it chose explicitly.

Nothing here rebuilds the recall index: the excluded surface is not in it.

Layering: L3. Imports :mod:`athenaeum.pii` (L3; surface layout and
retirement), :mod:`athenaeum.pii_attribution` (L3 sibling; the issue athenaeum#2210
attribution seam — a same-layer edge exactly like the ``pii`` one above, and
one :mod:`athenaeum.pii_attribution` never reciprocates), :mod:`athenaeum.models`
(L1) and :mod:`athenaeum.atomic_io` (L0). The merge paths that call it
(:mod:`athenaeum.dedupe`, :mod:`athenaeum.pending_merges`) are L4; the
repair, which needs :mod:`athenaeum.pending_merges`' target resolver, lives in
:mod:`athenaeum.contact_record_repair` so this module never imports upward.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum import pii, pii_attribution
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
#: A divergent duplicate copy whose extra values were folded onto the
#: ``<uid>``-named record before it was retired.
REASON_DUPLICATE_FOLD = "duplicate-fold"
#: A record settled (folded or retired) because its own wiki page was
#: removed — a retirement, a decay sweep, or an oversize log-demote (issue
#: athenaeum#2217). Used uniformly by :func:`settle_removed_uid_records`
#: regardless of which branch it takes.
REASON_PAGE_REMOVED = "page-removed"


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


def _identifier_keys(*metas: Mapping[str, Any]) -> set[str]:
    """``pii.normalize_identifier`` of every value across ``FOLD_FIELDS`` on *metas*.

    The orphan test's identifier universe (issue athenaeum#2196, AC1): a
    per-address fact entry (:data:`_PER_IDENTIFIER_FACT_FIELDS`) whose
    :func:`_fact_identifier` is not in this set, computed on the record(s) in
    question, describes an address neither record lists. Deliberately the
    WIDE set — :data:`FOLD_FIELDS` (= :data:`pii.CONTACT_DATA_FIELDS`,
    includes ``phones``) rather than the narrower
    :data:`pii.CONTACT_IDENTIFIER_FIELDS` :func:`missing_values` iterates for
    VALUE comparison — per the issue's explicit AC1 wording; the wider set
    errs toward KEEPING an entry, never toward dropping one it need not.
    """
    keys: set[str] = set()
    for meta in metas:
        for field_name in FOLD_FIELDS:
            for value in _field_values(meta, field_name):
                key = pii.normalize_identifier(value)
                if key:
                    keys.add(key)
    return keys


def orphaned_fact_entries(meta: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Per-field fact entries on *meta* describing an address *meta* itself does not list.

    The single-record form of the same orphan test :func:`merge_contact_values`
    and :func:`holds_nothing_more` apply during a fold (issue athenaeum#2196,
    AC1/AC4) — here "the record in question" is *meta* itself, which is what
    ``repair --contact-records`` needs to report and prune an orphan that
    predates this fix and that no fold or merge touches (a record with no
    duplicate and no absorption to trigger one). Keyed by fact field
    (:data:`_PER_IDENTIFIER_FACT_FIELDS`); a field with no orphan entry is
    absent from the result, never present with an empty list.
    """
    keys = _identifier_keys(meta)
    out: dict[str, list[dict[str, Any]]] = {}
    for fact_field in _PER_IDENTIFIER_FACT_FIELDS:
        orphans = [e for e in _fact_entries(meta, fact_field) if _fact_identifier(e) not in keys]
        if orphans:
            out[fact_field] = orphans
    return out


def prune_orphan_fact_entries(meta: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    """A copy of *meta* with every :func:`orphaned_fact_entries` entry dropped.

    Returns ``(pruned_meta, entries_dropped)``. ``entries_dropped == 0``
    returns a plain ``dict(meta)`` unchanged, so a caller can skip a write
    when there was nothing to prune — the same "write only on a real change"
    discipline the rest of this module follows.
    """
    orphans = orphaned_fact_entries(meta)
    if not orphans:
        return dict(meta), 0
    pruned = dict(meta)
    dropped = 0
    for fact_field, bad in orphans.items():
        bad_keys = {_fact_identifier(e) for e in bad}
        kept = [e for e in _fact_entries(meta, fact_field) if _fact_identifier(e) not in bad_keys]
        dropped += len(bad)
        if kept:
            pruned[fact_field] = kept
        else:
            pruned.pop(fact_field, None)
    return pruned, dropped


def holds_nothing_more(copy: Mapping[str, Any], keep: Mapping[str, Any]) -> bool:
    """True when every value and per-address fact on *copy* is already on *keep*.

    The retire-safe test for a duplicate record: retiring *copy* then loses
    nothing any reader could have seen.

    A fact entry on *copy* that :func:`merge_contact_values` would drop as an
    orphan (its identifier is in neither record's address fields — issue
    athenaeum#2196, AC1/AC4) does not count as "more": the fold would never
    carry it across in the first place, so retiring *copy* loses nothing a
    fold could have preserved.
    """
    if missing_values(copy, keep):
        return False
    identifier_keys = _identifier_keys(copy, keep)
    for fact_field in _PER_IDENTIFIER_FACT_FIELDS:
        extra = [
            e
            for e in _facts_not_on(copy, keep, fact_field)
            if _fact_identifier(e) in identifier_keys
        ]
        if extra:
            return False
    return True


def merge_contact_values(
    canonical: Mapping[str, Any],
    absorbed: Mapping[str, Any],
    *,
    source: Any = FOLD_SOURCE,
    attribution_context: "pii_attribution.AttributionContext | None" = None,
    target_uid: str = "",
    target_name: str | None = None,
    target_type: str | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, int],
    int,
    tuple[tuple[str, "pii_attribution.AttributionVerdict"], ...],
]:
    """Fold *absorbed*'s values onto a copy of *canonical*.

    Returns ``(merged_meta, values_added_per_field, facts_carried,
    attribution_verdicts)`` — the last a ``(value, verdict)`` pair per
    checked value, since a bare verdict carries no value
    (:class:`athenaeum.pii_attribution.AttributionVerdict` deliberately
    never does) and the caller needs the value to park a suspect entry. A
    value the canonical record already lists is dropped. An added value
    keeps the absorbed record's own ``field_sources`` provenance when it had
    one, else gets *source*; either way the entry is written in the
    corrections applier's list shape, ``{"value": ..., "source": ...}``.

    **Orphan guard (issue athenaeum#2196, AC1).** A per-address fact entry on
    *absorbed* is carried onto the merged record only when its identifier is
    in the MERGED record's address set (:func:`_identifier_keys` over
    :data:`FOLD_FIELDS`) — computed AFTER the value-fold above, so an
    address this SAME call is adding (because *absorbed* lists it and
    *canonical* did not) still counts, and its fact entry travels with it.
    An entry describing an address neither record lists is never written.

    **Attribution (issue athenaeum#2210).** When *attribution_context* is
    given, the seam runs once per value :func:`missing_values` would carry
    onto the canonical record — i.e. exactly the values this call would
    otherwise add. This function stays pure (no logging, no parking — see
    :func:`athenaeum.pii_attribution.check_contact_attribution`'s own
    "pure apart from the log" for what IS a side effect here regardless):
    the verdicts are returned for :func:`fold_contact_record` to park. A
    D1-narrow verdict (:func:`athenaeum.pii_attribution.is_d1_narrow`)
    excludes that one value from the merge entirely — it is never carried
    onto the canonical record — while every other value is added exactly as
    it is with *attribution_context* omitted (design
    ``docs/design/attribution-verification.md`` §9.1). Omitting
    *attribution_context* (the default) skips the seam and returns an empty
    verdicts tuple, so every existing caller is byte-identical.
    """
    merged = dict(canonical)
    added: dict[str, int] = {}
    missing = missing_values(absorbed, canonical)
    verdicts: list[tuple[str, "pii_attribution.AttributionVerdict"]] = []
    if attribution_context is not None:
        parked_values: set[str] = set()
        for field_name, values in missing.items():
            for value in values:
                verdict = pii_attribution.check_contact_attribution(
                    value,
                    target_uid=target_uid,
                    target_name=target_name,
                    target_type=target_type,
                    source_ref=source,
                    writer=pii_attribution.WRITER_CONTACT_FOLD,
                    context=attribution_context,
                )
                verdicts.append((value, verdict))
                if pii_attribution.is_d1_narrow(verdict):
                    parked_values.add(_value_key(field_name, value))
        if parked_values:
            missing = {
                field_name: [v for v in values if _value_key(field_name, v) not in parked_values]
                for field_name, values in missing.items()
            }
            missing = {field_name: values for field_name, values in missing.items() if values}
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
    identifier_keys = _identifier_keys(merged)
    for fact_field in _PER_IDENTIFIER_FACT_FIELDS:
        extra = [
            e
            for e in _missing_facts(absorbed, canonical, fact_field)
            if _fact_identifier(e) in identifier_keys
        ]
        if extra:
            merged[fact_field] = [*_fact_entries(canonical, fact_field), *extra]
            facts += len(extra)
    return merged, added, facts, tuple(verdicts)


# ---------------------------------------------------------------------------
# Divergent duplicates: two records for ONE live uid
# ---------------------------------------------------------------------------


def _normalized_name(meta: Mapping[str, Any]) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(meta.get("name") or "")).casefold().split())


def _entries_by_identifier(meta: Mapping[str, Any], fact_field: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for entry in _fact_entries(meta, fact_field):
        out.setdefault(_fact_identifier(entry), entry)
    return out


def _same_fact(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return {k: v for k, v in a.items() if k != "identifier"} == {
        k: v for k, v in b.items() if k != "identifier"
    }


def duplicate_needs_review(copy: Mapping[str, Any], keep: Mapping[str, Any]) -> str | None:
    """Why two records for ONE uid cannot be folded mechanically, or ``None``.

    Two cases need a human:

    - ``name-differs``: both records carry a ``name`` and they differ after
      normalization. One uid should describe one entity, so differing names
      may mean a record was attached to the wrong uid.
    - ``validity-differs``: both records carry a validity entry (a bounce or
      close) for the same address and the entries differ. Picking one would
      decide whether an address is still deliverable, which is a fact
      question, not a bookkeeping one.

    A differing usage classification for the same address is NOT a review
    case: :func:`merge_duplicate_record` settles it by the store's own
    no-downgrade ladder.
    """
    copy_name, keep_name = _normalized_name(copy), _normalized_name(keep)
    if copy_name and keep_name and copy_name != keep_name:
        return "name-differs"
    keep_validity = _entries_by_identifier(keep, pii.IDENTIFIER_VALIDITY_FIELD)
    for key, entry in _entries_by_identifier(copy, pii.IDENTIFIER_VALIDITY_FIELD).items():
        other = keep_validity.get(key)
        if other is not None and not _same_fact(entry, other):
            return "validity-differs"
    return None


def _stronger_classification(
    keep_entry: Mapping[str, Any], copy_entry: Mapping[str, Any]
) -> Mapping[str, Any]:
    """The classification entry that survives a fold of two entries for one address.

    The stronger usage class wins (``observed`` > ``provider`` >
    ``unclassified``, the order of :data:`athenaeum.pii.USAGE_CLASSES`), so a
    fold can never downgrade an address. With equal classes the entry with
    the later ``observed_at`` wins, as a re-assertion of the same class
    refreshes provenance in place. A full tie keeps the ``<uid>``-named
    record's entry.
    """
    keep_rank = pii._classification_rank(str(keep_entry.get("usage_class") or ""))
    copy_rank = pii._classification_rank(str(copy_entry.get("usage_class") or ""))
    if copy_rank != keep_rank:
        return copy_entry if copy_rank < keep_rank else keep_entry
    keep_at = str(keep_entry.get("observed_at") or "")
    copy_at = str(copy_entry.get("observed_at") or "")
    return copy_entry if copy_at > keep_at else keep_entry


def merge_duplicate_record(
    keep: Mapping[str, Any], copy: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, int], int, int]:
    """Fold a divergent duplicate *copy* onto *keep*, the ``<uid>``-named record.

    Returns ``(merged_meta, values_added_per_field, facts_carried,
    classifications_reconciled)``. Values and per-address facts on only one
    copy are kept, through :func:`merge_contact_values`. A classification both
    records hold for the same address is settled by
    :func:`_stronger_classification`. Callers check
    :func:`duplicate_needs_review` first; this function does not.

    **Orphan guard (issue athenaeum#2196, decision 3).** The reconciliation
    loop below only ever swaps an EXISTING entry's value for an identifier
    :func:`merge_contact_values` has already allowed onto *merged* — it
    cannot itself introduce a new one. The entries list is re-filtered
    against the same merged-record identifier set anyway, defensively, so a
    later change to this loop cannot quietly reintroduce an orphan.
    """
    # Issue athenaeum#2210: this call site (two records for ONE live uid) is
    # not one of this issue's four wired writers — no attribution_context is
    # passed, so the seam is skipped and the verdicts tuple is always empty.
    merged, added, facts, _verdicts = merge_contact_values(keep, copy)
    copy_cls = _entries_by_identifier(copy, pii.CONTACT_CLASSIFICATION_FIELD)
    identifier_keys = _identifier_keys(merged)
    reconciled = 0
    entries = _fact_entries(merged, pii.CONTACT_CLASSIFICATION_FIELD)
    for position, entry in enumerate(entries):
        if _fact_identifier(entry) not in identifier_keys:
            # Orphaned on the merged record already (pre-existing on *keep*,
            # or describing an address neither copy lists) — never a
            # reconciliation candidate; pruned below regardless of outcome.
            continue
        other = copy_cls.get(_fact_identifier(entry))
        if other is None or _same_fact(entry, other):
            continue
        winner = _stronger_classification(entry, other)
        if winner is other:
            entries[position] = {**dict(other), "identifier": entry.get("identifier")}
            reconciled += 1
    pruned = [e for e in entries if _fact_identifier(e) in identifier_keys]
    if reconciled or len(pruned) != len(entries):
        if pruned:
            merged[pii.CONTACT_CLASSIFICATION_FIELD] = pruned
        else:
            merged.pop(pii.CONTACT_CLASSIFICATION_FIELD, None)
    return merged, added, facts, reconciled


@dataclass
class DuplicateFoldSummary:
    """Counts from folding divergent duplicate copies. Never carries a value."""

    copies_folded: int = 0
    values_added: dict[str, int] = field(default_factory=dict)
    facts_carried: int = 0
    classifications_reconciled: int = 0
    errors: list[str] = field(default_factory=list)


def fold_duplicate_record(
    contacts_root: Path,
    uid: str,
    copy_path: Path,
    keep_path: Path,
    *,
    dry_run: bool = False,
) -> DuplicateFoldSummary:
    """Fold *copy_path* onto *keep_path* (both records for *uid*), then retire the copy.

    The copy is retired with :data:`REASON_DUPLICATE_FOLD`, renamed out of the
    ``*.md`` glob and logged, never deleted. Refuses (an error, nothing
    written) when :func:`duplicate_needs_review` names a reason, so a caller
    that skipped the check still cannot fold a pair that needs a human.
    """
    summary = DuplicateFoldSummary()
    keep_meta, keep_body = parse_frontmatter(keep_path.read_text(encoding="utf-8"))
    copy_meta, _ = parse_frontmatter(copy_path.read_text(encoding="utf-8"))
    keep_meta = keep_meta if isinstance(keep_meta, dict) else {}
    copy_meta = copy_meta if isinstance(copy_meta, dict) else {}
    reason = duplicate_needs_review(copy_meta, keep_meta)
    if reason is not None:
        summary.errors.append(f"duplicate needs review ({reason})")
        return summary
    merged, added, facts, reconciled = merge_duplicate_record(keep_meta, copy_meta)
    summary.copies_folded = 1
    summary.values_added = added
    summary.facts_carried = facts
    summary.classifications_reconciled = reconciled
    if dry_run:
        return summary
    if added or facts or reconciled:
        atomic_write_text(keep_path, render_frontmatter(merged) + "\n" + keep_body)
    pii.retire_contact_record(
        Path(contacts_root),
        copy_path,
        folded_into=uid,
        reason=REASON_DUPLICATE_FOLD,
        canonical_record=keep_path,
    )
    return summary


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
    attribution_context: "pii_attribution.AttributionContext | None" = None,
) -> ContactFoldSummary:
    """Fold one absorbed record onto *canonical_uid*'s record, then retire it.

    The canonical record is the ``<uid>``-named record for *canonical_uid* when
    there is one, else the first in scan order. When the canonical uid has no
    record at all and the absorbed one carries something to move, a record is
    minted at :func:`athenaeum.pii.default_uid_record_path`. Under *dry_run*
    nothing is written and the summary describes what would be.

    *attribution_context* (issue athenaeum#2210): threaded into
    :func:`merge_contact_values`, target is *canonical_uid* — the fold files
    every carried value onto the CANONICAL record, so that is the
    attribution target, not the absorbed one. The returned verdicts are
    parked here (this function, unlike ``merge_contact_values``, does write
    to disk) for every non-``pass`` verdict, and only once the real write
    below actually happens — never under *dry_run*, matching every other
    write this function makes.
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

    canonical_name = canonical_meta.get("name")
    merged, added, facts, verdicts = merge_contact_values(
        canonical_meta,
        absorbed_meta,
        attribution_context=attribution_context,
        target_uid=canonical_uid,
        target_name=canonical_name if isinstance(canonical_name, str) else None,
    )
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
    if attribution_context is not None:
        for value, verdict in verdicts:
            if verdict.disposition == pii_attribution.DISPOSITION_PASS:
                continue
            pii_attribution.park_suspect(
                attribution_context,
                value,
                verdict,
                target_uid=canonical_uid,
                target_name=canonical_name if isinstance(canonical_name, str) else None,
                writer=pii_attribution.WRITER_CONTACT_FOLD,
                source_ref=str(absorbed_path),
                shadow=not pii_attribution.is_d1_narrow(verdict),
            )
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
    attribution_context: "pii_attribution.AttributionContext | None" = None,
) -> ContactFoldSummary:
    """The merge hook: fold every record of each absorbed uid onto its canonical.

    *absorbed_to_canonical* is ``(absorbed_uid, canonical_uid)`` pairs, in the
    order the merge executed them. Idempotent: a uid whose records were
    already folded has none left to find. A missing contacts surface is a
    no-op. One failing record is reported in ``errors`` and does not stop the
    rest — the merge that called this has already happened.

    *attribution_context* (issue athenaeum#2210): threaded to every
    :func:`fold_contact_record` call. ``None`` (the default) skips the seam
    entirely, so every existing caller is byte-identical.
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
                        attribution_context=attribution_context,
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
                if pii.surface_class_for_page_class(absorbed_page_types.get(absorbed, ""), config)
                == pii.PII_ENTITY_CLASS
            ]
            if not pairs:
                return ContactFoldSummary()
        root = resolve_contacts_root(Path(knowledge_root), config)
        if root is None:
            return ContactFoldSummary()
        # Issue athenaeum#2210: this IS the real production entry point every
        # merge path (dedupe.py, pending_merges.py) calls, so the
        # attribution context is built HERE rather than threaded as a new
        # parameter through every merge caller — those callers already
        # have no attribution concept and should not need one.
        attribution_context = pii_attribution.build_attribution_context(
            Path(knowledge_root), config
        )
        summary = fold_absorbed_contact_records(
            root, pairs, attribution_context=attribution_context
        )
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


# ---------------------------------------------------------------------------
# Page removal: fold or retire a removed uid's records (issue athenaeum#2217)
# ---------------------------------------------------------------------------


@dataclass
class SettleSummary:
    """Counts from settling one removed uid's records. Never carries a value.

    ``records_folded``/``values_added``/``facts_carried``/
    ``canonical_records_minted`` mirror :class:`ContactFoldSummary` exactly
    (the successor branch delegates to :func:`fold_contact_record`);
    ``records_retired`` counts the no-successor branch, which retires
    outright rather than folding.
    """

    records_folded: int = 0
    records_retired: int = 0
    values_added: dict[str, int] = field(default_factory=dict)
    facts_carried: int = 0
    canonical_records_minted: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, other: SettleSummary) -> None:
        self.records_folded += other.records_folded
        self.records_retired += other.records_retired
        for key, count in other.values_added.items():
            self.values_added[key] = self.values_added.get(key, 0) + count
        self.facts_carried += other.facts_carried
        self.canonical_records_minted += other.canonical_records_minted
        self.errors.extend(other.errors)


def settle_removed_uid_records(
    contacts_root: Path,
    uid: str,
    *,
    successor_uid: str | None,
    reason: str,
    index: pii.ExcludedRecordIndex,
    dry_run: bool = False,
) -> SettleSummary:
    """Fold or retire every record *uid* left behind once its page is gone.

    *uid*'s page has already been removed (a retirement, a decay sweep, an
    oversize log-demote) — this never touches wiki pages, only the excluded
    contacts surface. A uid with no records on *index* is a no-op, returning
    a zero summary.

    With *successor_uid* given, each of *uid*'s records is folded onto the
    successor's record through :func:`fold_contact_record` (the same
    primitive the merge hook uses) — the retirement recorded a merge target,
    so the departing uid's values genuinely belong to a live entity. With
    *successor_uid* ``None``, each record is retired outright through
    :func:`athenaeum.pii.retire_contact_record` with no ``folded_into``: the
    uid was not absorbed by anything, it simply has no page anymore.

    One record's failure is reported in ``errors`` and does not stop the
    rest, mirroring :func:`fold_absorbed_contact_records`.
    """
    summary = SettleSummary()
    root = Path(contacts_root)
    wanted = str(uid or "").strip()
    if not wanted:
        return summary
    paths = [p for p in index.all_by_uid(wanted) if p.exists()]
    if not paths:
        return summary
    successor = str(successor_uid or "").strip() or None
    for path in paths:
        try:
            if successor is not None:
                fold_summary = fold_contact_record(
                    root,
                    path,
                    canonical_uid=successor,
                    index=index,
                    reason=reason,
                    dry_run=dry_run,
                )
                summary.records_folded += fold_summary.records_folded
                for key, count in fold_summary.values_added.items():
                    summary.values_added[key] = summary.values_added.get(key, 0) + count
                summary.facts_carried += fold_summary.facts_carried
                summary.canonical_records_minted += fold_summary.canonical_records_minted
                summary.errors.extend(fold_summary.errors)
            else:
                summary.records_retired += 1
                if not dry_run:
                    pii.retire_contact_record(root, path, folded_into=None, reason=reason)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            summary.errors.append(f"{type(exc).__name__} settling a removed uid's record")
            log.warning(
                "contact-record settle failed for one record of a removed uid (%s): %s",
                type(exc).__name__,
                exc,
            )
    return summary


def settle_removed_uid_records_after_removal(
    knowledge_root: Path,
    uid: str,
    *,
    successor_uid: str | None,
    reason: str,
    config: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> SettleSummary:
    """Best-effort wrapper the page-removal paths call: resolve the surface,
    settle, never raise.

    Mirrors :func:`fold_contact_records_after_merge`'s shape exactly: resolves
    the contacts surface through :func:`resolve_contacts_root` (loading the
    knowledge root's config when *config* is ``None``), so none of
    :mod:`athenaeum.retire_pages`, :mod:`athenaeum.decay_sweep`, or
    :mod:`athenaeum.tiers` hand-crafts a surface path, and a deployment that
    has not mapped ``pii`` to an excluded surface is a no-op. A failure is
    logged and returned in ``errors``; it never fails the removal this was
    called after, which has already happened.
    """
    wanted = str(uid or "").strip()
    if not wanted:
        return SettleSummary()
    try:
        if config is None:
            from athenaeum.config import load_config

            config = load_config(knowledge_root)
        root = resolve_contacts_root(Path(knowledge_root), config)
        if root is None:
            return SettleSummary()
        summary = settle_removed_uid_records(
            root,
            wanted,
            successor_uid=successor_uid,
            reason=reason,
            index=pii.ExcludedRecordIndex(root),
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — must never fail the removal
        log.warning(
            "contact-record settle after page removal skipped (%s): %s",
            type(exc).__name__,
            exc,
        )
        summary = SettleSummary(errors=[f"{type(exc).__name__}: contact-record settle skipped"])
    if summary.records_folded or summary.records_retired:
        log.info(
            "contact-record settle: %d record(s) folded, %d retired after page removal",
            summary.records_folded,
            summary.records_retired,
        )
    return summary
