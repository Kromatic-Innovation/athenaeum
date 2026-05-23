# SPDX-License-Identifier: Apache-2.0
"""Tests for the issue #156 auto-apply lane.

Covers:

- ``resolve_auto_apply`` env > yaml > default precedence.
- ``resolve_auto_apply_threshold`` env > yaml > default + range validation.
- ``_get_model`` precedence (env > yaml > default).
- ``apply_auto_resolution`` idempotency.
- Round-trip: an auto-resolved block survives ``ingest_answers`` and the
  ``Auto-resolved`` marker lands in the raw answer file.
- Integration: ``tier4_escalate`` flips only high-confidence (>= threshold)
  blocks; low-confidence stay ``- [ ]``. Exact-threshold matches are inclusive.
- ``auto_apply=False`` short-circuits even at confidence 1.0.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.answers import ingest_answers
from athenaeum.models import EscalationItem
from athenaeum.resolutions import (
    DEFAULT_AUTO_APPLY,
    DEFAULT_AUTO_APPLY_THRESHOLD,
    DEFAULT_RESOLVE_MODEL,
    ResolutionProposal,
    _get_model,
    apply_auto_resolution,
    resolve_auto_apply,
    resolve_auto_apply_threshold,
)
from athenaeum.tiers import tier4_escalate


def _make_proposal(
    confidence: float, rationale: str = "user > unsourced"
) -> ResolutionProposal:
    return ResolutionProposal(
        recommended_winner="a",
        action="keep_a",
        rationale=rationale,
        confidence=confidence,
        source_precedence_used=["a:user > b:unsourced"],
    )


# ---------------------------------------------------------------------------
# Config resolution — resolve_auto_apply
# ---------------------------------------------------------------------------


class TestResolveAutoApply:
    def test_env_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_AUTO_APPLY", "true")
        assert resolve_auto_apply({"resolve": {"auto_apply": False}}) is True

    def test_env_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_AUTO_APPLY", "false")
        assert resolve_auto_apply({"resolve": {"auto_apply": True}}) is False

    def test_env_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_AUTO_APPLY", "YES")
        assert resolve_auto_apply(None) is True

    def test_env_invalid_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_AUTO_APPLY", "maybe")
        # Falls through to yaml setting.
        assert resolve_auto_apply({"resolve": {"auto_apply": False}}) is False

    def test_yaml_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_AUTO_APPLY", raising=False)
        assert resolve_auto_apply({"resolve": {"auto_apply": True}}) is True

    def test_yaml_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_AUTO_APPLY", raising=False)
        assert resolve_auto_apply({"resolve": {"auto_apply": False}}) is False

    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_AUTO_APPLY", raising=False)
        assert resolve_auto_apply(None) is DEFAULT_AUTO_APPLY is True


# ---------------------------------------------------------------------------
# Config resolution — resolve_auto_apply_threshold
# ---------------------------------------------------------------------------


class TestResolveAutoApplyThreshold:
    def test_env_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD", "0.95")
        assert resolve_auto_apply_threshold(None) == 0.95

    def test_env_out_of_range_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD", "1.5")
        with pytest.raises(ValueError, match="ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD"):
            resolve_auto_apply_threshold(None)

    def test_env_non_numeric_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD", "abc")
        with pytest.raises(ValueError, match="ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD"):
            resolve_auto_apply_threshold(None)

    def test_yaml_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD", raising=False)
        cfg = {"resolve": {"auto_apply_threshold": 0.85}}
        assert resolve_auto_apply_threshold(cfg) == 0.85

    def test_yaml_out_of_range_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD", raising=False)
        cfg = {"resolve": {"auto_apply_threshold": 2.0}}
        with pytest.raises(ValueError, match="resolve.auto_apply_threshold"):
            resolve_auto_apply_threshold(cfg)

    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD", raising=False)
        assert (
            resolve_auto_apply_threshold(None) == DEFAULT_AUTO_APPLY_THRESHOLD == 0.90
        )


# ---------------------------------------------------------------------------
# _get_model precedence
# ---------------------------------------------------------------------------


class TestGetModelPrecedence:
    def test_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_MODEL", "env-model")
        cfg = {"resolve": {"model": "yaml-model"}}
        assert _get_model(cfg) == "env-model"

    def test_yaml_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_MODEL", raising=False)
        cfg = {"resolve": {"model": "yaml-model"}}
        assert _get_model(cfg) == "yaml-model"

    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVE_MODEL", raising=False)
        assert _get_model(None) == DEFAULT_RESOLVE_MODEL


# ---------------------------------------------------------------------------
# apply_auto_resolution idempotency + shape
# ---------------------------------------------------------------------------


_BLOCK = (
    '## [2026-05-23] Entity: "Tristan" (from wiki/tristan.md)\n'
    "- [ ] Is Tristan German or American?\n"
    "\n"
    "**Conflict type**: factual\n"
    "**Description**: Snippet A says German; snippet B says American.\n"
)


class TestApplyAutoResolution:
    def test_flips_checkbox(self) -> None:
        out = apply_auto_resolution(_BLOCK, _make_proposal(0.93), model="m-1")
        assert "- [x] Is Tristan German or American?" in out
        assert "- [ ] Is Tristan German or American?" not in out

    def test_inserts_answer_block_before_conflict_type(self) -> None:
        out = apply_auto_resolution(_BLOCK, _make_proposal(0.93), model="m-1")
        answer_pos = out.index("**Answer:**")
        conflict_pos = out.index("**Conflict type**:")
        assert answer_pos < conflict_pos
        assert "**Auto-resolved**: true" in out
        assert "**Resolver model**: m-1" in out
        assert "**Resolver confidence**: 0.93" in out

    def test_idempotent(self) -> None:
        once = apply_auto_resolution(_BLOCK, _make_proposal(0.93), model="m-1")
        twice = apply_auto_resolution(once, _make_proposal(0.93), model="m-1")
        assert once == twice

    def test_no_unchecked_returns_unchanged(self) -> None:
        already = _BLOCK.replace("- [ ]", "- [x]")
        out = apply_auto_resolution(already, _make_proposal(0.93), model="m-1")
        assert out == already


# ---------------------------------------------------------------------------
# Round-trip through ingest_answers
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_auto_resolved_block_survives_ingest(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        pending = wiki / "_pending_questions.md"

        item = EscalationItem(
            raw_ref="wiki/tristan.md",
            entity_name="Tristan",
            conflict_type="factual",
            description="A says German; B says American.",
            proposal=_make_proposal(0.95, rationale="user direct overrides unsourced"),
        )
        cfg = {"resolve": {"auto_apply": True, "auto_apply_threshold": 0.90}}
        tier4_escalate([item], pending, config=cfg)

        written = pending.read_text(encoding="utf-8")
        assert "- [x]" in written
        assert "**Auto-resolved**: true" in written

        # Now ingest — the [x] block should be archived and a raw answer
        # file should be written.
        raw_root = tmp_path / "raw"
        n = ingest_answers(pending, raw_root)
        assert n == 1

        answer_files = list((raw_root / "answers").glob("*.md"))
        assert len(answer_files) == 1
        body = answer_files[0].read_text(encoding="utf-8")
        assert "Auto-resolved" in body
        assert "Resolver confidence" in body

        # Archive carries the original block.
        archive = (pending.parent / "_pending_questions_archive.md").read_text(
            encoding="utf-8"
        )
        assert "Auto-resolved" in archive


# ---------------------------------------------------------------------------
# tier4_escalate integration — high-conf flipped, low-conf untouched
# ---------------------------------------------------------------------------


class TestTier4Integration:
    def test_mixed_confidence(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True, "auto_apply_threshold": 0.90}}
        items = [
            EscalationItem(
                raw_ref="wiki/high.md",
                entity_name="HighConfEntity",
                conflict_type="factual",
                description="conflict A vs B",
                proposal=_make_proposal(0.95),
            ),
            EscalationItem(
                raw_ref="wiki/low.md",
                entity_name="LowConfEntity",
                conflict_type="factual",
                description="conflict C vs D",
                proposal=_make_proposal(0.50),
            ),
        ]
        tier4_escalate(items, pending, config=cfg)
        text = pending.read_text(encoding="utf-8")

        # High-conf block flipped.
        high_idx = text.index("HighConfEntity")
        low_idx = text.index("LowConfEntity")
        high_block = text[high_idx:low_idx]
        low_block = text[low_idx:]
        assert "- [x]" in high_block
        assert "**Auto-resolved**: true" in high_block

        # Low-conf untouched.
        assert "- [ ]" in low_block
        assert "**Auto-resolved**" not in low_block

    def test_exact_threshold_inclusive(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True, "auto_apply_threshold": 0.90}}
        item = EscalationItem(
            raw_ref="wiki/edge.md",
            entity_name="EdgeEntity",
            conflict_type="factual",
            description="exactly at threshold",
            proposal=_make_proposal(0.90),
        )
        tier4_escalate([item], pending, config=cfg)
        text = pending.read_text(encoding="utf-8")
        assert "- [x]" in text
        assert "**Auto-resolved**: true" in text

    def test_auto_apply_disabled_short_circuit(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": False, "auto_apply_threshold": 0.90}}
        item = EscalationItem(
            raw_ref="wiki/highest.md",
            entity_name="HighestEntity",
            conflict_type="factual",
            description="confidence one point oh",
            proposal=_make_proposal(1.0),
        )
        tier4_escalate([item], pending, config=cfg)
        text = pending.read_text(encoding="utf-8")
        assert "- [ ]" in text
        assert "**Auto-resolved**" not in text

    def test_no_config_no_auto_apply(self, tmp_path: Path) -> None:
        """Legacy callers (config=None) get pre-#156 behavior."""
        pending = tmp_path / "_pending_questions.md"
        item = EscalationItem(
            raw_ref="wiki/legacy.md",
            entity_name="LegacyEntity",
            conflict_type="factual",
            description="d",
            proposal=_make_proposal(0.99),
        )
        tier4_escalate([item], pending)
        text = pending.read_text(encoding="utf-8")
        assert "- [ ]" in text
        assert "**Auto-resolved**" not in text
