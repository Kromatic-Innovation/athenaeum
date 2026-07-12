# SPDX-License-Identifier: Apache-2.0
"""Delta-scoped incremental compile (issue #370 PR2).

Computes the SUBSET of the auto-memory corpus that a set of changed files can
affect, so the deterministic ``client=None`` compile path (session_end / ingest
tier0) can re-cluster + re-merge only the affected clusters instead of the whole
corpus. Proven byte-equivalent to the whole-corpus path by
``tests/test_delta_compile_equivalence.py``.

Key correctness property (the equivalence guarantee):

The change-closure below determines single-linkage adjacency with the EXACT
cosine over the SAME embeddings the full cluster pass uses
(:func:`athenaeum.clusters._resolve_embeddings` — chromadb-stored MiniLM vectors,
with the deterministic hashing-trick fallback for any file the recall index has
not embedded). It does NOT rely on chromadb's approximate-nearest-neighbor
ranking, so the affected-cluster set is a provable SUPERSET of the clusters that
actually change between the prior report and a full re-cluster — no ANN recall
gap can silently drop an affected cluster. The by-vector ANN primitive
:meth:`athenaeum.search.VectorBackend.query_neighbors` is provided for a future
memory-bounded closure on very large corpora; this exact closure is used by
default because #370 PR2's mandate is correctness > speed and fetching the
stored vectors is cheap (a pure read — never re-embeds).

Fallback triggers (each returns ``None`` from :func:`compute_affected_clusters`
so the caller runs a full whole-corpus compile, logging the reason):

- D1  no prior cluster report (nothing to scope against).
- D2  closure blow-up: affected clusters or pooled members exceed the caps.
- D3  a CHANGED (non-new) file that should be indexed missed chromadb — its
      only vector is the hashing fallback, so its neighbors are untrustworthy.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.clusters import (
    Cluster,
    _cosine,
    _fallback_embeddings,
    _indexed_id_for,
)
from athenaeum.models import AutoMemoryFile

log = logging.getLogger(__name__)


@dataclass
class AffectedScope:
    """The delta-scoped subset a set of changed files can affect.

    - ``affected_ids``: the PRIOR cluster ids (from the existing report) that
      the change touches — these rows are removed from the report and replaced
      by the re-clustered ``pool``'s rows (see :func:`splice_cluster_report`).
    - ``pool``: the exact set of live :class:`AutoMemoryFile` records to re-run
      through the cluster pass (the affected clusters' members + the changed /
      new files), in the caller's discovery order so member-path ordering in the
      re-clustered rows matches a full run byte-for-byte.
    - ``new_paths``: changed files that had no prior cluster (brand-new intake).
    """

    affected_ids: set[str]
    pool: list[AutoMemoryFile]
    new_paths: set[Path]


def _relpath_for(path: Path, extra_roots: Sequence[Path]) -> str:
    """Relpath of *path* under the first matching extra root (POSIX), else str.

    Mirrors the member-path scheme :func:`athenaeum.clusters.cluster_auto_memory_files`
    writes, so a changed file's relpath keys into the prior report's
    ``member_paths``. Works for a removed file (pure path arithmetic; no stat).
    """
    ampath = path.resolve()
    for root in extra_roots:
        try:
            return ampath.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    return str(path)


def _resolve_all_embeddings(
    files: Sequence[AutoMemoryFile],
    *,
    extra_roots: Sequence[Path],
    cache_dir: Path,
) -> tuple[dict[str, list[float]], set[str]]:
    """Return ``({abspath_str: vector}, hit_relpaths)``.

    Resolves the SAME per-file vectors the full cluster pass uses — chromadb
    hits first, hashing-trick fallback for misses — and additionally reports
    which files were served from chromadb (by relpath) so the caller can apply
    the D3 stale-index guard. Kept separate from
    :func:`athenaeum.clusters._resolve_embeddings` because that helper does not
    surface the hit set.
    """
    id_to_file: dict[str, AutoMemoryFile] = {}
    for am in files:
        idx_id = _indexed_id_for(am, extra_roots)
        if idx_id is not None:
            id_to_file[idx_id] = am

    embeddings: dict[str, list[float]] = {}
    hit_relpaths: set[str] = set()
    if id_to_file:
        try:
            from athenaeum.search import VectorBackend

            raw_hits = VectorBackend().fetch_embeddings(id_to_file.keys(), cache_dir)
        except Exception as exc:  # noqa: BLE001 — degrade to fallback on any error
            log.debug("delta: fetch_embeddings failed: %s", exc)
            raw_hits = {}
        for idx_id, vec in raw_hits.items():
            am = id_to_file.get(idx_id)
            if am is None:
                continue
            embeddings[str(am.path)] = vec
            hit_relpaths.add(_relpath_for(am.path, extra_roots))

    missing = [am for am in files if str(am.path) not in embeddings]
    if missing:
        embeddings.update(_fallback_embeddings(missing))
    return embeddings, hit_relpaths


def compute_affected_clusters(
    changed: set[Path],
    prior_rows: list[dict[str, Any]],
    all_files: Sequence[AutoMemoryFile],
    *,
    extra_roots: Sequence[Path],
    cache_dir: Path,
    threshold: float,
    max_affected_clusters: int,
    max_affected_members: int,
) -> AffectedScope | None:
    """Compute the delta scope, or ``None`` to signal a full-compile fallback.

    Args:
        changed: Absolute paths of auto-memory files that were added or modified
            (and, incidentally, removed — a path no longer in *all_files* is
            treated as a member that LEFT its prior cluster).
        prior_rows: Rows of the existing cluster report (``member_paths`` are
            POSIX relpaths under the first extra root).
        all_files: The full current auto-memory corpus (discovery order).
        extra_roots: Intake roots, for relpath ↔ absolute translation.
        cache_dir: Shared embedder cache (chromadb lives under it).
        threshold: Single-linkage cosine cutoff (must match the cluster pass).
        max_affected_clusters / max_affected_members: D2 blow-up caps.

    Returns:
        An :class:`AffectedScope`, or ``None`` for D1/D2/D3 (a WARNING is logged
        with the reason before returning ``None``).
    """
    if not prior_rows:
        log.warning("delta: no prior cluster report (D1) — full compile")
        return None

    # Prior report maps.
    path_to_cid: dict[str, str] = {}
    cid_to_relpaths: dict[str, list[str]] = {}
    for row in prior_rows:
        cid = str(row.get("cluster_id", ""))
        relpaths = [str(m) for m in row.get("member_paths", [])]
        cid_to_relpaths[cid] = relpaths
        for rp in relpaths:
            path_to_cid[rp] = cid

    # Live corpus maps.
    file_by_relpath: dict[str, AutoMemoryFile] = {}
    for am in all_files:
        file_by_relpath[_relpath_for(am.path, extra_roots)] = am

    embeddings, hit_relpaths = _resolve_all_embeddings(
        all_files, extra_roots=extra_roots, cache_dir=cache_dir
    )

    # Translate changed absolute paths → relpaths; classify new vs known;
    # apply the D3 stale-index guard on changed-but-not-new files.
    changed_relpaths: set[str] = set()
    new_paths: set[Path] = set()
    for p in changed:
        rp = _relpath_for(p, extra_roots)
        changed_relpaths.add(rp)
        prior_cid = path_to_cid.get(rp)
        is_live = rp in file_by_relpath
        if prior_cid is None:
            # Brand-new intake (no prior cluster). A chromadb miss here is
            # acceptable: a full run would embed it with the same hashing
            # fallback, so equivalence still holds (F4).
            if is_live:
                new_paths.add(p)
            continue
        # Known file (has a prior cluster). If it is still live but its vector
        # missed chromadb, the recall index is stale for a file we expected to
        # be embedded → its neighbours are untrustworthy (D3).
        if is_live and rp not in hit_relpaths:
            log.warning(
                "delta: changed file %s is not in the chromadb index (D3) — "
                "full compile",
                rp,
            )
            return None

    # Exact single-linkage closure over the SAME embeddings the cluster pass
    # uses. Only files incident to a changed file (directly or transitively
    # through another changed file) can change cluster membership, because every
    # edge among UNCHANGED files already existed in the prior report — so any
    # unchanged pair above threshold is already co-clustered. We therefore only
    # need to expand from the changed files and the members of the clusters they
    # pull in; re-querying an unchanged member can never surface a NEW affected
    # cluster (its cross-cluster edges, if any, would have merged those clusters
    # already). The frontier still walks pooled members for defensive closure,
    # bounded by the D2 caps.
    all_relpaths = list(file_by_relpath.keys())
    vec_by_relpath: dict[str, list[float]] = {}
    for rp, am in file_by_relpath.items():
        v = embeddings.get(str(am.path))
        if v is not None:
            vec_by_relpath[rp] = v

    affected_ids: set[str] = set()
    pool_relpaths: set[str] = set()
    frontier: deque[str] = deque()

    def _add_cluster(cid: str) -> None:
        if cid in affected_ids:
            return
        affected_ids.add(cid)
        for mp in cid_to_relpaths.get(cid, []):
            if mp in file_by_relpath and mp not in pool_relpaths:
                pool_relpaths.add(mp)
                frontier.append(mp)

    def _add_file(rp: str) -> None:
        if rp in file_by_relpath and rp not in pool_relpaths:
            pool_relpaths.add(rp)
            frontier.append(rp)

    # Seed: each changed file's prior cluster (covers "member left cluster")
    # plus the changed/new file itself (covers "member joined / new cluster").
    for rp in changed_relpaths:
        cid = path_to_cid.get(rp)
        if cid is not None:
            _add_cluster(cid)
        _add_file(rp)

    def _over_caps() -> bool:
        return (
            len(affected_ids) > max_affected_clusters
            or len(pool_relpaths) > max_affected_members
        )

    while frontier:
        if _over_caps():
            log.warning(
                "delta: closure blow-up (D2) — %d affected cluster(s) / %d "
                "pooled member(s) exceed caps (%d / %d); full compile",
                len(affected_ids),
                len(pool_relpaths),
                max_affected_clusters,
                max_affected_members,
            )
            return None
        rp = frontier.popleft()
        vi = vec_by_relpath.get(rp)
        if vi is None:
            continue
        for other in all_relpaths:
            if other == rp:
                continue
            vj = vec_by_relpath.get(other)
            if vj is None:
                continue
            if _cosine(vi, vj) >= threshold:
                other_cid = path_to_cid.get(other)
                if other_cid is not None:
                    _add_cluster(other_cid)
                else:
                    # A confirmed neighbour with no prior cluster (e.g. another
                    # brand-new file) still belongs in the pool so the
                    # re-cluster can link them.
                    _add_file(other)

    if _over_caps():
        log.warning(
            "delta: closure blow-up (D2) — %d affected cluster(s) / %d pooled "
            "member(s) exceed caps (%d / %d); full compile",
            len(affected_ids),
            len(pool_relpaths),
            max_affected_clusters,
            max_affected_members,
        )
        return None

    # Preserve discovery order so re-clustered member_paths match a full run.
    pool = [
        am for am in all_files if _relpath_for(am.path, extra_roots) in pool_relpaths
    ]

    log.info(
        "delta: %d changed file(s) → %d affected cluster(s), %d pooled member(s)",
        len(changed),
        len(affected_ids),
        len(pool),
    )
    return AffectedScope(affected_ids=affected_ids, pool=pool, new_paths=new_paths)


def splice_cluster_report(
    prior_rows: list[dict[str, Any]],
    affected_ids: set[str],
    new_partial: Sequence[Cluster],
) -> list[Cluster]:
    """Replace the affected prior rows with the freshly re-clustered pool rows.

    ``[row for row not in affected_ids] + new_partial``. The kept rows are
    returned as :class:`Cluster` objects (rebuilt from the JSONL row) so the
    whole report can be re-serialised by
    :func:`athenaeum.clusters.write_cluster_report` in one shape.
    """
    spliced: list[Cluster] = []
    for row in prior_rows:
        if str(row.get("cluster_id", "")) in affected_ids:
            continue
        spliced.append(
            Cluster(
                cluster_id=str(row.get("cluster_id", "")),
                member_paths=[str(m) for m in row.get("member_paths", [])],
                centroid_score=float(row.get("centroid_score", 1.0) or 0.0),
                rationale=str(row.get("rationale", "")),
            )
        )
    spliced.extend(new_partial)
    return spliced
