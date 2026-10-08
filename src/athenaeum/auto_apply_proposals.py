# SPDX-License-Identifier: Apache-2.0
"""Auto-apply-threshold proposal drafter (issue athenaeum#2018, athenaeum#719
Plan step 6).

The self-tuning loop applied to a fourth trigger source: the tier
audit-sampling lane (:mod:`athenaeum.calibration`, issue athenaeum#438)
already measures, for each reasoning tier, how often a human REVIEWER
overturns the tier's own watched verdict (a T1 reject, a T2 approve —
see :data:`athenaeum.calibration._WATCHED_VERDICT`). A high overturn rate
on T1's reject direction means T1 is being too strict — rejecting pairs a
human would have approved — which is exactly the signal that argues for
WIDENING (lowering) the corresponding auto-apply confidence floor
(:func:`athenaeum.resolutions.resolve_auto_apply_threshold_for`) so fewer
borderline cases need a human at all. This module drafts that proposal on
the SAME drafter/ledger/decision-type rail
:mod:`athenaeum.dimension_proposals` (issue athenaeum#2015) and
:mod:`athenaeum.rule_proposals` (issue athenaeum#905) already built.

**Fully deterministic — no LLM, no client, no prompt.** Every field is
derived mechanically from :func:`athenaeum.calibration.calibration_summary`'s
counts and the configured step size
(:func:`athenaeum.config.resolve_auto_apply_proposals_widen_step`); this
module never asks a model anything.

**Never narrows.** A draft is only emitted when the proposed threshold is
STRICTLY LOWER (more permissive — "wider") than the resolver action's
CURRENT resolved threshold; see :func:`draft_auto_apply_threshold_proposal`.
A never-auto-apply action (:func:`athenaeum.resolutions.
resolve_auto_apply_threshold_for` returning ``None``) is never drafted for
either — there is no floor to widen.

**Proposal-only — approving only widens config, never bypasses in-flight
decisions.** Approving a drafted proposal
(:func:`approve_auto_apply_threshold_proposal`) appends an ``approve``
record to this module's OWN ledger; :func:`athenaeum.config.
auto_apply_threshold_ledger_override_for` reads that ledger back as a
new, lower-precedence-than-explicit-config layer inside
:func:`athenaeum.resolutions.resolve_auto_apply_threshold_for`. Both
``resolutions.py``'s and ``verdict_effects.py``'s auto-apply gates decide
auto-apply AT THE MOMENT a verdict is produced, never by re-sweeping a
pending queue later — so a decision already escalated to (and sitting in)
``_pending_questions.md``/``_pending_merges.md`` before this proposal was
even drafted, let alone approved, is never retroactively reopened or
auto-finalized by a later-widened threshold. See
``tests/test_auto_apply_proposals.py`` for the regression proving this.

**Id is the (action, current, proposed) triple.** :func:`proposal_item_id`
hashes all three — not the action alone — so rejecting one proposed widen
step does not permanently suppress every future proposal for that action;
a later pass computing a DIFFERENT proposed value gets a fresh id.

Layering: L4 domain/pipeline module, a peer of
:mod:`athenaeum.dimension_proposals` and :mod:`athenaeum.rule_proposals`.
Imports :mod:`athenaeum.calibration` (L3, for
:func:`~athenaeum.calibration.calibration_summary`), :mod:`athenaeum.
resolutions` (for :func:`~athenaeum.resolutions.
resolve_auto_apply_threshold_for` / :data:`~athenaeum.resolutions.
DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION`), :mod:`athenaeum.config` (L2),
and :mod:`athenaeum.store` (L3). :mod:`athenaeum.decisions` imports this
module for its mapper; this module never imports ``decisions`` back, so
no cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.calibration import calibration_summary
from athenaeum.config import (
    resolve_auto_apply_proposals_disagreement_trigger,
    resolve_auto_apply_proposals_widen_step,
)
from athenaeum.resolutions import resolve_auto_apply_threshold_for
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Schema version stamped on every ledger record.
AUTO_APPLY_PROPOSALS_LEDGER_VERSION = 1

#: Sidecar filename under ``wiki_root``. Consulted directly (as plain
#: JSONL, not through this module) by :func:`athenaeum.config.
#: auto_apply_threshold_ledger_override_for` -- keep the literal in
#: sync if this ever changes.
AUTO_APPLY_PROPOSALS_LEDGER_FILENAME = "_auto_apply_proposals.jsonl"

#: Record kinds. Mirrors :mod:`athenaeum.dimension_proposals` exactly.
PROPOSAL_KIND = "proposal"
APPROVE_KIND = "approve"
REJECT_KIND = "reject"

#: Tiers :func:`athenaeum.calibration.calibration_summary` tracks whose
#: overturn direction argues for WIDENING an auto-apply floor. Only T1
#: (false-reject risk) widens the per-action floor this module targets —
#: see module docstring. T2's watched verdict is "approve" (false-approve
#: risk), the opposite direction: a high T2 overturn rate argues for a
#: NARROWER floor, which this module never drafts (see "Never narrows").
_WIDENING_TIER = "T1"


def default_auto_apply_proposals_ledger_path(wiki_root: Path) -> Path:
    """Default ledger path: ``<wiki_root>/_auto_apply_proposals.jsonl``."""
    return Path(wiki_root) / AUTO_APPLY_PROPOSALS_LEDGER_FILENAME


def _now_iso(now: datetime | None = None) -> str:
    return now_iso(now)


def proposal_item_id(action: str, current_threshold: float, proposed_threshold: float) -> str:
    """Deterministic id for the ``(action, current, proposed)`` triple.

    Deliberately NOT keyed on ``action`` alone (see module docstring): a
    rejected widen-by-0.05 proposal must not suppress a later, differently
    sized widen proposal for the same action.
    """
    payload = json.dumps(
        {
            "action": action,
            "current_threshold": round(current_threshold, 6),
            "proposed_threshold": round(proposed_threshold, 6),
        },
        sort_keys=True,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return digest[:16]


def _append_jsonl_line(path: Path, line: str) -> None:
    append_line_durable(path, line.encode("utf-8"))


def read_auto_apply_proposals_ledger(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed ledger record, tolerating a torn trailing line.

    Same tolerant-reader contract as
    :func:`athenaeum.dimension_proposals.read_dimension_proposals_ledger`.
    """
    target = (
        ledger_path
        if ledger_path is not None
        else default_auto_apply_proposals_ledger_path(wiki_root)
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


def list_pending_auto_apply_threshold_proposals(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Drafted auto-apply-threshold proposals awaiting ratification."""
    records = read_auto_apply_proposals_ledger(wiki_root, ledger_path=ledger_path)
    resolved = _resolved_ids(records)
    return [
        r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) not in resolved
    ]


# ---------------------------------------------------------------------------
# Detection + drafting
# ---------------------------------------------------------------------------


def disagreement_rate(wiki_root: Path, *, tier: str = _WIDENING_TIER) -> float | None:
    """The reviewed-but-overturned rate for *tier* (default ``"T1"``).

    ``None`` when nothing has been reviewed yet for *tier* — an unreviewed
    sample asserts nothing about accuracy, mirrors
    :func:`athenaeum.calibration.default_acceptance_rubber_stamp_rate`'s
    own ``None``-until-reviewed contract.
    """
    bucket = calibration_summary(Path(wiki_root)).get(tier)
    if not bucket or bucket.get("reviewed", 0) <= 0:
        return None
    return bucket["overturned"] / bucket["reviewed"]


@dataclass(frozen=True)
class AutoApplyThresholdProposalDraft:
    """One drafted proposal: widen one resolver action's auto-apply floor."""

    id: str
    action: str
    tier: str
    current_threshold: float
    proposed_threshold: float
    step: float
    disagreement_rate_value: float
    trigger: float
    sampled: int
    reviewed: int
    overturned: int

    def to_ledger_record(self, *, now: datetime | None = None) -> dict[str, Any]:
        return {
            "v": AUTO_APPLY_PROPOSALS_LEDGER_VERSION,
            "kind": PROPOSAL_KIND,
            "id": self.id,
            "created_at": _now_iso(now),
            "action": self.action,
            "tier": self.tier,
            "current_threshold": self.current_threshold,
            "proposed_threshold": self.proposed_threshold,
            "step": self.step,
            "disagreement_rate": self.disagreement_rate_value,
            "trigger": self.trigger,
            "sampled": self.sampled,
            "reviewed": self.reviewed,
            "overturned": self.overturned,
        }


def draft_auto_apply_threshold_proposal(
    action: str,
    *,
    wiki_root: Path,
    config: dict[str, Any] | None = None,
    tier: str = _WIDENING_TIER,
) -> AutoApplyThresholdProposalDraft | None:
    """Draft one widen proposal for *action*, or ``None`` if not warranted.

    ``None`` when: the tier's disagreement rate is unavailable or below
    :func:`athenaeum.config.resolve_auto_apply_proposals_disagreement_trigger`;
    *action* never auto-applies (:func:`athenaeum.resolutions.
    resolve_auto_apply_threshold_for` returns ``None``); or the computed
    step would not actually widen the floor (clamped to ``0.0`` and
    already at or below it) — see module docstring's "Never narrows".
    """
    rate = disagreement_rate(wiki_root, tier=tier)
    trigger = resolve_auto_apply_proposals_disagreement_trigger(config)
    if rate is None or rate < trigger:
        return None

    current = resolve_auto_apply_threshold_for(config, action, wiki_root=Path(wiki_root))
    if current is None:
        return None

    step = resolve_auto_apply_proposals_widen_step(config)
    proposed = max(0.0, round(current - step, 6))
    if proposed >= current:
        return None

    bucket = calibration_summary(Path(wiki_root)).get(tier, {})
    item_id = proposal_item_id(action, current, proposed)
    return AutoApplyThresholdProposalDraft(
        id=item_id,
        action=action,
        tier=tier,
        current_threshold=current,
        proposed_threshold=proposed,
        step=step,
        disagreement_rate_value=rate,
        trigger=trigger,
        sampled=bucket.get("sampled", 0),
        reviewed=bucket.get("reviewed", 0),
        overturned=bucket.get("overturned", 0),
    )


@dataclass
class AutoApplyProposalRunSummary:
    """Outcome of one :func:`run_auto_apply_proposal_detection` pass."""

    actions_seen: int = 0
    not_warranted: int = 0
    proposed: int = 0
    skipped_pending: int = 0
    skipped_suppressed: int = 0
    drafts: list[AutoApplyThresholdProposalDraft] = field(default_factory=list)


def run_auto_apply_proposal_detection(
    actions: list[str],
    *,
    wiki_root: Path,
    config: dict[str, Any] | None = None,
    tier: str = _WIDENING_TIER,
    ledger_path: Path | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> AutoApplyProposalRunSummary:
    """Detect + draft: for each resolver action in *actions*, draft a widen
    proposal if warranted.

    Idempotent per ``(action, current, proposed)`` id: a pending or
    rejected proposal for the SAME triple is never re-drafted, mirroring
    :func:`athenaeum.dimension_proposals.run_dimension_proposal_drafting`.

    *dry_run* (default ``False``): when true, drafts are computed and
    returned in the summary but NOT appended to the ledger.
    """
    summary = AutoApplyProposalRunSummary()
    wiki_root = Path(wiki_root)
    summary.actions_seen = len(actions)
    if not actions:
        return summary

    records = read_auto_apply_proposals_ledger(wiki_root, ledger_path=ledger_path)
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
        else default_auto_apply_proposals_ledger_path(wiki_root)
    )

    for action in actions:
        draft = draft_auto_apply_threshold_proposal(
            action, wiki_root=wiki_root, config=config, tier=tier
        )
        if draft is None:
            summary.not_warranted += 1
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
# Resolution: approve (widens config only) / reject
# ---------------------------------------------------------------------------


def approve_auto_apply_threshold_proposal(
    wiki_root: Path,
    *,
    proposal_id: str,
    note: str = "",
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Approve one pending proposal.

    **Widens config only — never bypasses in-flight decisions** (see
    module docstring). This appends an ``approve`` record carrying the
    proposal's own ``action``/``proposed_threshold``; that record is what
    :func:`athenaeum.config.auto_apply_threshold_ledger_override_for`
    reads back as the new floor. It never touches
    ``_pending_questions.md``, ``_pending_merges.md``, or any ALREADY
    DECIDED verdict — those were finalized at the moment the verdict was
    produced, not by a later sweep this ledger could retroactively affect.

    Raises :class:`ValueError` for an unknown or already-resolved
    *proposal_id*.
    """
    wiki_root = Path(wiki_root)
    records = read_auto_apply_proposals_ledger(wiki_root, ledger_path=ledger_path)
    proposal = next(
        (r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) == proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError(f"unknown auto-apply-threshold proposal id: {proposal_id!r}")
    if proposal_id in _resolved_ids(records):
        raise ValueError(f"auto-apply-threshold proposal already resolved: {proposal_id!r}")

    record = {
        "v": AUTO_APPLY_PROPOSALS_LEDGER_VERSION,
        "kind": APPROVE_KIND,
        "id": proposal_id,
        "created_at": _now_iso(now),
        "answered_at": _now_iso(now),
        "action": proposal.get("action"),
        "proposed_threshold": proposal.get("proposed_threshold"),
        "note": note,
    }
    target = (
        ledger_path
        if ledger_path is not None
        else default_auto_apply_proposals_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, sort_keys=True) + "\n")
    log.info(
        "athenaeum#2018: approved auto-apply-threshold proposal %s (action=%s -> %s)",
        proposal_id,
        proposal.get("action"),
        proposal.get("proposed_threshold"),
    )
    return record


def reject_auto_apply_threshold_proposal(
    wiki_root: Path,
    *,
    proposal_id: str,
    note: str = "",
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reject one pending proposal: records the rejection, which permanently
    suppresses the underlying ``(action, current, proposed)`` triple.

    Raises :class:`ValueError` for an unknown or already-resolved
    *proposal_id*.
    """
    wiki_root = Path(wiki_root)
    records = read_auto_apply_proposals_ledger(wiki_root, ledger_path=ledger_path)
    proposal = next(
        (r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) == proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError(f"unknown auto-apply-threshold proposal id: {proposal_id!r}")
    if proposal_id in _resolved_ids(records):
        raise ValueError(f"auto-apply-threshold proposal already resolved: {proposal_id!r}")

    record = {
        "v": AUTO_APPLY_PROPOSALS_LEDGER_VERSION,
        "kind": REJECT_KIND,
        "id": proposal_id,
        "created_at": _now_iso(now),
        "answered_at": _now_iso(now),
        "note": note,
    }
    target = (
        ledger_path
        if ledger_path is not None
        else default_auto_apply_proposals_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, sort_keys=True) + "\n")
    log.info("athenaeum#2018: rejected auto-apply-threshold proposal %s", proposal_id)
    return record


__all__ = [
    "AUTO_APPLY_PROPOSALS_LEDGER_VERSION",
    "AUTO_APPLY_PROPOSALS_LEDGER_FILENAME",
    "PROPOSAL_KIND",
    "APPROVE_KIND",
    "REJECT_KIND",
    "default_auto_apply_proposals_ledger_path",
    "proposal_item_id",
    "read_auto_apply_proposals_ledger",
    "list_pending_auto_apply_threshold_proposals",
    "disagreement_rate",
    "AutoApplyThresholdProposalDraft",
    "draft_auto_apply_threshold_proposal",
    "AutoApplyProposalRunSummary",
    "run_auto_apply_proposal_detection",
    "approve_auto_apply_threshold_proposal",
    "reject_auto_apply_threshold_proposal",
]
