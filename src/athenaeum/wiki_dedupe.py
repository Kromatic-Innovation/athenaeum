# SPDX-License-Identifier: Apache-2.0
"""Wiki-page dedup pass (issue athenaeum#290) — L4 domain/pipeline.

Contract: clusters already-COMPILED wiki pages (not raw intake) against
each other and appends a merge proposal to ``wiki/_pending_merges.md``
when two-or-more concept/reference/principle pages look like duplicates.
Factoring rule: this module owns the WIKI-VS-WIKI clustering pass only —
it deliberately does NOT reimplement clustering (reuses
``clusters.cluster_auto_memory_files``), draft synthesis (reuses
``merge.synthesize_body`` / ``merge.derive_topic_slug``), or the
sidecar-write/idempotency logic (reuses
``pending_merges.write_pending_merge``); it is glue over those three, not
a fourth implementation of any of them.

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

Out of scope (deliberate — see issue athenaeum#290):

- LLM-based draft synthesis / rich merge rationale.
- Real contradiction detection beyond the existing cohesion threshold.
- Retroactively re-clustering already ``archived``/``superseded_by`` pages.

Layering (L4 domain/pipeline). ``wiki_dedupe.py`` imports ``athenaeum.merge``
and ``athenaeum.pending_merges`` at module TOP level — normal downward
dependencies; neither imports this module back. After issue athenaeum#545 dissolved
the librarian-centered named-8 coupling, ``wiki_dedupe.py`` is NOT part of any
import SCC. ``librarian.py`` calls into this module only via its own deferred
import (a one-way edge). The one function-local ``from athenaeum.pending_merges
import _make_id, parse_pending_merges`` inside ``propose_wiki_page_merges`` is
NOT a cycle-breaker (``pending_merges`` is already imported at top level
above); it is a plain in-function convenience import.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.authority import is_pointer_stub
from athenaeum.clusters import (
    EMBEDDER_CHROMADB_DEFAULT,
    EMBEDDER_FALLBACK_HASHING,
    Cluster,
    _fallback_embeddings,
    cluster_auto_memory_files,
    prune_cluster_rotations,
    resolve_cluster_threshold,
    resolve_rotation_retention,
)
from athenaeum.config import load_config, resolve_heartbeat_interval
from athenaeum.merge import (
    derive_topic_slug,
    synthesize_body,
)
from athenaeum.merge_type_gate import (
    _merge_proposal_suppression_reason,
    build_cite_proposal,
    cross_class_precheck,
)
from athenaeum.models import AutoMemoryFile, parse_frontmatter, validity_bound_str
from athenaeum.pending_merges import write_pending_merge
from athenaeum.pii import is_pii_flagged
from athenaeum.progress import PhaseHeartbeat
from athenaeum.search import embed_texts
from athenaeum.storage import is_merge_eligible

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

# Issue athenaeum#1142 (AC2): durable sidecar ledger for wiki-dedupe clusters
# SUPPRESSED by the athenaeum#400/#421 degenerate-over-cluster gate
# (``_merge_proposal_suppression_reason``) — today the ONLY trace of a
# suppression is a one-shot log.info line (see ``propose_wiki_page_merges``
# below), and a suppressed cluster never becomes a ``_pending_merges.md``
# proposal, so nothing durable in the store can answer "which embedder
# produced cluster X, and why was it suppressed?" without a live host log
# read. Lives alongside ``_pending_merges.md`` under ``wiki/`` — the natural
# home for a wiki-dedupe-pass artifact, per the issue's own suggestion.
DEFAULT_WIKI_SUPPRESSIONS_FILENAME = "_wiki_dedupe_suppressions.jsonl"


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
    """
    if not files:
        return {}, {}
    provider = embedding_provider or embed_texts
    texts = [am.content for am in files]
    vectors = provider(texts)
    if vectors is None or len(vectors) != len(files):
        # Issue athenaeum#1032: one-time WARNING — this fallback used to engage with
        # no log call at all, so athenaeum#1005's over-cluster diagnosis had no way to
        # tell from run artifacts whether the hashing-trick embedder (rather
        # than real MiniLM vectors) produced a cluster's similarity scores.
        _warn_wiki_fallback_engaged_once(len(files))
        fallback = _fallback_embeddings(files)
        sources = {str(am.path): EMBEDDER_FALLBACK_HASHING for am in files}
        return fallback, sources
    embeddings = {str(am.path): list(map(float, vec)) for am, vec in zip(files, vectors)}
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


def _write_wiki_suppressions_report(
    rows: list[dict[str, Any]],
    wiki_root: Path,
    *,
    knowledge_root: Path,
    config: dict[str, Any] | None,
    rotate: bool = True,
) -> Path:
    """Persist THIS RUN's suppressed wiki-dedupe clusters durably (athenaeum#1142 AC2).

    Mirrors :func:`athenaeum.clusters.write_cluster_report`'s convention
    deliberately, rather than inventing a new one (issue athenaeum#1142's own
    guidance: follow how the existing operational ledgers in this repo
    handle retention) — the CANONICAL file
    (``wiki/_wiki_dedupe_suppressions.jsonl``) is fully REPLACED on every
    call (current-run state, not an accumulating log), and one timestamped
    ``<stem>-<UTC-iso>.jsonl`` rotation sibling is written alongside it so
    history isn't lost between runs. Rotations are pruned to the SAME
    ``librarian.rotation_retention`` window (env ``ATHENAEUM_ROTATION_RETENTION``
    > yaml > default 30) that already governs
    ``raw/_librarian-clusters.jsonl``'s rotations — one retention policy for
    every JSONL cluster/suppression report in the store, not a second knob.
    This is the AC4 retention answer stated in code: bounded, self-pruning,
    never unbounded-append (contrast the sibling athenaeum#1229 item, fixing
    exactly the failure mode — a 1.4M-row unbounded ledger — an
    unbounded design here would eventually reproduce).

    Called even when *rows* is empty, so the canonical file always reflects
    "this run suppressed nothing" rather than going stale from the last run
    that did.
    """
    output_path = wiki_root / DEFAULT_WIKI_SUPPRESSIONS_FILENAME
    payload_lines = [json.dumps(row, sort_keys=True) for row in rows]
    text = "\n".join(payload_lines) + ("\n" if payload_lines else "")

    if rotate:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        timestamped = output_path.with_name(
            f"{output_path.stem}-{ts}{output_path.suffix}"
        )
        atomic_write_text(timestamped, text)

    atomic_write_text(output_path, text)

    retention = resolve_rotation_retention(knowledge_root, config)
    prune_cluster_rotations(output_path, keep=retention)
    return output_path


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
        # Issue athenaeum#1142 AC2/AC4: the suppressions ledger reflects THIS
        # run's state even when there was nothing to cluster at all — a
        # dry_run preview still touches no durable state, matching every
        # other write in this function.
        if not dry_run:
            _write_wiki_suppressions_report(
                [], wiki_root, knowledge_root=knowledge_root, config=resolved_config
            )
        return []

    merges_path = wiki_root / "_pending_merges.md"
    proposals: list[dict[str, Any]] = []
    suppressed_rows: list[dict[str, Any]] = []

    for cluster in clusters:
        heartbeat.tick(cluster.cluster_id)
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

        # Issue athenaeum#478: the athenaeum#400/#421 degenerate-over-cluster suppression gate
        # must run on THIS write path too, not just merge.py's resolver path.
        # ``find_wiki_page_clusters`` uses the SAME shared formation routine
        # (``cluster_auto_memory_files``) as the raw-source C1-C4 pass — as of
        # issue athenaeum#681, formation itself is complete-linkage (single-linkage
        # connected components refined into cliques), so a weak bridging edge
        # can no longer chain hundreds/thousands of loosely-related pages into
        # a giant component in the first place. This gate is therefore a
        # BACKSTOP, not the load-bearing defense: it still matters for
        # legitimately large/incohesive cliques and for any pre-athenaeum#681
        # legacy data, but it is no longer what stands between formation and
        # the giant-component incident.
        #
        # Historical note (pre-athenaeum#681, before formation was complete-linkage):
        # single-linkage chains reached ``_pending_merges.md`` directly (the
        # live 1,711-/1,746-source ``merge-workflow-pattern`` and 16-source
        # ``contact-contacts-wiki`` proposals), bypassing the active-by-default
        # ``max_merge_sources`` (5) / ``min_merge_mean_similarity`` (0.6)
        # guardrails below.
        #
        # Issue athenaeum#803: confirmed this formation logic is (and must stay)
        # shared with the raw-source clusterer rather than forked a second
        # time — do not reimplement single-linkage-then-complete-linkage
        # refinement here or in ``merge.py``'s resolver path. Change the
        # algorithm once, in :mod:`athenaeum.clusters`
        # (``_single_linkage``/``_complete_linkage``/``cluster_auto_memory_files``),
        # and both call sites pick it up.
        #
        # This gate call mirrors merge.py ``_emit_escalation``'s call EXACTLY
        # (size cap + complete-linkage min-pairwise + mean-cohesion +
        # confidence floor), evaluated BEFORE the proposal is written — and
        # before the ``dry_run`` branch, so a dry-run preview reflects what a
        # real gated run would do.
        suppression = _merge_proposal_suppression_reason(
            n_sources=len(sources),
            confidence=cluster.centroid_score,
            config=resolved_config,
            mean_similarity=cluster.centroid_score,
            min_pairwise=cluster.min_pairwise_score,
            cluster_threshold=resolved_threshold,
        )
        if suppression is not None:
            # Issue athenaeum#1032: names the embedder that produced the suppressed
            # cluster's vectors — the over-cluster diagnosis this gate guards
            # against needs to know whether a coarse hashing-trick fallback
            # (rather than real MiniLM similarity) drove the suppression.
            # Issue athenaeum#1085: n_sources is recorded as its own structured field,
            # UNCONDITIONALLY — not parsed out of `suppression`, which only
            # embeds the source count when the size-cap gate happens to be
            # the one that fired (gate ordering is pinned by
            # TestGateOrdering, so a cohesion/confidence/chain suppression
            # previously recorded no size at all).
            log.info(
                "wiki-page dedup: SUPPRESSED degenerate merge proposal for "
                "cluster %s (%s); embedder=%s; n_sources=%d; not written to "
                "_pending_merges.md",
                cluster.cluster_id,
                suppression,
                cluster.embedder,
                len(sources),
            )
            # Issue athenaeum#1142 AC2: durably record what the log line above
            # only states once, in a rotating log an operator may never see
            # again — reason, embedder, and the shape numbers that produced
            # the suppression, keyed by cluster id.
            suppressed_rows.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "n_sources": len(sources),
                    "reason": suppression,
                    "embedder": cluster.embedder,
                    "mean_similarity": float(cluster.centroid_score),
                    "min_pairwise_score": float(cluster.min_pairwise_score),
                    "cluster_threshold": float(resolved_threshold),
                    "sources": list(sources),
                    "suppressed_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
            continue

        # Issue athenaeum#433: type-compatibility precheck. A cluster spanning >1
        # distinct memory_class values may not be merged — same-class only
        # (docs/memory-taxonomy.md #3). Rejected BEFORE the merge-target
        # slug/draft body are even computed: a cross-class cluster never
        # becomes a merge proposal, a cite proposal is built in its place.
        rejection = cross_class_precheck(sources)
        if rejection is not None:
            cite = build_cite_proposal(sources, rejection)
            log.info(
                "wiki-page dedup: cross-class cluster rejected for merge "
                "(%s); emitting cite proposal instead (citing=%d, cited=%d)",
                rejection.reason,
                len(cite.citing),
                len(cite.cited),
            )
            proposals.append(
                {
                    "action": cite.action,
                    "citing": cite.citing,
                    "cited": cite.cited,
                    "rationale": cite.rationale,
                    "rejection": rejection.to_dict(),
                }
            )
            continue

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
        # "what would a real run do" framing (Quine review of athenaeum#293).
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
            # Issue athenaeum#1142 AC1: carries the same embedder identity athenaeum#1032
            # already stamps on the log line into the durable proposal
            # itself.
            embedder=cluster.embedder,
        )
        proposals.append(proposal)
        log.info(
            "wiki-page dedup: proposed merge %r covering %d page(s)",
            merge_target_name,
            len(members),
        )

    heartbeat.done()
    # Issue athenaeum#1142 AC2/AC4: one snapshot of THIS run's suppressions,
    # replacing the canonical file every real run (never touched on
    # dry_run) — see _write_wiki_suppressions_report's docstring for the
    # retention story.
    if not dry_run:
        _write_wiki_suppressions_report(
            suppressed_rows, wiki_root, knowledge_root=knowledge_root, config=resolved_config
        )
    return proposals
