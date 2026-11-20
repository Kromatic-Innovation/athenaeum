# SPDX-License-Identifier: Apache-2.0
"""``athenaeum raised-from-lint`` -- pages whose ``raised_from`` block is bad (athenaeum#2799).

Thin CLI dispatcher over :mod:`athenaeum.raise_provenance`; read-only. Plain
output names each offending page by uid (or file name when it has none) and its
reason code, one line per reason, and prints nothing when the corpus is clean.
Exit codes: ``0`` -- nothing to report; :data:`EXIT_FINDINGS` (2) -- at least one
page reported (same convention as ``store-link-lint``). It rewrites no file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.raise_provenance import scan_raised_from

EXIT_FINDINGS = 2


def cmd_raised_from_lint(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum raised-from-lint``."""
    report = scan_raised_from(_resolve_knowledge_root(args))
    if args.json:
        payload = [
            {"uid": f.uid, "page": f.page, "reasons": list(f.reasons)}
            for f in report.findings
        ]
        sys.stdout.write(json.dumps(payload) + "\n")
    else:
        for f in report.findings:
            for reason in f.reasons:
                print(f"{f.uid or f.page}: {reason}")
    return 0 if report.ok else EXIT_FINDINGS


def add_raised_from_lint_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum raised-from-lint`` on *subparsers*."""
    parser = subparsers.add_parser(
        "raised-from-lint",
        help=(
            "Read-only: list every page whose raised_from provenance block is "
            "malformed, or whose raise: source disagrees with it."
        ),
    )
    parser.set_defaults(func=cmd_raised_from_lint)
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (a list of findings) instead of plain text.",
    )
