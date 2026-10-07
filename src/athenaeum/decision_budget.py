# SPDX-License-Identifier: Apache-2.0
"""Decision-queue budget instrumentation (issue athenaeum#1990, #717 AC group 6).

:mod:`athenaeum.decisions` unifies every pending human decision into one
queue; :mod:`athenaeum.decision_framing` caps each item's context and
publishes its token count. Neither reports whether the queue, as a whole,
is within the three effort bounds #717 sets:

- items/day: rolling 30-day mean <= ``librarian.decisions_budget_items_per_day_max``
- per-item context: <= ``librarian.decisions_max_item_context_tokens`` (already
  enforced by :func:`athenaeum.decision_framing.frame_decision`)
- decision time: p50 <= ``librarian.decisions_budget_decision_minutes_p50_max``

This module adds exactly that: a durable **answer-event ledger** —
``wiki/_decision_budget_events.jsonl`` — recording ``{decision_id,
decision_type, raised_at, answered_at}`` once per resolved decision, plus
the five #717 figures computed from it and from the live pending-decision
list:

- **items/day** — rolling-window mean over :data:`answered_at` timestamps
  in the ledger (:func:`items_per_day`).
- **per-item context size distribution** — over the LIVE queue's
  ``context_tokens`` (already measured by :func:`athenaeum.decision_framing.
  frame_decision`); this module does not re-measure it
  (:func:`context_size_distribution`).
- **decision time p50/p95** — over the ledger's ``(raised_at, answered_at)``
  pairs, via :func:`athenaeum.metrics.minutes_between` (the same shared
  implementation :func:`athenaeum.decisions.decision_time_minutes`
  delegates to — see the layering note below) (:func:`decision_time_percentiles`).
- **p95 item age** — over the LIVE queue's age, via
  :func:`athenaeum.metrics.days_since` (ditto, :func:`athenaeum.decisions.age_days`'s
  shared implementation) (:func:`item_age_p95_days`).
- **queue depth trend** — reconstructed day-by-day from the live queue's
  ``created_at`` plus the ledger's ``answered_at`` (:func:`queue_depth_trend`).
  This UNDERCOUNTS any day before this module started recording answer
  events — there is no retroactive history — which is a known, stated
  limitation, not a silent one.

The single writer is :func:`record_decision_answered`. It is called from
exactly two places: :mod:`athenaeum.decision_answers`'s
``apply_decision_answers`` (the unified answer-applier, covering
``question``/``confirmation``, ``merge``, ``audit``, ``proposed-rule``) and
:func:`athenaeum.quarantine.release_quarantine` (quarantine resolves through
its own dedicated path, not the unified applier). ``retraction`` has NO
resolution path anywhere in this codebase today — nothing ever marks a
retraction-review item answered — so no event is ever recorded for it; see
the issue's PR description for why this is reported as an unmet AC rather
than a resolver invented for this change.

Layering: L4 domain/pipeline module, mirroring :mod:`athenaeum.decisions`.
Imports L3 services (:mod:`athenaeum.store`, :mod:`athenaeum.metrics`)
freely, and deliberately does NOT import :mod:`athenaeum.decisions` —
:func:`athenaeum.quarantine.release_quarantine` imports THIS module, and
:mod:`athenaeum.decisions` already imports :mod:`athenaeum.quarantine`
(for ``list_pending_quarantine``), so a ``decision_budget -> decisions``
edge would close a 3-cycle
(``decisions -> quarantine -> decision_budget -> decisions``) that
``tests/test_import_graph_acyclic.py`` forbids. The day-diff/decision-time
math both modules need lives one layer down, in :mod:`athenaeum.metrics`
(L3), which each imports independently — see that module's
:func:`~athenaeum.metrics.days_since` / :func:`~athenaeum.metrics.
minutes_between`.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum.metrics import percentile
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Ledger filename, alongside ``_pending_questions.md`` / ``_pending_merges.md``.
DECISION_BUDGET_EVENTS_FILENAME = "_decision_budget_events.jsonl"

#: Separate shapes ledger (AC: "the recurring decision shapes driving an
#: overflow are recorded ... rather than silently absorbed").
DECISION_BUDGET_SHAPES_FILENAME = "_decision_budget_shapes.jsonl"

DECISION_BUDGET_EVENTS_VERSION = 1
DECISION_BUDGET_SHAPES_VERSION = 1


def default_events_path(wiki_root: Path) -> Path:
    return Path(wiki_root) / DECISION_BUDGET_EVENTS_FILENAME


def default_shapes_path(wiki_root: Path) -> Path:
    return Path(wiki_root) / DECISION_BUDGET_SHAPES_FILENAME


def record_decision_answered(
    wiki_root: Path,
    *,
    decision_id: str,
    decision_type: str,
    raised_at: str,
    answered_at: str | None = None,
    events_path: Path | None = None,
) -> dict[str, Any]:
    """Append one answer event to the budget ledger. Never raises.

    ``raised_at`` is the item's own raise timestamp (the detector/agent's
    ``created_at``/``raised_at``, looked up by the caller BEFORE the
    resolver runs — an answered item no longer appears in the live queue,
    so this is the last point it is cheaply available). ``answered_at``
    defaults to the current instant.

    Durable append via :func:`athenaeum.store.append_line_durable` — the
    single ``O_APPEND`` + ``fsync`` primitive every other ledger in this
    codebase uses (issue athenaeum#980).
    """
    record = {
        "v": DECISION_BUDGET_EVENTS_VERSION,
        "decision_id": decision_id,
        "decision_type": decision_type,
        "raised_at": raised_at or "",
        "answered_at": answered_at or now_iso(),
    }
    target = events_path if events_path is not None else default_events_path(wiki_root)
    try:
        append_line_durable(
            target, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        )
    except OSError as exc:  # pragma: no cover - defensive; must never break a resolve
        log.warning("decision_budget: failed to record answer event: %s", exc)
    return record


def read_decision_budget_events(
    wiki_root: Path, *, events_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed event, tolerating a torn trailing line.

    Mirrors the read pattern every other JSONL ledger in this codebase uses
    (e.g. :func:`athenaeum.retraction_cascade.read_retraction_reviews`): a
    malformed or torn line (an in-flight crash at worst leaves a torn
    TRAILING line) is skipped, never a fatal error for the whole file.
    """
    target = events_path if events_path is not None else default_events_path(wiki_root)
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("decision_id"):
            out.append(rec)
    return out


def _parse_day(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def items_per_day(
    events: list[dict[str, Any]],
    *,
    window_days: int,
    now: datetime | None = None,
) -> float:
    """Rolling ``window_days``-day mean of answered items (#717's items/day figure).

    Counts events whose ``answered_at`` falls within the trailing window
    (inclusive of today), divided by ``window_days``. Events with an
    unparseable ``answered_at`` are excluded, not treated as "today".
    """
    if window_days <= 0:
        return 0.0
    ref = (now or datetime.now(timezone.utc)).date()
    start = ref - timedelta(days=window_days - 1)
    count = 0
    for event in events:
        day = _parse_day(event.get("answered_at", ""))
        if day is not None and start <= day <= ref:
            count += 1
    return count / window_days


def decision_time_percentiles(
    events: list[dict[str, Any]], *, pctiles: tuple[float, ...] = (50.0, 95.0)
) -> dict[str, float | None]:
    """Decision-time percentiles (minutes) over every event's ``(raised_at,
    answered_at)`` pair.

    An event missing either timestamp, or carrying one that fails to parse,
    is EXCLUDED — never treated as a zero-minute decision (the issue's own
    test requirement). Returns ``None`` for every percentile when no event
    yields a usable decision time.
    """
    from athenaeum.metrics import minutes_between

    minutes = [
        m
        for event in events
        if (
            m := minutes_between(
                str(event.get("raised_at") or ""), str(event.get("answered_at") or "")
            )
        )
        is not None
    ]
    if not minutes:
        return {f"p{int(p)}": None for p in pctiles}
    values = [float(m) for m in minutes]
    return {f"p{int(p)}": percentile(values, p) for p in pctiles}


def context_size_distribution(pending_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Per-item context-size distribution over the LIVE queue.

    Reads ``context_tokens`` — already measured by
    :func:`athenaeum.decision_framing.frame_decision`, the queue's single
    admission gate — off each item :func:`athenaeum.decisions.
    list_pending_decisions` returns. This module performs no second token
    measurement.
    """
    values = [
        float(item["context_tokens"])
        for item in pending_items
        if isinstance(item.get("context_tokens"), (int, float))
    ]
    if not values:
        return None
    return {
        "count": len(values),
        "mean": sum(values) / len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values),
    }


def item_age_p95_days(
    pending_items: list[dict[str, Any]], *, today: date | None = None
) -> float | None:
    """p95 item age (days) over the LIVE queue's currently-pending items.

    Uses :func:`athenaeum.decisions.age_days` on each item's ``created_at``.
    An item whose age cannot be determined is excluded.
    """
    from athenaeum.metrics import days_since

    values = [
        float(age)
        for item in pending_items
        if (age := days_since(str(item.get("created_at") or ""), today=today)) is not None
    ]
    if not values:
        return None
    return percentile(values, 95)


def queue_depth_trend(
    pending_items: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    window_days: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Reconstructed day-by-day open-item count over the trailing window.

    For each day ``d`` in the window: ``depth(d)`` is the number of items
    raised on or before ``d`` that were either never answered, or answered
    strictly after ``d``. Built from the LIVE pending items' ``created_at``
    plus the answer-event ledger's ``(raised_at, answered_at)`` pairs.

    Known limitation (stated, not silent): an item answered before this
    ledger existed leaves no event, so depth for any day before this module
    started recording is UNDERCOUNTED — it only ever sees items still open
    today plus items this module itself watched get answered.
    """
    if window_days <= 0:
        return []
    ref = (now or datetime.now(timezone.utc)).date()
    days = [ref - timedelta(days=offset) for offset in range(window_days - 1, -1, -1)]

    # (raised_day, answered_day_or_None) pairs — live-pending items are
    # "answered_day=None" (still open today); ledger events carry both ends.
    pairs: list[tuple[date, date | None]] = []
    for item in pending_items:
        raised_day = _parse_day(str(item.get("created_at") or ""))
        if raised_day is not None:
            pairs.append((raised_day, None))
    for event in events:
        raised_day = _parse_day(str(event.get("raised_at") or ""))
        answered_day = _parse_day(str(event.get("answered_at") or ""))
        if raised_day is not None and answered_day is not None:
            pairs.append((raised_day, answered_day))

    trend: list[dict[str, Any]] = []
    for day in days:
        depth = sum(
            1
            for raised_day, answered_day in pairs
            if raised_day <= day and (answered_day is None or answered_day > day)
        )
        trend.append({"date": day.isoformat(), "depth": depth})
    return trend


def record_overflow_shapes(
    wiki_root: Path,
    *,
    breach_dims: list[str],
    pending_items: list[dict[str, Any]],
    shapes_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Record the recurring decision *shapes* driving a sustained overflow.

    #717's own AC: sustained overflow must not be silently absorbed — the
    shapes driving it are recorded for the self-tuning child (#719) to mine
    later. ``breach_dims`` names which of the three bounds are in breach
    (e.g. ``["items_per_day", "decision_minutes_p50"]``); the shape itself
    is the per-``decision_type`` count across the currently-pending items,
    the cheapest honest signal available without a second history ledger.
    Returns ``None`` (writes nothing) when ``breach_dims`` is empty.
    """
    if not breach_dims:
        return None
    type_counts = Counter(str(item.get("type") or "unknown") for item in pending_items)
    record = {
        "v": DECISION_BUDGET_SHAPES_VERSION,
        "recorded_at": now_iso(now),
        "breach_dims": sorted(breach_dims),
        "type_counts": dict(sorted(type_counts.items())),
    }
    target = shapes_path if shapes_path is not None else default_shapes_path(wiki_root)
    try:
        append_line_durable(
            target, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        )
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("decision_budget: failed to record overflow shape: %s", exc)
        return None
    return record


def budget_report(
    wiki_root: Path,
    pending_items: list[dict[str, Any]],
    *,
    items_per_day_max: int,
    decision_minutes_p50_max: int,
    decision_minutes_p95_max: int,
    item_age_p95_days_max: int,
    window_days: int,
    events_path: Path | None = None,
    now: datetime | None = None,
    record_shapes: bool = True,
) -> dict[str, Any]:
    """The five #717 budget figures, plus a prominent breach report.

    ``pending_items`` is the caller's already-fetched
    :func:`athenaeum.decisions.list_pending_decisions` result (never
    re-fetched here, so this function performs no I/O beyond the answer
    ledger). When ``record_shapes`` is true (the default) and any bound is
    in breach, the recurring decision shapes are recorded via
    :func:`record_overflow_shapes` — the "report it prominently AND record
    the shapes" half of #717's overflow AC; the CLI/status caller owns
    rendering the prominent part.
    """
    events = read_decision_budget_events(wiki_root, events_path=events_path)
    items_day = items_per_day(events, window_days=window_days, now=now)
    decision_time = decision_time_percentiles(events)
    context_dist = context_size_distribution(pending_items)
    age_p95 = item_age_p95_days(pending_items, today=(now or datetime.now(timezone.utc)).date())
    depth_trend = queue_depth_trend(pending_items, events, window_days=window_days, now=now)

    breach_dims: list[str] = []
    if items_day > items_per_day_max:
        breach_dims.append("items_per_day")
    p50 = decision_time.get("p50")
    if p50 is not None and p50 > decision_minutes_p50_max:
        breach_dims.append("decision_minutes_p50")
    p95 = decision_time.get("p95")
    if p95 is not None and p95 > decision_minutes_p95_max:
        breach_dims.append("decision_minutes_p95")
    if age_p95 is not None and age_p95 > item_age_p95_days_max:
        breach_dims.append("item_age_p95_days")

    recorded_shape = None
    if record_shapes and breach_dims:
        recorded_shape = record_overflow_shapes(
            wiki_root, breach_dims=breach_dims, pending_items=pending_items, now=now
        )

    return {
        "items_per_day": items_day,
        "items_per_day_max": items_per_day_max,
        "decision_time_minutes": decision_time,
        "decision_minutes_p50_max": decision_minutes_p50_max,
        "decision_minutes_p95_max": decision_minutes_p95_max,
        "context_size_tokens": context_dist,
        "item_age_p95_days": age_p95,
        "item_age_p95_days_max": item_age_p95_days_max,
        "queue_depth_trend": depth_trend,
        "window_days": window_days,
        "breach": bool(breach_dims),
        "breach_dims": breach_dims,
        "recorded_shape": recorded_shape,
    }


def format_budget_report(report: dict[str, Any]) -> str:
    """Human-readable rendering of :func:`budget_report`.

    Shared between ``athenaeum decisions budget`` (:mod:`athenaeum.
    _cmd_decisions`) and ``athenaeum status`` (:mod:`athenaeum.status`) so
    the two surfaces never drift into two different renderings of one
    report. Lives here (L4) rather than in either presentation module so
    neither has to import the other.
    """
    lines = ["Decision-queue budget", "=" * 40]
    lines.append(
        f"Items/day (rolling {report['window_days']}d mean): "
        f"{report['items_per_day']:.2f} (max {report['items_per_day_max']})"
    )
    dt = report["decision_time_minutes"]
    p50 = dt.get("p50")
    p95 = dt.get("p95")
    p50_str = f"{p50:.0f}min" if p50 is not None else "n/a"
    p95_str = f"{p95:.0f}min" if p95 is not None else "n/a"
    lines.append(
        f"Decision time: p50={p50_str} (max {report['decision_minutes_p50_max']}min), "
        f"p95={p95_str} (max {report['decision_minutes_p95_max']}min)"
    )
    ctx = report.get("context_size_tokens")
    if ctx:
        lines.append(
            f"Per-item context: mean={ctx['mean']:.0f} p50={ctx['p50']:.0f} "
            f"p95={ctx['p95']:.0f} max={ctx['max']:.0f} tokens (n={ctx['count']})"
        )
    else:
        lines.append("Per-item context: n/a (queue empty)")
    age = report.get("item_age_p95_days")
    age_str = f"{age:.1f}d" if age is not None else "n/a"
    lines.append(f"p95 item age: {age_str} (max {report['item_age_p95_days_max']}d)")
    trend = report.get("queue_depth_trend") or []
    if trend:
        lines.append(
            f"Queue depth trend: {trend[0]['depth']} -> {trend[-1]['depth']} " f"over {len(trend)}d"
        )
    if report.get("breach"):
        lines.append(
            "BREACH: effort budget in breach on "
            + ", ".join(report.get("breach_dims", []))
            + " — see docs/design/ for the delegation-ratchet consequence."
        )
    else:
        lines.append("Within budget.")
    return "\n".join(lines)


__all__ = [
    "DECISION_BUDGET_EVENTS_FILENAME",
    "DECISION_BUDGET_SHAPES_FILENAME",
    "default_events_path",
    "default_shapes_path",
    "record_decision_answered",
    "read_decision_budget_events",
    "items_per_day",
    "decision_time_percentiles",
    "context_size_distribution",
    "item_age_p95_days",
    "queue_depth_trend",
    "record_overflow_shapes",
    "budget_report",
    "format_budget_report",
]
