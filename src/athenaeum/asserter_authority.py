# SPDX-License-Identifier: Apache-2.0
"""Asserter authority as a PARTIAL ORDER over declared grants (issue athenaeum#715).

Auto-supersession's asserter precondition offers three routes to a winner:
``(a)`` the same asserter revising its own claim, ``(b)`` strictly greater
authority, ``(c)`` equal authority with independent corroboration.
:mod:`athenaeum.models` already answers ``(a)`` -- :func:`~athenaeum.models.compare_asserters`
returns ``same``/``different``/``unknown`` over the OIDC-durable identity key.
This module answers ``(b)`` and ``(c)``, which had no implementation anywhere
in the repo before athenaeum#715.

**Authority is a partial order, not a chain.** athenaeum#715 states this as a
hard constraint -- "no branch may assume a total order" -- and requires that
INCOMPARABLE grants be treated as EQUAL authority, so an incomparable-peer
conflict takes the corroboration-or-queue path rather than silently picking a
winner. :func:`compare_authority` therefore has FOUR outcomes, not three, and
:func:`treated_as_equal_authority` is the helper every caller should use in
place of ``== AUTHORITY_EQUAL``.

The order itself is set inclusion over each asserter's grant closure:

- ``A`` is strictly greater than ``B`` when ``closure(B)`` is a proper subset
  of ``closure(A)`` -- A can do everything B can, and more.
- They are equal when the closures are equal.
- They are INCOMPARABLE when neither closure contains the other. Two asserters
  holding disjoint grants (``billing`` vs ``deploy``) are peers, not ranked.

:func:`~athenaeum.config.resolve_authority_grant_implications` supplies the
implication graph that makes the order non-trivial (``admin`` implies
``editor`` implies ``reader``); with no graph configured the order degenerates
to inclusion over literally-declared grants, which is still a correct partial
order.

**Undeclared authority is INCOMPARABLE, never lesser.** An asserter that
declares no grants at all has an empty closure, and the empty set is a subset
of everything -- so plain inclusion would rank *every* granted asserter above
*every* Claude-session intake, which carries no grants. That is precisely
backwards for a destructive action: "we know nothing about this writer" must
never become "this writer loses". :func:`compare_authority` special-cases it
(see that function's table), so an ungranted side is at worst equal and never
automatically defeated.

Layering: L0. Pure functions over already-parsed frontmatter dicts. Imports
:mod:`athenaeum.models` for the asserter block shape and nothing else at
module scope; the config resolver is imported lazily inside
:func:`compare_authority` so this module stays importable from anywhere in the
dependency graph.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from athenaeum.models import parse_asserter

__all__ = [
    "AUTHORITY_EQUAL",
    "AUTHORITY_GREATER",
    "AUTHORITY_INCOMPARABLE",
    "AUTHORITY_LESS",
    "AUTHORITY_RELATIONS",
    "GRANTS_KEY",
    "compare_authority",
    "declared_grants",
    "grant_closure",
    "strictly_greater_authority",
    "treated_as_equal_authority",
]

#: The frontmatter key, inside the ``asserter:`` block, holding the grant list.
#: :func:`athenaeum.models.parse_asserter` passes unknown keys through
#: untouched, so this needs no parser change.
GRANTS_KEY = "grants"

AUTHORITY_GREATER = "greater"
AUTHORITY_LESS = "less"
AUTHORITY_EQUAL = "equal"
AUTHORITY_INCOMPARABLE = "incomparable"

#: Every value :func:`compare_authority` can return. Four, not three --
#: see the module docstring.
AUTHORITY_RELATIONS: frozenset[str] = frozenset(
    {AUTHORITY_GREATER, AUTHORITY_LESS, AUTHORITY_EQUAL, AUTHORITY_INCOMPARABLE}
)


def declared_grants(meta: dict[str, Any] | None) -> frozenset[str]:
    """Return the grants declared in *meta*'s ``asserter:`` block.

    Accepts a page's whole frontmatter dict (not the asserter sub-dict) so
    callers can pass a :class:`~athenaeum.comparator.ComparatorPage`'s
    ``meta`` straight through. Fail-open in the same style as every other
    frontmatter reader here: a missing block, a non-list ``grants``, or
    non-string members all yield an empty set rather than raising. Blank and
    duplicate entries are dropped.
    """
    asserter = parse_asserter(meta)
    raw = asserter.get(GRANTS_KEY)
    if isinstance(raw, str):
        # A single grant written as a bare scalar rather than a one-item list.
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(member.strip() for member in raw if isinstance(member, str) and member.strip())


def grant_closure(
    grants: frozenset[str] | set[str] | list[str] | tuple[str, ...],
    implications: dict[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    """Expand *grants* under the *implications* graph, transitively.

    Cycle-safe by construction: this is a breadth-first walk with a visited
    set, so a malformed configuration declaring ``a -> b`` and ``b -> a``
    terminates with ``{a, b}`` instead of hanging a nightly run. A grant with
    no entry in the graph expands to itself.
    """
    implications = implications or {}
    seen: set[str] = set()
    queue: deque[str] = deque(
        member for member in grants if isinstance(member, str) and member.strip()
    )
    while queue:
        grant = queue.popleft().strip()
        if not grant or grant in seen:
            continue
        seen.add(grant)
        queue.extend(implications.get(grant, frozenset()))
    return frozenset(seen)


def compare_authority(
    meta_a: dict[str, Any] | None,
    meta_b: dict[str, Any] | None,
    *,
    config: dict[str, Any] | None = None,
) -> str:
    """Compare two pages' asserter authority. Returns one of :data:`AUTHORITY_RELATIONS`.

    The table, in evaluation order:

    ===========================  ===========================  ==================
    ``meta_a`` grants            ``meta_b`` grants            result
    ===========================  ===========================  ==================
    none declared                none declared                ``equal``
    none declared                any                          ``incomparable``
    any                          none declared                ``incomparable``
    closure equal                closure equal                ``equal``
    closure ⊃ B's                --                           ``greater``
    closure ⊂ B's                --                           ``less``
    neither contains the other   --                           ``incomparable``
    ===========================  ===========================  ==================

    Rows 2 and 3 are the "undeclared authority is incomparable, never lesser"
    rule from the module docstring: without them, plain set inclusion would
    make every ungranted asserter lose to every granted one.

    ``greater`` here means *strictly* greater, which is what athenaeum#715's
    condition (b) requires; :func:`strictly_greater_authority` is the boolean
    form. Callers deciding whether to take the corroboration-or-queue path
    must use :func:`treated_as_equal_authority`, not an equality test against
    :data:`AUTHORITY_EQUAL`.
    """
    from athenaeum.config import resolve_authority_grant_implications

    implications = resolve_authority_grant_implications(config)
    raw_a = declared_grants(meta_a)
    raw_b = declared_grants(meta_b)

    if not raw_a and not raw_b:
        return AUTHORITY_EQUAL
    if not raw_a or not raw_b:
        return AUTHORITY_INCOMPARABLE

    closure_a = grant_closure(raw_a, implications)
    closure_b = grant_closure(raw_b, implications)
    if closure_a == closure_b:
        return AUTHORITY_EQUAL
    if closure_b < closure_a:
        return AUTHORITY_GREATER
    if closure_a < closure_b:
        return AUTHORITY_LESS
    return AUTHORITY_INCOMPARABLE


def strictly_greater_authority(
    meta_a: dict[str, Any] | None,
    meta_b: dict[str, Any] | None,
    *,
    config: dict[str, Any] | None = None,
) -> bool:
    """``True`` when *meta_a*'s asserter is STRICTLY greater than *meta_b*'s.

    athenaeum#715's auto-supersession condition (b). Deliberately ``False``
    for ``incomparable``: an incomparable peer is not a superior.
    """
    return compare_authority(meta_a, meta_b, config=config) == AUTHORITY_GREATER


def treated_as_equal_authority(relation: str) -> bool:
    """``True`` when *relation* must take athenaeum#715's condition (c) path.

    "Incomparable grants are treated as equal authority -- authority is a
    partial order, not a chain -- so incomparable-peer conflicts take the
    corroboration-or-queue path." This helper is that sentence, so no caller
    has to remember to write ``in (EQUAL, INCOMPARABLE)`` and no caller can
    accidentally treat an incomparable pair as rankable.
    """
    return relation in (AUTHORITY_EQUAL, AUTHORITY_INCOMPARABLE)
