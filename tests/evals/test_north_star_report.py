# SPDX-License-Identifier: Apache-2.0
"""Fixture-tests for the north-star report's shape (issue athenaeum#1523).

Mirrors issue athenaeum#1333's own discharge shape for exactly this
situation ("the report document is written under ``measurements/`` ... and
its shape is fixture-tested"): this module renders a full report from
SYNTHETIC rollout rows (built by hand, grounded in the real ``core`` corpus
so the target-page/query-quality machinery exercises real text) and asserts
its structure — no live rollout, no model client, no spend.

``rollout``-marked and fully offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.evals.containment import GridCell, ResultStore
from tests.evals.corpus import build_corpus
from tests.evals.north_star_report import (
    GroupStats,
    NorthStarReport,
    RolloutRow,
    _push_delivered_text,
    append_rollout_row,
    build_report,
    compute_group_stats,
    crossover_scales,
    delivered_text_for_utilization,
    delivered_uids_for_utilization,
    distinctive_ngram_overlap,
    grade_correctness,
    lexical_overlap,
    load_rollout_rows,
    render_report,
    uid_citation_rate,
    weak_probes,
    write_report,
)
from tests.evals.rollout import Arm, RolloutRecord, ToolCall, TurnTokenUsage

pytestmark = pytest.mark.rollout

# The real "core" corpus's pto_allowance probe/page, used verbatim so the
# text-metric machinery (lexical overlap, n-gram overlap, uid extraction)
# exercises real content instead of a hand-invented stand-in.
PROBE_ID = "pto_allowance"
CORPUS_SCALE = "core"
TARGET_UID = "policy-pto"
TARGET_BODY = (
    "The firm's PTO allowance is 25 days per year plus UK bank holidays. Up to "
    "five days may be carried into the following year; the rest lapse."
)

# Real corpus, loaded once, used by the correctness-grading fixtures below
# (issue athenaeum#1573) -- probe ids and planted answer_tokens are read off
# it rather than duplicated as literals, so a corpus edit cannot silently
# desync these tests from the ground truth they claim to grade.
_CORPUS = build_corpus(scale=CORPUS_SCALE)


def _probe(probe_id: str):
    for probe in _CORPUS.probes:
        if probe.id == probe_id:
            return probe
    raise KeyError(probe_id)


PUSH_DELIVERED = (
    "PTO policy (score: 9.5)\n"
    "**Path:** wiki/policy-pto.md\n"
    "**Tags:** policy, people\n"
    "**Uid:** policy-pto\n"
    "**Type:** principle\n"
    "\n"
    f"{TARGET_BODY}\n"
)


def _push_record(*, answer: str, injected_tokens: int = 60) -> RolloutRecord:
    return RolloutRecord(
        arm=Arm.PUSH_PAGES_UPPER_BOUND,
        probe_id=PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=200, output_tokens=30)],
        injected_context_tokens=injected_tokens,
        turn_count=1,
        transcript=[{"pushed_context": PUSH_DELIVERED, "answer": answer}],
    )


def _pull_record(*, called: bool, answer: str = "I don't know.") -> RolloutRecord:
    if not called:
        return RolloutRecord(
            arm=Arm.PULL,
            probe_id=PROBE_ID,
            probe_class="single_hop",
            corpus_scale=CORPUS_SCALE,
            answer=answer,
            turn_tokens=[TurnTokenUsage(turn=1, input_tokens=50, output_tokens=10)],
            recall_called=False,
            turn_count=1,
            transcript=[{"type": "assistant", "message": {"content": []}}],
        )
    transcript = [
        {
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 60, "output_tokens": 5},
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp__athenaeum__recall",
                        "input": {"query": "PTO allowance days per year"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": PUSH_DELIVERED}],
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 90, "output_tokens": 25},
                "content": [{"type": "text", "text": answer}],
            },
        },
    ]
    return RolloutRecord(
        arm=Arm.PULL,
        probe_id=PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[
            TurnTokenUsage(turn=1, input_tokens=60, output_tokens=5),
            TurnTokenUsage(turn=2, input_tokens=90, output_tokens=25),
        ],
        tool_calls=[
            ToolCall(name="mcp__athenaeum__recall", query="PTO allowance days per year")
        ],
        recall_called=True,
        turn_count=2,
        transcript=transcript,
    )


def _none_record() -> RolloutRecord:
    return RolloutRecord(
        arm=Arm.NONE,
        probe_id=PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer="I don't have that information.",
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=15, output_tokens=8)],
        turn_count=1,
        transcript=[{"answer": "I don't have that information."}],
    )


def _oracle_record(*, answer: str) -> RolloutRecord:
    return RolloutRecord(
        arm=Arm.ORACLE,
        probe_id=PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=180, output_tokens=28)],
        injected_context_tokens=90,
        turn_count=1,
        transcript=[{"oracle_context": "irrelevant for this test", "answer": answer}],
    )


# Issue athenaeum#1574: the breadcrumb payload shape the SHIPPED hook
# actually renders -- ``name — description`` bullets, NO uid marker at all
# (contrast ``PUSH_DELIVERED`` above, which has ``**Uid:**``). Fixture
# records below are grounded in this shape so AC4's "waste/utilization
# recomputed against the breadcrumb payload, or n/a -- never near-zero"
# tests exercise the real asymmetry, not an invented one.
BREADCRUMB_DELIVERED = (
    "[Knowledge context] Wiki pages relevant to this message "
    "(use `recall` MCP tool for full details):\n"
    f"  - PTO policy — {TARGET_BODY}\n"
)


def _push_breadcrumb_record(*, answer: str, injected_tokens: int = 12) -> RolloutRecord:
    return RolloutRecord(
        arm=Arm.PUSH_BREADCRUMB,
        probe_id=PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=70, output_tokens=15)],
        injected_context_tokens=injected_tokens,
        turn_count=1,
        transcript=[{"pushed_context": BREADCRUMB_DELIVERED, "answer": answer}],
    )


def _push_breadcrumb_pull_record(*, called: bool, answer: str = "I don't know.") -> RolloutRecord:
    """Mirrors ``_pull_record`` exactly, plus a leading breadcrumb entry --
    :func:`tests.evals.rollout.run_push_breadcrumb_pull` always records
    ``transcript[0] == {"pushed_context": ...}`` followed by the real
    stream-json events (see that function's own docstring)."""
    breadcrumb_entry = {"pushed_context": BREADCRUMB_DELIVERED}
    if not called:
        return RolloutRecord(
            arm=Arm.PUSH_BREADCRUMB_PULL,
            probe_id=PROBE_ID,
            probe_class="single_hop",
            corpus_scale=CORPUS_SCALE,
            answer=answer,
            turn_tokens=[TurnTokenUsage(turn=1, input_tokens=55, output_tokens=10)],
            recall_called=False,
            injected_context_tokens=12,
            turn_count=1,
            transcript=[breadcrumb_entry, {"type": "assistant", "message": {"content": []}}],
        )
    pull_events = [
        {
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 60, "output_tokens": 5},
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp__athenaeum__recall",
                        "input": {"query": "PTO allowance days per year"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": PUSH_DELIVERED}],
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 90, "output_tokens": 25},
                "content": [{"type": "text", "text": answer}],
            },
        },
    ]
    return RolloutRecord(
        arm=Arm.PUSH_BREADCRUMB_PULL,
        probe_id=PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[
            TurnTokenUsage(turn=1, input_tokens=60, output_tokens=5),
            TurnTokenUsage(turn=2, input_tokens=90, output_tokens=25),
        ],
        tool_calls=[
            ToolCall(name="mcp__athenaeum__recall", query="PTO allowance days per year")
        ],
        recall_called=True,
        injected_context_tokens=12,
        turn_count=2,
        transcript=[breadcrumb_entry, *pull_events],
    )


def _row(record: RolloutRecord, *, replicate: int = 0) -> RolloutRow:
    cell = GridCell(
        probe=record.probe_id, arm=record.arm.value, corpus_scale=record.corpus_scale,
        replicate=replicate,
    )
    return RolloutRow(cell=cell, record=record)


# ---------------------------------------------------------------------------
# Text metrics
# ---------------------------------------------------------------------------


def test_lexical_overlap_formula_is_jaccard_over_content_terms() -> None:
    assert lexical_overlap("PTO allowance days", "the PTO allowance is 25 days") > 0.0
    # Disjoint vocabulary -> 0.0, never a crash.
    assert lexical_overlap("invoice cadence", "PTO allowance") == 0.0


def test_lexical_overlap_empty_input_is_zero_not_a_crash() -> None:
    assert lexical_overlap("", "anything") == 0.0
    assert lexical_overlap("anything", "") == 0.0
    assert lexical_overlap("", "") == 0.0


def test_lexical_overlap_perfect_match_is_one() -> None:
    assert lexical_overlap("PTO allowance policy", "PTO allowance policy") == pytest.approx(1.0)


def test_distinctive_ngram_overlap_denominator_is_delivered_side() -> None:
    delivered = "the firm's PTO allowance is 25 days per year plus UK bank holidays"
    answer_echoes_fully = delivered
    answer_echoes_nothing = "completely unrelated text about invoice cadence timing"
    assert distinctive_ngram_overlap(delivered, answer_echoes_fully) == pytest.approx(1.0)
    assert distinctive_ngram_overlap(delivered, answer_echoes_nothing) == 0.0


def test_distinctive_ngram_overlap_short_delivered_text_is_zero() -> None:
    assert distinctive_ngram_overlap("two words", "two words") == 0.0


def test_uid_citation_rate_none_when_nothing_delivered() -> None:
    record = _none_record()
    assert uid_citation_rate(record, ()) is None


def test_uid_citation_rate_computed_when_uid_literal_present() -> None:
    record = _push_record(answer=f"The PTO allowance is 25 days ({TARGET_UID}).")
    assert uid_citation_rate(record, (TARGET_UID,)) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Delivered-content extraction
# ---------------------------------------------------------------------------


def test_delivered_uids_push_parses_bold_uid_marker() -> None:
    row = _row(_push_record(answer="whatever"))
    assert tuple(delivered_uids_for_utilization(row)) == (TARGET_UID,)


def test_delivered_uids_oracle_uses_expected_uids_directly() -> None:
    row = _row(_oracle_record(answer="whatever"))
    assert delivered_uids_for_utilization(row) == (TARGET_UID,)


def test_delivered_uids_pull_parses_tool_result_when_called() -> None:
    row = _row(_pull_record(called=True, answer="25 days per year"))
    uids = delivered_uids_for_utilization(row)
    assert TARGET_UID in uids


def test_delivered_uids_pull_empty_when_not_called() -> None:
    row = _row(_pull_record(called=False))
    assert delivered_uids_for_utilization(row) == ()


def test_delivered_text_none_arm_is_empty() -> None:
    row = _row(_none_record())
    assert delivered_text_for_utilization(row) == ""


# -- issue athenaeum#1574 AC4: waste/utilization against the breadcrumb ----
# payload, or explicitly n/a -- never a silent near-zero.


def test_delivered_text_push_breadcrumb_uses_the_real_breadcrumb_payload() -> None:
    """Recomputed against what was ACTUALLY delivered (a few short
    bullets), not against a five-page basis the arm never received."""
    row = _row(_push_breadcrumb_record(answer="whatever"))
    assert delivered_text_for_utilization(row) == BREADCRUMB_DELIVERED


def test_delivered_uids_push_breadcrumb_is_structurally_not_applicable() -> None:
    """The shipped hook's bullet is ``name`` or ``name — description`` --
    no uid marker at all. There is no textual basis to recover delivered
    uids, so this must be the empty tuple (read downstream as n/a), never
    an attempt that happens to return zero matches."""
    row = _row(_push_breadcrumb_record(answer="The PTO allowance is 25 days."))
    assert delivered_uids_for_utilization(row) == ()


def test_uid_citation_rate_push_breadcrumb_is_none_not_a_silent_zero() -> None:
    row = _row(_push_breadcrumb_record(answer="The PTO allowance is 25 days."))
    delivered_uids = delivered_uids_for_utilization(row)
    assert uid_citation_rate(row.record, delivered_uids) is None


def test_delivered_uids_push_breadcrumb_pull_uses_the_pulled_portion_only() -> None:
    """When PUSH_BREADCRUMB_PULL actually calls recall, the PULLED content
    carries real uid markers (the same ``recall_search`` rendering PULL
    gets) even though the injected breadcrumb itself carries none."""
    row = _row(_push_breadcrumb_pull_record(called=True, answer="25 days per year"))
    uids = delivered_uids_for_utilization(row)
    assert TARGET_UID in uids


def test_delivered_uids_push_breadcrumb_pull_empty_when_not_called() -> None:
    row = _row(_push_breadcrumb_pull_record(called=False))
    assert delivered_uids_for_utilization(row) == ()


def test_delivered_text_push_breadcrumb_pull_combines_breadcrumb_and_pulled_text() -> None:
    """Recomputed against the FULL delivered basis -- the injected
    breadcrumb AND whatever the tool call additionally returned -- so
    n-gram utilization credits either source the answer might have drawn
    on."""
    row = _row(_push_breadcrumb_pull_record(called=True, answer="25 days per year"))
    text = delivered_text_for_utilization(row)
    assert "Wiki pages relevant" in text  # the breadcrumb portion
    assert TARGET_BODY in text  # the pulled-page portion


def test_delivered_text_push_breadcrumb_pull_is_breadcrumb_only_when_not_called() -> None:
    row = _row(_push_breadcrumb_pull_record(called=False))
    assert delivered_text_for_utilization(row) == BREADCRUMB_DELIVERED


# ---------------------------------------------------------------------------
# Correctness grading (issue athenaeum#1573) -- floor/ceiling ground truth.
# Each fixture below targets ONE acceptance criterion with its own positive
# AND negative control; none of these collapse into a single happy-path
# assertion.
# ---------------------------------------------------------------------------


def _record(*, arm: Arm, probe_id: str, probe_class: str, answer: str) -> RolloutRecord:
    return RolloutRecord(
        arm=arm,
        probe_id=probe_id,
        probe_class=probe_class,
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=10, output_tokens=10)],
        turn_count=1,
    )


def test_correctness_grades_incorrect_on_distractor_token_without_probes_own_token() -> None:
    """AC2: an answer containing a DIFFERENT probe's planted token -- a
    distractor page's own ground truth, not this probe's -- must not grade
    correct just because it contains *some* recognized token."""
    pto_probe = _probe("pto_allowance")  # token: Cinderquill, on policy-pto
    other_probe = _probe("confidentiality_rule")  # token: Harrowvex, on policy-confidentiality
    assert pto_probe.answer_tokens and other_probe.answer_tokens
    assert pto_probe.answer_tokens != other_probe.answer_tokens

    distractor_token = other_probe.answer_tokens[0]
    wrong_answer = _record(
        arm=Arm.NONE,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"The PTO allowance is documented under {distractor_token}.",
    )
    assert grade_correctness(wrong_answer, pto_probe, _CORPUS) is False

    # Positive control: the SAME shape of answer, but carrying the probe's
    # own token, grades correct -- proves the miss above is about which
    # token is present, not some unrelated reason (e.g. answer length).
    right_token = pto_probe.answer_tokens[0]
    right_answer = _record(
        arm=Arm.ORACLE,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"The PTO allowance is documented under {right_token}.",
    )
    assert grade_correctness(right_answer, pto_probe, _CORPUS) is True


def test_follow_through_grading_requires_every_planted_token() -> None:
    """AC3 (issue athenaeum#1737): ``follow_through`` probes plant a token on
    EACH of at least two pages -- an answer that surfaced the breadcrumb page
    but never followed the edge to the second-hop page carries only one of
    them, and must grade incorrect, never a partial credit."""
    probe = _probe("fenwick_relationship_history")
    assert len(probe.answer_tokens) >= 2

    one_token_answer = _record(
        arm=Arm.PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=f"Fenwick Systems' relationship is coordinated per {probe.answer_tokens[0]}.",
    )
    assert grade_correctness(one_token_answer, probe, _CORPUS) is False

    all_tokens_answer = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=(
            f"Fenwick Systems' relationship is coordinated per {probe.answer_tokens[0]}, "
            f"and renegotiation timing is tracked per {probe.answer_tokens[1]}."
        ),
    )
    assert grade_correctness(all_tokens_answer, probe, _CORPUS) is True


def test_weak_probes_lists_probe_the_none_arm_already_answers_correctly() -> None:
    """AC4: a probe the NONE arm (no context delivered) answers correctly is
    a floor-leak signal and must be named in the weak-probe list."""
    pto_probe = _probe("pto_allowance")
    leaking_token = pto_probe.answer_tokens[0]
    leaky_none_row = _row(
        _record(
            arm=Arm.NONE,
            probe_id=pto_probe.id,
            probe_class=pto_probe.probe_class,
            answer=f"It's 25 days, code {leaking_token}.",
        )
    )

    # Negative control: a different NONE-arm probe whose answer does NOT
    # carry its own token must NOT be listed as weak.
    confidentiality_probe = _probe("confidentiality_rule")
    honest_none_row = _row(
        _record(
            arm=Arm.NONE,
            probe_id=confidentiality_probe.id,
            probe_class=confidentiality_probe.probe_class,
            answer="I don't have that information.",
        )
    )

    weak = weak_probes([leaky_none_row, honest_none_row])
    assert weak == (pto_probe.id,)


def test_weak_probes_never_lists_an_abstention_probe_the_none_arm_got_right() -> None:
    """A correctly-abstaining NONE arm is the expected null result, not a
    floor leak. Without this exclusion every abstention probe would be listed
    as weak on every run, drowning the signal the list exists to carry.

    Raised by Seer review on PR athenaeum#1670.
    """
    abstention_probe = _probe("abstain_unknown_client")
    assert abstention_probe.probe_class == "abstention"

    # This answer grades CORRECT for an abstention probe (asserts no planted
    # token, uses declining language) -- so only the probe_class exclusion
    # keeps it out of the weak list.
    abstaining_none_row = _row(
        _record(
            arm=Arm.NONE,
            probe_id=abstention_probe.id,
            probe_class=abstention_probe.probe_class,
            answer="I don't have that information; it is not in the corpus.",
        )
    )
    assert grade_correctness(abstaining_none_row.record, abstention_probe, _CORPUS) is True

    # Positive control in the same run: a genuine floor leak IS still listed.
    pto_probe2 = _probe("pto_allowance")
    leaky_row = _row(
        _record(
            arm=Arm.NONE,
            probe_id=pto_probe2.id,
            probe_class=pto_probe2.probe_class,
            answer=f"It's 25 days, code {pto_probe2.answer_tokens[0]}.",
        )
    )

    assert weak_probes([abstaining_none_row, leaky_row]) == (pto_probe2.id,)


def test_abstention_grades_correct_only_when_no_token_is_asserted() -> None:
    """AC5: an abstention probe grades correct only when the answer asserts
    NONE of the corpus's planted tokens and uses declining language -- one
    fixture that asserts (confabulates another probe's token) and one that
    genuinely abstains."""
    abstention_probe = _probe("abstain_unknown_client")
    assert abstention_probe.probe_class == "abstention"
    assert abstention_probe.answer_tokens == ()

    confabulated_token = _probe("pto_allowance").answer_tokens[0]
    asserting_answer = _record(
        arm=Arm.NONE,
        probe_id=abstention_probe.id,
        probe_class=abstention_probe.probe_class,
        answer=f"Harrowgate Industrial's payment terms are set under {confabulated_token}.",
    )
    assert grade_correctness(asserting_answer, abstention_probe, _CORPUS) is False

    abstaining_answer = _record(
        arm=Arm.NONE,
        probe_id=abstention_probe.id,
        probe_class=abstention_probe.probe_class,
        answer="I don't know -- Harrowgate Industrial is not in the corpus.",
    )
    assert grade_correctness(abstaining_answer, abstention_probe, _CORPUS) is True


def test_correctness_rate_rendered_per_group() -> None:
    """AC3 (render half): the report's markdown carries a correctness
    section broken out per (probe_class, corpus_scale, arm), the same shape
    as every other dimension."""
    pto_probe = _probe("pto_allowance")
    token = pto_probe.answer_tokens[0]
    rows = [
        _row(
            _record(
                arm=Arm.NONE,
                probe_id=pto_probe.id,
                probe_class=pto_probe.probe_class,
                answer="I don't know.",
            )
        ),
        _row(
            _record(
                arm=Arm.ORACLE,
                probe_id=pto_probe.id,
                probe_class=pto_probe.probe_class,
                answer=f"25 days, per {token}.",
            )
        ),
    ]
    report = build_report(rows)
    text = render_report(report)
    assert "## Correctness" in text
    assert "correctness_rate" in text
    stats_by_arm = {s.arm: s for s in report.stats}
    assert stats_by_arm["none"].correctness_rate == pytest.approx(0.0)
    assert stats_by_arm["oracle"].correctness_rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Persistence bridge round-trip through ResultStore
# ---------------------------------------------------------------------------


def test_append_and_load_round_trips_through_result_store(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    records = [
        _none_record(),
        _push_record(answer="25 days"),
        _oracle_record(answer="25 days"),
        _pull_record(called=True, answer="25 days"),
    ]
    for record in records:
        cell = GridCell(
            probe=record.probe_id, arm=record.arm.value, corpus_scale=record.corpus_scale,
            replicate=0,
        )
        append_rollout_row(store, cell, record)

    rows = load_rollout_rows(store)
    assert len(rows) == 4
    assert {row.record.arm for row in rows} == {
        Arm.NONE, Arm.PUSH_PAGES_UPPER_BOUND, Arm.ORACLE, Arm.PULL,
    }
    for row, original in zip(rows, records):
        assert row.record == original
        assert row.cell.replicate == 0


def test_load_rollout_rows_resumes_a_pre_1574_row_with_the_legacy_push_arm_value(
    tmp_path: Path,
) -> None:
    """Issue athenaeum#1574 AC5: a result-store row APPENDED BEFORE this
    issue renamed the arm persisted the raw JSON string ``"arm": "push"``
    (the exact value ``RolloutRecord.to_payload()`` wrote pre-rename).
    Written directly here — not through the current ``append_rollout_row``,
    which would write the NEW value and prove nothing about resuming an OLD
    one — to simulate exactly that on-disk row. The read path
    (``load_rollout_rows``) and everything a resumed run does downstream of
    it (group stats, report rendering) must still work against it, not
    raise ``ValueError``.
    """
    store = ResultStore(tmp_path / "results.jsonl")
    legacy_record = _push_record(answer="25 days")
    legacy_payload = {**legacy_record.to_payload(), "arm": "push", "replicate": 0}
    store.append("legacy-push-cell", legacy_payload)

    rows = load_rollout_rows(store)

    assert len(rows) == 1
    [row] = rows
    # The record resolves through Arm's legacy alias to the renamed member.
    assert row.record.arm is Arm.PUSH_PAGES_UPPER_BOUND
    # The GridCell carries the raw on-disk string verbatim (GridCell.arm is
    # a plain str, no enum coercion) -- both shapes must coexist cleanly.
    assert row.cell.arm == "push"

    # Downstream consumers must not choke on a resumed legacy row either --
    # group stats and the rendered report are exactly what a resumed run
    # would compute, and they key off the RECORD's (corrected) arm value.
    stats = compute_group_stats(rows)
    assert stats[0].arm == Arm.PUSH_PAGES_UPPER_BOUND.value
    report = build_report(rows)
    text = render_report(report)
    assert "push_pages_upper_bound" in text


def test_load_rollout_rows_empty_store_is_empty_list(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "nonexistent.jsonl")
    assert load_rollout_rows(store) == []


# ---------------------------------------------------------------------------
# Grouping never collapses probe_class x corpus_scale
# ---------------------------------------------------------------------------


def test_compute_group_stats_breaks_out_by_class_and_scale_and_arm() -> None:
    rows = [
        _row(_none_record()),
        _row(_push_record(answer=f"25 days ({TARGET_UID})")),
        _row(_oracle_record(answer="25 days")),
        _row(_pull_record(called=True, answer=f"25 days ({TARGET_UID})")),
        _row(_pull_record(called=False)),
    ]
    stats = compute_group_stats(rows)
    keys = {(s.probe_class, s.corpus_scale, s.arm) for s in stats}
    assert keys == {
        ("single_hop", "core", "none"),
        ("single_hop", "core", "push_pages_upper_bound"),
        ("single_hop", "core", "oracle"),
        ("single_hop", "core", "pull"),
    }


def test_pull_no_call_rate_is_first_class_and_only_populated_for_pull() -> None:
    rows = [_row(_pull_record(called=True)), _row(_pull_record(called=False))]
    stats = compute_group_stats(rows)
    pull_stat = next(s for s in stats if s.arm == "pull")
    assert pull_stat.no_call_rate == pytest.approx(0.5)

    non_pull_rows = [_row(_none_record())]
    non_pull_stats = compute_group_stats(non_pull_rows)
    assert non_pull_stats[0].no_call_rate is None


def test_push_injected_tokens_counted_regardless_of_citation() -> None:
    """PUSH's injected_context_tokens must be populated even when the
    delivered page is never cited -- the "paid whether used or not"
    asymmetry the issue names."""
    uncited_answer = "I do not know the PTO policy."  # never mentions the uid or the figure
    row = _row(_push_record(answer=uncited_answer, injected_tokens=77))
    stats = compute_group_stats([row])
    assert stats[0].mean_injected_context_tokens == pytest.approx(77.0)
    # And it is still "wasted" -- the uid was delivered but never cited.
    assert stats[0].mean_wasted_page_fraction == pytest.approx(1.0)


def test_no_scalar_composite_field_exists_on_group_stats() -> None:
    """Frontier framing, never a composite: GroupStats must not carry a
    combined/weighted/score field (issue athenaeum#1523's explicit
    framing requirement)."""
    field_names = {f.name for f in GroupStats.__dataclass_fields__.values()}
    forbidden_substrings = ("composite", "weighted", "_score")
    for name in field_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in name, f"GroupStats.{name} looks like a composite score field"


# ---------------------------------------------------------------------------
# render_report shape
# ---------------------------------------------------------------------------


def _sample_report(*, aborted: bool = False, abort_reason: str = "") -> NorthStarReport:
    rows = [
        _row(_none_record()),
        _row(_push_record(answer=f"25 days ({TARGET_UID})")),
        _row(_oracle_record(answer="25 days")),
        _row(_pull_record(called=True, answer=f"25 days ({TARGET_UID})")),
        _row(_pull_record(called=False)),
    ]
    return build_report(rows, aborted=aborted, abort_reason=abort_reason)


def test_render_report_leads_with_pull_no_call_rate() -> None:
    text = render_report(_sample_report())
    no_call_idx = text.index("PULL no-call rate")
    later_sections = ("Query quality", "## Cost", "## Efficiency", "## Waste", "## Utilization")
    for later_section in later_sections:
        assert no_call_idx < text.index(later_section)


def test_render_report_breaks_out_probe_class_and_corpus_scale_columns() -> None:
    text = render_report(_sample_report())
    assert "probe_class" in text
    assert "corpus_scale" in text


def test_render_report_states_overlap_formula_with_denominator() -> None:
    text = render_report(_sample_report())
    assert "lexical_overlap(a, b)" in text
    assert "denominator" in text.lower() or "union" in text.lower()


def test_render_report_never_emits_a_single_weighted_score() -> None:
    text = render_report(_sample_report())
    assert "composite" not in text.lower() or "never a" in text.lower()
    # No numeric field literally named score/weight in a table header.
    assert "| score |" not in text
    assert "| weighted_score |" not in text


def test_render_report_states_a_verdict_on_whether_free_data_settle_it() -> None:
    text = render_report(_sample_report())
    assert "Verdict" in text


def test_render_report_partial_banner_when_aborted() -> None:
    text = render_report(_sample_report(aborted=True, abort_reason="spend ceiling crossed"))
    assert text.startswith("> **PARTIAL RUN**")
    assert "spend ceiling crossed" in text


def test_render_report_no_partial_banner_when_not_aborted() -> None:
    text = render_report(_sample_report())
    assert not text.startswith("> **PARTIAL")


def test_render_report_includes_provenance_stamp() -> None:
    text = render_report(_sample_report())
    assert "athenaeum_version" in text
    assert "git_sha" in text
    assert "generated" in text
    assert "corpus_digest" in text


def test_render_report_labels_the_upper_bound_arm_and_lists_breadcrumb_arms_alongside_it() -> (
    None
):
    """Issue athenaeum#1574 AC3: the renamed page-level arm is explicitly
    labelled an upper bound, and the report surfaces the breadcrumb arms
    alongside it -- not a separate report, not silently dropped."""
    rows = [
        _row(_none_record()),
        _row(_push_record(answer=f"25 days ({TARGET_UID})")),
        _row(_push_breadcrumb_record(answer="25 days")),
        _row(_push_breadcrumb_pull_record(called=True, answer=f"25 days ({TARGET_UID})")),
        _row(_oracle_record(answer="25 days")),
        _row(_pull_record(called=True, answer=f"25 days ({TARGET_UID})")),
    ]
    text = render_report(build_report(rows))

    # The legend explicitly names the upper-bound reading.
    legend_idx = text.index("Arms in this report")
    assert "upper bound" in text.lower()
    assert legend_idx < text.index("## PULL no-call rate")

    # And the breadcrumb arms actually appear in the generic per-dimension
    # tables (Cost/Efficiency/Waste/Utilization/Correctness), not merely in
    # the legend prose.
    for table_heading in ("## Cost", "## Efficiency", "## Waste", "## Utilization"):
        section_start = text.index(table_heading)
        next_heading = text.index("## ", section_start + len(table_heading))
        section_text = text[section_start:next_heading]
        assert "push_pages_upper_bound" in section_text
        assert "push_breadcrumb" in section_text
        assert "push_breadcrumb_pull" in section_text


# ---------------------------------------------------------------------------
# write_report: dated filename, non-clobbering
# ---------------------------------------------------------------------------


def test_write_report_dated_filename(tmp_path: Path) -> None:
    report = _sample_report()
    path = write_report(report, out_dir=tmp_path)
    assert path.name == f"north-star-{report.generated[:10]}.md"
    assert path.read_text(encoding="utf-8") == render_report(report)


def test_write_report_non_clobbering_numeric_suffix(tmp_path: Path) -> None:
    report = _sample_report()
    first = write_report(report, out_dir=tmp_path)
    second = write_report(report, out_dir=tmp_path)
    third = write_report(report, out_dir=tmp_path)

    assert first != second != third
    assert second.name.endswith("-2.md")
    assert third.name.endswith("-3.md")
    # First report's own bytes are untouched by the later writes.
    assert first.read_text(encoding="utf-8") == render_report(report)


# ---------------------------------------------------------------------------
# No LLM judge is invoked anywhere on the report path
# ---------------------------------------------------------------------------


def test_no_model_client_constructed_on_report_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _exploding_build_llm_client(*args: Any, **kwargs: Any) -> MagicMock:
        raise AssertionError("the north-star report path must never construct a live client")

    monkeypatch.setattr("athenaeum.provider.build_llm_client", _exploding_build_llm_client)

    # issue athenaeum#1573: correctness grading (grade_correctness / weak_probes)
    # must be exercised here too, not just left passing incidentally -- a
    # weak-probe (leaked-token NONE answer) and an abstention row cover the
    # two NEW branches (non-abstention token match, abstention decline rule)
    # this guard did not exercise before this issue.
    pto_probe = _probe("pto_allowance")
    abstention_probe = _probe("abstain_unknown_client")
    leaking_token = pto_probe.answer_tokens[0]

    rows = [
        _row(_none_record()),
        _row(_push_record(answer=f"25 days ({TARGET_UID})")),
        _row(_oracle_record(answer="25 days")),
        _row(_pull_record(called=True, answer=f"25 days ({TARGET_UID})")),
        _row(
            _record(
                arm=Arm.NONE,
                probe_id=pto_probe.id,
                probe_class=pto_probe.probe_class,
                answer=f"It's 25 days, per {leaking_token}.",
            )
        ),
        _row(
            _record(
                arm=Arm.NONE,
                probe_id=abstention_probe.id,
                probe_class=abstention_probe.probe_class,
                answer="I don't know -- not in the corpus.",
            )
        ),
    ]
    report = build_report(rows)  # must not raise
    text = render_report(report)  # must not raise
    write_report(report, out_dir=tmp_path)  # must not raise

    # The new code path actually ran (not just skipped): at least one group's
    # correctness_rate is populated, and the leaked-token row surfaced as a
    # weak probe -- both computed without ever touching a live model client.
    assert any(s.correctness_rate is not None for s in report.stats)
    assert pto_probe.id in report.weak_probes
    assert "## Correctness" in text


# ---------------------------------------------------------------------------
# Native arms (issue athenaeum#1725) -- offline, no subprocess
# ---------------------------------------------------------------------------


def _native_index_record(
    *, answer: str = "25 days", coverage: float = 1.0, transcript_zero: dict | None = None
) -> RolloutRecord:
    """A NATIVE_INDEX row shaped exactly like
    ``tests.evals.rollout.run_native_index`` produces: ``transcript[0]`` is a
    leading ``{"native_memory": {...}}`` dict (the SAME idiom
    ``run_push_breadcrumb_pull`` uses for its own arm metadata), never a
    ``pushed_context`` key."""
    entry = transcript_zero or {
        "native_memory": {
            "pages": 99,
            "index_lines_written": 99,
            "index_bytes_written": 9000,
            "index_lines_loaded": 99,
            "index_bytes_loaded": 9000,
            "coverage": coverage,
            "truncated_by_claude_code": False,
        }
    }
    return RolloutRecord(
        arm=Arm.NATIVE_INDEX,
        probe_id=PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=100, output_tokens=20)],
        turn_count=1,
        transcript=[entry],
    )


def test_push_delivered_text_is_empty_for_a_native_transcript() -> None:
    """``_push_delivered_text`` reads ``transcript[0].get("pushed_context")``
    -- a native arm's leading dict carries no such key at all (it carries
    ``native_memory`` instead), so this must return ``""``, never raise and
    never accidentally pick up unrelated content."""
    record = _native_index_record()
    assert _push_delivered_text(record) == ""


def test_group_stats_mean_index_coverage_populated_only_for_native_index() -> None:
    rows = [
        _row(_native_index_record(coverage=0.4)),
        _row(_pull_record(called=False)),
    ]
    stats = compute_group_stats(rows)
    native_stat = next(s for s in stats if s.arm == "native_index")
    pull_stat = next(s for s in stats if s.arm == "pull")
    assert native_stat.mean_index_coverage == pytest.approx(0.4)
    assert pull_stat.mean_index_coverage is None


def _group_stat(
    *, probe_class: str, corpus_scale: str, arm: str, correctness: float | None
) -> GroupStats:
    return GroupStats(
        probe_class=probe_class,
        corpus_scale=corpus_scale,
        arm=arm,
        n=1,
        no_call_rate=None,
        mean_self_query_overlap=None,
        mean_topic_query_overlap=None,
        mean_input_tokens_per_turn=None,
        mean_output_tokens_per_turn=None,
        mean_injected_context_tokens=None,
        mean_turn_count=1.0,
        mean_tool_call_count=0.0,
        mean_wasted_page_fraction=None,
        mean_wasted_tokens_estimate=None,
        mean_uid_citation_rate=None,
        mean_distinctive_ngram_overlap=None,
        correctness_rate=correctness,
        mean_index_coverage=None,
    )


def test_crossover_scales_finds_the_smallest_crossing_scale() -> None:
    """Athenaeum starts BEHIND native at ``core`` and overtakes it at
    ``small`` -- the crossover must name ``small``, not ``core`` (where
    native still wins) and not some later scale (the smallest one that
    crosses is what the design doc's decision rule needs)."""
    stats = [
        _group_stat(probe_class="single_hop", corpus_scale="core", arm="pull", correctness=0.5),
        _group_stat(
            probe_class="single_hop", corpus_scale="core", arm="native_index", correctness=0.6
        ),
        _group_stat(probe_class="single_hop", corpus_scale="small", arm="pull", correctness=0.7),
        _group_stat(
            probe_class="single_hop", corpus_scale="small", arm="native_index", correctness=0.5
        ),
    ]
    assert crossover_scales(stats) == {"single_hop": "small"}


def test_crossover_scales_never_crosses_is_absent_not_fabricated() -> None:
    """A probe class where native always wins gets NO entry -- the caller
    renders ``n/a``, never a fabricated scale."""
    stats = [
        _group_stat(probe_class="multi_hop", corpus_scale="core", arm="pull", correctness=0.2),
        _group_stat(
            probe_class="multi_hop", corpus_scale="core", arm="native_index", correctness=0.9
        ),
        _group_stat(probe_class="multi_hop", corpus_scale="medium", arm="pull", correctness=0.3),
        _group_stat(
            probe_class="multi_hop", corpus_scale="medium", arm="native_grep", correctness=0.95
        ),
    ]
    assert crossover_scales(stats) == {}


def test_crossover_scales_excludes_floor_and_ceiling_arms() -> None:
    """``none`` (floor) and ``oracle`` (ceiling) must NEVER count as
    "Athenaeum's correctness" -- both massively outscore native here, but
    the only DELIVERY arm (``pull``) does not, so there must be NO crossover
    at ``core`` even though a naive scan including floor/ceiling would find
    one immediately."""
    stats = [
        _group_stat(probe_class="single_hop", corpus_scale="core", arm="none", correctness=0.95),
        _group_stat(probe_class="single_hop", corpus_scale="core", arm="oracle", correctness=1.0),
        _group_stat(probe_class="single_hop", corpus_scale="core", arm="pull", correctness=0.1),
        _group_stat(
            probe_class="single_hop", corpus_scale="core", arm="native_index", correctness=0.6
        ),
    ]
    assert crossover_scales(stats) == {}


def test_render_report_includes_index_coverage_and_crossover_sections() -> None:
    rows = [
        _row(_pull_record(called=True, answer=f"25 days ({TARGET_UID})")),
        _row(_native_index_record(answer="25 days", coverage=0.75)),
    ]
    report = build_report(rows)
    text = render_report(report)
    assert "## Index coverage (NATIVE_INDEX only" in text
    assert "## Crossover scale" in text
    assert "0.750" in text


# ---------------------------------------------------------------------------
# Mode per cell (issue athenaeum#1733) -- a render-only section over
# report.rows directly; must not require touching GroupStats/
# compute_group_stats/grade_correctness (reserved for sibling lanes).
# ---------------------------------------------------------------------------


def test_render_report_mode_per_cell_reflects_each_row_own_mode() -> None:
    api_record = RolloutRecord(
        arm=Arm.PULL,
        probe_id=PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer="25 days",
        mode="api",
    )
    cli_record = RolloutRecord(
        arm=Arm.NATIVE_GREP,
        probe_id=PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer="25 days",
        mode="cli",
    )
    rows = [_row(api_record), _row(cli_record)]
    report = build_report(rows)

    text = render_report(report)

    assert "## Mode per cell" in text
    mode_section = text[text.index("## Mode per cell") :]
    assert f"| {PROBE_ID} | pull | {CORPUS_SCALE} | 0 | api |" in mode_section
    assert f"| {PROBE_ID} | native_grep | {CORPUS_SCALE} | 0 | cli |" in mode_section
