# SPDX-License-Identifier: Apache-2.0
"""Paired re-grade over two result stores for ``push_breadcrumb_pull``/``core``
(issue athenaeum#1932).

Reads two :class:`tests.evals.containment.ResultStore` files with the
unchanged :func:`tests.evals.north_star_report.load_rollout_rows`, grades
every ``push_breadcrumb_pull``/``core`` cell with the unchanged
:func:`tests.evals.north_star_report.grade_correctness` and
:func:`tests.evals.north_star_report.marker_miss_with_delivery`, and reports:

* per-store correct / marker-miss-with-delivery / delivery-gap counts
  (:func:`summarize`);
* paired correct-to-incorrect / incorrect-to-correct discordance over the
  probes gradable in both stores (:func:`discordant_pairs`);
* how many paired inputs are equal -- the delivered ``pushed_context`` after
  :func:`tests.evals.hook_divergence.normalize`, and every ``recall`` tool
  result whose tool input is identical in both runs, scores stripped
  (:func:`input_equality`).

Makes **no model call of any kind**: it only reads already-persisted result
stores and re-runs the existing, unchanged grading functions over them.

Run it (from the repository root)::

    python -m tests.evals.paired_regrade --left STORE_A --right STORE_B

No real result-store path is hard-coded here: this repository is public and
the operator-host eval-store directory is handed to a caller via the issue's
own Plan section, never via a literal path in this module.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path

from tests.evals.containment import ResultStore
from tests.evals.corpus import Corpus, build_corpus
from tests.evals.hook_divergence import normalize as normalize_pushed_context
from tests.evals.north_star_report import (
    GRADER_REVISION,
    grade_correctness,
    load_rollout_rows,
    marker_miss_with_delivery,
)
from tests.evals.rollout import Arm

#: Strips a rendered recall-result score annotation -- the same shape
#: ``(score: 9.5)`` the specify lane's scratch ``recalldiff.py`` stripped --
#: so two tool results that differ only in score are still counted equal.
_SCORE_RE = re.compile(r"\(score: [0-9.]+\)")


def _strip_scores(text: str) -> str:
    return _SCORE_RE.sub("", text)


@dataclasses.dataclass(frozen=True)
class CellGrade:
    """One graded ``push_breadcrumb_pull``/``core`` cell out of one store."""

    probe_id: str
    probe_class: str
    correct: bool | None
    marker_miss_with_delivery: bool | None
    delivery_gap: bool
    pushed_context: str | None
    #: ``(normalized tool_input json, raw tool_result text)`` pairs, in
    #: transcript order, for every ``recall`` tool call this cell made.
    recall_calls: tuple[tuple[str, str], ...]


def _pushed_context(record: object) -> str | None:
    """The delivered breadcrumb string, stored as ``transcript[0]["pushed_context"]``
    (``tests/evals/rollout.py``'s push-arm builder) -- ``None`` for an arm/record
    shape that never carries one."""
    transcript = getattr(record, "transcript", None)
    if not transcript or not isinstance(transcript[0], dict):
        return None
    value = transcript[0].get("pushed_context")
    return value if isinstance(value, str) else None


def _recall_calls(record: object) -> tuple[tuple[str, str], ...]:
    """Every ``recall`` tool call's ``(input, result)`` pair in *record*'s
    transcript, keyed by matching ``tool_use``/``tool_result`` ids -- the same
    pairing the specify lane's scratch ``recalldiff.py`` used, re-derived here
    rather than trusted.

    ``transcript[1:]``: index 0 is the push-arm's own breadcrumb entry (see
    :func:`_pushed_context`), never a turn to scan for tool calls.
    """
    transcript = getattr(record, "transcript", None) or []
    uses: dict[str, str] = {}
    out: list[tuple[str, str]] = []
    for entry in transcript[1:]:
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and str(block.get("name", "")).endswith("recall"):
                uses[block.get("id")] = json.dumps(block.get("input"), sort_keys=True)
            if block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                raw = block.get("content")
                raw = raw if isinstance(raw, str) else json.dumps(raw)
                out.append((uses[block["tool_use_id"]], raw))
    return tuple(out)


def grade_store(store_path: Path, corpus: Corpus | None = None) -> dict[str, CellGrade]:
    """Grade every ``push_breadcrumb_pull``/``core`` cell in *store_path*.

    Returns ``{probe_id: CellGrade}``. Delivery gap is defined as
    ``correct is False and marker_miss_with_delivery is False`` -- a cell
    graded wrong whose wrongness :func:`marker_miss_with_delivery` does not
    attribute to a delivered-but-unmatched marker. An abstention cell (both
    graders return ``None``) is excluded by construction (``correct is
    None``), matching the issue's own denominator.
    """
    corpus = corpus or build_corpus(scale="core")
    probes_by_id = {p.id: p for p in corpus.probes}
    store = ResultStore(store_path)
    out: dict[str, CellGrade] = {}
    for row in load_rollout_rows(store):
        record = row.record
        if record.arm is not Arm.PUSH_BREADCRUMB_PULL or record.corpus_scale != corpus.scale:
            continue
        probe = probes_by_id.get(record.probe_id)
        if probe is None:
            continue
        correct = grade_correctness(record, probe, corpus)
        mm = marker_miss_with_delivery(record, probe, corpus)
        delivery_gap = bool(correct is False and mm is False)
        out[record.probe_id] = CellGrade(
            probe_id=record.probe_id,
            probe_class=probe.probe_class,
            correct=correct,
            marker_miss_with_delivery=mm,
            delivery_gap=delivery_gap,
            pushed_context=_pushed_context(record),
            recall_calls=_recall_calls(record),
        )
    return out


@dataclasses.dataclass(frozen=True)
class StoreSummary:
    n_graded: int
    correct: int
    marker_miss_with_delivery: int
    delivery_gap: int


def _gradable(cells: dict[str, CellGrade]) -> list[CellGrade]:
    """Cells counted in the issue's 45-probe denominator: non-abstention
    (the class :func:`tests.evals.north_star_report.marker_miss_with_delivery`
    always returns ``None`` for, and which the issue's own Correctness table
    already excludes as its own aggregated row) with a defined
    :func:`tests.evals.north_star_report.grade_correctness` result -- the
    same two-part filter the specify lane's scratch ``regrade.py`` used
    (``cls != "abstention" and g is not None``)."""
    return [c for c in cells.values() if c.probe_class != "abstention" and c.correct is not None]


def summarize(cells: dict[str, CellGrade]) -> StoreSummary:
    """Correct / marker-miss-with-delivery / delivery-gap counts over every
    gradable (non-abstention) cell in *cells*."""
    graded = _gradable(cells)
    return StoreSummary(
        n_graded=len(graded),
        correct=sum(1 for c in graded if c.correct),
        marker_miss_with_delivery=sum(1 for c in graded if c.marker_miss_with_delivery),
        delivery_gap=sum(1 for c in graded if c.delivery_gap),
    )


@dataclasses.dataclass(frozen=True)
class Discordance:
    """Paired correctness discordance between two stores, over probes
    gradable in both."""

    paired_n: int
    correct_to_incorrect: tuple[str, ...]
    incorrect_to_correct: tuple[str, ...]


def discordant_pairs(left: dict[str, CellGrade], right: dict[str, CellGrade]) -> Discordance:
    """*left* is the reference (e.g. the shell-hook floor); *right* is the
    later reading. A probe missing from either store, or ungraded
    (abstention) in either, is excluded from ``paired_n`` -- pairing requires
    both sides to have graded the SAME probe."""
    common = sorted(set(left) & set(right))
    left_gradable = {c.probe_id for c in _gradable(left)}
    right_gradable = {c.probe_id for c in _gradable(right)}
    gradable = [pid for pid in common if pid in left_gradable and pid in right_gradable]
    c2i = tuple(pid for pid in gradable if left[pid].correct and not right[pid].correct)
    i2c = tuple(pid for pid in gradable if not left[pid].correct and right[pid].correct)
    return Discordance(paired_n=len(gradable), correct_to_incorrect=c2i, incorrect_to_correct=i2c)


@dataclasses.dataclass(frozen=True)
class InputEquality:
    """How many paired inputs are equal between two stores (issue athenaeum#1932
    acceptance criterion 3)."""

    pushed_context_total: int
    pushed_context_equal: int
    recall_pairs_total: int
    recall_pairs_equal: int


def input_equality(left: dict[str, CellGrade], right: dict[str, CellGrade]) -> InputEquality:
    """Compare, over every probe present in both *left* and *right*:

    * the delivered ``pushed_context`` after
      :func:`tests.evals.hook_divergence.normalize` -- a probe counts only
      when BOTH sides carry a non-``None`` pushed_context;
    * every ``recall`` tool result whose normalized tool input is IDENTICAL
      in both runs, scores stripped -- a probe can contribute zero, one, or
      several such pairs, one per shared input.
    """
    common = sorted(set(left) & set(right))
    pc_total = pc_equal = 0
    recall_total = recall_equal = 0
    for pid in common:
        lc, rc = left[pid], right[pid]
        if lc.pushed_context is not None and rc.pushed_context is not None:
            pc_total += 1
            if normalize_pushed_context(lc.pushed_context) == normalize_pushed_context(
                rc.pushed_context
            ):
                pc_equal += 1
        by_input_l = dict(lc.recall_calls)
        by_input_r = dict(rc.recall_calls)
        for shared_input in sorted(set(by_input_l) & set(by_input_r)):
            recall_total += 1
            if _strip_scores(by_input_l[shared_input]) == _strip_scores(by_input_r[shared_input]):
                recall_equal += 1
    return InputEquality(
        pushed_context_total=pc_total,
        pushed_context_equal=pc_equal,
        recall_pairs_total=recall_total,
        recall_pairs_equal=recall_equal,
    )


def miss_and_discordant_cells(
    left: dict[str, CellGrade], right: dict[str, CellGrade]
) -> tuple[str, ...]:
    """Probe ids in scope for the blind classification (issue athenaeum#1932
    acceptance criterion 4): every marker-miss-with-delivery cell in either
    store, plus every probe in the discordant pairs between them. Each
    returned probe id contributes BOTH its left-store and right-store cells
    to the classification sheet -- this function names the probes, not the
    individual cells."""
    discord = discordant_pairs(left, right)
    miss_ids = {pid for pid, c in left.items() if c.marker_miss_with_delivery} | {
        pid for pid, c in right.items() if c.marker_miss_with_delivery
    }
    discord_ids = set(discord.correct_to_incorrect) | set(discord.incorrect_to_correct)
    return tuple(sorted(miss_ids | discord_ids))


def _report(args: argparse.Namespace) -> int:
    corpus = build_corpus(scale="core")
    left = grade_store(Path(args.left), corpus)
    right = grade_store(Path(args.right), corpus)
    ls, rs = summarize(left), summarize(right)
    discord = discordant_pairs(left, right)
    eq = input_equality(left, right)
    payload = {
        "grader_revision": GRADER_REVISION,
        "left": {"path": args.left, **dataclasses.asdict(ls)},
        "right": {"path": args.right, **dataclasses.asdict(rs)},
        "discordance": {
            "paired_n": discord.paired_n,
            "correct_to_incorrect": list(discord.correct_to_incorrect),
            "incorrect_to_correct": list(discord.incorrect_to_correct),
        },
        "input_equality": dataclasses.asdict(eq),
        "classification_scope": list(miss_and_discordant_cells(left, right)),
    }
    print(json.dumps(payload, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--left", required=True, help="earlier/reference result store path")
    parser.add_argument("--right", required=True, help="later result store path")
    args = parser.parse_args(argv)
    return _report(args)


if __name__ == "__main__":  # pragma: no cover -- CLI entry
    sys.exit(main())
