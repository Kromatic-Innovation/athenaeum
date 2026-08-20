# SPDX-License-Identifier: Apache-2.0
"""Backlog price sheet + decision-inflow sensitivity table (issue athenaeum#713, artifact 2).

Prices what it costs to drain the raw-intake backlog under the plan the v6
design actually specifies: apply write-refusal classes and retention packs
RETROACTIVELY, before compiling (never compile what the model would refuse),
then drain the remainder as a budgeted batch-API burst on a cheaper tier.
The burst is paced by **queue capacity** (the human decision budget), not by
dollars — this module's sensitivity table is the instrument for that pacing
question: at a given decision-inflow rate (human decisions per 100 compiled
files), how many days does the backlog take to reach terminal disposition,
and where does that cross the 6-month horizon.

Reuses, never reimplements:

- **Backlog count** — :func:`athenaeum.intake.discover_raw_files`, the SAME
  function ``athenaeum status`` and the backlog-drain advisor already use.
  Always RE-COUNTED from the live ``raw/`` tree, never a copied literal (the
  issue's own explicit instruction: the ~3,644 anchor is stale by
  construction).
- **calls/file, tokens/file** — :mod:`athenaeum.drain_advisor`'s existing
  ledger-observed estimators (:func:`~athenaeum.drain_advisor.observed_calls_per_file`,
  :func:`~athenaeum.drain_advisor.observed_tokens_per_file`), reading the
  SAME spend ledger :mod:`athenaeum.spend` already writes — no second
  ledger, no re-derivation.
- **wall-clock/file** — :mod:`athenaeum.run_summary_log`'s parser over the
  ``librarian-run-summary`` log lines :mod:`athenaeum.librarian` already
  emits (issue athenaeum#464) — the ledger itself has no elapsed-time field.
- **Dollar pricing** — :func:`athenaeum.drain_advisor.estimate_drain_cost_usd`,
  which prices via :mod:`athenaeum.models`' per-MTok rate table — the SAME
  table :mod:`athenaeum.spend` (``athenaeum spend --reprice``) and
  ``athenaeum drain``'s own ETA advisor already price against. This module
  does not invent a second price list.

**The write-refusal / retention-pack pre-filter does not exist as code yet**
(v6 design lock §6.10/§9, not yet built — out of THIS issue's scope, whose
own "Out of scope" section rules out building the comparator/registry/queue).
So the "cost WITH the pre-filter applied" column cannot be computed from a
real classifier run here — it is accepted as an explicit, OPERATOR-SUPPLIED
``prefilter_excluded_fraction`` (the fraction of the backlog a *future* run of
that classifier would exclude). Passing ``None`` (the default — the honest
state until that classifier exists) reports that column as
not-yet-measurable rather than a fabricated saving; the "without" column is
always the real, fully-measured total.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.intake`,
:mod:`athenaeum.drain_advisor`, :mod:`athenaeum.run_summary_log`,
:mod:`athenaeum.spend`, :mod:`athenaeum.config`, :mod:`athenaeum.measurement_docs`
— none of which import this module back.
"""

from __future__ import annotations

import math
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.config import load_config
from athenaeum.drain_advisor import (
    DEFAULT_AVG_INPUT_TOKENS_PER_FILE,
    DEFAULT_AVG_OUTPUT_TOKENS_PER_FILE,
    estimate_drain_cost_usd,
    observed_calls_per_file,
    observed_tokens_per_file,
)
from athenaeum.intake import discover_raw_files
from athenaeum.measurement_docs import append_measurement_section
from athenaeum.run_summary_log import entity_phase_wall_clock_per_file
from athenaeum.spend import read_ledger, resolve_ledger_path

SECTION_HEADING = "## Backlog price sheet"
REPRODUCE_COMMAND = "athenaeum measure backlog-price"

#: Decision-inflow rates (human decisions per 100 COMPILED files) covered by
#: the sensitivity table — the AC's explicit "at least the range 5/100 ->
#: 50/100".
DEFAULT_INFLOW_RATES_PER_100: tuple[int, ...] = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50)

#: Human decision budget: at most this many pending-merge decisions a human
#: reviewer works through per day. Named in the issue body verbatim
#: ("≤20-items/day human budget"); no code elsewhere in this repo defines
#: this figure, so it is a documented, reversible default here — an operator
#: with a different real budget passes ``--human-daily-budget``.
DEFAULT_HUMAN_DAILY_BUDGET = 20

#: "6-month horizon" in days, used to flag which sensitivity rows breach it.
DEFAULT_SIX_MONTH_DAYS = 182

#: Fixed narrative text for the two required prose bullets (AC "price sheet"
#: bullets 3-4) — the queue-paced schedule statement and the triage-valve
#: floor. Rendered with the measured backlog substituted in; never a
#: fabricated conclusion, just the design's own stated policy.
_QUEUE_PACED_NOTE = (
    "The burst is paced by QUEUE CAPACITY, not by dollars: compile batches are "
    "throttled so the human decision budget stays true throughout the drain — "
    "never more compiled-and-awaiting-decision inventory than the decision-inflow "
    "rate can absorb at the stated daily budget."
)
_TRIAGE_VALVE_NOTE = (
    "Triage valve: the raw tail beyond the decision budget is cold-tiered "
    "UNCOMPILED (retrievable but never compiled), with one floor — every raw file "
    "matching a hot/warm recall hit or session reference from the trailing 6 "
    "months must compile or be individually human-waived."
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _get_version() -> str:
    from athenaeum import __version__

    return __version__


def _get_git_sha(repo_root: Path | None = None) -> str:
    cwd = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        sha = out.stdout.strip()
        return sha if sha else "unknown"
    except Exception:  # noqa: BLE001 — best-effort, never break the measurement run
        return "unknown"


@dataclass
class SensitivityRow:
    """One decision-inflow-rate row of the backlog-drain sensitivity table."""

    rate_per_100: int
    decisions: int
    days_to_terminal_disposition: float
    breaches_six_month_horizon: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate_per_100": self.rate_per_100,
            "decisions": self.decisions,
            "days_to_terminal_disposition": self.days_to_terminal_disposition,
            "breaches_six_month_horizon": self.breaches_six_month_horizon,
        }


def sensitivity_table(
    compiled_backlog: int,
    *,
    rates_per_100: Sequence[int] = DEFAULT_INFLOW_RATES_PER_100,
    human_daily_budget: int = DEFAULT_HUMAN_DAILY_BUDGET,
    six_month_days: int = DEFAULT_SIX_MONTH_DAYS,
) -> list[SensitivityRow]:
    """Days-to-terminal-disposition against the human decision budget, per inflow rate.

    ``decisions = round(compiled_backlog * rate / 100)``;
    ``days = round(decisions / human_daily_budget)``. Matches the issue's own
    worked example: 3,644 files at 10/100 -> 364 decisions -> ~18 days; at
    50/100 -> ~1,822 decisions -> ~91 days — the issue's own "18"/"91" are
    nearest-day figures (364/20 = 18.2, 1822/20 = 91.1), not a ceiling.
    """
    rows: list[SensitivityRow] = []
    for rate in rates_per_100:
        decisions = round(compiled_backlog * rate / 100)
        if human_daily_budget > 0:
            days: float = round(decisions / human_daily_budget)
        else:
            days = math.inf
        rows.append(
            SensitivityRow(
                rate_per_100=rate,
                decisions=decisions,
                days_to_terminal_disposition=days,
                breaches_six_month_horizon=(days > six_month_days),
            )
        )
    return rows


@dataclass
class PriceSheetResult:
    """Full backlog price sheet: measured backlog + costs + sensitivity table."""

    backlog_count: int
    calls_per_file: float | None
    calls_per_file_source: str
    avg_input_tokens_per_file: float
    avg_output_tokens_per_file: float
    tokens_source: str
    wall_clock_per_file_seconds: float | None
    wall_clock_source: str
    write_model: str
    cost_without_prefilter_usd: float
    wall_clock_without_prefilter_seconds: float | None
    prefilter_excluded_fraction: float | None
    cost_with_prefilter_usd: float | None
    wall_clock_with_prefilter_seconds: float | None
    sensitivity: list[SensitivityRow]
    athenaeum_version: str
    git_sha: str
    generated: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated": self.generated,
            "backlog_count": self.backlog_count,
            "calls_per_file": self.calls_per_file,
            "calls_per_file_source": self.calls_per_file_source,
            "avg_input_tokens_per_file": self.avg_input_tokens_per_file,
            "avg_output_tokens_per_file": self.avg_output_tokens_per_file,
            "tokens_source": self.tokens_source,
            "wall_clock_per_file_seconds": self.wall_clock_per_file_seconds,
            "wall_clock_source": self.wall_clock_source,
            "write_model": self.write_model,
            "cost_without_prefilter_usd": self.cost_without_prefilter_usd,
            "wall_clock_without_prefilter_seconds": self.wall_clock_without_prefilter_seconds,
            "prefilter_excluded_fraction": self.prefilter_excluded_fraction,
            "cost_with_prefilter_usd": self.cost_with_prefilter_usd,
            "wall_clock_with_prefilter_seconds": self.wall_clock_with_prefilter_seconds,
            "sensitivity": [row.to_dict() for row in self.sensitivity],
            "athenaeum_version": self.athenaeum_version,
            "git_sha": self.git_sha,
        }


def build_price_sheet(
    knowledge_root: Path,
    *,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    summary_log_records: list | None = None,
    prefilter_excluded_fraction: float | None = None,
    inflow_rates_per_100: Sequence[int] = DEFAULT_INFLOW_RATES_PER_100,
    human_daily_budget: int = DEFAULT_HUMAN_DAILY_BUDGET,
    six_month_days: int = DEFAULT_SIX_MONTH_DAYS,
    repo_root: Path | None = None,
) -> PriceSheetResult:
    """Build the backlog price sheet.

    Args:
        knowledge_root: Root of the knowledge directory (``raw/`` lives at
            ``knowledge_root / "raw"``).
        config: Optional resolved config dict; loaded lazily otherwise.
        cache_dir: Spend-ledger cache dir override (test seam).
        summary_log_records: Pre-parsed :class:`athenaeum.run_summary_log.RunSummaryRecord`
            list — the caller (CLI) is responsible for reading and parsing
            the operator's nightly log via
            :func:`athenaeum.run_summary_log.parse_run_summary_log`, since
            that log lives OUTSIDE this repo/knowledge root. ``None`` (the
            default) means "no log available" — wall-clock figures are
            reported as not-yet-measurable, never fabricated.
        prefilter_excluded_fraction: Operator-supplied fraction of the
            backlog a write-refusal/retention-pack pre-filter would exclude.
            ``None`` (default) — that classifier does not exist yet in this
            codebase — reports the "with prefilter" column as n/a.
    """
    from athenaeum.tiers import DEFAULT_WRITE_MODEL

    raw_root = knowledge_root / "raw"
    resolved_config = config if config is not None else load_config(knowledge_root)

    backlog = len(discover_raw_files(raw_root, resolved_config))

    ledger = read_ledger(resolve_ledger_path(resolved_config, cache_dir=cache_dir))
    calls_per_file = observed_calls_per_file(ledger)
    calls_source = "ledger" if calls_per_file is not None else "none (no librarian ledger history)"

    tokens = observed_tokens_per_file(ledger)
    if tokens is not None:
        avg_input, avg_output = tokens
        tokens_source = "ledger"
    else:
        avg_input, avg_output = (
            DEFAULT_AVG_INPUT_TOKENS_PER_FILE,
            DEFAULT_AVG_OUTPUT_TOKENS_PER_FILE,
        )
        tokens_source = "code-default fallback (no ledger history) — NOT a measured figure"

    wall_clock_per_file: float | None = None
    wall_clock_source = "none (no run-summary log provided)"
    if summary_log_records:
        result = entity_phase_wall_clock_per_file(list(summary_log_records))
        if result is not None:
            wall_clock_per_file, _n = result
            wall_clock_source = "run-summary log (entity phase)"
        else:
            wall_clock_source = "none (run-summary log provided but no usable entity-phase data)"

    write_model = DEFAULT_WRITE_MODEL

    cost_without = estimate_drain_cost_usd(
        backlog=backlog,
        avg_input_per_file=avg_input,
        avg_output_per_file=avg_output,
        model=write_model,
        config=resolved_config,
        batch=True,
    )
    wall_clock_without = (
        wall_clock_per_file * backlog if wall_clock_per_file is not None else None
    )

    cost_with: float | None = None
    wall_clock_with: float | None = None
    if prefilter_excluded_fraction is not None:
        remaining = round(backlog * max(0.0, 1.0 - prefilter_excluded_fraction))
        cost_with = estimate_drain_cost_usd(
            backlog=remaining,
            avg_input_per_file=avg_input,
            avg_output_per_file=avg_output,
            model=write_model,
            config=resolved_config,
            batch=True,
        )
        if wall_clock_per_file is not None:
            wall_clock_with = wall_clock_per_file * remaining

    sensitivity = sensitivity_table(
        backlog,
        rates_per_100=inflow_rates_per_100,
        human_daily_budget=human_daily_budget,
        six_month_days=six_month_days,
    )

    return PriceSheetResult(
        backlog_count=backlog,
        calls_per_file=calls_per_file,
        calls_per_file_source=calls_source,
        avg_input_tokens_per_file=avg_input,
        avg_output_tokens_per_file=avg_output,
        tokens_source=tokens_source,
        wall_clock_per_file_seconds=wall_clock_per_file,
        wall_clock_source=wall_clock_source,
        write_model=write_model,
        cost_without_prefilter_usd=cost_without,
        wall_clock_without_prefilter_seconds=wall_clock_without,
        prefilter_excluded_fraction=prefilter_excluded_fraction,
        cost_with_prefilter_usd=cost_with,
        wall_clock_with_prefilter_seconds=wall_clock_with,
        sensitivity=sensitivity,
        athenaeum_version=_get_version(),
        git_sha=_get_git_sha(repo_root),
        generated=_now_iso(),
    )


def render_snapshot_entry(result: PriceSheetResult) -> str:
    """Render one dated ``### Snapshot ...`` sub-entry for the shared docs file."""

    def _fmt(v: float | None, suffix: str = "") -> str:
        return f"{v:.2f}{suffix}" if v is not None else "n/a"

    def _or_na(v: float | None) -> str:
        return str(v) if v is not None else "n/a"

    prefilter_line = (
        "n/a — pre-filter fraction not supplied (the write-refusal/retention-pack "
        "classifier does not exist yet; pass --prefilter-excluded-fraction once it does)"
        if result.prefilter_excluded_fraction is None
        else (
            f"${_fmt(result.cost_with_prefilter_usd)} "
            f"({_fmt(result.wall_clock_with_prefilter_seconds, 's')}), "
            f"at {result.prefilter_excluded_fraction:.1%} excluded"
        )
    )
    lines = [
        f"### Snapshot {result.generated}",
        "",
        f"Reproduce with: `{REPRODUCE_COMMAND}`",
        "",
        f"- raw_backlog_count: {result.backlog_count} (re-counted, not copied)",
        f"- calls_per_file: {_or_na(result.calls_per_file)} [{result.calls_per_file_source}]",
        f"- avg_input_tokens_per_file: {result.avg_input_tokens_per_file:.0f} "
        f"[{result.tokens_source}]",
        f"- avg_output_tokens_per_file: {result.avg_output_tokens_per_file:.0f} "
        f"[{result.tokens_source}]",
        f"- wall_clock_per_file_seconds: {_or_na(result.wall_clock_per_file_seconds)} "
        f"[{result.wall_clock_source}]",
        f"- write_model (priced against): {result.write_model}",
        f"- cost_without_prefilter_usd: ${_fmt(result.cost_without_prefilter_usd)} "
        f"({_fmt(result.wall_clock_without_prefilter_seconds, 's')})",
        f"- cost_with_prefilter: {prefilter_line}",
        f"- athenaeum_version: {result.athenaeum_version}",
        f"- git_sha: {result.git_sha}",
        "",
        f"{_QUEUE_PACED_NOTE}",
        "",
        f"{_TRIAGE_VALVE_NOTE}",
        "",
        "Sensitivity table (decision-inflow rate -> days to terminal disposition):",
        "",
        "| rate/100 compiled | decisions | days | breaches 6mo |",
        "|---|---|---|---|",
    ]
    for row in result.sensitivity:
        days_str = "inf" if math.isinf(row.days_to_terminal_disposition) else str(
            int(row.days_to_terminal_disposition)
        )
        lines.append(
            f"| {row.rate_per_100} | {row.decisions} | {days_str} | "
            f"{'YES' if row.breaches_six_month_horizon else 'no'} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_snapshot(result: PriceSheetResult, *, docs_path: Path) -> Path:
    """Idempotently write/append this snapshot into *docs_path*.

    Refuses (raises :class:`ValueError`) when ``backlog_count == 0`` — an
    empty backlog has nothing to price.
    """
    if result.backlog_count == 0:
        raise ValueError(
            "refusing to write snapshot: backlog_count=0 — no raw intake files "
            "found under this knowledge root, so there is nothing to price"
        )
    entry = render_snapshot_entry(result)
    return append_measurement_section(
        docs_path, section_heading=SECTION_HEADING, entry_markdown=entry
    )
