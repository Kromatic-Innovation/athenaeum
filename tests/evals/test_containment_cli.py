# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the containment CLI (issue athenaeum#1521 AC3/AC4).

UNMARKED — no network, no credential; `_demo_cell_runner` never calls an
LLM. Proves a contributor can run `smoke` with zero flags and zero thought
about cost, and that `--max-spend` still refuses an over-budget `--scale
full` run through the same entrypoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.tiers import DEFAULT_WRITE_MODEL
from tests.evals import containment_cli
from tests.evals.containment import DEFAULT_CELL_TOKEN_ESTIMATE, price_grid


def test_default_max_spend_covers_smoke_scale() -> None:
    """Arithmetic proof that the CLI's own default --max-spend is never
    self-refusing at the default scale -- pinned as a TEST (not a bare
    assert in the CLI module) so a future rate-table change fails here
    loudly instead of the CLI silently refusing its own default."""
    from tests.evals.containment import build_grid

    grid = build_grid(
        "smoke",
        probes=containment_cli.EXAMPLE_PROBES,
        arms=containment_cli.EXAMPLE_ARMS,
        corpus_scales=containment_cli.EXAMPLE_CORPUS_SCALES,
        replicates=containment_cli.EXAMPLE_REPLICATES,
    )
    estimate = price_grid(
        grid,
        model=DEFAULT_WRITE_MODEL,
        max_spend_usd=containment_cli.DEFAULT_MAX_SPEND_USD,
        per_cell=DEFAULT_CELL_TOKEN_ESTIMATE,
    )  # must not raise
    assert estimate.estimated_usd < containment_cli.DEFAULT_MAX_SPEND_USD


def test_smoke_runs_with_zero_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AC: smoke must be runnable "without thinking about cost" -- a bare
    invocation (no --scale, no --max-spend, no --store) must succeed."""
    monkeypatch.setattr(containment_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = containment_cli.main([])

    assert exit_code == 0
    lines = (tmp_path / "r.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # smoke is always exactly one cell
    row = json.loads(lines[0])
    assert row["note"].startswith("containment harness self-test cell")


def test_full_scale_over_tiny_budget_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(containment_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = containment_cli.main(["--scale", "full", "--max-spend", "0.0"])

    assert exit_code == 1
    assert not (tmp_path / "r.jsonl").exists(), "a refused grid must never execute any cell"
    captured = capsys.readouterr()
    assert "refusing to start" in captured.err


def test_resume_via_cli_does_not_re_execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_path = tmp_path / "r.jsonl"
    monkeypatch.setattr(containment_cli, "_default_store_path", lambda: store_path)
    # "small" (12 cells @ $0.12 = $1.44) exceeds the CLI's smoke-sized
    # default --max-spend ($1.00) -- raise it explicitly for this scale.
    args = ["--scale", "small", "--max-spend", "5.0"]

    first = containment_cli.main(args)
    assert first == 0
    first_lines = store_path.read_text(encoding="utf-8").strip().splitlines()

    second = containment_cli.main(args)
    assert second == 0
    second_lines = store_path.read_text(encoding="utf-8").strip().splitlines()

    # Re-running the identical scale must not add any new rows -- every
    # cell was already persisted by the first invocation.
    assert second_lines == first_lines
