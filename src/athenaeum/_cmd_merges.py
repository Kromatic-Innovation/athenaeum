# SPDX-License-Identifier: Apache-2.0
"""``athenaeum merges {list,next,count,provenance}`` — pending + executed merges.

The mirror of ``athenaeum questions`` for the resolver's merge-proposal
sidecar (``wiki/_pending_merges.md``). Before issue #401 merges had **no CLI
at all** — they were reachable only through the ``list_pending_merges`` MCP
tool, so a real backlog (34 proposals aged 1–4 weeks, found 2026-07-20) could
sit unseen because no briefing path could read it.

Each item is rendered as an **answerable question**: the source pages are
named by their human title (frontmatter ``name:``, not the uuid-slug) with a
one-line gist each, and a ``question`` field phrases the decision plainly —
so a human can decide approve/reject without opening the raw wiki files.

Five modes:

- ``list``        all unresolved merges (optionally ``--limit``, ``--json``)
- ``next``        the OLDEST unresolved merge (one block)
- ``count``       ``N unresolved (oldest: <iso-date>)`` summary
- ``provenance``  EXECUTED merges from ``wiki/_merge_provenance.jsonl``
                   (issue #425) — which source pages a merge relied on,
                   queryable by ``--canonical-slug`` / ``--merge-id``.
- ``revalidate``  re-validate existing unresolved proposals against the
                   CURRENT suppression gate and archive stale ones (issue
                   #481). Dry-run by default; ``--apply`` writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.decisions import list_pending_merges_rich
from athenaeum.provenance import read_merge_provenance


def _resolve_merges_path(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()
    )
    return knowledge_root / "wiki" / "_pending_merges.md"


def _resolve_wiki_root(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()
    )
    return knowledge_root / "wiki"


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


def _format_provenance_record(record: dict) -> str:
    """Human-readable rendering for one executed-merge provenance record."""
    lines = [
        f"## [{record.get('ts', '?')}] merge {record.get('merge_id', '?')} "
        f"({record.get('write_kind', '?')})",
        f"  canonical: {record.get('canonical_slug', '?')}",
        "  sources:",
    ]
    for src in record.get("source_paths") or []:
        lines.append(f"    - {src}")
    return "\n".join(lines)


def _cmd_revalidate(args: argparse.Namespace) -> int:
    """``athenaeum merges revalidate [--apply]`` — issue #481.

    Re-validate existing unresolved ``_pending_merges.md`` blocks against the
    CURRENT suppression gate and archive stale ones. Dry-run by default,
    mirroring ``authority convert``'s shape: it reports what WOULD be retired
    and writes nothing unless ``--apply`` is passed.
    """
    from athenaeum.config import load_config
    from athenaeum.pending_merges import revalidate_pending_merges

    knowledge_root = (
        (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()
    )
    merges_path = knowledge_root / "wiki" / "_pending_merges.md"
    config = load_config(knowledge_root)
    apply = getattr(args, "apply", False)

    result = revalidate_pending_merges(merges_path, config=config, apply=apply)

    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "applied": result.applied,
                    "kept": result.kept,
                    "retired": [
                        {
                            "id": r.id,
                            "merge_target_name": r.merge_target_name,
                            "n_sources": r.n_sources,
                            "confidence": r.confidence,
                            "reason": r.reason,
                        }
                        for r in result.retired
                    ],
                }
            )
            + "\n"
        )
        return 0

    if not result.retired:
        print("0 stale proposals — nothing to retire against the current gate")
        return 0

    verb = "Retired" if result.applied else "Would retire"
    print(
        f"{verb} {len(result.retired)} stale proposal(s) against the current "
        f"suppression gate:"
    )
    for r in result.retired:
        print(f"  - {r.merge_target_name!r} ({r.n_sources} sources): {r.reason}")
    if not result.applied:
        print(
            "\nDry-run — no changes written. Re-run with --apply to archive "
            "them to _pending_merges_archive.md."
        )
    return 0


def cmd_merges(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum merges {list,next,count,provenance}``.

    Like ``questions``, never raises on a missing/empty ``_pending_merges.md``:
    count returns 0 / null oldest, list/next print nothing and exit 0. Same
    discipline for ``provenance`` against a missing/empty
    ``_merge_provenance.jsonl``.
    """
    sub = getattr(args, "merges_target", None)
    if sub not in ("list", "next", "count", "provenance", "revalidate"):
        print(
            "usage: athenaeum merges "
            "{list,next,count,provenance,revalidate} [...]",
            file=sys.stderr,
        )
        return 2

    if sub == "revalidate":
        return _cmd_revalidate(args)

    if sub == "provenance":
        wiki_root = _resolve_wiki_root(args)
        records = read_merge_provenance(
            wiki_root,
            canonical_slug=getattr(args, "canonical_slug", None),
            merge_id=getattr(args, "merge_id", None),
        )
        if args.json:
            sys.stdout.write(json.dumps(records) + "\n")
            return 0
        if not records:
            print("0 recorded")
            return 0
        for idx, record in enumerate(records):
            if idx > 0:
                print()
            print(_format_provenance_record(record))
        return 0

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

    revalidate_p = m_sub.add_parser(
        "revalidate",
        help=(
            "Re-validate existing unresolved merge proposals against the "
            "CURRENT suppression gate and archive stale ones (issue #481). "
            "Dry-run by default; pass --apply to write."
        ),
    )
    _add_common(revalidate_p)
    revalidate_p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Archive proposals the current gate would suppress. Default: "
            "dry-run — report only, write nothing."
        ),
    )

    provenance_p = m_sub.add_parser(
        "provenance",
        help=(
            "List EXECUTED merges from `wiki/_merge_provenance.jsonl` "
            "(issue #425) — which source pages each merge relied on."
        ),
    )
    _add_common(provenance_p)
    provenance_p.add_argument(
        "--canonical-slug",
        default=None,
        help="Filter to records for this canonical target slug.",
    )
    provenance_p.add_argument(
        "--merge-id",
        default=None,
        help="Filter to the record for this merge id.",
    )
