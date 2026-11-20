# SPDX-License-Identifier: Apache-2.0
"""``athenaeum machine-local-check`` -- tracked machine-local files (athenaeum#2994).

Thin CLI dispatcher over :mod:`athenaeum.sync_machine_local`; read-only. By default
it checks every repository of the store that syncs in ``integrate`` mode; ``--repo``
checks one repository. Exit ``0`` -- nothing found; :data:`EXIT_TRACKED_FOUND` (2)
-- at least one tracked machine-local file; ``1`` -- a repository could not be
read. It untracks and deletes nothing; that is an operator step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.sync_machine_local import check_repo

EXIT_TRACKED_FOUND = 2


def _repos(args: argparse.Namespace) -> list[Path]:
    if args.repo is not None:
        return [args.repo]
    from athenaeum.config import load_config
    from athenaeum.sync_integrate import plan_repo_syncs

    root = _resolve_knowledge_root(args)
    return [p.repo for p in plan_repo_syncs(root, load_config(root)) if p.decision.integrate]


def cmd_machine_local_check(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum machine-local-check``."""
    results = []
    for repo in _repos(args):
        paths, error = check_repo(repo)
        results.append({"repo": str(repo), "tracked": list(paths), "error": error})
    found = any(r["tracked"] for r in results)
    errored = any(r["error"] for r in results)
    if args.json:
        sys.stdout.write(json.dumps({"ok": not found and not errored, "repos": results}) + "\n")
    else:
        if not results:
            print("no repository in integrate mode to check")
        for r in results:
            if r["error"]:
                print(f"{r['repo']}: could not read ({r['error']})")
            elif r["tracked"]:
                print(f"{r['repo']}: {len(r['tracked'])} tracked machine-local file(s):")
                for p in r["tracked"]:
                    print(f"  {p}")
            else:
                print(f"{r['repo']}: OK -- no machine-local file is tracked")
    if found:
        return EXIT_TRACKED_FOUND
    return 1 if errored else 0


def add_machine_local_check_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum machine-local-check`` on *subparsers*."""
    parser = subparsers.add_parser(
        "machine-local-check",
        help=(
            "Read-only: list machine-local files (per the artifact registry) that a "
            "shared repository tracks. Exit 2 when one is found. Changes nothing."
        ),
    )
    parser.set_defaults(func=cmd_machine_local_check)
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Check this one git repository instead of the store's integrate-mode ones.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
