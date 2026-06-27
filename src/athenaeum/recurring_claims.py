# SPDX-License-Identifier: Apache-2.0
"""Cross-entity recurring-claim detector (issue #272, slice 1 of #258).

READ-ONLY. The detector surfaces the SAME claim restated across DIFFERENT
wiki entities (different files / uids) and emits a report. It mutates
nothing under ``wiki/``.

Claim unit (issue #262): when an entity carries source ``claim:`` footnotes
(stamped by ``retire.py`` on move), each footnote claim is one occurrence at
``footnote`` granularity. Otherwise the body is split into sentences and each
sentence is one occurrence at ``sentence`` granularity (a coarse fallback so
entities written before footnote-targeting still participate).

Grouping is embedding-driven: claim texts are embedded via an INJECTED
provider (``list[str] -> list[vec] | None``) so the suite stays offline.

Linkage policy is **complete-linkage**: a claim joins a group only when its
cosine clears ``threshold`` against EVERY current member of that group (not
just one). This is deliberately the strictest linkage — single-linkage would
fuse A and C whenever A~B and B~C even if A and C are dissimilar, silently
grouping distinct claims. A group is only reported when it spans >= 2 distinct
entities, so two restatements within one entity are not a cross-entity
recurrence. The group key is a stable, order-independent hash of the grouped
claim set (mirrors :func:`athenaeum.fingerprint.claim_pair_fingerprint`), so
repeated runs over an unchanged wiki yield identical keys.

Complexity: embedding is a single batched provider call; the cosine matrix is
vectorized via numpy when available (pure-Python ``_cosine`` fallback). The
greedy complete-linkage pass is O(N^2) in the worst case over occurrence
count — fine for a per-wiki sweep; revisit if claim counts grow large.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from athenaeum.cross_scope import _cosine
from athenaeum.fingerprint import normalize_side
from athenaeum.merge import _parse_one_source
from athenaeum.models import is_inactive_memory, parse_frontmatter
from athenaeum.search import _iter_wiki_entries

# Injected embedding provider: maps a list of texts to a list of vectors, or
# returns None when no embedding backend is available (graceful degradation).
EmbeddingProvider = Callable[[list[str]], "list[list[float]] | None"]

# Default cosine cutoff. Matches the cross_scope similarity convention (0.85).
DEFAULT_THRESHOLD = 0.85

# Sentence-boundary candidate for the body fallback: a run of terminal
# punctuation followed by whitespace. Each candidate is then vetted (next
# char must start a new sentence; the preceding token must not be a known
# abbreviation) so ``e.g.`` / ``U.S.`` / ``Dr.`` and decimals don't split.
_BOUNDARY_RE = re.compile(r"[.!?]+\s+")

# Lower-cased abbreviation tokens (sans trailing period) that must NOT end a
# sentence even when followed by a capitalized word.
_ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "e.g", "i.e", "etc", "vs", "dr", "mr", "mrs", "ms", "prof",
        "inc", "ltd", "co", "corp", "u.s", "u.k", "a.m", "p.m",
        "ph.d", "st", "no", "fig", "approx", "jr", "sr", "al",
    }
)


@dataclass
class Occurrence:
    """One claim appearance inside a single wiki entity."""

    claim_text: str
    entity_id: str
    granularity: str  # "footnote" (source claim:) | "sentence" (body fallback)


@dataclass
class Group:
    """A claim restated across >= 2 distinct entities."""

    occurrences: list[Occurrence]
    key: str

    @property
    def entity_count(self) -> int:
        return len({o.entity_id for o in self.occurrences})

    @property
    def representative(self) -> str:
        """A stable representative claim text for the group (truthy)."""
        texts = [o.claim_text for o in self.occurrences if o.claim_text]
        return sorted(texts)[0] if texts else ""


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _footnote_claims(meta: dict[str, object]) -> list[str]:
    """Return the source ``claim:`` texts on an entity (issue #262)."""
    sources = meta.get("sources")
    if not isinstance(sources, list):
        return []
    claims: list[str] = []
    for raw in sources:
        entry = _parse_one_source(raw, fallback_scope="")
        if entry is None:
            continue
        claim = entry.get("claim")
        if isinstance(claim, str) and claim.strip():
            claims.append(claim.strip())
    return claims


def _body_sentences(body: str) -> list[str]:
    """Split a body into trimmed sentences for the coarse fallback unit.

    A boundary only fires when the next character starts a new sentence
    (uppercase or digit) AND the token before the punctuation is not a known
    abbreviation. Decimals (``3.14``) carry no whitespace after the dot, so the
    boundary regex never matches them.
    """
    text = body.strip()
    if not text:
        return []
    sentences: list[str] = []
    start = 0
    for m in _BOUNDARY_RE.finditer(text):
        nxt = text[m.end()] if m.end() < len(text) else ""
        if not (nxt.isupper() or nxt.isdigit()):
            continue
        preceding = text[start : m.start()]
        toks = preceding.split()
        if toks and toks[-1].lower() in _ABBREVIATIONS:
            continue
        sentence = preceding.strip()
        if sentence:
            sentences.append(sentence)
        start = m.end()
    tail = text[start:].strip().rstrip(".!?").strip()
    if tail:
        sentences.append(tail)
    return sentences


def extract_claim_occurrences(wiki_root: Path) -> list[Occurrence]:
    """Scan ``wiki/**`` and return one :class:`Occurrence` per claim.

    Entity discovery reuses :func:`athenaeum.search._iter_wiki_entries`, the
    canonical wiki scan: a SHALLOW pass that excludes ``_``-prefixed files,
    the ``MEMORY.md`` table-of-contents index, and ``_``-prefixed subdirs (a
    shallow scan never descends into them). This keeps "wiki entity" identical
    to what recall / indexing treat as an entity. Inactive entities
    (``superseded_by`` / ``deprecated``, per :func:`is_inactive_memory`) are
    then skipped. Entities with source ``claim:`` footnotes yield ``footnote``
    occurrences; otherwise the body is sentence-split into ``sentence`` ones.
    """
    occurrences: list[Occurrence] = []
    for _fname, path in _iter_wiki_entries(wiki_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = parse_frontmatter(text)
        if not isinstance(meta, dict):
            meta = {}
        if is_inactive_memory(meta):
            continue
        entity_id = path.stem
        claims = _footnote_claims(meta)
        if claims:
            for claim in claims:
                occurrences.append(
                    Occurrence(
                        claim_text=claim,
                        entity_id=entity_id,
                        granularity="footnote",
                    )
                )
        else:
            for sentence in _body_sentences(body):
                occurrences.append(
                    Occurrence(
                        claim_text=sentence,
                        entity_id=entity_id,
                        granularity="sentence",
                    )
                )
    return occurrences


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _group_key(occurrences: list[Occurrence]) -> str:
    """Stable, order-independent key for a grouped claim set (issue #272).

    Hashes the SORTED set of normalized claim texts (same normalization as
    :func:`athenaeum.fingerprint.claim_pair_fingerprint`), so a repeated run
    over the same group yields an identical key regardless of discovery order.

    Note (slice-2 keying consideration): the key hashes CLAIM TEXT ONLY, not
    the entity ids. Two genuinely independent recurring-claim sets that happen
    to share the exact same normalized claim texts would collide on the same
    key. That is fine for this read-only report (identical claim sets ARE the
    same recurrence), but a future slice that persists/links by key across
    runs should fold entity identity in if it needs per-occurrence stability.
    """
    norm = sorted({normalize_side(o.claim_text) for o in occurrences})
    payload = "\n##\n".join(norm).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:16]


def _cosine_matrix(vectors: list[list[float]]):
    """Return an NxN cosine-similarity matrix via numpy, or ``None``.

    Vectorizes the O(N^2 * dim) pairwise cosine into batched numpy ops. Returns
    ``None`` when numpy is unavailable so the caller falls back to the
    pure-Python :func:`athenaeum.cross_scope._cosine`. Zero-norm rows yield 0
    similarity (never group), matching ``_cosine``'s zero-vector contract.
    """
    try:
        import numpy as np  # noqa: PLC0415
    except ImportError:
        return None
    arr = np.asarray(vectors, dtype=float)
    norms = np.linalg.norm(arr, axis=1)
    safe = np.where(norms == 0.0, 1.0, norms)
    normed = arr / safe[:, None]
    sims = normed @ normed.T
    zero = norms == 0.0
    if zero.any():
        sims[zero, :] = 0.0
        sims[:, zero] = 0.0
    return sims


def group_recurring_claims(
    occurrences: list[Occurrence],
    threshold: float,
    embedding_provider: EmbeddingProvider,
) -> list[Group]:
    """Group cross-entity restatements via COMPLETE-LINKAGE clustering.

    Short-circuits (does NOT call ``embedding_provider``) when fewer than two
    occurrences exist. Returns ``[]`` when the provider returns ``None`` (no
    embedding backend).

    Linkage is complete (strictest): an occurrence joins a cluster only when
    its cosine clears ``threshold`` against EVERY current member, so A and C
    are never fused merely because each is similar to a bridging B. A cluster
    is reported only when it spans >= 2 distinct entities, so two restatements
    within one entity never count as a recurrence.
    """
    if len(occurrences) < 2:
        return []
    vectors = embedding_provider([o.claim_text for o in occurrences])
    if vectors is None:
        return []

    n = len(occurrences)
    sims = _cosine_matrix(vectors)
    if sims is not None:
        def _sim(i: int, j: int) -> float:
            return float(sims[i][j])
    else:
        def _sim(i: int, j: int) -> float:
            return _cosine(vectors[i], vectors[j])

    # Greedy complete-linkage: an index joins the first cluster it clears
    # threshold against ALL members of; otherwise it seeds a new cluster.
    # Deterministic given the sorted occurrence order from extraction.
    clusters: list[list[int]] = []
    for i in range(n):
        for cluster in clusters:
            if all(_sim(i, m) >= threshold for m in cluster):
                cluster.append(i)
                break
        else:
            clusters.append([i])

    groups: list[Group] = []
    for members in clusters:
        members_occ = [occurrences[idx] for idx in members]
        if len({o.entity_id for o in members_occ}) < 2:
            continue
        groups.append(Group(occurrences=members_occ, key=_group_key(members_occ)))
    groups.sort(key=lambda g: g.key)
    return groups


def find_recurring_claims(
    wiki_root: Path,
    threshold: float,
    embedding_provider: EmbeddingProvider,
) -> list[Group]:
    """Extract claim occurrences from ``wiki_root`` then group recurrences."""
    occurrences = extract_claim_occurrences(wiki_root)
    return group_recurring_claims(occurrences, threshold, embedding_provider)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_report(
    groups: list[Group],
    threshold: float,
    entities_scanned: int,
) -> str:
    """Render the recurring-claim report as YAML."""
    data = {
        "summary": {
            "recurring_claim_count": len(groups),
            "entities_scanned": entities_scanned,
            "threshold": threshold,
        },
        "recurring_claims": [
            {
                "key": g.key,
                "entity_count": g.entity_count,
                "representative": g.representative,
                "occurrences": [
                    {
                        "entity_id": o.entity_id,
                        "claim_text": o.claim_text,
                        "granularity": o.granularity,
                    }
                    for o in g.occurrences
                ],
            }
            for g in groups
        ],
    }
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
