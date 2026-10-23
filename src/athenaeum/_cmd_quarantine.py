# SPDX-License-Identifier: Apache-2.0
"""``athenaeum quarantine requeue`` -- re-queue legacy quarantine entries once
(issue athenaeum#2075 AC5).

Operator-side, one-shot, after the retry schedule is deployed::

    athenaeum quarantine requeue            # dry run: lists what would move
    athenaeum quarantine requeue --apply    # releases them back into raw/

Only pending entries parked by the pre-schedule one-shot rule (no attempt
history) and tripped on the LLM-call or wall-clock bound are eligible; an
entry that already failed the schedule is never re-queued, so a second
invocation is a no-op for those. Domain logic: :mod:`athenaeum.quarantine`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.runlock import RunLock


def add_quarantine_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``quarantine``."""
    parser = subparsers.add_parser(
        "quarantine",
        help="Quarantine maintenance (issue athenaeum#2075). 'requeue' releases "
        "legacy one-shot quarantine entries (LLM-call / wall-clock bound, no "
        "attempt history) back into raw/ so they run under the retry "
        "schedule. Dry-run unless --apply.",
    )
    parser.add_argument("action", choices=["requeue"])
    parser.add_argument("--apply", action="store_true", help="Write changes.")
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_quarantine)


def cmd_quarantine(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum quarantine``.

    Exit codes: 0 clean; 1 missing knowledge root or at least one entry failed
    to requeue; 75 the run lock is held (--apply only).
    """
    from athenaeum.config import load_config
    from athenaeum.quarantine import requeue_legacy_quarantine

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    lock: RunLock | None = None
    if args.apply:
        acquired = _acquire_or_exit(knowledge_root, args, load_config(knowledge_root))
        if isinstance(acquired, int):
            return acquired
        lock = acquired
    try:
        rows = requeue_legacy_quarantine(wiki_root, knowledge_root / "raw", apply=args.apply)
    finally:
        if lock is not None:
            lock.release()

    print(f"=== quarantine requeue ({'APPLY' if args.apply else 'DRY RUN'}) ===")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row["status"] != "skipped":
            print(f"  {row['status']:10} {row['bound']:11} {row['ref']}")
    print(f"  pending entries:      {len(rows)}")
    for status, n in sorted(counts.items()):
        print(f"  {status + ':':22}{n}")
    return 1 if any(r["status"].startswith("error") for r in rows) else 0
