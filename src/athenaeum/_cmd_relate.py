# SPDX-License-Identifier: Apache-2.0
"""``athenaeum relate`` — set or remove the parent/child page relation by
hand (issue athenaeum#2535).

``--child <uid> --parent <uid>`` attaches (``via: manual``);
``--child <uid> --detach`` removes whatever parent it currently has.
Domain logic (invariants, atomic double-write, idempotent rendering):
:mod:`athenaeum.page_relations`.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.

Layering: L5 (presentation), same tier as every other ``_cmd_*.py`` module.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def add_relate_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``relate``."""
    parser = subparsers.add_parser(
        "relate",
        help="Set or remove the parent/child page relation by hand (issue "
        "athenaeum#2535). `--child <uid> --parent <uid>` attaches it "
        "(via: manual); `--child <uid> --detach` removes it. Runs under "
        "the run lock; exits 75 while another process holds it.",
    )
    parser.add_argument("--child", required=True, metavar="UID", help="The child page's uid.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--parent", metavar="UID", help="The parent page's uid to attach --child under."
    )
    group.add_argument(
        "--detach", action="store_true", help="Remove --child's current parent relation."
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_relate)


def cmd_relate(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum relate``.

    Exit codes: 0 success; 1 the relation was refused (unknown uid,
    invariant violation) or the knowledge root has no ``wiki/``; 75 the run
    lock is held.
    """
    from athenaeum.config import load_config
    from athenaeum.page_relations import RelationError, remove_relation, set_relation

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    acquired = _acquire_or_exit(knowledge_root, args, load_config(knowledge_root))
    if isinstance(acquired, int):
        return acquired
    lock = acquired
    try:
        if args.detach:
            remove_relation(wiki_root, child_uid=args.child)
            print(f"detached {args.child}")
        else:
            set_relation(wiki_root, child_uid=args.child, parent_uid=args.parent, via="manual")
            print(f"{args.child} is now a child of {args.parent} (via: manual)")
    except RelationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()
    return 0
