# SPDX-License-Identifier: Apache-2.0
"""Wiki-page dedup pass (issue #290).

The C1-C4 auto-memory pipeline (:mod:`athenaeum.clusters`,
:mod:`athenaeum.merge`) only ever clusters ``raw/auto-memory/*.md`` intake
against itself. It never compares already-compiled entity-topic wiki pages
(``wiki/<slug>.md``) against EACH OTHER — so nothing catches the case where
the same recurring question gets answered by a brand-new standalone wiki
page every time instead of updating the existing one.

This module closes that gap for a narrow, deliberately-scoped page class:
``concept`` / ``reference`` / ``principle`` wiki pages (excludes person
wikis, ``_pending_*.md`` sidecars, and ``wiki/auto-*.md`` cluster outputs,
which already go through C1-C4). Already-resolved pages (``archived`` tag
or a ``superseded_by`` frontmatter key) are excluded — they don't need to
be re-flagged.

Design (see PR body for the full rationale):

- Reuses :func:`athenaeum.clusters.cluster_auto_memory_files` for the
  actual single-linkage/cosine clustering — this module does NOT
  reimplement that logic. Each candidate wiki page is wrapped as an
  :class:`~athenaeum.models.AutoMemoryFile` (the dataclass already has
  exactly the shape clustering needs: ``path``, ``origin_scope``,
  ``name``, ``description``, ``content``) so the existing clustering
  code path runs unmodified.
- Embeddings are resolved directly via :func:`athenaeum.search.embed_texts`
  (the same chromadb MiniLM embedder the recall vector index uses) rather
  than through the raw-intake extra-root id-matching scheme in
  ``clusters._resolve_embeddings`` — wiki pages are indexed under a
  different id shape (bare filename, no root prefix) than
  ``raw/auto-memory`` extra-root entries, so that lookup path would never
  hit. ``cluster_auto_memory_files`` grew an ``embeddings=`` override
  (clusters.py) specifically so this precomputed-embeddings caller does
  not have to duplicate the cosine/single-linkage code to route around
  that mismatch. When chromadb is unavailable, falls back to the same
  hashing-trick embedder ``clusters.py`` already uses for its own
  no-deps degradation path (imported, not duplicated).
- Draft synthesis reuses :func:`athenaeum.merge.synthesize_body` (C3's
  deterministic concatenate-with-paragraph-dedupe strategy) and
  :func:`athenaeum.merge.derive_topic_slug` (C3's topic-slug heuristic) —
  no new synthesis strategy, per the issue's explicit scope note.
- Idempotency: proposals are appended via
  :func:`athenaeum.pending_merges.write_pending_merge`, which already
  skips a re-append when a block with the same source-set + target-name
  id exists (resolved or not) in ``wiki/_pending_merges.md`` — this
  module does not re-derive that stability logic.

Out of scope (deliberate — see issue #290):

- LLM-based draft synthesis / rich merge rationale.
- Real contradiction detection beyond the existing cohesion threshold.
- Retroactively re-clustering already ``archived``/``superseded_by`` pages.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from athenaeum.clusters import (
    Cluster,
    _fallback_embeddings,
    cluster_auto_memory_files,
    resolve_cluster_threshold,
)
from athenaeum.config import load_config
from athenaeum.merge import derive_topic_slug, synthesize_body
from athenaeum.models import AutoMemoryFile, parse_frontmatter, validity_bound_str
from athenaeum.pending_merges import write_pending_merge
from athenaeum.search import embed_texts

log = logging.getLogger(__name__)

# The page types this pass considers. Person wikis, ``wiki/auto-*.md``
# cluster outputs (already covered by C1-C4), and any other entity type
# are out of scope for the MVP (issue #290 acceptance criteria).
DEDUPE_CANDIDATE_TYPES: frozenset[str] = frozenset(
    {"concept", "reference", "principle"}
)

# Marks a page's synthetic scope on the shared AutoMemoryFile shape —
# distinguishes wiki-page clusters from raw-intake clusters in log lines
# and cluster ids (``derive_topic_slug`` / ``cluster_id`` prefixing).
WIKI_ORIGIN_SCOPE = "wiki"

EmbeddingProvider = Callable[[list[str]], "list[list[float]] | None"]


def discover_wiki_dedupe_candidates(wiki_root: Path) -> list[AutoMemoryFile]:
    """Load ``wiki/*.md`` pages eligible for the dedup pass.

    Eligible: top-level ``wiki/<slug>.md`` files (not ``_pending_*.md``
    sidecars, not ``wiki/auto-*.md`` C1-C4 cluster outputs, not any
    subdirectory) whose frontmatter ``type`` is in
    :data:`DEDUPE_CANDIDATE_TYPES`. Excludes pages tagged ``archived`` or
    carrying a truthy ``superseded_by`` key — those are already-resolved
    and must not be re-flagged.

    Returns records sorted by filename for deterministic ordering.
    """
    if not wiki_root.is_dir():
        return []

    candidates: list[AutoMemoryFile] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        if path.name.startswith("auto-"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = parse_frontmatter(text)
        if not isinstance(meta, dict) or not meta:
            continue
        page_type = str(meta.get("type") or "")
        if page_type not in DEDUPE_CANDIDATE_TYPES:
            continue

        tags_raw = meta.get("tags") or []
        tags = [str(t).lower() for t in tags_raw] if isinstance(tags_raw, list) else []
        if "archived" in tags:
            continue
        if meta.get("superseded_by"):
            continue

        name = str(meta.get("name") or path.stem)
        description = str(meta.get("description") or "")
        candidates.append(
            AutoMemoryFile(
                path=path,
                origin_scope=WIKI_ORIGIN_SCOPE,
                memory_type=page_type,
                name=name,
                description=description,
                # Issue #308: populate temporal bounds for consistency with the
                # other AutoMemoryFile construction sites and to close a latent
                # lockstep gap should this record ever reach is_inactive().
                valid_from=validity_bound_str(meta, "valid_from"),
                valid_until=validity_bound_str(meta, "valid_until"),
                _content=body,
            )
        )
    return candidates


def _resolve_wiki_embeddings(
    files: Sequence[AutoMemoryFile],
    *,
    embedding_provider: EmbeddingProvider | None,
) -> dict[str, list[float]]:
    """Embed candidate page bodies, falling back to the hashing trick.

    ``embedding_provider`` defaults to :func:`athenaeum.search.embed_texts`
    (real MiniLM embeddings via chromadb). Tests should inject a stub —
    same convention as :func:`athenaeum.recurring_claims.group_recurring_claims`
    — so the suite never depends on chromadb being installed. When the
    provider returns ``None`` (chromadb absent / embedding call failed),
    falls back to :func:`athenaeum.clusters._fallback_embeddings` — the
    exact no-deps degradation path ``clusters.py`` already ships, reused
    here rather than reimplemented.
    """
    if not files:
        return {}
    provider = embedding_provider or embed_texts
    texts = [am.content for am in files]
    vectors = provider(texts)
    if vectors is None or len(vectors) != len(files):
        return _fallback_embeddings(files)
    return {str(am.path): list(map(float, vec)) for am, vec in zip(files, vectors)}


def find_wiki_page_clusters(
    wiki_root: Path,
    *,
    threshold: float,
    embedding_provider: EmbeddingProvider | None = None,
) -> list[Cluster]:
    """Cluster eligible wiki pages; returns only clusters of size >= 2.

    Singletons are dropped here (unlike the raw auto-memory C2 pass,
    which returns them for a uniform report shape) — the wiki-page pass
    only ever acts on candidate duplicates, so a size-1 "cluster" carries
    no signal a caller needs.
    """
    files = discover_wiki_dedupe_candidates(wiki_root)
    if len(files) < 2:
        return []

    embeddings = _resolve_wiki_embeddings(files, embedding_provider=embedding_provider)
    clusters = cluster_auto_memory_files(
        files,
        extra_roots=[wiki_root],
        threshold=threshold,
        embeddings=embeddings,
    )
    return [c for c in clusters if len(c.member_paths) >= 2]


def _member_bodies_for_cluster(
    cluster: Cluster,
    by_relpath: dict[str, AutoMemoryFile],
) -> list[tuple[str, str, str]]:
    """Build the ``(scope, filename, body)`` triples ``synthesize_body`` wants."""
    triples: list[tuple[str, str, str]] = []
    for relpath in cluster.member_paths:
        am = by_relpath.get(relpath)
        if am is None:
            continue
        triples.append((WIKI_ORIGIN_SCOPE, am.path.name, am.content))
    return triples


def propose_wiki_page_merges(
    knowledge_root: Path,
    *,
    config: dict[str, Any] | None = None,
    threshold: float | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Find duplicate-topic wiki pages and propose merges for human review.

    For each cluster of size >= 2 above the configured cluster-cohesion
    threshold, checks whether a merge proposal for this exact source set
    already exists (resolved or not) in ``wiki/_pending_merges.md`` —
    reusing :func:`athenaeum.pending_merges.write_pending_merge`'s own
    idempotency check (source-set + target-name id stability) rather than
    re-deriving it here — and appends a new proposal when none does.

    Args:
        knowledge_root: Root of the knowledge directory (``wiki/`` lives
            at ``knowledge_root / "wiki"``).
        config: Optional resolved config dict (loaded lazily otherwise).
        threshold: Optional cosine-similarity override. Defaults to
            :func:`athenaeum.clusters.resolve_cluster_threshold` — the
            SAME threshold the raw auto-memory C2 pass uses, per the
            issue's explicit "don't invent a new threshold" scope note.
        embedding_provider: Optional embedder override — see
            :func:`_resolve_wiki_embeddings`. Tests inject a stub.
        dry_run: When True, returns the proposals that WOULD be written
            without touching ``wiki/_pending_merges.md``.

    Returns:
        A list of dicts (one per NEWLY-written or would-be-written
        proposal this call) with ``merge_target_name``, ``sources``,
        ``rationale``, and ``confidence`` — for CLI reporting and tests.
        Proposals already present from a prior run are silently skipped
        (not returned) since nothing new happened for them.
    """
    resolved_config = config if config is not None else load_config(knowledge_root)
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        return []

    resolved_threshold = (
        threshold
        if threshold is not None
        else resolve_cluster_threshold(knowledge_root, resolved_config)
    )

    files = discover_wiki_dedupe_candidates(wiki_root)
    by_relpath: dict[str, AutoMemoryFile] = {}
    for am in files:
        try:
            relpath = am.path.resolve().relative_to(wiki_root.resolve()).as_posix()
        except ValueError:
            relpath = am.path.name
        by_relpath[relpath] = am

    clusters = find_wiki_page_clusters(
        wiki_root, threshold=resolved_threshold, embedding_provider=embedding_provider
    )
    if not clusters:
        return []

    merges_path = wiki_root / "_pending_merges.md"
    proposals: list[dict[str, Any]] = []

    for cluster in clusters:
        members = [
            by_relpath[relpath]
            for relpath in cluster.member_paths
            if relpath in by_relpath
        ]
        if len(members) < 2:
            continue

        # Sources are absolute paths — wiki pages (unlike raw auto-memory
        # intake) are not retired/moved by any downstream pass, so these
        # stay stable across runs and are safe to use as the id-stability
        # key inside write_pending_merge.
        sources = [str(am.path.resolve()) for am in members]
        merge_target_name = derive_topic_slug(cluster.member_paths, cluster.cluster_id)
        member_bodies = _member_bodies_for_cluster(cluster, by_relpath)
        draft_body = synthesize_body(member_bodies)
        rationale = (
            f"{len(members)} wiki pages cluster on the same topic "
            f"({cluster.rationale})"
        )

        # Check idempotency BEFORE branching on dry_run — otherwise a
        # dry-run preview reports proposals a real run would silently
        # skip as already-present, which contradicts dry-run's own
        # "what would a real run do" framing (Quine review of #293).
        from athenaeum.pending_merges import _make_id, parse_pending_merges

        existing_ids = {pm.id for pm in parse_pending_merges(merges_path)}
        block_id = _make_id(sources, merge_target_name)
        if block_id in existing_ids:
            log.debug(
                "wiki-page dedup: proposal %s already present; skipping", block_id
            )
            continue

        proposal = {
            "merge_target_name": merge_target_name,
            "sources": sources,
            "rationale": rationale,
            "draft_merged_body": draft_body,
            "confidence": cluster.centroid_score,
        }

        if dry_run:
            proposals.append(proposal)
            continue

        write_pending_merge(
            merges_path,
            merge_target_name=merge_target_name,
            sources=sources,
            rationale=rationale,
            draft_merged_body=draft_body,
            confidence=cluster.centroid_score,
        )
        proposals.append(proposal)
        log.info(
            "wiki-page dedup: proposed merge %r covering %d page(s)",
            merge_target_name,
            len(members),
        )

    return proposals
