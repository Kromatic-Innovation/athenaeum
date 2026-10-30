# SPDX-License-Identifier: Apache-2.0
"""``athenaeum repair --contact-records``: clean up what earlier merges left (issue athenaeum#2184).

Two populations on the excluded contacts surface, both counts-only:

- records whose uid has no live page because a recorded merge absorbed it
  (a tombstone's ``folded_into``, a canonical page's ``merged_from``, or a
  ``_merge_provenance.jsonl`` fold record). Each is folded onto the live
  canonical with :func:`athenaeum.contact_record_fold.fold_absorbed_contact_records`,
  the same fold the merge paths now run;
- uids claimed by two records: a slug-only filename next to a
  ``<uid>-``-prefixed one. The slug copy is retired outright when
  :func:`athenaeum.contact_record_fold.holds_nothing_more` says every value
  and per-address fact on it is already on the prefixed copy. A DIVERGENT
  copy (it holds something the prefixed copy lacks) is folded onto the
  prefixed copy first with
  :func:`athenaeum.contact_record_fold.fold_duplicate_record` — values and
  facts on only one copy are kept, a same-address classification is settled
  by the no-downgrade ladder — and then retired.

Records whose uid has no page and no merge record, an ambiguous or
unresolvable canonical, a divergent duplicate that
:func:`athenaeum.contact_record_fold.duplicate_needs_review` flags (differing
names, or differing validity entries for one address), and any other
duplicate shape are reported and never touched.

Layering: L4. Imports :mod:`athenaeum.contact_record_fold` (L3),
:mod:`athenaeum.pii` (L3), :mod:`athenaeum.pending_merges` (L4, for the
fold's own target resolver), :mod:`athenaeum.provenance` and
:mod:`athenaeum.models` (L1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from athenaeum import pii
from athenaeum.atomic_io import atomic_write_text
from athenaeum.contact_record_fold import (
    REASON_DUPLICATE,
    ContactFoldSummary,
    DuplicateFoldSummary,
    duplicate_needs_review,
    fold_absorbed_contact_records,
    fold_duplicate_record,
    holds_nothing_more,
    merge_contact_values,
    merge_duplicate_record,
    missing_values,
    orphaned_fact_entries,
    preferred_record,
    prune_orphan_fact_entries,
    uid_prefixed,
)
from athenaeum.models import (
    EntityIndex,
    is_tombstone,
    parse_frontmatter,
    render_frontmatter,
    slugify,
    tombstone_target,
)
from athenaeum.pending_merges import resolve_target_page
from athenaeum.provenance import read_merge_provenance

#: How far :func:`_live_ends` follows a chain of absorbed uids. Folds are
#: acyclic by construction; this only bounds a hand-edited loop.
_MAX_CHAIN = 16


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
    #: Divergent pairs folded onto the ``<uid>``-named copy, then retired.
    duplicate_fold: list[tuple[str, Path, Path]] = field(default_factory=list)
    duplicate_fold_values_to_add: int = 0
    #: Divergent pairs that need a human (see ``duplicate_needs_review``).
    duplicate_divergent: int = 0
    duplicate_review_reasons: dict[str, int] = field(default_factory=dict)
    duplicate_both_prefixed: int = 0
    duplicate_other_shape: int = 0
    orphan_values_to_add: int = 0
    #: Issue athenaeum#2196 — per-address fact entries describing an address
    #: the record carrying them does not itself list, found on ANY scanned
    #: record (independent of its uid's live/absorbed/duplicate state).
    #: Total entry count, across every record.
    orphan_fact_entries: int = 0
    #: Paths of the records above, so ``apply`` knows which to prune without
    #: a second scan. Kept for apply, never printed — same discipline every
    #: other path list on this dataclass already follows.
    orphan_fact_entry_paths: list[Path] = field(default_factory=list)

    @property
    def actionable(self) -> int:
        return (
            len(self.orphan_folds)
            + len(self.duplicate_retire)
            + len(self.duplicate_fold)
            + len(self.orphan_fact_entry_paths)
        )

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
            "duplicate_divergent_to_fold": len(self.duplicate_fold),
            "duplicate_divergent_values_to_add": self.duplicate_fold_values_to_add,
            "duplicate_divergent_reported": self.duplicate_divergent,
            "duplicate_divergent_name_differs": self.duplicate_review_reasons.get(
                "name-differs", 0
            ),
            "duplicate_divergent_validity_differs": self.duplicate_review_reasons.get(
                "validity-differs", 0
            ),
            "duplicate_both_uid_prefixed": self.duplicate_both_prefixed,
            "duplicate_other_shape": self.duplicate_other_shape,
            "orphan_fact_entries": self.orphan_fact_entries,
        }


def plan_contact_record_repair(knowledge_root: Path, contacts_root: Path) -> ContactRepairPlan:
    """Read-only: classify every uid on the contacts surface. Writes nothing."""
    plan = ContactRepairPlan()
    facts = _read_wiki_facts(Path(knowledge_root) / "wiki")
    index = pii.ExcludedRecordIndex(Path(contacts_root))
    records = pii.iter_contact_records(Path(contacts_root))
    plan.records_scanned = len(records)
    # Issue athenaeum#2196: orphan fact entries are a per-RECORD property,
    # independent of the uid-level live/absorbed/duplicate classification
    # below — a record with no duplicate and nothing absorbing it can still
    # carry one (the corrections applier's `remove` branch is the live
    # source), so every scanned record is checked here rather than folded
    # into one of the branches below.
    for record_path in records:
        orphans = orphaned_fact_entries(pii.read_bounce_record(record_path))
        if orphans:
            plan.orphan_fact_entries += sum(len(v) for v in orphans.values())
            plan.orphan_fact_entry_paths.append(record_path)
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
    target_path = preferred_record(index.all_by_uid(canonical_uid), canonical_uid)
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
    prefixed = [p for p in paths if uid_prefixed(p, uid)]
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
    copy_meta, keep_meta = pii.read_bounce_record(copy), pii.read_bounce_record(keep)
    if holds_nothing_more(copy_meta, keep_meta):
        plan.duplicate_retire.append((uid, copy, keep))
        return
    reason = duplicate_needs_review(copy_meta, keep_meta)
    if reason is not None:
        plan.duplicate_divergent += 1
        plan.duplicate_review_reasons[reason] = plan.duplicate_review_reasons.get(reason, 0) + 1
        return
    plan.duplicate_fold.append((uid, copy, keep))
    _, added, _, _ = merge_duplicate_record(keep_meta, copy_meta)
    plan.duplicate_fold_values_to_add += sum(added.values())


@dataclass
class ContactRepairResult:
    """Counts from :func:`apply_contact_record_repair`."""

    fold: ContactFoldSummary = field(default_factory=ContactFoldSummary)
    duplicates_retired: int = 0
    duplicates_folded: DuplicateFoldSummary = field(default_factory=DuplicateFoldSummary)
    #: Issue athenaeum#2196 — orphan fact entries dropped from still-live
    #: records (never counts an entry on a record that was itself retired
    #: by one of the actions above — nothing to prune there).
    orphan_fact_entries_pruned: int = 0
    errors: list[str] = field(default_factory=list)


def apply_contact_record_repair(
    plan: ContactRepairPlan, contacts_root: Path
) -> ContactRepairResult:
    """Act on *plan*: retire subset duplicates, fold divergent ones, then fold absorbed uids."""
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
    folded = result.duplicates_folded
    for uid, copy, keep in plan.duplicate_fold:
        try:
            one = fold_duplicate_record(root, uid, copy, keep)
        except (OSError, UnicodeDecodeError) as exc:
            result.errors.append(f"{type(exc).__name__} folding a divergent duplicate record")
            continue
        folded.copies_folded += one.copies_folded
        for key, count in one.values_added.items():
            folded.values_added[key] = folded.values_added.get(key, 0) + count
        folded.facts_carried += one.facts_carried
        folded.classifications_reconciled += one.classifications_reconciled
        folded.errors.extend(one.errors)
    result.errors.extend(folded.errors)
    index = pii.ExcludedRecordIndex(root)
    result.fold = fold_absorbed_contact_records(
        root,
        [(uid, canonical) for uid, canonical, _ in plan.orphan_folds],
        index=index,
    )
    result.errors.extend(result.fold.errors)

    # Issue athenaeum#2196 (AC4): prune orphan fact entries LAST, re-checking
    # each path fresh rather than trusting the plan's pre-action snapshot —
    # the actions above can rename a path away (retired, no longer exists:
    # skip, nothing to prune) or rewrite one's content (a duplicate-fold
    # target, an absorbed-fold canonical: re-reading picks up the final
    # state rather than a stale one). None of the fold/merge primitives
    # above ever ADD a new orphan (that is what decisions 1-3 of this issue
    # guard against), so this worklist — built once, before any action ran
    # — still covers every record that could have one once they are done.
    for record_path in plan.orphan_fact_entry_paths:
        if not record_path.exists():
            continue
        try:
            text = record_path.read_text(encoding="utf-8")
            meta, body = parse_frontmatter(text)
            meta = meta if isinstance(meta, dict) else {}
            pruned, dropped = prune_orphan_fact_entries(meta)
        except (OSError, UnicodeDecodeError) as exc:
            result.errors.append(f"{type(exc).__name__} reading a record to prune orphan facts")
            continue
        if not dropped:
            continue
        try:
            atomic_write_text(record_path, render_frontmatter(pruned) + "\n" + body)
        except OSError as exc:
            result.errors.append(f"{type(exc).__name__} pruning orphan fact entries")
            continue
        result.orphan_fact_entries_pruned += dropped
    return result
