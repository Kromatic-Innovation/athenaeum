# SPDX-License-Identifier: Apache-2.0
"""``athenaeum codex-context`` — CLI wiring for the Codex Tier-2 adapter
(issue athenaeum#1364). Thin argparse only; the logic lives in
:mod:`athenaeum.codex_adapter`.

Layering: L5 (presentation), same tier as every other ``_cmd_*.py`` module.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from athenaeum.config import resolve_cache_dir


def _positive(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return n


def cmd_codex_context(args: argparse.Namespace) -> int:
    from athenaeum.codex_adapter import refresh

    return refresh(
        args.seed,
        cache_dir=resolve_cache_dir(Path(args.cache_dir) if args.cache_dir else None),
        output=Path(args.output) if args.output else None,
        n=args.n,
        backend=args.backend,
        interval_seconds=args.interval_seconds,
        max_age_seconds=args.max_age_seconds,
    )


def add_codex_context_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "codex-context",
        help="Write a periodic Athenaeum knowledge snapshot for OpenAI Codex "
        "(Tier-2 static context file, issue athenaeum#1364)",
    )
    parser.add_argument(
        "--seed", required=True, help="Static topic string the snapshot is built for"
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Snapshot path, or '-' for stdout (default: ~/.codex/athenaeum-context.md)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=_positive,
        default=900,
        help="Declared refresh cadence; must match the scheduler (default: 900)",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=_positive,
        default=None,
        help="Seconds until the snapshot is stale (default: twice the interval)",
    )
    parser.add_argument("--n", type=_positive, default=None, help="Max candidates (default: 7)")
    parser.add_argument(
        "--backend",
        choices=("fts5", "vector"),
        default="fts5",
        help="Search backend (default: fts5)",
    )
    parser.add_argument("--cache-dir", default=None, help="Athenaeum cache directory")
    parser.set_defaults(func=cmd_codex_context)
