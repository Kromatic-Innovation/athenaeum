# SPDX-License-Identifier: Apache-2.0
"""Fixture-tests for the north-star report's shape (issue athenaeum#1523).

Mirrors issue athenaeum#1333's own discharge shape for exactly this
situation ("the report document is written under ``measurements/`` ... and
its shape is fixture-tested"): this module renders a full report from
SYNTHETIC rollout rows (built by hand, grounded in the real ``core`` corpus
so the target-page/query-quality machinery exercises real text) and asserts
its structure — no live rollout, no model client, no spend.

Token-free: no live rollout, no model client, no spend. Not ``rollout``-
marked (issue athenaeum#1742) — runs in the default selection alongside
every other offline test under this directory.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import pytest

import tests.evals.north_star_report as nsr
from tests.evals.containment import GridCell, ResultStore
from tests.evals.corpus import Probe, build_corpus, deep_hop_uids
from tests.evals.north_star_report import (
    GRADER_REVISION,
    SIZE_SCALE_ORDER,
    GroupStats,
    NorthStarReport,
    RolloutRow,
    TurnCapCount,
    _all_answer_tokens,
    _delivered_uids,
    _is_abstention,
    _is_read_entity_tool,
    _push_delivered_text,
    _read_entity_delivered_uids,
    append_rollout_row,
    build_report,
    compute_group_stats,
    compute_verdicts,
    crossover_scales,
    deep_hop_delivered,
    delivered_text_for_utilization,
    delivered_uids_for_utilization,
    distinctive_ngram_overlap,
    grade_correctness,
    grade_coverage,
    grade_harm,
    lexical_overlap,
    load_rollout_rows,
    marker_miss_with_delivery,
    render_report,
    tag_followed,
    uid_citation_rate,
    weak_probes,
    write_report,
)
from tests.evals.rollout import Arm, RolloutRecord, ToolCall, TurnTokenUsage

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
# "hybrid: on|off" header line -- issue athenaeum#1816
# ---------------------------------------------------------------------------


def test_hybrid_line_absent_for_a_non_vector_report() -> None:
    """No vector row at all -- the question does not apply, so the line
    is omitted entirely rather than printing a value for a backend that
    was never dispatched."""
    record = dataclasses.replace(_none_record(), search_backend="fts5")
    text = render_report(build_report([_row(record)]))
    assert "hybrid:" not in text


def test_hybrid_line_on_for_an_active_vector_dispatch() -> None:
    record = dataclasses.replace(
        _none_record(), search_backend="vector", hybrid_active=True
    )
    text = render_report(build_report([_row(record)]))
    assert "- hybrid: on" in text


def test_hybrid_line_off_reproduces_the_pre_fix_measurement() -> None:
    """Issue athenaeum#1816's own bug: a vector dispatch that built only
    the vector index, so every recall call fell back to vector-only
    ranking. Header must surface that plainly rather than silently
    reading like a normal vector report."""
    record = dataclasses.replace(
        _none_record(), search_backend="vector", hybrid_active=False
    )
    text = render_report(build_report([_row(record)]))
    assert "- hybrid: off" in text


def test_hybrid_line_unknown_for_rows_predating_the_field() -> None:
    """Back-compat: a store persisted before issue athenaeum#1816 has
    ``hybrid_active=None`` on every row -- must read as "unknown", never
    silently coerced to "on" or "off"."""
    record = dataclasses.replace(
        _none_record(), search_backend="vector", hybrid_active=None
    )
    text = render_report(build_report([_row(record)]))
    assert "- hybrid: unknown" in text


# ---------------------------------------------------------------------------
# Harness failures + config isolation -- issue athenaeum#1819
# ---------------------------------------------------------------------------


def test_harness_failure_count_is_zero_when_nothing_failed() -> None:
    text = render_report(build_report([_row(_none_record())]))
    assert "- harness failures: 0" in text


def test_harness_failed_row_excluded_from_correctness_and_cost() -> None:
    """A row with ``harness_failure`` set must never be graded as an
    ordinary miss -- it is dropped from ``compute_group_stats``,
    ``weak_probes`` and the cost-per-correct table entirely, not merely
    scored as incorrect."""
    failed = dataclasses.replace(
        _oracle_record(answer="I don't have that information."),
        arm=Arm.PUSH_BREADCRUMB_PULL,
        mode="cli",
        harness_failure="final answer looks like an unresolved permission request",
    )
    report = build_report([_row(failed)])
    assert report.harness_failure_count == 1
    assert report.graded_rows == ()
    assert len(report.rows) == 1
    assert report.stats == ()
    text = render_report(report)
    assert "- harness failures: 1" in text


def test_harness_failed_row_stays_in_total_rollout_rows_and_mode_table() -> None:
    """The row itself is never discarded from the report -- only from
    grading -- so the header's total count and the mode-per-cell table
    still account for it."""
    failed = dataclasses.replace(
        _none_record(), mode="cli", harness_failure="empty pushed_context"
    )
    ok = _none_record()
    report = build_report([_row(failed), _row(ok)])
    assert len(report.rows) == 2
    assert len(report.graded_rows) == 1
    assert report.harness_failure_count == 1


def test_turn_cap_counts_empty_when_nothing_hit_the_cap() -> None:
    """issue athenaeum#1836: a run with zero turns_exhausted rows renders
    byte-identical to a pre-athenaeum#1836 report -- no turn_cap line at
    all, not a zero-count one."""
    report = build_report([_row(_none_record())])
    assert report.turn_cap_counts == ()
    text = render_report(report)
    assert "turn_cap" not in text


def test_turn_cap_counts_broken_out_per_arm_and_scale() -> None:
    """AC3: the report renders a per-arm, per-scale turn_cap count next to
    the harness-failure count. Two turn_cap rows on the same (arm, scale)
    group count together; a harness failure of a DIFFERENT kind on another
    row is excluded from this specific count."""
    turn_capped_1 = dataclasses.replace(
        _oracle_record(answer="Let me read the payment terms page to get more detail:"),
        arm=Arm.PUSH_BREADCRUMB_PULL,
        mode="api",
        harness_failure="turn_cap: api tool loop exhausted its turn budget",
        turns_exhausted=True,
    )
    turn_capped_2 = dataclasses.replace(turn_capped_1, probe_id="abstain_unknown_policy")
    other_failure = dataclasses.replace(
        _none_record(), mode="cli", harness_failure="empty pushed_context"
    )
    report = build_report([_row(turn_capped_1), _row(turn_capped_2), _row(other_failure)])

    assert report.harness_failure_count == 3
    assert report.turn_cap_counts == (
        TurnCapCount(arm=Arm.PUSH_BREADCRUMB_PULL.value, corpus_scale=CORPUS_SCALE, count=2),
    )
    text = render_report(report)
    assert (
        f"turn_cap[arm={Arm.PUSH_BREADCRUMB_PULL.value}, scale={CORPUS_SCALE}]: 2" in text
    )


def test_config_isolated_line_absent_for_an_api_only_report() -> None:
    record = dataclasses.replace(_none_record(), mode="api")
    text = render_report(build_report([_row(record)]))
    assert "config isolated:" not in text


def test_config_isolated_line_yes_when_every_cli_row_is_isolated() -> None:
    record = dataclasses.replace(_none_record(), mode="cli", config_isolated=True)
    text = render_report(build_report([_row(record)]))
    assert "- config isolated: yes" in text


def test_config_isolated_line_no_for_a_pre_1819_cli_row() -> None:
    """Back-compat: a store persisted before issue athenaeum#1819 decodes
    ``config_isolated=False`` on every row -- "not known to be isolated",
    never a false claim of isolation."""
    record = dataclasses.replace(_none_record(), mode="cli", config_isolated=False)
    text = render_report(build_report([_row(record)]))
    assert "- config isolated: no" in text


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


def _record(
    *,
    arm: Arm,
    probe_id: str,
    probe_class: str,
    answer: str,
    transcript: list[dict[str, Any]] | None = None,
) -> RolloutRecord:
    return RolloutRecord(
        arm=arm,
        probe_id=probe_id,
        probe_class=probe_class,
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=10, output_tokens=10)],
        turn_count=1,
        transcript=transcript if transcript is not None else [],
    )


def _recall_output_record(
    *, arm: Arm, probe_id: str, probe_class: str, answer: str, recall_text: str
) -> RolloutRecord:
    """A PULL/PUSH_BREADCRUMB_PULL-shaped record whose recall tool_result
    carries *recall_text* -- the same ``type: user`` / ``tool_result``
    transcript shape :func:`_pull_delivered_text` parses, minimal (no
    surrounding assistant turns) since grading only reads the tool output.
    """
    transcript = [
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": recall_text}],
                    }
                ]
            },
        }
    ]
    return _record(
        arm=arm, probe_id=probe_id, probe_class=probe_class, answer=answer, transcript=transcript
    )


def test_correctness_grades_incorrect_on_distractor_marker_without_probes_own_marker() -> None:
    """AC2 (issue athenaeum#1831): an answer containing a DIFFERENT probe's
    planted content marker -- a distractor page's own ground truth, not
    this probe's -- must not grade correct just because it contains *some*
    recognized marker."""
    pto_probe = _probe("pto_allowance")  # marker: "25 days per year", on policy-pto
    other_probe = _probe("confidentiality_rule")  # marker: "Nadia Frost", on policy-confidentiality
    assert pto_probe.answer_markers and other_probe.answer_markers
    assert pto_probe.answer_markers != other_probe.answer_markers

    _distractor_uid, distractor_marker = other_probe.answer_markers[0]
    wrong_answer = _record(
        arm=Arm.ORACLE,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"The PTO allowance is documented, see {distractor_marker}.",
    )
    assert grade_correctness(wrong_answer, pto_probe, _CORPUS) is False

    # Positive control: the SAME shape of answer, but carrying the probe's
    # own marker, grades correct -- proves the miss above is about which
    # marker is present, not some unrelated reason (e.g. answer length).
    _right_uid, right_marker = pto_probe.answer_markers[0]
    right_answer = _record(
        arm=Arm.ORACLE,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"The PTO allowance is {right_marker}.",
    )
    assert grade_correctness(right_answer, pto_probe, _CORPUS) is True


def test_follow_through_grading_requires_every_planted_marker() -> None:
    """AC3 (issue athenaeum#1831, athenaeum#1737): ``follow_through`` probes
    plant a marker on EACH of at least two pages -- an answer that carries
    only one of them must grade incorrect, never a partial credit."""
    probe = _probe("fenwick_relationship_history")
    assert len(probe.answer_markers) == 2
    (_uid1, marker1), (_uid2, marker2) = probe.answer_markers

    one_marker_answer = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=f"Fenwick Systems' relationship: {marker1}.",
    )
    assert grade_correctness(one_marker_answer, probe, _CORPUS) is False

    all_markers_answer = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=f"Fenwick Systems' relationship: {marker1}. Checkpoint: {marker2}.",
    )
    assert grade_correctness(all_markers_answer, probe, _CORPUS) is True


# ---------------------------------------------------------------------------
# Uid-citation correctness (issue athenaeum#1793, operator ruling option 1)
# ---------------------------------------------------------------------------

# **Uid:** marker text for both `fenwick_relationship_history` pages
# (`client-fenwick-systems` plants "Quillbrook", `person-dara-holt` plants
# "Marrowfen" -- tests/evals/data/corpus/probes/probes.yaml /
# tests/evals/data/corpus/core/10-follow-through.yaml), in the same
# ``recall_search`` rendering shape as ``PUSH_DELIVERED`` above.
FENWICK_UID = "client-fenwick-systems"
DARA_UID = "person-dara-holt"
_FENWICK_RECALL_TEXT = (
    "Fenwick Systems (score: 9.1)\n"
    "**Path:** wiki/client-fenwick-systems.md\n"
    f"**Uid:** {FENWICK_UID}\n"
    "**Type:** client\n"
)
_DARA_RECALL_TEXT = (
    "Dara Holt (score: 8.7)\n"
    "**Path:** wiki/person-dara-holt.md\n"
    f"**Uid:** {DARA_UID}\n"
    "**Type:** person\n"
)


def test_uid_citation_counts_as_correct_when_delivered_in_recall_output() -> None:
    """A uid in the probe's expected_uids, cited in the answer, AND present
    in the cell's OWN recall tool output grades correct -- the answer never
    mentions the planted tag at all."""
    pto_probe = _probe("pto_allowance")  # expected_uids: [policy-pto], token: Cinderquill
    uid_answer = _recall_output_record(
        arm=Arm.PULL,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer="The PTO allowance is 25 days per year; see policy-pto for the source page.",
        recall_text=PUSH_DELIVERED,  # carries **Uid:** policy-pto
    )
    assert grade_correctness(uid_answer, pto_probe, _CORPUS) is True


def test_uid_citation_grades_wrong_when_uid_not_in_the_cells_recall_output() -> None:
    """AC (negative test): the SAME uid citation, but the cell's own recall
    output never surfaced that page -- the model could only have guessed or
    leaked the uid, and must still grade wrong (the athenaeum#1753 leak
    guard is unchanged)."""
    pto_probe = _probe("pto_allowance")
    other_recall_text = (
        "Confidentiality policy (score: 7.0)\n"
        "**Path:** wiki/policy-confidentiality.md\n"
        "**Uid:** policy-confidentiality\n"
        "**Type:** policy\n"
    )
    uid_answer_not_delivered = _recall_output_record(
        arm=Arm.PULL,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer="The PTO allowance is 25 days per year; see policy-pto for the source page.",
        recall_text=other_recall_text,
    )
    assert grade_correctness(uid_answer_not_delivered, pto_probe, _CORPUS) is False


def test_uid_not_in_expected_uids_grades_wrong_even_if_delivered() -> None:
    """A uid the answer cites that is NOT one of the probe's expected_uids
    grades wrong, even though it really was delivered in the cell's own
    recall output -- citing the right kind of thing about the wrong page is
    not a correct answer to THIS probe."""
    pto_probe = _probe("pto_allowance")
    both_pages_recall_text = PUSH_DELIVERED + "\n" + (
        "Confidentiality policy (score: 7.0)\n"
        "**Path:** wiki/policy-confidentiality.md\n"
        "**Uid:** policy-confidentiality\n"
        "**Type:** policy\n"
    )
    wrong_uid_answer = _recall_output_record(
        arm=Arm.PULL,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer="See policy-confidentiality for the relevant policy.",
        recall_text=both_pages_recall_text,
    )
    assert grade_correctness(wrong_uid_answer, pto_probe, _CORPUS) is False


def test_tag_quoted_but_marker_absent_grades_wrong() -> None:
    """AC counter-example 3 (issue athenaeum#1831): the answer quotes the
    page's ``[ref: TAG]`` token but never states the content marker --
    correctness no longer credits the tag at all. ``_tag_followed`` still
    credits the citation, as the report-only diagnostic it now is."""
    pto_probe = _probe("pto_allowance")
    tag_answer = _record(
        arm=Arm.ORACLE,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"[ref: {pto_probe.answer_tokens[0]}]",
    )
    assert grade_correctness(tag_answer, pto_probe, _CORPUS) is False
    assert tag_followed(tag_answer, pto_probe) is True


def test_push_breadcrumb_grades_on_the_bullets_own_name_evidence() -> None:
    """PUSH_BREADCRUMB carries no uid marker at all (issue athenaeum#1574
    AC4), but its rendered bullet DOES name the page (``  - <name> --
    <description>``) -- issue athenaeum#1831's ``_breadcrumb_delivered_uids``
    reads that as real, selective delivery evidence (top-3 of the whole
    corpus), never content-only. Marker present AND the page named in the
    breadcrumb -> correct; marker present but the page NOT named (a
    different page's breadcrumb, or none at all) -> wrong, the same leak
    guard every other arm gets."""
    pto_probe = _probe("pto_allowance")
    _uid, marker = pto_probe.answer_markers[0]
    named_answer = _record(
        arm=Arm.PUSH_BREADCRUMB,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"The PTO allowance is {marker}.",
        transcript=[{"pushed_context": BREADCRUMB_DELIVERED}],  # names "PTO policy"
    )
    assert grade_correctness(named_answer, pto_probe, _CORPUS) is True

    unnamed_answer = _record(
        arm=Arm.PUSH_BREADCRUMB,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"The PTO allowance is {marker}.",
        transcript=[
            {"pushed_context": "  - Confidentiality policy — who may see client data\n"}
        ],
    )
    assert grade_correctness(unnamed_answer, pto_probe, _CORPUS) is False


def test_native_index_grades_on_a_topic_file_actually_read() -> None:
    """NATIVE_INDEX's loaded MEMORY.md index text is never used as delivery
    evidence (issue athenaeum#1831 -- at ``core`` scale it untruncatedly
    names every page, which would be a tautology, not evidence). What counts
    is a topic file the model's OWN ``read`` tool actually opened during the
    turn, recorded the same ``loaded_memory_files`` way NATIVE_GREP already
    is (see ``run_native_index``). Marker present AND the topic file read ->
    correct; marker present with nothing read -> wrong."""
    pto_probe = _probe("pto_allowance")
    _uid, marker = pto_probe.answer_markers[0]
    read_record = _record(
        arm=Arm.NATIVE_INDEX,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"The PTO allowance is {marker}.",
        transcript=[
            {
                "native_memory": {
                    "loaded_memory_files": {"/memory/policy-pto.md": "..."},
                }
            }
        ],
    )
    assert grade_correctness(read_record, pto_probe, _CORPUS) is True

    unread_record = _record(
        arm=Arm.NATIVE_INDEX,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"The PTO allowance is {marker}.",
        transcript=[{"native_memory": {"loaded_memory_files": {}}}],
    )
    assert grade_correctness(unread_record, pto_probe, _CORPUS) is False


def test_follow_through_markers_both_pages_delivered_grades_correct() -> None:
    """Each planted marker independently requires its OWN page to be
    delivered: both markers present, and both pages' uids in this cell's
    own recall output -- grades correct."""
    probe = _probe("fenwick_relationship_history")
    assert probe.expected_uids == (FENWICK_UID, DARA_UID)
    (_uid1, marker1), (_uid2, marker2) = probe.answer_markers

    both_delivered_answer = _recall_output_record(
        arm=Arm.PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=f"{marker1}. {marker2}.",
        recall_text=_FENWICK_RECALL_TEXT + "\n" + _DARA_RECALL_TEXT,
    )
    assert grade_correctness(both_delivered_answer, probe, _CORPUS) is True


def test_follow_through_markers_only_one_page_delivered_grades_wrong() -> None:
    """AC counter-example 4 (issue athenaeum#1831): a multi_hop/follow_through
    probe with BOTH markers present in the answer, but only ONE of the two
    expected pages' uids actually delivered -- must grade wrong."""
    probe = _probe("fenwick_relationship_history")
    (_uid1, marker1), (_uid2, marker2) = probe.answer_markers

    only_dara_delivered_answer = _recall_output_record(
        arm=Arm.PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=f"{marker1}. {marker2}.",
        recall_text=_DARA_RECALL_TEXT,  # only dara-holt delivered, not fenwick-systems
    )
    assert grade_correctness(only_dara_delivered_answer, probe, _CORPUS) is False


def test_athenaeum_1831_acceptance_criteria_counter_examples() -> None:
    """The four counter-example tests issue athenaeum#1831's acceptance
    criteria name explicitly, in one place for direct traceability. Each is
    ALSO covered by its own dedicated test elsewhere in this module (see
    each assertion's comment for the sibling test) -- this one exists so a
    reviewer can check the AC off against a single, self-contained test.

    1. correct marker, no tag, page delivered -> True
    2. correct marker, page NOT delivered (leaked/guessed) -> False
    3. tag quoted, marker absent -> False
    4. multi_hop probe: both markers present, only one page delivered -> False
    """
    pto_probe = _probe("pto_allowance")
    _pto_uid, pto_marker = pto_probe.answer_markers[0]
    tag = pto_probe.answer_tokens[0]

    # (1) -- see also test_correctness_no_longer_needs_the_tag_but_tag_followed_still_does
    # in test_reference_tag_contract.py.
    no_tag_delivered = _record(
        arm=Arm.ORACLE,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"The firm's PTO allowance is {pto_marker}.",
    )
    assert grade_correctness(no_tag_delivered, pto_probe, _CORPUS) is True

    # (2) -- correct marker, but PULL's own transcript delivered nothing for
    # this probe (no recall call at all): the leak guard denies credit.
    leaked_marker_undelivered = _record(
        arm=Arm.PULL,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"The firm's PTO allowance is {pto_marker}.",
    )
    assert grade_correctness(leaked_marker_undelivered, pto_probe, _CORPUS) is False

    # (3) -- see also test_tag_quoted_but_marker_absent_grades_wrong above.
    tag_only = _record(
        arm=Arm.ORACLE,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer=f"[ref: {tag}]",
    )
    assert grade_correctness(tag_only, pto_probe, _CORPUS) is False

    # (4) -- see also test_follow_through_markers_only_one_page_delivered_grades_wrong
    # above, which this mirrors using the same fenwick_relationship_history probe.
    fenwick_probe = _probe("fenwick_relationship_history")
    (_uid1, marker1), (_uid2, marker2) = fenwick_probe.answer_markers
    only_one_page_delivered = _recall_output_record(
        arm=Arm.PULL,
        probe_id=fenwick_probe.id,
        probe_class=fenwick_probe.probe_class,
        answer=f"{marker1}. {marker2}.",
        recall_text=_FENWICK_RECALL_TEXT,  # only client-fenwick-systems delivered
    )
    assert grade_correctness(only_one_page_delivered, fenwick_probe, _CORPUS) is False


def test_weak_probes_lists_probe_the_none_arm_already_answers_correctly() -> None:
    """AC4: a probe the NONE arm (no context delivered) answers correctly is
    a floor-leak signal and must be named in the weak-probe list."""
    pto_probe = _probe("pto_allowance")
    _leaking_uid, leaking_marker = pto_probe.answer_markers[0]
    leaky_none_row = _row(
        _record(
            arm=Arm.NONE,
            probe_id=pto_probe.id,
            probe_class=pto_probe.probe_class,
            answer=f"It's {leaking_marker}.",
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
    _uid2, marker2 = pto_probe2.answer_markers[0]
    leaky_row = _row(
        _record(
            arm=Arm.NONE,
            probe_id=pto_probe2.id,
            probe_class=pto_probe2.probe_class,
            answer=f"It's {marker2}.",
        )
    )

    assert weak_probes([abstaining_none_row, leaky_row]) == (pto_probe2.id,)


def test_abstention_grades_on_declining_language_not_on_token_absence() -> None:
    """AC5 as amended by athenaeum#1836: an abstention probe grades correct
    when the answer uses declining language, whether or not it also mentions
    another probe's planted token -- a confident assertion with no declining
    language grades wrong, a decline that cites a neighbouring true fact
    grades right (operator ruling on athenaeum#1791: right is right)."""
    abstention_probe = _probe("abstain_unknown_client")
    assert abstention_probe.probe_class == "abstention"
    assert abstention_probe.answer_tokens == ()

    neighbouring_token = _probe("pto_allowance").answer_tokens[0]
    asserting_answer = _record(
        arm=Arm.NONE,
        probe_id=abstention_probe.id,
        probe_class=abstention_probe.probe_class,
        answer=f"Harrowgate Industrial's payment terms are set under {neighbouring_token}.",
    )
    assert grade_correctness(asserting_answer, abstention_probe, _CORPUS) is False

    abstaining_answer = _record(
        arm=Arm.NONE,
        probe_id=abstention_probe.id,
        probe_class=abstention_probe.probe_class,
        answer="I don't know -- Harrowgate Industrial is not in the corpus.",
    )
    assert grade_correctness(abstaining_answer, abstention_probe, _CORPUS) is True

    declining_with_neighbour = _record(
        arm=Arm.NONE,
        probe_id=abstention_probe.id,
        probe_class=abstention_probe.probe_class,
        answer=(
            "I could not find Harrowgate Industrial in the knowledge base; the search "
            f"only returned the PTO policy page (reference tag {neighbouring_token})."
        ),
    )
    assert grade_correctness(declining_with_neighbour, abstention_probe, _CORPUS) is True


# ---------------------------------------------------------------------------
# issue athenaeum#1836: absence-phrase families + marker-leak guard
# ---------------------------------------------------------------------------


def test_is_abstention_accepts_no_formal_policy_documented_phrasing() -> None:
    """The exact real-world example from run 35399179014 (issue
    athenaeum#1836): a corpus distractor (the PTO page) sits adjacent in
    vocabulary to the absent parental-leave policy, so a correct abstention
    reads 'no formal policy documented' rather than the flat 'not found'
    phrases the old grader required."""
    probe = _probe("abstain_unknown_policy")
    answer = "The firm does not have a formalised parental leave policy documented."
    assert _is_abstention(answer, probe, _CORPUS) is True
    assert grade_correctness(_record(
        arm=Arm.NONE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=answer,
    ), probe, _CORPUS) is True


def test_is_abstention_accepts_a_decline_that_cites_a_neighbouring_true_fact() -> None:
    """The real medium-scale cells from run 35399179014: `pull` and
    `native_index` both declined the parental-leave question AND quoted the
    adjacent PTO fact they did find ('25 days per year'). A planted-marker
    deny-list graded both wrong; under the athenaeum#1791 ruling they are
    correct abstentions, so the marker must not fail them."""
    probe = _probe("abstain_unknown_policy")
    pto_probe = _probe("pto_allowance")
    _uid, marker = pto_probe.answer_markers[0]

    citing_answer = (
        "There is no formal parental leave policy documented; what is documented is "
        f"the PTO policy: {marker}."
    )
    assert _is_abstention(citing_answer, probe, _CORPUS) is True

    # The issue's literal counter-example: a confident, specific value with
    # no declining language fails on phrase grounds.
    assert _is_abstention("the parental leave policy is 12 weeks", probe, _CORPUS) is False


def test_is_abstention_rejects_a_tool_preamble() -> None:
    """The other real-world example from the same run: a turn-capped
    push_breadcrumb_pull cell's last-seen text was a tool preamble, never a
    declining answer -- must not be mistaken for a correct abstention."""
    probe = _probe("abstain_unknown_client")
    answer = "Let me read the payment terms review page to get more detail:"
    assert _is_abstention(answer, probe, _CORPUS) is False


def test_correctness_rate_rendered_per_group() -> None:
    """AC3 (render half): the report's markdown carries a correctness
    section broken out per (probe_class, corpus_scale, arm), the same shape
    as every other dimension."""
    pto_probe = _probe("pto_allowance")
    _uid, marker = pto_probe.answer_markers[0]
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
                answer=f"{marker}.",
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
    _leaking_uid, leaking_marker = pto_probe.answer_markers[0]

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
                answer=f"It's {leaking_marker}.",
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
        harm_free_rate=None,
        coverage_rate=None,
        marker_resolution_rate=None,
        mean_index_coverage=None,
        tag_followed_rate=None,
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
# Harm grading (issue athenaeum#1772) and coverage grading (issue
# athenaeum#1773) -- both report_only wave-2 mechanisms (athenaeum#1791
# §2.1): pure functions plus GroupStats/render_report plumbing, feeding no
# §7 condition. Neither probe class exists in the real corpus yet, so every
# fixture here builds a synthetic Probe with dataclasses.replace over a real
# one rather than waiting on a sibling lane's probe class.
# ---------------------------------------------------------------------------


def test_grade_harm_true_when_no_forbidden_token_present() -> None:
    pto_probe = _probe("pto_allowance")
    harm_probe = dataclasses.replace(pto_probe, forbidden_tokens=("Ghostword",))
    record = _record(
        arm=Arm.ORACLE,
        probe_id=harm_probe.id,
        probe_class=harm_probe.probe_class,
        answer="The PTO allowance is 25 days.",
    )
    assert grade_harm(record, harm_probe) is True


def test_grade_harm_false_when_forbidden_token_present() -> None:
    pto_probe = _probe("pto_allowance")
    harm_probe = dataclasses.replace(pto_probe, forbidden_tokens=("Ghostword",))
    record = _record(
        arm=Arm.ORACLE,
        probe_id=harm_probe.id,
        probe_class=harm_probe.probe_class,
        answer="The policy is governed by Ghostword.",
    )
    assert grade_harm(record, harm_probe) is False


def test_grade_harm_is_case_insensitive_like_grade_correctness() -> None:
    """Same normalizer as grade_correctness (_normalize_for_match) -- a
    forbidden token present under different casing must still be caught."""
    pto_probe = _probe("pto_allowance")
    harm_probe = dataclasses.replace(pto_probe, forbidden_tokens=("Ghostword",))
    record = _record(
        arm=Arm.ORACLE,
        probe_id=harm_probe.id,
        probe_class=harm_probe.probe_class,
        answer="The policy is governed by GHOSTWORD.",
    )
    assert grade_harm(record, harm_probe) is False


def test_grade_harm_none_when_probe_has_no_forbidden_tokens() -> None:
    """Empty forbidden_tokens grades None -- never False -- true for every
    probe class shipped so far (this issue ships the mechanism only)."""
    pto_probe = _probe("pto_allowance")
    assert pto_probe.forbidden_tokens == ()
    record = _record(
        arm=Arm.ORACLE,
        probe_id=pto_probe.id,
        probe_class=pto_probe.probe_class,
        answer="The PTO allowance is 25 days.",
    )
    assert grade_harm(record, pto_probe) is None


def test_all_answer_tokens_does_not_read_forbidden_tokens() -> None:
    """Isolation pin: _all_answer_tokens (grade_correctness's abstention
    deny-list) must never absorb forbidden_tokens -- the two mechanisms are
    unrelated, and a forbidden token leaking into the deny-list would make
    an unrelated abstention probe grade an answer as confabulation just
    because it named a harm-mechanism decoy token."""
    corpus = build_corpus(scale=CORPUS_SCALE)
    harm_only_token = "ZzyxwvutsrqponUNIQUE"
    assert not any(harm_only_token in probe.answer_tokens for probe in corpus.probes)
    patched_probes = [
        dataclasses.replace(p, forbidden_tokens=(harm_only_token,))
        if p.id == "pto_allowance"
        else p
        for p in corpus.probes
    ]
    patched_corpus = dataclasses.replace(corpus, probes=patched_probes)
    assert harm_only_token not in _all_answer_tokens(patched_corpus)


def test_grade_correctness_abstention_ignores_forbidden_tokens_of_other_probes() -> None:
    """Behavioural companion to the structural isolation pin above: an
    abstention record whose answer contains ANOTHER probe's forbidden_tokens
    value must still grade as a correct abstention through
    grade_correctness -- forbidden_tokens has no bearing on the
    confabulation deny-list grade_correctness actually reads
    (_all_answer_tokens), so naming a harm-mechanism decoy token is not
    confabulation."""
    abstention_probe = _probe("abstain_unknown_client")
    pto_probe = _probe("pto_allowance")
    harm_probe = dataclasses.replace(pto_probe, forbidden_tokens=("Ghostword",))
    corpus = build_corpus(scale=CORPUS_SCALE)
    patched_probes = [harm_probe if p.id == pto_probe.id else p for p in corpus.probes]
    patched_corpus = dataclasses.replace(corpus, probes=patched_probes)

    record = _record(
        arm=Arm.NONE,
        probe_id=abstention_probe.id,
        probe_class=abstention_probe.probe_class,
        answer="I don't know -- but note Ghostword just in case.",
    )
    assert grade_correctness(record, abstention_probe, patched_corpus) is True


def test_grade_coverage_full_when_every_answer_token_present() -> None:
    probe = _probe("fenwick_relationship_history")
    assert len(probe.answer_tokens) >= 2
    answer = " and ".join(probe.answer_tokens)
    record = _record(
        arm=Arm.ORACLE, probe_id=probe.id, probe_class=probe.probe_class, answer=answer
    )
    assert grade_coverage(record, probe) == pytest.approx(1.0)


def test_grade_coverage_partial_fraction_when_some_answer_tokens_present() -> None:
    probe = _probe("fenwick_relationship_history")
    assert len(probe.answer_tokens) >= 2
    record = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=f"Coordinated per {probe.answer_tokens[0]}.",
    )
    assert grade_coverage(record, probe) == pytest.approx(1.0 / len(probe.answer_tokens))


def test_grade_coverage_none_when_probe_has_no_answer_tokens() -> None:
    abstention_probe = _probe("abstain_unknown_client")
    assert abstention_probe.answer_tokens == ()
    record = _record(
        arm=Arm.NONE,
        probe_id=abstention_probe.id,
        probe_class=abstention_probe.probe_class,
        answer="I don't know.",
    )
    assert grade_coverage(record, abstention_probe) is None


def test_compute_group_stats_harm_free_rate_only_over_forbidden_token_rows(monkeypatch) -> None:
    """harm_free_rate is computed ONLY over rows whose probe carries
    forbidden_tokens -- a group with no such probe must read None, not a
    fabricated rate over ungraded rows."""
    import tests.evals.north_star_report as nsr

    pto_probe = _probe("pto_allowance")
    harm_probe = dataclasses.replace(pto_probe, forbidden_tokens=("Ghostword",))
    patched_probes = [harm_probe if p.id == pto_probe.id else p for p in _CORPUS.probes]
    patched_corpus = dataclasses.replace(_CORPUS, probes=patched_probes)
    monkeypatch.setitem(nsr._CORPUS_CACHE, CORPUS_SCALE, patched_corpus)

    safe_row = _row(
        _record(
            arm=Arm.ORACLE,
            probe_id=harm_probe.id,
            probe_class=harm_probe.probe_class,
            answer="25 days.",
        )
    )
    unsafe_row = _row(
        _record(
            arm=Arm.ORACLE,
            probe_id=harm_probe.id,
            probe_class=harm_probe.probe_class,
            answer="Governed by Ghostword.",
        ),
        replicate=1,
    )

    stats = compute_group_stats([safe_row, unsafe_row])

    stat = next(s for s in stats if s.arm == "oracle")
    assert stat.harm_free_rate == pytest.approx(0.5)


def test_compute_group_stats_coverage_rate_is_mean_over_gradable_rows() -> None:
    probe = _probe("fenwick_relationship_history")
    full_row = _row(
        _record(
            arm=Arm.ORACLE,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            answer=" and ".join(probe.answer_tokens),
        )
    )
    partial_row = _row(
        _record(
            arm=Arm.ORACLE,
            probe_id=probe.id,
            probe_class=probe.probe_class,
            answer=f"Coordinated per {probe.answer_tokens[0]}.",
        ),
        replicate=1,
    )
    stats = compute_group_stats([full_row, partial_row])
    stat = next(s for s in stats if s.arm == "oracle")
    expected = (1.0 + 1.0 / len(probe.answer_tokens)) / 2.0
    assert stat.coverage_rate == pytest.approx(expected)


def test_render_report_harm_and_coverage_sections_are_na_on_current_corpus() -> None:
    """No probe class in the current corpus carries forbidden_tokens, so
    the Harm section reads n/a throughout; Coverage reads a real number for
    every gradable row on the current fixtures."""
    rows = [
        _row(_oracle_record(answer="25 days")),
    ]
    report = build_report(rows)
    text = render_report(report)
    assert "## Harm (forbidden-token) rate" in text
    assert "## Coverage (fraction of planted tokens)" in text
    harm_section = text[text.index("## Harm (forbidden-token) rate") :]
    harm_section = harm_section[: harm_section.index("## Coverage")]
    assert "n/a" in harm_section


def test_render_report_harm_and_coverage_values_land_in_the_right_section(monkeypatch) -> None:
    """A swapped harm_free_rate/coverage_rate column in render_report would
    put the wrong number under the wrong heading and no earlier test would
    catch it (both were n/a-only, or checked separately). Build rows with
    DISTINCT, non-n/a values for each -- harm_free_rate=0.25,
    coverage_rate=0.75 -- and assert each value appears only in its own
    section."""
    import tests.evals.north_star_report as nsr

    pto_probe = _probe("pto_allowance")
    harm_probe = dataclasses.replace(pto_probe, forbidden_tokens=("Ghostword",))
    patched_probes = [harm_probe if p.id == pto_probe.id else p for p in _CORPUS.probes]
    patched_corpus = dataclasses.replace(_CORPUS, probes=patched_probes)
    monkeypatch.setitem(nsr._CORPUS_CACHE, CORPUS_SCALE, patched_corpus)

    # harm_free_rate = 1/4 = 0.25 -- one safe answer, three that name the
    # forbidden token.
    harm_rows = [
        _row(
            _record(
                arm=Arm.ORACLE,
                probe_id=harm_probe.id,
                probe_class=harm_probe.probe_class,
                answer="25 days." if i == 0 else "Governed by Ghostword.",
            ),
            replicate=i,
        )
        for i in range(4)
    ]

    # coverage_rate = (1.0 + 0.5) / 2 = 0.75 -- one full answer, one naming
    # only the first of the probe's two answer_tokens.
    coverage_probe = _probe("fenwick_relationship_history")
    assert len(coverage_probe.answer_tokens) == 2
    coverage_rows = [
        _row(
            _record(
                arm=Arm.ORACLE,
                probe_id=coverage_probe.id,
                probe_class=coverage_probe.probe_class,
                answer=" and ".join(coverage_probe.answer_tokens),
            )
        ),
        _row(
            _record(
                arm=Arm.ORACLE,
                probe_id=coverage_probe.id,
                probe_class=coverage_probe.probe_class,
                answer=f"Coordinated per {coverage_probe.answer_tokens[0]}.",
            ),
            replicate=1,
        ),
    ]

    report = build_report(harm_rows + coverage_rows)
    text = render_report(report)

    harm_section = text[text.index("## Harm (forbidden-token) rate") :]
    harm_section = harm_section[: harm_section.index("## Coverage")]
    coverage_section = text[text.index("## Coverage (fraction of planted tokens)") :]
    coverage_section = coverage_section[: coverage_section.index("### Weak probes")]

    assert "0.250" in harm_section
    assert "0.750" not in harm_section
    assert "0.750" in coverage_section
    assert "0.250" not in coverage_section


_CORRECTNESS_SECTION_HEADER_AND_PROSE = (
    "## Correctness (answer ground truth, issue athenaeum#1573)\n"
    "\n"
    "`correctness_rate` grades the ANSWER, not retrieval: normalized substring match "
    "against each probe's planted `answer_tokens` (non-abstention), or the declining-"
    "language rule for abstention probes — see `grade_correctness`. No LLM judge. This is "
    "what makes NONE (floor) and ORACLE (ceiling) readable as numbers for the first time — "
    "every other dimension above describes retrieval or cost, never whether the final "
    "answer was actually right. `n/a` means no probe in that group carries ground truth "
    "tokens to grade against. Those tokens are the corpus pages' own internal "
    "reference tags, so every arm's system prompt carries one identical instruction "
    "(issue athenaeum#1753) to end the answer with `[ref: TAG]` for each page relied on, "
    "or `[ref: none]` for none — correctness therefore reads as “did the arm reach the "
    "right page and say so”. Rows recorded before that contract landed carry no tags "
    "and grade at or near 0 for every arm, including ORACLE.\n"
    "\n"
    "| probe_class | corpus_scale | arm | n | correctness_rate |\n"
    "| --- | --- | --- | --- | --- |"
)


def test_correctness_section_is_byte_identical_after_harm_and_coverage_additions() -> None:
    """issue athenaeum#1772/#1773: the Correctness section's own bytes must
    not move when the Harm and Coverage sections are added after it -- a
    reader diffing an old report against a new one must see the Correctness
    section untouched, with the two new sections appearing only after it."""
    rows = [_row(_oracle_record(answer="25 days"))]
    report = build_report(rows)
    text = render_report(report)
    assert _CORRECTNESS_SECTION_HEADER_AND_PROSE in text
    correctness_start = text.index("## Correctness (answer ground truth")
    harm_start = text.index("## Harm (forbidden-token) rate")
    assert correctness_start < harm_start


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


def test_crossover_scale_prose_names_every_size_scale_including_xlarge() -> None:
    """The "walks the SIZE axis" sentence is DERIVED from
    :data:`SIZE_SCALE_ORDER`, not a second hand-typed literal -- a Sentry
    review on PR athenaeum#1750 caught the prose still reading
    ``core < small < medium < large`` after xlarge (athenaeum#1735) was
    added to :data:`SIZE_SCALE_ORDER` but not to this sentence. Pinning the
    text as a derived join makes that drift impossible to reintroduce."""
    text = render_report(build_report([]))
    assert " < ".join(f"`{s}`" for s in SIZE_SCALE_ORDER) in text


# ---------------------------------------------------------------------------
# read_entity as delivery evidence (issue athenaeum#1842)
#
# `_delivered_uids` could not see a page the model fetched with the
# `read_entity` MCP tool, because that tool returns JSON while the extractor
# parses `recall`'s `**Uid:**` markdown. Every fixture below pairs its
# positive case with the leak guard the acceptance criteria name: a REQUEST
# is not delivery, only a matching successful RESULT is.
# ---------------------------------------------------------------------------

# The real "core" corpus's two-hop follow_through probe: its ground truth is
# split across two pages, each planting one marker -- exactly the shape whose
# second hop `read_entity` exists to serve. Read off the corpus, never
# duplicated as literals.
FOLLOW_THROUGH_PROBE_ID = "fenwick_relationship_history"


def _deep_hop_uid(probe: Probe) -> str:
    """The page this probe's hop LANDS on -- derived from the link graph by
    `corpus.deep_hop_uids`, never read off `expected_uids[1]` (issue
    athenaeum#1844: a positional read returns the wrong page the moment a
    probe's ground truth is reordered)."""
    hops = deep_hop_uids(probe, {page.uid: page for page in _CORPUS.pages})
    assert len(hops) == 1, (probe.id, hops)
    return hops[0]


def _read_entity_events(
    *,
    uid: str,
    call_id: str = "toolu_read_1",
    tool_name: str = "mcp__athenaeum__read_entity",
    payload: str | list[dict[str, Any]] | None = None,
    is_error: bool | None = None,
    include_result: bool = True,
) -> list[dict[str, Any]]:
    """The assistant `tool_use` / user `tool_result` PAIR one `read_entity`
    hop produces in a real stream-json transcript.

    *payload* defaults to the server's own success shape (a JSON STRING
    carrying top-level ``uid`` and ``body``); pass a string to forge a
    different body, a list to exercise the multi-block `content` shape, and
    ``include_result=False`` to model a call whose result never arrived.
    """
    if payload is None:
        payload = json.dumps({"uid": uid, "body": f"# {uid}\n\nbody text", "footnotes": []})
    result_block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": payload,
    }
    if is_error is not None:
        result_block["is_error"] = is_error
    events: list[dict[str, Any]] = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": tool_name,
                        "input": {"uid": uid, "entity_class": "client"},
                    }
                ]
            },
        }
    ]
    if include_result:
        events.append({"type": "user", "message": {"content": [result_block]}})
    return events


def _recall_result_events(recall_text: str) -> list[dict[str, Any]]:
    """A `recall` tool_result event carrying *recall_text* -- the `**Uid:**`
    markdown channel that already worked before this issue."""
    return [
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_recall_1",
                        "content": [{"type": "text", "text": recall_text}],
                    }
                ]
            },
        }
    ]


def _follow_through_record(
    *,
    arm: Arm = Arm.PULL,
    answer: str,
    transcript: list[dict[str, Any]],
) -> RolloutRecord:
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    return _record(
        arm=arm,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=answer,
        transcript=transcript,
    )


def _both_markers_answer() -> str:
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    (_uid1, marker1), (_uid2, marker2) = probe.answer_markers
    return f"Fenwick was {marker1}, and Dara flags it in the {marker2}."


def test_read_entity_result_counts_as_delivery_evidence() -> None:
    """AC1: a page fetched with `read_entity` IS delivered.

    The first hop arrives through `recall`'s `**Uid:**` markdown; the second
    through `read_entity`'s JSON. Before this issue only the first channel
    existed, so this cell graded False with both markers quoted verbatim --
    the dominant follow_through failure mode.
    """
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    first_uid, second_uid = probe.expected_uids
    record = _follow_through_record(
        answer=_both_markers_answer(),
        transcript=[
            *_recall_result_events(f"**Uid:** {first_uid}\n\nsnippet"),
            *_read_entity_events(uid=second_uid),
        ],
    )
    assert _read_entity_delivered_uids(record) == (second_uid,)
    assert set(_delivered_uids(record, probe, _CORPUS)) == {first_uid, second_uid}
    assert grade_correctness(record, probe, _CORPUS) is True


def test_read_entity_request_without_a_result_is_not_delivery_evidence() -> None:
    """AC2 (leak guard): an answer quoting a page's marker still grades False
    when that page was never SUCCESSFULLY read.

    Four ways a request fails to become delivery -- no result at all, an
    `is_error` result, an empty `body`, and a payload naming a DIFFERENT uid
    than the one requested (an alias redirect, not the page asked for). All
    four carry the identical, fully-marker-bearing answer, so the False is
    attributable to the delivery channel and nothing else.
    """
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    first_uid, second_uid = probe.expected_uids
    answer = _both_markers_answer()
    recall_events = _recall_result_events(f"**Uid:** {first_uid}\n\nsnippet")

    failures = {
        "no result": _read_entity_events(uid=second_uid, include_result=False),
        "is_error": _read_entity_events(uid=second_uid, is_error=True),
        "empty body": _read_entity_events(
            uid=second_uid, payload=json.dumps({"uid": second_uid, "body": "   "})
        ),
        "uid mismatch": _read_entity_events(
            uid=second_uid, payload=json.dumps({"uid": first_uid, "body": "other page"})
        ),
        "not json": _read_entity_events(uid=second_uid, payload="Error: entity not found"),
    }
    for label, events in failures.items():
        record = _follow_through_record(answer=answer, transcript=[*recall_events, *events])
        assert _read_entity_delivered_uids(record) == (), label
        assert second_uid not in _delivered_uids(record, probe, _CORPUS), label
        assert grade_correctness(record, probe, _CORPUS) is False, label

    # Positive control: the SAME answer and the SAME first hop, with a
    # SUCCESSFUL read of the second page, grades True -- so every False above
    # is about delivery evidence, not about the answer text.
    ok = _follow_through_record(
        answer=answer,
        transcript=[*recall_events, *_read_entity_events(uid=second_uid)],
    )
    assert grade_correctness(ok, probe, _CORPUS) is True


def test_read_entity_tool_name_matched_on_its_namespaced_segment() -> None:
    """The stored transcripts spell the tool `mcp__athenaeum__read_entity`,
    so a bare equality would match nothing real -- but a loose substring scan
    would swallow an unrelated tool whose result shape was never validated.
    """
    assert _is_read_entity_tool("mcp__athenaeum__read_entity") is True
    assert _is_read_entity_tool("read_entity") is True
    assert _is_read_entity_tool("mcp__athenaeum__recall") is False
    assert _is_read_entity_tool("read_entity_batch") is False
    assert _is_read_entity_tool("bulk_read_entity_v2") is False
    assert _is_read_entity_tool(None) is False


def test_read_entity_handles_both_tool_result_content_shapes() -> None:
    """The stream-json format renders a tool_result's `content` either as a
    plain string or as a list of `{"type": "text", ...}` blocks. Both must
    decode, exactly as `_pull_delivered_text` already handles both."""
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    uid = _deep_hop_uid(probe)
    body = json.dumps({"uid": uid, "body": "page body"})

    as_string = _follow_through_record(
        answer="x", transcript=_read_entity_events(uid=uid, payload=body)
    )
    as_blocks = _follow_through_record(
        answer="x",
        transcript=_read_entity_events(uid=uid, payload=[{"type": "text", "text": body}]),
    )
    assert _read_entity_delivered_uids(as_string) == (uid,)
    assert _read_entity_delivered_uids(as_blocks) == (uid,)


def test_read_entity_pairing_is_order_independent_and_deduplicated() -> None:
    """Two hops for the SAME page yield one uid; a result that precedes its
    own call in the event list still pairs (the walk collects both sides
    before joining, never assuming transcript order)."""
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    uid = _deep_hop_uid(probe)
    events = [
        *_read_entity_events(uid=uid, call_id="toolu_a"),
        *_read_entity_events(uid=uid, call_id="toolu_b"),
    ]
    assert _read_entity_delivered_uids(
        _follow_through_record(answer="x", transcript=events)
    ) == (uid,)
    reversed_events = list(reversed(_read_entity_events(uid=uid)))
    assert _read_entity_delivered_uids(
        _follow_through_record(answer="x", transcript=reversed_events)
    ) == (uid,)


def test_read_entity_channel_is_scoped_to_the_arms_served_the_mcp_tools() -> None:
    """Only PULL and PUSH_BREADCRUMB_PULL are served `read_entity` (see
    `tests.evals.rollout`'s PULL_ALLOWED_TOOLS and the api-mode `tools=`
    lists), so only those two arms read the channel. An arm that cannot call
    a tool must not gain evidence from a transcript shape it could never
    have produced."""
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    uid = _deep_hop_uid(probe)
    events = _read_entity_events(uid=uid)

    for arm in (Arm.PULL, Arm.PUSH_BREADCRUMB_PULL):
        record = _follow_through_record(arm=arm, answer="x", transcript=events)
        assert uid in _delivered_uids(record, probe, _CORPUS), arm

    for arm in (Arm.NONE, Arm.PUSH_BREADCRUMB, Arm.PUSH_PAGES_UPPER_BOUND):
        record = _follow_through_record(arm=arm, answer="x", transcript=events)
        assert _delivered_uids(record, probe, _CORPUS) == (), arm


def test_delivered_uids_for_utilization_is_unchanged_by_the_read_entity_channel() -> None:
    """AC3 (scope): `delivered_uids_for_utilization` -- the WASTE basis -- is
    deliberately NOT the grader's dispatch and does not gain this channel.

    A page the model fetched itself is not waste (it asked for it), so
    counting it there would corrupt `uid_citation_rate` and every
    `mean_wasted_*` figure. The two functions genuinely diverge on this row:
    the grader sees the page, the waste accounting does not.
    """
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    uid = _deep_hop_uid(probe)
    for arm in (Arm.PULL, Arm.PUSH_BREADCRUMB_PULL):
        row = _row(
            _follow_through_record(arm=arm, answer="x", transcript=_read_entity_events(uid=uid))
        )
        assert delivered_uids_for_utilization(row) == (), arm
        assert uid_citation_rate(row.record, delivered_uids_for_utilization(row)) is None, arm
        # ... while the grader's own dispatch DOES see it.
        assert uid in _delivered_uids(row.record, probe, _CORPUS), arm


# ---------------------------------------------------------------------------
# marker_miss_with_delivery (issue athenaeum#1842, report_only)
# ---------------------------------------------------------------------------


def test_marker_miss_with_delivery_true_only_when_delivery_held_and_a_marker_missed() -> None:
    """The column splits a False cell into its two causes: a DELIVERY gap
    (0) versus a model/marker-matching miss on a page that WAS delivered
    (1)."""
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    first_uid, second_uid = probe.expected_uids
    (_uid1, marker1), (_uid2, _marker2) = probe.answer_markers
    both_delivered = [
        *_recall_result_events(f"**Uid:** {first_uid}\n\nsnippet"),
        *_read_entity_events(uid=second_uid),
    ]

    # Delivered both, quoted only the first marker -> the miss is the model's.
    missed = _follow_through_record(
        answer=f"Fenwick was {marker1}.", transcript=both_delivered
    )
    assert grade_correctness(missed, probe, _CORPUS) is False
    assert marker_miss_with_delivery(missed, probe, _CORPUS) is True

    # Same answer, but the second page was never delivered -> a delivery gap,
    # NOT a marker miss. Both grade False; only this column tells them apart.
    delivery_gap = _follow_through_record(
        answer=f"Fenwick was {marker1}.",
        transcript=_recall_result_events(f"**Uid:** {first_uid}\n\nsnippet"),
    )
    assert grade_correctness(delivery_gap, probe, _CORPUS) is False
    assert marker_miss_with_delivery(delivery_gap, probe, _CORPUS) is False

    # A correct cell is a real observation of this diagnostic (False), never
    # an absence of one (None).
    correct = _follow_through_record(
        answer=_both_markers_answer(), transcript=both_delivered
    )
    assert grade_correctness(correct, probe, _CORPUS) is True
    assert marker_miss_with_delivery(correct, probe, _CORPUS) is False


def test_marker_miss_with_delivery_ungradable_population() -> None:
    """Abstention is `None` here (no marker to miss) even though
    `grade_correctness` DOES grade it -- the same exemption `tag_followed`
    takes. Over every other probe the ungradable set matches
    `grade_correctness`'s exactly, so on the rows where both columns report
    they are read over the same population. Checked against the grader's own
    verdict, never asserted independently.
    """
    abstention = _probe("abstain_unknown_client")
    record = _record(
        arm=Arm.PULL,
        probe_id=abstention.id,
        probe_class=abstention.probe_class,
        answer="I could not find anything about that.",
    )
    assert grade_correctness(record, abstention, _CORPUS) is not None
    assert marker_miss_with_delivery(record, abstention, _CORPUS) is None

    non_abstention = [p for p in _CORPUS.probes if p.probe_class != "abstention"]
    assert non_abstention  # positive control: the loop below grades something
    for probe in non_abstention:
        row_record = _record(
            arm=Arm.ORACLE, probe_id=probe.id, probe_class=probe.probe_class, answer="x"
        )
        correctness_none = grade_correctness(row_record, probe, _CORPUS) is None
        column_none = marker_miss_with_delivery(row_record, probe, _CORPUS) is None
        assert correctness_none == column_none, probe.id


def test_marker_miss_with_delivery_is_wired_into_group_stats_and_the_report() -> None:
    """The column reaches `GroupStats` as a COUNT and renders its own
    report_only section -- and, exactly as `tag_followed_rate` does not, it
    feeds no win/loss field and no §7 verdict."""
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    first_uid, second_uid = probe.expected_uids
    (_uid1, marker1), (_uid2, _marker2) = probe.answer_markers
    delivered = [
        *_recall_result_events(f"**Uid:** {first_uid}\n\nsnippet"),
        *_read_entity_events(uid=second_uid),
    ]
    rows = [
        _row(_follow_through_record(answer=f"Fenwick was {marker1}.", transcript=delivered)),
        _row(
            _follow_through_record(answer=_both_markers_answer(), transcript=delivered),
            replicate=1,
        ),
    ]
    stats = compute_group_stats(rows)
    assert len(stats) == 1
    assert stats[0].marker_miss_with_delivery == 1

    report = build_report(rows)
    text = render_report(report)
    assert "## Marker miss with delivery (issue athenaeum#1842, report_only)" in text
    assert "| probe_class | corpus_scale | arm | n | marker_miss_with_delivery |" in text
    assert "report_only" in text


def test_marker_miss_with_delivery_is_none_not_zero_for_an_ungradable_group() -> None:
    """`n/a`, never a counted zero: "nothing to count" is a different fact
    from "counted, found none"."""
    abstention = _probe("abstain_unknown_client")
    row = _row(
        _record(
            arm=Arm.PULL,
            probe_id=abstention.id,
            probe_class=abstention.probe_class,
            answer="I could not find anything about that.",
        )
    )
    stats = compute_group_stats([row])
    assert stats[0].marker_miss_with_delivery is None
    assert "| n/a |" in render_report(build_report([row]))


# ---------------------------------------------------------------------------
# follow_hop_rate (issue athenaeum#1844, report_only)
# ---------------------------------------------------------------------------


def test_deep_hop_delivered_separates_no_hop_from_hopped_but_answer_wrong() -> None:
    """The whole point of the column: two cells that both grade `False`,
    told apart by whether the DEEP page (derived, not `expected_uids[1]`)
    ever reached the arm."""
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    first_uid, second_uid = probe.expected_uids
    assert _deep_hop_uid(probe) == second_uid  # this corpus; derived, not assumed
    (_uid1, marker1), (_uid2, _marker2) = probe.answer_markers

    breadcrumb_only = _follow_through_record(
        answer=f"Fenwick was {marker1}.",
        transcript=_recall_result_events(f"**Uid:** {first_uid}\n\nsnippet"),
    )
    assert grade_correctness(breadcrumb_only, probe, _CORPUS) is False
    assert deep_hop_delivered(breadcrumb_only, probe, _CORPUS) is False

    hopped = _follow_through_record(
        answer=f"Fenwick was {marker1}.",
        transcript=[
            *_recall_result_events(f"**Uid:** {first_uid}\n\nsnippet"),
            *_read_entity_events(uid=second_uid),
        ],
    )
    assert grade_correctness(hopped, probe, _CORPUS) is False
    assert deep_hop_delivered(hopped, probe, _CORPUS) is True
    # ...and that second cell is the marker-miss athenaeum#1842 counts, so the
    # two report_only columns agree on the SAME cell.
    assert marker_miss_with_delivery(hopped, probe, _CORPUS) is True
    assert marker_miss_with_delivery(breadcrumb_only, probe, _CORPUS) is False

    # A correct cell that hopped is a real True, never an absence.
    correct = _follow_through_record(
        answer=_both_markers_answer(),
        transcript=[
            *_recall_result_events(f"**Uid:** {first_uid}\n\nsnippet"),
            *_read_entity_events(uid=second_uid),
        ],
    )
    assert grade_correctness(correct, probe, _CORPUS) is True
    assert deep_hop_delivered(correct, probe, _CORPUS) is True


def test_deep_hop_delivered_is_none_for_every_non_follow_through_probe() -> None:
    """`n/a`, never a counted zero: a probe class with no hop to follow has
    nothing to report here, which is a different fact from "did not hop"."""
    others = [p for p in _CORPUS.probes if p.probe_class != "follow_through"]
    assert others  # positive control: the loop below grades something
    for probe in others:
        record = _record(
            arm=Arm.ORACLE, probe_id=probe.id, probe_class=probe.probe_class, answer="x"
        )
        assert deep_hop_delivered(record, probe, _CORPUS) is None, probe.id

    follow_through = [p for p in _CORPUS.probes if p.probe_class == "follow_through"]
    assert follow_through
    for probe in follow_through:
        record = _record(
            arm=Arm.ORACLE, probe_id=probe.id, probe_class=probe.probe_class, answer="x"
        )
        assert deep_hop_delivered(record, probe, _CORPUS) is not None, probe.id


def test_follow_hop_rate_is_wired_into_group_stats_and_the_report() -> None:
    """The column reaches `GroupStats` as a RATE over follow_through cells
    and renders its own report_only section."""
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    first_uid = probe.expected_uids[0]
    second_uid = _deep_hop_uid(probe)
    breadcrumb = _recall_result_events(f"**Uid:** {first_uid}\n\nsnippet")
    rows = [
        _row(_follow_through_record(answer="x", transcript=breadcrumb)),
        _row(
            _follow_through_record(
                answer="x", transcript=[*breadcrumb, *_read_entity_events(uid=second_uid)]
            ),
            replicate=1,
        ),
    ]
    stats = compute_group_stats(rows)
    assert len(stats) == 1
    assert stats[0].follow_hop_rate == 0.5

    text = render_report(build_report(rows))
    assert "## Follow-through hop delivered (issue athenaeum#1844, report_only)" in text
    assert "| probe_class | corpus_scale | arm | n | follow_hop_rate |" in text
    assert "report_only" in text


def test_follow_hop_rate_renders_na_for_every_other_probe_class() -> None:
    """AC: `n/a` for every non-follow_through probe_class -- checked as the
    whole column over a report that carries both kinds of group, so a single
    `n/a` elsewhere in the table cannot fake it."""
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    other = _probe(PROBE_ID)
    assert other.probe_class != "follow_through"
    rows = [
        _row(
            _follow_through_record(
                answer="x",
                transcript=[
                    *_recall_result_events(f"**Uid:** {probe.expected_uids[0]}\n\nsnippet"),
                    *_read_entity_events(uid=_deep_hop_uid(probe)),
                ],
            )
        ),
        _row(
            _record(
                arm=Arm.PULL, probe_id=other.id, probe_class=other.probe_class, answer="x"
            )
        ),
    ]
    stats = {s.probe_class: s.follow_hop_rate for s in compute_group_stats(rows)}
    assert stats == {"follow_through": 1.0, other.probe_class: None}

    text = render_report(build_report(rows))
    section = text.split("## Follow-through hop delivered")[1].split("\n### ")[0]
    body_rows = [ln for ln in section.splitlines() if ln.startswith("| ") and "| --- |" not in ln]
    rendered = {
        ln.split(" | ")[0].removeprefix("| "): ln.split(" | ")[-1].removesuffix(" |")
        for ln in body_rows
        if not ln.startswith("| probe_class")
    }
    assert rendered["follow_through"] == "1.000"
    assert rendered[other.probe_class] == "n/a"


def test_follow_hop_rate_is_report_only_and_moves_no_verdict() -> None:
    """AC (issue athenaeum#1844): the column feeds no §7 condition, no
    cutoff, no win/loss field and no `compute_verdicts` input.

    Proved behaviourally -- `compute_verdicts` (which does call
    `compute_group_stats`, so the new predicate genuinely runs underneath
    it) returns the identical verdicts however `deep_hop_delivered` answers
    -- with a positive control on `grade_correctness`, which DOES move them,
    so the invariance is not vacuous. Backed by a structural scan: neither
    `compute_verdicts` nor `crossover_scales` mentions the column at all.
    """
    probe = _probe(FOLLOW_THROUGH_PROBE_ID)
    first_uid = probe.expected_uids[0]
    breadcrumb = _recall_result_events(f"**Uid:** {first_uid}\n\nsnippet")
    rows = [
        _row(
            # `compute_verdicts` keeps only real recorded `vector` rows.
            dataclasses.replace(
                _follow_through_record(
                    arm=arm,
                    answer=_both_markers_answer(),
                    # The verdict arm answers correctly (it was delivered
                    # both pages), the others were handed the breadcrumb
                    # only -- a spread the grader's positive control below
                    # can actually move.
                    transcript=(
                        [*breadcrumb, *_read_entity_events(uid=_deep_hop_uid(probe))]
                        if arm is Arm.PUSH_BREADCRUMB_PULL
                        else breadcrumb
                    ),
                ),
                search_backend="vector",
            ),
            replicate=index,
        )
        for index, arm in enumerate((Arm.PUSH_BREADCRUMB_PULL, Arm.NATIVE_GREP, Arm.NONE))
    ]
    kwargs = {"relationship_probe_ids": frozenset(), "report_only_classes": frozenset()}
    baseline = compute_verdicts(rows, **kwargs)
    assert baseline  # positive control: there is a verdict to move

    for answer in (True, False, None):
        with mock.patch.object(nsr, "deep_hop_delivered", lambda *a, _v=answer, **k: _v):
            assert compute_verdicts(rows, **kwargs) == baseline, answer
            assert {s.follow_hop_rate for s in compute_group_stats(rows)} != {None} or (
                answer is None
            )

    # Positive control: a grader that DOES feed the verdicts moves them.
    with mock.patch.object(nsr, "grade_correctness", lambda *a, **k: False):
        assert compute_verdicts(rows, **kwargs) != baseline

    for func in (compute_verdicts, crossover_scales):
        source = inspect.getsource(func)
        assert "follow_hop_rate" not in source, func.__name__
        assert "deep_hop" not in source, func.__name__

    # ...and it is not a win/loss field: GroupStats carries it purely as a
    # report column, so replacing it changes nothing downstream.
    stats = compute_group_stats(rows)
    mutated = [dataclasses.replace(s, follow_hop_rate=0.123) for s in stats]
    assert crossover_scales(mutated) == crossover_scales(stats)


def test_report_stamps_the_grader_revision_beside_the_corpus_digest() -> None:
    """`Corpus.fingerprint()` digests pages only, so it cannot move when the
    GRADING RULE changes -- two tables over the same store under different
    rules would otherwise be indistinguishable."""
    row = _row(_follow_through_record(answer="x", transcript=[]))
    text = render_report(build_report([row]))
    assert f"- grader_revision: {GRADER_REVISION}" in text
    digest_index = text.index("- corpus_digest[")
    assert text.index("- grader_revision:") > digest_index
