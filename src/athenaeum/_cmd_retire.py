# SPDX-License-Identifier: Apache-2.0
"""``athenaeum retire-pages`` — retire explicit wiki pages by uid (issue athenaeum#1625).

Mirrors ``_cmd_decay.py``'s CLI shape exactly (dry-run default, ``--apply``
git-archives the kill-list in a two-commit pair and rebuilds the recall
index) — see ``athenaeum.retire_pages`` for the domain logic and why this
convention rather than a new one.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum._cli_shared import (
    _acquire_or_exit,
    _add_lock_args,
    rebuild_recall_index,
)
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

#: Recovery is git-only (issue athenaeum#1625 Plan step 7): no tombstone, no
#: second store. Named here once and reused in both the ``--apply`` help
#: text and the post-commit success message so the two can never drift.
_RECOVERY_HINT = (
    "the retired page's content lives in the provenance-snapshot commit "
    "(HEAD~1 immediately after this run's archive commit) — run `git show "
    "<that-commit-sha>:<page-path>` to recover it, or `git log "
    "--diff-filter=D -- <page-path>` first if you need to find the commit."
)


def add_retire_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``retire-pages``."""

    parser = subparsers.add_parser(
        "retire-pages",
        help="Retire explicit wiki pages by uid (issue athenaeum#1625). "
        "Default is dry-run (prints the kill-list, affected pending-merge "
        "proposals, and index entries); --apply git-archives the kill-list "
        "in a two-commit pair, withdraws referencing pending-merge "
        "proposals, rebuilds wiki/_index.md, and rebuilds the recall "
        f"index. Unknown or ambiguous uids abort before any commit. "
        f"Recovery is git-only: {_RECOVERY_HINT}",
    )
    parser.add_argument(
        "--uids",
        nargs="+",
        required=True,
        metavar="FILE|UID",
        help="Either a single path to a file listing one uid per line "
        "(blank lines and lines starting with '#' are ignored), or one or "
        "more literal uids. Every uid must resolve to exactly one wiki "
        "page; an unknown or ambiguous uid aborts the whole run before any "
        "commit.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Git-archive the kill-list (two-commit: provenance snapshot, "
        "then git rm + index rebuild + pending-merge withdrawal) and "
        "rebuild the recall index. Without this flag the command is a "
        f"dry-run. Recovery is git-only: {_RECOVERY_HINT}",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Why these pages are being retired. Recorded verbatim in both "
        "commit messages (--apply only) and in every withdrawn "
        "pending-merge proposal's archive note.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for the recall index rebuild "
        "(default: ~/.cache/athenaeum). --apply only.",
    )
    parser.add_argument(
        "--backend",
        choices=["fts5", "vector"],
        default=None,
        help="Override the recall index backend for the rebuild "
        "(default: read from athenaeum.yaml). --apply only.",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_retire_pages)


def _read_uids(tokens: list[str]) -> list[str]:
    """Resolve ``--uids``' ``FILE|UID...`` shape to a flat uid list.

    A single token that names an existing file is read as one uid per
    line (blank lines and ``#``-comments dropped); any other shape (one or
    several tokens that are not, singly, an existing file) is treated as
    literal uids directly.
    """
    if len(tokens) == 1 and Path(tokens[0]).is_file():
        lines = Path(tokens[0]).read_text(encoding="utf-8").splitlines()
        return [
            stripped
            for line in lines
            if (stripped := line.strip()) and not stripped.startswith("#")
        ]
    return list(tokens)


def cmd_retire_pages(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum retire-pages``.

    Exit codes (mirroring ``decay-sweep``):
        0 - clean run (no uids resolved, OR ``--apply`` succeeded with no
            errors).
        1 - errors encountered (unknown/ambiguous uid, apply without git,
            withdrawal/rebuild failure, ...).
        2 - dry-run found pages that WOULD be retired (CI / sign-off signal).
    """
    from athenaeum.config import load_config
    from athenaeum.retire_pages import apply_retirement, build_retire_report, resolve_uids

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    uids = _read_uids(args.uids)
    candidates, resolution_errors = resolve_uids(wiki_root, uids)
    if resolution_errors:
        # Unknown/ambiguous uids abort before any commit (issue athenaeum#1625
        # AC): resolution is pure read-only frontmatter scanning above —
        # nothing has touched git yet.
        for err in resolution_errors:
            print(f"  ERR {err}", file=sys.stderr)
        return 1

    if not candidates:
        print("No uids given - nothing to retire.")
        return 0

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== retire-pages ({mode}) ===")
    print(f"  reason: {args.reason}")
    print(f"  kill:   {len(candidates)}")
    for cand in candidates:
        print(f"    {cand.uid}: {cand.path.name}")

    preview = build_retire_report(knowledge_root, candidates)
    if preview.withdrawn_merges:
        print("\n  PENDING MERGES TO WITHDRAW:")
        for w in preview.withdrawn_merges:
            print(f"    {w.merge_target_name}: {w.reason}")
    if preview.index_lines_removed:
        print("\n  INDEX ENTRIES TO REMOVE:")
        for name in preview.index_lines_removed:
            print(f"    {name}")

    if not args.apply:
        return 2

    cfg = load_config(knowledge_root)

    # --apply (mutating): acquire the single-machine run lock (issue athenaeum#309).
    lock = _acquire_or_exit(knowledge_root, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        report = apply_retirement(knowledge_root, candidates, reason=args.reason)
        for apply_err in report.errors:
            print(f"  ERR {apply_err}", file=sys.stderr)
        if report.errors:
            return 1

        if report.committed:
            print(
                f"\n  retired {len(candidates)} page(s); committed. "
                f"Recovery: {_RECOVERY_HINT}"
            )
            rebuild_recall_index(knowledge_root, cfg, args)
        else:
            print("\n  nothing retired.")
        return 0
    finally:
        lock.release()
