# SPDX-License-Identifier: Apache-2.0
"""Fold an absorbed uid's contact record into the canonical one (issue athenaeum#2184).

When a merge absorbs a person page — ``dedupe persons --apply`` unlinks it, a
``fold-into-existing`` approve tombstones it — the absorbed uid's record on the
excluded contacts surface used to stay behind, still listing the absorbed
person's addresses under the absorbed uid. Every such address then resolved to
two uids, which kept raising ``email-handle-ambiguous`` questions, and no
sanctioned write path could reach the record afterwards.

This module is the fix, in two halves that share one fold primitive:

1. **The merge hook** — :func:`fold_absorbed_contact_records`. The merge paths
   call it after they absorb a page. Each of the absorbed uid's records is
   folded onto the canonical uid's record: addresses the canonical record does
   not list are added, with per-value provenance in ``field_sources`` (the same
   list shape the corrections applier writes); addresses it already lists are
   dropped; per-address facts (bounce closes, usage classifications) travel
   with the addresses they describe. The absorbed record is then retired with
   :func:`athenaeum.pii.retire_contact_record` — renamed out of the
   ``*.md`` glob and logged in a ledger, never deleted.

2. **The repair** — :func:`plan_contact_record_repair` /
   :func:`apply_contact_record_repair`, reached through
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

Layering: L3 domain module over :mod:`athenaeum.pii` (surface layout and
retirement), :mod:`athenaeum.models` (wiki page parsing) and
:mod:`athenaeum.provenance` (the merge ledger).
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
from athenaeum.models import (
    EntityIndex,
    is_tombstone,
    parse_frontmatter,
    render_frontmatter,
    slugify,
    tombstone_target,
)

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

#: How far :func:`_live_end` follows a chain of absorbed uids. Folds are
#: acyclic by construction; this only bounds a hand-edited loop.
_MAX_CHAIN = 16


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


def _uid_prefixed(path: Path, uid: str) -> bool:
    stem = path.stem
    return stem == uid or stem.startswith(f"{uid}-")


def _preferred_record(paths: Iterable[Path], uid: str) -> Path | None:
    """The record a fold should land on: the ``<uid>``-named one first, else scan order."""
    existing = [p for p in paths if p.exists()]
    for path in existing:
        if _uid_prefixed(path, uid):
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

    canonical_path = _preferred_record(index.all_by_uid(canonical_uid), canonical_uid)
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


# ---------------------------------------------------------------------------
# The repair
# ---------------------------------------------------------------------------


@dataclass
class _WikiFacts:
    live_uids: set[str] = field(default_factory=set)
    tombstone_targets: dict[str, str] = field(default_factory=dict)  # uid -> slug
    merged_into: dict[str, set[str]] = field(default_factory=dict)  # absorbed -> canonicals
    slug_to_uid: dict[str, str] = field(default_factory=dict)
    provenance_folds: list[tuple[str, str]] = field(default_factory=list)  # (stem, slug)
    wiki_root: Path | None = None
    _slug_cache: dict[str, str | None] = field(default_factory=dict)

    def uid_for_slug(self, slug: str) -> str | None:
        """The uid of the page a fold's target slug names, resolved as the fold did.

        A fold records ``folded_into`` / ``canonical_slug`` as the BARE slug of
        its ``merge_target_name``, while a real page lives at
        ``<uid>-<slug>.md``. So this goes through
        :func:`athenaeum.pending_merges.resolve_target_page` — the same
        identity resolver the fold used to pick its target — rather than
        matching filenames, falling back to an exact stem match.
        """
        if slug in self._slug_cache:
            return self._slug_cache[slug]
        uid: str | None = None
        if self.wiki_root is not None and slug:
            from athenaeum.pending_merges import resolve_target_page

            page = resolve_target_page(slug, self.wiki_root)
            if page is not None:
                parsed = EntityIndex.read_page_record(page)
                if parsed is not None and str(parsed[0].uid or "").strip():
                    uid = str(parsed[0].uid).strip()
        if uid is None:
            uid = self.slug_to_uid.get(slug)
        self._slug_cache[slug] = uid
        return uid


def _read_wiki_facts(wiki_root: Path) -> _WikiFacts:
    """One pass over the wiki: which uids are live, and every recorded absorption.

    Uids are read from page frontmatter through
    :meth:`athenaeum.models.EntityIndex.read_page_record` — the same per-page
    rule ``EntityIndex`` uses — never inferred from a filename. A tombstone
    (``status: folded``) is NOT live, though ``EntityIndex`` indexes it.
    """
    facts = _WikiFacts(wiki_root=wiki_root)
    if not wiki_root.is_dir():
        return facts
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        parsed = EntityIndex.read_page_record(path)
        if parsed is None:
            continue
        record, meta = parsed
        uid = str(record.uid or "").strip()
        if not uid:
            continue
        facts.slug_to_uid.setdefault(slugify(path.stem), uid)
        if is_tombstone(meta):
            target = tombstone_target(meta)
            if target:
                facts.tombstone_targets[uid] = slugify(target)
            continue
        facts.live_uids.add(uid)
        merged_from = meta.get("merged_from")
        if isinstance(merged_from, str):
            merged_from = [merged_from]
        if isinstance(merged_from, list):
            for absorbed in merged_from:
                absorbed_uid = str(absorbed or "").strip()
                if absorbed_uid and absorbed_uid != uid:
                    facts.merged_into.setdefault(absorbed_uid, set()).add(uid)

    from athenaeum.provenance import read_merge_provenance

    for rec in read_merge_provenance(wiki_root):
        if rec.get("applied") is False:
            continue
        canonical_slug = slugify(str(rec.get("canonical_slug") or ""))
        if not canonical_slug:
            continue
        sources = rec.get("folded_sources") or rec.get("source_paths") or []
        if not isinstance(sources, list):
            continue
        for src in sources:
            stem = Path(str(src)).stem
            if stem and slugify(stem) != canonical_slug:
                facts.provenance_folds.append((stem, canonical_slug))
    return facts


def _absorbers(uid: str, facts: _WikiFacts) -> set[str]:
    """Every uid a recorded merge says absorbed *uid* (one hop)."""
    out: set[str] = set(facts.merged_into.get(uid, ()))
    for slug in _absorbing_slugs(uid, facts):
        resolved = facts.uid_for_slug(slug)
        if resolved:
            out.add(resolved)
    out.discard(uid)
    return out


def _absorbing_slugs(uid: str, facts: _WikiFacts) -> list[str]:
    slugs: list[str] = []
    slug = facts.tombstone_targets.get(uid)
    if slug:
        slugs.append(slug)
    for stem, canonical_slug in facts.provenance_folds:
        # A merge record names source PATHS, not uids; a source page whose
        # filename carries the uid is the only link it offers.
        if stem == uid or stem.startswith(f"{uid}-"):
            slugs.append(canonical_slug)
    return slugs


def _has_absorption_record(uid: str, facts: _WikiFacts) -> bool:
    """Whether ANY merge record names *uid* as absorbed, resolvable or not."""
    return bool(facts.merged_into.get(uid)) or bool(_absorbing_slugs(uid, facts))


def _live_ends(uid: str, facts: _WikiFacts) -> set[str]:
    """Follow absorptions from *uid* to the live canonical uid(s) at the end."""
    ends: set[str] = set()
    frontier = _absorbers(uid, facts)
    seen = {uid}
    for _ in range(_MAX_CHAIN):
        if not frontier:
            break
        nxt: set[str] = set()
        for candidate in frontier - seen:
            seen.add(candidate)
            if candidate in facts.live_uids:
                ends.add(candidate)
            else:
                nxt |= _absorbers(candidate, facts)
        frontier = nxt
    return ends


@dataclass
class ContactRepairPlan:
    """What ``repair --contact-records`` found. Paths are kept for apply, never printed."""

    records_scanned: int = 0
    uids_scanned: int = 0
    orphan_folds: list[tuple[str, str, list[Path]]] = field(default_factory=list)
    orphan_ambiguous: int = 0
    orphan_canonical_not_live: int = 0
    pageless_unmapped: int = 0
    duplicate_retire: list[tuple[str, Path, Path]] = field(default_factory=list)
    duplicate_divergent: int = 0
    duplicate_both_prefixed: int = 0
    duplicate_other_shape: int = 0
    orphan_values_to_add: int = 0

    @property
    def actionable(self) -> int:
        return len(self.orphan_folds) + len(self.duplicate_retire)

    def counts(self) -> dict[str, int]:
        return {
            "records_scanned": self.records_scanned,
            "uids_scanned": self.uids_scanned,
            "absorbed_uids_to_fold": len(self.orphan_folds),
            "absorbed_records_to_fold": sum(len(p) for _, _, p in self.orphan_folds),
            "absorbed_values_to_add": self.orphan_values_to_add,
            "absorbed_ambiguous_canonical": self.orphan_ambiguous,
            "absorbed_canonical_unresolved": self.orphan_canonical_not_live,
            "pageless_no_merge_record": self.pageless_unmapped,
            "duplicate_copies_to_retire": len(self.duplicate_retire),
            "duplicate_divergent_reported": self.duplicate_divergent,
            "duplicate_both_uid_prefixed": self.duplicate_both_prefixed,
            "duplicate_other_shape": self.duplicate_other_shape,
        }


def plan_contact_record_repair(knowledge_root: Path, contacts_root: Path) -> ContactRepairPlan:
    """Read-only: classify every uid on the contacts surface. Writes nothing."""
    plan = ContactRepairPlan()
    facts = _read_wiki_facts(Path(knowledge_root) / "wiki")
    index = pii.ExcludedRecordIndex(Path(contacts_root))
    plan.records_scanned = len(pii.iter_contact_records(Path(contacts_root)))
    uids = index.uids()
    plan.uids_scanned = len(uids)
    for uid in uids:
        paths = index.all_by_uid(uid)
        if uid in facts.live_uids:
            if len(paths) > 1:
                _classify_duplicate(uid, paths, plan)
            continue
        if not _has_absorption_record(uid, facts):
            plan.pageless_unmapped += 1
            continue
        ends = _live_ends(uid, facts)
        if len(ends) == 1:
            canonical = next(iter(ends))
            plan.orphan_folds.append((uid, canonical, list(paths)))
            plan.orphan_values_to_add += _values_to_add(paths, canonical, index)
        elif not ends:
            plan.orphan_canonical_not_live += 1
        else:
            plan.orphan_ambiguous += 1
    return plan


def _values_to_add(paths: list[Path], canonical_uid: str, index: pii.ExcludedRecordIndex) -> int:
    target_path = _preferred_record(index.all_by_uid(canonical_uid), canonical_uid)
    target = pii.read_bounce_record(target_path) if target_path else {}
    total = 0
    for path in paths:
        missing = missing_values(pii.read_bounce_record(path), target)
        total += sum(len(v) for v in missing.values())
        # Later records see the earlier ones' values as already present.
        target = merge_contact_values(target, pii.read_bounce_record(path))[0]
    return total


def _classify_duplicate(uid: str, paths: list[Path], plan: ContactRepairPlan) -> None:
    if len(paths) != 2:
        plan.duplicate_other_shape += 1
        return
    prefixed = [p for p in paths if _uid_prefixed(p, uid)]
    if len(prefixed) == 2:
        plan.duplicate_both_prefixed += 1
        return
    if len(prefixed) != 1:
        plan.duplicate_other_shape += 1
        return
    keep = prefixed[0]
    copy = next(p for p in paths if p != keep)
    if keep.parent != copy.parent:
        plan.duplicate_other_shape += 1
        return
    if holds_nothing_more(pii.read_bounce_record(copy), pii.read_bounce_record(keep)):
        plan.duplicate_retire.append((uid, copy, keep))
    else:
        plan.duplicate_divergent += 1


@dataclass
class ContactRepairResult:
    """Counts from :func:`apply_contact_record_repair`."""

    fold: ContactFoldSummary = field(default_factory=ContactFoldSummary)
    duplicates_retired: int = 0
    errors: list[str] = field(default_factory=list)


def apply_contact_record_repair(
    plan: ContactRepairPlan, contacts_root: Path
) -> ContactRepairResult:
    """Act on *plan*: retire subset duplicates, then fold absorbed uids' records."""
    result = ContactRepairResult()
    root = Path(contacts_root)
    for uid, copy, keep in plan.duplicate_retire:
        try:
            pii.retire_contact_record(
                root, copy, folded_into=uid, reason=REASON_DUPLICATE, canonical_record=keep
            )
            result.duplicates_retired += 1
        except (OSError, UnicodeDecodeError) as exc:
            result.errors.append(f"{type(exc).__name__} retiring a duplicate record")
    index = pii.ExcludedRecordIndex(root)
    result.fold = fold_absorbed_contact_records(
        root,
        [(uid, canonical) for uid, canonical, _ in plan.orphan_folds],
        index=index,
    )
    result.errors.extend(result.fold.errors)
    return result
