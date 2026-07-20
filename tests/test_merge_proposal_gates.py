# SPDX-License-Identifier: Apache-2.0
"""Issue #400 — suppress degenerate over-cluster merge proposals.

The resolver's ``propose_merge`` path had no size cap or confidence floor, so a
degenerate over-cluster (1,600+ source memories folded into one proposed page at
~0.33 confidence) was written to ``wiki/_pending_merges.md`` and re-proposed
every run. These unit tests cover the two new merge-proposal gates and the
config resolvers behind them; the end-to-end suppression through the merge pass
is covered in ``test_librarian_merge.py``.
"""

from __future__ import annotations

import pytest

from athenaeum.config import resolve_max_merge_sources, resolve_min_merge_confidence
from athenaeum.merge import _merge_proposal_suppression_reason


class TestResolveMaxMergeSources:
    def test_default_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_MERGE_SOURCES", raising=False)
        assert resolve_max_merge_sources(None) == 25
        assert resolve_max_merge_sources({}) == 25

    def test_yaml_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_MERGE_SOURCES", raising=False)
        assert resolve_max_merge_sources({"librarian": {"max_merge_sources": 100}}) == 100

    def test_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MAX_MERGE_SOURCES", "7")
        assert resolve_max_merge_sources({"librarian": {"max_merge_sources": 100}}) == 7

    def test_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_MERGE_SOURCES", raising=False)
        assert resolve_max_merge_sources({"librarian": {"max_merge_sources": 0}}) == 0

    def test_bool_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_MERGE_SOURCES", raising=False)
        # `max_merge_sources: yes` parses as True (int subclass) — must NOT
        # become a cap of 1.
        assert resolve_max_merge_sources({"librarian": {"max_merge_sources": True}}) == 25

    def test_non_numeric_env_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MAX_MERGE_SOURCES", "lots")
        assert resolve_max_merge_sources(None) == 25


class TestResolveMinMergeConfidence:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MIN_MERGE_CONFIDENCE", raising=False)
        assert resolve_min_merge_confidence(None) == 0.0
        assert resolve_min_merge_confidence({}) == 0.0

    def test_yaml_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MIN_MERGE_CONFIDENCE", raising=False)
        assert resolve_min_merge_confidence(
            {"librarian": {"min_merge_confidence": 0.5}}
        ) == 0.5

    def test_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MIN_MERGE_CONFIDENCE", "0.6")
        assert resolve_min_merge_confidence(
            {"librarian": {"min_merge_confidence": 0.5}}
        ) == 0.6

    def test_bool_and_negative_fall_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_MIN_MERGE_CONFIDENCE", raising=False)
        assert resolve_min_merge_confidence(
            {"librarian": {"min_merge_confidence": True}}
        ) == 0.0
        assert resolve_min_merge_confidence(
            {"librarian": {"min_merge_confidence": -0.1}}
        ) == 0.0

    def test_non_numeric_env_and_yaml_fall_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_MIN_MERGE_CONFIDENCE", "high")
        assert resolve_min_merge_confidence(None) == 0.0
        monkeypatch.delenv("ATHENAEUM_MIN_MERGE_CONFIDENCE", raising=False)
        assert resolve_min_merge_confidence(
            {"librarian": {"min_merge_confidence": "high"}}
        ) == 0.0


class TestSuppressionReason:
    def test_over_cluster_suppressed_by_size_cap(self) -> None:
        # The #400 incident shape: 1,700 sources at 0.33 confidence.
        reason = _merge_proposal_suppression_reason(
            n_sources=1700, confidence=0.33, config=None
        )
        assert reason is not None
        assert "1700 sources" in reason and "max_merge_sources=25" in reason

    def test_small_confident_merge_not_suppressed(self) -> None:
        assert (
            _merge_proposal_suppression_reason(n_sources=3, confidence=0.9, config=None)
            is None
        )

    def test_size_cap_boundary_inclusive_keep(self) -> None:
        # Exactly at the cap is kept; one over is suppressed (strict >).
        assert (
            _merge_proposal_suppression_reason(n_sources=25, confidence=0.9, config=None)
            is None
        )
        assert (
            _merge_proposal_suppression_reason(n_sources=26, confidence=0.9, config=None)
            is not None
        )

    def test_disabled_cap_allows_any_size(self) -> None:
        cfg = {"librarian": {"max_merge_sources": 0}}
        assert (
            _merge_proposal_suppression_reason(
                n_sources=1700, confidence=0.9, config=cfg
            )
            is None
        )

    def test_confidence_floor_suppresses_when_configured(self) -> None:
        cfg = {"librarian": {"min_merge_confidence": 0.5}}
        reason = _merge_proposal_suppression_reason(
            n_sources=3, confidence=0.33, config=cfg
        )
        assert reason is not None and "low confidence" in reason

    def test_size_cap_checked_before_confidence(self) -> None:
        # Both would trip; the size reason wins (it is the shape-level signal).
        cfg = {"librarian": {"max_merge_sources": 1, "min_merge_confidence": 0.9}}
        reason = _merge_proposal_suppression_reason(
            n_sources=1700, confidence=0.1, config=cfg
        )
        assert reason is not None and "sources" in reason
