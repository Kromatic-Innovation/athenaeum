# SPDX-License-Identifier: Apache-2.0
"""Prior human rulings on merge proposals, for the proposers to consult (issue athenaeum#2077).

A merge proposer that dedupes only against OPEN proposals buys a rejection
exactly one night: the next pass re-proposes the pair and the human is asked
the same question again. This module reads the decisions already on disk --
the resolved blocks of ``_pending_merges.md`` and everything archived in
``_pending_merges_archive.md`` -- and answers one question for a proposer:
"has this source set already been ruled on, and is that ruling still in force?"

There is no second parser. Blocks come from
:func:`athenaeum.pending_merges.parse_pending_merges`, so provenance is read
exactly the way every other consumer reads it:

* a ``reject`` is always human. :func:`athenaeum.pending_merges.resolve_merge`
  ignores ``auto_applied`` on a reject (the T2 auto-finalize path never
  rejects), so ``decision == "reject"`` with ``auto_applied`` false is the
  human-rejection signature. A block retired by the staleness sweep carries
  no decision and is never counted.
* an ``approve`` (human or auto-applied) folded the sources together, so the
  pair is settled: it is never re-proposed, and the cooldown does not apply.

A source set is keyed by the page FILENAMES (``Path.name``): the block header
itself only ever renders basenames, and the wiki is flat, so the key is stable
across a moved checkout where absolute source paths are not.

Only a rejection decays. ``librarian.rejected_merge_cooldown_days`` (default
``0`` = never) is the window after which a rejected set may be proposed again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

ARCHIVE_FILENAME = "_pending_merges_archive.md"
PENDING_FILENAME = "_pending_merges.md"


@dataclass(frozen=True)
class PriorRuling:
    """One resolved merge block, reduced to what a proposer needs."""

    merge_id: str
    decision: str  # "reject" (human) | "approve"
    sources: frozenset[str]  # page filenames
    base_name: str  # display name, else merge target name
    ruled_at: datetime | None  # None when no timestamp could be recovered
    note: str = ""

    def describe(self) -> str:
        """Short phrase for a proposal rationale or log line."""
        when = self.ruled_at.date().isoformat() if self.ruled_at else "an unknown date"
        verb = "rejected" if self.decision == "reject" else "approved"
        tail = f" (note: {self.note})" if self.note else ""
        return f"previously {verb} by a human on {when}{tail}"


def source_key(sources: Iterable[str]) -> frozenset[str]:
    """Order- and location-independent key for a source set."""
    return frozenset(Path(s).name for s in sources)


def _parse_when(answered_at: str, created_at: str) -> datetime | None:
    for raw, fmt in ((answered_at, "%Y-%m-%dT%H:%M:%SZ"), (created_at, "%Y-%m-%d")):
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def load_prior_rulings(wiki_root: Path) -> list[PriorRuling]:
    """Every human rejection and every approval recorded under *wiki_root*."""
    from athenaeum.pending_merges import parse_pending_merges

    rulings: list[PriorRuling] = []
    for filename in (PENDING_FILENAME, ARCHIVE_FILENAME):
        for pm in parse_pending_merges(wiki_root / filename):
            if not pm.resolved or not pm.sources:
                continue
            if pm.decision == "reject":
                decision = "reject"
            elif pm.decision == "approve":
                decision = "approve"
            else:
                continue
            rulings.append(
                PriorRuling(
                    merge_id=pm.id,
                    decision=decision,
                    sources=source_key(pm.sources),
                    base_name=pm.display_name or pm.merge_target_name,
                    ruled_at=_parse_when(pm.answered_at, pm.created_at),
                    note=pm.note,
                )
            )
    return rulings


def ruling_in_force(
    ruling: PriorRuling, *, cooldown_days: int, now: datetime | None = None
) -> bool:
    """Is *ruling* still binding on proposers?

    An approval always is. A rejection is, unless *cooldown_days* is positive
    and the rejection is older than that. A rejection with no recoverable
    timestamp never expires (the conservative direction).
    """
    if ruling.decision != "reject" or cooldown_days <= 0 or ruling.ruled_at is None:
        return True
    current = now or datetime.now(timezone.utc)
    return current - ruling.ruled_at <= timedelta(days=cooldown_days)


@dataclass(frozen=True)
class RulingMatch:
    """What the archive says about one source set.

    ``in_force`` is the ruling a proposer must honour (skip the proposal);
    ``expired`` is the most recent rejection whose cooldown has lapsed (the
    proposal may go ahead, and its rationale should name it).
    """

    in_force: PriorRuling | None = None
    expired: PriorRuling | None = None


def _recency(ruling: PriorRuling) -> datetime:
    return ruling.ruled_at or datetime.min.replace(tzinfo=timezone.utc)


def match_rulings(
    rulings: Iterable[PriorRuling],
    sources: Iterable[str],
    *,
    cooldown_days: int = 0,
    now: datetime | None = None,
) -> RulingMatch:
    """Classify the rulings on exactly this source set."""
    key = source_key(sources)
    live: list[PriorRuling] = []
    lapsed: list[PriorRuling] = []
    for r in rulings:
        if r.sources != key:
            continue
        (live if ruling_in_force(r, cooldown_days=cooldown_days, now=now) else lapsed).append(r)
    return RulingMatch(
        in_force=max(live, key=_recency) if live else None,
        expired=max(lapsed, key=_recency) if lapsed else None,
    )


__all__ = [
    "ARCHIVE_FILENAME",
    "PENDING_FILENAME",
    "PriorRuling",
    "RulingMatch",
    "load_prior_rulings",
    "match_rulings",
    "ruling_in_force",
    "source_key",
]
