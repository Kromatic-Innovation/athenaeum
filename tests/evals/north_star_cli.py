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
import dataclasses
import json
import os
import queue
import sys
import tempfile
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml

from athenaeum.models import TokenUsage
from tests.evals.containment import (
    DEFAULT_CELL_SECONDS,
    NORTH_STAR_CELL_TOKEN_ESTIMATE,
    SCALE_BUDGETS,
    CellTokenEstimate,
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
from tests.evals.corpus import SCALES, build_corpus, generate_core_observations
from tests.evals.harness import EvalSession, build_live_client
from tests.evals.north_star_report import (
    DEFAULT_MEASUREMENTS_DIR,
    DEFAULT_VERDICT_ARM,
    MixedFloorError,
    WriteCost,
    WritePathStats,
    append_rollout_row,
    build_report,
    compute_write_path_stats,
    load_rollout_rows_and_diagnostics,
    write_report,
)
from tests.evals.rollout import (
    ALL_ARMS,
    DEFAULT_ROLLOUT_MODEL,
    run_native_writer_dispatch,
    run_probe_all_arms,
)
from tests.evals.rollout_session import ROLLOUT_TOKEN_CEILING, assert_rollout_ceiling
from tests.evals.write_path import compile_observation_stream

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

#: Issue athenaeum#1787: the ONLY corpus scales a ``--search-backend
#: vector`` dispatch may name -- the backend-fidelity second dispatch is a
#: comparison pass against two representative scales (issue athenaeum#1787's
#: own proposal), not the full 6-scale fts5 grid.
_VECTOR_SCOPED_SCALES: frozenset[str] = frozenset({"core", "medium"})

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

#: Default Phase 2 corpus scale(s) (issue athenaeum#1785): ``"medium"``, not
#: ``"core"``. ``WritePathStats``/``WriteCost`` are keyed by ``(system,
#: corpus_scale)``, and design doc §6 amortises write cost over the probe
#: set at that scale while §7 condition 3 only reads write cost at
#: ``"medium"`` and above (``north_star_report._CUTOFF_ELIGIBLE_SCALES``).
#: A Phase 2 run pinned to ``"core"`` would produce a write-cost figure
#: condition 3 cannot consume. ``generate_core_observations`` itself is
#: scale-invariant (its own docstring: validated, then never consulted) --
#: this default governs only which scale the resulting rows are LABELLED
#: at, which is what the report joins on.
DEFAULT_PHASE2_SCALES: tuple[str, ...] = ("medium",)

#: Both write-path producers Phase 2 drives: athenaeum's real librarian
#: compile (:func:`~tests.evals.write_path.compile_observation_stream`) and
#: the native writer (:func:`~tests.evals.rollout.run_native_writer_dispatch`).
DEFAULT_PHASE2_SYSTEMS: tuple[str, ...] = ("athenaeum", "native")
_VALID_PHASE2_SYSTEMS: tuple[str, ...] = ("athenaeum", "native")

#: Declared (not measured) per-OBSERVATION token estimate for Phase 2's
#: pre-flight spend gate (issue athenaeum#1785) -- the write-path sibling of
#: :data:`~tests.evals.containment.NORTH_STAR_CELL_TOKEN_ESTIMATE`, which
#: prices one READ cell (a multi-turn tool-use loop over a materialized
#: corpus). One Phase 2 observation is cheaper than a full read cell, same
#: order of magnitude: the athenaeum side is a single tier1/tier2/tier3
#: classify+write pass through the librarian over one short raw-intake
#: note, and the native side is one short ``claude -p``-shaped tool-use
#: session saving (at most) that same note. 1,200 input / 300 output is a
#: round, deliberately conservative guess -- NOT a measured figure -- kept
#: in this module rather than ``containment.py`` because it prices
#: observations, not grid cells; replace it once the first live Phase 2
#: grid (athenaeum#1788) establishes real numbers, the same "declared, not
#: measured" discipline ``NORTH_STAR_CELL_TOKEN_ESTIMATE``'s own docstring
#: states.
PHASE2_OBSERVATION_TOKEN_ESTIMATE = CellTokenEstimate(input_tokens=1_200, output_tokens=300)


def write_relevance_floor_config(
    knowledge_root: Path,
    *,
    relevance_floor_vector: float | None,
    relevance_floor_fts5: float | None,
    search_backend: str,
) -> None:
    """Write ``athenaeum.yaml`` into *knowledge_root* setting
    ``recall.relevance_floor`` for whichever backend(s) an operator passed
    (issue athenaeum#1761).

    A no-op -- writes nothing, touches no directory -- when both floors are
    ``None`` (neither ``--relevance-floor-vector`` nor
    ``--relevance-floor-fts5`` was given): that is this issue's own
    acceptance criterion, the default dispatch stays byte-identical to
    today's behaviour.

    Both the PLAIN key (``recall.relevance_floor.<backend>``, read by an
    explicit ``recall`` call -- the API-mode PULL/PUSH_BREADCRUMB_PULL tool
    executors in ``tests.evals.rollout``) and the PUSH-scoped key
    (``recall.relevance_floor.push.<backend>``, read first by the shipped
    breadcrumb hook, which IS the unprompted push path) are set to the SAME
    value, so one flag governs both delivery paths this issue names rather
    than requiring two.

    Also stamps a top-level ``search_backend: <search_backend>`` key. This
    matters specifically for the breadcrumb hook: ``examples/claude-code/
    user-prompt-recall.sh`` only runs its vector half (and therefore only
    ever applies a ``vector`` floor) on a turn where its own
    ``SEARCH_BACKEND`` resolves to ``"vector"`` -- which
    ``session-start-recall.sh`` caches from this SAME ``athenaeum.yaml`` key,
    not from this CLI's own ``--search-backend`` flag. Without this line, a
    dispatch that built its index with ``--search-backend vector`` and set
    ``--relevance-floor-vector`` would still see the hook run FTS5-only and
    silently never exercise the vector floor at all. Written unconditionally
    whenever a floor is active (not only for a vector floor) so the hook's
    resolved backend always matches the one this run's index was built
    with.

    Called once per materialized knowledge root, before
    :func:`tests.evals.rollout.run_probe_all_arms` runs against it -- never
    inside :meth:`tests.evals.corpus.Corpus.materialize`, which this issue's
    proposal explicitly keeps config-free.
    """
    if relevance_floor_vector is None and relevance_floor_fts5 is None:
        return
    backend_floor: dict[str, float] = {}
    if relevance_floor_vector is not None:
        backend_floor["vector"] = relevance_floor_vector
    if relevance_floor_fts5 is not None:
        backend_floor["fts5"] = relevance_floor_fts5
    config = {
        "search_backend": search_backend,
        "recall": {
            "relevance_floor": {
                **backend_floor,
                "push": dict(backend_floor),
            }
        },
    }
    knowledge_root.mkdir(parents=True, exist_ok=True)
    (knowledge_root / "athenaeum.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )


def _floor_scan_summary_one_backend(backend_label: str, rows: Sequence[object]) -> str:
    """One backend's block of :func:`floor_scan_summary` -- see that
    function's docstring for why this is grouped rather than pooled."""
    scores: list[float] = []
    carrying = 0
    for row in rows:
        record_scores = row.record.retrieval_hit_scores  # type: ignore[attr-defined]
        if record_scores:
            carrying += 1
            scores.extend(record_scores)
    lines = [
        f"backend={backend_label} (lower is better)",
        f"{carrying} of {len(rows)} rows carry retrieval_hit_scores "
        "(rows persisted before issue athenaeum#1761 carry none).",
    ]
    if not scores:
        lines.append("no scores to summarise.")
        return "\n".join(lines)
    scores.sort()
    n = len(scores)

    def _pct(p: float) -> float:
        idx = min(n - 1, max(0, round(p * (n - 1))))
        return scores[idx]

    lines.append(
        f"n={n} min={scores[0]:.4f} p25={_pct(0.25):.4f} median={_pct(0.5):.4f} "
        f"p75={_pct(0.75):.4f} max={scores[-1]:.4f}"
    )
    return "\n".join(lines)


def floor_scan_summary(rows: Sequence[object]) -> str:
    """Summarise the retrieval-hit scores carried by *rows* (issue
    athenaeum#1761 item 4), so an operator can pick a
    ``--relevance-floor-vector``/``--relevance-floor-fts5`` value before
    dispatching a floor-on grid.

    *rows* is ``Sequence[tests.evals.north_star_report.RolloutRow]`` (typed
    loosely here to avoid a report-module import cycle at CLI-module load
    time); each row's ``.record.retrieval_hit_scores`` is the field
    :func:`tests.evals.rollout.run_probe_all_arms` populates -- a
    same-backend/same-index approximation of what the shipped breadcrumb
    hook saw for that probe's query, NOT the hook's own internal ranking
    (see that field's docstring for the exact caveat). A store written
    before that field existed carries ``None`` on every row; this function
    says so explicitly rather than printing a misleadingly-empty summary.

    Issue athenaeum#1764: grouped by ``.record.search_backend`` and printed
    as one block per backend, each carrying a "lower is better" note and
    the backend's own name. FTS5 bm25 scores (large negative) and vector
    distances (0 to 2) are not comparable numbers -- pooling them into one
    blended percentile summary, which this function used to do, produces a
    threshold that means nothing for either backend. A store mixing
    backends therefore prints one block per backend, never one blended
    block; a store written before ``search_backend`` existed groups its
    rows under ``"unknown"`` rather than silently dropping them.
    """
    if not rows:
        return "0 rows in store."
    groups: dict[str | None, list[object]] = {}
    for row in rows:
        backend = row.record.search_backend  # type: ignore[attr-defined]
        groups.setdefault(backend, []).append(row)
    blocks = [
        _floor_scan_summary_one_backend(
            backend if backend is not None else "unknown (pre-athenaeum#1764 store)",
            group_rows,
        )
        for backend, group_rows in sorted(
            groups.items(), key=lambda item: (item[0] is None, item[0] or "")
        )
    ]
    return "\n\n".join(blocks)


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
        # ``--max-spend 0`` (or negative) authorizes nothing, so it derives
        # nothing -- but the provenance must SAY that rather than claim no
        # spend flag was given, which is a different thing an operator would
        # debug differently.
        return (
            ROLLOUT_TOKEN_CEILING,
            f"ROLLOUT_TOKEN_CEILING default (--max-spend ${args.max_spend:.2f} "
            "derives no ceiling)",
        )
    return (
        ROLLOUT_TOKEN_CEILING,
        "ROLLOUT_TOKEN_CEILING default (neither --max-tokens nor --max-spend given)",
    )


def _default_store_path() -> Path:
    """System temp dir, never the repo tree -- a separate function (not a
    module constant) so tests can monkeypatch it for isolation."""
    return Path(tempfile.gettempdir()) / "athenaeum-north-star" / "results.jsonl"


def _resolve_store_path(args: argparse.Namespace) -> Path:
    """``--store``, or :func:`_default_store_path` when the flag was
    omitted -- the one place this resolution happens, so the pre-flight
    floor-mismatch check (issue athenaeum#1764) and the store ``main``
    actually runs cells against always agree on which file they mean."""
    return args.store if args.store is not None else _default_store_path()


def _resolve_phase2_store_path(args: argparse.Namespace) -> Path:
    """``--phase2-store``, or ``<store>.phase2.jsonl`` next to the resolved
    ``--store`` path (issue athenaeum#1785) -- the SIBLING JSONL, always
    distinct from ``--store`` itself, so Phase 2 rows (keyed by ``(system,
    corpus_scale)``, carrying no ``GridCell.cell_key()``) can never pass
    through the main grid's ``ResultStore``/resume contract."""
    if args.phase2_store is not None:
        return args.phase2_store
    return Path(str(_resolve_store_path(args)) + ".phase2.jsonl")


def _resolve_phase2_scales(args: argparse.Namespace) -> list[str]:
    """``--phase2-scales``, split and validated against :data:`SCALES` --
    the same validate-before-spend discipline :func:`_build_cells` applies
    to ``--corpus-scales``."""
    scales = args.phase2_scales.split(",") if args.phase2_scales else list(DEFAULT_PHASE2_SCALES)
    unknown = [s for s in scales if s not in SCALES]
    if unknown:
        raise ValueError(
            f"unknown corpus scale(s) {unknown!r} in --phase2-scales; known: {sorted(SCALES)}"
        )
    return scales


def _resolve_phase2_systems(args: argparse.Namespace) -> list[str]:
    """``--phase2-systems``, split and validated against
    :data:`_VALID_PHASE2_SYSTEMS`."""
    systems = (
        args.phase2_systems.split(",") if args.phase2_systems else list(DEFAULT_PHASE2_SYSTEMS)
    )
    unknown = [s for s in systems if s not in _VALID_PHASE2_SYSTEMS]
    if unknown:
        raise ValueError(
            f"unknown system(s) {unknown!r} in --phase2-systems; known: "
            f"{list(_VALID_PHASE2_SYSTEMS)}"
        )
    return systems


def _phase2_read_rows(path: Path) -> list[dict[str, Any]]:
    """Every decodable row from the Phase 2 sibling JSONL at *path*.

    A row that will not decode is SKIPPED, not fatal -- the same tolerance
    :class:`~tests.evals.containment.ResultStore` applies to the main grid
    store (a process killed mid-``write`` can leave a torn final line), kept
    here as a small hand-rolled reader rather than a ``ResultStore``
    instance: these rows carry no ``cell_key`` and must never pass through
    ``GridCell.cell_key()`` (issue athenaeum#1785 proposal)."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _phase2_latest_rows(
    rows: Sequence[dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """The LATEST row for each ``(kind, system, corpus_scale)`` triple.

    A resumed/re-run ``(system, scale)`` pair (issue athenaeum#1785 Quine
    review of PR#1813, should-fix 1: a partial compile is re-run by
    default, not accepted) appends a FRESH ``write_path``/``write_cost``/
    ``meta`` trio after the earlier one -- the later trio is canonical,
    mirroring ``ResultStore``'s own last-write-wins reasoning for a resumed
    group (``load_rollout_rows_and_diagnostics``'s duplicate-row handling).
    Rows of any other ``kind`` are ignored."""
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        kind = row.get("kind")
        if kind not in ("write_path", "write_cost", "meta"):
            continue
        key = (kind, row.get("system"), row.get("corpus_scale"))
        latest[key] = row
    return latest


def _phase2_completed_keys(rows: Sequence[dict[str, Any]]) -> set[tuple[str, str]]:
    """``(system, corpus_scale)`` pairs whose LATEST rows carry BOTH a
    ``write_path`` and a ``write_cost`` row -- the PAIR is the completion
    signal for resume (issue athenaeum#1785), not either row alone: a crash
    between writing the two would otherwise strand a ``(system, scale)``
    with retention stats recorded but no cost, silently skipped forever on
    a later resume. :func:`_run_phase2_group` always appends both rows (plus
    a ``meta`` row) in a single call, so an interrupted run leaves at most
    one incomplete pair, correctly re-run on resume. Does NOT by itself
    distinguish a clean completion from a partial one -- see
    :func:`_phase2_partial_keys`, which callers must consult separately
    before treating a "completed" key as safe to skip."""
    latest = _phase2_latest_rows(rows)
    have_stats = {(s, c) for (kind, s, c) in latest if kind == "write_path"}
    have_cost = {(s, c) for (kind, s, c) in latest if kind == "write_cost"}
    return have_stats & have_cost


def _phase2_partial_keys(rows: Sequence[dict[str, Any]]) -> set[tuple[str, str]]:
    """``(system, corpus_scale)`` pairs whose LATEST ``meta`` row carries
    ``partial: True`` -- issue athenaeum#1785 (Quine review of PR#1813,
    should-fix 1). Only an athenaeum compile's ``meta`` row ever carries
    this key (:func:`_run_phase2_group`'s athenaeum branch); a native
    group's ``meta`` row has no ``partial`` field and is never included
    here. Used both to gate resume (a partial key is re-run, not skipped,
    unless ``--phase2-accept-partial``) and to mark the rendered "Write
    path (Phase 2)" table (``build_report(phase2_partial=...)``)."""
    latest = _phase2_latest_rows(rows)
    return {
        (s, c)
        for (kind, s, c), row in latest.items()
        if kind == "meta" and row.get("partial") is True
    }


def _phase2_append_rows(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Append *rows* to the Phase 2 sibling JSONL, flushed and fsync'd --
    mirrors :meth:`~tests.evals.containment.ResultStore.append`'s own
    durability discipline, but is deliberately NOT a ``ResultStore``: see
    :func:`_phase2_read_rows`'s docstring for why."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_path_stats_row(stats: WritePathStats) -> dict[str, Any]:
    return {"kind": "write_path", **dataclasses.asdict(stats)}


def _write_cost_row(cost: WriteCost) -> dict[str, Any]:
    return {
        "kind": "write_cost",
        "system": cost.system,
        "corpus_scale": cost.corpus_scale,
        "input_tokens": cost.input_tokens,
        "output_tokens": cost.output_tokens,
    }


def load_phase2_results(path: Path) -> tuple[list[WritePathStats], list[WriteCost]]:
    """Read the Phase 2 sibling JSONL at *path* back into the
    ``(write_path_stats, write_costs)`` shape :func:`~tests.evals.north_star_report.build_report`
    accepts.

    Reads the LATEST row per ``(kind, system, corpus_scale)`` (issue
    athenaeum#1785 Quine review of PR#1813, should-fix 1 -- see
    :func:`_phase2_latest_rows`), not every row: a partial key that was
    re-run appends a second ``write_path``/``write_cost`` trio, and only the
    final one should reach the report, never both pooled as two separate
    rows for the same pair. A row of an unrecognised ``kind`` (e.g.
    ``meta``), or one missing a required field, is skipped rather than
    raising -- the sibling store is diagnostic-friendly by design, and a
    torn or partially-written row must not crash report rendering."""
    stats: list[WritePathStats] = []
    costs: list[WriteCost] = []
    for (kind, _system, _scale), row in _phase2_latest_rows(_phase2_read_rows(path)).items():
        try:
            if kind == "write_path":
                stats.append(
                    WritePathStats(
                        system=row["system"],
                        corpus_scale=row["corpus_scale"],
                        pages_targeted=row["pages_targeted"],
                        pages_written=row.get("pages_written"),
                        answer_tokens_total=row["answer_tokens_total"],
                        answer_tokens_retained=row.get("answer_tokens_retained"),
                        observations_total=row["observations_total"],
                        observations_measured=row["observations_measured"],
                        observations_dropped=row.get("observations_dropped"),
                    )
                )
            elif kind == "write_cost":
                costs.append(
                    WriteCost(
                        system=row["system"],
                        corpus_scale=row["corpus_scale"],
                        input_tokens=row["input_tokens"],
                        output_tokens=row["output_tokens"],
                    )
                )
        except KeyError:
            continue
    return stats, costs


def _run_phase2_group(
    system: str,
    scale: str,
    *,
    observations: Sequence[Any],
    stream: Any,
    materialize_root: Path,
    client: Any,
    session: EvalSession,
    model: str,
    mode: str,
    claude_binary: str,
) -> list[dict[str, Any]]:
    """Run Phase 2's write path for ONE ``(system, scale)``, returning the
    sibling-store rows this group produced: always a ``write_path`` row, a
    ``write_cost`` row (the completion pair -- see
    :func:`_phase2_completed_keys`), and a ``meta`` row carrying whatever
    else this issue's brief asks a Phase 2 row to record -- the compile's
    own exit code/partial flag for ``athenaeum``, or
    mode/prompt_fidelity/turns_exhausted/session count for ``native``.

    A partial (exit 75) athenaeum compile is recorded here with
    ``meta.partial = True`` and its real ``write_path``/``write_cost`` rows
    -- CompileOutcome's own docstring: that run made real, partial progress
    and the resulting store is still valid to measure. "Never silently
    pooled" (this issue's brief) means the partial flag must be visible on
    the row, not that the numbers are withheld.
    """
    group_root = materialize_root / "phase2" / f"{system}-{scale}"
    if system == "athenaeum":
        store_files, write_cost, outcome = compile_observation_stream(
            stream,
            group_root / "knowledge",
            client=client,
            model=model,
            session=session,
        )
        stats = compute_write_path_stats(system, scale, observations, store_files)
        meta = {
            "kind": "meta",
            "system": system,
            "corpus_scale": scale,
            "exit_code": outcome.exit_code,
            "partial": outcome.partial,
        }
    elif system == "native":
        result = run_native_writer_dispatch(
            observations,
            group_root / "native",
            mode=mode,
            client=client,
            session=session,
            model=model,
            claude_binary=claude_binary,
        )
        stats = compute_write_path_stats(system, scale, observations, result.memory_files)
        input_tokens = sum(t.input_tokens for s in result.sessions for t in s.turn_tokens)
        output_tokens = sum(t.output_tokens for s in result.sessions for t in s.turn_tokens)
        write_cost = WriteCost(
            system=system,
            corpus_scale=scale,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        meta = {
            "kind": "meta",
            "system": system,
            "corpus_scale": scale,
            "mode": result.mode,
            "prompt_fidelity": result.prompt_fidelity,
            "turns_exhausted": sum(1 for s in result.sessions if s.turns_exhausted),
            "sessions": len(result.sessions),
        }
    else:  # pragma: no cover -- _resolve_phase2_systems already validates
        raise ValueError(f"unknown Phase 2 system {system!r}")
    return [_write_path_stats_row(stats), _write_cost_row(write_cost), meta]


def run_phase2(
    args: argparse.Namespace,
    *,
    client: Any,
    session: EvalSession,
    materialize_root: Path,
    phase2_store_path: Path,
) -> None:
    """Run every not-yet-completed ``(system, scale)`` Phase 2 group and
    append its rows to *phase2_store_path* -- resume granularity is the
    ``(system, scale)`` pair (see :func:`_phase2_completed_keys`).

    A key whose LATEST rows came from a PARTIAL (exit 75) athenaeum compile
    (:func:`_phase2_partial_keys`) is, by default, treated as NOT done and
    RE-RUN -- a deadline-tripped compile made real but incomplete progress,
    and a second attempt may finish it (issue athenaeum#1785 Quine review of
    PR#1813, should-fix 1). Pass ``--phase2-accept-partial`` to accept the
    partial rows as final instead; that key is then skipped like any other
    completed one, and a warning naming it is printed to stderr so a silent
    resume never quietly settles for incomplete numbers.

    The observation stream is generated once per *scale* (issue
    athenaeum#1785 proposal / design doc §6.3): it is scale-invariant by
    construction (``generate_core_observations``'s own docstring), but its
    ``.scale`` attribute is what :func:`~tests.evals.write_path.compile_observation_stream`
    stamps onto the ``WriteCost`` it returns, so generating it under the
    requested *scale* keeps that row's ``corpus_scale`` matching the label
    this run is reporting under, even though the underlying observations
    are byte-identical across scales.
    """
    scales = _resolve_phase2_scales(args)
    systems = _resolve_phase2_systems(args)
    existing_rows = _phase2_read_rows(phase2_store_path)
    done = _phase2_completed_keys(existing_rows)
    partial = _phase2_partial_keys(existing_rows)
    accept_partial = args.phase2_accept_partial
    for scale in scales:
        stream = generate_core_observations(scale=scale)
        for system in systems:
            key = (system, scale)
            if key in done:
                if key not in partial:
                    continue
                if accept_partial:
                    print(
                        f"phase2: accepting partial (exit 75) compile for {key} "
                        "(--phase2-accept-partial) -- not re-running",
                        file=sys.stderr,
                    )
                    continue
                # A partial key, --phase2-accept-partial NOT given: fall
                # through and re-run this group.
            rows = _run_phase2_group(
                system,
                scale,
                observations=stream.observations,
                stream=stream,
                materialize_root=materialize_root,
                client=client,
                session=session,
                model=args.model,
                mode=args.mode,
                claude_binary=args.claude_binary,
            )
            _phase2_append_rows(phase2_store_path, rows)


def _phase2_summary(
    args: argparse.Namespace,
    write_path_stats: Sequence[WritePathStats],
    write_costs: Sequence[WriteCost],
    phase2_store_path: Path,
) -> str:
    """One human-readable line for the report header (issue athenaeum#1785:
    "the report header states phase2 on/off, scales, systems and the
    API-writer fidelity marker"). ``""`` when Phase 2 never ran and the
    sibling store carries nothing -- :class:`NorthStarReport.phase2_summary`
    renders nothing for an empty string, byte-identical to a report from
    before this field existed.

    The fidelity marker comes from the ``meta`` rows' ``prompt_fidelity``
    field -- ``"reconstructed"`` for every native group that ran in
    ``--mode api`` (:attr:`~tests.evals.rollout.NativeWriterResult.prompt_fidelity`'s
    own docstring: an api-mode Phase 2 number is an approximation pending
    the CLI spot-check, never silently pooled with a cli-mode row as
    equally faithful) -- so a reader of the header alone, without opening
    the sibling store, still sees that label.
    """
    if not args.phase2 and not write_path_stats and not write_costs:
        return ""
    fidelity_markers = sorted(
        {
            row["prompt_fidelity"]
            for row in _phase2_read_rows(phase2_store_path)
            if row.get("kind") == "meta" and row.get("prompt_fidelity")
        }
    )
    fidelity_note = (
        f", native prompt_fidelity={','.join(fidelity_markers)}" if fidelity_markers else ""
    )
    if not args.phase2:
        return f"off (sibling store carries data from an earlier --phase2 run{fidelity_note})"
    scales = _resolve_phase2_scales(args)
    systems = _resolve_phase2_systems(args)
    return f"on (scales={','.join(scales)}, systems={','.join(systems)}{fidelity_note})"


def check_floor_mismatch(
    rows: Sequence[object],
    *,
    relevance_floor_vector: float | None,
    relevance_floor_fts5: float | None,
    search_backend: str,
) -> str | None:
    """Compare the floors AND the search backend THIS dispatch is about to
    write against the values already recorded on *rows* read from
    ``--store`` (issue athenaeum#1764 item 3). Returns a human-readable
    mismatch description, or ``None`` when it is safe to proceed.

    *rows* is ``Sequence[tests.evals.north_star_report.RolloutRow]`` (typed
    loosely, as :func:`floor_scan_summary` above already is, to avoid a
    report-module import cycle at CLI-module load time).

    Passes (returns ``None``) for an empty store (no rows recorded at all --
    nothing to conflict with) and for a store whose rows all carry ``None``
    for a floor backend when this dispatch ALSO requests ``None`` for that
    backend -- the ordinary "no floor, never has been" case every pre-
    athenaeum#1761 store is in. Refuses on any other disagreement: resuming
    a floor-off store with a floor now requested, a floor-on store with a
    DIFFERENT value now requested, or (should a store somehow already carry
    more than one distinct value for a backend) a store that isn't uniform
    to begin with.

    ``search_backend`` is checked the same way, with one deliberate
    asymmetry from the floor checks: a row's ``search_backend`` is ``None``
    only for a pre-athenaeum#1764 store (the field did not exist yet), NOT
    for "no backend was used" -- every real dispatch always has SOME search
    backend, ``--search-backend`` defaults to ``"fts5"`` rather than
    parsing to ``None``. So a ``None``-backend row must never trip this
    check on its own regardless of what THIS dispatch requests; it is
    dropped from the recorded set entirely before comparing, rather than
    compared against ``search_backend`` the way a floor's ``None`` is
    compared against a requested ``None``. A store with only ``None``-
    backend rows (or none at all) therefore always passes the backend half
    of this check, and a store carrying a REAL recorded backend still
    refuses on any dispatch that names a different one.

    The reason this matters BEFORE any cell runs, not merely at report time
    (``north_star_report.build_report`` already refuses to pool differing
    floor values, issue athenaeum#1761 -- it has no equivalent check for
    backend at all, since ``RolloutRow``/``NorthStarReport`` carry no pooled
    backend field): ``_run_cells``' resume contract keys a group as done
    purely on its ``(probe, corpus_scale, replicate, arm)`` cell keys, which
    carry no floor value AND no backend at all -- see this module's own
    ``--relevance-floor-vector`` help text. Resuming an fts5-built store
    under ``--search-backend vector`` would silently skip every already-done
    group (spending nothing, producing nothing new) while reading floor
    scores computed against a completely different index -- and, for a
    floor mismatch, wouldn't even surface downstream until the mixed-floor
    error, long after an operator would have wanted to know. Catching both
    here, before pricing or a single cell, is strictly cheaper.
    """
    mismatches: list[str] = []
    for attr, requested, flag in (
        ("relevance_floor_vector", relevance_floor_vector, "--relevance-floor-vector"),
        ("relevance_floor_fts5", relevance_floor_fts5, "--relevance-floor-fts5"),
    ):
        recorded = {getattr(row.record, attr) for row in rows}  # type: ignore[attr-defined]
        if not recorded:
            continue
        if recorded == {requested}:
            continue
        distinct = sorted(recorded, key=lambda v: (v is None, v if v is not None else 0.0))
        mismatches.append(
            f"{attr}: --store already has {distinct!r}, this dispatch requests "
            f"{requested!r} ({flag}) -- resuming would silently mix them"
        )
    # search_backend: None-backend rows (pre-athenaeum#1764 stores) are
    # dropped before comparing -- see the docstring's "deliberate asymmetry"
    # paragraph. Only a REAL recorded backend can trip this.
    recorded_backends = {
        row.record.search_backend  # type: ignore[attr-defined]
        for row in rows
        if row.record.search_backend is not None  # type: ignore[attr-defined]
    }
    if recorded_backends and recorded_backends != {search_backend}:
        distinct_backends = sorted(recorded_backends)
        mismatches.append(
            f"search_backend: --store already has {distinct_backends!r}, this dispatch "
            f"requests {search_backend!r} (--search-backend) -- resuming would read floor "
            "scores computed against a different index"
        )
    if not mismatches:
        return None
    return "; ".join(mismatches)


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
    parser.add_argument(
        "--relevance-floor-vector",
        type=float,
        default=None,
        help=(
            "issue athenaeum#1761: write recall.relevance_floor.vector (plain and "
            "push-scoped) into athenaeum.yaml under every materialized knowledge root, so "
            "the breadcrumb hook and the API-mode recall tool apply this floor. Omitted "
            "(default): no floor, today's behaviour. A floor-on run should use its own "
            "--store path -- cell keys carry no floor value, so resuming a floor-off store "
            "with a floor turned on treats its cells as already done."
        ),
    )
    parser.add_argument(
        "--relevance-floor-fts5",
        type=float,
        default=None,
        help=(
            "issue athenaeum#1761: same mechanism as --relevance-floor-vector, for the "
            "fts5 backend. Omitted (default): no floor."
        ),
    )
    parser.add_argument(
        "--allow-floor-mismatch",
        action="store_true",
        help=(
            "issue athenaeum#1764: proceed even when --relevance-floor-vector/"
            "--relevance-floor-fts5/--search-backend disagree with the floor values or "
            "backend already recorded in --store rows. Omitted (default): the CLI refuses "
            "before running any cell rather than silently mixing floor configurations or "
            "backends into one store."
        ),
    )
    parser.add_argument(
        "--floor-scan",
        type=Path,
        default=None,
        help=(
            "issue athenaeum#1761: read an existing --store JSONL and print a summary of "
            "the retrieval_hit_scores its rows carry, then exit 0 -- makes zero paid calls "
            "and runs no cells, so an operator can choose a --relevance-floor-vector/"
            "--relevance-floor-fts5 value before dispatching a floor-on grid."
        ),
    )
    parser.add_argument(
        "--phase2",
        action="store_true",
        help=(
            "issue athenaeum#1785: also run the Phase 2 write path before the read grid -- "
            "compile_observation_stream for the athenaeum system, run_native_writer_dispatch "
            "for the native system -- writing one row per (system, scale) to a sibling JSONL "
            "derived from --store (see --phase2-store). Default off; existing (non-Phase-2) "
            "CLI behavior is byte-identical when omitted."
        ),
    )
    parser.add_argument(
        "--phase2-scales",
        default=None,
        help=(
            "comma-separated corpus scales for the Phase 2 write path (default: "
            f"{','.join(DEFAULT_PHASE2_SCALES)})"
        ),
    )
    parser.add_argument(
        "--phase2-store",
        type=Path,
        default=None,
        help=(
            "sibling JSONL path for Phase 2 rows (default: <--store path>.phase2.jsonl). "
            "Never the same file as --store: Phase 2 rows carry no GridCell.cell_key() and "
            "must never pass through the main ResultStore's resume contract."
        ),
    )
    parser.add_argument(
        "--phase2-systems",
        default=None,
        help=(
            "comma-separated systems for the Phase 2 write path (default: "
            f"{','.join(DEFAULT_PHASE2_SYSTEMS)})"
        ),
    )
    parser.add_argument(
        "--phase2-accept-partial",
        action="store_true",
        help=(
            "issue athenaeum#1785 (Quine review of PR#1813): by default, a (system, scale) "
            "pair whose sibling-store rows came from a partial (exit 75, deadline-tripped) "
            "athenaeum compile is RE-RUN on resume rather than accepted as final -- a second "
            "attempt may finish what the first one did not. Pass this flag to accept the "
            "partial rows as-is and skip re-running that pair instead (prints a warning "
            "naming the key)."
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
    # Issue athenaeum#1787: the vector-backend second dispatch is scoped to
    # `core` + `medium` only (29 * 8 * 2 = 464 cells, design doc §3.6) --
    # validated HERE, at parse time, same "validate before spend" discipline
    # as the unknown-scale check above, rather than left to run an
    # unbounded (and unbudgeted-for) vector grid at `large`/`xlarge`.
    if args.search_backend == "vector":
        out_of_scope = [s for s in corpus_scales if s not in _VECTOR_SCOPED_SCALES]
        if out_of_scope:
            raise ValueError(
                f"--search-backend vector is scoped to {sorted(_VECTOR_SCOPED_SCALES)!r} "
                f"only (issue athenaeum#1787); --corpus-scales named {out_of_scope!r} too. "
                "Pass --corpus-scales core,medium (or a subset) for a vector dispatch."
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
    relevance_floor_vector: float | None = None,
    relevance_floor_fts5: float | None = None,
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
        group_root = materialize_root / f"w{slot}" / f"{corpus_scale}-{replicate}"
        try:
            # Issue athenaeum#1761: written BEFORE run_probe_all_arms so it
            # exists under this group's knowledge root the moment
            # Corpus.materialize creates that root -- the CLI's own
            # concern per the issue's proposal (Corpus.materialize itself
            # stays config-free). A no-op when both floors are None.
            write_relevance_floor_config(
                group_root,
                relevance_floor_vector=relevance_floor_vector,
                relevance_floor_fts5=relevance_floor_fts5,
                search_backend=search_backend,
            )
            records = run_probe_all_arms(
                probe_id,
                corpus_scale,
                session=session,
                materialize_root=group_root,
                model=model,
                search_backend=search_backend,
                claude_binary=claude_binary,
                replicate=replicate,
                mode=mode,
                should_stop=stop.is_set,
            )
        finally:
            slots.put(slot)
        for record in records.values():
            record.relevance_floor_vector = relevance_floor_vector
            record.relevance_floor_fts5 = relevance_floor_fts5
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
    # Issue athenaeum#1761 item 4: a pure read of an existing store, zero
    # paid calls, zero cells run -- checked first, before any grid-sizing
    # or spend validation below, none of which applies to a scan.
    if args.floor_scan is not None:
        store = ResultStore(args.floor_scan)
        diagnostics = load_rollout_rows_and_diagnostics(store)
        print(floor_scan_summary(list(diagnostics.rows)))
        return 0
    if args.workers < 1:
        print(f"--workers must be >= 1, got {args.workers}", file=sys.stderr)
        return 1
    # Validated the same way and in the same place as --workers: a ceiling of
    # zero or less is not a tighter budget, it is a grid that can never run a
    # cell, and finding that out from a mid-grid abort message is strictly
    # worse than being told at parse time.
    if args.max_tokens is not None and args.max_tokens < 1:
        print(f"--max-tokens must be >= 1, got {args.max_tokens}", file=sys.stderr)
        return 1
    # Issue athenaeum#1764 item 3: checked before grid-sizing/pricing below,
    # and BEFORE a single cell runs -- see check_floor_mismatch's own
    # docstring for why this must happen this early rather than merely at
    # report time.
    if not args.allow_floor_mismatch:
        existing_rows = load_rollout_rows_and_diagnostics(
            ResultStore(_resolve_store_path(args))
        ).rows
        mismatch = check_floor_mismatch(
            list(existing_rows),
            relevance_floor_vector=args.relevance_floor_vector,
            relevance_floor_fts5=args.relevance_floor_fts5,
            search_backend=args.search_backend,
        )
        if mismatch is not None:
            print(
                f"floor mismatch against --store {_resolve_store_path(args)}: {mismatch} "
                "-- refusing to start. Pass --allow-floor-mismatch to override.",
                file=sys.stderr,
            )
            return 1
    cells = _build_cells(args)
    max_spend = resolve_max_spend(args)
    token_ceiling, ceiling_source = resolve_token_ceiling(args)
    projected_tokens = len(cells) * NORTH_STAR_CELL_TOKEN_ESTIMATE.total_tokens

    # Issue athenaeum#1785: validated here, before any spend, mirroring
    # _build_cells's own "validate before spend" discipline for
    # --corpus-scales. phase2_cell_count is (scales x systems); each of
    # those groups runs the FULL observation stream once, so the token
    # projection below multiplies by n_observations, not by
    # phase2_cell_count alone.
    phase2_scales = _resolve_phase2_scales(args) if args.phase2 else []
    phase2_systems = _resolve_phase2_systems(args) if args.phase2 else []
    phase2_cell_count = len(phase2_scales) * len(phase2_systems)
    # A pure local call (generate_core_observations makes zero network
    # calls) -- safe to run even on the dry-run/refusal path, same as
    # every other pre-flight sizing call in this function.
    n_phase2_observations = len(generate_core_observations().observations) if args.phase2 else 0
    phase2_projected_tokens = (
        phase2_cell_count * n_phase2_observations * PHASE2_OBSERVATION_TOKEN_ESTIMATE.total_tokens
    )
    projected_tokens += phase2_projected_tokens

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
            f"(~{NORTH_STAR_CELL_TOKEN_ESTIMATE.total_tokens} tokens/cell estimate) "
            f"{'plus ' + str(phase2_projected_tokens) + ' Phase 2 tokens' if args.phase2 else ''}"
        )
        if args.phase2:
            print(
                f"phase2: scales={','.join(phase2_scales)} systems={','.join(phase2_systems)} "
                f"{n_phase2_observations} observations x {phase2_cell_count} (scale x system) "
                f"groups (~{PHASE2_OBSERVATION_TOKEN_ESTIMATE.total_tokens} tokens/observation "
                "estimate)"
            )

    # Issue athenaeum#1785: the spend gate must count Phase 2 alongside the
    # read grid -- a --max-spend that only priced Phase 1 would let a real
    # dispatch abort mid-Phase-2 having already paid for it. price_grid
    # prices ONE per_cell rate over a cell list; Phase 2 uses a DIFFERENT
    # per-observation rate, so it cannot share one price_grid call with the
    # read grid. Instead: price the read grid without letting it refuse on
    # its own (max_spend_usd=inf can never trip SpendCeilingExceededError),
    # price Phase 2 the same way via a synthetic per-observation TokenUsage
    # accumulation, sum the two USD figures, and apply ONE combined refusal
    # against --max-spend -- so neither phase can silently exceed the
    # ceiling on its own while the sum still reads as "under budget".
    read_grid_estimate = price_grid(
        cells,
        model=args.model,
        max_spend_usd=float("inf"),
        per_cell=NORTH_STAR_CELL_TOKEN_ESTIMATE,
    )
    phase2_usage = TokenUsage()
    for _ in range(phase2_cell_count * n_phase2_observations):
        phase2_usage.add_tokens(
            PHASE2_OBSERVATION_TOKEN_ESTIMATE.input_tokens,
            PHASE2_OBSERVATION_TOKEN_ESTIMATE.output_tokens,
            model=args.model,
        )
    phase2_estimated_usd = phase2_usage.estimated_cost_usd
    combined_estimated_usd = read_grid_estimate.estimated_usd + phase2_estimated_usd
    if combined_estimated_usd > max_spend:
        print(
            f"planned grid ({read_grid_estimate.cell_count} cells @ {args.model}) prices at "
            f"${read_grid_estimate.estimated_usd:.2f}"
            + (
                f" plus Phase 2 (${phase2_estimated_usd:.2f}) = ${combined_estimated_usd:.2f}"
                if args.phase2
                else ""
            )
            + f", exceeding --max-spend ${max_spend:.2f} -- refusing to start. Shrink --scale, "
            "the probe/arm/corpus-scale/replicate lists, --phase2-scales/--phase2-systems, or "
            "raise --max-spend deliberately.",
            file=sys.stderr,
        )
        return 1
    estimate = read_grid_estimate

    print(
        f"scale={args.scale} cells={estimate.cell_count} "
        f"estimated=${estimate.estimated_usd:.4f} model={args.model}"
        + (f" phase2_estimated=${phase2_estimated_usd:.4f}" if args.phase2 else "")
    )

    # Deliberately NOT inside `if args.dry_run` (Quine review of PR
    # athenaeum#1757): a LIVE run whose projection already exceeds its own
    # ceiling is going to abort mid-grid having paid for every cell up to
    # that point, which is precisely the athenaeum#1754 failure mode. The
    # pre-flight knows that before the first paid call, so it refuses here
    # for real runs and dry runs alike -- and after all three projection
    # lines have printed on the dry-run path, so the operator sees the wall
    # clock, the ceiling and the price rather than only the refusal.
    if projected_tokens > token_ceiling:
        print(
            f"projected tokens {projected_tokens} exceed the token ceiling "
            f"{token_ceiling} ({ceiling_source}) -- refusing to start. "
            "Shrink --scale or the probe/corpus-scale/replicate lists, or "
            "raise --max-tokens (or --max-spend, which derives it).",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print("dry run: zero cells executed, zero paid calls made")
        return 0

    store = ResultStore(_resolve_store_path(args))
    # Recorded BEFORE the first cell runs, so a run killed mid-grid by the
    # job timeout still leaves the denominator the report's partial banner
    # needs (issue athenaeum#1751).
    write_planned_cells(store, len(cells))
    materialize_root = args.materialize_root or Path(
        tempfile.mkdtemp(prefix="athenaeum-north-star-")
    )
    session = EvalSession()
    phase2_store_path = _resolve_phase2_store_path(args)

    aborted = False
    abort_reason = ""
    try:
        if args.phase2:
            # Issue athenaeum#1785 proposal: the write path runs BEFORE the
            # read grid. Shares *session* with _run_cells below, so
            # --max-spend/--max-tokens governs Phase 1 + Phase 2 combined --
            # assert_rollout_ceiling (after _run_cells) and _run_cells's own
            # per-group ceiling check both read the SAME accumulator Phase 2
            # already added to. build_live_client() is called only here,
            # inside the try and after every refusal/dry-run return above --
            # a Phase-2-off run, or the dry-run path, never constructs one.
            client = build_live_client()
            run_phase2(
                args,
                client=client,
                session=session,
                materialize_root=materialize_root,
                phase2_store_path=phase2_store_path,
            )
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
            relevance_floor_vector=args.relevance_floor_vector,
            relevance_floor_fts5=args.relevance_floor_fts5,
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

    # Re-read rather than reusing *existing_rows* from the item-3 pre-flight
    # check above (Quine review "optional" note): that read happened BEFORE
    # a single cell ran, this one happens AFTER -- the store has new rows in
    # between on any run that actually executed cells, so the two reads are
    # of genuinely different store contents, not the same data decoded
    # twice.
    diagnostics = load_rollout_rows_and_diagnostics(store)
    # Read back regardless of aborted/exception state above -- a Phase 2
    # group that completed before a Phase 1 failure (or before a
    # KeyboardInterrupt) already appended its rows durably (fsync'd,
    # ResultStore-style -- see _phase2_append_rows), so a PARTIAL report
    # still shows whatever Phase 2 data exists.
    phase2_write_path_stats, phase2_write_costs = load_phase2_results(phase2_store_path)
    phase2_summary_text = _phase2_summary(
        args, phase2_write_path_stats, phase2_write_costs, phase2_store_path
    )
    # Issue athenaeum#1785 (Quine review of PR#1813, should-fix 1): read
    # separately from load_phase2_results's DEDUPED stats/costs, so a row
    # marked partial is still visible to the report table even though it is
    # ALSO the row load_phase2_results kept as canonical (a key can only be
    # "completed" with a partial flag if it was never successfully re-run --
    # see run_phase2's own resume logic).
    phase2_partial_pairs = _phase2_partial_keys(_phase2_read_rows(phase2_store_path))
    try:
        report = build_report(
            list(diagnostics.rows),
            aborted=aborted,
            abort_reason=abort_reason,
            write_path_stats=phase2_write_path_stats,
            write_costs=phase2_write_costs,
            verdict_arm=args.verdict_arm,
            planned_cells=read_planned_cells(store),
            torn_rows=diagnostics.torn,
            duplicate_rows=diagnostics.duplicates,
            phase2_summary=phase2_summary_text,
            phase2_partial=phase2_partial_pairs,
        )
    except MixedFloorError as exc:
        # Issue athenaeum#1764 item 1: a mixed-floor store (rows carrying
        # differing relevance_floor_vector/relevance_floor_fts5 values --
        # see north_star_report._pooled_floor_value) raises HERE, separately
        # from whatever _run_cells did or did not do above. Left uncaught
        # this crashed main() with a bare traceback and wrote no report at
        # all, even though the store itself (fsync'd per row) survived
        # intact. Recover by marking the run aborted, naming the exact
        # mismatched values found (str(exc) already does -- see that
        # function's own message), and rebuilding the SAME report with
        # pooling skipped so a PARTIAL .md still gets written rather than
        # losing the report entirely. Deliberate consequence: the rendered
        # header's own relevance_floor_vector/relevance_floor_fts5 lines
        # read "off" (pool_floor_values=False reports both as None) even
        # though the store demonstrably carries a real floor -- this is
        # fine precisely because the PARTIAL banner directly above it
        # already carries abort_reason naming the actual values found; the
        # header fields are not asked to double as that explanation.
        aborted = True
        abort_reason = (f"{abort_reason}; {exc}" if abort_reason else str(exc))
        try:
            report = build_report(
                list(diagnostics.rows),
                aborted=True,
                abort_reason=abort_reason,
                write_path_stats=phase2_write_path_stats,
                write_costs=phase2_write_costs,
                verdict_arm=args.verdict_arm,
                planned_cells=read_planned_cells(store),
                torn_rows=diagnostics.torn,
                duplicate_rows=diagnostics.duplicates,
                pool_floor_values=False,
                phase2_summary=phase2_summary_text,
                phase2_partial=phase2_partial_pairs,
            )
        except Exception as recovery_exc:  # noqa: BLE001 -- see below
            # Quine review "should": this recovery build_report call can
            # ALSO raise -- for example an unknown corpus_scale reaching
            # _corpus_for_scale/build_corpus inside build_report, a
            # genuinely different failure this except clause has no special
            # handling for. Left unguarded this crashed main() with a bare
            # traceback a SECOND time, exactly the failure mode item 1
            # exists to prevent. Fall back to build_report([]) -- rows=()
            # is proven never to raise
            # (test_build_report_empty_rows_does_not_raise) -- so a PARTIAL
            # report stub still gets written, naming BOTH failures, rather
            # than losing the report and the exit code together.
            abort_reason = (
                f"{abort_reason}; report construction also failed: {recovery_exc}"
            )
            report = build_report(
                [],
                aborted=True,
                abort_reason=abort_reason,
                verdict_arm=args.verdict_arm,
                planned_cells=read_planned_cells(store),
            )
    path = write_report(report, out_dir=args.out_dir)
    print(f"report written: {path}")
    return 0 if not aborted else 1


if __name__ == "__main__":
    raise SystemExit(main())
