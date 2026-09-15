# SPDX-License-Identifier: Apache-2.0
"""``athenaeum schema migrate`` — issue athenaeum#1628 Plan item 4.

Presentation for :mod:`athenaeum.schema_migrate`. Dry-run by default and
``--apply`` to write, matching ``memory-class backfill`` / ``subject
backfill`` rather than inventing a third convention for a corpus-wide
sweep; the explicit ``--dry-run`` flag is accepted too so a caller can
state the safe mode rather than rely on the default.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def cmd_schema(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum schema migrate``."""
    if getattr(args, "schema_target", None) != "migrate":
        print("usage: athenaeum schema migrate [...]", file=sys.stderr)
        return 2

    from athenaeum.schema_migrate import apply_migrations, build_migrate_report

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"error: no wiki directory at {wiki_root}", file=sys.stderr)
        return 1

    should_write = bool(getattr(args, "apply", False)) and not bool(
        getattr(args, "dry_run", False)
    )

    report = build_migrate_report(wiki_root)
    changed = apply_migrations(report) if should_write else 0

    if args.json:
        payload = report.to_dict()
        payload["applied"] = should_write
        payload["files_changed"] = changed
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0

    by_reason = report.counts_by_reason()
    print(f"scanned: {report.scanned} page(s) under {wiki_root}")
    print(f"migratable: {len(report.migrations)}")
    print("skipped:")
    for reason, count in by_reason.items():
        if reason == "migrated":
            continue
        print(f"  {reason}: {count}")
    print(
        f"applied: {changed} file(s) written"
        if should_write
        else "dry run: nothing written (pass --apply to write)"
    )
    return 0


def add_schema_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum schema`` and its ``migrate`` mode."""
    parser = subparsers.add_parser(
        "schema",
        help=(
            "Page schema-version maintenance: apply pending EAGER "
            "rule-based kernel migrations corpus-wide (issue athenaeum#1628)."
        ),
    )
    parser.set_defaults(func=cmd_schema)
    sub = parser.add_subparsers(dest="schema_target")

    migrate = sub.add_parser(
        "migrate",
        help="Apply pending rule-based/eager schema migrations "
        "(athenaeum.schema_migrations.MIGRATIONS) — dry-run unless --apply. "
        "Never touches a model-derivation migration (those advance only "
        "via 'athenaeum audit') and never overwrites an already-populated "
        "field.",
    )
    migrate.set_defaults(func=cmd_schema)
    migrate.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="Write the pending migrations. Without this flag the command "
        "reports and writes nothing.",
    )
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Report without writing. This is already the default; the "
        "flag exists so a caller can state it, and it OVERRIDES --apply "
        "when both are given (safe mode wins).",
    )
    migrate.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
