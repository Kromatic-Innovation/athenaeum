# SPDX-License-Identifier: Apache-2.0
"""Wiki-page dedup pass (issues athenaeum#290, athenaeum#715) — L4 domain/pipeline.

Contract: clusters already-COMPILED wiki pages (not raw intake) against
each other and, for every candidate PAIR inside a cluster, runs the
five-verdict comparator (:mod:`athenaeum.comparator`) and enacts whatever
it decides (:mod:`athenaeum.verdict_effects`) — a ``duplicate`` verdict
writes fold EVIDENCE (never a merged body), ``specialization`` writes
``refines:``, ``contradiction`` routes to supersession-or-queue,
``underdetermined``/``distinct`` are ledger-only. Factoring rule: this
module owns the WIKI-VS-WIKI clustering pass only — it deliberately does
NOT reimplement clustering (reuses ``clusters.cluster_auto_memory_files``)
or verdict decision/enactment (reuses ``comparator``/``verdict_effects``);
it is glue over those two, not a third implementation of either.

**Cut-over (issue athenaeum#715).** Before this issue, this module ran its OWN
duplicate-detection algorithm: the same complete-linkage clustering below,
followed by the ``merge_type_gate`` confidence/complete-linkage/mean-
similarity suppression gates, a deterministic-concatenation draft body
(``merge.synthesize_body``), and a direct write to
``wiki/_pending_merges.md`` (``pending_merges.write_pending_merge``) —
entirely independent of, and running in PARALLEL with, the five-verdict
comparator once it landed (PRs athenaeum#1128/athenaeum#1131). Issue athenaeum#715's own AC
forbids exactly that shape ("the old paths are removed, not left in
parallel"). That old algorithm is DELETED, not merely gated: clustering is
kept (candidate generation — "similarity's only job is proposing pairs"),
but everything downstream of it now runs through the comparator. The
CROSS-CLASS precheck (:func:`athenaeum.merge_type_gate.cross_class_precheck`)
is kept as a pre-comparator filter, NOT deleted with the rest of
``merge_type_gate``'s gates: the comparator's own ``MEMORY_CLASS`` kernel
dimension (:mod:`athenaeum.dimensions`) ships in ``LifecycleState.BACKFILL``,
not ``ENFORCED``, so Gate 1 does not yet consult it — until it does, this is
the only live defense against clustering a policy page with the records it
governs (the PII-hazard shape athenaeum#715's own comments call out).

The whole pass is gated on :func:`athenaeum.config.resolve_comparator_enabled`
— the SAME pre-existing comparator master switch (default OFF), not a new
flag. With it off, this function returns ``[]`` immediately: no proposals,
old-style or new, are produced. This is not a half-cut-over: there is
exactly one implementation (the comparator's), currently dark by the same
knob the rest of the comparator subsystem already ships behind.

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
  actual clustering — this module does NOT reimplement that logic, and
  MUST NOT grow a second copy of it (issue athenaeum#803). Formation is
  complete-linkage: single-linkage connected components are computed
  first as a cheap scoping step, then refined into complete-linkage
  cliques (issue athenaeum#681) so a weak bridging edge can no longer
  chain this wiki-page pass into a giant component either — the SAME
  formation routine the raw-source C1-C4 pass uses, intentionally shared
  rather than forked. See :mod:`athenaeum.clusters` module docstring for
  the formation algorithm itself; do not re-describe or re-derive it
  here. Each candidate wiki page is wrapped as an
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
  not have to duplicate the cosine/complete-linkage code to route around
  that mismatch. When chromadb is unavailable, falls back to the same
  hashing-trick embedder ``clusters.py`` already uses for its own
  no-deps degradation path (imported, not duplicated). Issue athenaeum#1140:
  each page body is chunked (:func:`_chunk_page_text`) and its chunks'
  vectors mean-pooled (:func:`athenaeum.vecmath.mean_pool`) before
  reaching the clusterer -- see :func:`_resolve_wiki_embeddings` -- so
  chromadb's default embedder's 256-token truncation window no longer
  discards everything past a page's lede.
- Every pair within a candidate cluster (``itertools.combinations``) is
  compared independently — clustering only proposes candidates, the
  comparator decides each pair on its own, per athenaeum#715's "similarity is
  never a verdict input" rule.
- Idempotency is the ledger's: :func:`athenaeum.comparator.record_comparison`
  skips a pair whose verdict is already FRESH before spending an LLM call —
  this module does not re-derive that check.

Out of scope (deliberate):

- Real-time re-clustering triggered by a single page edit (issue athenaeum#290
  scope) or applying/enacting a duplicate fold (a separate, future child of
  athenaeum#709 — this module only ever produces evidence, never a merged page).
- Retroactively re-clustering already ``archived``/``superseded_by`` pages.

Layering (L4 domain/pipeline). ``wiki_dedupe.py`` imports
``athenaeum.comparator`` / ``athenaeum.verdict_effects`` at module TOP level
— normal downward dependencies; neither imports this module back. After
issue athenaeum#545 dissolved the librarian-centered named-8 coupling,
``wiki_dedupe.py`` is NOT part of any import SCC. ``librarian.py`` calls
into this module only via its own deferred import (a one-way edge).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.authority import is_pointer_stub
from athenaeum.clusters import (
    EMBEDDER_CHROMADB_DEFAULT,
    EMBEDDER_FALLBACK_HASHING,
    Cluster,
    _fallback_embeddings,
    cluster_auto_memory_files,
    resolve_cluster_threshold,
)
from athenaeum.comparator import (
    flush_content_relation_unavailable_warning,
    page_from_path,
    record_comparison,
)
from athenaeum.config import load_config, resolve_comparator_enabled, resolve_heartbeat_interval
from athenaeum.merge_type_gate import cross_class_precheck
from athenaeum.models import AutoMemoryFile, TokenUsage, parse_frontmatter, validity_bound_str
from athenaeum.pii import is_pii_flagged
from athenaeum.progress import PhaseHeartbeat
from athenaeum.runlock import RunLock
from athenaeum.search import embed_texts
from athenaeum.storage import is_merge_eligible
from athenaeum.vecmath import mean_pool
from athenaeum.verdict_effects import apply_verdict_effect

if TYPE_CHECKING:  # pragma: no cover - typing only
    from athenaeum.provider import LLMBackend

log = logging.getLogger(__name__)

# The page types this pass considers. Person wikis, ``wiki/auto-*.md``
# cluster outputs (already covered by C1-C4), and any other entity type
# are out of scope for the MVP (issue athenaeum#290 acceptance criteria).
DEDUPE_CANDIDATE_TYPES: frozenset[str] = frozenset(
    {"concept", "reference", "principle"}
)

# Marks a page's synthetic scope on the shared AutoMemoryFile shape —
# distinguishes wiki-page clusters from raw-intake clusters in log lines
# and cluster ids (``derive_topic_slug`` / ``cluster_id`` prefixing).
WIKI_ORIGIN_SCOPE = "wiki"

EmbeddingProvider = Callable[[list[str]], "list[list[float]] | None"]

# Issue athenaeum#1032: one-time WARNING flag — ``_resolve_wiki_embeddings`` can be
# called many times across a run (once per ``find_wiki_page_clusters`` /
# ``run_shadow_linkage`` call); fires at most once per process so a run that
# repeatedly hits the no-chromadb path doesn't spam the log.
_WIKI_FALLBACK_WARNED = False

# Issue athenaeum#1243: the wiki-dedupe suppression ledger (athenaeum#1142) has no
# producer under the comparator path — re-site before flipping
# comparator_enabled on.
# Issue athenaeum#1140 (AC1): chromadb's default ONNX MiniLM embedding function
# hard-codes ``tokenizer.enable_truncation(max_length=256)`` — content past
# that window is invisible to the embedder, and the corpus writes
# structurally uniform ledes, so truncation-exposed pages collapse into
# dense, unrelated cliques (full measurement trail in the issue). Chunking
# each page body into pieces no larger than ``_CHUNK_CHARS`` characters
# before embedding — then mean-pooling the per-chunk vectors
# (:func:`athenaeum.vecmath.mean_pool`) — lets body content past the first
# chunk reach the final vector instead of being silently discarded.
#
# ``_CHUNK_CHARS`` is a CHARACTER budget, deliberately NOT derived from the
# installed chromadb package's token-truncation constant (the issue's own
# "Not verified" section warns that constant was read from the installed
# package, not confirmed stable across chromadb versions, and this
# module's remedy should not assume it is). Measured against the live
# ``/knowledge`` corpus (issue athenaeum#1140 PR): the empirical char-length at
# which ~57% of eligible pages are exceeded (matching the issue's own 57%
# figure) is ~1,200 characters. 900 is chosen with a ~300-character margin
# below that measured boundary so a chunk is never truncated by the real
# tokenizer even if the exact 256-token constant drifts modestly across a
# chromadb upgrade — the fix degrades gracefully (a slightly-tighter
# tokenizer would still cut chunks well before their content is
# meaningfully lost) rather than silently reintroducing the defect. Not
# exposed as a config knob (issue athenaeum#1140's own guidance: "do not add a
# knob you cannot test") — this constant is a technical implementation
# detail of the chunking strategy, not a tunable policy like
# ``cluster_threshold``.
_CHUNK_CHARS = 900


def _chunk_page_text(text: str, *, chunk_chars: int = _CHUNK_CHARS) -> list[str]:
    """Split *text* into whitespace-joined chunks of at most *chunk_chars*.

    Word-based accumulation (never splits inside a word): words are packed
    into a chunk until the next word would push it over ``chunk_chars``,
    then a new chunk starts. Internal whitespace (including newlines) is
    normalized to single spaces — irrelevant for an embedding call, which
    only reads the token stream, not the page's original formatting.

    Always returns at least one chunk (``[""]`` for empty/whitespace-only
    input) so a caller can zip chunks 1:1 back to the file that produced
    them without a length mismatch. A page whose content already fits in
    one chunk returns a single-element list containing exactly that page's
    normalized full text — i.e. this is a no-op for any page short enough
    that the pre-athenaeum#1140 whole-page embedding never truncated it, which is
    exactly what keeps every existing (short-body) test fixture's stub
    embedding-provider call unaffected by this change.
    """
    words = text.split()
    if not words:
        return [""]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        added_len = len(word) + (1 if current else 0)
        if current and current_len + added_len > chunk_chars:
            chunks.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += added_len
    if current:
        chunks.append(" ".join(current))
    return chunks or [""]


def discover_wiki_dedupe_candidates(
    wiki_root: Path,
    *,
    config: dict[str, Any] | None = None,
) -> list[AutoMemoryFile]:
    """Load ``wiki/*.md`` pages eligible for the dedup pass.

    Eligible: top-level ``wiki/<slug>.md`` files (not ``_pending_*.md``
    sidecars, not ``wiki/auto-*.md`` C1-C4 cluster outputs, not any
    subdirectory) whose frontmatter ``type`` is in
    :data:`DEDUPE_CANDIDATE_TYPES`. Excludes pages tagged ``archived`` or
    carrying a truthy ``superseded_by`` key — those are already-resolved
    and must not be re-flagged. Also excludes pages carrying a truthy
    ``pointer_stub`` flag (issue athenaeum#426) — a stub already points at its
    authoritative live source and is not merge-eligible (stub hygiene). Also
    excludes pages carrying a truthy ``pii`` flag (issue athenaeum#427) —
    belt-and-suspenders: a page an operator has hand-flagged as carrying PII
    inline is never proposed as a merge source, even when it is not (yet)
    routed to the excluded storage surface.

    When *config* is provided, the storage-adapter layer (athenaeum#429) is also
    consulted: a page whose entity class resolves to a surface with
    ``merge_eligible=False`` is dropped even if it sits in ``wiki/`` and its
    ``type`` is a dedup-candidate type — fail-closed defense-in-depth so a
    class an operator routed to an excluded surface can never be proposed for a
    merge. ``config=None`` (the default) skips this consult entirely, so
    behavior is byte-identical to the pre-athenaeum#429 pass for any caller that does not
    thread config through.

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
        # athenaeum#429: honor the storage-adapter corpus policy — a class routed to a
        # non-merge-eligible surface is dropped (fail-closed). No-op by default
        # (every class maps to the wiki surface, merge_eligible=True).
        if config is not None and not is_merge_eligible(page_type, config):
            continue
        # athenaeum#427: belt-and-suspenders — a hand-flagged ``pii: true`` page is
        # never a merge source, independent of the storage-adapter policy.
        if is_pii_flagged(meta):
            continue

        tags_raw = meta.get("tags") or []
        tags = [str(t).lower() for t in tags_raw] if isinstance(tags_raw, list) else []
        if "archived" in tags:
            continue
        if meta.get("superseded_by"):
            continue
        # Issue athenaeum#426: a pointer stub already points at ITS authoritative live
        # source and contributes nothing beyond that one line — it must never
        # be proposed as a merge source (stub hygiene).
        if is_pointer_stub(meta):
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
                # Issue athenaeum#308: populate temporal bounds for consistency with the
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
) -> tuple[dict[str, list[float]], dict[str, str]]:
    """Embed candidate page bodies, falling back to the hashing trick.

    ``embedding_provider`` defaults to :func:`athenaeum.search.embed_texts`
    (real MiniLM embeddings via chromadb). Tests should inject a stub —
    same convention as :func:`athenaeum.recurring_claims.group_recurring_claims`
    — so the suite never depends on chromadb being installed. When the
    provider returns ``None`` (chromadb absent / embedding call failed),
    falls back to :func:`athenaeum.clusters._fallback_embeddings` — the
    exact no-deps degradation path ``clusters.py`` already ships, reused
    here rather than reimplemented.

    Returns ``(embeddings, sources)`` (issue athenaeum#1032) — ``sources`` maps every
    key in ``embeddings`` to :data:`athenaeum.clusters.EMBEDDER_CHROMADB_DEFAULT`
    or :data:`athenaeum.clusters.EMBEDDER_FALLBACK_HASHING`, uniformly (this
    resolver is all-or-nothing: either every candidate embeds via ``provider``
    or every candidate falls back together), so
    :func:`athenaeum.clusters.cluster_auto_memory_files` can record which
    embedder produced each formed cluster's vectors.

    Issue athenaeum#1140 (AC1): each candidate's body is split into
    :data:`_CHUNK_CHARS`-sized chunks (:func:`_chunk_page_text`) BEFORE
    calling ``provider`` — a single batched call embeds every chunk from
    every candidate — and each candidate's final vector is the
    mean-pooled, re-normalized combination of its own chunks'
    vectors (:func:`athenaeum.vecmath.mean_pool`). This is scoped to the
    wiki-dedupe path only: :func:`athenaeum.search.embed_texts` itself is
    NOT modified, so every other caller of that shared function
    (:mod:`athenaeum.fingerprint`, :mod:`athenaeum.tiers`,
    :mod:`athenaeum.clusters`'s own raw-intake embedding resolution,
    :mod:`athenaeum._cmd_curate`) is byte-for-byte unaffected — see the PR
    body for the blast-radius survey. A page short enough to fit in one
    chunk is embedded exactly as before (one call, one vector,
    mean-pool-of-one is a re-normalize no-op), so this is a pure ADDITION
    of body content for the long-page case, not a behavior change for the
    short-page case.

    The fallback path (:func:`athenaeum.clusters._fallback_embeddings`,
    engaged when ``provider`` returns ``None``/a mismatched-length result)
    is deliberately NOT chunked — it is a hashing-trick bag-of-words
    embedder with no token window to truncate against (dead end #3 in the
    issue: 16,083/16,083 production wiki suppressions were already
    ``chromadb-default``, never ``fallback-hashing``), so chunking it would
    add cost with no defect to fix.
    """
    if not files:
        return {}, {}
    provider = embedding_provider or embed_texts

    chunk_lists = [_chunk_page_text(am.content) for am in files]
    flat_chunks = [chunk for chunks in chunk_lists for chunk in chunks]
    vectors = provider(flat_chunks)
    if vectors is None or len(vectors) != len(flat_chunks):
        # Issue athenaeum#1032: one-time WARNING — this fallback used to engage with
        # no log call at all, so athenaeum#1005's over-cluster diagnosis had no way to
        # tell from run artifacts whether the hashing-trick embedder (rather
        # than real MiniLM vectors) produced a cluster's similarity scores.
        _warn_wiki_fallback_engaged_once(len(files))
        fallback = _fallback_embeddings(files)
        sources = {str(am.path): EMBEDDER_FALLBACK_HASHING for am in files}
        return fallback, sources

    embeddings: dict[str, list[float]] = {}
    offset = 0
    for am, chunks in zip(files, chunk_lists):
        n_chunks = len(chunks)
        chunk_vectors = [
            list(map(float, vec)) for vec in vectors[offset : offset + n_chunks]
        ]
        offset += n_chunks
        embeddings[str(am.path)] = mean_pool(chunk_vectors)
    sources = {str(am.path): EMBEDDER_CHROMADB_DEFAULT for am in files}
    return embeddings, sources


def _warn_wiki_fallback_engaged_once(n_candidates: int) -> None:
    """One-time WARNING (issue athenaeum#1032) when the wiki-dedupe pass falls back
    to the hashing-trick embedder. See ``_resolve_wiki_embeddings`` above."""
    global _WIKI_FALLBACK_WARNED
    if _WIKI_FALLBACK_WARNED:
        return
    _WIKI_FALLBACK_WARNED = True
    log.warning(
        "wiki-page dedup: embed_texts produced no usable vectors for %d "
        "candidate page(s); falling back to the fallback-hashing embedder "
        "(clusters._fallback_embeddings) to produce them (issue athenaeum#1032)",
        n_candidates,
    )


def find_wiki_page_clusters(
    wiki_root: Path,
    *,
    threshold: float,
    embedding_provider: EmbeddingProvider | None = None,
    config: dict[str, Any] | None = None,
) -> list[Cluster]:
    """Cluster eligible wiki pages; returns only clusters of size >= 2.

    Singletons are dropped here (unlike the raw auto-memory C2 pass,
    which returns them for a uniform report shape) — the wiki-page pass
    only ever acts on candidate duplicates, so a size-1 "cluster" carries
    no signal a caller needs.

    *config* is threaded through to :func:`discover_wiki_dedupe_candidates` so
    the storage-adapter merge-eligibility policy (athenaeum#429) is honored; ``None``
    keeps behavior byte-identical.
    """
    files = discover_wiki_dedupe_candidates(wiki_root, config=config)
    if len(files) < 2:
        return []

    embeddings, embedder_sources = _resolve_wiki_embeddings(
        files, embedding_provider=embedding_provider
    )
    clusters = cluster_auto_memory_files(
        files,
        extra_roots=[wiki_root],
        threshold=threshold,
        embeddings=embeddings,
        embedder_sources=embedder_sources,
    )
    return [c for c in clusters if len(c.member_paths) >= 2]


def propose_wiki_page_merges(
    knowledge_root: Path,
    *,
    config: dict[str, Any] | None = None,
    threshold: float | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    dry_run: bool = False,
    client: "LLMBackend | None" = None,
    usage: TokenUsage | None = None,
    lock: RunLock | None = None,
) -> list[dict[str, Any]]:
    """Compare candidate duplicate wiki pages via the five-verdict comparator.

    For each cluster of size >= 2 above the configured cluster-cohesion
    threshold, every PAIR within the cluster (``itertools.combinations``) is
    compared independently via :func:`athenaeum.comparator.record_comparison`
    — clustering only proposes candidates (issue athenaeum#715: "similarity's
    only job is proposing pairs"); it never decides a verdict itself. A
    decided, non-fresh verdict is enacted via
    :func:`athenaeum.verdict_effects.apply_verdict_effect` (a ``duplicate``
    verdict writes fold EVIDENCE, never a merged body — see that module's
    docstring for all five branches).

    Dark by default: returns ``[]`` immediately unless
    :func:`athenaeum.config.resolve_comparator_enabled` is true — the SAME
    knob the rest of the comparator subsystem gates on (issue athenaeum#715's
    cut-over deliberately does not introduce a second flag).

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
        dry_run: When True, every pair is compared via
            :func:`athenaeum.comparator.compare_pages` directly (no ledger
            write, no memoization, no enacted effect) and the WOULD-BE
            verdicts are returned for preview.
        client: Optional live LLM client for the comparator's Gate 2
            (:func:`athenaeum.comparator.content_relation`). ``None``
            degrades to Gate-1-only pairs reporting a verdict; a pair Gate 1
            cannot settle reports no verdict (never a fabricated one).
        usage: Optional run-level :class:`~athenaeum.models.TokenUsage` so
            the comparator's Gate 2 calls are threaded into the run budget.
        lock: The caller's already-acquired :class:`~athenaeum.runlock.RunLock`
            — required (and used) for every non-dry-run comparison, since
            ledgering a verdict is a single-appender write. When ``None`` and
            *dry_run* is also ``False``, this pass is skipped entirely
            (logged, never raised) rather than comparing without a lock.

    Returns:
        A list of dicts, one per pair this call actually decided a verdict
        for (fresh/memoized pairs are silently skipped — nothing new
        happened for them): ``pair`` (the ledger pair key), ``verdict``,
        ``action`` (the :class:`~athenaeum.verdict_effects.EffectResult`
        action token; omitted in dry-run, which never enacts anything), and
        ``sources`` (the two absolute page paths).
    """
    resolved_config = config if config is not None else load_config(knowledge_root)
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        return []

    if not resolve_comparator_enabled(resolved_config):
        return []

    resolved_threshold = (
        threshold
        if threshold is not None
        else resolve_cluster_threshold(knowledge_root, resolved_config)
    )

    files = discover_wiki_dedupe_candidates(wiki_root, config=resolved_config)
    by_relpath: dict[str, AutoMemoryFile] = {}
    for am in files:
        try:
            relpath = am.path.resolve().relative_to(wiki_root.resolve()).as_posix()
        except ValueError:
            relpath = am.path.name
        by_relpath[relpath] = am

    clusters = find_wiki_page_clusters(
        wiki_root,
        threshold=resolved_threshold,
        embedding_provider=embedding_provider,
        config=resolved_config,
    )

    # Issue athenaeum#398: this pass (athenaeum#290) is one of the post-compile dark zones —
    # a wedge examining a candidate cluster previously produced zero log
    # output. Emit start/done even with zero candidate clusters so a
    # watchdog can see the phase ran.
    heartbeat_interval = resolve_heartbeat_interval(resolved_config)
    heartbeat = PhaseHeartbeat(
        "wiki-dedupe", total=len(clusters), interval_s=heartbeat_interval
    )
    heartbeat.start()
    if not clusters:
        heartbeat.done()
        return []

    if not dry_run and lock is None:
        log.warning(
            "wiki-page dedup: comparator pass skipped — no RunLock held and "
            "not a dry-run (ledgering a verdict requires the caller's "
            "already-acquired lock)"
        )
        heartbeat.done()
        return []

    results: list[dict[str, Any]] = []

    for cluster in clusters:
        heartbeat.tick(cluster.cluster_id)
        members = [
            by_relpath[relpath]
            for relpath in cluster.member_paths
            if relpath in by_relpath
        ]
        if len(members) < 2:
            continue

        for am_a, am_b in combinations(members, 2):
            path_a, path_b = am_a.path.resolve(), am_b.path.resolve()
            sources = [str(path_a), str(path_b)]

            # Issue athenaeum#433: type-compatibility precheck, kept as a
            # pre-comparator filter (see module docstring) since the
            # comparator's own MEMORY_CLASS dimension is not yet ENFORCED.
            # A cross-class pair is skipped entirely — no proposal, no
            # ledger entry, no LLM call.
            rejection = cross_class_precheck(sources)
            if rejection is not None:
                log.info(
                    "wiki-page dedup: cross-class pair skipped (%s): %s / %s",
                    rejection.reason,
                    path_a,
                    path_b,
                )
                continue

            try:
                page_a = page_from_path(path_a)
                page_b = page_from_path(path_b)
            except (OSError, UnicodeDecodeError) as exc:
                log.warning(
                    "wiki-page dedup: could not read pair for comparison "
                    "(%s / %s): %s",
                    path_a,
                    path_b,
                    exc,
                )
                continue

            if dry_run:
                from athenaeum.comparator import compare_pages

                dry_outcome = compare_pages(
                    page_a, page_b, client=client, config=resolved_config, usage=usage
                )
                if dry_outcome.verdict is not None:
                    results.append(
                        {
                            "pair": "|".join(sorted((page_a.id, page_b.id))),
                            "verdict": dry_outcome.verdict,
                            "sources": sources,
                        }
                    )
                continue

            assert lock is not None  # guarded above
            record = record_comparison(
                wiki_root,
                page_a,
                page_b,
                client=client,
                config=resolved_config,
                usage=usage,
                lock=lock,
            )
            if not record["ok"] or record.get("skipped") == "fresh":
                continue
            outcome = record.get("outcome")
            if outcome is None:
                continue
            effect = apply_verdict_effect(
                page_a,
                page_b,
                outcome,
                wiki_root=wiki_root,
                path_a=path_a,
                path_b=path_b,
                config=resolved_config,
            )
            results.append(
                {
                    "pair": record["pair"],
                    "verdict": record["verdict"],
                    "action": effect.action,
                    "sources": sources,
                }
            )
            log.info(
                "wiki-page dedup: comparator verdict=%s action=%s for %s",
                record["verdict"],
                effect.action,
                record["pair"],
            )

    # Issue athenaeum#1245: summarize (rather than lose) every content_relation
    # "no LLM client / call failed" occurrence this pass into ONE WARNING with the
    # affected-pair count, instead of one unattributable WARNING per pair.
    flush_content_relation_unavailable_warning()
    heartbeat.done()
    return results
