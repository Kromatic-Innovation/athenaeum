# SPDX-License-Identifier: Apache-2.0
"""``athenaeum owner set`` -- record the owner (issue athenaeum#2652).

Presentation for :mod:`athenaeum.owner_page`. Non-interactive and idempotent:
creates the owner's person page and writes ``owner.uid`` plus aliases into
``athenaeum.yaml``. ``athenaeum init --owner-name`` calls the same
:func:`apply_owner_set` helper, so the two entry points cannot drift.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args, _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def apply_owner_set(knowledge_root: Path, name: str, aliases: list[str] | None) -> int:
    """Run :func:`athenaeum.owner_page.set_owner` and print the outcome."""
    from athenaeum.owner_page import set_owner

    try:
        result = set_owner(knowledge_root, name, aliases)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not result.changed:
        print(f"Owner already set (uid {result.uid}); nothing changed.")
        return 0
    if result.page_created:
        print(f"Created owner page {result.page_path} (access: personal)")
    for alias in result.page_aliases_added:
        print(f"Added alias to owner page: {alias}")
    if result.config_changed:
        print(f"Recorded owner.uid and aliases in {knowledge_root / 'athenaeum.yaml'}")
    return 0


def add_owner_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum owner`` and its ``set`` mode."""
    parser = subparsers.add_parser(
        "owner",
        help="Manage the knowledge-base owner identity (issue athenaeum#2652). "
        "'set' creates the owner's person page and records owner.uid.",
    )
    parser.set_defaults(func=cmd_owner)
    sub = parser.add_subparsers(dest="owner_target")
    set_p = sub.add_parser(
        "set",
        help="Create the owner's person page and record owner.uid and aliases "
        "in athenaeum.yaml. Idempotent; never asks for or writes an email or phone.",
    )
    set_p.add_argument("--name", required=True, help="The owner's full name, e.g. 'Ada Example'.")
    set_p.add_argument(
        "--alias",
        action="append",
        default=[],
        help="Additional alias for name matching (repeatable). Use full names; "
        "a single-word alias is ignored by the name matcher.",
    )
    set_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(set_p)
    set_p.set_defaults(func=cmd_owner)


def cmd_owner(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum owner set``."""
    if getattr(args, "owner_target", None) != "set":
        print("usage: athenaeum owner set --name NAME [--alias ALIAS ...]", file=sys.stderr)
        return 2
    knowledge_root = _resolve_knowledge_root(args)
    if not (knowledge_root / "wiki").is_dir():
        print(
            f"error: no wiki directory at {knowledge_root / 'wiki'}; "
            f"run 'athenaeum init --path {knowledge_root}' first.",
            file=sys.stderr,
        )
        return 1
    lock: "RunLock | int" = _acquire_or_exit(knowledge_root, args)
    if isinstance(lock, int):
        return lock
    try:
        return apply_owner_set(knowledge_root, args.name, args.alias)
    finally:
        lock.release()
