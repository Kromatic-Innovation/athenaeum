# SPDX-License-Identifier: Apache-2.0
"""Policy-pack-edit proposal drafter (issue athenaeum#2019, athenaeum#719 Plan step 6).

athenaeum#719's own AC states it plainly: "Policy-pack edits are proposal-only and
never auto-applied — policy changes are irreversible-class decisions." This
module is the drafter half of that sentence; the ratification half
(approving a proposal into a real edit of the live pack) is the next
child's job, exactly as :mod:`athenaeum.dimension_proposals` leaves
ratification to its own next child.

**The artifact a proposal edits.** A policy pack is
:class:`athenaeum.erasure.RetentionPack` — a named, ordered tuple of
:class:`athenaeum.erasure.RetentionRule` entries (``(memory_class,
data_class, jurisdiction) -> action[, period]``) plus a
``default_action``/``default_period`` fallback. This module never imports
:mod:`athenaeum.erasure` (no cycle risk either way, but the proposal payload
below is a plain, pack-library-agnostic diff shape — see
:class:`PolicyPackEditDraft` — so a human ratifying later reads a diff, not
a live object graph) and never mutates
``src/athenaeum/retention_packs/`` or any pack the operator has configured.

**No detector exists yet.** Unlike the tier-movement half of this issue
(triggered mechanically off :func:`athenaeum.usage_report.compute_usage_report`),
nothing in this codebase today measures "this pack's rule for
``(memory_class, data_class, jurisdiction)`` should change." This module
therefore exposes a drafting ENTRY POINT —
:func:`draft_policy_pack_edit_proposal` — that a future detector (or an
operator-triggered CLI command, out of this issue's scope) calls with the
pack id and the proposed diff already computed; it does not itself scan
anything. This is a known, stated limitation, not a silent one.

**Irreversible by framing, enforced here by construction.** The proposed
diff is carried as DATA (old rule / new rule, or an old/new
default_action+default_period pair) — this module never executes it
against a live :class:`~athenaeum.erasure.RetentionPack`. Combined with
:mod:`athenaeum.decision_framing`'s ``"policy-pack-edit"`` entry
(``REVERSIBILITY_IRREVERSIBLE``, ``ROUTING_AUTHORITY``) and
:mod:`athenaeum.decision_answers`'s ``ANSWERABLE_AS`` (which this type is
deliberately absent from, same as ``dimension-proposal`` and
``quarantine``), the only way a drafted proposal could ever become an
effective change is a future ratification applier dispatching on an
explicit human ``"approve"`` answer — never a reversibility-based
auto-apply path. ``tests/test_policy_pack_edit_proposals.py``'s
``test_auto_apply_never_acts_on_policy_pack_edit`` is the regression for
this.

**Ledger-only; no resolution path in this module.** Same precedent as
:mod:`athenaeum.dimension_proposals` / :mod:`athenaeum.tier_movement_proposals`:
detects (here, "detect" means "a caller supplies a diff"), drafts, and
appends to ``wiki/_policy_pack_edit_proposals.jsonl``.

Layering: L4 domain/pipeline module, a peer of
:mod:`athenaeum.dimension_proposals` and :mod:`athenaeum.tier_movement_proposals`.
Imports only :mod:`athenaeum.store` (L3) at module scope; deliberately does
NOT import :mod:`athenaeum.erasure` (see "The artifact a proposal edits"
above) so this module's existence creates no new edge toward that one.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Schema version stamped on every ledger record.
POLICY_PACK_EDIT_PROPOSALS_LEDGER_VERSION = 1

#: Sidecar filename under ``wiki_root``.
POLICY_PACK_EDIT_PROPOSALS_LEDGER_FILENAME = "_policy_pack_edit_proposals.jsonl"

#: Record kinds. Only ``proposal`` is ever written by this module.
PROPOSAL_KIND = "proposal"
APPROVE_KIND = "approve"
REJECT_KIND = "reject"


def default_policy_pack_edit_proposals_ledger_path(wiki_root: Path) -> Path:
    """Default ledger path: ``<wiki_root>/_policy_pack_edit_proposals.jsonl``."""
    return Path(wiki_root) / POLICY_PACK_EDIT_PROPOSALS_LEDGER_FILENAME


def _now_iso(now: datetime | None = None) -> str:
    return now_iso(now)


def proposal_item_id(pack_name: str, proposed_diff: dict[str, Any]) -> str:
    """Deterministic id for one ``(pack_name, proposed_diff)`` pair.

    NOT per-event — mirrors :func:`athenaeum.dimension_proposals.proposal_item_id`:
    the same edit proposed twice for the same pack is the same id, so a
    rejected edit stays suppressed by set-membership alone.
    """
    payload = json.dumps({"pack_name": pack_name, "proposed_diff": proposed_diff}, sort_keys=True)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return digest[:16]


def _append_jsonl_line(path: Path, line: str) -> None:
    append_line_durable(path, line.encode("utf-8"))


def read_policy_pack_edit_proposals_ledger(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed ledger record, tolerating a torn trailing line."""
    target = (
        ledger_path
        if ledger_path is not None
        else default_policy_pack_edit_proposals_ledger_path(wiki_root)
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


def list_pending_policy_pack_edit_proposals(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Drafted policy-pack-edit proposals awaiting ratification."""
    records = read_policy_pack_edit_proposals_ledger(wiki_root, ledger_path=ledger_path)
    resolved = _resolved_ids(records)
    return [
        r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) not in resolved
    ]


@dataclass(frozen=True)
class PolicyPackEditDraft:
    """One drafted proposal: a named pack + the proposed diff against it.

    ``proposed_diff`` is a plain dict, pack-library-agnostic by design (see
    module docstring): the convention this drafter uses is
    ``{"memory_class": ..., "data_class": ..., "jurisdiction": ...,
    "old_action": ... | None, "new_action": ..., "old_period": ... | None,
    "new_period": ... | None}`` for a single-rule edit, or
    ``{"default_action": {"old": ..., "new": ...}, "default_period":
    {"old": ..., "new": ...}}`` for a pack-default edit — never a live
    :class:`athenaeum.erasure.RetentionPack`/``RetentionRule`` object.
    """

    id: str
    pack_name: str
    proposed_diff: dict[str, Any]
    rationale: str

    def to_ledger_record(self, *, now: datetime | None = None) -> dict[str, Any]:
        return {
            "v": POLICY_PACK_EDIT_PROPOSALS_LEDGER_VERSION,
            "kind": PROPOSAL_KIND,
            "id": self.id,
            "created_at": _now_iso(now),
            "pack_name": self.pack_name,
            "proposed_diff": dict(self.proposed_diff),
            "rationale": self.rationale,
        }


def draft_policy_pack_edit_proposal(
    pack_name: str,
    proposed_diff: dict[str, Any],
    *,
    rationale: str,
) -> PolicyPackEditDraft:
    """Draft one policy-pack-edit proposal.

    Deterministic — a pure function of its arguments, no LLM call, no
    client. *rationale* is REQUIRED (unlike the dimension/tier-movement
    drafters, which synthesize their own) because there is no detector here
    to synthesize one from; the caller (today: an operator-triggered CLI
    command or a future detector, both out of this issue's scope) states
    why the edit is proposed.
    """
    item_id = proposal_item_id(pack_name, proposed_diff)
    return PolicyPackEditDraft(
        id=item_id,
        pack_name=pack_name,
        proposed_diff=dict(proposed_diff),
        rationale=rationale,
    )


@dataclass
class PolicyPackEditProposalRunSummary:
    """Outcome of one :func:`run_policy_pack_edit_proposal_drafting` call."""

    candidates_seen: int = 0
    proposed: int = 0
    skipped_pending: int = 0
    skipped_suppressed: int = 0
    drafts: list[PolicyPackEditDraft] = field(default_factory=list)


def run_policy_pack_edit_proposal_drafting(
    candidates: list[tuple[str, dict[str, Any], str]],
    *,
    wiki_root: Path,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> PolicyPackEditProposalRunSummary:
    """Draft + ledger a batch of ``(pack_name, proposed_diff, rationale)``
    candidates, idempotently.

    Gated by
    :func:`athenaeum.config.resolve_policy_pack_edit_proposals_enabled`
    (default OFF, per athenaeum#719's DoD). Returns an empty summary, doing no
    I/O at all, when the key is off.

    There is no detector in this module (see module docstring) — *candidates*
    is supplied by the caller. This function's job is the same
    idempotent-draft-then-ledger shape every other proposal drafter in this
    codebase uses, not detection.

    *dry_run* (default ``False``): drafts are computed and returned in the
    summary but NOT appended to the ledger.
    """
    from athenaeum.config import resolve_policy_pack_edit_proposals_enabled

    summary = PolicyPackEditProposalRunSummary()
    if not resolve_policy_pack_edit_proposals_enabled(config):
        return summary

    wiki_root = Path(wiki_root)
    records = read_policy_pack_edit_proposals_ledger(wiki_root, ledger_path=ledger_path)
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
        else default_policy_pack_edit_proposals_ledger_path(wiki_root)
    )

    for pack_name, proposed_diff, rationale in candidates:
        summary.candidates_seen += 1
        item_id = proposal_item_id(pack_name, proposed_diff)
        if item_id in rejected_ids:
            summary.skipped_suppressed += 1
            continue
        if item_id in pending_ids:
            summary.skipped_pending += 1
            continue

        draft = draft_policy_pack_edit_proposal(pack_name, proposed_diff, rationale=rationale)
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
    "POLICY_PACK_EDIT_PROPOSALS_LEDGER_VERSION",
    "POLICY_PACK_EDIT_PROPOSALS_LEDGER_FILENAME",
    "PROPOSAL_KIND",
    "APPROVE_KIND",
    "REJECT_KIND",
    "default_policy_pack_edit_proposals_ledger_path",
    "proposal_item_id",
    "read_policy_pack_edit_proposals_ledger",
    "list_pending_policy_pack_edit_proposals",
    "PolicyPackEditDraft",
    "draft_policy_pack_edit_proposal",
    "PolicyPackEditProposalRunSummary",
    "run_policy_pack_edit_proposal_drafting",
]
