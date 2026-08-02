# SPDX-License-Identifier: Apache-2.0
"""``athenaeum {init,status,disable,enable,spend}`` — knowledge-base lifecycle.

Five small, independent subcommands grouped here because each is a thin,
single-purpose operator command over knowledge-base or process-level state
(create the directory, report status, flip the kill switch, report spend) —
none warrants its own module, and they share no code with the compile/index
pipeline (``_cmd_index.py``) or the ``run`` pipeline (``_cmd_run.py``).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` (or a small same-domain group module like this
one) and registers via ``add_<name>_subparser`` — this is where a NEW
subcommand goes, not inline in ``cli.py``'s ``main()``. This module may
import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry points, kept lazy/local to keep top-level import
cost down.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def add_lifecycle_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Register ``init``, ``status``, ``disable``, ``enable``, ``spend``."""

    # init command
    init_parser = subparsers.add_parser(
        "init", help="Initialize a new knowledge directory"
    )
    init_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
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
        help="Overwrite existing template files at the destination; "
        "no backup is created (only applies with --with-templates).",
    )
    init_parser.set_defaults(func=cmd_init)

    # status command
    status_parser = subparsers.add_parser("status", help="Show knowledge base status")
    status_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    status_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the kill-switch state "
        "(default: ~/.cache/athenaeum). Only affects the kill-switch line.",
    )
    status_parser.set_defaults(func=cmd_status)

    # disable / enable commands — the kill switch (issue athenaeum#379)
    disable_parser = subparsers.add_parser(
        "disable",
        help="Turn athenaeum's background work off (compile, detectors, "
        "recall, notifications). Reversible with 'athenaeum enable'.",
    )
    disable_parser.add_argument(
        "--compile",
        action="store_true",
        help="Granular: stop only the expensive compile/detect pass "
        "(session-end contradiction detection); leave recall on.",
    )
    disable_parser.add_argument(
        "--reason",
        type=str,
        default=None,
        help="Optional note recorded in the state file and shown by "
        "'athenaeum status'.",
    )
    disable_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for the state file "
        "(default: ~/.cache/athenaeum).",
    )
    disable_parser.set_defaults(func=cmd_disable)

    enable_parser = subparsers.add_parser(
        "enable",
        help="Undo 'athenaeum disable' — restore all background work.",
    )
    enable_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for the state file "
        "(default: ~/.cache/athenaeum).",
    )
    enable_parser.set_defaults(func=cmd_enable)

    # spend command — report the durable LLM-spend ledger (issue athenaeum#378)
    spend_parser = subparsers.add_parser(
        "spend",
        help="Report LLM spend from the durable ledger ($ for API, tokens for "
        "subscription — never blended)",
    )
    spend_parser.add_argument(
        "--since",
        type=str,
        default="7d",
        help="Lower bound: a window (7d / 24h / 30m / 2w) or an ISO date "
        "(2026-07-01). Default: 7d.",
    )
    spend_parser.add_argument(
        "--by-model", action="store_true", help="Break down per serving model."
    )
    spend_parser.add_argument(
        "--by-provider",
        action="store_true",
        help="Break down per run type within each cost path.",
    )
    spend_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (what /good-morning consumes).",
    )
    spend_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory for config resolution (default: ~/knowledge).",
    )
    spend_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache dir holding spend.jsonl (default: ATHENAEUM_CACHE_DIR env, "
        "else ~/.cache/athenaeum).",
    )
    spend_parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Explicit ledger file path (overrides --cache-dir and config).",
    )
    spend_parser.set_defaults(func=cmd_spend)


def cmd_init(args: argparse.Namespace) -> int:
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
    elif args.templates_dest is not None:
        print(
            "warning: --templates-dest is ignored without --with-templates; "
            "no templates were copied.",
            file=sys.stderr,
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from athenaeum.killswitch import format_status_line
    from athenaeum.status import format_status, status

    # Kill-switch state (issue athenaeum#379) is process/cache state, independent of the
    # knowledge base — report it first so 'athenaeum status' always answers
    # "is it on?" even when the knowledge directory is missing.
    print(format_status_line(getattr(args, "cache_dir", None)))
    print()

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}")
        print(f"Run 'athenaeum init --path {args.path}' first, then retry.")
        return 1
    info = status(target)
    print(format_status(info))
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    """Kill switch (athenaeum#379): stop athenaeum background work, reversibly."""
    from athenaeum import killswitch

    scope = killswitch.SCOPE_COMPILE if args.compile else killswitch.SCOPE_ALL
    path = killswitch.disable(
        scope, reason=args.reason, cache_dir=getattr(args, "cache_dir", None)
    )
    if scope == killswitch.SCOPE_COMPILE:
        print(
            "athenaeum disabled (compile): the session-end compile/detect pass "
            "is off; recall stays on."
        )
    else:
        print(
            "athenaeum disabled: all background work is off — compile, "
            "contradiction detection, recall, and notifications."
        )
    print(f"State: {path}")
    print("Re-enable with: athenaeum enable")

    env_state = killswitch.current_state(getattr(args, "cache_dir", None))
    if env_state.source == "env" and env_state.scope != scope:
        print(
            f"note: {killswitch.ENV_VAR}={os.environ.get(killswitch.ENV_VAR)!r} "
            f"overrides the state file (effective scope: {env_state.scope}).",
            file=sys.stderr,
        )
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    """Kill switch (athenaeum#379): undo 'athenaeum disable'."""
    from athenaeum import killswitch

    removed = killswitch.enable(cache_dir=getattr(args, "cache_dir", None))
    if removed:
        print("athenaeum enabled: background work restored.")
    else:
        print("athenaeum was already enabled (no state file to remove).")

    env = killswitch.current_state(getattr(args, "cache_dir", None))
    if env.source == "env":
        print(
            f"warning: {killswitch.ENV_VAR}={os.environ.get(killswitch.ENV_VAR)!r} "
            f"still forces disabled (scope: {env.scope}); unset it to fully "
            "re-enable.",
            file=sys.stderr,
        )
        return 0
    return 0


def cmd_spend(args: argparse.Namespace) -> int:
    """Report LLM spend from the durable ledger (issue athenaeum#378).

    Separates real API DOLLARS from subscription TOKENS — never a blended
    figure. ``--json`` emits the machine-readable shape ``/good-morning``
    consumes; the human default renders the two paths as distinct rows.
    """
    import json

    from athenaeum import spend
    from athenaeum.config import load_config, resolve_cache_dir

    target = args.path.expanduser().resolve()
    config = load_config(target) if target.exists() else None

    if args.ledger is not None:
        ledger_path = args.ledger.expanduser().resolve()
    else:
        ledger_path = spend.resolve_ledger_path(
            config, cache_dir=resolve_cache_dir(args.cache_dir).resolve()
        )

    try:
        since_dt = spend.parse_since(args.since)
    except ValueError:
        print(
            f"Invalid --since value: {args.since!r} — use a window (7d / 24h / "
            f"30m / 2w) or an ISO date (2026-07-01).",
            file=sys.stderr,
        )
        return 2

    records = spend.read_ledger(ledger_path, since=since_dt)
    summary = spend.summarize(
        records, by_model=args.by_model, by_provider=args.by_provider
    )

    if args.json:
        payload = {
            "since": since_dt.isoformat().replace("+00:00", "Z"),
            "ledger_path": str(ledger_path),
            **summary,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            spend.format_summary(
                summary,
                since_label=args.since,
                by_model=args.by_model,
                by_provider=args.by_provider,
            )
        )
    return 0
