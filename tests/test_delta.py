# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the delta-compile primitives (issue #370 PR2).

Complements the end-to-end equivalence gate (test_delta_compile_equivalence.py)
with targeted coverage of the building blocks: ``query_neighbors`` (real
chromadb), the fallback triggers (D1 no report, D3 stale index), the report
splice, and run()'s D5 veto of the delta path under LLM contradiction mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import librarian as lib
from athenaeum.clusters import Cluster
from athenaeum.delta import (
    AffectedScope,
    compute_affected_clusters,
    splice_cluster_report,
)


def _write_am(root: Path, scope: str, name: str, body: str) -> Path:
    d = root / "raw" / "auto-memory" / scope
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(f"---\nname: {name[:-3]}\ntype: auto-memory\n---\n{body}\n")
    return p


def _config(root: Path) -> None:
    (root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n"
    )
    (root / "wiki").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# splice_cluster_report
# ---------------------------------------------------------------------------


def test_splice_replaces_affected_keeps_rest() -> None:
    prior = [
        {"cluster_id": "a-1", "member_paths": ["alpha/x.md"], "centroid_score": 1.0},
        {"cluster_id": "b-2", "member_paths": ["beta/y.md"], "centroid_score": 0.9},
    ]
    new_partial = [Cluster(cluster_id="a-9", member_paths=["alpha/x.md", "alpha/z.md"])]
    spliced = splice_cluster_report(prior, {"a-1"}, new_partial)
    ids = [c.cluster_id for c in spliced]
    assert ids == ["b-2", "a-9"]  # kept row first, then the re-clustered partial
    assert spliced[0].member_paths == ["beta/y.md"]


# ---------------------------------------------------------------------------
# compute_affected_clusters fallbacks
# ---------------------------------------------------------------------------


def test_d1_no_prior_report_returns_none(tmp_path: Path) -> None:
    root = tmp_path
    _config(root)
    p = _write_am(root, "alpha", "project_x.md", "hello world content")
    from athenaeum.config import resolve_extra_intake_roots
    from athenaeum.librarian import discover_auto_memory_files

    files = discover_auto_memory_files(root)
    scope = compute_affected_clusters(
        {p},
        [],  # D1: empty prior report
        files,
        extra_roots=resolve_extra_intake_roots(root),
        cache_dir=root / ".cache",
        threshold=0.55,
        max_affected_clusters=8,
        max_affected_members=200,
    )
    assert scope is None


def test_d3_changed_file_missing_from_index_returns_none(tmp_path: Path) -> None:
    """A changed file that HAS a prior cluster but is not in chromadb → D3."""
    root = tmp_path
    _config(root)
    p = _write_am(root, "alpha", "project_x.md", "hello world content")
    from athenaeum.config import resolve_extra_intake_roots
    from athenaeum.librarian import discover_auto_memory_files

    files = discover_auto_memory_files(root)
    # Prior report references the file, but NO chromadb index exists at cache_dir
    # → fetch_embeddings returns {} → the changed file is live but not a hit → D3.
    prior = [
        {
            "cluster_id": "alpha-old",
            "member_paths": ["alpha/project_x.md"],
            "centroid_score": 1.0,
        }
    ]
    scope = compute_affected_clusters(
        {p},
        prior,
        files,
        extra_roots=resolve_extra_intake_roots(root),
        cache_dir=root / ".cache-does-not-exist",
        threshold=0.55,
        max_affected_clusters=8,
        max_affected_members=200,
    )
    assert scope is None


def test_new_file_missing_index_is_ok_not_d3(tmp_path: Path) -> None:
    """A brand-new file (no prior cluster) missing from chromadb is NOT D3."""
    root = tmp_path
    _config(root)
    _write_am(root, "alpha", "project_x.md", "hello world content")
    new = _write_am(root, "alpha", "project_new.md", "totally different subject")
    from athenaeum.config import resolve_extra_intake_roots
    from athenaeum.librarian import discover_auto_memory_files

    files = discover_auto_memory_files(root)
    prior = [
        {
            "cluster_id": "alpha-old",
            "member_paths": ["alpha/project_x.md"],
            "centroid_score": 1.0,
        }
    ]
    # No index, but the CHANGED file (project_new) has no prior cluster → new_paths,
    # and project_x is unchanged, so no D3. Falls back to hashing-trick vectors.
    scope = compute_affected_clusters(
        {new},
        prior,
        files,
        extra_roots=resolve_extra_intake_roots(root),
        cache_dir=root / ".cache-none",
        threshold=0.55,
        max_affected_clusters=8,
        max_affected_members=200,
    )
    assert scope is not None
    assert isinstance(scope, AffectedScope)
    assert new in scope.new_paths


# ---------------------------------------------------------------------------
# query_neighbors (real chromadb path)
# ---------------------------------------------------------------------------


def test_query_neighbors_ranks_near_dups(tmp_path: Path) -> None:
    pytest.importorskip("chromadb")
    from athenaeum.clusters import _indexed_id_for
    from athenaeum.config import resolve_extra_intake_roots
    from athenaeum.librarian import discover_auto_memory_files
    from athenaeum.search import VectorBackend

    root = tmp_path
    _config(root)
    _write_am(
        root, "alpha", "project_a.md", "the quick brown fox jumps over the lazy dog"
    )
    _write_am(
        root, "alpha", "project_b.md", "the quick brown fox jumps over the lazy dogs"
    )
    _write_am(
        root,
        "beta",
        "reference_z.md",
        "postgres pgvector hnsw ivfflat migration runbook",
    )
    cache = root / ".cache"
    cache.mkdir()
    backend = VectorBackend()
    backend.build_index(
        root / "wiki", cache, extra_roots=[root / "raw" / "auto-memory"]
    )

    files = discover_auto_memory_files(root)
    roots = resolve_extra_intake_roots(root)
    by = {f.path.name: f for f in files}
    a_id = _indexed_id_for(by["project_a.md"], roots)
    a_vec = backend.fetch_embeddings([a_id], cache)[a_id]

    neighbors = backend.query_neighbors(a_vec, cache, k=10, exclude_ids=[a_id])
    ids = [nid for nid, _dist in neighbors]
    # The near-dup project_b must rank first; the unrelated beta file is farther.
    assert ids, "expected neighbors"
    b_id = _indexed_id_for(by["project_b.md"], roots)
    z_id = _indexed_id_for(by["reference_z.md"], roots)
    assert ids[0] == b_id
    assert a_id not in ids  # excluded
    # b (near-dup) is closer than z (unrelated) in the returned ranking.
    dist = dict(neighbors)
    assert dist[b_id] < dist[z_id]


# ---------------------------------------------------------------------------
# D5: run's delta veto under LLM contradiction mode
# ---------------------------------------------------------------------------


class _FakeClient:
    pass


def _capture_compile(monkeypatch):
    seen: dict[str, object] = {}

    def fake_cluster(files, kr, *, config=None, dry_run=False, changed_paths=None):
        seen["cluster_changed_paths"] = changed_paths
        return None

    def fake_merge(kr, **kwargs):
        seen["only_cluster_ids"] = kwargs.get("only_cluster_ids")
        return []

    monkeypatch.setattr(lib, "_run_cluster_pass", fake_cluster)
    monkeypatch.setattr(lib, "merge_clusters_to_wiki", fake_merge)
    return seen


def test_d5_llm_mode_forces_whole_corpus(tmp_path: Path, monkeypatch) -> None:
    """client set + cross_scope_mode != off → delta vetoed (whole-corpus)."""
    root = tmp_path
    _config(root)
    p = _write_am(root, "alpha", "project_x.md", "content")
    from athenaeum.config import load_config
    from athenaeum.librarian import discover_auto_memory_files

    config = load_config(root)  # default cross_scope_mode = ancestor (active)
    files = discover_auto_memory_files(root, config=config)
    seen = _capture_compile(monkeypatch)
    lib._compile_auto_memory(
        files,
        root,
        config=config,
        dry_run=False,
        client=_FakeClient(),
        usage=None,
        changed_paths={p},
    )
    # D5: the cluster pass ran WHOLE-CORPUS (changed_paths not threaded through).
    assert seen["cluster_changed_paths"] is None
    assert seen["only_cluster_ids"] is None


def test_delta_engages_on_client_none(tmp_path: Path, monkeypatch) -> None:
    """client=None → delta threads changed_paths and scopes the merge."""
    root = tmp_path
    _config(root)
    p = _write_am(root, "alpha", "project_x.md", "content")
    from athenaeum.config import load_config
    from athenaeum.librarian import discover_auto_memory_files

    config = load_config(root)
    files = discover_auto_memory_files(root, config=config)

    seen: dict[str, object] = {}

    def fake_cluster(files, kr, *, config=None, dry_run=False, changed_paths=None):
        seen["cluster_changed_paths"] = changed_paths
        return {"alpha-new"}  # pretend one cluster was affected

    def fake_collision(kr, cfg, ids):
        return False

    def fake_merge(kr, **kwargs):
        seen["only_cluster_ids"] = kwargs.get("only_cluster_ids")
        return []

    monkeypatch.setattr(lib, "_run_cluster_pass", fake_cluster)
    monkeypatch.setattr(lib, "_delta_slug_collision", fake_collision)
    monkeypatch.setattr(lib, "merge_clusters_to_wiki", fake_merge)

    lib._compile_auto_memory(
        files,
        root,
        config=config,
        dry_run=False,
        client=None,
        usage=None,
        changed_paths={p},
    )
    assert seen["cluster_changed_paths"] == {p}
    assert seen["only_cluster_ids"] == {"alpha-new"}
