# SPDX-License-Identifier: Apache-2.0
"""Athenaeum CLI entry point."""

import argparse
import logging
import os
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def _iso_date(value: str) -> date:
    """Argparse type for an ISO-8601 ``YYYY-MM-DD`` ``--as-of`` date (issue #308).

    Unlike the fail-open frontmatter date parse, an operator explicitly
    requesting an as-of view with a malformed date gets a loud parse error
    rather than a silent today-view.
    """
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid ISO date (expected YYYY-MM-DD): {value!r}"
        ) from None


def _positive_int(value: str) -> int:
    """Argparse type for flags that must be a strictly positive integer.

    Issue #220: a zero or negative ``--max-api-calls`` would defer the
    entire intake while exiting 0 — reject it at parse time instead.
    """
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid positive int value: {value!r}"
        ) from None
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


#: Exit code returned when a mutating command cannot acquire the run lock
#: (issue #309). Non-zero so cron / alerting sees the contention; distinct
#: from the generic error (1) and dry-run-found (2) codes some commands use.
EXIT_LOCK_HELD = 75


def _add_lock_args(parser: argparse.ArgumentParser) -> None:
    """Add the shared run-lock ``--wait`` / ``--force`` flags (issue #309).

    Mutating commands acquire an exclusive lock on
    ``<knowledge_root>/.athenaeum.lock`` so overlapping runs (nightly cron +
    manual) don't race wiki writes, sidecar appends, or the API-call budget.
    """
    group = parser.add_argument_group("run lock (single-machine, issue #309)")
    group.add_argument(
        "--wait",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Block up to SECONDS for the run lock instead of failing fast. "
        "Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml "
        "librarian.lock_timeout, then 0 (fail fast).",
    )
    group.add_argument(
        "--force",
        action="store_true",
        help="Break the run lock even if a process is still holding it (the "
        "current holder is logged first) and proceed. Use ONLY when you are "
        "certain the holder is hung or dead; never run two --force invocations "
        "concurrently.",
    )


def _acquire_or_exit(
    knowledge_root: Path,
    args: argparse.Namespace,
    config: dict[str, Any] | None = None,
) -> "RunLock | int":
    """Acquire the run lock or return :data:`EXIT_LOCK_HELD` (issue #309).

    Returns an acquired :class:`~athenaeum.runlock.RunLock` on success (the
    caller must ``release()`` it, ideally in a ``finally``), or the
    :data:`EXIT_LOCK_HELD` exit code after printing the holder to stderr.
    The ``--wait`` flag overrides the resolved default timeout.
    """
    from athenaeum.config import resolve_lock_timeout
    from athenaeum.runlock import LockHeld, RunLock

    wait = getattr(args, "wait", None)
    if wait is None:
        wait = resolve_lock_timeout(config)
    lock = RunLock(knowledge_root, wait=wait, force=getattr(args, "force", False))
    try:
        lock.acquire()
    except LockHeld as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_HELD
    return lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athenaeum",
        description="Knowledge management pipeline — append-only intake, tiered compilation",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {_get_version()}"
    )
    subparsers = parser.add_subparsers(dest="command")

    # init command
    init_parser = subparsers.add_parser(
        "init", help="Initialize a new knowledge directory"
    )
    init_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Target directory (default: ~/knowledge)",
    )
    init_parser.add_argument(
        "--with-templates",
        action="store_true",
        help="Also copy bundled entity-author templates "
        "(person/company/project/concept/source) into <path>/templates/.",
    )
    init_parser.add_argument(
        "--templates-dest",
        type=Path,
        default=None,
        help="Override the templates destination directory "
        "(default: <path>/templates).",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing template files at the destination; "
        "no backup is created (only applies with --with-templates).",
    )

    # status command
    status_parser = subparsers.add_parser("status", help="Show knowledge base status")
    status_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    status_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the kill-switch state "
        "(default: ~/.cache/athenaeum). Only affects the kill-switch line.",
    )

    # disable / enable commands — the kill switch (issue #379)
    disable_parser = subparsers.add_parser(
        "disable",
        help="Turn athenaeum's background work off (compile, detectors, "
        "recall, notifications). Reversible with 'athenaeum enable'.",
    )
    disable_parser.add_argument(
        "--compile",
        action="store_true",
        help="Granular: stop only the expensive compile/detect pass "
        "(session-end contradiction detection); leave recall on.",
    )
    disable_parser.add_argument(
        "--reason",
        type=str,
        default=None,
        help="Optional note recorded in the state file and shown by "
        "'athenaeum status'.",
    )
    disable_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for the state file "
        "(default: ~/.cache/athenaeum).",
    )

    enable_parser = subparsers.add_parser(
        "enable",
        help="Undo 'athenaeum disable' — restore all background work.",
    )
    enable_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for the state file "
        "(default: ~/.cache/athenaeum).",
    )

    # serve command — start the MCP memory server
    serve_parser = subparsers.add_parser("serve", help="Start the MCP memory server")
    serve_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge). The raw/wiki roots "
        "default to <path>/raw and <path>/wiki; the KNOWLEDGE_RAW_PATH / "
        "KNOWLEDGE_WIKI_PATH environment variables override them individually "
        "(drop-in parity with the legacy knowledge-mcp server, issue #355).",
    )
    serve_parser.add_argument(
        "--audience",
        type=str,
        default=None,
        help="Issue #312: pin this server to a restricted read scope. "
        "Comma-separated role/group ids (e.g. 'operations,voltaire'). The "
        "recall tool then returns only pages tagged for one of these roles "
        "(plus 'access: open' pages); untagged/confidential/personal pages "
        "are withheld. Unset = owner = full access. Overrides "
        "ATHENAEUM_AUDIENCE and serve.audience in athenaeum.yaml.",
    )

    # run command — execute the librarian pipeline
    run_parser = subparsers.add_parser("run", help="Run the librarian pipeline")
    run_parser.add_argument(
        "--raw-root",
        type=Path,
        default=None,
        help="Raw intake directory (default: ~/knowledge/raw)",
    )
    run_parser.add_argument(
        "--wiki-root",
        type=Path,
        default=None,
        help="Wiki output directory (default: ~/knowledge/wiki)",
    )
    run_parser.add_argument(
        "--knowledge-root",
        "--path",
        type=Path,
        default=None,
        help="Knowledge git repo root (default: ~/knowledge). "
        "--path is an alias, matching init/status/serve.",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without writing files or committing",
    )
    run_parser.add_argument(
        "--max-files",
        type=_positive_int,
        default=None,
        help=(
            "Stop after processing this many raw files (default: "
            "ATHENAEUM_MAX_FILES env, then athenaeum.yaml "
            "librarian.max_files, then 50)"
        ),
    )
    run_parser.add_argument(
        "--max-api-calls",
        type=_positive_int,
        default=None,
        help=(
            "Maximum estimated API calls per run (default: "
            "ATHENAEUM_MAX_API_CALLS env, then athenaeum.yaml "
            "librarian.max_api_calls, then 800)"
        ),
    )
    run_parser.add_argument(
        "--strict-budget",
        action="store_true",
        help="Exit nonzero when the run trips the API call budget "
        "(the DEGRADED path) instead of the default 0. Opt-in, for "
        "exit-code-based alerting; the warning summary and deferred-work "
        "manifest are written either way.",
    )
    run_parser.add_argument(
        "--batch-mode",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Submit tier-2/tier-3 LLM calls via the Anthropic Messages "
        "Batch API at a 50%% token discount (issue #236). Latency-tolerant: "
        "most batches finish within an hour, 24h worst case — intended for "
        "the nightly run. --no-batch-mode forces the synchronous path even "
        "when the env/yaml default is on. Default: ATHENAEUM_BATCH_MODE "
        "env, then athenaeum.yaml librarian.batch_mode, then off.",
    )
    run_parser.add_argument(
        "--no-retire",
        dest="retire",
        action="store_false",
        default=None,
        help="Skip the move-then-retire pass (issue #261): raw auto-memory "
        "is neither moved into the wiki nor git-removed. Overrides the "
        "athenaeum.yaml librarian.retire toggle (default on). See the "
        "README 'Data lifecycle & upgrade impact' section.",
    )
    run_parser.add_argument(
        "--push",
        dest="push_after_run",
        action="store_true",
        default=None,
        help="After a successful run that produced at least one commit, "
        "invoke `git push` on the knowledge repo (issue #284) using the "
        "operator's ambient git credentials. Overrides the athenaeum.yaml "
        "librarian.push_after_run toggle (default off). No-op on --dry-run "
        "or when the run produced no commits. A push failure is reported "
        "as a non-fatal warning; commits remain local and the next run "
        "retries (`git push` is idempotent).",
    )
    run_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    run_parser.add_argument(
        "--cluster-only",
        action="store_true",
        help="Only run C2 auto-memory discovery + clustering — skip the "
        "entity tier pipeline. Writes the cluster JSONL report and "
        "exits. Useful for validating the cluster output before C3.",
    )
    run_parser.add_argument(
        "--merge-only",
        action="store_true",
        help="Only run C3 cluster merge — read the canonical cluster "
        "JSONL from the last C2 run and emit wiki/auto-*.md entries. "
        "Skips discovery, clustering, and the entity tier pipeline.",
    )
    _add_lock_args(run_parser)

    # test-mcp command — smoke-test the MCP memory setup without a session
    test_mcp_parser = subparsers.add_parser(
        "test-mcp",
        help="Smoke-test MCP remember/recall against a synthetic knowledge dir",
    )
    test_mcp_parser.add_argument(
        "--keep",
        action="store_true",
        help="Don't delete the temp knowledge dir on exit (for debugging)",
    )

    # people command — frontmatter-only filter over type:person wikis
    people_parser = subparsers.add_parser(
        "people",
        help="List type:person wikis filtered by frontmatter (company / tag / tier / score). "
        "No LLM, no embeddings — deterministic over the wiki tree.",
    )
    people_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    people_parser.add_argument(
        "--company",
        action="append",
        default=[],
        help=(
            "Match current_company OR linkedin_company_at_connect "
            "(case-insensitive substring). Repeat to AND."
        ),
    )
    people_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Require this exact tag (repeat to AND).",
    )
    people_parser.add_argument(
        "--tier",
        default="",
        help="Shorthand for --tag tier:<value> (warm-a / warm-b / warm-c / extended / active).",
    )
    people_parser.add_argument(
        "--title-regex",
        action="append",
        default=[],
        help=(
            "Match current_title OR linkedin_position_at_connect against this "
            "regex (case-insensitive). Repeat to AND multiple patterns."
        ),
    )
    people_parser.add_argument(
        "--company-regex",
        action="append",
        default=[],
        help=(
            "Match current_company OR linkedin_company_at_connect against this "
            "regex (case-insensitive). Repeat to AND multiple patterns."
        ),
    )
    people_parser.add_argument(
        "--top-touch",
        type=int,
        default=0,
        help="Sort by recent-touch signal (meeting+sent counts) and return top N. "
        "Default sort is by warm_score desc.",
    )
    people_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max rows to print (default: 50; 0 = unlimited)",
    )
    people_parser.add_argument(
        "--format",
        choices=["table", "tsv"],
        default="table",
        help="Output shape (default: table).",
    )

    # query-topics command — LLM-based topic extraction for hook query rewriting
    query_topics_parser = subparsers.add_parser(
        "query-topics",
        help="Extract substantive search topics from a prompt (Haiku). "
        "Used by the UserPromptSubmit hook to rewrite queries before "
        "FTS5/vector search. Prints one topic per line to stdout; "
        "empty output means fall back to the caller's built-in extractor.",
    )
    query_topics_parser.add_argument(
        "prompt",
        type=str,
        help="The user's raw message.",
    )
    query_topics_parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Seconds to wait for the LLM before giving up (default: 3.0)",
    )
    query_topics_parser.add_argument(
        "--knowledge-root",
        "--path",
        type=Path,
        default=None,
        help="Knowledge directory whose athenaeum.yaml supplies "
        "models.topic (default: ~/knowledge). "
        "--path is an alias, matching init/status/serve.",
    )

    # stopwords command — print the canonical stopword list for shell hooks
    subparsers.add_parser(
        "stopwords",
        help="Print the stopword list (one word per line). "
        "Used by the example UserPromptSubmit hook's regex fallback "
        "to stay in sync with the FTS5 query filter.",
    )

    # ingest-answers command — convert resolved `[x]` blocks in
    # _pending_questions.md into raw intake files and archive the answered
    # blocks. Idempotent — safe to run from a scheduler.
    ingest_answers_parser = subparsers.add_parser(
        "ingest-answers",
        help="Ingest answered pending questions from _pending_questions.md",
    )
    ingest_answers_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(ingest_answers_parser)

    # ingest-merges command (issue #299) — move resolved (`[x]`) blocks out
    # of `wiki/_pending_merges.md` into `_pending_merges_archive.md`, mirroring
    # ingest-answers for the questions sidecar. Idempotent — safe to run from
    # a scheduler.
    ingest_merges_parser = subparsers.add_parser(
        "ingest-merges",
        help="Archive resolved pending merges from wiki/_pending_merges.md",
    )
    ingest_merges_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(ingest_merges_parser)

    # reresolve-questions command (issue #188) — re-run the resolver on OPEN,
    # PROPOSAL-LESS pending questions so a prior cap-hit / offline escalation
    # self-heals. Budget-aware + idempotent; offline (no key) is a no-op.
    reresolve_parser = subparsers.add_parser(
        "reresolve-questions",
        help="Re-resolve open proposal-less pending questions "
        "(self-heal transient cap/offline escalations, issue #188)",
    )
    reresolve_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(reresolve_parser)

    # recall command — shell-accessible recall for validation harnesses
    # and operator debugging. Wraps the MCP `recall` tool so scripts and
    # `gh_wait_status.sh`-style tooling can exercise the same search path
    # without spinning up a Claude Code session.
    recall_parser = subparsers.add_parser(
        "recall",
        help="Search the wiki from the shell (one tab-separated hit per line)",
    )
    recall_parser.add_argument(
        "query",
        type=str,
        help="Search query string",
    )
    recall_parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Maximum results to return (default: 5)",
    )
    recall_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    recall_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory (default: ~/.cache/athenaeum)",
    )
    recall_parser.add_argument(
        "--backend",
        choices=["keyword", "fts5", "vector"],
        default=None,
        help="Override configured backend (default: read from athenaeum.yaml)",
    )
    recall_parser.add_argument(
        "--audience",
        type=str,
        default=None,
        help="Issue #312: run recall under a restricted read scope. "
        "Comma-separated role/group ids; only pages tagged for one of these "
        "roles (or 'access: open') are returned. Unset = owner = full access. "
        "Exercises the identical filter path as `serve --audience`.",
    )
    recall_parser.add_argument(
        "--as-of",
        dest="as_of",
        type=_iso_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Issue #308: temporal 'as-of' view. Return the wiki as it stood on "
        "this date — pages outside their [valid_from, valid_until] window then "
        "are excluded; a fact valid then but expired now is included. Builds a "
        "throwaway as-of index in a scratch cache dir (indexed backends) or "
        "filters at query time (keyword); the live index is untouched. Unset = "
        "today.",
    )

    # dedupe command — find / merge duplicate person wikis
    dedupe_parser = subparsers.add_parser(
        "dedupe",
        help="Find or merge duplicate wiki entries.",
    )
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

    # dedupe wiki-pages — cluster compiled concept/reference/principle
    # wiki pages against EACH OTHER (issue #290) and propose merges via
    # the shared wiki/_pending_merges.md sidecar for human approval.
    # Unlike `dedupe persons`, there is no --apply step here: the only
    # side effect is an idempotent proposal append, never a direct merge.
    dedupe_wiki_pages = dedupe_sub.add_parser(
        "wiki-pages",
        help="Cluster concept/reference/principle wiki pages and propose "
        "merges for near-duplicate topics (issue #290). Writes idempotent "
        "proposals to wiki/_pending_merges.md; --dry-run previews without "
        "writing.",
    )
    dedupe_wiki_pages.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
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

    # claims command — cross-entity recurring-claim detector (issue #272,
    # slice 1 of #258). READ-ONLY: scans the wiki, embeds claim texts via the
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
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    claims_parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Cosine similarity cutoff (default: 0.85)",
    )

    # auto-memory command — operate on compiled wiki/auto-*.md pages.
    # `prune` (issue #278) builds a kill-list of operational/ephemeral
    # auto-memory pages via the same classifier the intake gate uses and,
    # on --apply, git rm's them in one labeled commit + rebuilds the recall
    # index. Default is dry-run (prints kill + retained lists).
    auto_memory_parser = subparsers.add_parser(
        "auto-memory",
        help="Operate on compiled wiki/auto-*.md pages (issue #278).",
    )
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
        default=Path("~/knowledge"),
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

    # repair command — frontmatter YAML repair tools (tag-indent, value-quoting)
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

    # questions command — surface unresolved pending-question blocks
    from athenaeum._cmd_questions import add_questions_subparser

    add_questions_subparser(subparsers)

    # reindex command (issue #349) — rebuild the search index out-of-band.
    # ``rebuild-index`` is kept as a back-compat alias for the #348 spelling;
    # both dispatch to the identical handler (no duplicated index engine).
    rebuild_parser = subparsers.add_parser(
        "reindex",
        aliases=["rebuild-index"],
        help="Rebuild the search index (FTS5 or vector, per config). "
        "--incremental (default) applies only the #348 hash-diff delta; "
        "--full rebuilds from scratch.",
    )
    rebuild_parser.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    rebuild_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory (default: ~/.cache/athenaeum)",
    )
    rebuild_parser.add_argument(
        "--backend",
        choices=["fts5", "vector"],
        default=None,
        help="Override configured backend (default: read from athenaeum.yaml)",
    )
    reindex_mode = rebuild_parser.add_mutually_exclusive_group()
    reindex_mode.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "Apply only the changed/added/deleted hash-diff delta (issue "
            "#348). This is the DEFAULT; the flag makes it explicit."
        ),
    )
    reindex_mode.add_argument(
        "--full",
        action="store_true",
        help=(
            "Wipe and fully rebuild instead of applying only the "
            "changed/added/deleted delta (issue #348). Use for seeding or "
            "after an embedding-model change; default is incremental."
        ),
    )
    rebuild_parser.add_argument(
        "--as-of",
        dest="as_of",
        type=_iso_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Issue #308: build an as-of index reflecting the wiki as it stood "
        "on this date (pages outside their [valid_from, valid_until] window then "
        "are excluded). Always a full build; point --cache-dir at a scratch "
        "directory so the live index is not overwritten, then "
        "`recall --cache-dir <that>`. Unset = today (the normal live index).",
    )
    _add_lock_args(rebuild_parser)

    # compile command (issue #359) — compile-as-of. Recompiles a HISTORICAL
    # wiki snapshot as it would have stood on --as-of, into a scratch --out
    # dir. Distinct from slice 3's read-time `recall/reindex --as-of` filter:
    # this RE-RUNS the deterministic C3 blend (resurrecting members expired
    # now but valid then), never touching the live wiki or raw tree.
    compile_parser = subparsers.add_parser(
        "compile",
        help="Issue #359: recompile a historical wiki snapshot as-of a past "
        "date into a scratch --out dir (compile-as-of). Distinct from the "
        "read-time `recall/reindex --as-of` filter — this re-runs the C3 "
        "blend so members expired now but valid then are re-included. "
        "Deterministic (no LLM); never mutates the live wiki or raw tree.",
    )
    compile_parser.add_argument(
        "--path",
        "--knowledge-root",
        dest="path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge).",
    )
    compile_parser.add_argument(
        "--as-of",
        dest="as_of",
        type=_iso_date,
        required=True,
        metavar="YYYY-MM-DD",
        help="Historical date to recompile as of (inclusive). Members whose "
        "validity window had closed on this date (or that carry a tombstone) "
        "are excluded; members expired now but valid then are re-included. "
        "Rewind is valid-time, not transaction-time (see docs §8.7).",
    )
    compile_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        metavar="DIR",
        help="Scratch directory to write the recompiled wiki into. MUST NOT "
        "be the live wiki/ directory.",
    )

    # ingest command (issue #349) — on-demand compile of new/changed raw
    # intake into the wiki. The manual escape hatch that makes a just-
    # remembered fact recallable now, decoupled from the nightly `run`.
    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Compile new/changed raw intake into the wiki on demand "
        "(issue #349). --incremental (default) compiles only files new/"
        "changed since the last ingest; --full recompiles.",
    )
    ingest_parser.add_argument(
        "--path",
        "--knowledge-root",
        dest="path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge). "
        "--knowledge-root is an alias, matching `run`.",
    )
    ingest_mode = ingest_parser.add_mutually_exclusive_group()
    ingest_mode.add_argument(
        "--incremental",
        dest="incremental",
        action="store_true",
        default=None,
        help="Compile only raw files new/changed since the last successful "
        "ingest (tracked via a content-hash stamp). This is the DEFAULT.",
    )
    ingest_mode.add_argument(
        "--full",
        dest="incremental",
        action="store_false",
        help="Recompile all pending raw intake, ignoring the ingest stamp.",
    )
    ingest_parser.add_argument(
        "--session",
        type=str,
        default=None,
        help="Scope the new/changed detection to one originSessionId "
        "(the SessionEnd use-case, issue #350).",
    )
    ingest_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the ingest stamp manifest "
        "(default: ~/.cache/athenaeum)",
    )
    ingest_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the compile without writing files, committing, or "
        "updating the ingest stamp.",
    )
    ingest_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    _add_lock_args(ingest_parser)

    # session-end command (issue #350) — the change-gated compile-then-index
    # composition the cwc SessionEnd hook and the nightly-after-librarian path
    # invoke as ONE command: incremental `ingest` of this session's new raw,
    # then (only when the compile actually ran) an incremental `reindex` so the
    # freshly-compiled wiki pages become recallable. An idle SessionEnd is a
    # fast no-op with zero LLM cost and no reindex.
    session_end_parser = subparsers.add_parser(
        "session-end",
        help="Change-gated ingest + reindex for SessionEnd (issue #350): "
        "compile this session's new raw intake, then refresh the index — a "
        "fast no-op (no LLM, no reindex) when nothing changed.",
    )
    session_end_parser.add_argument(
        "--path",
        "--knowledge-root",
        dest="path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge). "
        "--knowledge-root is an alias, matching `run`/`ingest`.",
    )
    session_end_mode = session_end_parser.add_mutually_exclusive_group()
    session_end_mode.add_argument(
        "--incremental",
        dest="incremental",
        action="store_true",
        default=None,
        help="Compile only raw new/changed since the last ingest and apply the "
        "#348 index delta. This is the DEFAULT.",
    )
    session_end_mode.add_argument(
        "--full",
        dest="incremental",
        action="store_false",
        help="Force a full recompile of all pending raw intake AND a full "
        "index rebuild (operator escape hatch).",
    )
    session_end_parser.add_argument(
        "--session",
        type=str,
        default=None,
        help="Scope the new/changed detection to one originSessionId "
        "(the SessionEnd use-case).",
    )
    session_end_parser.add_argument(
        "--backend",
        choices=["fts5", "vector"],
        default=None,
        help="Override configured search backend (default: read from "
        "athenaeum.yaml)",
    )
    session_end_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the ingest + index manifests "
        "(default: ~/.cache/athenaeum)",
    )
    session_end_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the compile without writing files, committing, updating the "
        "ingest stamp, or reindexing.",
    )
    session_end_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    _add_lock_args(session_end_parser)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "init":
        return _cmd_init(args)

    if args.command == "status":
        return _cmd_status(args)

    if args.command == "disable":
        return _cmd_disable(args)

    if args.command == "enable":
        return _cmd_enable(args)

    if args.command == "serve":
        return _cmd_serve(args)

    if args.command == "run":
        return _cmd_run(args)

    if args.command == "ingest-answers":
        return _cmd_ingest_answers(args)

    if args.command == "ingest-merges":
        return _cmd_ingest_merges(args)

    if args.command == "reresolve-questions":
        return _cmd_reresolve_questions(args)

    if args.command in ("reindex", "rebuild-index"):
        return _cmd_rebuild_index(args)

    if args.command == "compile":
        return _cmd_compile_as_of(args)

    if args.command == "ingest":
        return _cmd_ingest(args)

    if args.command == "session-end":
        return _cmd_session_end(args)

    if args.command == "recall":
        return _cmd_recall(args)

    if args.command == "people":
        return _cmd_people(args)

    if args.command == "query-topics":
        return _cmd_query_topics(args)

    if args.command == "test-mcp":
        return _cmd_test_mcp(args)

    if args.command == "stopwords":
        return _cmd_stopwords(args)

    if args.command == "dedupe":
        return _cmd_dedupe(args)

    if args.command == "claims":
        return _cmd_claims(args)

    if args.command == "auto-memory":
        return _cmd_auto_memory(args)

    if args.command == "repair":
        return _cmd_repair(args)

    if args.command == "questions":
        from athenaeum._cmd_questions import cmd_questions

        return cmd_questions(args)

    return 0


def _cmd_dedupe(args: argparse.Namespace) -> int:
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

    # --apply (mutating): acquire the single-machine run lock (issue #309).
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
    """Dispatch ``athenaeum dedupe wiki-pages`` (issue #290).

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

    # Issue #309: --dry-run writes nothing, so it does NOT take the lock. The
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


def _cmd_claims(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum claims --find`` (issue #272). READ-ONLY.

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


def _cmd_auto_memory(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum auto-memory prune`` (issue #278)."""
    target = getattr(args, "auto_memory_target", None)
    if target != "prune":
        print("usage: athenaeum auto-memory prune [--apply] ...", file=sys.stderr)
        return 2
    return _cmd_auto_memory_prune(args)


def _cmd_auto_memory_prune(args: argparse.Namespace) -> int:
    """Prune operational/ephemeral ``wiki/auto-*.md`` pages (issue #278).

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

    # --apply (mutating): acquire the single-machine run lock (issue #309).
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


def _rebuild_recall_index(
    knowledge_root: Path,
    cfg: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    """Rebuild the recall index after a prune apply (issue #278).

    Mirrors :func:`_cmd_rebuild_index`'s backend resolution so the index
    reflects the removed pages. A rebuild failure is reported but never
    fails the prune (the git removal already committed).
    """
    from athenaeum.config import resolve_extra_intake_roots
    from athenaeum.search import build_fts5_index, build_vector_index

    wiki_root = knowledge_root / "wiki"
    backend = getattr(args, "backend", None) or cfg.get("search_backend", "fts5")
    cache_dir = (
        (getattr(args, "cache_dir", None) or Path("~/.cache/athenaeum"))
        .expanduser()
        .resolve()
    )
    extra_roots = resolve_extra_intake_roots(knowledge_root, cfg)
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        if backend == "vector":
            count = build_vector_index(wiki_root, cache_dir, extra_roots=extra_roots)
        else:
            count = build_fts5_index(wiki_root, cache_dir, extra_roots=extra_roots)
        print(f"  recall index rebuilt ({backend}): {count} page(s).")
    except Exception as exc:  # noqa: BLE001 - rebuild failure must not fail prune
        print(
            f"  WARN recall index rebuild failed ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )


def _cmd_init(args: argparse.Namespace) -> int:
    from athenaeum.init import copy_templates, init_knowledge_dir

    target = init_knowledge_dir(args.path)
    print(f"Initialized knowledge directory at {target}")

    if getattr(args, "with_templates", False):
        dest = args.templates_dest if args.templates_dest else target / "templates"
        dest = dest.expanduser().resolve()
        written, skipped = copy_templates(dest, force=args.force)
        for fname in written:
            print(f"  wrote   {dest / fname}")
        for fname in skipped:
            print(f"  skipped {dest / fname} (exists; pass --force to overwrite)")
    elif args.templates_dest is not None:
        print(
            "warning: --templates-dest is ignored without --with-templates; "
            "no templates were copied.",
            file=sys.stderr,
        )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from athenaeum.killswitch import format_status_line
    from athenaeum.status import format_status, status

    # Kill-switch state (issue #379) is process/cache state, independent of the
    # knowledge base — report it first so 'athenaeum status' always answers
    # "is it on?" even when the knowledge directory is missing.
    print(format_status_line(getattr(args, "cache_dir", None)))
    print()

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}")
        print(f"Run 'athenaeum init --path {args.path}' first, then retry.")
        return 1
    info = status(target)
    print(format_status(info))
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    """Kill switch (#379): stop athenaeum background work, reversibly."""
    from athenaeum import killswitch

    scope = killswitch.SCOPE_COMPILE if args.compile else killswitch.SCOPE_ALL
    path = killswitch.disable(
        scope, reason=args.reason, cache_dir=getattr(args, "cache_dir", None)
    )
    if scope == killswitch.SCOPE_COMPILE:
        print(
            "athenaeum disabled (compile): the session-end compile/detect pass "
            "is off; recall stays on."
        )
    else:
        print(
            "athenaeum disabled: all background work is off — compile, "
            "contradiction detection, recall, and notifications."
        )
    print(f"State: {path}")
    print("Re-enable with: athenaeum enable")

    env_state = killswitch.current_state(getattr(args, "cache_dir", None))
    if env_state.source == "env" and env_state.scope != scope:
        print(
            f"note: {killswitch.ENV_VAR}={os.environ.get(killswitch.ENV_VAR)!r} "
            f"overrides the state file (effective scope: {env_state.scope}).",
            file=sys.stderr,
        )
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    """Kill switch (#379): undo 'athenaeum disable'."""
    from athenaeum import killswitch

    removed = killswitch.enable(cache_dir=getattr(args, "cache_dir", None))
    if removed:
        print("athenaeum enabled: background work restored.")
    else:
        print("athenaeum was already enabled (no state file to remove).")

    env = killswitch.current_state(getattr(args, "cache_dir", None))
    if env.source == "env":
        print(
            f"warning: {killswitch.ENV_VAR}={os.environ.get(killswitch.ENV_VAR)!r} "
            f"still forces disabled (scope: {env.scope}); unset it to fully "
            "re-enable.",
            file=sys.stderr,
        )
        return 0
    return 0


def _resolve_serve_roots(target: Path) -> tuple[Path, Path]:
    """Resolve the raw/wiki roots for ``athenaeum serve``.

    Defaults to ``<target>/raw`` and ``<target>/wiki``. When set, the
    ``KNOWLEDGE_RAW_PATH`` / ``KNOWLEDGE_WIKI_PATH`` environment variables
    override the respective root INDEPENDENTLY. This preserves drop-in parity
    with the legacy standalone ``knowledge-mcp`` server this command supersedes
    (issue #355): an existing MCP config (or ``start.sh``) that pins those env
    vars keeps working unchanged after the cwc copy is removed. ``--path`` is
    still where config (``athenaeum.yaml``) and extra intake roots resolve, so
    the common case (both under ``~/knowledge``) is unaffected.
    """
    raw_env = os.environ.get("KNOWLEDGE_RAW_PATH")
    wiki_env = os.environ.get("KNOWLEDGE_WIKI_PATH")
    raw_root = Path(raw_env).expanduser().resolve() if raw_env else target / "raw"
    wiki_root = Path(wiki_env).expanduser().resolve() if wiki_env else target / "wiki"
    return raw_root, wiki_root


def _cmd_serve(args: argparse.Namespace) -> int:
    from athenaeum.config import (
        load_config,
        resolve_audience,
        resolve_extra_intake_roots,
        resolve_screening,
    )
    from athenaeum.mcp_server import create_server

    target = args.path.expanduser().resolve()
    raw_root, wiki_root = _resolve_serve_roots(target)

    if not target.exists():
        print(f"Knowledge directory not found: {target}")
        print(f"Run 'athenaeum init --path {args.path}' first, then retry.")
        return 1

    cfg = load_config(target)
    backend = cfg.get("search_backend", "fts5")
    cache_dir = Path("~/.cache/athenaeum").expanduser()
    extra_roots = resolve_extra_intake_roots(target, cfg)

    # Issue #312: resolve the serve-time read-scope pin (CLI > env > yaml).
    # None = owner = full access (existing single-user behavior).
    caller_audience = resolve_audience(cfg, getattr(args, "audience", None))
    if caller_audience is not None:
        print(
            "[audience] recall restricted to roles: "
            f"{', '.join(sorted(caller_audience))} "
            "(untagged/confidential/personal pages withheld)",
            file=sys.stderr,
        )

    # Issue #320: resolve intake screening (env > yaml > off). Fails fast with
    # a clear message on a mis-set screening block rather than serving with a
    # silently inert classifier.
    from athenaeum.screening import ScreeningConfigError

    try:
        screening = resolve_screening(cfg)
    except ScreeningConfigError as exc:
        print(f"[screening] invalid configuration: {exc}", file=sys.stderr)
        return 1
    if screening["medical"]["action"] != "off":
        print(
            "[screening] medical intake → "
            f"{screening['medical']['action']} "
            f"(access: {screening['medical']['access']})",
            file=sys.stderr,
        )

    # Warn on config/cache mismatch. The recall tool silently returns zero
    # hits when the configured backend's index is missing, so users with
    # `search_backend: vector` but an fts5-only cache (common when you flip
    # backends in athenaeum.yaml but forget to rebuild) see recall "work"
    # but return nothing. Catch that up front.
    _warn_if_backend_cache_missing(backend, cache_dir)

    server = create_server(
        raw_root=raw_root,
        wiki_root=wiki_root,
        search_backend=backend,
        cache_dir=cache_dir,
        extra_roots=extra_roots,
        caller_audience=caller_audience,
        screening=screening,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


def _warn_if_backend_cache_missing(backend: str, cache_dir: Path) -> None:
    """Print a warning if the configured backend has no cache on disk.

    The keyword backend has no cache. FTS5 expects ``wiki-index.db``;
    vector expects ``wiki-vectors/``. When either is missing, recall
    silently returns empty — the warning tells the user to run
    ``athenaeum rebuild-index``.
    """
    if backend == "keyword":
        return
    if backend == "fts5":
        if not (cache_dir / "wiki-index.db").is_file():
            print(
                f"[warn] search_backend=fts5 but no index at "
                f"{cache_dir / 'wiki-index.db'}.\n"
                f"       Run `athenaeum rebuild-index --path <knowledge>` "
                f"before relying on recall.",
                file=sys.stderr,
            )
        return
    if backend == "vector":
        if not (cache_dir / "wiki-vectors").is_dir():
            print(
                f"[warn] search_backend=vector but no index at "
                f"{cache_dir / 'wiki-vectors'}.\n"
                f"       Run `athenaeum rebuild-index --path <knowledge>` "
                f"before relying on recall.",
                file=sys.stderr,
            )
        return
    print(
        f"[warn] unknown search_backend {backend!r}; "
        f"recall will fail until this is fixed in athenaeum.yaml.",
        file=sys.stderr,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    from athenaeum.librarian import DEFAULT_KNOWLEDGE_ROOT, run

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    knowledge_root = args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT
    raw_root = args.raw_root or (knowledge_root / "raw")
    wiki_root = args.wiki_root or (knowledge_root / "wiki")

    # Issue #309: a --dry-run reads nothing mutating, so it does NOT take the
    # single-machine run lock. A real run acquires it so overlapping runs
    # (nightly cron + manual) don't race wiki writes or the API-call budget.
    if args.dry_run:
        return run(
            raw_root=raw_root,
            wiki_root=wiki_root,
            knowledge_root=knowledge_root,
            dry_run=args.dry_run,
            max_files=args.max_files,
            max_api_calls=args.max_api_calls,
            cluster_only=getattr(args, "cluster_only", False),
            merge_only=getattr(args, "merge_only", False),
            strict_budget=args.strict_budget,
            batch_mode=args.batch_mode,
            retire=getattr(args, "retire", None),
            push_after_run=getattr(args, "push_after_run", None),
        )

    from athenaeum.config import load_config

    lock = _acquire_or_exit(knowledge_root, args, load_config(knowledge_root))
    if isinstance(lock, int):
        return lock
    try:
        return run(
            raw_root=raw_root,
            wiki_root=wiki_root,
            knowledge_root=knowledge_root,
            dry_run=args.dry_run,
            max_files=args.max_files,
            max_api_calls=args.max_api_calls,
            cluster_only=getattr(args, "cluster_only", False),
            merge_only=getattr(args, "merge_only", False),
            strict_budget=args.strict_budget,
            batch_mode=args.batch_mode,
            retire=getattr(args, "retire", None),
            push_after_run=getattr(args, "push_after_run", None),
            install_signal_handlers=True,
        )
    finally:
        lock.release()


def _cmd_ingest_answers(args: argparse.Namespace) -> int:
    """Ingest answered blocks from `_pending_questions.md` as raw intake.

    See :func:`athenaeum.answers.ingest_answers` for the semantics.

    When ``ANTHROPIC_API_KEY`` is set, builds a live Anthropic client and
    passes it to ``ingest_answers`` so free-text answers can use the
    LLM-backed proposer (issue #210). When the key is absent or client
    construction fails, the annotation fallback is used instead.
    """
    from athenaeum.answers import ingest_answers
    from athenaeum.config import load_config
    from athenaeum.provider import build_llm_client

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}", file=sys.stderr)
        print(
            f"Run 'athenaeum init --path {args.path}' first, then retry.",
            file=sys.stderr,
        )
        return 1

    pending_path = target / "wiki" / "_pending_questions.md"
    raw_root = target / "raw"

    cfg = load_config(target)

    # Issue #210/#330: build the LLM client via the provider seam so free-text
    # answers trigger the LLM-backed source-edit proposer. Returns None for the
    # ``api`` backend with no ANTHROPIC_API_KEY (offline annotation fallback);
    # returns the subscription CLI client for ``claude-cli``. Fail gracefully
    # (None) on any construction error.
    anthropic_client = None
    try:
        anthropic_client = build_llm_client(cfg)
    except Exception:  # noqa: BLE001
        pass

    lock = _acquire_or_exit(target, args, cfg)  # issue #309
    if isinstance(lock, int):
        return lock
    try:
        count = ingest_answers(
            pending_path, raw_root, client=anthropic_client, config=cfg
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(
            f"Fatal error ingesting answers ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    finally:
        lock.release()

    print(f"Ingested {count} answered question(s).")
    return 0


def _cmd_ingest_merges(args: argparse.Namespace) -> int:
    """Archive resolved blocks from `wiki/_pending_merges.md` (issue #299).

    See :func:`athenaeum.pending_merges.ingest_resolved_merges` for the
    semantics. Mirrors :func:`_cmd_ingest_answers`'s CLI shape.
    """
    from athenaeum.pending_merges import ingest_resolved_merges

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}", file=sys.stderr)
        print(
            f"Run 'athenaeum init --path {args.path}' first, then retry.",
            file=sys.stderr,
        )
        return 1

    merges_path = target / "wiki" / "_pending_merges.md"

    from athenaeum.config import load_config

    lock = _acquire_or_exit(target, args, load_config(target))  # issue #309
    if isinstance(lock, int):
        return lock
    try:
        count = ingest_resolved_merges(merges_path)
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(
            f"Fatal error ingesting merges ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    finally:
        lock.release()

    print(f"Archived {count} resolved merge(s).")
    return 0


def _cmd_reresolve_questions(args: argparse.Namespace) -> int:
    """Re-resolve open, proposal-less pending questions (issue #188).

    Mirrors :func:`_cmd_ingest_answers`: loads config, builds a live Anthropic
    client from ``ANTHROPIC_API_KEY`` (``None`` when absent — offline is a
    no-op), and delegates to :func:`athenaeum.tiers.reresolve_open_questions`.
    """
    from athenaeum.config import load_config
    from athenaeum.provider import build_llm_client
    from athenaeum.tiers import reresolve_open_questions

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}", file=sys.stderr)
        return 1

    pending_path = target / "wiki" / "_pending_questions.md"
    cfg = load_config(target)

    # Issue #330: construct via the provider seam (api key -> SDK client;
    # claude-cli -> subscription CLI client; None when the api backend has no
    # key, preserving the offline no-op below).
    anthropic_client = None
    try:
        anthropic_client = build_llm_client(cfg)
    except Exception:  # noqa: BLE001
        pass

    lock = _acquire_or_exit(target, args, cfg)  # issue #309
    if isinstance(lock, int):
        return lock
    try:
        count = reresolve_open_questions(
            pending_path, client=anthropic_client, config=cfg
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(
            f"Fatal error re-resolving questions ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    finally:
        lock.release()

    if anthropic_client is None:
        print("No ANTHROPIC_API_KEY; offline — left proposal-less questions as-is.")
    else:
        print(f"Re-resolved {count} proposal-less question(s).")
    return 0


def _reindex_summary(
    command: str,
    backend: str,
    mode: str,
    pages: int,
    t0: float,
    exit_code: int,
) -> None:
    """Print the one-line JSON reindex summary (issue #349, counts+duration)."""
    import json
    import time

    print(
        json.dumps(
            {
                "command": command,
                "backend": backend,
                "mode": mode,
                "pages": pages,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "exit_code": exit_code,
            }
        )
    )


def _cmd_compile_as_of(args: argparse.Namespace) -> int:
    """Issue #359: recompile a historical wiki snapshot as-of a past date.

    Re-runs the deterministic C3 blend (no LLM) with ``--as-of`` threaded into
    the per-member active predicate, writing to ``--out``. Never mutates the
    live wiki or raw tree. Distinct from slice 3's read-time filter — see
    :func:`athenaeum.merge.compile_as_of`.
    """
    from athenaeum.config import load_config
    from athenaeum.merge import compile_as_of

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.exists():
        print(f"Wiki directory not found: {wiki_root}", file=sys.stderr)
        return 1

    as_of = args.as_of
    out_dir = args.out.expanduser().resolve()
    if out_dir == wiki_root.expanduser().resolve():
        print(
            "--out must not be the live wiki directory; point it at a scratch "
            f"path (got {out_dir})",
            file=sys.stderr,
        )
        return 1

    cfg = load_config(knowledge_root)
    try:
        entries = compile_as_of(knowledge_root, as_of, out_dir, config=cfg)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"Recompiled {len(entries)} wiki entr"
        f"{'y' if len(entries) == 1 else 'ies'} as of "
        f"{as_of.isoformat()} into {out_dir} "
        "(compile-as-of; live wiki untouched)"
    )
    return 0


def _cmd_rebuild_index(args: argparse.Namespace) -> int:
    import time

    from athenaeum.config import (
        load_config,
        resolve_embedding_model,
        resolve_extra_intake_roots,
        resolve_index_globs,
        resolve_reindex_full_rehash_max_age_days,
    )
    from athenaeum.search import build_fts5_index, build_vector_index

    # Issue #349: `reindex` is the canonical name; `rebuild-index` is a
    # back-compat alias routed here. Report whichever the user invoked.
    command = getattr(args, "command", "reindex") or "reindex"
    t0 = time.monotonic()

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    cache_dir = (args.cache_dir or Path("~/.cache/athenaeum")).expanduser().resolve()

    if not wiki_root.exists():
        print(f"Wiki directory not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    if args.backend is not None:
        backend = args.backend
    else:
        backend = cfg.get("search_backend", "fts5")

    extra_roots = resolve_extra_intake_roots(knowledge_root, cfg)
    include_globs, exclude_globs = resolve_index_globs(cfg)
    embedding_model = resolve_embedding_model(cfg)
    full_rehash_max_age_days = resolve_reindex_full_rehash_max_age_days(
        knowledge_root, cfg
    )
    incremental = not getattr(args, "full", False)

    # Issue #308: an as-of index reflects a past date's validity windows and is
    # always a FULL build (a historical snapshot has no manifest to diff), so it
    # reports "full" regardless of the incremental default.
    as_of = getattr(args, "as_of", None)
    as_of_note = f" as of {as_of.isoformat()}" if as_of is not None else ""
    mode = "full" if (not incremental or as_of is not None) else "incremental"

    cache_dir.mkdir(parents=True, exist_ok=True)

    # Rebuilding the index always writes — acquire the run lock so it can't
    # race a concurrent `run` rebuilding the same index (issue #309).
    lock = _acquire_or_exit(knowledge_root, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        if backend == "vector":
            try:
                count = build_vector_index(
                    wiki_root,
                    cache_dir,
                    extra_roots=extra_roots,
                    incremental=incremental,
                    include_globs=include_globs,
                    exclude_globs=exclude_globs,
                    embedding_model=embedding_model,
                    as_of=as_of,
                    full_rehash_max_age_days=full_rehash_max_age_days,
                )
            except ImportError as exc:
                print(f"Vector backend unavailable: {exc}", file=sys.stderr)
                print("Install with: pip install athenaeum[vector]", file=sys.stderr)
                _reindex_summary(command, backend, mode, 0, t0, 1)
                return 1
            print(
                f"Vector index rebuilt{as_of_note} ({mode}): {count} pages "
                f"(wiki + {len(extra_roots)} extra root(s))"
            )
            _reindex_summary(command, backend, mode, count, t0, 0)
            return 0

        if backend == "fts5":
            count = build_fts5_index(
                wiki_root,
                cache_dir,
                extra_roots=extra_roots,
                incremental=incremental,
                include_globs=include_globs,
                exclude_globs=exclude_globs,
                as_of=as_of,
                full_rehash_max_age_days=full_rehash_max_age_days,
            )
            print(
                f"FTS5 index rebuilt{as_of_note} ({mode}): {count} pages "
                f"(wiki + {len(extra_roots)} extra root(s))"
            )
            _reindex_summary(command, backend, mode, count, t0, 0)
            return 0

        print(f"Unknown search backend: {backend}", file=sys.stderr)
        _reindex_summary(command, backend, mode, 0, t0, 1)
        return 1
    finally:
        lock.release()


def _cmd_ingest(args: argparse.Namespace) -> int:
    """On-demand compile of new/changed raw intake (issue #349).

    Thin CLI wrapper over :func:`athenaeum.librarian.ingest` — the single
    reusable incremental-ingest engine the SessionEnd path (#350) also calls.
    Acquires the shared run lock (single-flight, #309) around the compile,
    prints a one-line JSON summary (counts + duration), and exits non-zero
    when the underlying compile fails.
    """
    import json

    from athenaeum.config import load_config
    from athenaeum.librarian import DEFAULT_KNOWLEDGE_ROOT, ingest

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    knowledge_root = (
        args.path.expanduser().resolve() if args.path else DEFAULT_KNOWLEDGE_ROOT
    )
    raw_root = knowledge_root / "raw"
    wiki_root = knowledge_root / "wiki"
    # Default incremental: neither flag → None → True; --full → False.
    incremental = True if args.incremental is None else args.incremental

    cfg = load_config(knowledge_root)

    # Issue #309 single-flight: a real compile mutates wiki/ and shares the
    # nightly-run lock. A --dry-run reads only, so it does not take the lock.
    lock: RunLock | int | None = None
    if not args.dry_run:
        lock = _acquire_or_exit(knowledge_root, args, cfg)
        if isinstance(lock, int):
            return lock
    try:
        result = ingest(
            raw_root=raw_root,
            wiki_root=wiki_root,
            knowledge_root=knowledge_root,
            incremental=incremental,
            session=args.session,
            cache_dir=args.cache_dir,
            config=cfg,
            dry_run=args.dry_run,
            install_signal_handlers=not args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean JSON error line
        print(
            json.dumps(
                {
                    "command": "ingest",
                    "error": f"{type(exc).__name__}: {exc}",
                    "exit_code": 1,
                }
            )
        )
        return 1
    finally:
        if lock is not None and not isinstance(lock, int):
            lock.release()

    print(json.dumps(result.summary()))
    return result.exit_code


def _cmd_session_end(args: argparse.Namespace) -> int:
    """Change-gated SessionEnd ingest + reindex (issue #350).

    Thin CLI wrapper over :func:`athenaeum.librarian.session_end` — the single
    reusable composition the cwc SessionEnd hook and the nightly-after-librarian
    path invoke. Acquires the shared run lock (single-flight, #309) ONCE around
    both the compile and the reindex, prints a one-line JSON summary (nested
    ingest counts + reindex pages + duration), and exits non-zero when the
    underlying compile fails.
    """
    import json

    from athenaeum.config import load_config
    from athenaeum.librarian import DEFAULT_KNOWLEDGE_ROOT, session_end

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    knowledge_root = (
        args.path.expanduser().resolve() if args.path else DEFAULT_KNOWLEDGE_ROOT
    )
    raw_root = knowledge_root / "raw"
    wiki_root = knowledge_root / "wiki"
    incremental = True if args.incremental is None else args.incremental

    # Kill switch (#379): the compile/detect pass is the expensive, unattended
    # ``claude -p`` fan-out — honour the disabled flag BEFORE the lock or any
    # work, so 'athenaeum disable' (or --compile) prevents the next pass with
    # no pkill needed. Emit a JSON no-op line so callers tailing the pipe see it.
    from athenaeum.killswitch import current_state, is_disabled

    if is_disabled("compile", cache_dir=args.cache_dir):
        state = current_state(args.cache_dir)
        print(
            json.dumps(
                {
                    "command": "session-end",
                    "noop": True,
                    "reason": "disabled",
                    "scope": state.scope,
                    "source": state.source,
                }
            )
        )
        sys.stdout.flush()
        return 0

    cfg = load_config(knowledge_root)

    # Issue #309 single-flight: the compile + reindex both mutate on-disk state
    # (wiki/ and the index) and share the nightly-run lock. A --dry-run reads
    # only, so it does not take the lock.
    lock: RunLock | int | None = None
    if not args.dry_run:
        lock = _acquire_or_exit(knowledge_root, args, cfg)
        if isinstance(lock, int):
            return lock
    try:
        result = session_end(
            raw_root=raw_root,
            wiki_root=wiki_root,
            knowledge_root=knowledge_root,
            incremental=incremental,
            session=args.session,
            cache_dir=args.cache_dir,
            config=cfg,
            backend=args.backend,
            dry_run=args.dry_run,
            install_signal_handlers=not args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean JSON error line
        print(
            json.dumps(
                {
                    "command": "session-end",
                    "error": f"{type(exc).__name__}: {exc}",
                    "exit_code": 1,
                }
            )
        )
        return 1
    finally:
        if lock is not None and not isinstance(lock, int):
            lock.release()

    print(json.dumps(result.summary()))
    # Issue #370: the summary line is the only stdout; flush both streams so a
    # caller tailing the pipe sees the result immediately (the reindex can run
    # for minutes and the run otherwise looks like a silent hang).
    sys.stdout.flush()
    sys.stderr.flush()
    return result.exit_code


def _cmd_recall(args: argparse.Namespace) -> int:
    """Shell-accessible recall — prints one tab-separated hit per line.

    Output format per line: ``<score>\\t<filename>\\t<preview>``, where
    ``<preview>`` is the first 80 chars of the wiki page body (post
    frontmatter), newlines collapsed to spaces. Used by validation
    harnesses and operator debugging scripts that can't rely on an MCP
    session. Reads ``search_backend`` + extra intake roots from
    ``athenaeum.yaml`` the same way ``serve`` and ``rebuild-index`` do,
    so results match what the MCP ``recall`` tool would return.
    """
    from athenaeum.config import (
        load_config,
        resolve_audience,
        resolve_extra_intake_roots,
    )
    from athenaeum.models import (
        is_inactive_memory,
        is_page_authorized,
        parse_frontmatter,
    )
    from athenaeum.search import get_backend

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"

    if not wiki_root.exists():
        print(f"Wiki directory not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    backend_name = args.backend or cfg.get("search_backend", "fts5")
    cache_dir = (args.cache_dir or Path("~/.cache/athenaeum")).expanduser().resolve()
    extra_roots = resolve_extra_intake_roots(knowledge_root, cfg)

    # Issue #312: resolve the read-scope pin (CLI > env > yaml). None = owner.
    caller_audience = resolve_audience(cfg, getattr(args, "audience", None))

    try:
        backend = get_backend(backend_name)
    except KeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Issue #308: an as-of view queries the wiki as it stood on a past date.
    # Indexed backends (fts5/vector) filter at BUILD time, so we build a
    # THROWAWAY as-of index in a scratch cache dir and query that — the live
    # index at ``cache_dir`` is never touched. The keyword backend scans on
    # query and honors ``as_of`` directly; a scratch build is a cheap no-op for
    # it. ``as_of`` is passed to ``query`` too so keyword filters at query time.
    as_of = getattr(args, "as_of", None)
    query_cache = cache_dir
    if as_of is not None:
        query_cache = cache_dir / "_asof" / as_of.isoformat()
        query_cache.mkdir(parents=True, exist_ok=True)
        try:
            backend.build_index(
                wiki_root, query_cache, extra_roots=extra_roots, as_of=as_of
            )
        except ImportError as exc:
            print(f"As-of index build failed: {exc}", file=sys.stderr)
            return 1

    try:
        hits = backend.query(
            args.query,
            query_cache,
            n=args.top_k,
            wiki_root=wiki_root,
            caller_audience=caller_audience,
            as_of=as_of,
        )
    except NotImplementedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    from athenaeum.mcp_server import _resolve_hit_path

    for filename, _name, score in hits:
        page_path, _display = _resolve_hit_path(filename, wiki_root, extra_roots)
        preview = ""
        fm: dict[str, object] = {}
        readable = False
        if page_path is not None and page_path.is_file():
            try:
                text = page_path.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(text)
                preview = " ".join(body.split())[:80]
                readable = True
            except (OSError, UnicodeDecodeError):
                pass
        # Layer C fail-closed re-check against fresh on-disk frontmatter.
        if caller_audience is not None and (
            not readable or not is_page_authorized(fm, caller_audience)
        ):
            continue
        # Issue #308: temporal backstop — drop any hit outside its validity
        # window relative to ``as_of`` (default today), so the CLI output stays
        # consistent with the requested view regardless of backend build state.
        if readable and is_inactive_memory(fm, as_of):
            continue
        print(f"{score:.2f}\t{filename}\t{preview}")

    return 0


def _cmd_people(args: argparse.Namespace) -> int:
    """List type:person wikis filtered by frontmatter — frontmatter-only, no LLM.

    Filters AND together. Companies match current_company OR
    linkedin_company_at_connect (case-insensitive substring). Tags must
    match exactly. Tier is shorthand for ``tag tier:<value>``. Default
    sort is by ``warm_score`` desc; ``--top-touch N`` switches to a
    recent-touch composite score and returns the top N.
    """
    import re

    from athenaeum.models import parse_frontmatter

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    needle_companies = [c.lower() for c in args.company if c]
    required_tags = list(args.tag)
    if args.tier:
        required_tags.append(f"tier:{args.tier}")

    title_regexes = [
        re.compile(p, re.IGNORECASE) for p in (args.title_regex or []) if p
    ]
    company_regexes = [
        re.compile(p, re.IGNORECASE) for p in (args.company_regex or []) if p
    ]

    rows: list[dict] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        if not meta or meta.get("type") != "person":
            continue

        tags_raw = meta.get("tags") or []
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        if required_tags and not all(t in tags for t in required_tags):
            continue

        company_fields = [
            str(meta.get("current_company") or ""),
            str(meta.get("linkedin_company_at_connect") or ""),
        ]
        if needle_companies:
            blob = " ".join(company_fields).lower()
            if not all(needle in blob for needle in needle_companies):
                continue
        if company_regexes:
            company_blob = " ".join(company_fields)
            if not all(rx.search(company_blob) for rx in company_regexes):
                continue

        title_fields = [
            str(meta.get("current_title") or ""),
            str(meta.get("linkedin_position_at_connect") or ""),
        ]
        if title_regexes:
            title_blob = " ".join(title_fields)
            if not all(rx.search(title_blob) for rx in title_regexes):
                continue

        try:
            warm_score = float(meta.get("warm_score") or 0)
        except (TypeError, ValueError):
            warm_score = 0.0
        try:
            meeting_count = int(meta.get("meeting_count_24mo") or 0)
        except (TypeError, ValueError):
            meeting_count = 0
        try:
            sent_count = int(meta.get("sent_count_24mo") or 0)
        except (TypeError, ValueError):
            sent_count = 0

        title = (
            meta.get("current_title") or meta.get("linkedin_position_at_connect") or ""
        )
        company = (
            meta.get("current_company") or meta.get("linkedin_company_at_connect") or ""
        )
        rows.append(
            {
                "name": str(meta.get("name") or ""),
                "current_title": str(title),
                "current_company": str(company),
                "warm_score": warm_score,
                "meeting_count_24mo": meeting_count,
                "sent_count_24mo": sent_count,
                "touch_score": meeting_count * 3 + sent_count,
                "last_touch": str(meta.get("last_touch") or ""),
                "uid": str(meta.get("uid") or ""),
                "path": path.name,
            }
        )

    if args.top_touch:
        rows.sort(key=lambda r: -r["touch_score"])
        rows = rows[: args.top_touch]
    else:
        rows.sort(key=lambda r: -r["warm_score"])
        if args.limit > 0:
            rows = rows[: args.limit]

    if args.format == "tsv":
        for r in rows:
            print(
                "\t".join(
                    str(r[k])
                    for k in (
                        "name",
                        "current_title",
                        "current_company",
                        "warm_score",
                        "meeting_count_24mo",
                        "sent_count_24mo",
                        "last_touch",
                        "uid",
                        "path",
                    )
                )
            )
        return 0

    if not rows:
        print("(no matches)")
        return 0

    name_w = max(len(r["name"]) for r in rows)
    title_w = max(len(r["current_title"][:40]) for r in rows) or 1
    company_w = max(len(r["current_company"][:30]) for r in rows) or 1
    print(
        f"{'name':{name_w}}  {'title':{title_w}}  "
        f"{'company':{company_w}}  score   touch  last_touch"
    )
    for r in rows:
        print(
            f"{r['name']:{name_w}}  "
            f"{r['current_title'][:40]:{title_w}}  "
            f"{r['current_company'][:30]:{company_w}}  "
            f"{r['warm_score']:>6.1f}  "
            f"{r['touch_score']:>5}  "
            f"{r['last_touch']}"
        )
    print(f"\n{len(rows)} match(es)")
    return 0


def _cmd_stopwords(_args: argparse.Namespace) -> int:
    """Print the canonical stopword list, one word per line, sorted."""
    from athenaeum.search import STOPWORDS

    for word in STOPWORDS:
        print(word)
    return 0


def _cmd_query_topics(args: argparse.Namespace) -> int:
    """Print extracted topics, one per line. Empty output = fall back."""
    from athenaeum.config import load_config
    from athenaeum.query_topics import extract_topics

    # Issue #232: load the operator's yaml so ``models.topic`` reaches the
    # call. --knowledge-root covers non-default roots; when omitted,
    # load_config falls back to ~/knowledge.
    knowledge_root = (
        args.knowledge_root.expanduser().resolve()
        if args.knowledge_root is not None
        else None
    )
    config = load_config(knowledge_root)
    for topic in extract_topics(args.prompt, timeout=args.timeout, config=config):
        print(topic)
    return 0


def _cmd_test_mcp(args: argparse.Namespace) -> int:
    """Smoke-test the MCP remember/recall round-trip without a live session.

    MCP tools are only callable from within a running Claude Code session
    (the tool list is established at session start). This command exercises
    the underlying functions directly against a synthetic knowledge dir so
    users can verify their athenaeum install works before relying on it.

    Steps:
      1. remember_write  — appends a test observation to raw/
      2. recall_search   — keyword search against a seeded wiki page
      3. create_server   — verifies FastMCP is importable and the server
                           factory returns a configured instance
    """
    import shutil
    import tempfile

    from athenaeum.mcp_server import recall_search, remember_write

    tmp_root = Path(tempfile.mkdtemp(prefix="athenaeum-test-mcp-"))
    raw_root = tmp_root / "raw"
    wiki_root = tmp_root / "wiki"
    raw_root.mkdir()
    wiki_root.mkdir()

    (wiki_root / "test-page.md").write_text(
        "---\n"
        "name: Athenaeum Test Page\n"
        "tags: [smoke-test]\n"
        "description: Seeded page used by `athenaeum test-mcp` to exercise recall.\n"
        "---\n\n"
        "This page contains the keyword ATHENAEUMSMOKETEST for recall verification.\n"
    )

    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    def _record(name: str, ok: bool, detail: str = "") -> None:
        if ok:
            passed.append(name)
            print(f"  PASS  {name}")
        else:
            failed.append((name, detail))
            print(f"  FAIL  {name}: {detail}", file=sys.stderr)

    print(f"Testing athenaeum MCP setup (temp dir: {tmp_root})")

    try:
        result = remember_write(
            raw_root,
            "Smoke test observation from `athenaeum test-mcp`.",
            source="test-mcp",
            # Declare per-claim provenance so the smoke test itself doesn't
            # trip the issue-#90 "no `sources` supplied" warning.
            sources="cli:athenaeum-test-mcp",
        )
        written = list((raw_root / "test-mcp").glob("*.md"))
        ok = result.startswith("Saved to ") and len(written) == 1
        _record("remember_write", ok, f"unexpected result: {result!r}")

        result = recall_search(wiki_root, "ATHENAEUMSMOKETEST", top_k=3)
        ok = "Athenaeum Test Page" in result
        _record("recall_search (keyword)", ok, f"no match in: {result[:200]!r}")

        try:
            from athenaeum.mcp_server import create_server

            server = create_server(raw_root=raw_root, wiki_root=wiki_root)
            ok = server is not None and hasattr(server, "run")
            _record("create_server (FastMCP)", ok, "factory returned unusable object")
        except ImportError as exc:
            _record(
                "create_server (FastMCP)",
                False,
                f"FastMCP not installed: {exc}. Install with: pip install athenaeum[mcp]",
            )
    finally:
        if args.keep:
            print(f"\nTemp dir preserved at: {tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


def _cmd_repair(args: argparse.Namespace) -> int:
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

    knowledge_root = (args.knowledge_root or Path("~/knowledge")).expanduser().resolve()
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
        typed = LEGACY_SLUG_MAP_LOOKUP(slug)
        print(f"    {slug} -> {typed}: {count} wikis")

    for path, err in report.errors[:20]:
        print(f"  ERR {path.name}: {err}", file=sys.stderr)

    if report.errors:
        return 1
    if not apply and report.would_rewrite > 0:
        return 2
    return 0


def LEGACY_SLUG_MAP_LOOKUP(slug: str) -> str:
    """Look up a slug in :data:`athenaeum.repair.LEGACY_SLUG_MAP` for output.

    Helper to keep the import surface inside ``_cmd_repair_legacy_slugs``
    minimal. Returns the raw slug if not present (defensive — should never
    happen because the runner already filtered unknowns).
    """
    from athenaeum.repair import LEGACY_SLUG_MAP

    return LEGACY_SLUG_MAP.get(slug, slug)


def _get_version() -> str:
    from athenaeum import __version__

    return __version__


if __name__ == "__main__":
    sys.exit(main())
