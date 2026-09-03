# SPDX-License-Identifier: Apache-2.0
"""Ordinary-night steady-state table (issue athenaeum#713, artifact 3).

Reproduce with: ``athenaeum measure ordinary-night`` (see
:data:`REPRODUCE_COMMAND` below; issue athenaeum#1095 AC7 requires the exact
invocation live in this module's own docstring, not only ``CHANGELOG.md``).

The v6 comparator slice (child of athenaeum#709) does not start until this table
**closes** — shows an ordinary night's total call and wall-clock load,
INCLUDING the comparator regime's amortized addition, fitting inside both the
nightly call budget and the nightly wall-clock window. This module is the
read-only instrument that builds the table; it does not decide what to do
when the table fails to close (see :func:`closure_verdict`).

Reuses, never re-derives:

- **calls/file** — :func:`athenaeum.drain_advisor.observed_calls_per_file`
  over the spend ledger (same instrument artifact 2 uses).
- **wall-clock/file** — :func:`athenaeum.run_summary_log.entity_phase_wall_clock_per_file`
  over the operator's ``librarian-run-summary`` log lines (same instrument
  artifact 2 uses).
- **nightly call budget / nightly window** — the ACTUAL configured values,
  :func:`athenaeum.librarian.librarian_max_api_calls` (default 800) and
  :func:`athenaeum.librarian.librarian_max_runtime` (default 3600s) — never
  hardcoded a second time here.
- **comparator pair rate** — defaults from artifact 1's
  :class:`athenaeum.shadow_linkage.ShadowLinkageResult` (an operator passes
  the already-measured ``comparator_pair_count`` in), per the issue's
  explicit "defaults drawn from artifact 1" instruction.

**files/day of ordinary intake** is measured directly from ``raw/`` file
naming timestamps (``RAW_FILE_RE``, ``YYYYMMDDTHHMMSSZ``-prefixed) over a
trailing window — see :func:`measure_files_per_day`. This is a LOWER-BOUND
proxy: it counts files still present in ``raw/`` whose name-embedded
timestamp falls in the window, so a file already drained within the window
is not counted. Reported as such; not silently presented as a precise rate.

**The comparator, TTL re-check, invalidation-wave, and audit-sampling
subsystems do not exist as code yet** (out of THIS issue's scope). Their
per-night load is therefore an explicit, OPERATOR-SUPPLIED assumption
(:class:`AmortizedLoadAssumptions`), never a measured or invented figure —
every field is rendered with the assumption that produced it, per the AC.

**The closes/does-not-close decision is reported, never silently resolved.**
When the table does not close, :func:`closure_verdict` enumerates the three
documented options the design lock names (entity-phase compile moves to the
cheap classification tier / intake is rate-shaped / the latency target
relaxes further) WITHOUT picking one — that choice is an explicit operator
decision recorded in the issue's closing comment (per the issue body), not
this instrument's to make.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.librarian` (budget/window
resolvers + ``RUN_SUMMARY_PREFIX`` via :mod:`athenaeum.run_summary_log`),
:mod:`athenaeum.intake`, :mod:`athenaeum.drain_advisor`,
:mod:`athenaeum.measurement_docs`. None import this module back.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from athenaeum.config import load_config
from athenaeum.drain_advisor import observed_calls_per_file
from athenaeum.intake import discover_raw_files
from athenaeum.librarian import librarian_max_api_calls, librarian_max_runtime
from athenaeum.measurement_docs import append_measurement_section
from athenaeum.run_summary_log import entity_phase_wall_clock_per_file
from athenaeum.spend import read_ledger, resolve_ledger_path
from athenaeum.store import now_iso

SECTION_HEADING = "## Ordinary-night steady state"
REPRODUCE_COMMAND = "athenaeum measure ordinary-night"

#: Default trailing window (days) :func:`measure_files_per_day` scans.
DEFAULT_INTAKE_WINDOW_DAYS = 14

#: Wave duty-cycle target from the AC ("<=25% target").
WAVE_DUTY_CYCLE_TARGET = 0.25

#: The three documented options named in the issue body for a table that
#: does not close. Order matches the issue's own listing. Never auto-selected.
DOCUMENTED_NON_CLOSURE_OPTIONS: tuple[str, ...] = (
    "entity-phase compile moves to the cheap classification tier",
    "intake is rate-shaped",
    "the <=48h p95 latency target relaxes further",
)

ClosureVerdict = Literal["closes", "does-not-close", "indeterminate"]


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


def measure_files_per_day(
    knowledge_root: Path,
    *,
    config: dict[str, Any] | None = None,
    window_days: int = DEFAULT_INTAKE_WINDOW_DAYS,
    now: datetime | None = None,
) -> tuple[float, int]:
    """Lower-bound files/day of ordinary intake over a trailing window.

    Counts raw files under ``raw/`` (:func:`athenaeum.intake.discover_raw_files`)
    whose filename-embedded timestamp (``YYYYMMDDTHHMMSSZ``, the intake-time
    stamp every ``discover_raw_files`` hit carries via ``RawFile.timestamp``)
    falls within ``[now - window_days, now]``, divided by ``window_days``.

    Returns ``(files_per_day, matched_file_count)``. This is a LOWER BOUND:
    it only sees files STILL in ``raw/`` (not yet drained) — any file that
    arrived and was compiled within the window is invisible to this count.
    Files with an unparseable/absent timestamp are excluded, never counted
    as "now".
    """
    raw_root = knowledge_root / "raw"
    resolved_config = config if config is not None else load_config(knowledge_root)
    files = discover_raw_files(raw_root, resolved_config)

    reference = now if now is not None else datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=window_days)

    matched = 0
    for rf in files:
        stamp = rf.timestamp
        if not stamp:
            continue
        digits = stamp.rstrip("Zz")
        try:
            ts = datetime.strptime(digits, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if cutoff <= ts <= reference:
            matched += 1

    if window_days <= 0:
        return (0.0, matched)
    return (matched / window_days, matched)


@dataclass
class AmortizedLoadAssumptions:
    """Explicit, operator-supplied per-night load the (not-yet-built)
    comparator regime will add — every field is an ASSUMPTION, not a
    measurement, and is rendered with the note that produced it.

    ``comparator_pairs_per_night`` defaults from artifact 1's measured
    ``comparator_pair_count`` amortized over ``comparator_amortization_nights``
    (the issue's own "defaults drawn from artifact 1" instruction) —
    everything else defaults to zero (the conservative "regime not yet
    adding load" reversible default) until an operator supplies a real
    assumption.
    """

    comparator_pairs_per_night: float = 0.0
    comparator_calls_per_pair: float = 1.0
    comparator_seconds_per_pair: float = 0.0
    ttl_recheck_calls_per_night: float = 0.0
    ttl_recheck_seconds_per_night: float = 0.0
    invalidation_wave_calls_per_night: float = 0.0
    invalidation_wave_seconds_per_night: float = 0.0
    audit_sampling_calls_per_night: float = 0.0
    audit_sampling_seconds_per_night: float = 0.0
    note: str = (
        "comparator/TTL/invalidation-wave/audit-sampling subsystems are not yet "
        "built (out of athenaeum#713 scope) — every figure above is an explicit "
        "operator-supplied ASSUMPTION, not a measurement"
    )

    @property
    def total_calls_per_night(self) -> float:
        return (
            self.comparator_pairs_per_night * self.comparator_calls_per_pair
            + self.ttl_recheck_calls_per_night
            + self.invalidation_wave_calls_per_night
            + self.audit_sampling_calls_per_night
        )

    @property
    def total_seconds_per_night(self) -> float:
        return (
            self.comparator_pairs_per_night * self.comparator_seconds_per_pair
            + self.ttl_recheck_seconds_per_night
            + self.invalidation_wave_seconds_per_night
            + self.audit_sampling_seconds_per_night
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparator_pairs_per_night": self.comparator_pairs_per_night,
            "comparator_calls_per_pair": self.comparator_calls_per_pair,
            "comparator_seconds_per_pair": self.comparator_seconds_per_pair,
            "ttl_recheck_calls_per_night": self.ttl_recheck_calls_per_night,
            "ttl_recheck_seconds_per_night": self.ttl_recheck_seconds_per_night,
            "invalidation_wave_calls_per_night": self.invalidation_wave_calls_per_night,
            "invalidation_wave_seconds_per_night": self.invalidation_wave_seconds_per_night,
            "audit_sampling_calls_per_night": self.audit_sampling_calls_per_night,
            "audit_sampling_seconds_per_night": self.audit_sampling_seconds_per_night,
            "total_calls_per_night": self.total_calls_per_night,
            "total_seconds_per_night": self.total_seconds_per_night,
            "note": self.note,
        }


def wave_duty_cycle(nights_in_wave: int | None, total_nights: int | None) -> float | None:
    """``nights_in_wave / total_nights``, or ``None`` when either is unknown.

    ``None`` inputs (the default — the comparator's wave cadence is not yet
    defined) propagate to ``None`` rather than a fabricated ``0.0``.
    """
    if nights_in_wave is None or total_nights is None or total_nights <= 0:
        return None
    return nights_in_wave / total_nights


def closure_verdict(
    *,
    nightly_calls_total: float | None,
    nightly_call_budget: int,
    nightly_seconds_total: float | None,
    nightly_window_seconds: int,
) -> ClosureVerdict:
    """Does the ordinary night fit inside BOTH budgets?

    ``"indeterminate"`` when either total is unmeasurable (``None``) — an
    honest third state, never silently treated as closing.
    """
    if nightly_calls_total is None or nightly_seconds_total is None:
        return "indeterminate"
    calls_ok = nightly_calls_total <= nightly_call_budget
    window_ok = nightly_window_seconds <= 0 or nightly_seconds_total <= nightly_window_seconds
    return "closes" if (calls_ok and window_ok) else "does-not-close"


@dataclass
class OrdinaryNightResult:
    """Full ordinary-night steady-state table."""

    files_per_day: float
    files_per_day_source: str
    files_per_day_sample_count: int
    intake_window_days: int
    calls_per_file: float | None
    calls_per_file_source: str
    wall_clock_per_file_seconds: float | None
    wall_clock_source: str
    ordinary_calls_total: float | None
    ordinary_seconds_total: float | None
    amortized: AmortizedLoadAssumptions
    nightly_calls_total: float | None
    nightly_seconds_total: float | None
    nightly_call_budget: int
    nightly_window_seconds: int
    verdict: ClosureVerdict
    nights_in_wave: int | None
    total_nights: int | None
    duty_cycle: float | None
    athenaeum_version: str
    git_sha: str
    generated: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated": self.generated,
            "files_per_day": self.files_per_day,
            "files_per_day_source": self.files_per_day_source,
            "files_per_day_sample_count": self.files_per_day_sample_count,
            "intake_window_days": self.intake_window_days,
            "calls_per_file": self.calls_per_file,
            "calls_per_file_source": self.calls_per_file_source,
            "wall_clock_per_file_seconds": self.wall_clock_per_file_seconds,
            "wall_clock_source": self.wall_clock_source,
            "ordinary_calls_total": self.ordinary_calls_total,
            "ordinary_seconds_total": self.ordinary_seconds_total,
            "amortized": self.amortized.to_dict(),
            "nightly_calls_total": self.nightly_calls_total,
            "nightly_seconds_total": self.nightly_seconds_total,
            "nightly_call_budget": self.nightly_call_budget,
            "nightly_window_seconds": self.nightly_window_seconds,
            "verdict": self.verdict,
            "nights_in_wave": self.nights_in_wave,
            "total_nights": self.total_nights,
            "duty_cycle": self.duty_cycle,
            "athenaeum_version": self.athenaeum_version,
            "git_sha": self.git_sha,
        }


def build_ordinary_night_table(
    knowledge_root: Path,
    *,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    summary_log_records: list | None = None,
    amortized: AmortizedLoadAssumptions | None = None,
    nights_in_wave: int | None = None,
    total_nights: int | None = None,
    intake_window_days: int = DEFAULT_INTAKE_WINDOW_DAYS,
    now: datetime | None = None,
    repo_root: Path | None = None,
    calls_per_file: float | None = None,
    files_per_day: float | None = None,
    wall_clock_per_file_seconds: float | None = None,
) -> OrdinaryNightResult:
    """Build the ordinary-night steady-state table.

    Args:
        knowledge_root: Root of the knowledge directory.
        summary_log_records: Pre-parsed run-summary records (see
            :func:`athenaeum.run_summary_log.parse_run_summary_log`); ``None``
            means "no log available" — wall-clock figures report n/a.
        amortized: Explicit comparator/TTL/wave/audit load assumptions.
            ``None`` uses :class:`AmortizedLoadAssumptions`'s all-zero
            default (the conservative "regime adds nothing yet" state).
        nights_in_wave, total_nights: Wave-cadence inputs for the duty-cycle
            check; ``None`` (default) reports the duty cycle as n/a.
        calls_per_file: Operator-supplied override for calls/file (issue
            athenaeum#1095 AC5). ``None`` (default) re-derives it from the
            spend ledger via :func:`athenaeum.drain_advisor.observed_calls_per_file`,
            exactly as before this override existed.
        files_per_day: Operator-supplied override for files/day of ordinary
            intake (issue athenaeum#1095 AC5). ``None`` (default) re-derives
            it via :func:`measure_files_per_day` — the trailing-window
            lower-bound scan of ``raw/`` is skipped entirely when an override
            is supplied, so ``files_per_day_sample_count`` reports ``0`` in
            that case (no measurement was taken to sample-count).
        wall_clock_per_file_seconds: Operator-supplied override for
            wall-clock/file (issue athenaeum#1095 AC5). ``None`` (default)
            re-derives it from ``summary_log_records``, exactly as before
            this override existed.
    """
    resolved_config = config if config is not None else load_config(knowledge_root)

    if files_per_day is not None:
        resolved_files_per_day = files_per_day
        sample_count = 0
        files_per_day_source = "operator-supplied"
    else:
        resolved_files_per_day, sample_count = measure_files_per_day(
            knowledge_root, config=resolved_config, window_days=intake_window_days, now=now
        )
        files_per_day_source = "measured (trailing window, lower bound)"

    ledger = read_ledger(
        resolve_ledger_path(
            resolved_config, cache_dir=cache_dir, wiki_root=knowledge_root / "wiki"
        )
    )
    resolved_calls_per_file: float | None
    if calls_per_file is not None:
        resolved_calls_per_file = calls_per_file
        calls_source = "operator-supplied"
    else:
        resolved_calls_per_file = observed_calls_per_file(ledger)
        calls_source = (
            "ledger"
            if resolved_calls_per_file is not None
            else "none (no librarian ledger history)"
        )

    if wall_clock_per_file_seconds is not None:
        resolved_wall_clock_per_file: float | None = wall_clock_per_file_seconds
        wall_clock_source = "operator-supplied"
    else:
        resolved_wall_clock_per_file = None
        wall_clock_source = "none (no run-summary log provided)"
        if summary_log_records:
            result = entity_phase_wall_clock_per_file(list(summary_log_records))
            if result is not None:
                resolved_wall_clock_per_file, _n = result
                wall_clock_source = "run-summary log (entity phase)"
            else:
                wall_clock_source = (
                    "none (run-summary log provided but no usable entity-phase data)"
                )

    ordinary_calls_total = (
        resolved_calls_per_file * resolved_files_per_day
        if resolved_calls_per_file is not None
        else None
    )
    ordinary_seconds_total = (
        resolved_wall_clock_per_file * resolved_files_per_day
        if resolved_wall_clock_per_file is not None
        else None
    )

    resolved_amortized = amortized if amortized is not None else AmortizedLoadAssumptions()

    nightly_calls_total = (
        ordinary_calls_total + resolved_amortized.total_calls_per_night
        if ordinary_calls_total is not None
        else None
    )
    nightly_seconds_total = (
        ordinary_seconds_total + resolved_amortized.total_seconds_per_night
        if ordinary_seconds_total is not None
        else None
    )

    call_budget = librarian_max_api_calls(resolved_config)
    window_seconds = librarian_max_runtime(resolved_config)

    verdict = closure_verdict(
        nightly_calls_total=nightly_calls_total,
        nightly_call_budget=call_budget,
        nightly_seconds_total=nightly_seconds_total,
        nightly_window_seconds=window_seconds,
    )

    return OrdinaryNightResult(
        files_per_day=resolved_files_per_day,
        files_per_day_source=files_per_day_source,
        files_per_day_sample_count=sample_count,
        intake_window_days=intake_window_days,
        calls_per_file=resolved_calls_per_file,
        calls_per_file_source=calls_source,
        wall_clock_per_file_seconds=resolved_wall_clock_per_file,
        wall_clock_source=wall_clock_source,
        ordinary_calls_total=ordinary_calls_total,
        ordinary_seconds_total=ordinary_seconds_total,
        amortized=resolved_amortized,
        nightly_calls_total=nightly_calls_total,
        nightly_seconds_total=nightly_seconds_total,
        nightly_call_budget=call_budget,
        nightly_window_seconds=window_seconds,
        verdict=verdict,
        nights_in_wave=nights_in_wave,
        total_nights=total_nights,
        duty_cycle=wave_duty_cycle(nights_in_wave, total_nights),
        athenaeum_version=_get_version(),
        git_sha=_get_git_sha(repo_root),
        generated=now_iso(),
    )


_VERDICT_SENTENCE = {
    "closes": (
        "The ordinary night CLOSES: total call and wall-clock load fits inside both budgets."
    ),
    "does-not-close": (
        "The ordinary night DOES NOT CLOSE: at least one of the call/wall-clock "
        "budgets is exceeded."
    ),
    "indeterminate": (
        "The ordinary night's closure is INDETERMINATE: calls/file or "
        "wall-clock/file could not be measured in this environment."
    ),
}


def render_snapshot_entry(result: OrdinaryNightResult) -> str:
    """Render one dated ``### Snapshot ...`` sub-entry for the shared docs file."""

    def _n(v: float | None) -> str:
        return f"{v:.2f}" if v is not None else "n/a"

    duty_cycle_str = (
        f"{result.duty_cycle:.1%}"
        if result.duty_cycle is not None
        else "n/a (wave cadence not yet defined)"
    )

    files_per_day_line = (
        f"- files_per_day (ordinary intake, lower bound over trailing "
        f"{result.intake_window_days}d, n={result.files_per_day_sample_count}): "
        f"{result.files_per_day:.3f}"
        if result.files_per_day_source != "operator-supplied"
        else f"- files_per_day: {result.files_per_day:.3f} [{result.files_per_day_source}]"
    )
    lines = [
        f"### Snapshot {result.generated}",
        "",
        f"Reproduce with: `{REPRODUCE_COMMAND}`",
        "",
        f"**{_VERDICT_SENTENCE[result.verdict]}**",
        "",
        files_per_day_line,
        f"- calls_per_file: {_n(result.calls_per_file)} [{result.calls_per_file_source}]",
        f"- wall_clock_per_file_seconds: {_n(result.wall_clock_per_file_seconds)} "
        f"[{result.wall_clock_source}]",
        f"- ordinary_calls_total: {_n(result.ordinary_calls_total)}",
        f"- ordinary_seconds_total: {_n(result.ordinary_seconds_total)}",
        f"- amortized_calls_per_night: {result.amortized.total_calls_per_night:.2f} "
        f"({result.amortized.note})",
        f"- amortized_seconds_per_night: {result.amortized.total_seconds_per_night:.2f}",
        f"- nightly_calls_total: {_n(result.nightly_calls_total)} "
        f"vs nightly_call_budget: {result.nightly_call_budget}",
        f"- nightly_seconds_total: {_n(result.nightly_seconds_total)} "
        f"vs nightly_window_seconds: {result.nightly_window_seconds}",
        f"- wave_duty_cycle: {duty_cycle_str} vs target: {WAVE_DUTY_CYCLE_TARGET:.0%}",
        f"- athenaeum_version: {result.athenaeum_version}",
        f"- git_sha: {result.git_sha}",
        "",
    ]
    if result.verdict != "closes":
        lines += [
            "Documented options (NOT auto-selected — operator decision required, "
            "record the choice in this issue's closing comment):",
            "",
            *[f"- {opt}" for opt in DOCUMENTED_NON_CLOSURE_OPTIONS],
            "",
        ]
    return "\n".join(lines)


def write_snapshot(result: OrdinaryNightResult, *, docs_path: Path) -> Path:
    """Idempotently write/append this snapshot into *docs_path*. Never refuses —
    an ``indeterminate``/``does-not-close`` verdict is itself the meaningful
    result and must be committed, not withheld.
    """
    entry = render_snapshot_entry(result)
    return append_measurement_section(
        docs_path, section_heading=SECTION_HEADING, entry_markdown=entry
    )
