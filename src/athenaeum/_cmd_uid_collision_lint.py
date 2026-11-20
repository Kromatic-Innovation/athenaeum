# SPDX-License-Identifier: Apache-2.0
"""``athenaeum uid-collision-lint`` -- one uid on two pages in a surface (athenaeum#2993).

Thin CLI dispatcher over :mod:`athenaeum.uid_collision_lint`; read-only. Exit
codes: ``0`` -- no collision; :data:`EXIT_COLLISIONS_FOUND` (2) -- at least one
uid is carried by more than one page of a surface (same convention as
``store-link-lint``); ``1`` -- ``--surface`` names no known surface. It
rewrites nothing; a collision is for a person to decide.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.uid_collision_lint import UnknownSurface, scan_uid_collisions

EXIT_COLLISIONS_FOUND = 2
EXIT_BAD_SURFACE = 1


def cmd_uid_collision_lint(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum uid-collision-lint``."""
    try:
        report = scan_uid_collisions(_resolve_knowledge_root(args), surface=args.surface)
    except UnknownSurface as exc:
        print(f"uid-collision-lint: {exc}", file=sys.stderr)
        return EXIT_BAD_SURFACE
    if args.json:
        payload = {
            "pages_scanned": report.pages_scanned,
            "ok": report.ok,
            "collisions": [
                {
                    "surface": c.surface,
                    "uid": c.uid,
                    "pages": [{"path": p.path, "tombstone": p.tombstone} for p in c.pages],
                }
                for c in report.collisions
            ],
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0 if report.ok else EXIT_COLLISIONS_FOUND
    print(f"scanned {report.pages_scanned} page(s)")
    if report.ok:
        print("uids OK -- no uid is carried by more than one page in a surface")
        return 0
    print(f"{len(report.collisions)} uid(s) carried by more than one page:")
    for c in report.collisions:
        print(f"  {c.uid} [{c.surface}]")
        for p in c.pages:
            print(f"    {p.path}{' (tombstone)' if p.tombstone else ''}")
    return EXIT_COLLISIONS_FOUND


def add_uid_collision_lint_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum uid-collision-lint`` on *subparsers*."""
    parser = subparsers.add_parser(
        "uid-collision-lint",
        help=(
            "Read-only: list every uid carried by more than one page inside a "
            "single surface (tombstones count). The same uid on two different "
            "surfaces is not reported. Exits 2 when any collision is found."
        ),
    )
    parser.set_defaults(func=cmd_uid_collision_lint)
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--surface",
        default=None,
        help=(
            "Scan only this surface (wiki or a configured surface name). "
            "Default: each surface on its own."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit machine-readable JSON (collisions with page paths and tombstone "
            "flags) instead of plain text."
        ),
    )
