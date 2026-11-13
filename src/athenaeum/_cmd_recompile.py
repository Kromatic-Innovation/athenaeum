# SPDX-License-Identifier: Apache-2.0
"""``athenaeum recompile {candidates}`` -- page-recompile candidate scan
(issue athenaeum#2534, slice S1 of athenaeum#2525).

Only ``candidates`` exists in this slice: it runs
:func:`athenaeum.page_recompile.find_recompile_candidates` and prints
COUNTS ONLY (candidates, flagged, over ceiling, blocked, with a count per
blocked reason) -- never a page name, uid, or filename (AC8). Assignment,
the move itself, and any status/nightly surfacing are later slices' jobs
and are deliberately not wired here. Makes no LLM call, writes nothing, and
never takes the run lock.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def add_recompile_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum recompile`` and its ``candidates`` subcommand."""
    parser = subparsers.add_parser(
        "recompile",
        help="Page-recompile maintenance pass (issue athenaeum#2534). Only "
        "`candidates` exists today: a read-only scan reported as counts.",
    )
    parser.set_defaults(func=_cmd_recompile_dispatch)
    sub = parser.add_subparsers(dest="recompile_target")

    candidates_p = sub.add_parser(
        "candidates",
        help="Scan for active pages over librarian.page_size_threshold_chars "
        "or flagged needs_split: true, and report counts only -- no page "
        "name, uid, or filename is ever printed. Read-only: no run lock, "
        "no writes, no LLM call.",
    )
    candidates_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    candidates_p.set_defaults(func=_cmd_candidates)


def _cmd_recompile_dispatch(args: argparse.Namespace) -> int:
    print("usage: athenaeum recompile {candidates} [...]", file=sys.stderr)
    return 2


def _cmd_candidates(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum recompile candidates``.

    Exit codes: 0 clean run; 1 wiki root missing.
    """
    from athenaeum.config import load_config
    from athenaeum.page_recompile import find_recompile_candidates

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    scan = find_recompile_candidates(wiki_root, cfg)

    reason_counts: dict[str, int] = {}
    for blocked in scan.blocked:
        reason_counts[blocked.reason] = reason_counts.get(blocked.reason, 0) + 1

    # Counts only (AC8) -- no path, uid, or filename from `scan` is ever
    # formatted into a print() call below.
    print("=== recompile candidates ===")
    print(f"  candidates:           {len(scan.candidates)}")
    print(f"    flagged:            {sum(c.flagged for c in scan.candidates)}")
    print(f"    over hard ceiling:  {sum(c.over_ceiling for c in scan.candidates)}")
    print(f"  blocked:              {len(scan.blocked)}")
    for reason, count in sorted(reason_counts.items()):
        print(f"    {reason}:{' ' * max(1, 20 - len(reason))}{count}")
    return 0
