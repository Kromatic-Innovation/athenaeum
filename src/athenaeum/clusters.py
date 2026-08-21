# SPDX-License-Identifier: Apache-2.0
"""Auto-memory cluster pass (issue athenaeum#196, C2).

Groups :class:`~athenaeum.models.AutoMemoryFile` records (discovered by
C1, :func:`athenaeum.librarian.discover_auto_memory_files`) into
near-duplicate clusters using the existing chromadb ``VectorBackend``
embedder. Writes a JSONL report that C3 (the merge pass) consumes.

Scope for this module:

- Input: a list of ``AutoMemoryFile`` records + a chromadb cache dir
  that already holds embeddings for those records (the recall index
  build populates this via ``extra_roots``, see issue athenaeum#192).
- Output: a JSONL report at ``raw/_librarian-clusters.jsonl`` (path
  configurable) with one row per cluster. A timestamped sibling file
  is written on every run; the canonical name is atomically replaced.
- Clustering: complete-linkage on pairwise cosine similarity, threshold
  configured by ``librarian.cluster_threshold`` (default 0.6). Single-linkage
  connected components are computed first as a cheap scoping step, then refined
  into complete-linkage cliques (issue athenaeum#681) so a weak bridging edge can no
  longer chain a giant component; every multi-member cluster is a clique in
  which EVERY pair clears the threshold.
- Singletons: size-1 clusters pass through unchanged. There is NO
  minimum-cluster-size filter.

Out of scope (deliberate — later lanes):

- Merging clusters into wiki entries (C3, athenaeum#197).
- Contradiction detection inside a cluster (C4, athenaeum#198).

The embedder MUST be the shared chromadb collection. This module does
not import sentence_transformers, openai, cohere, or any other
embedding provider.

**Layering:** L3 service. Module scope imports :mod:`athenaeum.atomic_io`,
:mod:`athenaeum.config`, and :mod:`athenaeum.models` (L1/L2) — never L4. The
``from athenaeum.search import VectorBackend`` import inside
:func:`_resolve_embeddings` (below) is function-local for IMPORT COST, NOT a
cycle-breaker: :mod:`athenaeum.search` does not import this module. Deferring
it means a caller who supplies its own ``embeddings`` map (skipping chromadb
entirely, per :func:`cluster_auto_memory_files`'s ``embeddings`` argument)
never pays ``search``'s optional ``chromadb`` import cost.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import math
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import resolve_cache_dir
from athenaeum.models import AutoMemoryFile
from athenaeum.vecmath import cosine

log = logging.getLogger(__name__)

# Default cache dir mirrors search.py's expectations — callers usually
# pass this in explicitly (librarian.run() resolves it against the
# knowledge root), but we expose a default for shell/library callers.
# Issue athenaeum#521: resolved through the shared config resolver (arg > env > default)
# so ~/.cache/athenaeum is constructed in exactly one place.
DEFAULT_CACHE_DIR = resolve_cache_dir()

# Default threshold — empirically tuned against the near-duplicate
# clustering fixture (see test_librarian_clusters.py). MiniLM
# cosines on 4000-char prefixes put the typo clone at ~0.59 against the
# anchor near-duplicate file and the iMessage variant at
# ~0.70; unrelated singletons land at ~0.24 (sentry) and ~0.07 (user).
# At 0.55 the near-duplicate voltaire/nanoclaw notes are all mutually above
# the threshold, so complete-linkage keeps them one clique while leaving the
# singletons alone. 0.6 was too tight (typo fell out); a drop to 0.55 is the
# minimum that preserves the load-bearing property of the regression fixture.
# Tunable via ``librarian.cluster_threshold``.
DEFAULT_CLUSTER_THRESHOLD = 0.55

# Default output path, resolved relative to the knowledge root.
DEFAULT_CLUSTER_OUTPUT = "raw/_librarian-clusters.jsonl"

# Default number of timestamped rotation siblings to keep (issue athenaeum#311).
# Each run writes one ``<stem>-<UTC-iso>.jsonl`` rotation next to the
# canonical report; without pruning these accumulate unbounded (~365/yr).
# They are debugging artifacts only — recovery is git-based — so a modest
# window is plenty. ``0`` (or negative) disables pruning entirely.
DEFAULT_ROTATION_RETENTION = 30

# Issue athenaeum#1032: embedder identity strings recorded on each ``Cluster`` —
# observability only, so athenaeum#1005's over-cluster diagnosis can tell which
# embedder produced a cluster's vectors from run artifacts instead of having
# to re-derive it from context. See ``_resolve_embeddings``/
# ``cluster_auto_memory_files`` below for how a cluster's value is derived.
EMBEDDER_CHROMADB_DEFAULT = "chromadb-default"
EMBEDDER_FALLBACK_HASHING = "fallback-hashing"
# A cluster whose members' vectors came from BOTH sources — possible only via
# the raw-intake path's per-file partial fallback (some ids hit the chromadb
# collection, others didn't). Reported rather than picking one source
# arbitrarily; does not change which files land in the cluster.
EMBEDDER_MIXED = "mixed"
# Legacy ``raw/_librarian-clusters.jsonl`` rows written before this issue, and
# callers that supply their own ``embeddings`` map without an
# ``embedder_sources`` counterpart, have no recorded source.
EMBEDDER_UNKNOWN = "unknown"


@dataclass
class Cluster:
    """A group of auto-memory files judged to be near-duplicates."""

    cluster_id: str
    member_paths: list[str] = field(default_factory=list)
    centroid_score: float = 0.0
    # Issue athenaeum#421: minimum pairwise cosine among members (complete-linkage
    # coherence metric). 1.0 for singletons and pre-athenaeum#421 rows that lack the
    # field. A cluster is a complete-linkage clique at ``threshold`` iff this
    # is ``>= threshold``. Issue athenaeum#681 makes formation itself complete-linkage,
    # so a freshly-written multi-member cluster now ALWAYS has ``min_pairwise
    # >= threshold`` and the merge-proposal gate's min-pairwise arm is a
    # backstop for legacy/pre-athenaeum#681 rows rather than the load-bearing gate.
    min_pairwise_score: float = 1.0
    rationale: str = ""
    # Issue athenaeum#1032: which embedder produced this cluster's vectors — one of
    # the ``EMBEDDER_*`` constants above. Defaults to ``EMBEDDER_UNKNOWN`` so
    # pre-athenaeum#1032 JSONL rows (and any caller that hasn't been updated to pass
    # ``embedder_sources``) still deserialize without a KeyError.
    embedder: str = EMBEDDER_UNKNOWN

    def to_row(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "member_paths": list(self.member_paths),
            "centroid_score": float(self.centroid_score),
            "min_pairwise_score": float(self.min_pairwise_score),
            "rationale": self.rationale,
            "embedder": self.embedder,
        }


def _indexed_id_for(am: AutoMemoryFile, extra_roots: Sequence[Path]) -> str | None:
    """Compute the id used by :func:`search._iter_extra_root_entries` for *am*.

    Recall's index registers extra-root files as
    ``{root_name}/{relpath_posix}``. We reconstruct that id from the
    AutoMemoryFile's absolute path + the configured extra roots so
    ``VectorBackend.fetch_embeddings`` can look them up without a second
    scan. Returns None if the path doesn't live under any configured root
    (e.g. a stale record from a reloaded config).
    """
    ampath = am.path.resolve()
    for root in extra_roots:
        try:
            rel = ampath.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        return f"{root.name}/{rel}"
    return None


def _fallback_embeddings(
    files: Sequence[AutoMemoryFile],
) -> dict[str, list[float]]:
    """Hashing-trick fallback for when chromadb has no usable index.

    Deterministic, no external deps. Produces a fixed-dim sparse vector
    by hashing tokens from the file's name/description/body. Used only
    when the VectorBackend cannot return embeddings (e.g. optional
    chromadb extra not installed, or the index was never built). This
    is NOT a replacement embedder — it's a graceful-degradation path
    that keeps clustering available and testable without chromadb, and
    it is documented in the PR body.

    The hash feature space still reuses the shared MiniLM dimension
    (384) so the rest of the pipeline sees vectors of the expected
    shape; it does NOT call out to sentence-transformers/openai/cohere.
    """
    dim = 384
    out: dict[str, list[float]] = {}
    for am in files:
        vec = [0.0] * dim
        try:
            body = am.content
        except OSError:
            body = ""
        text = " ".join([am.name, am.description, am.path.stem, body]).lower()
        tokens = [t for t in (text.replace("_", " ").split()) if len(t) >= 2]
        if not tokens:
            vec[0] = 1.0
            out[str(am.path)] = vec
            continue
        for tok in tokens:
            idx = hash(tok) % dim
            # sign trick for mild decorrelation
            sign = 1.0 if (hash(tok + "_s") % 2 == 0) else -1.0
            vec[idx] += sign
        # l2-normalize so cosine is well-defined
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0.0:
            vec = [x / norm for x in vec]
        out[str(am.path)] = vec
    return out


def _resolve_embeddings(
    files: Sequence[AutoMemoryFile],
    *,
    extra_roots: Sequence[Path],
    cache_dir: Path,
) -> tuple[dict[str, list[float]], dict[str, str]]:
    """Resolve an embedding for every file, keyed by absolute path string.

    Strategy:
      1. Try ``VectorBackend.fetch_embeddings`` with the recall-index
         ids — this is the hot path and reuses the existing collection.
      2. For any file whose id was missing from the collection, fall
         back to the hashing-trick vector. This keeps the pipeline
         robust against a stale or partial recall index.

    Returns ``(embeddings, sources)`` — ``sources`` maps the same
    ``str(path)`` keys to the ``EMBEDDER_*`` constant that produced each
    vector (issue athenaeum#1032), so callers (:func:`cluster_auto_memory_files`) can
    record which embedder backed each formed cluster.
    """
    # Build id → AutoMemoryFile map so we can translate hits back.
    id_to_file: dict[str, AutoMemoryFile] = {}
    for am in files:
        idx_id = _indexed_id_for(am, extra_roots)
        if idx_id is None:
            continue
        id_to_file[idx_id] = am

    embeddings: dict[str, list[float]] = {}
    sources: dict[str, str] = {}
    hit_ids: set[str] = set()
    if id_to_file:
        try:
            # Deferred for IMPORT COST, not a cycle: athenaeum.search does not
            # import this module. VectorBackend pulls in chromadb (an optional
            # [vector] extra) on first real use — deferring means a caller that
            # supplies its own `embeddings` map to cluster_auto_memory_files
            # never pays that cost.
            from athenaeum.search import VectorBackend

            backend = VectorBackend()
            raw_hits = backend.fetch_embeddings(id_to_file.keys(), cache_dir)
        except Exception as exc:  # noqa: BLE001 — degrade to fallback on any error
            log.debug("VectorBackend.fetch_embeddings failed: %s", exc)
            raw_hits = {}
        for idx_id, vec in raw_hits.items():
            hit_file = id_to_file.get(idx_id)
            if hit_file is None:
                continue
            embeddings[str(hit_file.path)] = vec
            sources[str(hit_file.path)] = EMBEDDER_CHROMADB_DEFAULT
            hit_ids.add(idx_id)

    # Fallback for any misses.
    missing = [am for am in files if str(am.path) not in embeddings]
    if missing:
        # Issue athenaeum#1032: raised DEBUG -> WARNING — this was invisible at the
        # deployed INFO level, so the coarse-embedding-fallback engagement that
        # feeds athenaeum#1005's over-cluster diagnosis left no trace in normal runs.
        log.warning(
            "cluster embeddings: %d of %d served from chromadb, %d fell back "
            "to the fallback-hashing embedder",
            len(files) - len(missing),
            len(files),
            len(missing),
        )
        embeddings.update(_fallback_embeddings(missing))
        for am in missing:
            sources[str(am.path)] = EMBEDDER_FALLBACK_HASHING

    return embeddings, sources


def _build_adjacency(
    file_ids: Sequence[str],
    embeddings: dict[str, list[float]],
    threshold: float,
) -> tuple[list[set[int]], dict[tuple[int, int], float]]:
    """Return the threshold adjacency AND the weight of every surviving edge.

    The adjacency list (index → neighbour set, ``cosine >= threshold``) drives
    :func:`_single_linkage`. Alongside it we capture ``edge_sim[(i, j)]`` (``i <
    j``) = the actual cosine for each edge that cleared the threshold — reused by
    :func:`_complete_linkage` (issue athenaeum#681) to refine each connected component
    into complete-linkage cliques WITHOUT a second ``O(n^2)`` cosine pass.
    Pairs below the threshold are absent from both structures (they never form
    an edge and never merge two members into one cluster).
    """
    n = len(file_ids)
    adj: list[set[int]] = [set() for _ in range(n)]
    edge_sim: dict[tuple[int, int], float] = {}
    vecs = [embeddings.get(fid) for fid in file_ids]
    for i in range(n):
        vi = vecs[i]
        if vi is None:
            continue
        for j in range(i + 1, n):
            vj = vecs[j]
            if vj is None:
                continue
            sim = cosine(vi, vj)
            if sim >= threshold:
                adj[i].add(j)
                adj[j].add(i)
                edge_sim[(i, j)] = sim
    return adj, edge_sim


def _single_linkage(adj: list[set[int]]) -> list[list[int]]:
    """Connected components over an undirected adjacency list."""
    n = len(adj)
    seen = [False] * n
    components: list[list[int]] = []
    for start in range(n):
        if seen[start]:
            continue
        stack = [start]
        comp: list[int] = []
        while stack:
            node = stack.pop()
            if seen[node]:
                continue
            seen[node] = True
            comp.append(node)
            stack.extend(adj[node])
        components.append(sorted(comp))
    return components


def _complete_linkage(
    component: Sequence[int],
    adj: list[set[int]],
    edge_sim: dict[tuple[int, int], float],
    threshold: float,
) -> list[list[int]]:
    """Refine a single-linkage *component* into complete-linkage clusters.

    Issue athenaeum#681. :func:`_single_linkage` only requires each member to be
    transitively CONNECTED at ``threshold``, so a single weak bridging edge can
    chain thousands of loosely-related pages into one giant component — the
    ~2,200-source cluster the merge-proposal gate (athenaeum#400/#421) then rebuilds and
    discards on every run. This refines each connected component into
    complete-linkage clusters: every returned cluster is a clique at
    ``threshold`` — EVERY pair of members has ``cosine >= threshold`` — so the
    giant component is never formed in the first place. The athenaeum#421 min-pairwise
    suppression arm becomes a redundant backstop (formation now guarantees what
    it used to check) rather than the load-bearing gate.

    Greedy agglomerative complete linkage: start with each member as its own
    cluster and repeatedly merge the two clusters whose complete-linkage
    similarity — the MINIMUM pairwise cosine across the two membership sets, the
    ``farthest`` pair — is highest, so long as that minimum is ``>= threshold``
    (which keeps the union a clique). Stop when no admissible merge remains;
    members left unmerged fall out as singletons, exactly as the single-linkage
    pass emits isolated nodes.

    Only edges recorded in ``edge_sim`` (pairs that cleared ``threshold``) can
    ever join two clusters, so the search is confined to the component's
    internal edges via ``adj``. Cost is driven by those edges, reusing the
    ``O(n^2)`` cosine pass already paid by :func:`_build_adjacency`; a component
    that is already a clique collapses in one sweep. Deterministic: the max-heap
    breaks ties on cluster id, which is assigned in a fixed traversal order.
    """
    if len(component) <= 1:
        return [list(component)]

    comp_set = set(component)

    # Each active cluster is a stable integer id → sorted member indices. Ids
    # are never reused (``next_id`` only increments), so every cluster-pair key
    # below is unique and its recorded similarity never mutates in place —
    # staleness is detected purely by whether a cluster is still ``active``.
    members: dict[int, list[int]] = {}
    node_cid: dict[int, int] = {}
    cadj: dict[int, set[int]] = {}
    clsim: dict[tuple[int, int], float] = {}
    next_id = 0
    for node in sorted(component):
        members[next_id] = [node]
        cadj[next_id] = set()
        node_cid[node] = next_id
        next_id += 1

    heap: list[tuple[float, int, int]] = []

    def _record_pair(a: int, b: int, sim: float) -> None:
        lo, hi = (a, b) if a < b else (b, a)
        clsim[(lo, hi)] = sim
        cadj[a].add(b)
        cadj[b].add(a)
        heapq.heappush(heap, (-sim, lo, hi))

    # Seed admissible cluster pairs from the component's internal edges only.
    for node in component:
        for nb in adj[node]:
            if nb <= node or nb not in comp_set:
                continue
            _record_pair(node_cid[node], node_cid[nb], edge_sim[(node, nb)])

    active = set(members)
    while heap:
        neg_sim, a, b = heapq.heappop(heap)
        if a not in active or b not in active:
            continue  # one side already merged away — stale entry
        sim = -neg_sim
        if clsim.get((a, b)) != sim:
            continue  # superseded (defensive; keys are unique so rare)

        merged = next_id
        next_id += 1
        members[merged] = sorted(members[a] + members[b])
        cadj[merged] = set()
        active.discard(a)
        active.discard(b)
        active.add(merged)

        # A cluster c is admissible with the union iff it was admissible with
        # BOTH a and b (min cross-sim across the union must clear ``threshold``);
        # the new complete-linkage sim is the min of the two old sims — already
        # >= threshold since both inputs were. Clusters admissible with only one
        # side correctly drop out (one cross pair is sub-threshold).
        for c in (cadj[a] & cadj[b]) - {a, b}:
            if c not in active:
                continue
            sa = clsim[(a, c) if a < c else (c, a)]
            sb = clsim[(b, c) if b < c else (c, b)]
            _record_pair(merged, c, sa if sa < sb else sb)

        # Drop the merged-away ids from their neighbours' adjacency so future
        # intersections don't reconsider them.
        for c in cadj[a]:
            cadj[c].discard(a)
        for c in cadj[b]:
            cadj[c].discard(b)

    return [sorted(members[cid]) for cid in sorted(active)]


def _mean_intra_similarity(
    indices: Sequence[int],
    file_ids: Sequence[str],
    embeddings: dict[str, list[float]],
) -> float:
    """Mean pairwise cosine among cluster members; 1.0 for singletons."""
    if len(indices) <= 1:
        return 1.0
    vecs = [embeddings.get(file_ids[i]) for i in indices]
    total = 0.0
    pairs = 0
    for i in range(len(indices)):
        vi = vecs[i]
        if vi is None:
            continue
        for j in range(i + 1, len(indices)):
            vj = vecs[j]
            if vj is None:
                continue
            total += cosine(vi, vj)
            pairs += 1
    return total / pairs if pairs else 1.0


def _min_intra_similarity(
    indices: Sequence[int],
    file_ids: Sequence[str],
    embeddings: dict[str, list[float]],
) -> float:
    """Minimum pairwise cosine among cluster members; 1.0 for singletons.

    Issue athenaeum#421: the complete-linkage coherence metric for the merge-proposal
    path. Single-linkage (:func:`_single_linkage`) only guarantees each member
    is transitively CONNECTED to the cluster at ``threshold`` — a weak
    ``cosine >= threshold`` bridge can chain otherwise-dissimilar members into
    one giant component (the 1,711-page incident). A cluster is a
    complete-linkage clique iff EVERY pair clears the threshold, i.e. iff this
    minimum pairwise cosine is ``>= threshold``. Recording it per cluster lets
    the merge-proposal gate suppress single-linkage chains WITHOUT changing the
    single-linkage grouping the compile/delta passes depend on.

    Same O(n^2) cost already paid by :func:`_mean_intra_similarity`; missing
    embeddings (``None`` vecs) are skipped, and a cluster with no comparable
    pair returns 1.0 (nothing to contradict complete-linkage)."""
    if len(indices) <= 1:
        return 1.0
    vecs = [embeddings.get(file_ids[i]) for i in indices]
    lowest = 1.0
    pairs = 0
    for i in range(len(indices)):
        vi = vecs[i]
        if vi is None:
            continue
        for j in range(i + 1, len(indices)):
            vj = vecs[j]
            if vj is None:
                continue
            lowest = min(lowest, cosine(vi, vj))
            pairs += 1
    return lowest if pairs else 1.0


def _shared_tokens(files: Sequence[AutoMemoryFile], limit: int = 4) -> list[str]:
    """Find tokens that appear in every file's name+description — for rationale."""
    if not files:
        return []
    token_sets: list[set[str]] = []
    for am in files:
        text = f"{am.name} {am.description} {am.path.stem}".lower()
        tokens = {t for t in text.replace("_", " ").split() if len(t) >= 3}
        token_sets.append(tokens)
    common = set.intersection(*token_sets) if token_sets else set()
    # Drop obvious structural words
    boring = {
        "the",
        "and",
        "for",
        "with",
        "memory",
        "feedback",
        "project",
        "reference",
        "user",
        "recall",
        "note",
        "auto",
    }
    interesting = sorted(t for t in common if t not in boring)
    return interesting[:limit]


def cluster_auto_memory_files(
    files: Sequence[AutoMemoryFile],
    *,
    extra_roots: Sequence[Path],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    threshold: float = DEFAULT_CLUSTER_THRESHOLD,
    embeddings: dict[str, list[float]] | None = None,
    embedder_sources: dict[str, str] | None = None,
) -> list[Cluster]:
    """Group auto-memory files into near-duplicate clusters.

    Args:
        files: Output of
            :func:`athenaeum.librarian.discover_auto_memory_files`, OR any
            other sequence of :class:`AutoMemoryFile`-shaped records —
            e.g. the wiki-page wrapper built by
            :mod:`athenaeum.wiki_dedupe` (issue athenaeum#290). Nothing below this
            line branches on ``memory_type``/``origin_scope`` semantics
            specific to raw auto-memory intake, so any caller that can
            produce ``AutoMemoryFile`` records (or a lightweight stand-in
            with the same ``path``/``origin_scope``/``name``/
            ``description``/``content`` shape) gets clustering for free.
        extra_roots: The same list recall uses for index scans — needed
            to translate absolute file paths into the ``<root>/<rel>``
            ids chromadb stores, AND to compute each cluster's relative
            ``member_paths``. Usually obtained via
            :func:`athenaeum.config.resolve_extra_intake_roots`, but a
            caller with a different embedding source (see ``embeddings``
            below) can pass whatever root it wants relative paths anchored
            to (e.g. the wiki root).
        cache_dir: Root of the shared embedder cache (chromadb is at
            ``<cache_dir>/wiki-vectors/``). Ignored when ``embeddings`` is
            supplied.
        threshold: Cosine similarity cutoff for clustering — every pair
            within a returned multi-member cluster clears it (complete
            linkage, issue athenaeum#681). Defaults to :data:`DEFAULT_CLUSTER_THRESHOLD`.
        embeddings: Optional precomputed ``{str(path): vector}`` map. When
            supplied, the chromadb lookup + hashing-trick fallback in
            :func:`_resolve_embeddings` is skipped entirely and this map is
            used as-is — the caller owns embedding resolution (e.g. a
            direct :func:`athenaeum.search.embed_texts` call keyed by
            wiki-page path, where the raw-intake extra-root id scheme
            doesn't apply). ``None`` (the default) preserves the original
            chromadb-then-fallback behavior byte-for-byte.
        embedder_sources: Optional ``{str(path): EMBEDDER_*}`` map (issue athenaeum#1032)
            recording which embedder produced each file's vector — same
            keys as ``embeddings``. Only meaningful together with a supplied
            ``embeddings`` map; when ``embeddings`` is ``None`` this is
            ignored and overwritten with what :func:`_resolve_embeddings`
            itself resolves. A member missing from this map (or the map
            itself being ``None``) reports :data:`EMBEDDER_UNKNOWN` on its
            cluster — observability bookkeeping only, never affects which
            files land in a cluster.

    Returns:
        A list of :class:`Cluster` records. Empty input → empty list.
        Singletons are returned as size-1 clusters unchanged.
    """
    if not files:
        return []

    if embeddings is None:
        embeddings, embedder_sources = _resolve_embeddings(
            files,
            extra_roots=list(extra_roots),
            cache_dir=cache_dir,
        )
    if embedder_sources is None:
        embedder_sources = {}
    # Index files by the string form of their absolute path (stable and
    # unique; avoids Path equality surprises across tempdirs).
    file_ids: list[str] = [str(am.path) for am in files]

    adj, edge_sim = _build_adjacency(file_ids, embeddings, threshold)
    # Single-linkage first — cheaply scopes complete-linkage refinement to each
    # connected component (two members in different components share no
    # ``>= threshold`` edge, so they can never land in one clique). Issue athenaeum#681:
    # every multi-member component is then split into complete-linkage cliques
    # so a weak bridging edge can no longer chain a giant component that the
    # merge-proposal gate would only rebuild and discard.
    components: list[list[int]] = []
    for comp in _single_linkage(adj):
        if len(comp) == 1:
            components.append(comp)
        else:
            components.extend(_complete_linkage(comp, adj, edge_sim, threshold))

    # Stable cluster id: a content-address over the sorted member relpaths,
    # prefixed by a human-readable scope hint. Issue athenaeum#370 (delta compile):
    # the previous positional ``f"{scope_hint}-{seq:04d}"`` id was UNSTABLE —
    # the same cluster got a different id depending on how many other
    # clusters the component enumeration happened to precede it with. A
    # delta run over a pool SUBSET enumerates a different set of components,
    # so a positional id would drift for byte-identical clusters and break
    # full-vs-delta equivalence (the id renders into wiki frontmatter). A
    # content-address is deterministic across a full run and any delta run
    # whose pool reproduces the same member set. ``scope_hint`` is the min
    # origin_scope over members (set-determined, order-independent) so a
    # cross-scope cluster's prefix never depends on member ordering.
    clusters: list[Cluster] = []
    for component in components:
        members = [files[i] for i in component]
        relpaths: list[str] = []
        for am in members:
            relpath = None
            ampath = am.path.resolve()
            for root in extra_roots:
                try:
                    relpath = ampath.relative_to(root.resolve()).as_posix()
                    break
                except ValueError:
                    continue
            relpaths.append(relpath or str(am.path))

        scope_hint = min(am.origin_scope for am in members).lstrip("-_") or "unscoped"
        digest = (
            hashlib.sha1(
                "\n".join(sorted(relpaths)).encode("utf-8")
            ).hexdigest()[:8]
        )
        cluster_id = f"{scope_hint}-{digest}"

        member_ids = [file_ids[i] for i in component]
        cluster_embedder = _cluster_embedder_identity(member_ids, embedder_sources)

        centroid = _mean_intra_similarity(component, file_ids, embeddings)
        min_pairwise = _min_intra_similarity(component, file_ids, embeddings)
        if len(component) == 1:
            rationale = f"singleton; scope={members[0].origin_scope}"
        else:
            shared = _shared_tokens(members)
            token_blurb = (
                ", ".join(shared) if shared else "no shared frontmatter tokens"
            )
            rationale = (
                f"cosine >= {threshold:.2f}; "
                f"members share tokens: {token_blurb}; "
                f"mean intra-sim {centroid:.2f}; "
                f"min pairwise {min_pairwise:.2f}"
            )
        clusters.append(
            Cluster(
                cluster_id=cluster_id,
                member_paths=relpaths,
                centroid_score=centroid,
                min_pairwise_score=min_pairwise,
                rationale=rationale,
                embedder=cluster_embedder,
            )
        )

    return clusters


def _cluster_embedder_identity(
    member_ids: Sequence[str], sources: dict[str, str]
) -> str:
    """Collapse each member's recorded embedder source into one cluster value.

    Issue athenaeum#1032. Uniform sources (the common case — a caller-supplied
    ``embeddings``/``embedder_sources`` pair is always uniform today, and the
    raw-intake ``_resolve_embeddings`` path usually is too) report that single
    value. A cluster whose members were served by different embedders —
    possible only via ``_resolve_embeddings``'s per-file partial fallback,
    where some ids hit the chromadb collection and others didn't — reports
    :data:`EMBEDDER_MIXED` rather than picking one arbitrarily. A member with
    no recorded source (caller didn't pass ``embedder_sources``) reports
    :data:`EMBEDDER_UNKNOWN`. Bookkeeping only — never influences which files
    land in a cluster.
    """
    seen = {sources.get(mid, EMBEDDER_UNKNOWN) for mid in member_ids}
    if len(seen) == 1:
        return next(iter(seen))
    return EMBEDDER_MIXED


def _atomic_replace(target: Path, text: str) -> None:
    """Write *text* to *target* atomically (tempfile + os.replace)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmppath = tempfile.mkstemp(
        prefix=target.name + ".",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmppath, target)
    except Exception:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
        raise


def write_cluster_report(
    clusters: Iterable[Cluster],
    output_path: Path,
    *,
    rotate: bool = True,
) -> tuple[Path, Path | None]:
    """Write *clusters* as JSONL to *output_path*.

    If ``rotate`` is True (default), also writes a timestamped sibling
    (``<stem>-<UTC-iso>.jsonl``) so historical reports are preserved;
    the canonical filename is atomically replaced with the latest run.
    Returns ``(canonical_path, timestamped_path or None)``.
    """
    payload_lines: list[str] = []
    for cluster in clusters:
        payload_lines.append(json.dumps(cluster.to_row(), sort_keys=True))
    text = "\n".join(payload_lines) + ("\n" if payload_lines else "")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    timestamped: Path | None = None
    if rotate:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        timestamped = output_path.with_name(
            f"{output_path.stem}-{ts}{output_path.suffix}"
        )
        atomic_write_text(timestamped, text)

    _atomic_replace(output_path, text)
    return output_path, timestamped


def prune_cluster_rotations(output_path: Path, *, keep: int) -> list[Path]:
    """Delete all but the *keep* most-recent timestamped rotation siblings.

    Given a canonical report path (e.g.
    ``raw/_librarian-clusters.jsonl``), finds its timestamped rotation
    siblings — ``<stem>-*<suffix>`` in the same directory, written by
    :func:`write_cluster_report` — and deletes the oldest ones, keeping
    only the ``keep`` newest. The canonical file itself never matches the
    ``<stem>-...`` glob and is never deleted.

    Rotations are named ``<stem>-%Y%m%dT%H%M%SZ<suffix>`` (fixed-width,
    zero-padded UTC), so lexicographic filename order equals chronological
    order — the sort is deterministic and does NOT depend on filesystem
    mtimes.

    Args:
        output_path: The canonical (non-timestamped) report path.
        keep: How many newest rotations to retain. ``keep <= 0`` disables
            pruning: nothing is deleted and an empty list is returned.

    Returns:
        The list of rotation paths that were deleted (empty if none).
    """
    if keep <= 0:
        return []

    output_path = Path(output_path)
    pattern = f"{output_path.stem}-*{output_path.suffix}"
    canonical_name = output_path.name
    # Only genuine `%Y%m%dT%H%M%SZ` rotations are eligible. A stray sibling
    # like `<stem>-backup<suffix>` matches the glob but sorts AFTER every
    # `-2026…` name (letters > digits), so a pure lexicographic sort would
    # let it evade pruning while a real recent rotation gets pruned. Restrict
    # to the timestamp shape so the sort is over homogeneous, sortable names.
    stamp_re = re.compile(
        rf"^{re.escape(output_path.stem)}-\d{{8}}T\d{{6}}Z"
        rf"{re.escape(output_path.suffix)}$"
    )
    rotations = sorted(
        p
        for p in output_path.parent.glob(pattern)
        if p.name != canonical_name and p.is_file() and stamp_re.match(p.name)
    )
    if len(rotations) <= keep:
        return []

    doomed = rotations[:-keep]
    pruned: list[Path] = []
    for path in doomed:
        path.unlink()
        pruned.append(path)
    return pruned


def resolve_cluster_output_path(
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
) -> Path:
    """Resolve the cluster report path from config, relative to *knowledge_root*."""
    if config is None:
        from athenaeum.config import load_config

        config = load_config(knowledge_root)
    librarian_cfg = config.get("librarian")
    if not isinstance(librarian_cfg, dict):
        librarian_cfg = {}
    raw_path = librarian_cfg.get("cluster_output") or DEFAULT_CLUSTER_OUTPUT
    candidate = Path(str(raw_path)).expanduser()
    if not candidate.is_absolute():
        candidate = knowledge_root / candidate
    return candidate


def resolve_cluster_threshold(
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
) -> float:
    """Resolve the cluster threshold from config; fall back to default."""
    if config is None:
        from athenaeum.config import load_config

        config = load_config(knowledge_root)
    librarian_cfg = config.get("librarian")
    if not isinstance(librarian_cfg, dict):
        librarian_cfg = {}
    value = librarian_cfg.get("cluster_threshold")
    try:
        if value is None:
            return DEFAULT_CLUSTER_THRESHOLD
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_CLUSTER_THRESHOLD


def resolve_rotation_retention(
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
) -> int:
    """Resolve the rotation-retention window (issue athenaeum#311).

    Precedence: ``ATHENAEUM_ROTATION_RETENTION`` env > ``librarian.
    rotation_retention`` yaml > :data:`DEFAULT_ROTATION_RETENTION` (30).
    Mirrors :func:`resolve_cluster_threshold`, plus an env override in the
    style of ``librarian_max_files`` so a scheduled deployment can tune the
    window without editing config. A resolved value ``<= 0`` disables
    pruning (keep all rotations). ``bool`` yaml values (an ``int`` subclass)
    are rejected so ``rotation_retention: yes`` cannot become ``1``.
    """
    env = os.environ.get("ATHENAEUM_ROTATION_RETENTION")
    if env is not None:
        try:
            return int(env)
        except (TypeError, ValueError):
            pass

    if config is None:
        from athenaeum.config import load_config

        config = load_config(knowledge_root)
    librarian_cfg = config.get("librarian")
    if not isinstance(librarian_cfg, dict):
        librarian_cfg = {}
    value = librarian_cfg.get("rotation_retention")
    # A Python ``bool`` is an ``int`` subclass — reject it up front so
    # ``rotation_retention: yes`` cannot become a count of 1. Otherwise
    # coerce via ``int(...)`` for parity with ``resolve_cluster_threshold``'s
    # ``float(...)`` contract, so quoted yaml (``"5"``) resolves correctly.
    if isinstance(value, bool):
        return DEFAULT_ROTATION_RETENTION
    try:
        if value is None:
            return DEFAULT_ROTATION_RETENTION
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_ROTATION_RETENTION
