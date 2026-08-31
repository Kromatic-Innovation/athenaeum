# SPDX-License-Identifier: Apache-2.0
"""``athenaeum pii-restore`` -- anchored PII-restore repair CLI (issue athenaeum#1037).

Thin presentation layer over :mod:`athenaeum.pii_restore`: parses arguments,
resolves the three roots (knowledge/wiki/contacts), builds a plan, prints the
report, and -- only under ``--apply`` -- takes the run lock, writes, and
triggers (or instructs) the reindex step. All classification, git plumbing,
and the safety pins live in the library module; nothing here decides
restore-vs-residue.

Dry-run is the default; ``--apply`` is explicit (athenaeum#1037's own first AC).

Exit codes, matching the ``athenaeum repair`` convention this issue names
(``_cmd_repair.py``'s ``--backfill-sources`` / ``--legacy-source-slugs``
branches):

    0 -- clean run (zero restorable markers found, OR ``--apply`` succeeded).
    1 -- errors (a PII-safety refusal, an unreadable root, lock contention,
         or git history being unconsultable for at least one marker --
         athenaeum#1228; the tool refuses to report a plan in that state
         rather than print a false ``TOTAL RESTORABLE = 0``).
    2 -- dry-run found markers that WOULD be restored (CI gate signal).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` -- this
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import cost
down.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def add_pii_restore_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum pii-restore`` and its flags on *subparsers*."""
    p = subparsers.add_parser(
        "pii-restore",
        help="Anchored PII-restore: recover non-PII tokens a "
        "[contact redacted -> excluded surface] marker replaced, via "
        "rename-following and retro-filename history lookup (issue athenaeum#1037). "
        "Default is dry-run; pass --apply to write fixes.",
    )
    p.add_argument(
        "--knowledge-root",
        type=Path,
        default=None,
        help="Knowledge root / git repo root (default: ~/knowledge).",
    )
    p.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="Wiki directory to scan for markers (default: <knowledge-root>/wiki).",
    )
    p.add_argument(
        "--contacts-root",
        type=Path,
        default=None,
        help="Excluded 'pii' surface root, resolved from config by default. "
        "Never scanned for markers and never written to.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write restorations. Without this flag the command is a dry-run.",
    )
    p.add_argument(
        "--reindex",
        action="store_true",
        help="After a successful --apply, rebuild the search index so restored "
        "text replaces corrupted text in the vector/fts5 index (issue athenaeum#1037). "
        "Rewriting a page changes its content hash -- WITHOUT this, --apply "
        "leaves the corrupted prose live in the index. Ignored on a dry-run.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of marker sites scanned (debugging/bounded runs).",
    )
    _add_lock_args(p)
    p.set_defaults(func=cmd_pii_restore)


def cmd_pii_restore(args: argparse.Namespace) -> int:
    from athenaeum.config import load_config
    from athenaeum.pii import contacts_surface_root
    from athenaeum.pii_restore import (
        GIT_HISTORY_UNAVAILABLE_REASON,
        PiiRestoreSafetyError,
        apply_restore_plan,
        build_restore_plan,
        render_report,
    )

    knowledge_root = (args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    wiki_root = (args.wiki_root or (knowledge_root / "wiki")).expanduser().resolve()
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    config = load_config(knowledge_root)
    contacts_root = (
        args.contacts_root.expanduser().resolve()
        if args.contacts_root
        else contacts_surface_root(knowledge_root, config)
    )

    plan = build_restore_plan(knowledge_root, wiki_root, contacts_root, limit=args.limit)

    unavailable = plan.git_history_unavailable_count()
    if unavailable:
        # Fail loudly rather than proceeding (athenaeum#1228): do NOT print a
        # dry-run/apply report here -- with even one marker's history
        # unconsulted, "TOTAL RESTORABLE"/"TOTAL RESTORED" would not be a
        # trustworthy corpus fact, only an artifact of git being unreachable.
        print(
            f"error: git history could not be consulted for {unavailable} "
            f"marker site(s) under {knowledge_root} ({GIT_HISTORY_UNAVAILABLE_REASON}) "
            "-- refusing to report a restore plan. This usually means the "
            "knowledge root is not a git repository, or `git log` failed for "
            "some other reason. Every affected marker would otherwise be "
            "misclassified as 'no-pre-image:page-created-after-migration' and "
            "TOTAL RESTORABLE would silently read as a false 0. Fix the "
            "repository/mount and re-run.",
            file=sys.stderr,
        )
        return 1

    if not args.apply:
        print(render_report(plan, applied=False))
        return 2 if plan.restorations else 0

    lock: "RunLock | int" = _acquire_or_exit(knowledge_root, args, config)
    if isinstance(lock, int):
        return lock
    try:
        try:
            result = apply_restore_plan(plan, wiki_root=wiki_root, contacts_root=contacts_root)
        except PiiRestoreSafetyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(render_report(plan, applied=True, result=result))
        _post_apply_index_step(args, knowledge_root, config)
    finally:
        lock.release()
    return 0


def _post_apply_index_step(
    args: argparse.Namespace, knowledge_root: Path, config: dict[str, Any] | None
) -> None:
    """Reindex (if ``--reindex``) or instruct one, matching ``migrate-pii``'s
    convention (:func:`athenaeum._cmd_storage._post_apply_index_step`).

    ``pii-restore --apply`` rewrites markdown but does not itself touch the
    search index: the index still embeds the pre-restore (corrupted) page
    text until a reindex runs, so restored prose is not actually reachable
    via ``recall`` without this step (issue athenaeum#1037's own AC). Kept as a
    near-duplicate of the ``migrate-pii`` version rather than a shared helper
    -- the two commands' post-apply messaging differs (restored vs. migrated
    text) and factoring out a two-line body for one shared caller would cost
    more than it saves.
    """
    if not args.reindex:
        print(
            "NOTE: the search index still carries the pre-restore (corrupted) "
            "page text -- the restored prose remains unreachable via recall "
            "until you reindex. Run:\n"
            f"  athenaeum reindex --path {knowledge_root}\n"
            "(incremental is sufficient; rewritten pages change content hash).",
            file=sys.stderr,
        )
        return
    try:
        from athenaeum.librarian import reindex as _reindex
    except ImportError as exc:  # pragma: no cover - defensive
        print(
            f"warning: could not import the reindexer ({exc}); run "
            f"`athenaeum reindex --path {knowledge_root}` manually so the "
            "restored text is reachable via recall.",
            file=sys.stderr,
        )
        return
    try:
        backend_name, pages = _reindex(knowledge_root, config=config)
    except ImportError as exc:
        backend = config.get("search_backend") if config else "?"
        print(
            f"warning: reindex failed to load the '{backend}' backend ({exc}); "
            f"run `athenaeum reindex --path {knowledge_root}` manually so the "
            "restored text is reachable via recall.",
            file=sys.stderr,
        )
        return
    print(
        f"reindexed ({backend_name}, {pages} page(s)) -- restored text is now "
        "reachable via recall."
    )
