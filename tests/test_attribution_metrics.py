# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the calibration metrics (issue athenaeum#2211).

All canned data, no model call — these exist so the metric code itself is
never the thing wrong with a calibration measurement (mirrors
``tests/test_eval_metrics.py``'s own rationale for the retrieval ladder).
"""

from __future__ import annotations

import pytest

from athenaeum.models import TokenUsage
from tests.evals.metrics import (
    abstain_share,
    auroc,
    cost_per_case,
    derive_threshold,
    expected_calibration_error,
    reliability_table,
    wilson_interval,
    wilson_lower_bound,
)


class TestWilsonInterval:
    def test_all_successes_upper_bound_touches_one(self) -> None:
        """A documented edge property of the Wilson interval: observing
        n/n successes gives an upper bound of EXACTLY 1.0 (symmetrically,
        0/n gives a lower bound of exactly 0.0) — see
        ``test_all_failures_lower_bound_touches_zero``."""
        lower, upper = wilson_interval(10, 10)
        assert upper == 1.0
        assert lower < upper

    def test_near_perfect_score_interval_is_below_one(self) -> None:
        lower, upper = wilson_interval(9, 10)
        assert upper < 1.0
        assert lower < upper

    def test_all_failures_lower_bound_touches_zero(self) -> None:
        lower, upper = wilson_interval(0, 10)
        assert lower == 0.0
        assert upper > 0.0

    def test_more_data_narrows_the_interval(self) -> None:
        small_lower, small_upper = wilson_interval(9, 10)
        big_lower, big_upper = wilson_interval(90, 100)
        assert (big_upper - big_lower) < (small_upper - small_lower)

    def test_n_zero_is_maximally_uncertain(self) -> None:
        assert wilson_interval(0, 0) == (0.0, 1.0)

    def test_lower_bound_matches_interval(self) -> None:
        assert wilson_lower_bound(8, 10) == wilson_interval(8, 10)[0]

    def test_higher_confidence_widens_interval(self) -> None:
        narrow = wilson_interval(8, 10, confidence=0.80)
        wide = wilson_interval(8, 10, confidence=0.99)
        assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


class TestAuroc:
    def test_perfect_separation_is_one(self) -> None:
        scores = [0.9, 0.8, 0.3, 0.1]
        labels = [True, True, False, False]
        assert auroc(scores, labels) == pytest.approx(1.0)

    def test_inverted_separation_is_zero(self) -> None:
        scores = [0.1, 0.2, 0.8, 0.9]
        labels = [True, True, False, False]
        assert auroc(scores, labels) == pytest.approx(0.0)

    def test_no_information_is_near_half(self) -> None:
        scores = [0.5, 0.5, 0.5, 0.5]
        labels = [True, False, True, False]
        assert auroc(scores, labels) == pytest.approx(0.5)

    def test_ties_count_as_half_a_point(self) -> None:
        # One positive and one negative tied at the same score: half credit.
        assert auroc([0.5, 0.5], [True, False]) == pytest.approx(0.5)

    def test_no_positive_case_raises(self) -> None:
        with pytest.raises(ValueError):
            auroc([0.1, 0.2], [False, False])

    def test_no_negative_case_raises(self) -> None:
        with pytest.raises(ValueError):
            auroc([0.1, 0.2], [True, True])


class TestReliabilityTable:
    def test_five_bins_for_ten_cases(self) -> None:
        scores = [0.1, 0.15, 0.3, 0.35, 0.5, 0.55, 0.7, 0.75, 0.9, 0.95]
        correct = [False, False, False, True, True, True, True, True, True, True]
        table = reliability_table(scores, correct, n_bins=5)
        assert len(table) == 5
        for lo, hi, acc in table:
            assert lo <= hi
            assert 0.0 <= acc <= 1.0

    def test_perfectly_calibrated_bins_match_accuracy_to_midpoint_ordering(self) -> None:
        # Low-score bin is all-wrong, high-score bin is all-right.
        scores = [0.1] * 4 + [0.9] * 4
        correct = [False] * 4 + [True] * 4
        table = reliability_table(scores, correct, n_bins=2)
        assert table[0][2] == 0.0
        assert table[1][2] == 1.0

    def test_empty_input_yields_no_bins(self) -> None:
        assert reliability_table([], []) == []

    def test_fewer_cases_than_bins_still_returns_nonempty_bins_only(self) -> None:
        table = reliability_table([0.2, 0.8], [True, False], n_bins=5)
        assert len(table) == 2


class TestExpectedCalibrationError:
    def test_perfectly_calibrated_is_near_zero(self) -> None:
        # Predicted scores equal observed accuracy within each bin exactly.
        scores = [0.0, 0.0, 1.0, 1.0]
        correct = [False, False, True, True]
        assert expected_calibration_error(scores, correct, n_bins=2) == pytest.approx(0.0)

    def test_badly_miscalibrated_is_large(self) -> None:
        # High confidence (0.95) but wrong half the time.
        scores = [0.95] * 10
        correct = [True, False] * 5
        assert expected_calibration_error(scores, correct, n_bins=1) == pytest.approx(0.45)

    def test_empty_input_is_zero(self) -> None:
        assert expected_calibration_error([], []) == 0.0


class TestDeriveThreshold:
    def test_finds_lowest_qualifying_threshold(self) -> None:
        scores = [0.2, 0.4, 0.6, 0.8, 1.0]
        correct = [False, False, True, True, True]
        # At threshold 0.6: {0.6, 0.8, 1.0} all correct -> accuracy 1.0 >= 0.9.
        threshold = derive_threshold(scores, correct, target_accuracy=0.9)
        assert threshold == 0.6

    def test_returns_none_when_nothing_qualifies(self) -> None:
        # Not even the single highest-scoring case is correct, so no
        # threshold -- including the maximum score -- can ever clear the
        # target.
        scores = [0.2, 0.4, 0.6, 0.8, 1.0]
        correct = [False, False, False, False, False]
        assert derive_threshold(scores, correct, target_accuracy=0.5) is None

    def test_empty_scores_returns_none(self) -> None:
        assert derive_threshold([], [], target_accuracy=0.9) is None

    def test_low_target_picks_the_lowest_score(self) -> None:
        scores = [0.3, 0.6, 0.9]
        correct = [True, True, True]
        assert derive_threshold(scores, correct, target_accuracy=0.5) == 0.3


class TestAbstainShare:
    def test_half_below_threshold(self) -> None:
        assert abstain_share([0.1, 0.2, 0.8, 0.9], threshold=0.5) == pytest.approx(0.5)

    def test_nothing_below_threshold_is_zero(self) -> None:
        assert abstain_share([0.6, 0.7, 0.8], threshold=0.5) == 0.0

    def test_empty_scores_is_zero(self) -> None:
        assert abstain_share([], threshold=0.5) == 0.0

    def test_threshold_boundary_is_not_abstained(self) -> None:
        # Strictly below -- a score equal to the threshold passes.
        assert abstain_share([0.5], threshold=0.5) == 0.0


class TestCostPerCase:
    def test_divides_estimated_cost_by_case_count(self) -> None:
        usage = TokenUsage()
        usage.add(1000, 500, model="claude-haiku-4-5-20251001", knob="attribution")
        per_case = cost_per_case(usage, 10)
        assert per_case == pytest.approx(usage.estimated_cost_usd / 10)
        assert per_case > 0.0

    def test_zero_cases_is_zero(self) -> None:
        usage = TokenUsage()
        assert cost_per_case(usage, 0) == 0.0

    def test_negative_cases_is_zero(self) -> None:
        usage = TokenUsage()
        assert cost_per_case(usage, -3) == 0.0
