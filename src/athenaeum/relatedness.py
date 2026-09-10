# SPDX-License-Identifier: Apache-2.0
"""Compile-time ``related:`` edge writer (issue athenaeum#1576).

The librarian did not write ``related:`` edges. The only writer in the
codebase was the oversize-page split disposition (``tiers.py``, ``role:
split-from``); ``WikiEntity.related`` otherwise stayed ``[]``, and on the
live corpus ~7.9% of pages carried any edge at all (issue athenaeum#1568).
The viewer's ``breadcrumb`` state, which fires only when a page PUSHED this
session names a pulled page in ``related:``, therefore almost never fired.

What this module does
---------------------

Given the wiki as it stands and a page being compiled, propose the edges
that page should carry OUT to pages already in the wiki. It writes nothing
and reads no LLM: :func:`propose_related_edges` is a pure function over a
:class:`RelatednessIndex`.

The signal, and why it is the lowest rung that works
----------------------------------------------------

``docs/north-star.md`` §2.2 makes the ladder binding: deterministic first,
then cheap reasoning, then expensive. Issue athenaeum#1576 named three
candidate rungs. All three were measured against issue athenaeum#1570's
relatedness corpus (``tests/evals/data/corpus/core/08-relatedness.yaml``,
Cluster A -- three pages describing one concept at three altitudes, carrying
zero edges between them) before this module was written:

1. **Resolved name / alias mentions.** Zero edges on Cluster A. Not one of
   the three pages names another's ``name:`` or any of its ``aliases:``.
   That is the fixture's whole point: pages a reader calls related need not
   cite each other.
2. **Shared distinctive tags.** Zero edges on Cluster A. The three pages'
   only shared tag is ``delivery``, which sits on 15 of the corpus's 96 core
   pages -- so any distinctiveness threshold that admits it links those 15
   to each other as well. Their remaining tags (``engagement-transfer``,
   ``framework``, ``staffing``) are singletons and overlap nothing.
3. **MiniLM embedding neighbourhood** (the rung athenaeum#1576 named next).
   Measured with ``athenaeum.search.embed_texts`` over the same text fields
   this module uses. It reaches full recall at k>=3 but drags two spurious
   edges in with it (``model-handover-ladder -> ops-escalation-path`` at
   cosine 0.488, ABOVE its 0.481 to the correct target, and
   ``process-shadow-fortnight -> ops-client-reporting`` at 0.484, likewise
   above): precision 0.600, f1 0.750. Note athenaeum#1140's chunk-and-mean-
   pool is a mathematical no-op on this fixture -- the bodies are ~150 words,
   one chunk each -- so that measurement IS the production embedding path.

What this module ships instead is a rung BELOW all three: corpus-weighted
distinctive-term overlap, computed from the wiki's own text. No model, no
network, no metered call, and no chromadb ONNX download -- which is also why
the eval that grades it (``tests/evals/test_relatedness_writer_eval.py``)
runs in the DEFAULT CI selection rather than behind ``-m embedding``. On the
same fixture it scores recall 1.000, precision 1.000, f1 1.000. Reaching for
rung 3 (a model-proposed CREATE-prompt field) would have been reaching past
a cheaper rung that already passes.

Why it is not "link everything"
-------------------------------

    One hop, same session: a pulled page counts as breadcrumbed only if some
    page PUSHED in this session names it in ``related:``. Transitive closure
    over a 25k-page corpus would make nearly everything a breadcrumb and the
    colour would stop carrying information.
    -- ``src/athenaeum/_cmd_viewer.py:577-580``

Three constraints keep the edge count near one per page rather than near N:

* **Mutual k-nearest-neighbour.** An edge ``u -> v`` is proposed only when
  ``v`` is among ``u``'s top-k AND ``u`` is among ``v``'s top-k. A page that
  is merely *near* a hub page does not get to link to it.
* **An absolute similarity floor**, so a page with no real neighbours writes
  no edges at all rather than linking to its least-bad option.
* **A hard cap** on edges per page.

Measured edge density with the shipped defaults: 1.60 edges/page at the
``core`` scale (96 pages), 1.80 at ``small`` (200), 2.51 at ``medium``
(1000). It does grow with corpus size, and that is stated rather than
glossed -- but it grows toward the ``max_edges`` cap of 4, not toward N.
The adversary the one-hop caution is about scores N-1 edges per page (999 at
``medium``); the gap is three orders of magnitude, and it is the CAP, not
the corpus, that bounds the ceiling.

Why not reuse an existing similarity mechanism
----------------------------------------------

``wiki_dedupe.py``'s docstring is explicit that clustering must not grow a
second copy (issue athenaeum#803), so this was checked rather than assumed:

* ``athenaeum.clusters.cluster_auto_memory_files`` forms complete-linkage
  CLIQUES over embedding vectors. This module needs a per-page ranked
  neighbour list with a mutuality test, not a partition, and it deliberately
  does not use embeddings (see above). Different output, different input.
* ``athenaeum.search.Fts5Backend.query`` is BM25 over an on-disk index, and
  truncates its query to the first eight terms (``search.py``, ``terms[:8]``
  in the MATCH builder). A whole page's text collapsed to eight terms is not
  the page's neighbourhood, and BM25 scores are neither symmetric nor
  comparably normalized across queries, so the mutuality test could not be
  expressed over it.

Layering (L3 service). Its only athenaeum imports are ``config`` (L2) and
``models.parse_frontmatter`` (L1), both deferred to call time so importing
this module costs nothing; nothing imports it back. Nothing here touches the
filesystem except :func:`build_index_from_wiki`, which reads pages under a
root the caller supplies.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: ``role`` stamped on every edge this module proposes (issue athenaeum#1576
#: AC4). It names the SIGNAL, not the semantic relation, so a later pass can
#: retract this whole class of edge -- ``related: [{role: term-overlap}]`` --
#: without touching ``split-from``/``split-into`` or anything a future signal
#: writes. Deliberately not ``related`` (the corpus-wide default, which names
#: nothing) and not ``mentions`` (already spoken for by the eval corpus's
#: ``links:`` sugar, with different provenance).
ROLE_TERM_OVERLAP = "term-overlap"

#: Neighbours considered on each side of the mutuality test. 4 is the LOWEST
#: value that survives the corpus-size axis: k=2 and k=3 score 3/3 edges at
#: the ``core`` scale but degrade to 2/3 at 200 pages and again at 1000,
#: while k=4 holds 3/3 with ZERO spurious edges at core, small and medium
#: alike. k=5 also holds, at a linearly higher edge density -- so this is the
#: bottom of a plateau, not a tuned point. Precision was 1.000 at every k and
#: every floor measured: the mutuality test, not the parameter, is what keeps
#: spurious edges out.
DEFAULT_NEIGHBOURS = 4

#: Cosine floor below which a candidate is not a neighbour at all. Chosen so
#: a page with no real relations writes nothing rather than linking to its
#: least-bad option. The measured result is identical at 0.06, 0.08 and 0.10
#: -- a plateau this default sits in the middle of, which is the evidence
#: that it was not reverse-engineered from the fixture. 0.14 is past the far
#: edge (recall falls to 2/3), so the plateau's width is known, not assumed.
DEFAULT_FLOOR = 0.08

#: Hard ceiling on edges written per page, independent of *k*. A backstop
#: against a pathological page (a glossary, an index page) that is genuinely
#: mutual-near to many others.
DEFAULT_MAX_EDGES = 4

#: Terms retained per page vector, highest weight first. A page's tail terms
#: contribute almost nothing to a cosine and dominate the memory footprint of
#: the posting lists at 25k pages; pruning bounds the index at O(pages * 64)
#: postings instead of O(total distinct words).
DEFAULT_MAX_TERMS = 64

#: Body prefix indexed, in characters. Bounds tokenization cost on a very
#: long page. Well above the corpus's page-size threshold for ordinary pages.
DEFAULT_MAX_BODY_CHARS = 8000

#: Pages above which the writer declines to build an index at all, logs, and
#: proposes no edges. A ceiling, not a tuning knob: it exists so that turning
#: this on against an unexpectedly large corpus degrades to the pre-athenaeum#1576
#: behaviour (no edges) rather than to an unbounded build.
DEFAULT_MAX_INDEX_PAGES = 50_000

_TOKEN_RE = re.compile(r"[a-z][a-z\-']+")

# Closed-class words carry no topical signal and, unlike a corpus-derived
# cutoff, this list does not shift when the corpus does. IDF already suppresses
# corpus-specific boilerplate, so the list stays deliberately short: a longer
# hand-maintained list is a second, unmeasured tuning surface.
_STOPWORDS = frozenset(
    """
    a an the and or of to in on for is are was were be been being it its this that
    with as at by from not no but if then than so such which who whom whose what
    when where how all any both each few more most other some own same too very
    can will just should now they them their there here we you your our i he she
    his her have has had do does did one two three four five
    """.split()
)


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) > 2 and token not in _STOPWORDS
    ]


def page_text(
    name: str,
    aliases: Sequence[str],
    tags: Sequence[str],
    body: str,
    *,
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
) -> str:
    """The text fields this module indexes, in one place.

    Name, aliases and tags are included alongside the body deliberately: they
    are the page's most distinctive terms and a body-only index systematically
    under-weights them. The same composition is used for every page and for
    every candidate, so a comparison is never between differently-composed
    texts -- the failure mode that makes a similarity measurement unfair.
    """
    parts = [name, " ".join(aliases), " ".join(tags), body[:max_body_chars]]
    return "\n".join(part for part in parts if part)


@dataclass(frozen=True)
class Neighbour:
    """One scored candidate. ``score`` is a cosine in ``[0, 1]``."""

    uid: str
    score: float


class RelatednessIndex:
    """Distinctive-term index over wiki pages, supporting ranked neighbours.

    Term weights are ``(1 + log tf) * idf``, L2-normalized per page, so a
    neighbour score is a plain cosine. IDF is recomputed from the live
    document frequencies whenever the index has changed since a page's
    weights were last cached, so a page added mid-run is scored against the
    same statistics as every other page rather than against a stale snapshot.

    Not thread-safe, and not intended to be: the librarian's entity phase is
    single-threaded and this index is scoped to one run.
    """

    def __init__(self, *, max_terms: int = DEFAULT_MAX_TERMS) -> None:
        self._max_terms = max_terms
        self._tf: dict[str, Counter[str]] = {}
        self._df: Counter[str] = Counter()
        self._postings: dict[str, set[str]] = {}
        self._generation = 0
        self._weights: dict[str, dict[str, float]] = {}
        self._weights_generation = -1

    def __len__(self) -> int:
        return len(self._tf)

    def __contains__(self, uid: str) -> bool:
        return uid in self._tf

    @property
    def uids(self) -> list[str]:
        return list(self._tf)

    def add(self, uid: str, text: str) -> None:
        """Index (or re-index) *uid* under *text*.

        Re-indexing an already-present uid replaces it, so a caller that
        compiles the same page twice in one run cannot double-count it into
        the document frequencies.
        """
        if uid in self._tf:
            self.remove(uid)
        counts = Counter(_tokenize(text))
        if not counts:
            return
        # Prune to the highest-tf terms BEFORE they reach the posting lists.
        # tf, not tf-idf: idf is not stable while the index is still being
        # built, and pruning on a moving statistic would make the index
        # depend on insertion order.
        kept = dict(counts.most_common(self._max_terms))
        self._tf[uid] = Counter(kept)
        for term in kept:
            self._df[term] += 1
            self._postings.setdefault(term, set()).add(uid)
        self._generation += 1

    def remove(self, uid: str) -> None:
        counts = self._tf.pop(uid, None)
        if counts is None:
            return
        for term in counts:
            self._df[term] -= 1
            posting = self._postings.get(term)
            if posting is not None:
                posting.discard(uid)
                if not posting:
                    del self._postings[term]
            if self._df[term] <= 0:
                del self._df[term]
        self._generation += 1

    # -- scoring ----------------------------------------------------------

    def _rebuild_weights(self) -> None:
        total = len(self._tf) or 1
        idf = {term: math.log((total + 1) / (count + 1)) + 1.0 for term, count in self._df.items()}
        weights: dict[str, dict[str, float]] = {}
        for uid, counts in self._tf.items():
            raw = {
                term: (1.0 + math.log(tf)) * idf[term] for term, tf in counts.items() if term in idf
            }
            norm = math.sqrt(sum(value * value for value in raw.values())) or 1.0
            weights[uid] = {term: value / norm for term, value in raw.items()}
        self._weights = weights
        self._weights_generation = self._generation

    def _weights_for(self, uid: str) -> dict[str, float]:
        if self._weights_generation != self._generation:
            self._rebuild_weights()
        return self._weights.get(uid, {})

    def neighbours(
        self,
        uid: str,
        *,
        k: int = DEFAULT_NEIGHBOURS,
        floor: float = DEFAULT_FLOOR,
        candidates: set[str] | None = None,
    ) -> list[Neighbour]:
        """Top-*k* pages by cosine to *uid*, scoring at least *floor*.

        ``candidates``, when given, restricts the result to those uids. It is
        NOT how the compile-time direction rule is enforced -- that falls out
        of the index only ever containing pages that already exist -- but it
        lets a caller scope a query without rebuilding an index.
        """
        query = self._weights_for(uid)
        if not query:
            return []
        # A plain dict rather than a Counter: Counter is typed as int-valued,
        # and these accumulators are cosine partial sums.
        scores: dict[str, float] = {}
        for term, weight in query.items():
            for other in self._postings.get(term, ()):
                if other == uid:
                    continue
                if candidates is not None and other not in candidates:
                    continue
                contribution = weight * self._weights_for(other).get(term, 0.0)
                scores[other] = scores.get(other, 0.0) + contribution
        ranked = sorted(
            (
                Neighbour(uid=other, score=score)
                for other, score in scores.items()
                if score >= floor
            ),
            # uid breaks ties so the result is stable across runs; a
            # set-iteration-order-dependent neighbour list would make the
            # writer non-reproducible for a fixed corpus.
            key=lambda n: (-n.score, n.uid),
        )
        return ranked[:k]


def propose_related_edges(
    uid: str,
    index: RelatednessIndex,
    *,
    k: int = DEFAULT_NEIGHBOURS,
    floor: float = DEFAULT_FLOOR,
    max_edges: int = DEFAULT_MAX_EDGES,
) -> list[dict[str, str]]:
    """The ``related:`` rows *uid* should carry, in ``WikiEntity.related`` shape.

    *uid* must already be in *index*. Every returned edge points at a page
    that is mutually among *uid*'s top-*k* -- see this module's docstring for
    why mutuality rather than a bare top-k.

    The compile-time direction (issue athenaeum#1576 AC5: this changes what
    compilation writes GOING FORWARD, and backfilling existing edgeless pages
    is out of scope) is structural rather than a rule the caller has to
    remember: the index holds the pages that already exist, edges point from
    the page being compiled into it, and no existing page's frontmatter is
    touched.
    """
    proposals = index.neighbours(uid, k=k, floor=floor)
    edges: list[dict[str, str]] = []
    for candidate in proposals:
        mutual = index.neighbours(candidate.uid, k=k, floor=floor)
        if any(back.uid == uid for back in mutual):
            edges.append({"uid": candidate.uid, "role": ROLE_TERM_OVERLAP})
        if len(edges) >= max_edges:
            break
    return edges


# ---------------------------------------------------------------------------
# Wiki-facing helpers
# ---------------------------------------------------------------------------


def build_index_from_wiki(
    wiki_root: Path,
    *,
    max_pages: int = DEFAULT_MAX_INDEX_PAGES,
    max_terms: int = DEFAULT_MAX_TERMS,
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
) -> RelatednessIndex | None:
    """Index every entity page under *wiki_root*, or ``None`` if too large.

    Returns ``None`` -- meaning "propose no edges" -- above *max_pages*,
    rather than raising or building an unbounded index. Sidecars
    (``_pending_*.md``) and cluster outputs (``auto-*.md``) are skipped: they
    are not entities and must never become edge targets.
    """
    from athenaeum.models import parse_frontmatter

    paths = [
        path
        for path in sorted(wiki_root.glob("*.md"))
        if not path.name.startswith("_") and not path.name.startswith("auto-")
    ]
    if len(paths) > max_pages:
        log.info(
            "relatedness-writer-skipped pages=%d max=%d: no edges proposed "
            "this run (issue athenaeum#1576)",
            len(paths),
            max_pages,
        )
        return None

    index = RelatednessIndex(max_terms=max_terms)
    for path in paths:
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        uid = str(meta.get("uid") or "").strip()
        if not uid:
            continue
        index.add(
            uid,
            page_text(
                str(meta.get("name") or ""),
                _as_str_list(meta.get("aliases")),
                _as_str_list(meta.get("tags")),
                body,
                max_body_chars=max_body_chars,
            ),
        )
    return index


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value if isinstance(item, (str, int, float))]
    return []


def stamp_related_edges(
    entities: Sequence[Any],
    index: RelatednessIndex | None,
    *,
    k: int = DEFAULT_NEIGHBOURS,
    floor: float = DEFAULT_FLOOR,
    max_edges: int = DEFAULT_MAX_EDGES,
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
) -> int:
    """Add proposed edges to each newly-created entity, in place.

    Each entity is added to *index* after its own edges are proposed, so a
    later entity in the same run can link to an earlier one -- the same
    already-exists rule, applied within the run.

    Edges ALREADY on an entity (the ``split-from`` rows the oversize split
    writes) are preserved, and a duplicate target is never appended twice:
    this signal adds to the page's edge set, it does not own it. Returns the
    number of edges added.
    """
    if index is None:
        return 0
    added = 0
    for entity in entities:
        uid = getattr(entity, "uid", "")
        if not uid:
            continue
        index.add(
            uid,
            page_text(
                getattr(entity, "name", "") or "",
                getattr(entity, "aliases", None) or [],
                getattr(entity, "tags", None) or [],
                getattr(entity, "body", "") or "",
                max_body_chars=max_body_chars,
            ),
        )
        existing = {str(row.get("uid")) for row in (entity.related or []) if isinstance(row, dict)}
        for edge in propose_related_edges(uid, index, k=k, floor=floor, max_edges=max_edges):
            if edge["uid"] in existing:
                continue
            entity.related.append(edge)
            existing.add(edge["uid"])
            added += 1
    return added


# ---------------------------------------------------------------------------
# Per-run index cache
# ---------------------------------------------------------------------------
#
# The librarian's entity phase reaches its write boundary once per RAW FILE,
# and a run processes many. Building the index there would re-tokenize the
# whole wiki per file, which at 25k pages is the one cost in this design that
# could actually matter -- the neighbour queries themselves are posting-list
# lookups over at most ``DEFAULT_MAX_TERMS`` terms and are not the concern.
#
# So the index is built at most once per (process, wiki_root) and the run's own
# freshly-created pages are folded into it incrementally by
# :func:`stamp_related_edges` as they are compiled. That is correct rather than
# merely cheap: ``athenaeum run`` is a batch process, so "once per process" IS
# "once per run", and a page created earlier in the run is exactly the kind of
# page a later one should be able to link to.

_RUN_INDEX: dict[Path, RelatednessIndex | None] = {}


def run_index(wiki_root: Path, *, config: dict[str, Any] | None = None) -> RelatednessIndex | None:
    """The index for this run, built on first use for *wiki_root*.

    ``None`` means "propose no edges" -- either the writer is disabled
    (``librarian.relatedness_writer: false``) or the wiki is larger than
    :data:`DEFAULT_MAX_INDEX_PAGES`. ``None`` is cached too, so a disabled or
    over-size wiki is not re-scanned once per raw file.
    """
    from athenaeum.config import resolve_relatedness_writer_enabled

    if not resolve_relatedness_writer_enabled(config):
        return None
    key = wiki_root.resolve()
    if key not in _RUN_INDEX:
        _RUN_INDEX[key] = build_index_from_wiki(wiki_root)
    return _RUN_INDEX[key]


def reset_run_index() -> None:
    """Drop the cached index.

    Called by tests, and by anything that needs the next
    :func:`run_index` to re-read the wiki from disk.
    """
    _RUN_INDEX.clear()
