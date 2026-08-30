# SPDX-License-Identifier: Apache-2.0
"""``athenaeum reconcile`` — retire dual-written raw-intake files (athenaeum#1143).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``_cmd_repair.py``'s module docstring for the rule this follows.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def add_reconcile_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum reconcile`` and its flags on *subparsers*."""
    parser = subparsers.add_parser(
        "reconcile",
        help="Retire pending raw-intake files whose content is already "
        "materialized in the wiki (dual-write cleanup, athenaeum#1143). "
        "Default is dry-run; pass --apply to remove.",
    )
    parser.add_argument(
        "--source",
        default="drive",
        help="raw/<source>/ tree to reconcile (default: drive).",
    )
    parser.add_argument(
        "--import-commit",
        required=True,
        help="Git commit (in --knowledge-root) at which the source dual-wrote "
        "raw intake and wiki pages together. No default — pass the exact "
        "SHA for the dual-write event you are reconciling.",
    )
    parser.add_argument(
        "--knowledge-root",
        type=Path,
        default=None,
        help="Knowledge root (default: ~/knowledge). Must be a git repo "
        "containing --import-commit.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Remove reconciled files via `git rm` + commit. Without this "
        "flag, the command is a dry-run: nothing is written.",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_reconcile)


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Run the dual-write reconcile pass.

    Exit codes (mirrors the ``repair`` contract in ``_cmd_repair.py``):
        0 — clean run (nothing to remove, OR ``--apply`` succeeded with no
            errors).
        1 — errors encountered (read failures, lookup failures, or the
            knowledge root / import commit could not be resolved).
        2 — dry-run found files that WOULD be removed (CI/operator gate
            signal).
    """
    from athenaeum.config import load_config
    from athenaeum.reconcile import run_reconcile

    knowledge_root = (args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    if not knowledge_root.is_dir():
        print(f"Knowledge root not found: {knowledge_root}", file=sys.stderr)
        return 1
    if not (knowledge_root / "raw").is_dir():
        print(f"No raw/ tree under knowledge root: {knowledge_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)

    # --apply mutates the raw tree and can race a concurrent `run`, so it
    # takes the run lock (issue athenaeum#309), same as every other
    # write-capable command. A dry-run reads only — no lock.
    lock: "RunLock | int | None" = None
    if args.apply:
        lock = _acquire_or_exit(knowledge_root, args, cfg)
        if isinstance(lock, int):
            return lock
    try:
        report = run_reconcile(
            knowledge_root,
            source=args.source,
            import_commit=args.import_commit,
            config=cfg,
            dry_run=not args.apply,
        )

        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"=== reconcile {args.source} ({mode}, import_commit={args.import_commit}) ===")
        print(f"  removed:        {len(report.removed)}")
        print(f"  retained:       {len(report.retained)}")
        print(f"  genuinely_new:  {len(report.genuinely_new)}")
        print(f"  errors:         {len(report.errors)}")

        by_reason: dict[str, int] = {}
        for disp in report.dispositions:
            if disp.disposition != "removed":
                by_reason[disp.disposition] = by_reason.get(disp.disposition, 0) + 1
        for reason, count in sorted(by_reason.items()):
            print(f"    retained[{reason}]: {count}")

        if report.removed and not args.apply:
            for ref in report.removed[:20]:
                print(f"    would remove: {ref}")
            if len(report.removed) > 20:
                print(f"    ... and {len(report.removed) - 20} more")

        for ref, err in report.errors[:20]:
            print(f"  ERR {ref}: {err}", file=sys.stderr)

        if report.committed:
            print("  committed: yes (git rm + commit)")

        if report.errors:
            return 1
        if not args.apply and report.removed:
            return 2
        return 0
    finally:
        if lock is not None and not isinstance(lock, int):
            lock.release()
