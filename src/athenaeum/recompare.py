# SPDX-License-Identifier: Apache-2.0
"""Re-run the five-verdict comparator over the pending merge queue (athenaeum#715).

The last acceptance criterion of athenaeum#715's comparator work: "Once the
comparator is live, re-run it over the existing pending merge proposals and
record a verdict per proposal in the ledger. Until this re-run happens, those
proposals stay pending: do not approve, reject, or archive any of them by
hand."

That sentence is the whole design brief, and it has three sharp edges.

**1. This module NEVER applies a merge. There is no apply path to reach.**
``apply=True`` means exactly one thing: verdicts are WRITTEN TO THE LEDGER
(:mod:`athenaeum.verdicts`) instead of only being printed. It does not
approve, reject, archive, or otherwise mutate ``_pending_merges.md`` — this
module never opens that file for writing at all. :func:`can_auto_apply` is
the executable form of that guarantee and returns ``False`` unconditionally;
:func:`recompare_pending_merges` has no branch that consults it hoping for a
``True``. The queue drains by a human reading verdicts, not by this command.

**2. The PII-hazard proposals can never be approved by this re-run.** athenaeum#715
requires that the proposals flagged as PII hazards "must never be approved —
not by this re-run, not by auto-apply, not by agent triage ... If the re-run's
verdict for either is ``duplicate``, it still routes to a human."
:func:`identify_pii_hazards` finds them BEFORE any comparison runs, using the
signals that already exist in this repo rather than a new classifier:
:func:`athenaeum.pii.is_pii_flagged` on each source's frontmatter, plus
inline-contact detection on the body. Every hazardous proposal is routed
:data:`ROUTE_HUMAN` regardless of its verdict, and is never ledgered — which
is belt and braces, because :func:`athenaeum.comparator.record_comparison`
independently refuses an erasure-class pair on the same signal.

**3. Similarity does not decide anything here.** The stored
``**Confidence**:`` line on every existing block is read only to be REPORTED
alongside the new verdict, so an operator can see what the old path thought
next to what the comparator decided. It is never an input to a verdict, never
a threshold, and never a route. athenaeum#715: "Similarity is never a verdict
input and never a confidence."

**Aggregation.** A pending proposal is a CLUSTER (2..N sources); the
comparator is PAIRWISE. So each proposal decomposes into its source pairs,
every pair gets its own ledgered verdict, and the proposal's reported
aggregate is derived deterministically by :func:`aggregate_verdict` — with a
MIXED cluster (some pairs duplicate, others not) reported as
``underdetermined`` rather than forced into a single answer, because a cluster
that is only partly duplicative is precisely the over-clustering failure
athenaeum#658 D1 describes and it needs a human, not a verdict.

Layering: L4. Reads the pending-merge sidecar and the corpus, calls the
comparator, writes only to the verdict ledger. The CLI wrapper lives in
:mod:`athenaeum._cmd_merges` (``athenaeum merges recompare``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.comparator import (
    VERDICT_CONTRADICTION,
    VERDICT_DISTINCT,
    VERDICT_DUPLICATE,
    VERDICT_SPECIALIZATION,
    VERDICT_UNDERDETERMINED,
    ComparatorPage,
    page_from_path,
    record_comparison,
)
from athenaeum.dimensions import DEFAULT_REGISTRY, DimensionRegistry
from athenaeum.models import TokenUsage, parse_frontmatter
from athenaeum.pending_merges import PendingMerge, parse_pending_merges
from athenaeum.pii import find_inline_emails, find_inline_phones, is_pii_flagged

if TYPE_CHECKING:  # pragma: no cover - typing only
    from athenaeum.provider import LLMBackend
    from athenaeum.runlock import RunLock

log = logging.getLogger(__name__)

__all__ = [
    "MAX_PAIRS_PER_PROPOSAL",
    "ROUTE_HUMAN",
    "ROUTE_LEDGER",
    "ProposalRecompare",
    "RecompareResult",
    "aggregate_verdict",
    "can_auto_apply",
    "identify_pii_hazards",
    "recompare_pending_merges",
    "resolve_source_path",
]

#: Route for anything a human must see before it moves. Every PII-hazard
#: proposal takes this route no matter what verdict the comparator reached.
ROUTE_HUMAN = "human"

#: Route for an ordinary proposal whose pair verdicts were ledgered.
ROUTE_LEDGER = "ledger"

#: Hard cap on pairwise comparisons spent on any ONE proposal. A degenerate
#: over-cluster (athenaeum#658 D1 found one with ~1,900 sources) would otherwise
#: be O(n^2) LLM calls on its own. Anything past the cap is COUNTED and
#: reported in ``notes`` -- never silently dropped.
MAX_PAIRS_PER_PROPOSAL = 45


@dataclass(frozen=True)
class ProposalRecompare:
    """One pending proposal's re-run outcome."""

    proposal_id: str
    merge_target_name: str
    sources: list[str]
    pii_hazard: bool
    pii_hazard_reasons: list[str]
    pair_verdicts: dict[str, str | None]
    aggregate: str | None
    route: str
    stored_confidence: float
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RecompareResult:
    """The whole re-run."""

    applied: bool
    total: int
    compared: int
    skipped_fresh: int
    skipped_missing_source: int
    pii_hazard_ids: list[str]
    proposals: list[ProposalRecompare] = field(default_factory=list)


def can_auto_apply(proposal: ProposalRecompare) -> bool:
    """Always ``False``. This is an assertion, not a policy hook.

    athenaeum#715 requires that the PII-hazard proposals "cannot reach an
    auto-apply path", and the safest way to guarantee that for them is for
    there to be no auto-apply path for ANY proposal in this module. This
    function exists so that guarantee is executable and testable rather than
    merely documented: a future edit that introduces an apply path has to
    change this function, and the test that pins it to ``False`` for a
    ``duplicate``-verdict PII-hazard proposal will fail loudly.
    """
    return False


def resolve_source_path(source: str, wiki_root: Path) -> Path | None:
    """Resolve one ``**Sources**:`` entry to a readable path under *wiki_root*.

    Existing blocks store ABSOLUTE paths from the machine that wrote them
    (``/Users/<someone>/knowledge/wiki/<file>.md``), which do not resolve on
    any other machine or against a scratch store. Resolution order: the path
    as given if it exists, else its basename under *wiki_root*. Returns
    ``None`` when neither exists -- the caller counts that rather than
    guessing at a rename.
    """
    candidate = Path(source)
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    basename = wiki_root / candidate.name
    if basename.is_file():
        return basename
    relative = wiki_root / source
    if relative.is_file():
        return relative
    return None


def identify_pii_hazards(paths: list[Path]) -> list[str]:
    """Return human-readable reasons this proposal is a PII hazard, or ``[]``.

    Run BEFORE any comparison, per athenaeum#715 ("Identify them before the
    re-run"). Uses only signals that already exist in this repo:

    - :func:`athenaeum.pii.is_pii_flagged` -- the ``pii:`` frontmatter flag
      every corpus consumer already honours, including the verdict ledger's
      own erasure-class refusal.
    - :func:`athenaeum.pii.find_inline_emails` /
      :func:`athenaeum.pii.find_inline_phones` -- inline contact data in the
      body, which is the shape that makes a fold irreversible in a way a
      later erasure request cannot undo.

    A reason names the file, so the operator can go and look.
    """
    reasons: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - unreadable source
            reasons.append(f"{path.name}: unreadable ({exc})")
            continue
        meta, body = parse_frontmatter(text)
        if is_pii_flagged(meta):
            reasons.append(f"{path.name}: pii: frontmatter flag")
        emails = find_inline_emails(body)
        if emails:
            reasons.append(f"{path.name}: {len(emails)} inline email address(es)")
        phones = find_inline_phones(body)
        if phones:
            reasons.append(f"{path.name}: {len(phones)} inline phone number(s)")
    return reasons


def aggregate_verdict(pair_verdicts: dict[str, str | None]) -> str | None:
    """Reduce a cluster's pair verdicts to ONE reported aggregate, deterministically.

    Precedence, most-serious first, and each rule stated as the reason it
    exists:

    1. Any ``contradiction`` -> ``contradiction``. One conflicting pair makes
       the whole cluster unmergeable; nothing later can outvote it.
    2. Any ``underdetermined`` -> ``underdetermined``. A missing coordinate
       somewhere in the cluster means the cluster is not decided.
    3. All decided pairs ``duplicate`` -> ``duplicate``. The only route to a
       fold proposal.
    4. Any ``specialization`` -> ``specialization``. A general/specific
       relation inside the cluster is a ``refines:`` relationship, not a fold.
    5. All decided pairs ``distinct`` -> ``distinct``.
    6. No pair decided at all -> ``None`` (Gate 2 was unavailable throughout).
    7. Anything else -- a MIXED cluster, some pairs duplicate and some not --
       -> ``underdetermined``. This is deliberately not forced into a single
       answer: a partly-duplicative cluster is exactly athenaeum#658 D1's
       over-clustering failure, and it needs a human to split it, not a
       verdict that flattens it.
    """
    decided = [v for v in pair_verdicts.values() if v is not None]
    if not decided:
        return None
    unique = set(decided)
    if VERDICT_CONTRADICTION in unique:
        return VERDICT_CONTRADICTION
    if VERDICT_UNDERDETERMINED in unique:
        return VERDICT_UNDERDETERMINED
    if unique == {VERDICT_DUPLICATE}:
        return VERDICT_DUPLICATE
    if VERDICT_SPECIALIZATION in unique:
        return VERDICT_SPECIALIZATION
    if unique == {VERDICT_DISTINCT}:
        return VERDICT_DISTINCT
    return VERDICT_UNDERDETERMINED


def _pair_key(page_a: ComparatorPage, page_b: ComparatorPage) -> str:
    return "|".join(sorted((page_a.id, page_b.id)))


def recompare_pending_merges(
    wiki_root: Path,
    *,
    merges_path: Path | None = None,
    config: dict[str, Any] | None = None,
    client: "LLMBackend | None" = None,
    registry: DimensionRegistry = DEFAULT_REGISTRY,
    usage: TokenUsage | None = None,
    apply: bool = False,
    limit: int | None = None,
    lock: "RunLock | None" = None,
    now: datetime | None = None,
) -> RecompareResult:
    """Re-run the comparator over every UNRESOLVED pending merge proposal.

    *apply* controls one thing and one thing only: whether pair verdicts are
    appended to the verdict ledger. Dry-run (the default, mirroring
    ``athenaeum merges revalidate``'s shape) still performs the comparisons
    and reports what it would record. ``_pending_merges.md`` is never written
    by either mode -- see the module docstring.

    *lock* is required when *apply* is true (every
    :mod:`athenaeum.verdicts` mutator demands an already-acquired
    :class:`~athenaeum.runlock.RunLock`); passing ``apply=True`` without one
    raises :class:`ValueError` rather than half-running.
    """
    if apply and lock is None:
        raise ValueError(
            "recompare_pending_merges(apply=True) requires an acquired RunLock -- "
            "every athenaeum.verdicts mutator is single-appender by contract"
        )

    merges_path = merges_path or (wiki_root / "_pending_merges.md")
    all_proposals: list[PendingMerge] = parse_pending_merges(merges_path)
    unresolved = [p for p in all_proposals if not p.resolved]
    if limit is not None and limit > 0:
        unresolved = unresolved[:limit]

    results: list[ProposalRecompare] = []
    compared = 0
    skipped_fresh = 0
    skipped_missing = 0

    for proposal in unresolved:
        # Issue athenaeum#1230: an --apply recompare over a large backlog is a
        # per-PAIR LLM classify loop (up to MAX_PAIRS_PER_PROPOSAL pairs per
        # proposal, no cap on the number of proposals) with the SAME shape as
        # the ingest-path gap this issue fixes elsewhere — the run lock this
        # function was already handed (for the verdict-ledger single-appender
        # contract) is never refreshed, so its heartbeat age grows with total
        # wall time. Tick it once per proposal (not per pair — cheap enough
        # that no separate interval/throttle is worth the complexity here).
        if apply and lock is not None:
            lock.heartbeat()
        notes: list[str] = []
        paths: list[Path] = []
        for source in proposal.sources:
            resolved = resolve_source_path(source, wiki_root)
            if resolved is None:
                skipped_missing += 1
                notes.append(f"source not found: {source}")
                continue
            paths.append(resolved)

        hazard_reasons = identify_pii_hazards(paths)
        is_hazard = bool(hazard_reasons)

        pair_verdicts: dict[str, str | None] = {}
        if len(paths) < 2:
            notes.append("fewer than two readable sources -- nothing to compare")
        elif is_hazard:
            # athenaeum#715: never approved, never auto-applied, and not
            # ledgered either -- record_comparison would refuse the pair on
            # the same signal, so spending an LLM call to be refused is pure
            # cost. The proposal is reported and routed to a human.
            notes.append("PII hazard -- not compared, routed to a human")
        else:
            pages = [page_from_path(path) for path in paths]
            pairs = list(combinations(pages, 2))
            if len(pairs) > MAX_PAIRS_PER_PROPOSAL:
                notes.append(
                    f"{len(pairs) - MAX_PAIRS_PER_PROPOSAL} of {len(pairs)} pairs "
                    f"skipped over the {MAX_PAIRS_PER_PROPOSAL}-pair cap"
                )
                pairs = pairs[:MAX_PAIRS_PER_PROPOSAL]
            for page_a, page_b in pairs:
                key = _pair_key(page_a, page_b)
                if apply and lock is not None:
                    outcome = record_comparison(
                        wiki_root,
                        page_a,
                        page_b,
                        client=client,
                        config=config,
                        usage=usage,
                        lock=lock,
                        registry=registry,
                    )
                    if outcome.get("skipped") == "fresh":
                        skipped_fresh += 1
                    else:
                        compared += 1
                    pair_verdicts[key] = outcome.get("verdict")
                else:
                    from athenaeum.comparator import compare_pages

                    dry = compare_pages(
                        page_a,
                        page_b,
                        client=client,
                        config=config,
                        usage=usage,
                        registry=registry,
                    )
                    compared += 1
                    pair_verdicts[key] = dry.verdict

        aggregate = aggregate_verdict(pair_verdicts)
        # athenaeum#715: "If the re-run's verdict for either is `duplicate`, it
        # still routes to a human." That is unconditional here rather than a
        # branch on the verdict: a hazardous proposal is never compared at
        # all, so its aggregate is always None and there is no duplicate
        # verdict to special-case. Routing is decided by the hazard alone.
        route = ROUTE_HUMAN if is_hazard else ROUTE_LEDGER
        results.append(
            ProposalRecompare(
                proposal_id=proposal.id,
                merge_target_name=proposal.merge_target_name,
                sources=list(proposal.sources),
                pii_hazard=is_hazard,
                pii_hazard_reasons=hazard_reasons,
                pair_verdicts=pair_verdicts,
                aggregate=aggregate,
                route=route,
                stored_confidence=proposal.confidence,
                notes=notes,
            )
        )

    return RecompareResult(
        applied=apply,
        total=len(unresolved),
        compared=compared,
        skipped_fresh=skipped_fresh,
        skipped_missing_source=skipped_missing,
        pii_hazard_ids=[r.proposal_id for r in results if r.pii_hazard],
        proposals=results,
    )
