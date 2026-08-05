# SPDX-License-Identifier: Apache-2.0
"""``athenaeum merges {list,next,count,provenance}`` — pending + executed merges.

The mirror of ``athenaeum questions`` for the resolver's merge-proposal
sidecar (``wiki/_pending_merges.md``). Before issue athenaeum#401 merges had **no CLI
at all** — they were reachable only through the ``list_pending_merges`` MCP
tool, so a real backlog (34 proposals aged 1–4 weeks, found 2026-07-20) could
sit unseen because no briefing path could read it.

Each item is rendered as an **answerable question**: the source pages are
named by their human title (frontmatter ``name:``, not the uuid-slug) with a
one-line gist each, and a ``question`` field phrases the decision plainly —
so a human can decide approve/reject without opening the raw wiki files.

Six modes:

- ``list``        all unresolved merges (optionally ``--limit``, ``--json``)
- ``next``        the OLDEST unresolved merge (one block)
- ``count``       ``N unresolved (oldest: <iso-date>)`` summary
- ``provenance``  EXECUTED merges from ``wiki/_merge_provenance.jsonl``
                   (issue athenaeum#425) — which source pages a merge relied on,
                   queryable by ``--canonical-slug`` / ``--merge-id``.
- ``revalidate``  re-validate existing unresolved proposals against the
                   CURRENT suppression gate and archive stale ones (issue
                   athenaeum#481). Dry-run by default; ``--apply`` writes.
- ``propose-fold`` propose folding source pages INTO a named canonical page
                   (issue athenaeum#747) — the operator-facing entry point for
                   the fold path, deriving merge_target_name / write_kind so
                   no hand-built proposal is needed. Dry-run by default;
                   ``--apply`` queues via the existing ``write_pending_merge``.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this
is where a NEW subcommand goes, not inline in ``cli.py``'s ``main()``. This
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import cost
down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.decisions import list_pending_merges_rich
from athenaeum.provenance import read_merge_provenance


def _resolve_merges_path(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    )
    return knowledge_root / "wiki" / "_pending_merges.md"


def _resolve_wiki_root(args: argparse.Namespace) -> Path:
    knowledge_root = (
        (getattr(args, "path", None) or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
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


def _resolve_wiki_page(wiki_root: Path, ref: str) -> Path | None:
    """Resolve an operator-supplied page reference to an existing wiki file.

    Accepts a bare slug (``maria-springer``), a slugged filename
    (``maria-springer.md``), or a path (absolute, or relative to the wiki
    root / cwd). Returns the resolved existing file, or ``None`` when no
    candidate exists (issue athenaeum#747).
    """
    candidates: list[Path] = []
    p = Path(ref)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(wiki_root / ref)
        if not ref.endswith(".md"):
            candidates.append(wiki_root / f"{ref}.md")
        candidates.append(p)
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return None


def _cmd_propose_fold(args: argparse.Namespace) -> int:
    """``athenaeum merges propose-fold --into A --source B [--source C ...]``.

    Operator-facing entry point for the fold path (issue athenaeum#747). Proposes
    folding one or more source pages INTO a named canonical page, deriving
    every field the fold mechanics depend on so a hand-constructed proposal —
    the exact origin of the athenaeum#748 ``write_kind`` misclassification — is
    unnecessary:

    - ``merge_target_name`` is read from the canonical page's ``name:``
      frontmatter (never taken as a string), so the target slug and the page
      cannot disagree.
    - ``write_kind`` is DERIVED by :func:`write_pending_merge` (athenaeum#748),
      never accepted from the caller.
    - ``draft_merged_body`` defaults to the canonical page's current text
      VERBATIM (a fold's value is alias bookkeeping + source deletion, not a
      body rewrite); ``--draft-file`` overrides for a genuine content merge.

    Dry-run by default (writes nothing, prints the plan); ``--apply`` queues
    the proposal via the existing :func:`write_pending_merge` path. Approval
    stays the separate, unchanged ``resolve_merge`` step — no second write
    path is introduced.
    """
    from athenaeum.models import parse_frontmatter, slugify
    from athenaeum.pending_merges import (
        _make_id,
        classify_write_kind,
        write_pending_merge,
    )

    wiki_root = _resolve_wiki_root(args)

    def _fail(message: str) -> int:
        if args.json:
            sys.stdout.write(json.dumps({"ok": False, "error": message}) + "\n")
        else:
            print(f"error: {message}", file=sys.stderr)
        return 2

    # 1. Resolve the canonical --into page.
    into_path = _resolve_wiki_page(wiki_root, args.into)
    if into_path is None:
        return _fail(
            f"--into {args.into!r} is not an existing wiki page under {wiki_root}"
        )

    into_meta, _ = parse_frontmatter(into_path.read_text(encoding="utf-8"))
    name = into_meta.get("name") if isinstance(into_meta, dict) else None
    if not name or not str(name).strip():
        return _fail(
            f"canonical page {into_path} has no usable `name:` frontmatter to "
            "derive the merge target name from"
        )
    merge_target_name = str(name)
    target_slug = slugify(merge_target_name)

    # The fold classifies by slugify(name); if the canonical page's own
    # filename is not that slug (e.g. a uid-prefixed `<uid>-<slug>.md`), the
    # derived write_kind would be `create-merged` and the fold would silently
    # NOT delete the sources. Refuse with a clear, actionable error rather
    # than queue a proposal that will not fold — this is the athenaeum#748
    # misclassification, prevented at proposal time.
    expected = wiki_root / f"{target_slug}.md"
    if not expected.is_file() or expected.resolve() != into_path:
        return _fail(
            f"canonical page {into_path.name} has name {merge_target_name!r}, "
            f"which slugifies to {target_slug!r}, but {expected.name} is not "
            "that page. Rename the page so its filename matches its slug "
            "(`athenaeum storage migrate-pii --rename-only`) before folding."
        )

    # 2. Resolve the source pages.
    source_paths: list[Path] = []
    for s in args.source:
        sp = _resolve_wiki_page(wiki_root, s)
        if sp is None:
            return _fail(f"--source {s!r} is not an existing wiki page under {wiki_root}")
        if sp == into_path:
            return _fail(
                f"--source {s!r} is the same page as --into; a page cannot be "
                "folded into itself"
            )
        source_paths.append(sp)

    # Sources are [canonical, *folded] — the canonical reappears in its own
    # source set (harmless: the fold write path excludes it from deletion).
    sources = [str(into_path)] + [str(p) for p in source_paths]

    # 3. Draft body — verbatim canonical text unless overridden.
    if args.draft_file:
        draft_path = Path(args.draft_file).expanduser()
        if not draft_path.is_file():
            return _fail(f"--draft-file {args.draft_file!r} does not exist")
        draft_merged_body = draft_path.read_text(encoding="utf-8")
        draft_source = str(draft_path)
    else:
        draft_merged_body = into_path.read_text(encoding="utf-8")
        draft_source = "canonical page (verbatim)"

    derived_write_kind = classify_write_kind(merge_target_name, wiki_root)
    merge_id = _make_id(sources, merge_target_name)
    rationale = (
        args.rationale
        or f"Operator-proposed fold of {len(source_paths)} page(s) into "
        f"{merge_target_name!r} via `merges propose-fold`."
    )

    plan = {
        "ok": True,
        "applied": bool(args.apply),
        "id": merge_id,
        "merge_target_name": merge_target_name,
        "canonical_page": str(into_path),
        "sources": sources,
        "folded_sources": [str(p) for p in source_paths],
        "write_kind": derived_write_kind,
        "draft_source": draft_source,
        "rationale": rationale,
    }

    if not args.apply:
        if args.json:
            sys.stdout.write(json.dumps(plan) + "\n")
            return 0
        print(f"Dry-run — proposing fold into {merge_target_name!r}:")
        print(f"  canonical page: {into_path}")
        print(f"  derived write_kind: {derived_write_kind}")
        print(f"  draft body: {draft_source}")
        print("  folded sources:")
        for p in source_paths:
            print(f"    - {p}")
        print("\nNo changes written. Re-run with --apply to queue the proposal.")
        return 0

    write_pending_merge(
        wiki_root / "_pending_merges.md",
        merge_target_name=merge_target_name,
        sources=sources,
        rationale=rationale,
        draft_merged_body=draft_merged_body,
        confidence=1.0,
        write_kind=None,  # derived (athenaeum#748) — never taken from the caller
    )

    if args.json:
        sys.stdout.write(json.dumps(plan) + "\n")
        return 0
    print(f"Queued fold proposal {merge_id} into {merge_target_name!r} "
          f"({len(source_paths)} source(s)). Approve with the resolver "
          "(`resolve_merge`) — approval is unchanged.")
    return 0


def _cmd_revalidate(args: argparse.Namespace) -> int:
    """``athenaeum merges revalidate [--apply]`` — issue athenaeum#481.

    Re-validate existing unresolved ``_pending_merges.md`` blocks against the
    CURRENT suppression gate and archive stale ones. Dry-run by default,
    mirroring ``authority convert``'s shape: it reports what WOULD be retired
    and writes nothing unless ``--apply`` is passed.
    """
    from athenaeum.config import load_config
    from athenaeum.pending_merges import revalidate_pending_merges

    knowledge_root = (
        (getattr(args, "path", None) or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
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
    if sub not in (
        "list",
        "next",
        "count",
        "provenance",
        "revalidate",
        "propose-fold",
    ):
        print(
            "usage: athenaeum merges "
            "{list,next,count,provenance,revalidate,propose-fold} [...]",
            file=sys.stderr,
        )
        return 2

    if sub == "propose-fold":
        return _cmd_propose_fold(args)

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
    m_parser.set_defaults(func=cmd_merges)
    m_sub = m_parser.add_subparsers(dest="merges_target")

    def _add_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--path",
            type=Path,
            default=DEFAULT_KNOWLEDGE_ROOT,
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
            "CURRENT suppression gate and archive stale ones (issue athenaeum#481). "
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

    fold_p = m_sub.add_parser(
        "propose-fold",
        help=(
            "Propose folding one or more source pages INTO a named canonical "
            "page (issue athenaeum#747). Derives merge_target_name from the "
            "canonical page's `name:` and write_kind from the corpus — no "
            "hand-built proposal. Dry-run by default; --apply to queue."
        ),
    )
    _add_common(fold_p)
    fold_p.add_argument(
        "--into",
        required=True,
        help=(
            "The canonical page to fold sources into (a slug, a `<slug>.md` "
            "filename, or a path). Must be an existing wiki page whose "
            "filename matches its `name:` slug."
        ),
    )
    fold_p.add_argument(
        "--source",
        action="append",
        default=[],
        required=True,
        metavar="PAGE",
        help=(
            "A source page to fold away (repeatable). Each must be an existing "
            "wiki page and must not equal --into."
        ),
    )
    fold_p.add_argument(
        "--draft-file",
        default=None,
        help=(
            "Override the merged draft body with this file's contents (for a "
            "genuine content merge). Default: the canonical page's current "
            "text VERBATIM."
        ),
    )
    fold_p.add_argument(
        "--rationale",
        default=None,
        help="Optional human rationale recorded on the proposal.",
    )
    fold_p.add_argument(
        "--apply",
        action="store_true",
        help="Queue the proposal. Default: dry-run — print the plan, write nothing.",
    )

    provenance_p = m_sub.add_parser(
        "provenance",
        help=(
            "List EXECUTED merges from `wiki/_merge_provenance.jsonl` "
            "(issue athenaeum#425) — which source pages each merge relied on."
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
