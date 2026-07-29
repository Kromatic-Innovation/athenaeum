# SPDX-License-Identifier: Apache-2.0
"""``athenaeum storage`` — storage-surface operator commands (issue #479).

A thin CLI dispatcher over :mod:`athenaeum.storage_migrate` (which holds the
pure transform logic), mirroring :mod:`athenaeum._cmd_authority`'s shape: the
top-level ``storage`` parser owns a ``storage_target`` sub-command, and each
mode is a small function that resolves inputs, calls a library transform, and
prints/writes. No business logic lives here.

Two sub-commands:

- ``migrate-pii`` — move a live entity page's archival contact data
  (emails/phones) to the #427 excluded surface, dry-run by default (``--apply``
  writes). Single page (``--page``) or bulk over the whole entity set
  (``--all`` / ``--glob``, issue #495).
- ``lint-pii`` — a corpus-wide PII gate: scan EVERY file under ``wiki/`` (not
  only entity pages — ``_``-prefixed queue/index/archive files and ``.bak``
  files included) for an inline email/phone and exit non-zero on any finding,
  so a body-text email cannot silently regrow after the sweep (issue #495).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import load_config
from athenaeum.pii import is_pii_class_excluded, scan_corpus_pii
from athenaeum.storage_migrate import (
    PiiMigrationPlan,
    iter_entity_pages,
    iter_glob_pages,
    plan_pii_migration,
)

#: Exit code when ``lint-pii`` finds inline PII. Mirrors
#: :data:`athenaeum._cmd_outbound.EXIT_PII_FOUND` (2) — a "found something to
#: act on" signal distinct from the generic error code 1 — so a shell can gate
#: on a clean scan (``athenaeum storage lint-pii && ...``). Defined locally
#: rather than imported to keep the two lint CLIs decoupled (same rationale the
#: detectors themselves are shared but the CLIs are not).
EXIT_PII_FOUND = 2

#: How often bulk apply/scan emits a progress line to stderr. A silent
#: 11.5k-page run is indistinguishable from a hung one (issue #495), so
#: progress is reported every this-many pages plus a final summary.
_PROGRESS_EVERY = 500


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
            "Move archival contact data (emails/phones) off entity pages to "
            "the #427 excluded surface, leaving durable identifiers only. "
            "Single page (--page) or bulk (--all / --glob)."
        ),
    )
    migrate_p.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge root (default: ~/knowledge).",
    )
    # Exactly one target selector. --page keeps #479's single-page behavior
    # byte-for-byte; --all / --glob are #495's bulk modes.
    target = migrate_p.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--page",
        type=Path,
        default=None,
        help="Path to a single live entity wiki page to migrate.",
    )
    target.add_argument(
        "--all",
        action="store_true",
        help=(
            "Bulk: migrate every entity page (top-level wiki/*.md, skipping "
            "_-prefixed queue/index/archive files) that carries contact data. "
            "Idempotent — re-running skips already-migrated pages, so a run "
            "that dies halfway resumes cleanly with no double-writes."
        ),
    )
    target.add_argument(
        "--glob",
        default=None,
        metavar="PATTERN",
        help=(
            "Bulk: migrate every file under wiki/ matching PATTERN (supports "
            "recursive ** globs; not restricted to *.md), e.g. an archive to "
            "redact in place. Same idempotent/resumable semantics as --all."
        ),
    )
    migrate_p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write the changes (rewrite the origin page + create the excluded "
            "contact record). Without this flag the command is a dry-run that "
            "prints what would change and writes nothing. In bulk mode the "
            "dry-run prints a summary (pages affected, records to create), not "
            "one diff per page."
        ),
    )
    migrate_p.add_argument(
        "--reindex",
        action="store_true",
        help=(
            "After a successful --apply, rebuild the search index so the "
            "migrated contact data is no longer recallable (issue #502). "
            "Rewriting a page changes its content hash, so an incremental "
            "reindex evicts the stale index entry and re-embeds the scrubbed "
            "text — WITHOUT this, --apply leaves the pre-migration text live in "
            "the index and every migrated address stays reachable via recall. "
            "Ignored on a dry-run (nothing changed to reindex)."
        ),
    )

    lint_p = s_sub.add_parser(
        "lint-pii",
        help=(
            "Corpus-wide PII gate: scan EVERY file under wiki/ (queue/index/"
            "archive/_-prefixed and .bak files included) for an inline email/"
            "phone; exit non-zero on any finding (issue #495)."
        ),
    )
    lint_p.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge root (default: ~/knowledge).",
    )
    lint_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON findings instead of plain text.",
    )


def cmd_storage(args: argparse.Namespace) -> int:
    sub = getattr(args, "storage_target", None)
    if sub == "migrate-pii":
        return _cmd_storage_migrate_pii(args)
    if sub == "lint-pii":
        return _cmd_storage_lint_pii(args)
    print("usage: athenaeum storage {migrate-pii,lint-pii} [...]", file=sys.stderr)
    return 2


def _apply_plan(plan: PiiMigrationPlan) -> None:
    """Write one migration plan: excluded record first, then scrub the origin.

    Excluded record is written BEFORE the origin is scrubbed so a crash between
    the two writes leaves the archival copy safely on disk (never the reverse —
    a scrubbed origin with no excluded record would lose the contact data). The
    excluded path is deterministic (``page_path.name``), so a re-run overwrites
    the same record and scrubs the still-dirty origin: idempotent, no
    double-write. Both writes are atomic (temp-file + rename).
    """
    plan.excluded_page_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(plan.excluded_page_path, plan.excluded_page_text or "")
    atomic_write_text(plan.page_path, plan.rewritten_page_text or "")


def _post_apply_index_step(
    args: argparse.Namespace, knowledge_root: Path, config: dict | None
) -> None:
    """Reindex (if --reindex) and/or emit the required-follow-up notice (#502).

    ``migrate-pii --apply`` rewrites the markdown but does NOT itself touch the
    search index: with the vector backend the embeddings still carry the
    PRE-migration page text, so every migrated address stays recallable until a
    reindex runs. An operator who sees only "migrated N page(s)" and stops has
    moved nothing out of reach of ``recall`` (issue #502, live-sweep finding).

    So after a successful apply this NEVER prints an unqualified success on its
    own — the caller's "migrated" line is always followed here by either the
    reindex result (``--reindex``) or an explicit instruction to run one. An
    incremental reindex suffices: a rewritten page's whole-file content hash
    changes, so the differ evicts its stale index entry and re-embeds the
    scrubbed text (a ``--full`` rebuild is not required).
    """
    if not args.reindex:
        print(
            "NOTE: the search index still carries the pre-migration page text — "
            "the migrated contact data remains recallable until you reindex. Run:\n"
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
            "migrated data leaves the search index.",
            file=sys.stderr,
        )
        return
    try:
        backend_name, pages = _reindex(knowledge_root, config=config)
    except ImportError as exc:
        # e.g. the `vector` backend configured but chromadb not installed.
        backend = config.get("search_backend") if config else "?"
        print(
            f"warning: reindex failed to load the '{backend}' backend ({exc}); "
            f"run `athenaeum reindex --path {knowledge_root}` manually so the "
            "migrated data leaves the search index.",
            file=sys.stderr,
        )
        return
    print(
        f"reindexed ({backend_name}, {pages} page(s)) — migrated contact data "
        "is no longer in the search index."
    )


def _cmd_storage_migrate_pii(args: argparse.Namespace) -> int:
    if args.all or args.glob is not None:
        return _cmd_storage_migrate_pii_bulk(args)
    return _cmd_storage_migrate_pii_single(args)


def _cmd_storage_migrate_pii_single(args: argparse.Namespace) -> int:
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

    _apply_plan(plan)
    print(f"migrated PII off {page_path}\n{summary}")
    _post_apply_index_step(args, knowledge_root, config)
    return 0


def _resolve_bulk_pages(args: argparse.Namespace, wiki_root: Path) -> list[Path]:
    """Resolve the target page set for --all / --glob (materialized for a total)."""
    if args.glob is not None:
        return list(iter_glob_pages(wiki_root, args.glob))
    return list(iter_entity_pages(wiki_root))


def _cmd_storage_migrate_pii_bulk(args: argparse.Namespace) -> int:
    """Bulk migrate every targeted page's PII off-corpus (issue #495).

    Idempotent + resumable by construction: each page is planned independently
    and a page with no contact data is skipped, so a re-run (after a clean
    finish OR a crash halfway) applies only the remaining dirty pages — no run
    ledger, no double-writes. Dry-run prints a summary, not 11.5k diffs;
    apply reports progress so a long run is distinguishable from a hung one.
    """
    knowledge_root = _resolve_knowledge_root(args)
    wiki_root = knowledge_root / "wiki"
    config = load_config(knowledge_root)

    # Same safety gate as the single-page path: refuse to --apply (which would
    # write contact records) unless the operator has actually mapped ``pii`` to
    # an excluded surface — writing there otherwise leaks the PII back into the
    # corpus. The dry-run still previews with a warning.
    if not is_pii_class_excluded(config):
        msg = (
            "error: the 'pii' entity class is not mapped to an excluded surface "
            "(storage.mapping.pii). Writing there would keep the contact data in "
            "the corpus. Configure storage.mapping.pii: excluded in athenaeum.yaml "
            "before migrating."
        )
        if args.apply:
            print(msg, file=sys.stderr)
            return 1
        print(
            "[DRY RUN] WARNING: 'pii' is not mapped to an excluded surface; "
            "--apply would be refused until you configure storage.mapping.pii.",
            file=sys.stderr,
        )

    pages = _resolve_bulk_pages(args, wiki_root)
    total = len(pages)
    selector = args.glob if args.glob is not None else "--all (entity pages)"
    print(
        f"[migrate-pii] scanning {total} page(s) under {wiki_root} ({selector})",
        file=sys.stderr,
    )

    affected = 0
    records = 0
    total_emails = 0
    total_phones = 0
    name_pii_excluded = 0
    for i, page_path in enumerate(pages, start=1):
        try:
            plan = plan_pii_migration(page_path, config, knowledge_root)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[migrate-pii] skip {page_path}: {exc}", file=sys.stderr)
            continue
        if plan.name_field_pii:
            name_pii_excluded += 1
        if plan.changed:
            affected += 1
            records += 1
            total_emails += len(plan.emails)
            total_phones += len(plan.phones)
            if args.apply:
                _apply_plan(plan)
        if i % _PROGRESS_EVERY == 0 or i == total:
            verb = "migrated" if args.apply else "would migrate"
            print(
                f"[migrate-pii] {i}/{total} scanned, {verb} {affected}",
                file=sys.stderr,
            )

    mode = "migrated" if args.apply else "[DRY RUN] would migrate"
    print(
        f"{mode} {affected} page(s) of {total} scanned; "
        f"{records} excluded contact record(s) to create; "
        f"{total_emails} email(s), {total_phones} phone(s)."
    )
    if name_pii_excluded:
        # The name-is-an-email population (#502): EXCLUDED from this automatic
        # path (renaming breaks slugs/edges) and handled in a separate slice.
        # Surface it so it is visible, not silently dropped.
        print(
            f"NOTE: {name_pii_excluded} page(s) are named after an email "
            "address (name:/preferred_name:) and were NOT migrated — renaming "
            "is unsafe and is handled by the separate name-is-an-email slice."
        )
    if not args.apply and affected:
        print("re-run with --apply to write the changes.")
    if args.apply and affected:
        _post_apply_index_step(args, knowledge_root, config)
    return 0


def _cmd_storage_lint_pii(args: argparse.Namespace) -> int:
    """Corpus-wide PII gate (issue #495): non-zero exit on any inline finding.

    Scans EVERY file under ``wiki/`` — not only entity pages, so ``_``-prefixed
    queue/index/archive files and stray ``.bak`` files are covered — for an
    inline email/phone token. Exits :data:`EXIT_PII_FOUND` (2) when any is
    found so a body-text email cannot silently regrow after the sweep, ``0``
    when the corpus is clean.
    """
    knowledge_root = _resolve_knowledge_root(args)
    wiki_root = knowledge_root / "wiki"
    findings = scan_corpus_pii(wiki_root)

    if args.json:
        import json

        payload = [
            {
                "path": str(f.path),
                "emails": f.emails,
                "phones": f.phones,
            }
            for f in findings
        ]
        sys.stdout.write(json.dumps(payload) + "\n")
        return EXIT_PII_FOUND if findings else 0

    if not findings:
        print(f"0 inline PII findings under {wiki_root}")
        return 0

    n = sum(len(f.emails) + len(f.phones) for f in findings)
    print(f"{n} inline PII finding(s) in {len(findings)} file(s) under {wiki_root}:")
    for f in findings:
        parts: list[str] = []
        if f.emails:
            parts.append(f"emails={f.emails}")
        if f.phones:
            parts.append(f"phones={f.phones}")
        print(f"  {f.path}: {'; '.join(parts)}")
    return EXIT_PII_FOUND
