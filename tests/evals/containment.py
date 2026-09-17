# SPDX-License-Identifier: Apache-2.0
"""Eval containment harness: pre-flight spend gate + append-only result store
+ a run-level ``--scale`` knob (issue athenaeum#1521).

Why this exists: the north-star arm comparison (probes x arms x
corpus-scales x replicates) cannot safely run without containment. Before
this module, nothing priced a planned grid before running it (cost was
discovered after the fact), a partial run was lost on interruption (a
re-run repaid for cells that had already completed), and there was no
run-level budget tier — only ``tests/evals/corpus.py``'s CORPUS scales,
which control corpus SIZE, not how much of the grid a run actually covers.

Deliberately sits ALONGSIDE ``tests/evals/harness.py`` rather than
extending it (issue athenaeum#1521's explicit instruction): ``harness.py``'s
``EvalSession``/``EVAL_TOKEN_CEILING`` model ONE session's worth of
single-call component evals (detector/resolver/recall/classify/merge/
write-tier-compare). A grid run is a DIFFERENT shape — many independent
cells, each independently priced, independently persisted, resumable
after a kill — and folding rollout token usage into ``EVAL_TOKEN_CEILING``
would blow that ceiling immediately (see ``tests/evals/rollout_session.py``
for the separate ceiling this module's callers must use instead).

Bradley-Terry fitting and the actual arm-comparison rollout logic are
OUT OF SCOPE (issue athenaeum#1521's explicit exclusion) — this module has no
opinion on what a "probe" or "arm" IS; it is generic containment
machinery over four caller-supplied axes (probe id, arm id, corpus scale,
replicate index). The future arm-comparison issue plugs its real axis
values and its real per-cell runner into :func:`build_grid` and
:func:`run_grid`; nothing here needs to change when it does.

Layering: sits under ``tests/evals/`` (not ``src/athenaeum/``) on purpose,
per this issue's own guidance — a new ``src/athenaeum/*.py`` module would
need a ``tests/fixtures/layer_declarations.py`` entry; a test-only module
does not.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.models import TokenUsage

# ---------------------------------------------------------------------------
# Grid cells + the --scale knob
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GridCell:
    """One (probe, arm, corpus scale, replicate) unit of grid work.

    These four fields are the cell's STABLE IDENTITY — :meth:`cell_key`
    encodes exactly them, nothing else. Anything that changes what work a
    cell represents (a fifth axis, in a future extension) must join this
    identity, or two genuinely different cells could collide on the same
    result-store key and one would silently shadow the other.
    """

    probe: str
    arm: str
    corpus_scale: str
    replicate: int

    def cell_key(self) -> str:
        """Stable identity key for the append-only result store.

        A canonical JSON encoding of the ``(probe, arm, corpus_scale,
        replicate)`` tuple, NOT a bare ``"|"``-joined string (issue
        athenaeum#1521 PR review, finding 1: a naive delimiter join collides
        whenever a component itself contains the delimiter — e.g.
        ``probe="a|b", arm="c"`` and ``probe="a", arm="b|c"`` both joined to
        ``"a|b|c|core|0"``, which means the resume logic would have silently
        treated an unrun cell as already-paid-for). JSON array encoding is
        injective for this fixed-position, fixed-type tuple: quoting and
        backslash-escaping make component boundaries unambiguous regardless
        of what characters a component contains, and ``json.loads(cell_key())``
        round-trips to the exact original tuple, which two DIFFERENT tuples
        could never both do.

        The key FORMAT is not a stable, documented contract — nothing in
        this repo persists a result-store file across a code change (issue
        athenaeum#1521 shipped with none committed), so there is no
        migration concern today, but a reader should not assume today's
        JSON shape is guaranteed not to change again.
        """
        return json.dumps(
            [self.probe, self.arm, self.corpus_scale, self.replicate],
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class ScaleBudget:
    """Per-axis caps for one ``--scale`` tier.

    ``None`` means "uncapped" — use every value the caller supplied on that
    axis. A cap truncates the caller's sequence (preserving order), it never
    reorders or samples it, so the SAME probes/arms/corpus-scales/replicates
    list produces a strict subset at a smaller scale, not a different one.
    """

    name: str
    max_probes: int | None
    max_arms: int | None
    max_corpus_scales: int | None
    max_replicates: int | None


#: The three run-level budgets a contributor chooses between. ``smoke`` caps
#: every axis to exactly 1 so the grid is always a single cell, regardless of
#: how many probes/arms/corpus-scales/replicates the caller has defined —
#: this is what makes "smoke is runnable without thinking about cost" true
#: even as the real probe/arm lists grow long after this issue lands.
#: ``full`` is uncapped: every axis runs at whatever size the caller
#: supplied, which is the actual north-star comparison.
SCALE_BUDGETS: dict[str, ScaleBudget] = {
    "smoke": ScaleBudget(
        "smoke", max_probes=1, max_arms=1, max_corpus_scales=1, max_replicates=1
    ),
    "small": ScaleBudget(
        "small", max_probes=3, max_arms=2, max_corpus_scales=1, max_replicates=2
    ),
    "full": ScaleBudget(
        "full", max_probes=None, max_arms=None, max_corpus_scales=None, max_replicates=None
    ),
}


def _cap(values: Sequence[Any], limit: int | None) -> list[Any]:
    return list(values) if limit is None else list(values)[:limit]


def build_grid(
    scale: str,
    *,
    probes: Sequence[str],
    arms: Sequence[str],
    corpus_scales: Sequence[str],
    replicates: Sequence[int],
) -> list[GridCell]:
    """Build the grid for *scale*, capping each axis per :data:`SCALE_BUDGETS`.

    ONE code path drives all three budgets — ``smoke``/``small``/``full`` all
    call this same function; only the returned cardinality differs (issue
    athenaeum#1521's explicit design constraint). Cell order is the itertools
    product order over the (capped) axes, so a rerun at the same scale with
    the same inputs always enumerates cells in the same order — important
    for the result store's resume behaviour to be deterministic.
    """
    if scale not in SCALE_BUDGETS:
        raise ValueError(f"unknown scale {scale!r}; known: {sorted(SCALE_BUDGETS)}")
    budget = SCALE_BUDGETS[scale]
    capped_probes = _cap(probes, budget.max_probes)
    capped_arms = _cap(arms, budget.max_arms)
    capped_corpus_scales = _cap(corpus_scales, budget.max_corpus_scales)
    capped_replicates = _cap(replicates, budget.max_replicates)
    return [
        GridCell(probe=probe, arm=arm, corpus_scale=corpus_scale, replicate=replicate)
        for probe, arm, corpus_scale, replicate in itertools.product(
            capped_probes, capped_arms, capped_corpus_scales, capped_replicates
        )
    ]


# ---------------------------------------------------------------------------
# Pre-flight spend gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellTokenEstimate:
    """Declared (not measured) per-cell token estimate for pre-flight pricing.

    A pre-flight gate must price a grid BEFORE any cell has run, so there is
    no observed token count to price from yet — this is a deliberately
    conservative DECLARED estimate, the same shape as
    ``athenaeum.drain_advisor.DEFAULT_AVG_INPUT_TOKENS_PER_FILE`` /
    ``DEFAULT_AVG_OUTPUT_TOKENS_PER_FILE``. Override with real figures once
    a first live batch establishes them (see ``containment_cli.py``'s
    ``--input-tokens-per-cell``/``--output-tokens-per-cell``).
    """

    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        """Input + output — the shape a TOKEN ceiling is expressed in, as
        opposed to the two-rate split pricing needs."""
        return self.input_tokens + self.output_tokens


#: An agent rollout cell is a multi-turn tool-using run, not a single
#: completion — conservatively sized well above a single component-eval
#: call (contrast ``athenaeum.drain_advisor``'s per-FILE estimates, two
#: orders of magnitude smaller). Deliberately a round, clearly-a-guess
#: number rather than a false-precision figure: this is a PRE-FLIGHT
#: estimate, not a measurement.
DEFAULT_CELL_TOKEN_ESTIMATE = CellTokenEstimate(input_tokens=20_000, output_tokens=4_000)

#: The north-star grid's own per-cell estimate, MEASURED rather than declared
#: (issue athenaeum#1754). Source: GitHub Actions run 35200779015 — the first
#: live full-grid dispatch — which accumulated 2,018,960 tokens over 392
#: completed cells, i.e. about 5,150 tokens per cell, against the 24,000 the
#: shared :data:`DEFAULT_CELL_TOKEN_ESTIMATE` guessed. Separate from that
#: constant rather than replacing it: ``containment_cli.py`` prices a
#: different driver's cells from it, and this provenance is a north-star
#: measurement only.
#:
#: What is evidence and what is not: the 5,150 TOTAL is measured; the 5:1
#: input/output split is inherited from ``DEFAULT_CELL_TOKEN_ESTIMATE`` (that
#: run's summary recorded the accumulated total, not the two halves). The
#: split matters because :func:`tokens_for_spend` derives a token ceiling
#: from a USD ceiling at this mix — a wrong mix mis-sizes that ceiling even
#: though the total is right.
NORTH_STAR_CELL_TOKEN_ESTIMATE = CellTokenEstimate(input_tokens=4_300, output_tokens=850)

#: Declared (not measured) per-cell wall-clock estimate, the TIME sibling of
#: :data:`DEFAULT_CELL_TOKEN_ESTIMATE`'s token estimate (issue athenaeum#1751).
#: A tool-using cell is a multi-turn Messages API loop, so tens of seconds is
#: the right order of magnitude; like the token estimate this is deliberately
#: a round, clearly-a-guess number a first live batch should replace, not a
#: false-precision figure. It exists so ``--dry-run`` can answer "does a
#: dispatch at N workers fit the job's ``timeout-minutes`` window?" BEFORE the
#: operator clicks, which is the only question a pre-flight can usefully
#: answer about time.
DEFAULT_CELL_SECONDS = 45.0


def project_wall_clock_seconds(
    cell_count: int,
    *,
    workers: int,
    seconds_per_cell: float = DEFAULT_CELL_SECONDS,
) -> float:
    """Projected wall-clock seconds for *cell_count* cells at *workers* workers.

    Flat ``cells * seconds / workers``. This is only honest because the
    north-star driver's unit of concurrency is a (probe, corpus_scale,
    replicate) GROUP of exactly ``len(ALL_ARMS)`` cells whose arms run
    serially inside the group -- so cells-per-unit-time really does scale
    with the worker count. A future partial-arm group (one that runs fewer
    arms per call) would break that equality and make this number optimistic
    by the ratio of the group sizes; anyone adding one must revisit this.
    """
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    return cell_count * seconds_per_cell / workers


def format_duration(seconds: float) -> str:
    """``h:mm:ss`` -- the shape an operator compares against a job's
    ``timeout-minutes`` without doing arithmetic in their head."""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


class SpendCeilingExceededError(Exception):
    """Raised by :func:`price_grid` when the priced grid exceeds ``--max-spend``.

    A normal :class:`Exception` (unlike ``harness.FixtureStaleError`` /
    ``harness.EmptyRecordingError`` / ``tier_compare.EmptyCorpusError``,
    which subclass ``BaseException`` so they survive a call site's own
    ``except Exception``): this is a PRE-FLIGHT refusal raised before any
    cell has run and before any production call site's broad exception
    handler is anywhere on the stack, so there is nothing here that needs
    to survive being caught. The CLI catches it directly to print a refusal
    and exit non-zero.
    """


@dataclass(frozen=True)
class SpendEstimate:
    """The priced outcome of one planned grid, always returned by
    :func:`price_grid` — even when it refused to start (the caller can
    inspect the estimate the refusal was based on)."""

    cell_count: int
    estimated_usd: float
    max_spend_usd: float
    model: str


def tokens_for_spend(
    max_spend_usd: float,
    *,
    model: str,
    per_cell: CellTokenEstimate = DEFAULT_CELL_TOKEN_ESTIMATE,
) -> int:
    """How many tokens *max_spend_usd* buys at *model*'s rate (athenaeum#1754).

    The inverse of :func:`price_grid`'s arithmetic, and deliberately built
    out of the same two pieces — ``athenaeum.models.TokenUsage``'s rate
    table and *per_cell*'s input/output mix — so a USD ceiling and a token
    ceiling derived from it can never disagree about what a cell costs.
    Prices one cell, then scales: tokens = spend / cost_per_cell *
    tokens_per_cell.

    The mix is load-bearing, not incidental: input and output tokens are
    priced differently (5x apart on the Haiku tier), so "tokens per dollar"
    is only meaningful relative to an assumed split. Pass the same
    *per_cell* the caller prices with.

    Returns 0 for a zero-or-negative spend or an unpriced model, which the
    caller should read as "no ceiling could be derived" and fall back to its
    own constant rather than treating as a ceiling of zero.
    """
    if max_spend_usd <= 0:
        return 0
    usage = TokenUsage()
    usage.add_tokens(per_cell.input_tokens, per_cell.output_tokens, model=model)
    cost_per_cell = usage.estimated_cost_usd
    if cost_per_cell <= 0:
        return 0
    return int(max_spend_usd / cost_per_cell * per_cell.total_tokens)


def price_grid(
    cells: Sequence[GridCell],
    *,
    model: str,
    max_spend_usd: float,
    per_cell: CellTokenEstimate = DEFAULT_CELL_TOKEN_ESTIMATE,
) -> SpendEstimate:
    """Price *cells* at *model*'s rate and refuse to start over budget.

    Reuses ``athenaeum.models.TokenUsage.estimated_cost_usd`` — the SAME
    per-model rate table (``athenaeum.models._MODEL_RATES_USD_PER_MTOK``)
    every other cost estimate in this codebase prices against
    (``tests/evals/tier_compare.py``, ``athenaeum.drain_advisor``,
    ``athenaeum.backlog_price_sheet``) — rather than a second, hardcoded
    price list.

    Raises :class:`SpendCeilingExceededError` when the estimate exceeds
    *max_spend_usd*; returns the :class:`SpendEstimate` unchanged otherwise.
    """
    usage = TokenUsage()
    for _ in cells:
        usage.add_tokens(per_cell.input_tokens, per_cell.output_tokens, model=model)
    estimate = SpendEstimate(
        cell_count=len(cells),
        estimated_usd=usage.estimated_cost_usd,
        max_spend_usd=max_spend_usd,
        model=model,
    )
    if estimate.estimated_usd > max_spend_usd:
        raise SpendCeilingExceededError(
            f"planned grid ({estimate.cell_count} cells @ {model}) prices at "
            f"${estimate.estimated_usd:.2f}, exceeding --max-spend "
            f"${max_spend_usd:.2f} — refusing to start. Shrink --scale, the "
            "probe/arm/corpus-scale/replicate lists, or raise --max-spend "
            "deliberately."
        )
    return estimate


# ---------------------------------------------------------------------------
# Append-only result store
# ---------------------------------------------------------------------------


def _terminate_torn_tail(handle: Any) -> None:
    """Close off an unterminated final line before appending after it.

    A process killed mid-``write`` leaves a partial row with NO trailing
    newline. Appending straight onto that fragment glues the next row's
    JSON to it, and the result is a single line that decodes as neither:
    the fragment is expected (a torn row is counted and tolerated), but the
    NEW row -- a cell that just ran and was just paid for -- is destroyed
    with it. Worse, it is destroyed the same way on every subsequent
    resume, because each resume re-runs that cell and re-glues it to the
    same unterminated tail, so the cell can never persist at all.

    Writing the missing newline first keeps the fragment torn (it stays
    counted, and its cell still reads as not-completed, so the resume
    re-runs it) while letting every row appended after it decode normally.
    """
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        return
    handle.seek(-1, os.SEEK_END)
    if handle.read(1) != b"\n":
        handle.write(b"\n")


class ResultStore:
    """Append-only JSONL store of completed grid-cell results.

    One line per completed cell, written with ``open(..., "a")`` (never
    rewritten in place), flushed and ``fsync``'d immediately after each
    write — so a hard kill loses at most the one cell that was in flight
    when it happened, never a previously-completed cell's row.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        # issue athenaeum#1751: the grid now runs cells on a bounded thread
        # pool, so two workers can finish at the same moment. Without this
        # lock their two ``write()`` calls could interleave mid-line and
        # produce a corrupt JSONL row that ``load_rollout_rows`` would then
        # fail to decode -- losing not just the in-flight cell but every row
        # after it in the file. One lock per store INSTANCE is the right
        # grain because a run shares exactly one instance (see
        # ``north_star_cli._run_cells``); two instances over the same path
        # would still race, which is why callers must not construct a second.
        self._append_lock = threading.Lock()

    def completed_keys(self) -> set[str]:
        """Return every cell key already persisted, read once up front.

        Reads the whole file in one pass rather than being consulted
        per-cell mid-run — the file only grows monotonically during a run
        of :func:`run_grid`, so a snapshot taken at the start is exactly
        "what a resume must not repay for".

        A row that will not decode is SKIPPED, not fatal — see
        :func:`count_torn_rows` for why one can exist and why the tolerance
        cannot be narrowed to "the last line only". Skipping is the safe
        direction here: an unreadable row is treated as not-yet-completed,
        so its cell is re-run and re-appended rather than silently
        considered paid for.
        """
        keys: set[str] = set()
        for row in self._iter_rows():
            key = row.get("cell_key")
            if isinstance(key, str):
                keys.add(key)
        return keys

    def _iter_rows(self) -> Iterator[dict[str, Any]]:
        """Yield every decodable row, skipping blank and torn ones."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row

    def count_torn_rows(self) -> int:
        """How many lines in the store will not decode as a JSON object.

        A store is written append-only with an ``fsync`` per row, so within
        ONE run only the final line can be torn — a process killed at its
        job ``timeout-minutes`` mid-``write`` leaves a partial line, and a
        rollout row is hundreds of KB, so that window is real rather than
        theoretical.

        The tolerance deliberately is NOT narrowed to "the last line only",
        because this issue's own workflow change makes a resumed run the
        expected recovery path: a resume appends AFTER the torn tail, which
        puts the torn line in the MIDDLE of the file. A strict
        last-line-only rule would start raising on exactly the store it was
        written to rescue. Counting instead of raising keeps the audit
        trail — the count reaches the report's partial banner, so a torn
        row is visible rather than merely survived.
        """
        if not self.path.exists():
            return 0
        torn = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    torn += 1
                    continue
                if not isinstance(row, dict):
                    torn += 1
        return torn

    def append(self, cell_key: str, payload: dict[str, Any]) -> None:
        """Append one completed cell's result, flushed and fsync'd.

        Serialised on :attr:`_append_lock` so concurrent workers never
        interleave a partial line (issue athenaeum#1751). The JSON is
        rendered BEFORE the lock is taken -- the critical section covers
        only the file work, so a slow ``json.dumps`` on a big payload does
        not stall every other worker.
        """
        row = {"cell_key": cell_key, **payload}
        line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        with self._append_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # "a+b", not "ab": the tail check below has to READ the last
            # byte, and append-only mode is write-only. Writes still always
            # land at the end regardless of where the read left the cursor.
            with self.path.open("a+b") as handle:
                _terminate_torn_tail(handle)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())


def run_grid(
    cells: Iterable[GridCell],
    cell_runner: Callable[[GridCell], dict[str, Any]],
    store: ResultStore,
    *,
    workers: int = 1,
) -> list[dict[str, Any]]:
    """Run every cell in *cells* not already present in *store*.

    ``store.completed_keys()`` is read exactly ONCE, before any cell runs
    — this is what makes a resume after a mid-grid kill cheap and correct:
    every cell already persisted (from before the kill) is skipped without
    calling *cell_runner* at all, and every remaining cell is executed and
    appended exactly once. Returns the payloads for cells executed on THIS
    call only (not the ones skipped as already-complete).

    *workers* (issue athenaeum#1751) bounds how many cells run at a time.
    ``1`` — the default, so no existing caller changes behaviour — keeps
    the original strictly-serial loop, and both the store rows and the
    returned list stay in *cells* order. Above ``1`` the cells are
    independent units dispatched to a :class:`ThreadPoolExecutor`, and
    **order is no longer defined**: rows land in completion order and the
    returned list matches. The SET of executed cells is identical either
    way, which is the property callers may rely on — anything needing a
    stable ordering must sort by cell identity itself.

    A failing cell does not cost the others their results: every cell that
    completed is appended, and the first exception is re-raised only once
    the pool has drained.
    """
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    already_done = store.completed_keys()
    pending = [cell for cell in cells if cell.cell_key() not in already_done]
    results: list[dict[str, Any]] = []
    if workers == 1:
        for cell in pending:
            payload = cell_runner(cell)
            store.append(cell.cell_key(), payload)
            results.append(payload)
        return results
    first_failure: BaseException | None = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(cell_runner, cell): cell for cell in pending}
        for future in as_completed(futures):
            cell = futures[future]
            try:
                payload = future.result()
            except Exception as exc:  # noqa: BLE001 -- see below
                # Drain, do not bail. Raising straight out of this loop
                # would abandon every future that had ALREADY completed but
                # was not yet pulled off ``as_completed`` -- their cells ran
                # and were paid for, and their results would never reach the
                # store. That is exactly the silent loss the append-only
                # store exists to prevent, and it would also break resume:
                # the next run would re-pay for work that had succeeded.
                # So every completed cell is persisted, and the FIRST
                # failure is re-raised below, once there is nothing left to
                # lose by raising it.
                if first_failure is None:
                    first_failure = exc
                continue
            store.append(cell.cell_key(), payload)
            results.append(payload)
    if first_failure is not None:
        raise first_failure
    return results


# ---------------------------------------------------------------------------
# Planned-cell-count sidecar (partial-run detection)
# ---------------------------------------------------------------------------

#: Suffix appended to a store's own filename for its planned-count sidecar.
PLANNED_SIDECAR_SUFFIX = ".planned.json"


def planned_sidecar_path(store: ResultStore) -> Path:
    """Where *store*'s planned-cell-count sidecar lives.

    A SIDECAR rather than a header line inside the JSONL (issue
    athenaeum#1751): ``ResultStore`` is strictly append-only and every
    reader — ``completed_keys``, ``load_rollout_rows`` — assumes every line
    is a cell row. A header would have to be special-cased in both, and a
    resumed run would have to decide whether to rewrite it. A separate file
    written once at run start costs neither.
    """
    return store.path.with_name(store.path.name + PLANNED_SIDECAR_SUFFIX)


def write_planned_cells(store: ResultStore, planned: int) -> None:
    """Record how many cells the run that is STARTING intends to complete.

    Written before the first cell runs, so a run killed mid-grid (the
    60-minute job timeout this issue exists for) still leaves behind enough
    to tell "all 1392 cells, complete" from "the 300 that fit". Rewritten on
    a resume with the same planned total, which is by construction the full
    grid's size, not the remaining count.
    """
    path = planned_sidecar_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"planned_cells": planned}) + "\n", encoding="utf-8")


def read_planned_cells(store: ResultStore) -> int | None:
    """The planned cell count for *store*, or ``None`` when unknowable.

    ``None`` — a missing, empty or unparseable sidecar — means "cannot tell
    whether this store is partial", which renders WITHOUT a partial banner.
    That is deliberate: a store from before this issue, or one whose sidecar
    was lost, must not be libelled as partial on no evidence.
    """
    path = planned_sidecar_path(store)
    if not path.exists():
        return None
    try:
        planned = json.loads(path.read_text(encoding="utf-8"))["planned_cells"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None
    return int(planned) if isinstance(planned, int) else None
