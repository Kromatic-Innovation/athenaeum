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
RELATIONSHIP_UID = "client-bluewater"
RELATIONSHIP_ANSWER_MARKER = "Twelve consulting days per month"
MULTI_HOP_RELATIONSHIP_PROBE_ID = "spend_approver_named"
MULTI_HOP_RELATIONSHIP_ANSWER_TOKEN = "Voltmere"
# Only "person-amir-osei" is answer-bearing for this multi_hop probe -- its
# other expected_uids page, "policy-budget-approval", carries no planted
# marker (tests.evals.corpus.answer_bearing_uids confirms this on the real
# core corpus). Delivering only the policy page would therefore never grade
# correct; the person page is the one that matters here.
MULTI_HOP_RELATIONSHIP_UID = "person-amir-osei"
MULTI_HOP_RELATIONSHIP_ANSWER_MARKER = "from a written note by itself"
OTHER_PROBE_ID = "pto_allowance"
OTHER_ANSWER_TOKEN = "Cinderquill"
OTHER_UID = "policy-pto"
OTHER_ANSWER_MARKER = "25 days per year"
OTHER_PROBE_ID_2 = "confidentiality_rule"
OTHER_ANSWER_TOKEN_2 = "Harrowvex"
OTHER_UID_2 = "policy-confidentiality"
OTHER_ANSWER_MARKER_2 = "Nadia Frost"
ABSTENTION_PROBE_ID = "abstain_unknown_person"

VERDICT_ARM = Arm.PUSH_BREADCRUMB_PULL


def _recall_transcript(*uids: str) -> list[dict]:
    """A PULL/PUSH_BREADCRUMB_PULL-shaped transcript whose recall
    tool_result names *uids* via ``**Uid:** <uid>`` lines -- the same shape
    ``_pull_delivered_text`` parses (mirrors
    ``test_north_star_report.py``'s ``_recall_output_record`` helper)."""
    text = "".join(f"**Uid:** {uid}\n" for uid in uids)
    return [
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": text}],
                    }
                ]
            },
        }
    ]


def _push_transcript(*uids: str) -> list[dict]:
    """A PUSH_PAGES_UPPER_BOUND-shaped transcript: *uids* named via
    ``**Uid:** <uid>`` lines in ``transcript[0]["pushed_context"]``, the
    field ``_push_delivered_text`` reads."""
    text = "".join(f"**Uid:** {uid}\n" for uid in uids)
    return [{"pushed_context": text}]


def _native_transcript(*uids: str) -> list[dict]:
    """A NATIVE_INDEX/NATIVE_GREP-shaped transcript: *uids* as the stems of
    ``transcript[0]["native_memory"]["loaded_memory_files"]`` paths, the
    shape ``_native_loaded_uids`` reads."""
    return [{"native_memory": {"loaded_memory_files": {f"{uid}.md": "" for uid in uids}}}]


def _record(
    *,
    arm: Arm,
    probe_id: str,
    probe_class: str,
    answer: str,
    input_tokens: int = 100,
    output_tokens: int = 50,
    # Issue athenaeum#1825: compute_verdicts now keeps only
    # `search_backend == "vector"` rows (re-pinned by operator ruling on
    # issue athenaeum#1736), so every fixture built by this helper must
    # carry that stamp by default or the whole verdicts suite's ordinary
    # (backend-agnostic) test data would be silently excluded. Tests that
    # specifically exercise the fts5/None exclusion pass a different value.
    search_backend: str = "vector",
    # Issue athenaeum#1831: grade_correctness now also requires the page's
    # uid in this rollout's delivered-uid evidence (arm-specific, extracted
    # from the transcript) -- see _recall_transcript/_push_transcript/
    # _native_transcript above. None (the default) means "no transcript",
    # correct for Arm.ORACLE (delivers expected_uids without one) and for
    # any fixture whose answer is deliberately wrong.
    transcript: list[dict] | None = None,
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
        search_backend=search_backend,
        transcript=transcript if transcript is not None else [],
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
        write_tokens_native_share_amortized=None,
        write_tokens_native_share_raw=None,
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
                arm=Arm.ORACLE,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"the allowance is {OTHER_ANSWER_MARKER}",
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
                arm=Arm.ORACLE,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"the allowance is {OTHER_ANSWER_MARKER}",
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
                arm=Arm.ORACLE,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"the allowance is {OTHER_ANSWER_MARKER}",
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


# ---------------------------------------------------------------------------
# Shared-write accounting (athenaeum#1870, ruling athenaeum#1830): an
# Athenaeum arm's write cost is the native writer's own amortised share
# PLUS the librarian's filing share, never the filing share alone. Native
# arms are unchanged; an absent native WriteCost degrades honestly to
# filing-only, distinguishable in the rendered table from both "no write
# data at all" and "this arm is native".
# ---------------------------------------------------------------------------


def test_athenaeum_arm_combined_write_cost_is_native_share_plus_filing() -> None:
    rows = [
        _row(
            _record(
                arm=Arm.ORACLE,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"the allowance is {OTHER_ANSWER_MARKER}",
                input_tokens=100,
                output_tokens=50,
            )
        ),
    ]
    write_costs = [
        WriteCost(
            system="athenaeum", corpus_scale=CORPUS_SCALE, input_tokens=1000, output_tokens=500
        ),
        WriteCost(
            system="native", corpus_scale=CORPUS_SCALE, input_tokens=6000, output_tokens=3000
        ),
    ]
    costs = compute_cost_per_correct(
        rows, write_costs=write_costs, probe_counts={CORPUS_SCALE: 10}
    )
    cell = costs[0]
    # filing total = 1500, native total = 9000; each amortized over 10
    # probes * 1 row in this cell -- 150.0 and 900.0.
    assert cell.write_tokens_native_share_amortized == 900.0
    assert cell.write_tokens_native_share_raw == 9000
    assert cell.write_tokens_amortized == 150.0 + 900.0
    assert cell.write_tokens_raw == 1500 + 9000
    # (100 + 50 + 1050) / 1 correct
    assert cell.cost_per_correct == 1200.0

    rendered = "\n".join(render_cost_per_correct_table(costs))
    assert "| write_native_share_amortized | write_filing_share_amortized " in rendered
    assert "900.0" in rendered  # native share, isolated
    assert "150.0" in rendered  # filing share, isolated
    assert "1050.0" in rendered  # combined write_tokens_amortized
    assert "10500" in rendered  # combined write_tokens_raw


def test_native_arm_write_cost_unchanged_by_shared_write_accounting() -> None:
    rows = [
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer="no idea",  # correctness is irrelevant to this test
            )
        ),
    ]
    write_costs = [
        WriteCost(
            system="athenaeum", corpus_scale=CORPUS_SCALE, input_tokens=1000, output_tokens=500
        ),
        WriteCost(
            system="native", corpus_scale=CORPUS_SCALE, input_tokens=6000, output_tokens=3000
        ),
    ]
    costs = compute_cost_per_correct(
        rows, write_costs=write_costs, probe_counts={CORPUS_SCALE: 10}
    )
    cell = costs[0]
    # Native total = 9000, amortized over 10 probes * 1 row = 900.0 -- the
    # athenaeum (filing) entry never enters a native arm's own figure, and
    # there is no separate "share" to report (the whole figure already is
    # native): both new fields are None, never 0.0.
    assert cell.write_tokens_amortized == 900.0
    assert cell.write_tokens_raw == 9000
    assert cell.write_tokens_native_share_amortized is None
    assert cell.write_tokens_native_share_raw is None

    rendered = "\n".join(render_cost_per_correct_table(costs))
    row_line = next(line for line in rendered.splitlines() if "native_index" in line)
    cells = [c.strip() for c in row_line.strip("|").split("|")]
    # probe_class | corpus_scale | arm | n | correct_n | read_tokens |
    # write_native_share_amortized | write_filing_share_amortized |
    # write_tokens_amortized | write_tokens_raw | cost_per_correct
    assert cells[6] == "n/a"  # native share: no split for a native arm
    assert cells[7] == "n/a"  # filing share: ditto
    assert cells[8] == "900.0"
    assert cells[9] == "9000"


def test_athenaeum_arm_degrades_to_filing_only_when_native_write_cost_absent() -> None:
    """No ("native", scale) WriteCost was captured at all -- e.g. a Phase 2
    run dispatched with --phase2-systems athenaeum alone. The arm is
    charged filing cost only (matching the pre-athenaeum#1870 figure), the
    native-share fields stay None, and the rendered table marks the gap
    with a distinct token rather than reusing the "n/a" a native arm gets."""
    rows = [
        _row(
            _record(
                arm=Arm.ORACLE,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"the allowance is {OTHER_ANSWER_MARKER}",
                input_tokens=100,
                output_tokens=50,
            )
        ),
    ]
    write_costs = [
        WriteCost(
            system="athenaeum", corpus_scale=CORPUS_SCALE, input_tokens=1000, output_tokens=500
        ),
        # No "native" WriteCost at this scale.
    ]
    costs = compute_cost_per_correct(
        rows, write_costs=write_costs, probe_counts={CORPUS_SCALE: 10}
    )
    cell = costs[0]
    assert cell.write_tokens_amortized == 150.0
    assert cell.write_tokens_raw == 1500
    assert cell.write_tokens_native_share_amortized is None
    assert cell.write_tokens_native_share_raw is None

    rendered = "\n".join(render_cost_per_correct_table(costs))
    row_line = next(line for line in rendered.splitlines() if "oracle" in line)
    cells = [c.strip() for c in row_line.strip("|").split("|")]
    assert cells[6] == "not-captured"  # native share -- distinct from "n/a"
    assert cells[7] == "150.0"  # filing share -- the figure IS filing-only
    assert cells[8] == "150.0"  # combined amortized (filing-only)
    assert cells[9] == "1500"


def test_render_report_is_phase1_only_when_report_carries_no_write_costs() -> None:
    rows = [
        _row(
            _record(
                arm=Arm.ORACLE,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"the allowance is {OTHER_ANSWER_MARKER}",
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
# Issue athenaeum#1825: §7 verdicts are RE-PINNED to the "vector" arm
# (operator ruling on issue athenaeum#1736, 2026-09-18) -- this SUPERSEDES
# the earlier fts5 pin (ruling R1, issue athenaeum#1787). An fts5-backend
# row (and a pre-athenaeum#1764 row whose `search_backend` is the dataclass
# default -- ``None``, since the field did not exist yet) must never enter
# compute_verdicts, even if a caller hands it a mixed *rows* sequence
# (check_floor_mismatch only guards the CLI's own --store path, not this
# function directly). The spoiler set direction below FLIPS from the
# pre-athenaeum#1825 shape: it is now the fts5/None-backend rows that must be
# excluded, not the vector rows.
# ---------------------------------------------------------------------------


def test_compute_verdicts_ignores_fts5_backend_rows() -> None:
    """A spoiler set of default-backend (``search_backend=None``, the
    pre-athenaeum#1764/fts5 shape) rows, planted so that if they were pooled
    into condition 1's correctness rate the verdict would FLIP from pass to
    fail, must not move the verdict at all: the vector-only computation and
    the vector-plus-fts5-spoiler computation must agree.
    """
    vector_rows = [
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                transcript=_recall_transcript(RELATIONSHIP_UID),
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

    # Spoiler: the SAME probe, with the dataclass-default ``search_backend``
    # (``None`` -- the pre-athenaeum#1764/fts5 shape), with the verdict arm
    # now wrong and native now right -- if pooled with the vector rows above
    # this would flip condition 1 from pass (1/1 > 0/1) to a tie or a fail.
    # It must be excluded entirely, not merely outweighed.
    fts5_rows = [
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer="I don't know",
                search_backend=None,
            ),
            replicate=1,
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                search_backend=None,
            ),
            replicate=1,
        ),
    ]

    relationship_ids = frozenset({RELATIONSHIP_PROBE_ID})
    vector_only = compute_verdicts(vector_rows, relationship_probe_ids=relationship_ids)
    mixed = compute_verdicts(vector_rows + fts5_rows, relationship_probe_ids=relationship_ids)

    assert len(vector_only) == 1
    assert len(mixed) == 1
    assert vector_only[0].condition1_pass is True
    assert mixed[0].condition1_pass is True
    assert mixed[0] == vector_only[0]


def test_render_decision_block_names_the_pin_for_an_fts5_only_store() -> None:
    """Issue athenaeum#1825 AC: an old fts5-only store (every row
    ``search_backend=None``, the pre-athenaeum#1764 shape) renders a §7
    decision block that says WHICH backend the verdict arm is pinned to and
    WHY the verdict is empty -- not the generic "no scale ... was
    evaluated" wording a genuinely empty store gets."""
    fts5_only_rows = [
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                search_backend=None,
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer="I don't know",
                search_backend=None,
            )
        ),
    ]
    report = build_report(fts5_only_rows)

    rendered = "\n".join(render_decision_block(report))

    assert '`search_backend="vector"`' in rendered
    assert "athenaeum#1736" in rendered
    assert "2026-09-18" in rendered
    # Must NOT read as the genuinely-empty-store message -- that would hide
    # the real reason (wrong backend dispatched, not "no run happened").
    assert "so there is nothing to certify a cutoff against" not in rendered


def test_render_decision_block_genuinely_empty_store_keeps_the_generic_message() -> None:
    """Control for the test above: a truly empty store (no rows at all)
    must still get the pre-existing generic message, not the fts5-only-pin
    explanation -- that explanation requires rows to be PRESENT and ALL
    excluded, not merely absent."""
    rendered = "\n".join(render_decision_block(_minimal_report()))

    assert "so there is nothing to certify a cutoff against" in rendered
    assert "has rows, but none recorded" not in rendered


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
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                transcript=_recall_transcript(RELATIONSHIP_UID),
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
                        f"the answer is {MULTI_HOP_RELATIONSHIP_ANSWER_MARKER}"
                        if i < 3
                        else "no idea"
                    ),
                    transcript=(
                        _native_transcript(MULTI_HOP_RELATIONSHIP_UID) if i < 3 else None
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
                answer=f"policy is {OTHER_ANSWER_MARKER_2}",
                transcript=_recall_transcript(OTHER_UID_2),
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="single_hop",
                answer=f"policy is {OTHER_ANSWER_MARKER_2}",
                transcript=_native_transcript(OTHER_UID_2),
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
                transcript=_recall_transcript(OTHER_UID),
                **token_kwargs,
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=native_answer,
                transcript=_native_transcript(OTHER_UID),
            )
        ),
    ]


def test_condition3_verdict_level_limit() -> None:
    rows = _condition3_rows(
        verdict_answer=f"25 days ({OTHER_ANSWER_MARKER})",
        native_answer=f"25 days ({OTHER_ANSWER_MARKER})",
        input_tokens=250,
        output_tokens=50,  # verdict cost = 300
    )
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert verdicts[0].condition3_reading == "limit"
    assert verdicts[0].condition3_pass is True


def test_condition3_verdict_level_fail() -> None:
    rows = _condition3_rows(
        verdict_answer=f"25 days ({OTHER_ANSWER_MARKER})",
        native_answer=f"25 days ({OTHER_ANSWER_MARKER})",
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
        verdict_answer=f"25 days ({OTHER_ANSWER_MARKER})", native_answer="no idea"
    )
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert verdicts[0].condition3_reading == "native-zero"
    assert verdicts[0].condition3_pass is True


# ---------------------------------------------------------------------------
# Arm pinning (ruling R1): a non-verdict Athenaeum arm would win condition 1,
# but only the pinned verdict arm counts.
# ---------------------------------------------------------------------------


def test_arm_pinning_ignores_a_winning_non_verdict_arm() -> None:
    """A "pick the most-correct Athenaeum arm" mutant would use
    push_pages_upper_bound's 1/1 (100%) against native's 0/1 (0%) and PASS
    condition 1. The correct, arm-PINNED implementation reads only the
    verdict arm (push_breadcrumb_pull), which also scored 0/1 -- 0.0 is not
    strictly greater than native's own 0.0, so condition 1 must FAIL. The
    two implementations diverge on this fixture, which is what makes it
    detect the mutant (a same-scoring native, as in an earlier draft of
    this test, does not: both implementations would fail it either way)."""
    rows = [
        # push_pages_upper_bound (NOT the verdict arm) wins big.
        _row(
            _record(
                arm=Arm.PUSH_PAGES_UPPER_BOUND,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                transcript=_push_transcript(RELATIONSHIP_UID),
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
        # native ALSO loses -- so a mutant reading push_pages_upper_bound's
        # 1/1 against native's 0/1 would flip this fixture to a PASS.
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer="I don't know",
            )
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset({RELATIONSHIP_PROBE_ID}))
    assert len(verdicts) == 1
    assert verdicts[0].condition1_pass is False
    assert "push_breadcrumb_pull" in verdicts[0].condition1_detail
    assert "push_pages_upper_bound" not in verdicts[0].condition1_detail


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


def test_cutoff_reaches_xlarge_when_it_is_the_only_passing_scale() -> None:
    """Issue athenaeum#1735: pins ``xlarge`` as a real, reachable cutoff
    result. Without ``xlarge`` in ``SIZE_SCALE_ORDER`` (and therefore in
    ``_CUTOFF_ELIGIBLE_SCALES``), this verdict set would fall through
    ``compute_cutoff_scale``'s walk and return ``"none"`` even though an
    xlarge-only rollout actually passed every condition -- a silent data
    loss a smaller-scales-only fixture set cannot detect."""
    verdicts = [
        _failing_verdict("medium"),
        _failing_verdict("large"),
        _all_pass_verdict("xlarge"),
    ]
    assert compute_cutoff_scale(verdicts) == "xlarge"


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


# ---------------------------------------------------------------------------
# Condition 2, restored: a genuine failure path (mutation target: deleting
# `condition2_pass = False`), and arm-pinning on condition 2 itself.
# ---------------------------------------------------------------------------


def test_condition1_passes_and_condition2_fails_on_a_compared_class() -> None:
    rows = [
        # Relationship subset (bluewater_terms, company page): verdict arm wins.
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                transcript=_recall_transcript(RELATIONSHIP_UID),
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
        # Non-relationship (pto_allowance): verdict arm loses to native.
        _row(
            _record(
                arm=VERDICT_ARM,
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
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
                transcript=_native_transcript(OTHER_UID),
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


def test_condition2_arm_pinning_ignores_a_rescuing_non_verdict_arm() -> None:
    """A "pick the most-correct Athenaeum arm" mutant would let
    push_pages_upper_bound's correct answer rescue this class. The pinned
    verdict arm scored incorrectly and must still fail condition 2."""
    rows = [
        _row(
            _record(
                arm=Arm.PUSH_PAGES_UPPER_BOUND,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="single_hop",
                answer=f"policy is {OTHER_ANSWER_MARKER_2}",
                transcript=_push_transcript(OTHER_UID_2),
            )
        ),
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="single_hop",
                answer="no idea",
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="single_hop",
                answer=f"policy is {OTHER_ANSWER_MARKER_2}",
                transcript=_native_transcript(OTHER_UID_2),
            )
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert len(verdicts) == 1
    assert verdicts[0].condition2_pass is False
    assert "single_hop" in verdicts[0].condition2_detail


# ---------------------------------------------------------------------------
# Verdict-arm wiring, end to end: build_report -> render_report.
# ---------------------------------------------------------------------------


def test_build_report_and_render_report_thread_a_non_default_verdict_arm() -> None:
    rows = [
        _row(
            _record(
                arm=Arm.PULL,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer="I don't know",  # wrong -- verdict arm loses
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",  # native wins
                transcript=_native_transcript(RELATIONSHIP_UID),
            )
        ),
    ]
    report = build_report(rows, verdict_arm="pull")
    rendered = render_report(report)
    assert "**Verdict arm:** `pull`" in rendered
    # The rendered failing-condition detail must name the ACTUAL verdict
    # arm used ("pull"), proving build_report -> render_report ->
    # render_decision_block -> compute_verdicts threads it through, not a
    # hardcoded default surviving somewhere along that chain.
    assert "'pull'" in rendered


# ---------------------------------------------------------------------------
# Condition 1: a tie must not pass (`>`, never `>=`).
# ---------------------------------------------------------------------------


def test_condition1_tie_does_not_pass() -> None:
    rows = [
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                transcript=_recall_transcript(RELATIONSHIP_UID),
            ),
            replicate=0,
        ),
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer="I don't know",
            ),
            replicate=1,
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                transcript=_native_transcript(RELATIONSHIP_UID),
            ),
            replicate=0,
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer="I don't know",
            ),
            replicate=1,
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset({RELATIONSHIP_PROBE_ID}))
    # Both sides pool to 1/2 = 0.5 -- an exact tie. `>=` would pass this;
    # the design doc's rule ("beats", "exceeds") is strict `>`.
    assert verdicts[0].condition1_pass is False


# ---------------------------------------------------------------------------
# Two native arms: condition 1 picks the HIGHER pooled rate (max, never
# min); condition 3 picks the CHEAPER DEFINED cost (min, never max), and a
# zero-scoring native arm is dropped rather than treated as free.
# ---------------------------------------------------------------------------


def test_condition1_picks_the_higher_pooled_native_rate() -> None:
    rows = [
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                transcript=_recall_transcript(RELATIONSHIP_UID),
            ),
            replicate=0,
        ),
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer="I don't know",
            ),
            replicate=1,
        ),
        # native_index: 0/1 correct.
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer="I don't know",
            )
        ),
        # native_grep: 1/1 correct -- the HIGHER pooled rate; a min-picking
        # mutant would compare the verdict arm against native_index's 0.0
        # instead and incorrectly PASS.
        _row(
            _record(
                arm=Arm.NATIVE_GREP,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                transcript=_native_transcript(RELATIONSHIP_UID),
            )
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset({RELATIONSHIP_PROBE_ID}))
    assert verdicts[0].condition1_pass is False  # verdict 0.5 <= best native 1.0
    assert "native_grep" in verdicts[0].condition1_detail
    assert "native_index" not in verdicts[0].condition1_detail


def test_compute_cost_ratios_picks_the_cheaper_defined_native_cost() -> None:
    costs = [
        _cost("cls", "core", "pull", 150.0),
        _cost("cls", "core", "native_index", 300.0),
        _cost("cls", "core", "native_grep", 100.0),  # the cheaper one
    ]
    ratio = compute_cost_ratios(costs, verdict_arm="pull")[0]
    assert ratio.native_cost == 100.0
    assert ratio.ratio == 1.5
    assert ratio.reading == "limit"  # a max-picking mutant would read 0.5 ("aspirational")


def test_compute_cost_ratios_drops_a_zero_scoring_native_arm() -> None:
    costs = [
        _cost("cls", "core", "pull", 150.0),
        _cost("cls", "core", "native_index", None),  # zero correct answers -- must be dropped
        _cost("cls", "core", "native_grep", 100.0),
    ]
    ratio = compute_cost_ratios(costs, verdict_arm="pull")[0]
    assert ratio.native_cost == 100.0
    assert ratio.reading == "limit"


# ---------------------------------------------------------------------------
# Condition 3: worst reading across classes (never the best), and ruling R5
# (a class with no native rows at all is skipped, named, and does not fail
# the scale on its own).
# ---------------------------------------------------------------------------


def test_condition3_reports_the_worst_reading_not_the_best() -> None:
    rows = [
        # class_a: verdict cost 300, native cost 150 -> ratio 2.0 ("limit").
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID,
                probe_class="class_a",
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
                input_tokens=250,
                output_tokens=50,
                transcript=_recall_transcript(OTHER_UID),
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID,
                probe_class="class_a",
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
                transcript=_native_transcript(OTHER_UID),
            )
        ),
        # class_b: verdict cost 500, native cost 150 -> ratio ~3.33 ("fail").
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="class_b",
                answer=f"policy is {OTHER_ANSWER_MARKER_2}",
                input_tokens=450,
                output_tokens=50,
                transcript=_recall_transcript(OTHER_UID_2),
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="class_b",
                answer=f"policy is {OTHER_ANSWER_MARKER_2}",
                transcript=_native_transcript(OTHER_UID_2),
            )
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert len(verdicts) == 1
    # The worst (highest-ranked) reading across classes is "fail" -- a
    # best-picking mutant would report "limit" (class_a) instead.
    assert verdicts[0].condition3_reading == "fail"
    assert verdicts[0].condition3_pass is False
    assert "compared 2 classes, skipped 0" in verdicts[0].condition3_detail


def test_condition3_skips_a_class_with_no_native_rows_at_all() -> None:
    rows = [
        # class_a: compared, verdict cost 300 vs native cost 150 -> "limit".
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID,
                probe_class="class_a",
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
                input_tokens=250,
                output_tokens=50,
                transcript=_recall_transcript(OTHER_UID),
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID,
                probe_class="class_a",
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
                transcript=_native_transcript(OTHER_UID),
            )
        ),
        # class_b: verdict arm only -- NO native row at all -- skipped.
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="class_b",
                answer=f"policy is {OTHER_ANSWER_MARKER_2}",
            )
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert len(verdicts) == 1
    verdict = verdicts[0]
    # Only the compared class ("limit") counts -- the skip must not fail
    # the scale on its own (ruling R5).
    assert verdict.condition3_reading == "limit"
    assert verdict.condition3_pass is True
    assert "compared 1 classes, skipped 1" in verdict.condition3_detail
    assert "class_b" in verdict.condition3_detail


# ---------------------------------------------------------------------------
# The amortisation-denominator sentence, pinned exactly.
# ---------------------------------------------------------------------------


def test_decision_block_states_the_amortisation_denominator_in_words() -> None:
    rows = [
        _row(
            _record(
                arm=Arm.ORACLE,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"the allowance is {OTHER_ANSWER_MARKER}",
            )
        ),
    ]
    write_costs = [
        WriteCost(
            system="athenaeum", corpus_scale=CORPUS_SCALE, input_tokens=1000, output_tokens=500
        )
    ]
    report = build_report(rows, write_costs=write_costs)
    rendered = render_report(report)
    probe_count = len(_CORPUS.probes)
    assert f"write cost amortised over {probe_count} probes at `{CORPUS_SCALE}`." in rendered


# ---------------------------------------------------------------------------
# Final round (Quine re-review, PR#1740): the verdict_arm seam between
# compute_verdicts and compute_cost_ratios, _READING_RANK["undefined"]'s
# worst-rank position, and the passing-cutoff render path.
# ---------------------------------------------------------------------------


def test_compute_verdicts_passes_verdict_arm_through_to_cost_ratios() -> None:
    """A fixture where ``--verdict-arm pull`` wins condition 1 (a
    relationship row) AND has a DEFINED, in-budget cost (a separate,
    non-relationship row) -- condition 3 must read `target`/`limit` for
    `pull` specifically. If compute_verdicts dropped `verdict_arm` when
    calling compute_cost_ratios (silently falling back to
    DEFAULT_VERDICT_ARM, "push_breadcrumb_pull"), no CostPerCorrect row
    would match that arm at all, `athenaeum_cost` would be `None`, and this
    would flip straight to `"undefined"` even though `pull` unambiguously
    won and stayed in budget -- that flip is what this test detects."""
    rows = [
        # Relationship subset: pull wins condition 1.
        _row(
            _record(
                arm=Arm.PULL,
                probe_id=RELATIONSHIP_PROBE_ID,
                probe_class="single_hop",
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                transcript=_recall_transcript(RELATIONSHIP_UID),
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
        # Separate, non-relationship class: pull has a DEFINED, in-budget
        # cost per correct (150 / 100 = 1.5 -> "limit").
        _row(
            _record(
                arm=Arm.PULL,
                probe_id=OTHER_PROBE_ID,
                probe_class="cost_class",
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
                input_tokens=100,
                output_tokens=50,
                transcript=_recall_transcript(OTHER_UID),
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID,
                probe_class="cost_class",
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
                input_tokens=70,
                output_tokens=30,
                transcript=_native_transcript(OTHER_UID),
            )
        ),
    ]
    verdicts = compute_verdicts(
        rows, verdict_arm="pull", relationship_probe_ids=frozenset({RELATIONSHIP_PROBE_ID})
    )
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.condition1_pass is True
    assert verdict.condition3_reading in ("target", "limit")
    assert verdict.condition3_pass is True


def test_reading_rank_undefined_beats_limit_as_the_worst_reading() -> None:
    """One `limit` class and one `undefined` class (both COMPARED -- native
    ran in both) at the same scale: the worst reading must be `undefined`,
    never `limit`. This pins `_READING_RANK["undefined"]` above `"limit"`
    directly through `compute_verdicts` (not just `compute_cost_ratios`)."""
    rows = [
        # class_a: limit (verdict 150, native 100 -> ratio 1.5).
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID,
                probe_class="class_a",
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
                input_tokens=100,
                output_tokens=50,
                transcript=_recall_transcript(OTHER_UID),
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID,
                probe_class="class_a",
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
                input_tokens=70,
                output_tokens=30,
                transcript=_native_transcript(OTHER_UID),
            )
        ),
        # class_b: undefined -- BOTH sides score zero (native_present True,
        # so this is a COMPARED class under R5, not a skip).
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="class_b",
                answer="no idea",
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="class_b",
                answer="no idea",
            )
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert len(verdicts) == 1
    assert verdicts[0].condition3_reading == "undefined"
    assert verdicts[0].condition3_pass is False


def test_render_decision_block_prints_the_passing_cutoff_scale() -> None:
    """A fixture where `medium` passes all three conditions -- the block
    must print the ACTUAL cutoff scale, never a hardcoded `none`."""
    verdicts = [_all_pass_verdict("medium")]
    rendered = "\n".join(render_decision_block(_minimal_report(), verdicts))
    assert "**Cutoff scale:** `medium`" in rendered
    assert "**Cutoff scale:** `none`" not in rendered


def test_condition1_fails_when_there_are_no_relationship_rows() -> None:
    rows = [
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID,
                probe_class="single_hop",
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
            )
        ),
    ]
    verdicts = compute_verdicts(rows, relationship_probe_ids=frozenset())
    assert len(verdicts) == 1
    assert verdicts[0].condition1_pass is False


# ---------------------------------------------------------------------------
# report_only (issue athenaeum#1776): a report_only class is excluded from
# conditions 2 and 3 as if its rows were never in the store; condition 1's
# relationship subset is unaffected; render_decision_block names the
# excluded classes.
# ---------------------------------------------------------------------------


def test_condition2_and_3_report_only_class_excluded_matches_rows_removed() -> None:
    """A report_only class where the verdict arm LOSES to native would flip
    condition 2 to fail if compared -- excluding it must produce verdicts
    IDENTICAL to the same rows with that class's rows removed entirely, not
    merely an extra 'skipped' entry (skips still fail nothing, but they are
    counted; report_only rows must not even be counted)."""
    base_rows = [
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="single_hop",
                answer=f"policy is {OTHER_ANSWER_MARKER_2}",
                transcript=_recall_transcript(OTHER_UID_2),
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID_2,
                probe_class="single_hop",
                answer=f"policy is {OTHER_ANSWER_MARKER_2}",
                transcript=_native_transcript(OTHER_UID_2),
            )
        ),
    ]
    report_only_rows = [
        _row(
            _record(
                arm=VERDICT_ARM,
                probe_id=OTHER_PROBE_ID,
                probe_class="unprompted_push",
                answer="no idea",
            )
        ),
        _row(
            _record(
                arm=Arm.NATIVE_INDEX,
                probe_id=OTHER_PROBE_ID,
                probe_class="unprompted_push",
                answer=f"25 days ({OTHER_ANSWER_MARKER})",
            )
        ),
    ]

    verdicts_with = compute_verdicts(
        base_rows + report_only_rows,
        relationship_probe_ids=frozenset(),
        report_only_classes=frozenset({"unprompted_push"}),
    )
    verdicts_without = compute_verdicts(
        base_rows,
        relationship_probe_ids=frozenset(),
        report_only_classes=frozenset(),
    )
    assert verdicts_with == verdicts_without
    assert verdicts_with[0].condition2_pass is True
    assert "compared 1 classes, skipped 0" in verdicts_with[0].condition2_detail
    assert "unprompted_push" not in verdicts_with[0].condition2_detail
    assert "unprompted_push" not in verdicts_with[0].condition3_detail


def test_render_decision_block_prints_report_only_classes_excluded_line_empty_today() -> None:
    """issue athenaeum#1776: the decision block always names the excluded
    report_only classes -- 'empty today' (still true after athenaeum#1780)
    because ``_minimal_report()`` has zero rows, so ``_report_only_probe_classes``
    never examines any scale's corpus at all -- a different reason than
    "every class is enrolled" now that ``aggregation`` exists, but the same
    observable line."""
    rendered = "\n".join(render_decision_block(_minimal_report(), verdicts=[]))
    assert "report-only classes excluded: (none)" in rendered


def test_render_report_decision_block_pinned_with_report_only_line_added() -> None:
    """Pins the existing run-4-style decision block (real core corpus,
    single_hop probes, condition 1 + condition 2 paths both exercised) --
    every pre-existing assertion from
    ``test_build_report_and_render_report_thread_a_non_default_verdict_arm``
    still holds, plus the new report-only line, so athenaeum#1776 adds
    exactly one line to this report and changes nothing else. That line's
    class list grew from '(none)' to 'aggregation' once athenaeum#1780
    landed the corpus's first report_only-by-default class, to
    'aggregation, contradiction, negative_knowledge' once athenaeum#1781
    (item G) landed the real core corpus's other two report_only classes,
    and to 'aggregation, contradiction, negative_knowledge, unprompted_push'
    once athenaeum#1778 landed the real core corpus's fourth report_only
    class, read straight from the corpus by ``_report_only_probe_classes``
    regardless of which probe classes appear in *this* test's own rows."""
    rows = [
        _row(
            _record(
                arm=Arm.PULL,
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
                answer=f"terms are {RELATIONSHIP_ANSWER_MARKER}",
                transcript=_native_transcript(RELATIONSHIP_UID),
            )
        ),
    ]
    report = build_report(rows, verdict_arm="pull")
    rendered = render_report(report)
    assert "**Verdict arm:** `pull`" in rendered
    assert "'pull'" in rendered
    assert (
        "report-only classes excluded: aggregation, contradiction, negative_knowledge, "
        "unprompted_push" in rendered
    )
    decision_idx = rendered.index("## Decision")
    report_only_idx = rendered.index("report-only classes excluded:")
    arms_idx = rendered.index("## Arms in this report")
    assert decision_idx < report_only_idx < arms_idx
