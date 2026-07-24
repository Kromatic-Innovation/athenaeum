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

from athenaeum.config import (
    resolve_max_merge_sources,
    resolve_min_merge_confidence,
    resolve_min_merge_mean_similarity,
)
from athenaeum.merge import (
    _classify_merge_write_kind,
    _merge_proposal_suppression_reason,
)


class TestResolveMaxMergeSources:
    def test_default_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Issue #421 tightened the merge-proposal fan-in cap from 25 to 5.
        monkeypatch.delenv("ATHENAEUM_MAX_MERGE_SOURCES", raising=False)
        assert resolve_max_merge_sources(None) == 5
        assert resolve_max_merge_sources({}) == 5

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
        assert resolve_max_merge_sources({"librarian": {"max_merge_sources": True}}) == 5

    def test_non_numeric_env_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MAX_MERGE_SOURCES", "lots")
        assert resolve_max_merge_sources(None) == 5


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
        assert "1700 sources" in reason and "max_merge_sources=5" in reason

    def test_small_confident_merge_not_suppressed(self) -> None:
        assert (
            _merge_proposal_suppression_reason(n_sources=3, confidence=0.9, config=None)
            is None
        )

    def test_size_cap_boundary_inclusive_keep(self) -> None:
        # Issue #421: default cap is now 5. Exactly at the cap is kept; one over
        # is suppressed (strict >). The 6-source boundary is the #421 AC case.
        assert (
            _merge_proposal_suppression_reason(n_sources=5, confidence=0.9, config=None)
            is None
        )
        assert (
            _merge_proposal_suppression_reason(n_sources=6, confidence=0.9, config=None)
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


class TestResolveMinMergeMeanSimilarity:
    """Issue #421 — the ACTIVE-by-default mean-pairwise-cohesion floor."""

    def test_default_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY", raising=False)
        assert resolve_min_merge_mean_similarity(None) == 0.6
        assert resolve_min_merge_mean_similarity({}) == 0.6

    def test_yaml_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY", raising=False)
        assert (
            resolve_min_merge_mean_similarity(
                {"librarian": {"min_merge_mean_similarity": 0.75}}
            )
            == 0.75
        )

    def test_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY", "0.9")
        assert (
            resolve_min_merge_mean_similarity(
                {"librarian": {"min_merge_mean_similarity": 0.75}}
            )
            == 0.9
        )

    def test_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY", raising=False)
        assert (
            resolve_min_merge_mean_similarity(
                {"librarian": {"min_merge_mean_similarity": 0.0}}
            )
            == 0.0
        )

    def test_bool_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY", raising=False)
        assert (
            resolve_min_merge_mean_similarity(
                {"librarian": {"min_merge_mean_similarity": True}}
            )
            == 0.6
        )

    def test_non_numeric_env_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY", "high")
        assert resolve_min_merge_mean_similarity(None) == 0.6


class TestCompleteLinkageGate:
    """Issue #421 — complete-linkage: min pairwise below cluster threshold = chain."""

    def test_chain_suppressed(self) -> None:
        # A 4-member cluster whose weakest pair (0.40) is below the 0.55
        # threshold is a single-linkage chain, not a merge — suppressed even
        # though it is small and mean-cohesive.
        reason = _merge_proposal_suppression_reason(
            n_sources=4,
            confidence=0.9,
            config=None,
            mean_similarity=0.8,
            min_pairwise=0.40,
            cluster_threshold=0.55,
        )
        assert reason is not None and "complete-linkage" in reason

    def test_clique_passes(self) -> None:
        # Every pair clears the threshold → a genuine complete-linkage clique.
        assert (
            _merge_proposal_suppression_reason(
                n_sources=4,
                confidence=0.9,
                config=None,
                mean_similarity=0.8,
                min_pairwise=0.70,
                cluster_threshold=0.55,
            )
            is None
        )

    def test_min_at_threshold_kept(self) -> None:
        # Boundary is inclusive-keep (>=): min exactly at threshold is a clique.
        assert (
            _merge_proposal_suppression_reason(
                n_sources=3,
                confidence=0.9,
                config=None,
                mean_similarity=0.8,
                min_pairwise=0.55,
                cluster_threshold=0.55,
            )
            is None
        )

    def test_disabled_when_threshold_zero(self) -> None:
        # A caller that does not supply a threshold (0.0) disables this arm.
        assert (
            _merge_proposal_suppression_reason(
                n_sources=3,
                confidence=0.9,
                config=None,
                mean_similarity=0.8,
                min_pairwise=0.01,
                cluster_threshold=0.0,
            )
            is None
        )


class TestMeanSimilarityFloor:
    """Issue #421 — mean pairwise 0.59 suppressed, 0.61 passes (the AC boundary)."""

    def test_below_floor_suppressed(self) -> None:
        reason = _merge_proposal_suppression_reason(
            n_sources=3,
            confidence=0.9,
            config=None,
            mean_similarity=0.59,
            min_pairwise=0.59,
            cluster_threshold=0.55,
        )
        assert reason is not None and "low cohesion" in reason

    def test_above_floor_passes(self) -> None:
        assert (
            _merge_proposal_suppression_reason(
                n_sources=3,
                confidence=0.9,
                config=None,
                mean_similarity=0.61,
                min_pairwise=0.61,
                cluster_threshold=0.55,
            )
            is None
        )

    def test_at_floor_inclusive_keep(self) -> None:
        # 0.60 exactly clears the floor (strict <).
        assert (
            _merge_proposal_suppression_reason(
                n_sources=3,
                confidence=0.9,
                config=None,
                mean_similarity=0.60,
                min_pairwise=0.60,
                cluster_threshold=0.55,
            )
            is None
        )


class TestGateOrdering:
    """Issue #421 — size cap, then complete-linkage, then mean, then confidence."""

    def test_size_cap_before_complete_linkage(self) -> None:
        reason = _merge_proposal_suppression_reason(
            n_sources=1700,
            confidence=0.9,
            config=None,
            mean_similarity=0.33,
            min_pairwise=0.01,
            cluster_threshold=0.55,
        )
        assert reason is not None and "sources" in reason

    def test_complete_linkage_before_mean(self) -> None:
        # min below threshold AND mean below floor: the chain reason wins.
        reason = _merge_proposal_suppression_reason(
            n_sources=3,
            confidence=0.9,
            config=None,
            mean_similarity=0.30,
            min_pairwise=0.20,
            cluster_threshold=0.55,
        )
        assert reason is not None and "complete-linkage" in reason

    def test_incident_shape_zero_proposals(self) -> None:
        # The 1,711-source / mean-sim-0.33 incident produces a suppression under
        # the #421 defaults (size cap fires first; complete-linkage + mean would
        # also fire). Regression alongside test_over_cluster_suppressed_by_size_cap.
        reason = _merge_proposal_suppression_reason(
            n_sources=1711,
            confidence=0.33,
            config=None,
            mean_similarity=0.33,
            min_pairwise=0.05,
            cluster_threshold=0.55,
        )
        assert reason is not None


class TestClassifyMergeWriteKind:
    """Issue #421 — slug-collision precheck classification."""

    def test_create_merged_when_slug_free(self, tmp_path) -> None:
        (tmp_path).mkdir(parents=True, exist_ok=True)
        assert (
            _classify_merge_write_kind("Brand New Topic", tmp_path) == "create-merged"
        )

    def test_fold_into_existing_when_slug_taken(self, tmp_path) -> None:
        from athenaeum.models import slugify

        name = "Existing Topic"
        (tmp_path / f"{slugify(name)}.md").write_text("# existing", encoding="utf-8")
        assert _classify_merge_write_kind(name, tmp_path) == "fold-into-existing"

    def test_mirrors_approve_time_target_path(self, tmp_path) -> None:
        # The precheck's existence check must match resolve_merge(approve)'s
        # target path exactly, so a create-merged proposal never hits
        # target_exists: same slugify, same `<slug>.md`, same root.
        from athenaeum.models import slugify

        name = "Weird   Name/With Slashes"
        target = tmp_path / f"{slugify(name)}.md"
        assert _classify_merge_write_kind(name, tmp_path) == "create-merged"
        target.write_text("x", encoding="utf-8")
        assert _classify_merge_write_kind(name, tmp_path) == "fold-into-existing"
