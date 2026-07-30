# SPDX-License-Identifier: Apache-2.0
"""Backlog-drain ETA advisor + one-command supervised API+batch drain (issue #470).

When the raw-intake backlog outgrows the nightly caps, the operator used to find
out only by reading logs: the DEGRADED summary reports COUNTS, not time-to-drain,
and the API+batch remedy lived as tribal knowledge spread across env vars and
flags. This module closes both gaps:

* **ETA advisor (Feature 1)** — pure estimators over the #378 spend ledger that
  project how long the backlog will take to drain at the OBSERVED nightly
  throughput (not a hardcoded guess), plus :func:`build_advisory`, which
  :func:`athenaeum.librarian.run` emits as a WARNING (and ``athenaeum status``
  surfaces) when that projection exceeds ``librarian.drain_warn_days``.
* **Drain orchestrator (Feature 2)** — :func:`run_drain`, a thin loop over the
  existing :func:`athenaeum.librarian.run` machinery. It forces the API+batch
  path (the #236 50%-token-discount transport) and an UNBOUNDED run (batch
  block-polls; a finite deadline is the known cwc#615 failure mode), and guards
  a MANDATORY cumulative dollar ceiling across every intake window.

Athenaeum performs no credential handling (issue #284/#330): the drain requires
``ANTHROPIC_API_KEY`` in the environment and errors out naming that requirement
if it is absent — it never mints, guesses, or hardcodes a credential.

The estimate promises COST plus "hours, not nights", never wall-clock precision:
same-page merges serialize on the batch path (the deliberate #236 grouping), so
the advisor's night count is a caps/provider projection, not a runtime promise.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from athenaeum import spend
from athenaeum.models import _rates_for_model

log = logging.getLogger("athenaeum")

#: Stable, machine-greppable prefix for the end-of-run backlog-drain advisor
#: WARNING (mirrors ``RUN_SUMMARY_PREFIX`` in :mod:`athenaeum.librarian`).
DRAIN_ADVISOR_PREFIX = "backlog-drain-advisor"

#: Representative batch-tier model used to PRICE the up-front drain cost estimate
#: and the advisor's suggested budget. The entity tiers span haiku/sonnet/opus;
#: sonnet is the tier-2/tier-3 workhorse, so it is the honest single-model proxy
#: for a coarse estimate. Priced via :func:`athenaeum.models._rates_for_model`.
DRAIN_ESTIMATE_MODEL = "claude-sonnet-4"

#: Coarse per-file token fallbacks used ONLY when the ledger carries no usable
#: throughput history (no prior librarian run recorded ``files_processed``).
#: Deliberately coarse: the estimate promises cost + "hours, not nights".
DEFAULT_AVG_INPUT_TOKENS_PER_FILE = 20_000
DEFAULT_AVG_OUTPUT_TOKENS_PER_FILE = 1_500

#: Anthropic Messages Batch API discount (issue #236): batch-attributed tokens
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
# Feature 1 — ETA advisor estimators (pure; unit-tested directly)
# ---------------------------------------------------------------------------


def _librarian_records_with_files(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ledger records from librarian runs that recorded a positive files count.

    A ``files_processed`` field is present only on records written after issue
    #470 (older records lack it and are skipped — they cannot inform a rate).
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
      carries no usable history yet (issue #470: "fall back to this-run's rate");
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


def estimate_drain_cost_usd(
    *,
    backlog: int,
    avg_input_per_file: float,
    avg_output_per_file: float,
    model: str = DRAIN_ESTIMATE_MODEL,
    batch: bool = True,
) -> float:
    """Coarse USD to drain *backlog* files at *model* list prices.

    ``backlog × avg-tokens-per-file × per-MTok rate``, with the #236 batch
    discount applied when *batch* is set. Priced via the single per-model rate
    table in :mod:`athenaeum.models` (never a second hardcoded price site).
    """
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
    """A backlog-drain ETA advisory (issue #470, Feature 1)."""

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
    model: str = DRAIN_ESTIMATE_MODEL,
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


# ---------------------------------------------------------------------------
# Feature 2 — drain orchestrator + pre-flight guards
# ---------------------------------------------------------------------------


def check_api_key(env: dict[str, str] | None = None) -> str | None:
    """Return an error string when ``ANTHROPIC_API_KEY`` is absent, else None."""
    environ = env if env is not None else os.environ
    if not environ.get("ANTHROPIC_API_KEY"):
        return (
            "athenaeum drain requires ANTHROPIC_API_KEY in the environment: the "
            "drain runs the metered API + Batch path (issue #236) and athenaeum "
            "performs no credential handling (issue #284/#330). Set "
            "ANTHROPIC_API_KEY and retry."
        )
    return None


def check_batch_deadline(*, max_runtime: int) -> str | None:
    """Return an error string when a finite run deadline is in effect (cwc#615).

    The drain always runs batch mode, which block-polls the Anthropic Batch API;
    a finite wall-clock deadline kills the run mid-batch, wasting the submitted
    (already-billed) batch. The drain therefore requires an UNBOUNDED run.
    """
    if (
        isinstance(max_runtime, int)
        and not isinstance(max_runtime, bool)
        and max_runtime > 0
    ):
        return (
            f"athenaeum drain requires an unbounded run, but a finite run deadline "
            f"is in effect (max_runtime={max_runtime}s via ATHENAEUM_MAX_RUNTIME / "
            f"librarian.max_runtime). Batch mode block-polls the Anthropic Batch "
            f"API; a bounded window is the cwc#615 failure mode (the run is killed "
            f"mid-batch, wasting the submitted batch). Unset the deadline (or set "
            f"it to 0) and retry."
        )
    return None


def resolve_drain_runtime(
    config: dict[str, Any] | None, env: dict[str, str] | None = None
) -> int:
    """Resolve the run deadline the drain would face: env > yaml > 0 (unbounded).

    Unlike :func:`athenaeum.librarian.librarian_max_runtime` (default 3600s), the
    drain default is UNBOUNDED (0): the drain forces ``max_runtime=0``. A finite
    value here means the operator EXPLICITLY set one, which
    :func:`check_batch_deadline` then refuses.
    """
    environ = env if env is not None else os.environ
    raw_env = environ.get("ATHENAEUM_MAX_RUNTIME")
    if raw_env is not None:
        try:
            return int(raw_env)
        except (TypeError, ValueError):
            pass
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("max_runtime")
            if isinstance(raw, int) and not isinstance(raw, bool):
                return raw
    return 0


def _ledger_writable(ledger_path: Path) -> bool:
    """Return True if the spend ledger at *ledger_path* can be appended to.

    Issue #568 (H1): :func:`run_drain`'s MANDATORY cumulative dollar ceiling is
    computed by re-reading this ledger every window (:func:`drain_spend_usd`).
    If ledger writes fail silently — bad ``ATHENAEUM_SPEND_LEDGER`` path, wrong
    permissions, a full disk — ``drain_spend_usd`` returns ``0.0`` forever and
    the total spend becomes ``max_usd × number_of_windows``: unbounded real
    dollars, with the guard the docstring calls MANDATORY reading blind. So we
    probe writability up front using the same ``O_APPEND | O_CREAT`` open the
    real writer (:func:`spend._append_line`) uses, and abort loudly on failure
    rather than proceed. Creating an empty ledger file here is harmless — an
    empty ledger is valid and is exactly what the first real write would make.
    """
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(ledger_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.close(fd)
        return True
    except OSError as exc:
        log.error(
            "athenaeum drain: spend ledger %s is NOT writable (%s) — the "
            "cumulative dollar ceiling re-reads this ledger every window, so "
            "proceeding would spend up to $max_usd PER WINDOW with no "
            "cumulative bound. Aborting (issue #568). Fix ATHENAEUM_SPEND_LEDGER "
            "/ permissions / free disk space and retry.",
            ledger_path,
            exc,
        )
        return False


def drain_spend_usd(ledger_path: Path, *, since: datetime) -> float:
    """Sum metered API dollars recorded in the ledger since *since*.

    The drain-session cumulative-spend accounting: reads the #378 ledger
    (tolerating torn lines) and sums ``estimated_cost_usd`` across every
    non-subscription record written since the drain started. Subscription
    (``claude-cli``) rows are always $0 and skipped.
    """
    total = 0.0
    for record in spend.read_ledger(ledger_path, since=since):
        if record.get("provider") == spend.PROVIDER_CLAUDE_CLI:
            continue
        total += float(record.get("estimated_cost_usd", 0.0) or 0.0)
    return total


def run_drain(
    *,
    knowledge_root: Path,
    raw_root: Path,
    wiki_root: Path,
    max_usd: float,
    max_files: int | None,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    run_fn: Callable[..., int] | None = None,
    backlog_fn: Callable[[Path], int] | None = None,
    now: datetime | None = None,
) -> int:
    """Loop intake windows through the API+batch path until the backlog drains.

    Stops when (a) the raw backlog is empty, (b) the CUMULATIVE dollar ceiling
    (*max_usd*, mapped across the whole drain — not per window) trips, or (c) a
    window makes zero progress (stopped loudly, never spinning). Assumes the
    pre-flight guards (API key, deadline, confirmation) already passed — the CLI
    handler runs those. Forces ``provider=api``, ``batch_mode=True``, and
    ``max_runtime=0`` on every window. Returns an int exit code (0 on a clean
    completion / ceiling stop; nonzero on a zero-progress stop).

    *run_fn* / *backlog_fn* are injectable seams for testing; they default to
    :func:`athenaeum.librarian.run` and a live ``discover_raw_files`` count.
    """
    from athenaeum.librarian import discover_raw_files
    from athenaeum.librarian import run as librarian_run

    run_fn = run_fn or librarian_run
    backlog_fn = backlog_fn or (lambda root: len(discover_raw_files(root)))
    ledger_path = ledger_path or spend.resolve_ledger_path(config)

    # Issue #568 (H1): the cumulative dollar ceiling below is only as trustworthy
    # as the ledger it reads. Verify the ledger is writable BEFORE spending a
    # cent — abort rather than run a blind drain whose per-window ceiling would
    # never sum to a cumulative bound (see :func:`_ledger_writable`).
    if not _ledger_writable(ledger_path):
        return 1

    drain_start = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    # Force the API path: batch mode is Anthropic-endpoint-only (issue #330), so
    # a claude-cli-configured repo must be overridden for the drain. Loud, not
    # silent — the operator asked for a batch drain.
    if os.environ.get("ATHENAEUM_LLM_PROVIDER") != "api":
        log.info("athenaeum drain: forcing provider=api (batch mode is API-only).")
        os.environ["ATHENAEUM_LLM_PROVIDER"] = "api"

    window = 0
    while True:
        spent = drain_spend_usd(ledger_path, since=drain_start)
        remaining = max_usd - spent
        if remaining <= 0:
            log.warning(
                "athenaeum drain: cumulative spend ceiling reached "
                "($%.2f/$%.2f across %d window(s)) — stopping.",
                spent,
                max_usd,
                window,
            )
            return 0
        backlog = backlog_fn(raw_root)
        if backlog <= 0:
            log.info(
                "athenaeum drain: raw backlog empty — done "
                "(%d window(s), $%.2f spent).",
                window,
                spent,
            )
            return 0

        # Map the drain's REMAINING budget onto the per-run dollar ceiling for
        # THIS window (env wins over yaml inside run(), issue #378). The
        # cumulative guard is the loop re-reading spend from the ledger each pass.
        os.environ["ATHENAEUM_SPEND_MAX_USD_PER_RUN"] = f"{remaining:.6f}"
        log.info(
            "athenaeum drain: window %d — backlog %d, $%.2f of $%.2f spent, "
            "$%.2f remaining this window.",
            window + 1,
            backlog,
            spent,
            max_usd,
            remaining,
        )

        rc = run_fn(
            raw_root=raw_root,
            wiki_root=wiki_root,
            knowledge_root=knowledge_root,
            max_files=max_files,
            max_runtime=0,
            batch_mode=True,
            install_signal_handlers=False,
        )
        window += 1

        new_backlog = backlog_fn(raw_root)
        if new_backlog >= backlog:
            log.error(
                "athenaeum drain: window %d made ZERO progress (backlog %d → "
                "%d) — stopping loudly to avoid a spin. Inspect "
                "wiki/_deferred_work.md and any failed files.",
                window,
                backlog,
                new_backlog,
            )
            return 1
        if rc not in (0, 124):
            # A window that still made progress but exited nonzero (e.g. some
            # files failed): log it and keep draining — the zero-progress guard
            # above terminates the loop once only-undrainable files remain.
            log.warning(
                "athenaeum drain: window %d run exited %d but made progress "
                "(%d → %d); continuing.",
                window,
                rc,
                backlog,
                new_backlog,
            )
