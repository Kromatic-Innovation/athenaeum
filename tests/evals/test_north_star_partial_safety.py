# SPDX-License-Identifier: Apache-2.0
"""Partial-run safety and `main()`-level wiring for the grid (issue athenaeum#1751).

Companion to ``test_north_star_concurrency.py``, which proves the
concurrency properties in isolation. This module proves the things that
only hold END TO END, plus the ways a run can stop that are not an
ordinary exception:

* `main()` really reaches concurrency (a peak-in-flight measurement, so
  dropping `workers=args.workers` from the `_run_cells` call fails here and
  nowhere else) and really threads the planned count into the WRITTEN
  report (asserted against `write_report`'s file, so dropping
  `planned_cells=read_planned_cells(store)` fails here too).
* A torn trailing JSONL row — what a kill mid-``write`` leaves, and rollout
  rows are hundreds of KB — is skipped, counted, surfaced, and survives the
  resume that puts it mid-file.
* `KeyboardInterrupt` still produces a PARTIAL report and cancels groups
  that had not started.
* The spend ceiling stops an in-flight group at its next ARM rather than
  running out all eight.

**Deliberately unmarked** (issue athenaeum#1742): every cell here runs
through a stub or ``tests.conftest.FakeLLMClient``, no live LLM client is
constructed, no ``claude`` binary is spawned and nothing gates on
``ANTHROPIC_API_KEY`` -- so this module costs no tokens and must run in
``ci.yml``'s default job. ``rollout`` is a TOKEN-COST marker, not a
file-family one.
"""

from __future__ import annotations

import dataclasses
import inspect
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage
from tests.evals import north_star_cli
from tests.evals.containment import (
    GridCell,
    ResultStore,
    SpendCeilingExceededError,
    read_planned_cells,
    run_grid,
)
from tests.evals.harness import EvalSession
from tests.evals.north_star_report import (
    append_rollout_row,
    build_report,
    load_rollout_rows_and_diagnostics,
    render_decision_block,
)
from tests.evals.rollout import ALL_ARMS, RolloutRecord, TurnTokenUsage, run_probe_all_arms

#: ``--scale small`` with the default single replicate selects three
#: (probe, corpus_scale, replicate) groups.
SMALL_SCALE_GROUPS = 3
GENEROUS_MAX_SPEND = "100.0"


def _stub_records(probe_id: str, corpus_scale: str) -> dict[str, RolloutRecord]:
    return {
        arm.value: RolloutRecord(
            arm=arm,
            probe_id=probe_id,
            probe_class="single_hop",
            corpus_scale=corpus_scale,
            answer=f"stub answer for {arm.value}",
            turn_tokens=[TurnTokenUsage(turn=1, input_tokens=10, output_tokens=5)],
            turn_count=1,
            transcript=[{"answer": f"stub answer for {arm.value}"}],
        )
        for arm in ALL_ARMS
    }


def _plain_stub(
    probe_id: str,
    corpus_scale: str,
    *,
    session: Any,
    materialize_root: Any,
    model: str,
    search_backend: str,
    claude_binary: str,
    replicate: int,
    mode: str = "cli",
    should_stop: Any = None,
) -> dict[str, RolloutRecord]:
    return _stub_records(probe_id, corpus_scale)


def _small_grid_args(tmp_path: Path, *, workers: int) -> list[str]:
    return [
        "--scale",
        "small",
        "--max-spend",
        GENEROUS_MAX_SPEND,
        "--workers",
        str(workers),
        "--materialize-root",
        str(tmp_path / f"mat-{workers}"),
        "--out-dir",
        str(tmp_path / f"measurements-{workers}"),
        "--store",
        str(tmp_path / f"store-{workers}.jsonl"),
    ]


def _sole_report_text(out_dir: Path) -> str:
    reports = list(out_dir.glob("north-star-*.md"))
    assert len(reports) == 1, f"expected exactly one report in {out_dir}, got {reports}"
    return reports[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# main() really reaches concurrency
# ---------------------------------------------------------------------------


def test_main_actually_runs_groups_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observes CONCURRENCY, not merely a ``--workers`` flag that parses.

    Deleting ``workers=args.workers`` from ``main``'s ``_run_cells`` call
    leaves every other test green -- the row set, the store and the report
    are all identical at one worker. Only a peak-in-flight measurement
    fails, which is why this measures that rather than counting calls.
    """
    in_flight = 0
    peak = 0
    guard = threading.Lock()

    def _stub(
        probe_id: str,
        corpus_scale: str,
        *,
        session: Any,
        materialize_root: Any,
        model: str,
        search_backend: str,
        claude_binary: str,
        replicate: int,
        mode: str = "cli",
        should_stop: Any = None,
    ) -> dict[str, RolloutRecord]:
        nonlocal in_flight, peak
        with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.05)
        with guard:
            in_flight -= 1
        return _stub_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub)
    assert north_star_cli.main(_small_grid_args(tmp_path, workers=2)) == 0
    assert peak >= 2, f"never had two groups in flight (peak {peak}) -- the run is serial"


# ---------------------------------------------------------------------------
# main() threads the planned count into the WRITTEN report
# ---------------------------------------------------------------------------


def test_main_writes_a_report_carrying_the_partial_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, against the file ``write_report`` produced.

    Asserting on ``render_decision_block``'s return value would still pass
    with ``planned_cells=read_planned_cells(store)`` deleted from ``main``,
    because that argument is exactly what carries the denominator across
    the ``main`` -> ``build_report`` boundary. Only the written report
    proves the whole chain.
    """
    planned = SMALL_SCALE_GROUPS * len(ALL_ARMS)

    # A complete run renders no banner.
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _plain_stub)
    assert north_star_cli.main(_small_grid_args(tmp_path, workers=1)) == 0
    assert read_planned_cells(ResultStore(tmp_path / "store-1.jsonl")) == planned
    assert "partial:" not in _sole_report_text(tmp_path / "measurements-1")

    # A run that dies part-way does. ONE ``main`` call, so the whole chain
    # is under test: sidecar written at start -> groups persisted as they go
    # -> failure -> report rendered over exactly what survived.
    done: list[str] = []
    guard = threading.Lock()

    def _dying_stub(
        probe_id: str,
        corpus_scale: str,
        *,
        session: Any,
        materialize_root: Any,
        model: str,
        search_backend: str,
        claude_binary: str,
        replicate: int,
        mode: str = "cli",
        should_stop: Any = None,
    ) -> dict[str, RolloutRecord]:
        with guard:
            if len(done) >= SMALL_SCALE_GROUPS - 1:
                raise RuntimeError("simulated mid-grid failure")
            done.append(probe_id)
        return _stub_records(probe_id, corpus_scale)

    # A FRESH store: resuming the completed one above would rerun nothing
    # and so could never go partial.
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _dying_stub)
    exit_code = north_star_cli.main(
        [
            "--scale",
            "small",
            "--max-spend",
            GENEROUS_MAX_SPEND,
            "--workers",
            "1",
            "--materialize-root",
            str(tmp_path / "mat-dying"),
            "--out-dir",
            str(tmp_path / "measurements-dying"),
            "--store",
            str(tmp_path / "store-dying.jsonl"),
        ]
    )

    assert exit_code == 1
    survived = (SMALL_SCALE_GROUPS - 1) * len(ALL_ARMS)
    text = _sole_report_text(tmp_path / "measurements-dying")
    assert f"partial: {survived} of {planned} cells" in text
    assert "simulated mid-grid failure" in text


# ---------------------------------------------------------------------------
# A torn trailing row is survived, counted and surfaced
# ---------------------------------------------------------------------------


def test_a_torn_trailing_row_is_skipped_counted_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill mid-``write`` leaves a partial line, and a rollout row is
    hundreds of KB, so the window is real. The row must not be fatal, must
    NOT count as completed (or a resume would skip a cell that was never
    persisted), and must be visible in the report rather than dropped."""
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _plain_stub)
    assert north_star_cli.main(_small_grid_args(tmp_path, workers=1)) == 0

    store_path = tmp_path / "store-1.jsonl"
    store = ResultStore(store_path)
    intact = store.completed_keys()
    lines = store_path.read_text(encoding="utf-8").splitlines()
    torn_tail = lines[-1][: len(lines[-1]) // 2]
    store_path.write_text("\n".join(lines[:-1] + [torn_tail]), encoding="utf-8")

    assert store.count_torn_rows() == 1
    assert len(store.completed_keys()) == len(intact) - 1

    diagnostics = load_rollout_rows_and_diagnostics(store)
    assert diagnostics.torn == 1
    assert diagnostics.duplicates == 0
    assert len(diagnostics.rows) == len(intact) - 1

    block = "\n".join(
        render_decision_block(
            build_report(
                list(diagnostics.rows),
                planned_cells=len(intact),
                torn_rows=diagnostics.torn,
            )
        )
    )
    assert "1 torn row" in block


def test_a_torn_row_in_the_middle_survives_a_resume(tmp_path: Path) -> None:
    """A resume appends AFTER the torn tail, which puts the torn line in the
    MIDDLE of the file.

    The workflow now passes a stable ``--store`` precisely so a
    timeout-killed run can be resumed, so this is a path this issue
    creates. A tolerance narrowed to "the last line only" would start
    raising on exactly the store it was written to rescue.
    """
    store = ResultStore(tmp_path / "s.jsonl")
    store.append('["a","none","core",0]', {"probe_id": "a"})
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write('{"cell_key": "torn", "probe_i\n')
    store.append('["b","none","core",0]', {"probe_id": "b"})

    assert store.count_torn_rows() == 1
    assert store.completed_keys() == {'["a","none","core",0]', '["b","none","core",0]'}


def test_a_complete_store_reports_no_torn_rows(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "s.jsonl")
    cell = GridCell(probe="a", arm="none", corpus_scale="core", replicate=0)
    append_rollout_row(store, cell, _stub_records("a", "core")["none"])

    assert store.count_torn_rows() == 0
    diagnostics = load_rollout_rows_and_diagnostics(store)
    assert diagnostics.torn == 0
    assert diagnostics.duplicates == 0
    assert len(diagnostics.rows) == 1


# ---------------------------------------------------------------------------
# KeyboardInterrupt: PARTIAL report, queued groups cancelled
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_writes_a_partial_report_and_cancels_queued_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C is the most likely way a long grid ends early, and it is a
    ``BaseException`` -- the previous ``except Exception`` let it straight
    past, so the operator who stopped the run got no report at all over the
    cells already paid for."""
    started: list[str] = []
    guard = threading.Lock()

    def _interrupting_stub(
        probe_id: str,
        corpus_scale: str,
        *,
        session: Any,
        materialize_root: Any,
        model: str,
        search_backend: str,
        claude_binary: str,
        replicate: int,
        mode: str = "cli",
        should_stop: Any = None,
    ) -> dict[str, RolloutRecord]:
        with guard:
            started.append(probe_id)
            first = len(started) == 1
        if first:
            raise KeyboardInterrupt("simulated Ctrl-C")
        return _stub_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _interrupting_stub)

    exit_code = north_star_cli.main(_small_grid_args(tmp_path, workers=1))

    assert exit_code == 1
    assert len(started) < SMALL_SCALE_GROUPS, "queued groups ran instead of being cancelled"
    text = _sole_report_text(tmp_path / "measurements-1")
    assert "PARTIAL RUN" in text
    assert "simulated Ctrl-C" in text


def test_keyboard_interrupt_cancels_queued_groups_under_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    guard = threading.Lock()

    def _interrupting_stub(
        probe_id: str,
        corpus_scale: str,
        *,
        session: Any,
        materialize_root: Any,
        model: str,
        search_backend: str,
        claude_binary: str,
        replicate: int,
        mode: str = "cli",
        should_stop: Any = None,
    ) -> dict[str, RolloutRecord]:
        with guard:
            started.append(probe_id)
            first = len(started) == 1
        if first:
            raise KeyboardInterrupt("simulated Ctrl-C")
        # Every other group is slow, so the pool cannot quietly drain the
        # queue while the main thread is reacting to the interrupt.
        time.sleep(0.5)
        return _stub_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _interrupting_stub)

    # Two replicates x three probes = six groups, so there is a queue to
    # cancel. At two workers, only the interrupting group, its concurrent
    # sibling, and at most one group the freed worker picks up before the
    # main thread reacts can ever start -- the rest are cancelled.
    exit_code = north_star_cli.main(
        [
            "--scale",
            "small",
            "--replicates",
            "0,1",
            "--max-spend",
            GENEROUS_MAX_SPEND,
            "--workers",
            "2",
            "--materialize-root",
            str(tmp_path / "mat-int"),
            "--out-dir",
            str(tmp_path / "measurements-int"),
            "--store",
            str(tmp_path / "store-int.jsonl"),
        ]
    )

    assert exit_code == 1
    assert len(started) <= 3, f"queued groups ran instead of being cancelled: {started}"
    assert "PARTIAL RUN" in _sole_report_text(tmp_path / "measurements-int")


# ---------------------------------------------------------------------------
# The ceiling stops an in-flight group at its next ARM, not at its end
# ---------------------------------------------------------------------------


def test_should_stop_halts_a_group_between_arms(tmp_path: Path) -> None:
    """The between-arms check is what bounds the overshoot.

    Without it, a ceiling tripped on another worker's cell still lets every
    in-flight group run out all ``len(ALL_ARMS)`` of its arms -- up to eight
    cells of overshoot per worker. With it, the group stops at its next arm
    boundary, so the overshoot is the one arm already in flight.

    ``should_stop`` here goes True only from its THIRD call, so two arms
    genuinely run first: a check evaluated once before the loop (rather than
    per iteration) would report ``0 of 8`` and fail this.
    """
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response(
            "stub answer", usage=make_llm_usage(input_tokens=10, output_tokens=5)
        )
    )
    checks = 0

    def _stop_from_the_third_check() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(SpendCeilingExceededError) as excinfo:
        run_probe_all_arms(
            "pto_allowance",
            "core",
            session=session,
            materialize_root=tmp_path / "mat",
            search_backend="keyword",
            client=client,
            mode="cli",
            should_stop=_stop_from_the_third_check,
        )

    message = str(excinfo.value)
    assert f"after 2 of {len(ALL_ARMS)} arms" in message
    assert "spend ceiling" in message


def test_should_stop_never_consulted_is_a_full_group(tmp_path: Path) -> None:
    """``should_stop`` defaults to ``None``, so every pre-existing caller
    and every stub in this suite is unaffected by its addition."""
    assert inspect.signature(run_probe_all_arms).parameters["should_stop"].default is None


# ---------------------------------------------------------------------------
# The wall-clock projection is clamped the same way the pool is
# ---------------------------------------------------------------------------


def test_projection_clamps_workers_to_the_group_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``_run_cells`` clamps its pool to the number of groups, so a
    projection dividing by an unclamped worker count would promise a run far
    faster than anything that can happen. ``smoke`` is exactly one group."""
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    assert north_star_cli.main(["--scale", "smoke", "--dry-run", "--workers", "64"]) == 0

    out = capsys.readouterr().out
    assert "at 1 workers" in out, out


# ---------------------------------------------------------------------------
# Per-turn token deltas are exact under concurrency
# ---------------------------------------------------------------------------


def test_per_turn_token_deltas_are_exact_under_concurrency() -> None:
    """The per-TURN figure, not just the session total.

    ``_observe_turn`` used to take a before/after reading of the session's
    running totals around ``observe_response``. That pair spans a window in
    which another worker's response can land, and the subtraction then
    attributes the other worker's tokens to this turn -- corrupting the
    per-turn rows that every cost and efficiency dimension in the report is
    computed from, while the session total stayed correct and hid it.
    """
    from tests.evals.rollout import _observe_turn

    session = EvalSession()
    response = make_llm_response(
        "stub answer", usage=make_llm_usage(input_tokens=7, output_tokens=11)
    )
    seen: list[tuple[int, int]] = []
    seen_guard = threading.Lock()

    def _hammer() -> None:
        for turn in range(200):
            usage = _observe_turn(session, "m", response, turn=turn)
            with seen_guard:
                seen.append((usage.input_tokens, usage.output_tokens))

    threads = [threading.Thread(target=_hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert set(seen) == {(7, 11)}, "a turn was attributed another turn's tokens"
    assert session.input_tokens == 8 * 200 * 7
    assert session.output_tokens == 8 * 200 * 11


# ---------------------------------------------------------------------------
# A torn tail with NO trailing newline must not swallow the resumed row
# ---------------------------------------------------------------------------


def test_an_unterminated_torn_tail_does_not_glue_the_next_row_onto_it(
    tmp_path: Path,
) -> None:
    """The nastier half of a mid-``write`` kill.

    A process killed while writing leaves a fragment with NO trailing
    newline. Appending straight onto it glues the next row's JSON to the
    fragment, and the combined line decodes as neither: the fragment is
    expected and tolerated, but the NEW row -- a cell that just ran and was
    just paid for -- is destroyed with it. It is destroyed the same way on
    every later resume too, because each resume re-runs that cell and
    re-glues it to the same unterminated tail, so the cell can never
    persist at all.
    """
    store = ResultStore(tmp_path / "s.jsonl")
    store.append('["a","none","core",0]', {"probe_id": "a"})
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write('{"cell_key": "torn", "probe_i')  # NO trailing newline

    store.append('["b","none","core",0]', {"probe_id": "b"})

    # The fragment stays torn; the row appended after it survives intact.
    assert store.count_torn_rows() == 1
    assert store.completed_keys() == {'["a","none","core",0]', '["b","none","core",0]'}
    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3, lines


def test_resuming_a_grid_over_an_unterminated_tail_leaves_no_duplicate_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, at the shape a timeout kill actually leaves.

    Before the newline guard this produced 31 raw lines, 30 decodable rows
    and only 24 distinct cells -- six duplicates, one destroyed row, and no
    partial banner, because the banner compared ROW count (30) against the
    planned 24 and concluded the grid was complete.
    """
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _plain_stub)
    assert north_star_cli.main(_small_grid_args(tmp_path, workers=1)) == 0

    store_path = tmp_path / "store-1.jsonl"
    store = ResultStore(store_path)
    planned = SMALL_SCALE_GROUPS * len(ALL_ARMS)
    assert len(store.completed_keys()) == planned

    # Tear the final row and drop its newline, exactly as a kill would.
    lines = store_path.read_text(encoding="utf-8").splitlines()
    store_path.write_text(
        "\n".join(lines[:-1]) + "\n" + lines[-1][: len(lines[-1]) // 2], encoding="utf-8"
    )

    lines_before_resume = len(store_path.read_text(encoding="utf-8").splitlines())

    # Resume with the same store: the torn cell's group re-runs in full.
    assert north_star_cli.main(_small_grid_args(tmp_path, workers=1)) == 0

    # Every re-run row got its OWN line. Without the newline guard the first
    # of them fuses with the fragment and one line goes missing -- which is
    # the assertion that holds regardless of which row happened to land
    # first, unlike a distinct-cell count that the reader's de-duplication
    # can make look correct by luck.
    lines_after_resume = len(store_path.read_text(encoding="utf-8").splitlines())
    assert lines_after_resume == lines_before_resume + len(ALL_ARMS)

    diagnostics = load_rollout_rows_and_diagnostics(store)
    assert diagnostics.torn == 1
    assert len(store.completed_keys()) == planned
    keys = [row.cell.cell_key() for row in diagnostics.rows]
    assert len(keys) == len(set(keys)), "the reader returned duplicate cells"
    assert len(keys) == planned


def test_the_reader_collapses_a_resumed_group_to_one_row_per_cell(
    tmp_path: Path,
) -> None:
    """Duplicate cell keys are NORMAL after a resume, not corruption: resume
    granularity is the whole group, so a group that stopped part-way is
    re-run in full and its already-landed rows are appended again. The
    reader collapses them, last row winning, and counts what it collapsed."""
    store = ResultStore(tmp_path / "s.jsonl")
    # A REAL corpus probe id: ``build_report`` resolves every row's probe
    # against the corpus, so an invented id raises rather than rendering.
    probe_id = north_star_cli.DEFAULT_PROBES[0]
    cell = GridCell(probe=probe_id, arm="none", corpus_scale="core", replicate=0)
    first = _stub_records(probe_id, "core")["none"]
    second = dataclasses.replace(first, answer="the re-run's answer")
    append_rollout_row(store, cell, first)
    append_rollout_row(store, cell, second)

    diagnostics = load_rollout_rows_and_diagnostics(store)

    assert len(diagnostics.rows) == 1
    assert diagnostics.duplicates == 1
    assert diagnostics.torn == 0
    assert diagnostics.rows[0].record.answer == "the re-run's answer"

    # Distinct cells, not rows, decide the banner: two rows for one cell of a
    # planned two must still read as partial.
    block = "\n".join(
        render_decision_block(
            build_report(list(diagnostics.rows), planned_cells=2, duplicate_rows=1)
        )
    )
    assert "partial: 1 of 2 cells" in block
    assert "superseded row" in block


# ---------------------------------------------------------------------------
# should_stop is actually wired from main() through to run_probe_all_arms
# ---------------------------------------------------------------------------


def test_main_passes_a_should_stop_that_flips_when_the_ceiling_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting `should_stop=stop.is_set` from ``_run_cells`` must fail here.

    Every other ceiling test would still pass without it: the run still
    aborts and still stops dispatching NEW groups. What is lost is the
    bound on a group already in flight, which is only observable by holding
    the callable the runner was handed and watching it flip.
    """
    seen: list[Any] = []
    guard = threading.Lock()

    def _capturing_stub(
        probe_id: str,
        corpus_scale: str,
        *,
        session: EvalSession,
        materialize_root: Any,
        model: str,
        search_backend: str,
        claude_binary: str,
        replicate: int,
        mode: str = "cli",
        should_stop: Any = None,
    ) -> dict[str, RolloutRecord]:
        with guard:
            seen.append(should_stop)
        session.observe_response(
            model,
            make_llm_response("x", usage=make_llm_usage(input_tokens=1000, output_tokens=1000)),
        )
        return _stub_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _capturing_stub)
    # One group's usage already exceeds this.
    monkeypatch.setattr(north_star_cli, "ROLLOUT_TOKEN_CEILING", 100)

    assert north_star_cli.main(_small_grid_args(tmp_path, workers=1)) == 1

    assert seen, "no group ever ran"
    assert all(callable(cb) for cb in seen), (
        "run_probe_all_arms was handed should_stop=None -- an in-flight group "
        "would run out all its arms after the ceiling tripped"
    )
    # The callable is live, not a snapshot: it reads False before the trip
    # and True after it.
    assert all(cb() for cb in seen), "should_stop never flipped after the ceiling tripped"


# ---------------------------------------------------------------------------
# run_grid drains before it re-raises
# ---------------------------------------------------------------------------


def test_run_grid_appends_every_completed_cell_before_reraising(tmp_path: Path) -> None:
    """One cell raises; the rest complete. Their rows must all be persisted.

    Raising straight out of the ``as_completed`` loop would abandon every
    future that had completed but not yet been pulled -- cells that ran and
    were paid for, whose results never reach the store. That also breaks
    resume: the next run re-pays for work that had already succeeded.
    """
    cells = [
        GridCell(probe=f"p{i}", arm="none", corpus_scale="core", replicate=0) for i in range(12)
    ]
    doomed = "p7"

    def _runner(cell: GridCell) -> dict[str, Any]:
        if cell.probe == doomed:
            # Let the others get well ahead, so several futures are sitting
            # completed-but-unpulled when this one surfaces.
            time.sleep(0.2)
            raise RuntimeError("simulated cell failure")
        return {"probe": cell.probe}

    store = ResultStore(tmp_path / "s.jsonl")
    with pytest.raises(RuntimeError, match="simulated cell failure"):
        run_grid(cells, _runner, store, workers=4)

    persisted = store.completed_keys()
    expected = {cell.cell_key() for cell in cells if cell.probe != doomed}
    assert persisted == expected, "a completed cell's row was dropped when another cell raised"
    assert store.count_torn_rows() == 0
