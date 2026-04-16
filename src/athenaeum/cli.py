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

    # init command — placeholder, implemented in #159
    subparsers.add_parser("init", help="Initialize a new knowledge directory")

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
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "init":
        print("athenaeum init — not yet implemented")
        return 1

    if args.command == "run":
        return _cmd_run(args)

    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from athenaeum.librarian import DEFAULT_KNOWLEDGE_ROOT, DEFAULT_RAW_ROOT, DEFAULT_WIKI_ROOT, run

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    return run(
        raw_root=args.raw_root or DEFAULT_RAW_ROOT,
        wiki_root=args.wiki_root or DEFAULT_WIKI_ROOT,
        knowledge_root=args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT,
        dry_run=args.dry_run,
        max_files=args.max_files,
    )


def _get_version() -> str:
    from athenaeum import __version__

    return __version__


if __name__ == "__main__":
    sys.exit(main())
