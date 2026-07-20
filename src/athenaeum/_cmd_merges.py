# SPDX-License-Identifier: Apache-2.0
"""``athenaeum merges {list,next,count}`` — surface pending merge proposals.

The mirror of ``athenaeum questions`` for the resolver's merge-proposal
sidecar (``wiki/_pending_merges.md``). Before issue #401 merges had **no CLI
at all** — they were reachable only through the ``list_pending_merges`` MCP
tool, so a real backlog (34 proposals aged 1–4 weeks, found 2026-07-20) could
sit unseen because no briefing path could read it.

Each item is rendered as an **answerable question**: the source pages are
named by their human title (frontmatter ``name:``, not the uuid-slug) with a
one-line gist each, and a ``question`` field phrases the decision plainly —
so a human can decide approve/reject without opening the raw wiki files.

Three modes mirror ``questions``:

- ``list``   all unresolved merges (optionally ``--limit``, ``--json``)
- ``next``   the OLDEST unresolved merge (one block)
- ``count``  ``N unresolved (oldest: <iso-date>)`` summary
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.decisions import list_pending_merges_rich


def _resolve_merges_path(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()
    )
    return knowledge_root / "wiki" / "_pending_merges.md"


def _format_block(merge: dict) -> str:
    """Human-readable rendering for stdout (non-JSON path)."""
    lines = [
        f"## [{merge['created_at']}] Merge: {merge['merge_target_name']!r} "
        f"(confidence {merge['confidence']:.2f})",
        f"  id: {merge['id']}",
        f"  question: {merge['question']}",
    ]
    if merge.get("rationale"):
        lines.append(f"  rationale: {merge['rationale']}")
    lines.append("  sources:")
    for src in merge["sources"]:
        gist = f" — {src['gist']}" if src["gist"] else ""
        lines.append(f"    - {src['title']}{gist}")
    return "\n".join(lines)


def cmd_merges(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum merges {list,next,count}``.

    Like ``questions``, never raises on a missing/empty ``_pending_merges.md``:
    count returns 0 / null oldest, list/next print nothing and exit 0.
    """
    sub = getattr(args, "merges_target", None)
    if sub not in ("list", "next", "count"):
        print("usage: athenaeum merges {list,next,count} [...]", file=sys.stderr)
        return 2

    merges_path = _resolve_merges_path(args)
    merges = list_pending_merges_rich(merges_path)

    if sub == "count":
        oldest = merges[0]["created_at"] if merges else None
        if args.json:
            sys.stdout.write(
                json.dumps({"count": len(merges), "oldest": oldest}) + "\n"
            )
        elif not merges:
            print("0 unresolved")
        else:
            print(f"{len(merges)} unresolved (oldest: {oldest})")
        return 0

    if sub == "next":
        if not merges:
            if args.json:
                sys.stdout.write("null\n")
            return 0
        merge = merges[0]
        if args.json:
            sys.stdout.write(json.dumps(merge) + "\n")
        else:
            print(_format_block(merge))
        return 0

    # sub == "list"
    limit = getattr(args, "limit", 0) or 0
    if limit > 0:
        merges = merges[:limit]

    if args.json:
        sys.stdout.write(json.dumps(merges) + "\n")
        return 0

    if not merges:
        print("0 unresolved")
        return 0

    for idx, merge in enumerate(merges):
        if idx > 0:
            print()
        print(_format_block(merge))
    return 0


def add_merges_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum merges`` and its three modes on ``subparsers``."""
    m_parser = subparsers.add_parser(
        "merges",
        help=(
            "Inspect unresolved resolver merge proposals in "
            "`wiki/_pending_merges.md`. Three modes: list, next, count. "
            "The merges half of `athenaeum decisions`."
        ),
    )
    m_sub = m_parser.add_subparsers(dest="merges_target")

    def _add_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--path",
            type=Path,
            default=Path("~/knowledge"),
            help="Knowledge directory (default: ~/knowledge)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON instead of plain text.",
        )

    list_p = m_sub.add_parser("list", help="List all unresolved merge proposals.")
    _add_common(list_p)
    list_p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Truncate to first N (default: 0 = unlimited).",
    )

    next_p = m_sub.add_parser(
        "next", help="Show the oldest unresolved merge (single block)."
    )
    _add_common(next_p)

    count_p = m_sub.add_parser(
        "count", help="Print `N unresolved (oldest: <iso-date>)`."
    )
    _add_common(count_p)
