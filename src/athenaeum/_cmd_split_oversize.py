# SPDX-License-Identifier: Apache-2.0
"""``athenaeum split-oversize`` -- split oversized pages after merging into
them (issue athenaeum#2078 AC3).

Dry-run by default. ``--apply`` splits the pages the operator approved with
``--uid`` and, with ``--auto``, every page over the configurable hard ceiling
(``librarian.oversize_hard_ceiling_chars``). stdout carries counts only; the
per-page detail (uids, filenames) goes to ``--report`` when given. Domain
logic lives in :mod:`athenaeum.oversize_split`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.runlock import RunLock


def add_split_oversize_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``split-oversize``."""
    parser = subparsers.add_parser(
        "split-oversize",
        help="Split oversized pages (flagged needs_split after a merge, or over "
        "the hard ceiling) into a hub plus linked atomic pages (issue "
        "athenaeum#2078). Default is dry-run. --apply splits the pages named "
        "with --uid; --auto also splits every page over "
        "librarian.oversize_hard_ceiling_chars. Makes no LLM call.",
    )
    parser.add_argument(
        "--uid",
        action="append",
        default=[],
        metavar="UID",
        help="Operator-approved page to split (repeatable).",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Also split every candidate over the hard ceiling without "
        "per-page approval.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the splits. Without this flag nothing is written "
        "except --report.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="PATH",
        help="Write per-page JSON detail here (never printed to stdout).",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_split_oversize)


def cmd_split_oversize(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum split-oversize``.

    Exit codes: 0 clean run; 1 wiki root missing or report unwritable;
    75 the run lock is held (--apply only).
    """
    from athenaeum.config import load_config
    from athenaeum.oversize_split import find_split_candidates, split_pages

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    candidates = find_split_candidates(wiki_root, cfg)
    lock: RunLock | None = None
    if args.apply:
        acquired = _acquire_or_exit(knowledge_root, args, cfg)
        if isinstance(acquired, int):
            return acquired
        lock = acquired
    try:
        outcomes = split_pages(
            wiki_root,
            uids=set(args.uid),
            auto=args.auto,
            config=cfg,
            dry_run=not args.apply,
        )
    finally:
        if lock is not None:
            lock.release()

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== split-oversize ({mode}) ===")
    print(f"  candidates:           {len(candidates)}")
    print(f"    flagged:            {sum(c.flagged for c in candidates)}")
    print(f"    over hard ceiling:  {sum(c.over_ceiling for c in candidates)}")
    for status, count in sorted(Counter(o.status for o in outcomes).items()):
        print(f"  {status + ':':22}{count}")
    if args.report is not None:
        try:
            args.report.write_text(
                json.dumps(
                    [
                        {"uid": o.uid, "file": o.path.name, "chars": o.chars, "status": o.status}
                        for o in outcomes
                    ],
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"error: writing the report failed: {exc}", file=sys.stderr)
            return 1
        print(f"  report:               {args.report}")
    return 0
