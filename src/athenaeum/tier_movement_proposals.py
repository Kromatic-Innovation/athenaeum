# SPDX-License-Identifier: Apache-2.0
"""Tier-movement proposal drafter (issue athenaeum#2019, athenaeum#719 Plan step 6).

The self-tuning loop applied to a usage-metric threshold: when
:func:`athenaeum.usage_report.compute_usage_report` shows a claim pushed at
least ``pushed_min`` times while being referenced at most ``referenced_max``
times in the trailing ``window_days`` window, this module drafts a queue
item a human ratifies — never an automatic mutation.

**Advisory only — there is no tier left to move a claim into.**
athenaeum#1514 retired the ``memory_tier`` vocabulary (``hot``/``warm``/``cold``/
``refused``) and the promote-on-use sweep that moved claims between them;
``athenaeum.memory_tiers`` no longer exists (see ``CHANGELOG.md``'s
"Removed" entry for that issue). ``cold``/``refused`` survive as the
unrelated, already-enforced ``storage.is_embedded`` and
:mod:`athenaeum.never_ingest` mechanisms, neither of which this module
touches. A drafted proposal here therefore carries
:data:`PROPOSED_ACTION_ADVISORY_REVIEW` — a flag that a human should look at
this claim's usage pattern — and NOTHING ELSE; there is no executor, and
approving one today only records the human's call, exactly like
``dimension-proposal``'s pre-ratification-child state
(:mod:`athenaeum.dimension_proposals`). **No ledger record this module
writes may ever carry the retired ``hot``/``warm``/``cold``/``refused``
tokens** — see ``tests/test_tier_movement_proposals.py``'s
``test_ledger_record_never_carries_retired_tier_vocabulary`` for the
regression this encodes.

**`tiers.py` holds nothing for this trigger.** That module is the intake
pipeline's tier1-tier4 classify/escalate machinery (name collision with the
retired retrieval-cost tier vocabulary, unrelated concepts) and has no usage
metrics at all. :func:`athenaeum.usage_report.compute_usage_report` — the
ONE documented interface for this signal (see that module's own "interface
athenaeum#718 consumes" note, which this module obeys identically, and
:mod:`athenaeum.audit_queue`'s re-audit queue, the only other live
consumer) — is this drafter's entire trigger source.

**Deterministic — no LLM, no client.** Every field of a drafted proposal is
read straight off a :class:`~athenaeum.usage_report.ClaimUsage` plus the
static action constant; mirrors :mod:`athenaeum.dimension_proposals`'s own
"fully deterministic" posture.

**Ledger-only; no resolution path in this module.** Same precedent as
:mod:`athenaeum.dimension_proposals`: this module detects, drafts, and
appends to ``wiki/_tier_movement_proposals.jsonl``.
:mod:`athenaeum.decisions` reads that ledger for display,
:mod:`athenaeum.decision_framing` frames the item, and nothing in this
module or those two ever mutates a claim's storage state.

Layering: L4 domain/pipeline module, a peer of
:mod:`athenaeum.dimension_proposals`. Imports :mod:`athenaeum.usage_report`
(L3) and :mod:`athenaeum.store` (L3, for the shared durable-append/now_iso
primitives); :mod:`athenaeum.config` is imported function-locally in
:func:`run_tier_movement_proposal_drafting` only, mirroring
:mod:`athenaeum.dimension_proposals`'s module-level import of the same
config resolver (no cycle risk either way, but keeping the import local here
matches this module's own test fixtures, which construct drafts directly
without ever needing config).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum.store import append_line_durable, now_iso
from athenaeum.usage_report import ClaimUsage, compute_usage_report

log = logging.getLogger(__name__)

#: Schema version stamped on every ledger record.
TIER_MOVEMENT_PROPOSALS_LEDGER_VERSION = 1

#: Sidecar filename under ``wiki_root``, alongside ``_dimension_proposals.jsonl``.
TIER_MOVEMENT_PROPOSALS_LEDGER_FILENAME = "_tier_movement_proposals.jsonl"

#: Record kinds. Only ``proposal`` is ever written by this module.
PROPOSAL_KIND = "proposal"
APPROVE_KIND = "approve"
REJECT_KIND = "reject"

#: The one action this drafter may ever propose. See module docstring —
#: NEVER a retired tier-vocabulary token.
PROPOSED_ACTION_ADVISORY_REVIEW = "advisory-review"

#: Retired vocabulary (athenaeum#1514) that must never appear in a record this
#: module writes. Checked by this module's own regression test.
_RETIRED_TIER_TOKENS: frozenset[str] = frozenset({"hot", "warm", "cold", "refused"})


def default_tier_movement_proposals_ledger_path(wiki_root: Path) -> Path:
    """Default ledger path: ``<wiki_root>/_tier_movement_proposals.jsonl``."""
    return Path(wiki_root) / TIER_MOVEMENT_PROPOSALS_LEDGER_FILENAME


def _now_iso(now: datetime | None = None) -> str:
    return now_iso(now)


def proposal_item_id(usage: ClaimUsage, *, window_days: int) -> str:
    """Deterministic id for one ``(claim id, usage snapshot, window)`` triple.

    NOT per-event — mirrors :func:`athenaeum.dimension_proposals.proposal_item_id`:
    a re-drafted proposal for the SAME claim with the SAME usage counts and
    window is the same id, so a rejected one stays suppressed by
    set-membership alone.
    """
    payload = json.dumps(
        {
            "id": usage.id,
            "pushed_count": usage.pushed_count,
            "referenced_count": usage.referenced_count,
            "window_days": window_days,
        },
        sort_keys=True,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return digest[:16]


def _append_jsonl_line(path: Path, line: str) -> None:
    append_line_durable(path, line.encode("utf-8"))


def read_tier_movement_proposals_ledger(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed ledger record, tolerating a torn trailing line."""
    target = (
        ledger_path
        if ledger_path is not None
        else default_tier_movement_proposals_ledger_path(wiki_root)
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


def list_pending_tier_movement_proposals(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Drafted tier-movement proposals awaiting ratification."""
    records = read_tier_movement_proposals_ledger(wiki_root, ledger_path=ledger_path)
    resolved = _resolved_ids(records)
    return [
        r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) not in resolved
    ]


@dataclass(frozen=True)
class TierMovementProposalDraft:
    """One drafted proposal: a claim's usage snapshot + the advisory action."""

    id: str
    claim_id: str
    pushed_count: int
    referenced_count: int
    window_days: int
    pushed_min: int
    referenced_max: int
    proposed_action: str
    rationale: str

    def to_ledger_record(self, *, now: datetime | None = None) -> dict[str, Any]:
        return {
            "v": TIER_MOVEMENT_PROPOSALS_LEDGER_VERSION,
            "kind": PROPOSAL_KIND,
            "id": self.id,
            "created_at": _now_iso(now),
            "claim_id": self.claim_id,
            "pushed_count": self.pushed_count,
            "referenced_count": self.referenced_count,
            "window_days": self.window_days,
            "pushed_min": self.pushed_min,
            "referenced_max": self.referenced_max,
            "proposed_action": self.proposed_action,
            "rationale": self.rationale,
        }


def draft_tier_movement_proposal(
    usage: ClaimUsage,
    *,
    window_days: int,
    pushed_min: int,
    referenced_max: int,
) -> TierMovementProposalDraft:
    """Draft one advisory proposal out of *usage*'s crossed threshold.

    Deterministic — every field is derived from *usage* and the static
    defaults below; no LLM call, no client, no randomness. See module
    docstring for why :data:`PROPOSED_ACTION_ADVISORY_REVIEW` is the only
    action this ever proposes.
    """
    item_id = proposal_item_id(usage, window_days=window_days)
    rationale = (
        f"Pushed {usage.pushed_count} time(s) but referenced "
        f"{usage.referenced_count} time(s) in the trailing {window_days}d "
        f"window (threshold: pushed >= {pushed_min}, referenced <= "
        f"{referenced_max}). No automatic tier exists to move this claim "
        f"into (athenaeum#1514 retired the memory-tier vocabulary and its "
        f"promote-on-use sweep) -- this is a human review flag only."
    )
    return TierMovementProposalDraft(
        id=item_id,
        claim_id=usage.id,
        pushed_count=usage.pushed_count,
        referenced_count=usage.referenced_count,
        window_days=window_days,
        pushed_min=pushed_min,
        referenced_max=referenced_max,
        proposed_action=PROPOSED_ACTION_ADVISORY_REVIEW,
        rationale=rationale,
    )


@dataclass
class TierMovementProposalRunSummary:
    """Outcome of one :func:`run_tier_movement_proposal_drafting` pass."""

    candidates_seen: int = 0
    proposed: int = 0
    skipped_pending: int = 0
    skipped_suppressed: int = 0
    skipped_below_threshold: int = 0
    drafts: list[TierMovementProposalDraft] = field(default_factory=list)


def run_tier_movement_proposal_drafting(
    *,
    wiki_root: Path,
    cache_dir: Path | None = None,
    config: dict[str, Any] | None = None,
    usages: dict[str, ClaimUsage] | None = None,
    ledger_path: Path | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> TierMovementProposalRunSummary:
    """Detect + draft: usage records crossing the configured threshold ->
    drafted, ledgered proposals.

    Gated by :func:`athenaeum.config.resolve_tier_movement_proposals_enabled`
    (default OFF, per athenaeum#719's DoD: a self-tuning trigger lands dark
    behind a documented config key until the operator opts in). Returns an
    empty summary, doing no I/O at all, when the key is off.

    *usages*, optional: a pre-computed ``{id: ClaimUsage}`` map (mainly for
    tests). ``None`` (the default) computes it via
    :func:`athenaeum.usage_report.compute_usage_report` over the resolved
    window — the ONE sanctioned read of the push-metrics ledgers (see module
    docstring).

    Idempotent per candidate id: a pending or rejected (claim, usage
    snapshot, window) triple is never re-drafted, mirroring
    :func:`athenaeum.dimension_proposals.run_dimension_proposal_drafting`.

    *dry_run* (default ``False``): drafts are computed and returned in the
    summary but NOT appended to the ledger.
    """
    from athenaeum.config import (
        resolve_tier_movement_proposals_enabled,
        resolve_tier_movement_pushed_min,
        resolve_tier_movement_referenced_max,
        resolve_tier_movement_window_days,
    )

    summary = TierMovementProposalRunSummary()
    if not resolve_tier_movement_proposals_enabled(config):
        return summary

    wiki_root = Path(wiki_root)
    window_days = resolve_tier_movement_window_days(config)
    pushed_min = resolve_tier_movement_pushed_min(config)
    referenced_max = resolve_tier_movement_referenced_max(config)

    if usages is None:
        reference_now = now or datetime.now(timezone.utc)
        since = reference_now - timedelta(days=window_days)
        usages = compute_usage_report(cache_dir=cache_dir, since=since, wiki_root=wiki_root)

    records = read_tier_movement_proposals_ledger(wiki_root, ledger_path=ledger_path)
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
        else default_tier_movement_proposals_ledger_path(wiki_root)
    )

    for usage in usages.values():
        summary.candidates_seen += 1
        if usage.pushed_count < pushed_min or usage.referenced_count > referenced_max:
            summary.skipped_below_threshold += 1
            continue

        item_id = proposal_item_id(usage, window_days=window_days)
        if item_id in rejected_ids:
            summary.skipped_suppressed += 1
            continue
        if item_id in pending_ids:
            summary.skipped_pending += 1
            continue

        draft = draft_tier_movement_proposal(
            usage,
            window_days=window_days,
            pushed_min=pushed_min,
            referenced_max=referenced_max,
        )
        summary.drafts.append(draft)

        if dry_run:
            continue

        _append_jsonl_line(
            target, json.dumps(draft.to_ledger_record(now=now), sort_keys=True) + "\n"
        )
        pending_ids.add(item_id)
        summary.proposed += 1

    return summary


__all__ = [
    "TIER_MOVEMENT_PROPOSALS_LEDGER_VERSION",
    "TIER_MOVEMENT_PROPOSALS_LEDGER_FILENAME",
    "PROPOSAL_KIND",
    "APPROVE_KIND",
    "REJECT_KIND",
    "PROPOSED_ACTION_ADVISORY_REVIEW",
    "default_tier_movement_proposals_ledger_path",
    "proposal_item_id",
    "read_tier_movement_proposals_ledger",
    "list_pending_tier_movement_proposals",
    "TierMovementProposalDraft",
    "draft_tier_movement_proposal",
    "TierMovementProposalRunSummary",
    "run_tier_movement_proposal_drafting",
]
