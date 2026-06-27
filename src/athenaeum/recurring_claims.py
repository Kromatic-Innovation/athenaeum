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
provider (``list[str] -> list[vec] | None``) so the suite stays offline. Two
occurrences group when their pairwise cosine clears ``threshold`` AND they
live in DIFFERENT entities — two restatements within one entity are not a
cross-entity recurrence. A group is only reported when it spans >= 2 distinct
entities. The group key is a stable, order-independent hash of the grouped
claim set (mirrors :func:`athenaeum.fingerprint.claim_pair_fingerprint`), so
repeated runs over an unchanged wiki yield identical keys.
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

# Injected embedding provider: maps a list of texts to a list of vectors, or
# returns None when no embedding backend is available (graceful degradation).
EmbeddingProvider = Callable[[list[str]], "list[list[float]] | None"]

# Default cosine cutoff. Matches the cross_scope similarity convention (0.85).
DEFAULT_THRESHOLD = 0.85

# Sentence splitter for the body fallback: break after ``.``/``!``/``?`` runs.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


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
    """Split a body into trimmed sentences for the coarse fallback unit."""
    out: list[str] = []
    for chunk in _SENTENCE_SPLIT_RE.split(body.strip()):
        sentence = chunk.strip().rstrip(".!?").strip()
        if sentence:
            out.append(sentence)
    return out


def extract_claim_occurrences(wiki_root: Path) -> list[Occurrence]:
    """Scan ``wiki/**`` and return one :class:`Occurrence` per claim.

    Skips ``_``-prefixed metadata files and inactive entities
    (``superseded_by`` / ``deprecated``, per :func:`is_inactive_memory`).
    Entities with source ``claim:`` footnotes yield ``footnote`` occurrences;
    otherwise the body is sentence-split into ``sentence`` occurrences.
    """
    if not wiki_root.exists():
        return []
    occurrences: list[Occurrence] = []
    for path in sorted(wiki_root.rglob("*.md")):
        if not path.is_file() or path.name.startswith("_"):
            continue
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
    """
    norm = sorted({normalize_side(o.claim_text) for o in occurrences})
    payload = "\n##\n".join(norm).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:16]


def group_recurring_claims(
    occurrences: list[Occurrence],
    threshold: float,
    embedding_provider: EmbeddingProvider,
) -> list[Group]:
    """Group cross-entity restatements whose pairwise cosine >= ``threshold``.

    Short-circuits (does NOT call ``embedding_provider``) when fewer than two
    occurrences exist. Returns ``[]`` when the provider returns ``None`` (no
    embedding backend). Edges are only drawn between DIFFERENT entities, so two
    restatements within one entity never form a recurrence; a group is reported
    only when it spans >= 2 distinct entities.
    """
    if len(occurrences) < 2:
        return []
    vectors = embedding_provider([o.claim_text for o in occurrences])
    if vectors is None:
        return []

    n = len(occurrences)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if occurrences[i].entity_id == occurrences[j].entity_id:
                continue
            if _cosine(vectors[i], vectors[j]) >= threshold:
                union(i, j)

    components: dict[int, list[int]] = {}
    for idx in range(n):
        components.setdefault(find(idx), []).append(idx)

    groups: list[Group] = []
    for members in components.values():
        members_occ = [occurrences[i] for i in members]
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
