# SPDX-License-Identifier: Apache-2.0
"""Tests for the auto-memory cluster pass (C2, issue athenaeum#196).

Covers :mod:`athenaeum.clusters` and its integration into
:func:`athenaeum.librarian.run` via ``cluster_only=True``. These tests
build synthetic ``raw/auto-memory/`` trees under ``tmp_path`` — the real
``~/knowledge/`` is never touched.

Load-bearing fixtures:

- ``voltaire_near_duplicate_root`` — 5 files sharing voltaire/nanoclaw
  tokens (including one typo-clone ``project_voltair_nanoclaw.md``) plus
  2 unrelated singletons. At the shipped threshold (0.55) the 5 voltaire
  files must land in ONE cluster while the singletons pass through as
  size-1 clusters with no filtering. This is the ground-truth fixture
  for threshold tuning.
- ``contradiction_root`` — two ``feedback_prior_session_debris_*.md``
  files giving OPPOSING guidance. C2 must cluster them together (same
  topic, different recommendations). C4 flags the contradiction — not
  C2's job.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_vector_index(knowledge_root: Path, extra_roots) -> Path:
    """Build the chromadb vector index and return the cache dir.

    The cluster pass reads from this cache dir — wiring it up here means
    tests exercise the production path (real MiniLM embeddings) instead
    of falling back to the hashing-trick path. The hashing-trick path is
    still covered by the fallback unit test below.

    Skips the calling test when chromadb (the ``[vector]`` extra) is not
    installed — repo convention, matching tests/test_search.py.
    """
    pytest.importorskip("chromadb")
    from athenaeum.search import VectorBackend

    cache_dir = knowledge_root / ".athenaeum-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # build_index requires a wiki root — an empty dir is fine; the
    # collection still gets populated from extra_roots.
    wiki_root = knowledge_root / "wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)
    VectorBackend().build_index(wiki_root, cache_dir, extra_roots=extra_roots)
    return cache_dir


def _write_config(knowledge_root: Path, threshold: float | None = None) -> None:
    """Write an athenaeum.yaml that opts into raw/auto-memory."""
    threshold_line = (
        f"\nlibrarian:\n  cluster_threshold: {threshold:.2f}\n"
        if threshold is not None
        else ""
    )
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n" + threshold_line,
        encoding="utf-8",
    )


def _write_auto_memory_file(
    scope_dir: Path,
    name: str,
    frontmatter_name: str,
    body: str,
) -> Path:
    """Write a single auto-memory markdown file and return its path."""
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / name
    path.write_text(
        "---\n" f"name: {frontmatter_name}\n" "type: project\n" "---\n" f"{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def voltaire_near_duplicate_root(tmp_path: Path) -> Path:
    """5 voltaire/nanoclaw files (incl. typo clone) + 2 unrelated singletons."""
    knowledge_root = tmp_path / "knowledge"
    auto = knowledge_root / "raw" / "auto-memory"
    voltaire = auto / "-Users-tristankromer-Code-voltaire"

    # 5 near-duplicate voltaire/nanoclaw notes, including the typo clone.
    # Bodies share a dense vocabulary (voltaire, nanoclaw, ticklestick,
    # toolchain, agent, session) so MiniLM embeddings are tightly
    # clustered even on short markdown fragments — this mirrors the
    # real-world density of the per-scope auto-memory notes voltaire
    # writes in production (and is the whole reason C2 exists).
    common_tail = (
        "The voltaire nanoclaw ticklestick toolchain handles agent "
        "session events, iMessage channel traffic, and Claude Code "
        "pipelines. Voltaire and nanoclaw are the core components, "
        "and ticklestick is the orchestration layer."
    )
    _write_auto_memory_file(
        voltaire,
        "project_voltaire_nanoclaw.md",
        "Voltaire nanoclaw toolchain",
        "Voltaire and nanoclaw are the ticklestick agent toolchain. " + common_tail,
    )
    _write_auto_memory_file(
        voltaire,
        "project_voltaire_iMessage_channel.md",
        "Voltaire iMessage channel",
        "Voltaire runs the nanoclaw iMessage channel handler. " + common_tail,
    )
    _write_auto_memory_file(
        voltaire,
        "project_nanoclaw_voltaire_tickle.md",
        "Nanoclaw ticklestick voltaire",
        "Nanoclaw and voltaire run ticklestick pipelines together. " + common_tail,
    )
    _write_auto_memory_file(
        voltaire,
        "project_voltaire_sessions.md",
        "Voltaire sessions",
        "Voltaire nanoclaw session events flow through ticklestick. " + common_tail,
    )
    # Typo clone — C2 must still cluster this with the 4 above despite
    # the prefix misspelling.
    _write_auto_memory_file(
        voltaire,
        "project_voltair_nanoclaw.md",
        "Voltair typo",
        "Voltair nanoclaw toolchain typo file. " + common_tail,
    )

    # Two unrelated singletons in another scope — must pass through as
    # size-1 clusters (no min-cluster-size filter).
    other = auto / "some-scope"
    _write_auto_memory_file(
        other,
        "reference_sentry_projects.md",
        "Sentry projects",
        "Sentry project IDs and slugs for the kromatic org.",
    )
    _write_auto_memory_file(
        other,
        "user_tristan_profile.md",
        "Tristan profile",
        "Consultant, German family, values cost-consciousness.",
    )

    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def contradiction_root(tmp_path: Path) -> Path:
    """Two feedback files on the same topic with opposing guidance."""
    knowledge_root = tmp_path / "knowledge"
    auto = knowledge_root / "raw" / "auto-memory"
    scope = auto / "-Users-tristankromer-Code"

    _write_auto_memory_file(
        scope,
        "feedback_prior_session_debris_v1.md",
        "Prior session debris v1",
        "Commit prior-session debris directly to develop. Do not park on WIP.",
    )
    _write_auto_memory_file(
        scope,
        "feedback_prior_session_debris_v2.md",
        "Prior session debris v2",
        "Park prior-session debris on a WIP branch. Do not commit directly.",
    )

    _write_config(knowledge_root)
    return knowledge_root


@pytest.fixture
def singleton_pair_root(tmp_path: Path) -> Path:
    """Two completely unrelated files — must become 2 size-1 clusters."""
    knowledge_root = tmp_path / "knowledge"
    auto = knowledge_root / "raw" / "auto-memory"
    scope = auto / "scope-x"

    _write_auto_memory_file(
        scope,
        "reference_dns_flakiness.md",
        "DNS resolver flakiness",
        "macOS mDNSResponder flakes for specific hostnames under cgo resolver.",
    )
    _write_auto_memory_file(
        scope,
        "user_tristan_profile.md",
        "Tristan profile",
        "Consultant background, values thought leadership and cost-consciousness.",
    )

    _write_config(knowledge_root)
    return knowledge_root


# ---------------------------------------------------------------------------
# Pure-function tests (no CLI, no filesystem output)
# ---------------------------------------------------------------------------


class TestCosineHelpers:
    """Issue athenaeum#542: cosine moved to athenaeum.vecmath; see tests/test_vecmath.py
    for the full behavior suite. These stay here as a thin regression check
    that athenaeum.clusters still resolves similarity via the shared helper.
    """

    def test_cosine_identity_is_one(self) -> None:
        from athenaeum.vecmath import cosine

        v = [0.3, -0.2, 0.5, 1.1]
        assert cosine(v, v) == pytest.approx(1.0)

    def test_cosine_zero_vector(self) -> None:
        from athenaeum.vecmath import cosine

        assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_cosine_length_mismatch(self) -> None:
        from athenaeum.vecmath import cosine

        assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


class TestSingleLinkage:
    def test_components_are_connected(self) -> None:
        from athenaeum.clusters import _single_linkage

        # Graph: 0-1, 2 isolated, 3-4-5 chain
        adj: list[set[int]] = [
            {1},
            {0},
            set(),
            {4},
            {3, 5},
            {4},
        ]
        components = _single_linkage(adj)
        assert sorted(sorted(c) for c in components) == [[0, 1], [2], [3, 4, 5]]


class TestCompleteLinkage:
    """Issue athenaeum#681: cluster formation refines single-linkage components into
    complete-linkage cliques so a weak bridging edge can no longer chain a
    giant component that the merge-proposal gate would only rebuild and discard.
    """

    @staticmethod
    def _all_pairs_clear(cluster, edge_sim, threshold) -> bool:
        """Every pair in *cluster* is an edge >= threshold (the clique invariant)."""
        for i in range(len(cluster)):
            for j in range(i + 1, len(cluster)):
                a, b = cluster[i], cluster[j]
                key = (a, b) if a < b else (b, a)
                if edge_sim.get(key, 0.0) < threshold:
                    return False
        return True

    def test_chain_does_not_stay_one_giant_cluster(self) -> None:
        from athenaeum.clusters import _complete_linkage

        # 0-1-2 chain: adjacent pairs clear the threshold, 0-2 does not.
        # Single-linkage would return {0,1,2}; complete-linkage must not.
        adj: list[set[int]] = [{1}, {0, 2}, {1}]
        edge_sim = {(0, 1): 0.9, (1, 2): 0.9}
        clusters = _complete_linkage([0, 1, 2], adj, edge_sim, 0.55)

        assert [0, 1, 2] not in clusters
        assert all(self._all_pairs_clear(c, edge_sim, 0.55) for c in clusters)
        # No member is dropped.
        assert sorted(m for c in clusters for m in c) == [0, 1, 2]

    def test_true_clique_is_preserved(self) -> None:
        from athenaeum.clusters import _complete_linkage

        # Every pair clears the threshold → one clique, unchanged.
        adj: list[set[int]] = [{1, 2}, {0, 2}, {0, 1}]
        edge_sim = {(0, 1): 0.8, (0, 2): 0.8, (1, 2): 0.8}
        clusters = _complete_linkage([0, 1, 2], adj, edge_sim, 0.55)
        assert clusters == [[0, 1, 2]]

    def test_star_hub_cannot_form_clique(self) -> None:
        from athenaeum.clusters import _complete_linkage

        # Hub 0 bridges 4 leaves that are mutually dissimilar. Single-linkage
        # would chain all 5; complete-linkage caps every cluster at 2 (the hub
        # pairs with one leaf) and loses no member.
        adj: list[set[int]] = [{1, 2, 3, 4}, {0}, {0}, {0}, {0}]
        edge_sim = {(0, 1): 0.9, (0, 2): 0.9, (0, 3): 0.9, (0, 4): 0.9}
        clusters = _complete_linkage([0, 1, 2, 3, 4], adj, edge_sim, 0.55)
        assert max(len(c) for c in clusters) == 2
        assert sorted(m for c in clusters for m in c) == [0, 1, 2, 3, 4]
        assert all(self._all_pairs_clear(c, edge_sim, 0.55) for c in clusters)

    def test_deterministic(self) -> None:
        from athenaeum.clusters import _complete_linkage

        adj: list[set[int]] = [{1}, {0, 2}, {1, 3}, {2}]
        edge_sim = {(0, 1): 0.7, (1, 2): 0.9, (2, 3): 0.7}
        results = {
            tuple(tuple(c) for c in _complete_linkage([0, 1, 2, 3], adj, edge_sim, 0.55))
            for _ in range(8)
        }
        assert len(results) == 1

    def test_singleton_component_passthrough(self) -> None:
        from athenaeum.clusters import _complete_linkage

        assert _complete_linkage([7], [set()] * 8, {}, 0.55) == [[7]]


# ---------------------------------------------------------------------------
# cluster_auto_memory_files behaviour
# ---------------------------------------------------------------------------


class TestClusterVoltaireFixture:
    def test_all_five_voltaire_files_collapse_to_one_cluster(
        self,
        voltaire_near_duplicate_root: Path,
    ) -> None:
        """THE load-bearing acceptance test: 5 voltaire/nanoclaw files → 1 cluster."""
        from athenaeum.clusters import (
            DEFAULT_CLUSTER_THRESHOLD,
            cluster_auto_memory_files,
        )
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(voltaire_near_duplicate_root)
        extra_roots = resolve_extra_intake_roots(voltaire_near_duplicate_root)
        cache_dir = _build_vector_index(voltaire_near_duplicate_root, extra_roots)
        clusters = cluster_auto_memory_files(
            files,
            extra_roots=extra_roots,
            cache_dir=cache_dir,
            threshold=DEFAULT_CLUSTER_THRESHOLD,
        )

        # Separate voltaire members from singleton members.
        voltaire_clusters = [
            c
            for c in clusters
            if any("voltair" in p or "nanoclaw" in p for p in c.member_paths)
        ]
        singleton_clusters = [c for c in clusters if c not in voltaire_clusters]

        # LOAD-BEARING ASSERTION: exactly one voltaire cluster with all 5 files.
        assert len(voltaire_clusters) == 1, (
            f"expected 1 voltaire cluster, got {len(voltaire_clusters)}: "
            f"{[c.member_paths for c in voltaire_clusters]}"
        )
        assert len(voltaire_clusters[0].member_paths) == 5

        # The typo clone must be inside it — that's the whole point.
        paths_joined = " ".join(voltaire_clusters[0].member_paths)
        assert "project_voltair_nanoclaw.md" in paths_joined

        # Unrelated files stay singletons (no min-cluster-size filter).
        assert len(singleton_clusters) == 2
        assert all(len(c.member_paths) == 1 for c in singleton_clusters)

    def test_rationale_human_debuggable(
        self,
        voltaire_near_duplicate_root: Path,
    ) -> None:
        from athenaeum.clusters import cluster_auto_memory_files
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(voltaire_near_duplicate_root)
        extra_roots = resolve_extra_intake_roots(voltaire_near_duplicate_root)
        cache_dir = _build_vector_index(voltaire_near_duplicate_root, extra_roots)
        clusters = cluster_auto_memory_files(
            files,
            extra_roots=extra_roots,
            cache_dir=cache_dir,
        )
        voltaire_cluster = next(c for c in clusters if len(c.member_paths) > 1)
        assert "cosine" in voltaire_cluster.rationale.lower()
        assert voltaire_cluster.centroid_score > 0.0


class TestClusterFormationIsCompleteLinkage:
    """Issue athenaeum#681 end-to-end: a single-linkage chain of pages must NOT be
    formed into one giant cluster by ``cluster_auto_memory_files``. Uses
    injected embeddings (the ``embeddings=`` override) so the test is
    deterministic and needs no chromadb.
    """

    @staticmethod
    def _chain_files_and_embeddings(tmp_path: Path):
        import math

        from athenaeum.models import AutoMemoryFile

        # Unit vectors on a circle 20 degrees apart: adjacent pages have
        # cosine cos(20 deg) ~= 0.94 (an edge at threshold 0.9); pages two
        # apart have cos(40 deg) ~= 0.77 (below 0.9, NOT an edge). So the five
        # pages form a single-linkage CHAIN but no clique larger than 2.
        scope = tmp_path / "raw" / "auto-memory" / "proj"
        scope.mkdir(parents=True, exist_ok=True)
        files = []
        embeddings: dict[str, list[float]] = {}
        for i in range(5):
            path = scope / f"project_chain_{i}.md"
            path.write_text("---\nname: chain\ntype: project\n---\nbody\n", "utf-8")
            am = AutoMemoryFile(path=path, origin_scope="proj", memory_type="project")
            files.append(am)
            angle = math.radians(20 * i)
            embeddings[str(path)] = [math.cos(angle), math.sin(angle), 0.0]
        return files, embeddings

    def test_chain_is_not_one_giant_cluster(self, tmp_path: Path) -> None:
        from athenaeum.clusters import cluster_auto_memory_files

        files, embeddings = self._chain_files_and_embeddings(tmp_path)
        clusters = cluster_auto_memory_files(
            files,
            extra_roots=[tmp_path / "raw" / "auto-memory"],
            threshold=0.9,
            embeddings=embeddings,
        )

        # The degenerate all-five giant cluster must not exist.
        assert all(len(c.member_paths) < 5 for c in clusters)
        # Every member is accounted for exactly once.
        all_members = [p for c in clusters for p in c.member_paths]
        assert len(all_members) == 5
        assert len(set(all_members)) == 5
        # Every multi-member cluster is a genuine complete-linkage clique:
        # its recorded min pairwise cosine clears the threshold.
        for c in clusters:
            if len(c.member_paths) > 1:
                assert c.min_pairwise_score >= 0.9

    def test_clique_still_forms_one_cluster(self, tmp_path: Path) -> None:
        import math

        from athenaeum.clusters import cluster_auto_memory_files
        from athenaeum.models import AutoMemoryFile

        # Three near-identical pages (2 degrees apart) — a true clique.
        scope = tmp_path / "raw" / "auto-memory" / "proj"
        scope.mkdir(parents=True, exist_ok=True)
        files, embeddings = [], {}
        for i in range(3):
            path = scope / f"project_clique_{i}.md"
            path.write_text("---\nname: clique\ntype: project\n---\nbody\n", "utf-8")
            files.append(
                AutoMemoryFile(path=path, origin_scope="proj", memory_type="project")
            )
            angle = math.radians(2 * i)
            embeddings[str(path)] = [math.cos(angle), math.sin(angle), 0.0]

        clusters = cluster_auto_memory_files(
            files,
            extra_roots=[tmp_path / "raw" / "auto-memory"],
            threshold=0.9,
            embeddings=embeddings,
        )
        multi = [c for c in clusters if len(c.member_paths) > 1]
        assert len(multi) == 1
        assert len(multi[0].member_paths) == 3


class TestClusterContradictionFixture:
    def test_contradictory_files_cluster_together(
        self,
        contradiction_root: Path,
    ) -> None:
        """Same topic, opposing guidance → one cluster. C4 handles the disagreement."""
        from athenaeum.clusters import cluster_auto_memory_files
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(contradiction_root)
        extra_roots = resolve_extra_intake_roots(contradiction_root)
        cache_dir = _build_vector_index(contradiction_root, extra_roots)
        clusters = cluster_auto_memory_files(
            files,
            extra_roots=extra_roots,
            cache_dir=cache_dir,
        )

        # Exactly one cluster of size 2. C2 does not care that the
        # guidance is contradictory — it just groups by topic.
        assert len(clusters) == 1
        assert len(clusters[0].member_paths) == 2


class TestSingletonPassthrough:
    def test_two_unrelated_files_yield_two_clusters(
        self,
        singleton_pair_root: Path,
    ) -> None:
        from athenaeum.clusters import cluster_auto_memory_files
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(singleton_pair_root)
        extra_roots = resolve_extra_intake_roots(singleton_pair_root)
        cache_dir = _build_vector_index(singleton_pair_root, extra_roots)
        clusters = cluster_auto_memory_files(
            files,
            extra_roots=extra_roots,
            cache_dir=cache_dir,
        )

        assert len(clusters) == 2
        assert all(len(c.member_paths) == 1 for c in clusters)
        # Centroid of a singleton is defined as 1.0.
        assert all(c.centroid_score == pytest.approx(1.0) for c in clusters)

    def test_empty_input_yields_empty_output(self) -> None:
        from athenaeum.clusters import cluster_auto_memory_files

        assert cluster_auto_memory_files([], extra_roots=[]) == []


class TestFallbackEmbedder:
    def test_fallback_does_not_crash_without_chromadb_index(
        self,
        singleton_pair_root: Path,
    ) -> None:
        """With no pre-built index, clustering falls back to hashing-trick vectors.

        The hashing trick isn't a semantic embedder — it's a no-deps
        degradation path so C2 is still runnable when the operator hasn't
        built the recall vector index. This test just confirms the code
        path returns shaped output without errors.
        """
        from athenaeum.clusters import cluster_auto_memory_files
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(singleton_pair_root)
        extra_roots = resolve_extra_intake_roots(singleton_pair_root)
        # Intentionally point at a cache dir with no vector index.
        clusters = cluster_auto_memory_files(
            files,
            extra_roots=extra_roots,
            cache_dir=singleton_pair_root / ".empty-cache",
            threshold=0.9,  # high threshold so the two unrelated files stay apart
        )
        assert len(clusters) == 2
        assert all(len(c.member_paths) == 1 for c in clusters)

    def test_fallback_engagement_logs_info_and_records_embedder(
        self,
        singleton_pair_root: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issue athenaeum#1279: the "served from chromadb" count line is INFO (was
        WARNING, issue athenaeum#1032) and names the raw auto-memory C2 cluster
        pass explicitly — the demoted-and-reworded half of this issue's
        misdirection finding. This engagement is normal, by-design
        ephemeral-intake behavior (``raw/auto-memory/`` is consume-and-delete,
        so the recall index and the live intake tree are routinely out of
        sync), NOT evidence anything is broken; the pre-athenaeum#1279 WARNING
        text, with no path attribution, sent an unrelated wiki-dedupe
        over-cluster investigation sideways for a full pass in production
        (see this issue and athenaeum#1005's comment trail). Every cluster formed
        from hashing-trick vectors still records ``embedder="fallback-hashing"``
        so run artifacts can tell which embedder produced a cluster's
        vectors — that part of athenaeum#1032 is unchanged.
        """
        import logging

        from athenaeum.clusters import (
            EMBEDDER_FALLBACK_HASHING,
            cluster_auto_memory_files,
        )
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(singleton_pair_root)
        extra_roots = resolve_extra_intake_roots(singleton_pair_root)

        caplog.set_level(logging.INFO, logger="athenaeum.clusters")
        clusters = cluster_auto_memory_files(
            files,
            extra_roots=extra_roots,
            cache_dir=singleton_pair_root / ".empty-cache",
            threshold=0.9,
        )
        assert clusters
        assert all(c.embedder == EMBEDDER_FALLBACK_HASHING for c in clusters)

        fallback_lines = [
            r for r in caplog.records if "served from the chromadb" in r.getMessage()
        ]
        assert len(fallback_lines) == 1
        assert fallback_lines[0].levelno == logging.INFO
        # Not WARNING: a benign by-design engagement must not read as alarming.
        assert not any(r.levelno == logging.WARNING for r in fallback_lines)
        # Names the actual code path, so a reader is never left to guess
        # (or misattribute to wiki-dedupe) which pass produced the line.
        assert "raw auto-memory C2 cluster pass" in fallback_lines[0].getMessage()

    def test_out_embedder_counts_tallies_per_file_not_per_cluster(
        self,
        singleton_pair_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Issue athenaeum#1279: ``out_embedder_counts`` tallies PER-FILE, so a run
        whose every affected cluster is a harmless singleton (this issue's
        motivating incident: raw-intake chromadb service fell ~98%->0% over
        two days with every affected cluster a singleton) still produces a
        real, non-zero count — the exact signal the pre-athenaeum#1279 code had
        no durable, machine-readable form for.

        Mixes a chromadb-served file with a fallback-served file in the SAME
        call (stubbing ``VectorBackend.fetch_embeddings`` for one of the two
        ids, same technique as
        ``test_chromadb_hit_records_chromadb_default_embedder`` above) so
        both counters land in one assertion.
        """
        from athenaeum.clusters import (
            EMBEDDER_CHROMADB_DEFAULT,
            EMBEDDER_FALLBACK_HASHING,
            _indexed_id_for,
            cluster_auto_memory_files,
        )
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files
        from athenaeum.search import VectorBackend

        files = discover_auto_memory_files(singleton_pair_root)
        assert len(files) == 2
        extra_roots = resolve_extra_intake_roots(singleton_pair_root)
        served_id = _indexed_id_for(files[0], extra_roots)

        def _fake_fetch_embeddings(
            self: VectorBackend, ids: Iterable[str], cache_dir: Path
        ) -> dict[str, list[float]]:
            # Only the FIRST file's id is ever served — the second always
            # falls back, synthesizing the mixed-source run this test needs.
            return {idx_id: [1.0, 0.0] for idx_id in ids if idx_id == served_id}

        monkeypatch.setattr(VectorBackend, "fetch_embeddings", _fake_fetch_embeddings)

        out_counts: dict[str, int] = {}
        clusters = cluster_auto_memory_files(
            files,
            extra_roots=extra_roots,
            cache_dir=singleton_pair_root / ".empty-cache",
            threshold=0.9,
            out_embedder_counts=out_counts,
        )
        assert len(clusters) == 2  # two singletons; counting is per-FILE regardless
        assert out_counts == {
            EMBEDDER_CHROMADB_DEFAULT: 1,
            EMBEDDER_FALLBACK_HASHING: 1,
        }

    def test_out_embedder_counts_none_by_default_is_a_pure_no_op(
        self, singleton_pair_root: Path
    ) -> None:
        """``out_embedder_counts`` omitted (every pre-athenaeum#1279 caller)
        changes nothing about the return value — the out-param write is
        gated on ``is not None``, not merely on presence of a fallback."""
        from athenaeum.clusters import cluster_auto_memory_files
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(singleton_pair_root)
        extra_roots = resolve_extra_intake_roots(singleton_pair_root)
        clusters = cluster_auto_memory_files(
            files,
            extra_roots=extra_roots,
            cache_dir=singleton_pair_root / ".empty-cache",
            threshold=0.9,
        )
        assert len(clusters) == 2

    def test_chromadb_hit_records_chromadb_default_embedder(
        self,
        singleton_pair_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Issue athenaeum#1032: when every file's vector is served from the
        chromadb collection (no fallback engaged), every formed cluster
        records ``embedder="chromadb-default"``.

        Stubs :meth:`VectorBackend.fetch_embeddings` (issue athenaeum#1032's "stub
        embedding provider" seam for this module — ``_resolve_embeddings``
        has no injectable-callable shape of its own, unlike
        ``wiki_dedupe._resolve_wiki_embeddings``) instead of depending on the
        real optional ``chromadb`` extra.
        """
        from athenaeum.clusters import (
            EMBEDDER_CHROMADB_DEFAULT,
            _indexed_id_for,
            cluster_auto_memory_files,
        )
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files
        from athenaeum.search import VectorBackend

        files = discover_auto_memory_files(singleton_pair_root)
        extra_roots = resolve_extra_intake_roots(singleton_pair_root)
        expected_ids = {_indexed_id_for(am, extra_roots) for am in files}

        def _fake_fetch_embeddings(
            self: VectorBackend, ids: Iterable[str], cache_dir: Path
        ) -> dict[str, list[float]]:
            return {idx_id: [1.0, 0.0] for idx_id in ids if idx_id in expected_ids}

        monkeypatch.setattr(VectorBackend, "fetch_embeddings", _fake_fetch_embeddings)

        clusters = cluster_auto_memory_files(
            files,
            extra_roots=extra_roots,
            cache_dir=singleton_pair_root / ".empty-cache",
            threshold=0.9,
        )
        assert clusters
        assert all(c.embedder == EMBEDDER_CHROMADB_DEFAULT for c in clusters)

    def test_fallback_embeddings_stable_across_pythonhashseed(
        self, singleton_pair_root: Path
    ) -> None:
        """Issue athenaeum#1050: ``_fallback_embeddings`` hashed tokens with the
        builtin ``hash()``, which is salted per-process by ``PYTHONHASHSEED``
        (a str/bytes DoS mitigation) — so the same token mapped to a
        different feature index/sign in every process, and every cosine
        derived from a fallback vector changed run to run (flaked CI run
        32379062624). Spawns two subprocesses with explicit, DIFFERENT
        ``PYTHONHASHSEED`` values and asserts they compute a byte-identical
        fingerprint of ``_fallback_embeddings`` output for the same files —
        this would NOT have held before the fix (see the manual before/after
        digest check in the PR description for the failing-before proof;
        asserting the negative here would make this test depend on the
        pre-fix implementation staying importable, which it no longer is).
        """
        script = (
            "import hashlib, json, sys\n"
            "from pathlib import Path\n"
            "from athenaeum.clusters import _fallback_embeddings\n"
            "from athenaeum.librarian import discover_auto_memory_files\n"
            f"files = discover_auto_memory_files(Path({str(singleton_pair_root)!r}))\n"
            "vecs = _fallback_embeddings(files)\n"
            "payload = json.dumps(vecs, sort_keys=True).encode()\n"
            "sys.stdout.write(hashlib.sha256(payload).hexdigest())\n"
        )

        digests = set()
        for seed in ("0", "1"):
            proc = subprocess.run(
                [sys.executable, "-c", script],
                env={**os.environ, "PYTHONHASHSEED": seed},
                capture_output=True,
                text=True,
                check=True,
            )
            digests.add(proc.stdout.strip())

        assert len(digests) == 1, (
            f"_fallback_embeddings vectors differ across PYTHONHASHSEED: {digests}"
        )


# ---------------------------------------------------------------------------
# Output / rotation
# ---------------------------------------------------------------------------


class TestClusterReportJSONL:
    def test_each_row_has_expected_schema(self, tmp_path: Path) -> None:
        from athenaeum.clusters import Cluster, write_cluster_report

        clusters = [
            Cluster(
                cluster_id="scope-0000",
                member_paths=["a/x.md", "a/y.md"],
                centroid_score=0.82,
                rationale="cosine >= 0.60; members share tokens",
            ),
            Cluster(
                cluster_id="scope-0001",
                member_paths=["a/solo.md"],
                centroid_score=1.0,
                rationale="singleton",
            ),
        ]
        out = tmp_path / "raw" / "_librarian-clusters.jsonl"
        canonical, timestamped = write_cluster_report(clusters, out)

        assert canonical == out
        assert timestamped is not None and timestamped.is_file()

        rows = [json.loads(line) for line in out.read_text().splitlines()]
        assert len(rows) == 2
        for row in rows:
            # Issue athenaeum#1032: "embedder" joined the row schema.
            assert set(row.keys()) == {
                "cluster_id",
                "member_paths",
                "centroid_score",
                "min_pairwise_score",
                "rationale",
                "embedder",
            }
            assert isinstance(row["cluster_id"], str)
            assert isinstance(row["member_paths"], list)
            assert all(isinstance(p, str) for p in row["member_paths"])
            assert isinstance(row["centroid_score"], float)
            assert isinstance(row["min_pairwise_score"], float)
            assert isinstance(row["rationale"], str)
            assert isinstance(row["embedder"], str)

    def test_rotation_preserves_previous_run(self, tmp_path: Path) -> None:
        """Two back-to-back runs should leave 2 timestamped files + canonical."""
        from athenaeum.clusters import Cluster, write_cluster_report

        out = tmp_path / "_librarian-clusters.jsonl"
        write_cluster_report(
            [Cluster(cluster_id="x-0000", member_paths=["a.md"])],
            out,
        )
        # Ensure rotation filename varies across calls — it's UTC-second
        # granularity, so a tiny sleep would do, but we just check that
        # both runs produce a file at the canonical path and at least
        # one timestamped sibling exists.
        write_cluster_report(
            [Cluster(cluster_id="x-0000", member_paths=["a.md", "b.md"])],
            out,
        )
        timestamped = list(tmp_path.glob("_librarian-clusters-*.jsonl"))
        assert timestamped, "rotation should write at least one timestamped file"
        assert out.is_file()


# ---------------------------------------------------------------------------
# CLI / run() integration
# ---------------------------------------------------------------------------


class TestClusterOnlyRun:
    def test_cluster_only_writes_report_without_tier_pipeline(
        self,
        voltaire_near_duplicate_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``run(cluster_only=True)`` must write the JSONL and return 0 without LLM."""
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import run

        extra_roots = resolve_extra_intake_roots(voltaire_near_duplicate_root)
        cache_dir = _build_vector_index(voltaire_near_duplicate_root, extra_roots)
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))

        rc = run(
            raw_root=voltaire_near_duplicate_root / "raw",
            wiki_root=voltaire_near_duplicate_root / "wiki",
            knowledge_root=voltaire_near_duplicate_root,
            dry_run=False,
            cluster_only=True,
        )
        assert rc == 0

        out = voltaire_near_duplicate_root / "raw" / "_librarian-clusters.jsonl"
        assert out.is_file()
        rows = [json.loads(line) for line in out.read_text().splitlines() if line]
        # voltaire cluster + 2 singletons
        assert len(rows) >= 1
        voltaire_rows = [
            r
            for r in rows
            if any("voltair" in p or "nanoclaw" in p for p in r["member_paths"])
        ]
        assert len(voltaire_rows) == 1
        assert len(voltaire_rows[0]["member_paths"]) == 5

    def test_cluster_only_dry_run_does_not_write_report(
        self,
        voltaire_near_duplicate_root: Path,
    ) -> None:
        from athenaeum.librarian import run

        rc = run(
            knowledge_root=voltaire_near_duplicate_root,
            raw_root=voltaire_near_duplicate_root / "raw",
            wiki_root=voltaire_near_duplicate_root / "wiki",
            dry_run=True,
            cluster_only=True,
        )
        assert rc == 0
        out = voltaire_near_duplicate_root / "raw" / "_librarian-clusters.jsonl"
        assert not out.exists()


# ---------------------------------------------------------------------------
# Embedder-reuse guardrail
# ---------------------------------------------------------------------------


class TestNoParallelEmbedder:
    def test_src_does_not_import_second_embedder(self) -> None:
        """Repo-wide static check: clustering MUST reuse chromadb, not add a 2nd provider."""
        import pathlib
        import re

        src_root = pathlib.Path(__file__).resolve().parents[1] / "src" / "athenaeum"
        # Match actual import statements only (line-leading, with optional
        # whitespace). The docstring mentioning these package names in
        # prose must NOT trip the guardrail.
        forbidden = re.compile(
            r"^\s*(?:from|import)\s+(sentence_transformers|openai|cohere)\b"
        )
        offenders: list[str] = []
        for py in src_root.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for line in text.splitlines():
                if forbidden.match(line) and "# explicitly allowed" not in line:
                    offenders.append(f"{py.relative_to(src_root)}: {line.strip()}")
        assert not offenders, (
            "clustering must reuse VectorBackend; second embedder detected:\n"
            + "\n".join(offenders)
        )


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestClusterConfig:
    def test_threshold_default_is_applied(self, tmp_path: Path) -> None:
        from athenaeum.clusters import (
            DEFAULT_CLUSTER_THRESHOLD,
            resolve_cluster_threshold,
        )

        # No config file — falls back to the shipped default.
        assert resolve_cluster_threshold(tmp_path) == DEFAULT_CLUSTER_THRESHOLD

    def test_threshold_override_via_yaml(self, tmp_path: Path) -> None:
        from athenaeum.clusters import resolve_cluster_threshold

        _write_config(tmp_path, threshold=0.75)
        assert resolve_cluster_threshold(tmp_path) == pytest.approx(0.75)

    def test_output_path_relative_to_knowledge_root(self, tmp_path: Path) -> None:
        from athenaeum.clusters import resolve_cluster_output_path

        _write_config(tmp_path)
        out = resolve_cluster_output_path(tmp_path)
        assert out == tmp_path / "raw" / "_librarian-clusters.jsonl"


# ---------------------------------------------------------------------------
# Rotation pruning / retention (issue athenaeum#311)
# ---------------------------------------------------------------------------


def _make_rotation(dir_path: Path, stamp: str) -> Path:
    """Create a timestamped rotation sibling and return its path."""
    p = dir_path / f"_librarian-clusters-{stamp}.jsonl"
    p.write_text("{}\n", encoding="utf-8")
    return p


class TestPruneClusterRotations:
    # UTC timestamps chosen so lexicographic order == chronological order,
    # exactly as ``write_cluster_report`` emits them (%Y%m%dT%H%M%SZ).
    _STAMPS = [
        "20260101T000000Z",
        "20260102T000000Z",
        "20260103T000000Z",
        "20260104T000000Z",
        "20260105T000000Z",
    ]

    def _seed(self, tmp_path: Path) -> Path:
        canonical = tmp_path / "_librarian-clusters.jsonl"
        canonical.write_text("canonical\n", encoding="utf-8")
        for stamp in self._STAMPS:
            _make_rotation(tmp_path, stamp)
        return canonical

    def test_keeps_n_newest_deletes_older_by_filename_order(
        self,
        tmp_path: Path,
    ) -> None:
        from athenaeum.clusters import prune_cluster_rotations

        canonical = self._seed(tmp_path)
        pruned = prune_cluster_rotations(canonical, keep=2)

        # The two NEWEST rotations survive; the three oldest are pruned.
        remaining = sorted(p.name for p in tmp_path.glob("_librarian-clusters-*.jsonl"))
        assert remaining == [
            "_librarian-clusters-20260104T000000Z.jsonl",
            "_librarian-clusters-20260105T000000Z.jsonl",
        ]
        assert sorted(p.name for p in pruned) == [
            "_librarian-clusters-20260101T000000Z.jsonl",
            "_librarian-clusters-20260102T000000Z.jsonl",
            "_librarian-clusters-20260103T000000Z.jsonl",
        ]

    def test_keep_zero_disables_pruning(self, tmp_path: Path) -> None:
        from athenaeum.clusters import prune_cluster_rotations

        canonical = self._seed(tmp_path)
        pruned = prune_cluster_rotations(canonical, keep=0)

        assert pruned == []
        assert len(list(tmp_path.glob("_librarian-clusters-*.jsonl"))) == 5

    def test_negative_keep_disables_pruning(self, tmp_path: Path) -> None:
        from athenaeum.clusters import prune_cluster_rotations

        canonical = self._seed(tmp_path)
        assert prune_cluster_rotations(canonical, keep=-5) == []
        assert len(list(tmp_path.glob("_librarian-clusters-*.jsonl"))) == 5

    def test_canonical_file_is_never_deleted(self, tmp_path: Path) -> None:
        from athenaeum.clusters import prune_cluster_rotations

        canonical = self._seed(tmp_path)
        # Aggressive prune: keep only 1 rotation.
        prune_cluster_rotations(canonical, keep=1)
        assert canonical.is_file()
        assert canonical.read_text(encoding="utf-8") == "canonical\n"

    def test_noop_when_fewer_than_keep(self, tmp_path: Path) -> None:
        from athenaeum.clusters import prune_cluster_rotations

        canonical = tmp_path / "_librarian-clusters.jsonl"
        canonical.write_text("canonical\n", encoding="utf-8")
        _make_rotation(tmp_path, self._STAMPS[0])
        assert prune_cluster_rotations(canonical, keep=30) == []
        assert len(list(tmp_path.glob("_librarian-clusters-*.jsonl"))) == 1

    def test_non_timestamp_sibling_is_ignored(self, tmp_path: Path) -> None:
        """A stray `<stem>-backup.jsonl` matches the glob but is NOT a
        `%Y%m%dT%H%M%SZ` rotation. It must be neither counted toward `keep`
        nor deleted — and must not shield a real old rotation from pruning
        (letters sort after digits, so a lexicographic sort would misplace it).
        """
        from athenaeum.clusters import prune_cluster_rotations

        canonical = tmp_path / "_librarian-clusters.jsonl"
        canonical.write_text("canonical\n", encoding="utf-8")
        # Two genuine rotations + one non-timestamp sibling.
        _make_rotation(tmp_path, "20260101T000000Z")
        _make_rotation(tmp_path, "20260102T000000Z")
        stray = tmp_path / "_librarian-clusters-backup.jsonl"
        stray.write_text("not a rotation\n", encoding="utf-8")

        pruned = prune_cluster_rotations(canonical, keep=1)

        # Only the older of the two REAL rotations is pruned; the stray is
        # untouched and did not count toward `keep`.
        assert [p.name for p in pruned] == [
            "_librarian-clusters-20260101T000000Z.jsonl"
        ]
        assert stray.is_file()
        assert (tmp_path / "_librarian-clusters-20260102T000000Z.jsonl").is_file()


class TestRotationRetentionConfig:
    def test_default_is_30(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_ROTATION_RETENTION", raising=False)
        from athenaeum.clusters import (
            DEFAULT_ROTATION_RETENTION,
            resolve_rotation_retention,
        )

        assert DEFAULT_ROTATION_RETENTION == 30
        assert resolve_rotation_retention(tmp_path) == 30

    def test_yaml_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_ROTATION_RETENTION", raising=False)
        from athenaeum.clusters import resolve_rotation_retention

        (tmp_path / "athenaeum.yaml").write_text(
            "librarian:\n  rotation_retention: 5\n",
            encoding="utf-8",
        )
        assert resolve_rotation_retention(tmp_path) == 5

    def test_env_beats_yaml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from athenaeum.clusters import resolve_rotation_retention

        (tmp_path / "athenaeum.yaml").write_text(
            "librarian:\n  rotation_retention: 5\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ATHENAEUM_ROTATION_RETENTION", "2")
        assert resolve_rotation_retention(tmp_path) == 2

    def test_bool_yaml_value_falls_back_to_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``rotation_retention: yes`` parses to bool True (an int subclass)
        # and must NOT be read as a window of 1.
        monkeypatch.delenv("ATHENAEUM_ROTATION_RETENTION", raising=False)
        from athenaeum.clusters import resolve_rotation_retention

        (tmp_path / "athenaeum.yaml").write_text(
            "librarian:\n  rotation_retention: true\n",
            encoding="utf-8",
        )
        assert resolve_rotation_retention(tmp_path) == 30

    def test_zero_disables_via_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_ROTATION_RETENTION", raising=False)
        from athenaeum.clusters import resolve_rotation_retention

        (tmp_path / "athenaeum.yaml").write_text(
            "librarian:\n  rotation_retention: 0\n",
            encoding="utf-8",
        )
        assert resolve_rotation_retention(tmp_path) == 0

    def test_quoted_string_value_is_coerced(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Parity with resolve_cluster_threshold's float() coercion: quoted
        # yaml like `"5"` must resolve to 5, not silently fall back.
        monkeypatch.delenv("ATHENAEUM_ROTATION_RETENTION", raising=False)
        from athenaeum.clusters import resolve_rotation_retention

        (tmp_path / "athenaeum.yaml").write_text(
            'librarian:\n  rotation_retention: "5"\n',
            encoding="utf-8",
        )
        assert resolve_rotation_retention(tmp_path) == 5

    def test_non_numeric_string_falls_back_to_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_ROTATION_RETENTION", raising=False)
        from athenaeum.clusters import resolve_rotation_retention

        (tmp_path / "athenaeum.yaml").write_text(
            'librarian:\n  rotation_retention: "lots"\n',
            encoding="utf-8",
        )
        assert resolve_rotation_retention(tmp_path) == 30


class TestRotationPruneNonFatal:
    def test_prune_failure_does_not_abort_run(
        self,
        voltaire_near_duplicate_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A raising prune must warn but leave the run successful (rc=0)."""
        import athenaeum.librarian as librarian_mod
        from athenaeum.librarian import run

        def _boom(*_args: object, **_kwargs: object) -> list:
            raise OSError("simulated unlink failure")

        monkeypatch.setattr(librarian_mod, "prune_cluster_rotations", _boom)

        with caplog.at_level("WARNING"):
            rc = run(
                raw_root=voltaire_near_duplicate_root / "raw",
                wiki_root=voltaire_near_duplicate_root / "wiki",
                knowledge_root=voltaire_near_duplicate_root,
                dry_run=False,
                cluster_only=True,
            )

        assert rc == 0
        out = voltaire_near_duplicate_root / "raw" / "_librarian-clusters.jsonl"
        assert out.is_file()
        assert any(
            "cluster rotation prune failed" in rec.message for rec in caplog.records
        )


class TestMinIntraSimilarity:
    """Issue athenaeum#421: the complete-linkage coherence metric (minimum pairwise)."""

    def test_singleton_is_one(self) -> None:
        from athenaeum.clusters import _min_intra_similarity

        assert _min_intra_similarity([0], ["a"], {"a": [1.0, 0.0]}) == 1.0

    def test_clique_reports_weakest_pair(self) -> None:
        from athenaeum.clusters import _mean_intra_similarity, _min_intra_similarity

        # Three unit vectors: a-b close, a-c close, b-c orthogonal (min ~0).
        emb = {
            "a": [1.0, 0.0, 0.0],
            "b": [0.9, 0.1, 0.0],
            "c": [0.0, 0.0, 1.0],
        }
        ids = ["a", "b", "c"]
        idx = [0, 1, 2]
        mn = _min_intra_similarity(idx, ids, emb)
        mean = _mean_intra_similarity(idx, ids, emb)
        # The b-c orthogonal pair drives the minimum to ~0, well below the mean.
        assert mn < 0.1
        assert mn < mean

    def test_missing_embeddings_skipped(self) -> None:
        from athenaeum.clusters import _min_intra_similarity

        # No comparable pair (both vecs missing) → 1.0 (nothing to contradict).
        assert _min_intra_similarity([0, 1], ["a", "b"], {}) == 1.0
