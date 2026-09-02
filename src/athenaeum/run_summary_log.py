# SPDX-License-Identifier: Apache-2.0
"""``librarian-run-summary`` reader/writer (issues athenaeum#713, athenaeum#1102).

:mod:`athenaeum.librarian` emits one greppable ``librarian-run-summary
total_secs=... | entity secs=... calls=... files=... | ...`` line per run
(:func:`athenaeum.librarian._render_run_summary`, issue athenaeum#464) — but
only to whatever log sink the deployment wraps the run in (e.g. the nightly
cron's log file); prose in a log message, not itself persisted to a durable,
queryable ledger the way the spend ledger is. There is therefore no in-repo,
in-process source for **wall-clock-per-file** (issue athenaeum#713) or for a
**per-phase, per-run record a later run can aggregate over** without parsing
that prose (issue athenaeum#1102 AC1/AC2 — athenaeum#608's per-contract LLM
schema-mismatch rate needs exactly this: the ``resolution`` contract's 7
observations are a symptom of the entity phase's wall-clock overrun, and nothing
before athenaeum#1102 recorded that overrun anywhere durable).

Two halves, both read-only of the run they instrument (neither ever affects
phase logic, ordering, or exit code):

* **Parser** (athenaeum#713, unchanged): :func:`parse_run_summary_line` /
  :func:`parse_run_summary_text` / :func:`parse_run_summary_log` read
  ``librarian-run-summary`` lines from a log file the operator points them at
  (the SAME log the ``reasoning-tier-measurements.md`` precedent grepped by
  hand, issue athenaeum#784) into :class:`RunSummaryRecord`. Pure text parsing —
  never opens the log file itself except via the explicit ``_log`` suffix
  helper; callers pass in text (or a path) they already have.
* **Durable ledger** (athenaeum#1102, new): :func:`write_run_summary_record` appends
  ONE JSONL record per run to a durable, machine-readable ledger under the
  athenaeum cache dir — mirroring :mod:`athenaeum.spend`'s
  ``append_line_durable`` + :func:`athenaeum.config.resolve_cache_dir`
  convention exactly (see :func:`default_run_summary_ledger_path`'s docstring
  for why the CACHE dir, not ``wiki_root`` — the same reasoning
  :mod:`athenaeum.zero_yield` documents for its own state file).
  :func:`read_run_summary_ledger` reads it back, tolerating a torn trailing
  line exactly like :func:`athenaeum.spend.read_ledger`. This is the form
  AC2 asks for — a record, not a log line an operator has to have piped
  somewhere durable themselves.

Layering: L2 utility/leaf. Imports :mod:`athenaeum.config` (L2) and
:mod:`athenaeum.store` (L0/L1) for the ledger half — the same two modules
:mod:`athenaeum.spend` imports, for the same reason. Imports NOTHING from
:mod:`athenaeum.librarian` (issue athenaeum#1102: ``RUN_SUMMARY_PREFIX`` moved HERE,
which is now the format's canonical owner, and ``librarian.py`` imports it
back — the reverse of the pre-athenaeum#1102 direction). That is deliberate:
``RunContext.emit_run_summary`` (in ``librarian.py``) calls
:func:`write_run_summary_record` at the end of every run, so a
``run_summary_log -> librarian`` edge would close a 2-node import cycle the
day that call landed (see ``tests/test_import_graph_acyclic.py``, which walks
top-level AND function-local imports and fails on ANY multi-node SCC).
Keeping this module a true leaf — ``librarian.py`` depends on it, never the
other way — is what lets ``emit_run_summary`` import it directly.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_cache_dir
from athenaeum.store import append_line_durable

log = logging.getLogger(__name__)

#: Stable prefix for the one-line, key=value, machine-greppable run summary
#: (issue athenaeum#464). Canonical HERE (moved from ``librarian.py`` in issue
#: athenaeum#1102 so this module can stay a leaf — see the module docstring's
#: layering note); ``librarian.py`` imports this name rather than defining
#: its own copy, so a rename is still a one-line fix, just from the other
#: direction.
RUN_SUMMARY_PREFIX = "librarian-run-summary"

#: Matches ``key=value`` tokens where value has no whitespace — the shape
#: every field in a ``librarian-run-summary`` line uses (``total_secs=12.3``,
#: ``calls=6``, ``schema_fragments=a:b,c:d``, ...).
_KV_RE = re.compile(r"(\w+)=(\S+)")


@dataclass
class RunSummaryRecord:
    """One parsed ``librarian-run-summary`` line."""

    total_secs: float
    head_fields: dict[str, str] = field(default_factory=dict)
    #: ``{phase_name: {"secs": float, **other_fields_as_str}}``.
    phases: dict[str, dict[str, str]] = field(default_factory=dict)

    def phase_float(self, phase: str, key: str) -> float | None:
        """Best-effort float read of ``phases[phase][key]``; ``None`` if absent/unparseable."""
        raw = self.phases.get(phase, {}).get(key)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def phase_int(self, phase: str, key: str) -> int | None:
        raw = self.phases.get(phase, {}).get(key)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None


def parse_run_summary_line(line: str) -> RunSummaryRecord | None:
    """Parse one ``librarian-run-summary ...`` line. ``None`` if it doesn't match.

    Format (see :func:`athenaeum.librarian._render_run_summary`)::

        librarian-run-summary total_secs=12.3 schema_fragments=... | \
            entity secs=4.2 calls=6 created=2 updated=1 escalated=0 files=3 | \
            auto-memory secs=7.8 ... | retire secs=0.1
    """
    line = line.strip()
    idx = line.find(RUN_SUMMARY_PREFIX)
    if idx < 0:
        return None
    body = line[idx + len(RUN_SUMMARY_PREFIX) :].strip()
    segments = [seg.strip() for seg in body.split("|")]
    if not segments:
        return None

    head = segments[0]
    head_fields = dict(_KV_RE.findall(head))
    total_secs_raw = head_fields.pop("total_secs", None)
    if total_secs_raw is None:
        return None
    try:
        total_secs = float(total_secs_raw)
    except ValueError:
        return None

    phases: dict[str, dict[str, str]] = {}
    for seg in segments[1:]:
        if not seg:
            continue
        parts = seg.split(None, 1)
        phase_name = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        phases[phase_name] = dict(_KV_RE.findall(rest))

    return RunSummaryRecord(
        total_secs=total_secs, head_fields=head_fields, phases=phases
    )


def parse_run_summary_text(text: str) -> list[RunSummaryRecord]:
    """Parse every ``librarian-run-summary`` line found in *text* (a whole log)."""
    records: list[RunSummaryRecord] = []
    for line in text.splitlines():
        rec = parse_run_summary_line(line)
        if rec is not None:
            records.append(rec)
    return records


def parse_run_summary_log(path: Path) -> list[RunSummaryRecord]:
    """Read-only: parse every run-summary line in the log file at *path*.

    Returns an empty list if the file does not exist or cannot be read —
    a missing/unreadable log is "no history to report", never an error this
    read-only instrument raises.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return parse_run_summary_text(text)


def entity_phase_wall_clock_per_file(
    records: list[RunSummaryRecord],
) -> tuple[float, int] | None:
    """Observed entity-phase seconds-per-file, summed across *records*.

    Returns ``(seconds_per_file, total_files)`` over every run-summary record
    whose ``entity`` phase segment carries both ``secs=`` and ``files=`` with
    ``files > 0``; ``None`` when no record has usable entity-phase data (the
    honest "no history" state, mirroring
    :func:`athenaeum.drain_advisor.observed_tokens_per_file`'s ``None``
    contract for the sibling calls/tokens-per-file instrument).
    """
    total_secs = 0.0
    total_files = 0
    for rec in records:
        secs = rec.phase_float("entity", "secs")
        files = rec.phase_int("entity", "files")
        if secs is None or files is None or files <= 0:
            continue
        total_secs += secs
        total_files += files
    if total_files <= 0:
        return None
    return (total_secs / total_files, total_files)


# ---------------------------------------------------------------------------
# Durable ledger (issue athenaeum#1102 AC1/AC2)
# ---------------------------------------------------------------------------

#: Ledger schema version — bump additively if the record shape changes.
#: v2 (issue athenaeum#1184) adds the optional ``economics`` and ``alerts`` keys;
#: both are additive and a v1 reader that ignores unknown keys is unaffected.
#: v3 (issue athenaeum#1283) adds the optional ``refusal`` key — see
#: :func:`build_run_summary_ledger_record`'s docstring for its shape and
#: omission rule, and :func:`refusal_in_record` for why the version bump
#: itself matters here (not merely additive bookkeeping): a record written
#: under v1/v2 predates the athenaeum#1135 refusal verdict even existing on
#: ``RunContext``, so its ABSENT ``refusal`` key means "this record cannot
#: speak to whether that run was a refusal" — that is true of EVERY v1/v2
#: record, unconditionally, regardless of the key's presence in a v3+
#: record. A v3+ record's own ABSENT ``refusal`` key carries a DIFFERENT,
#: narrower meaning: the verdict was never evaluated for that particular
#: run (e.g. a wall-clock deadline trip in a pre-entity phase, handled by
#: ``RunContext.stop_on_deadline``, which emits a summary and returns
#: before ``_run_finalize_phase`` -- where the verdict is computed -- ever
#: runs) -- also "cannot speak", just for a run-shape reason rather than a
#: schema-age one. Only a v3+ record whose ``refusal`` key IS present
#: speaks to the verdict at all, via that dict's own ``tripped`` field --
#: see :func:`refusal_in_record`, the one place all three cases are read.
RUN_SUMMARY_LEDGER_VERSION = 3

#: Ledger filename under the cache dir (mirrors ``athenaeum.spend.LEDGER_FILENAME``).
RUN_SUMMARY_LEDGER_FILENAME = "run_summary.jsonl"


def default_run_summary_ledger_path(cache_dir: Path | None = None) -> Path:
    """Resolve the durable run-summary ledger path: ``<cache_dir>/run_summary.jsonl``.

    Under the CACHE dir (:func:`athenaeum.config.resolve_cache_dir`), not
    ``wiki_root``. :meth:`athenaeum.librarian.RunContext.emit_run_summary` is
    called at the END of a run on every exit path — including after the
    entity phase's own ``git_snapshot`` commit, the LAST commit point in the
    normal flow — so a write under the knowledge repo here would leave an
    uncommitted straggler file in the working tree every single run. Mirrors
    :mod:`athenaeum.zero_yield`'s documented reasoning ("Why the cache dir,
    not ``wiki_root``" in its module docstring) and
    :func:`athenaeum.spend.default_ledger_path` exactly.
    """
    base = cache_dir if cache_dir is not None else resolve_cache_dir()
    return Path(base).expanduser() / RUN_SUMMARY_LEDGER_FILENAME


def build_run_summary_ledger_record(
    profile: "list[tuple[str, float, dict]]",
    *,
    ts: datetime | None = None,
    economics: dict[str, Any] | None = None,
    alerts: "list[dict[str, Any]] | None" = None,
    refusal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one durable ledger record from a ``run()`` profile.

    Mirrors :func:`athenaeum.librarian._render_run_summary`'s input shape
    exactly (the same *profile* list :meth:`~athenaeum.librarian.RunContext.
    emit_run_summary` already threads to the prose renderer) but keeps every
    field JSON-native (int / float / str / bool) instead of stringifying it
    into a ``key=value`` token — the AC2 distinction between "a
    machine-readable record" and "prose inside a log message". ``phases`` is
    keyed by phase name (the same grouping :class:`RunSummaryRecord` uses for
    the parsed prose form), each value a ``{"secs": float, **fields}`` dict —
    including each phase's ``reason`` field (athenaeum#1102 AC1), so a later run can
    aggregate "completed" vs "entity-share"/"deadline"/"budget" yields across
    runs without re-parsing prose.

    *economics* / *alerts* (issue athenaeum#1184, schema v2) are the optional
    cost/matches-per-file regression fields — see
    :func:`compute_run_economics` and :func:`evaluate_regression_alerts`.
    Both are omitted (not written as ``null``) when not given, so a caller
    that has nothing to report (e.g. a merge-only/cluster-only run that never
    reaches the entity phase) writes a record identical in shape to a
    pre-athenaeum#1184 one.

    *refusal* (issue athenaeum#1283, schema v3) is the optional athenaeum#1135
    zero-progress-refusal verdict, built by the caller
    (:meth:`athenaeum.librarian.RunContext.emit_run_summary`) from the SAME
    ``ctx.librarian_refusal`` verdict ``_run_finalize_phase`` already
    computed once — a small JSON-native dict keyed on a ``tripped`` bool:
    ``{"tripped": True, "reason": ctx.entity_exit_reason, "files": 0}`` when
    the run WAS a refusal, ``{"tripped": False}`` when it was evaluated and
    was NOT. Pass ``None`` (the default) ONLY when the verdict was never
    evaluated for this run at all (``ctx.librarian_refusal is None`` —
    e.g. a wall-clock deadline trip via ``RunContext.stop_on_deadline``,
    which calls this before ``_run_finalize_phase`` ever runs); that is the
    one case that omits the ``refusal`` key entirely, mirroring the
    ``economics``/``alerts`` "omit, don't null" convention immediately
    above for the SAME reason (nothing to report) but a DIFFERENT trigger
    (unevaluated, not merely absent/zero) — an evaluated-clean run still
    writes ``{"tripped": False}``, not an omission, precisely so a reader
    cannot mistake "never evaluated" for "confirmed clean". This DOES mean
    a clean run's record is no longer byte-identical in shape to a
    pre-athenaeum#1283 one (it gains a `{"tripped": false}` refusal block) —
    a deliberate trade of that stability for an honest three-state record;
    the version bump already signals the shape changed. See
    :func:`refusal_in_record` for the reader that consumes all three states.
    """
    stamp = (ts if ts is not None else datetime.now(tz=timezone.utc)).astimezone(
        timezone.utc
    )
    total_secs = sum(secs for _phase, secs, _fields in profile)
    phases: dict[str, dict[str, Any]] = {}
    for phase, secs, fields in profile:
        phases[phase] = {"secs": round(secs, 3), **fields}
    record: dict[str, Any] = {
        "v": RUN_SUMMARY_LEDGER_VERSION,
        "ts": stamp.isoformat().replace("+00:00", "Z"),
        "total_secs": round(total_secs, 3),
        "phases": phases,
    }
    if economics is not None:
        record["economics"] = economics
    if alerts:
        record["alerts"] = alerts
    if refusal:
        record["refusal"] = refusal
    return record


def write_run_summary_record(
    profile: "list[tuple[str, float, dict]]",
    *,
    cache_dir: Path | None = None,
    ledger_path: Path | None = None,
    ts: datetime | None = None,
    economics: dict[str, Any] | None = None,
    alerts: "list[dict[str, Any]] | None" = None,
    refusal: dict[str, Any] | None = None,
) -> bool:
    """Append one durable run-summary record. Best-effort (issue athenaeum#1102 AC2).

    Mirrors :func:`athenaeum.spend.record_spend`'s contract: never raises —
    every failure is logged (debug level; this is pure observability, not a
    correctness-affecting ledger) and swallowed, since a ledger write must
    never break or slow the run it measures. No-ops (returns ``False``) when
    *profile* is empty — an early-abort path with nothing yet to report; the
    prose ``librarian-run-summary`` line is unconditional even then, so this
    is a deliberate, narrower gate (an empty record carries no aggregable
    information). Returns ``True`` when a record was written.

    *economics* / *alerts* (issue athenaeum#1184) and *refusal* (issue
    athenaeum#1283) pass straight through to
    :func:`build_run_summary_ledger_record` — see its docstring.
    """
    if not profile:
        return False
    try:
        record = build_run_summary_ledger_record(
            profile, ts=ts, economics=economics, alerts=alerts, refusal=refusal
        )
        target = (
            ledger_path
            if ledger_path is not None
            else default_run_summary_ledger_path(cache_dir)
        )
        append_line_durable(
            target, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        )
        return True
    except Exception as exc:  # noqa: BLE001 — a ledger write must never break a run
        log.debug("run-summary ledger write skipped (%s): %s", type(exc).__name__, exc)
        return False


# ---------------------------------------------------------------------------
# Cost/matches-per-file regression metrics (issue athenaeum#1184)
# ---------------------------------------------------------------------------
#
# The gap this closes: athenaeum#1177 catches ZERO yield (a run that produced
# nothing); nothing before this caught "yield at 3x the price" (a run that
# produced the right output while paying an order of magnitude more for it).
# Fan-out grew from 1.02 to ~10 matches/file over months with no metric and
# no test — discovered only when the operator ran out of API credits. This
# is the instrument that would have made that whole investigation
# unnecessary.
#
# Two denominators, on purpose (the "measured trap" the issue names): a raw
# file the entity loop DRAINED this run (``RunContext.files_processed_count``
# — completed to a terminal outcome, i.e. NOT deferred by a budget/deadline
# trip and NOT a hard processing failure; the same figure
# :mod:`athenaeum.spend`'s ledger row and the athenaeum#899 zero-yield alarm both
# already call "files processed") is not the same population as one that
# actually PRODUCED an action (created/updated an entity) — many drained
# files produce zero actions (a pure Tier-1 dedup skip, an escalate-only
# file), so cost-or-matches ÷ files-processed UNDERSTATES the true
# per-acting-file figure. Both denominators are recorded on every run;
# :data:`_RATCHETED_METRICS` below picks the ACTED denominator for
# cost/matches specifically, since that is the "real" economic unit the
# fan-out regression is about — processed-file counts are kept for context
# and for ``calls_per_file`` (an LLM call is spent per file PROCESSED,
# regardless of whether it acts, so that is its natural denominator).
#
# Scope caveat, stated once here rather than repeated on every field: `matched`
# sums post-junk-filter Tier-1 matches on the SYNCHRONOUS entity-loop path
# only (see ``RunContext.total_matched``'s docstring in librarian.py) — it
# includes old-format matches that land in ``result.skipped`` (they still
# dispatched a Tier-1 hit, even though Tier-3 never runs for them) and
# excludes the batch-API transport (off by default) and the rare per-file
# over-budget exception path. `cost_usd` is the WHOLE RUN's notional cost
# (entity phase + auto-memory/C2-C4 + retire + reresolve), not entity-phase-
# scoped, while `files_acted` IS entity-phase-scoped — an auto-memory spend
# swing with zero fan-out change will still move `cost_per_file_acted`. Both
# are documented limitations of reusing whole-run counters rather than
# knob-scoping the dollar figure (which would mean repricing outside the
# spend ledger's own arithmetic — exactly what the issue says not to do).

#: Baseline window: how many of the OLDEST prior runs in the ledger a
#: ratchet's baseline is computed over — deliberately the OLDEST slice of
#: history, not the most recent ("trailing") one. A trailing-window mean
#: chases exactly the slow drift this instrument exists to catch: athenaeum#1167's
#: own regression (1.02 -> ~10 matches/file, a ~2.6%/run compounding ramp
#: over ~90 nightly runs) moves a same-sized trailing mean by roughly the
#: same ~2.6%/run it is supposed to be measuring against, so the RATIO
#: between "now" and "an hour ago" never approaches the alert threshold even
#: as the absolute figure grows 10x. Anchoring to the earliest ``window``
#: runs instead gives a baseline that does not itself drift with the
#: regression, so the ratio grows with the regression and eventually trips.
#: An operator who deliberately re-baselines (e.g. after a real corpus-size
#: step change is accepted as the new normal) does so by trimming the
#: ledger's older lines, the same lever :mod:`athenaeum.spend` already
#: expects for its own ledger.
REGRESSION_BASELINE_WINDOW = 20

#: A run's ratcheted metric may exceed its rolling baseline by this
#: multiplier before an alert fires. A RATIO against a rolling baseline —
#: not a fixed absolute constant — is the point (issue athenaeum#1184's own
#: framing): the regression this instrument exists to catch is SLOW drift,
#: which a fixed threshold set generously enough not to false-positive on
#: day one would never trip either. Each successive small step looks fine
#: against a fixed number; ratcheting against the run's OWN recent history
#: is what makes accumulation visible.
REGRESSION_ALERT_RATIO = 3.0

#: Minimum prior samples required before a ratchet evaluates at all — a
#: baseline of 1-2 runs is noise, not a trend worth alerting on.
REGRESSION_MIN_SAMPLES = 5

#: Stable, greppable WARNING prefix — mirrors ``ZERO_YIELD_PREFIX`` /
#: ``STUCK_FILE_PREFIX`` / ``QUARANTINE_FILE_PREFIX`` in ``librarian.py``
#: (the existing convention this instrument surfaces through, per the
#: issue's "look at how existing warnings surface" instruction) so an
#: operator's nightly log sweep can grep this alongside the other run-state
#: alarms without parsing prose.
REGRESSION_ALERT_PREFIX = "librarian-econ-regression"


def _safe_ratio(numerator: float, denominator: int) -> float | None:
    """``numerator / denominator``, or ``None`` when *denominator* is 0.

    ``None`` (never 0.0 or an exception) is the honest value for "this ratio
    has no meaningful denominator this run" — e.g. ``matches_per_file_acted``
    when zero files acted. A silently-substituted 0.0 would read as "matches
    collapsed to zero", the opposite of "not computable".
    """
    return (numerator / denominator) if denominator > 0 else None


def compute_run_economics(
    *,
    files_processed: int,
    files_acted: int,
    matched: int,
    calls: int,
    merge_calls: int,
    merge_echoed_chars: int,
    cost_usd: float,
) -> dict[str, Any]:
    """Derive the athenaeum#1184 per-file economics from one run's raw counters.

    *cost_usd* should be the run's ``TokenUsage.notional_cost_usd`` — the
    SAME per-model pricing :mod:`athenaeum.spend` already uses for its own
    ledger rows (:func:`athenaeum.spend.build_record`'s ``notional_usd``),
    reused here rather than recomputed, per the issue's instruction to treat
    the spend ledger as the system of record. ``notional_cost_usd`` (not
    ``estimated_cost_usd``) is deliberate: ``estimated_cost_usd`` reads as
    literal $0 on the subscription (``claude-cli``) provider, which would
    make this instrument go blind on exactly the fleet that runs the
    nightly — ``notional_cost_usd`` is the same real token cost regardless
    of who is billed for it.

    Returns a flat dict (JSON-native, safe to embed directly in the
    ``run_summary.jsonl`` record) with BOTH denominators recorded — see the
    module-level comment above for why — plus the four ratios the issue's
    acceptance criteria name: ``cost_per_file``, ``matches_per_file``,
    ``calls_per_file`` (processed-denominator), ``echoed_chars_per_call``
    (denominated on merge calls, not files — see field docstring below).
    """
    return {
        "files_processed": files_processed,
        "files_acted": files_acted,
        "matched": matched,
        "calls": calls,
        "merge_calls": merge_calls,
        "echoed_chars": merge_echoed_chars,
        "cost_usd": round(cost_usd, 6),
        # "processed" variants: context, and calls_per_file's natural home
        # (an LLM call is spent per file PROCESSED regardless of outcome).
        "cost_per_file_processed": _safe_ratio(cost_usd, files_processed),
        "matches_per_file_processed": _safe_ratio(matched, files_processed),
        "calls_per_file_processed": _safe_ratio(calls, files_processed),
        # "acted" variants: the real per-acting-file figure the issue's
        # "measured trap" section calls out — the one a naive
        # cost/files_processed would understate.
        "cost_per_file_acted": _safe_ratio(cost_usd, files_acted),
        "matches_per_file_acted": _safe_ratio(matched, files_acted),
        # Denominated on MERGE CALLS, not files: each patch-attempt or
        # full-echo-fallback call has its own echoed-chars figure, and one
        # file can produce more than one merge call (a fallback is a SECOND
        # call, issue athenaeum#490's ~10x-output-cost path) — files-per-call
        # would blur exactly the "more retries, not more matches" distinction
        # calls_per_file exists to separate.
        "echoed_chars_per_call": _safe_ratio(merge_echoed_chars, merge_calls),
    }


#: Which economics keys are ratcheted against a rolling baseline, and in
#: what order alerts are reported. cost/matches ratchet on the ACTED
#: denominator (the real per-acting-file figure); calls ratchets on
#: PROCESSED (its natural denominator, see ``compute_run_economics``);
#: echoed-chars-per-call needs no processed/acted choice (denominated on
#: calls). Together these are exactly the issue's four named metrics.
_RATCHETED_METRICS: tuple[str, ...] = (
    "cost_per_file_acted",
    "matches_per_file_acted",
    "calls_per_file_processed",
    "echoed_chars_per_call",
)


def evaluate_regression_alerts(
    current: dict[str, Any],
    history: "list[dict[str, Any]]",
    *,
    window: int = REGRESSION_BASELINE_WINDOW,
    ratio: float = REGRESSION_ALERT_RATIO,
    min_samples: int = REGRESSION_MIN_SAMPLES,
) -> "list[dict[str, Any]]":
    """Ratchet *current* run's economics against an EARLY baseline of *history*.

    *current* is one :func:`compute_run_economics` result. *history* is a
    list of PRIOR runs' economics dicts, oldest-first (the same order
    :func:`read_run_summary_ledger` returns) — only the OLDEST *window*
    entries are used (see :data:`REGRESSION_BASELINE_WINDOW`'s docstring for
    why oldest, not trailing: a trailing baseline chases slow drift instead
    of catching it). Returns one alert dict per :data:`_RATCHETED_METRICS`
    key whose current value exceeds ``baseline * ratio``; empty when nothing
    tripped, including the "no history yet" and "fewer than *min_samples*
    prior runs" cases — an undersized baseline is not evidence of a
    regression, it is evidence there is no baseline yet.

    The baseline itself is the MINIMUM (not the mean) of the oldest-window
    samples: a mean over that window is still vulnerable if the regression
    had already started accumulating within it; the minimum is the most
    conservative "most normal this metric has ever looked" anchor, so a
    monotonically worsening metric is compared against its best-ever period,
    not an average that itself includes some of the drift.

    A ``None`` value (an unratioable metric this run — see
    :func:`_safe_ratio`) is skipped for both the current value and any
    history sample: it neither trips nor contributes to the baseline. A
    ``0.0`` sample (a real, legitimate value -- e.g. a fresh-entity file with
    ``matched == 0``, or ANY sample on the batch-API path where
    ``files_acted`` stays 0 by design) is ALSO excluded from the baseline,
    for a different reason: with a MIN-based baseline, one zero sample in the
    genesis window would floor ``baseline`` at 0 forever (the ``window`` is a
    fixed oldest-slice, never ages out), permanently disabling the ratchet
    for that metric's whole ledger lifetime via the ``baseline <= 0: continue``
    guard below. Excluding zeros from the SAMPLE POOL (not from *min_samples*
    accounting -- a window with real signal among the zeros still ratchets)
    is safer than keeping a zero-tolerant baseline: it costs at most a
    slightly later ratchet start, never a permanently-disabled one.
    """
    alerts: list[dict[str, Any]] = []
    window_records = history[:window] if window > 0 else history
    for metric in _RATCHETED_METRICS:
        samples = [
            r[metric]
            for r in window_records
            if isinstance(r.get(metric), (int, float))
            and not isinstance(r.get(metric), bool)
            and r[metric] > 0
        ]
        if len(samples) < min_samples:
            continue
        baseline = min(samples)
        if baseline <= 0:
            continue
        value = current.get(metric)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if value > baseline * ratio:
            alerts.append(
                {
                    "metric": metric,
                    "value": round(float(value), 6),
                    "baseline": round(baseline, 6),
                    "ratio": round(value / baseline, 3),
                    "threshold_ratio": ratio,
                    "samples": len(samples),
                }
            )
    return alerts


def build_economics_and_alerts(
    *,
    files_processed: int,
    files_acted: int,
    matched: int,
    calls: int,
    merge_calls: int,
    merge_echoed_chars: int,
    cost_usd: float,
    cache_dir: Path | None = None,
    ledger_path: Path | None = None,
) -> "tuple[dict[str, Any], list[dict[str, Any]]]":
    """One-call orchestration: compute this run's economics, then ratchet
    them against the durable ledger's own history (issue athenaeum#1184).

    Reads the SAME ledger this run is about to append to
    (:func:`read_run_summary_ledger`) BEFORE the append happens, so the
    current run is never compared against itself. Read failures degrade to
    "no history" (empty list) rather than raising — mirrors every other
    read in this module's fail-open contract; a missing/corrupt ledger must
    never block computing or reporting this run's own economics.
    """
    economics = compute_run_economics(
        files_processed=files_processed,
        files_acted=files_acted,
        matched=matched,
        calls=calls,
        merge_calls=merge_calls,
        merge_echoed_chars=merge_echoed_chars,
        cost_usd=cost_usd,
    )
    try:
        target = (
            ledger_path
            if ledger_path is not None
            else default_run_summary_ledger_path(cache_dir)
        )
        history = [
            rec["economics"]
            for rec in read_run_summary_ledger(target)
            if isinstance(rec.get("economics"), dict)
        ]
    except Exception as exc:  # noqa: BLE001 — observability must never raise
        log.debug("run-summary: economics history read skipped: %s", exc)
        history = []
    alerts = evaluate_regression_alerts(economics, history)
    return economics, alerts


def read_run_summary_ledger(
    ledger_path: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read durable run-summary ledger records, tolerating a torn trailing line.

    Mirrors :func:`athenaeum.spend.read_ledger` exactly: malformed lines (a
    crash mid-write, or hand-editing) are skipped, not fatal; a missing file
    reads as "no history yet" (``[]``). Optional ``since``/``until`` bounds
    filter by ``ts`` (inclusive lower, exclusive upper); a record with an
    unparseable ``ts`` is dropped when a bound is given.
    """
    if not ledger_path.exists():
        return []
    try:
        raw_text = ledger_path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn trailing write or hand-edit; skip
        if not isinstance(record, dict):
            continue
        if since is not None or until is not None:
            raw_ts = record.get("ts")
            parsed_ts: datetime | None = None
            if isinstance(raw_ts, str):
                try:
                    parsed_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                except ValueError:
                    parsed_ts = None
            if parsed_ts is None:
                continue
            if parsed_ts.tzinfo is None:
                parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
            if since is not None and parsed_ts < since:
                continue
            if until is not None and parsed_ts >= until:
                continue
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Source-starvation streaks (issue athenaeum#1291 AC3)
# ---------------------------------------------------------------------------
#
# The scheduler change (``athenaeum.intake.round_robin_by_source``) bounds the
# worst-case wait, but a bound is not a report: an operator still cannot see
# that a specific source is going hungry, and the pre-athenaeum#1291 signal --
# ``beyond_window`` ("plus N more beyond the max_files window", rendered by
# ``librarian._write_deferred_manifest``) -- is a COUNT. It reads as ordinary
# backpressure while potentially describing a permanent stall, because it never
# says WHICH files, so it can never say that the SAME ones are excluded every
# run.
#
# This computes the missing streak from the ledger athenaeum#1102 already
# writes, rather than adding a second piece of persisted state. The entity
# phase records its zero-slot sources for the run as the ``starved`` field on
# its profile segment; ``build_run_summary_ledger_record`` copies every phase
# field into the durable JSONL verbatim, so the field is already durable; and
# the streak is then just "how many consecutive trailing records also name it".

#: Entity-phase profile field naming this run's zero-slot sources, as one
#: comma-joined token (the same convention the ``reconciled`` field uses).
STARVATION_FIELD = "starved"

#: K in "K consecutive runs with pending intake and zero slots" -- the streak
#: at which a source is named in the run summary's head segment and a WARNING
#: fires. Three is the smallest streak that is unambiguously not a one-off
#: (one run is ordinary windowing; two could be two coincidentally busy runs).
STARVATION_STREAK_THRESHOLD = 3

#: Stable, greppable WARNING prefix -- mirrors :data:`REGRESSION_ALERT_PREFIX`
#: above, so an operator's existing nightly log sweep catches this without a
#: new channel to watch.
STARVATION_ALERT_PREFIX = "librarian-source-starvation"


def starved_sources_in_record(record: dict[str, Any]) -> set[str] | None:
    """This run's zero-slot sources from one ledger *record*.

    Returns ``None`` for a record whose entity phase never ran (a merge-only
    or cluster-only run, or an early deadline trip). That is deliberately
    distinct from ``set()`` ("the entity phase ran and starved nobody"):
    :func:`starvation_streaks` must not let a run that could not possibly
    schedule anything break a streak.
    """
    phases = record.get("phases")
    if not isinstance(phases, dict):
        return None
    entity = phases.get("entity")
    if not isinstance(entity, dict):
        return None
    token = entity.get(STARVATION_FIELD)
    if not isinstance(token, str):
        # The field is omitted entirely on a run that starved nobody (the
        # "render only when non-empty" convention every optional entity field
        # in the profile follows), so absence means an empty set, not None.
        return set()
    return {part.strip() for part in token.split(",") if part.strip()}


def starvation_streaks(
    starved_now: "Iterable[str]", history: "list[dict[str, Any]]"
) -> dict[str, int]:
    """Consecutive-run starvation streak per source, INCLUDING this run.

    *history* is the ledger's records oldest-first (exactly what
    :func:`read_run_summary_ledger` returns), read BEFORE this run's own
    record is appended -- so a source starved for the first time this run
    scores ``1``, and one starved on the two prior runs as well scores ``3``.

    Records with no entity phase are skipped rather than counted as a
    non-starving run (see :func:`starved_sources_in_record`).
    """
    prior = [
        starved
        for starved in (starved_sources_in_record(rec) for rec in history)
        if starved is not None
    ]
    streaks: dict[str, int] = {}
    for source in starved_now:
        streak = 1
        for entry in reversed(prior):
            if source not in entry:
                break
            streak += 1
        streaks[source] = streak
    return streaks


def read_starvation_streaks(
    starved_now: "Iterable[str]",
    *,
    cache_dir: Path | None = None,
    ledger_path: Path | None = None,
) -> dict[str, int]:
    """:func:`starvation_streaks` against the durable ledger. Best-effort.

    Mirrors :func:`build_economics_and_alerts`' fail-open contract exactly: a
    missing or corrupt ledger reads as "no history", and no failure here may
    ever raise into the run it is only observing. An empty *starved_now*
    short-circuits without touching the filesystem.
    """
    sources = list(starved_now)
    if not sources:
        return {}
    try:
        target = (
            ledger_path
            if ledger_path is not None
            else default_run_summary_ledger_path(cache_dir)
        )
        history = read_run_summary_ledger(target)
    except Exception as exc:  # noqa: BLE001 — observability must never raise
        log.debug("run-summary: starvation history read skipped: %s", exc)
        history = []
    return starvation_streaks(sources, history)



def starvation_priority(history: "list[dict[str, Any]]") -> list[str]:
    """The next run's scheduling priority head: LONGEST-STARVED SOURCE FIRST.

    This is what makes :func:`athenaeum.intake.round_robin_by_source`'s turn
    order rotate across runs (issue athenaeum#1291 AC1). Round-robin alone
    bounds the wait only while ``max_files`` is at least the number of
    sources; below that a FIXED turn order starves the same trailing sources
    on every run forever -- sort-position starvation again, merely at a
    different threshold.

    Rotation has to AGE, not just alternate. Feeding back the previous run's
    zero-slot sources in name order is not enough: a source can keep losing
    its turn to sources that were only starved once, and still wait
    unboundedly (verified -- 5 sources, a window of 2, and the last source is
    never scheduled). Ordering the head by DESCENDING consecutive-starvation
    streak makes the wait strictly monotone: a source's rank rises every run
    it is skipped, so it reaches the head within ``ceil(n_sources /
    max_files)`` runs. Ties break by name, so the result is deterministic.

    *history* is the ledger's records oldest-first
    (:func:`read_run_summary_ledger`). Returns ``[]`` when the most recent
    entity run starved nobody, or when there is no entity run to read --
    both mean "no rotation needed", i.e. plain discovery-order turns.
    """
    index = None
    for i in range(len(history) - 1, -1, -1):
        if starved_sources_in_record(history[i]) is not None:
            index = i
            break
    if index is None:
        return []
    previous = starved_sources_in_record(history[index]) or set()
    if not previous:
        return []
    # Streaks as of THAT run, so `history[:index]` — the run itself supplies
    # the +1 `starvation_streaks` always adds for "this run".
    streaks = starvation_streaks(previous, history[:index])
    return sorted(previous, key=lambda source: (-streaks[source], source))


def previous_starved_sources(history: "list[dict[str, Any]]") -> list[str]:
    """The most recent entity run's zero-slot sources, sorted by name.

    Records with no entity phase (merge-only / cluster-only runs, early
    deadline trips) are skipped rather than read as "nobody was starved" --
    they scheduled nothing, so they are no evidence either way, the same
    distinction :func:`starved_sources_in_record` draws for the streak
    counter. :func:`starvation_priority` is the scheduling-order form of
    this; use that one to drive the scheduler.
    """
    for record in reversed(history):
        starved = starved_sources_in_record(record)
        if starved is not None:
            return sorted(starved)
    return []


def read_starvation_priority(
    *,
    cache_dir: Path | None = None,
    ledger_path: Path | None = None,
) -> list[str]:
    """:func:`starvation_priority` against the durable ledger. Best-effort.

    Reuses the athenaeum#1102 run-summary ledger the entity phase already
    writes, so the fair-scheduling rotation introduces no second piece of
    persisted state. Fail-open like every other read in this module: a
    missing or corrupt ledger reads as "no history" (``[]``, i.e. plain
    discovery-order turns), and nothing here may raise into the run it is
    only observing.
    """
    try:
        target = (
            ledger_path
            if ledger_path is not None
            else default_run_summary_ledger_path(cache_dir)
        )
        return starvation_priority(read_run_summary_ledger(target))
    except Exception as exc:  # noqa: BLE001 — observability must never raise
        log.debug("run-summary: starvation priority read skipped: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Zero-progress-refusal streak (issue athenaeum#1283)
# ---------------------------------------------------------------------------
#
# athenaeum#1135 already detects, at run time, a run that stopped early for a
# resource reason (budget/deadline/spend-ceiling — see
# ``librarian._LIBRARIAN_EARLY_STOP_REASONS``) and committed nothing, and
# logs it loudly (the ``librarian-run-degraded`` marker line, plus a non-zero
# ``EXIT_LIBRARIAN_REFUSAL`` exit code). But that verdict died with the
# process: nothing BETWEEN runs could see it, so ``athenaeum status`` read
# healthy straight through the exact incident that motivated athenaeum#1135 in the
# first place — the athenaeum#899 zero-yield counter it relies on excludes this
# case by design (it requires ``api_calls > 0``, and a budget-exhausted run
# makes zero calls). This section closes that gap the same way athenaeum#1291's
# source-starvation streak (above) closed an analogous one: by computing the
# missing "how many runs in a row" figure from the SAME athenaeum#1102 ledger,
# rather than adding a second piece of persisted state.
# ``RunContext.emit_run_summary`` (``librarian.py``) already writes the
# athenaeum#1135 verdict into every record's optional ``refusal`` field (see
# :func:`build_run_summary_ledger_record`'s *refusal* parameter), so the
# streak is just "how many consecutive trailing records also carry it".

#: The ledger record key carrying this run's athenaeum#1135 refusal verdict —
#: see :func:`build_run_summary_ledger_record`'s *refusal* parameter.
REFUSAL_FIELD = "refusal"

#: Stable, greppable line prefix for the ``status.py`` render (issue
#: athenaeum#1283) — mirrors :data:`STARVATION_ALERT_PREFIX` /
#: :data:`REGRESSION_ALERT_PREFIX` above, so an operator's existing
#: log/status sweep catches this without a new channel to watch. Unlike
#: those two, this fires at streak 1 — see ``status.format_status``: a
#: single refusal is already "status must not read healthy", not a
#: threshold alarm.
REFUSAL_ALERT_PREFIX = "librarian-run-refusal"


def refusal_in_record(record: dict[str, Any]) -> bool | None:
    """This record's athenaeum#1135 refusal verdict — THREE outcomes, not two.

    This is the load-bearing distinction the whole streak counter below
    depends on. The ``refusal`` field, when present, is itself a small dict
    keyed on ``tripped`` (see :func:`build_run_summary_ledger_record`'s
    *refusal* parameter) — this function is the one place that dict gets
    unpacked into the bool a reader actually wants:

    * ``True`` — this run's ``refusal`` field is present and its
      ``tripped`` sub-field is truthy: the verdict WAS evaluated and it WAS
      a refusal.
    * ``False`` — this run's ``refusal`` field is present and its
      ``tripped`` sub-field is falsy: the verdict WAS evaluated (by a
      version of the code new enough to record it) and it was NOT a
      refusal.
    * ``None`` — the verdict CANNOT be read from this record, for either of
      two distinct reasons, both collapsed to the same honest answer:

      1. This record's ``v`` predates 3 (or ``v`` is missing/unparseable,
         e.g. a hand-edited or torn line that still parsed as JSON) — a
         record written before athenaeum#1283 landed cannot speak to whether
         that run was a refusal at all: the athenaeum#1135 verdict existed at
         run time (as a log line only), but nothing wrote it into the
         ledger.
      2. This record's ``v`` is ``>= 3`` but its ``refusal`` field is
         MISSING (or not a dict) — the schema supports the field, but
         ``RunContext.librarian_refusal`` was still ``None`` (never
         evaluated) for THIS particular run when
         :meth:`~athenaeum.librarian.RunContext.emit_run_summary` wrote it.
         Not hypothetical: :meth:`~athenaeum.librarian.RunContext.
         stop_on_deadline` (a wall-clock deadline trip in a pre-entity
         phase) calls ``emit_run_summary`` and returns straight to
         ``run()``'s caller BEFORE ``_run_finalize_phase`` — where the
         verdict is computed — ever runs; the ``cluster_only``/
         ``merge_only`` early-exit paths share the same property.

      Never collapse either case into ``False`` — doing so would silently
      treat a pre-athenaeum#1283 record, OR a run whose verdict was simply
      never reached, as "confirmed not a refusal": exactly the
      false-negative this whole issue is about, just moved one layer down
      into the reader (or the writer) instead of being visible at the
      source.
    """
    raw_version = record.get("v")
    try:
        version = int(raw_version)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if version < 3:
        return None
    detail = record.get(REFUSAL_FIELD)
    if not isinstance(detail, dict):
        # Schema supports the field (v >= 3), but this run's verdict was
        # never evaluated -- e.g. the stop_on_deadline path named above.
        # "Cannot speak", the same honest answer as the pre-v3 case, just a
        # different reason.
        return None
    return bool(detail.get("tripped"))


def refusal_streak(history: "list[dict[str, Any]]") -> int:
    """Consecutive TRAILING refusal runs in *history* (oldest-first, exactly
    what :func:`read_run_summary_ledger` returns).

    Walks *history* from the newest record backwards, counting while
    :func:`refusal_in_record` reads ``True``, and STOPS — rather than
    skipping past — the moment it reads anything other than ``True`` (a
    genuine ``False`` clean run, OR a ``None`` that cannot speak to whether
    it was a refusal: the streak MIGHT continue further back, but this
    instrument has no way to know, so it reports only the confirmed
    trailing run count rather than guessing through the gap). No logic
    change was needed here to fix athenaeum#1283's writer-side bug (the
    record-shape fix that made an unevaluated run distinguishable from an
    evaluated-clean one lives in :func:`build_run_summary_ledger_record` /
    :func:`refusal_in_record`); this function already stopped on anything
    that wasn't ``True``, so it was already correct once its input became
    honest.

    ``None`` now has TWO distinct sources, not one, and this function
    treats both identically (stop, don't bridge past):

    1. A ``v < 3`` record — predates the ``refusal`` field's existence.
    2. A ``v >= 3`` record whose verdict was simply never evaluated for that
       run (e.g. a wall-clock deadline trip via ``RunContext.
       stop_on_deadline``, which emits a summary before
       ``_run_finalize_phase`` -- where the verdict is computed -- ever
       runs) — see :func:`refusal_in_record`'s docstring for the concrete
       code path.

    This is where :func:`refusal_in_record`'s three-state design pays for
    itself regardless of which ``None`` source is in play: a naive
    two-state reader would either (a) treat an ambiguous record as "not a
    refusal" and silently truncate a real streak the moment it hits one, or
    (b) treat it as "was a refusal" and fabricate one that never happened.
    Both are wrong; stopping at the ambiguous record is the only honest
    answer — it under-reports a streak that truly extends past the
    ambiguous point, never over-reports one.
    """
    streak = 0
    for record in reversed(history):
        if refusal_in_record(record) is not True:
            break
        streak += 1
    return streak


def read_refusal_streak(
    *,
    cache_dir: Path | None = None,
    ledger_path: Path | None = None,
) -> "tuple[int, dict[str, Any] | None]":
    """:func:`refusal_streak` against the durable ledger, best-effort, paired
    with the most recent run's ``refusal`` detail dict.

    Mirrors every other convenience reader in this module's fail-open
    contract: a missing or corrupt ledger reads as "no history"
    (``(0, None)``), and nothing here may ever raise into a caller —
    ``status.py`` above all, which is documented read-only/side-effect-free
    (see its module docstring's factoring rule).

    Returns ``(streak, most_recent_refusal_detail)``: *streak* is
    :func:`refusal_streak`'s count; *most_recent_refusal_detail* is the
    newest record's ``refusal`` dict (``{"reason": ..., "files": 0}``) when
    ``streak > 0``, else ``None`` — so a single call gives a caller (e.g.
    ``status.format_status``) both "N consecutive refusals" and the reason
    to name, without a second ledger read.
    """
    try:
        target = (
            ledger_path
            if ledger_path is not None
            else default_run_summary_ledger_path(cache_dir)
        )
        history = read_run_summary_ledger(target)
    except Exception as exc:  # noqa: BLE001 — observability must never raise
        log.debug("run-summary: refusal streak read skipped: %s", exc)
        return (0, None)
    streak = refusal_streak(history)
    if streak <= 0 or not history:
        return (0, None)
    detail = history[-1].get(REFUSAL_FIELD)
    return (streak, detail if isinstance(detail, dict) else None)
