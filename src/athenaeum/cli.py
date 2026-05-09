# SPDX-License-Identifier: Apache-2.0
"""Athenaeum CLI entry point."""

import argparse
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athenaeum",
        description="Knowledge management pipeline — append-only intake, tiered compilation",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {_get_version()}"
    )
    subparsers = parser.add_subparsers(dest="command")

    # init command
    init_parser = subparsers.add_parser(
        "init", help="Initialize a new knowledge directory"
    )
    init_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Target directory (default: ~/knowledge)",
    )
    init_parser.add_argument(
        "--with-templates",
        action="store_true",
        help="Also copy bundled entity-author templates "
        "(person/company/project/concept/source) into <path>/templates/.",
    )
    init_parser.add_argument(
        "--templates-dest",
        type=Path,
        default=None,
        help="Override the templates destination directory "
        "(default: <path>/templates).",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing template files at the destination "
        "(only applies with --with-templates).",
    )

    # status command
    status_parser = subparsers.add_parser("status", help="Show knowledge base status")
    status_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )

    # serve command — start the MCP memory server
    serve_parser = subparsers.add_parser("serve", help="Start the MCP memory server")
    serve_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )

    # run command — execute the librarian pipeline
    run_parser = subparsers.add_parser("run", help="Run the librarian pipeline")
    run_parser.add_argument(
        "--raw-root",
        type=Path,
        default=None,
        help="Raw intake directory (default: ~/knowledge/raw)",
    )
    run_parser.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="Wiki output directory (default: ~/knowledge/wiki)",
    )
    run_parser.add_argument(
        "--knowledge-root",
        type=Path,
        default=None,
        help="Knowledge git repo root (default: ~/knowledge)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without writing files or committing",
    )
    run_parser.add_argument(
        "--max-files",
        type=int,
        default=50,
        help="Stop after processing this many raw files (default: 50)",
    )
    run_parser.add_argument(
        "--max-api-calls",
        type=int,
        default=200,
        help="Maximum estimated API calls per run (default: 200)",
    )
    run_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    run_parser.add_argument(
        "--cluster-only",
        action="store_true",
        help="Only run C2 auto-memory discovery + clustering — skip the "
        "entity tier pipeline. Writes the cluster JSONL report and "
        "exits. Useful for validating the cluster output before C3.",
    )
    run_parser.add_argument(
        "--merge-only",
        action="store_true",
        help="Only run C3 cluster merge — read the canonical cluster "
        "JSONL from the last C2 run and emit wiki/auto-*.md entries. "
        "Skips discovery, clustering, and the entity tier pipeline.",
    )

    # test-mcp command — smoke-test the MCP memory setup without a session
    test_mcp_parser = subparsers.add_parser(
        "test-mcp",
        help="Smoke-test MCP remember/recall against a synthetic knowledge dir",
    )
    test_mcp_parser.add_argument(
        "--keep",
        action="store_true",
        help="Don't delete the temp knowledge dir on exit (for debugging)",
    )

    # people command — frontmatter-only filter over type:person wikis
    people_parser = subparsers.add_parser(
        "people",
        help="List type:person wikis filtered by frontmatter (company / tag / tier / score). "
        "No LLM, no embeddings — deterministic over the wiki tree.",
    )
    people_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    people_parser.add_argument(
        "--company",
        action="append",
        default=[],
        help=(
            "Match current_company OR linkedin_company_at_connect "
            "(case-insensitive substring). Repeat to AND."
        ),
    )
    people_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Require this exact tag (repeat to AND).",
    )
    people_parser.add_argument(
        "--tier",
        default="",
        help="Shorthand for --tag tier:<value> (warm-a / warm-b / warm-c / extended / active).",
    )
    people_parser.add_argument(
        "--title-regex",
        action="append",
        default=[],
        help=(
            "Match current_title OR linkedin_position_at_connect against this "
            "regex (case-insensitive). Repeat to AND multiple patterns."
        ),
    )
    people_parser.add_argument(
        "--company-regex",
        action="append",
        default=[],
        help=(
            "Match current_company OR linkedin_company_at_connect against this "
            "regex (case-insensitive). Repeat to AND multiple patterns."
        ),
    )
    people_parser.add_argument(
        "--top-touch",
        type=int,
        default=0,
        help="Sort by recent-touch signal (meeting+sent counts) and return top N. "
        "Default sort is by warm_score desc.",
    )
    people_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max rows to print (default: 50; 0 = unlimited)",
    )
    people_parser.add_argument(
        "--format",
        choices=["table", "tsv"],
        default="table",
        help="Output shape (default: table).",
    )

    # query-topics command — LLM-based topic extraction for hook query rewriting
    query_topics_parser = subparsers.add_parser(
        "query-topics",
        help="Extract substantive search topics from a prompt (Haiku). "
        "Used by the UserPromptSubmit hook to rewrite queries before "
        "FTS5/vector search. Prints one topic per line to stdout; "
        "empty output means fall back to the caller's built-in extractor.",
    )
    query_topics_parser.add_argument(
        "prompt",
        type=str,
        help="The user's raw message.",
    )
    query_topics_parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Seconds to wait for the LLM before giving up (default: 3.0)",
    )

    # stopwords command — print the canonical stopword list for shell hooks
    subparsers.add_parser(
        "stopwords",
        help="Print the stopword list (one word per line). "
        "Used by the example UserPromptSubmit hook's regex fallback "
        "to stay in sync with the FTS5 query filter.",
    )

    # ingest-answers command — convert resolved `[x]` blocks in
    # _pending_questions.md into raw intake files and archive the answered
    # blocks. Idempotent — safe to run from a scheduler.
    ingest_answers_parser = subparsers.add_parser(
        "ingest-answers",
        help="Ingest answered pending questions from _pending_questions.md",
    )
    ingest_answers_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )

    # recall command — shell-accessible recall for validation harnesses
    # and operator debugging. Wraps the MCP `recall` tool so scripts and
    # `gh_wait_status.sh`-style tooling can exercise the same search path
    # without spinning up a Claude Code session.
    recall_parser = subparsers.add_parser(
        "recall",
        help="Search the wiki from the shell (one tab-separated hit per line)",
    )
    recall_parser.add_argument(
        "query",
        type=str,
        help="Search query string",
    )
    recall_parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Maximum results to return (default: 5)",
    )
    recall_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    recall_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory (default: ~/.cache/athenaeum)",
    )
    recall_parser.add_argument(
        "--backend",
        choices=["keyword", "fts5", "vector"],
        default=None,
        help="Override configured backend (default: read from athenaeum.yaml)",
    )

    # dedupe command — find / merge duplicate person wikis
    dedupe_parser = subparsers.add_parser(
        "dedupe",
        help="Find or merge duplicate wiki entries.",
    )
    dedupe_sub = dedupe_parser.add_subparsers(dest="dedupe_target")
    dedupe_persons = dedupe_sub.add_parser(
        "persons",
        help="Person-wiki dedupe (HIGH-confidence apollo_id / linkedin / "
        "exact-name match). Default --find prints a YAML report; "
        "--apply consumes the report and merges.",
    )
    dedupe_persons.add_argument(
        "--find",
        action="store_true",
        help="Discover duplicate pairs and write a YAML report.",
    )
    dedupe_persons.add_argument(
        "--apply",
        action="store_true",
        help="Read a report and perform the merge (idempotent).",
    )
    dedupe_persons.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="Wiki directory (default: ~/knowledge/wiki).",
    )
    dedupe_persons.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Path to write the YAML report (default: stdout). --find only.",
    )
    dedupe_persons.add_argument(
        "--from",
        dest="from_path",
        type=Path,
        default=None,
        help="Path to the YAML report to apply (default: stdin). --apply only.",
    )

    # repair command — frontmatter YAML repair tools (tag-indent, value-quoting)
    repair_parser = subparsers.add_parser(
        "repair",
        help="Repair YAML-frontmatter corruption in wiki files. "
        "Default is dry-run; pass --apply to write fixes.",
    )
    repair_mode = repair_parser.add_mutually_exclusive_group(required=True)
    repair_mode.add_argument(
        "--tag-indent",
        action="store_true",
        help="Normalize block-list indentation under top-level keys "
        "(tags:, emails:, aliases:, ...).",
    )
    repair_mode.add_argument(
        "--value-quoting",
        action="store_true",
        help="Quote unquoted YAML values that break safe_load "
        "(values starting with '-' or '[').",
    )
    repair_mode.add_argument(
        "--all",
        action="store_true",
        help="Run all repair passes in sequence (tag-indent then value-quoting).",
    )
    repair_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write fixes. Without this flag, the command is a dry-run.",
    )
    repair_parser.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="Wiki directory (default: ~/knowledge/wiki)",
    )

    # rebuild-index command — rebuild the search index out-of-band
    rebuild_parser = subparsers.add_parser(
        "rebuild-index",
        help="Rebuild the search index (FTS5 or vector, per config)",
    )
    rebuild_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    rebuild_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory (default: ~/.cache/athenaeum)",
    )
    rebuild_parser.add_argument(
        "--backend",
        choices=["fts5", "vector"],
        default=None,
        help="Override configured backend (default: read from athenaeum.yaml)",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "init":
        return _cmd_init(args)

    if args.command == "status":
        return _cmd_status(args)

    if args.command == "serve":
        return _cmd_serve(args)

    if args.command == "run":
        return _cmd_run(args)

    if args.command == "ingest-answers":
        return _cmd_ingest_answers(args)

    if args.command == "rebuild-index":
        return _cmd_rebuild_index(args)

    if args.command == "recall":
        return _cmd_recall(args)

    if args.command == "people":
        return _cmd_people(args)

    if args.command == "query-topics":
        return _cmd_query_topics(args)

    if args.command == "test-mcp":
        return _cmd_test_mcp(args)

    if args.command == "stopwords":
        return _cmd_stopwords(args)

    if args.command == "dedupe":
        return _cmd_dedupe(args)

    if args.command == "repair":
        return _cmd_repair(args)

    return 0


def _cmd_dedupe(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum dedupe persons --find|--apply``."""
    from athenaeum.dedupe import (
        find_duplicate_persons,
        merge_duplicate_persons,
        pairs_from_yaml,
        pairs_to_yaml,
    )

    target = getattr(args, "dedupe_target", None)
    if target != "persons":
        print("usage: athenaeum dedupe persons [--find | --apply] ...", file=sys.stderr)
        return 2

    wiki_root = (args.wiki_root or Path("~/knowledge/wiki")).expanduser().resolve()

    if args.find and args.apply:
        print("error: pass either --find or --apply, not both", file=sys.stderr)
        return 2
    if not args.find and not args.apply:
        print("error: pass --find or --apply", file=sys.stderr)
        return 2

    if args.find:
        if not wiki_root.is_dir():
            print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
            return 1
        pairs = find_duplicate_persons(wiki_root)
        report = pairs_to_yaml(pairs)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(report, encoding="utf-8")
            print(f"Wrote {len(pairs)} pair(s) → {args.out}", file=sys.stderr)
        else:
            sys.stdout.write(report)
        return 0

    # --apply
    if args.from_path:
        text = args.from_path.read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    pairs = pairs_from_yaml(text)
    merge_report = merge_duplicate_persons(pairs, apply=True, wiki_root=wiki_root)
    print(
        f"merged={merge_report.merged} "
        f"already_merged={merge_report.already_merged} "
        f"missing_canonical={merge_report.missing_canonical} "
        f"skipped_parse={merge_report.skipped_parse} "
        f"errors={len(merge_report.errors)}"
    )
    for err in merge_report.errors:
        print(f"  ERROR: {err}", file=sys.stderr)
    return 0 if not merge_report.errors else 1


def _cmd_init(args: argparse.Namespace) -> int:
    from athenaeum.init import copy_templates, init_knowledge_dir

    target = init_knowledge_dir(args.path)
    print(f"Initialized knowledge directory at {target}")

    if getattr(args, "with_templates", False):
        dest = args.templates_dest if args.templates_dest else target / "templates"
        dest = dest.expanduser().resolve()
        written, skipped = copy_templates(dest, force=args.force)
        for fname in written:
            print(f"  wrote   {dest / fname}")
        for fname in skipped:
            print(f"  skipped {dest / fname} (exists; pass --force to overwrite)")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from athenaeum.status import format_status, status

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}")
        print(f"Run 'athenaeum init --path {args.path}' first, then retry.")
        return 1
    info = status(target)
    print(format_status(info))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from athenaeum.config import load_config, resolve_extra_intake_roots
    from athenaeum.mcp_server import create_server

    target = args.path.expanduser().resolve()
    raw_root = target / "raw"
    wiki_root = target / "wiki"

    if not target.exists():
        print(f"Knowledge directory not found: {target}")
        print(f"Run 'athenaeum init --path {args.path}' first, then retry.")
        return 1

    cfg = load_config(target)
    backend = cfg.get("search_backend", "fts5")
    cache_dir = Path("~/.cache/athenaeum").expanduser()
    extra_roots = resolve_extra_intake_roots(target, cfg)

    # Warn on config/cache mismatch. The recall tool silently returns zero
    # hits when the configured backend's index is missing, so users with
    # `search_backend: vector` but an fts5-only cache (common when you flip
    # backends in athenaeum.yaml but forget to rebuild) see recall "work"
    # but return nothing. Catch that up front.
    _warn_if_backend_cache_missing(backend, cache_dir)

    server = create_server(
        raw_root=raw_root,
        wiki_root=wiki_root,
        search_backend=backend,
        cache_dir=cache_dir,
        extra_roots=extra_roots,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


def _warn_if_backend_cache_missing(backend: str, cache_dir: Path) -> None:
    """Print a warning if the configured backend has no cache on disk.

    The keyword backend has no cache. FTS5 expects ``wiki-index.db``;
    vector expects ``wiki-vectors/``. When either is missing, recall
    silently returns empty — the warning tells the user to run
    ``athenaeum rebuild-index``.
    """
    if backend == "keyword":
        return
    if backend == "fts5":
        if not (cache_dir / "wiki-index.db").is_file():
            print(
                f"[warn] search_backend=fts5 but no index at "
                f"{cache_dir / 'wiki-index.db'}.\n"
                f"       Run `athenaeum rebuild-index --path <knowledge>` "
                f"before relying on recall.",
                file=sys.stderr,
            )
        return
    if backend == "vector":
        if not (cache_dir / "wiki-vectors").is_dir():
            print(
                f"[warn] search_backend=vector but no index at "
                f"{cache_dir / 'wiki-vectors'}.\n"
                f"       Run `athenaeum rebuild-index --path <knowledge>` "
                f"before relying on recall.",
                file=sys.stderr,
            )
        return
    print(
        f"[warn] unknown search_backend {backend!r}; "
        f"recall will fail until this is fixed in athenaeum.yaml.",
        file=sys.stderr,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    from athenaeum.librarian import DEFAULT_KNOWLEDGE_ROOT, run

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    knowledge_root = args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT
    raw_root = args.raw_root or (knowledge_root / "raw")
    wiki_root = args.wiki_root or (knowledge_root / "wiki")

    return run(
        raw_root=raw_root,
        wiki_root=wiki_root,
        knowledge_root=knowledge_root,
        dry_run=args.dry_run,
        max_files=args.max_files,
        max_api_calls=args.max_api_calls,
        cluster_only=getattr(args, "cluster_only", False),
        merge_only=getattr(args, "merge_only", False),
    )


def _cmd_ingest_answers(args: argparse.Namespace) -> int:
    """Ingest answered blocks from `_pending_questions.md` as raw intake.

    See :func:`athenaeum.answers.ingest_answers` for the semantics.
    """
    from athenaeum.answers import ingest_answers

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}", file=sys.stderr)
        print(
            f"Run 'athenaeum init --path {args.path}' first, then retry.",
            file=sys.stderr,
        )
        return 1

    pending_path = target / "wiki" / "_pending_questions.md"
    raw_root = target / "raw"

    try:
        count = ingest_answers(pending_path, raw_root)
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(
            f"Fatal error ingesting answers ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2

    print(f"Ingested {count} answered question(s).")
    return 0


def _cmd_rebuild_index(args: argparse.Namespace) -> int:
    from athenaeum.config import load_config, resolve_extra_intake_roots
    from athenaeum.search import build_fts5_index, build_vector_index

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    cache_dir = (args.cache_dir or Path("~/.cache/athenaeum")).expanduser().resolve()

    if not wiki_root.exists():
        print(f"Wiki directory not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    if args.backend is not None:
        backend = args.backend
    else:
        backend = cfg.get("search_backend", "fts5")

    extra_roots = resolve_extra_intake_roots(knowledge_root, cfg)

    cache_dir.mkdir(parents=True, exist_ok=True)

    if backend == "vector":
        try:
            count = build_vector_index(
                wiki_root,
                cache_dir,
                extra_roots=extra_roots,
            )
        except ImportError as exc:
            print(f"Vector backend unavailable: {exc}", file=sys.stderr)
            print("Install with: pip install athenaeum[vector]", file=sys.stderr)
            return 1
        print(
            f"Vector index rebuilt: {count} pages "
            f"(wiki + {len(extra_roots)} extra root(s))"
        )
        return 0

    if backend == "fts5":
        count = build_fts5_index(
            wiki_root,
            cache_dir,
            extra_roots=extra_roots,
        )
        print(
            f"FTS5 index rebuilt: {count} pages "
            f"(wiki + {len(extra_roots)} extra root(s))"
        )
        return 0

    print(f"Unknown search backend: {backend}", file=sys.stderr)
    return 1


def _cmd_recall(args: argparse.Namespace) -> int:
    """Shell-accessible recall — prints one tab-separated hit per line.

    Output format per line: ``<score>\\t<filename>\\t<preview>``, where
    ``<preview>`` is the first 80 chars of the wiki page body (post
    frontmatter), newlines collapsed to spaces. Used by validation
    harnesses and operator debugging scripts that can't rely on an MCP
    session. Reads ``search_backend`` + extra intake roots from
    ``athenaeum.yaml`` the same way ``serve`` and ``rebuild-index`` do,
    so results match what the MCP ``recall`` tool would return.
    """
    from athenaeum.config import load_config, resolve_extra_intake_roots
    from athenaeum.models import parse_frontmatter
    from athenaeum.search import get_backend

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"

    if not wiki_root.exists():
        print(f"Wiki directory not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    backend_name = args.backend or cfg.get("search_backend", "fts5")
    cache_dir = (args.cache_dir or Path("~/.cache/athenaeum")).expanduser().resolve()
    extra_roots = resolve_extra_intake_roots(knowledge_root, cfg)

    try:
        backend = get_backend(backend_name)
    except KeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        hits = backend.query(
            args.query,
            cache_dir,
            n=args.top_k,
            wiki_root=wiki_root,
        )
    except NotImplementedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    from athenaeum.mcp_server import _resolve_hit_path

    for filename, _name, score in hits:
        page_path, _display = _resolve_hit_path(filename, wiki_root, extra_roots)
        preview = ""
        if page_path is not None and page_path.is_file():
            try:
                text = page_path.read_text(encoding="utf-8")
                _fm, body = parse_frontmatter(text)
                preview = " ".join(body.split())[:80]
            except (OSError, UnicodeDecodeError):
                pass
        print(f"{score:.2f}\t{filename}\t{preview}")

    return 0


def _cmd_people(args: argparse.Namespace) -> int:
    """List type:person wikis filtered by frontmatter — frontmatter-only, no LLM.

    Filters AND together. Companies match current_company OR
    linkedin_company_at_connect (case-insensitive substring). Tags must
    match exactly. Tier is shorthand for ``tag tier:<value>``. Default
    sort is by ``warm_score`` desc; ``--top-touch N`` switches to a
    recent-touch composite score and returns the top N.
    """
    import re

    from athenaeum.models import parse_frontmatter

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    needle_companies = [c.lower() for c in args.company if c]
    required_tags = list(args.tag)
    if args.tier:
        required_tags.append(f"tier:{args.tier}")

    title_regexes = [
        re.compile(p, re.IGNORECASE) for p in (args.title_regex or []) if p
    ]
    company_regexes = [
        re.compile(p, re.IGNORECASE) for p in (args.company_regex or []) if p
    ]

    rows: list[dict] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        if not meta or meta.get("type") != "person":
            continue

        tags_raw = meta.get("tags") or []
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        if required_tags and not all(t in tags for t in required_tags):
            continue

        company_fields = [
            str(meta.get("current_company") or ""),
            str(meta.get("linkedin_company_at_connect") or ""),
        ]
        if needle_companies:
            blob = " ".join(company_fields).lower()
            if not all(needle in blob for needle in needle_companies):
                continue
        if company_regexes:
            company_blob = " ".join(company_fields)
            if not all(rx.search(company_blob) for rx in company_regexes):
                continue

        title_fields = [
            str(meta.get("current_title") or ""),
            str(meta.get("linkedin_position_at_connect") or ""),
        ]
        if title_regexes:
            title_blob = " ".join(title_fields)
            if not all(rx.search(title_blob) for rx in title_regexes):
                continue

        try:
            warm_score = float(meta.get("warm_score") or 0)
        except (TypeError, ValueError):
            warm_score = 0.0
        try:
            meeting_count = int(meta.get("meeting_count_24mo") or 0)
        except (TypeError, ValueError):
            meeting_count = 0
        try:
            sent_count = int(meta.get("sent_count_24mo") or 0)
        except (TypeError, ValueError):
            sent_count = 0

        title = (
            meta.get("current_title") or meta.get("linkedin_position_at_connect") or ""
        )
        company = (
            meta.get("current_company") or meta.get("linkedin_company_at_connect") or ""
        )
        rows.append(
            {
                "name": str(meta.get("name") or ""),
                "current_title": str(title),
                "current_company": str(company),
                "warm_score": warm_score,
                "meeting_count_24mo": meeting_count,
                "sent_count_24mo": sent_count,
                "touch_score": meeting_count * 3 + sent_count,
                "last_touch": str(meta.get("last_touch") or ""),
                "uid": str(meta.get("uid") or ""),
                "path": path.name,
            }
        )

    if args.top_touch:
        rows.sort(key=lambda r: -r["touch_score"])
        rows = rows[: args.top_touch]
    else:
        rows.sort(key=lambda r: -r["warm_score"])
        if args.limit > 0:
            rows = rows[: args.limit]

    if args.format == "tsv":
        for r in rows:
            print(
                "\t".join(
                    str(r[k])
                    for k in (
                        "name",
                        "current_title",
                        "current_company",
                        "warm_score",
                        "meeting_count_24mo",
                        "sent_count_24mo",
                        "last_touch",
                        "uid",
                        "path",
                    )
                )
            )
        return 0

    if not rows:
        print("(no matches)")
        return 0

    name_w = max(len(r["name"]) for r in rows)
    title_w = max(len(r["current_title"][:40]) for r in rows) or 1
    company_w = max(len(r["current_company"][:30]) for r in rows) or 1
    print(
        f"{'name':{name_w}}  {'title':{title_w}}  "
        f"{'company':{company_w}}  score   touch  last_touch"
    )
    for r in rows:
        print(
            f"{r['name']:{name_w}}  "
            f"{r['current_title'][:40]:{title_w}}  "
            f"{r['current_company'][:30]:{company_w}}  "
            f"{r['warm_score']:>6.1f}  "
            f"{r['touch_score']:>5}  "
            f"{r['last_touch']}"
        )
    print(f"\n{len(rows)} match(es)")
    return 0


def _cmd_stopwords(_args: argparse.Namespace) -> int:
    """Print the canonical stopword list, one word per line, sorted."""
    from athenaeum.search import STOPWORDS

    for word in STOPWORDS:
        print(word)
    return 0


def _cmd_query_topics(args: argparse.Namespace) -> int:
    """Print extracted topics, one per line. Empty output = fall back."""
    from athenaeum.query_topics import extract_topics

    for topic in extract_topics(args.prompt, timeout=args.timeout):
        print(topic)
    return 0


def _cmd_test_mcp(args: argparse.Namespace) -> int:
    """Smoke-test the MCP remember/recall round-trip without a live session.

    MCP tools are only callable from within a running Claude Code session
    (the tool list is established at session start). This command exercises
    the underlying functions directly against a synthetic knowledge dir so
    users can verify their athenaeum install works before relying on it.

    Steps:
      1. remember_write  — appends a test observation to raw/
      2. recall_search   — keyword search against a seeded wiki page
      3. create_server   — verifies FastMCP is importable and the server
                           factory returns a configured instance
    """
    import shutil
    import tempfile

    from athenaeum.mcp_server import recall_search, remember_write

    tmp_root = Path(tempfile.mkdtemp(prefix="athenaeum-test-mcp-"))
    raw_root = tmp_root / "raw"
    wiki_root = tmp_root / "wiki"
    raw_root.mkdir()
    wiki_root.mkdir()

    (wiki_root / "test-page.md").write_text(
        "---\n"
        "name: Athenaeum Test Page\n"
        "tags: [smoke-test]\n"
        "description: Seeded page used by `athenaeum test-mcp` to exercise recall.\n"
        "---\n\n"
        "This page contains the keyword ATHENAEUMSMOKETEST for recall verification.\n"
    )

    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    def _record(name: str, ok: bool, detail: str = "") -> None:
        if ok:
            passed.append(name)
            print(f"  PASS  {name}")
        else:
            failed.append((name, detail))
            print(f"  FAIL  {name}: {detail}", file=sys.stderr)

    print(f"Testing athenaeum MCP setup (temp dir: {tmp_root})")

    try:
        result = remember_write(
            raw_root,
            "Smoke test observation from `athenaeum test-mcp`.",
            source="test-mcp",
        )
        written = list((raw_root / "test-mcp").glob("*.md"))
        ok = result.startswith("Saved to ") and len(written) == 1
        _record("remember_write", ok, f"unexpected result: {result!r}")

        result = recall_search(wiki_root, "ATHENAEUMSMOKETEST", top_k=3)
        ok = "Athenaeum Test Page" in result
        _record("recall_search (keyword)", ok, f"no match in: {result[:200]!r}")

        try:
            from athenaeum.mcp_server import create_server

            server = create_server(raw_root=raw_root, wiki_root=wiki_root)
            ok = server is not None and hasattr(server, "run")
            _record("create_server (FastMCP)", ok, "factory returned unusable object")
        except ImportError as exc:
            _record(
                "create_server (FastMCP)",
                False,
                f"FastMCP not installed: {exc}. Install with: pip install athenaeum[mcp]",
            )
    finally:
        if args.keep:
            print(f"\nTemp dir preserved at: {tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


def _cmd_repair(args: argparse.Namespace) -> int:
    """Run frontmatter repair pass(es).

    Exit codes:
        0 — clean run (zero changes needed, OR ``--apply`` succeeded
            with no errors).
        1 — errors encountered (read/write/parse failures).
        2 — dry-run found fixes (CI gate signal).
    """
    from athenaeum.repair import RepairReport, repair_tag_indent, repair_value_quoting

    wiki_root = (args.wiki_root or Path("~/knowledge/wiki")).expanduser().resolve()
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    RepairFn = Callable[[Path, bool], RepairReport]
    passes: list[tuple[str, RepairFn]]
    if args.all:
        passes = [
            ("tag-indent", repair_tag_indent),
            ("value-quoting", repair_value_quoting),
        ]
    elif args.tag_indent:
        passes = [("tag-indent", repair_tag_indent)]
    else:  # args.value_quoting (mutex group guarantees one of the three)
        passes = [("value-quoting", repair_value_quoting)]

    total_changed = 0
    total_errors = 0
    mode = "APPLY" if args.apply else "DRY RUN"

    for name, func in passes:
        report: RepairReport = func(wiki_root, apply=args.apply)
        total_changed += report.files_changed
        total_errors += len(report.errors)
        print(f"=== repair {name} ({mode}) ===")
        print(f"  files_scanned: {report.files_scanned}")
        print(f"  files_changed: {report.files_changed}")
        print(f"  errors:        {len(report.errors)}")
        if report.changes and not args.apply:
            for path, summary in report.changes[:20]:
                print(f"    {path.name}: {summary}")
            if len(report.changes) > 20:
                print(f"    ... and {len(report.changes) - 20} more")
        for path, err in report.errors[:20]:
            print(f"  ERR {path.name}: {err}", file=sys.stderr)

    if total_errors > 0:
        return 1
    if not args.apply and total_changed > 0:
        return 2
    return 0


def _get_version() -> str:
    from athenaeum import __version__

    return __version__


if __name__ == "__main__":
    sys.exit(main())
