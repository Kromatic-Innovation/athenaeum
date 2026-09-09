# SPDX-License-Identifier: Apache-2.0
"""Contributor-facing entry point for the containment harness (athenaeum#1521).

``python -m tests.evals.containment_cli`` with NO flags runs the ``smoke``
scale (a single grid cell) against a default budget the smoke grid is
guaranteed to fit under, and writes its one result to an append-only store
under the system temp dir — a contributor never has to think about cost to
try this out (the issue's explicit acceptance criterion). ``--scale
small``/``--scale full`` opt into bigger, costlier grids; ``--max-spend``
is the pre-flight refusal threshold either way.

The example probe/arm/corpus-scale axis values below are a SELF-TEST
placeholder, not real content — the actual north-star arm comparison
(real probes, real candidate arms, Bradley-Terry fitting) is explicitly out
of this issue's scope and is wired by a future issue, which supplies its
own axis values and its own per-cell runner to :func:`tests.evals.containment.build_grid`
/ :func:`tests.evals.containment.run_grid`. ``_demo_cell_runner`` below never
calls an LLM and never spends money — it exists only to exercise the grid/
spend-gate/result-store machinery end-to-end, offline.

This module is NEVER invoked by ``ci.yml`` or ``evals.yml`` (see
``tests/evals/test_containment_ci_wiring.py``) — it is a manual/local tool,
matching how ``tests/evals/README.md``'s "Running locally" section already
documents the live-eval suite.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from athenaeum.tiers import DEFAULT_WRITE_MODEL
from tests.evals.containment import (
    DEFAULT_CELL_TOKEN_ESTIMATE,
    SCALE_BUDGETS,
    CellTokenEstimate,
    GridCell,
    ResultStore,
    SpendCeilingExceededError,
    build_grid,
    price_grid,
    run_grid,
)

# Placeholder axis values (see module docstring) — enough of each axis that
# `--scale small`/`--scale full` visibly differ in cardinality from `smoke`
# and from each other.
EXAMPLE_PROBES: tuple[str, ...] = ("probe-a", "probe-b", "probe-c", "probe-d")
EXAMPLE_ARMS: tuple[str, ...] = ("control", "candidate")
EXAMPLE_CORPUS_SCALES: tuple[str, ...] = ("core", "small")
EXAMPLE_REPLICATES: tuple[int, ...] = (0, 1, 2)

#: A budget the `smoke` scale (always exactly 1 cell, see
#: `containment.SCALE_BUDGETS`) is guaranteed to fit under at the default
#: per-cell token estimate and `DEFAULT_WRITE_MODEL`'s rate — see
#: `test_containment_cli.py::test_default_max_spend_covers_smoke_scale` for
#: the arithmetic proof, kept in a test rather than asserted here so a
#: future rate-table change fails a test loudly instead of the CLI silently
#: refusing its own default.
DEFAULT_MAX_SPEND_USD = 1.0


def _default_store_path() -> Path:
    """Where a bare invocation persists results — the system temp dir, never
    the repo tree. A separate function (not a module-level constant) so
    tests can monkeypatch it for isolation without touching a shared path."""
    return Path(tempfile.gettempdir()) / "athenaeum-eval-containment" / "results.jsonl"


def _demo_cell_runner(cell: GridCell) -> dict[str, Any]:
    """Deterministic, offline, zero-cost stand-in for a real rollout.

    Real content — an actual agent rollout against `cell.probe`/`cell.arm`
    — is out of THIS issue's scope; this only proves the grid/store
    machinery actually executes and persists a cell.
    """
    return {
        "probe": cell.probe,
        "arm": cell.arm,
        "corpus_scale": cell.corpus_scale,
        "replicate": cell.replicate,
        "note": (
            "containment harness self-test cell — no rollout executed "
            "(athenaeum#1521 scope excludes arm rollouts/Bradley-Terry fitting)"
        ),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.evals.containment_cli",
        description=__doc__,
    )
    parser.add_argument(
        "--scale",
        choices=sorted(SCALE_BUDGETS),
        default="smoke",
        help="run-level budget tier (default: smoke)",
    )
    parser.add_argument(
        "--max-spend",
        type=float,
        default=DEFAULT_MAX_SPEND_USD,
        help=f"refuse to start above this priced USD total (default: ${DEFAULT_MAX_SPEND_USD:.2f})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_WRITE_MODEL,
        help="model to price the grid against (default: athenaeum's write-knob default)",
    )
    parser.add_argument(
        "--input-tokens-per-cell",
        type=int,
        default=DEFAULT_CELL_TOKEN_ESTIMATE.input_tokens,
    )
    parser.add_argument(
        "--output-tokens-per-cell",
        type=int,
        default=DEFAULT_CELL_TOKEN_ESTIMATE.output_tokens,
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="append-only JSONL result path (default: a system-temp-dir path)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cells = build_grid(
        args.scale,
        probes=EXAMPLE_PROBES,
        arms=EXAMPLE_ARMS,
        corpus_scales=EXAMPLE_CORPUS_SCALES,
        replicates=EXAMPLE_REPLICATES,
    )
    per_cell = CellTokenEstimate(
        input_tokens=args.input_tokens_per_cell,
        output_tokens=args.output_tokens_per_cell,
    )
    try:
        estimate = price_grid(
            cells, model=args.model, max_spend_usd=args.max_spend, per_cell=per_cell
        )
    except SpendCeilingExceededError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"scale={args.scale} cells={estimate.cell_count} "
        f"estimated=${estimate.estimated_usd:.4f} model={args.model}"
    )
    store = ResultStore(args.store if args.store is not None else _default_store_path())
    results = run_grid(cells, _demo_cell_runner, store)
    print(
        f"executed {len(results)} new cell(s) (skipped: already-complete cells); "
        f"store={store.path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
