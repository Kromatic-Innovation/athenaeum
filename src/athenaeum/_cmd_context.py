# SPDX-License-Identifier: Apache-2.0
"""``athenaeum context`` — CLI wiring for the sidecar core (issue athenaeum#1358).

Thin argv/stdin parsing plus one call into
:func:`athenaeum.context.build_context_for_turn` (session-dedup + the core
build + push-telemetry, issue athenaeum#1621) and a JSON print of the
result. All the logic lives in :mod:`athenaeum.context` per that module's
own import-weight contract; this file exists only because ``athenaeum
context`` needs *a* subcommand registration, and ``cli.py``'s factoring
rule ("every subcommand lives in its own ``_cmd_<name>.py``") means that
registration cannot live inline in ``athenaeum.context`` itself.

The Claude Code adapter (:mod:`athenaeum.claude_code_adapter`, issue
athenaeum#1621) calls the SAME :func:`~athenaeum.context.build_context_for_turn`
this module calls, rather than going through this CLI wrapper — see that
module's own docstring for why, and see
:func:`athenaeum.context.build_context_for_turn`'s docstring for why the
sequence is shared rather than reimplemented here.

**This subcommand is not the fast path.** Invoking it via the installed
``athenaeum`` console script pays ``athenaeum.cli.build_parser()``'s cost of
importing every ``_cmd_*`` module up front (see that module's docstring) —
unrelated to, and unfixed by, athenaeum#1358/#1360. An adapter that wants the
FTS5-path wall-clock budget imports :mod:`athenaeum.context` directly
(``import athenaeum.context`` / ``from athenaeum.context import
build_context``), NOT through this CLI wrapper's ``build_parser()`` cost.

Loading ``context.py`` by file path
(``importlib.util.spec_from_file_location``) buys nothing over an ordinary
import and should not be used here — this is measured, not assumed:
``docs/measurements/retrieval-entry-point-measurements.md`` (athenaeum#1357's spike,
"Finding 2") found the pre-convergence shell hook's identical bypass of
``search.py`` this way does not skip the package root either, because the
loaded module's own ``from athenaeum.X import ...`` statements still import
``athenaeum/__init__.py`` first — loading the file under a different module
name changes nothing about what its import statements do. Since
athenaeum#1360 landed, that package-root cost is cheap enough that an
ordinary import already meets the budget (see that doc's "Re-measured after
athenaeum#1360" section) and the bypass is pointless rather than harmful.

This subcommand exists for scripting/debugging convenience and for tests
that want to exercise "the deployed adapter end to end" (issue athenaeum#1362)
via a real subprocess, not as the low-latency call site.

Layering: L5 (presentation), same tier as every other ``_cmd_*.py`` module.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_CACHE_DIR, resolve_cache_dir


def cmd_context(args: argparse.Namespace) -> int:
    from athenaeum.context import build_context_for_turn

    prompt = args.prompt
    session_id = args.session_id

    if args.stdin_json or prompt is None:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        prompt = payload.get("prompt", prompt) or ""
        session_id = payload.get("session_id", session_id) or "unknown"

    if not prompt:
        print(json.dumps({"error": "no prompt supplied"}), file=sys.stderr)
        return 1

    session_id = session_id or "unknown"
    cache_dir = resolve_cache_dir(Path(args.cache_dir) if args.cache_dir else None)

    envelope = build_context_for_turn(
        prompt,
        session_id,
        cache_dir=cache_dir,
        n=args.n,
        budget=args.budget,
        search_backend=args.backend,
        use_llm=not args.no_llm,
        llm_timeout=args.llm_timeout,
    )

    print(json.dumps(envelope))
    return 0


def add_context_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "context",
        help="Build one sidecar context envelope (ranked candidates + rendered "
        "text) for a prompt — the agent-neutral core, issue athenaeum#1358",
    )
    parser.add_argument(
        "prompt", nargs="?", default=None, help="The prompt text (omit to read JSON from stdin)"
    )
    parser.add_argument(
        "--session-id", default=None, help="Session id, for dedup bookkeeping by the caller"
    )
    parser.add_argument(
        "--stdin-json",
        action="store_true",
        help='Read {"prompt": ..., "session_id": ...} from stdin (hook-input shape)',
    )
    parser.add_argument("--n", type=int, default=3, help="Max candidates (default: 3)")
    parser.add_argument(
        "--budget", type=int, default=None, help="Push token budget (default: resolved/1200)"
    )
    parser.add_argument(
        "--backend",
        choices=("fts5", "vector"),
        default="fts5",
        help="Search backend (default: fts5)",
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="Skip LLM term extraction, use the regex fallback"
    )
    parser.add_argument(
        "--llm-timeout", type=float, default=3.0, help="LLM extraction timeout in seconds"
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help=f"Cache dir holding wiki-index.db (default: {DEFAULT_CACHE_DIR})",
    )
    parser.set_defaults(func=cmd_context)
