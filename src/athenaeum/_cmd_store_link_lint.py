# SPDX-License-Identifier: Apache-2.0
"""``athenaeum store-link-lint`` -- links that point at a more private store (athenaeum#2619).

Thin CLI dispatcher over :mod:`athenaeum.store_links`; read-only. Exit codes:
``0`` -- no violation; :data:`EXIT_VIOLATIONS_FOUND` (2) -- at least one link
from a page to a page on a more private surface (same convention as
``fold-lint``). It rewrites nothing; fixing a link is the operator's call.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.store_links import scan_store_links

EXIT_VIOLATIONS_FOUND = 2


def cmd_store_link_lint(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum store-link-lint``."""
    report = scan_store_links(_resolve_knowledge_root(args))
    if args.json:
        payload = {
            "pages_scanned": report.pages_scanned,
            "ok": report.ok,
            "violations": [v.__dict__ for v in report.violations],
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0 if report.ok else EXIT_VIOLATIONS_FOUND
    print(f"scanned {report.pages_scanned} page(s)")
    if report.ok:
        print("store links OK -- no link points at a more private store")
        return 0
    print(f"{len(report.violations)} link(s) point at a more private store:")
    for v in report.violations:
        print(f"  {v.describe()}")
    return EXIT_VIOLATIONS_FOUND


def add_store_link_lint_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum store-link-lint`` on *subparsers*."""
    parser = subparsers.add_parser(
        "store-link-lint",
        help=(
            "Read-only: list every link from a page to a page on a more private "
            "store (lower visibility rank). A link to a more shared store is fine."
        ),
    )
    parser.set_defaults(func=cmd_store_link_lint)
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
