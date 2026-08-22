# SPDX-License-Identifier: Apache-2.0
"""``athenaeum measure {shadow-linkage,backlog-price,ordinary-night}`` — issue athenaeum#713.

Three read-only measurement-pack subcommands, one per artifact the v6
comparator slice (child of athenaeum#709) is gated on:

- ``shadow-linkage``    :mod:`athenaeum.shadow_linkage` — shadow-mode
                         complete-linkage cluster population, zero LLM calls.
- ``backlog-price``     :mod:`athenaeum.backlog_price_sheet` — backlog price
                         sheet + decision-inflow sensitivity table.
- ``ordinary-night``    :mod:`athenaeum.ordinary_night_table` — ordinary-night
                         steady-state table, closes-or-not verdict.

All three: read-only against the live store (no wiki write, no
``_pending_merges.md`` mutation, no reindex), write/append their dated
snapshot into ``docs/memory-model-measurements.md`` unless ``--dry-run`` was
passed, and print a machine-readable summary with ``--json``.

Factoring rule (L5 presentation): mirrors :mod:`athenaeum._cmd_push_metrics`'s
shape exactly — one ``_cmd_measure.py`` for the whole small command family,
registering via ``add_measure_subparser``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--docs-path",
        type=Path,
        default=Path("docs/memory-model-measurements.md"),
        help="Where the snapshot section is written/appended "
        "(default: docs/memory-model-measurements.md).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and display the measurement without writing to --docs-path.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )


def _read_summary_log(path: Path | None) -> list:
    if path is None:
        return []
    from athenaeum.run_summary_log import parse_run_summary_log

    return parse_run_summary_log(path)


def cmd_shadow_linkage(args: argparse.Namespace) -> int:
    from athenaeum import shadow_linkage

    knowledge_root = args.path.expanduser().resolve()
    result = shadow_linkage.run_shadow_linkage(knowledge_root)

    docs_path = args.docs_path.expanduser().resolve()
    written = False
    if not args.dry_run:
        try:
            shadow_linkage.write_snapshot(result, docs_path=docs_path)
            written = True
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.json:
        payload = result.to_dict()
        payload["dry_run"] = args.dry_run
        sys.stdout.write(json.dumps(payload) + "\n")
    else:
        line = (
            "dry run: no snapshot written (pass without --dry-run to write)"
            if args.dry_run
            else f"snapshot written to: {docs_path}"
        )
        print(
            f"candidate_file_count: {result.candidate_file_count}\n"
            f"threshold: {result.threshold:.4f}\n"
            f"complete-linkage clusters: {result.complete_linkage.cluster_count} "
            f"(pairs: {result.complete_linkage.comparator_pair_count})\n"
            f"single-linkage components: {result.single_linkage.cluster_count} "
            f"(pairs: {result.single_linkage.comparator_pair_count})\n"
            f"{line}"
        )
    return 0 if (written or args.dry_run) else 1


def cmd_backlog_price(args: argparse.Namespace) -> int:
    from athenaeum import backlog_price_sheet

    knowledge_root = args.path.expanduser().resolve()
    summary_records = _read_summary_log(args.summary_log)

    result = backlog_price_sheet.build_price_sheet(
        knowledge_root,
        cache_dir=args.cache_dir,
        summary_log_records=summary_records,
        prefilter_excluded_fraction=args.prefilter_excluded_fraction,
        human_daily_budget=args.human_daily_budget,
        six_month_days=args.six_month_days,
        backlog_count=args.backlog_count,
        calls_per_file=args.calls_per_file,
        wall_clock_per_file_seconds=args.wall_clock_per_file_seconds,
    )

    docs_path = args.docs_path.expanduser().resolve()
    written = False
    if not args.dry_run:
        try:
            backlog_price_sheet.write_snapshot(result, docs_path=docs_path)
            written = True
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.json:
        payload = result.to_dict()
        payload["dry_run"] = args.dry_run
        sys.stdout.write(json.dumps(payload) + "\n")
    else:
        line = (
            "dry run: no snapshot written (pass without --dry-run to write)"
            if args.dry_run
            else f"snapshot written to: {docs_path}"
        )
        print(
            f"backlog_count: {result.backlog_count}\n"
            f"cost_without_prefilter_usd: ${result.cost_without_prefilter_usd:.2f}\n"
            f"calls_per_file: {result.calls_per_file} [{result.calls_per_file_source}]\n"
            f"wall_clock_per_file_seconds: {result.wall_clock_per_file_seconds} "
            f"[{result.wall_clock_source}]\n"
            f"{line}"
        )
    return 0 if (written or args.dry_run) else 1


def cmd_ordinary_night(args: argparse.Namespace) -> int:
    from athenaeum import ordinary_night_table as ont

    knowledge_root = args.path.expanduser().resolve()
    summary_records = _read_summary_log(args.summary_log)

    comparator_pairs_per_night = args.comparator_pairs_per_night
    if (
        comparator_pairs_per_night is None
        and args.comparator_pair_count is not None
        and args.comparator_amortization_nights > 0
    ):
        comparator_pairs_per_night = (
            args.comparator_pair_count / args.comparator_amortization_nights
        )
    if comparator_pairs_per_night is None:
        comparator_pairs_per_night = 0.0

    amortized = ont.AmortizedLoadAssumptions(
        comparator_pairs_per_night=comparator_pairs_per_night,
        comparator_calls_per_pair=args.comparator_calls_per_pair,
        comparator_seconds_per_pair=args.comparator_seconds_per_pair,
        ttl_recheck_calls_per_night=args.ttl_recheck_calls_per_night,
        ttl_recheck_seconds_per_night=args.ttl_recheck_seconds_per_night,
        invalidation_wave_calls_per_night=args.invalidation_wave_calls_per_night,
        invalidation_wave_seconds_per_night=args.invalidation_wave_seconds_per_night,
        audit_sampling_calls_per_night=args.audit_sampling_calls_per_night,
        audit_sampling_seconds_per_night=args.audit_sampling_seconds_per_night,
    )

    result = ont.build_ordinary_night_table(
        knowledge_root,
        cache_dir=args.cache_dir,
        summary_log_records=summary_records,
        amortized=amortized,
        nights_in_wave=args.nights_in_wave,
        total_nights=args.total_nights,
        intake_window_days=args.intake_window_days,
        calls_per_file=args.calls_per_file,
        files_per_day=args.files_per_day,
        wall_clock_per_file_seconds=args.wall_clock_per_file_seconds,
    )

    docs_path = args.docs_path.expanduser().resolve()
    if not args.dry_run:
        ont.write_snapshot(result, docs_path=docs_path)

    if args.json:
        payload = result.to_dict()
        payload["dry_run"] = args.dry_run
        sys.stdout.write(json.dumps(payload) + "\n")
    else:
        line = (
            "dry run: no snapshot written (pass without --dry-run to write)"
            if args.dry_run
            else f"snapshot written to: {docs_path}"
        )
        print(
            f"verdict: {result.verdict}\n"
            f"files_per_day: {result.files_per_day:.3f}\n"
            f"nightly_calls_total: {result.nightly_calls_total} "
            f"vs budget {result.nightly_call_budget}\n"
            f"nightly_seconds_total: {result.nightly_seconds_total} "
            f"vs window {result.nightly_window_seconds}\n"
            f"{line}"
        )
    return 0


def add_measure_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum measure`` and its three subcommands on ``subparsers``."""
    m_parser = subparsers.add_parser(
        "measure",
        help="v6 memory-model measurement pack (issue athenaeum#713): shadow-mode "
        "complete-linkage population, backlog price sheet, ordinary-night "
        "steady-state table.",
    )
    m_parser.set_defaults(func=lambda args: _dispatch(args))
    m_sub = m_parser.add_subparsers(dest="measure_target")

    shadow_p = m_sub.add_parser(
        "shadow-linkage",
        help="Shadow-mode complete-linkage cluster population over the live "
        "wiki store: embeddings only, zero LLM calls, read-only.",
    )
    _add_common(shadow_p)
    shadow_p.set_defaults(func=cmd_shadow_linkage)

    price_p = m_sub.add_parser(
        "backlog-price",
        help="Backlog price sheet with a decision-inflow sensitivity table.",
    )
    _add_common(price_p)
    price_p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the spend ledger "
        "(default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum)",
    )
    price_p.add_argument(
        "--summary-log",
        type=Path,
        default=None,
        help="Path to a nightly log file containing 'librarian-run-summary' "
        "lines, used to derive wall-clock/file. Omit to report wall-clock "
        "figures as not-yet-measurable (no fabricated figure).",
    )
    price_p.add_argument(
        "--prefilter-excluded-fraction",
        type=float,
        default=None,
        help="Fraction of the backlog a write-refusal/retention-pack "
        "pre-filter would exclude (that classifier does not exist yet in "
        "this codebase — omit to report the 'with prefilter' column as n/a).",
    )
    from athenaeum.backlog_price_sheet import (
        DEFAULT_HUMAN_DAILY_BUDGET,
        DEFAULT_SIX_MONTH_DAYS,
    )

    price_p.add_argument(
        "--human-daily-budget",
        type=int,
        default=DEFAULT_HUMAN_DAILY_BUDGET,
        help="Human decisions/day the sensitivity table paces against "
        "(default: 20, per the issue's stated budget).",
    )
    price_p.add_argument(
        "--six-month-days",
        type=int,
        default=DEFAULT_SIX_MONTH_DAYS,
        help="Day count marking the 6-month horizon a sensitivity row can "
        "breach (default: 182).",
    )
    price_p.add_argument(
        "--backlog-count",
        type=int,
        default=None,
        help="Operator-supplied override for the backlog file count (issue "
        "athenaeum#1095 AC3(a)). Omit to re-derive it from the live raw/ tree "
        "(default). When supplied, the snapshot records "
        "backlog_count_source=operator-supplied.",
    )
    price_p.add_argument(
        "--calls-per-file",
        type=float,
        default=None,
        help="Operator-supplied override for calls/file (issue athenaeum#1095 "
        "AC3(b)). Omit to re-derive it from the spend ledger (default). When "
        "supplied, the snapshot records calls_per_file_source=operator-supplied.",
    )
    price_p.add_argument(
        "--wall-clock-per-file-seconds",
        type=float,
        default=None,
        help="Operator-supplied override for wall-clock/file (issue "
        "athenaeum#1095 AC3(c)). Omit to re-derive it from --summary-log "
        "(default). When supplied, the snapshot records "
        "wall_clock_source=operator-supplied.",
    )
    price_p.set_defaults(func=cmd_backlog_price)

    night_p = m_sub.add_parser(
        "ordinary-night",
        help="Ordinary-night steady-state table: measured load + amortized "
        "comparator-regime assumptions vs the nightly call/wall-clock budgets.",
    )
    _add_common(night_p)
    night_p.add_argument("--cache-dir", type=Path, default=None)
    night_p.add_argument(
        "--summary-log",
        type=Path,
        default=None,
        help="Same as backlog-price's --summary-log.",
    )
    night_p.add_argument(
        "--intake-window-days",
        type=int,
        default=14,
        help="Trailing window (days) files/day-of-intake is measured over (default: 14).",
    )
    night_p.add_argument(
        "--comparator-pair-count",
        type=int,
        default=None,
        help="Artifact 1's measured comparator_pair_count (complete-linkage), "
        "amortized over --comparator-amortization-nights to derive "
        "comparator-pairs-per-night. Overridden by --comparator-pairs-per-night "
        "if both are given.",
    )
    night_p.add_argument("--comparator-amortization-nights", type=int, default=7)
    night_p.add_argument("--comparator-pairs-per-night", type=float, default=None)
    night_p.add_argument("--comparator-calls-per-pair", type=float, default=1.0)
    night_p.add_argument("--comparator-seconds-per-pair", type=float, default=0.0)
    night_p.add_argument("--ttl-recheck-calls-per-night", type=float, default=0.0)
    night_p.add_argument("--ttl-recheck-seconds-per-night", type=float, default=0.0)
    night_p.add_argument("--invalidation-wave-calls-per-night", type=float, default=0.0)
    night_p.add_argument("--invalidation-wave-seconds-per-night", type=float, default=0.0)
    night_p.add_argument("--audit-sampling-calls-per-night", type=float, default=0.0)
    night_p.add_argument("--audit-sampling-seconds-per-night", type=float, default=0.0)
    night_p.add_argument("--nights-in-wave", type=int, default=None)
    night_p.add_argument("--total-nights", type=int, default=None)
    night_p.add_argument(
        "--calls-per-file",
        type=float,
        default=None,
        help="Operator-supplied override for calls/file (issue athenaeum#1095 "
        "AC5). Omit to re-derive it from the spend ledger (default). When "
        "supplied, the snapshot records calls_per_file_source=operator-supplied.",
    )
    night_p.add_argument(
        "--files-per-day",
        type=float,
        default=None,
        help="Operator-supplied override for files/day of ordinary intake "
        "(issue athenaeum#1095 AC5). Omit to re-derive it from the trailing "
        "--intake-window-days scan of raw/ (default). When supplied, the "
        "snapshot records files_per_day_source=operator-supplied.",
    )
    night_p.add_argument(
        "--wall-clock-per-file-seconds",
        type=float,
        default=None,
        help="Operator-supplied override for wall-clock/file (issue "
        "athenaeum#1095 AC5). Omit to re-derive it from --summary-log "
        "(default). When supplied, the snapshot records "
        "wall_clock_source=operator-supplied.",
    )
    night_p.set_defaults(func=cmd_ordinary_night)


def _dispatch(args: argparse.Namespace) -> int:
    print(
        "usage: athenaeum measure {shadow-linkage,backlog-price,ordinary-night} [...]",
        file=sys.stderr,
    )
    return 2
