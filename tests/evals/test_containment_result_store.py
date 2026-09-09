# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the append-only result store (issue athenaeum#1521 AC2).

UNMARKED — no network, no credential. Simulates a mid-grid kill
DETERMINISTICALLY (raising from a cell callable, per the issue's own
suggested technique) and proves the resume executes each remaining cell
EXACTLY ONCE, verified against an execution-count spy — never re-running a
cell whose result the store already has.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests.evals.containment import GridCell, ResultStore, run_grid

GRID = [
    GridCell(probe=f"p{i}", arm="control", corpus_scale="core", replicate=0) for i in range(5)
]


class _SimulatedKill(Exception):
    """Raised from a cell callable to simulate a hard kill mid-grid."""


class _CountingRunner:
    """Records every cell it was actually asked to run, and how many times."""

    def __init__(self, calls: Counter[str], *, kill_after: int | None = None) -> None:
        self.calls = calls
        self.kill_after = kill_after
        self.invocations = 0

    def __call__(self, cell: GridCell) -> dict[str, Any]:
        if self.kill_after is not None and self.invocations >= self.kill_after:
            raise _SimulatedKill(f"simulated kill before cell {cell.cell_key()}")
        self.invocations += 1
        self.calls[cell.cell_key()] += 1
        return {"probe": cell.probe, "arm": cell.arm}


def test_resume_does_not_repay_for_completed_cells(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    calls: Counter[str] = Counter()

    # First run: kill after 2 cells have genuinely completed.
    first_runner = _CountingRunner(calls, kill_after=2)
    with pytest.raises(_SimulatedKill):
        run_grid(GRID, first_runner, store)
    assert first_runner.invocations == 2
    assert store.completed_keys() == {GRID[0].cell_key(), GRID[1].cell_key()}

    # Resume: a FRESH runner/spy over the SAME store file.
    second_runner = _CountingRunner(calls, kill_after=None)
    results = run_grid(GRID, second_runner, store)

    # The fresh spy must only have been asked to run the 3 remaining cells.
    assert second_runner.invocations == 3
    assert len(results) == 3

    # Cross-run proof: every cell executed EXACTLY once across BOTH runs --
    # not on an output comparison, on the execution-count spy itself.
    assert calls == Counter({cell.cell_key(): 1 for cell in GRID})

    # The store now has every cell, no duplicate rows.
    assert store.completed_keys() == {cell.cell_key() for cell in GRID}
    lines = store.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5
    keys_in_file = [json.loads(line)["cell_key"] for line in lines]
    assert len(keys_in_file) == len(set(keys_in_file)) == 5


def test_kill_before_any_cell_completes_resumes_from_scratch(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    calls: Counter[str] = Counter()

    first_runner = _CountingRunner(calls, kill_after=0)
    with pytest.raises(_SimulatedKill):
        run_grid(GRID, first_runner, store)
    assert first_runner.invocations == 0
    assert store.completed_keys() == set()

    second_runner = _CountingRunner(calls, kill_after=None)
    results = run_grid(GRID, second_runner, store)
    assert second_runner.invocations == 5
    assert len(results) == 5


def test_store_never_rewrites_earlier_rows(tmp_path: Path) -> None:
    """Append-only means append-only: an earlier row's bytes must be
    byte-identical after a later append, never rewritten in place."""
    store = ResultStore(tmp_path / "results.jsonl")
    store.append(GRID[0].cell_key(), {"n": 1})
    first_line = store.path.read_text(encoding="utf-8").splitlines()[0]

    store.append(GRID[1].cell_key(), {"n": 2})
    lines_after = store.path.read_text(encoding="utf-8").splitlines()
    assert lines_after[0] == first_line
    assert len(lines_after) == 2


def test_completed_keys_on_missing_store_is_empty(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "does-not-exist.jsonl")
    assert store.completed_keys() == set()


def test_run_grid_skips_zero_cell_runner_calls_when_fully_complete(tmp_path: Path) -> None:
    """A grid that is ALREADY fully persisted must not invoke the runner at
    all on the next call -- the strongest form of "does not repay"."""
    store = ResultStore(tmp_path / "results.jsonl")
    calls: Counter[str] = Counter()
    run_grid(GRID, _CountingRunner(calls), store)
    assert sum(calls.values()) == 5

    never_called = _CountingRunner(Counter())
    results = run_grid(GRID, never_called, store)
    assert never_called.invocations == 0
    assert results == []
