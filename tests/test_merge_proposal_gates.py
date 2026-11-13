# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#400 — suppress degenerate over-cluster merge proposals.

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
from athenaeum.merge_type_gate import _merge_proposal_suppression_reason
from athenaeum.pending_merges import classify_write_kind


class TestResolveMaxMergeSources:
    def test_default_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Issue athenaeum#421 tightened the merge-proposal fan-in cap from 25 to 5.
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
        # The athenaeum#400 incident shape: 1,700 sources at 0.33 confidence.
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
        # Issue athenaeum#421: default cap is now 5. Exactly at the cap is kept; one over
        # is suppressed (strict >). The 6-source boundary is the athenaeum#421 AC case.
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
    """Issue athenaeum#421 — the ACTIVE-by-default mean-pairwise-cohesion floor."""

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


class TestSuppressionReasonPrecision:
    """Issue athenaeum#804 — the suppression message must print each
    threshold-comparison value at a precision that makes the printed
    comparison decidable.

    The original live-store evidence (``mean pairwise 0.60 <
    min_merge_mean_similarity=0.60`` for a cluster whose actual mean was
    0.5996 — true at full float precision, false as printed at 2dp) drove
    the fix via the mean-similarity and complete-linkage arms, both of
    which issue athenaeum#2435 later removed as dead code (their only
    caller never supplied the data either arm needed to fire). The
    remaining confidence-floor and size-cap arms below are unaffected by
    that removal and still need the precision fix.
    """

    def test_low_confidence_message_precision(self) -> None:
        cfg = {"librarian": {"min_merge_confidence": 0.5000}}
        reason = _merge_proposal_suppression_reason(
            n_sources=3, confidence=0.4999, config=cfg
        )
        assert reason is not None and "low confidence" in reason
        assert "0.4999" in reason
        assert "0.5000" in reason

    def test_over_cluster_message_unchanged_already_exact(self) -> None:
        # n_sources/max_merge_sources are integers — no rounding ambiguity
        # existed here, so this message is untouched by the athenaeum#804 fix.
        reason = _merge_proposal_suppression_reason(
            n_sources=6, confidence=0.9, config=None
        )
        assert reason == "over-cluster: 6 sources > max_merge_sources=5"


class TestGateOrdering:
    """Issue athenaeum#421 — size cap checked before confidence.

    The complete-linkage and mean-similarity arms this class used to order
    against were removed as dead code by issue athenaeum#2435 (their only
    caller, ``revalidate_pending_merges``, never supplied the
    ``min_pairwise``/``mean_similarity``/``cluster_threshold`` data either
    arm needed to fire).
    """

    def test_incident_shape_zero_proposals(self) -> None:
        # The 1,711-source incident produces a suppression under the
        # athenaeum#421 size-cap default. Regression alongside
        # test_over_cluster_suppressed_by_size_cap.
        reason = _merge_proposal_suppression_reason(
            n_sources=1711,
            confidence=0.33,
            config=None,
        )
        assert reason is not None


class TestClassifyMergeWriteKind:
    """Issue athenaeum#421 — slug-collision precheck classification."""

    def test_create_merged_when_slug_free(self, tmp_path) -> None:
        (tmp_path).mkdir(parents=True, exist_ok=True)
        assert (
            classify_write_kind("Brand New Topic", tmp_path) == "create-merged"
        )

    def test_fold_into_existing_when_slug_taken(self, tmp_path) -> None:
        from athenaeum.models import slugify

        name = "Existing Topic"
        (tmp_path / f"{slugify(name)}.md").write_text("# existing", encoding="utf-8")
        assert classify_write_kind(name, tmp_path) == "fold-into-existing"

    def test_mirrors_approve_time_target_path(self, tmp_path) -> None:
        # The precheck's existence check must match resolve_merge(approve)'s
        # target path exactly, so a create-merged proposal never hits
        # target_exists: same slugify, same `<slug>.md`, same root.
        from athenaeum.models import slugify

        name = "Weird   Name/With Slashes"
        target = tmp_path / f"{slugify(name)}.md"
        assert classify_write_kind(name, tmp_path) == "create-merged"
        target.write_text("x", encoding="utf-8")
        assert classify_write_kind(name, tmp_path) == "fold-into-existing"
