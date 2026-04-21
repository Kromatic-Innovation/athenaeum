# SPDX-License-Identifier: Apache-2.0
"""Tests for the auto-memory cluster pass (C2, issue #196).

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
    """
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
        if threshold is not None else ""
    )
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n"
        + threshold_line,
        encoding="utf-8",
    )


def _write_auto_memory_file(
    scope_dir: Path, name: str, frontmatter_name: str, body: str,
) -> Path:
    """Write a single auto-memory markdown file and return its path."""
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / name
    path.write_text(
        "---\n"
        f"name: {frontmatter_name}\n"
        "type: project\n"
        "---\n"
        f"{body}\n",
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
        voltaire, "project_voltaire_nanoclaw.md",
        "Voltaire nanoclaw toolchain",
        "Voltaire and nanoclaw are the ticklestick agent toolchain. "
        + common_tail,
    )
    _write_auto_memory_file(
        voltaire, "project_voltaire_iMessage_channel.md",
        "Voltaire iMessage channel",
        "Voltaire runs the nanoclaw iMessage channel handler. "
        + common_tail,
    )
    _write_auto_memory_file(
        voltaire, "project_nanoclaw_voltaire_tickle.md",
        "Nanoclaw ticklestick voltaire",
        "Nanoclaw and voltaire run ticklestick pipelines together. "
        + common_tail,
    )
    _write_auto_memory_file(
        voltaire, "project_voltaire_sessions.md",
        "Voltaire sessions",
        "Voltaire nanoclaw session events flow through ticklestick. "
        + common_tail,
    )
    # Typo clone — C2 must still cluster this with the 4 above despite
    # the prefix misspelling.
    _write_auto_memory_file(
        voltaire, "project_voltair_nanoclaw.md",
        "Voltair typo",
        "Voltair nanoclaw toolchain typo file. "
        + common_tail,
    )

    # Two unrelated singletons in another scope — must pass through as
    # size-1 clusters (no min-cluster-size filter).
    other = auto / "some-scope"
    _write_auto_memory_file(
        other, "reference_sentry_projects.md",
        "Sentry projects",
        "Sentry project IDs and slugs for the kromatic org.",
    )
    _write_auto_memory_file(
        other, "user_tristan_profile.md",
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
        scope, "feedback_prior_session_debris_v1.md",
        "Prior session debris v1",
        "Commit prior-session debris directly to develop. Do not park on WIP.",
    )
    _write_auto_memory_file(
        scope, "feedback_prior_session_debris_v2.md",
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
        scope, "reference_dns_flakiness.md",
        "DNS resolver flakiness",
        "macOS mDNSResponder flakes for specific hostnames under cgo resolver.",
    )
    _write_auto_memory_file(
        scope, "user_tristan_profile.md",
        "Tristan profile",
        "Consultant background, values thought leadership and cost-consciousness.",
    )

    _write_config(knowledge_root)
    return knowledge_root


# ---------------------------------------------------------------------------
# Pure-function tests (no CLI, no filesystem output)
# ---------------------------------------------------------------------------


class TestCosineHelpers:
    def test_cosine_identity_is_one(self) -> None:
        from athenaeum.clusters import _cosine

        v = [0.3, -0.2, 0.5, 1.1]
        assert _cosine(v, v) == pytest.approx(1.0)

    def test_cosine_zero_vector(self) -> None:
        from athenaeum.clusters import _cosine

        assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_cosine_length_mismatch(self) -> None:
        from athenaeum.clusters import _cosine

        assert _cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


class TestSingleLinkage:
    def test_components_are_connected(self) -> None:
        from athenaeum.clusters import _single_linkage

        # Graph: 0-1, 2 isolated, 3-4-5 chain
        adj: list[set[int]] = [
            {1}, {0}, set(), {4}, {3, 5}, {4},
        ]
        components = _single_linkage(adj)
        assert sorted(sorted(c) for c in components) == [[0, 1], [2], [3, 4, 5]]


# ---------------------------------------------------------------------------
# cluster_auto_memory_files behaviour
# ---------------------------------------------------------------------------


class TestClusterVoltaireFixture:
    def test_all_five_voltaire_files_collapse_to_one_cluster(
        self, voltaire_near_duplicate_root: Path,
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
            c for c in clusters
            if any("voltair" in p or "nanoclaw" in p for p in c.member_paths)
        ]
        singleton_clusters = [
            c for c in clusters
            if c not in voltaire_clusters
        ]

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
        self, voltaire_near_duplicate_root: Path,
    ) -> None:
        from athenaeum.clusters import cluster_auto_memory_files
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(voltaire_near_duplicate_root)
        extra_roots = resolve_extra_intake_roots(voltaire_near_duplicate_root)
        cache_dir = _build_vector_index(voltaire_near_duplicate_root, extra_roots)
        clusters = cluster_auto_memory_files(
            files, extra_roots=extra_roots, cache_dir=cache_dir,
        )
        voltaire_cluster = next(
            c for c in clusters if len(c.member_paths) > 1
        )
        assert "cosine" in voltaire_cluster.rationale.lower()
        assert voltaire_cluster.centroid_score > 0.0


class TestClusterContradictionFixture:
    def test_contradictory_files_cluster_together(
        self, contradiction_root: Path,
    ) -> None:
        """Same topic, opposing guidance → one cluster. C4 handles the disagreement."""
        from athenaeum.clusters import cluster_auto_memory_files
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(contradiction_root)
        extra_roots = resolve_extra_intake_roots(contradiction_root)
        cache_dir = _build_vector_index(contradiction_root, extra_roots)
        clusters = cluster_auto_memory_files(
            files, extra_roots=extra_roots, cache_dir=cache_dir,
        )

        # Exactly one cluster of size 2. C2 does not care that the
        # guidance is contradictory — it just groups by topic.
        assert len(clusters) == 1
        assert len(clusters[0].member_paths) == 2


class TestSingletonPassthrough:
    def test_two_unrelated_files_yield_two_clusters(
        self, singleton_pair_root: Path,
    ) -> None:
        from athenaeum.clusters import cluster_auto_memory_files
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(singleton_pair_root)
        extra_roots = resolve_extra_intake_roots(singleton_pair_root)
        cache_dir = _build_vector_index(singleton_pair_root, extra_roots)
        clusters = cluster_auto_memory_files(
            files, extra_roots=extra_roots, cache_dir=cache_dir,
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
        self, singleton_pair_root: Path,
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
            assert set(row.keys()) == {
                "cluster_id", "member_paths", "centroid_score", "rationale",
            }
            assert isinstance(row["cluster_id"], str)
            assert isinstance(row["member_paths"], list)
            assert all(isinstance(p, str) for p in row["member_paths"])
            assert isinstance(row["centroid_score"], float)
            assert isinstance(row["rationale"], str)

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
        self, voltaire_near_duplicate_root: Path, monkeypatch: pytest.MonkeyPatch,
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
            r for r in rows
            if any("voltair" in p or "nanoclaw" in p for p in r["member_paths"])
        ]
        assert len(voltaire_rows) == 1
        assert len(voltaire_rows[0]["member_paths"]) == 5

    def test_cluster_only_dry_run_does_not_write_report(
        self, voltaire_near_duplicate_root: Path,
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
