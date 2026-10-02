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

import dataclasses
from pathlib import Path

from tests.evals.containment import GridCell, ResultStore
from tests.evals.corpus import (
    Corpus,
    Page,
    Probe,
    _normalize_marker_for_match,
    build_corpus,
)
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


# ---------------------------------------------------------------------------
# athenaeum#1935: the eight repaired probes, re-graded through this module
# ---------------------------------------------------------------------------
#
# athenaeum#1932 classified the `push_breadcrumb_pull`/`core` marker-miss
# cells blind and found most of the residual gap was a GRADING artefact:
# the asked fact was in the answer, reworded or reformatted past the
# authored `answer_markers` substring. athenaeum#1935 repaired those probes
# by ADDING marker alternatives, and this section re-grades the repair
# through `grade_store`/`summarize`/`discordant_pairs` -- the same module,
# the same unchanged `grade_correctness`.
#
# The fixture is synthetic in the one sense that matters: every answer
# string below is INVENTED for this test. None is a line from a paid result
# store, and no paid store is read (the stores the classification came from
# live on the operator host and are not in this repository at all). The
# probe and marker text IS the real `probes.yaml` ground truth, because the
# repair under test is a change to that ground truth -- a synthetic probe
# could not show it.

#: The authored (pre-athenaeum#1935) marker for each repaired uid, pinned
#: literally rather than derived, so a yaml reordering that put an
#: alternative first -- silently turning the negative control below into a
#: no-op -- fails here instead of passing vacuously.
AUTHORED_MARKERS_1935: dict[str, dict[str, str]] = {
    "person_not_repo": {"person-rowan-wrenfield": "pricing committee"},
    "former_client_not_current": {"client-alderway": "12-person strategy consultancy"},
    "driftgate_migration_funding": {
        "project-driftgate-migration": "92 thousand GBP per quarter",
        "project-driftgate-migration-status": "92 thousand GBP per quarter",
    },
    "dual_signoff_threshold": {
        "policy-dual-signoff": "6400 GBP requires two independent approvers",
        "policy-dual-signoff-brief": "6400 GBP requires two independent approvers",
    },
    "mira_castellane_role": {
        "person-mira-castellane": "a role she has held since March 2019",
        "person-mira-castellane-directory": "a role she has held since March 2019",
    },
    "lighthouse_migration_rollback": {
        "project-lighthouse-migration": "unresolved data-integrity tickets"
    },
    "aggregation_retainer_clients": {
        "client-alderway": "12-person strategy consultancy",
        "client-bluewater": "coastal logistics operator",
        "client-atlas": "haulage operator",
        "client-larchmoor": "fabric wholesaler",
        "client-dunmere": "data-services firm",
        "client-hollowick": "structurally identical to Atlas Freight's arrangement",
        "client-castleford": "board-level advisory",
    },
    "aggregation_inhouse_tools": {
        "tool-buildpipe": "before anything ships",
        "tool-ledgerscribe": "invoicing tool",
        "tool-shiftboard": "spreadsheet-based rota",
        "tool-formready": "own templates",
        "tool-auditline": "which client record and when",
        "tool-rosterkeep": "worked which engagement over time",
    },
}

#: One INVENTED answer per repaired probe, written to state the fact the
#: probe's own `query` asks for in a voicing the authored marker above does
#: not contain. Each is the shape athenaeum#1932's blind labeller called
#: `marker_broken_fact_present`: right answer, wrong substring.
REWORDED_ANSWERS_1935: dict[str, str] = {
    # Authored marker names the committee Rowan sits on; the query asks what
    # he DECIDED. This answer gives the decision and never says "committee".
    "person_not_repo": (
        "Rowan Wrenfield's call on pricing was to hold the day rate flat for the 2026 "
        "financial year and absorb the indirect-cost rise rather than bill it on to "
        "retainer accounts."
    ),
    # Authored marker is Alderway's headcount and sector -- a detail the
    # query ("which clients are currently on retainer?") never asks for.
    "former_client_not_current": (
        "Currently on retainer: Alderway Advisory, which is the firm's largest retainer "
        "client by revenue, plus Bluewater Marine and Atlas Freight. Verity and Solent "
        "are former engagements, not current ones."
    ),
    # Reports the figure, then the cadence in the query's own words, so the
    # authored "... per quarter" run does not appear contiguously.
    "driftgate_migration_funding": (
        "Driftgate Migration is funded at 92 thousand GBP each quarter. Both the "
        "programme page and the independently written status note give that figure."
    ),
    # States the requirement before the threshold, and writes the threshold
    # with a thousands separator and a currency symbol.
    "dual_signoff_threshold": (
        "The Harrowmere Protocol requires two independent approvers before any payment "
        "above GBP 6,400 is released; a single approver suffices below that figure."
    ),
    # Re-voices the body's appositive ("a role she has held") as a verb.
    "mira_castellane_role": (
        "Mira Castellane has led the Rivencourt practice since March 2019, reporting "
        "directly to the managing partner."
    ),
    # Answers the rollback question itself; the authored marker is the
    # decommission gate, and is hyphenated where an answer need not be.
    "lighthouse_migration_rollback": (
        "If a rollback is triggered during cutover, reads move back to the legacy system "
        "immediately and the new stack is treated as the system under investigation. The "
        "team budgets four hours from trigger to full fail back."
    ),
    # Answers "which clients" with the clients' names -- which is what the
    # query asks -- and none of the seven authored descriptors.
    "aggregation_retainer_clients": (
        "The clients on a standing retainer are Alderway Advisory, Bluewater Marine, "
        "Atlas Freight, Larchmoor Textiles, Dunmere Analytics, Hollowick Freight and "
        "Castleford Group."
    ),
    # Names each tool and what it IS. "invoicing tool" is Ledgerscribe's
    # authored marker and is deliberately still here: athenaeum#1935 left
    # that one page unrepaired because its marker was already minimal.
    "aggregation_inhouse_tools": (
        "Six tools are built and run in house: buildpipe, the firm's internal build and "
        "release pipeline; Ledgerscribe, its in-house invoicing tool; Shiftboard, a "
        "staffing and scheduling tool; Formready, an in-house proposal generator; "
        "Auditline, which records who accessed which client record; and Rosterkeep, "
        "which tracks who has worked which engagement."
    ),
}


def _core_corpus() -> Corpus:
    return build_corpus(scale="core")


def _authored_only(corpus: Corpus) -> Corpus:
    """*corpus* with every athenaeum#1935 alternative stripped back to the
    authored marker -- the pre-repair ground truth, used as the negative
    control. Built by filtering against :data:`AUTHORED_MARKERS_1935` rather
    than by "keep the first entry", so it cannot silently degrade into a
    no-op if the yaml is reordered."""
    probes = []
    for probe in corpus.probes:
        authored = AUTHORED_MARKERS_1935.get(probe.id)
        if authored is None:
            probes.append(probe)
            continue
        kept = tuple(
            (uid, marker) for uid, marker in probe.answer_markers if authored.get(uid) == marker
        )
        assert set(dict(kept)) == set(authored), (
            f"{probe.id}: authored markers no longer present in probes.yaml -- "
            "AUTHORED_MARKERS_1935 is stale, so the negative control would be a no-op"
        )
        probes.append(dataclasses.replace(probe, answer_markers=kept))
    return dataclasses.replace(corpus, probes=tuple(probes))


def _repaired_record(probe: Probe, answer: str) -> RolloutRecord:
    """A ``PUSH_BREADCRUMB_PULL`` cell for *probe* whose every expected page
    is DELIVERED (so clause (b) is satisfied and the only thing under test is
    the marker conjunct, clause (a)), carrying *answer*.

    Delivery rides the PULL half, via the ``**Uid:**`` field
    ``uids_from_recall_output`` parses -- not the breadcrumb, whose real hook
    emits at most three bullets and so cannot deliver a seven-page
    aggregation probe at all."""
    recall_text = "\n".join(f"**Uid:** {uid}" for uid in probe.expected_uids)
    return RolloutRecord(
        arm=Arm.PUSH_BREADCRUMB_PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale="core",
        answer=answer,
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=10, output_tokens=10)],
        turn_count=1,
        transcript=[
            {"pushed_context": ""},
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": recall_text}]},
            },
        ],
    )


def _reworded_store(tmp_path: Path, corpus: Corpus) -> Path:
    probes_by_id = {p.id: p for p in corpus.probes}
    rows = [
        _repaired_record(probes_by_id[pid], answer)
        for pid, answer in sorted(REWORDED_ANSWERS_1935.items())
    ]
    path = tmp_path / "reworded-1935.jsonl"
    _write_store(path, rows)
    return path


def test_reworded_answers_miss_the_authored_markers_athenaeum_1935() -> None:
    """The negative control, asserted directly on the strings: each invented
    answer must NOT contain the authored marker it is meant to miss.

    Without this the main test below could pass because the answers happen to
    quote the authored marker anyway, proving nothing about the alternatives
    (the "recorded negative control that is a no-op" failure). Ledgerscribe's
    marker is the one deliberate exception -- athenaeum#1935 left that page
    unrepaired, so its authored marker is expected to be present."""
    for pid, answer in REWORDED_ANSWERS_1935.items():
        normalized = _normalize_marker_for_match(answer)
        for uid, marker in AUTHORED_MARKERS_1935[pid].items():
            if uid == "tool-ledgerscribe":
                assert _normalize_marker_for_match(marker) in normalized
                continue
            assert _normalize_marker_for_match(marker) not in normalized, (
                f"{pid}/{uid}: the reworded answer still contains the authored marker "
                f"{marker!r}, so it is not a test of the athenaeum#1935 alternative"
            )


def test_repaired_probes_grade_correct_under_the_repair_athenaeum_1935(tmp_path: Path) -> None:
    """The eight repaired probes grade CORRECT on the reworded answers under
    today's `probes.yaml`, and INCORRECT under the pre-repair marker set --
    re-graded by this module over one synthetic store and two corpora.

    This is the paired re-grade athenaeum#1935 asks for, with the pairing on
    the GRADING RULE rather than on the run: identical stored rows, graded
    before and after the marker repair, which is exactly the comparison
    ``GRADER_REVISION`` exists to keep legible (`corpus_digest` digests pages
    only, so a probes.yaml-only change moves no other header field)."""
    repaired_corpus = _core_corpus()
    control_corpus = _authored_only(repaired_corpus)
    store = _reworded_store(tmp_path, repaired_corpus)

    before = grade_store(store, control_corpus)
    after = grade_store(store, repaired_corpus)

    expected = set(REWORDED_ANSWERS_1935)
    assert set(before) == expected
    assert set(after) == expected

    # Pinned counts. Before the repair every one of the eight is a
    # marker-miss-with-delivery -- delivered, fact present, substring
    # missed. After it, every one grades correct and no miss is left.
    before_summary = summarize(before)
    assert (
        before_summary.n_graded,
        before_summary.correct,
        before_summary.marker_miss_with_delivery,
        before_summary.delivery_gap,
    ) == (8, 0, 8, 0)
    after_summary = summarize(after)
    assert (
        after_summary.n_graded,
        after_summary.correct,
        after_summary.marker_miss_with_delivery,
        after_summary.delivery_gap,
    ) == (8, 8, 0, 0)

    # Paired: all eight flip incorrect-to-correct, none the other way.
    discord = discordant_pairs(before, after)
    assert discord.paired_n == 8
    assert discord.correct_to_incorrect == ()
    assert set(discord.incorrect_to_correct) == expected

    # And the classification scope empties out: there is no marker-miss cell
    # left on the repaired side to classify.
    assert set(miss_and_discordant_cells(before, after)) == expected
    assert {pid for pid, c in after.items() if c.marker_miss_with_delivery} == set()


def test_remote_equipment_stipend_cap_is_left_unrepaired_athenaeum_1935() -> None:
    """athenaeum#1935 deliberately did NOT repair this probe: athenaeum#1932's
    two independent blind passes disagree about whether its 2026-09-19 answer
    carried the asked fact (`genuine_omission` from the five-way classifier,
    "asked fact present" from the separate asked-fact pass). A repair chosen
    while the ground truth is contested would be a guess, so the probe keeps
    its single authored marker until the disagreement is settled."""
    corpus = _core_corpus()
    probe = next(p for p in corpus.probes if p.id == "remote_equipment_stipend_cap")
    assert probe.answer_markers == (
        ("policy-remote-equipment-stipend", "visible to each employee in the expense system"),
    )
