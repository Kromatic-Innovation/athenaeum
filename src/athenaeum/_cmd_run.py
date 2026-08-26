# SPDX-License-Identifier: Apache-2.0
"""``athenaeum run`` — execute the librarian pipeline.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this
is where a NEW subcommand goes, not inline in ``cli.py``'s ``main()``. This
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import cost
down.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args, _positive_int
from athenaeum.logconf import configure_logging


def add_run_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum run`` and its flags on *subparsers*."""
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
        "--max-runtime",
        type=int,
        default=None,
        help=(
            "Run-level wall-clock deadline in seconds (issue athenaeum#396). On trip "
            "the run commits partial progress, releases the lock, and exits "
            "75 (EXIT_GRACEFUL_PARTIAL, resumable; issue athenaeum#897 — 124 is "
            "reserved for an external kill, e.g. coreutils timeout, and is never "
            "returned by this internal check) — bounding the WHOLE run incl. the "
            "post-compile phases, not just the per-file loop. Default: "
            "ATHENAEUM_MAX_RUNTIME env, then athenaeum.yaml librarian.max_runtime, "
            "then 3600. Pass 0 (or a negative value) to disable the deadline "
            "(unbounded run). Full exit-code contract: docs/exit-codes.md."
        ),
    )
    run_parser.add_argument(
        "--strict-budget",
        action="store_true",
        help="Exit nonzero (1) when the run trips the API call budget "
        "(the DEGRADED path) instead of the default 0. Opt-in, for "
        "exit-code-based alerting; the warning summary and deferred-work "
        "manifest are written either way. Broader than a zero-progress "
        "refusal (fires on ANY deferral, not just a zero-files one) and "
        "wins if both this and --allow-degraded are set.",
    )
    run_parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="Exit 0 even when the run stopped early for a resource reason "
        "(budget / spend-ceiling / entity-share) AND committed ZERO files "
        "-- the athenaeum#1135 DEGRADED REFUSAL, which otherwise exits "
        "EXIT_LIBRARIAN_REFUSAL (3) by default so a cron wrapper can tell "
        "'compiled nothing' apart from success by exit code alone. The "
        "'librarian-run-degraded reason=... files=0 ...' marker line is "
        "still logged at ERROR either way -- this flag controls only the "
        "exit code, not the log line. Opt-in escape hatch for a deliberate "
        "deterministic-phases-only / budget-starved run. --strict-budget "
        "takes precedence if both are set (see its help). Full exit-code "
        "contract: docs/exit-codes.md.",
    )
    run_parser.add_argument(
        "--batch-mode",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Submit tier-2/tier-3 LLM calls via the Anthropic Messages "
        "Batch API at a 50%% token discount (issue athenaeum#236). Latency-tolerant: "
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
        help="Skip the move-then-retire pass (issue athenaeum#261): raw auto-memory "
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
        "invoke `git push` on the knowledge repo (issue athenaeum#284) using the "
        "operator's ambient git credentials. Overrides the athenaeum.yaml "
        "librarian.push_after_run toggle (default off). No-op on --dry-run "
        "or when the run produced no commits. A push failure is reported "
        "as a non-fatal warning; commits remain local and the next run "
        "retries (`git push` is idempotent).",
    )
    run_parser.add_argument(
        "--pull",
        dest="pull_before_run",
        action="store_true",
        default=None,
        help="Before the run starts, invoke `git pull --ff-only --autostash` "
        "on the knowledge repo (issue athenaeum#399) using the operator's ambient "
        "git credentials, so the run compiles against origin's latest. "
        "Overrides the athenaeum.yaml librarian.pull_before_run toggle "
        "(default off). No-op on --dry-run. A pull failure (e.g. diverged "
        "history that --ff-only rejects) is reported as a non-fatal "
        "warning; the run proceeds against the local tree.",
    )
    run_parser.add_argument(
        "--full-compile",
        action="store_true",
        help="Force a whole-corpus auto-memory compile this run, bypassing "
        "both the delta gate and the librarian.full_compile_every_days "
        "cadence (issue athenaeum#463). Use for an immediate full reconciliation "
        "(e.g. after suspecting delta drift) without waiting for the "
        "periodic backstop.",
    )
    run_parser.add_argument(
        "--full-contradiction-sweep",
        action="store_true",
        help="Force C4 (contradiction detection) over EVERY cluster this "
        "run, regardless of the delta gate or --full-compile's own "
        "cadence, and advance the contradiction-sweep-completed stamp "
        "(issue athenaeum#909). Distinct from --full-compile: this forces "
        "only C4, not a full C2 re-cluster. The explicit escape hatch — "
        "absent this flag, a full-corpus contradiction sweep never runs "
        "implicitly.",
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
    run_parser.set_defaults(func=cmd_run)


def cmd_run(args: argparse.Namespace) -> int:
    from athenaeum.librarian import DEFAULT_KNOWLEDGE_ROOT, run

    configure_logging(verbose=getattr(args, "verbose", False))

    knowledge_root = args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT
    raw_root = args.raw_root or (knowledge_root / "raw")
    wiki_root = args.wiki_root or (knowledge_root / "wiki")

    # Issue athenaeum#309: a --dry-run reads nothing mutating, so it does NOT take the
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
            max_runtime=args.max_runtime,
            cluster_only=getattr(args, "cluster_only", False),
            merge_only=getattr(args, "merge_only", False),
            strict_budget=args.strict_budget,
            allow_degraded=args.allow_degraded,
            batch_mode=args.batch_mode,
            retire=getattr(args, "retire", None),
            push_after_run=getattr(args, "push_after_run", None),
            pull_before_run=getattr(args, "pull_before_run", None),
            full_compile=getattr(args, "full_compile", False),
            full_contradiction_sweep=getattr(
                args, "full_contradiction_sweep", False
            ),
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
            max_runtime=args.max_runtime,
            cluster_only=getattr(args, "cluster_only", False),
            merge_only=getattr(args, "merge_only", False),
            strict_budget=args.strict_budget,
            allow_degraded=args.allow_degraded,
            batch_mode=args.batch_mode,
            retire=getattr(args, "retire", None),
            push_after_run=getattr(args, "push_after_run", None),
            pull_before_run=getattr(args, "pull_before_run", None),
            install_signal_handlers=True,
            full_compile=getattr(args, "full_compile", False),
            full_contradiction_sweep=getattr(
                args, "full_contradiction_sweep", False
            ),
            # Issue athenaeum#526 (H10): thread the run lock's heartbeat into the
            # librarian so its per-phase/per-file loop refreshes the lockfile's
            # heartbeat — making heartbeat_age_seconds report progress age, not
            # acquire age, so a healthy long run is never auto-broken.
            heartbeat=lock.heartbeat,
            # Issue athenaeum#712: also thread the lock itself — the finalize
            # phase's verdict-ledger advisor reuses it (single-appender) rather
            # than acquiring a second one. Only ever consulted when
            # librarian.verdict_ledger_enabled is on.
            lock=lock,
        )
    finally:
        lock.release()
