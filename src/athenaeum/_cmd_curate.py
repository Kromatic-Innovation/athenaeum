# SPDX-License-Identifier: Apache-2.0
"""``athenaeum {dedupe,claims}`` — entity/content curation commands.

Two subcommands grouped here because each finds-or-fixes duplication/noise
in the compiled wiki: person/wiki-page dedupe (``dedupe persons`` /
``dedupe wiki-pages``) and cross-entity recurring-claim detection (``claims``).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` (or a small same-domain group module like this
one) and registers via ``add_<name>_subparser`` — this is where a NEW
subcommand goes, not inline in ``cli.py``'s ``main()``. This module may
import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry points, kept lazy/local to keep top-level import
cost down.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def add_curate_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Register ``dedupe``, ``claims``."""

    # dedupe command — find / merge duplicate person wikis
    dedupe_parser = subparsers.add_parser(
        "dedupe",
        help="Find or merge duplicate wiki entries.",
    )
    dedupe_parser.set_defaults(func=cmd_dedupe)
    dedupe_sub = dedupe_parser.add_subparsers(dest="dedupe_target")
    dedupe_persons = dedupe_sub.add_parser(
        "persons",
        help="Person-wiki dedupe (HIGH-confidence apollo_id / linkedin / "
        "exact-name match). Default --find prints a YAML report; "
        "--apply consumes the report and merges.",
    )
    dedupe_persons.add_argument(
        "--find",
        action="store_true",
        help="Discover duplicate pairs and write a YAML report.",
    )
    dedupe_persons.add_argument(
        "--apply",
        action="store_true",
        help="Read a report and perform the merge (idempotent).",
    )
    dedupe_persons.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="Wiki directory (default: ~/knowledge/wiki).",
    )
    dedupe_persons.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Path to write the YAML report (default: stdout). --find only.",
    )
    dedupe_persons.add_argument(
        "--from",
        dest="from_path",
        type=Path,
        default=None,
        help="Path to the YAML report to apply (default: stdin). --apply only.",
    )
    _add_lock_args(dedupe_persons)
    dedupe_persons.set_defaults(func=cmd_dedupe)

    # dedupe wiki-pages — cluster compiled concept/reference/principle
    # wiki pages against EACH OTHER (issue athenaeum#290) and propose merges via
    # the shared wiki/_pending_merges.md sidecar for human approval.
    # Unlike `dedupe persons`, there is no --apply step here: the only
    # side effect is an idempotent proposal append, never a direct merge.
    dedupe_wiki_pages = dedupe_sub.add_parser(
        "wiki-pages",
        help="Cluster concept/reference/principle wiki pages and propose "
        "merges for near-duplicate topics (issue athenaeum#290). Writes idempotent "
        "proposals to wiki/_pending_merges.md; --dry-run previews without "
        "writing.",
    )
    dedupe_wiki_pages.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    dedupe_wiki_pages.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be proposed without writing to "
        "wiki/_pending_merges.md.",
    )
    dedupe_wiki_pages.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Cosine similarity cutoff (default: librarian.cluster_threshold "
        "/ 0.55 — same threshold the raw auto-memory cluster pass uses).",
    )
    _add_lock_args(dedupe_wiki_pages)
    dedupe_wiki_pages.set_defaults(func=cmd_dedupe)

    # claims command — cross-entity recurring-claim detector (issue athenaeum#272,
    # slice 1 of athenaeum#258). READ-ONLY: scans the wiki, embeds claim texts via the
    # recall-index provider, and prints a YAML report of claims restated across
    # distinct entities. Mutates nothing under wiki/.
    claims_parser = subparsers.add_parser(
        "claims",
        help="Detect claims restated across distinct wiki entities (read-only). "
        "Default --find prints a YAML report.",
    )
    claims_parser.add_argument(
        "--find",
        action="store_true",
        help="Discover recurring claims and print a YAML report.",
    )
    claims_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    claims_parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Cosine similarity cutoff (default: 0.85)",
    )
    claims_parser.set_defaults(func=cmd_claims)


def cmd_dedupe(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum dedupe persons --find|--apply`` / ``dedupe wiki-pages``."""
    target = getattr(args, "dedupe_target", None)

    if target == "wiki-pages":
        return _cmd_dedupe_wiki_pages(args)

    from athenaeum.dedupe import (
        find_duplicate_persons,
        merge_duplicate_persons,
        pairs_from_yaml,
        pairs_to_yaml,
    )

    if target != "persons":
        print(
            "usage: athenaeum dedupe persons [--find | --apply] ... "
            "| athenaeum dedupe wiki-pages [--dry-run] ...",
            file=sys.stderr,
        )
        return 2

    wiki_root = (args.wiki_root or Path("~/knowledge/wiki")).expanduser().resolve()

    if args.find and args.apply:
        print("error: pass either --find or --apply, not both", file=sys.stderr)
        return 2
    if not args.find and not args.apply:
        print("error: pass --find or --apply", file=sys.stderr)
        return 2

    if args.find:
        if not wiki_root.is_dir():
            print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
            return 1
        from athenaeum.config import load_config, resolve_owner

        owner = resolve_owner(load_config(wiki_root.parent))
        pairs = find_duplicate_persons(wiki_root, owner=owner)
        report = pairs_to_yaml(pairs)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(report, encoding="utf-8")
            print(f"Wrote {len(pairs)} pair(s) → {args.out}", file=sys.stderr)
        else:
            sys.stdout.write(report)
        return 0

    # --apply (mutating): acquire the single-machine run lock (issue athenaeum#309).
    if args.from_path:
        text = args.from_path.read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    pairs = pairs_from_yaml(text)
    from athenaeum.config import (
        load_config,
        resolve_google_contact_keys,
        resolve_owner,
    )

    cfg = load_config(wiki_root.parent)
    lock = _acquire_or_exit(wiki_root.parent, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        gc_keys = resolve_google_contact_keys(cfg)
        # Owner is resolved on the APPLY path too, not just --find: it arms
        # the owner-address guard in the merge (issue athenaeum#1739). Without it
        # the guard exists but nothing reaches it from the CLI.
        owner = resolve_owner(cfg)
        from athenaeum.contact_record_fold import resolve_contacts_root

        merge_report = merge_duplicate_persons(
            pairs,
            apply=True,
            wiki_root=wiki_root,
            google_contact_keys=gc_keys,
            owner=owner,
            # Issue athenaeum#2184: fold each absorbed uid's contact record.
            # None (no fold) when `pii` is not mapped to an excluded surface.
            contacts_root=resolve_contacts_root(wiki_root.parent, cfg),
        )
    finally:
        lock.release()
    print(
        f"merged={merge_report.merged} "
        f"already_merged={merge_report.already_merged} "
        f"missing_canonical={merge_report.missing_canonical} "
        f"skipped_parse={merge_report.skipped_parse} "
        f"references_rewritten={merge_report.references_rewritten} "
        f"contact_records_folded={merge_report.contact_records_folded} "
        f"contact_values_added={merge_report.contact_values_added} "
        f"errors={len(merge_report.errors)}"
    )
    # Key and count only — the dropped value is the operator's own address.
    for key, count in sorted(merge_report.owner_addresses_dropped.items()):
        print(f"owner_addresses_dropped[{key}]={count}")
    for err in merge_report.errors:
        print(f"  ERROR: {err}", file=sys.stderr)
    return 0 if not merge_report.errors else 1


def _cmd_dedupe_wiki_pages(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum dedupe wiki-pages`` (issues athenaeum#290, athenaeum#715).

    Clusters concept/reference/principle wiki pages; every candidate pair is
    then decided by the five-verdict comparator and its verdict enacted (a
    ``duplicate`` verdict writes fold EVIDENCE — never a merged body, never
    ``wiki/_pending_merges.md``; see ``athenaeum.verdict_effects``). Since
    athenaeum#715's cut-over this command's own OLD confidence/suppression-gate
    algorithm no longer exists, so it refuses to run (rather than silently
    doing nothing) when the comparator subsystem is disabled — the same
    posture ``athenaeum decisions recompare`` already takes.
    """
    from athenaeum.config import load_config, resolve_comparator_enabled
    from athenaeum.wiki_dedupe import propose_wiki_page_merges

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    config = load_config(knowledge_root)
    if not resolve_comparator_enabled(config):
        print(
            "The five-verdict comparator is disabled. Enable it with "
            "ATHENAEUM_COMPARATOR_ENABLED=1 or librarian.comparator_enabled: true "
            "before running this command (issue athenaeum#715) — the old "
            "detection algorithm this command used to run has been retired.",
            file=sys.stderr,
        )
        return 2

    from athenaeum.provider import ProviderConfigError, build_llm_client

    client = None
    try:
        client = build_llm_client(config=config, knob="classify")
    except ProviderConfigError as exc:
        print(f"Provider misconfigured: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — offline degrade, mirrors `_cmd_recompare`
        print(
            f"No LLM client ({exc}); Gate 2 unavailable — pairs Gate 1 cannot "
            "settle will report no verdict.",
            file=sys.stderr,
        )

    # Issue athenaeum#309: --dry-run writes nothing, so it does NOT take the lock. A
    # real run ledgers verdicts and enacts effects → locked.
    lock: RunLock | int | None = None
    if not args.dry_run:
        lock = _acquire_or_exit(knowledge_root, args, config)
        if isinstance(lock, int):
            return lock
    try:
        results = propose_wiki_page_merges(
            knowledge_root,
            config=config,
            threshold=args.threshold,
            dry_run=args.dry_run,
            client=client,
            lock=None if isinstance(lock, int) else lock,
        )
    finally:
        if lock is not None and not isinstance(lock, int):
            lock.release()

    if args.dry_run:
        print(f"[DRY RUN] would decide {len(results)} pair(s):")
    else:
        print(f"Decided {len(results)} pair(s) (fresh/memoized pairs are skipped):")
    for r in results:
        action = r.get("action", "preview")
        print(f"  - {r['pair']}: verdict={r['verdict']} action={action}")
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum claims --find`` (issue athenaeum#272). READ-ONLY.

    Scans the configured wiki, embeds claim texts via the recall-index
    embedding provider, and prints a YAML report of claims restated across
    distinct entities. Degrades gracefully to an empty report when no
    embedding backend is available.
    """
    from athenaeum.recurring_claims import (
        DEFAULT_THRESHOLD,
        extract_claim_occurrences,
        group_recurring_claims,
        render_report,
    )
    from athenaeum.search import embed_texts

    if not args.find:
        print("usage: athenaeum claims --find ...", file=sys.stderr)
        return 2

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    threshold = args.threshold if args.threshold is not None else DEFAULT_THRESHOLD
    # Scan the wiki ONCE: reuse the occurrence list for both the entity count
    # and the grouping pass instead of re-walking the tree (C6).
    occurrences = extract_claim_occurrences(wiki_root)
    entities_scanned = len({o.entity_id for o in occurrences})
    groups = group_recurring_claims(
        occurrences, threshold=threshold, embedding_provider=embed_texts
    )
    sys.stdout.write(
        render_report(groups, threshold=threshold, entities_scanned=entities_scanned)
    )
    return 0
