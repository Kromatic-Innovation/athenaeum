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

``--dry-run`` makes **zero paid calls**: it prices the grid, prints the
projection, and returns before constructing a client, spawning
``claude -p``, or running a single cell — see
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
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from tests.evals.containment import (
    DEFAULT_CELL_TOKEN_ESTIMATE,
    SCALE_BUDGETS,
    GridCell,
    ResultStore,
    SpendCeilingExceededError,
    build_grid,
    price_grid,
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
from tests.evals.rollout_session import assert_rollout_ceiling

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
) -> None:
    """Group *cells* by (probe, corpus_scale, replicate) and run each
    not-yet-complete group through :func:`run_probe_all_arms` exactly once
    -- see the module docstring's "Resume granularity" note."""
    already_done = store.completed_keys()
    groups: dict[tuple[str, str, int], list[GridCell]] = {}
    for cell in cells:
        groups.setdefault((cell.probe, cell.corpus_scale, cell.replicate), []).append(cell)

    for (probe_id, corpus_scale, replicate), group_cells in groups.items():
        group_keys = {c.cell_key() for c in group_cells}
        if group_keys <= already_done:
            continue
        records = run_probe_all_arms(
            probe_id,
            corpus_scale,
            session=session,
            materialize_root=materialize_root / f"{corpus_scale}-{replicate}",
            model=model,
            search_backend=search_backend,
            claude_binary=claude_binary,
            replicate=replicate,
            mode=mode,
        )
        for cell in group_cells:
            append_rollout_row(store, cell, records[cell.arm])


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cells = _build_cells(args)

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
        rows, aborted=aborted, abort_reason=abort_reason, verdict_arm=args.verdict_arm
    )
    path = write_report(report, out_dir=args.out_dir)
    print(f"report written: {path}")
    return 0 if not aborted else 1


if __name__ == "__main__":
    raise SystemExit(main())
