# SPDX-License-Identifier: Apache-2.0
"""``athenaeum doctor`` — read-only install health check (issue athenaeum#2298).

Thin CLI dispatcher over :mod:`athenaeum.doctor`, mirroring
``athenaeum contact-surface-lint``'s shape (no detection logic of its own —
see ``_cmd_contact_surface_lint.py``'s module docstring for the same
"factoring rule" note). Every check — package version, interpreter version,
MCP registration, hooks, LLM key mode, search index, last compile time,
schedule state (issue athenaeum#2304) —
lives in :mod:`athenaeum.doctor`; this module only builds the argparse
surface, calls :func:`athenaeum.doctor.run_checks`, prints the report, and
returns the exit code.

**Deliberately does NOT take the run lock (no ``_acquire_or_exit`` call).**
This command makes no writes at all (issue athenaeum#2298 AC5) — not even
the append-only writes ``contact-surface-lint``'s ``--attribution`` mode can
make — so it never contends with a compile run.

Exit codes (issue athenaeum#2298 AC2):

    0 -- every check is "ok" or "skip" (the package-version check offline).
    1 -- at least one check is "fail".

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.

Layering: L5.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def add_doctor_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``doctor``."""
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Read-only health check of an athenaeum install — package "
        "version, interpreter, MCP registration, hooks, LLM key mode, "
        "search index, last compile time. Exits non-zero on any failed "
        "check.",
    )
    doctor_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    doctor_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory (default: ATHENAEUM_CACHE_DIR env, else "
        "~/.cache/athenaeum)",
    )
    doctor_parser.add_argument(
        "--claude-config-dir",
        type=Path,
        default=None,
        help="Claude Code config directory holding settings.json/"
        ".claude.json (default: CLAUDE_CONFIG_DIR env, else ~/.claude)",
    )
    doctor_parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Home directory to check the `athenaeum schedule` state under "
        "(issue athenaeum#2304; default: the real home directory).",
    )
    doctor_parser.set_defaults(func=cmd_doctor)


def cmd_doctor(args: argparse.Namespace) -> int:
    from athenaeum import doctor

    knowledge_root = args.path.expanduser().resolve()
    results = doctor.run_checks(
        knowledge_root,
        cache_dir=args.cache_dir,
        claude_config_dir=args.claude_config_dir,
        home=args.home,
    )
    print(doctor.format_report(results))
    return doctor.exit_code(results)
