# SPDX-License-Identifier: Apache-2.0
"""THE merge gate for issue #370 PR2 — delta-scoped incremental compile.

Proves that the delta-scoped compile (cluster + merge only the changed files and
their affected clusters) produces BYTE-IDENTICAL output to the whole-corpus
compile, on the deterministic ``client=None`` path. Built on a REAL chromadb
index so ``fetch_embeddings`` / ``query_neighbors`` run the production embedding
path (real MiniLM vectors), not the hashing-trick fallback.

For each scenario S ∈ {new-cluster, join-existing, bridge-two, leave-cluster,
cascade-that-trips-fallback}:

- GOLDEN-BASE: write the fixture, build the index, full compile → wiki + report.
- Branch A: from a copy of GOLDEN-BASE, apply S, reindex, WHOLE-CORPUS compile.
- Branch B: from a copy of GOLDEN-BASE, apply S, reindex, DELTA compile.

Assertions:
- Branch A wiki bytes == Branch B wiki bytes (every ``auto-*.md``, incl. orphans).
- Cluster membership per stabilized ``cluster_id`` equal between A and B.
- UNTOUCHED entries in Branch B are byte- AND mtime-identical to GOLDEN-BASE
  (proving the delta path did not rewrite them).
- The cascade scenario logs a D2 blow-up WARNING and its delta output equals the
  whole-corpus output (a correct full fallback).

The embedding thresholds are deterministic for a fixed MiniLM model (the fixture
cosines were probed: near-dups ~0.99, cross-topic <0.25, the bridge ~0.60 to
both bridged clusters). ``pytest.importorskip("chromadb")`` skips when the
``[vector]`` extra is absent (repo convention).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from athenaeum.clusters import DEFAULT_CLUSTER_THRESHOLD
from athenaeum.config import load_config, resolve_extra_intake_roots
from athenaeum.delta import compute_affected_clusters
from athenaeum.librarian import _compile_auto_memory, discover_auto_memory_files
from athenaeum.merge import read_cluster_rows, resolve_cluster_output_path

# ---------------------------------------------------------------------------
# Fixture corpus: 12 files / 4 scope dirs → 5 clusters (alpha×4, beta×3,
# gamma×3, plus two singletons in the `misc` scope). Probed cosines: within a
# cluster ~0.99; every cross-cluster pair < 0.25.
# ---------------------------------------------------------------------------

# Bodies are wrapped with implicit string concatenation (NOT reflowed) so the
# exact text — and therefore the probed MiniLM cosines — stays byte-identical.
_A = (
    "Voltaire the autonomous inbox EA labels the inbox, drafts replies, "
    "and surfaces low-confidence items as pending decisions during email triage."
)
_B = (
    "The postgres pgvector migration runbook covers ivfflat and hnsw index rebuilds, "
    "dimension changes, and reindex ordering for the vector store."
)

BASE_FILES: dict[str, dict[str, str]] = {
    "alpha": {
        "project_voltaire_triage_a.md": _A,
        "project_voltaire_triage_b.md": (
            "Voltaire the autonomous inbox EA labels the inbox, drafts replies, "
            "and surfaces low-confidence email items as pending triage decisions "
            "each session."
        ),
        "project_voltaire_triage_c.md": (
            "Voltaire the autonomous inbox executive assistant labels the inbox, "
            "drafts replies, and surfaces low-confidence items as pending "
            "decisions in email triage."
        ),
        "feedback_voltaire_triage_d.md": (
            "During email triage Voltaire the autonomous inbox EA labels the inbox "
            "and drafts replies, surfacing low confidence items as pending decisions."
        ),
    },
    "beta": {
        "project_pgvector_migrate_a.md": _B,
        "project_pgvector_migrate_b.md": (
            "The postgres pgvector migration runbook covers hnsw and ivfflat index "
            "rebuilds, dimension changes, and reindex ordering for the vector store "
            "column."
        ),
        "reference_pgvector_migrate_c.md": (
            "Postgres pgvector migration runbook: ivfflat and hnsw index rebuilds, "
            "dimension changes, and reindex ordering for the vector store table."
        ),
    },
    "gamma": {
        "reference_docker_orphan_a.md": (
            "Force-quitting Docker Desktop kills only the GUI while "
            "com.docker.backend keeps running and holds every socket, blocking the "
            "next launch."
        ),
        "reference_docker_orphan_b.md": (
            "Force quitting Docker Desktop kills only the GUI but com.docker.backend "
            "keeps running and holds each socket, blocking the next Docker launch."
        ),
        "reference_docker_orphan_c.md": (
            "When you force-quit Docker Desktop it kills only the GUI; "
            "com.docker.backend keeps running and holds every socket and blocks the "
            "next launch."
        ),
    },
    "misc": {
        "reference_linkedin_export.md": (
            "The LinkedIn data export lives under the Desktop WIP folder for the "
            "given year and contains the connections CSV and company follows."
        ),
        "reference_apollo_yaml.md": (
            "The apollo enricher script splices two-space tag entries into "
            "zero-space blocks, breaking yaml safe_load on thousands of wiki files."
        ),
    },
}

# Distinct-topic bodies used by scenarios.
_NEW_TOPIC = (
    "Kubernetes horizontal pod autoscaler tuning for burst traffic with custom "
    "metrics and cooldown windows on the staging cluster."
)
# Bridge body: 2×alpha + 2×beta text → probed ~0.60 to alpha, ~0.68 to beta
# (both above the 0.55 threshold), so it single-links alpha and beta.
_BRIDGE = f"{_A} {_A} {_B} {_B}"


def _write_config(root: Path, extra: str = "") -> None:
    (root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n" + extra,
        encoding="utf-8",
    )


def _write_am_file(root: Path, scope: str, name: str, body: str) -> Path:
    d = root / "raw" / "auto-memory" / scope
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(
        f"---\nname: {name[:-3]}\ntype: auto-memory\n---\n{body}\n", encoding="utf-8"
    )
    return path


def _write_base(root: Path) -> None:
    for scope, files in BASE_FILES.items():
        for name, body in files.items():
            _write_am_file(root, scope, name, body)
    _write_config(root)
    (root / "wiki").mkdir(parents=True, exist_ok=True)


def _build_index(root: Path) -> Path:
    from athenaeum.search import VectorBackend

    cache = root / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    VectorBackend().build_index(
        root / "wiki", cache, extra_roots=[root / "raw" / "auto-memory"]
    )
    return cache


def _compile(root: Path, *, changed: set[Path] | None, monkeypatch) -> None:
    """Drive the exact auto-memory compile run() uses, on the client=None path.

    ``changed=None`` → whole-corpus; a set → delta (run's own D5/F6 gates apply
    inside ``_compile_auto_memory``).
    """
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(root / ".cache"))
    config = load_config(root)
    files = discover_auto_memory_files(root, config=config)
    _compile_auto_memory(
        files,
        root,
        config=config,
        dry_run=False,
        client=None,
        usage=None,
        changed_paths=changed,
    )


def _read_wiki(root: Path) -> dict[str, bytes]:
    wiki = root / "wiki"
    return {p.name: p.read_bytes() for p in sorted(wiki.glob("auto-*.md"))}


def _wiki_mtimes(root: Path) -> dict[str, int]:
    wiki = root / "wiki"
    return {p.name: p.stat().st_mtime_ns for p in sorted(wiki.glob("auto-*.md"))}


def _cluster_membership(root: Path) -> dict[str, list[str]]:
    path = resolve_cluster_output_path(root, config=load_config(root))
    return {
        str(r.get("cluster_id", "")): sorted(str(m) for m in r.get("member_paths", []))
        for r in read_cluster_rows(path)
    }


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build the base corpus + index and produce the whole-corpus GOLDEN-BASE."""
    pytest.importorskip("chromadb")
    root = tmp_path / "golden"
    _write_base(root)
    _build_index(root)
    _compile(root, changed=None, monkeypatch=monkeypatch)
    return root


def _make_branch(golden_root: Path, dest: Path) -> Path:
    """Copy GOLDEN-BASE (raw + cache + wiki + report) into a fresh branch dir.

    ``copytree`` preserves mtimes (copy2), so an unaffected wiki file that the
    delta path does not rewrite keeps GOLDEN-BASE's mtime — the load-bearing
    signal for the "not rewritten" assertion.
    """
    shutil.copytree(golden_root, dest)
    return dest


def _reindex(root: Path) -> None:
    from athenaeum.search import VectorBackend

    # Incremental (#348): re-embeds only the added/changed/removed file(s).
    VectorBackend().build_index(
        root / "wiki", root / ".cache", extra_roots=[root / "raw" / "auto-memory"]
    )


# ---------------------------------------------------------------------------
# Scenario mutators: each applies S to a branch's raw + returns changed abspaths.
# ---------------------------------------------------------------------------


def _s_new_cluster(root: Path) -> set[Path]:
    return {_write_am_file(root, "misc", "reference_k8s_hpa.md", _NEW_TOPIC)}


def _s_join_existing(root: Path) -> set[Path]:
    # Near-dup of the alpha cluster → joins it (alpha grows 4 → 5).
    body = (
        "Voltaire the autonomous inbox EA labels the inbox and drafts replies, "
        "surfacing low confidence items as pending decisions during email triage "
        "sessions."
    )
    return {_write_am_file(root, "alpha", "project_voltaire_triage_e.md", body)}


def _s_bridge_two(root: Path) -> set[Path]:
    # Single-links alpha and beta into one cluster (alpha4 + beta3 + bridge = 8).
    return {_write_am_file(root, "alpha", "project_bridge_ab.md", _BRIDGE)}


def _s_leave_cluster(root: Path) -> set[Path]:
    # Rewrite an alpha member's body to a near-dup of the gamma (docker) topic:
    # it LEAVES alpha (4 → 3) and joins gamma (3 → 4). Both clusters keep their
    # existing topic slugs (alpha still "triage/voltaire", gamma still
    # "docker/orphan"), so there is no run-global slug collision — this exercises
    # the clean delta path (member leaves a cluster) end-to-end.
    body = BASE_FILES["gamma"]["reference_docker_orphan_a.md"]
    path = root / "raw" / "auto-memory" / "alpha" / "project_voltaire_triage_c.md"
    path.write_text(
        f"---\nname: project_voltaire_triage_c\ntype: auto-memory\n---\n{body}\n",
        encoding="utf-8",
    )
    return {path}


SCENARIOS = {
    "new-cluster": _s_new_cluster,
    "join-existing": _s_join_existing,
    "bridge-two": _s_bridge_two,
    "leave-cluster": _s_leave_cluster,
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_delta_equals_full(
    golden: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    mutate = SCENARIOS[scenario]
    golden_wiki = _read_wiki(golden)
    golden_mtimes = _wiki_mtimes(golden)

    # Branch A — whole-corpus compile including S.
    branch_a = _make_branch(golden, tmp_path / f"{scenario}-A")
    mutate(branch_a)
    _reindex(branch_a)
    _compile(branch_a, changed=None, monkeypatch=monkeypatch)

    # Branch B — delta compile with the same S.
    branch_b = _make_branch(golden, tmp_path / f"{scenario}-B")
    changed = mutate(branch_b)
    _reindex(branch_b)
    _compile(branch_b, changed=changed, monkeypatch=monkeypatch)

    wiki_a = _read_wiki(branch_a)
    wiki_b = _read_wiki(branch_b)

    # (1) Full == delta, byte-for-byte, over the entire wiki (orphans included).
    assert wiki_a == wiki_b, (
        f"[{scenario}] delta wiki diverged from full wiki: "
        f"A-only={set(wiki_a) - set(wiki_b)}, B-only={set(wiki_b) - set(wiki_a)}"
    )

    # (2) Cluster membership per stabilized cluster_id is identical.
    assert _cluster_membership(branch_a) == _cluster_membership(
        branch_b
    ), f"[{scenario}] spliced delta report diverged from full report"

    # (3) Untouched entries in Branch B are byte + mtime identical to GOLDEN
    #     (proof the delta path did not rewrite them). "Affected" = any entry
    #     the whole-corpus compile changed vs GOLDEN, plus any new entry.
    affected = {
        name
        for name in set(golden_wiki) | set(wiki_a)
        if golden_wiki.get(name) != wiki_a.get(name)
    }
    assert affected, f"[{scenario}] expected at least one affected entry"
    b_mtimes = _wiki_mtimes(branch_b)
    for name, gbytes in golden_wiki.items():
        if name in affected:
            continue
        assert wiki_b[name] == gbytes, f"[{scenario}] untouched {name} bytes changed"
        assert (
            b_mtimes[name] == golden_mtimes[name]
        ), f"[{scenario}] untouched {name} was rewritten (mtime changed) by delta"


def test_cascade_trips_fallback(
    golden: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A change whose closure exceeds the caps trips D2 → correct full fallback."""
    # Branch A — whole-corpus compile including the bridge (affects 2 clusters).
    branch_a = _make_branch(golden, tmp_path / "cascade-A")
    _s_bridge_two(branch_a)
    _reindex(branch_a)
    _compile(branch_a, changed=None, monkeypatch=monkeypatch)

    # Branch B — delta requested but with a max_affected_clusters cap of 1: the
    # bridge pulls in alpha AND beta (2 clusters) → D2 blow-up → full fallback.
    branch_b = _make_branch(golden, tmp_path / "cascade-B")
    _write_config(
        branch_b,
        extra="librarian:\n  delta:\n    max_affected_clusters: 1\n",
    )
    changed = _s_bridge_two(branch_b)
    _reindex(branch_b)
    with caplog.at_level(logging.WARNING, logger="athenaeum.delta"):
        _compile(branch_b, changed=changed, monkeypatch=monkeypatch)

    # A D2 blow-up WARNING was logged, and the fallback output equals the full
    # whole-corpus output.
    assert any(
        "blow-up (D2)" in rec.getMessage() for rec in caplog.records
    ), "expected a D2 blow-up WARNING"
    assert _read_wiki(branch_a) == _read_wiki(branch_b)
    assert _cluster_membership(branch_a) == _cluster_membership(branch_b)


def test_no_changed_paths_is_byte_identical_to_full(
    golden: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recompile with ``changed_paths=None`` reproduces GOLDEN byte-for-byte.

    Guards the invariant that the new params default to whole-corpus behaviour:
    a re-run over the unchanged corpus rewrites every entry to identical bytes
    (the content-addressed cluster_id is stable), so nothing drifts.
    """
    golden_wiki = _read_wiki(golden)
    rerun = _make_branch(golden, tmp_path / "rerun")
    _compile(rerun, changed=None, monkeypatch=monkeypatch)
    assert _read_wiki(rerun) == golden_wiki


# ---------------------------------------------------------------------------
# Chain topology: A–B–C where B is the SOLE bridge holding C (probed cosines:
# A~B=0.61, B~C=0.77, A~C=0.16 — so {A,B,C} is ONE single-linkage cluster).
# Changing B to drop the B~C link must repartition the WHOLE prior cluster.
# ---------------------------------------------------------------------------

# B mixes both topics (== the bridge recipe) so it links A (voltaire) and C
# (pgvector); A and C do not link each other. Kept verbatim for stable cosines.
# Filenames are chosen so the post-split clusters derive DISTINCT topic slugs
# ({A,B} → "inbox/triage/bridge", {C} → "pgvector/store"), exercising the clean
# delta path rather than the F6 slug-collision fallback (covered separately).
_CHAIN_B = f"{_A} {_A} {_B} {_B}"
_CHAIN_DOCK = BASE_FILES["gamma"]["reference_docker_orphan_a.md"]

_CHAIN_MEMBERS = [
    "chain/project_inbox_triage_a.md",
    "chain/project_inbox_triage_bridge.md",
    "chain/reference_pgvector_store.md",
]


def _write_chain_base(root: Path, b_body: str) -> None:
    _write_am_file(root, "chain", "project_inbox_triage_a.md", _A)
    _write_am_file(root, "chain", "project_inbox_triage_bridge.md", b_body)
    _write_am_file(root, "chain", "reference_pgvector_store.md", _B)
    # An unrelated singleton, to prove delta leaves untouched entries alone.
    _write_am_file(root, "misc", "reference_docker_note.md", _CHAIN_DOCK)
    _write_config(root)
    (root / "wiki").mkdir(parents=True, exist_ok=True)


def _break_bridge(root: Path) -> set[Path]:
    """Rewrite B to a pure-A near-dup so it no longer links C (B~C falls below)."""
    p = root / "raw" / "auto-memory" / "chain" / "project_inbox_triage_bridge.md"
    p.write_text(
        f"---\nname: project_inbox_triage_bridge\ntype: auto-memory\n---\n{_A}\n",
        encoding="utf-8",
    )
    return {p}


def test_chain_transitive_repartition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-linkage chain A–B–C repartitions correctly when the sole bridge
    B changes: the WHOLE prior cluster {A,B,C} is pooled and re-clustered, so C
    (unchanged, and no longer linked by B) is split off — matching a full run.
    """
    pytest.importorskip("chromadb")

    golden = tmp_path / "chain-golden"
    _write_chain_base(golden, _CHAIN_B)
    _build_index(golden)
    _compile(golden, changed=None, monkeypatch=monkeypatch)

    # Sanity: GOLDEN really is the A–B–C chain as one cluster + a docker singleton.
    gmem = _cluster_membership(golden)
    assert any(
        sorted(m) == _CHAIN_MEMBERS for m in gmem.values()
    ), f"expected the A-B-C chain as one cluster, got {list(gmem.values())}"
    golden_wiki = _read_wiki(golden)
    golden_mtimes = _wiki_mtimes(golden)

    # (a) Pin the closure: compute_affected_clusters must pool the ENTIRE prior
    #     cluster {A,B,C} even though changed B no longer links C.
    probe = _make_branch(golden, tmp_path / "chain-probe")
    changed = _break_bridge(probe)
    _reindex(probe)
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(probe / ".cache"))
    config = load_config(probe)
    files = discover_auto_memory_files(probe, config=config)
    prior_rows = read_cluster_rows(resolve_cluster_output_path(probe, config=config))
    scope = compute_affected_clusters(
        changed,
        prior_rows,
        files,
        extra_roots=resolve_extra_intake_roots(probe),
        cache_dir=probe / ".cache",
        threshold=DEFAULT_CLUSTER_THRESHOLD,
        max_affected_clusters=8,
        max_affected_members=200,
    )
    assert scope is not None
    assert sorted(p.name for p in (f.path for f in scope.pool)) == [
        "project_inbox_triage_a.md",
        "project_inbox_triage_bridge.md",
        "reference_pgvector_store.md",
    ]

    # (b) Full == delta, byte-for-byte + membership, and C split into a singleton.
    branch_a = _make_branch(golden, tmp_path / "chain-A")
    _break_bridge(branch_a)
    _reindex(branch_a)
    _compile(branch_a, changed=None, monkeypatch=monkeypatch)

    branch_b = _make_branch(golden, tmp_path / "chain-B")
    changed_b = _break_bridge(branch_b)
    _reindex(branch_b)
    _compile(branch_b, changed=changed_b, monkeypatch=monkeypatch)

    wiki_a = _read_wiki(branch_a)
    wiki_b = _read_wiki(branch_b)
    assert wiki_a == wiki_b
    assert _cluster_membership(branch_a) == _cluster_membership(branch_b)
    # C really was repartitioned out into its own cluster.
    assert any(
        m == ["chain/reference_pgvector_store.md"]
        for m in _cluster_membership(branch_b).values()
    ), "C should split into its own singleton cluster after the bridge breaks"

    # The unrelated docker singleton is untouched (byte + mtime) by the delta.
    affected = {
        name
        for name in set(golden_wiki) | set(wiki_a)
        if golden_wiki.get(name) != wiki_a.get(name)
    }
    b_mtimes = _wiki_mtimes(branch_b)
    for name, gbytes in golden_wiki.items():
        if name in affected:
            continue
        assert wiki_b[name] == gbytes
        assert (
            b_mtimes[name] == golden_mtimes[name]
        ), f"untouched {name} was rewritten by delta"


def test_f6_slug_collision_falls_back(
    golden: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An affected entry's base slug colliding with another corpus entry trips
    the F6 guard → full whole-corpus fallback (WARNING logged; delta == full).

    Rewriting alpha's ``project_voltaire_triage_c`` body to a distinct topic
    makes it LEAVE the alpha cluster as a singleton — but it keeps its
    voltaire/triage filename, so its derived base slug still equals the shrunken
    alpha cluster's slug. ``_delta_slug_collision`` detects that and forces a
    full compile (the subset merge could otherwise pick a divergent suffix).
    """

    def mutate(root: Path) -> set[Path]:
        p = root / "raw" / "auto-memory" / "alpha" / "project_voltaire_triage_c.md"
        p.write_text(
            "---\nname: project_voltaire_triage_c\ntype: auto-memory\n---\n"
            f"{_NEW_TOPIC}\n",
            encoding="utf-8",
        )
        return {p}

    branch_a = _make_branch(golden, tmp_path / "f6-A")
    mutate(branch_a)
    _reindex(branch_a)
    _compile(branch_a, changed=None, monkeypatch=monkeypatch)

    branch_b = _make_branch(golden, tmp_path / "f6-B")
    changed = mutate(branch_b)
    _reindex(branch_b)
    with caplog.at_level(logging.WARNING, logger="athenaeum"):
        _compile(branch_b, changed=changed, monkeypatch=monkeypatch)

    assert any(
        "collides run-globally (F6)" in rec.getMessage() for rec in caplog.records
    ), "expected an F6 slug-collision WARNING"
    assert _read_wiki(branch_a) == _read_wiki(branch_b)
    assert _cluster_membership(branch_a) == _cluster_membership(branch_b)
