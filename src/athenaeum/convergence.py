# SPDX-License-Identifier: Apache-2.0
"""Quarterly convergence report for the self-tuning loop (issue athenaeum#2020,
athenaeum#719 Plan steps 7-8).

A supply-only metric (dimension-proposal ratification rate) cannot tell real
convergence apart from quiet abandonment: ratifications falling while the
missing-dimension signal keeps rising looks identical to success on a
supply-only read (issue athenaeum#2020's own Motivation). This module
computes BOTH quarterly series and labels the pair with one of three
deterministic readings, plus a fourth for too little history:

- **supply** — count of ``approve`` resolutions on ``dimension-proposal``
  ledger items (:mod:`athenaeum.dimension_proposals`), bucketed by the
  quarter of each record's ``answered_at``. A ``rename`` verdict
  (:func:`athenaeum.decision_answers._apply_dimension_proposal_answer`)
  writes the SAME ``kind: "approve"`` ledger record a plain ``approve``
  does — this counts both; it is counting "the proposal was ratified", not
  "ratified under its drafted name".
- **demand** — count of unresolved ``underdetermined`` verdicts naming at
  least one dimension absent from the CURRENT
  :class:`~athenaeum.dimensions.DimensionRegistry`, bucketed by the quarter
  of each verdict entry's ``at``. "Unresolved" means: take the latest
  (highest ``at``) live entry per pair (mirroring
  :func:`athenaeum.verdicts.lookup_pair`'s own dedup rule, since
  :func:`athenaeum.verdicts.iter_live_entries` can hold more than one entry
  per pair before the next :func:`athenaeum.verdicts.compact` run), then
  keep only the pairs whose latest entry is still ``underdetermined`` — a
  pair re-verdicted to something else since is no longer in this
  population. Deliberately NOT :mod:`athenaeum.signal_mining`'s
  shape-clustering machinery: this report counts VERDICTS, not recurring
  typed SHAPES, and
  :func:`~athenaeum.signal_mining.mine_underdetermined_shapes`'s own
  ``window_days`` filter (default 30) would zero out every quarter but the
  most recent one — exactly wrong for a report whose whole point is
  multi-quarter history. This module reads the SAME ledger
  (:func:`athenaeum.verdicts.iter_live_entries`) directly, unwindowed.

**The in-progress quarter is excluded from both series.** A partial
current-quarter bucket always looks like it is "falling" relative to a full
prior quarter, which would bias every live read toward convergence. Both
series are built only from quarters strictly before the quarter containing
*now*.

**Deterministic three-way classifier, pinned precedence.** Given the two
series' trends (each "falling" / "rising" / "flat", by comparing the last
completed quarter to the first observed quarter):

1. fewer than 2 completed quarters of history (in either series' observed
   range) -> ``insufficient_data``
2. both falling -> ``convergence``
3. supply falling, demand rising -> ``abandonment``
4. anything else (the catch-all: supply not falling, i.e. flat or rising,
   regardless of demand; or supply falling with demand flat) ->
   ``cyc_failure_mode`` — the registry keeps growing (or demand never
   actually recedes) with no sign of the loop closing itself out, the
   failure mode named by issue athenaeum#719's own Motivation (the
   `Cyc <https://en.wikipedia.org/wiki/Cyc>`_ precedent: an ever-expanding,
   hand-curated ontology that never converges).

Checked in this exact order — ``insufficient_data`` always wins when it
applies, then ``convergence``, then ``abandonment``, then the catch-all —
so every input maps to exactly one of the four labels.

Pure and side-effect-free: reads the dimension-proposals ledger and the
verdict ledger, writes nothing.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.dimension_proposals`
(L4 peer, ledger reader only), :mod:`athenaeum.verdicts` (L2),
:mod:`athenaeum.dimensions` (L1/L2, via :func:`athenaeum.config.
resolve_dimensions`), and :mod:`athenaeum.config` (L1) — all function-local
except the lightweight ``dataclasses``/``datetime`` stdlib imports, mirroring
:mod:`athenaeum.signal_mining`'s own lazy-import discipline for its
heavier peer (:mod:`athenaeum.resolution_claims`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: The four possible readings, in their pinned precedence order (see module
#: docstring). Order here is documentation only — the classifier itself
#: hardcodes the precedence; this tuple is what a caller/test iterates to
#: assert exhaustiveness.
READINGS: tuple[str, ...] = (
    "insufficient_data",
    "convergence",
    "abandonment",
    "cyc_failure_mode",
)

_EXPLANATIONS: dict[str, str] = {
    "insufficient_data": (
        "Fewer than two completed quarters of history exist yet; no reading can be drawn."
    ),
    "convergence": (
        "Both ratifications (supply) and the missing-dimension signal "
        "(demand) are falling: the registry is settling."
    ),
    "abandonment": (
        "Ratifications (supply) are falling while the missing-dimension "
        "signal (demand) is rising: this looks like quiet abandonment, not "
        "convergence."
    ),
    "cyc_failure_mode": (
        "Neither convergence nor abandonment: the registry keeps growing "
        "(or demand is not falling) with no sign of the loop closing "
        "itself out — the open-ended-ontology failure mode."
    ),
}


def _quarter_key(dt: datetime) -> str:
    """``"YYYY-Qn"`` for *dt* (UTC-naive-safe: callers pass tz-aware or
    naive consistently; this function only reads ``.year``/``.month``)."""
    q = (dt.month - 1) // 3 + 1
    return f"{dt.year}-Q{q}"


def _quarter_tuple(key: str) -> tuple[int, int]:
    year_str, _, q_str = key.partition("-Q")
    return int(year_str), int(q_str)


def _next_quarter(key: str) -> str:
    year, q = _quarter_tuple(key)
    if q >= 4:
        return f"{year + 1}-Q1"
    return f"{year}-Q{q + 1}"


def _dense_quarter_range(keys: list[str]) -> list[str]:
    """Every quarter from the earliest to the latest observed *keys*,
    inclusive, with no gaps — a quarter nothing happened in is a real ``0``,
    never a skipped bucket."""
    if not keys:
        return []
    ordered = sorted(set(keys), key=_quarter_tuple)
    start, end = ordered[0], ordered[-1]
    out = [start]
    while out[-1] != end:
        out.append(_next_quarter(out[-1]))
    return out


def _parse_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _trend(series: dict[str, int], quarters: list[str]) -> str:
    """``"falling"`` / ``"rising"`` / ``"flat"`` comparing the first to the
    last of *quarters* (both already restricted to completed quarters)."""
    if len(quarters) < 2:
        return "flat"
    first = series.get(quarters[0], 0)
    last = series.get(quarters[-1], 0)
    if last < first:
        return "falling"
    if last > first:
        return "rising"
    return "flat"


def classify(supply_trend: str, demand_trend: str, *, enough_history: bool) -> str:
    """The deterministic four-way classifier (module docstring's pinned
    precedence). Exposed standalone so a test can enumerate the full
    trend x trend table without recomputing series."""
    if not enough_history:
        return "insufficient_data"
    if supply_trend == "falling" and demand_trend == "falling":
        return "convergence"
    if supply_trend == "falling" and demand_trend == "rising":
        return "abandonment"
    return "cyc_failure_mode"


@dataclass
class ConvergenceReport:
    """One computed convergence report (issue athenaeum#2020 AC1/AC2)."""

    quarters: list[str] = field(default_factory=list)
    supply: dict[str, int] = field(default_factory=dict)
    demand: dict[str, int] = field(default_factory=dict)
    supply_trend: str = "flat"
    demand_trend: str = "flat"
    reading: str = "insufficient_data"
    explanation: str = _EXPLANATIONS["insufficient_data"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "quarters": list(self.quarters),
            "supply": dict(self.supply),
            "demand": dict(self.demand),
            "supply_trend": self.supply_trend,
            "demand_trend": self.demand_trend,
            "reading": self.reading,
            "explanation": self.explanation,
        }


def _supply_series(wiki_root: Path, *, current_quarter: str) -> dict[str, int]:
    from athenaeum.dimension_proposals import APPROVE_KIND, read_dimension_proposals_ledger

    counts: dict[str, int] = {}
    for record in read_dimension_proposals_ledger(wiki_root):
        if record.get("kind") != APPROVE_KIND:
            continue
        at = _parse_at(record.get("answered_at") or record.get("created_at"))
        if at is None:
            continue
        key = _quarter_key(at)
        if key >= current_quarter:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def _demand_series(wiki_root: Path, *, registry: Any, current_quarter: str) -> dict[str, int]:
    """Count unresolved ``underdetermined`` verdicts naming at least one
    unregistered dimension, by quarter.

    Deliberately NOT :func:`athenaeum.verdicts.list_by_verdict` directly:
    that helper reads every entry in every live monthly partition
    unfiltered-per-pair, which can hold MORE THAN ONE entry for the same
    pair before :func:`athenaeum.verdicts.compact` next runs (compaction is
    a separate, periodic phase — see that function's own docstring). Using
    it as-is would double-count a pair that was underdetermined and has
    SINCE been re-verdicted but not yet compacted. This mirrors
    :func:`athenaeum.verdicts.lookup_pair`'s own "most recently decided
    (``at``) wins" dedup rule instead, applied across every pair at once.
    """
    from athenaeum.verdicts import iter_live_entries

    latest_by_pair: dict[str, Any] = {}
    for _, entry in iter_live_entries(wiki_root):
        current = latest_by_pair.get(entry.pair)
        if current is None or entry.at > current.at:
            latest_by_pair[entry.pair] = entry

    counts: dict[str, int] = {}
    for entry in latest_by_pair.values():
        if entry.verdict != "underdetermined":
            continue
        at = _parse_at(entry.at)
        if at is None:
            continue
        key = _quarter_key(at)
        if key >= current_quarter:
            continue
        unregistered = [m for m in entry.missing if registry.get(str(m)) is None]
        if not unregistered:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def _effective_registry(config: dict[str, Any] | None) -> Any:
    from athenaeum.config import resolve_dimensions
    from athenaeum.dimensions import DEFAULT_REGISTRY, DimensionRegistryError

    try:
        return resolve_dimensions(config)
    except DimensionRegistryError:
        # A malformed operator `dimensions:` block is a real config error
        # elsewhere (resolve_dimensions raises loud there); this report is
        # read-only advisory output, so it fails soft to the kernel-only
        # registry rather than ever raising out of a status/CLI read.
        return DEFAULT_REGISTRY


def compute_convergence_report(
    wiki_root: Path,
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ConvergenceReport:
    """Compute the quarterly convergence report (issue athenaeum#2020 AC1/AC2).

    Pure: reads ``<wiki_root>/_dimension_proposals.jsonl`` and the verdict
    ledger under *wiki_root*; writes nothing. *config* resolves the
    effective dimension registry (:func:`athenaeum.config.resolve_dimensions`)
    against which a verdict's ``missing`` names are checked.
    """
    wiki_root = Path(wiki_root)
    now = now or datetime.now(timezone.utc)
    current_quarter = _quarter_key(now)
    registry = _effective_registry(config)

    supply = _supply_series(wiki_root, current_quarter=current_quarter)
    demand = _demand_series(wiki_root, registry=registry, current_quarter=current_quarter)

    quarters = _dense_quarter_range(list(supply) + list(demand))
    supply_dense = {q: supply.get(q, 0) for q in quarters}
    demand_dense = {q: demand.get(q, 0) for q in quarters}

    enough_history = len(quarters) >= 2
    supply_trend = _trend(supply_dense, quarters) if enough_history else "flat"
    demand_trend = _trend(demand_dense, quarters) if enough_history else "flat"
    reading = classify(supply_trend, demand_trend, enough_history=enough_history)

    return ConvergenceReport(
        quarters=quarters,
        supply=supply_dense,
        demand=demand_dense,
        supply_trend=supply_trend,
        demand_trend=demand_trend,
        reading=reading,
        explanation=_EXPLANATIONS[reading],
    )


__all__ = [
    "READINGS",
    "ConvergenceReport",
    "classify",
    "compute_convergence_report",
]
