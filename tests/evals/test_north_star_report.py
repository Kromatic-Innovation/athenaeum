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
from tests.evals.north_star_report import (
    GroupStats,
    NorthStarReport,
    RolloutRow,
    append_rollout_row,
    build_report,
    compute_group_stats,
    delivered_text_for_utilization,
    delivered_uids_for_utilization,
    distinctive_ngram_overlap,
    lexical_overlap,
    load_rollout_rows,
    render_report,
    uid_citation_rate,
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
        arm=Arm.PUSH,
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
    assert {row.record.arm for row in rows} == {Arm.NONE, Arm.PUSH, Arm.ORACLE, Arm.PULL}
    for row, original in zip(rows, records):
        assert row.record == original
        assert row.cell.replicate == 0


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
        ("single_hop", "core", "push"),
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

    rows = [
        _row(_none_record()),
        _row(_push_record(answer=f"25 days ({TARGET_UID})")),
        _row(_oracle_record(answer="25 days")),
        _row(_pull_record(called=True, answer=f"25 days ({TARGET_UID})")),
    ]
    report = build_report(rows)  # must not raise
    render_report(report)  # must not raise
    write_report(report, out_dir=tmp_path)  # must not raise
