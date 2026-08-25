# SPDX-License-Identifier: Apache-2.0
"""Two bounded comparator instruments (issue athenaeum#715, phase 2) — L4 orchestrator.

:mod:`athenaeum.comparator` implements the five-verdict algorithm exactly as
athenaeum#715 specifies it, but the issue's own text asks for two more pieces of
behavior sitting ON TOP of that algorithm rather than inside it — both landed
here, deliberately kept out of ``comparator.py`` so that module stays a pure
transcription of the Gate-1/Gate-2 decision tree with no scheduling or
proposal-generation concerns mixed in:

1. **``compatible`` TTL re-check.** athenaeum#715: "TTL re-check on ``compatible``
   verdicts for high-write subjects -- default: re-compare after 6 months or
   20 content-adjacent writes. Configurable and documented." A ``compatible``
   content relation surfaces in the ledger as ``verdict == VERDICT_DISTINCT``
   with ``separator == [COEXIST_SEPARATOR]`` and
   ``comparator_version == COMPARATOR_VERSION_GATE2`` -- see
   :mod:`athenaeum.comparator`'s module docstring, "``content:coexist``
   separator marker". That is the ONE verdict shape this instrument selects:
   it is the sole DISTINCT exit with no *coordinate* separating the pair, so
   it is the one most likely to be silently falsified by later writes to
   either side (the subject drifts; the two pages start answering the same
   question).

   :mod:`athenaeum.verdicts` has **no time-based TTL today** -- freshness is
   purely the boolean ``stale`` flag (see that module's ``get_verdict_status``
   docstring). This instrument does not invent a second freshness concept: it
   is the missing age/write-count TRIGGER, expressed entirely by calling
   :func:`athenaeum.verdicts.mark_pairs_stale` on the pairs it selects, so the
   comparator's EXISTING ``get_verdict_status``-based memoization
   (:func:`athenaeum.comparator.record_comparison`) naturally re-compares them
   the next time it runs. :func:`run_ttl_recheck` never calls an LLM itself.

2. **Sibling-scope widening proposals.** athenaeum#715: "Top-band-similarity,
   scope-separated DISTINCTs in guideline-like classes get one memoized
   ``content_relation`` call anyway; an ``equivalent`` result emits a
   scope-widening proposal to the queue rather than leaving convergent local
   practice permanently fragmented. Bounded by a documented budget so this
   cannot become an unbounded LLM cost." A candidate is a pair Gate 1 ALREADY
   settled as DISTINCT specifically because the ``scope`` dimension came back
   ``Relation.DISJOINT`` (siblings -- genuinely different scopes, not merely
   "some dimension disjoint") -- see :func:`sibling_widening_candidates` for
   exactly how that is detected (the same :func:`athenaeum.dimensions.compare_dimension`
   call Gate 1 itself would have made for the ``scope`` dimension, called
   directly on that one dimension rather than re-running the whole Gate-1
   sweep).

   **Similarity is candidate generation ONLY (issue athenaeum#715 AC: no
   confidence thresholds on verdicts).** The caller-supplied similarity
   score's one and only job is proposing WHICH DISJOINT-scope pairs are worth
   spending a Gate-2 call on; it never reaches a verdict and is not present
   anywhere on :class:`WideningCandidate` or :class:`WideningProposal` --
   only the ``equivalent | conflicting | compatible`` result of the ONE
   memoized :func:`athenaeum.comparator.content_relation` call decides
   whether a proposal is emitted, exactly mirroring how Gate 2 itself decides
   (see ``comparator.py``'s module docstring, "No confidence thresholds,
   anywhere"). This is visibly NOT the confidence-threshold pattern athenaeum#715
   bans: a threshold gates a VERDICT; this gates which pairs are even OFFERED
   to the judge.

   A :class:`WideningProposal` is a PROPOSAL to the queue, never an automatic
   coordinate rewrite -- :func:`run_sibling_widening` returns proposals for a
   caller to route; nothing in this module mutates a page's ``claimed_scope``
   or writes a verdict for a widening candidate (the candidate's own Gate-1
   DISTINCT verdict, if any, is untouched).

**Budget discipline (issue athenaeum#715 AC: "bounded... so this cannot become
an unbounded LLM cost").** :func:`run_sibling_widening` enforces
:func:`athenaeum.config.resolve_sibling_widening_budget` BEFORE each
``content_relation`` call, never after -- a candidate that would exceed the
remaining budget is never dispatched. Every candidate skipped this way is
counted in the returned ``skipped_over_budget``, never silently dropped: a
truncated-but-uncounted list reads as "covered everything" to a caller that
only checks ``proposals``, which is exactly the failure this module's own
non-negotiables forbid.

**Memoization, reused not reinvented.** Both instruments read
:func:`athenaeum.verdicts.get_verdict_status` before doing anything that
costs an LLM call or a ledger write -- :func:`run_ttl_recheck` never calls
Gate 2 at all (it only marks pairs stale), and :func:`run_sibling_widening`
skips any candidate whose verdict is already fresh, exactly the same
memoization gate :func:`athenaeum.comparator.record_comparison` applies to
its own pairs.

**Content-adjacent writes, honestly defined.** There is no write-audit log in
this repo to consult, so :func:`record_content_writes` maintains the
smallest honest counter that IS measurable: a per-page ``{hash, writes}``
pair, incremented only when a page's content hash changes between two calls.
This has a real, named limitation -- it counts only writes OBSERVED on a run
where the instrument was actually invoked with that page's current hash; a
deployment that never calls :func:`record_content_writes` will never trigger
the write half of the TTL (the day half still fires on schedule regardless).
This is deliberately not hidden: see :func:`record_content_writes`'s own
docstring.

**No confidence thresholds, anywhere (issue athenaeum#715 AC).** Mirrors
:mod:`athenaeum.comparator`'s identical stance verbatim. Neither
:func:`select_compatible_ttl_expired` nor :func:`run_ttl_recheck` reads a
model-reported scalar of any kind -- the TTL trigger is age (an integer day
count) and a write counter (an integer), never a score. Neither
:class:`WideningCandidate` nor :class:`WideningProposal` carries a
similarity/confidence field, and the only branch in :func:`run_sibling_widening`
that decides whether to emit a proposal tests ``result.relation ==
ContentRelation.EQUIVALENT`` -- a three-way categorical from Gate 2, not a
threshold comparison.

**All I/O stays under ``wiki_root``.** Both instruments' persistent state
(the content-write counter, the write-baseline sidecar, and the verdict
ledger itself) lives under ``<wiki_root>/_verdicts/`` -- the same directory
:mod:`athenaeum.verdicts` already owns. Neither instrument ever reads or
writes anything under a caller's ``~/knowledge`` home directory; ``wiki_root``
is always an explicit, caller-supplied argument, never resolved from an
environment default.

Layering: L4 orchestrator, sitting directly above athenaeum#712's verdict ledger
(L2, :mod:`athenaeum.verdicts`), athenaeum#714's dimension registry (L1/L2,
:mod:`athenaeum.dimensions`), and athenaeum#715's own five-verdict comparator (L4,
:mod:`athenaeum.comparator`), reusing all three rather than reinventing any
part of them. Does not import :mod:`athenaeum.librarian` or
:mod:`athenaeum.decision_answers` -- like ``comparator.py`` itself, this
module lands dark; wiring either instrument into a scheduled run is a
separate, future step.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.comparator import (
    COEXIST_SEPARATOR,
    COMPARATOR_VERSION_GATE2,
    VERDICT_DISTINCT,
    ComparatorPage,
    ContentRelation,
    content_relation,
)
from athenaeum.config import (
    resolve_compatible_recheck_days,
    resolve_compatible_recheck_writes,
    resolve_sibling_widening_budget,
    resolve_sibling_widening_classes,
    resolve_sibling_widening_min_similarity,
)
from athenaeum.dimensions import (
    DEFAULT_REGISTRY,
    DimensionRegistry,
    Relation,
    compare_dimension,
    coordinate_value,
)
from athenaeum.runlock import RunLock
from athenaeum.verdicts import (
    VerdictEntry,
    get_verdict_status,
    iter_live_entries,
    ledger_dir,
    make_pair_key,
    mark_pairs_stale,
)

if TYPE_CHECKING:
    from athenaeum.models import TokenUsage
    from athenaeum.provider import LLMBackend

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Instrument 1 -- compatible TTL re-check
# ---------------------------------------------------------------------------

#: Filename (under ``<wiki_root>/_verdicts/``) of the content-write counter --
#: see :func:`record_content_writes`.
CONTENT_WRITE_COUNTER_NAME = "_content_writes.json"

#: Filename (under ``<wiki_root>/_verdicts/``) of the internal per-pair
#: write-count baseline sidecar -- NOT part of this module's documented
#: public contract (no ``resolve_*`` knob names it, no caller reads it
#: directly); it exists purely so :func:`select_compatible_ttl_expired` can
#: answer "has either side's write counter ADVANCED... since [it was last
#: checked]" without a second freshness concept living in
#: :mod:`athenaeum.verdicts`'s own schema (which this issue's own text
#: forbids touching -- see module docstring). Reset by :func:`run_ttl_recheck`
#: for every pair it flags, so a page's cumulative write count is never
#: replayed against the same baseline twice.
_PAIR_WRITE_BASELINE_NAME = "_content_write_baselines.json"

#: Stale-mark reason :func:`run_ttl_recheck` stamps on every pair it selects
#: -- reused as-is by :func:`athenaeum.verdicts.mark_pairs_stale`'s existing
#: "first stale reason wins, never overwritten" contract.
TTL_STALE_REASON = "compatible-ttl-expired"


def _content_writes_path(wiki_root: Path) -> Path:
    return ledger_dir(wiki_root) / CONTENT_WRITE_COUNTER_NAME


def _pair_write_baselines_path(wiki_root: Path) -> Path:
    return ledger_dir(wiki_root) / _PAIR_WRITE_BASELINE_NAME


def _load_json_dict(path: Path) -> dict[str, Any]:
    """Tolerant JSON-object read: missing/corrupt/non-dict -> ``{}``.

    Mirrors :mod:`athenaeum.verdicts`'s own fail-open posture toward its
    sidecar files (``_read_jsonl_tolerant``) -- a crash mid-write on either
    of this module's small state files degrades to "counter/baseline reset
    to empty," never a raised exception that would break a caller's run.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def count_content_writes(wiki_root: Path) -> dict[str, int]:
    """Current cumulative content-write count per page, as last recorded.

    Reads :data:`CONTENT_WRITE_COUNTER_NAME`; ``{}`` if the counter has never
    been written (issue athenaeum#715 -- see :func:`record_content_writes` for
    what "a write" means here). Malformed per-page entries are skipped rather
    than raising.
    """
    raw = _load_json_dict(_content_writes_path(wiki_root))
    out: dict[str, int] = {}
    for page_id, entry in raw.items():
        if isinstance(entry, dict) and isinstance(entry.get("writes"), int):
            out[str(page_id)] = entry["writes"]
    return out


def record_content_writes(wiki_root: Path, page_hashes: dict[str, str]) -> dict[str, int]:
    """Update the content-write counter for *page_hashes* (``{page_id: content_hash}``).

    A page's ``writes`` counter increments by exactly one when its hash
    CHANGES from the last value this function saw for that page id; an
    unchanged hash (including a page's first-ever appearance here) leaves
    ``writes`` untouched (a first sighting starts at ``0`` -- there is no
    prior hash to have changed FROM, so it is not itself a write).

    **This is the honest, measurable definition of "content-adjacent write"
    available in this repo (issue athenaeum#715's own phrase) -- and it has a
    real limitation, named here rather than hidden: it counts only writes
    OBSERVED on a run where THIS function was actually called with the
    page's current hash.** A page that changes between two calls that skip
    this function entirely is invisible to the counter; a deployment that
    never wires this in gets the day-based half of the TTL
    (:func:`resolve_compatible_recheck_days`) but never the write-based half.
    That is a real trade-off, not a bug -- there is no corpus-wide write
    audit log this module could consult instead without inventing one, which
    is explicitly out of this instrument's scope.

    Returns the FULL resulting ``{page_id: writes}`` map (every page ever
    recorded, not just the ones in *page_hashes*) so a caller can inspect the
    state it just wrote without a second read. Atomic write only
    (:func:`athenaeum.atomic_io.atomic_write_text`); this function does not
    take (or require) a :class:`~athenaeum.runlock.RunLock` -- unlike every
    :mod:`athenaeum.verdicts` ledger mutator, this counter is this module's
    own sidecar, not the ledger itself, and a caller may invoke it once per
    page write rather than once per locked run. A race between two
    unsynchronized callers can lose an update (last writer wins) but can
    never corrupt the file (atomic replace) or double-count past what either
    caller individually observed.
    """
    path = _content_writes_path(wiki_root)
    state = _load_json_dict(path)
    for page_id, new_hash in page_hashes.items():
        prior = state.get(page_id)
        if isinstance(prior, dict) and isinstance(prior.get("writes"), int):
            writes = prior["writes"]
            if prior.get("hash") != new_hash:
                writes += 1
        else:
            writes = 0
        state[page_id] = {"hash": new_hash, "writes": writes}
    atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    return {
        str(pid): entry["writes"]
        for pid, entry in state.items()
        if isinstance(entry, dict) and isinstance(entry.get("writes"), int)
    }


def _parse_at_date(at: str) -> date | None:
    if not at:
        return None
    try:
        return date.fromisoformat(at[:10])
    except ValueError:
        return None


def _as_date(now: datetime | date) -> date:
    return now.date() if isinstance(now, datetime) else now


def _current_pair_entries(wiki_root: Path) -> dict[str, VerdictEntry]:
    """The current (deduped, latest-``at``-wins) live verdict per pair.

    Same tie-break :func:`athenaeum.verdicts.lookup_pair` applies to a single
    pair key, computed for every pair in one ledger scan rather than one
    ``lookup_pair`` call per candidate -- :mod:`athenaeum.verdicts` has no
    bulk equivalent of its own, and this module does not modify that file to
    add one.
    """
    by_pair: dict[str, VerdictEntry] = {}
    for _month, entry in iter_live_entries(wiki_root):
        current = by_pair.get(entry.pair)
        if current is None or entry.at >= current.at:
            by_pair[entry.pair] = entry
    return by_pair


def _is_compatible_verdict(entry: VerdictEntry) -> bool:
    """True iff *entry* is the ``compatible`` content-relation shape (see
    module docstring): DISTINCT, ``separator == [COEXIST_SEPARATOR]``,
    decided by Gate 2."""
    return (
        entry.verdict == VERDICT_DISTINCT
        and entry.separator == [COEXIST_SEPARATOR]
        and entry.basis.comparator_version == COMPARATOR_VERSION_GATE2
    )


def select_compatible_ttl_expired(
    wiki_root: Path,
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Pair keys whose ``compatible`` verdict has crossed the TTL (issue athenaeum#715).

    A pair is selected when its current live verdict is the ``compatible``
    shape (:func:`_is_compatible_verdict`), is not already ``stale``, and
    EITHER:

    - its ``at`` is at least :func:`athenaeum.config.resolve_compatible_recheck_days`
      days before *now* (UTC today if *now* is omitted), OR
    - either side's content-write counter (:func:`count_content_writes`) has
      advanced by at least :func:`athenaeum.config.resolve_compatible_recheck_writes`
      since the last time this pair was checked (tracked internally -- see
      module docstring's ``_PAIR_WRITE_BASELINE_NAME`` note; a pair never
      before checked has an implicit baseline of ``0`` for each side, so its
      very first check compares the counter's full cumulative value against
      the threshold).

    Either trigger firing is sufficient. Pure with respect to the ledger and
    this module's OWN state files (no writes) -- :func:`run_ttl_recheck` is
    the only function that mutates anything, so calling this twice in a row
    with no intervening write returns the identical list.
    """
    now_dt = now or datetime.now(timezone.utc)
    now_date = _as_date(now_dt)
    recheck_days = resolve_compatible_recheck_days(config)
    recheck_writes = resolve_compatible_recheck_writes(config)
    writes = count_content_writes(wiki_root)
    baselines = _load_json_dict(_pair_write_baselines_path(wiki_root))

    out: list[str] = []
    for pair_key, entry in _current_pair_entries(wiki_root).items():
        if entry.stale:
            continue
        if not _is_compatible_verdict(entry):
            continue

        at_date = _parse_at_date(entry.at)
        age_expired = at_date is not None and (now_date - at_date).days >= recheck_days

        sides = pair_key.split("+", 1)
        baseline = baselines.get(pair_key)
        baseline = baseline if isinstance(baseline, dict) else {}
        writes_expired = any(
            writes.get(side, 0) - int(baseline.get(side, 0) or 0) >= recheck_writes
            for side in sides
        )

        if age_expired or writes_expired:
            out.append(pair_key)
    return sorted(out)


def _reset_pair_write_baselines(wiki_root: Path, pair_keys: list[str]) -> None:
    """Snapshot each of *pair_keys*'s sides' CURRENT write counts as the new
    baseline -- called by :func:`run_ttl_recheck` immediately after flagging
    a pair, so the write-count clock restarts from "now" rather than
    re-triggering on the same historical write count forever."""
    if not pair_keys:
        return
    writes = count_content_writes(wiki_root)
    path = _pair_write_baselines_path(wiki_root)
    baselines = _load_json_dict(path)
    for pair_key in pair_keys:
        sides = pair_key.split("+", 1)
        baselines[pair_key] = {side: writes.get(side, 0) for side in sides}
    atomic_write_text(path, json.dumps(baselines, indent=2, sort_keys=True) + "\n")


def run_ttl_recheck(
    wiki_root: Path,
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
    lock: RunLock,
) -> dict[str, Any]:
    """Stale-mark every pair :func:`select_compatible_ttl_expired` selects.

    Never calls an LLM -- this function only reads the ledger and this
    module's own counters, then writes stale marks via
    :func:`athenaeum.verdicts.mark_pairs_stale` (which itself requires the
    caller to already hold *lock* -- this function does not acquire it,
    matching every :mod:`athenaeum.verdicts` mutator's contract). Stale-marked
    pairs are picked up by :func:`athenaeum.comparator.record_comparison`'s
    existing memoization the next time it runs on them -- this function does
    not itself decide a new verdict.

    Returns ``{"ok": True, "expired": int, "marked_stale": int, "pairs":
    list[str]}``. ``expired`` and ``len(pairs)`` are always equal;
    ``marked_stale`` (from :func:`~athenaeum.verdicts.mark_pairs_stale`'s own
    return) can be smaller if a selected pair was concurrently marked stale
    by something else between selection and the write (mark_pairs_stale
    leaves an already-stale entry's reason untouched rather than double
    counting it).
    """
    now_dt = now or datetime.now(timezone.utc)
    expired_pairs = select_compatible_ttl_expired(wiki_root, config=config, now=now_dt)
    reasons = {pair: TTL_STALE_REASON for pair in expired_pairs}
    marked = mark_pairs_stale(wiki_root, reasons, lock=lock) if reasons else 0
    if expired_pairs:
        _reset_pair_write_baselines(wiki_root, expired_pairs)
    return {
        "ok": True,
        "expired": len(expired_pairs),
        "marked_stale": marked,
        "pairs": list(expired_pairs),
    }


# ---------------------------------------------------------------------------
# Instrument 2 -- sibling-scope widening proposals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WideningCandidate:
    """A scope-sibling pair worth ONE memoized Gate-2 call (issue athenaeum#715).

    Deliberately carries no similarity/confidence field -- see module
    docstring, "No confidence thresholds, anywhere". ``scope_a``/``scope_b``
    are each side's RAW ``scope`` coordinate (:func:`athenaeum.dimensions.coordinate_value`),
    kept only for the eventual :class:`WideningProposal`'s ``scopes`` list.
    """

    page_a_id: str
    page_b_id: str
    scope_a: Any
    scope_b: Any


@dataclass(frozen=True)
class WideningProposal:
    """A proposal to the queue: *page_a_id*/*page_b_id* look like convergent
    local practice across sibling scopes (issue athenaeum#715). Never applied
    automatically -- see module docstring. No similarity/confidence field;
    ``rationale`` is Gate 2's own one-sentence explanation
    (:attr:`athenaeum.comparator.ContentRelationResult.rationale`), not a
    number.
    """

    page_a_id: str
    page_b_id: str
    scopes: list[Any] = field(default_factory=list)
    rationale: str = ""


def sibling_widening_candidates(
    pairs: Sequence[tuple[ComparatorPage, ComparatorPage, float]],
    *,
    config: dict[str, Any] | None = None,
    registry: DimensionRegistry = DEFAULT_REGISTRY,
) -> list[WideningCandidate]:
    """Filter *pairs* down to sibling-scope widening candidates (issue athenaeum#715).

    *pairs* is ``(page_a, page_b, similarity)`` -- *similarity* is the
    caller's own measure (never computed here) and is consulted for exactly
    one purpose: the minimum-similarity floor below. A pair qualifies when
    ALL of:

    1. *similarity* ``>=`` :func:`athenaeum.config.resolve_sibling_widening_min_similarity`.
    2. The ``scope`` dimension specifically compares
       :data:`athenaeum.dimensions.Relation.DISJOINT` between the two sides
       -- the exact :func:`athenaeum.dimensions.compare_dimension` call
       :func:`athenaeum.comparator.gate1_separator_relations` would have made
       for ``scope`` alone (honoring that dimension's own ``applies_to`` /
       lifecycle-state rules; this does not re-run the whole Gate-1 sweep,
       only the one dimension this instrument cares about). This is what
       "sibling scopes" means here: genuinely different, disjoint territory
       on the SAME axis Gate 1 already uses to separate pairs.
    3. Both sides' ``memory-class`` coordinate is in
       :func:`athenaeum.config.resolve_sibling_widening_classes`.

    Order-preserving relative to *pairs*. A *registry* missing either the
    ``scope`` or ``memory-class`` kernel dimension (never true for
    :data:`athenaeum.dimensions.DEFAULT_REGISTRY` or any registry
    :func:`athenaeum.dimensions.build_registry` produces, since kernel
    dimensions are not deletable) yields no candidates at all rather than
    raising -- there is nothing this instrument could safely evaluate
    without those two axes.
    """
    scope_dim = registry.get("scope")
    class_dim = registry.get("memory-class")
    if scope_dim is None or class_dim is None:
        return []

    min_similarity = resolve_sibling_widening_min_similarity(config)
    allowed_classes = resolve_sibling_widening_classes(config)

    out: list[WideningCandidate] = []
    for page_a, page_b, similarity in pairs:
        if similarity < min_similarity:
            continue
        if compare_dimension(scope_dim, page_a.meta, page_b.meta) != Relation.DISJOINT:
            continue
        class_a = coordinate_value(class_dim, page_a.meta)
        class_b = coordinate_value(class_dim, page_b.meta)
        if class_a not in allowed_classes or class_b not in allowed_classes:
            continue
        out.append(
            WideningCandidate(
                page_a_id=page_a.id,
                page_b_id=page_b.id,
                scope_a=coordinate_value(scope_dim, page_a.meta),
                scope_b=coordinate_value(scope_dim, page_b.meta),
            )
        )
    return out


def run_sibling_widening(
    pairs: Sequence[tuple[ComparatorPage, ComparatorPage, float]],
    *,
    wiki_root: Path,
    client: "LLMBackend | None",
    config: dict[str, Any] | None = None,
    usage: "TokenUsage | None" = None,
    registry: DimensionRegistry = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """Spend at most the configured budget probing sibling-scope candidates (issue athenaeum#715).

    For each :func:`sibling_widening_candidates` result, in order:

    1. **Memoization first, free.** If :func:`athenaeum.verdicts.get_verdict_status`
       already reports a FRESH decided verdict for the pair, skip it --
       never spend budget re-deciding a pair the ledger already covers
       (mirrors :func:`athenaeum.comparator.record_comparison`'s own
       memoization gate).
    2. **Budget enforced BEFORE the call, never after.** Once ``spent`` has
       reached :func:`athenaeum.config.resolve_sibling_widening_budget`, every
       remaining (non-memoized) candidate is counted in
       ``skipped_over_budget`` and NOT dispatched -- no silent truncation:
       every candidate this function declines to probe is accounted for in
       the returned total.
    3. **One memoized Gate-2 call.** :func:`athenaeum.comparator.content_relation`
       -- the SAME function Gate 2 itself calls, so this instrument spends
       from the identical LLM budget/knob surface, not a parallel one. Only
       :attr:`athenaeum.comparator.ContentRelation.EQUIVALENT` emits a
       :class:`WideningProposal`; ``compatible``/``conflicting``/``unavailable``
       emit nothing for that pair.

    This function never writes to the verdict ledger and never acquires a
    :class:`~athenaeum.runlock.RunLock` -- it only READS
    ``get_verdict_status`` for memoization. Proposals are returned for a
    caller to route to the review queue; nothing here rewrites a page's
    ``claimed_scope`` or appends a verdict for the candidate pair (issue
    athenaeum#715: "a proposal... never an automatic coordinate rewrite").

    Returns ``{"budget": int, "spent": int, "skipped_over_budget": int,
    "proposals": list[WideningProposal]}``.
    """
    budget = resolve_sibling_widening_budget(config)
    candidates = sibling_widening_candidates(pairs, config=config, registry=registry)
    page_lookup = {(page_a.id, page_b.id): (page_a, page_b) for page_a, page_b, _sim in pairs}

    spent = 0
    skipped_over_budget = 0
    proposals: list[WideningProposal] = []

    for candidate in candidates:
        pair_key = make_pair_key(candidate.page_a_id, candidate.page_b_id)
        status = get_verdict_status(wiki_root, pair_key)
        if status["decided"] and status["fresh"]:
            continue

        if spent >= budget:
            skipped_over_budget += 1
            continue

        page_a, page_b = page_lookup[(candidate.page_a_id, candidate.page_b_id)]
        result = content_relation(page_a, page_b, client, config=config, usage=usage)
        spent += 1

        if result.relation == ContentRelation.EQUIVALENT:
            proposals.append(
                WideningProposal(
                    page_a_id=candidate.page_a_id,
                    page_b_id=candidate.page_b_id,
                    scopes=[candidate.scope_a, candidate.scope_b],
                    rationale=result.rationale,
                )
            )

    return {
        "budget": budget,
        "spent": spent,
        "skipped_over_budget": skipped_over_budget,
        "proposals": proposals,
    }


__all__ = [
    "CONTENT_WRITE_COUNTER_NAME",
    "TTL_STALE_REASON",
    "WideningCandidate",
    "WideningProposal",
    "count_content_writes",
    "record_content_writes",
    "run_sibling_widening",
    "run_ttl_recheck",
    "select_compatible_ttl_expired",
    "sibling_widening_candidates",
]
