# SPDX-License-Identifier: Apache-2.0
"""``athenaeum retire`` — retire any information by uid, in whichever store holds it.

Issue athenaeum#3151 widened ``athenaeum retire-pages`` (issue athenaeum#1625,
wiki pages only) to every store a uid can live in: wiki pages, contact
records and off-corpus records, each checked against the caller's rights in
that store. ``retire-pages`` stays registered as an alias for one release and
is then removed.

Same CLI shape as before: dry-run default, ``--apply`` retires. See
:mod:`athenaeum.retire_targets` for resolution, access and the per-store
mechanics, and :mod:`athenaeum.retire_pages` for the page path it reuses.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum._cli_shared import (
    _acquire_or_exit,
    _add_lock_args,
    rebuild_recall_index,
)
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

#: Recovery is git-only (issue athenaeum#1625 Plan step 7): no tombstone, no
#: second store. Named here once and reused in both the ``--apply`` help
#: text and the post-commit success message so the two can never drift.
_RECOVERY_HINT = (
    "a retired page's or contact record's content lives in the "
    "provenance-snapshot commit (HEAD~1 immediately after that store's archive "
    "commit) — run `git show <that-commit-sha>:<path>` to recover it, or `git "
    "log --diff-filter=D -- <path>` first if you need to find the commit."
)

#: The off-corpus store is outside git on purpose (a delete there is a true
#: erasure), so it has no snapshot and no recovery.
_OFF_CORPUS_HINT = (
    "an off-corpus record is erased, not archived: that store keeps no "
    "history, so it cannot be recovered."
)

_DEPRECATED_ALIAS = "retire-pages"


def add_retire_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``retire`` and its one-release alias ``retire-pages``."""
    for name in ("retire", _DEPRECATED_ALIAS):
        _add_one(subparsers, name)


def _add_one(subparsers: argparse._SubParsersAction, name: str) -> None:
    alias_note = (
        " Deprecated alias of `athenaeum retire` (issue athenaeum#3151); it is "
        "removed in the next release."
        if name == _DEPRECATED_ALIAS
        else ""
    )
    parser = subparsers.add_parser(
        name,
        help="Retire information by uid, in whichever store holds it: a wiki "
        "page, a contact record or an off-corpus record (issues athenaeum#1625, "
        "athenaeum#3151). Only what the caller can access is retired: a uid on "
        "a store the caller cannot read reads as unknown, and one on a store "
        "the caller cannot write is refused by name. Default is dry-run; "
        "--apply takes a provenance snapshot commit, removes, reindexes and "
        "records --reason. Unknown, ambiguous or refused uids abort the whole "
        f"run before any write. Recovery is git-only: {_RECOVERY_HINT} "
        f"{_OFF_CORPUS_HINT}{alias_note}",
    )
    select = parser.add_mutually_exclusive_group(required=True)
    select.add_argument(
        "--uids",
        nargs="+",
        metavar="FILE|UID",
        help="Either a single path to a file listing one uid per line "
        "(blank lines and lines starting with '#' are ignored), or one or "
        "more literal uids. A bare uid resolves to whichever store holds it; "
        "a uid naming a wiki page retires that page and its contact records. "
        "Prefix a uid to pin the store: page:<uid>, contact:<uid>, "
        "off-corpus:<uid> (contact:<uid> is how to name the contact records "
        "of a uid whose page is live). An unknown, ambiguous or refused uid "
        "aborts the whole run before any write.",
    )
    select.add_argument(
        "--pageless",
        action="store_true",
        help="Select every contact record `athenaeum contact-surface-lint` "
        "reports as page-less (no live page and no merge record). Prints, "
        "per uid, the record count, each value masked, whether the value "
        "also lives on a live record, and the commit that added the record; "
        "--apply retires the selection.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Retire the selection. Pages: a two-commit pair (provenance "
        "snapshot, then git rm + wiki index rebuild + pending-merge "
        "withdrawal), the pages' contact records settled, then the recall "
        "index rebuilt. Contact records: a two-commit pair, then the "
        "persisted contact-record index updated. Off-corpus records: erased "
        "with both index shards. Without this flag the command is a dry-run. "
        f"Recovery is git-only: {_RECOVERY_HINT} {_OFF_CORPUS_HINT}",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Why this information is being retired. Recorded verbatim in "
        "the archive commit messages, every withdrawn pending-merge "
        "proposal's archive note, and the off-corpus retirement ledger "
        "(--apply only).",
    )
    parser.add_argument(
        "--force-retire",
        action="store_true",
        help="Override a retirement guard: allow retiring the contact records "
        "of a uid whose page is live (they would otherwise be its only "
        "contact records). The override is "
        "recorded in the archive commit message.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for the recall index rebuild, the persisted "
        "contact-record index and the off-corpus index shards "
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
    parser.set_defaults(func=cmd_retire)


def _read_uids(tokens: list[str]) -> list[str]:
    """Resolve ``--uids``' ``FILE|UID...`` shape to a flat uid list.

    A single token that names an existing file is read as one uid per
    line (blank lines and ``#``-comments dropped); any other shape (one or
    several tokens that are not, singly, an existing file) is treated as
    literal uids directly.
    """
    if len(tokens) == 1 and Path(tokens[0]).is_file():
        lines = Path(tokens[0]).read_text(encoding="utf-8").splitlines()
        return [
            stripped
            for line in lines
            if (stripped := line.strip()) and not stripped.startswith("#")
        ]
    return list(tokens)


def cmd_retire(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum retire`` (and its alias ``retire-pages``).

    Exit codes (mirroring ``decay-sweep``):
        0 - clean run (nothing selected, OR ``--apply`` succeeded with no
            errors).
        1 - errors encountered (unknown/ambiguous/refused uid, apply without
            git, withdrawal/rebuild failure, ...).
        2 - dry-run found information that WOULD be retired (CI / sign-off
            signal).
    """
    from athenaeum.config import load_config, resolve_cache_dir
    from athenaeum.retire_targets import (
        STORE_CONTACT,
        STORE_OFF_CORPUS,
        STORE_PAGE,
        apply_plan,
        plan_pageless_retirement,
        plan_retirement,
        preview_pages,
    )

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1
    if getattr(args, "command", None) == _DEPRECATED_ALIAS:
        print(
            "note: `retire-pages` is a deprecated alias of `athenaeum retire` "
            "and is removed in the next release.",
            file=sys.stderr,
        )

    cfg = load_config(knowledge_root)
    if args.pageless:
        plan = plan_pageless_retirement(knowledge_root, cfg)
    else:
        plan = plan_retirement(knowledge_root, _read_uids(args.uids), cfg, force=args.force_retire)

    if plan.blocked:
        # Nothing has been written: planning is read-only (issue athenaeum#1625
        # AC, extended to every store by athenaeum#3151).
        for msg in plan.errors:
            print(f"  ERR {msg}", file=sys.stderr)
        for err in plan.unresolved:
            print(f"  ERR {err}", file=sys.stderr)
        for refusal in plan.refused:
            print(f"  ERR {refusal}", file=sys.stderr)
        if plan.refused:
            print(
                "  nothing retired: a run retires everything it names or nothing.",
                file=sys.stderr,
            )
        return 1

    if not plan.targets:
        print("Nothing selected - nothing to retire.")
        return 0

    pages = plan.of_store(STORE_PAGE)
    contacts = plan.of_store(STORE_CONTACT)
    off = plan.of_store(STORE_OFF_CORPUS)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== retire ({mode}) ===")
    print(f"  reason: {args.reason}")
    if pages:
        print(f"  pages: {len(pages)}")
        for t in pages:
            note = ""
            if t.cascade_records:
                note = f"  (+{t.cascade_records} contact record(s) settled with it)"
            print(f"    {t.uid}: {t.paths[0].name}{note}")
    if contacts:
        print(f"  contact records: {sum(len(t.paths) for t in contacts)}")
        summaries = {s.uid: s for s in plan.pageless}
        for t in contacts:
            print(f"    {t.uid}: {len(t.paths)} record(s)")
            summary = summaries.get(t.uid)
            if summary is not None:
                print(f"      values: {len(summary.masked_values)}")
                for masked, live in zip(summary.masked_values, summary.on_live_record):
                    where = "also on a live record" if live else "on no live record"
                    print(f"        {masked} ({where})")
                print(f"      origin commit: {summary.origin_commit or 'unknown'}")
    if off:
        print(f"  off-corpus records: {len(off)}")
        for t in off:
            print(f"    {t.uid}")
        print(f"  note: {_OFF_CORPUS_HINT}")

    if pages:
        preview = preview_pages(knowledge_root, plan)
        if preview.withdrawn_merges:
            print("\n  PENDING MERGES TO WITHDRAW:")
            for w in preview.withdrawn_merges:
                print(f"    {w.merge_target_name}: {w.reason}")
        if preview.index_lines_removed:
            print("\n  INDEX ENTRIES TO REMOVE:")
            for name in preview.index_lines_removed:
                print(f"    {name}")

    if not args.apply:
        return 2

    # --apply (mutating): acquire the single-machine run lock (issue athenaeum#309).
    lock = _acquire_or_exit(knowledge_root, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        cache_dir = resolve_cache_dir(args.cache_dir).resolve()
        report = apply_plan(
            knowledge_root,
            plan,
            reason=args.reason,
            cache_dir=cache_dir,
            config=cfg,
            force=args.force_retire,
        )
        for apply_err in report.errors:
            print(f"  ERR {apply_err}", file=sys.stderr)
        pages_done = bool(report.pages and report.pages.committed)
        if pages_done:
            print(f"\n  retired {len(pages)} page(s); committed.")
            rebuild_recall_index(knowledge_root, cfg, args)
        if report.contact_records_retired:
            print(f"  retired {report.contact_records_retired} contact record(s); committed.")
        if report.off_corpus_erased:
            print(f"  erased {report.off_corpus_erased} off-corpus record(s).")
        if pages_done or report.contact_records_retired:
            print(f"  Recovery: {_RECOVERY_HINT}")
        if report.errors:
            return 1
        if not (pages_done or report.contact_records_retired or report.off_corpus_erased):
            print("\n  nothing retired.")
        return 0
    finally:
        lock.release()
