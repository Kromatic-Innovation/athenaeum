# SPDX-License-Identifier: Apache-2.0
"""Auto-supersession -- the destructive half of the five-verdict comparator
(issue athenaeum#715, phase 2).

:mod:`athenaeum.comparator` DECIDES a verdict; this module decides whether a
``contradiction`` verdict is safe to auto-ENACT (retire the loser's page,
unattended) versus safe only to hand to a human via the review queue. Nothing
here calls an LLM, computes a similarity scalar, or reads a confidence field
-- issue athenaeum#715's own text is explicit that "thresholds attach to
reversibility, never to model self-reports," and auto-retiring a claim is the
single most irreversible thing this subsystem can do to a page. Every
precondition below is typed/structural: a date comparison, a set-membership
check, a partial-order lookup, or a ledger count.

**The silent-no-op trap, and why this module refuses to repeat it.** Issue
athenaeum#715 was filed carrying a design comment about
:func:`athenaeum.resolutions._narrow_scope_interval`: that helper returns
``None`` when the other side has no ``valid_from``, so a defensible verdict
silently enacts to *nothing* and the pair goes right on re-escalating,
indistinguishable from a resolver that never ran. :func:`enact_supersession`
is built the opposite way on purpose:

- It never returns ``None`` as a "handled" outcome. Success returns the
  written :class:`~pathlib.Path`; anything that would otherwise have been a
  silent no-op raises :class:`SupersessionNotEnactable` instead, so a caller
  cannot mistake "nothing happened" for "done."
- :func:`decide_supersession` never lets a page-global retirement stand in
  for a claim-level one: the ``located`` precondition (AC3 below) fails
  closed whenever :class:`~athenaeum.comparator.CompareOutcome`'s
  ``conflicting_passages`` is empty, because there is no claim to retire
  without a located passage to retire it *at*.
- A pair with no ``observed_at`` on either side can never reach enactment.
  Both the observed-time and recorded-time preconditions (AC6/AC7) are
  ABSENT-FAILS-CLOSED, not absent-degrades-to-underdetermined-but-still-
  applies -- there is no branch anywhere below that treats a missing
  temporal coordinate as "fine, proceed."

**Claim-level retirement on a page-level file (an honest limitation).** This
repo has no block-level claim model -- issue athenaeum#715 explicitly defers
one. :func:`enact_supersession` therefore does the most honest thing
available at page granularity: it stamps ``superseded_claim`` with the
*located* passage(s) from Gate 2's ``conflicting_passages`` (never the whole
page), so a reader of the loser's frontmatter can see exactly which claim was
retired even though the retirement mechanism (``superseded_by`` on the whole
file) is page-granular. Agreeing content elsewhere on the same page is not
retired and is not implied to be.

**Authority is a partial order, asserter comparison is three-valued.** This
module is a pure consumer of :mod:`athenaeum.asserter_authority` (routes (b)
and (c) below) and :func:`athenaeum.models.compare_asserters` (route (a)) --
see those modules' docstrings for why authority never collapses to a total
order and why an unknown asserter identity is never silently treated as
"same." Nothing here re-derives or second-guesses those verdicts.

**No block-level content model means "corroborates" and "conflicts" collapse
onto one structural signal without an LLM call.** AC9's "no third conflicting
live claim" and route (c)'s "independent corroborating live claim" both ask
the same underlying question -- "does a third live page occupy the winner's
exact coordinates?" -- and this module has no way to tell, without a
prohibited LLM call, whether such a third page *agrees* or *disagrees* with
the winner. The two conditions are kept from contradicting each other (a
route-(c) pass can never by construction also trip AC9's failure) by keying
them on DIFFERENT, deliberately conservative, mutually-exclusive asserter
relationships to the LOSER and to the pair as a whole:

- AC9 flags a third live (non-superseded) claim at the winner's coordinates
  whenever :func:`~athenaeum.models.compare_asserters` says its asserter is
  NOT confirmed different from the LOSER's -- i.e. "same" *or* "unknown"
  both count against the pair, queuing on doubt. The reading: "is the
  position being retired actually isolated to this one page, or does the
  same disputing party have another live copy of it sitting elsewhere?" If
  so, retiring just this one loser page would not actually resolve anything
  corpus-wide, and auto-applying would look like it did -- another shape of
  silent no-op.
- Route (c) corroboration requires a third live claim CONFIRMED different
  (via :func:`~athenaeum.models.compare_asserters`, never "unknown") from
  BOTH the winner's and the loser's identity -- a genuinely independent third
  voice, found live and unsuperseded at the winner's exact coordinates.
  "Unknown" never grants the permissive route.

Because AC9's trigger set requires "not confirmed different from loser" and
route (c)'s trigger set requires "confirmed different from loser," the two
sets can never overlap -- a route-(c) corroborator is never also an AC9
trigger, so the permissive route is genuinely reachable rather than always
self-cancelling. This is a conservative structural proxy, not a real
agreement check, and it is documented here as exactly that.

**Winner determination is doubly used.** The winner is the side strictly
later on observed-time (AC6); if observed-time cannot separate the pair
(absent on either side, or equal) there IS no winner, ``winner_id=None``, and
the pair queues -- see the silent-no-op note above. Every OTHER precondition
below (AC4/5/7/8/9/rate-limits) is still evaluated independently even when no
real winner exists, using ``page_a`` as a fixed structural placeholder for
"the side that would win" -- this is what lets a single failing precondition
be tested in isolation without every other precondition also going dark for
lack of a determined winner. The placeholder never leaks into the returned
decision: ``winner_id``/``loser_id`` are ``None`` whenever AC6 itself fails.

**Rate limits gate route (a) only, never the whole decision.** AC10's two
caps (per-claim self-revision, per-asserter weekly volume) exist to catch,
respectively, one asserter oscillating a single fact and one asserter
drifting the corpus broadly -- both routed through route (a) (same asserter
revising their own claim). Routes (b) and (c) are not "the same asserter
again," so the caps are recorded ``True`` (not applicable) under those routes
rather than blocking them -- the two rate-limit conditions constrain
``asserter_route``'s route-(a) branch internally; they are not independent
top-level AND-gates over the whole decision (a route-(b)/(c) pass must not be
vetoed by a route-(a) rate limit that was never invoked).

**Ledger write ownership.** :func:`decide_supersession` is a pure decision
function -- it only ever READS the audit ledger (to count prior ``applied``
records for the rate limits) and never writes to it, so it stays cheap and
side-effect-free to call repeatedly (tests call it dozens of times per
process). Only :func:`enact_supersession` appends to the ledger, and only
once a write has actually landed on the loser's frontmatter -- an ``applied``
ledger row is proof a retirement genuinely happened, never a projection of
one that might not.

**A known gap, called out rather than hidden.** :func:`enact_supersession`'s
signature takes ``winner_id`` (a bare page-id string), matching the AC's
API -- it never reads or writes the winner's file ("never touch the
winner"). But AC10's rate limits are keyed on the winner's *asserter
identity*, not its page id (the same asserter can win under a different
page each time), and that identity lives only in the winner's frontmatter.
The optional keyword-only ``winner_meta`` parameter closes that gap: pass the
winner :class:`~athenaeum.comparator.ComparatorPage`'s ``meta`` so the
appended ledger record carries a real ``winner_asserter_key`` that future
:func:`decide_supersession` calls can count against. Omitting it still
appends a fully truthful audit record (the retirement is never silently
dropped) -- only the asserter key degrades to ``[]``, which simply never
matches a future rate-limit lookup, i.e. it fails safe by under-counting a
cap rather than over-blocking on an identity nobody supplied. This is
additive and backward compatible with the documented five-argument call
shape; no existing argument changed name, position, or meaning.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum.asserter_authority import (
    compare_authority,
    strictly_greater_authority,
    treated_as_equal_authority,
)
from athenaeum.atomic_io import atomic_write_text
from athenaeum.comparator import (
    VERDICT_CONTRADICTION,
    ComparatorPage,
    CompareOutcome,
    gate1_separator_relations,
)
from athenaeum.config import (
    resolve_auto_supersession_enabled,
    resolve_standing_state_claim_kinds,
    resolve_supersession_asserter_weekly_max,
    resolve_supersession_claim_window_max,
    resolve_supersession_self_revision_window_days,
)
from athenaeum.dimensions import (
    DEFAULT_REGISTRY,
    DimensionRegistry,
    LifecycleState,
    Relation,
    compare_dimension,
    coordinate_value,
    dimension_applies,
)
from athenaeum.models import (
    asserter_identity_key,
    compare_asserters,
    parse_asserter,
    parse_claim_kind,
    parse_frontmatter,
    parse_observed_at,
    parse_superseded_by,
    render_frontmatter,
)
from athenaeum.store import append_line_durable

log = logging.getLogger(__name__)

__all__ = [
    "SUPERSESSION_APPLIED",
    "SUPERSESSION_LEDGER_NAME",
    "SUPERSESSION_QUEUE",
    "SupersessionDecision",
    "SupersessionNotEnactable",
    "append_supersession_record",
    "decide_supersession",
    "enact_supersession",
    "read_supersession_records",
]

#: The two values :attr:`SupersessionDecision.action` can take.
SUPERSESSION_APPLIED = "applied"
SUPERSESSION_QUEUE = "queue"

#: The audit ledger filename, under ``wiki_root`` -- append-only JSONL, one
#: record per successfully ENACTED supersession (see the module docstring's
#: "Ledger write ownership" section for why queued decisions are not logged
#: here).
SUPERSESSION_LEDGER_NAME = "_supersessions.jsonl"

#: Preconditions that jointly gate :data:`SUPERSESSION_APPLIED` -- every name
#: here must be ``True`` for :func:`decide_supersession` to return applied.
#: ``rate_limit_per_claim``/``rate_limit_per_asserter`` are deliberately NOT
#: members of this tuple (see module docstring, "Rate limits gate route (a)
#: only") -- they are folded into ``asserter_route`` instead.
_DRIVING_CONDITIONS: tuple[str, ...] = (
    "auto_supersession_enabled",
    "verdict_is_contradiction",
    "located",
    "standing_state",
    "no_overlaps",
    "observed_time_strictly_later",
    "recorded_time_not_earlier",
    "asserter_route",
    "no_third_conflicting_live_claim",
)

#: Every condition name eligible to appear in ``blocked_by`` -- the driving
#: set plus the two rate-limit knobs (informational: a caller can see a rate
#: limit tripped even on a decision that ultimately applied via a different
#: route). The three ``route_*`` sub-booleans are deliberately excluded --
#: they are detail on how ``asserter_route`` was decided, not independent
#: preconditions in their own right.
_BLOCKABLE_CONDITIONS: tuple[str, ...] = _DRIVING_CONDITIONS + (
    "rate_limit_per_claim",
    "rate_limit_per_asserter",
)

#: Rolling window for the per-asserter weekly cap (AC10) -- fixed at 7 days,
#: unlike the per-claim window which is the configurable
#: :func:`~athenaeum.config.resolve_supersession_self_revision_window_days`.
_ASSERTER_WEEKLY_WINDOW_DAYS = 7


class SupersessionNotEnactable(Exception):
    """Raised by :func:`enact_supersession` when a decision cannot be written.

    Deliberately the ONLY way :func:`enact_supersession` can fail to write --
    it never returns ``None`` (see module docstring, "silent-no-op trap").
    Covers both a decision that is not actually applicable (wrong action,
    empty located_passages, unresolved winner/loser) and a real I/O failure
    (unreadable/unwritable loser path).
    """


@dataclass(frozen=True)
class SupersessionDecision:
    """The result of one :func:`decide_supersession` call.

    ``conditions`` is ALWAYS fully populated (every name in
    :data:`_BLOCKABLE_CONDITIONS` plus the three ``route_*`` detail keys) --
    a caller/test can read exactly which precondition(s) failed without
    re-deriving them. ``blocked_by`` is the sorted subset of
    :data:`_BLOCKABLE_CONDITIONS` that evaluated ``False``; empty exactly
    when ``action == SUPERSESSION_APPLIED``.
    """

    action: str
    winner_id: str | None
    loser_id: str | None
    located_passages: list[str]
    conditions: dict[str, bool]
    blocked_by: list[str]
    reason: str
    rate_limited: str | None = None


# ---------------------------------------------------------------------------
# Small, self-contained date/datetime coercion (deliberately NOT importing
# athenaeum.models._coerce_iso_date / athenaeum.dimensions._coerce_date_or_none
# -- both private-by-convention to their own modules, mirroring
# athenaeum.dimensions's own documented rationale for its self-contained
# duplicate: keeps this module independently testable and fail-open).
# ---------------------------------------------------------------------------


def _parse_recorded_at(meta: dict[str, Any] | None) -> datetime | None:
    """Return the frontmatter ``recorded_at`` as a timezone-aware ``datetime``.

    ``recorded_at`` is stamped with second precision (see
    :mod:`athenaeum.models`'s ``_recorded_time_now``), so this parses the
    full ISO-8601 instant rather than truncating to a date the way
    ``observed_at``/``valid_from`` do -- AC7's "not earlier than" needs
    same-day ordering, not just same-day equality. Naive values are treated
    as UTC so every comparison in this module is between aware datetimes.
    Fail-open: missing / unparseable -> ``None``.
    """
    if not meta:
        return None
    raw = meta.get("recorded_at")
    if raw is None or raw == "":
        return None
    dt: datetime | None = None
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, date):
        dt = datetime(raw.year, raw.month, raw.day)
    elif isinstance(raw, str):
        value = raw.strip()
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            try:
                dt = datetime.combine(date.fromisoformat(value[:10]), datetime.min.time())
            except ValueError:
                log.debug("supersession: unparseable recorded_at %r; treating as absent", raw)
                return None
    else:
        log.debug("supersession: non-date recorded_at %r; treating as absent", raw)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_aware(dt: datetime) -> datetime:
    """Attach UTC if *dt* is naive; a comparison helper for ledger ``at`` values."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _asserter_key(meta: dict[str, Any] | None) -> tuple[str, ...]:
    """``asserter_identity_key(parse_asserter(meta))`` -- the recurring pair."""
    return asserter_identity_key(parse_asserter(meta))


# ---------------------------------------------------------------------------
# AC9 / route (c) -- the shared "same coordinates as the winner" structural
# test, and the disjoint asserter-relationship split that keeps the two from
# contradicting each other (see module docstring).
# ---------------------------------------------------------------------------


def _same_coordinates(
    registry: DimensionRegistry,
    meta_winner: dict[str, Any] | None,
    meta_candidate: dict[str, Any] | None,
) -> bool:
    """True when *meta_candidate* sits at the SAME coordinates as *meta_winner*.

    "Same coordinates" (AC9's own phrase): every separator dimension Gate 1
    would consult between the two is either ``Relation.EQUAL`` or the
    coordinate is ABSENT on both sides (not merely ``unknown`` for some other
    reason, e.g. a ``backfill``-state dimension with one side populated --
    that is a real difference in what is known, not "both silent"). Any
    other relation (``contains``/``overlaps``/``disjoint``, or ``unknown``
    with a coordinate present on one side) means "not the same slot."
    """
    for dimension in registry:
        if not dimension.separates:
            continue
        if dimension.state != LifecycleState.ENFORCED:
            continue
        if not (
            dimension_applies(dimension, meta_winner)
            and dimension_applies(dimension, meta_candidate)
        ):
            continue
        relation = compare_dimension(dimension, meta_winner, meta_candidate)
        if relation == Relation.EQUAL:
            continue
        if (
            relation == Relation.UNKNOWN
            and coordinate_value(dimension, meta_winner) is None
            and coordinate_value(dimension, meta_candidate) is None
        ):
            continue
        return False
    return True


def _third_party_signals(
    *,
    registry: DimensionRegistry,
    winner: ComparatorPage,
    loser: ComparatorPage,
    live_claims: tuple[ComparatorPage, ...],
    exclude_ids: frozenset[str],
) -> tuple[bool, bool]:
    """Return ``(no_third_conflicting_live_claim, route_c_corroborated_signal)``.

    ``route_c_corroborated_signal`` is JUST the "an independent corroborating
    live claim exists" half of route (c) -- the caller still ANDs it with
    ``treated_as_equal_authority(...)`` per AC8's route (c) definition.
    """
    asserter_winner = parse_asserter(winner.meta)
    asserter_loser = parse_asserter(loser.meta)

    conflicting_found = False
    corroborating_found = False
    for candidate in live_claims:
        if candidate.id in exclude_ids:
            continue
        if parse_superseded_by(candidate.meta):
            continue
        if not _same_coordinates(registry, winner.meta, candidate.meta):
            continue
        asserter_candidate = parse_asserter(candidate.meta)
        loser_cmp = compare_asserters(asserter_loser, asserter_candidate)
        winner_cmp = compare_asserters(asserter_winner, asserter_candidate)
        # AC9: queue on doubt -- "same" AND "unknown" both count as NOT
        # confirmed different from the loser.
        if loser_cmp != "different":
            conflicting_found = True
        # Route (c): doubt never grants the permissive route -- both
        # comparisons must be an affirmatively-confirmed "different".
        if winner_cmp == "different" and loser_cmp == "different":
            corroborating_found = True

    return (not conflicting_found, corroborating_found)


# ---------------------------------------------------------------------------
# The decision function
# ---------------------------------------------------------------------------


def decide_supersession(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome,
    *,
    wiki_root: Path,
    config: dict[str, Any] | None = None,
    registry: DimensionRegistry = DEFAULT_REGISTRY,
    now: datetime | None = None,
    live_claims: Sequence[ComparatorPage] = (),
) -> SupersessionDecision:
    """Decide whether *outcome* (already computed by
    :func:`athenaeum.comparator.compare_pages`) may be auto-enacted.

    Pure / read-only: the only I/O is reading
    :func:`read_supersession_records` under *wiki_root* to count prior
    ``applied`` events for the two rate limits (AC10), and only when route
    (a) is structurally in play. Never writes -- see module docstring,
    "Ledger write ownership."

    Returns :class:`SupersessionDecision` with ``action ==
    SUPERSESSION_APPLIED`` only when every name in
    :data:`_DRIVING_CONDITIONS` is ``True``; otherwise
    ``SUPERSESSION_QUEUE``. See the module docstring for the winner/loser
    determination and the AC9/route-(c) asserter-relationship split.
    """
    now_dt = _as_aware(now) if now is not None else datetime.now(timezone.utc)
    live_claims_tuple: tuple[ComparatorPage, ...] = tuple(live_claims)

    # --- AC1/AC2/AC3: cheap, winner-independent preconditions ---
    cond_enabled = resolve_auto_supersession_enabled(config)
    cond_contradiction = outcome.verdict == VERDICT_CONTRADICTION
    located_passages = list(outcome.conflicting_passages)
    cond_located = bool(located_passages)

    # --- AC4: standing-state claim kind, both sides ---
    standing_kinds = resolve_standing_state_claim_kinds(config)
    cond_standing_state = (
        parse_claim_kind(page_a.meta) in standing_kinds
        and parse_claim_kind(page_b.meta) in standing_kinds
    )

    # --- AC5: no partial-overlap on any consulted separator dimension ---
    rels = gate1_separator_relations(registry, page_a.meta, page_b.meta)
    cond_no_overlaps = not any(relation == Relation.OVERLAPS for relation in rels.values())

    # --- Winner/loser determination (AC6) ---
    observed_a = parse_observed_at(page_a.meta)
    observed_b = parse_observed_at(page_b.meta)
    winner: ComparatorPage | None
    loser: ComparatorPage | None
    if observed_a is not None and observed_b is not None and observed_a != observed_b:
        winner, loser = (page_a, page_b) if observed_a > observed_b else (page_b, page_a)
        cond_observed_later = True
    else:
        winner, loser = None, None
        cond_observed_later = False

    # A fixed structural placeholder so every OTHER precondition below stays
    # independently computable (and independently testable) even when AC6
    # itself fails -- see module docstring, "Winner determination is doubly
    # used." Never leaks into the returned winner_id/loser_id.
    put_winner: ComparatorPage
    put_loser: ComparatorPage
    if winner is not None and loser is not None:
        put_winner, put_loser = winner, loser
    else:
        put_winner, put_loser = page_a, page_b
    exclude_ids = frozenset({page_a.id, page_b.id})

    # --- AC7: winner's recorded_at not earlier than loser's ---
    recorded_winner = _parse_recorded_at(put_winner.meta)
    recorded_loser = _parse_recorded_at(put_loser.meta)
    cond_recorded_not_earlier = (
        recorded_winner is not None
        and recorded_loser is not None
        and recorded_winner >= recorded_loser
    )

    # --- AC8: asserter route (a) / (b) / (c) ---
    asserter_put_winner = parse_asserter(put_winner.meta)
    asserter_put_loser = parse_asserter(put_loser.meta)
    route_a_same_asserter = compare_asserters(asserter_put_winner, asserter_put_loser) == "same"
    route_b_greater_authority = strictly_greater_authority(
        put_winner.meta, put_loser.meta, config=config
    )
    authority_relation = compare_authority(put_winner.meta, put_loser.meta, config=config)
    equal_or_incomparable = treated_as_equal_authority(authority_relation)

    cond_no_third_conflicting, corroboration_signal = _third_party_signals(
        registry=registry,
        winner=put_winner,
        loser=put_loser,
        live_claims=live_claims_tuple,
        exclude_ids=exclude_ids,
    )
    route_c_corroborated = equal_or_incomparable and corroboration_signal

    # --- AC10: rate limits, route (a) only ---
    rate_limit_per_claim = True
    rate_limit_per_asserter = True
    rate_limited: str | None = None
    if route_a_same_asserter:
        winner_key = _asserter_key(put_winner.meta)
        records = read_supersession_records(wiki_root)
        window_days = resolve_supersession_self_revision_window_days(config)
        claim_max = resolve_supersession_claim_window_max(config)
        weekly_max = resolve_supersession_asserter_weekly_max(config)

        per_claim_count = 0
        per_asserter_count = 0
        for record in records:
            if record.get("action") != SUPERSESSION_APPLIED:
                continue
            record_key = tuple(record.get("winner_asserter_key") or ())
            if not record_key or record_key != winner_key:
                continue
            at_raw = record.get("at")
            at_dt = _parse_recorded_at({"recorded_at": at_raw}) if at_raw else None
            if at_dt is None:
                continue
            age = now_dt - at_dt
            if age <= timedelta(days=_ASSERTER_WEEKLY_WINDOW_DAYS):
                per_asserter_count += 1
            if record.get("loser_id") == put_loser.id and age <= timedelta(days=window_days):
                per_claim_count += 1

        rate_limit_per_claim = (per_claim_count + 1) < claim_max
        rate_limit_per_asserter = (per_asserter_count + 1) <= weekly_max
        if not rate_limit_per_claim:
            rate_limited = "per-claim"
        elif not rate_limit_per_asserter:
            rate_limited = "per-asserter"

    route_a_effective = route_a_same_asserter and rate_limit_per_claim and rate_limit_per_asserter
    cond_asserter_route = route_a_effective or route_b_greater_authority or route_c_corroborated

    conditions: dict[str, bool] = {
        "auto_supersession_enabled": cond_enabled,
        "verdict_is_contradiction": cond_contradiction,
        "located": cond_located,
        "standing_state": cond_standing_state,
        "no_overlaps": cond_no_overlaps,
        "observed_time_strictly_later": cond_observed_later,
        "recorded_time_not_earlier": cond_recorded_not_earlier,
        "asserter_route": cond_asserter_route,
        "no_third_conflicting_live_claim": cond_no_third_conflicting,
        "rate_limit_per_claim": rate_limit_per_claim,
        "rate_limit_per_asserter": rate_limit_per_asserter,
        "route_a_same_asserter": route_a_same_asserter,
        "route_b_greater_authority": route_b_greater_authority,
        "route_c_corroborated": route_c_corroborated,
    }

    all_pass = all(conditions[name] for name in _DRIVING_CONDITIONS)
    action = SUPERSESSION_APPLIED if all_pass else SUPERSESSION_QUEUE
    blocked_by = sorted(name for name in _BLOCKABLE_CONDITIONS if not conditions[name])

    winner_id = winner.id if winner is not None else None
    loser_id = loser.id if loser is not None else None

    if action == SUPERSESSION_APPLIED:
        if route_a_effective:
            reason = "applied: route (a) -- same asserter revising its own claim"
        elif route_b_greater_authority:
            reason = "applied: route (b) -- winner has strictly greater authority"
        else:
            reason = (
                "applied: route (c) -- equal/incomparable authority with an "
                "independent corroborating live claim"
            )
    else:
        reason = "queued: blocked by " + ", ".join(blocked_by)

    return SupersessionDecision(
        action=action,
        winner_id=winner_id,
        loser_id=loser_id,
        located_passages=located_passages,
        conditions=conditions,
        blocked_by=blocked_by,
        reason=reason,
        rate_limited=rate_limited,
    )


# ---------------------------------------------------------------------------
# Enactment
# ---------------------------------------------------------------------------


def enact_supersession(
    decision: SupersessionDecision,
    *,
    loser_path: Path,
    winner_id: str,
    wiki_root: Path,
    now: datetime | None = None,
    winner_meta: dict[str, Any] | None = None,
) -> Path:
    """Enact *decision* by retiring the loser's page. Never returns ``None``.

    Writes, atomically (:func:`athenaeum.atomic_io.atomic_write_text`), on
    the LOSER page's frontmatter ONLY -- ``superseded_by``,
    ``superseded_claim`` (the located passage(s), never the whole page --
    see module docstring's "claim-level retirement on a page-level file"),
    and ``superseded_at``. Every other frontmatter key and the body are
    preserved verbatim. The WINNER's file is never read or written.

    Then appends an ``action="applied"`` record to the ledger
    (:func:`append_supersession_record`). ``winner_meta`` is an optional,
    additive keyword-only parameter (see module docstring, "A known gap") --
    when supplied, the appended record carries a real
    ``winner_asserter_key`` so future :func:`decide_supersession` calls can
    count this event against AC10's rate limits; omitting it still appends a
    truthful record, just with an empty (never-matching) key.

    Raises :class:`SupersessionNotEnactable` -- never returns ``None`` --
    when *decision* is not an applied decision, is missing a located claim
    or a resolved winner/loser (both would make this a silent page-global
    retirement, the exact trap this module exists to avoid), or the loser
    path cannot be read or written.
    """
    if decision.action != SUPERSESSION_APPLIED:
        raise SupersessionNotEnactable(
            f"cannot enact a {decision.action!r} decision (only "
            f"{SUPERSESSION_APPLIED!r} decisions are enactable)"
        )
    if not decision.located_passages:
        raise SupersessionNotEnactable(
            "decision has no located_passages -- refusing a page-global retirement"
        )
    if decision.winner_id is None or decision.loser_id is None:
        raise SupersessionNotEnactable("decision has no resolved winner_id/loser_id")

    loser_path = Path(loser_path)
    try:
        text = loser_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SupersessionNotEnactable(f"cannot read loser page {loser_path}: {exc}") from exc

    meta, body = parse_frontmatter(text)
    meta = dict(meta) if meta else {}
    now_dt = _as_aware(now) if now is not None else datetime.now(timezone.utc)
    stamped_at = now_dt.isoformat(timespec="seconds")

    meta["superseded_by"] = winner_id
    meta["superseded_claim"] = list(decision.located_passages)
    meta["superseded_at"] = stamped_at
    rendered = render_frontmatter(meta) + body

    try:
        atomic_write_text(loser_path, rendered)
    except OSError as exc:
        raise SupersessionNotEnactable(f"cannot write loser page {loser_path}: {exc}") from exc

    record: dict[str, Any] = {
        "action": SUPERSESSION_APPLIED,
        "winner_id": winner_id,
        "loser_id": decision.loser_id,
        "winner_asserter_key": list(_asserter_key(winner_meta)) if winner_meta else [],
        "located_passages": list(decision.located_passages),
        "reason": decision.reason,
        "at": stamped_at,
    }
    append_supersession_record(wiki_root, record)
    return loser_path


# ---------------------------------------------------------------------------
# Audit ledger -- durable JSONL append, tolerant read
# ---------------------------------------------------------------------------


def _ledger_path(wiki_root: Path) -> Path:
    return Path(wiki_root) / SUPERSESSION_LEDGER_NAME


def append_supersession_record(wiki_root: Path, record: dict[str, Any]) -> Path:
    """Append *record* (any JSON-serializable dict) to the audit ledger.

    Durable ``O_APPEND`` + fsync via
    :func:`athenaeum.store.append_line_durable` -- the shared primitive
    every other JSONL ledger in this repo (e.g.
    :func:`athenaeum.verdicts.append_verdict`) already routes through, so a
    crash mid-write can at worst leave a torn TRAILING line, never corrupt an
    already-written record. Returns the ledger path.
    """
    path = _ledger_path(wiki_root)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    append_line_durable(path, line.encode("utf-8"))
    return path


def read_supersession_records(wiki_root: Path) -> list[dict[str, Any]]:
    """Read the audit ledger as JSONL, tolerantly. ``[]`` if absent.

    Skips blank lines and any line that fails to parse as a JSON object
    (a torn trailing write, or a hand-edit) rather than raising -- mirrors
    :mod:`athenaeum.verdicts`'s ``_read_jsonl_tolerant`` posture.
    """
    path = _ledger_path(wiki_root)
    if not path.exists():
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records
