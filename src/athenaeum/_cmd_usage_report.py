# SPDX-License-Identifier: Apache-2.0
"""``athenaeum usage-report`` — per-claim usage CLI (issue athenaeum#968).

Thin CLI dispatcher over :mod:`athenaeum.usage_report` — no logic of its
own, mirroring ``athenaeum push-metrics``'s own factoring (a self-contained
``_cmd_<name>.py`` registering via ``add_<name>_subparser``).

ids-only output (see :mod:`athenaeum.usage_report`'s module docstring): the
default text rendering and the ``--json`` rendering both carry only ids,
counts, and timestamps.
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


def cmd_usage_report(args: argparse.Namespace) -> int:
    """``athenaeum usage-report [--claim-id ID] [--since ...] [--json]``."""
    from athenaeum import usage_report

    since = None
    if getattr(args, "since", None):
        from athenaeum.spend import parse_since

        since = parse_since(args.since)

    report = usage_report.compute_usage_report(
        cache_dir=args.cache_dir, since=since, wiki_root=_resolve_wiki_root(args)
    )

    claim_id = getattr(args, "claim_id", None)
    if claim_id:
        usage = report.get(claim_id)
        if args.json:
            sys.stdout.write(json.dumps(usage.to_dict() if usage else None) + "\n")
        elif usage is None:
            print(f"no usage records for claim id {claim_id!r}")
        else:
            _print_row(usage)
        return 0

    rows = usage_report.usage_report_to_list(report)
    if args.json:
        sys.stdout.write(json.dumps(rows) + "\n")
        return 0

    if not rows:
        print("0 claims with usage records")
        return 0
    print(f"{len(rows)} claim(s) with usage records:")
    for row in rows:
        print(
            f"  {row['id']}: pushed={row['pushed_count']} "
            f"referenced={row['referenced_count']} "
            f"last_referenced={row['last_referenced'] or 'never'}"
        )
    return 0


def _print_row(usage) -> None:
    print(
        f"id: {usage.id}\n"
        f"pushed_count: {usage.pushed_count}\n"
        f"referenced_count: {usage.referenced_count}\n"
        f"last_pushed: {usage.last_pushed or 'never'}\n"
        f"last_referenced: {usage.last_referenced or 'never'}"
    )


def add_usage_report_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum usage-report`` on ``subparsers``."""
    p = subparsers.add_parser(
        "usage-report",
        help="Per-claim usage report (pushed / referenced / last-referenced) "
        "computed from the push-metrics ledgers — ids-only, no content "
        "(issue athenaeum#968).",
    )
    p.set_defaults(func=cmd_usage_report)
    p.add_argument(
        "--path",
        "--knowledge-root",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory whose wiki/ AC4-relocated push-records "
        "ledger this report reads (default: ~/knowledge). Issue athenaeum#980.",
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the push-metrics ledgers "
        "(default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum)",
    )
    p.add_argument(
        "--since",
        default=None,
        help="Window lower bound: relative (7d/24h/30m/2w) or absolute "
        "ISO-8601. Default: the whole ledger.",
    )
    p.add_argument(
        "--claim-id",
        default=None,
        metavar="ID",
        help="Report usage for a single claim id only.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
