# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the north-star grid's bounded concurrency (issue athenaeum#1751).

Four properties, one per acceptance criterion:

1. A grid run at ``--workers 4`` persists the SAME SET of rows as the same
   grid at ``--workers 1`` — concurrency changes throughput and row ORDER,
   never which cells were run or what they were keyed by.
2. The rollout token ceiling stops EVERY worker, not merely the one that
   noticed it: strictly fewer groups reach ``run_probe_all_arms`` than the
   grid planned, and the run still reports itself aborted.
3. ``--dry-run`` prints a projected wall clock for the chosen worker count,
   including on the over-budget refusal path — which is exactly the path an
   operator sizing a full dispatch is on.
4. A store holding fewer rows than its planned-count sidecar renders the
   ``partial: N of M cells`` banner; a complete one renders without it.

``rollout``-marked (imports ``tests.evals.rollout``) and fully offline: every
cell runs through a stub, no client is ever constructed and no ``claude -p``
is ever spawned.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.evals import north_star_cli
from tests.evals.containment import (
    GridCell,
    ResultStore,
    format_duration,
    planned_sidecar_path,
    project_wall_clock_seconds,
    read_planned_cells,
    run_grid,
    write_planned_cells,
)
from tests.evals.harness import EvalSession
from tests.evals.north_star_report import build_report, render_decision_block
from tests.evals.rollout import ALL_ARMS, RolloutRecord, TurnTokenUsage

pytestmark = pytest.mark.rollout

#: ``--scale small`` selects 3 probes x 1 corpus scale x 1 replicate (the
#: ``--replicates`` default is the single index 0) = 3 groups, each expanding
#: to every arm. More than one group is what makes "fewer groups ran than
#: planned" a meaningful assertion at 2 workers.
SMALL_SCALE_GROUPS = 3

#: Comfortably above ``small``'s priced total, so these tests exercise the
#: concurrency path rather than re-testing the pre-flight refusal.
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


def _cell_keys(store_path: Path) -> set[str]:
    return {
        json.loads(line)["cell_key"]
        for line in store_path.read_text(encoding="utf-8").strip().splitlines()
    }


# ---------------------------------------------------------------------------
# AC1a: --workers 4 == --workers 1, as a set
# ---------------------------------------------------------------------------


def test_four_workers_persist_the_same_row_set_as_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row SET is the contract; the row ORDER explicitly is not (see
    ``containment.run_grid``'s own docstring). Comparing sets is therefore
    not a weaker assertion than comparing lists — it is the right one."""

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
        return _stub_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub)

    assert north_star_cli.main(_small_grid_args(tmp_path, workers=1)) == 0
    assert north_star_cli.main(_small_grid_args(tmp_path, workers=4)) == 0

    serial = _cell_keys(tmp_path / "store-1.jsonl")
    parallel = _cell_keys(tmp_path / "store-4.jsonl")
    assert parallel == serial
    assert len(serial) == SMALL_SCALE_GROUPS * len(ALL_ARMS)
    # No duplicate or dropped rows. Note this does NOT exercise the store's
    # append lock: ``_run_cells`` appends under its own ledger lock, so the
    # store lock is belt-and-braces for that caller. It earns its keep for
    # ``run_grid(workers>1)``, where appends really do come off several
    # threads, and as the guarantee for any future caller that appends
    # from a worker directly.
    parallel_lines = (tmp_path / "store-4.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(parallel_lines) == len(parallel)


def test_concurrent_groups_never_share_a_materialize_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two groups running at once must not write into the same corpus tree.

    ``run_probe_all_arms`` materialises a wiki, builds a search index and
    seeds a hook ``HOME`` under the root it is given; sharing one between
    concurrent workers is a write-write race, not merely wasteful. Asserted
    by holding every in-flight root and checking for an overlap at entry —
    which catches a slot pool that hands the same slot out twice.
    """
    in_flight: set[str] = set()
    overlaps: list[str] = []
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
        root = str(materialize_root)
        with guard:
            if root in in_flight:
                overlaps.append(root)
            in_flight.add(root)
        # Hold the root long enough that the sibling worker is certainly
        # inside its own group at the same time, so a shared root would be
        # observed rather than missed by luck.
        time.sleep(0.05)
        with guard:
            in_flight.discard(root)
        return _stub_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub)
    assert north_star_cli.main(_small_grid_args(tmp_path, workers=2)) == 0
    assert overlaps == []


# ---------------------------------------------------------------------------
# AC1b: the spend ceiling stops ALL workers
# ---------------------------------------------------------------------------


def test_token_ceiling_stops_every_worker_mid_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aborting is only half the property.

    A ceiling merely asserted at the END of the run would also make this
    test's exit code 1 while every cell still ran and every cell was still
    paid for. The load-bearing assertion is the second one: strictly fewer
    groups reached the runner than the grid planned.
    """
    calls: list[str] = []
    guard = threading.Lock()

    def _burning_stub(
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
            calls.append(probe_id)
        session.observe_response(
            model,
            SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=1000)),
        )
        return _stub_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _burning_stub)
    # One group's worth of usage (2000 tokens) already exceeds this.
    monkeypatch.setattr(north_star_cli, "ROLLOUT_TOKEN_CEILING", 100)

    exit_code = north_star_cli.main(_small_grid_args(tmp_path, workers=2))

    assert exit_code == 1
    assert 0 < len(calls) < SMALL_SCALE_GROUPS
    # At 2 workers at most 2 groups can be past the stop check when the
    # first one trips it, so this is a deterministic bound, not a race.
    assert len(calls) <= 2


def test_rejects_a_worker_count_below_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _exploding(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an invalid --workers must never run a cell")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    assert north_star_cli.main(["--workers", "0"]) == 1
    assert "--workers must be >= 1" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AC2: --dry-run projects wall clock, including on the refusal path
# ---------------------------------------------------------------------------


def test_dry_run_prints_projected_wall_clock_for_the_worker_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    # `full` has far more groups than workers, so the requested count is the
    # effective one -- `smoke` would clamp to its single group (see
    # test_north_star_partial_safety.py's clamp test).
    exit_code = north_star_cli.main(
        ["--scale", "full", "--max-spend", GENEROUS_MAX_SPEND, "--dry-run", "--workers", "8"]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "projected wall clock:" in out
    assert "at 8 workers" in out


def test_dry_run_projects_wall_clock_even_when_the_grid_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The issue's own repro command.

    ``--scale full --dry-run`` exceeds the default ``--max-spend``, so the
    price gate refuses before the summary line is ever reached. That is the
    single most important place for the projection to appear: sizing a full
    dispatch against the job's ``timeout-minutes`` is precisely why an
    operator runs this command.
    """
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(["--scale", "full", "--dry-run"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "refusing to start" in captured.err
    assert "projected wall clock:" in captured.out
    assert f"at {north_star_cli.DEFAULT_WORKERS} workers" in captured.out


def test_projection_scales_inversely_with_the_worker_count() -> None:
    one = project_wall_clock_seconds(1392, workers=1)
    four = project_wall_clock_seconds(1392, workers=4)
    assert four == pytest.approx(one / 4)
    assert format_duration(3661) == "1:01:01"
    with pytest.raises(ValueError):
        project_wall_clock_seconds(10, workers=0)


# ---------------------------------------------------------------------------
# AC3: the partial banner
# ---------------------------------------------------------------------------


def _row_bearing_report(row_count: int, planned: int | None):
    """A report over *row_count* rows keyed by REAL corpus probe ids --
    ``build_report`` resolves every row's probe against the corpus, so
    invented ids raise rather than rendering."""
    from tests.evals.north_star_report import RolloutRow

    probe_ids = north_star_cli.DEFAULT_PROBES[:row_count]
    assert len(probe_ids) == row_count, "corpus has fewer probes than the test asks for"
    rows = [
        RolloutRow(
            cell=GridCell(probe=probe_id, arm="none", corpus_scale="core", replicate=0),
            record=_stub_records(probe_id, "core")["none"],
        )
        for probe_id in probe_ids
    ]
    return build_report(rows, planned_cells=planned)


def test_partial_store_renders_the_banner() -> None:
    block = "\n".join(render_decision_block(_row_bearing_report(3, planned=8)))
    assert "partial: 3 of 8 cells" in block


def test_complete_store_renders_no_banner() -> None:
    block = "\n".join(render_decision_block(_row_bearing_report(8, planned=8)))
    assert "partial:" not in block


def test_unknown_planned_count_renders_no_banner() -> None:
    """A store with no sidecar must not be libelled as partial."""
    block = "\n".join(render_decision_block(_row_bearing_report(3, planned=None)))
    assert "partial:" not in block


def test_planned_sidecar_round_trips_and_degrades_to_none(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "nested" / "results.jsonl")
    assert read_planned_cells(store) is None

    write_planned_cells(store, 1392)
    assert read_planned_cells(store) == 1392

    planned_sidecar_path(store).write_text("not json at all", encoding="utf-8")
    assert read_planned_cells(store) is None


def test_a_run_writes_the_planned_sidecar_before_any_cell_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int | None] = []

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
        seen.append(read_planned_cells(ResultStore(tmp_path / "store-1.jsonl")))
        return _stub_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub)
    assert north_star_cli.main(_small_grid_args(tmp_path, workers=1)) == 0

    expected = SMALL_SCALE_GROUPS * len(ALL_ARMS)
    assert seen and all(value == expected for value in seen)


# ---------------------------------------------------------------------------
# The generic cell loop underneath
# ---------------------------------------------------------------------------


def test_run_grid_workers_run_the_same_cell_set_as_serial(tmp_path: Path) -> None:
    cells = [
        GridCell(probe=f"p{i}", arm="none", corpus_scale="core", replicate=0) for i in range(12)
    ]

    def _runner(cell: GridCell) -> dict[str, Any]:
        return {"probe": cell.probe}

    serial_store = ResultStore(tmp_path / "serial.jsonl")
    parallel_store = ResultStore(tmp_path / "parallel.jsonl")
    run_grid(cells, _runner, serial_store, workers=1)
    run_grid(cells, _runner, parallel_store, workers=4)

    assert _cell_keys(parallel_store.path) == _cell_keys(serial_store.path)
    assert len(parallel_store.completed_keys()) == len(cells)

    # Resume still skips everything already persisted, concurrently too.
    def _never_called(cell: GridCell) -> dict[str, Any]:
        raise AssertionError("a completed cell must never be re-run")

    assert run_grid(cells, _never_called, parallel_store, workers=4) == []

    with pytest.raises(ValueError):
        run_grid(cells, _runner, parallel_store, workers=0)


def test_eval_session_token_counters_survive_concurrent_updates() -> None:
    """Pins that the counters are EXACT under concurrent load.

    Honest about what it can and cannot do: the ledger's ``+=`` is several
    bytecodes and a lost update would undercount tokens (silently loosening
    the ceiling that spends against them), but under CPython's GIL an
    unlocked version of this would only fail probabilistically. So this is
    not a falsifying test for "the lock is necessary" -- it is a regression
    pin on "the totals are exact", which is the property callers depend on
    and which any future rewrite (a lock-free accumulator, a free-threaded
    build) must keep.
    """
    session = EvalSession()
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=3, output_tokens=5))
    threads = [
        threading.Thread(
            target=lambda: [session.observe_response("m", response) for _ in range(500)]
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert session.input_tokens == 8 * 500 * 3
    assert session.output_tokens == 8 * 500 * 5
    assert session.per_model["m"]["input_tokens"] == 8 * 500 * 3
