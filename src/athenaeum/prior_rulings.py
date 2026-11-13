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
* an ``attach-as-subpage`` (issue athenaeum#2539) folds nothing -- it sets a
  parent/child relation between the two pages instead -- but it is just as
  settling as an approve: the pair is never re-proposed, and the cooldown
  does not apply either.

A source set is keyed by the page FILENAMES (``Path.name``): the block header
itself only ever renders basenames, and the wiki is flat, so the key is stable
across a moved checkout where absolute source paths are not.

Only a rejection decays. ``librarian.rejected_merge_cooldown_days`` (default
``0`` = never) is the window after which a rejected set may be proposed again.

Issue athenaeum#2514 adds a fourth, non-human ruling: ``archived``. A
proposal the queue-hygiene sweep
(:func:`athenaeum.pending_merges.revalidate_pending_merges` with
``apply=True``) moved to the archive is ALSO a ruling the proposers must
respect -- before this, a sweep that archived 17 stale proposals was
followed by a night that re-proposed every one of them, and 16 had to be
archived again the next day. It decays on EVIDENCE rather than on a clock:
the sweep records a content digest
(:func:`athenaeum.pending_merges.source_content_digest`) over the source
pages, and the archive stops binding the moment that digest no longer
matches what is on disk -- i.e. exactly when one of the source pages has
changed since the archive. A block archived before that digest line existed
records no digest, so no change can be proven and the archive keeps binding
(the conservative direction: a proposal nobody has touched is not a new
question).
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

#: :attr:`PriorRuling.decision` for a proposal the queue-hygiene sweep
#: archived rather than a human answering it (issue athenaeum#2514).
ARCHIVED_DECISION = "archived"


@dataclass(frozen=True)
class PriorRuling:
    """One resolved merge block, reduced to what a proposer needs."""

    merge_id: str
    decision: str  # "reject" (human) | "approve" | "attach-as-subpage" | "archived"
    sources: frozenset[str]  # page filenames
    base_name: str  # display name, else merge target name
    ruled_at: datetime | None  # None when no timestamp could be recovered
    note: str = ""
    #: Issue athenaeum#2514: ``True`` only on an ``archived`` ruling whose
    #: recorded content digest no longer matches the source
    #: pages on disk -- i.e. the one circumstance in which an archive stops
    #: binding. Computed once, at load time, by :func:`load_prior_rulings`
    #: (the only place that holds both the recorded digest and the wiki
    #: root), so :func:`ruling_in_force` stays a pure function.
    sources_changed: bool = False

    def describe(self) -> str:
        """Short phrase for a proposal rationale or log line."""
        when = self.ruled_at.date().isoformat() if self.ruled_at else "an unknown date"
        if self.decision == ARCHIVED_DECISION:
            # Issue athenaeum#2514: never "by a human" -- the queue-hygiene
            # sweep archived this, and saying otherwise would misattribute
            # a machine sweep to the operator in a reviewer-facing rationale.
            tail = f" (reason: {self.note})" if self.note else ""
            return f"previously archived as stale on {when}{tail}"
        if self.decision == "reject":
            verb = "rejected"
        elif self.decision == "attach-as-subpage":
            # Issue athenaeum#2539: distinct from "approved" -- an attach
            # folds nothing, so describing it as an approval would misname
            # what actually happened on disk.
            verb = "resolved (attach-as-subpage)"
        else:
            verb = "approved"
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
    from athenaeum.pending_merges import parse_pending_merges, source_content_digest

    rulings: list[PriorRuling] = []
    # One memo for this whole load, so a page named by twenty archived
    # blocks is read once. Scoped to the call: every digest below is then
    # taken at the same instant.
    page_digests: dict[Path, str] = {}
    for filename in (PENDING_FILENAME, ARCHIVE_FILENAME):
        for pm in parse_pending_merges(wiki_root / filename):
            if not pm.sources:
                continue
            if not pm.decision and pm.archived_at:
                # Issue athenaeum#2514 AC2: no human decision, but the
                # queue-hygiene sweep moved this block to the archive. That
                # IS a ruling -- "this proposal is not worth a human's
                # time" -- and the proposers must respect it until one of
                # its source pages actually changes. An archived block that
                # records no digest (written before this issue) can never
                # prove a change, so `sources_changed` stays False and the
                # archive keeps binding.
                rulings.append(
                    PriorRuling(
                        merge_id=pm.id,
                        decision=ARCHIVED_DECISION,
                        sources=source_key(pm.sources),
                        base_name=pm.display_name or pm.merge_target_name,
                        ruled_at=_parse_when(pm.archived_at, pm.created_at),
                        note=pm.archive_reason,
                        sources_changed=bool(pm.source_digest)
                        and pm.source_digest
                        != source_content_digest(
                            pm.sources,
                            wiki_root=wiki_root,
                            page_digests=page_digests,
                        ),
                    )
                )
                continue
            if not pm.resolved:
                continue
            if pm.decision == "reject":
                decision = "reject"
            elif pm.decision == "approve":
                decision = "approve"
            elif pm.decision == "attach-as-subpage":
                # Issue athenaeum#2539: an attach, like an approve, settles
                # the pair -- never re-proposed, cooldown does not apply
                # (see ruling_in_force: only "reject" ever decays).
                decision = "attach-as-subpage"
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

    An ``archived`` ruling (issue athenaeum#2514) ignores *cooldown_days*
    entirely -- it is not a clock-decaying human answer but a machine
    sweep's verdict on a proposal that has gone stale, and the only thing
    that unmakes it is one of its source pages changing. That is
    :attr:`PriorRuling.sources_changed`, already decided at load time.
    """
    if ruling.decision == ARCHIVED_DECISION:
        return not ruling.sources_changed
    if ruling.decision != "reject" or cooldown_days <= 0 or ruling.ruled_at is None:
        return True
    current = now or datetime.now(timezone.utc)
    return current - ruling.ruled_at <= timedelta(days=cooldown_days)


@dataclass(frozen=True)
class RulingMatch:
    """What the archive says about one source set.

    ``in_force`` is the ruling a proposer must honour (skip the proposal);
    ``expired`` is the most recent ruling that no longer binds -- a rejection
    whose cooldown has lapsed, or (issue athenaeum#2514) an archive whose
    source pages have changed since. The proposal may go ahead, and its
    rationale should name it.
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
    "ARCHIVED_DECISION",
    "ARCHIVE_FILENAME",
    "PENDING_FILENAME",
    "PriorRuling",
    "RulingMatch",
    "load_prior_rulings",
    "match_rulings",
    "ruling_in_force",
    "source_key",
]
