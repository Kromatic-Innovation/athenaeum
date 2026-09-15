# SPDX-License-Identifier: Apache-2.0
"""Meaning-based entity-name resolution fallback (issue athenaeum#1615).

Every entity-name match today is an exact-string dict lookup:
:meth:`athenaeum.models.EntityIndex.lookup` and
:meth:`athenaeum.person_registry.PersonRegistry.lookup` are both a
``dict.get(name.lower())`` (see athenaeum#1615's motivation). A raw mention of
"Bryan Went" misses an existing page named "Bryan Went 🦁" and a second page
gets minted for the same person.

This module is the reusable resolver athenaeum#1615 adds: given a candidate name
(or page) and a set of existing candidate pages of the SAME type (the caller
is responsible for type-scoping — see :meth:`athenaeum.models.EntityIndex.
pages_of_type`), decide whether the candidate is the SAME subject as one of
them, using:

1. **Candidate generation** — embedding similarity over normalized names
   (:func:`normalize_name` strips emoji/symbols and casefolds). The embedder
   is injectable (``embedder``) and defaults to
   :func:`athenaeum.search.embed_texts`, which itself degrades gracefully
   (returns ``None``) when chromadb is unavailable.
2. **Confirmation** — an injectable ``confirm`` callable checks the top-k
   embedding candidates. This module has NO knowledge of LLMs, prompts, or
   Anthropic clients; :mod:`athenaeum.tiers` supplies a concrete tier-2-based
   confirmer (``tiers._tier2_confirm_same_subject``) that IS aware of those
   things and wires it in at the :func:`athenaeum.tiers.validate_create_name`
   call site. This split keeps the resolver a pure, cheaply-testable
   algorithm — exactly what athenaeum#1244 needs to call directly on page pairs.

Layering: L4 (domain/pipeline). Imports :mod:`athenaeum.config` (L2) and
:mod:`athenaeum.search` (L3), both strictly below; :mod:`athenaeum.tiers`
(L4) imports this module for its create-path wiring, and athenaeum#1244's
page-pair resolver is expected to do the same.

Degrades to :class:`NoMatch` — never a silent merge — whenever any input is
unavailable: no existing pages, no embedder, no confirmer, or the confirmer
raising. A wrong merge welds two real subjects together and is hard to
detect; a duplicate page is recoverable. See each branch's ``log.warning``
call, which is what makes a degradation VISIBLE rather than silent (the
build-lane brief's explicit requirement).
"""

from __future__ import annotations

import logging
import math
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence, Union

from athenaeum.config import resolve_name_similarity_threshold
from athenaeum.search import embed_texts

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubjectPage:
    """One page (candidate or existing) the resolver can compare.

    ``uid`` is ``None`` for a bare-name candidate (the "name against the
    index" call shape); every *existing_pages* entry the caller supplies
    should carry a real ``uid``. ``body``/``path`` are optional — a
    confirmer that wants page content may read ``path`` lazily rather than
    require every caller to pre-load every candidate's full body.
    """

    name: str
    uid: str | None = None
    type: str | None = None
    body: str = ""
    path: Path | None = None


@dataclass(frozen=True)
class Match:
    """The candidate is the same subject as an existing page."""

    uid: str


@dataclass(frozen=True)
class Ambiguous:
    """Multiple existing pages are plausibly the same subject; a human must decide."""

    uids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class NoMatch:
    """No existing page is confidently the same subject — create as today."""


ResolutionResult = Union[Match, Ambiguous, NoMatch]

#: A confirmer receives the candidate and the scored, threshold-passing
#: embedding candidates (highest similarity first, capped to top-k) and
#: returns a :data:`ResolutionResult`. Never called with an empty sequence.
ConfirmFn = Callable[[SubjectPage, Sequence[tuple[SubjectPage, float]]], ResolutionResult]

#: An embedder takes a list of texts and returns one vector per text, or
#: ``None`` when embedding is unavailable — the same contract as
#: :func:`athenaeum.search.embed_texts`.
EmbedFn = Callable[[list[str]], "list[list[float]] | None"]

#: How many top-scoring embedding candidates are handed to the confirmer.
#: The issue's plan step 5 measures whether more/fewer candidates changes
#: tier-2 call counts; 3 is a reasonable starting point, not a tuned value.
DEFAULT_TOP_K = 3


def normalize_name(name: str) -> str:
    """Casefold *name* and strip decorative/symbol/control characters.

    Strips any character whose Unicode general category starts with ``S``
    (Symbol — covers emoji, which live in the So subcategory) or ``C``
    (Other — control/format characters), then collapses whitespace. This is
    what lets "Bryan Went 🦁" normalize to the same string as "Bryan Went".
    Deliberately category-based rather than an explicit emoji codepoint
    range: it also strips non-emoji decorative symbols (e.g. "☆") without a
    range list to maintain.
    """
    stripped = "".join(ch for ch in name if unicodedata.category(ch)[0] not in ("S", "C"))
    return " ".join(stripped.split()).casefold()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def resolve_same_subject(
    candidate: "str | SubjectPage",
    existing_pages: Sequence[SubjectPage],
    *,
    embedder: EmbedFn | None = None,
    confirm: ConfirmFn | None = None,
    config: dict[str, Any] | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> ResolutionResult:
    """Decide whether *candidate* is the same subject as one of *existing_pages*.

    *candidate* may be a bare name (the create-path call shape used by
    :func:`athenaeum.tiers.validate_create_name`) or a :class:`SubjectPage`
    (the page-pair call shape athenaeum#1244 needs). *existing_pages* should
    already be scoped to the candidate's own entity type by the caller — see
    :meth:`athenaeum.models.EntityIndex.pages_of_type` — matching
    :func:`athenaeum.tiers.validate_create_name`'s existing type-scoping rule
    (a ``type: project`` page and a ``type: person`` page may legitimately
    share a name).

    Returns :class:`NoMatch` immediately (no embedder/confirm call at all)
    when *existing_pages* is empty.
    """
    if not existing_pages:
        return NoMatch()

    cand = candidate if isinstance(candidate, SubjectPage) else SubjectPage(name=candidate)
    cand_norm = normalize_name(cand.name)
    if not cand_norm:
        return NoMatch()

    embed = embedder if embedder is not None else embed_texts
    texts = [cand_norm] + [normalize_name(p.name) for p in existing_pages]
    vectors = embed(texts)
    if vectors is None:
        log.warning(
            "entity-resolution-similarity degraded=embedder-unavailable "
            "candidate=%r existing_count=%d — falling back to no_match "
            "(issue athenaeum#1615)",
            cand.name,
            len(existing_pages),
        )
        return NoMatch()

    cand_vec, *page_vecs = vectors
    threshold = resolve_name_similarity_threshold(config)
    scored = [
        (page, _cosine(cand_vec, vec))
        for page, vec in zip(existing_pages, page_vecs)
        if _cosine(cand_vec, vec) >= threshold
    ]
    if not scored:
        return NoMatch()
    scored.sort(key=lambda pair: pair[1], reverse=True)
    top = tuple(scored[:top_k])

    if confirm is None:
        log.warning(
            "entity-resolution-similarity degraded=no-confirmer candidate=%r "
            "top_candidates=%s — embedding alone is never sufficient to "
            "merge; falling back to no_match (issue athenaeum#1615)",
            cand.name,
            [(p.uid, round(score, 4)) for p, score in top],
        )
        return NoMatch()

    try:
        result = confirm(cand, top)
    except Exception:
        log.warning(
            "entity-resolution-similarity degraded=confirm-error candidate=%r "
            "top_candidates=%s — falling back to no_match (issue athenaeum#1615)",
            cand.name,
            [(p.uid, round(score, 4)) for p, score in top],
            exc_info=True,
        )
        return NoMatch()

    if isinstance(result, Match):
        candidate_uids = {p.uid for p, _ in top if p.uid is not None}
        if result.uid not in candidate_uids:
            log.warning(
                "entity-resolution-similarity degraded=confirm-uid-outside-candidates "
                "candidate=%r confirmed_uid=%s top_uids=%s — falling back to "
                "no_match (issue athenaeum#1615)",
                cand.name,
                result.uid,
                sorted(candidate_uids),
            )
            return NoMatch()
    return result
