# SPDX-License-Identifier: Apache-2.0
"""Shared numeric helpers with no other home (issue athenaeum#1990).

:func:`percentile` was private to :mod:`athenaeum.spend` (``_percentile``).
The decision-budget instrumentation (:mod:`athenaeum.decision_budget`) needs
the exact same nearest-rank percentile on the exact same rounding — the
issue is explicit that a second percentile implementation with different
rounding must not be forked. Promoting it here (an L3 service module with no
dependents of its own) lets both :mod:`athenaeum.spend` and
:mod:`athenaeum.decision_budget` import one implementation without creating
an edge between those two same-layer modules.

Layering: L3 service module. Stdlib-only; imports nothing from this
package.
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of *values* (0 <= pct <= 100). Assumes non-empty."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[rank]


def parse_utc_timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp, tolerating a bare ``YYYY-MM-DD`` date.

    Issue athenaeum#1990: shared by :func:`athenaeum.decisions.
    decision_time_minutes` and :mod:`athenaeum.decision_budget` so neither
    forks a second date-parsing convention. A full
    ``YYYY-MM-DDThh:mm:ssZ`` timestamp parses to that instant. A bare date
    (e.g. the question queue's header-only ``created_at``, which carries no
    time-of-day) parses to midnight UTC on that date. Returns ``None`` when
    neither shape parses.
    """
    text = (value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            day = date.fromisoformat(text[:10])
        except ValueError:
            return None
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def minutes_between(start: str, end: str) -> int | None:
    """Whole minutes between two ISO-8601 UTC timestamps (issue athenaeum#1990).

    Returns ``None`` — never a fabricated duration — when either
    timestamp is missing/unparseable (:func:`parse_utc_timestamp`), or when
    the pair resolves to a negative duration (clock skew or a malformed
    record, not a real decision time).
    """
    started = parse_utc_timestamp(start)
    ended = parse_utc_timestamp(end)
    if started is None or ended is None:
        return None
    minutes = int((ended - started).total_seconds() // 60)
    return minutes if minutes >= 0 else None


def days_since(value: str, *, today: date | None = None) -> int | None:
    """Whole days between ``value`` (an ISO date/datetime) and ``today``.

    Issue athenaeum#1990: shared by :func:`athenaeum.decisions.age_days` and
    :mod:`athenaeum.decision_budget`. Returns ``None`` when ``value`` can't
    be parsed. Only the date portion is used, so a full
    ``YYYY-MM-DDThh:mm:ssZ`` timestamp works too.
    """
    if not value:
        return None
    day_part = value.strip()[:10]
    try:
        day = date.fromisoformat(day_part)
    except ValueError:
        return None
    ref = today or date.today()
    return (ref - day).days


__all__ = ["percentile", "parse_utc_timestamp", "minutes_between", "days_since"]
