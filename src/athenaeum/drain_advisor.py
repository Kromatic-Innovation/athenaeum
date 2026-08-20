# SPDX-License-Identifier: Apache-2.0
"""Backlog-drain ETA advisor — pure estimators over the athenaeum#378 spend ledger.

Extracted from :mod:`athenaeum.drain` in issue athenaeum#640. This is "Feature 1" of the
issue athenaeum#470 drain work: pure functions that project how long the raw-intake
backlog will take to drain at the OBSERVED nightly throughput (not a hardcoded
guess), plus :func:`build_advisory`, which :func:`athenaeum.librarian.run` emits
as a WARNING (and ``athenaeum status`` surfaces) when that projection exceeds
``librarian.drain_warn_days``.

WHY IT LIVES HERE (issue athenaeum#640): both :mod:`athenaeum.librarian` and
:mod:`athenaeum.status` need :func:`build_advisory`, but they sit BELOW the
:mod:`athenaeum.drain` orchestrator (``drain`` calls ``librarian.run``). When
the advisor lived in ``drain``, ``librarian`` and ``status`` reached back UP
into ``drain`` for it — the ``librarian`` <-> ``drain`` / ``status`` -> ``drain``
back-edges that pinned the ``{drain, librarian, status}`` residual import SCC
(athenaeum#545 audit M8). Hoisting the advisor DOWN to this leaf — it imports only the
:mod:`athenaeum.config`, :mod:`athenaeum.models` and :mod:`athenaeum.tiers`
services, none of which import ``drain``/``librarian``/``status`` back —
dissolves that cycle: ``librarian``/``status``/``drain`` all now depend on this
module one-directionally.

The estimate promises COST plus "hours, not nights", never wall-clock precision:
same-page merges serialize on the batch path (the deliberate athenaeum#236 grouping), so
the advisor's night count is a caps/provider projection, not a runtime promise.

Factoring rule: ONLY the pure ETA estimators + :class:`DrainAdvisory` +
:func:`build_advisory` belong here. The supervised drain loop / orchestrator
(``run_drain`` and its pre-flight guards) stays in :mod:`athenaeum.drain`; CLI
arg parsing/gating stays in :mod:`athenaeum._cmd_drain`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from athenaeum.config import resolve_model
from athenaeum.models import _rates_for_model
from athenaeum.tiers import DEFAULT_WRITE_MODEL

#: Stable, machine-greppable prefix for the end-of-run backlog-drain advisor
#: WARNING (mirrors ``RUN_SUMMARY_PREFIX`` in :mod:`athenaeum.librarian`).
DRAIN_ADVISOR_PREFIX = "backlog-drain-advisor"


def _resolve_estimate_model(config: dict[str, Any] | None = None) -> str:
    """Model id used to PRICE the drain cost estimate and suggested budget.

    Issue athenaeum#571 (M18): resolved from the ``models.write`` knob (env
    ``ATHENAEUM_WRITE_MODEL`` > yaml ``models.write`` > code default
    :data:`athenaeum.tiers.DEFAULT_WRITE_MODEL`), NOT a hardcoded literal. The
    drain writes entities at the write model, so the estimate must price at
    whatever ``models.write`` actually resolves to — override it to Opus and the
    estimate follows, instead of silently understating cost (and the 1.25x
    budget margin) at a stale Sonnet rate. Priced via
    :func:`athenaeum.models._rates_for_model`.
    """
    return resolve_model("write", "ATHENAEUM_WRITE_MODEL", DEFAULT_WRITE_MODEL, config)

#: Coarse per-file token fallbacks used ONLY when the ledger carries no usable
#: throughput history (no prior librarian run recorded ``files_processed``).
#: Deliberately coarse: the estimate promises cost + "hours, not nights".
#: Typed ``float`` (not ``int``) because these feed the same
#: ``avg_input_per_file`` / ``avg_output_per_file`` slots as the observed,
#: division-derived averages from :func:`observed_tokens_per_file`.
DEFAULT_AVG_INPUT_TOKENS_PER_FILE: float = 20_000
DEFAULT_AVG_OUTPUT_TOKENS_PER_FILE: float = 1_500

#: Anthropic Messages Batch API discount (issue athenaeum#236): batch-attributed tokens
#: bill at half list price. Mirrors the ``0.5`` applied in
#: :meth:`athenaeum.models.TokenUsage._cost_for`.
BATCH_DISCOUNT = 0.5

#: How many recent librarian ledger records feed the throughput / tokens-per-file
#: rolling averages.
MAX_HISTORY = 14

#: Safety margin applied to the coarse cost estimate when suggesting a
#: ``--max-usd`` budget in the advisor command, so the suggested budget covers
#: the whole backlog rather than tripping the guard partway through.
_SUGGESTED_BUDGET_MARGIN = 1.25


# ---------------------------------------------------------------------------
# ETA advisor estimators (pure; unit-tested directly)
# ---------------------------------------------------------------------------


def _librarian_records_with_files(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ledger records from librarian runs that recorded a positive files count.

    A ``files_processed`` field is present only on records written after issue
    athenaeum#470 (older records lack it and are skipped — they cannot inform a rate).
    ``bool`` is rejected explicitly (``True``/``False`` are ``int`` subclasses).
    """
    out: list[dict[str, Any]] = []
    for record in records:
        if record.get("run_type") != "librarian":
            continue
        files = record.get("files_processed")
        if isinstance(files, bool) or not isinstance(files, int):
            continue
        if files <= 0:
            continue
        out.append(record)
    return out


def estimate_files_per_night(
    records: list[dict[str, Any]],
    *,
    this_run_files: int = 0,
    max_history: int = MAX_HISTORY,
) -> tuple[float, str]:
    """Observed files-drained-per-night and the source of the estimate.

    Returns ``(files_per_night, source)`` where *source* is:

    * ``"ledger"`` — averaged over the most recent librarian runs that recorded
      a ``files_processed`` count (the real observed throughput);
    * ``"this-run"`` — fallback to the count THIS run drained, when the ledger
      carries no usable history yet (issue athenaeum#470: "fall back to this-run's rate");
    * ``"none"`` — no history and this run drained nothing; the caller cannot
      project a finite ETA.
    """
    history = _librarian_records_with_files(records)
    if history:
        recent = history[-max_history:]
        total = sum(int(r["files_processed"]) for r in recent)
        return (total / len(recent), "ledger")
    if isinstance(this_run_files, int) and this_run_files > 0:
        return (float(this_run_files), "this-run")
    return (0.0, "none")


def estimate_eta_nights(backlog: int, files_per_night: float) -> float:
    """Project nights-to-drain: ``ceil(backlog / files_per_night)``.

    Returns ``0`` for an empty backlog and ``math.inf`` when the rate is
    unknown/zero (the backlog cannot be projected to drain).
    """
    if backlog <= 0:
        return 0
    if files_per_night <= 0:
        return math.inf
    return math.ceil(backlog / files_per_night)


def observed_tokens_per_file(
    records: list[dict[str, Any]],
    *,
    max_history: int = MAX_HISTORY,
) -> tuple[float, float] | None:
    """Average ``(input, output)`` tokens per file over recent librarian runs.

    Returns ``None`` when the ledger has no usable history (the caller then
    falls back to :data:`DEFAULT_AVG_INPUT_TOKENS_PER_FILE` /
    :data:`DEFAULT_AVG_OUTPUT_TOKENS_PER_FILE`).
    """
    history = _librarian_records_with_files(records)[-max_history:]
    total_files = sum(int(r["files_processed"]) for r in history)
    if total_files <= 0:
        return None
    total_input = sum(int(r.get("input_tokens", 0) or 0) for r in history)
    total_output = sum(int(r.get("output_tokens", 0) or 0) for r in history)
    return (total_input / total_files, total_output / total_files)


def observed_calls_per_file(
    records: list[dict[str, Any]],
    *,
    max_history: int = MAX_HISTORY,
) -> float | None:
    """Average API calls per file over recent librarian runs (issue athenaeum#713).

    Sibling to :func:`observed_tokens_per_file`, same ``_librarian_records_with_files``
    history window and the same ``None`` contract: ``None`` when the ledger has no
    usable history (never a fabricated ``0.0``). This is a RUN-LEVEL ratio — the
    ledger records ``api_calls`` per run, not per LLM-tier — so it mixes tier-1
    (zero-LLM), tier-2 (one classify call/file), and tier-3 (the bulk, content
    writing) calls. It is the closest figure this ledger can produce to a
    tier-3-only "calls/file" measurement without new per-knob call
    instrumentation (:attr:`athenaeum.models.TokenUsage.per_knob` tracks TOKENS
    per knob, not call counts) — callers that need a tier-3-only figure must
    label this a proxy, not re-derive a false precision from it.
    """
    history = _librarian_records_with_files(records)[-max_history:]
    total_files = sum(int(r["files_processed"]) for r in history)
    if total_files <= 0:
        return None
    total_calls = sum(int(r.get("api_calls", 0) or 0) for r in history)
    return total_calls / total_files


def estimate_drain_cost_usd(
    *,
    backlog: int,
    avg_input_per_file: float,
    avg_output_per_file: float,
    model: str | None = None,
    config: dict[str, Any] | None = None,
    batch: bool = True,
) -> float:
    """Coarse USD to drain *backlog* files at *model* list prices.

    *model* defaults to the resolved ``models.write`` id
    (:func:`_resolve_estimate_model`, issue athenaeum#571/M18) when not given explicitly,
    so the estimate tracks the model the drain actually writes with; pass
    *config* to route the yaml ``models.write`` knob.

    ``backlog × avg-tokens-per-file × per-MTok rate``, with the athenaeum#236 batch
    discount applied when *batch* is set. Priced via the single per-model rate
    table in :mod:`athenaeum.models` (never a second hardcoded price site).
    """
    if model is None:
        model = _resolve_estimate_model(config)
    input_rate, output_rate = _rates_for_model(model)  # USD per million tokens
    per_file = (
        avg_input_per_file * input_rate + avg_output_per_file * output_rate
    ) / 1_000_000
    cost = max(0, backlog) * per_file
    if batch:
        cost *= BATCH_DISCOUNT
    return cost


def _round_up_budget(value: float) -> float:
    """Round *value* up to the next 1/2/5×10^n "nice" number for a suggested budget."""
    if value <= 0:
        return 0.0
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5):
        candidate = step * magnitude
        if candidate >= value:
            return float(candidate)
    return float(10 * magnitude)


def _fmt_usd_arg(value: float) -> str:
    """Format a dollar amount as a clean CLI arg (``10`` not ``10.0``)."""
    return f"{value:g}"


@dataclass
class DrainAdvisory:
    """A backlog-drain ETA advisory (issue athenaeum#470, Feature 1)."""

    backlog: int
    files_per_night: float
    eta_nights: float
    rate_source: str
    suggested_max_usd: float
    command: str
    #: Human sentence WITHOUT the greppable prefix (for ``athenaeum status``).
    summary: str
    #: Full machine-greppable WARNING line (``DRAIN_ADVISOR_PREFIX`` + summary).
    line: str


def build_advisory(
    *,
    backlog: int,
    ledger_records: list[dict[str, Any]],
    warn_days: int,
    this_run_files: int = 0,
    model: str | None = None,
    config: dict[str, Any] | None = None,
) -> DrainAdvisory | None:
    """Build a :class:`DrainAdvisory` when the backlog ETA exceeds *warn_days*.

    Returns ``None`` — stays silent — when the backlog is empty or its projected
    ETA is at/below the threshold. An UNKNOWN rate (no history, nothing drained
    this run) yields ``eta_nights == inf``, which always exceeds the threshold:
    a backlog that cannot be projected to drain is exactly what warrants a
    heads-up. The advisory's command is a copy-pastable ``athenaeum drain``
    invocation with a suggested ``--max-usd`` budget derived from the coarse
    cost estimate.
    """
    if backlog <= 0:
        return None
    files_per_night, source = estimate_files_per_night(
        ledger_records, this_run_files=this_run_files
    )
    eta_nights = estimate_eta_nights(backlog, files_per_night)
    if eta_nights != math.inf and eta_nights <= warn_days:
        return None  # below threshold — stay silent

    tokens = observed_tokens_per_file(ledger_records)
    if tokens is None:
        avg_input, avg_output = (
            DEFAULT_AVG_INPUT_TOKENS_PER_FILE,
            DEFAULT_AVG_OUTPUT_TOKENS_PER_FILE,
        )
    else:
        avg_input, avg_output = tokens
    cost = estimate_drain_cost_usd(
        backlog=backlog,
        avg_input_per_file=avg_input,
        avg_output_per_file=avg_output,
        model=model,
        config=config,
        batch=True,
    )
    suggested = _round_up_budget(cost * _SUGGESTED_BUDGET_MARGIN)
    command = f"athenaeum drain --max-usd {_fmt_usd_arg(suggested)} --yes"

    if eta_nights == math.inf:
        eta_phrase = "an unknown number of nights (no throughput history)"
    else:
        eta_phrase = f"{int(eta_nights)} night(s)"
    summary = (
        f"{backlog} deferred file(s) ≈ {eta_phrase} to drain at current "
        f"caps/provider ({source} rate) — consider: {command}"
    )
    line = f"{DRAIN_ADVISOR_PREFIX}: {summary}"
    return DrainAdvisory(
        backlog=backlog,
        files_per_night=files_per_night,
        eta_nights=eta_nights,
        rate_source=source,
        suggested_max_usd=suggested,
        command=command,
        summary=summary,
        line=line,
    )
