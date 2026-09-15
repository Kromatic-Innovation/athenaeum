# SPDX-License-Identifier: Apache-2.0
"""Tests for the ported resolver actions on the comparator's verdict-effects
path (issue athenaeum#1680): ``not_a_conflict`` (suppress) and
``attribute_both`` may auto-apply under thresholds mirrored from
:mod:`athenaeum.resolutions`; ``propose_merge`` never auto-applies, at any
confidence.

Fully offline: no LLM client anywhere, matching ``tests/test_verdict_effects.py``'s
own discipline (this module's own docstring: "no confidence, no similarity,
no LLM call, anywhere in this module").
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import athenaeum.resolutions as resolutions_mod
from athenaeum.comparator import ComparatorPage, page_from_text
from athenaeum.decisions import list_pending_decisions
from athenaeum.verdict_effects import (
    RESOLVER_ATTRIBUTE_BOTH_ACTION,
    RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION,
    RESOLVER_DEFAULT_AUTO_APPLY_THRESHOLD,
    RESOLVER_DESTRUCTIVE_AUTO_APPLY_THRESHOLD,
    RESOLVER_NEVER_AUTO_APPLY_ACTIONS,
    RESOLVER_PROPOSE_MERGE_ACTION,
    RESOLVER_SUPPRESS_ACTION,
    EffectResult,
    apply_propose_merge_effect,
    apply_suppress_or_attribute_both_effect,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _page(
    page_id: str, *, name: str | None = None, body: str = "some claim text"
) -> ComparatorPage:
    text = f"---\nname: {name or page_id}\n---\n{body}\n"
    return page_from_text(page_id, text)


# ---------------------------------------------------------------------------
# Mirror-divergence guard: the comparator-domain constants must equal
# resolutions.py's own table, so the two can never silently drift apart.
# ---------------------------------------------------------------------------


class TestResolverActionMirrorMatchesResolutions:
    def test_action_token_strings_match(self) -> None:
        assert RESOLVER_SUPPRESS_ACTION == resolutions_mod.SUPPRESS_ACTION
        assert RESOLVER_ATTRIBUTE_BOTH_ACTION == resolutions_mod.ATTRIBUTE_BOTH_ACTION
        assert RESOLVER_PROPOSE_MERGE_ACTION == resolutions_mod.PROPOSE_MERGE_ACTION

    def test_default_auto_apply_threshold_matches(self) -> None:
        assert RESOLVER_DEFAULT_AUTO_APPLY_THRESHOLD == resolutions_mod.DEFAULT_AUTO_APPLY_THRESHOLD

    def test_destructive_floor_matches_correct_and_forget_actions(self) -> None:
        destructive = {
            resolutions_mod.DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION["correct_a"],
            resolutions_mod.DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION["correct_b"],
            resolutions_mod.DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION["forget_a"],
            resolutions_mod.DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION["forget_b"],
        }
        assert destructive == {RESOLVER_DESTRUCTIVE_AUTO_APPLY_THRESHOLD}

    def test_per_action_thresholds_match_resolutions_table(self) -> None:
        for action, threshold in RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION.items():
            assert (
                resolutions_mod.DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION[action] == threshold
            ), f"{action} threshold diverged from resolutions.py"

    def test_ported_actions_are_not_on_the_destructive_floor(self) -> None:
        # Operator decision (issue athenaeum#1680): suppress/attribute_both
        # stay on the 0.90-or-below band, never reclassified as destructive.
        for threshold in RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION.values():
            assert threshold < RESOLVER_DESTRUCTIVE_AUTO_APPLY_THRESHOLD

    def test_never_auto_apply_sentinel_matches(self) -> None:
        assert RESOLVER_NEVER_AUTO_APPLY_ACTIONS == resolutions_mod._NEVER_AUTO_APPLY_ACTIONS

    def test_propose_merge_absent_from_auto_apply_table(self) -> None:
        assert RESOLVER_PROPOSE_MERGE_ACTION not in RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION


# ---------------------------------------------------------------------------
# suppress / attribute_both: threshold-gated auto-apply vs. escalate
# ---------------------------------------------------------------------------


class TestSuppressAndAttributeBothThresholdBands:
    @pytest.mark.parametrize(
        "action",
        [RESOLVER_SUPPRESS_ACTION, RESOLVER_ATTRIBUTE_BOTH_ACTION],
    )
    def test_at_or_above_threshold_auto_applies(self, tmp_path: Path, action: str) -> None:
        threshold = RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION[action]
        wiki_root = tmp_path / "wiki"
        result = apply_suppress_or_attribute_both_effect(
            action, threshold, _page("a"), _page("b"), wiki_root=wiki_root
        )
        assert result.action == "auto-applied"
        assert result.queued == []
        assert not (wiki_root / "_pending_questions.md").exists()

    @pytest.mark.parametrize(
        "action",
        [RESOLVER_SUPPRESS_ACTION, RESOLVER_ATTRIBUTE_BOTH_ACTION],
    )
    def test_just_below_threshold_escalates_to_human(self, tmp_path: Path, action: str) -> None:
        threshold = RESOLVER_AUTO_APPLY_THRESHOLD_PER_ACTION[action]
        wiki_root = tmp_path / "wiki"
        result = apply_suppress_or_attribute_both_effect(
            action, threshold - 0.01, _page("a"), _page("b"), wiki_root=wiki_root
        )
        assert result.action == "queued"
        assert result.queued
        questions = wiki_root / "_pending_questions.md"
        assert questions.is_file()

    def test_attribute_both_at_1_0_confidence_auto_applies(self, tmp_path: Path) -> None:
        # Deterministic stance short-circuit in resolutions.py emits
        # attribute_both at confidence 1.0 -- confirm the mirrored gate
        # honors that too.
        result = apply_suppress_or_attribute_both_effect(
            RESOLVER_ATTRIBUTE_BOTH_ACTION,
            1.0,
            _page("a"),
            _page("b"),
            wiki_root=tmp_path / "wiki",
        )
        assert result.action == "auto-applied"

    def test_escalation_band_0_90_to_0_95_queues_for_attribute_both(self, tmp_path: Path) -> None:
        # [0.90, 0.95) is the destructive escalation band in resolutions.py;
        # attribute_both's OWN floor is 0.90, so 0.92 already clears it and
        # auto-applies -- this pins that attribute_both was NOT bumped onto
        # the higher destructive band (it would escalate here if it had been).
        result = apply_suppress_or_attribute_both_effect(
            RESOLVER_ATTRIBUTE_BOTH_ACTION,
            0.92,
            _page("a"),
            _page("b"),
            wiki_root=tmp_path / "wiki",
        )
        assert result.action == "auto-applied"

    def test_suppress_queue_item_visible_in_unified_decisions(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        apply_suppress_or_attribute_both_effect(
            RESOLVER_SUPPRESS_ACTION, 0.5, _page("a"), _page("b"), wiki_root=wiki_root
        )
        decisions = list_pending_decisions(wiki_root)
        assert any(d["type"] == "question" for d in decisions)

    def test_propose_merge_rejected_with_pointer_to_the_right_function(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="apply_propose_merge_effect"):
            apply_suppress_or_attribute_both_effect(
                RESOLVER_PROPOSE_MERGE_ACTION, 1.0, _page("a"), _page("b"), wiki_root=tmp_path
            )

    def test_unknown_action_raises_not_silent_noop(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            apply_suppress_or_attribute_both_effect(
                "not-a-real-action", 1.0, _page("a"), _page("b"), wiki_root=tmp_path
            )


# ---------------------------------------------------------------------------
# propose_merge: never auto-applies, at any confidence -- structural guard
# ---------------------------------------------------------------------------


class TestProposeMergeNeverAutoApplies:
    def test_signature_has_no_confidence_parameter(self) -> None:
        # The structural half of the guarantee: there is no parameter here
        # a future refactor could thread a confidence comparison through
        # without first changing this signature (a visible, reviewable diff).
        sig = inspect.signature(apply_propose_merge_effect)
        assert "confidence" not in sig.parameters
        assert "draft_merged_body" not in sig.parameters

    @pytest.mark.parametrize(
        "confidence_shaped_value", [0.0, 0.5, 0.89, 0.90, 0.9499, 0.95, 0.999, 1.0]
    )
    def test_always_queues_regardless_of_any_confidence_shaped_value(
        self, tmp_path: Path, confidence_shaped_value: float
    ) -> None:
        # confidence_shaped_value is NOT passed to apply_propose_merge_effect
        # at all -- the parametrization itself is the point: no matter what
        # value a caller's resolver proposal carried, this function's
        # behavior is identical, because it structurally cannot consult it.
        del confidence_shaped_value
        wiki_root = tmp_path / "wiki"
        result = apply_propose_merge_effect(_page("a"), _page("b"), wiki_root=wiki_root)
        assert result.action == "queued"
        assert result.queued
        assert (wiki_root / "_pending_questions.md").is_file()

    def test_result_carries_no_confidence_or_draft_merged_body(self, tmp_path: Path) -> None:
        result = apply_propose_merge_effect(_page("a"), _page("b"), wiki_root=tmp_path / "wiki")
        assert "confidence" not in result.details
        assert "draft_merged_body" not in result.details
        assert not any("confidence" in k for k in result.details)
        assert not any("draft_merged_body" in k for k in result.details)

    def test_never_writes_pending_merges_file(self, tmp_path: Path) -> None:
        # pending_merges.write_pending_merge requires a mandatory
        # confidence + draft_merged_body -- this module's queue path must
        # never touch that file (module docstring, "Queue routing").
        wiki_root = tmp_path / "wiki"
        apply_propose_merge_effect(_page("a"), _page("b"), wiki_root=wiki_root)
        assert not (wiki_root / "_pending_merges.md").exists()

    def test_queue_item_visible_in_unified_decisions_with_no_confidence(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        apply_propose_merge_effect(_page("a"), _page("b"), wiki_root=wiki_root)
        decisions = list_pending_decisions(wiki_root)
        matches = [d for d in decisions if d["type"] == "question"]
        assert matches
        assert all(d["confidence"] is None for d in matches)

    def test_action_in_never_auto_apply_sentinel(self) -> None:
        assert RESOLVER_PROPOSE_MERGE_ACTION in RESOLVER_NEVER_AUTO_APPLY_ACTIONS


# ---------------------------------------------------------------------------
# EffectResult shape parity with existing branches
# ---------------------------------------------------------------------------


class TestEffectResultShapeParity:
    def test_auto_applied_result_is_an_effect_result(self, tmp_path: Path) -> None:
        result = apply_suppress_or_attribute_both_effect(
            RESOLVER_SUPPRESS_ACTION, 0.99, _page("a"), _page("b"), wiki_root=tmp_path / "wiki"
        )
        assert isinstance(result, EffectResult)

    def test_queued_result_is_an_effect_result(self, tmp_path: Path) -> None:
        result = apply_propose_merge_effect(_page("a"), _page("b"), wiki_root=tmp_path / "wiki")
        assert isinstance(result, EffectResult)
