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

    return RunSummaryRecord(total_secs=total_secs, head_fields=head_fields, phases=phases)


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
RUN_SUMMARY_LEDGER_VERSION = 1

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
    """
    stamp = (ts if ts is not None else datetime.now(tz=timezone.utc)).astimezone(
        timezone.utc
    )
    total_secs = sum(secs for _phase, secs, _fields in profile)
    phases: dict[str, dict[str, Any]] = {}
    for phase, secs, fields in profile:
        phases[phase] = {"secs": round(secs, 3), **fields}
    return {
        "v": RUN_SUMMARY_LEDGER_VERSION,
        "ts": stamp.isoformat().replace("+00:00", "Z"),
        "total_secs": round(total_secs, 3),
        "phases": phases,
    }


def write_run_summary_record(
    profile: "list[tuple[str, float, dict]]",
    *,
    cache_dir: Path | None = None,
    ledger_path: Path | None = None,
    ts: datetime | None = None,
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
    """
    if not profile:
        return False
    try:
        record = build_run_summary_ledger_record(profile, ts=ts)
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
        log.debug(
            "run-summary ledger write skipped (%s): %s", type(exc).__name__, exc
        )
        return False


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
