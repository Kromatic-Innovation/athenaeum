# SPDX-License-Identifier: Apache-2.0
"""Tests for issue #170 — asymmetric auto-apply threshold by action.

Lane 4 of the #166 librarian-reasoner improvements epic. The single-scalar
``resolve.auto_apply_threshold`` is replaced (additively, with backward-compat)
by a per-action map:

* ``not_a_conflict`` — default 0.75 (cheap to be wrong; re-detected next run).
* ``keep_a`` / ``keep_b`` — default 0.90 (mutates wiki bodies).
* ``propose_merge`` — NEVER auto-applies (always escalates to human regardless
  of confidence; the proposal carries an LLM-drafted merged body).

Backward compat: a config that only sets the legacy scalar
``resolve.auto_apply_threshold: 0.85`` still works — applied to ``keep_a`` and
``keep_b`` only. ``not_a_conflict`` gets the new per-action default. When both
are set, the per-action map wins for the actions it lists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.models import EscalationItem
from athenaeum.resolutions import (
    DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION,
    MergeProposal,
    ResolutionProposal,
    resolve_auto_apply_threshold_for,
)
from athenaeum.tiers import tier4_escalate

# ---------------------------------------------------------------------------
# resolve_auto_apply_threshold_for — pure config resolution
# ---------------------------------------------------------------------------


class TestPerActionDefaults:
    def test_not_a_conflict_default_is_0_75(self) -> None:
        assert resolve_auto_apply_threshold_for({}, "not_a_conflict") == 0.75

    def test_keep_a_default_is_0_90(self) -> None:
        assert resolve_auto_apply_threshold_for({}, "keep_a") == 0.90

    def test_keep_b_default_is_0_90(self) -> None:
        assert resolve_auto_apply_threshold_for({}, "keep_b") == 0.90

    def test_propose_merge_returns_none(self) -> None:
        # Hard rule, not a threshold — propose_merge never auto-applies.
        assert resolve_auto_apply_threshold_for({}, "propose_merge") is None

    def test_propose_merge_returns_none_even_with_explicit_override(self) -> None:
        # The sentinel is checked BEFORE per-action config so an accidental
        # entry in the YAML can't unlock auto-apply for propose_merge.
        cfg = {
            "resolve": {
                "auto_apply_threshold_per_action": {"propose_merge": 0.5},
            }
        }
        assert resolve_auto_apply_threshold_for(cfg, "propose_merge") is None

    def test_unknown_action_returns_none(self) -> None:
        # Unknown / non-auto-applicable actions fall through to None.
        assert resolve_auto_apply_threshold_for({}, "merge") is None
        assert resolve_auto_apply_threshold_for({}, "deprecate_both") is None
        assert resolve_auto_apply_threshold_for({}, "retain_both_with_context") is None

    def test_none_config_uses_per_action_defaults(self) -> None:
        # config=None still resolves to per-action defaults — this is the
        # pure-resolver behavior. The tiers.py gate adds its own
        # "config is None → return None" wrapper.
        assert resolve_auto_apply_threshold_for(None, "not_a_conflict") == 0.75
        assert resolve_auto_apply_threshold_for(None, "keep_a") == 0.90


class TestLegacyScalarBackwardCompat:
    def test_legacy_scalar_applies_to_keep_a(self) -> None:
        cfg = {"resolve": {"auto_apply_threshold": 0.85}}
        assert resolve_auto_apply_threshold_for(cfg, "keep_a") == 0.85

    def test_legacy_scalar_applies_to_keep_b(self) -> None:
        cfg = {"resolve": {"auto_apply_threshold": 0.85}}
        assert resolve_auto_apply_threshold_for(cfg, "keep_b") == 0.85

    def test_legacy_scalar_does_not_override_not_a_conflict_default(self) -> None:
        # The legacy scalar was a keep_a/keep_b knob. It does NOT push the
        # not_a_conflict default up to 0.85 — that would defeat the whole
        # point of #170 (cheaper threshold for false-suppress).
        cfg = {"resolve": {"auto_apply_threshold": 0.85}}
        assert resolve_auto_apply_threshold_for(cfg, "not_a_conflict") == 0.75

    def test_legacy_scalar_does_not_unlock_propose_merge(self) -> None:
        cfg = {"resolve": {"auto_apply_threshold": 0.85}}
        assert resolve_auto_apply_threshold_for(cfg, "propose_merge") is None


class TestPerActionOverride:
    def test_explicit_per_action_map(self) -> None:
        cfg = {
            "resolve": {
                "auto_apply_threshold_per_action": {
                    "not_a_conflict": 0.60,
                    "keep_a": 0.95,
                    "keep_b": 0.95,
                }
            }
        }
        assert resolve_auto_apply_threshold_for(cfg, "not_a_conflict") == 0.60
        assert resolve_auto_apply_threshold_for(cfg, "keep_a") == 0.95
        assert resolve_auto_apply_threshold_for(cfg, "keep_b") == 0.95

    def test_partial_per_action_uses_defaults_for_unspecified(self) -> None:
        cfg = {
            "resolve": {
                "auto_apply_threshold_per_action": {"not_a_conflict": 0.60},
            }
        }
        assert resolve_auto_apply_threshold_for(cfg, "not_a_conflict") == 0.60
        # keep_a falls through to the per-action default (0.90).
        assert resolve_auto_apply_threshold_for(cfg, "keep_a") == 0.90

    def test_out_of_range_raises(self) -> None:
        cfg = {
            "resolve": {
                "auto_apply_threshold_per_action": {"keep_a": 1.5},
            }
        }
        with pytest.raises(ValueError, match="keep_a"):
            resolve_auto_apply_threshold_for(cfg, "keep_a")

    def test_non_numeric_raises(self) -> None:
        cfg = {
            "resolve": {
                "auto_apply_threshold_per_action": {"keep_a": "high"},
            }
        }
        with pytest.raises(ValueError, match="keep_a"):
            resolve_auto_apply_threshold_for(cfg, "keep_a")


class TestMixedLegacyAndPerAction:
    def test_per_action_wins_where_set_legacy_fills_rest(self) -> None:
        # Legacy scalar = 0.85; per-action overrides keep_a only.
        cfg = {
            "resolve": {
                "auto_apply_threshold": 0.85,
                "auto_apply_threshold_per_action": {"keep_a": 0.99},
            }
        }
        # Explicit per-action override wins.
        assert resolve_auto_apply_threshold_for(cfg, "keep_a") == 0.99
        # keep_b is not in the per-action map → legacy scalar fills it.
        assert resolve_auto_apply_threshold_for(cfg, "keep_b") == 0.85
        # not_a_conflict still gets the new default (legacy scalar is
        # explicitly NOT applied to not_a_conflict).
        assert resolve_auto_apply_threshold_for(cfg, "not_a_conflict") == 0.75
        # propose_merge still never auto-applies.
        assert resolve_auto_apply_threshold_for(cfg, "propose_merge") is None


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _proposal(action: str, confidence: float) -> ResolutionProposal:
    return ResolutionProposal(
        recommended_winner="a" if action in ("keep_a",) else "neither",
        action=action,  # type: ignore[arg-type]
        rationale=f"test-{action}",
        confidence=confidence,
        source_precedence_used=["a:user > b:unsourced"],
    )


def _merge_proposal(confidence: float) -> MergeProposal:
    return MergeProposal(
        merge_target_name="test-merge",
        rationale="test propose_merge",
        draft_merged_body="merged body",
        confidence=confidence,
    )


def _escalation(
    name: str, proposal: ResolutionProposal | MergeProposal
) -> EscalationItem:
    return EscalationItem(
        raw_ref=f"wiki/{name.lower()}.md",
        entity_name=name,
        conflict_type="factual",
        description=f"conflict for {name}",
        proposal=proposal,
    )


# ---------------------------------------------------------------------------
# tier4_escalate integration — per-action gate
# ---------------------------------------------------------------------------


class TestTier4PerActionGate:
    def test_three_proposals_at_0_80_only_not_a_conflict_auto_applies(
        self, tmp_path: Path
    ) -> None:
        """At 0.80 confidence with default per-action thresholds:

        * not_a_conflict (threshold 0.75) → 0.80 >= 0.75 → auto-applies.
        * keep_a (threshold 0.90) → 0.80 < 0.90 → stays open.
        * propose_merge (threshold None) → never auto-applies.
        """
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True}}  # no thresholds — use defaults.
        items = [
            _escalation("NotAConflictEntity", _proposal("not_a_conflict", 0.80)),
            _escalation("KeepAEntity", _proposal("keep_a", 0.80)),
            _escalation("ProposeMergeEntity", _merge_proposal(0.80)),
        ]
        tier4_escalate(items, pending, config=cfg)
        text = pending.read_text(encoding="utf-8")

        nac_idx = text.index("NotAConflictEntity")
        ka_idx = text.index("KeepAEntity")
        pm_idx = text.index("ProposeMergeEntity")
        nac_block = text[nac_idx:ka_idx]
        ka_block = text[ka_idx:pm_idx]
        pm_block = text[pm_idx:]

        assert "- [x]" in nac_block
        assert "**Auto-resolved**: true" in nac_block

        assert "- [ ]" in ka_block
        assert "**Auto-resolved**" not in ka_block

        assert "- [ ]" in pm_block
        assert "**Auto-resolved**" not in pm_block

    def test_propose_merge_never_auto_applies_even_at_confidence_1_0(
        self, tmp_path: Path
    ) -> None:
        """Hard rule: propose_merge never auto-applies regardless of confidence.

        Even at 1.0 with auto_apply=True, the block stays ``- [ ]``.
        """
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True}}
        item = _escalation("PerfectMergeEntity", _merge_proposal(1.0))
        tier4_escalate([item], pending, config=cfg)
        text = pending.read_text(encoding="utf-8")

        assert "- [ ]" in text
        assert "**Auto-resolved**" not in text

    def test_keep_a_at_legacy_scalar_threshold_still_works(
        self, tmp_path: Path
    ) -> None:
        """Pre-#170 configs keep working: legacy scalar gates keep_a."""
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True, "auto_apply_threshold": 0.85}}
        item = _escalation("LegacyEntity", _proposal("keep_a", 0.86))
        tier4_escalate([item], pending, config=cfg)
        text = pending.read_text(encoding="utf-8")

        assert "- [x]" in text
        assert "**Auto-resolved**: true" in text


# ---------------------------------------------------------------------------
# Module constants — sanity pin
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_default_per_action_threshold_values(self) -> None:
        assert DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION == {
            "not_a_conflict": 0.75,
            "keep_a": 0.90,
            "keep_b": 0.90,
        }
