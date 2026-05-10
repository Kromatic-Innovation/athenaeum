# SPDX-License-Identifier: Apache-2.0
"""Tests for cross-scope contradiction detection (issue #125).

Covers the four modes (``off``, ``ancestor``, ``similarity``, ``both``),
ancestor pooling, similarity-pair generation, and the cluster-size cap.

No live network or live chromadb. The similarity sweep is exercised
against a stubbed embedding provider with deterministic vectors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from athenaeum.cross_scope import (
    DEFAULT_CLUSTER_SIZE_CAP,
    DEFAULT_MODE,
    DEFAULT_SIMILARITY_THRESHOLD,
    candidate_to_auto_memory_files,
    chunk_by_cap,
    cross_scope_similarity_pairs,
    pool_cluster_with_ancestors,
    resolve_cluster_size_cap,
    resolve_cross_scope_mode,
    resolve_similarity_threshold,
    scope_ancestors,
    sort_newest_first,
)
from athenaeum.merge import merge_clusters_to_wiki
from athenaeum.models import AutoMemoryFile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_am(
    scope_dir: Path,
    filename: str,
    body: str,
    *,
    origin_scope: str,
    created: str | None = None,
) -> AutoMemoryFile:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    fm = ["---", "name: probe", "type: feedback"]
    if created:
        fm.append(f"created: '{created}'")
    fm.append("---")
    path.write_text("\n".join(fm) + "\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(
        path=path,
        origin_scope=origin_scope,
        memory_type="feedback",
        name="probe",
    )


def _fake_client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _detected_payload(m1_ref: str, m2_ref: str) -> str:
    return (
        '{"detected": true, "conflict_type": "prescriptive", '
        f'"members_involved": ["{m1_ref}", "{m2_ref}"], '
        '"conflicting_passages": ["A", "B"], '
        '"rationale": "synthetic conflict"}'
    )


def _no_conflict_payload() -> str:
    return (
        '{"detected": false, "conflict_type": null, '
        '"members_involved": [], "conflicting_passages": [], '
        '"rationale": ""}'
    )


# ---------------------------------------------------------------------------
# scope_ancestors
# ---------------------------------------------------------------------------


class TestScopeAncestors:
    def test_three_level_scope(self) -> None:
        assert scope_ancestors("-Users-tristankromer-Code-foo") == [
            "-Users-tristankromer-Code",
            "-Users-tristankromer",
            "-Users",
        ]

    def test_root_scope_has_no_ancestors(self) -> None:
        assert scope_ancestors("-Users") == []

    def test_unscoped_has_no_ancestors(self) -> None:
        assert scope_ancestors("_unscoped") == []

    def test_empty_returns_empty(self) -> None:
        assert scope_ancestors("") == []


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestConfigResolution:
    def test_default_mode_is_ancestor(self) -> None:
        assert DEFAULT_MODE == "ancestor"
        assert resolve_cross_scope_mode(None) == "ancestor"

    def test_env_var_overrides_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_CROSS_SCOPE_MODE", "similarity")
        cfg = {"contradiction": {"cross_scope_mode": "off"}}
        assert resolve_cross_scope_mode(cfg) == "similarity"

    def test_invalid_env_falls_back_to_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_CROSS_SCOPE_MODE", "garbage")
        assert resolve_cross_scope_mode(None) == "ancestor"

    def test_yaml_config_picked_up(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_CROSS_SCOPE_MODE", raising=False)
        cfg = {"contradiction": {"cross_scope_mode": "both"}}
        assert resolve_cross_scope_mode(cfg) == "both"

    def test_size_cap_default(self) -> None:
        assert resolve_cluster_size_cap(None) == DEFAULT_CLUSTER_SIZE_CAP

    def test_size_cap_override(self) -> None:
        cfg = {"contradiction": {"cluster_size_cap": 5}}
        assert resolve_cluster_size_cap(cfg) == 5

    def test_threshold_default(self) -> None:
        assert resolve_similarity_threshold(None) == DEFAULT_SIMILARITY_THRESHOLD

    def test_threshold_override(self) -> None:
        cfg = {"contradiction": {"similarity_threshold": 0.9}}
        assert resolve_similarity_threshold(cfg) == 0.9


# ---------------------------------------------------------------------------
# Ancestor pooling + chunking
# ---------------------------------------------------------------------------


class TestAncestorPooling:
    def test_pools_ancestor_scope_member(self, tmp_path: Path) -> None:
        a = _write_am(
            tmp_path / "child",
            "feedback_a.md",
            "A body",
            origin_scope="-Users-tristankromer-Code-foo",
        )
        b = _write_am(
            tmp_path / "parent",
            "feedback_b.md",
            "B body",
            origin_scope="-Users-tristankromer-Code",
        )
        unrelated = _write_am(
            tmp_path / "other",
            "feedback_c.md",
            "C body",
            origin_scope="-Users-tristankromer-Code-bar",
        )
        pooled = pool_cluster_with_ancestors([a], [a, b, unrelated])
        pooled_paths = {str(am.path) for am in pooled}
        assert str(a.path) in pooled_paths
        assert str(b.path) in pooled_paths
        # Sibling scope is NOT an ancestor.
        assert str(unrelated.path) not in pooled_paths

    def test_dedupes_by_path(self, tmp_path: Path) -> None:
        a = _write_am(
            tmp_path / "child",
            "a.md",
            "A",
            origin_scope="-Users-tristankromer-Code-foo",
        )
        # Same file appears in both lists; should appear once.
        pooled = pool_cluster_with_ancestors([a], [a])
        assert len(pooled) == 1

    def test_no_ancestors_returns_originals(self, tmp_path: Path) -> None:
        a = _write_am(
            tmp_path / "u",
            "a.md",
            "A",
            origin_scope="_unscoped",
        )
        pooled = pool_cluster_with_ancestors([a], [a])
        assert pooled == [a]


class TestChunkByCap:
    def test_under_cap_no_split(self, tmp_path: Path) -> None:
        members = [
            _write_am(
                tmp_path / "s",
                f"a{i}.md",
                f"body {i}",
                origin_scope="-Users-tristankromer-Code-foo",
            )
            for i in range(3)
        ]
        chunks = chunk_by_cap(members, cap=5)
        assert len(chunks) == 1
        assert len(chunks[0]) == 3

    def test_over_cap_splits_two_chunks(self, tmp_path: Path) -> None:
        members = [
            _write_am(
                tmp_path / "s",
                f"a{i}.md",
                f"body {i}",
                origin_scope="-Users-tristankromer-Code-foo",
                created=f"2026-01-{i + 1:02d}",
            )
            for i in range(6)
        ]
        chunks = chunk_by_cap(members, cap=5)
        assert len(chunks) == 2
        assert len(chunks[0]) == 5
        assert len(chunks[1]) == 1

    def test_newest_first_in_chunks(self, tmp_path: Path) -> None:
        members = [
            _write_am(
                tmp_path / "s",
                f"a{i}.md",
                f"body {i}",
                origin_scope="-Users-tristankromer-Code-foo",
                created=f"2026-01-{i + 1:02d}",
            )
            for i in range(4)
        ]
        ordered = sort_newest_first(members)
        # Newest is the one with created '2026-01-04' (last index).
        assert ordered[0].path.name == "a3.md"


# ---------------------------------------------------------------------------
# Similarity sweep (stubbed embedding provider)
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedding provider for tests.

    Constructor takes ``{recall_index_id: vector}``. ``fetch_embeddings``
    returns the intersection of requested ids and the stored map.
    """

    def __init__(self, embeddings: dict[str, list[float]]) -> None:
        self._embeddings = embeddings

    def fetch_embeddings(
        self,
        ids: Any,
        cache_dir: Path,
    ) -> dict[str, list[float]]:
        del cache_dir
        return {i: self._embeddings[i] for i in ids if i in self._embeddings}


class TestSimilaritySweep:
    def test_finds_pair_above_threshold(self, tmp_path: Path) -> None:
        # Two raw entries in DIFFERENT scope branches (no ancestor link).
        root = tmp_path / "raw" / "auto-memory"
        a = _write_am(
            root / "-Users-trk-Code-foo",
            "feedback_a.md",
            "A body",
            origin_scope="-Users-trk-Code-foo",
        )
        b = _write_am(
            root / "-Users-trk-Code-bar",
            "feedback_b.md",
            "B body",
            origin_scope="-Users-trk-Code-bar",
        )
        a_id = f"auto-memory/-Users-trk-Code-foo/feedback_a.md"
        b_id = f"auto-memory/-Users-trk-Code-bar/feedback_b.md"
        embedder = _StubEmbedder(
            {
                a_id: [1.0, 0.0],
                b_id: [0.99, 0.05],  # ~0.998 cosine
            }
        )
        candidates = cross_scope_similarity_pairs(
            [a, b],
            extra_roots=[root],
            cache_dir=tmp_path / "cache",
            threshold=0.85,
            embedding_provider=embedder,
        )
        assert len(candidates) == 1
        assert candidates[0].similarity > 0.85

    def test_below_threshold_not_returned(self, tmp_path: Path) -> None:
        root = tmp_path / "raw" / "auto-memory"
        a = _write_am(
            root / "-Users-trk-Code-foo",
            "a.md",
            "A",
            origin_scope="-Users-trk-Code-foo",
        )
        b = _write_am(
            root / "-Users-trk-Code-bar",
            "b.md",
            "B",
            origin_scope="-Users-trk-Code-bar",
        )
        a_id = "auto-memory/-Users-trk-Code-foo/a.md"
        b_id = "auto-memory/-Users-trk-Code-bar/b.md"
        embedder = _StubEmbedder(
            {
                a_id: [1.0, 0.0],
                b_id: [0.0, 1.0],  # cosine 0
            }
        )
        candidates = cross_scope_similarity_pairs(
            [a, b],
            extra_roots=[root],
            cache_dir=tmp_path / "cache",
            threshold=0.85,
            embedding_provider=embedder,
        )
        assert candidates == []

    def test_excluded_pairs_filtered(self, tmp_path: Path) -> None:
        root = tmp_path / "raw" / "auto-memory"
        a = _write_am(
            root / "-Users-trk-Code-foo",
            "a.md",
            "A",
            origin_scope="-Users-trk-Code-foo",
        )
        b = _write_am(
            root / "-Users-trk-Code-bar",
            "b.md",
            "B",
            origin_scope="-Users-trk-Code-bar",
        )
        a_id = "auto-memory/-Users-trk-Code-foo/a.md"
        b_id = "auto-memory/-Users-trk-Code-bar/b.md"
        embedder = _StubEmbedder({a_id: [1.0, 0.0], b_id: [0.99, 0.05]})
        excluded = {tuple(sorted((str(a.path), str(b.path))))}
        candidates = cross_scope_similarity_pairs(
            [a, b],
            extra_roots=[root],
            cache_dir=tmp_path / "cache",
            threshold=0.85,
            excluded_pair_keys=excluded,
            embedding_provider=embedder,
        )
        assert candidates == []

    def test_includes_wiki_files(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw" / "auto-memory"
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True)
        wiki_path = wiki_root / "auto-foo.md"
        wiki_path.write_text(
            "---\nname: foo\ntype: auto-memory\n---\nWiki body\n",
            encoding="utf-8",
        )
        a = _write_am(
            raw_root / "-Users-trk-Code-foo",
            "a.md",
            "A",
            origin_scope="-Users-trk-Code-foo",
        )
        a_id = "auto-memory/-Users-trk-Code-foo/a.md"
        wiki_id = "auto-foo.md"
        embedder = _StubEmbedder(
            {
                a_id: [1.0, 0.0],
                wiki_id: [0.99, 0.05],
            }
        )
        candidates = cross_scope_similarity_pairs(
            [a],
            wiki_files=[wiki_path],
            wiki_root=wiki_root,
            extra_roots=[raw_root],
            cache_dir=tmp_path / "cache",
            threshold=0.85,
            embedding_provider=embedder,
        )
        assert len(candidates) == 1
        paths = {candidates[0].a_path, candidates[0].b_path}
        assert wiki_path in paths


# ---------------------------------------------------------------------------
# End-to-end mode wiring through merge_clusters_to_wiki
# ---------------------------------------------------------------------------


def _build_knowledge_root(
    tmp_path: Path,
    *,
    members: list[tuple[str, str, str]],
    cluster_groups: list[list[int]] | None = None,
) -> Path:
    """Stand up a minimal knowledge-root with raw + cluster JSONL.

    members: list of (scope, filename, body).
    cluster_groups: list of index-lists. If None, each member is a singleton.
    Returns the knowledge_root path.
    """
    root = tmp_path / "knowledge"
    raw = root / "raw" / "auto-memory"
    written: list[Path] = []
    for scope, filename, body in members:
        scope_dir = raw / scope
        scope_dir.mkdir(parents=True, exist_ok=True)
        path = scope_dir / filename
        path.write_text(
            "---\nname: probe\ntype: feedback\n---\n" + body + "\n",
            encoding="utf-8",
        )
        written.append(path)

    if cluster_groups is None:
        cluster_groups = [[i] for i in range(len(members))]

    rows: list[dict[str, Any]] = []
    for seq, group in enumerate(cluster_groups):
        member_paths = [f"{members[i][0]}/{members[i][1]}" for i in group]
        first_scope = members[group[0]][0].lstrip("-_") or "unscoped"
        rows.append(
            {
                "cluster_id": f"{first_scope}-{seq:04d}",
                "member_paths": member_paths,
                "centroid_score": 1.0,
                "rationale": "test",
            }
        )
    cluster_path = root / "raw" / "_librarian-clusters.jsonl"
    cluster_path.parent.mkdir(parents=True, exist_ok=True)
    cluster_path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    # Minimal config, points extra_intake_roots to raw/auto-memory.
    (root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    return root


class TestModeWiring:
    def test_mode_off_per_cluster_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_CROSS_SCOPE_MODE", "off")
        root = _build_knowledge_root(
            tmp_path,
            members=[
                ("-Users-trk-Code-foo", "feedback_a.md", "A"),
                ("-Users-trk-Code", "feedback_b.md", "B"),
            ],
        )
        client = _fake_client(_no_conflict_payload())
        entries = merge_clusters_to_wiki(root, client=client, dry_run=True)
        # Two singleton clusters; each is size 1 so detector short-circuits;
        # no pooling because mode=off.
        assert len(entries) == 2
        # No detector calls because both clusters were singletons.
        assert client.messages.create.call_count == 0

    def test_mode_ancestor_pools_across_scopes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC#4 reproducer: two raw entries in ancestor-related scopes pool
        into one detector call."""
        monkeypatch.setenv("ATHENAEUM_CROSS_SCOPE_MODE", "ancestor")
        root = _build_knowledge_root(
            tmp_path,
            members=[
                ("-Users-trk-Code-foo", "feedback_a.md", "Always commit directly."),
                (
                    "-Users-trk-Code",
                    "feedback_b.md",
                    "Never commit directly; park on WIP.",
                ),
            ],
        )
        # Cluster row 0 holds only `a`; b lives under an ancestor scope.
        # Mode=ancestor pools b into the cluster, so the detector sees 2.
        m1_ref = "-Users-trk-Code-foo/feedback_a.md"
        m2_ref = "-Users-trk-Code/feedback_b.md"
        client = _fake_client(_detected_payload(m1_ref, m2_ref))
        entries = merge_clusters_to_wiki(root, client=client, dry_run=True)
        # First entry's pooled cluster catches the contradiction.
        flagged = [e for e in entries if e.contradictions_detected]
        assert len(flagged) >= 1
        assert client.messages.create.call_count >= 1

    def test_mode_similarity_picks_up_cross_branch_pair(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cross-tree-branch pair (no ancestor link) is caught via similarity."""
        monkeypatch.setenv("ATHENAEUM_CROSS_SCOPE_MODE", "similarity")
        root = _build_knowledge_root(
            tmp_path,
            members=[
                ("-Users-trk-Code-foo", "feedback_a.md", "Always X."),
                ("-Users-trk-Code-bar", "feedback_b.md", "Never X."),
            ],
        )
        # Patch the VectorBackend used inside merge.py's similarity sweep.
        from athenaeum import cross_scope as cs

        a_id = "auto-memory/-Users-trk-Code-foo/feedback_a.md"
        b_id = "auto-memory/-Users-trk-Code-bar/feedback_b.md"
        embedder = _StubEmbedder(
            {
                a_id: [1.0, 0.0],
                b_id: [0.99, 0.05],
            }
        )
        original_pairs = cs.cross_scope_similarity_pairs

        def patched_pairs(*args: Any, **kwargs: Any):
            kwargs["embedding_provider"] = embedder
            return original_pairs(*args, **kwargs)

        monkeypatch.setattr(
            "athenaeum.merge.cross_scope_similarity_pairs",
            patched_pairs,
        )

        m1_ref = "-Users-trk-Code-foo/feedback_a.md"
        m2_ref = "-Users-trk-Code-bar/feedback_b.md"
        client = _fake_client(_detected_payload(m1_ref, m2_ref))
        merge_clusters_to_wiki(root, client=client, dry_run=True)
        # Detector should be called at least once for the similarity pair
        # (singleton clusters skip per-cluster calls).
        assert client.messages.create.call_count >= 1

    def test_mode_similarity_covers_wiki_entry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Wiki-vs-raw pair (raw original gone for wiki) flagged via similarity."""
        monkeypatch.setenv("ATHENAEUM_CROSS_SCOPE_MODE", "similarity")
        root = _build_knowledge_root(
            tmp_path,
            members=[
                ("-Users-trk-Code-foo", "feedback_a.md", "Raw body"),
            ],
        )
        # Pre-write a wiki/auto-bar.md (simulates a previous merge).
        wiki = root / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "auto-bar.md").write_text(
            "---\nname: bar\ntype: auto-memory\n---\nWiki body\n",
            encoding="utf-8",
        )

        a_id = "auto-memory/-Users-trk-Code-foo/feedback_a.md"
        wiki_id = "auto-bar.md"
        embedder = _StubEmbedder(
            {
                a_id: [1.0, 0.0],
                wiki_id: [0.99, 0.05],
            }
        )
        from athenaeum import cross_scope as cs

        original_pairs = cs.cross_scope_similarity_pairs

        def patched_pairs(*args: Any, **kwargs: Any):
            kwargs["embedding_provider"] = embedder
            return original_pairs(*args, **kwargs)

        monkeypatch.setattr(
            "athenaeum.merge.cross_scope_similarity_pairs",
            patched_pairs,
        )

        client = _fake_client(_detected_payload("a", "auto-bar"))
        merge_clusters_to_wiki(root, client=client, dry_run=True)
        # Similarity sweep should fire at least once on the raw+wiki pair.
        assert client.messages.create.call_count >= 1

    def test_mode_both_ancestor_and_similarity(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mode=both: ancestor catches case A, similarity catches case B."""
        monkeypatch.setenv("ATHENAEUM_CROSS_SCOPE_MODE", "both")
        root = _build_knowledge_root(
            tmp_path,
            members=[
                ("-Users-trk-Code-foo", "feedback_a.md", "Always X."),
                ("-Users-trk-Code", "feedback_b.md", "Never X."),
                ("-Users-trk-Code-bar", "feedback_c.md", "Always Y."),
                ("-Users-trk-Code-baz", "feedback_d.md", "Never Y."),
            ],
        )
        a_id = "auto-memory/-Users-trk-Code-foo/feedback_a.md"
        b_id = "auto-memory/-Users-trk-Code/feedback_b.md"
        c_id = "auto-memory/-Users-trk-Code-bar/feedback_c.md"
        d_id = "auto-memory/-Users-trk-Code-baz/feedback_d.md"
        # a/b will be pooled by ancestor; c/d only via similarity.
        embedder = _StubEmbedder(
            {
                a_id: [1.0, 0.0],
                b_id: [0.0, 1.0],  # different vectors: NOT a similarity pair
                c_id: [0.5, 0.5],
                d_id: [0.51, 0.49],  # near-duplicate: similarity pair
            }
        )
        from athenaeum import cross_scope as cs

        original_pairs = cs.cross_scope_similarity_pairs

        def patched_pairs(*args: Any, **kwargs: Any):
            kwargs["embedding_provider"] = embedder
            return original_pairs(*args, **kwargs)

        monkeypatch.setattr(
            "athenaeum.merge.cross_scope_similarity_pairs",
            patched_pairs,
        )

        client = _fake_client(_no_conflict_payload())
        merge_clusters_to_wiki(root, client=client, dry_run=True)
        # ancestor pools a+b → 1 call; similarity sweep for c+d → 1 call.
        # Plus detection on each non-singleton-after-pooling cluster.
        # Minimum guarantee: at least 2 calls (ancestor pool + similarity).
        assert client.messages.create.call_count >= 2

    def test_cluster_size_cap_splits_into_chunks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """N+1 pooled members with cap=N → 2 detector calls."""
        monkeypatch.setenv("ATHENAEUM_CROSS_SCOPE_MODE", "ancestor")
        # Make 4 children + 2 ancestors, pool size = 6, cap=3 → 2 chunks.
        members = [
            ("-Users-trk-Code-foo", f"feedback_a{i}.md", f"body {i}") for i in range(4)
        ] + [
            ("-Users-trk-Code", "feedback_p1.md", "parent 1"),
            ("-Users-trk-Code", "feedback_p2.md", "parent 2"),
        ]
        # Single cluster of just the foo children (indices 0..3); ancestors
        # get pooled in by the runtime.
        root = _build_knowledge_root(
            tmp_path,
            members=members,
            cluster_groups=[[0, 1, 2, 3]],
        )
        # Override config to cap at 3.
        (root / "athenaeum.yaml").write_text(
            "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n"
            "contradiction:\n  cluster_size_cap: 3\n",
            encoding="utf-8",
        )
        client = _fake_client(_no_conflict_payload())
        merge_clusters_to_wiki(root, client=client, dry_run=True)
        # Pool size 6, cap 3 → 2 chunks → 2 detector calls.
        assert client.messages.create.call_count == 2


# ---------------------------------------------------------------------------
# candidate_to_auto_memory_files smoke
# ---------------------------------------------------------------------------


class TestCandidateUnwrap:
    def test_returns_two_records(self, tmp_path: Path) -> None:
        from athenaeum.cross_scope import SimilarityCandidate

        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text(
            "---\nname: a\ntype: feedback\n---\nA body\n",
            encoding="utf-8",
        )
        b.write_text(
            "---\nname: b\ntype: feedback\n---\nB body\n",
            encoding="utf-8",
        )
        cand = SimilarityCandidate(
            a_path=a,
            b_path=b,
            similarity=0.9,
            a_scope="-X",
            b_scope="-Y",
        )
        ams = candidate_to_auto_memory_files(cand)
        assert len(ams) == 2
        assert ams[0].name == "a"
        assert ams[1].origin_scope == "-Y"
