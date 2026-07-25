# SPDX-License-Identifier: Apache-2.0
"""``athenaeum storage`` — storage-surface operator commands (issue #479).

A thin CLI dispatcher over :mod:`athenaeum.storage_migrate` (which holds the
pure transform logic), mirroring :mod:`athenaeum._cmd_authority`'s shape: the
top-level ``storage`` parser owns a ``storage_target`` sub-command, and each
mode is a small function that resolves inputs, calls a library transform, and
prints/writes. No business logic lives here.

Currently the only sub-command is ``migrate-pii`` — move a live entity page's
archival contact data (emails/phones) to the #427 excluded surface, dry-run by
default (``--apply`` writes).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import load_config
from athenaeum.pii import is_pii_class_excluded
from athenaeum.storage_migrate import plan_pii_migration


def _resolve_knowledge_root(args: argparse.Namespace) -> Path:
    return (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()


def add_storage_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``storage`` and its sub-commands (issue #479)."""
    s_parser = subparsers.add_parser(
        "storage",
        help="Storage-surface operator tasks (migrate a page's PII off-corpus).",
    )
    s_sub = s_parser.add_subparsers(dest="storage_target")

    migrate_p = s_sub.add_parser(
        "migrate-pii",
        help=(
            "Move a live entity page's archival contact data (emails/phones) "
            "to the #427 excluded surface, leaving durable identifiers only."
        ),
    )
    migrate_p.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge root (default: ~/knowledge).",
    )
    migrate_p.add_argument(
        "--page",
        type=Path,
        required=True,
        help="Path to the live entity wiki page to migrate.",
    )
    migrate_p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write the changes (rewrite the origin page + create the excluded "
            "contact record). Without this flag the command is a dry-run that "
            "prints what would change and writes nothing."
        ),
    )


def cmd_storage(args: argparse.Namespace) -> int:
    sub = getattr(args, "storage_target", None)
    if sub == "migrate-pii":
        return _cmd_storage_migrate_pii(args)
    print("usage: athenaeum storage {migrate-pii} [...]", file=sys.stderr)
    return 2


def _cmd_storage_migrate_pii(args: argparse.Namespace) -> int:
    knowledge_root = _resolve_knowledge_root(args)
    page_path: Path = args.page
    if not page_path.is_file():
        print(f"error: page not found: {page_path}", file=sys.stderr)
        return 1

    config = load_config(knowledge_root)

    # Safety gate: the excluded surface is only actually off-corpus when the
    # operator has mapped the ``pii`` class to an excluded-policy adapter.
    # Absent that mapping, ``surface_root_for_class`` falls back to the default
    # wiki surface (pii.py's documented no-op-convenience behavior) — writing
    # the contact record THERE would leak the PII straight back into the corpus.
    # Refuse to --apply rather than silently leak; the dry-run still previews.
    if not is_pii_class_excluded(config):
        msg = (
            "error: the 'pii' entity class is not mapped to an excluded surface "
            "(storage.mapping.pii). Writing there would keep the contact data in "
            "the corpus. Configure e.g.\n\n"
            "  storage:\n"
            "    mapping:\n"
            "      pii: excluded\n\n"
            "in athenaeum.yaml before migrating."
        )
        if args.apply:
            print(msg, file=sys.stderr)
            return 1
        print(
            "[DRY RUN] WARNING: 'pii' is not mapped to an excluded surface; "
            "--apply would be refused until you configure storage.mapping.pii.",
            file=sys.stderr,
        )

    plan = plan_pii_migration(page_path, config, knowledge_root)

    if not plan.changed:
        print(f"no archival contact data (emails/phones) found in {page_path}; nothing to migrate.")
        return 0

    summary = (
        f"emails={plan.emails or '[]'} phones={plan.phones or '[]'}\n"
        f"  origin page (rewritten, durable identifiers only): {plan.page_path}\n"
        f"  excluded contact record (new):                     {plan.excluded_page_path}"
    )

    if not args.apply:
        print(f"[DRY RUN] would migrate PII off {page_path}:", file=sys.stderr)
        print(summary)
        print("\n--- rewritten origin page ---")
        sys.stdout.write(plan.rewritten_page_text or "")
        print("\n--- new excluded contact record ---")
        sys.stdout.write(plan.excluded_page_text or "")
        return 0

    plan.excluded_page_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(plan.excluded_page_path, plan.excluded_page_text or "")
    atomic_write_text(plan.page_path, plan.rewritten_page_text or "")
    print(f"migrated PII off {page_path}\n{summary}")
    return 0
