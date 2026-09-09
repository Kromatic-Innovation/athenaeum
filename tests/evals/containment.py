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
from collections.abc import Callable, Iterable, Sequence
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


#: An agent rollout cell is a multi-turn tool-using run, not a single
#: completion — conservatively sized well above a single component-eval
#: call (contrast ``athenaeum.drain_advisor``'s per-FILE estimates, two
#: orders of magnitude smaller). Deliberately a round, clearly-a-guess
#: number rather than a false-precision figure: this is a PRE-FLIGHT
#: estimate, not a measurement.
DEFAULT_CELL_TOKEN_ESTIMATE = CellTokenEstimate(input_tokens=20_000, output_tokens=4_000)


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


class ResultStore:
    """Append-only JSONL store of completed grid-cell results.

    One line per completed cell, written with ``open(..., "a")`` (never
    rewritten in place), flushed and ``fsync``'d immediately after each
    write — so a hard kill loses at most the one cell that was in flight
    when it happened, never a previously-completed cell's row.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def completed_keys(self) -> set[str]:
        """Return every cell key already persisted, read once up front.

        Reads the whole file in one pass rather than being consulted
        per-cell mid-run — the file only grows monotonically during a run
        of :func:`run_grid`, so a snapshot taken at the start is exactly
        "what a resume must not repay for".
        """
        if not self.path.exists():
            return set()
        keys: set[str] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                keys.add(json.loads(line)["cell_key"])
        return keys

    def append(self, cell_key: str, payload: dict[str, Any]) -> None:
        """Append one completed cell's result, flushed and fsync'd."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"cell_key": cell_key, **payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def run_grid(
    cells: Iterable[GridCell],
    cell_runner: Callable[[GridCell], dict[str, Any]],
    store: ResultStore,
) -> list[dict[str, Any]]:
    """Run every cell in *cells* not already present in *store*, in order.

    ``store.completed_keys()`` is read exactly ONCE, before the loop starts
    — this is what makes a resume after a mid-grid kill cheap and correct:
    every cell already persisted (from before the kill) is skipped without
    calling *cell_runner* at all, and every remaining cell is executed and
    appended exactly once. Returns the payloads for cells executed on THIS
    call only (not the ones skipped as already-complete).
    """
    already_done = store.completed_keys()
    results: list[dict[str, Any]] = []
    for cell in cells:
        key = cell.cell_key()
        if key in already_done:
            continue
        payload = cell_runner(cell)
        store.append(key, payload)
        results.append(payload)
    return results
