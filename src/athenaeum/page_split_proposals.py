# SPDX-License-Identifier: Apache-2.0
"""Page-split proposal drafter (issue athenaeum#2018, athenaeum#719 Plan step 6).

The self-tuning loop applied to a third trigger source: an aggregate page
:mod:`athenaeum.page_decompose` already knows how to diagnose (via
:func:`~athenaeum.page_decompose.build_report`) and whose
coordinate-heterogeneity :func:`~athenaeum.page_decompose.
measure_coordinate_heterogeneity` already knows how to measure. This
module turns a report whose heterogeneity crosses a configurable trigger
into one deterministic, ledgered proposal a human can ratify — the SAME
drafter/ledger/decision-type rail :mod:`athenaeum.dimension_proposals`
(issue athenaeum#2015) and :mod:`athenaeum.rule_proposals` (issue athenaeum#905)
already built, reused here rather than invented a third time.

**Fully deterministic — no LLM, no client, no prompt.** Every field of a
drafted proposal is derived mechanically from a
:class:`~athenaeum.page_decompose.DecomposeReport` the caller already
built; this module makes no model call and holds no opinion about WHICH
bullets should move where — that plan already exists on the report
:func:`~athenaeum.page_decompose.apply_report` would act on.

**Proposal-only — never performs the split.** Approving a drafted
proposal (:func:`approve_page_split_proposal`) records the approval and
hands the page's ALREADY-BUILT report back to the caller as a plan for a
human-triggered follow-up (re-running
:func:`athenaeum.page_decompose.apply_report` with ``--apply`` is a
separate, explicit operator action) — it never calls
:func:`~athenaeum.page_decompose.apply_report` itself. This mirrors how
:func:`athenaeum.rule_proposals.approve_rule_proposal` writes a rule in
OBSERVE mode rather than ever flipping it live: ratifying a proposal here
authorizes a split, it does not execute one.

**Id is the page identity plus the plan.** :func:`proposal_item_id` hashes
``(source_uid, sorted distinct resolved subject uids)`` — not the page
alone — so a rejection suppresses only THIS plan for THIS page; if the
page's bullets later resolve to a materially different set of subjects
(a different plan), a fresh proposal can be drafted, mirroring
:func:`athenaeum.dimension_proposals.proposal_item_id`'s per-pair (not
per-shape-alone) keying discipline.

**Ledger-only; no resolution-side mutation beyond the ledger itself.**
This module only detects, drafts, appends to
``wiki/_page_split_proposals.jsonl``, and records approve/reject.
:mod:`athenaeum.decisions` reads that ledger for display,
:mod:`athenaeum.decision_framing` frames it two-way (approve/reject), and
:mod:`athenaeum.decision_answers` dispatches an answer to
:func:`approve_page_split_proposal` / :func:`reject_page_split_proposal`
— neither of which ever calls :mod:`athenaeum.page_decompose`'s own
``apply_report``/``atomic_write_text`` write path.

Layering: L4 domain/pipeline module, a peer of
:mod:`athenaeum.dimension_proposals` and :mod:`athenaeum.rule_proposals`.
Imports :mod:`athenaeum.page_decompose` (L4, for
:class:`~athenaeum.page_decompose.DecomposeReport` /
:func:`~athenaeum.page_decompose.measure_coordinate_heterogeneity` —
same-layer import, allowed), :mod:`athenaeum.config` (L2), and
:mod:`athenaeum.store` (L3, for the shared durable-append/now_iso
primitives). :mod:`athenaeum.decisions` imports this module for its
mapper; this module never imports ``decisions`` back, so no cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_page_split_proposals_heterogeneity_threshold
from athenaeum.page_decompose import DecomposeReport, measure_coordinate_heterogeneity
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Schema version stamped on every ledger record.
PAGE_SPLIT_PROPOSALS_LEDGER_VERSION = 1

#: Sidecar filename under ``wiki_root``, alongside ``_dimension_proposals.jsonl``
#: / ``_rule_proposals.jsonl``.
PAGE_SPLIT_PROPOSALS_LEDGER_FILENAME = "_page_split_proposals.jsonl"

#: Record kinds. Mirrors :mod:`athenaeum.dimension_proposals` exactly.
PROPOSAL_KIND = "proposal"
APPROVE_KIND = "approve"
REJECT_KIND = "reject"


def default_page_split_proposals_ledger_path(wiki_root: Path) -> Path:
    """Default ledger path: ``<wiki_root>/_page_split_proposals.jsonl``."""
    return Path(wiki_root) / PAGE_SPLIT_PROPOSALS_LEDGER_FILENAME


def _now_iso(now: datetime | None = None) -> str:
    return now_iso(now)


def proposal_item_id(source_uid: str, subject_uids: tuple[str, ...]) -> str:
    """Deterministic id for the ``(source_uid, plan)`` pair.

    The plan is the sorted, deduplicated set of distinct resolved subject
    uids the report's bullets named — NOT the page alone (see module
    docstring): a rejected (page, plan) pair is permanently suppressed by
    set-membership alone, but a later, materially different plan for the
    same page gets a fresh id.
    """
    payload = json.dumps(
        {"source_uid": source_uid, "subject_uids": sorted(set(subject_uids))},
        sort_keys=True,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return digest[:16]


def _append_jsonl_line(path: Path, line: str) -> None:
    append_line_durable(path, line.encode("utf-8"))


def read_page_split_proposals_ledger(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed ledger record, tolerating a torn trailing line.

    Same tolerant-reader contract as
    :func:`athenaeum.dimension_proposals.read_dimension_proposals_ledger`.
    """
    target = (
        ledger_path
        if ledger_path is not None
        else default_page_split_proposals_ledger_path(wiki_root)
    )
    if not target.exists():
        return []
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _kind_ids(records: list[dict[str, Any]], kind: str) -> set[str]:
    return {str(r.get("id")) for r in records if r.get("kind") == kind}


def _resolved_ids(records: list[dict[str, Any]]) -> set[str]:
    return _kind_ids(records, APPROVE_KIND) | _kind_ids(records, REJECT_KIND)


def list_pending_page_split_proposals(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Drafted page-split proposals awaiting ratification."""
    records = read_page_split_proposals_ledger(wiki_root, ledger_path=ledger_path)
    resolved = _resolved_ids(records)
    return [
        r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) not in resolved
    ]


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageSplitProposalDraft:
    """One drafted proposal: a candidate page-split plan."""

    id: str
    source_uid: str
    source_name: str
    source_path: str
    subject_until: str
    heterogeneity: int
    threshold: int
    subject_uids: tuple[str, ...]
    subjects_resolved: int
    subjects_unresolved: int
    bullet_count: int

    def to_ledger_record(self, *, now: datetime | None = None) -> dict[str, Any]:
        return {
            "v": PAGE_SPLIT_PROPOSALS_LEDGER_VERSION,
            "kind": PROPOSAL_KIND,
            "id": self.id,
            "created_at": _now_iso(now),
            "source_uid": self.source_uid,
            "source_name": self.source_name,
            "source_path": self.source_path,
            # Carried so a human-triggered follow-up can rebuild the SAME
            # DecomposeReport page_decompose.apply_report needs -- without
            # this, the approval record alone cannot reconstruct the
            # subject-span regex build_report was originally called with.
            "subject_until": self.subject_until,
            "heterogeneity": self.heterogeneity,
            "threshold": self.threshold,
            "subject_uids": list(self.subject_uids),
            "subjects_resolved": self.subjects_resolved,
            "subjects_unresolved": self.subjects_unresolved,
            "bullet_count": self.bullet_count,
        }


def draft_page_split_proposal(
    report: DecomposeReport, *, config: dict[str, Any] | None = None
) -> PageSplitProposalDraft | None:
    """Draft one proposal for *report*, or ``None`` if under the trigger.

    Deterministic — every field is derived from *report* itself; no LLM
    call, no client, no randomness.
    """
    heterogeneity = measure_coordinate_heterogeneity(report)
    threshold = resolve_page_split_proposals_heterogeneity_threshold(config)
    if heterogeneity < threshold:
        return None

    subject_uids = tuple(sorted({b.uid for b in report.bullets if b.uid}))
    item_id = proposal_item_id(report.source_uid, subject_uids)
    return PageSplitProposalDraft(
        id=item_id,
        source_uid=report.source_uid,
        source_name=report.source_name,
        source_path=report.source_path,
        subject_until=report.subject_until,
        heterogeneity=heterogeneity,
        threshold=threshold,
        subject_uids=subject_uids,
        subjects_resolved=report.subjects_resolved,
        subjects_unresolved=report.subjects_unresolved,
        bullet_count=len(report.bullets),
    )


@dataclass
class PageSplitProposalRunSummary:
    """Outcome of one :func:`run_page_split_proposal_detection` pass."""

    reports_seen: int = 0
    under_threshold: int = 0
    proposed: int = 0
    skipped_pending: int = 0
    skipped_suppressed: int = 0
    drafts: list[PageSplitProposalDraft] = field(default_factory=list)


def run_page_split_proposal_detection(
    reports: list[DecomposeReport],
    *,
    wiki_root: Path,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> PageSplitProposalRunSummary:
    """Detect + draft: reports whose heterogeneity crosses the configured
    trigger -> drafted, ledgered proposals.

    Idempotent per (page, plan) id: a pending or rejected proposal is never
    re-drafted (checked before any work is done, mirroring
    :func:`athenaeum.dimension_proposals.run_dimension_proposal_drafting`).

    *dry_run* (default ``False``): when true, drafts are computed and
    returned in the summary but NOT appended to the ledger.
    """
    summary = PageSplitProposalRunSummary()
    wiki_root = Path(wiki_root)
    summary.reports_seen = len(reports)
    if not reports:
        return summary

    records = read_page_split_proposals_ledger(wiki_root, ledger_path=ledger_path)
    resolved_ids = _resolved_ids(records)
    rejected_ids = _kind_ids(records, REJECT_KIND)
    pending_ids = {
        str(r.get("id"))
        for r in records
        if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) not in resolved_ids
    }

    target = (
        ledger_path
        if ledger_path is not None
        else default_page_split_proposals_ledger_path(wiki_root)
    )

    for report in reports:
        draft = draft_page_split_proposal(report, config=config)
        if draft is None:
            summary.under_threshold += 1
            continue

        if draft.id in rejected_ids:
            summary.skipped_suppressed += 1
            continue
        if draft.id in pending_ids:
            summary.skipped_pending += 1
            continue

        summary.drafts.append(draft)
        if dry_run:
            continue

        _append_jsonl_line(
            target, json.dumps(draft.to_ledger_record(now=now), sort_keys=True) + "\n"
        )
        pending_ids.add(draft.id)
        summary.proposed += 1

    return summary


# ---------------------------------------------------------------------------
# Resolution: approve (proposal-only -- never splits) / reject
# ---------------------------------------------------------------------------


def approve_page_split_proposal(
    wiki_root: Path,
    *,
    proposal_id: str,
    note: str = "",
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Approve one pending proposal.

    **Proposal-only (see module docstring): this NEVER calls**
    :func:`athenaeum.page_decompose.apply_report` **or writes to any wiki
    page.** It only records the approval. The returned record carries the
    proposal's own ``source_uid``/``source_path``/``subject_uids`` so a
    caller (CLI, MCP tool, a human) can hand them to
    :func:`~athenaeum.page_decompose.apply_report` as an explicit,
    separate, human-triggered follow-up — the split-then-ratify boundary
    issue athenaeum#719 draws everywhere else in this self-tuning loop.

    Raises :class:`ValueError` for an unknown or already-resolved
    *proposal_id* — each proposal is resolved at most once, mirroring
    :func:`athenaeum.rule_proposals.approve_rule_proposal`'s guard.
    """
    wiki_root = Path(wiki_root)
    records = read_page_split_proposals_ledger(wiki_root, ledger_path=ledger_path)
    proposal = next(
        (r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) == proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError(f"unknown page-split proposal id: {proposal_id!r}")
    if proposal_id in _resolved_ids(records):
        raise ValueError(f"page-split proposal already resolved: {proposal_id!r}")

    record = {
        "v": PAGE_SPLIT_PROPOSALS_LEDGER_VERSION,
        "kind": APPROVE_KIND,
        "id": proposal_id,
        "created_at": _now_iso(now),
        "answered_at": _now_iso(now),
        "source_uid": proposal.get("source_uid"),
        "source_path": proposal.get("source_path"),
        "subject_until": proposal.get("subject_until"),
        "subject_uids": proposal.get("subject_uids", []),
        "note": note,
    }
    target = (
        ledger_path
        if ledger_path is not None
        else default_page_split_proposals_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, sort_keys=True) + "\n")
    log.info(
        "athenaeum#2018: approved page-split proposal %s for %s (plan only, no split performed)",
        proposal_id,
        proposal.get("source_uid"),
    )
    return record


def reject_page_split_proposal(
    wiki_root: Path,
    *,
    proposal_id: str,
    note: str = "",
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reject one pending proposal: records the rejection, which permanently
    suppresses the underlying ``(source_uid, plan)`` pair.

    Raises :class:`ValueError` for an unknown or already-resolved
    *proposal_id*.
    """
    wiki_root = Path(wiki_root)
    records = read_page_split_proposals_ledger(wiki_root, ledger_path=ledger_path)
    proposal = next(
        (r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) == proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError(f"unknown page-split proposal id: {proposal_id!r}")
    if proposal_id in _resolved_ids(records):
        raise ValueError(f"page-split proposal already resolved: {proposal_id!r}")

    record = {
        "v": PAGE_SPLIT_PROPOSALS_LEDGER_VERSION,
        "kind": REJECT_KIND,
        "id": proposal_id,
        "created_at": _now_iso(now),
        "answered_at": _now_iso(now),
        "note": note,
    }
    target = (
        ledger_path
        if ledger_path is not None
        else default_page_split_proposals_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, sort_keys=True) + "\n")
    log.info("athenaeum#2018: rejected page-split proposal %s", proposal_id)
    return record


__all__ = [
    "PAGE_SPLIT_PROPOSALS_LEDGER_VERSION",
    "PAGE_SPLIT_PROPOSALS_LEDGER_FILENAME",
    "PROPOSAL_KIND",
    "APPROVE_KIND",
    "REJECT_KIND",
    "default_page_split_proposals_ledger_path",
    "proposal_item_id",
    "read_page_split_proposals_ledger",
    "list_pending_page_split_proposals",
    "PageSplitProposalDraft",
    "draft_page_split_proposal",
    "PageSplitProposalRunSummary",
    "run_page_split_proposal_detection",
    "approve_page_split_proposal",
    "reject_page_split_proposal",
]
