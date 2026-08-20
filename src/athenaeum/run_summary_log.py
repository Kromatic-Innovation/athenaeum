# SPDX-License-Identifier: Apache-2.0
"""Pure parser for ``librarian-run-summary`` log lines (issue athenaeum#713).

:mod:`athenaeum.librarian` already emits one greppable
``librarian-run-summary total_secs=... | entity secs=... calls=... files=...
| ...`` line per run (:func:`athenaeum.librarian._render_run_summary`,
issue athenaeum#464) — but only to whatever log sink the deployment wraps the
run in (e.g. the nightly cron's log file); it is not itself persisted to a
durable, queryable ledger the way the spend ledger is. There is therefore no
in-repo, in-process source for **wall-clock-per-file**, which the athenaeum#713
measurement pack needs for both the backlog price sheet (artifact 2) and the
ordinary-night steady-state table (artifact 3): calls/file and tokens/file
come from the spend ledger (:mod:`athenaeum.spend`,
:func:`athenaeum.drain_advisor.observed_tokens_per_file`), but wall-clock/file
has no ledger field to read (issue athenaeum#713's plan step 2: "measurement
collection for calls/file and wall-clock/file from EXISTING run-summary and
spend data rather than re-deriving it").

This module is that read-only instrument: it parses ``librarian-run-summary``
lines (from a log file the operator points it at — the SAME log the
``reasoning-tier-measurements.md`` precedent grepped by hand, issue athenaeum#784)
into structured records, and derives the entity-phase wall-clock/file ratio
from the ``entity`` phase segment's ``secs=``/``files=`` fields. Pure text
parsing — it never opens the log file itself; callers pass in text (or a
path) they already have. No LLM calls, no store mutation.

Layering: L2 utility. Imports nothing from athenaeum except the stable
``RUN_SUMMARY_PREFIX`` constant (so a prefix rename in ``librarian.py`` is a
one-line fix here too, never a silently-stale string literal).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from athenaeum.librarian import RUN_SUMMARY_PREFIX

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
