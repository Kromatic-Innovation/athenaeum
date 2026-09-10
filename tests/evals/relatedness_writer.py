# SPDX-License-Identifier: Apache-2.0
"""Replay the compile-time relatedness writer over a synthetic corpus.

Issue athenaeum#1576 is graded by issue athenaeum#1570's measure
(:mod:`tests.evals.relatedness`), and that measure reads ``related:`` edges
off pages. So the writer has to be RUN against a corpus before it can be
scored, and this module is that run.

It is deliberately thin. Every decision -- what counts as a neighbour, the
mutuality test, the floor, the cap, the role -- lives in
:mod:`athenaeum.relatedness` and is reached through
:func:`athenaeum.relatedness.stamp_related_edges`, the same entry point
``librarian._apply_tier3_results`` calls at the wiki's write boundary. If
this module reimplemented the scoring, AC1 would be verified against a copy
of the writer and production would be free to diverge from it.

What the replay models
----------------------

One librarian run against an empty wiki, compiling every page in the corpus
in ``created`` order. That is the honest shape of the compile-time contract:
a page's edges point only at pages that ALREADY exist when it is compiled,
so the corpus's oldest page writes nothing and each later page sees exactly
its predecessors. No page's existing frontmatter is rewritten, which is issue
athenaeum#1576 AC5 (no backfill) holding structurally rather than by
convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from athenaeum.relatedness import (
    DEFAULT_FLOOR,
    DEFAULT_MAX_EDGES,
    DEFAULT_NEIGHBOURS,
    RelatednessIndex,
    stamp_related_edges,
)
from tests.evals.corpus import Page, RelatedEdge


@dataclass
class _CompiledEntity:
    """The duck-typed surface :func:`stamp_related_edges` writes through.

    ``WikiEntity`` itself is not used here because constructing one drags in
    provenance/schema fields the corpus does not model and this measurement
    does not depend on. What matters is that the fields the writer reads
    (``uid``/``name``/``aliases``/``tags``/``body``) and the field it appends
    to (``related``) are the same ones, in the same shapes, as
    ``WikiEntity``'s.
    """

    uid: str
    name: str
    aliases: list[str]
    tags: list[str]
    body: str
    related: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class WriterRun:
    """A replay's outcome: the written corpus plus what it cost in edges."""

    pages: list[Page]
    edges_written: int

    @property
    def edges_per_page(self) -> float:
        return self.edges_written / len(self.pages) if self.pages else 0.0


def compile_corpus(
    pages: list[Page],
    *,
    k: int = DEFAULT_NEIGHBOURS,
    floor: float = DEFAULT_FLOOR,
    max_edges: int = DEFAULT_MAX_EDGES,
) -> WriterRun:
    """Run the writer over *pages* and return them with their edges written.

    Pages are compiled in ``(created, uid)`` order -- ``uid`` breaks the tie
    so a corpus with several pages sharing a date replays identically every
    time.
    """
    order = sorted(pages, key=lambda page: (str(page.created), page.uid))
    index = RelatednessIndex()
    proposed: dict[str, tuple[RelatedEdge, ...]] = {}
    written = 0

    for page in order:
        entity = _CompiledEntity(
            uid=page.uid,
            name=page.name,
            aliases=list(page.aliases),
            tags=list(page.tags),
            body=page.body,
            related=[{"uid": edge.uid, "role": edge.role} for edge in page.related],
        )
        written += stamp_related_edges([entity], index, k=k, floor=floor, max_edges=max_edges)
        proposed[page.uid] = tuple(
            RelatedEdge(uid=row["uid"], role=row["role"]) for row in entity.related
        )

    return WriterRun(
        pages=[replace(page, related=proposed[page.uid]) for page in pages],
        edges_written=written,
    )
