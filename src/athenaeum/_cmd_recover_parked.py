# SPDX-License-Identifier: Apache-2.0
"""``athenaeum recover-parked`` -- restore and re-ingest the observations
parked by refused oversize merges (issue athenaeum#2078 AC5).

Operator-side, one-shot, run after the AC1/AC2 fix is deployed::

    athenaeum recover-parked restore  --manifest M --apply
    athenaeum run                      # normal librarian run ingests the files
    athenaeum recover-parked resolve  --manifest M --apply

Dry-run unless ``--apply``. Counts only on stdout. Domain logic:
:mod:`athenaeum.parked_recovery`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.runlock import RunLock


def add_recover_parked_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``recover-parked``."""
    parser = subparsers.add_parser(
        "recover-parked",
        help="One-shot recovery of observations parked by refused oversize "
        "merges (issue athenaeum#2078): 'restore' writes the raw files listed "
        "in a manifest back from git history so the next librarian run "
        "ingests them; 'resolve' then archives the matching pending "
        "questions once their files have been consumed. Dry-run unless "
        "--apply. Counts only on stdout.",
    )
    parser.add_argument("phase", choices=["restore", "resolve"])
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="PATH",
        help="JSON list of {question_id, conflict_type, entity, raw_path, "
        "last_commit} rows. Host-side file; never commit it.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        metavar="PATH",
        help="Recovery state file (default: recovery-state.json beside the manifest).",
    )
    parser.add_argument("--apply", action="store_true", help="Write changes.")
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_recover_parked)


def cmd_recover_parked(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum recover-parked``.

    Exit codes: 0 clean run; 1 bad manifest/state or missing knowledge root;
    75 the run lock is held (--apply only).
    """
    from athenaeum.config import load_config
    from athenaeum.parked_recovery import (
        RecoveryError,
        load_manifest,
        resolve_recovered,
        restore_raw_files,
    )

    knowledge_root = args.path.expanduser().resolve()
    if not (knowledge_root / "wiki").is_dir():
        print(f"Wiki root not found: {knowledge_root / 'wiki'}", file=sys.stderr)
        return 1
    manifest = args.manifest.expanduser()
    state = (args.state or manifest.parent / "recovery-state.json").expanduser()

    lock: RunLock | None = None
    if args.apply:
        acquired = _acquire_or_exit(knowledge_root, args, load_config(knowledge_root))
        if isinstance(acquired, int):
            return acquired
        lock = acquired
    try:
        rows = load_manifest(manifest)
        if args.phase == "restore":
            counts = restore_raw_files(knowledge_root, rows, state, apply=args.apply)
        else:
            counts = resolve_recovered(
                knowledge_root,
                knowledge_root / "wiki" / "_pending_questions.md",
                rows,
                state,
                apply=args.apply,
            )
    except RecoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            lock.release()

    print(f"=== recover-parked {args.phase} ({'APPLY' if args.apply else 'DRY RUN'}) ===")
    print(f"  manifest rows:        {len(rows)}")
    for key, value in counts.items():
        print(f"  {key + ':':22}{value}")
    return 0
