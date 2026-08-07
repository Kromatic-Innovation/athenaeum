# SPDX-License-Identifier: Apache-2.0
"""``athenaeum push-metrics {baseline,coverage-audit}`` — v6 MVP (a), issue athenaeum#711.

Two subcommands over :mod:`athenaeum.push_metrics`:

- ``baseline``       compute precision + coverage over a stated window and
                       write/append a dated snapshot into
                       ``docs/memory-model-measurements.md``.
- ``coverage-audit``  sample N sessions' push records and emit a FILE
                       worksheet a human reviewer marks relevant-but-missed
                       on; the worksheet's miss rate (once reviewed) is the
                       coverage-floor baseline.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` —
mirrors :mod:`athenaeum._cmd_calibration`'s shape.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def _resolve_wiki_root(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    )
    return knowledge_root / "wiki"


def cmd_push_metrics(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum push-metrics {baseline,coverage-audit}``."""
    sub = getattr(args, "push_metrics_target", None)
    if sub not in ("baseline", "coverage-audit"):
        print(
            "usage: athenaeum push-metrics {baseline,coverage-audit} [...]",
            file=sys.stderr,
        )
        return 2

    from athenaeum import push_metrics

    if sub == "baseline":
        since = None
        if getattr(args, "since", None):
            from athenaeum.spend import parse_since

            since = parse_since(args.since)

        baseline = push_metrics.compute_baseline(
            since=since,
            cache_dir=args.cache_dir,
            exclude_sessions=getattr(args, "exclude_session", None),
        )
        docs_path = args.docs_path.expanduser().resolve()
        push_metrics.write_snapshot(baseline, docs_path=docs_path)

        if args.json:
            sys.stdout.write(json.dumps(baseline.to_dict()) + "\n")
        else:
            precision_str = (
                f"{baseline.precision:.4f}"
                if baseline.precision is not None
                else "n/a — accrues as sessions run"
            )
            excluded_sessions_str = (
                ",".join(baseline.excluded_sessions) if baseline.excluded_sessions else "none"
            )
            print(
                f"window: {baseline.start} .. {baseline.end}\n"
                f"sessions: {baseline.session_count}\n"
                f"push_records: {baseline.push_record_count}\n"
                f"reference_records: {baseline.reference_record_count}\n"
                f"precision: {precision_str}\n"
                f"excluded_sessions: {excluded_sessions_str}\n"
                f"excluded_push_records: {baseline.excluded_push_record_count}\n"
                f"excluded_reference_records: {baseline.excluded_reference_record_count}\n"
                f"athenaeum_version: {baseline.athenaeum_version}\n"
                f"git_sha: {baseline.git_sha}\n"
                f"snapshot written to: {docs_path}"
            )
        return 0

    # sub == "coverage-audit"
    wiki_root = _resolve_wiki_root(args)
    worksheet = push_metrics.build_coverage_worksheet(
        n=args.n,
        wiki_root=wiki_root,
        cache_dir=args.cache_dir,
        seed=getattr(args, "seed", None),
    )
    output_path = args.output.expanduser().resolve()
    push_metrics.write_coverage_worksheet(worksheet, output_path=output_path)

    if args.json:
        sys.stdout.write(json.dumps(worksheet) + "\n")
    else:
        print(
            f"sampled {worksheet['sampled_session_count']} session(s) -> "
            f"{output_path}\n"
            "Mark each candidate's reviewer_verdict, then compute the "
            "coverage-floor miss rate from the completed worksheet."
        )
    return 0


def add_push_metrics_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum push-metrics`` and its two modes on ``subparsers``."""
    p_parser = subparsers.add_parser(
        "push-metrics",
        help=(
            "Push-precision + coverage baseline: compute/record the "
            "precision snapshot, and sample sessions for a human-reviewed "
            "coverage-audit worksheet (issue athenaeum#711)."
        ),
    )
    p_parser.set_defaults(func=cmd_push_metrics)
    p_sub = p_parser.add_subparsers(dest="push_metrics_target")

    def _add_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--path",
            type=Path,
            default=DEFAULT_KNOWLEDGE_ROOT,
            help="Knowledge directory (default: ~/knowledge)",
        )
        parser.add_argument(
            "--cache-dir",
            type=Path,
            default=None,
            help="Cache directory holding the push-metrics ledgers "
            "(default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON instead of plain text.",
        )

    baseline_p = p_sub.add_parser(
        "baseline",
        help="Compute precision + coverage over a window; write the dated "
        "snapshot to docs/memory-model-measurements.md.",
    )
    _add_common(baseline_p)
    baseline_p.add_argument(
        "--since",
        default=None,
        help="Window lower bound: relative (7d/24h/30m/2w) or absolute "
        "ISO-8601. Default: the whole ledger (instrument-enabled to now).",
    )
    baseline_p.add_argument(
        "--docs-path",
        type=Path,
        default=Path("docs/memory-model-measurements.md"),
        help="Where the snapshot section is written/appended "
        "(default: docs/memory-model-measurements.md).",
    )
    baseline_p.add_argument(
        "--exclude-session",
        action="append",
        default=None,
        metavar="SESSION_ID",
        help="Exclude a KNOWN-synthetic session id (e.g. one that ran the "
        "test suite and leaked fixture pushes into the ledger, issue "
        "athenaeum#791) from the precision/session counts. Repeatable. "
        "Excluded sessions and their record counts are always reported, "
        "never silently dropped.",
    )

    coverage_p = p_sub.add_parser(
        "coverage-audit",
        help="Sample N sessions' push records into a reviewer worksheet "
        "(pushed set + candidate misses) for the coverage-floor baseline.",
    )
    _add_common(coverage_p)
    coverage_p.add_argument(
        "--n",
        type=int,
        default=10,
        help="Number of sessions to sample (default: 10).",
    )
    coverage_p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional deterministic sample seed (test/repro seam).",
    )
    coverage_p.add_argument(
        "--output",
        type=Path,
        default=Path("coverage-audit-worksheet.json"),
        help="Worksheet output file (default: ./coverage-audit-worksheet.json).",
    )
