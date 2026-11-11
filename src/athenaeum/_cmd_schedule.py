# SPDX-License-Identifier: Apache-2.0
"""``athenaeum schedule {install,remove}`` — recurring `athenaeum run`
schedule management (issue athenaeum#2304).

Thin CLI dispatcher over :mod:`athenaeum.schedule`, mirroring
``athenaeum receipts``'s shape (multiple verbs on one subcommand, each
``set_defaults(func=...)``-bound to its own handler) — no generation,
key-resolution or loader logic of its own; see :mod:`athenaeum.schedule`'s
module docstring for the real contract.

**Deliberately does NOT take the run lock (no ``_acquire_or_exit`` call).**
Neither verb touches the knowledge root's git state — ``install``/
``remove`` only read/write the unit file(s) under ``--home`` and shell out
to the platform loader — so neither contends with a compile run, and
neither is named in ``docs/reference/exit-codes.md``'s ``EXIT_LOCK_HELD``
producer list. (The schedule's own generated unit invoking ``athenaeum
run`` IS what takes the lock, at its own scheduled tick.)

Exit codes:

    0 -- success.
    1 -- ``install`` refused (no key source, issue athenaeum#2304 AC3, or an
         unsupported platform) OR the platform loader reported a non-zero
         exit.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.

Layering: L5.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory the generated unit runs `athenaeum run` "
        "against (default: ~/knowledge)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory to read a cached ANTHROPIC_API_KEY from "
        "(default: ATHENAEUM_CACHE_DIR env, else ~/.cache/athenaeum)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Home directory the unit is written under / read from "
        "(default: the real home directory). Mainly for testing an "
        "unusual HOME layout.",
    )
    parser.add_argument(
        "--backend",
        choices=("launchd", "systemd", "cron"),
        default=None,
        help="Force a backend instead of auto-detecting from the platform "
        "(macOS is always launchd; Linux auto-detects systemd when "
        "`systemctl` is on PATH, else cron).",
    )


def add_schedule_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum schedule`` and its ``install``/``remove``
    subcommands on *subparsers*."""
    parser = subparsers.add_parser(
        "schedule",
        help="Install or remove a recurring `athenaeum run` schedule: a "
        "launchd plist on macOS, or a systemd user timer or crontab entry "
        "on Linux, with absolute paths and the key source resolved "
        "without a login shell.",
    )
    parser.set_defaults(func=_cmd_schedule_dispatch)
    sub = parser.add_subparsers(dest="schedule_target")

    install_p = sub.add_parser(
        "install",
        help="Generate the platform's unit, write it, and load it. "
        "Refuses (exit 1) if no LLM key source is available, rather than "
        "installing a schedule that would fail every night.",
    )
    _add_common(install_p)
    install_p.add_argument(
        "--hour",
        type=int,
        default=None,
        help="Hour (0-23, local time) to run at (default: 3).",
    )
    install_p.add_argument(
        "--minute",
        type=int,
        default=None,
        help="Minute (0-59) to run at (default: 0).",
    )
    install_p.set_defaults(func=_cmd_install)

    remove_p = sub.add_parser(
        "remove",
        help="Unload and delete everything `install` wrote. A no-op "
        "(exit 0) when nothing is installed.",
    )
    _add_common(remove_p)
    remove_p.set_defaults(func=_cmd_remove)


def _cmd_schedule_dispatch(args: argparse.Namespace) -> int:
    print("usage: athenaeum schedule {install,remove} [...]", file=sys.stderr)
    return 2


def _cmd_install(args: argparse.Namespace) -> int:
    from athenaeum import schedule
    from athenaeum.config import load_config

    knowledge_root = args.path.expanduser().resolve()
    config = load_config(knowledge_root)
    hour = args.hour if args.hour is not None else schedule.DEFAULT_HOUR
    minute = args.minute if args.minute is not None else schedule.DEFAULT_MINUTE

    try:
        result = schedule.install(
            knowledge_root,
            config=config,
            cache_dir=args.cache_dir,
            home=args.home,
            hour=hour,
            minute=minute,
            platform_override=args.backend,
        )
    except (schedule.NoKeySourceError, schedule.UnsupportedPlatformError) as exc:
        print(f"athenaeum schedule install: error: {exc}", file=sys.stderr)
        return 1

    where = ", ".join(str(p) for p in result.paths) or "crontab"
    print(f"athenaeum schedule install: {result.platform} schedule installed ({where})")
    if result.loader_returncode != 0:
        print(
            f"  WARN loader exited {result.loader_returncode}: {result.loader_output}",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    from athenaeum import schedule

    try:
        result = schedule.remove(
            home=args.home,
            platform_override=args.backend,
        )
    except schedule.UnsupportedPlatformError as exc:
        print(f"athenaeum schedule remove: error: {exc}", file=sys.stderr)
        return 1

    if not result.had_schedule:
        print(f"athenaeum schedule remove: no {result.platform} schedule was installed")
        return 0

    where = ", ".join(str(p) for p in result.removed_paths) or "crontab entry"
    print(f"athenaeum schedule remove: {result.platform} schedule removed ({where})")
    if result.loader_returncode != 0:
        print(
            f"  WARN loader exited {result.loader_returncode}: {result.loader_output}",
            file=sys.stderr,
        )
        return 1
    return 0


__all__ = ["add_schedule_subparser"]
