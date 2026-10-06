# SPDX-License-Identifier: Apache-2.0
"""``athenaeum fold-lint`` — read-only fold-graph invariant check (issue athenaeum#716).

Thin CLI dispatcher over :mod:`athenaeum.fold_graph_lint`, mirroring
``athenaeum outbound-lint``'s shape (:mod:`athenaeum._cmd_outbound`): no
detection logic of its own, read-only by construction (this module never
imports anything that writes).

Exit codes: ``0`` — no violation found; :data:`EXIT_VIOLATIONS_FOUND` (2) — at
least one fold-graph invariant violation was found, mirroring
``outbound-lint``'s own "found something to act on" convention
(:data:`athenaeum._cmd_outbound.EXIT_PII_FOUND`).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_wiki_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.fold_graph_lint import scan_fold_graph

#: Exit code when a violation is found (mirrors ``outbound-lint``'s
#: ``EXIT_PII_FOUND`` convention — non-zero-on-found, distinct from the
#: generic error code 1).
EXIT_VIOLATIONS_FOUND = 2


def cmd_fold_lint(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum fold-lint``."""
    wiki_root = _resolve_wiki_root(args)
    report = scan_fold_graph(wiki_root)

    if args.json:
        payload = {
            "pages_scanned": report.pages_scanned,
            "tombstones": report.tombstones,
            "ok": report.ok,
            "violations": [
                {"kind": v.kind, "members": v.members, "detail": v.detail}
                for v in report.violations
            ],
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0 if report.ok else EXIT_VIOLATIONS_FOUND

    print(
        f"scanned {report.pages_scanned} page(s), {report.tombstones} tombstone(s)"
    )
    if report.ok:
        print("fold graph OK — no invariant violations found")
        return 0
    print(f"{len(report.violations)} fold-graph violation(s):")
    for v in report.violations:
        print(f"  [{v.kind}] {v.detail}")
    return EXIT_VIOLATIONS_FOUND


def add_fold_lint_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum fold-lint`` on *subparsers*."""
    parser = subparsers.add_parser(
        "fold-lint",
        help=(
            "Read-only check of the two fold-graph invariants (issue athenaeum#716): "
            "the folded_into graph is acyclic, and every fold set has exactly one "
            "live canonical page."
        ),
    )
    parser.set_defaults(func=cmd_fold_lint)
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
