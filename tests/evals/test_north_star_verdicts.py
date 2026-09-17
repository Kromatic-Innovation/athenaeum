# SPDX-License-Identifier: Apache-2.0
"""Offline coverage for the north-star report's cost-per-correct-answer,
cost-ratio reading, and three-verdicts additions (issue athenaeum#1734,
design lock ``docs/design/native-memory-baseline.md`` §6/§7), including the
orchestrator rulings from the Quine review of PR#1740:

- R1: all three §7 conditions read the SAME pinned Athenaeum arm
  (``push_breadcrumb_pull`` by default) -- never a cherry-picked
  most-favourable arm per condition.
- R2: condition 1 is a POOLED correctness rate over the whole
  relationship-probe subset, never a max taken over per-class rates.
- R3: condition 3 reads ``"native-zero"`` (a PASS) when the better native
  arm scored zero correct answers while the verdict arm scored at least
  one; ``"undefined"`` (a fail) only when both sides scored zero.
- R4: condition 2 skips (and names) any class where either side has no
  gradable rows, rather than silently treating it as "not worse".

Correctness-grading fixtures use the real ``core`` corpus (fast, no
generation) so ``grade_correctness`` exercises real probe/answer_tokens
data, mirroring ``test_north_star_report.py``'s own convention. Pure
cost-ratio-band and cutoff-scale tests operate on hand-built
``CostPerCorrect``/``ScaleVerdict`` values directly and need no corpus.
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
    _relationship_probe_ids,
    build_report,
    compute_cost_per_correct,
    compute_cost_ratios,
    compute_cutoff_scale,
    compute_verdicts,
    render_cost_per_correct_table,
    render_decision_block,
    render_report,
)
from tests.evals.rollout import Arm, RolloutRecord, TurnTokenUsage

CORPUS_SCALE = "core"
_CORPUS = build_corpus(scale=CORPUS_SCALE)

# Real core probes (tests/evals/data/corpus/probes/probes.yaml):
# - "bluewater_terms" (single_hop) expects client-bluewater, type=company --
#   IN the relationship-use-case subset.
# - "spend_approver_named" (multi_hop) expects policy-budget-approval AND
#   person-amir-osei -- a person page among its expected_uids, so IN the
#   subset even though one of its two expected pages is not person/company.
# - "acme_billing_exception" (distractor_robustness) expects client-acme
#   (type=company) -- NOT in the subset: its probe_class is not one of the
#   four relationship classes, despite the type match.
# - "pto_allowance" (single_hop) expects policy-pto, type=principle -- NOT
#   in the subset: its probe_class matches but its expected page's type
#   does not.
# - "confidentiality_rule" (single_hop) expects policy-confidentiality,
#   type=principle -- also NOT in the subset, used as a second
#   non-relationship probe for condition-2 fixtures.
# - "abstain_unknown_person" (abstention) has no expected_uids at all.
RELATIONSHIP_PROBE_ID = "bluewater_terms"
RELATIONSHIP_ANSWER_TOKEN = "Thornmere"
MULTI_HOP_RELATIONSHIP_PROBE_ID = "spend_approver_named"
MULTI_HOP_RELATIONSHIP_ANSWER_TOKEN = "Voltmere"
OTHER_PROBE_ID = "pto_allowance"
OTHER_ANSWER_TOKEN = "Cinderquill"
OTHER_PROBE_ID_2 = "confidentiality_rule"
OTHER_ANSWER_TOKEN_2 = "Harrowvex"
ABSTENTION_PROBE_ID = "abstain_unknown_person"

VERDICT_ARM = Arm.PUSH_BREADCRUMB_PULL


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


def _minimal_report():
    return build_report([])


def _all_pass_verdict(scale: str) -> ScaleVerdict:
    return ScaleVerdict(
        corpus_scale=scale,
        verdict_arm=VERDICT_ARM.value,
        condition1_pass=True,
        condition1_detail="ok",
        condition2_pass=True,
        condition2_detail="ok",
        condition3_pass=True,
        condition3_reading="target",
        condition3_detail="ok",
    )


def _failing_verdict(scale: str, *, detail: str = "lost the relationship use case") -> ScaleVerdict:
    return ScaleVerdict(
        corpus_scale=scale,
        verdict_arm=VERDICT_ARM.value,
        condition1_pass=False,
        condition1_detail=detail,
        condition2_pass=True,
        condition2_detail="ok",
        condition3_pass=True,
        condition3_reading="target",
        condition3_detail="ok",
    )


# ---------------------------------------------------------------------------
# _reading_for_ratio: the four bands (AC: "ratio and reading for each of the
# four bands").
# ---------------------------------------------------------------------------


def test_reading_bands_at_and_around_each_boundary() -> None:
    assert _reading_for_ratio(0.5) == "aspirational"
    assert _reading_for_ratio(0.51) == "target"
    assert _reading_for_ratio(1.0) == "target"
    assert _reading_for_ratio(1.01) == "limit"
    assert _reading_for_ratio(2.0) == "limit"
    assert _reading_for_ratio(2.01) == "fail"
    assert _reading_for_ratio(None) == "undefined"


# ---------------------------------------------------------------------------
# compute_cost_ratios: ruling R1 (pinned verdict arm, no cherry-picking) and
# ruling R3 (native-zero pass vs. both-sides-zero undefined fail).
# ---------------------------------------------------------------------------


def test_compute_cost_ratios_reports_each_band_for_the_pinned_verdict_arm() -> None:
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
    ratios = {r.probe_class: r for r in compute_cost_ratios(costs, verdict_arm="pull")}

    assert ratios["aspirational_class"].reading == "aspirational"
    assert ratios["aspirational_class"].ratio == 0.5
    assert ratios["target_class"].reading == "target"
    assert ratios["target_class"].ratio == 0.75
    assert ratios["limit_class"].reading == "limit"
    assert ratios["limit_class"].ratio == 1.5
    assert ratios["fail_class"].reading == "fail"
    assert ratios["fail_class"].ratio == 2.5


def test_compute_cost_ratios_ignores_a_non_verdict_arm_even_if_cheaper() -> None:
    """Ruling R1: a cheaper NON-verdict Athenaeum arm must never be picked
    over the pinned verdict arm -- this is exactly the cherry-picking the
    Quine review caught."""
    costs = [
        _cost(
            "some_class", "core", "push_pages_upper_bound", 10.0
        ),  # cheap, but not the verdict arm
        _cost("some_class", "core", "push_breadcrumb_pull", 400.0),  # the verdict arm, expensive
        _cost("some_class", "core", "native_index", 200.0),
    ]
    ratio = compute_cost_ratios(costs, verdict_arm="push_breadcrumb_pull")[0]
    assert ratio.athenaeum_cost == 400.0
    assert ratio.ratio == 2.0
    assert ratio.reading == "limit"


def test_compute_cost_ratios_undefined_when_verdict_arm_has_zero_correct() -> None:
    costs = [
        _cost("zero_correct_class", "core", "pull", None),
        _cost("zero_correct_class", "core", "native_index", 200.0),
    ]
    ratio = compute_cost_ratios(costs, verdict_arm="pull")[0]
    assert ratio.ratio is None
    assert ratio.reading == "undefined"
    assert "pull" in ratio.detail


def test_compute_cost_ratios_native_zero_passes_when_verdict_arm_scored() -> None:
    """Ruling R3: the better native arm bought zero correct answers, the
    verdict arm bought at least one -- a PASS stated in words, never a
    fabricated ratio."""
    costs = [
        _cost("native_zero_class", "core", "pull", 150.0),
        _cost("native_zero_class", "core", "native_index", None),
    ]
    ratio = compute_cost_ratios(costs, verdict_arm="pull")[0]
    assert ratio.ratio is None
    assert ratio.reading == "native-zero"
    assert "native" in ratio.detail


def test_compute_cost_ratios_undefined_when_both_sides_scored_zero() -> None:
    """Ruling R3: if the verdict arm ALSO scored zero, this is NOT
    native-zero -- both sides bought nothing, so there is nothing to
    certify a pass against."""
    costs = [
        _cost("both_zero_class", "core", "pull", None),
        _cost("both_zero_class", "core", "native_index", None),
    ]
    ratio = compute_cost_ratios(costs, verdict_arm="pull")[0]
    assert ratio.ratio is None
    assert ratio.reading == "undefined"
    assert "both sides" in ratio.detail


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
    # Decision block leads; the cost-per-correct table renders after the
    # PULL no-call section, per the MUST fix to the introduced regression
    # (it must never precede "## PULL no-call rate", which "## Arms in
    # this report" itself precedes).
    decision_idx = rendered.index("## Decision")
    arms_idx = rendered.index("## Arms in this report")
    pull_idx = rendered.index("## PULL no-call rate")
    cost_idx = rendered.index("## Cost per correct answer")
    assert decision_idx < arms_idx < pull_idx < cost_idx


# ---------------------------------------------------------------------------
# _relationship_probe_ids: the class filter and the type filter, each
# mutated independently (MUST 3).
# ---------------------------------------------------------------------------


def test_relationship_probe_ids_includes_single_hop_company_probe() -> None:
    ids = _relationship_probe_ids({CORPUS_SCALE})
    assert RELATIONSHIP_PROBE_ID in ids  # single_hop + company: included


def test_relationship_probe_ids_includes_multi_hop_probe_with_a_person_page() -> None:
    ids = _relationship_probe_ids({CORPUS_SCALE})
    # "spend_approver_named" expects [policy-budget-approval, person-amir-osei]
    # -- ONE person page among two is enough to include it.
    assert MULTI_HOP_RELATIONSHIP_PROBE_ID in ids


def test_relationship_probe_ids_excludes_wrong_class_despite_type_match() -> None:
    """Class filter, mutated: "acme_billing_exception" targets client-acme
    (type=company) but its probe_class is distractor_robustness, not one of
    the four relationship classes -- excluded despite the type match."""
    ids = _relationship_probe_ids({CORPUS_SCALE})
    assert "acme_billing_exception" not in ids


def test_relationship_probe_ids_excludes_wrong_type_despite_class_match() -> None:
    """Type filter, mutated: "pto_allowance" is single_hop (a relationship
    class) but targets policy-pto (type=principle) -- excluded despite the
    class match."""
    ids = _relationship_probe_ids({CORPUS_SCALE})
    assert OTHER_PROBE_ID not in ids
    assert OTHER_PROBE_ID_2 not in ids


# ---------------------------------------------------------------------------
# compute_verdicts condition 1 (ruling R2): pooled correctness, not a max
# taken over per-class rates. Also covers condition 2's skip-count wording
# (ruling R4).
# ---------------------------------------------------------------------------


def test_condition1_pooled_fails_even_though_max_over_classes_would_pass() -> None:
    """Two relationship probe classes at the same scale:
    - class A (single_hop, "bluewater_terms"): verdict arm 1/1 correct
      (rate 1.0), native 0/1 correct (rate 0.0).
    - class B (multi_hop, "spend_approver_named"): verdict arm 0/4 correct
      (rate 0.0), native 3/4 correct (rate 0.75).

    The OLD (buggy) max-over-classes comparison would read
    max(1.0, 0.0)=1.0 > max(0.0, 0.75)=0.75 and PASS. The pooled rate is
    verdict 1/5=0.2 vs native 3/5=0.6 -- FAILS. This is the fixture that
    would pass on the wrong implementation and must fail on the right one.
    """
    rows = [
        _row(
            _record(
                arm=VERDICT_ARM,
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
    ]
    for i in range(4):
        rows.append(
            _row(
                _record(
                    arm=VERDICT_ARM,
                    probe_id=MULTI_HOP_RELATIONSHIP_PROBE_ID,
                    probe_class="multi_hop",
                    answer="no idea",
                ),
                replicate=i,
            )
        )
        rows.append(
            _row(
                _record(
                    arm=Arm.NATIVE_INDEX,
                    probe_id=MULTI_HOP_RELATIONSHIP_PROBE_ID,
                    probe_class="multi_hop",
                    answer=(
                        f"the answer is {MULTI_HOP_RELATIONSHIP_ANSWER_TOKEN}"
                        if i < 3
                        else "no idea"
                    ),
                ),
                replicate=i,
            )
        )

    verdicts = compute_verdicts(
        rows,
        relationship_probe_ids=frozenset({RELATIONSHIP_PROBE_ID, MULTI_HOP_RELATIONSHIP_PROBE_ID}),
    )
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.condition1_pass is False
    assert "0.2" in verdict.condition1_detail or "1/5" in verdict.condition1_detail


def test_condition2_skips_a_class_with_no_gradable_rows_on_one_side() -> None:
    """Ruling R4: a class where only the verdict arm has rows (no native
    row at all) is SKIPPED, not silently treated as a pass, and the count
    is stated in the detail."""
    rows = [
        # Compared class: single_hop, both sides correct -- passes.
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="single_hop",
                answer=f"policy is {OTHER_ANSWER_TOKEN_2}",
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="single_hop",
                answer=f"policy is {OTHER_ANSWER_TOKEN_2}",
            )
        ),
        # Skipped class: abstention, verdict arm only -- no native row.
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=ABSTENTION_PROBE_ID,
                probe_class="abstention",
                answer="I don't know",
            )
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.condition2_pass is True
    assert "compared 1 classes" in verdict.condition2_detail
    assert "skipped 1" in verdict.condition2_detail
    assert "abstention" in verdict.condition2_detail


# ---------------------------------------------------------------------------
# compute_verdicts condition 3 (ruling R3), end to end: limit, fail,
# undefined, and native-zero.
# ---------------------------------------------------------------------------


def _condition3_rows(
    *, verdict_answer: str, native_answer: str, **token_kwargs
) -> list[RolloutRow]:
    return [
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=verdict_answer,
                **token_kwargs,
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=native_answer,
            )
        ),
    ]


def test_condition3_verdict_level_limit() -> None:
    rows = _condition3_rows(
        verdict_answer=f"25 days ({OTHER_ANSWER_TOKEN})",
        native_answer=f"25 days ({OTHER_ANSWER_TOKEN})",
        input_tokens=250,
        output_tokens=50,  # verdict cost = 300
    )
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert verdicts[0].condition3_reading == "limit"
    assert verdicts[0].condition3_pass is True


def test_condition3_verdict_level_fail() -> None:
    rows = _condition3_rows(
        verdict_answer=f"25 days ({OTHER_ANSWER_TOKEN})",
        native_answer=f"25 days ({OTHER_ANSWER_TOKEN})",
        input_tokens=450,
        output_tokens=50,  # verdict cost = 500, native default cost = 150 -> ratio 3.33
    )
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert verdicts[0].condition3_reading == "fail"
    assert verdicts[0].condition3_pass is False


def test_condition3_verdict_level_undefined_when_both_sides_score_zero() -> None:
    rows = _condition3_rows(verdict_answer="no idea", native_answer="no idea")
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert verdicts[0].condition3_reading == "undefined"
    assert verdicts[0].condition3_pass is False


def test_condition3_verdict_level_native_zero_passes() -> None:
    rows = _condition3_rows(
        verdict_answer=f"25 days ({OTHER_ANSWER_TOKEN})", native_answer="no idea"
    )
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert verdicts[0].condition3_reading == "native-zero"
    assert verdicts[0].condition3_pass is True


# ---------------------------------------------------------------------------
# Arm pinning (ruling R1): a non-verdict Athenaeum arm would win condition 1,
# but only the pinned verdict arm counts.
# ---------------------------------------------------------------------------


def test_arm_pinning_ignores_a_winning_non_verdict_arm() -> None:
    rows = [
        # push_pages_upper_bound (NOT the verdict arm) wins big.
        _row(
            _record(
                arm=Arm.PUSH_PAGES_UPPER_BOUND,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_TOKEN}",
            )
        ),
        # the verdict arm (push_breadcrumb_pull) loses.
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer="I don't know",
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_TOKEN}",
            )
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset({RELATIONSHIP_PROBE_ID}))
    assert len(verdicts) == 1
    # push_pages_upper_bound scored 1/1 (100%), which would have beaten
    # native's 1/1 (100%) in a tie-break, or at least not lost -- but the
    # PINNED verdict arm (push_breadcrumb_pull) scored 0/1 and must lose.
    assert verdicts[0].condition1_pass is False
    assert "push_breadcrumb_pull" in verdicts[0].condition1_detail


# ---------------------------------------------------------------------------
# Cutoff scale: "none" and the medium-or-above eligibility gate; the
# decision block names the failing condition per scale.
# ---------------------------------------------------------------------------


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


def test_render_decision_block_names_failing_condition_per_scale_when_cutoff_is_none() -> None:
    verdicts = [
        _failing_verdict("medium", detail="condition 1: relationship use case not won (x)"),
        _failing_verdict("large", detail="condition 1: relationship use case not won (y)"),
    ]
    rendered = "\n".join(render_decision_block(_minimal_report(), verdicts))
    assert "**Cutoff scale:** `none`" in rendered
    assert "Failing condition per scale" in rendered
    assert "`medium`: condition 1: relationship use case not won (x)" in rendered
    assert "`large`: condition 1: relationship use case not won (y)" in rendered


def test_render_decision_block_suppresses_failing_heading_when_nothing_failed() -> None:
    """SHOULD 4: cutoff is "none" only because no ELIGIBLE (medium-or-above)
    scale was evaluated at all -- every scale actually present passed, so
    there is nothing to list, and the heading must not print an empty list."""
    verdicts = [_all_pass_verdict("core")]
    rendered = "\n".join(render_decision_block(_minimal_report(), verdicts))
    assert "**Cutoff scale:** `none`" in rendered
    assert "Failing condition per scale" not in rendered


def test_render_decision_block_marks_cutoff_eligibility_per_scale() -> None:
    verdicts = [_all_pass_verdict("core"), _all_pass_verdict("medium")]
    rendered = "\n".join(render_decision_block(_minimal_report(), verdicts))
    lines = [
        line
        for line in rendered.splitlines()
        if line.startswith("| core") or line.startswith("| medium")
    ]
    assert any(line.split("|")[2].strip() == "no" for line in lines if line.startswith("| core"))
    assert any(line.split("|")[2].strip() == "yes" for line in lines if line.startswith("| medium"))


def test_render_decision_block_prints_the_verdict_arm() -> None:
    rendered = "\n".join(
        render_decision_block(_minimal_report(), verdict_arm="push_breadcrumb_pull")
    )
    assert "**Verdict arm:** `push_breadcrumb_pull`" in rendered
