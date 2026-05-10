# SPDX-License-Identifier: Apache-2.0
"""Cross-scope contradiction detection helpers (issue #125, #81-A).

The base C4 detector (:mod:`athenaeum.contradictions`) operates on one
cluster at a time. Each cluster is scoped to a single ``raw/auto-memory/``
subdirectory because C2's clustering is keyed by origin scope. That misses
two manifestations the merge pipeline still has to catch:

1. **Raw-vs-raw at merge time** — two raw entries that should be compared
   sit in different scope folders (e.g. one under
   ``-Users-tristankromer-Code-foo`` and one under
   ``-Users-tristankromer-Code``). The per-scope clustering never groups
   them, so the detector never sees them.
2. **Wiki-vs-wiki post-merge** — distinct ``wiki/auto-*.md`` entries
   merged from different clusters can still contradict each other, and
   the librarian deletes the raw originals after a successful merge.

This module adds two cooperative passes the merge orchestrator can
toggle in via ``ATHENAEUM_CROSS_SCOPE_MODE``:

- **ancestor pooling**: when materializing a per-scope cluster, also
  include every member from any *ancestor* scope (root scope plus every
  prefix). De-dupe by absolute path; if the pooled cluster exceeds
  ``cluster_size_cap``, split into ordered chunks (newest-first by
  ``created`` frontmatter, ``mtime`` fallback) and run the detector
  once per chunk.
- **similarity sweep**: a second-pass embedding cross-product over BOTH
  ``raw/auto-memory/**`` AND ``wiki/**``. Any pair whose cosine
  similarity exceeds the configured threshold and that is NOT already
  contained in a single cluster is fed to the detector as a 2-member
  pseudo-cluster. Embeddings come from the recall index — we do NOT
  open a second chromadb collection.

Modes:

- ``off`` — pass through; no cross-scope work.
- ``ancestor`` (default) — ancestor pooling only.
- ``similarity`` — per-scope clusters + similarity sweep.
- ``both`` — ancestor pooling first, similarity sweep second over the
  remaining unpaired entries.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from athenaeum.models import AutoMemoryFile, parse_frontmatter

log = logging.getLogger(__name__)


# Env var + config defaults --------------------------------------------------

ENV_VAR = "ATHENAEUM_CROSS_SCOPE_MODE"
DEFAULT_MODE = "ancestor"
VALID_MODES = ("off", "ancestor", "similarity", "both")

DEFAULT_CLUSTER_SIZE_CAP = 25
DEFAULT_SIMILARITY_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def resolve_cross_scope_mode(config: dict[str, Any] | None = None) -> str:
    """Resolve cross-scope mode. Env var wins over config; default ``ancestor``."""
    env_val = os.environ.get(ENV_VAR)
    if env_val:
        env_val = env_val.strip().lower()
        if env_val in VALID_MODES:
            return env_val
        log.warning(
            "cross_scope: invalid %s=%r; falling back to %s",
            ENV_VAR,
            env_val,
            DEFAULT_MODE,
        )
    if config is not None:
        contradiction_cfg = config.get("contradiction") or {}
        cfg_val = contradiction_cfg.get("cross_scope_mode")
        if isinstance(cfg_val, str):
            cfg_val = cfg_val.strip().lower()
            if cfg_val in VALID_MODES:
                return cfg_val
            log.warning(
                "cross_scope: invalid contradiction.cross_scope_mode=%r; "
                "falling back to %s",
                cfg_val,
                DEFAULT_MODE,
            )
    return DEFAULT_MODE


def resolve_cluster_size_cap(config: dict[str, Any] | None = None) -> int:
    """Resolve the pooled-cluster size cap (default 25)."""
    if config is None:
        return DEFAULT_CLUSTER_SIZE_CAP
    contradiction_cfg = config.get("contradiction") or {}
    raw = contradiction_cfg.get("cluster_size_cap")
    try:
        if raw is None:
            return DEFAULT_CLUSTER_SIZE_CAP
        cap = int(raw)
        return cap if cap > 0 else DEFAULT_CLUSTER_SIZE_CAP
    except (TypeError, ValueError):
        return DEFAULT_CLUSTER_SIZE_CAP


def resolve_similarity_threshold(config: dict[str, Any] | None = None) -> float:
    """Resolve cosine similarity threshold for cross-scope sweep (default 0.85)."""
    if config is None:
        return DEFAULT_SIMILARITY_THRESHOLD
    contradiction_cfg = config.get("contradiction") or {}
    raw = contradiction_cfg.get("similarity_threshold")
    try:
        if raw is None:
            return DEFAULT_SIMILARITY_THRESHOLD
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SIMILARITY_THRESHOLD


# ---------------------------------------------------------------------------
# Ancestor scope walking
# ---------------------------------------------------------------------------


def scope_ancestors(scope: str) -> list[str]:
    """Return ancestor scope identifiers for a path-hash scope.

    Scope identifiers follow the convention ``-Users-tristankromer-Code-foo``
    (slashes replaced by dashes, leading dash). Ancestors are produced by
    successively dropping trailing segments. ``_unscoped`` and any scope
    not starting with ``-`` has no ancestors.

    Example:
        ``-Users-tristankromer-Code-foo`` →
        ``["-Users-tristankromer-Code", "-Users-tristankromer", "-Users"]``
    """
    if not isinstance(scope, str) or not scope.startswith("-"):
        return []
    parts = scope.lstrip("-").split("-")
    if len(parts) <= 1:
        return []
    out: list[str] = []
    for i in range(len(parts) - 1, 0, -1):
        out.append("-" + "-".join(parts[:i]))
    return out


# ---------------------------------------------------------------------------
# Ancestor pooling + size-cap chunking
# ---------------------------------------------------------------------------


def _created_sort_key(am: AutoMemoryFile) -> tuple[int, str]:
    """Sort key for newest-first ordering. Uses frontmatter ``created`` if present.

    Returns ``(rank, tiebreak)``. Rank is negated so a sort ascending puts
    newer first. We coerce ``created`` to a string and rely on ISO ordering;
    missing/unparseable creates fall back to mtime.
    """
    try:
        text = am.path.read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(text)
        created = meta.get("created") if isinstance(meta, dict) else None
        if isinstance(created, str) and created:
            # Negate by inverting via a lexicographically descending key
            # achieved with a sentinel: pair (0, -ord-string) is awkward;
            # simpler: store created and negate at the call site via reverse=True.
            return (0, created)
    except (OSError, UnicodeDecodeError):
        pass
    try:
        mtime = am.path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (1, f"{mtime:020.6f}")


def sort_newest_first(members: Sequence[AutoMemoryFile]) -> list[AutoMemoryFile]:
    """Return members ordered newest-first by ``created`` (or mtime fallback)."""
    return sorted(members, key=_created_sort_key, reverse=True)


def pool_cluster_with_ancestors(
    cluster_members: Sequence[AutoMemoryFile],
    all_members: Sequence[AutoMemoryFile],
) -> list[AutoMemoryFile]:
    """Pool *cluster_members* with members from any ancestor scope.

    The pool de-dupes by resolved absolute path. The original cluster
    members are kept first (so downstream receipts still attribute to the
    primary cluster), with ancestor-scope members appended in their
    discovery order.
    """
    if not cluster_members:
        return []
    scopes = {am.origin_scope for am in cluster_members}
    ancestors: set[str] = set()
    for s in scopes:
        ancestors.update(scope_ancestors(s))
    if not ancestors:
        return list(cluster_members)

    seen: set[str] = set()
    pooled: list[AutoMemoryFile] = []
    for am in cluster_members:
        try:
            key = str(am.path.resolve())
        except OSError:
            key = str(am.path)
        if key in seen:
            continue
        seen.add(key)
        pooled.append(am)
    for am in all_members:
        if am.origin_scope not in ancestors:
            continue
        try:
            key = str(am.path.resolve())
        except OSError:
            key = str(am.path)
        if key in seen:
            continue
        seen.add(key)
        pooled.append(am)
    return pooled


def chunk_by_cap(
    members: Sequence[AutoMemoryFile],
    cap: int,
) -> list[list[AutoMemoryFile]]:
    """Split *members* into newest-first chunks of size ``<= cap``.

    Returns ``[list(members)]`` if ``len(members) <= cap`` (no work to do).
    Otherwise sorts newest-first and slices into ``ceil(N/cap)`` chunks.
    """
    if cap <= 0 or len(members) <= cap:
        return [list(members)]
    ordered = sort_newest_first(members)
    chunks: list[list[AutoMemoryFile]] = []
    for i in range(0, len(ordered), cap):
        chunks.append(ordered[i : i + cap])
    return chunks


# ---------------------------------------------------------------------------
# Similarity sweep
# ---------------------------------------------------------------------------


@dataclass
class SimilarityCandidate:
    """One pair of paths that exceeded the cosine threshold."""

    a_path: Path
    b_path: Path
    similarity: float
    a_scope: str = ""
    b_scope: str = ""


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    import math

    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _wiki_indexed_id(wiki_path: Path, wiki_root: Path) -> str | None:
    """Recall index id for a wiki page, or None if not under wiki_root."""
    try:
        rel = wiki_path.resolve().relative_to(wiki_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    return rel


def _raw_indexed_id(am_path: Path, extra_roots: Sequence[Path]) -> str | None:
    """Recall index id for a raw auto-memory file."""
    try:
        ampath = am_path.resolve()
    except OSError:
        return None
    for root in extra_roots:
        try:
            rel = ampath.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        return f"{root.name}/{rel}"
    return None


def cross_scope_similarity_pairs(
    auto_memory_files: Sequence[AutoMemoryFile],
    *,
    wiki_files: Sequence[Path] = (),
    wiki_root: Path | None = None,
    extra_roots: Sequence[Path] = (),
    cache_dir: Path,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    excluded_pair_keys: Iterable[tuple[str, str]] = (),
    embedding_provider: Any = None,
) -> list[SimilarityCandidate]:
    """Find candidate pairs via cosine similarity over the recall index.

    Args:
        auto_memory_files: All discovered raw auto-memory files.
        wiki_files: Optional list of compiled ``wiki/auto-*.md`` paths to
            include in the sweep so wiki-vs-wiki contradictions can fire
            after raw originals are deleted.
        wiki_root: Wiki root directory (used to compute recall-index ids
            for ``wiki_files``). Required if ``wiki_files`` is non-empty.
        extra_roots: Same list passed to :mod:`athenaeum.clusters` —
            translates absolute auto-memory paths into recall index ids.
        cache_dir: Shared chromadb cache root (``<cache_dir>/wiki-vectors``).
        threshold: Cosine cutoff (default 0.85).
        excluded_pair_keys: Iterable of ``(path_a, path_b)`` string-key
            pairs already contained in a single cluster — these are
            filtered out so the per-scope pass's coverage isn't repeated.
            Order-insensitive (we sort the key).
        embedding_provider: Optional override for testing. Must expose a
            ``fetch_embeddings(ids, cache_dir)`` method. Defaults to
            :class:`athenaeum.search.VectorBackend`.

    Returns:
        List of :class:`SimilarityCandidate`, ordered by descending
        similarity. Empty list when chromadb is unavailable or no pair
        meets the threshold.
    """
    excluded: set[tuple[str, str]] = set()
    for a, b in excluded_pair_keys:
        excluded.add(tuple(sorted((str(a), str(b)))))

    # Build id → (path, scope) map for both raw + wiki.
    id_to_entry: dict[str, tuple[Path, str]] = {}
    for am in auto_memory_files:
        idx = _raw_indexed_id(am.path, extra_roots)
        if idx is None:
            continue
        id_to_entry[idx] = (am.path, am.origin_scope)
    if wiki_files and wiki_root is not None:
        for wp in wiki_files:
            idx = _wiki_indexed_id(wp, wiki_root)
            if idx is None:
                continue
            id_to_entry[idx] = (wp, "<wiki>")

    if len(id_to_entry) < 2:
        return []

    # Fetch embeddings from the shared collection (no new client/collection).
    if embedding_provider is None:
        try:
            from athenaeum.search import VectorBackend

            embedding_provider = VectorBackend()
        except Exception as exc:  # noqa: BLE001
            log.debug("cross_scope similarity: VectorBackend unavailable: %s", exc)
            return []

    try:
        raw_embeds = embedding_provider.fetch_embeddings(
            id_to_entry.keys(),
            cache_dir,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("cross_scope similarity: fetch_embeddings failed: %s", exc)
        return []

    items: list[tuple[str, Path, str, list[float]]] = []
    for idx, vec in raw_embeds.items():
        entry = id_to_entry.get(idx)
        if entry is None:
            continue
        path, scope = entry
        items.append((idx, path, scope, list(vec)))

    candidates: list[SimilarityCandidate] = []
    for i in range(len(items)):
        idx_i, path_i, scope_i, vec_i = items[i]
        for j in range(i + 1, len(items)):
            idx_j, path_j, scope_j, vec_j = items[j]
            sim = _cosine(vec_i, vec_j)
            if sim < threshold:
                continue
            key = tuple(sorted((str(path_i), str(path_j))))
            if key in excluded:
                continue
            candidates.append(
                SimilarityCandidate(
                    a_path=path_i,
                    b_path=path_j,
                    similarity=sim,
                    a_scope=scope_i,
                    b_scope=scope_j,
                )
            )
    candidates.sort(key=lambda c: c.similarity, reverse=True)
    return candidates


def candidate_to_auto_memory_files(
    candidate: SimilarityCandidate,
) -> list[AutoMemoryFile]:
    """Wrap a similarity candidate's two paths as :class:`AutoMemoryFile` records.

    Reads frontmatter on demand to populate ``name``/``description``. The
    detector consumes the file body itself, so this stub is sufficient
    for 2-member detector calls.
    """
    out: list[AutoMemoryFile] = []
    for path, scope in (
        (candidate.a_path, candidate.a_scope),
        (candidate.b_path, candidate.b_scope),
    ):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        meta, _ = parse_frontmatter(text) if text else ({}, "")
        name = ""
        description = ""
        memory_type = "unknown"
        if isinstance(meta, dict):
            name = str(meta.get("name", "") or "")
            description = str(meta.get("description", "") or "")
            memory_type = str(meta.get("type", "unknown") or "unknown")
        out.append(
            AutoMemoryFile(
                path=path,
                origin_scope=scope or "<unknown>",
                memory_type=memory_type,
                name=name,
                description=description,
            )
        )
    return out
