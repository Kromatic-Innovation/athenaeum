# SPDX-License-Identifier: Apache-2.0
"""``athenaeum convergence`` — issue athenaeum#2020 (athenaeum#719 Plan steps 7-8).

The CLI surface for :mod:`athenaeum.convergence`'s quarterly supply/demand
report. Always available and read-only, regardless of whether the self-
tuning loop's own nightly mining/drafting phase
(``librarian._run_signal_mining_phase``, gated by
:func:`athenaeum.config.resolve_signal_mining_enabled`) is turned on — an
operator must be able to check what the registry's history looks like even
while the automated loop stays off, the same "viewing is free, automating
costs an opt-in" split :mod:`athenaeum._cmd_decisions` already draws between
listing pending decisions and anything that spends.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config


def cmd_convergence(args: argparse.Namespace) -> int:
    from athenaeum.convergence import compute_convergence_report

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    config = load_config(knowledge_root)
    wiki_root = knowledge_root / "wiki"

    report = compute_convergence_report(wiki_root, config=config)

    if args.json:
        sys.stdout.write(json.dumps(report.to_dict()) + "\n")
        return 0

    print(f"convergence report: {report.reading}")
    print(f"  {report.explanation}")
    if report.quarters:
        print(f"  quarters: {', '.join(report.quarters)}")
        print(f"  supply (approve resolutions):  {report.supply}  [{report.supply_trend}]")
        print(f"  demand (unregistered-dim signal): {report.demand}  [{report.demand_trend}]")
    else:
        print("  no completed-quarter history yet")
    return 0


def add_convergence_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum convergence``."""
    parser = subparsers.add_parser(
        "convergence",
        help=(
            "Quarterly self-tuning-loop convergence report: supply (dimension-"
            "proposal approvals) vs. demand (unregistered-dimension signal), "
            "labelled convergence/abandonment/cyc_failure_mode/insufficient_data "
            "(issue athenaeum#2020)."
        ),
    )
    parser.set_defaults(func=cmd_convergence)
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
