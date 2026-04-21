# SPDX-License-Identifier: Apache-2.0
"""Auto-memory cluster pass (issue #196, C2).

Groups :class:`~athenaeum.models.AutoMemoryFile` records (discovered by
C1, :func:`athenaeum.librarian.discover_auto_memory_files`) into
near-duplicate clusters using the existing chromadb ``VectorBackend``
embedder. Writes a JSONL report that C3 (the merge pass) consumes.

Scope for this module:

- Input: a list of ``AutoMemoryFile`` records + a chromadb cache dir
  that already holds embeddings for those records (the recall index
  build populates this via ``extra_roots``, see issue #192).
- Output: a JSONL report at ``raw/_librarian-clusters.jsonl`` (path
  configurable) with one row per cluster. A timestamped sibling file
  is written on every run; the canonical name is atomically replaced.
- Clustering: single-linkage on pairwise cosine similarity, threshold
  configured by ``librarian.cluster_threshold`` (default 0.6).
- Singletons: size-1 clusters pass through unchanged. There is NO
  minimum-cluster-size filter.

Out of scope (deliberate — later lanes):

- Merging clusters into wiki entries (C3, #197).
- Contradiction detection inside a cluster (C4, #198).

The embedder MUST be the shared chromadb collection. This module does
not import sentence_transformers, openai, cohere, or any other
embedding provider.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.models import AutoMemoryFile

log = logging.getLogger(__name__)

# Default cache dir mirrors search.py's expectations — callers usually
# pass this in explicitly (librarian.run() resolves it against the
# knowledge root), but we expose a default for shell/library callers.
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "athenaeum"

# Default threshold — empirically tuned against the voltaire fixture
# (test_librarian_clusters.py::TestClusterVoltaireFixture). MiniLM
# cosines on 4000-char prefixes put the typo clone at ~0.59 against the
# anchor "project_voltaire_nanoclaw.md" and the iMessage variant at
# ~0.70; unrelated singletons land at ~0.24 (sentry) and ~0.07 (user).
# At 0.55, single-linkage pulls the typo in through the anchor while
# leaving the singletons alone. 0.6 was too tight (typo fell out); a
# drop to 0.55 is the minimum that preserves the load-bearing property
# of the regression fixture. Tunable via ``librarian.cluster_threshold``.
DEFAULT_CLUSTER_THRESHOLD = 0.55

# Default output path, resolved relative to the knowledge root.
DEFAULT_CLUSTER_OUTPUT = "raw/_librarian-clusters.jsonl"


@dataclass
class Cluster:
    """A group of auto-memory files judged to be near-duplicates."""

    cluster_id: str
    member_paths: list[str] = field(default_factory=list)
    centroid_score: float = 0.0
    rationale: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "member_paths": list(self.member_paths),
            "centroid_score": float(self.centroid_score),
            "rationale": self.rationale,
        }


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors. Zero on 0-norm."""
    if len(a) != len(b):
        return 0.0
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
) -> dict[str, list[float]]:
    """Resolve an embedding for every file, keyed by absolute path string.

    Strategy:
      1. Try ``VectorBackend.fetch_embeddings`` with the recall-index
         ids — this is the hot path and reuses the existing collection.
      2. For any file whose id was missing from the collection, fall
         back to the hashing-trick vector. This keeps the pipeline
         robust against a stale or partial recall index.
    """
    # Build id → AutoMemoryFile map so we can translate hits back.
    id_to_file: dict[str, AutoMemoryFile] = {}
    for am in files:
        idx_id = _indexed_id_for(am, extra_roots)
        if idx_id is None:
            continue
        id_to_file[idx_id] = am

    embeddings: dict[str, list[float]] = {}
    hit_ids: set[str] = set()
    if id_to_file:
        try:
            from athenaeum.search import VectorBackend  # noqa: WPS433
            backend = VectorBackend()
            raw_hits = backend.fetch_embeddings(id_to_file.keys(), cache_dir)
        except Exception as exc:  # noqa: BLE001
            log.debug("VectorBackend.fetch_embeddings failed: %s", exc)
            raw_hits = {}
        for idx_id, vec in raw_hits.items():
            am = id_to_file.get(idx_id)
            if am is None:
                continue
            embeddings[str(am.path)] = vec
            hit_ids.add(idx_id)

    # Fallback for any misses.
    missing = [am for am in files if str(am.path) not in embeddings]
    if missing:
        log.debug(
            "cluster embeddings: %d of %d served from chromadb, %d fell back",
            len(files) - len(missing), len(files), len(missing),
        )
        embeddings.update(_fallback_embeddings(missing))

    return embeddings


def _build_adjacency(
    file_ids: Sequence[str],
    embeddings: dict[str, list[float]],
    threshold: float,
) -> list[set[int]]:
    """Return adjacency list (index → neighbour set) at ``cosine >= threshold``."""
    n = len(file_ids)
    adj: list[set[int]] = [set() for _ in range(n)]
    vecs = [embeddings.get(fid) for fid in file_ids]
    for i in range(n):
        vi = vecs[i]
        if vi is None:
            continue
        for j in range(i + 1, n):
            vj = vecs[j]
            if vj is None:
                continue
            if _cosine(vi, vj) >= threshold:
                adj[i].add(j)
                adj[j].add(i)
    return adj


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
            total += _cosine(vi, vj)
            pairs += 1
    return total / pairs if pairs else 1.0


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
    boring = {"the", "and", "for", "with", "memory", "feedback", "project",
              "reference", "user", "recall", "note", "auto"}
    interesting = sorted(t for t in common if t not in boring)
    return interesting[:limit]


def cluster_auto_memory_files(
    files: Sequence[AutoMemoryFile],
    *,
    extra_roots: Sequence[Path],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    threshold: float = DEFAULT_CLUSTER_THRESHOLD,
) -> list[Cluster]:
    """Group auto-memory files into near-duplicate clusters.

    Args:
        files: Output of
            :func:`athenaeum.librarian.discover_auto_memory_files`.
        extra_roots: The same list recall uses for index scans — needed
            to translate absolute file paths into the ``<root>/<rel>``
            ids chromadb stores. Usually obtained via
            :func:`athenaeum.config.resolve_extra_intake_roots`.
        cache_dir: Root of the shared embedder cache (chromadb is at
            ``<cache_dir>/wiki-vectors/``).
        threshold: Cosine similarity cutoff for single-linkage
            clustering. Defaults to :data:`DEFAULT_CLUSTER_THRESHOLD`.

    Returns:
        A list of :class:`Cluster` records. Empty input → empty list.
        Singletons are returned as size-1 clusters unchanged.
    """
    if not files:
        return []

    embeddings = _resolve_embeddings(
        files, extra_roots=list(extra_roots), cache_dir=cache_dir,
    )
    # Index files by the string form of their absolute path (stable and
    # unique; avoids Path equality surprises across tempdirs).
    file_ids: list[str] = [str(am.path) for am in files]

    adj = _build_adjacency(file_ids, embeddings, threshold)
    components = _single_linkage(adj)

    # Stable cluster id: origin scope of the first member + a sequential
    # index, so re-runs on the same input produce the same ids and C3's
    # merge receipts stay traceable.
    clusters: list[Cluster] = []
    for seq, component in enumerate(components):
        members = [files[i] for i in component]
        scope_hint = members[0].origin_scope.lstrip("-_") or "unscoped"
        cluster_id = f"{scope_hint}-{seq:04d}"
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

        centroid = _mean_intra_similarity(component, file_ids, embeddings)
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
                f"mean intra-sim {centroid:.2f}"
            )
        clusters.append(Cluster(
            cluster_id=cluster_id,
            member_paths=relpaths,
            centroid_score=centroid,
            rationale=rationale,
        ))

    return clusters


def _atomic_replace(target: Path, text: str) -> None:
    """Write *text* to *target* atomically (tempfile + os.replace)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmppath = tempfile.mkstemp(
        prefix=target.name + ".", dir=str(target.parent),
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
        timestamped.write_text(text, encoding="utf-8")

    _atomic_replace(output_path, text)
    return output_path, timestamped


def resolve_cluster_output_path(
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
) -> Path:
    """Resolve the cluster report path from config, relative to *knowledge_root*."""
    if config is None:
        from athenaeum.config import load_config
        config = load_config(knowledge_root)
    librarian_cfg = config.get("librarian") or {}
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
    librarian_cfg = config.get("librarian") or {}
    value = librarian_cfg.get("cluster_threshold")
    try:
        if value is None:
            return DEFAULT_CLUSTER_THRESHOLD
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_CLUSTER_THRESHOLD
