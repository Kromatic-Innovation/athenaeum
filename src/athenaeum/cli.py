"""Athenaeum CLI entry point."""

import argparse
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athenaeum",
        description="Knowledge management pipeline — append-only intake, tiered compilation",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_get_version()}")
    subparsers = parser.add_subparsers(dest="command")

    # init command
    init_parser = subparsers.add_parser("init", help="Initialize a new knowledge directory")
    init_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Target directory (default: ~/knowledge)",
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
        "--raw-root", type=Path, default=None,
        help="Raw intake directory (default: ~/knowledge/raw)",
    )
    run_parser.add_argument(
        "--wiki-root", type=Path, default=None,
        help="Wiki output directory (default: ~/knowledge/wiki)",
    )
    run_parser.add_argument(
        "--knowledge-root", type=Path, default=None,
        help="Knowledge git repo root (default: ~/knowledge)",
    )
    run_parser.add_argument(
        "--dry-run", action="store_true",
        help="Run pipeline without writing files or committing",
    )
    run_parser.add_argument(
        "--max-files", type=int, default=50,
        help="Stop after processing this many raw files (default: 50)",
    )
    run_parser.add_argument(
        "--max-api-calls", type=int, default=200,
        help="Maximum estimated API calls per run (default: 200)",
    )
    run_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    # test-mcp command — smoke-test the MCP memory setup without a session
    test_mcp_parser = subparsers.add_parser(
        "test-mcp",
        help="Smoke-test MCP remember/recall against a synthetic knowledge dir",
    )
    test_mcp_parser.add_argument(
        "--keep", action="store_true",
        help="Don't delete the temp knowledge dir on exit (for debugging)",
    )

    # rebuild-index command — rebuild the search index out-of-band
    rebuild_parser = subparsers.add_parser(
        "rebuild-index",
        help="Rebuild the search index (FTS5 or vector, per config)",
    )
    rebuild_parser.add_argument(
        "--path", type=Path, default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    rebuild_parser.add_argument(
        "--cache-dir", type=Path, default=None,
        help="Cache directory (default: ~/.cache/athenaeum)",
    )
    rebuild_parser.add_argument(
        "--backend", choices=["fts5", "vector"], default=None,
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

    if args.command == "rebuild-index":
        return _cmd_rebuild_index(args)

    if args.command == "test-mcp":
        return _cmd_test_mcp(args)

    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    from athenaeum.init import init_knowledge_dir

    target = init_knowledge_dir(args.path)
    print(f"Initialized knowledge directory at {target}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from athenaeum.status import format_status, status

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}")
        return 1
    info = status(target)
    print(format_status(info))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from athenaeum.config import load_config
    from athenaeum.mcp_server import create_server

    target = args.path.expanduser().resolve()
    raw_root = target / "raw"
    wiki_root = target / "wiki"

    if not target.exists():
        print(f"Knowledge directory not found: {target}")
        print("Run 'athenaeum init --path {args.path}' first, then retry.")
        return 1

    cfg = load_config(target)
    backend = cfg.get("search_backend", "keyword")
    cache_dir = Path("~/.cache/athenaeum").expanduser()

    server = create_server(
        raw_root=raw_root,
        wiki_root=wiki_root,
        search_backend=backend,
        cache_dir=cache_dir,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


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
    )


def _cmd_rebuild_index(args: argparse.Namespace) -> int:
    from athenaeum.config import load_config
    from athenaeum.search import build_fts5_index, build_vector_index

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    cache_dir = (args.cache_dir or Path("~/.cache/athenaeum")).expanduser().resolve()

    if not wiki_root.exists():
        print(f"Wiki directory not found: {wiki_root}", file=sys.stderr)
        return 1

    if args.backend is not None:
        backend = args.backend
    else:
        cfg = load_config(knowledge_root)
        backend = cfg.get("search_backend", "fts5")

    cache_dir.mkdir(parents=True, exist_ok=True)

    if backend == "vector":
        try:
            count = build_vector_index(wiki_root, cache_dir)
        except ImportError as exc:
            print(f"Vector backend unavailable: {exc}", file=sys.stderr)
            print("Install with: pip install athenaeum[vector]", file=sys.stderr)
            return 1
        print(f"Vector index rebuilt: {count} wiki pages")
        return 0

    if backend == "fts5":
        count = build_fts5_index(wiki_root, cache_dir)
        print(f"FTS5 index rebuilt: {count} wiki pages")
        return 0

    print(f"Unknown search backend: {backend}", file=sys.stderr)
    return 1


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
                "create_server (FastMCP)", False,
                f"FastMCP not installed: {exc}. Install with: pip install athenaeum[mcp]",
            )
    finally:
        if args.keep:
            print(f"\nTemp dir preserved at: {tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


def _get_version() -> str:
    from athenaeum import __version__

    return __version__


if __name__ == "__main__":
    sys.exit(main())
