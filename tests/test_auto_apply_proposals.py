# SPDX-License-Identifier: Apache-2.0
"""Tests for the auto-apply-threshold proposal drafter (issue athenaeum#2018,
athenaeum#719 Plan step 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.auto_apply_proposals import (
    AUTO_APPLY_PROPOSALS_LEDGER_FILENAME,
    approve_auto_apply_threshold_proposal,
    default_auto_apply_proposals_ledger_path,
    disagreement_rate,
    draft_auto_apply_threshold_proposal,
    list_pending_auto_apply_threshold_proposals,
    proposal_item_id,
    reject_auto_apply_threshold_proposal,
    run_auto_apply_proposal_detection,
)
from athenaeum.calibration import read_calibration_ledger, record_audit_review, sample_tier_decision
from athenaeum.config import auto_apply_threshold_ledger_override_for
from athenaeum.resolutions import (
    DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION,
    resolve_auto_apply_threshold_for,
)

_FULL_SAMPLE_CONFIG = {
    "librarian": {
        "audit_sample_rate_t1_rejects": 1.0,
        "audit_sample_rate_t2_approvals": 1.0,
    }
}


def _sample_and_review(wiki_root: Path, *, proposal_id: str, human_verdict: str) -> None:
    sample_tier_decision(
        wiki_root,
        tier="T1",
        verdict="reject",
        proposal_id=proposal_id,
        reason="test",
        config=_FULL_SAMPLE_CONFIG,
    )
    audit_id = next(
        r["id"]
        for r in read_calibration_ledger(wiki_root)
        if r.get("kind") == "audit" and r.get("proposal_id") == proposal_id
    )
    record_audit_review(wiki_root, audit_id=audit_id, human_verdict=human_verdict)


def _seed_t1_disagreement(wiki_root: Path, *, overturned: int, confirmed: int) -> None:
    """Sample + review N T1-reject audits: *overturned* reviewed as
    "approve" (differs from the original "reject" -> overturned), *confirmed*
    reviewed as "reject" (matches -> confirmed)."""
    idx = 0
    for _ in range(overturned):
        idx += 1
        _sample_and_review(wiki_root, proposal_id=f"prop-{idx}", human_verdict="approve")
    for _ in range(confirmed):
        idx += 1
        _sample_and_review(wiki_root, proposal_id=f"prop-{idx}", human_verdict="reject")


class TestDisagreementRate:
    def test_none_when_nothing_reviewed(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        assert disagreement_rate(wiki_root) is None

    def test_computed_rate(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        assert disagreement_rate(wiki_root) == pytest.approx(0.75)


class TestDraftAutoApplyThresholdProposal:
    def test_below_trigger_drafts_nothing(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=1, confirmed=9)  # 10% disagreement
        draft = draft_auto_apply_threshold_proposal(
            "not_a_conflict",
            wiki_root=wiki_root,
            config={"librarian": {"auto_apply_proposals_disagreement_trigger": 0.2}},
        )
        assert draft is None

    def test_at_or_above_trigger_drafts_a_strictly_wider_value(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)  # 75% disagreement
        current = DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION["not_a_conflict"]
        draft = draft_auto_apply_threshold_proposal(
            "not_a_conflict",
            wiki_root=wiki_root,
            config={
                "librarian": {
                    "auto_apply_proposals_disagreement_trigger": 0.2,
                    "auto_apply_proposals_widen_step": 0.05,
                }
            },
        )
        assert draft is not None
        assert draft.current_threshold == pytest.approx(current)
        assert draft.proposed_threshold < draft.current_threshold
        assert draft.proposed_threshold == pytest.approx(current - 0.05)

    def test_never_auto_apply_action_drafts_nothing(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        draft = draft_auto_apply_threshold_proposal(
            "propose_merge",
            wiki_root=wiki_root,
            config={"librarian": {"auto_apply_proposals_disagreement_trigger": 0.2}},
        )
        assert draft is None  # propose_merge never auto-applies -- nothing to widen

    def test_floor_clamped_at_zero_never_narrows(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        # A step bigger than the current threshold would clamp to 0.0 --
        # still strictly wider, so it still drafts.
        draft = draft_auto_apply_threshold_proposal(
            "not_a_conflict",
            wiki_root=wiki_root,
            config={
                "librarian": {
                    "auto_apply_proposals_disagreement_trigger": 0.2,
                    "auto_apply_proposals_widen_step": 5.0,
                }
            },
        )
        assert draft is not None
        assert draft.proposed_threshold == 0.0


class TestProposalItemId:
    def test_different_proposed_value_gets_different_id(self) -> None:
        id_a = proposal_item_id("not_a_conflict", 0.75, 0.70)
        id_b = proposal_item_id("not_a_conflict", 0.75, 0.65)
        assert id_a != id_b


class TestRunAutoApplyProposalDetection:
    def test_idempotent_pending(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        config = {
            "librarian": {
                "auto_apply_proposals_disagreement_trigger": 0.2,
                "auto_apply_proposals_widen_step": 0.05,
            }
        }
        first = run_auto_apply_proposal_detection(
            ["not_a_conflict"], wiki_root=wiki_root, config=config
        )
        assert first.proposed == 1
        second = run_auto_apply_proposal_detection(
            ["not_a_conflict"], wiki_root=wiki_root, config=config
        )
        assert second.proposed == 0
        assert second.skipped_pending == 1

    def test_rejected_specific_value_does_not_suppress_a_different_value(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        config_a = {
            "librarian": {
                "auto_apply_proposals_disagreement_trigger": 0.2,
                "auto_apply_proposals_widen_step": 0.05,
            }
        }
        run_auto_apply_proposal_detection(["not_a_conflict"], wiki_root=wiki_root, config=config_a)
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        assert len(pending) == 1
        reject_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])

        config_b = {
            "librarian": {
                "auto_apply_proposals_disagreement_trigger": 0.2,
                "auto_apply_proposals_widen_step": 0.10,  # different step
            }
        }
        again = run_auto_apply_proposal_detection(
            ["not_a_conflict"], wiki_root=wiki_root, config=config_b
        )
        assert again.proposed == 1  # fresh id, not suppressed by the earlier rejection


class TestApproveWidensConfigOnlyNeverBypassesInFlight:
    """AC: approving an auto-apply-threshold proposal only widens config,
    never bypasses in-flight decisions."""

    def test_approve_widens_the_resolved_threshold(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        config = {
            "librarian": {
                "auto_apply_proposals_disagreement_trigger": 0.2,
                "auto_apply_proposals_widen_step": 0.05,
            }
        }
        before = resolve_auto_apply_threshold_for(config, "not_a_conflict", wiki_root=wiki_root)
        assert before == DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION["not_a_conflict"]

        run_auto_apply_proposal_detection(["not_a_conflict"], wiki_root=wiki_root, config=config)
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])

        after = resolve_auto_apply_threshold_for(config, "not_a_conflict", wiki_root=wiki_root)
        assert after == pytest.approx(before - 0.05)
        assert after < before

    def test_without_wiki_root_the_resolver_is_unaffected(self, tmp_path: Path) -> None:
        """A pre-athenaeum#2018 call site that never passes wiki_root sees
        zero behavior change -- the ledger override layer is a pure no-op
        for it."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        config = {
            "librarian": {
                "auto_apply_proposals_disagreement_trigger": 0.2,
                "auto_apply_proposals_widen_step": 0.05,
            }
        }
        run_auto_apply_proposal_detection(["not_a_conflict"], wiki_root=wiki_root, config=config)
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])

        unaffected = resolve_auto_apply_threshold_for(config, "not_a_conflict")
        assert unaffected == DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION["not_a_conflict"]

    def test_explicit_operator_config_still_wins_over_ledger_override(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        config = {
            "librarian": {
                "auto_apply_proposals_disagreement_trigger": 0.2,
                "auto_apply_proposals_widen_step": 0.05,
            }
        }
        run_auto_apply_proposal_detection(["not_a_conflict"], wiki_root=wiki_root, config=config)
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])

        explicit_config = dict(config)
        explicit_config["resolve"] = {"auto_apply_threshold_per_action": {"not_a_conflict": 0.99}}
        resolved = resolve_auto_apply_threshold_for(
            explicit_config, "not_a_conflict", wiki_root=wiki_root
        )
        assert resolved == 0.99  # operator's own explicit setting always wins

    def test_approve_never_touches_pending_merges_or_questions(self, tmp_path: Path) -> None:
        """A decision already escalated to a human before this proposal was
        drafted or approved must be untouched by the approval -- the
        resolver gates are decide-at-verdict-time, never a later re-sweep."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        pending_questions = wiki_root / "_pending_questions.md"
        pending_merges = wiki_root / "_pending_merges.md"
        pending_questions.write_text("# pending questions\n\n- an existing item\n")
        pending_merges.write_text("# pending merges\n\n- an existing merge\n")
        before_questions = pending_questions.read_bytes()
        before_merges = pending_merges.read_bytes()

        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        config = {
            "librarian": {
                "auto_apply_proposals_disagreement_trigger": 0.2,
                "auto_apply_proposals_widen_step": 0.05,
            }
        }
        run_auto_apply_proposal_detection(["not_a_conflict"], wiki_root=wiki_root, config=config)
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])

        assert pending_questions.read_bytes() == before_questions
        assert pending_merges.read_bytes() == before_merges

    def test_approve_unknown_id_raises(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        with pytest.raises(ValueError):
            approve_auto_apply_threshold_proposal(wiki_root, proposal_id="nope")

    def test_approve_already_resolved_raises(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        config = {
            "librarian": {
                "auto_apply_proposals_disagreement_trigger": 0.2,
                "auto_apply_proposals_widen_step": 0.05,
            }
        }
        run_auto_apply_proposal_detection(["not_a_conflict"], wiki_root=wiki_root, config=config)
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        proposal_id = pending[0]["id"]
        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=proposal_id)
        with pytest.raises(ValueError):
            approve_auto_apply_threshold_proposal(wiki_root, proposal_id=proposal_id)


class TestLedgerOverrideResolver:
    def test_most_recent_approved_wins(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        config = {
            "librarian": {
                "auto_apply_proposals_disagreement_trigger": 0.2,
                "auto_apply_proposals_widen_step": 0.05,
            }
        }
        run_auto_apply_proposal_detection(["not_a_conflict"], wiki_root=wiki_root, config=config)
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])
        first_override = auto_apply_threshold_ledger_override_for(
            "not_a_conflict", wiki_root=wiki_root
        )
        assert first_override is not None

    def test_no_match_returns_none(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        assert (
            auto_apply_threshold_ledger_override_for("not_a_conflict", wiki_root=wiki_root)
            is None
        )


class TestLedgerFilename:
    def test_filename_constant_matches_path_helper(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        assert (
            default_auto_apply_proposals_ledger_path(wiki_root)
            == wiki_root / AUTO_APPLY_PROPOSALS_LEDGER_FILENAME
        )
