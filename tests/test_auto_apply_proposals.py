# SPDX-License-Identifier: Apache-2.0
"""Tests for the auto-apply-threshold proposal drafter (issue athenaeum#2018,
athenaeum#719 Plan step 6)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from athenaeum.auto_apply_proposals import (
    AUTO_APPLY_PROPOSALS_LEDGER_FILENAME,
    approve_auto_apply_threshold_proposal,
    attribution_overturn_rate,
    default_auto_apply_proposals_ledger_path,
    disagreement_rate,
    draft_attribution_threshold_proposal,
    draft_auto_apply_threshold_proposal,
    list_pending_auto_apply_threshold_proposals,
    proposal_item_id,
    reject_auto_apply_threshold_proposal,
    run_attribution_threshold_proposal_detection,
    run_auto_apply_proposal_detection,
)
from athenaeum.calibration import (
    ATTRIBUTION_TIER_NAME,
    attribution_threshold_action_id,
    read_calibration_ledger,
    record_audit_review,
    sample_tier_decision,
)
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
            config={"librarian": {"auto_apply_proposals": {"disagreement_trigger": 0.2}}},
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
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.05,
                }
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
            config={"librarian": {"auto_apply_proposals": {"disagreement_trigger": 0.2}}},
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
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 5.0,
                }
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
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.05,
                }
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
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.05,
                }
            }
        }
        run_auto_apply_proposal_detection(["not_a_conflict"], wiki_root=wiki_root, config=config_a)
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        assert len(pending) == 1
        reject_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])

        config_b = {
            "librarian": {
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.10,  # different step
                }
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
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.05,
                }
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
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.05,
                }
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
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.05,
                }
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

    def test_ledger_override_wins_over_legacy_scalar_fallback(self, tmp_path: Path) -> None:
        """Issue athenaeum#2032: pin the precedence
        ``resolve_auto_apply_threshold_for``'s code (and its own docstring)
        already implements -- the ledger (layer 3) outranks the legacy
        scalar fallback (layer 4) -- against the actual resolver output.
        ``config.auto_apply_threshold_ledger_override_for``'s docstring
        previously mis-stated this (grouped the legacy env var in with the
        per-action override it is consulted BEFORE this layer, implying it
        too outranks the ledger; it is the opposite — see athenaeum#2032).

        ``keep_a`` is the action under test because the legacy scalar
        fallback (``resolve.auto_apply_threshold`` /
        ``ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD``) only ever applies to
        ``keep_a``/``keep_b`` — see ``_LEGACY_SCALAR_FALLBACK_ACTIONS``.
        """
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        config = {
            "librarian": {
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.05,
                }
            },
            # Legacy scalar -- keep_a/keep_b only. A deliberately STRICTER
            # value than keep_a's 0.90 default so "legacy won" and "ledger
            # won" are unambiguously distinguishable outcomes.
            "resolve": {"auto_apply_threshold": 0.99},
        }
        run_auto_apply_proposal_detection(["keep_a"], wiki_root=wiki_root, config=config)
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        assert pending, "expected a drafted keep_a widen proposal"
        proposed_threshold = pending[0]["proposed_threshold"]
        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])

        resolved = resolve_auto_apply_threshold_for(config, "keep_a", wiki_root=wiki_root)
        assert resolved == pytest.approx(proposed_threshold)
        assert resolved != pytest.approx(0.99)  # NOT the legacy scalar

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
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.05,
                }
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
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.05,
                }
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
                "auto_apply_proposals": {
                    "disagreement_trigger": 0.2,
                    "widen_step": 0.05,
                }
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


class TestAsOfCutoff:
    """Issue athenaeum#2032: ``as_of`` lets a caller re-deciding an
    ALREADY-PENDING item (``athenaeum.tiers.reresolve_open_questions``)
    only honor an approval that predates the item's own raise time. Unit
    tests against the ledger function directly; the end-to-end proof at the
    real ``tiers.py`` call site lives in
    ``tests/test_tiers_auto_apply_threshold_gating.py``.
    """

    def _approve(self, tmp_path: Path, *, approved_at: datetime) -> tuple[Path, float]:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)
        config = {
            "librarian": {
                "auto_apply_proposals": {"disagreement_trigger": 0.2, "widen_step": 0.05}
            }
        }
        run_auto_apply_proposal_detection(["not_a_conflict"], wiki_root=wiki_root, config=config)
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        proposed_threshold = pending[0]["proposed_threshold"]
        approve_auto_apply_threshold_proposal(
            wiki_root, proposal_id=pending[0]["id"], now=approved_at
        )
        return wiki_root, proposed_threshold

    def test_approval_at_or_before_as_of_is_honored(self, tmp_path: Path) -> None:
        wiki_root, proposed_threshold = self._approve(
            tmp_path, approved_at=datetime(2026, 1, 1, 0, 0, 0)
        )
        value = auto_apply_threshold_ledger_override_for(
            "not_a_conflict", wiki_root=wiki_root, as_of="2026-01-02T00:00:00Z"
        )
        assert value == pytest.approx(proposed_threshold)

        # The boundary itself (approved exactly AT as_of) is also honored.
        value_at_boundary = auto_apply_threshold_ledger_override_for(
            "not_a_conflict", wiki_root=wiki_root, as_of="2026-01-01T00:00:00Z"
        )
        assert value_at_boundary == pytest.approx(proposed_threshold)

    def test_approval_after_as_of_is_not_honored(self, tmp_path: Path) -> None:
        wiki_root, _ = self._approve(tmp_path, approved_at=datetime(2026, 1, 5, 0, 0, 0))
        value = auto_apply_threshold_ledger_override_for(
            "not_a_conflict", wiki_root=wiki_root, as_of="2026-01-01T00:00:00Z"
        )
        assert value is None

    def test_unparseable_as_of_fails_closed(self, tmp_path: Path) -> None:
        wiki_root, _ = self._approve(tmp_path, approved_at=datetime(2026, 1, 1, 0, 0, 0))
        assert (
            auto_apply_threshold_ledger_override_for(
                "not_a_conflict", wiki_root=wiki_root, as_of="not-a-timestamp"
            )
            is None
        )

    def test_empty_as_of_fails_closed(self, tmp_path: Path) -> None:
        wiki_root, _ = self._approve(tmp_path, approved_at=datetime(2026, 1, 1, 0, 0, 0))
        assert (
            auto_apply_threshold_ledger_override_for(
                "not_a_conflict", wiki_root=wiki_root, as_of=""
            )
            is None
        )

    def test_omitted_as_of_keeps_pre_2032_trust_latest_behavior(self, tmp_path: Path) -> None:
        wiki_root, proposed_threshold = self._approve(
            tmp_path, approved_at=datetime(2026, 1, 5, 0, 0, 0)
        )
        value = auto_apply_threshold_ledger_override_for("not_a_conflict", wiki_root=wiki_root)
        assert value == pytest.approx(proposed_threshold)


# ---------------------------------------------------------------------------
# Issue athenaeum#2219, design §8.3 — attribution-threshold drafting on the
# SAME rail, in EITHER direction (widen below the target error rate, narrow
# above it), approved through the SAME ledger-override mechanism.
# ---------------------------------------------------------------------------

#: ``1 - librarian.attribution.accuracy_floor`` with the floor pinned to
#: 0.95 (the operator decision of 2026-10-30 the issue cites) -- 0.05.
_ATTR_CONFIG = {"librarian": {"attribution": {"accuracy_floor": 0.95}}}


def _attr_config_with_threshold(question_class: str, threshold: float) -> dict:
    return {
        "librarian": {
            "attribution": {
                "accuracy_floor": 0.95,
                "thresholds": {question_class: threshold},
            }
        }
    }


def _sample_and_review_attribution(
    wiki_root: Path,
    *,
    proposal_id: str,
    question_class: str,
    human_verdict: str,
    verdict: str = "own",
) -> None:
    sample_tier_decision(
        wiki_root,
        tier=ATTRIBUTION_TIER_NAME,
        verdict=verdict,
        proposal_id=proposal_id,
        reason="test",
        config={"librarian": {"audit_sample_rate_attribution": 1.0}},
        question_class=question_class,
        applied=True,
    )
    audit_id = next(
        r["id"]
        for r in read_calibration_ledger(wiki_root)
        if r.get("kind") == "audit" and r.get("proposal_id") == proposal_id
    )
    record_audit_review(wiki_root, audit_id=audit_id, human_verdict=human_verdict)


def _seed_attribution_disagreement(
    wiki_root: Path, *, question_class: str, overturned: int, confirmed: int
) -> None:
    """Sample + review N attribution ``own`` audits: *overturned* reviewed
    as ``alias`` (differs -> overturned), *confirmed* reviewed as ``own``
    (matches -> confirmed)."""
    idx = 0
    for _ in range(overturned):
        idx += 1
        _sample_and_review_attribution(
            wiki_root,
            proposal_id=f"attr-{question_class}-{idx}",
            question_class=question_class,
            human_verdict="alias",
        )
    for _ in range(confirmed):
        idx += 1
        _sample_and_review_attribution(
            wiki_root,
            proposal_id=f"attr-{question_class}-{idx}",
            question_class=question_class,
            human_verdict="own",
        )


class TestAttributionOverturnRate:
    def test_none_when_nothing_reviewed(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        assert attribution_overturn_rate(wiki_root, "held-elsewhere") is None

    def test_computed_rate_is_per_class(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_attribution_disagreement(
            wiki_root, question_class="held-elsewhere", overturned=3, confirmed=1
        )
        _seed_attribution_disagreement(
            wiki_root, question_class="role-unmatched", overturned=0, confirmed=10
        )
        assert attribution_overturn_rate(wiki_root, "held-elsewhere") == pytest.approx(0.75)
        assert attribution_overturn_rate(wiki_root, "role-unmatched") == pytest.approx(0.0)


class TestDraftAttributionThresholdProposal:
    def test_no_current_threshold_drafts_nothing(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_attribution_disagreement(
            wiki_root, question_class="held-elsewhere", overturned=0, confirmed=10
        )
        # No explicit threshold override and no shipped calibration record
        # patched in -> threshold_for_class returns None -> nothing to move.
        draft = draft_attribution_threshold_proposal(
            "held-elsewhere", wiki_root=wiki_root, config=_ATTR_CONFIG
        )
        assert draft is None

    def test_unreviewed_class_drafts_nothing(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        draft = draft_attribution_threshold_proposal(
            "held-elsewhere",
            wiki_root=wiki_root,
            config=_attr_config_with_threshold("held-elsewhere", 0.9),
        )
        assert draft is None

    def test_overturns_below_target_error_rate_drafts_a_widen(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        # 0/10 overturned = 0.0, strictly below the 0.05 target -> widen.
        _seed_attribution_disagreement(
            wiki_root, question_class="held-elsewhere", overturned=0, confirmed=10
        )
        config = _attr_config_with_threshold("held-elsewhere", 0.9)
        draft = draft_attribution_threshold_proposal(
            "held-elsewhere", wiki_root=wiki_root, config=config
        )
        assert draft is not None
        assert draft.direction == "widen"
        assert draft.question_class == "held-elsewhere"
        assert draft.current_threshold == pytest.approx(0.9)
        assert draft.proposed_threshold == pytest.approx(0.85)  # lower = wider
        assert draft.proposed_threshold < draft.current_threshold
        assert draft.target_error_rate == pytest.approx(0.05)
        assert draft.overturn_rate_value == pytest.approx(0.0)
        assert draft.action == attribution_threshold_action_id("held-elsewhere")
        assert draft.sampled == 10
        assert draft.reviewed == 10
        assert draft.overturned == 0

    def test_overturns_above_target_error_rate_drafts_a_narrow(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        # 3/10 overturned = 0.3, well above the 0.05 target -> narrow.
        _seed_attribution_disagreement(
            wiki_root, question_class="held-elsewhere", overturned=3, confirmed=7
        )
        config = _attr_config_with_threshold("held-elsewhere", 0.9)
        draft = draft_attribution_threshold_proposal(
            "held-elsewhere", wiki_root=wiki_root, config=config
        )
        assert draft is not None
        assert draft.direction == "narrow"
        assert draft.current_threshold == pytest.approx(0.9)
        assert draft.proposed_threshold == pytest.approx(0.95)  # higher = narrower
        assert draft.proposed_threshold > draft.current_threshold
        assert draft.overturn_rate_value == pytest.approx(0.3)
        assert draft.overturned == 3

    def test_rate_exactly_at_target_drafts_nothing(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        # 1/20 overturned = 0.05, exactly the target -> no change either way.
        _seed_attribution_disagreement(
            wiki_root, question_class="held-elsewhere", overturned=1, confirmed=19
        )
        config = _attr_config_with_threshold("held-elsewhere", 0.9)
        draft = draft_attribution_threshold_proposal(
            "held-elsewhere", wiki_root=wiki_root, config=config
        )
        assert draft is None

    def test_each_question_class_drafts_independently(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_attribution_disagreement(
            wiki_root, question_class="held-elsewhere", overturned=0, confirmed=10
        )
        _seed_attribution_disagreement(
            wiki_root, question_class="role-unmatched", overturned=5, confirmed=5
        )
        config = {
            "librarian": {
                "attribution": {
                    "accuracy_floor": 0.95,
                    "thresholds": {"held-elsewhere": 0.9, "role-unmatched": 0.9},
                }
            }
        }
        widen = draft_attribution_threshold_proposal(
            "held-elsewhere", wiki_root=wiki_root, config=config
        )
        narrow = draft_attribution_threshold_proposal(
            "role-unmatched", wiki_root=wiki_root, config=config
        )
        assert widen is not None and widen.direction == "widen"
        assert narrow is not None and narrow.direction == "narrow"
        assert widen.action != narrow.action


class TestAttributionThresholdApprovalIsRequired:
    """AC5's third case: an unapproved draft changes nothing. The ledger
    override is what `pii_attribution.threshold_for_class` reads BELOW
    explicit config (issue athenaeum#2219) -- this pins the override reader
    itself, independent of whichever config layer a deployment also sets."""

    def test_drafting_alone_writes_no_override(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_attribution_disagreement(
            wiki_root, question_class="held-elsewhere", overturned=0, confirmed=10
        )
        config = _attr_config_with_threshold("held-elsewhere", 0.9)
        run_attribution_threshold_proposal_detection(
            ["held-elsewhere"], wiki_root=wiki_root, config=config
        )
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        assert len(pending) == 1
        action = attribution_threshold_action_id("held-elsewhere")
        assert (
            auto_apply_threshold_ledger_override_for(action, wiki_root=wiki_root) is None
        )

    def test_approval_writes_the_override_the_reader_honours(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_attribution_disagreement(
            wiki_root, question_class="held-elsewhere", overturned=0, confirmed=10
        )
        config = _attr_config_with_threshold("held-elsewhere", 0.9)
        run_attribution_threshold_proposal_detection(
            ["held-elsewhere"], wiki_root=wiki_root, config=config
        )
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        action = attribution_threshold_action_id("held-elsewhere")
        assert pending[0]["action"] == action
        proposed = pending[0]["proposed_threshold"]

        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])

        value = auto_apply_threshold_ledger_override_for(action, wiki_root=wiki_root)
        assert value == pytest.approx(proposed)
        # Idempotent: a second detection pass over the same seed data does
        # not re-draft (same (action, current, proposed) id is pending/
        # resolved either way) -- mirrors run_auto_apply_proposal_detection.
        run_attribution_threshold_proposal_detection(
            ["held-elsewhere"], wiki_root=wiki_root, config=config
        )
        assert len(read_calibration_ledger(wiki_root)) == 20  # unchanged by the rerun

    def test_rejection_also_writes_no_override(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_attribution_disagreement(
            wiki_root, question_class="held-elsewhere", overturned=0, confirmed=10
        )
        config = _attr_config_with_threshold("held-elsewhere", 0.9)
        run_attribution_threshold_proposal_detection(
            ["held-elsewhere"], wiki_root=wiki_root, config=config
        )
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        action = attribution_threshold_action_id("held-elsewhere")
        reject_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])
        assert (
            auto_apply_threshold_ledger_override_for(action, wiki_root=wiki_root) is None
        )

    def test_explicit_config_still_wins_over_an_approved_override(
        self, tmp_path: Path
    ) -> None:
        """threshold_for_class's own precedence: explicit config > ledger
        override > shipped record -- approving a proposal never overrides
        an operator's hand-set threshold."""
        from athenaeum.pii_attribution import threshold_for_class

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _seed_attribution_disagreement(
            wiki_root, question_class="held-elsewhere", overturned=0, confirmed=10
        )
        config = _attr_config_with_threshold("held-elsewhere", 0.9)
        run_attribution_threshold_proposal_detection(
            ["held-elsewhere"], wiki_root=wiki_root, config=config
        )
        pending = list_pending_auto_apply_threshold_proposals(wiki_root)
        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=pending[0]["id"])

        assert threshold_for_class(
            "held-elsewhere", config, wiki_root=wiki_root
        ) == pytest.approx(0.9)  # the explicit override, NOT the approved 0.85
