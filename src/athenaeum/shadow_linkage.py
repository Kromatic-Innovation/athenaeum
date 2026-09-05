# SPDX-License-Identifier: Apache-2.0
"""Shadow-mode complete-linkage measurement (issue athenaeum#713, artifact 1).

Reproduce with: ``athenaeum measure shadow-linkage`` (see
:data:`REPRODUCE_COMMAND` below; issue athenaeum#1095 AC7 requires the exact
invocation live in this module's own docstring, not only ``CHANGELOG.md``).

The v6 comparator slice (child of athenaeum#709) is gated on knowing the TRUE
post-athenaeum#681 candidate/proposal population before it is built — the
historical "~36 proposals/night" anchor was measured under single-linkage
with the suppression gate discarding the giant component's work nightly, so
it describes the OLD regime's survivors, not what athenaeum#681's complete-linkage
formation actually produces now. This module is the read-only instrument
that answers that question, in **shadow mode**: it runs the exact same
complete-linkage formation the wiki-dedupe pass uses
(:func:`athenaeum.clusters.cluster_auto_memory_files`, reused — NOT
reimplemented, per the athenaeum#803 discipline :mod:`athenaeum.wiki_dedupe` already
documents) over the live wiki-page population, but never writes a proposal,
never touches ``wiki/_pending_merges.md``, and makes **zero LLM calls** —
only chromadb/MiniLM embeddings (:func:`athenaeum.search.embed_texts`) or the
same no-deps hashing-trick fallback :mod:`athenaeum.clusters` already ships.

**Zero LLM calls, structurally.** Every function in this module's call graph
— :func:`athenaeum.wiki_dedupe.discover_wiki_dedupe_candidates` (frontmatter
parse only), :func:`athenaeum.wiki_dedupe._resolve_wiki_embeddings`
(chromadb/hashing only), :func:`athenaeum.clusters.cluster_auto_memory_files`
and the private :func:`athenaeum.clusters._build_adjacency` /
:func:`athenaeum.clusters._single_linkage` (pure cosine-similarity math) —
takes no LLM client / provider argument anywhere, and none of them import
:mod:`athenaeum.provider` or construct an ``anthropic`` client. Mirrors the
"NO ``client``/``provider``/model parameter anywhere" contract
:mod:`athenaeum.decay_sweep` already documents and tests via
``inspect.signature`` for its own zero-LLM sweep; ``TestNoLLMCalls`` below
does the parallel check by patching ``anthropic.Anthropic`` to explode.

**Both linkage paths, for comparison.** Complete-linkage formation
(:func:`cluster_auto_memory_files`, issue athenaeum#681) is what a fresh run
produces today; single-linkage connected components — the PRE-athenaeum#681 shape,
where one weak bridging edge can chain a giant component — are recomputed
here directly from the same adjacency (:func:`athenaeum.clusters._build_adjacency`
+ :func:`athenaeum.clusters._single_linkage`) so the "what changed" comparison
in the committed artifact is apples-to-apples: identical candidate set,
identical threshold, identical embeddings, only the linkage rule differs.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.clusters` and
:mod:`athenaeum.wiki_dedupe` (both L4, neither imports this module back) plus
:mod:`athenaeum.config` (L2) and :mod:`athenaeum.measurement_docs` (L2, new
in this issue). Never imported by either of them — a one-way edge, no cycle.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.clusters import (
    Cluster,
    _build_adjacency,
    _single_linkage,
    cluster_auto_memory_files,
    resolve_cluster_threshold,
)
from athenaeum.config import load_config
from athenaeum.measurement_docs import append_measurement_section
from athenaeum.models import AutoMemoryFile
from athenaeum.store import now_iso
from athenaeum.wiki_dedupe import _resolve_wiki_embeddings, discover_wiki_dedupe_candidates

#: The section this artifact writes into ``docs/measurements/memory-model-measurements.md``.
SECTION_HEADING = "## Shadow-mode complete-linkage population"

#: ``athenaeum measure shadow-linkage`` — the exact reproducible command
#: named on every rendered snapshot (AC "Reproducibility and wiring").
REPRODUCE_COMMAND = "athenaeum measure shadow-linkage"

EmbeddingProvider = Callable[[list[str]], "list[list[float]] | None"]


def _get_version() -> str:
    from athenaeum import __version__

    return __version__


def _get_git_sha(repo_root: Path | None = None) -> str:
    """Best-effort short git SHA of the running checkout. ``"unknown"`` if none."""
    cwd = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        sha = out.stdout.strip()
        return sha if sha else "unknown"
    except Exception:  # noqa: BLE001 — best-effort, never break the measurement run
        return "unknown"


def _size_distribution(sizes: Sequence[int]) -> dict[int, int]:
    dist: dict[int, int] = {}
    for n in sizes:
        dist[n] = dist.get(n, 0) + 1
    return dist


def _comparator_pair_count(sizes: Sequence[int]) -> int:
    """Sum of C(size, 2) over every cluster/component of size >= 2.

    This is the count of pairs that would reach the comparator's
    content-comparison stage: every pair of members WITHIN one cluster (a
    singleton contributes zero pairs).
    """
    total = 0
    for n in sizes:
        if n >= 2:
            total += n * (n - 1) // 2
    return total


def _corpus_digest(files: Sequence[AutoMemoryFile]) -> str:
    """Content-address the candidate corpus: sha256 of sorted ``name:sha256(body)[:12]``.

    Changes whenever a candidate page's content, or the candidate SET itself,
    changes — the "store snapshot identity" the AC requires alongside file
    count, athenaeum version, and git SHA.
    """
    parts: list[str] = []
    for am in files:
        try:
            body = am.content
        except OSError:
            body = ""
        h = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:12]
        parts.append(f"{am.path.name}:{h}")
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass
class LinkagePathSummary:
    """Cluster-formation figures for ONE linkage rule (complete or single)."""

    label: str
    cluster_count: int
    multi_member_cluster_count: int
    size_distribution: dict[int, int] = field(default_factory=dict)
    comparator_pair_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "cluster_count": self.cluster_count,
            "multi_member_cluster_count": self.multi_member_cluster_count,
            "size_distribution": {str(k): v for k, v in sorted(self.size_distribution.items())},
            "comparator_pair_count": self.comparator_pair_count,
        }


@dataclass
class ShadowLinkageResult:
    """Full shadow-mode measurement: both linkage paths, over one candidate set."""

    candidate_file_count: int
    threshold: float
    complete_linkage: LinkagePathSummary
    single_linkage: LinkagePathSummary
    corpus_digest: str
    athenaeum_version: str
    git_sha: str
    generated: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated": self.generated,
            "candidate_file_count": self.candidate_file_count,
            "threshold": self.threshold,
            "complete_linkage": self.complete_linkage.to_dict(),
            "single_linkage": self.single_linkage.to_dict(),
            "corpus_digest": self.corpus_digest,
            "athenaeum_version": self.athenaeum_version,
            "git_sha": self.git_sha,
        }


def run_shadow_linkage(
    knowledge_root: Path,
    *,
    config: dict[str, Any] | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    repo_root: Path | None = None,
) -> ShadowLinkageResult:
    """Measure the shadow-mode complete-linkage cluster population.

    Read-only: only reads ``wiki/*.md`` frontmatter/bodies and resolves
    embeddings (chromadb lookup or the no-deps hashing fallback) — never
    writes a wiki page, never touches ``_pending_merges.md``, never acquires
    the store lock. Zero LLM calls (see module docstring).

    Args:
        knowledge_root: Root of the knowledge directory (``wiki/`` lives at
            ``knowledge_root / "wiki"``).
        config: Optional resolved config dict; loaded lazily otherwise.
        embedding_provider: Optional embedder override (test seam) — see
            :func:`athenaeum.wiki_dedupe._resolve_wiki_embeddings`. ``None``
            uses the real :func:`athenaeum.search.embed_texts`.
        repo_root: Optional override for the git-SHA lookup (test seam).
    """
    wiki_root = knowledge_root / "wiki"
    resolved_config = config if config is not None else load_config(knowledge_root)
    threshold = resolve_cluster_threshold(knowledge_root, resolved_config)

    files = discover_wiki_dedupe_candidates(wiki_root, config=resolved_config)
    embeddings, embedder_sources = _resolve_wiki_embeddings(
        files, embedding_provider=embedding_provider
    )
    file_ids = [str(am.path) for am in files]

    complete_clusters: list[Cluster] = (
        cluster_auto_memory_files(
            files,
            extra_roots=[wiki_root],
            threshold=threshold,
            embeddings=embeddings,
            embedder_sources=embedder_sources,
        )
        if files
        else []
    )
    complete_sizes = [len(c.member_paths) for c in complete_clusters]
    complete_summary = LinkagePathSummary(
        label="complete-linkage (post-athenaeum#681, current formation)",
        cluster_count=len(complete_clusters),
        multi_member_cluster_count=sum(1 for n in complete_sizes if n >= 2),
        size_distribution=_size_distribution(complete_sizes),
        comparator_pair_count=_comparator_pair_count(complete_sizes),
    )

    if files:
        adj, _edge_sim = _build_adjacency(file_ids, embeddings, threshold)
        single_components = _single_linkage(adj)
    else:
        single_components = []
    single_sizes = [len(c) for c in single_components]
    single_summary = LinkagePathSummary(
        label="single-linkage (pre-athenaeum#681, historical anchor's regime)",
        cluster_count=len(single_components),
        multi_member_cluster_count=sum(1 for n in single_sizes if n >= 2),
        size_distribution=_size_distribution(single_sizes),
        comparator_pair_count=_comparator_pair_count(single_sizes),
    )

    return ShadowLinkageResult(
        candidate_file_count=len(files),
        threshold=threshold,
        complete_linkage=complete_summary,
        single_linkage=single_summary,
        corpus_digest=_corpus_digest(files),
        athenaeum_version=_get_version(),
        git_sha=_get_git_sha(repo_root),
        generated=now_iso(),
    )


def _render_path_lines(summary: LinkagePathSummary) -> list[str]:
    dist_str = ", ".join(
        f"{size}:{count}" for size, count in sorted(summary.size_distribution.items())
    ) or "(empty)"
    return [
        f"  - clusters formed: {summary.cluster_count} "
        f"({summary.multi_member_cluster_count} multi-member)",
        f"  - size distribution (size:cluster_count): {dist_str}",
        f"  - pairs reaching content-comparison stage: {summary.comparator_pair_count}",
    ]


def render_snapshot_entry(result: ShadowLinkageResult) -> str:
    """Render one dated ``### Snapshot ...`` sub-entry for the shared docs file."""
    lines = [
        f"### Snapshot {result.generated}",
        "",
        f"Reproduce with: `{REPRODUCE_COMMAND}`",
        "",
        f"- candidate_file_count: {result.candidate_file_count}",
        f"- cluster_threshold: {result.threshold:.4f}",
        f"- corpus_digest: {result.corpus_digest}",
        f"- athenaeum_version: {result.athenaeum_version}",
        f"- git_sha: {result.git_sha}",
        "",
        f"- {result.complete_linkage.label}:",
        *_render_path_lines(result.complete_linkage),
        "",
        f"- {result.single_linkage.label}:",
        *_render_path_lines(result.single_linkage),
        "",
    ]
    return "\n".join(lines)


def write_snapshot(result: ShadowLinkageResult, *, docs_path: Path) -> Path:
    """Idempotently write/append this snapshot into *docs_path*.

    Refuses (raises :class:`ValueError`) when ``candidate_file_count == 0`` —
    nothing was measured (an empty/missing wiki corpus), so there is nothing
    meaningful to commit. Mirrors :func:`athenaeum.push_metrics.write_snapshot`'s
    "refuse rather than write a placeholder" contract (issue athenaeum#795).
    """
    if result.candidate_file_count == 0:
        raise ValueError(
            "refusing to write snapshot: candidate_file_count=0 — no wiki-dedupe "
            "candidate pages found under this knowledge root, so the shadow-linkage "
            "population is not measurable here"
        )
    entry = render_snapshot_entry(result)
    return append_measurement_section(
        docs_path, section_heading=SECTION_HEADING, entry_markdown=entry
    )
