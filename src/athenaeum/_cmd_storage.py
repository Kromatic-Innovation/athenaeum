# SPDX-License-Identifier: Apache-2.0
"""``athenaeum storage`` — storage-surface operator commands (issue athenaeum#479).

A thin CLI dispatcher over :mod:`athenaeum.storage_migrate` (which holds the
pure transform logic), mirroring :mod:`athenaeum._cmd_authority`'s shape: the
top-level ``storage`` parser owns a ``storage_target`` sub-command, and each
mode is a small function that resolves inputs, calls a library transform, and
prints/writes. No business logic lives here.

Two sub-commands:

- ``migrate-pii`` — move a live entity page's archival contact data
  (emails/phones) to the athenaeum#427 excluded surface, dry-run by default (``--apply``
  writes). Single page (``--page``) or bulk over the whole entity set
  (``--all`` / ``--glob``, issue athenaeum#495). ``--rename-name-email`` (issue athenaeum#505,
  bulk-only) additionally migrates the ~80-page name-is-an-email population
  athenaeum#502 deliberately excluded: rename a confidently-nameable page (derived
  display name from the local-part), move the address off-corpus, and rewrite
  inbound wikilinks — an ambiguous local-part is left unrenamed and reported
  as a residual count instead.
- ``lint-pii`` — a corpus-wide PII gate: scan EVERY file under ``wiki/`` (not
  only entity pages — ``_``-prefixed queue/index/archive files and ``.bak``
  files included) for an inline email/phone and exit non-zero on any finding,
  so a body-text email cannot silently regrow after the sweep (issue athenaeum#495).
  Also scans ``raw/`` (issue athenaeum#1049) and reports it as a SEPARATE,
  non-gating surface: ``raw/`` is append-only and retains every original
  value by contract, so folding its count into the gate would make the
  command permanently fail with no fix in scope — see athenaeum#1049 and
  ``docs/sensitivity-value-routing.md`` §5.
- ``lint-mapping`` — the ``storage.mapping`` completeness lint + the deferred
  `(read_policy, adapter)` pair check (issue athenaeum#993, S5 of
  ``docs/sensitivity-class-vocabulary.md`` §9). A thin CLI wrapper over
  :mod:`athenaeum.sensitivity_lint`, which holds all the check logic; exits
  non-zero only on a completeness finding (a D4 policy-mismatch finding is
  advisory and never fails the gate on its own).

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
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config
from athenaeum.pending_merges_pii import scrub_pending_merges
from athenaeum.pii import (
    PII_ALLOWLIST_FILENAME,
    adjudicate_corpus_pii,
    is_pii_class_excluded,
    load_pii_allowlist,
    resolve_pii_scan_exclude_filenames,
    scan_corpus_pii,
    scan_excluded_by_name,
)
from athenaeum.rules import (
    DispositionPruneMismatchError,
    default_shape_rule_dispositions_path,
    prune_shape_rule_dispositions_to_positive,
)
from athenaeum.sensitivity_lint import (
    SensitivityMappingLintResult,
    lint_sensitivity_storage_mapping,
)
from athenaeum.storage_migrate import (
    NameEmailRenameReport,
    PiiMigrationPlan,
    bulk_rename_name_email_pages,
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

#: Exit code when ``lint-mapping`` finds a completeness gap (a sensitivity
#: class the scanned corpus carries with no live ``storage.mapping`` entry,
#: or one mapped to a nonexistent adapter). Same value/rationale as
#: :data:`EXIT_PII_FOUND` — kept as its own named constant (not imported)
#: since the two lint CLIs are deliberately decoupled. A D4 policy-mismatch
#: finding never triggers this exit code on its own — see
#: :attr:`athenaeum.sensitivity_lint.SensitivityMappingLintResult.is_clean`.
EXIT_MAPPING_ISSUES = 2

#: How often bulk apply/scan emits a progress line to stderr. A silent
#: 11.5k-page run is indistinguishable from a hung one (issue athenaeum#495), so
#: progress is reported every this-many pages plus a final summary.
_PROGRESS_EVERY = 500


def _resolve_knowledge_root(args: argparse.Namespace) -> Path:
    return (getattr(args, "path", None) or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()


def add_storage_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``storage`` and its sub-commands (issue athenaeum#479)."""
    s_parser = subparsers.add_parser(
        "storage",
        help="Storage-surface operator tasks (migrate a page's PII off-corpus).",
    )
    s_parser.set_defaults(func=cmd_storage)
    s_sub = s_parser.add_subparsers(dest="storage_target")

    migrate_p = s_sub.add_parser(
        "migrate-pii",
        help=(
            "Move archival contact data (emails/phones) off entity pages to "
            "the athenaeum#427 excluded surface, leaving durable identifiers only. "
            "Single page (--page) or bulk (--all / --glob)."
        ),
    )
    migrate_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge root (default: ~/knowledge).",
    )
    # Exactly one target selector. --page keeps athenaeum#479's single-page behavior
    # byte-for-byte; --all / --glob are athenaeum#495's bulk modes.
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
            "migrated contact data is no longer recallable (issue athenaeum#502). "
            "Rewriting a page changes its content hash, so an incremental "
            "reindex evicts the stale index entry and re-embeds the scrubbed "
            "text — WITHOUT this, --apply leaves the pre-migration text live in "
            "the index and every migrated address stays reachable via recall. "
            "Ignored on a dry-run (nothing changed to reindex)."
        ),
    )
    migrate_p.add_argument(
        "--rename-name-email",
        action="store_true",
        help=(
            "Also migrate the name-is-an-email population (issue athenaeum#505): a page "
            "whose name:/preferred_name: IS an email address (the athenaeum#502 carve-"
            "out) is renamed to a display name derived from the local-part "
            "(e.g. jane.doe@acme.com -> 'Jane Doe'), the address is moved to "
            "the excluded contact record, and inbound [[wikilink]]s are "
            "rewritten to the new slug. An ambiguous local-part (role address, "
            "+tag, initial-blob, numeric/opaque) is left unrenamed and counted "
            "as a residual rather than guessed at. Scoped by whichever target "
            "selector is in use (--page / --all / --glob); combines with the "
            "ordinary contact-data migration in the same run unless "
            "--rename-only is given."
        ),
    )
    migrate_p.add_argument(
        "--rename-only",
        action="store_true",
        help=(
            "Run ONLY the name-is-an-email rename slice (issue athenaeum#505); skip the "
            "body-text contact-data migration entirely. Implies "
            "--rename-name-email. Use this when the body-migration pass would "
            "act on findings you do not want migrated — e.g. while the phone "
            "axis still carries detector false positives, where a full "
            "--all --apply would redact real prose (issue athenaeum#745; the failure "
            "mode athenaeum#691 spent two restore passes repairing)."
        ),
    )
    migrate_p.add_argument(
        "--rename-to",
        metavar="NAME",
        default=None,
        help=(
            "Operator-supplied display name for a --page rename (issue athenaeum#745). "
            "athenaeum#505 refuses to GUESS a name from an ambiguous local-part, but "
            "offered no way to supply one — so the deferred population had no "
            "route through the tool and could only be hand-edited, which skips "
            "the excluded record, the slug rename and the inbound-link "
            "rewrite. This is a human asserting the name, so it bypasses the "
            "confidence gate by design. Requires --page and --rename-name-email "
            "(or --rename-only)."
        ),
    )
    migrate_p.add_argument(
        "--list-deferred",
        action="store_true",
        help=(
            "List the pages the rename slice deferred (ambiguous local-part) "
            "with their reason, instead of only counting them. This is the "
            "operator's manual-naming worklist — athenaeum#505 deliberately never "
            "guesses a display name, so the deferred set is work a human has "
            "to do and needs to be enumerable (issue athenaeum#745)."
        ),
    )

    lint_p = s_sub.add_parser(
        "lint-pii",
        help=(
            "Corpus-wide PII gate: scan EVERY file under wiki/ (queue/index/"
            "archive/_-prefixed and .bak files included) for an inline email/"
            "phone; exit non-zero on any finding (issue athenaeum#495). Also "
            "reports raw/ retention as a separate, non-gating count (issue "
            "athenaeum#1049)."
        ),
    )
    lint_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge root (default: ~/knowledge).",
    )
    lint_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON findings instead of plain text.",
    )
    lint_p.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=(
            "Adjudicated allowlist of values that are NOT PII (service "
            "accounts, tagged test addresses, example-domain placeholders, "
            "identifier/timestamp digit runs the phone axis misreads). Each "
            "entry needs a non-empty reason. Default: "
            f"<knowledge-root>/wiki/{PII_ALLOWLIST_FILENAME}. A missing file "
            "means nothing is adjudicated. The allowlist is excluded from its "
            "own scan (issue athenaeum#936, unblocking athenaeum#437)."
        ),
    )

    lint_mapping_p = s_sub.add_parser(
        "lint-mapping",
        help=(
            "storage.mapping completeness lint + the deferred (read_policy, "
            "adapter) pair check (issue athenaeum#993): every sensitivity class "
            "the scanned corpus carries must have a live storage.mapping "
            "entry naming a real adapter; exit non-zero on a gap. Advisory-"
            "only D4 policy-mismatch findings are also reported but never "
            "fail the gate on their own."
        ),
    )
    lint_mapping_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge root (default: ~/knowledge); also the default corpus root.",
    )
    lint_mapping_p.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help=(
            "Corpus root to scan for sensitivity_class: frontmatter "
            "(default: the --path knowledge root). Always caller-supplied — "
            "this lint never falls back to a hardcoded or environment-"
            "derived path (issue athenaeum#993's own AC)."
        ),
    )
    lint_mapping_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON findings instead of plain text.",
    )

    prune_p = s_sub.add_parser(
        "prune-dispositions",
        help=(
            "One-time prune of wiki/_shape_rule_dispositions.jsonl to its "
            "positive-disposition records only (issue athenaeum#1274 AC3/AC4). "
            "Dry-run by default: reports the disposition histogram and "
            "projected size. --apply writes."
        ),
    )
    prune_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge root (default: ~/knowledge).",
    )
    prune_p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write the pruned ledger (atomic replace). Without this flag the "
            "command is a dry-run that prints the histogram and projected "
            "size and writes nothing. Refuses to write (exit 1, nothing "
            "written) if a re-parse of the constructed output does not "
            "carry exactly the positive-row count the scan pass promised."
        ),
    )
    _add_lock_args(prune_p)


def cmd_storage(args: argparse.Namespace) -> int:
    sub = getattr(args, "storage_target", None)
    if sub == "migrate-pii":
        return _cmd_storage_migrate_pii(args)
    if sub == "lint-pii":
        return _cmd_storage_lint_pii(args)
    if sub == "lint-mapping":
        return _cmd_storage_lint_mapping(args)
    if sub == "prune-dispositions":
        return _cmd_storage_prune_dispositions(args)
    print(
        "usage: athenaeum storage {migrate-pii,lint-pii,lint-mapping,"
        "prune-dispositions} [...]",
        file=sys.stderr,
    )
    return 2


def _apply_plan(plan: PiiMigrationPlan) -> None:
    """Write one migration plan: excluded record first, then scrub the origin.

    Excluded record is written BEFORE the origin is scrubbed so a crash between
    the two writes leaves the archival copy safely on disk (never the reverse —
    a scrubbed origin with no excluded record would lose the contact data). The
    excluded path is deterministic (``page_path.name``), so a re-run MERGES
    into the same record (issue athenaeum#1108 — ``plan.excluded_page_text`` is
    already the merged text by the time it reaches here) and scrubs the
    still-dirty origin: idempotent, no double-write, no dropped prior data.
    Both writes are atomic (temp-file + rename). Callers must not invoke this
    when ``plan.excluded_record_conflicts`` is non-empty — that is refused
    upstream in ``_cmd_storage_migrate_pii_single``/``_bulk`` before this runs.
    """
    plan.excluded_page_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(plan.excluded_page_path, plan.excluded_page_text or "")
    atomic_write_text(plan.page_path, plan.rewritten_page_text or "")


def _print_excluded_record_conflicts(plan: PiiMigrationPlan, *, refusing: bool) -> None:
    """Print each scalar-identity disagreement between the plan and the record on disk.

    Never silent (issue athenaeum#1108, AC1): both the dry-run preview and the
    apply-time refusal route through this so a conflict is always visible
    before it could otherwise be mistaken for a clean merge.
    """
    fields = ", ".join(c.field for c in plan.excluded_record_conflicts)
    if refusing:
        print(
            f"error: refusing to --apply: the excluded contact record at "
            f"{plan.excluded_page_path} already disagrees with this run on "
            f"{fields}. Resolve by hand, then re-run:",
            file=sys.stderr,
        )
    else:
        print(
            f"[DRY RUN] WARNING: excluded record at {plan.excluded_page_path} "
            f"already disagrees with this run on {fields}; --apply would refuse "
            "until resolved:",
            file=sys.stderr,
        )
    for conflict in plan.excluded_record_conflicts:
        print(
            f"  {conflict.field}: existing={conflict.existing_value!r} "
            f"new={conflict.new_value!r}",
            file=sys.stderr,
        )


def _scrub_merge_sidecar(knowledge_root: Path, values: list[str], *, apply: bool) -> None:
    """Redact just-migrated values out of ``_pending_merges.md`` (issue athenaeum#1276).

    A merge proposal stores its ``draft_merged_body`` verbatim, so a page whose
    PII this run just moved off-corpus can still have a plain-text copy of the
    same addresses sitting in the sidecar — an invisible failure: the page reads
    clean, the excluded record exists, the index is refreshed, and ``lint-pii``
    still finds the values under ``wiki/``. Scrubbing here is what makes
    "migrated" mean migrated.

    Never silent in either direction: a redaction is reported, and so is a value
    the scrubber deliberately left on an identity-bearing line (see
    :class:`~athenaeum.pending_merges_pii.ProposalPiiResidual`). Follows the
    caller's dry-run/apply mode, so ``migrate-pii`` without ``--apply`` still
    writes nothing anywhere.
    """
    if not values:
        return
    merges_path = knowledge_root / "wiki" / "_pending_merges.md"
    try:
        result = scrub_pending_merges(merges_path, values=values, apply=apply)
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        print(
            f"warning: could not scrub {merges_path} ({exc}); the migrated "
            "contact data may still be embedded in a pending merge proposal. "
            "Run `athenaeum merges scrub-pii --apply` once resolved.",
            file=sys.stderr,
        )
        return
    if result.scrubbed:
        verb = "redacted" if result.applied else "[DRY RUN] would redact"
        print(
            f"{verb} {result.values_redacted} migrated value(s) from "
            f"{len(result.scrubbed)} pending merge proposal(s) in "
            f"{merges_path.name}."
        )
    for residual in result.residual:
        print(
            f"NOTE: {merges_path.name}: proposal {residual.merge_target_name!r} "
            f"still names {len(residual.values)} migrated value(s) on an "
            "identity-bearing line (header/sources) — left in place because "
            "rewriting it would re-id the proposal. Rename the underlying page "
            "(`migrate-pii --rename-name-email`, issue athenaeum#505) and "
            "re-propose.",
            file=sys.stderr,
        )


def _post_apply_index_step(
    args: argparse.Namespace, knowledge_root: Path, config: dict | None
) -> None:
    """Reindex (if --reindex) and/or emit the required-follow-up notice (athenaeum#502).

    ``migrate-pii --apply`` rewrites the markdown but does NOT itself touch the
    search index: with the vector backend the embeddings still carry the
    PRE-migration page text, so every migrated address stays recallable until a
    reindex runs. An operator who sees only "migrated N page(s)" and stops has
    moved nothing out of reach of ``recall`` (issue athenaeum#502, live-sweep finding).

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


def _run_rename_slice(
    args: argparse.Namespace,
    wiki_root: Path,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    pages: list[Path] | None,
) -> NameEmailRenameReport:
    """Run + report the name-is-an-email rename slice over *pages* (athenaeum#745).

    Shared by the single-page and bulk drivers so the slice is reachable from
    every target selector. Before athenaeum#745 it ran only under ``--all``, which
    meant a rename could not be applied without also accepting whatever the
    corpus-wide body migration would do in the same run.
    """
    report = bulk_rename_name_email_pages(
        wiki_root,
        config,
        knowledge_root,
        apply=args.apply,
        pages=pages,
        display_name_override=getattr(args, "rename_to", None),
    )
    mode = "renamed" if args.apply else "[DRY RUN] would rename"
    print(
        f"{mode} {report.renamed} name-is-an-email page(s) "
        f"of {report.scanned} scanned "
        f"({report.links_rewritten} inbound link(s) rewritten)."
    )
    if report.residual:
        print(
            f"NOTE: {report.residual} page(s) have an ambiguous local-part "
            "(role address, +tag, initial-blob, or numeric/opaque) and were "
            "left unrenamed — manual naming required, per issue athenaeum#505's "
            "fallback (never guess)."
        )
        if getattr(args, "list_deferred", False):
            print("\n--- deferred: manual naming required ---")
            for page_path, reason in report.deferred:
                print(f"  {page_path.name}\t{reason}")
    if not args.apply and report.renamed:
        print("re-run with --apply to write the renames.")
    return report


def _cmd_storage_migrate_pii(args: argparse.Namespace) -> int:
    # athenaeum#745: --rename-only is the rename slice on its own. It implies
    # --rename-name-email so the two flags cannot disagree.
    if getattr(args, "rename_only", False):
        args.rename_name_email = True
    if getattr(args, "rename_to", None):
        # An operator-supplied name names exactly ONE page; applying it across a
        # bulk target set would stamp the same name onto every match.
        if args.page is None:
            print("error: --rename-to requires --page.", file=sys.stderr)
            return 2
        args.rename_name_email = True
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

    # athenaeum#745: the rename slice, scoped to this one page. Runs BEFORE the body
    # migration because a rename moves the file — planning a migration against a
    # path the rename is about to invalidate would be reading a stale target.
    renamed_this_run = False
    if getattr(args, "rename_name_email", False):
        wiki_root = knowledge_root / "wiki"
        rename_report = _run_rename_slice(
            args, wiki_root, config, knowledge_root, [page_path]
        )
        if getattr(args, "rename_only", False):
            if args.apply and rename_report.renamed:
                _post_apply_index_step(args, knowledge_root, config)
            return 0
        if args.apply and rename_report.renamed:
            # The page MOVED. Retarget the body migration at its new path
            # rather than skipping it: --rename-name-email without
            # --rename-only asks for both operations, and the rename only
            # clears the name: field — body-text contact data on the same page
            # is a separate finding and would otherwise be silently left
            # behind (caught in review of athenaeum#745).
            renamed_this_run = True
            page_path = wiki_root / f"{rename_report.renames[-1][1]}.md"

    plan = plan_pii_migration(page_path, config, knowledge_root)

    if not plan.changed:
        print(f"no archival contact data (emails/phones) found in {page_path}; nothing to migrate.")
        if renamed_this_run:
            # The rename still wrote; its index step is owed regardless of
            # whether the body pass found anything.
            _post_apply_index_step(args, knowledge_root, config)
        return 0

    # athenaeum#1108: a record already at plan.excluded_page_path means this run
    # MERGES rather than creates — label the preview/summary accordingly so
    # dry-run review reflects what --apply will actually do (AC3).
    record_label = "merged into existing" if plan.excluded_record_existed else "new"
    summary = (
        f"emails={plan.emails or '[]'} phones={plan.phones or '[]'}\n"
        f"  origin page (rewritten, durable identifiers only): {plan.page_path}\n"
        f"  excluded contact record ({record_label}):                     {plan.excluded_page_path}"
    )

    if not args.apply:
        print(f"[DRY RUN] would migrate PII off {page_path}:", file=sys.stderr)
        if plan.excluded_record_conflicts:
            _print_excluded_record_conflicts(plan, refusing=False)
        print(summary)
        print("\n--- rewritten origin page ---")
        sys.stdout.write(plan.rewritten_page_text or "")
        print(f"\n--- {record_label} excluded contact record ---")
        sys.stdout.write(plan.excluded_page_text or "")
        _scrub_merge_sidecar(knowledge_root, plan.emails + plan.phones, apply=False)
        return 0

    if plan.excluded_record_conflicts:
        _print_excluded_record_conflicts(plan, refusing=True)
        return 1

    _apply_plan(plan)
    print(f"migrated PII off {page_path}\n{summary}")
    _scrub_merge_sidecar(knowledge_root, plan.emails + plan.phones, apply=True)
    _post_apply_index_step(args, knowledge_root, config)
    return 0


def _resolve_bulk_pages(args: argparse.Namespace, wiki_root: Path) -> list[Path]:
    """Resolve the target page set for --all / --glob (materialized for a total)."""
    if args.glob is not None:
        return list(iter_glob_pages(wiki_root, args.glob))
    return list(iter_entity_pages(wiki_root))


def _cmd_storage_migrate_pii_bulk(args: argparse.Namespace) -> int:
    """Bulk migrate every targeted page's PII off-corpus (issue athenaeum#495).

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

    # athenaeum#745: --rename-only runs the rename slice over the SAME target set and
    # skips the body-text migration entirely. This is the whole point of the
    # flag: while the phone axis still carries detector false positives, a full
    # body migration would redact real prose (the athenaeum#691 failure mode), so the
    # rename must be applicable without it.
    if getattr(args, "rename_only", False):
        report = _run_rename_slice(args, wiki_root, config, knowledge_root, pages)
        if args.apply and report.renamed:
            _post_apply_index_step(args, knowledge_root, config)
        return 0

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
    conflicted = 0
    conflicted_pages: list[tuple[Path, tuple]] = []
    # athenaeum#1276: every value this run moves off-corpus, accumulated so the
    # merge sidecar is scrubbed ONCE after the sweep rather than re-read and
    # rewritten per page (a 741-block file over an 11.5k-page scan).
    migrated_values: list[str] = []
    for i, page_path in enumerate(pages, start=1):
        try:
            plan = plan_pii_migration(page_path, config, knowledge_root)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[migrate-pii] skip {page_path}: {exc}", file=sys.stderr)
            continue
        if plan.name_field_pii:
            name_pii_excluded += 1
        if plan.changed:
            if plan.excluded_record_conflicts:
                # athenaeum#1108: the bulk path shares plan_pii_migration/_apply_plan
                # with the single-page path, so it shares the same merge logic —
                # and the same refusal to silently resolve a scalar-identity
                # disagreement. Unlike the single-page CLI this must not abort
                # the whole run (bulk is the unattended path): skip only this
                # page (neither write touches disk), count it, and keep going.
                conflicted += 1
                conflicted_pages.append((page_path, plan.excluded_record_conflicts))
            else:
                affected += 1
                records += 1
                total_emails += len(plan.emails)
                total_phones += len(plan.phones)
                migrated_values.extend(plan.emails)
                migrated_values.extend(plan.phones)
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
    _scrub_merge_sidecar(knowledge_root, migrated_values, apply=args.apply)
    if name_pii_excluded and not getattr(args, "rename_name_email", False):
        # The name-is-an-email population (athenaeum#502): EXCLUDED from this automatic
        # path (renaming breaks slugs/edges) and handled in a separate slice
        # (issue athenaeum#505, --rename-name-email below). Surface it so it is
        # visible, not silently dropped, when that slice was NOT requested.
        print(
            f"NOTE: {name_pii_excluded} page(s) are named after an email "
            "address (name:/preferred_name:) and were NOT migrated — renaming "
            "is unsafe by default; re-run with --rename-name-email to migrate "
            "this population too (issue athenaeum#505)."
        )

    if conflicted:
        # Surfaced, never silently resolved (issue athenaeum#1108, AC1): these pages
        # were NOT migrated — neither the excluded record nor the origin page
        # was touched — because the record already on disk disagrees with this
        # run on a scalar identity field. A human must reconcile, then re-run
        # (the bulk path is resumable by construction, so a re-run only
        # revisits pages still carrying contact data).
        print(
            f"NOTE: {conflicted} page(s) already have an excluded contact "
            "record that disagrees with this run on a scalar identity field "
            "(uid/name/contact_of) and were NOT migrated — resolve by hand, "
            "then re-run:"
        )
        for page_path, conflicts in conflicted_pages:
            fields = ", ".join(c.field for c in conflicts)
            print(f"  {page_path.name}: conflicting field(s): {fields}")

    rename_report: NameEmailRenameReport | None = None
    if getattr(args, "rename_name_email", False):
        # athenaeum#505's name-is-an-email carve-out, scoped to the same target set as
        # the body migration above (athenaeum#745 — previously this was skipped
        # entirely under --glob and always ran corpus-wide under --all).
        rename_report = _run_rename_slice(args, wiki_root, config, knowledge_root, pages)

    if not args.apply and affected:
        print("re-run with --apply to write the changes.")
    if args.apply and (affected or (rename_report and rename_report.renamed)):
        _post_apply_index_step(args, knowledge_root, config)
    return 1 if conflicted else 0


def _cmd_storage_lint_pii(args: argparse.Namespace) -> int:
    """Corpus-wide PII gate (issue athenaeum#495): non-zero exit on any inline finding.

    Scans EVERY file under ``wiki/`` — not only entity pages, so ``_``-prefixed
    queue/index/archive files and stray ``.bak`` files are covered — for an
    inline email/phone token. Exits :data:`EXIT_PII_FOUND` (2) when any is
    found so a body-text email cannot silently regrow after the sweep, ``0``
    when the corpus is clean.

    athenaeum#936 adds adjudication (unblocking athenaeum#437): a finding whose
    value carries a reasoned entry in the allowlist is reported as adjudicated
    residue and does NOT fail the gate, while an unexplained finding fails
    exactly as before — a value is never tolerated by OMISSION. The allowlist
    is excluded from its own scan, without which exit 0 is unreachable (the
    artifact is by construction a list of verbatim contact values).

    athenaeum#1049 additionally scans ``raw/`` (a SIBLING of ``wiki/``, never a
    descendant — this command never opened it before) and reports it as a
    SEPARATE surface, using the same detectors as the wiki scan. Raw findings
    do NOT affect the exit code: raw intake is append-only by contract
    elsewhere in this codebase (the sweep's stuck-file/quarantine ledgers and
    Tier 3's partial-progress contract both depend on that holding), so an
    original value sitting in ``raw/`` is today's normal, unavoidable state —
    not a regression this gate could ever clear. Folding it into the existing
    gate would make ``lint-pii`` fail permanently with no fix in scope, and
    would make a clean wiki look dirty, destroying the existing gate's
    meaning (`docs/sensitivity-value-routing.md` §5). The raw count exists so
    an operator — and this epic's definition of done — can CITE raw
    retention instead of it going unmeasured; it is reporting, not mutation,
    and carries no allowlist/adjudication of its own.

    issue athenaeum#1273 additionally excludes known machine-generated audit
    logs (default: :data:`athenaeum.pii.DEFAULT_PII_SCAN_EXCLUDE_FILENAMES`,
    operator-extendable via ``storage.pii_scan_exclude`` —
    :func:`athenaeum.config.resolve_pii_scan_exclude`) from BOTH the wiki and
    raw scans, by filename. Unlike the allowlist, this is not adjudication:
    ``_shape_rule_dispositions.jsonl`` regenerates nightly with fresh
    timestamps that a phone-shaped detector misreads by the hundred
    thousand, so no allowlist entry could ever stay valid, and running the
    real detectors over it made the command itself unusable (two runs killed
    at 68 and 106 minutes). Every excluded path is printed to stderr (and
    listed under the JSON payload's ``excluded`` key) — a silent skip inside
    a PII scanner is its own hazard.
    """
    knowledge_root = _resolve_knowledge_root(args)
    wiki_root = knowledge_root / "wiki"
    raw_root = knowledge_root / "raw"
    config = load_config(knowledge_root)
    allowlist_path = getattr(args, "allowlist", None) or (
        wiki_root / PII_ALLOWLIST_FILENAME
    )
    entries, errors = load_pii_allowlist(allowlist_path)
    # Machine-generated audit logs (issue athenaeum#1273): a filename-only
    # exclusion for logs like _shape_rule_dispositions.jsonl that regenerate
    # nightly under a stable name but unstable content, so no allowlist entry
    # could ever absorb their findings. Reported below rather than skipped
    # silently.
    scan_exclude_names = resolve_pii_scan_exclude_filenames(config)
    excluded_wiki_paths = scan_excluded_by_name(wiki_root, scan_exclude_names)
    excluded_raw_paths = scan_excluded_by_name(raw_root, scan_exclude_names)
    for excluded_path in excluded_wiki_paths + excluded_raw_paths:
        print(
            f"excluded from PII scan (machine-generated audit log, issue "
            f"athenaeum#1273): {excluded_path}",
            file=sys.stderr,
        )
    # Self-exclusion: scanning the allowlist would make every adjudicated value
    # a fresh finding and put exit 0 permanently out of reach.
    findings = scan_corpus_pii(
        wiki_root, exclude=[allowlist_path], exclude_names=scan_exclude_names
    )
    result = adjudicate_corpus_pii(findings, entries, errors=errors)
    # raw/ is scanned with the same self-exclusion (defensive: the allowlist
    # is conventionally under wiki/, but an operator-supplied --allowlist
    # could in principle point elsewhere) and NO adjudication — it is a raw
    # count, not a second gate.
    raw_findings = scan_corpus_pii(
        raw_root, exclude=[allowlist_path], exclude_names=scan_exclude_names
    )

    for err in result.errors:
        print(f"warning: allowlist entry ignored -- {err}", file=sys.stderr)
    for entry in result.stale:
        print(
            f"warning: stale allowlist entry (matches nothing in the corpus): "
            f"{entry.value!r} -- {entry.reason}",
            file=sys.stderr,
        )

    if args.json:
        import json

        wiki_payload = [
            {
                "path": str(f.path),
                # Back-compat: `emails`/`phones` remain the UNEXPLAINED tokens,
                # i.e. exactly what the gate fails on, as before athenaeum#936.
                "emails": f.unexplained_emails,
                "phones": f.unexplained_phones,
                "allowlisted": f.allowlisted,
                "adjudicated": f.is_adjudicated,
            }
            for f in result.findings
        ]
        raw_payload = [
            {"path": str(f.path), "emails": f.emails, "phones": f.phones}
            for f in raw_findings
        ]
        # athenaeum#1049: the top-level shape changes from a bare list to a dict
        # with "wiki" (the pre-existing payload, unchanged) and "raw" (new) so
        # the two surfaces stay distinguishable rather than summed. No known
        # consumer besides this repo's own test suite depends on the prior
        # bare-list shape (grepped at filing time).
        payload = {
            "wiki": wiki_payload,
            "raw": raw_payload,
            # issue athenaeum#1273: machine-generated audit logs skipped by
            # filename, reported rather than silently dropped.
            "excluded": [str(p) for p in excluded_wiki_paths + excluded_raw_paths],
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0 if result.is_clean else EXIT_PII_FOUND

    # With nothing adjudicated the output is verbatim what it was before
    # athenaeum#936 ("a missing allowlist means behaviour is unchanged from
    # today"); the two-population wording appears only once there IS residue to
    # distinguish, so the common case reads no differently for its operators.
    adjudicated = result.adjudicated_count
    qualifier = "unexplained " if adjudicated else ""
    residue = f" ({adjudicated} adjudicated residue)" if adjudicated else ""

    if result.is_clean:
        print(f"0 {qualifier}inline PII findings under {wiki_root}{residue}")
        wiki_rc = 0
    else:
        unexplained_files = [f for f in result.findings if not f.is_adjudicated]
        print(
            f"{result.unexplained_count} {qualifier}inline PII finding(s) in "
            f"{len(unexplained_files)} file(s) under {wiki_root}{residue}:"
        )
        for f in unexplained_files:
            parts: list[str] = []
            if f.unexplained_emails:
                parts.append(f"emails={f.unexplained_emails}")
            if f.unexplained_phones:
                parts.append(f"phones={f.unexplained_phones}")
            print(f"  {f.path}: {'; '.join(parts)}")
        wiki_rc = EXIT_PII_FOUND

    # athenaeum#1049: raw/ is reported unconditionally, informational only —
    # never gates the exit code (see the docstring for why).
    raw_count = sum(len(f.emails) + len(f.phones) for f in raw_findings)
    if raw_findings:
        print(
            f"{raw_count} inline PII finding(s) in {len(raw_findings)} file(s) "
            f"under {raw_root} (raw retention -- informational only, not "
            "gated; athenaeum#1049):"
        )
        for raw_finding in raw_findings:
            raw_parts: list[str] = []
            if raw_finding.emails:
                raw_parts.append(f"emails={raw_finding.emails}")
            if raw_finding.phones:
                raw_parts.append(f"phones={raw_finding.phones}")
            print(f"  {raw_finding.path}: {'; '.join(raw_parts)}")
    else:
        print(
            f"0 inline PII findings under {raw_root} (raw retention -- "
            "informational only, not gated; athenaeum#1049)"
        )

    return wiki_rc


def _cmd_storage_lint_mapping(args: argparse.Namespace) -> int:
    """CLI wrapper for the athenaeum#993 lint (S5 of the sensitivity-class design note).

    All check logic lives in :mod:`athenaeum.sensitivity_lint` — this
    function only resolves inputs, calls
    :func:`~athenaeum.sensitivity_lint.lint_sensitivity_storage_mapping`, and
    prints/exits, mirroring :func:`_cmd_storage_lint_pii`'s shape.

    Exit code reflects ONLY the completeness findings
    (:data:`EXIT_MAPPING_ISSUES` when any exist, ``0`` otherwise) — a D4
    policy-mismatch finding is always reported but never changes the exit
    code, per :attr:`~athenaeum.sensitivity_lint.SensitivityMappingLintResult.is_clean`.
    """
    knowledge_root = _resolve_knowledge_root(args)
    corpus_root = getattr(args, "corpus", None) or knowledge_root
    config = load_config(knowledge_root)

    result: SensitivityMappingLintResult = lint_sensitivity_storage_mapping(
        config, corpus_root
    )

    if args.json:
        import json

        payload = {
            "completeness": [
                {
                    "kind": f.kind,
                    "class_name": f.class_name,
                    "detail": f.detail,
                    "paths": [str(p) for p in f.paths],
                }
                for f in result.completeness
            ],
            "policy": [
                {
                    "kind": f.kind,
                    "class_name": f.class_name,
                    "detail": f.detail,
                }
                for f in result.policy
            ],
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0 if result.is_clean else EXIT_MAPPING_ISSUES

    if result.is_clean:
        print(f"0 storage.mapping completeness finding(s) under {corpus_root}")
    else:
        print(
            f"{len(result.completeness)} storage.mapping completeness "
            f"finding(s) under {corpus_root}:"
        )
        for f in result.completeness:
            print(f"  [{f.kind}] {f.detail}")

    if result.policy:
        print(
            f"{len(result.policy)} advisory (read_policy, adapter) pair "
            "mismatch finding(s) — does not fail this gate:"
        )
        for f in result.policy:
            print(f"  [{f.kind}] {f.detail}")

    return 0 if result.is_clean else EXIT_MAPPING_ISSUES


def _human_bytes(n: int) -> str:
    """Render *n* bytes as a short human-readable size (binary units).

    Local, tiny, and deliberately not shared with any other module — this
    command's report is the only caller, and pulling in a general-purpose
    formatter for one call site would be more machinery than the job needs.
    """
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{n} B"
        size /= 1024
    return f"{n} B"  # pragma: no cover - unreachable, loop always returns


def _cmd_storage_prune_dispositions(args: argparse.Namespace) -> int:
    """``athenaeum storage prune-dispositions`` (issue athenaeum#1274 AC3/AC4).

    Dry-run by default: reports the disposition histogram, positive-record
    count, and projected post-prune size with no write. ``--apply`` writes
    atomically via :func:`athenaeum.rules.prune_shape_rule_dispositions_to_positive`,
    guarded by the single-machine run lock (issue athenaeum#309) — the same
    guard every other mutating ``storage``/librarian command acquires, so a
    prune can never race the nightly's own concurrent appends to this same
    file (the exact hazard athenaeum#1274's own proposal names).

    Exit codes:
        0 - dry-run report printed, or ``--apply`` succeeded (including the
            no-op case where there was nothing to prune).
        1 - ledger missing, or the prune's own count-mismatch guard fired
            (:class:`athenaeum.rules.DispositionPruneMismatchError` —
            nothing was written).
        75 - the run lock is held by another process (``--apply`` only;
            :data:`athenaeum._cli_shared.EXIT_LOCK_HELD`).
    """
    knowledge_root = _resolve_knowledge_root(args)
    wiki_root = knowledge_root / "wiki"
    path = default_shape_rule_dispositions_path(wiki_root)
    if not path.is_file():
        print(f"no disposition ledger found at {path}; nothing to prune.")
        return 0

    mode = "APPLY" if args.apply else "DRY RUN"
    lock: "RunLock | None" = None
    if args.apply:
        cfg = load_config(knowledge_root)
        acquired = _acquire_or_exit(knowledge_root, args, cfg)
        if isinstance(acquired, int):
            return acquired
        lock = acquired
    try:
        try:
            report = prune_shape_rule_dispositions_to_positive(wiki_root, apply=args.apply)
        except DispositionPruneMismatchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        print(f"=== disposition prune ({mode}) ===")
        print(f"  ledger:            {path}")
        print(f"  total records:     {report.total_records}")
        print(
            f"  no-match:          {report.no_match_count} "
            f"({_pct(report.no_match_count, report.total_records)})"
        )
        print(
            f"  positive records:  {report.positive_count} "
            f"({_pct(report.positive_count, report.total_records)})"
        )
        for disposition in sorted(report.histogram):
            print(f"    {disposition}: {report.histogram[disposition]}")
        if report.malformed_lines:
            print(f"    <malformed, kept>: {report.malformed_lines}")
        print(
            f"  current size:      {report.current_bytes} B "
            f"({_human_bytes(report.current_bytes)})"
        )
        size_label = ("new size" if report.applied else "projected size") + ":"
        print(
            f"  {size_label.ljust(19)}{report.projected_bytes} B "
            f"({_human_bytes(report.projected_bytes)})"
        )
        print(f"  rows dropped:      {report.rows_dropped}")

        if not args.apply:
            if report.rows_dropped:
                print(f"\n  [DRY RUN] would drop {report.rows_dropped} no-match row(s).")
                print("  Re-run with --apply to write.")
            else:
                print("\n  [DRY RUN] nothing to prune.")
            return 0

        if report.applied:
            print(f"\n  pruned {report.rows_dropped} no-match row(s); ledger rewritten.")
        else:
            print("\n  nothing to prune; ledger left unchanged.")
        return 0
    finally:
        if lock is not None:
            lock.release()


def _pct(part: int, whole: int) -> str:
    if whole == 0:
        return "0.0%"
    return f"{100.0 * part / whole:.1f}%"
