# SPDX-License-Identifier: Apache-2.0
"""Contracts for the retrieval metrics and the push-outcome ladder.

Offline and free. These exist because the metric code must not be the thing
that is wrong: a scoring bug reads exactly like a model regression, and would
be chased in the wrong place for as long as it went unnoticed.
"""

from __future__ import annotations

import pytest

from tests.evals.metrics import (
    RecallOutcome,
    grade_abstention,
    grade_push,
    mrr,
    outcome_histogram,
    precision_at_k,
    recall_at_k,
)


class TestLadder:
    def test_miss_when_correct_page_absent(self) -> None:
        out = grade_push(suggested=["wrong-a", "wrong-b"], expected=["right"])
        assert out.outcome is RecallOutcome.MISS
        assert out.expected_missing == ("right",)
        assert out.recall == 0.0

    def test_suggested_when_present_but_unused(self) -> None:
        out = grade_push(suggested=["right", "wrong"], expected=["right"])
        assert out.outcome is RecallOutcome.SUGGESTED

    def test_used_when_agent_drew_on_it_but_push_was_noisy(self) -> None:
        out = grade_push(suggested=["right", "wrong"], expected=["right"], used=["right"])
        assert out.outcome is RecallOutcome.USED
        assert out.wasted_pages == 1

    def test_clean_when_used_and_nothing_wasted(self) -> None:
        out = grade_push(suggested=["right"], expected=["right"], used=["right"])
        assert out.outcome is RecallOutcome.CLEAN
        assert out.wasted_pages == 0
        assert out.precision == 1.0

    def test_precise_but_unused_does_not_reach_clean(self) -> None:
        """Pin interpretive choice 1 (see the module docstring).

        A precise-but-ignored push is not better than an imprecise but
        load-bearing one, so precision alone must not promote past USED. The
        precision component stays readable either way.
        """
        out = grade_push(suggested=["right"], expected=["right"], used=[])
        assert out.outcome is RecallOutcome.SUGGESTED
        assert out.precision == 1.0

    def test_use_of_a_wrong_page_does_not_promote(self) -> None:
        """Using a page that is not ground truth is not evidence of success."""
        out = grade_push(suggested=["right", "wrong"], expected=["right"], used=["wrong"])
        assert out.outcome is RecallOutcome.SUGGESTED
        assert out.used == ()

    def test_partial_recall_still_grades_on_what_was_used(self) -> None:
        out = grade_push(suggested=["a"], expected=["a", "b"], used=["a"])
        assert out.outcome is RecallOutcome.CLEAN
        assert out.recall == 0.5
        assert out.expected_missing == ("b",)

    def test_rungs_are_ordered(self) -> None:
        assert (
            RecallOutcome.MISS < RecallOutcome.SUGGESTED < RecallOutcome.USED < RecallOutcome.CLEAN
        )


class TestAbstention:
    def test_empty_push_is_ideal(self) -> None:
        assert grade_abstention([]).outcome is RecallOutcome.CLEAN

    def test_any_push_is_a_miss(self) -> None:
        out = grade_abstention(["plausible-but-wrong"])
        assert out.outcome is RecallOutcome.MISS
        assert out.wasted_pages == 1

    def test_abstention_probes_are_rejected_by_the_normal_ladder(self) -> None:
        """Grading an abstention probe on the main ladder would record a
        permanent MISS for behaving correctly, so it must fail loudly."""
        with pytest.raises(ValueError, match="abstention"):
            grade_push(suggested=[], expected=[])


class TestRankedMeasures:
    def test_recall_at_k_respects_the_cutoff(self) -> None:
        ranked = ["a", "b", "c", "d"]
        assert recall_at_k(ranked, ["a", "d"], k=2) == 0.5
        assert recall_at_k(ranked, ["a", "d"], k=4) == 1.0

    def test_precision_at_k_divides_by_results_returned(self) -> None:
        """A system returning two results, both correct, is fully precise and
        must not be penalised for returning fewer than k."""
        assert precision_at_k(["a", "b"], ["a", "b"], k=5) == 1.0

    def test_mrr_is_rank_sensitive_where_recall_is_not(self) -> None:
        first = ["right", "x", "y"]
        fifth = ["x", "y", "z", "w", "right"]
        assert recall_at_k(first, ["right"], k=5) == recall_at_k(fifth, ["right"], k=5)
        assert mrr(first, ["right"]) == 1.0
        assert mrr(fifth, ["right"]) == pytest.approx(0.2)

    def test_mrr_is_zero_when_absent(self) -> None:
        assert mrr(["x", "y"], ["right"]) == 0.0

    def test_empty_results_do_not_divide_by_zero(self) -> None:
        assert precision_at_k([], ["a"], k=5) == 0.0
        assert recall_at_k([], ["a"], k=5) == 0.0
        assert mrr([], ["a"]) == 0.0


def test_histogram_reports_every_rung_including_empty_ones() -> None:
    """An omitted zero row makes 'no CLEAN results at all' look like a missing
    row rather than the finding it is."""
    histogram = outcome_histogram(
        [
            grade_push(["right"], ["right"], used=["right"]),
            grade_push(["wrong"], ["right"]),
        ]
    )
    assert histogram == {"MISS": 1, "SUGGESTED": 0, "USED": 0, "CLEAN": 1}
