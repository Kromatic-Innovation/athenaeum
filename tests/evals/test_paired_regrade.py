# SPDX-License-Identifier: Apache-2.0
"""Fixture test for the paired re-grade module (issue athenaeum#1932).

Builds a small SYNTHETIC two-store fixture -- never the paid result stores
-- and asserts the module reproduces the exact counts the issue's
acceptance criteria specify: 37/45 correct, 5 marker-miss-with-delivery, 3
delivery-gap for the "left" (reference) store; 31/45, 10, 4 for the
"right" store; 7 correct-to-incorrect and 1 incorrect-to-correct paired
discordance (37 - 7 + 1 == 31, the arithmetic the fixture is built to
satisfy).

No model call, no live rollout, no paid store.
"""

from __future__ import annotations

from pathlib import Path

from tests.evals.containment import GridCell, ResultStore
from tests.evals.corpus import Corpus, Page, Probe
from tests.evals.north_star_report import append_rollout_row
from tests.evals.paired_regrade import (
    discordant_pairs,
    grade_store,
    input_equality,
    miss_and_discordant_cells,
    summarize,
)
from tests.evals.rollout import Arm, RolloutRecord, TurnTokenUsage

CORPUS_SCALE = "synthetic-1932"

#: 30 probes correct on both sides, 6 probes that flip correct->marker-miss,
#: 1 probe that flips correct->delivery-gap, 4 probes marker-miss on both
#: sides, 3 probes delivery-gap on both sides, 1 probe marker-miss-left that
#: flips to correct-right. 30 + 6 + 1 + 4 + 3 + 1 == 45.
CONCORDANT_CORRECT = [f"concordant_correct_{i}" for i in range(30)]
FLIP_TO_MM = [f"flip_to_mm_{i}" for i in range(6)]
FLIP_TO_DG = ["flip_to_dg_0"]
CONCORDANT_MM = [f"concordant_mm_{i}" for i in range(4)]
CONCORDANT_DG = [f"concordant_dg_{i}" for i in range(3)]
FLIP_TO_CORRECT = ["flip_to_correct_0"]

ALL_GRADABLE = (
    CONCORDANT_CORRECT + FLIP_TO_MM + FLIP_TO_DG + CONCORDANT_MM + CONCORDANT_DG + FLIP_TO_CORRECT
)
assert len(ALL_GRADABLE) == 45

ABSTENTION = [f"abstain_{i}" for i in range(3)]

#: Present in the left store only, to prove pairing drops a probe missing
#: from one side rather than crashing or silently miscounting.
LEFT_ONLY = "left_only_probe"


def _bullet(name: str) -> str:
    """The push-breadcrumb shape ``_breadcrumb_delivered_uids`` parses:
    ``  - <name> -- <description>``, using an em dash exactly as the real
    hook renders it."""
    return f"  - {name} — a synthetic page"


def _page(uid: str, token: str) -> Page:
    return Page(
        uid=uid,
        type="note",
        name=uid,
        body=f"This page plants the fact {token}.",
        tier="core",
    )


def _probe(pid: str, *, token: str, marker: str, probe_class: str = "single_hop") -> Probe:
    return Probe(
        id=pid,
        probe_class=probe_class,
        query=f"query for {pid}",
        expected_uids=(pid,),
        answer_tokens=(token,),
        answer_markers=((pid, marker),),
    )


def _abstention_probe(pid: str) -> Probe:
    return Probe(
        id=pid,
        probe_class="abstention",
        query=f"query for {pid}",
        expected_uids=(),
        answer_tokens=(),
        answer_markers=(),
    )


def _corpus() -> Corpus:
    pages: list[Page] = []
    probes: list[Probe] = []
    for pid in ALL_GRADABLE + [LEFT_ONLY]:
        pages.append(_page(pid, f"TOKEN-{pid}"))
        probes.append(_probe(pid, token=f"TOKEN-{pid}", marker=f"marker phrase for {pid}"))
    for pid in ABSTENTION:
        probes.append(_abstention_probe(pid))
    return Corpus(pages=pages, probes=probes, scale=CORPUS_SCALE)


def _record(
    *,
    pid: str,
    delivered: bool,
    marker_present: bool,
    marker: str,
    recall_calls: list[dict] | None = None,
) -> RolloutRecord:
    transcript: list[dict] = [{"pushed_context": _bullet(pid) if delivered else ""}]
    for call in recall_calls or ():
        transcript.append(
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": "recall",
                            "input": call["input"],
                        }
                    ]
                }
            }
        )
        transcript.append(
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": call["result"],
                        }
                    ]
                }
            }
        )
    answer = f"The answer is {marker}." if marker_present else "The answer omits the key fact."
    return RolloutRecord(
        arm=Arm.PUSH_BREADCRUMB_PULL,
        probe_id=pid,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=10, output_tokens=10)],
        turn_count=1,
        transcript=transcript,
    )


def _abstention_record(pid: str) -> RolloutRecord:
    return RolloutRecord(
        arm=Arm.PUSH_BREADCRUMB_PULL,
        probe_id=pid,
        probe_class="abstention",
        corpus_scale=CORPUS_SCALE,
        answer="I don't know -- nothing in the corpus speaks to that.",
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=10, output_tokens=10)],
        turn_count=1,
        transcript=[{"pushed_context": ""}],
    )


def _write_store(path: Path, rows: list[RolloutRecord]) -> ResultStore:
    store = ResultStore(path)
    for record in rows:
        cell = GridCell(
            probe=record.probe_id,
            arm=record.arm.value,
            corpus_scale=record.corpus_scale,
            replicate=0,
        )
        append_rollout_row(store, cell, record)
    return store


def _build_left_rows() -> list[RolloutRecord]:
    rows = []
    marker = "marker phrase for {}"
    for pid in CONCORDANT_CORRECT + FLIP_TO_MM + FLIP_TO_DG:
        rows.append(
            _record(pid=pid, delivered=True, marker_present=True, marker=marker.format(pid))
        )
    for pid in CONCORDANT_MM:
        rows.append(
            _record(pid=pid, delivered=True, marker_present=False, marker=marker.format(pid))
        )
    for pid in CONCORDANT_DG:
        rows.append(
            _record(pid=pid, delivered=False, marker_present=False, marker=marker.format(pid))
        )
    for pid in FLIP_TO_CORRECT:
        # Incorrect on the left: delivered, marker absent (marker-miss).
        rows.append(
            _record(pid=pid, delivered=True, marker_present=False, marker=marker.format(pid))
        )
    # A probe present on the left only, correct, to prove pairing drops it.
    rows.append(
        _record(pid=LEFT_ONLY, delivered=True, marker_present=True, marker=marker.format(LEFT_ONLY))
    )
    for pid in ABSTENTION:
        rows.append(_abstention_record(pid))
    return rows


def _build_right_rows() -> list[RolloutRecord]:
    rows = []
    marker = "marker phrase for {}"
    for pid in CONCORDANT_CORRECT:
        rows.append(
            _record(pid=pid, delivered=True, marker_present=True, marker=marker.format(pid))
        )
    for pid in FLIP_TO_MM:
        # Correct on the left, marker-miss on the right: delivered, marker absent.
        rows.append(
            _record(pid=pid, delivered=True, marker_present=False, marker=marker.format(pid))
        )
    for pid in FLIP_TO_DG:
        # Correct on the left, delivery-gap on the right: not delivered.
        rows.append(
            _record(pid=pid, delivered=False, marker_present=False, marker=marker.format(pid))
        )
    for pid in CONCORDANT_MM:
        rows.append(
            _record(pid=pid, delivered=True, marker_present=False, marker=marker.format(pid))
        )
    for pid in CONCORDANT_DG:
        rows.append(
            _record(pid=pid, delivered=False, marker_present=False, marker=marker.format(pid))
        )
    for pid in FLIP_TO_CORRECT:
        rows.append(
            _record(pid=pid, delivered=True, marker_present=True, marker=marker.format(pid))
        )
    # LEFT_ONLY is absent from the right store on purpose.
    for pid in ABSTENTION:
        rows.append(_abstention_record(pid))
    return rows


def test_paired_regrade_reproduces_spec_counts(tmp_path: Path) -> None:
    corpus = _corpus()
    left_path = tmp_path / "left.jsonl"
    right_path = tmp_path / "right.jsonl"
    _write_store(left_path, _build_left_rows())
    _write_store(right_path, _build_right_rows())

    left = grade_store(left_path, corpus)
    right = grade_store(right_path, corpus)

    left_summary = summarize(left)
    right_summary = summarize(right)
    # The left store carries one extra probe (left_only_probe, correct and
    # absent from the right store), so its own summary is 46/38 -- the
    # PAIRED 45/37 figure is what discordant_pairs below reports.
    assert (
        left_summary.n_graded,
        left_summary.correct,
        left_summary.marker_miss_with_delivery,
        left_summary.delivery_gap,
    ) == (46, 38, 5, 3)
    assert (
        right_summary.n_graded,
        right_summary.correct,
        right_summary.marker_miss_with_delivery,
        right_summary.delivery_gap,
    ) == (45, 31, 10, 4)

    discord = discordant_pairs(left, right)
    # left_only_probe is present in left but not right, and must not be
    # counted toward paired_n or either discordance list.
    assert LEFT_ONLY not in set(left) & set(right)
    assert discord.paired_n == 45
    assert len(discord.correct_to_incorrect) == 7
    assert set(discord.correct_to_incorrect) == set(FLIP_TO_MM) | set(FLIP_TO_DG)
    assert len(discord.incorrect_to_correct) == 1
    assert set(discord.incorrect_to_correct) == set(FLIP_TO_CORRECT)
    # The PAIRED left-correct count (37 == 38 minus the left-only probe)
    # minus the flips to incorrect, plus the one flip to correct, equals
    # the right store's correct count -- the issue's own 37 - 7 + 1 == 31.
    paired_left_correct = left_summary.correct - 1
    assert (
        paired_left_correct - len(discord.correct_to_incorrect) + len(discord.incorrect_to_correct)
        == right_summary.correct
    )

    # Scope is every marker-miss-with-delivery cell in EITHER store (the
    # concordant marker-miss probes, plus FLIP_TO_MM's right-side cells and
    # FLIP_TO_CORRECT's left-side cell) union every discordant-pair probe
    # (FLIP_TO_MM, FLIP_TO_DG, FLIP_TO_CORRECT). Concordant delivery-gap
    # probes are marker-miss on NEITHER side and are not a discordant pair,
    # so they are correctly excluded -- the issue's own acceptance
    # criterion scopes to marker-miss cells and discordant pairs only, not
    # every incorrect cell.
    scope = miss_and_discordant_cells(left, right)
    expected_scope = set(FLIP_TO_MM) | set(FLIP_TO_DG) | set(FLIP_TO_CORRECT) | set(CONCORDANT_MM)
    assert set(scope) == expected_scope


def test_paired_regrade_abstention_cells_excluded_from_denominator(tmp_path: Path) -> None:
    """The 3 abstention probes are present in both stores' raw rows but must
    not inflate the 45-probe gradable denominator, matching the issue's own
    Correctness table (abstention is a separate n=3 row)."""
    corpus = _corpus()
    left_path = tmp_path / "left.jsonl"
    right_path = tmp_path / "right.jsonl"
    _write_store(left_path, _build_left_rows())
    _write_store(right_path, _build_right_rows())

    left = grade_store(left_path, corpus)
    right = grade_store(right_path, corpus)
    assert all(pid in left for pid in ABSTENTION)
    assert all(pid in right for pid in ABSTENTION)
    # Left carries the extra left_only_probe (46); right does not (45).
    assert summarize(left).n_graded == 46
    assert summarize(right).n_graded == 45


def test_input_equality_pushed_context_and_recall_results(tmp_path: Path) -> None:
    """pushed_context equality (after hook_divergence normalization) and
    recall-result equality (scores stripped) over shared tool inputs."""
    pid_equal = CONCORDANT_CORRECT[0]
    pid_diff_context = CONCORDANT_CORRECT[1]
    pid_diff_recall = CONCORDANT_CORRECT[2]

    shared_input = {"query": "same query"}
    left_rows = [
        RolloutRecord(
            arm=Arm.PUSH_BREADCRUMB_PULL,
            probe_id=pid_equal,
            probe_class="single_hop",
            corpus_scale=CORPUS_SCALE,
            answer="answer",
            transcript=[
                {"pushed_context": _bullet(pid_equal) + "\n"},
            ],
        ),
        RolloutRecord(
            arm=Arm.PUSH_BREADCRUMB_PULL,
            probe_id=pid_diff_context,
            probe_class="single_hop",
            corpus_scale=CORPUS_SCALE,
            answer="answer",
            transcript=[{"pushed_context": _bullet(pid_diff_context)}],
        ),
        _record(
            pid=pid_diff_recall,
            delivered=True,
            marker_present=True,
            marker=f"marker phrase for {pid_diff_recall}",
            recall_calls=[
                {"id": "t1", "input": shared_input, "result": "page A (score: 9.1)\npage B"}
            ],
        ),
    ]
    right_rows = [
        RolloutRecord(
            arm=Arm.PUSH_BREADCRUMB_PULL,
            probe_id=pid_equal,
            probe_class="single_hop",
            corpus_scale=CORPUS_SCALE,
            answer="answer",
            # Shell hook's trailing-newline defect only -- normalize()
            # removes exactly this and nothing else.
            transcript=[{"pushed_context": _bullet(pid_equal)}],
        ),
        RolloutRecord(
            arm=Arm.PUSH_BREADCRUMB_PULL,
            probe_id=pid_diff_context,
            probe_class="single_hop",
            corpus_scale=CORPUS_SCALE,
            answer="answer",
            transcript=[{"pushed_context": _bullet(pid_diff_context + "-DIFFERENT")}],
        ),
        _record(
            pid=pid_diff_recall,
            delivered=True,
            marker_present=True,
            marker=f"marker phrase for {pid_diff_recall}",
            recall_calls=[
                {"id": "t1", "input": shared_input, "result": "page A (score: 4.4)\npage B"}
            ],
        ),
    ]
    left_path, right_path = tmp_path / "left.jsonl", tmp_path / "right.jsonl"
    _write_store(left_path, left_rows)
    _write_store(right_path, right_rows)
    corpus = Corpus(
        pages=[
            _page(pid_equal, "x"),
            _page(pid_diff_context, "x"),
            _page(pid_diff_recall, f"TOKEN-{pid_diff_recall}"),
        ],
        probes=[
            _probe(pid_equal, token="x", marker="m"),
            _probe(pid_diff_context, token="x", marker="m"),
            _probe(
                pid_diff_recall,
                token=f"TOKEN-{pid_diff_recall}",
                marker=f"marker phrase for {pid_diff_recall}",
            ),
        ],
        scale=CORPUS_SCALE,
    )
    left = grade_store(left_path, corpus)
    right = grade_store(right_path, corpus)
    eq = input_equality(left, right)
    # pushed_context: pid_equal equal after normalize(); pid_diff_context
    # differs; pid_diff_recall not compared here (both delivered, but not
    # part of this assertion's focus -- it also carries a pushed_context,
    # so it counts too, and is equal).
    assert eq.pushed_context_total == 3
    assert eq.pushed_context_equal == 2
    # recall: one shared input, equal once scores are stripped.
    assert eq.recall_pairs_total == 1
    assert eq.recall_pairs_equal == 1
