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
job's ``timeout-minutes`` -- and returns before constructing a client,
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
    DEFAULT_CELL_TOKEN_ESTIMATE,
    SCALE_BUDGETS,
    GridCell,
    ResultStore,
    SpendCeilingExceededError,
    build_grid,
    format_duration,
    price_grid,
    project_wall_clock_seconds,
    read_planned_cells,
    write_planned_cells,
)
from tests.evals.corpus import SCALES, build_corpus
from tests.evals.harness import EvalSession
from tests.evals.north_star_report import (
    DEFAULT_MEASUREMENTS_DIR,
    DEFAULT_VERDICT_ARM,
    append_rollout_row,
    build_report,
    load_rollout_rows,
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

DEFAULT_CORPUS_SCALES: tuple[str, ...] = tuple(sorted(SCALES))
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
        default=DEFAULT_MAX_SPEND_USD,
        help=f"refuse to start above this priced USD total (default: ${DEFAULT_MAX_SPEND_USD:.2f})",
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
      *workers*, so disk cost is ``workers`` trees, not one per group.
    * **The result store.** One ``ResultStore`` INSTANCE is shared, so its
      own append lock actually serialises writers; a second instance over
      the same path would defeat it.
    * **The token ledger and its ceiling.** ``EvalSession``'s counters are
      lock-guarded (see ``harness.EvalSession.__init__``), and the ceiling
      is re-checked under *ledger_lock* after every group. Once it trips,
      ``stop`` is set and every group still queued returns without running
      -- so the ceiling stops ALL workers, not just the one that noticed.
      The authoritative refusal is still ``assert_rollout_ceiling`` in
      :func:`main` after the pool drains; ``stop`` only prevents further
      spend once the outcome is already decided.
    """
    if workers < 1:
        raise ValueError(f"--workers must be >= 1, got {workers}")
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
            )
        finally:
            slots.put(slot)
        with ledger_lock:
            for cell in group_cells:
                append_rollout_row(store, cell, records[cell.arm])
            total_tokens = session.input_tokens + session.output_tokens
            if total_tokens > ROLLOUT_TOKEN_CEILING:
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
                    f"{ROLLOUT_TOKEN_CEILING}) mid-grid -- stopped every worker "
                    "before any further cell could spend. Shrink the grid or the "
                    "--scale tier, or raise ROLLOUT_TOKEN_CEILING deliberately."
                )

    if effective_workers == 1:
        for group_key, group_cells in pending:
            _run_group(group_key, group_cells)
        return

    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        futures = [pool.submit(_run_group, key, group) for key, group in pending]
        for future in as_completed(futures):
            # Re-raise the FIRST worker failure here rather than at pool
            # teardown, so main's own handler renders the partial report
            # with that exception's message as the abort reason. Groups
            # still queued behind it are cancelled by the `stop` flag only
            # when the ceiling tripped; an ordinary per-group failure lets
            # the rest finish, which is what the store's resume contract
            # wants (every group that CAN complete should).
            future.result()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.workers < 1:
        print(f"--workers must be >= 1, got {args.workers}", file=sys.stderr)
        return 1
    cells = _build_cells(args)

    # Printed BEFORE pricing, and therefore on the refusal path too (issue
    # athenaeum#1751): the operator question "does a full dispatch fit the
    # job's timeout window?" is asked precisely when the grid is big enough
    # to be refused at the default ceiling, and the projection needs only
    # the cell count and the worker count -- never a successful price.
    if args.dry_run:
        projected = project_wall_clock_seconds(len(cells), workers=args.workers)
        print(
            f"projected wall clock: {format_duration(projected)} "
            f"for {len(cells)} cells at {args.workers} workers "
            f"(~{DEFAULT_CELL_SECONDS:.0f}s/cell estimate)"
        )

    try:
        estimate = price_grid(
            cells,
            model=args.model,
            max_spend_usd=args.max_spend,
            per_cell=DEFAULT_CELL_TOKEN_ESTIMATE,
        )
    except SpendCeilingExceededError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"scale={args.scale} cells={estimate.cell_count} "
        f"estimated=${estimate.estimated_usd:.4f} model={args.model}"
    )

    if args.dry_run:
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
        )
        assert_rollout_ceiling(session)
    except Exception as exc:  # noqa: BLE001 -- must still write a PARTIAL report, never crash bare
        # A partial ResultStore (fsync'd per row -- see ResultStore.append)
        # survives even a hard failure mid-grid; report it as PARTIAL rather
        # than losing the rows already persisted or crashing without a
        # report at all.
        aborted = True
        abort_reason = str(exc)

    rows = load_rollout_rows(store)
    report = build_report(
        rows,
        aborted=aborted,
        abort_reason=abort_reason,
        verdict_arm=args.verdict_arm,
        planned_cells=read_planned_cells(store),
    )
    path = write_report(report, out_dir=args.out_dir)
    print(f"report written: {path}")
    return 0 if not aborted else 1


if __name__ == "__main__":
    raise SystemExit(main())
