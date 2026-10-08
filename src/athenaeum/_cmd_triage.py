# SPDX-License-Identifier: Apache-2.0
"""``athenaeum triage {run,report}`` — agent triage lane CLI (issue athenaeum#1995).

- ``run``     one pass over the unified decision queue
              (:func:`athenaeum.triage.run_triage`): authority items are
              prepared (never answered), competence ``question`` items are
              offered to the default researcher and submitted through the
              same ``athenaeum decisions answer`` interface when resolved.
              ``--dry-run`` reports what WOULD happen without submitting or
              sampling anything.
- ``report``  read-only summary of the agent-triage calibration channel:
              per the SAME ``calibration summary`` shape
              (:func:`athenaeum.calibration.calibration_summary`), plus the
              confirmed-wrong-in-a-rolling-quarter threshold state
              (:func:`athenaeum.calibration.triage_confirmed_wrong_count` /
              :func:`~athenaeum.calibration.triage_confirmed_wrong_threshold_breached`).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this
is where a NEW subcommand goes, not inline in ``cli.py``'s ``main()``. This
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import
cost down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_knowledge_root, _resolve_wiki_root
from athenaeum.calibration import (
    CONFIRMED_WRONG_QUARTER_THRESHOLD,
    TRIAGE_TIER_NAME,
    calibration_summary,
    triage_confirmed_wrong_count,
    triage_confirmed_wrong_threshold_breached,
)
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config
from athenaeum.triage import run_triage


def _cmd_run(args: argparse.Namespace) -> int:
    knowledge_root = _resolve_knowledge_root(args)
    config = load_config(knowledge_root)
    report = run_triage(knowledge_root, config=config, dry_run=args.dry_run)

    if args.json:
        sys.stdout.write(json.dumps(report.to_dict()) + "\n")
        return 0

    print(
        f"triage pass: {report.authority_prepared} prepared for human "
        f"(authority), {report.competence_absorbed} absorbed, "
        f"{report.competence_escalated} escalated to human (competence), "
        f"{report.refused} refused"
        + (" [dry-run]" if args.dry_run else "")
    )
    if not args.dry_run and report.competence_absorbed:
        print("Run `athenaeum ingest-answers` to apply the absorbed answer(s).")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    wiki_root = _resolve_wiki_root(args)
    summary = calibration_summary(wiki_root)
    triage_bucket = summary.get(TRIAGE_TIER_NAME, {})
    confirmed_wrong = triage_confirmed_wrong_count(wiki_root)
    breached = triage_confirmed_wrong_threshold_breached(wiki_root)

    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "tier": TRIAGE_TIER_NAME,
                    **triage_bucket,
                    "confirmed_wrong_rolling_quarter": confirmed_wrong,
                    "confirmed_wrong_threshold": CONFIRMED_WRONG_QUARTER_THRESHOLD,
                    "confirmed_wrong_threshold_breached": breached,
                }
            )
            + "\n"
        )
        return 0

    print(
        f"agent-triage calibration: sampled={triage_bucket.get('sampled', 0)} "
        f"reviewed={triage_bucket.get('reviewed', 0)} "
        f"overturned={triage_bucket.get('overturned', 0)}"
    )
    flag = " — REVIEW TRIPPED" if breached else ""
    print(
        f"confirmed-wrong (rolling quarter): {confirmed_wrong} / "
        f"{CONFIRMED_WRONG_QUARTER_THRESHOLD}{flag}"
    )
    return 0


def _cmd_triage_dispatch(args: argparse.Namespace) -> int:
    target = getattr(args, "triage_target", None)
    if target == "run":
        return _cmd_run(args)
    if target == "report":
        return _cmd_report(args)
    print("usage: athenaeum triage {run,report} [...]", file=sys.stderr)
    return 2


def add_triage_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum triage`` and its subcommands on *subparsers*."""
    t_parser = subparsers.add_parser(
        "triage",
        help=(
            "Agent triage lane over the unified decision queue (issue "
            "athenaeum#1995): prepares authority items for the human, "
            "absorbs research-resolvable competence items through the "
            "same `decisions answer` interface. Modes: run, report."
        ),
    )
    t_parser.set_defaults(func=_cmd_triage_dispatch)
    t_sub = t_parser.add_subparsers(dest="triage_target")

    run_p = t_sub.add_parser(
        "run",
        help=(
            "Walk the pending-decisions queue once: prepare authority "
            "items for the human, submit whatever the default researcher "
            "resolves for a competence question item."
        ),
    )
    run_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    run_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be absorbed without submitting or sampling anything.",
    )
    run_p.set_defaults(func=_cmd_run)

    report_p = t_sub.add_parser(
        "report",
        help=(
            "Read-only agent-triage calibration summary, including the "
            "confirmed-wrong-in-a-rolling-quarter threshold state."
        ),
    )
    report_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    report_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
    report_p.set_defaults(func=_cmd_report)


__all__ = ["add_triage_subparser"]
