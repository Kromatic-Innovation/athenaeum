# SPDX-License-Identifier: Apache-2.0
"""Driver for the ``used``-column heuristic measurement (issue athenaeum#1575).

``python -m tests.evals.used_heuristic_cli`` runs the synthetic confusion
matrix, optionally folds in agreement over an existing north-star
``ResultStore``, and writes the dated measurement under ``docs/measurements/``.

**Zero cost.** No client is constructed, no ``claude -p`` is spawned, no
Anthropic API call is made anywhere on this path — the whole measurement is
local fixtures plus already-persisted rollout rows. There is consequently no
spend gate to route through, because there is no spend.

Never invoked by ``ci.yml`` or selected by ``evals.yml`` (manual/local tool
only, same discipline as ``containment_cli.py`` — see
``tests/evals/test_used_heuristic_ci_wiring.py``).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from tests.evals.containment import ResultStore
from tests.evals.north_star_report import load_rollout_rows
from tests.evals.used_heuristic import (
    DEFAULT_MEASUREMENTS_DIR,
    AgreementStat,
    build_report,
    compute_rollout_agreement,
    render_report,
    run_synthetic_eval,
    write_report,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.evals.used_heuristic_cli",
        description=__doc__,
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help=(
            "north-star ResultStore jsonl to measure agreement over (AC2). "
            "Omitted: the agreement section reports that it was not measured."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_MEASUREMENTS_DIR,
        help=f"measurement output directory (default: {DEFAULT_MEASUREMENTS_DIR})",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="print the rendered report instead of writing a file",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="athenaeum-used-heuristic-") as tmp:
        synthetic = run_synthetic_eval(Path(tmp))

    agreement: list[AgreementStat] = []
    if args.store is not None:
        agreement = compute_rollout_agreement(load_rollout_rows(ResultStore(args.store)))

    report = build_report(
        synthetic,
        agreement,
        store_path=args.store,
        store_consulted=args.store is not None,
    )

    if args.stdout:
        print(render_report(report))
        return 0

    path = write_report(report, out_dir=args.out_dir)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
