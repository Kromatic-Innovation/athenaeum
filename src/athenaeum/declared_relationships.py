# SPDX-License-Identifier: Apache-2.0
"""Declared-relationship primitive (athenaeum#1682, port of C4 §3.8) — L1/L2 primitive.

Extracted out of :mod:`athenaeum.merge`'s ``_declared_relationship``
(issue athenaeum#167 / athenaeum#715), which pairwise-compares two
``AutoMemoryFile``s' own declared ``refines``/``supersedes``/
``merge_rejected_with`` frontmatter to short-circuit a pair that has
already been adjudicated by a human (or by ``pending_merges.resolve_merge``
on the reviewer's behalf) — no LLM call needed. C4's ``_filter_declared_pairs``
(``merge.py:271``) wraps this primitive with N-way chunk-pruning bookkeeping
that only exists because C4 batches multiple cluster members into one Haiku
call; the primitive itself was already pure and pairwise.

athenaeum#1682 ports the primitive (not the chunk-batching wrapper) onto
:mod:`athenaeum.comparator`'s pairwise Gate 1, which has no chunking concept
to begin with. Rather than have ``comparator.py`` import ``merge.py`` (an
upward edge — ``merge.py`` is L4, sitting above ``comparator.py``'s own L4
position with a cross-domain dependency neither module should carry on the
other) this module holds the primitive on its own, low enough that BOTH
``merge.py`` and ``comparator.py`` import it downward. ``merge.py``'s
``_declared_relationship`` now delegates here; its name, signature, and
docstring are unchanged, only its body became an adapter.

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
    """The four frontmatter facts one side of a pair carries (athenaeum#167).

    ``name`` is this side's own declared slug (frontmatter ``name:``).
    ``refines`` and ``merge_rejected_with`` are the raw ``name:`` slug
    lists from those two frontmatter keys. ``supersedes_names`` is just
    the ``name`` key out of each ``supersedes:`` record (mirrors
    :meth:`athenaeum.models.AutoMemoryFile.supersedes_names` — this
    primitive only ever needs the name, never ``as_of``/``reason``).
    """

    name: str
    refines: list[str] = field(default_factory=list)
    supersedes_names: list[str] = field(default_factory=list)
    merge_rejected_with: list[str] = field(default_factory=list)


def declared_relationship(
    a: DeclaredRelationshipFacts, b: DeclaredRelationshipFacts
) -> str | None:
    """Return a rationale slug when ``a`` and ``b`` declare each other.

    Direct port of :mod:`athenaeum.merge`'s ``_declared_relationship``
    (issue athenaeum#167) over plain facts instead of ``AutoMemoryFile``s,
    so a caller that has no ``AutoMemoryFile`` (e.g.
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
        claim". ``None`` when no declaration applies.
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
    if b_slug in a_rejected or a_slug in b_rejected:
        return DECLARED_MERGE_REJECTION
    return None
