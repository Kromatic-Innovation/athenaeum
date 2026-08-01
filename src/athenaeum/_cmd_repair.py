# SPDX-License-Identifier: Apache-2.0
"""``athenaeum repair`` — YAML-frontmatter repair tools.

A single subcommand with a mutually-exclusive repair-mode group
(``--tag-indent`` / ``--value-quoting`` / ``--legacy-source-slugs`` /
``--backfill-sources`` / ``--all``), kept in its own module because it is
sizable on its own (multiple report shapes, exit-code contract shared with
``auto-memory prune``).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this
is where a NEW subcommand goes, not inline in ``cli.py``'s ``main()``. This
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import cost
down.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.repair import RepairReport
    from athenaeum.runlock import RunLock


def add_repair_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum repair`` and its flags on *subparsers*."""
    repair_parser = subparsers.add_parser(
        "repair",
        help="Repair YAML-frontmatter corruption in wiki files. "
        "Default is dry-run; pass --apply to write fixes.",
    )
    repair_mode = repair_parser.add_mutually_exclusive_group(required=True)
    repair_mode.add_argument(
        "--tag-indent",
        action="store_true",
        help="Normalize block-list indentation under top-level keys "
        "(tags:, emails:, aliases:, ...).",
    )
    repair_mode.add_argument(
        "--value-quoting",
        action="store_true",
        help="Quote unquoted YAML values that break safe_load "
        "(values starting with '-' or '[').",
    )
    repair_mode.add_argument(
        "--legacy-source-slugs",
        action="store_true",
        help="Migrate legacy bare-slug `source:` values to typed "
        "`script:<slug>` form (issue #97 / design-lock §5).",
    )
    repair_mode.add_argument(
        "--backfill-sources",
        action="store_true",
        help="Re-classify memories whose source was DEFAULTED to "
        "`claude:inferred` against their origin transcript (issue #328): "
        "user-stated / agent-observed upgrades, else confirm inferred.",
    )
    repair_mode.add_argument(
        "--all",
        action="store_true",
        help="Run all repair passes in sequence (tag-indent then value-quoting).",
    )
    repair_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write fixes. Without this flag, the command is a dry-run.",
    )
    repair_parser.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="Wiki directory (default: ~/knowledge/wiki)",
    )
    repair_parser.add_argument(
        "--knowledge-root",
        type=Path,
        default=None,
        help="Knowledge root for --backfill-sources (default: ~/knowledge); "
        "auto-memory is read from <root>/raw/auto-memory.",
    )
    repair_parser.add_argument(
        "--projects-root",
        type=Path,
        default=None,
        help="Transcript root for --backfill-sources " "(default: ~/.claude/projects).",
    )
    repair_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="--backfill-sources: cap memories acted on per run (bounded "
        "resumable batch). Idempotency makes the resume implicit.",
    )
    _add_lock_args(repair_parser)
    repair_parser.set_defaults(func=cmd_repair)


def cmd_repair(args: argparse.Namespace) -> int:
    """Run frontmatter repair pass(es).

    Exit codes:
        0 — clean run (zero changes needed, OR ``--apply`` succeeded
            with no errors).
        1 — errors encountered (read/write/parse failures).
        2 — dry-run found fixes (CI gate signal).
    """
    from athenaeum.repair import (
        RepairReport,
        migrate_legacy_source_slugs,
        repair_tag_indent,
        repair_value_quoting,
    )

    # Source-backfill (issue #328) reads the scope-indexed auto-memory tree, not
    # the wiki, so it branches BEFORE the wiki_root resolution below.
    if getattr(args, "backfill_sources", False):
        return _cmd_repair_backfill_sources(args)

    wiki_root = (args.wiki_root or Path("~/knowledge/wiki")).expanduser().resolve()
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    # Issue #309: --apply mutates wiki frontmatter and can race a concurrent
    # `run`, so it takes the run lock. A dry-run reads only — no lock.
    lock: RunLock | int | None = None
    if args.apply:
        from athenaeum.config import load_config

        lock = _acquire_or_exit(wiki_root.parent, args, load_config(wiki_root.parent))
        if isinstance(lock, int):
            return lock
    try:
        # The legacy-source-slugs pass uses a different report shape, so it
        # runs through a dedicated branch instead of the RepairReport pipeline.
        if args.legacy_source_slugs:
            return _cmd_repair_legacy_slugs(
                wiki_root, apply=args.apply, runner=migrate_legacy_source_slugs
            )

        RepairFn = Callable[[Path, bool], RepairReport]
        passes: list[tuple[str, RepairFn]]
        if args.all:
            passes = [
                ("tag-indent", repair_tag_indent),
                ("value-quoting", repair_value_quoting),
            ]
        elif args.tag_indent:
            passes = [("tag-indent", repair_tag_indent)]
        else:  # args.value_quoting (mutex group guarantees one of the four)
            passes = [("value-quoting", repair_value_quoting)]

        total_changed = 0
        total_errors = 0
        mode = "APPLY" if args.apply else "DRY RUN"

        for name, func in passes:
            report: RepairReport = func(wiki_root, apply=args.apply)
            total_changed += report.files_changed
            total_errors += len(report.errors)
            print(f"=== repair {name} ({mode}) ===")
            print(f"  files_scanned: {report.files_scanned}")
            print(f"  files_changed: {report.files_changed}")
            print(f"  errors:        {len(report.errors)}")
            if report.changes and not args.apply:
                for path, summary in report.changes[:20]:
                    print(f"    {path.name}: {summary}")
                if len(report.changes) > 20:
                    print(f"    ... and {len(report.changes) - 20} more")
            for path, err in report.errors[:20]:
                print(f"  ERR {path.name}: {err}", file=sys.stderr)

        if total_errors > 0:
            return 1
        if not args.apply and total_changed > 0:
            return 2
        return 0
    finally:
        if lock is not None and not isinstance(lock, int):
            lock.release()


def _cmd_repair_backfill_sources(args: argparse.Namespace) -> int:
    """Run the #328 source-backfill pass over the auto-memory tree.

    Exit codes:
        0 — clean run (zero upgrades, OR ``--apply`` succeeded with no errors).
        1 — errors encountered (read/parse/write/validation failures), or the
            auto-memory root was not found.
        2 — dry-run found memories that WOULD be upgraded (CI gate signal).
    """
    from athenaeum.config import load_config, resolve_owner_asserter
    from athenaeum.repair import backfill_sources

    knowledge_root = (args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    auto_memory_root = knowledge_root / "raw" / "auto-memory"
    if not auto_memory_root.is_dir():
        print(f"Auto-memory root not found: {auto_memory_root}", file=sys.stderr)
        return 1

    projects_root = (
        args.projects_root.expanduser().resolve() if args.projects_root else None
    )
    cfg = load_config(knowledge_root)
    asserter = resolve_owner_asserter(cfg)

    # --apply mutates frontmatter and can race a concurrent `run`, so it takes
    # the run lock (issue #309). A dry-run reads only — no lock.
    lock: RunLock | int | None = None
    if args.apply:
        lock = _acquire_or_exit(knowledge_root, args, cfg)
        if isinstance(lock, int):
            return lock
    try:
        report = backfill_sources(
            auto_memory_root,
            projects_root=projects_root,
            apply=args.apply,
            asserter=asserter,
            limit=args.limit,
        )
        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"=== repair backfill-sources ({mode}) ===")
        print(f"  files_scanned:      {report.files_scanned}")
        print(f"  user-stated:        {report.user_stated}")
        print(f"  agent-observed:     {report.agent_observed}")
        print(f"  confirmed-inferred: {report.confirmed_inferred}")
        print(f"  skipped:            {len(report.skips)}")
        if report.changes and not args.apply:
            for path, summary in report.changes[:20]:
                print(f"    {path.name}: {summary}")
            if len(report.changes) > 20:
                print(f"    ... and {len(report.changes) - 20} more")
        for path, reason in report.skips[:20]:
            print(f"  SKIP {path.name}: {reason}", file=sys.stderr)
        for path, err in report.errors[:20]:
            print(f"  ERR {path.name}: {err}", file=sys.stderr)
        if report.resume_after:
            print(f"  resume_after:       {report.resume_after}")

        total_changed = (
            report.user_stated + report.agent_observed + report.confirmed_inferred
        )
        if report.errors:
            return 1
        if not args.apply and total_changed > 0:
            return 2
        return 0
    finally:
        if lock is not None and not isinstance(lock, int):
            lock.release()


def _cmd_repair_legacy_slugs(
    wiki_root: Path,
    *,
    apply: bool,
    runner: Callable[..., Any],
) -> int:
    """Run the legacy bare-slug ``source:`` migration (issue #97).

    Exit codes:
        0 — clean run (zero candidates found, OR ``--apply`` succeeded
            with no validation failures and no errors).
        1 — errors encountered (read/write/validation failures), OR
            unknown bare-slug values seen (migration ABORTED per
            design-lock §5.2).
        2 — dry-run found candidates that WOULD be migrated.
    """
    report = runner(wiki_root, apply=apply)
    mode = "APPLY" if apply else "DRY RUN"
    print(f"=== repair legacy-source-slugs ({mode}) ===")
    print(f"  files_scanned: {report.files_scanned}")

    if report.unknown_slugs:
        # ABORT path. No rewrites were attempted. Report all unknown
        # slugs and the first 10 file paths so a human can decide whether
        # to update LEGACY_SLUG_MAP (a design-doc revision, not an
        # in-script change).
        print("  ABORTED: unknown bare-slug values found", file=sys.stderr)
        for slug, count in sorted(report.unknown_slugs.items()):
            print(f"    {slug}: {count} wikis", file=sys.stderr)
        print("  first 10 affected files:", file=sys.stderr)
        for path, slug in report.unknown_slug_files:
            print(f"    {path.name} ({slug})", file=sys.stderr)
        return 1

    if apply:
        print(f"  rewrites_applied:        {report.rewrites_applied}")
        print(f"  skipped_validation_fail: {report.skipped_validation_fail}")
    else:
        print(f"  would_rewrite:           {report.would_rewrite}")
    for slug, count in sorted(report.per_slug_counts.items()):
        typed = _legacy_slug_map_lookup(slug)
        print(f"    {slug} -> {typed}: {count} wikis")

    for path, err in report.errors[:20]:
        print(f"  ERR {path.name}: {err}", file=sys.stderr)

    if report.errors:
        return 1
    if not apply and report.would_rewrite > 0:
        return 2
    return 0


def _legacy_slug_map_lookup(slug: str) -> str:
    """Look up a slug in :data:`athenaeum.repair.LEGACY_SLUG_MAP` for output.

    Helper to keep the import surface inside ``_cmd_repair_legacy_slugs``
    minimal. Returns the raw slug if not present (defensive — should never
    happen because the runner already filtered unknowns).
    """
    from athenaeum.repair import LEGACY_SLUG_MAP

    return LEGACY_SLUG_MAP.get(slug, slug)
