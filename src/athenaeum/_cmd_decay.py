# SPDX-License-Identifier: Apache-2.0
"""``athenaeum decay-sweep`` — deterministic sweep for expired ``bucket: daily``
wiki pages (issue athenaeum#904, AC6).

Mirrors ``athenaeum auto-memory prune``'s CLI shape exactly (dry-run default,
``--apply`` git-archives the kill-list and rebuilds the recall index) — see
``athenaeum.decay_sweep`` for why this convention rather than a new one.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args, _iso_date
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, resolve_cache_dir


def add_decay_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``decay-sweep``."""

    parser = subparsers.add_parser(
        "decay-sweep",
        help="Archive expired bucket:daily wiki pages (issue athenaeum#904). "
        "Default is dry-run (prints kill-list + retained-list); --apply "
        "git-archives the kill-list in a two-commit pair and rebuilds the "
        "recall index.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Git-archive the kill-list (two-commit: provenance snapshot, "
        "then git rm) and rebuild the recall index. Without this flag the "
        "command is a dry-run.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--as-of",
        type=_iso_date,
        default=None,
        help="Rewind the expiry check to this date (YYYY-MM-DD) instead of "
        "today. Dry-run only in practice, but accepted by --apply too.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for the recall index rebuild "
        "(default: ~/.cache/athenaeum). --apply only.",
    )
    parser.add_argument(
        "--backend",
        choices=["fts5", "vector"],
        default=None,
        help="Override the recall index backend for the rebuild "
        "(default: read from athenaeum.yaml). --apply only.",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_decay_sweep)


def cmd_decay_sweep(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum decay-sweep``.

    Exit codes (mirroring ``auto-memory prune`` / ``repair``):
        0 - clean run (nothing to sweep, OR ``--apply`` succeeded with no
            errors).
        1 - errors encountered (apply without git, unreadable pages, ...).
        2 - dry-run found pages that WOULD be archived (CI / sign-off signal).
    """
    from athenaeum.config import load_config
    from athenaeum.decay_sweep import apply_sweep, build_sweep_report

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    report = build_sweep_report(
        wiki_root, as_of=args.as_of, knowledge_root=knowledge_root, config=cfg
    )

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== decay sweep ({mode}) ===")
    print(f"  scanned:         {report.scanned}")
    print(f"  kill:            {len(report.kill)}")
    # Issue athenaeum#1116 AC3: pages the active retention pack claims
    # authoritatively (routed off-corpus instead of archived).
    print(f"  routed off-corpus: {len(report.routed_off_corpus)}")
    print(f"  retained:        {len(report.retained)}")

    if report.kill:
        print("\n  KILL-LIST:")
        for cand in report.kill:
            print(f"    {cand.path.name}: {cand.reason}")
    if report.routed_off_corpus:
        print("\n  ROUTED OFF-CORPUS:")
        for cand in report.routed_off_corpus:
            print(f"    {cand.path.name}: {cand.reason}")
    if report.retained:
        print("\n  RETAINED:")
        for path, reason in report.retained:
            print(f"    {path.name}: {reason}")

    if not args.apply:
        for err in report.errors:
            print(f"  ERR {err}", file=sys.stderr)
        if report.errors:
            return 1
        return 2 if (report.kill or report.routed_off_corpus) else 0

    # --apply (mutating): acquire the single-machine run lock (issue athenaeum#309).
    lock = _acquire_or_exit(knowledge_root, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        report = apply_sweep(
            knowledge_root,
            report,
            cache_dir=getattr(args, "cache_dir", None),
            config=cfg,
        )
        for err in report.errors:
            print(f"  ERR {err}", file=sys.stderr)
        if report.errors:
            return 1

        if report.committed:
            print(
                f"\n  archived {len(report.kill)} page(s), routed "
                f"{len(report.routed_off_corpus)} page(s) off-corpus; committed."
            )
            _rebuild_recall_index(knowledge_root, cfg, args)
        else:
            print("\n  nothing archived or routed.")
        return 0
    finally:
        lock.release()


def _rebuild_recall_index(
    knowledge_root: Path,
    cfg: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    """Rebuild the recall index after a sweep apply.

    Mirrors ``_cmd_curate._rebuild_recall_index``'s backend resolution so the
    index reflects the archived pages. A rebuild failure is reported but
    never fails the sweep (the git archival already committed).
    """
    from athenaeum.config import resolve_extra_intake_roots
    from athenaeum.search import build_fts5_index, build_vector_index

    wiki_root = knowledge_root / "wiki"
    backend = getattr(args, "backend", None) or cfg.get("search_backend", "fts5")
    cache_dir = resolve_cache_dir(getattr(args, "cache_dir", None)).resolve()
    extra_roots = resolve_extra_intake_roots(knowledge_root, cfg)
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        if backend == "vector":
            count = build_vector_index(
                wiki_root, cache_dir, extra_roots=extra_roots, config=cfg
            )
        else:
            count = build_fts5_index(
                wiki_root, cache_dir, extra_roots=extra_roots, config=cfg
            )
        print(f"  recall index rebuilt ({backend}): {count} page(s).")
    except Exception as exc:  # noqa: BLE001 - rebuild failure must not fail sweep
        print(
            f"  WARN recall index rebuild failed ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
