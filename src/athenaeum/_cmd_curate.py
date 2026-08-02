# SPDX-License-Identifier: Apache-2.0
"""``athenaeum {dedupe,claims,auto-memory}`` — entity/content curation commands.

Three subcommands grouped here because each finds-or-fixes duplication/noise
in the compiled wiki: person/wiki-page dedupe (``dedupe persons`` /
``dedupe wiki-pages``), cross-entity recurring-claim detection (``claims``),
and operational/ephemeral auto-memory pruning plus dangling-pointer cleanup
(``auto-memory prune`` / ``auto-memory prune-index``).

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
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, resolve_cache_dir

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def add_curate_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Register ``dedupe``, ``claims``, ``auto-memory``."""

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

    # auto-memory command — operate on compiled wiki/auto-*.md pages.
    # `prune` (issue athenaeum#278) builds a kill-list of operational/ephemeral
    # auto-memory pages via the same classifier the intake gate uses and,
    # on --apply, git rm's them in one labeled commit + rebuilds the recall
    # index. Default is dry-run (prints kill + retained lists).
    auto_memory_parser = subparsers.add_parser(
        "auto-memory",
        help="Operate on compiled wiki/auto-*.md pages (issue athenaeum#278).",
    )
    auto_memory_parser.set_defaults(func=cmd_auto_memory)
    auto_memory_sub = auto_memory_parser.add_subparsers(dest="auto_memory_target")
    prune_parser = auto_memory_sub.add_parser(
        "prune",
        help="Prune operational/ephemeral wiki/auto-*.md pages. Default is "
        "dry-run (prints kill-list + retained-list with reasons); --apply "
        "git rm's the kill-list in one commit and rebuilds the recall index.",
    )
    prune_parser.add_argument(
        "--apply",
        action="store_true",
        help="git rm the kill-list in one labeled commit and rebuild the "
        "recall index. Without this flag the command is a dry-run.",
    )
    prune_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    prune_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for the recall index rebuild "
        "(default: ~/.cache/athenaeum). --apply only.",
    )
    prune_parser.add_argument(
        "--backend",
        choices=["fts5", "vector"],
        default=None,
        help="Override the recall index backend for the rebuild "
        "(default: read from athenaeum.yaml). --apply only.",
    )
    _add_lock_args(prune_parser)
    prune_parser.set_defaults(func=cmd_auto_memory)

    # prune-index (issue athenaeum#388): one-shot backfill that drops dangling
    # <scope>/MEMORY.md pointers left by pre-athenaeum#388 move-then-retire runs. The
    # inline fix in retire.py prevents NEW dangling pointers; this sweeps the
    # ones already on disk. Default is dry-run; --apply rewrites + commits.
    prune_index_parser = auto_memory_sub.add_parser(
        "prune-index",
        help="Prune dangling pointers from <scope>/MEMORY.md indexes (issue "
        "athenaeum#388). A pointer is dangling when its target .md no longer exists on "
        "disk. Default is dry-run; --apply rewrites the indexes in one commit.",
    )
    prune_index_parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite the affected MEMORY.md indexes in one labeled commit. "
        "Without this flag the command is a dry-run.",
    )
    prune_index_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(prune_index_parser)
    prune_index_parser.set_defaults(func=cmd_auto_memory)

    # prune-code-entities (issue athenaeum#680): retire wiki entity pages that were minted
    # from filenames / paths (``skill.md``, ``project-registry.yaml``). The
    # write-side gate stops NEW ones; this sweeps the ones already on disk, using
    # the SAME code-artifact predicate. Default is dry-run; --apply git rm's them.
    prune_code_parser = auto_memory_sub.add_parser(
        "prune-code-entities",
        help="Retire wiki entity pages minted from filenames/paths (issue "
        "athenaeum#680) — a page whose entity name is a code artifact (has a source/"
        "config extension or a path separator). Default is dry-run; --apply "
        "git rm's the kill-list in one commit.",
    )
    prune_code_parser.add_argument(
        "--apply",
        action="store_true",
        help="git rm the kill-list in one labeled commit. Without this flag "
        "the command is a dry-run.",
    )
    prune_code_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(prune_code_parser)
    prune_code_parser.set_defaults(func=cmd_auto_memory)


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
    from athenaeum.config import load_config, resolve_google_contact_keys

    cfg = load_config(wiki_root.parent)
    lock = _acquire_or_exit(wiki_root.parent, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        gc_keys = resolve_google_contact_keys(cfg)
        merge_report = merge_duplicate_persons(
            pairs, apply=True, wiki_root=wiki_root, google_contact_keys=gc_keys
        )
    finally:
        lock.release()
    print(
        f"merged={merge_report.merged} "
        f"already_merged={merge_report.already_merged} "
        f"missing_canonical={merge_report.missing_canonical} "
        f"skipped_parse={merge_report.skipped_parse} "
        f"references_rewritten={merge_report.references_rewritten} "
        f"errors={len(merge_report.errors)}"
    )
    for err in merge_report.errors:
        print(f"  ERROR: {err}", file=sys.stderr)
    return 0 if not merge_report.errors else 1


def _cmd_dedupe_wiki_pages(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum dedupe wiki-pages`` (issue athenaeum#290).

    Clusters concept/reference/principle wiki pages and proposes merges
    for near-duplicate topics via the shared
    ``wiki/_pending_merges.md`` sidecar. Default writes proposals
    (idempotent — a rerun is a no-op for source sets already proposed);
    ``--dry-run`` previews without writing.
    """
    from athenaeum.wiki_dedupe import propose_wiki_page_merges

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    # Issue athenaeum#309: --dry-run writes nothing, so it does NOT take the lock. The
    # proposal-append path (default) mutates wiki/_pending_merges.md → locked.
    lock: RunLock | int | None = None
    if not args.dry_run:
        from athenaeum.config import load_config

        lock = _acquire_or_exit(knowledge_root, args, load_config(knowledge_root))
        if isinstance(lock, int):
            return lock
    try:
        proposals = propose_wiki_page_merges(
            knowledge_root,
            threshold=args.threshold,
            dry_run=args.dry_run,
        )
    finally:
        if lock is not None and not isinstance(lock, int):
            lock.release()

    if args.dry_run:
        print(f"[DRY RUN] would propose {len(proposals)} merge(s):")
    else:
        print(f"Proposed {len(proposals)} new merge(s) (see wiki/_pending_merges.md):")
    for p in proposals:
        print(f"  - {p['merge_target_name']}: {len(p['sources'])} source(s)")
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


def cmd_auto_memory(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum auto-memory {prune,prune-index}`` (issues athenaeum#278/#388)."""
    target = getattr(args, "auto_memory_target", None)
    if target == "prune":
        return _cmd_auto_memory_prune(args)
    if target == "prune-index":
        return _cmd_auto_memory_prune_index(args)
    if target == "prune-code-entities":
        return _cmd_auto_memory_prune_code_entities(args)
    print(
        "usage: athenaeum auto-memory "
        "{prune,prune-index,prune-code-entities} [--apply] ...",
        file=sys.stderr,
    )
    return 2


def _cmd_auto_memory_prune(args: argparse.Namespace) -> int:
    """Prune operational/ephemeral ``wiki/auto-*.md`` pages (issue athenaeum#278).

    Exit codes (mirroring ``repair``):
        0 - clean run (nothing to prune, OR ``--apply`` succeeded with no
            errors).
        1 - errors encountered (apply without git, unreadable pages, ...).
        2 - dry-run found pages that WOULD be pruned (CI / sign-off signal).
    """
    from athenaeum.auto_memory_prune import apply_prune, build_prune_report
    from athenaeum.config import (
        load_config,
        resolve_ephemeral_scopes,
        resolve_operational_markers,
    )

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    ephemeral_scopes = resolve_ephemeral_scopes(cfg)
    operational_markers = resolve_operational_markers(cfg)

    report = build_prune_report(
        wiki_root,
        ephemeral_scopes=ephemeral_scopes,
        operational_markers=operational_markers,
    )

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== auto-memory prune ({mode}) ===")
    print(f"  scanned:  {report.scanned}")
    print(f"  kill:     {len(report.kill)}")
    print(f"  retained: {len(report.retained)}")

    if report.kill:
        print("\n  KILL-LIST:")
        for cand in report.kill:
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
        return 2 if report.kill else 0

    # --apply (mutating): acquire the single-machine run lock (issue athenaeum#309).
    # The dry-run path above returns before here and never takes the lock.
    lock = _acquire_or_exit(knowledge_root, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        report = apply_prune(knowledge_root, report)
        for err in report.errors:
            print(f"  ERR {err}", file=sys.stderr)
        if report.errors:
            return 1

        if report.committed:
            print(f"\n  pruned {len(report.kill)} page(s); committed.")
            _rebuild_recall_index(knowledge_root, cfg, args)
        else:
            print("\n  nothing pruned.")
        return 0
    finally:
        lock.release()


def _cmd_auto_memory_prune_index(args: argparse.Namespace) -> int:
    """Backfill: prune dangling ``<scope>/MEMORY.md`` pointers (issue athenaeum#388).

    Move-then-retire ``git rm``\\s a raw member but only rewrites the sibling
    index for members it retires going forward; this one-shot sweep removes the
    pointers already orphaned by earlier runs. A pointer is dangling when its
    bare ``<file>.md`` target no longer exists in the scope directory.

    Exit codes (mirroring ``auto-memory prune`` / ``repair``):
        0 - clean run (nothing dangling, OR ``--apply`` succeeded, no errors).
        1 - errors encountered (apply without git, unreadable index, ...).
        2 - dry-run found dangling pointers that WOULD be pruned.
    """
    from athenaeum.config import load_config, resolve_extra_intake_roots
    from athenaeum.memory_index import apply_prune_index, build_dangling_report

    knowledge_root = args.path.expanduser().resolve()
    if not knowledge_root.is_dir():
        print(f"Knowledge root not found: {knowledge_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    intake_roots = resolve_extra_intake_roots(knowledge_root, config=cfg)
    report = build_dangling_report(intake_roots)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== prune dangling MEMORY.md pointers ({mode}) ===")
    print(f"  indexes scanned: {report.scanned_indexes}")
    print(f"  scopes affected: {len(report.scopes)}")
    print(f"  dangling total:  {report.total_dangling}")

    if report.scopes:
        print("\n  DANGLING:")
        for scope in report.scopes:
            print(
                f"    {scope.index_path.parent.name}: "
                f"{len(scope.dangling)}/{scope.total_pointers} pointers"
            )
            for target in scope.dangling:
                print(f"      - {target}")

    if not args.apply:
        for err in report.errors:
            print(f"  ERR {err}", file=sys.stderr)
        if report.errors:
            return 1
        return 2 if report.scopes else 0

    # --apply (mutating): acquire the single-machine run lock (issue athenaeum#309).
    lock = _acquire_or_exit(knowledge_root, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        report = apply_prune_index(knowledge_root, report)
        for err in report.errors:
            print(f"  ERR {err}", file=sys.stderr)
        if report.errors:
            return 1
        if report.committed:
            print(
                f"\n  pruned {report.total_dangling} pointer(s) across "
                f"{len(report.scopes)} scope(s); committed."
            )
        else:
            print("\n  nothing pruned.")
        return 0
    finally:
        lock.release()


def _cmd_auto_memory_prune_code_entities(args: argparse.Namespace) -> int:
    """Retire wiki entity pages minted from filenames / paths (issue athenaeum#680).

    The creation gate (:func:`athenaeum.tiers.is_code_artifact_name`) stops NEW
    code-artifact entities; this one-shot sweep retires the ones already on disk
    using the SAME predicate, so the operator allowlist / toggle apply
    identically. Removal is via the existing ``git rm`` retire path (recoverable).

    Exit codes (mirroring ``auto-memory prune`` / ``prune-index``):
        0 - clean run (nothing to retire, OR ``--apply`` succeeded, no errors).
        1 - errors encountered (apply without git, unreadable page, ...).
        2 - dry-run found pages that WOULD be retired.
    """
    from athenaeum.config import load_config
    from athenaeum.filename_entity_prune import (
        apply_filename_entity_prune,
        build_filename_entity_report,
        kill_rule_counts,
    )

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    report = build_filename_entity_report(wiki_root, config=cfg)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== retire filename-derived entity pages ({mode}) ===")
    print(f"  scanned:  {report.scanned}")
    print(f"  kill:     {len(report.kill)}")
    print(f"  retained: {len(report.retained)}")

    if report.kill:
        # Per-rule split so an operator can audit the kill-list by class rather
        # than by eyeball (athenaeum#721). A bare path separator no longer kills, so this
        # is expected to be all-``extension``; any other rule flags a regression.
        counts = kill_rule_counts(report)
        split = ", ".join(f"{rule}={n}" for rule, n in sorted(counts.items()))
        print(f"  by rule:  {split}")
        print("\n  KILL-LIST:")
        for cand in report.kill:
            print(f"    [{cand.rule}] {cand.path.name}: {cand.reason}")

    if not args.apply:
        for err in report.errors:
            print(f"  ERR {err}", file=sys.stderr)
        if report.errors:
            return 1
        return 2 if report.kill else 0

    # --apply (mutating): acquire the single-machine run lock (issue athenaeum#309).
    lock = _acquire_or_exit(knowledge_root, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        report = apply_filename_entity_prune(knowledge_root, report)
        for err in report.errors:
            print(f"  ERR {err}", file=sys.stderr)
        if report.errors:
            return 1
        if report.committed:
            print(f"\n  retired {len(report.kill)} page(s); committed.")
            _rebuild_recall_index(knowledge_root, cfg, args)
        else:
            print("\n  nothing retired.")
        return 0
    finally:
        lock.release()


def _rebuild_recall_index(
    knowledge_root: Path,
    cfg: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    """Rebuild the recall index after a prune apply (issue athenaeum#278).

    Mirrors the ``reindex`` subcommand's backend resolution so the index
    reflects the removed pages. A rebuild failure is reported but never
    fails the prune (the git removal already committed).
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
    except Exception as exc:  # noqa: BLE001 - rebuild failure must not fail prune
        print(
            f"  WARN recall index rebuild failed ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
