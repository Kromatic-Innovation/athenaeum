# SPDX-License-Identifier: Apache-2.0
"""``athenaeum drops {list,restore}`` -- operator surface over the drop
ledger (issue athenaeum#2609).

``list`` is read-only: it prints every recorded drop (newest first), each
naming the raw ref, the stage that dropped it (``classify`` / ``never-ingest``
/ ``other`` — see :mod:`athenaeum.drops`'s module docstring for what each
means), the reason text when the model/refusal gave one, and the capped,
PII-masked content excerpt. ``restore <ref>`` re-queues that ref's recorded
content as fresh raw intake (:func:`athenaeum.drops.restore_drop`) so a
wrong drop can be undone — it takes the run lock (it writes under
``raw/``), mirroring every other mutating ``_cmd_*`` subcommand's
``_acquire_or_exit`` discipline.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import
cost down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args, _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def add_drops_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum drops`` and its ``list``/``restore`` modes."""
    parser = subparsers.add_parser(
        "drops",
        help="Inspect and recover from the drop ledger (issue athenaeum#2609) -- "
        "raw files the last run consumed without creating or updating anything. "
        "'list' shows recent drops; 'restore <ref>' re-queues one as raw intake.",
    )
    parser.set_defaults(func=cmd_drops)
    sub = parser.add_subparsers(dest="drops_target")

    list_p = sub.add_parser("list", help="List recorded drops, newest first.")
    list_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    list_p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the drop ledger (default: ~/.cache/athenaeum).",
    )
    list_p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Show at most N drops (default: 20; 0 = unlimited).",
    )
    list_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )

    restore_p = sub.add_parser(
        "restore",
        help="Re-queue a dropped raw file's recorded content as fresh raw intake.",
    )
    restore_p.add_argument("ref", help="The dropped raw file's ref (source/filename).")
    restore_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    restore_p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the drop ledger (default: ~/.cache/athenaeum).",
    )
    _add_lock_args(restore_p)


def cmd_drops(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum drops``. No mode prints help and returns 0."""
    target = getattr(args, "drops_target", None)
    if target == "list":
        return _cmd_drops_list(args)
    if target == "restore":
        return _cmd_drops_restore(args)
    print(
        "Specify a mode: 'athenaeum drops list' or 'athenaeum drops restore <ref>'.",
        file=sys.stderr,
    )
    return 0


def _cmd_drops_list(args: argparse.Namespace) -> int:
    """``athenaeum drops list``. Exit codes: 0 always (read-only; an empty
    ledger is not an error)."""
    from athenaeum.config import resolve_cache_dir
    from athenaeum.drops import iter_drops

    cache_dir = resolve_cache_dir(getattr(args, "cache_dir", None))
    records = list(reversed(iter_drops(cache_dir)))  # newest first
    limit = getattr(args, "limit", 20)
    if limit:
        records = records[:limit]

    if getattr(args, "json", False):
        print(json.dumps([r.to_dict() for r in records], indent=2, sort_keys=True))
        return 0

    if not records:
        print("No drops recorded.")
        return 0
    for rec in records:
        reason = f" reason={rec.reason!r}" if rec.reason else ""
        print(f"{rec.ts}  {rec.raw_ref}  stage={rec.stage}{reason}  run={rec.run_id}")
        print(f"    {rec.excerpt[:120]!r}")
    print(f"\n{len(records)} drop(s) shown. Restore one with: athenaeum drops restore <ref>")
    return 0


def _cmd_drops_restore(args: argparse.Namespace) -> int:
    """``athenaeum drops restore <ref>``.

    Exit codes: 0 restored; 1 the ref was never dropped (nothing to
    restore); 75 (``EXIT_LOCK_HELD``) the run lock is held — named in
    ``docs/reference/exit-codes.md`` (issue athenaeum#2609).
    """
    from athenaeum.config import load_config, resolve_cache_dir
    from athenaeum.drops import restore_drop

    knowledge_root = _resolve_knowledge_root(args)
    raw_root = knowledge_root / "raw"
    cache_dir = resolve_cache_dir(getattr(args, "cache_dir", None))

    lock: "RunLock | int" = _acquire_or_exit(
        knowledge_root, args, load_config(knowledge_root)
    )
    if isinstance(lock, int):
        return lock
    try:
        dest = restore_drop(cache_dir, raw_root, args.ref)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()

    print(f"Restored {args.ref!r} -> {dest}")
    print("It will be reprocessed on the next 'athenaeum run'.")
    return 0
