# SPDX-License-Identifier: Apache-2.0
"""Declared-relationship primitive (athenaeum#1682, port of C4 §3.8) — L1/L2 primitive.

Extracted out of :mod:`athenaeum.merge`'s ``_declared_relationship``
(issue athenaeum#167 / athenaeum#715), which pairwise-compared two
``AutoMemoryFile``s' own declared ``refines``/``supersedes``/
``merge_rejected_with`` frontmatter to short-circuit a pair that has
already been adjudicated by a human (or by ``pending_merges.resolve_merge``
on the reviewer's behalf) — no LLM call needed. C4 also had a chunk-level
pruning helper that wrapped this primitive with N-way bookkeeping, which
only existed because C4 batched multiple cluster members into one Haiku
call; the primitive itself was already pure and pairwise.

athenaeum#1682 ports the primitive (not the chunk-batching wrapper) onto
:mod:`athenaeum.comparator`'s pairwise Gate 1, which has no chunking concept
to begin with. Rather than have ``comparator.py`` import ``merge.py`` (an
upward edge — ``merge.py`` is L4, sitting above ``comparator.py``'s own L4
position with a cross-domain dependency neither module should carry on the
other) this module holds the primitive on its own, low enough that
``comparator.py`` imports it downward. ``merge.py``'s own adapter around
this primitive (and the chunk-pruning wrapper above it) were dead code —
issue athenaeum#1256's C4 retirement left them with zero ``src/`` callers —
and issue athenaeum#2435 deleted both; this primitive is now reached only
through ``comparator.py``.

Layering: L1/L2 boundary primitive — imports only :mod:`athenaeum.models`
(L1, for :func:`~athenaeum.models.slugify`) and the stdlib ``logging``
module. No I/O, no config, no network — pure arithmetic over four
already-extracted frontmatter facts per side. Declared L2 in
``tests/fixtures/layer_declarations.py`` per that file's upper-bound
convention for a stated boundary (matches ``athenaeum.dimensions``'s own
"L1/L2" -> upper-bound note).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from athenaeum.models import slugify

log = logging.getLogger(__name__)

#: Rationale slugs :func:`declared_relationship` can return. Named constants
#: so callers never hardcode the literal strings — kept identical to the
#: values :mod:`athenaeum.merge`'s ``_declared_relationship`` has always
#: returned, so a caller migrating from that function sees no behaviour
#: change.
DECLARED_SUPERSESSION = "declared-supersession"
DECLARED_REFINEMENT = "declared-refinement"
DECLARED_MERGE_REJECTION = "declared-merge-rejection"


@dataclass(frozen=True)
class DeclaredRelationshipFacts:
    """The five frontmatter facts one side of a pair carries (athenaeum#167).

    ``name`` is this side's own declared slug (frontmatter ``name:``).
    ``refines`` and ``merge_rejected_with`` are the raw ``name:`` slug
    lists from those two frontmatter keys. ``supersedes_names`` is just
    the ``name`` key out of each ``supersedes:`` record (mirrors
    :meth:`athenaeum.models.AutoMemoryFile.supersedes_names` — this
    primitive only ever needs the name, never ``as_of``/``reason``).

    ``aliases`` (issue athenaeum#2546) is this side's OWN ``aliases:``
    frontmatter — the identity trail the athenaeum#2488 rename primitive and
    the fold write path both append to (a rename appends the previous
    ``name:``; a fold appends the folded-away sources' slugs). Only read by
    :func:`declared_relationship`'s ``merge_rejected_with`` branch, so a
    link recorded against a page's OLD name, or against a page later
    folded into this one, still resolves. Defaults to ``[]`` so every
    pre-athenaeum#2546 caller that never passes it is unaffected.
    """

    name: str
    refines: list[str] = field(default_factory=list)
    supersedes_names: list[str] = field(default_factory=list)
    merge_rejected_with: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


def identity_slugs(facts: DeclaredRelationshipFacts) -> set[str]:
    """This side's full identity: its own slug, plus every ``aliases:`` slug.

    Issue athenaeum#2546: factored out of :func:`declared_relationship`'s
    merge-rejection branch (the exact set it unions there) so every OTHER
    caller that needs to resolve "does this slug name this page, under any
    name it has ever carried" — e.g. ``revoke-merge-rejection`` stripping a
    ``merge_rejected_with:`` entry naming a page's PRE-rename name — reuses
    this one primitive instead of re-deriving the union by hand. Empty
    names/aliases are dropped, so a page with neither contributes nothing.
    """
    own = slugify((facts.name or "").strip())
    slugs = {own} if own else set()
    slugs.update(slugify(a) for a in facts.aliases if (a or "").strip())
    return slugs


def declared_relationship(
    a: DeclaredRelationshipFacts, b: DeclaredRelationshipFacts
) -> str | None:
    """Return a rationale slug when ``a`` and ``b`` declare each other.

    Direct port of :mod:`athenaeum.merge`'s ``_declared_relationship``
    (issue athenaeum#167; that adapter and its own chunk-pruning caller
    were deleted as dead code by issue athenaeum#2435) over plain facts
    instead of ``AutoMemoryFile``s, so a
    caller that has no ``AutoMemoryFile`` (e.g.
    :class:`athenaeum.comparator.ComparatorPage`) can reuse the exact same
    logic. Matches by slug (:func:`~athenaeum.models.slugify`), matching
    ``merge.py``'s original case-/punctuation-insensitive comparison
    (Quine review athenaeum#171 / SHOULD #4). A declaration on EITHER side
    suppresses the pair.

    Returns:
        ``"declared-supersession"`` when one side names the other in its
        ``supersedes`` list (the resolution is in the text — no human
        review needed). ``"declared-refinement"`` when one side names the
        other in its ``refines`` list (general + exception; both stay
        active and never count as a conflict). ``"declared-merge-rejection"``
        (issue athenaeum#715) when one side names the other in its
        ``merge_rejected_with`` list — a human REJECTED a merge proposal
        for this pair, an honest non-directional fact that is distinct
        from both of the above and must never be conflated with
        ``"declared-refinement"``: a refinement is an adjudicated
        specialization claim, a rejection is only "these are not the same
        claim". This branch (only) matches against each side's full
        identity — its own slug UNION its ``aliases:`` slugs (issue
        athenaeum#2546) — so a rejection recorded against a page's name
        before a rename (athenaeum#2488), or against a page since folded
        into one of ``a``/``b``, still fires: the rename/fold primitives
        both append the old identity to ``aliases:``, so it is still
        reachable from here without rewriting every historical link.
        ``None`` when no declaration applies.
    """
    a_name = (a.name or "").strip()
    b_name = (b.name or "").strip()
    if not a_name or not b_name:
        return None
    a_slug = slugify(a_name)
    b_slug = slugify(b_name)
    a_super = {slugify(n) for n in a.supersedes_names}
    b_super = {slugify(n) for n in b.supersedes_names}
    a_refines = {slugify(n) for n in a.refines}
    b_refines = {slugify(n) for n in b.refines}
    a_rejected = {slugify(n) for n in a.merge_rejected_with}
    b_rejected = {slugify(n) for n in b.merge_rejected_with}
    a_supersedes_b = b_slug in a_super
    b_supersedes_a = a_slug in b_super
    # MUST #3: mutual supersedes is itself a declared contradiction —
    # neither side wins deterministically. Log and refuse to declare;
    # the pair falls through to the detector/resolver path.
    if a_supersedes_b and b_supersedes_a:
        log.warning(
            "declared_relationships: mutual supersedes between %r and %r — "
            "not a declarable relationship",
            a_name,
            b_name,
        )
        return None
    if a_supersedes_b or b_supersedes_a:
        return DECLARED_SUPERSESSION
    if b_slug in a_refines or a_slug in b_refines:
        return DECLARED_REFINEMENT
    # Issue athenaeum#2546: identity-extended (alias-aware) match, so a
    # rename or a prior fold cannot silently drop a rejection — see the
    # docstring above. Empty when a side carries no aliases, so this is a
    # strict superset of the pre-athenaeum#2546 ``{a_slug}``/``{b_slug}``-only
    # check and changes nothing for any page that has never been renamed
    # or folded.
    a_identity = identity_slugs(a)
    b_identity = identity_slugs(b)
    if b_identity & a_rejected or a_identity & b_rejected:
        return DECLARED_MERGE_REJECTION
    return None


def merge_rejection_conflict(
    pages: Sequence[DeclaredRelationshipFacts],
) -> tuple[int, int] | None:
    """First pair of *pages* (by index) linked by ``merge_rejected_with``.

    Issue athenaeum#2546: the shared check behind "honoured everywhere" —
    the wiki-page clusterer, ``propose-fold`` and the fold-apply path in
    :mod:`athenaeum.pending_merges` all need to refuse a candidate group
    that would unite two mutually-rejected pages, directly or (since this
    reuses :func:`declared_relationship`'s alias-extended identity) through
    a fold chain. Rather than re-derive the pairwise check, this just calls
    :func:`declared_relationship` over every combination and reports the
    first pair whose rationale is :data:`DECLARED_MERGE_REJECTION` —
    O(n^2) in the (always small — a merge candidate group, never the whole
    corpus) size of *pages*.

    Returns ``None`` when no two pages conflict.
    """
    n = len(pages)
    for i in range(n):
        for j in range(i + 1, n):
            if declared_relationship(pages[i], pages[j]) == DECLARED_MERGE_REJECTION:
                return i, j
    return None


def unresolved_merge_rejected_with(
    pages: Sequence[DeclaredRelationshipFacts],
) -> list[tuple[str, str]]:
    """``(page_name, entry)`` for every ``merge_rejected_with`` entry that
    names no page in *pages* (issue athenaeum#2546 — the ``athenaeum doctor``
    dangling-link check).

    A page's OWN name and ``aliases:`` slugs both count as a live resolution
    target (matching :func:`declared_relationship`'s identity-extended
    match), so a link recorded against a page's pre-rename name, or a page
    since folded into another, is NOT reported as dangling — only an entry
    that resolves to nothing at all in *pages* is.
    """
    known: set[str] = set()
    for p in pages:
        name_slug = slugify((p.name or "").strip())
        if name_slug:
            known.add(name_slug)
        known.update(slugify(a) for a in p.aliases)
    out: list[tuple[str, str]] = []
    for p in pages:
        for entry in p.merge_rejected_with:
            if slugify(entry) not in known:
                out.append((p.name, entry))
    return out
