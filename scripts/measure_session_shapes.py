#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Measure merged-ledger session shapes: both / hook-only / pull-only
(issue athenaeum#1593 AC4).

**HOST-STATE.** This script reads the OPERATOR's real push-records
ledgers -- paths a build lane cannot mount or read (no ``~/.cache/athenaeum/``,
no ``~/knowledge/wiki/`` inside this repo's CI or dev containers) -- so it
ships here unexercised against live data. Run it on the machine that holds
the ledgers and drop the printed report into ``docs/measurements/`` under a
dated filename, e.g.::

    python scripts/measure_session_shapes.py \\
        --ledger ~/.cache/athenaeum/_push_records.jsonl \\
        --ledger ~/knowledge/wiki/_push_records.jsonl \\
        --split-date 2026-09-14 \\
        > docs/measurements/session-shapes-$(date +%F).md

Classifies each PUSH-RECORD row (never a reference-determination row --
those live in a separate ``_reference_records.jsonl`` and are out of scope
here) by the same reader rule :mod:`athenaeum.push_metrics`/
:mod:`athenaeum._cmd_viewer` already document (``docs/reference/configuration.md``):

- ``source`` in ``{"hook", "sidecar"}``           -> a PUSH (the passive/unbidden path)
- ``source`` absent, ``ts`` >= the cutover
  (``SOURCE_FIELD_FIRST_SEEN``, 2026-09-09T03:48:00Z) -> a PULL (an explicit MCP ``recall``)
- anything else (absent key + unusable/pre-cutover ``ts``) -> UNKNOWN
  provenance, excluded from the both/hook-only/pull-only table the same way
  the viewer excludes it from ``pushed_ids``/``pulled_ids`` (issue athenaeum#1542)

A session is:

- ``both``      -- at least one PUSH row and at least one PULL row
- ``hook-only`` -- at least one PUSH row, zero PULL rows
- ``pull-only`` -- zero PUSH rows, at least one PULL row
- ``unknown-only`` -- every row for that session is unknown-provenance;
  reported separately so it is never silently folded into a 0% figure for
  either real bucket.

The report is split into two windows by ``--split-date`` (default
2026-09-14, the day this issue's Motivation cites as the observed
staleness-recurrence date), bucketing each SESSION by its LATEST row's
``ts`` -- a session whose activity straddles the split date lands in the
window its most recent activity falls in.

The constants below are reproduced from :mod:`athenaeum.push_metrics`/
:mod:`athenaeum._cmd_viewer` rather than imported, so this script keeps
working if it is ever copied out and run standalone against an exported
ledger, away from an athenaeum checkout.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_UNBIDDEN_SOURCES = ("hook", "sidecar")
_SOURCE_FIELD_FIRST_SEEN = datetime(2026, 9, 9, 3, 48, 0, tzinfo=timezone.utc)

_SHAPES = ("both", "hook-only", "pull-only", "unknown-only")
_WINDOWS = ("before", "after", "unknown-window")


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_ledger(path: Path) -> list[dict[str, Any]]:
    """Tolerant JSONL reader -- a torn trailing line (a crash mid-append,
    the same failure mode ``push_metrics._read_jsonl`` already tolerates) is
    skipped, never a hard failure that would abort a report over one bad
    ledger out of several. A missing file reads as zero rows, reported in
    the ledgers-read table rather than raising -- a merged report over
    ledgers that legitimately differ in which machine wrote them should not
    require every one of them to exist."""
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
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


def classify_row(row: dict[str, Any]) -> str:
    """One of ``"push"``, ``"pull"``, ``"unknown"`` -- see module docstring."""
    if row.get("source") in _UNBIDDEN_SOURCES:
        return "push"
    ts = _parse_ts(row.get("ts"))
    if ts is not None and ts >= _SOURCE_FIELD_FIRST_SEEN:
        return "pull"
    return "unknown"


def measure(ledger_paths: list[Path], *, split_date: datetime) -> dict[str, Any]:
    """Merge *ledger_paths*, classify every row, bucket sessions into
    both/hook-only/pull-only/unknown-only, split by *split_date* on each
    session's LATEST row timestamp."""
    per_session: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"push": False, "pull": False, "latest_ts": None}
    )
    total_rows = 0
    rows_read_per_ledger: dict[str, int] = {}
    for path in ledger_paths:
        rows = read_ledger(path)
        rows_read_per_ledger[str(path)] = len(rows)
        for row in rows:
            total_rows += 1
            session_id = row.get("session_id")
            if not session_id:
                continue
            entry = per_session[session_id]
            kind = classify_row(row)
            if kind == "push":
                entry["push"] = True
            elif kind == "pull":
                entry["pull"] = True
            ts = _parse_ts(row.get("ts"))
            if ts is not None:
                latest = entry["latest_ts"]
                if latest is None or ts > latest:
                    entry["latest_ts"] = ts

    def _window(entry: dict[str, Any]) -> str:
        ts = entry["latest_ts"]
        if ts is None:
            return "unknown-window"
        return "before" if ts < split_date else "after"

    def _shape(entry: dict[str, Any]) -> str:
        if entry["push"] and entry["pull"]:
            return "both"
        if entry["push"]:
            return "hook-only"
        if entry["pull"]:
            return "pull-only"
        return "unknown-only"

    buckets: dict[str, dict[str, int]] = {
        window: dict.fromkeys(_SHAPES, 0) for window in _WINDOWS
    }
    for entry in per_session.values():
        buckets[_window(entry)][_shape(entry)] += 1

    return {
        "ledgers_read": rows_read_per_ledger,
        "total_rows": total_rows,
        "total_sessions": len(per_session),
        "windows": buckets,
    }


def render_markdown(result: dict[str, Any], *, split_date: datetime, generated_at: datetime) -> str:
    lines = [
        f"# Session shapes — merged ledger — generated {generated_at.date().isoformat()}",
        "",
        "Ledger files read:",
        "",
    ]
    for path, count in result["ledgers_read"].items():
        lines.append(f"- `{path}` — {count} row(s)")
    lines += [
        "",
        f"Total rows: {result['total_rows']}. Total sessions: {result['total_sessions']}.",
        f"Split date: {split_date.date().isoformat()} "
        "(session bucketed by its LATEST row's ts).",
        "",
        "| window | both | hook-only | pull-only | unknown-only |",
        "|---|---|---|---|---|",
    ]
    for window in _WINDOWS:
        b = result["windows"][window]
        lines.append(
            f"| {window} | {b['both']} | {b['hook-only']} | {b['pull-only']} | "
            f"{b['unknown-only']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure merged-ledger session shapes (athenaeum#1593 AC4)."
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        action="append",
        required=True,
        help="Path to a `_push_records.jsonl` ledger. Repeatable -- pass "
        "both the live cache-dir ledger and the withdrawn wiki-root one "
        "to reproduce the issue's 'merged' figure.",
    )
    parser.add_argument(
        "--split-date",
        type=str,
        default="2026-09-14",
        help="ISO date (YYYY-MM-DD) splitting the before/after windows "
        "(default: 2026-09-14).",
    )
    args = parser.parse_args(argv)
    split_date = datetime.fromisoformat(args.split_date).replace(tzinfo=timezone.utc)
    result = measure(args.ledger, split_date=split_date)
    report = render_markdown(
        result, split_date=split_date, generated_at=datetime.now(timezone.utc)
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
