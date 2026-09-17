# SPDX-License-Identifier: Apache-2.0
"""Offline coverage for the north-star report's cost-per-correct-answer,
cost-ratio reading, and three-verdicts additions (issue athenaeum#1734,
design lock ``docs/design/native-memory-baseline.md`` §6/§7).

Correctness-grading fixtures use the real ``core`` corpus (fast, no
generation) so ``grade_correctness`` exercises real probe/answer_tokens
data, mirroring ``test_north_star_report.py``'s own convention. Cost-ratio
band and cutoff-scale tests operate on hand-built ``CostPerCorrect``/
``ScaleVerdict`` values directly and need no corpus at all.
"""

from __future__ import annotations

from tests.evals.containment import GridCell
from tests.evals.corpus import build_corpus
from tests.evals.north_star_report import (
    CostPerCorrect,
    RolloutRow,
    ScaleVerdict,
    WriteCost,
    _reading_for_ratio,
    build_report,
    compute_cost_per_correct,
    compute_cost_ratios,
    compute_cutoff_scale,
    compute_verdicts,
    render_cost_per_correct_table,
    render_report,
)
from tests.evals.rollout import Arm, RolloutRecord, TurnTokenUsage

CORPUS_SCALE = "core"
_CORPUS = build_corpus(scale=CORPUS_SCALE)

# Real core probes (tests/evals/data/corpus/probes/probes.yaml):
# - "bluewater_terms" (single_hop) expects client-bluewater, type=company --
#   IN the relationship-use-case subset.
# - "pto_allowance" (single_hop) expects policy-pto, type=principle -- NOT
#   in the subset. Both are the same probe_class, which is deliberate: the
#   subset filter is per-PROBE, never per whole class.
RELATIONSHIP_PROBE_ID = "bluewater_terms"
RELATIONSHIP_ANSWER_TOKEN = "Thornmere"
OTHER_PROBE_ID = "pto_allowance"
OTHER_ANSWER_TOKEN = "Cinderquill"


def _record(
    *,
    arm: Arm,
    probe_id: str,
    probe_class: str,
    answer: str,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> RolloutRecord:
    return RolloutRecord(
        arm=arm,
        probe_id=probe_id,
        probe_class=probe_class,
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[
            TurnTokenUsage(turn=0, input_tokens=input_tokens, output_tokens=output_tokens)
        ],
    )


def _row(record: RolloutRecord, *, replicate: int = 0) -> RolloutRow:
    cell = GridCell(
        probe=record.probe_id,
        arm=record.arm.value,
        corpus_scale=record.corpus_scale,
        replicate=replicate,
    )
    return RolloutRow(cell=cell, record=record)


def _cost(probe_class: str, scale: str, arm: str, cost: float | None) -> CostPerCorrect:
    """A synthetic CostPerCorrect for pure cost-ratio-band tests -- no rows,
    no corpus, just the ratio machinery."""
    return CostPerCorrect(
        probe_class=probe_class,
        corpus_scale=scale,
        arm=arm,
        n=1,
        correct_n=0 if cost is None else 1,
        read_input_tokens=0,
        read_output_tokens=0,
        write_tokens_amortized=None,
        write_tokens_raw=None,
        cost_per_correct=cost,
        undefined_reason=None if cost is not None else "zero correct answers in this cell",
    )


# ---------------------------------------------------------------------------
# _reading_for_ratio / compute_cost_ratios: the four bands (AC: "ratio and
# reading for each of the four bands").
# ---------------------------------------------------------------------------


def test_reading_bands_at_and_around_each_boundary() -> None:
    assert _reading_for_ratio(0.5) == "aspirational"
    assert _reading_for_ratio(0.51) == "target"
    assert _reading_for_ratio(1.0) == "target"
    assert _reading_for_ratio(1.01) == "limit"
    assert _reading_for_ratio(2.0) == "limit"
    assert _reading_for_ratio(2.01) == "fail"
    assert _reading_for_ratio(None) == "undefined"


def test_compute_cost_ratios_reports_each_band() -> None:
    costs = [
        _cost("aspirational_class", "core", "pull", 100.0),
        _cost("aspirational_class", "core", "native_index", 200.0),
        _cost("target_class", "core", "pull", 150.0),
        _cost("target_class", "core", "native_index", 200.0),
        _cost("limit_class", "core", "pull", 300.0),
        _cost("limit_class", "core", "native_index", 200.0),
        _cost("fail_class", "core", "pull", 500.0),
        _cost("fail_class", "core", "native_index", 200.0),
    ]
    ratios = {r.probe_class: r for r in compute_cost_ratios(costs)}

    assert ratios["aspirational_class"].reading == "aspirational"
    assert ratios["aspirational_class"].ratio == 0.5
    assert ratios["target_class"].reading == "target"
    assert ratios["target_class"].ratio == 0.75
    assert ratios["limit_class"].reading == "limit"
    assert ratios["limit_class"].ratio == 1.5
    assert ratios["fail_class"].reading == "fail"
    assert ratios["fail_class"].ratio == 2.5


def test_compute_cost_ratios_undefined_when_athenaeum_side_has_zero_correct() -> None:
    costs = [
        _cost("zero_correct_class", "core", "pull", None),
        _cost("zero_correct_class", "core", "native_index", 200.0),
    ]
    ratio = compute_cost_ratios(costs)[0]
    assert ratio.ratio is None
    assert ratio.reading == "undefined"
    assert "athenaeum" in ratio.detail


# ---------------------------------------------------------------------------
# Zero-correct cell -> cost undefined, never inf/0 (AC2).
# ---------------------------------------------------------------------------


def test_zero_correct_cell_reports_cost_undefined_not_inf_or_zero() -> None:
    rows = [
        _row(
            _record(
                arm=Arm.PULL,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer="no idea",  # wrong -- no answer_tokens present
            )
        ),
    ]
    costs = compute_cost_per_correct(rows)
    assert len(costs) == 1
    cell = costs[0]
    assert cell.correct_n == 0
    assert cell.cost_per_correct is None
    assert cell.undefined_reason == "zero correct answers in this cell"


def test_cost_per_correct_correct_cell_divides_total_tokens() -> None:
    rows = [
        _row(
            _record(
                arm=Arm.PULL,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"25 days ({OTHER_ANSWER_TOKEN})",
                input_tokens=80,
                output_tokens=20,
            )
        ),
    ]
    costs = compute_cost_per_correct(rows)
    assert len(costs) == 1
    cell = costs[0]
    assert cell.correct_n == 1
    assert cell.read_input_tokens == 80
    assert cell.read_output_tokens == 20
    assert cell.cost_per_correct == 100.0
    assert cell.undefined_reason is None


# ---------------------------------------------------------------------------
# Phase 2 write cost: amortized over the probe set, raw spend alongside;
# Phase-1-only rendering has neither column (AC3).
# ---------------------------------------------------------------------------


def test_write_cost_amortized_and_raw_when_write_costs_supplied() -> None:
    rows = [
        _row(
            _record(
                arm=Arm.PULL,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"25 days ({OTHER_ANSWER_TOKEN})",
                input_tokens=100,
                output_tokens=50,
            )
        ),
    ]
    write_costs = [
        WriteCost(
            system="athenaeum", corpus_scale=CORPUS_SCALE, input_tokens=1000, output_tokens=500
        )
    ]
    costs = compute_cost_per_correct(rows, write_costs=write_costs, probe_counts={CORPUS_SCALE: 10})
    cell = costs[0]
    # write total = 1500, amortized over 10 probes * 1 row in this cell = 150.0
    assert cell.write_tokens_amortized == 150.0
    assert cell.write_tokens_raw == 1500
    # (100 + 50 + 150) / 1 correct
    assert cell.cost_per_correct == 300.0

    rendered = render_cost_per_correct_table(costs)
    text = "\n".join(rendered)
    assert "write_tokens_amortized" in text
    assert "write_tokens_raw" in text
    assert "150.0" in text
    assert "1500" in text


def test_render_cost_per_correct_table_omits_write_columns_when_phase1_only() -> None:
    rows = [
        _row(
            _record(
                arm=Arm.PULL,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"25 days ({OTHER_ANSWER_TOKEN})",
            )
        ),
    ]
    costs = compute_cost_per_correct(rows)  # no write_costs
    rendered = "\n".join(render_cost_per_correct_table(costs))
    # The prose paragraph mentions write_tokens_amortized unconditionally;
    # the TABLE COLUMN must not, so check the header/row markers specifically.
    assert "| write_tokens_amortized |" not in rendered
    assert "write_tokens_raw |" not in rendered
    assert "cost_per_correct" in rendered


def test_render_report_is_phase1_only_when_report_carries_no_write_costs() -> None:
    rows = [
        _row(
            _record(
                arm=Arm.PULL,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"25 days ({OTHER_ANSWER_TOKEN})",
            )
        ),
    ]
    report = build_report(rows)  # no write_costs kwarg
    rendered = render_report(report)
    assert "## Decision (design doc §7, athenaeum#1734)" in rendered
    assert "## Cost per correct answer (athenaeum#1734)" in rendered
    assert "| write_tokens_amortized |" not in rendered
    # Decision block renders before any dimension table.
    assert rendered.index("## Decision") < rendered.index("## Arms in this report")
    assert rendered.index("## Cost per correct answer") < rendered.index("## Arms in this report")


# ---------------------------------------------------------------------------
# compute_verdicts: condition 1 (relationship use case) passing while
# condition 2 (every other use case) fails at the same scale.
# ---------------------------------------------------------------------------


def test_condition1_passes_and_condition2_fails_at_same_scale() -> None:
    rows = [
        # Relationship subset (bluewater_terms, company page): Athenaeum wins.
        _row(
            _record(
                arm=Arm.PULL,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_TOKEN}",
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer="I don't know",
            )
        ),
        # Non-relationship (pto_allowance, policy page): Athenaeum loses.
        _row(
            _record(
                arm=Arm.PULL,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer="no idea",
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"25 days ({OTHER_ANSWER_TOKEN})",
            )
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset({RELATIONSHIP_PROBE_ID}))
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.condition1_pass is True
    assert verdict.condition2_pass is False
    assert "single_hop" in verdict.condition2_detail
    assert verdict.all_pass is False


# ---------------------------------------------------------------------------
# Cutoff scale: "none" and the medium-or-above eligibility gate.
# ---------------------------------------------------------------------------


def _all_pass_verdict(scale: str) -> ScaleVerdict:
    return ScaleVerdict(
        corpus_scale=scale,
        condition1_pass=True,
        condition1_detail="ok",
        condition2_pass=True,
        condition2_detail="ok",
        condition3_pass=True,
        condition3_reading="target",
        condition3_detail="ok",
    )


def _failing_verdict(scale: str) -> ScaleVerdict:
    return ScaleVerdict(
        corpus_scale=scale,
        condition1_pass=False,
        condition1_detail="lost the relationship use case",
        condition2_pass=True,
        condition2_detail="ok",
        condition3_pass=True,
        condition3_reading="target",
        condition3_detail="ok",
    )


def test_cutoff_is_none_when_no_eligible_scale_passes() -> None:
    verdicts = [_all_pass_verdict("core"), _all_pass_verdict("small"), _failing_verdict("medium")]
    # core/small pass every condition but are below the §7 "medium and
    # above" gate, and medium itself fails -- so there is no cutoff.
    assert compute_cutoff_scale(verdicts) == "none"


def test_cutoff_is_smallest_medium_or_above_passing_scale() -> None:
    verdicts = [
        _all_pass_verdict("core"),
        _failing_verdict("small"),
        _all_pass_verdict("medium"),
        _all_pass_verdict("large"),
    ]
    assert compute_cutoff_scale(verdicts) == "medium"
