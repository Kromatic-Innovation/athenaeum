# SPDX-License-Identifier: Apache-2.0
"""Scope-aware claim resolution — read side of the ``scope`` dimension (issue athenaeum#715).

:func:`athenaeum.verdict_effects.write_refines_declaration` is the WRITE side
of the ``specialization`` verdict: it appends a general claim's identifier to
a specific claim's frontmatter ``refines:`` list. That write has had no
matching read since it shipped — this module is that read. It answers one
question: **given a set of candidate claims and a query scope, which claims
apply, and which are the most specific?**

This module owns:

- The in-scope filter — a claim applies to a query scope iff the claim's
  ``scope`` coordinate CONTAINS the query's (:func:`athenaeum.dimensions.hierarchy_contains`).
- Most-specific selection among the in-scope claims, under the
  ``scope``-dimension containment order.
- Consuming ``refines:`` edges as an ADDITIONAL, explicit specificity
  relation on top of (and independent of) the ``claimed_scope`` coordinate.

It deliberately does NOT own:

- Computing or comparing the ``scope`` coordinate itself — that is
  :mod:`athenaeum.dimensions` (:data:`~athenaeum.dimensions.SCOPE`,
  :func:`~athenaeum.dimensions.hierarchy_contains`). This module only calls
  in.
- Deciding a verdict between two claims (``duplicate`` / ``contradiction`` /
  ``specialization`` / ``distinct`` / ``underdetermined``) — that is
  :mod:`athenaeum.comparator`. This module runs strictly AFTER a verdict has
  already been decided and (for ``specialization``) already written as a
  ``refines:`` edge; it never itself decides which claim is "more true".
- Writing ``refines:`` — that stays :func:`athenaeum.verdict_effects.write_refines_declaration`'s
  job alone (issue athenaeum#658 D3: ``refines:`` has exactly one writer, and this
  module is a reader).
- Wiring into ``recall`` — :mod:`athenaeum.mcp_server` calls in, gated by
  :func:`athenaeum.config.resolve_scope_aware_recall_enabled`; this module has
  no knowledge of MCP, config resolution, or the recall hit-rendering shape.
- Ranking by relevance, similarity, or recency. The only order this module
  imposes is the containment PARTIAL order plus explicit ``refines:`` edges;
  see :func:`resolve_most_specific` for why ties are never broken.

**No confidence, no similarity, no LLM call.** Pure, deterministic,
synchronous set/graph logic over already-parsed coordinates — no network, no
config reads, no I/O of any kind inside :func:`resolve_most_specific` itself.
This mirrors the same issue's standing rule that authority is a partial
order, not a chain, and that no confidence threshold gates a verdict
anywhere in the memory-model v6 chain.

Layering: L3. Imports only :mod:`athenaeum.dimensions` (L2, for
:func:`~athenaeum.dimensions.hierarchy_contains`) plus stdlib — no config, no
LLM client, no I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from athenaeum.dimensions import hierarchy_contains

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScopedClaim:
    """One candidate claim: an identifier, its ``scope`` coordinate, and the
    general-claim ids it declares ``refines:``.

    ``claimed_scope`` is the raw frontmatter ``claimed_scope`` value (or
    ``None`` for an unscoped/universal claim) — the same coordinate
    :func:`athenaeum.dimensions.coordinate_value` reads for the ``scope``
    kernel dimension. ``refines`` is the claim's own frontmatter ``refines:``
    list (general-claim ids this claim declares itself more specific than) —
    the exact field :func:`athenaeum.verdict_effects.write_refines_declaration`
    writes. Both default such that a claim built from a page with neither
    field set is trivially "unscoped, refines nothing".
    """

    id: str
    claimed_scope: str | None = None
    refines: tuple[str, ...] = ()


def resolve_most_specific(
    claims: Sequence[ScopedClaim],
    query_scope: str | None,
    *,
    refines: Mapping[str, Sequence[str]] | None = None,
) -> list[ScopedClaim]:
    """Return the most-specific in-scope claims for *query_scope*.

    Semantics, in order:

    1. **In-scope filter.** Claim ``C`` applies to query scope ``Q`` iff
       ``hierarchy_contains(C.claimed_scope, Q)`` — C's scope CONTAINS the
       query scope. An unscoped claim (``claimed_scope is None``) is
       universal and always applies. A claim scoped strictly BELOW the query
       (e.g. ``kromatic/platform`` for query ``kromatic``) does NOT apply.
    2. **Most-specific selection.** Among the in-scope claims, return every
       ``C`` for which no OTHER in-scope ``C'`` is strictly more specific
       (``hierarchy_contains(C.claimed_scope, C'.claimed_scope)`` and not the
       reverse) — the MINIMAL elements of the containment partial order.
    3. **``refines:`` edges are an additional, explicit specificity
       relation.** If in-scope claim ``C'`` declares ``refines: [C]`` (i.e.
       ``C.id`` appears in ``C'``'s refines edges — see the *refines*
       parameter below) and ``C`` is also in scope, ``C'`` is strictly more
       specific than ``C`` REGARDLESS of what their ``claimed_scope``
       coordinates say — even equal, even incomparable. This is the read
       side consuming what the ``specialization`` verdict's write side
       (:func:`athenaeum.verdict_effects.write_refines_declaration`) already
       recorded. A ``refines:`` entry naming a claim that is absent from
       *claims* or not in scope is ignored — never an error, never a
       spurious suppression.
    4. **Cycle-safe.** A ``refines:`` cycle (direct or transitive, e.g. ``A
       refines B refines A``) never infinite-loops and never suppresses any
       cycle member via that cycle's edges (containment-based suppression is
       unaffected). Detected via strongly-connected-component partitioning
       of the in-scope refines graph; logged at debug when found.
    5. **The result is a LIST; incomparable minima are ALL returned.** The
       containment order is a PARTIAL order — this function never invents a
       total order, never tie-breaks on recency/similarity/any scalar. The
       caller's input order is preserved among the returned minima.

    Args:
        claims: Candidate claims, in the caller's preferred output order.
        query_scope: The query's ``scope`` coordinate. ``None`` is the
            degenerate "unscoped query" case, NOT a wildcard: per
            :func:`~athenaeum.dimensions.hierarchy_contains`, a claim
            contains a ``None`` query only when that claim's own
            ``claimed_scope`` is also ``None`` (universal). Pass the actual
            query scope for the ordinary "narrow results to this scope" use.
        refines: Optional override for each claim's refines edges, keyed by
            claim id -> sequence of general-claim ids it refines. When
            given, this is the SOLE source of refines edges (a claim's own
            ``.refines`` attribute is ignored) — useful when the caller
            already has the edges in a separate lookup (e.g. freshly parsed
            frontmatter) and does not want to rebuild :class:`ScopedClaim`
            instances just to attach them. When omitted (the default), each
            claim's own ``.refines`` tuple is used.

    Returns:
        The most-specific in-scope claims, a subsequence of *claims*
        preserving its order.
    """
    in_scope = [c for c in claims if hierarchy_contains(c.claimed_scope, query_scope)]
    if not in_scope:
        return []

    in_scope_ids = {c.id for c in in_scope}

    def _edges(claim: ScopedClaim) -> Sequence[str]:
        if refines is not None:
            return refines.get(claim.id, ())
        return claim.refines

    # Build the in-scope refines graph: specific.id -> general.id, restricted
    # to edges whose target is itself in scope (an edge naming an absent or
    # out-of-scope claim contributes nothing — see docstring point 3).
    refines_graph: dict[str, set[str]] = {c.id: set() for c in in_scope}
    for claim in in_scope:
        for general_id in _edges(claim):
            if general_id == claim.id:
                continue  # a claim refining itself is not a specificity edge
            if general_id in in_scope_ids:
                refines_graph[claim.id].add(general_id)

    cycle_components = _cycle_components(refines_graph)

    def _refines_dominates(specific: ScopedClaim, general: ScopedClaim) -> bool:
        """True when *specific* strictly dominates *general* via a
        non-cyclic ``refines:`` edge."""
        if general.id not in refines_graph.get(specific.id, ()):
            return False
        specific_component = cycle_components.get(specific.id)
        if (
            specific_component is not None
            and specific_component == cycle_components.get(general.id)
        ):
            # Both sides sit in the SAME cycle -- ignore this edge (point 4:
            # suppress nothing among cycle members via the cycle's own
            # edges). An edge BRIDGING two disjoint cycles is ordinary and
            # still establishes specificity.
            return False
        return True

    def _containment_dominates(specific: ScopedClaim, general: ScopedClaim) -> bool:
        """True when *specific*'s scope is strictly more specific than
        *general*'s (contained-by, not equal, not incomparable)."""
        return hierarchy_contains(
            general.claimed_scope, specific.claimed_scope
        ) and not hierarchy_contains(specific.claimed_scope, general.claimed_scope)

    suppressed: set[str] = set()
    for general in in_scope:
        for specific in in_scope:
            if specific.id == general.id:
                continue
            if _containment_dominates(specific, general) or _refines_dominates(
                specific, general
            ):
                suppressed.add(general.id)
                break

    if cycle_components:
        log.debug(
            "scope_resolution: refines cycle detected among claim ids %s; "
            "suppressing nothing among them via those edges",
            sorted(cycle_components),
        )

    return [c for c in claims if c.id in in_scope_ids and c.id not in suppressed]


def _cycle_components(graph: Mapping[str, set[str]]) -> dict[str, int]:
    """Map every node that sits on a cycle in *graph* to its component id.

    *graph* is a directed graph of claim id -> the general-claim ids it
    refines. A node is "on a cycle" when it can reach itself by following one
    or more edges — covering both a direct 2-cycle (``A refines B`` and ``B
    refines A``) and a longer transitive one (``A -> B -> C -> A``). Two nodes
    share a component id exactly when each can reach the other, i.e. they are
    in the same strongly connected component.

    Returning COMPONENTS rather than a flat "is on some cycle" set is
    load-bearing, not tidiness. Two disjoint cycles can be joined by a
    perfectly ordinary ``refines:`` edge; with a flat set both endpoints of
    that bridging edge test as "cyclic" and the edge is discarded, silently
    keeping a general claim that a specific one legitimately refines. Point 4
    only ever meant "an edge INTERNAL to a cycle establishes no specificity" —
    a bridge between two cycles is not such an edge.

    Implemented as plain iterative reachability per node (the graphs this
    function sees are the in-scope refines edges for one resolve call — small
    by construction, so Tarjan's linear-time SCC algorithm would be premature
    machinery here; this is O(V*(V+E)), fine at that scale) rather than
    recursive DFS, so an adversarially long cycle cannot blow the call stack.
    """
    reachable: dict[str, set[str]] = {}
    for start in graph:
        seen: set[str] = set()
        stack = list(graph.get(start, ()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(graph.get(node, ()))
        reachable[start] = seen

    components: dict[str, int] = {}
    next_id = 0
    for node in graph:
        if node in components:
            continue
        # Reaching itself is what "on a cycle" means; a node that cannot is
        # in no component at all and its edges are always ordinary.
        if node not in reachable.get(node, ()):
            continue
        members = {
            other
            for other in graph
            if other in reachable.get(node, ()) and node in reachable.get(other, ())
        }
        members.add(node)
        for member in members:
            components[member] = next_id
        next_id += 1
    return components


__all__ = [
    "ScopedClaim",
    "resolve_most_specific",
]
