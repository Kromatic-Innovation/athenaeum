# SPDX-License-Identifier: Apache-2.0
"""Thin driver for the north-star rollout grid (issue athenaeum#1523).

``python -m tests.evals.north_star_cli`` builds a grid over every probe in
the core corpus x every rollout arm x the requested corpus scales x replicates
(:func:`tests.evals.containment.build_grid`), prices it through the SAME
pre-flight spend gate ``tests/evals/containment_cli.py`` uses
(:func:`tests.evals.containment.price_grid` — issue athenaeum#1523's
explicit instruction: route spend through the existing gate, never a new
one), runs each not-yet-completed (probe, corpus_scale, replicate) group
through :func:`tests.evals.rollout.run_probe_all_arms`, persists every arm's
:class:`~tests.evals.rollout.RolloutRecord` via
:func:`tests.evals.north_star_report.append_rollout_row`, then renders and
writes the markdown report under ``measurements/``.

**Resume granularity is the (probe, corpus_scale, replicate) SUPER-group,
not the individual arm cell.** ``run_probe_all_arms`` produces every
arm's records in one call — there is no "run just this one arm" entry
point — so a group is treated as done only when EVERY one of its arm
cell-keys is already present in the store; otherwise the whole group reruns
and every one of its four rows is appended fresh. This is a deliberate
simplification over ``tests.evals.containment.run_grid``'s per-cell resume
contract (which this module does not call directly, for exactly this
reason): re-running per individual arm cell here would either re-derive
already-completed arms wastefully, or — worse — double-append a cell key
that survived a partial group, which ``ResultStore`` never de-duplicates.

Issue athenaeum#1751 made that group the unit of CONCURRENCY as well as of
resume: ``--workers`` (default 4) groups run at a time on a thread pool,
because a serial ``--scale full`` grid is 1392 cells and could never finish
inside the workflow job's window. See :func:`_run_cells` for the three
pieces of shared state that makes safe and how each is handled.

``--dry-run`` makes **zero paid calls**: it prices the grid, prints the
projection -- including the projected WALL CLOCK at the chosen worker
count, so an operator can tell before dispatching whether a run fits the
job's ``timeout-minutes``, and (issue athenaeum#1754) the TOKEN CEILING
that will apply beside the projected token total, REFUSING up front when
the projection exceeds it rather than aborting mid-grid -- and returns
before constructing a client,
spawning ``claude -p``, or running a single cell — see
``test_north_star_cli.py::test_dry_run_never_constructs_a_client``, which
mirrors ``tests/test_shadow_parity.py``'s ``TestDryRunZeroCalls`` (issue
athenaeum#1333 AC5) pattern.

Issue athenaeum#1733: ``evals.yml`` gained a dedicated ``workflow_dispatch``
input that DOES invoke this module (manually, never on push -- see that
workflow's ``north-star`` job) to run the grid in api mode with the key the
workflow already loads. ``ci.yml`` still never touches it, and neither
workflow selects the ``rollout``/``containment`` pytest markers this module's
own machinery carries -- ``tests/evals/test_containment_ci_wiring.py``
asserts that half unchanged.
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import tempfile
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tests.evals.containment import (
    DEFAULT_CELL_SECONDS,
    NORTH_STAR_CELL_TOKEN_ESTIMATE,
    SCALE_BUDGETS,
    GridCell,
    ResultStore,
    SpendCeilingExceededError,
    build_grid,
    format_duration,
    price_grid,
    project_wall_clock_seconds,
    read_planned_cells,
    tokens_for_spend,
    write_planned_cells,
)
from tests.evals.corpus import SCALES, build_corpus
from tests.evals.harness import EvalSession
from tests.evals.north_star_report import (
    DEFAULT_MEASUREMENTS_DIR,
    DEFAULT_VERDICT_ARM,
    append_rollout_row,
    build_report,
    load_rollout_rows_and_diagnostics,
    write_report,
)
from tests.evals.rollout import ALL_ARMS, DEFAULT_ROLLOUT_MODEL, run_probe_all_arms
from tests.evals.rollout_session import ROLLOUT_TOKEN_CEILING, assert_rollout_ceiling

#: Every probe in the hand-authored corpus -- probes are IDENTICAL across
#: corpus scales (``tests.evals.corpus.build_corpus``'s own docstring: "every
#: scale answers the same probes against the same ground truth"), so the
#: core scale is enough to enumerate the full probe id list.
DEFAULT_PROBES: tuple[str, ...] = tuple(p.id for p in build_corpus("core").probes)

#: Every rollout arm, always -- the report's dimensions inherently compare
#: across every arm in ``tests.evals.rollout.ALL_ARMS`` (issue athenaeum#1574
#: grew this from four to six), so this driver does not expose an --arms
#: knob to run a subset.
DEFAULT_ARMS: tuple[str, ...] = tuple(arm.value for arm in ALL_ARMS)

#: Scales a blank ``--corpus-scales`` must NOT silently pull in (issue
#: athenaeum#1735). A ``NATIVE_GREP`` cell over ``xlarge`` (25,000 pages) is
#: the most expensive cell in the grid, so the rollout treats it as opt-in --
#: pass it explicitly via ``--corpus-scales xlarge`` (or the workflow's
#: ``north_star_corpus_scales`` dispatch input) rather than by default.
_OPT_IN_CORPUS_SCALES: frozenset[str] = frozenset({"xlarge"})

DEFAULT_CORPUS_SCALES: tuple[str, ...] = tuple(
    sorted(s for s in SCALES if s not in _OPT_IN_CORPUS_SCALES)
)
DEFAULT_REPLICATES: tuple[int, ...] = (0,)

#: Mirrors ``containment_cli.DEFAULT_MAX_SPEND_USD`` -- the ``smoke`` scale
#: (always exactly 1 cell) must never self-refuse at its own default; see
#: ``test_north_star_cli.py::test_default_max_spend_covers_smoke_scale``.
DEFAULT_MAX_SPEND_USD = 1.0

#: Default concurrency for the cell loop (issue athenaeum#1751). Four, not
#: "as many as there are cores": the work is network-bound on one Anthropic
#: account, and each worker holds its own materialized corpus tree plus
#: search index on disk (see :func:`_run_cells`), so the cost of another
#: worker is real even though the CPU is idle. Four is the number the
#: workflow's own dispatch input defaults to; raise both together.
DEFAULT_WORKERS = 4


def resolve_max_spend(args: argparse.Namespace) -> float:
    """``--max-spend``, or its default when the flag was omitted.

    The flag parses to ``None`` rather than straight to
    :data:`DEFAULT_MAX_SPEND_USD` so :func:`resolve_token_ceiling` can tell
    "the operator authorized a dollar figure" from "nobody said anything" --
    the two want different token ceilings.
    """
    return DEFAULT_MAX_SPEND_USD if args.max_spend is None else args.max_spend


def resolve_token_ceiling(args: argparse.Namespace) -> tuple[int, str]:
    """The token ceiling this run enforces, and where it came from (athenaeum#1754).

    Precedence, most explicit first:

    1. ``--max-tokens`` -- an operator naming the ceiling directly.
    2. ``--max-spend`` -- the ceiling that many dollars buys at ``--model``'s
       rate and :data:`NORTH_STAR_CELL_TOKEN_ESTIMATE`'s input/output mix.
       This is the point of the issue: the USD knob the operator already
       sets governs the token guard too, so a run dispatched with
       ``--max-spend 75`` cannot die at a 2,000,000-token constant with $73
       still authorized.
    3. :data:`ROLLOUT_TOKEN_CEILING` -- the fallback when neither was given,
       and now ONLY that.

    The constant is read through the module global on purpose, so a test
    monkeypatching ``north_star_cli.ROLLOUT_TOKEN_CEILING`` still moves the
    fallback.
    """
    if args.max_tokens is not None:
        return args.max_tokens, "--max-tokens"
    if args.max_spend is not None:
        derived = tokens_for_spend(
            args.max_spend,
            model=args.model,
            per_cell=NORTH_STAR_CELL_TOKEN_ESTIMATE,
        )
        if derived > 0:
            return derived, f"derived from --max-spend ${args.max_spend:.2f} at {args.model}"
    return (
        ROLLOUT_TOKEN_CEILING,
        "ROLLOUT_TOKEN_CEILING default (neither --max-tokens nor --max-spend given)",
    )


def _default_store_path() -> Path:
    """System temp dir, never the repo tree -- a separate function (not a
    module constant) so tests can monkeypatch it for isolation."""
    return Path(tempfile.gettempdir()) / "athenaeum-north-star" / "results.jsonl"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.evals.north_star_cli",
        description=__doc__,
    )
    parser.add_argument(
        "--scale",
        choices=sorted(SCALE_BUDGETS),
        default="smoke",
        help="run-level budget tier (default: smoke)",
    )
    parser.add_argument(
        "--max-spend",
        type=float,
        default=None,
        help=f"refuse to start above this priced USD total (default: ${DEFAULT_MAX_SPEND_USD:.2f})",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help=(
            "token ceiling for THIS run, overriding the compiled-in "
            f"{ROLLOUT_TOKEN_CEILING} (issue athenaeum#1754). Omitted, the ceiling is "
            "derived from --max-spend at --model's price, so one knob governs both; "
            "the constant applies only when neither flag is given."
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_ROLLOUT_MODEL,
        help="model for both the single-shot arms and PULL's claude -p spawn",
    )
    parser.add_argument(
        "--mode",
        choices=("api", "cli"),
        default=os.environ.get("ATHENAEUM_EVAL_MODE", "api"),
        help=(
            "execution path for the four tool-using arms (issue athenaeum#1733): "
            "'api' (default) drives an Anthropic Messages API tool-use loop and needs "
            "no logged-in claude CLI; 'cli' spawns claude -p as the fidelity spot-check. "
            "Falls back to the ATHENAEUM_EVAL_MODE env var, then 'api'."
        ),
    )
    parser.add_argument(
        "--probes",
        default=None,
        help="comma-separated probe ids (default: every probe in the core corpus)",
    )
    parser.add_argument(
        "--corpus-scales",
        default=None,
        help=f"comma-separated corpus scales (default: {','.join(DEFAULT_CORPUS_SCALES)})",
    )
    parser.add_argument(
        "--replicates",
        default="0",
        help="comma-separated replicate indices (default: 0)",
    )
    parser.add_argument(
        "--search-backend",
        default="fts5",
        help="retrieval backend for PUSH/PULL indexing (default: fts5)",
    )
    parser.add_argument("--claude-binary", default="claude")
    parser.add_argument("--store", type=Path, default=None, help="append-only JSONL result path")
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_MEASUREMENTS_DIR, help="report output directory"
    )
    parser.add_argument(
        "--materialize-root",
        type=Path,
        default=None,
        help="where corpus trees are materialized (default: a fresh system-temp dir)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            "how many (probe, corpus_scale, replicate) groups run concurrently "
            f"(default: {DEFAULT_WORKERS}). 1 restores the strictly-serial loop."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="price the grid and print the projection; makes zero paid calls",
    )
    parser.add_argument(
        "--verdict-arm",
        default=DEFAULT_VERDICT_ARM,
        help=(
            "the ONE Athenaeum arm the design-doc §7 decision block reads (ruling R1) -- "
            f"default: {DEFAULT_VERDICT_ARM!r}, the shipped configuration. Other arms still "
            "appear in the report's per-dimension tables, never in the verdicts."
        ),
    )
    return parser


#: A single-value placeholder for the arms axis of ``build_grid``'s cap.
#: This driver's groups always need ALL real arms at once
#: (``run_probe_all_arms`` has no "just this one arm" entry point -- see
#: the module docstring's "Resume granularity" note), so letting
#: ``SCALE_BUDGETS`` cap the arms axis the way ``containment_cli.py`` caps
#: it would silently pay for all arms per selected group while
#: persisting only the capped subset. Grid SELECTION therefore runs over
#: this one-value placeholder (so probes/corpus_scales/replicates are still
#: capped per ``--scale`` exactly like ``containment_cli.py`` does), and
#: every selected cell is expanded to its real arm cells in
#: :func:`_build_cells`, AFTER selection.
_ARM_AXIS_PLACEHOLDER: tuple[str, ...] = ("_all_arms",)


def _build_cells(args: argparse.Namespace) -> list[GridCell]:
    probes = args.probes.split(",") if args.probes else list(DEFAULT_PROBES)
    corpus_scales = (
        args.corpus_scales.split(",") if args.corpus_scales else list(DEFAULT_CORPUS_SCALES)
    )
    # Validated HERE, at parse time, rather than left to fail deep inside a
    # rollout once a cell for an unknown scale reaches build_corpus() --
    # issue athenaeum#1735's AC3 ("evals.yml's grid dispatch input accepts
    # xlarge") is only real if a typo'd scale is caught before any spend,
    # not after. `--corpus-scales` (and the workflow's free-text
    # `north_star_corpus_scales` input) accept an arbitrary string, so
    # nothing upstream of this call validates membership in SCALES.
    unknown = [s for s in corpus_scales if s not in SCALES]
    if unknown:
        raise ValueError(
            f"unknown corpus scale(s) {unknown!r} in --corpus-scales; known: {sorted(SCALES)}"
        )
    replicates = [int(r) for r in args.replicates.split(",")]
    placeholder_cells = build_grid(
        args.scale,
        probes=probes,
        arms=list(_ARM_AXIS_PLACEHOLDER),
        corpus_scales=corpus_scales,
        replicates=replicates,
    )
    return [
        GridCell(probe=c.probe, arm=arm, corpus_scale=c.corpus_scale, replicate=c.replicate)
        for c in placeholder_cells
        for arm in DEFAULT_ARMS
    ]


def _group_count(cells: Sequence[GridCell]) -> int:
    """How many (probe, corpus_scale, replicate) groups *cells* form.

    The grid's unit of concurrency, and therefore the real ceiling on how
    many workers can ever be busy at once -- see :func:`_run_cells`, whose
    pool is clamped to exactly this.
    """
    return len({(cell.probe, cell.corpus_scale, cell.replicate) for cell in cells})


def _run_cells(
    cells: Sequence[GridCell],
    *,
    store: ResultStore,
    session: EvalSession,
    materialize_root: Path,
    model: str,
    search_backend: str,
    claude_binary: str,
    mode: str,
    workers: int = 1,
    token_ceiling: int | None = None,
) -> None:
    """Group *cells* by (probe, corpus_scale, replicate) and run each
    not-yet-complete group through :func:`run_probe_all_arms` exactly once
    -- see the module docstring's "Resume granularity" note.

    The group, not the individual arm cell, is also the unit of CONCURRENCY
    (issue athenaeum#1751): *workers* groups run at a time on a thread pool.
    Three pieces of shared state make that safe, and each is handled
    deliberately rather than by hoping the GIL covers it:

    * **The materialized corpus tree.** ``run_probe_all_arms`` writes a wiki
      tree, a search index and a hook ``HOME`` under the ``materialize_root``
      it is handed, and every one of those derives from that single path. Two
      groups at the same ``(corpus_scale, replicate)`` used to share one
      directory -- harmless serially (each call rewrites identical bytes),
      a genuine data race in parallel, where one worker's index build can
      read another's half-written page. Each worker therefore gets its OWN
      ``w<slot>/`` prefix, taken from a slot pool for the duration of a
      group and returned in a ``finally``. Slots are pooled rather than
      derived from a thread id (which would collide) and bounded by
      *workers*, so disk cost is ``workers`` x the number of distinct
      ``(corpus_scale, replicate)`` pairs a worker happens to visit -- not
      one tree per group, which is the growth this bounds.
    * **The result store.** One ``ResultStore`` INSTANCE is shared, so its
      own append lock actually serialises writers; a second instance over
      the same path would defeat it.
    * **The token ledger and its ceiling.** ``EvalSession``'s counters are
      lock-guarded (see ``harness.EvalSession.__init__``), and the ceiling
      is re-checked under *ledger_lock* after every group. Once it trips,
      ``stop`` is set and every group still queued returns without running
      -- and ``run_probe_all_arms`` is handed ``stop.is_set`` so a group
      ALREADY in flight stops at its next arm boundary rather than running
      out its remaining arms. The ceiling therefore stops every worker
      within one cell each, not one group each.

    An interrupt (Ctrl-C, or the job being killed) cancels every group that
    has not started, sets ``stop`` for the ones that have, and lets the
    exception out so :func:`main` writes the PARTIAL report over whatever
    the store already holds.
    """
    if workers < 1:
        raise ValueError(f"--workers must be >= 1, got {workers}")
    # ``None`` means "nobody resolved one" -- the module constant, read here
    # rather than captured at def time so a monkeypatched global still moves
    # it (issue athenaeum#1754).
    effective_ceiling = ROLLOUT_TOKEN_CEILING if token_ceiling is None else token_ceiling
    already_done = store.completed_keys()
    groups: dict[tuple[str, str, int], list[GridCell]] = {}
    for cell in cells:
        groups.setdefault((cell.probe, cell.corpus_scale, cell.replicate), []).append(cell)

    pending = [
        (group_key, group_cells)
        for group_key, group_cells in groups.items()
        if not {c.cell_key() for c in group_cells} <= already_done
    ]
    if not pending:
        return

    effective_workers = min(workers, len(pending))
    slots: queue.Queue[int] = queue.Queue()
    for slot in range(effective_workers):
        slots.put(slot)
    ledger_lock = threading.Lock()
    stop = threading.Event()

    def _run_group(
        group_key: tuple[str, str, int], group_cells: list[GridCell]
    ) -> None:
        if stop.is_set():
            return
        probe_id, corpus_scale, replicate = group_key
        slot = slots.get()
        try:
            records = run_probe_all_arms(
                probe_id,
                corpus_scale,
                session=session,
                materialize_root=(
                    materialize_root / f"w{slot}" / f"{corpus_scale}-{replicate}"
                ),
                model=model,
                search_backend=search_backend,
                claude_binary=claude_binary,
                replicate=replicate,
                mode=mode,
                should_stop=stop.is_set,
            )
        finally:
            slots.put(slot)
        with ledger_lock:
            for cell in group_cells:
                append_rollout_row(store, cell, records[cell.arm])
            total_tokens = session.input_tokens + session.output_tokens
            if total_tokens > effective_ceiling:
                # Set the flag BEFORE raising: every group still queued
                # must see it and return without spending, which is what
                # makes this stop ALL workers rather than only this one.
                # ``SpendCeilingExceededError`` is reused rather than
                # invented anew -- it already names "the run is refusing to
                # spend more"; the only difference from its pre-flight use
                # is that here some cells have already run, which is why
                # main renders a PARTIAL report instead of exiting before
                # the store exists.
                stop.set()
                raise SpendCeilingExceededError(
                    f"rollout run exceeded token ceiling ({total_tokens} > "
                    f"{effective_ceiling}) mid-grid -- stopped every worker. "
                    "Groups not yet started never run, and a group already in "
                    "flight stops at its next arm boundary, so the overshoot is "
                    "at most one cell per worker. Shrink the grid or the --scale "
                    "tier, or raise the ceiling deliberately with --max-tokens "
                    "(or a larger --max-spend, which derives it)."
                )

    if effective_workers == 1:
        for group_key, group_cells in pending:
            _run_group(group_key, group_cells)
        return

    # Not a `with` block: the interrupt path below needs
    # ``cancel_futures=True``, which ``ThreadPoolExecutor.__exit__`` does
    # not pass.
    pool = ThreadPoolExecutor(max_workers=effective_workers)
    cancel_pending_groups = False
    try:
        futures = [pool.submit(_run_group, key, group) for key, group in pending]
        for future in as_completed(futures):
            # Re-raise the FIRST worker failure here rather than at pool
            # teardown, so main's own handler renders the partial report
            # with that exception's message as the abort reason.
            #
            # Raising here loses no completed work, unlike in
            # ``containment.run_grid``: a group appends its own rows before
            # returning, so a result that exists has already been persisted
            # and there is nothing sitting in an unpulled future to drop.
            future.result()
    except (KeyboardInterrupt, SystemExit):
        # Ctrl-C, or the job being killed. ``stop`` FIRST and
        # ``cancel_futures`` second, because cancellation only reaches
        # groups that have not started -- a group already running needs the
        # flag to return at its next arm boundary. Then let the exception
        # out, so main writes the PARTIAL report over whatever the store
        # already holds.
        stop.set()
        cancel_pending_groups = True
        raise
    finally:
        # An ordinary per-group failure is NOT cancelled: queued groups
        # should still run, because the store's resume contract wants every
        # group that CAN complete to complete. Only an interrupt (above) or
        # a ceiling trip (via `stop`) suppresses them.
        pool.shutdown(wait=True, cancel_futures=cancel_pending_groups)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.workers < 1:
        print(f"--workers must be >= 1, got {args.workers}", file=sys.stderr)
        return 1
    cells = _build_cells(args)
    max_spend = resolve_max_spend(args)
    token_ceiling, ceiling_source = resolve_token_ceiling(args)
    projected_tokens = len(cells) * NORTH_STAR_CELL_TOKEN_ESTIMATE.total_tokens

    # Printed BEFORE pricing, and therefore on the refusal path too (issue
    # athenaeum#1751): the operator question "does a full dispatch fit the
    # job's timeout window?" is asked precisely when the grid is big enough
    # to be refused at the default ceiling, and the projection needs only
    # the cell count and the worker count -- never a successful price.
    if args.dry_run:
        # Clamped to the group count, exactly as ``_run_cells`` clamps its
        # own pool: asking for 64 workers on a grid of 3 groups buys three
        # workers' worth of speed, and a projection that divided by 64
        # would promise a run 20x faster than anything that can happen.
        effective_workers = min(args.workers, max(_group_count(cells), 1))
        projected = project_wall_clock_seconds(len(cells), workers=effective_workers)
        print(
            f"projected wall clock: {format_duration(projected)} "
            f"for {len(cells)} cells at {effective_workers} workers "
            f"(~{DEFAULT_CELL_SECONDS:.0f}s/cell estimate)"
        )
        # Beside the wall clock and (below) the price, because those are the
        # three numbers an operator sizing a dispatch compares -- and, like
        # the wall clock, printed before pricing so the price-refusal path
        # shows it too (issue athenaeum#1754).
        print(
            f"token ceiling: {token_ceiling} ({ceiling_source}); "
            f"projected {projected_tokens} tokens for {len(cells)} cells "
            f"(~{NORTH_STAR_CELL_TOKEN_ESTIMATE.total_tokens} tokens/cell estimate)"
        )

    try:
        estimate = price_grid(
            cells,
            model=args.model,
            max_spend_usd=max_spend,
            per_cell=NORTH_STAR_CELL_TOKEN_ESTIMATE,
        )
    except SpendCeilingExceededError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"scale={args.scale} cells={estimate.cell_count} "
        f"estimated=${estimate.estimated_usd:.4f} model={args.model}"
    )

    if args.dry_run:
        # Refuse HERE, after all three projection lines have printed, so the
        # operator sees the wall clock, the ceiling and the price before the
        # refusal rather than instead of them. A grid that would trip the
        # ceiling mid-run is refused up front -- the failure mode issue
        # athenaeum#1754 exists to end is discovering it 392 cells in.
        if projected_tokens > token_ceiling:
            print(
                f"projected tokens {projected_tokens} exceed the token ceiling "
                f"{token_ceiling} ({ceiling_source}) -- refusing to start. "
                "Shrink --scale or the probe/corpus-scale/replicate lists, or "
                "raise --max-tokens (or --max-spend, which derives it).",
                file=sys.stderr,
            )
            return 1
        print("dry run: zero cells executed, zero paid calls made")
        return 0

    store = ResultStore(args.store if args.store is not None else _default_store_path())
    # Recorded BEFORE the first cell runs, so a run killed mid-grid by the
    # job timeout still leaves the denominator the report's partial banner
    # needs (issue athenaeum#1751).
    write_planned_cells(store, len(cells))
    materialize_root = args.materialize_root or Path(
        tempfile.mkdtemp(prefix="athenaeum-north-star-")
    )
    session = EvalSession()

    aborted = False
    abort_reason = ""
    try:
        _run_cells(
            cells,
            store=store,
            session=session,
            materialize_root=materialize_root,
            model=args.model,
            search_backend=args.search_backend,
            claude_binary=args.claude_binary,
            mode=args.mode,
            workers=args.workers,
            token_ceiling=token_ceiling,
        )
        assert_rollout_ceiling(session, ceiling=token_ceiling)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 -- see below
        # A partial ResultStore (fsync'd per row -- see ResultStore.append)
        # survives even a hard failure mid-grid; report it as PARTIAL rather
        # than losing the rows already persisted or crashing without a
        # report at all.
        #
        # ``KeyboardInterrupt`` is caught alongside ``Exception`` (issue
        # athenaeum#1751) because Ctrl-C is the single most likely way a
        # long grid ends early, and it is a ``BaseException`` -- the
        # previous ``except Exception`` let it past, so the operator who
        # stopped the run got no report over the cells already paid for.
        # ``SystemExit`` is deliberately NOT caught: an explicit exit is a
        # decision to stop, not a failure to report on. ``_run_cells`` has
        # already cancelled every group that had not started.
        aborted = True
        abort_reason = str(exc) or type(exc).__name__

    diagnostics = load_rollout_rows_and_diagnostics(store)
    report = build_report(
        list(diagnostics.rows),
        aborted=aborted,
        abort_reason=abort_reason,
        verdict_arm=args.verdict_arm,
        planned_cells=read_planned_cells(store),
        torn_rows=diagnostics.torn,
        duplicate_rows=diagnostics.duplicates,
    )
    path = write_report(report, out_dir=args.out_dir)
    print(f"report written: {path}")
    return 0 if not aborted else 1


if __name__ == "__main__":
    raise SystemExit(main())
