# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#463 (slice D of athenaeum#460) — live-client delta + full-compile cadence.

Extends the athenaeum#370 PR2 delta machinery (proven byte-equivalent by
``test_delta_compile_equivalence.py``) to the nightly LIVE-client run, which
previously ALWAYS forced a whole-corpus compile (the original D5 fallback).
Two new cache-dir stamps (``auto-memory-manifest.json``,
``full-compile-stamp.json``) let ``run()`` compute its own delta baseline
and a periodic whole-corpus reconciliation cadence
(``librarian.full_compile_every_days``, default 7 days) that is the
consistency backstop for the live-client delta path.

These tests drive the real ``run()`` entrypoint end-to-end (a real,
lightweight knowledge git repo; no chromadb — clustering falls back to the
hashing-trick embedding, see ``_deterministic_fallback_embeddings`` below, when
no vector index is built) with ``athenaeum.merge.detect_contradictions``
stubbed to a deterministic, call-counting fake — no live API, no network.
``retire=False`` throughout so the auto-memory intake files are NOT
moved/removed after a compile, letting a SECOND run see (and delta against)
the same corpus plus whatever the test adds.

Issue athenaeum#370's ``_fallback_embeddings`` (the graceful-degradation path used when
chromadb / the ``[vector]`` extra is unavailable, as in this environment) hash-
buckets tokens with Python's builtin ``hash()``, which is salted per-process by
``PYTHONHASHSEED`` — fine for its intended purpose (a same-run cosine
comparison), but it makes a fixed-threshold cross-topic-separation assertion
FLAKY across separate test-process runs (a different seed occasionally buckets
"sky"/"pgvector" tokens into a colliding dimension, single-linking two
otherwise-unrelated clusters). ``_deterministic_fallback_embeddings`` patches
in a ``hashlib``-based (unsalted) equivalent for every test in this module so
clustering is 100% reproducible without needing the real ``[vector]`` extra.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.librarian import (
    AUTO_MEMORY_MANIFEST_NAME,
    FULL_COMPILE_STAMP_NAME,
    _load_auto_memory_manifest,
    _load_full_compile_stamp,
    run,
)

pytestmark = pytest.mark.usefixtures("_fake_api_key", "_deterministic_fallback_embeddings")


def _stable_fallback_embeddings(files):
    """Deterministic (unsalted) stand-in for ``clusters._fallback_embeddings``.

    Identical shape/contract (384-dim, l2-normalized, hashing-trick bag of
    tokens from name/description/stem/body) but buckets tokens with
    ``hashlib.sha256`` instead of the salted builtin ``hash()``, so cosine
    similarity is reproducible across separate test-process invocations.
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
        tokens = [t for t in text.replace("_", " ").split() if len(t) >= 2]
        if not tokens:
            vec[0] = 1.0
            out[str(am.path)] = vec
            continue
        for tok in tokens:
            digest = hashlib.sha256(tok.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0.0:
            vec = [x / norm for x in vec]
        out[str(am.path)] = vec
    return out


@pytest.fixture(autouse=True)
def _deterministic_fallback_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    # athenaeum.delta imports ``_fallback_embeddings`` by name (``from
    # athenaeum.clusters import _fallback_embeddings``), binding its OWN
    # module-level reference at import time — patching
    # ``athenaeum.clusters._fallback_embeddings`` alone does NOT affect that
    # already-bound reference. The whole-corpus cluster pass
    # (athenaeum.clusters.cluster_auto_memory_files) and the delta closure
    # computation (athenaeum.delta.compute_affected_clusters) must both be
    # patched or the delta path stays flaky while the whole-corpus path looks
    # deterministic.
    monkeypatch.setattr(
        "athenaeum.clusters._fallback_embeddings", _stable_fallback_embeddings
    )
    monkeypatch.setattr(
        "athenaeum.delta._fallback_embeddings", _stable_fallback_embeddings
    )


@pytest.fixture
def _fake_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # A syntactically-plausible key so build_llm_client constructs a real (but
    # never-called — detect_contradictions is stubbed below) anthropic.Anthropic
    # client, exercising the ``client is not None`` (live) path.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")


def _write_am(root: Path, scope: str, name: str, body: str) -> Path:
    d = root / "raw" / "auto-memory" / scope
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(
        f"---\nname: {name[:-3]}\ntype: auto-memory\n---\n{body}\n", encoding="utf-8"
    )
    return path


def _seed_root(tmp_path: Path, cache_subdir: str = ".cache") -> Path:
    """A minimal knowledge git repo with two auto-memory scopes/clusters.

    ``alpha`` (2 members, "sky is blue") and ``beta`` (2 members, "pgvector
    migration") are semantically distinct, so the hashing-trick fallback
    embedding (no chromadb index built) still single-links each scope into
    its own cluster and keeps them apart from each other.
    """
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    _write_am(root, "alpha", "project_x0.md", "The sky is blue and clear today.")
    _write_am(root, "alpha", "project_x1.md", "The sky is blue and very clear today.")
    _write_am(root, "beta", "project_y0.md", "Postgres migrations use pgvector ivfflat.")
    _write_am(root, "beta", "project_y1.md", "Postgres migrations use pgvector hnsw.")
    (root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    return root


class _DetectSpy:
    """Deterministic, call-counting stand-in for ``detect_contradictions``.

    Never detects a contradiction (so retire/escalation stay out of the way)
    and records, per call, the sorted member paths of the cluster it was
    invoked on — so a test can assert BOTH the total call count and exactly
    which clusters were (not) touched.
    """

    def __init__(self) -> None:
        self.n_calls = 0
        self.clusters_seen: list[tuple[str, ...]] = []

    def __call__(self, members, client, *, config=None, usage=None):
        self.n_calls += 1
        self.clusters_seen.append(tuple(sorted(str(m.path) for m in members)))
        return ContradictionResult(detected=False, rationale="stub-no-conflict")


@pytest.fixture
def detect_spy(monkeypatch: pytest.MonkeyPatch) -> _DetectSpy:
    spy = _DetectSpy()
    monkeypatch.setattr("athenaeum.merge.detect_contradictions", spy)
    return spy


def _run(root: Path, **kwargs) -> int:
    kwargs.setdefault("retire", False)
    kwargs.setdefault("max_runtime", 0)
    return run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        dry_run=False,
        **kwargs,
    )


def _cache_dir(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = root / ".cache"
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
    return cache


# ---------------------------------------------------------------------------
# AC1 — live-client delta + reconciliation full run converge with nightly
# whole-corpus full runs (deterministic outputs + call counts, not detector
# text).
# ---------------------------------------------------------------------------


def test_live_delta_then_reconcile_converges_with_whole_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    # Branch W: whole-corpus baseline — force full compile every run.
    root_w = _seed_root(tmp_path / "w")
    _cache_dir(root_w, monkeypatch)
    assert _run(root_w, full_compile=True) == 0
    _write_am(root_w, "alpha", "project_x2.md", "The sky is blue and pretty clear today.")
    assert _run(root_w, full_compile=True) == 0
    wiki_w = {p.name: p.read_bytes() for p in sorted((root_w / "wiki").glob("auto-*.md"))}

    # Branch D: delta run (live client, default cadence) then a manual
    # reconciliation full run — must converge to the same wiki bytes.
    root_d = _seed_root(tmp_path / "d")
    _cache_dir(root_d, monkeypatch)
    assert _run(root_d) == 0  # run 1: no prior manifest -> whole-corpus baseline
    _write_am(root_d, "alpha", "project_x2.md", "The sky is blue and pretty clear today.")
    assert _run(root_d) == 0  # run 2: delta-eligible, scopes to alpha only
    assert _run(root_d, full_compile=True) == 0  # reconciliation: whole-corpus
    wiki_d = {p.name: p.read_bytes() for p in sorted((root_d / "wiki").glob("auto-*.md"))}

    assert wiki_w == wiki_d


# ---------------------------------------------------------------------------
# AC2 — a delta night makes ZERO detector calls for clusters untouched by
# changed_paths.
# ---------------------------------------------------------------------------


def test_delta_night_zero_calls_for_unaffected_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    root = _seed_root(tmp_path)
    _cache_dir(root, monkeypatch)
    assert _run(root) == 0  # baseline: establishes the auto-memory manifest stamp
    detect_spy.n_calls = 0
    detect_spy.clusters_seen.clear()

    # Only touch alpha.
    _write_am(root, "alpha", "project_x2.md", "The sky is blue and pretty clear today.")
    assert _run(root) == 0

    assert detect_spy.n_calls == 1, "exactly one cluster (alpha) should be detected"
    (only_cluster,) = detect_spy.clusters_seen
    assert all("alpha" in p for p in only_cluster), (
        f"beta must not be touched by the delta merge: {detect_spy.clusters_seen}"
    )


# ---------------------------------------------------------------------------
# AC3 — cadence forces whole-corpus when the stamp is stale; --full-compile /
# full_compile=True forces it manually too.
# ---------------------------------------------------------------------------


def test_stale_full_compile_stamp_forces_whole_corpus_and_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    root = _seed_root(tmp_path)
    cache = _cache_dir(root, monkeypatch)
    assert _run(root) == 0  # baseline

    stamp_path = cache / FULL_COMPILE_STAMP_NAME
    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    old_at = datetime.now(timezone.utc) - timedelta(days=10)  # > default 7-day cadence
    stamp["at"] = old_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp_path.write_text(json.dumps(stamp), encoding="utf-8")
    stale_mtime = stamp_path.stat().st_mtime_ns

    _write_am(root, "alpha", "project_x2.md", "The sky is blue and pretty clear today.")
    detect_spy.n_calls = 0
    detect_spy.clusters_seen.clear()
    assert _run(root) == 0

    # Whole-corpus: BOTH clusters detected (alpha AND beta), not just alpha.
    assert detect_spy.n_calls == 2
    touched_scopes = {
        "alpha" if any("alpha" in p for p in cl) else "beta"
        for cl in detect_spy.clusters_seen
    }
    assert touched_scopes == {"alpha", "beta"}

    # The stamp was reset (mtime + timestamp both advanced).
    new_stamp = _load_full_compile_stamp(stamp_path)
    assert new_stamp is not None
    assert stamp_path.stat().st_mtime_ns != stale_mtime
    assert new_stamp["at"] > stamp["at"]


def test_full_compile_true_forces_whole_corpus_even_when_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    root = _seed_root(tmp_path)
    _cache_dir(root, monkeypatch)
    assert _run(root) == 0  # baseline — fresh full-compile stamp just written

    _write_am(root, "alpha", "project_x2.md", "The sky is blue and pretty clear today.")
    detect_spy.n_calls = 0
    detect_spy.clusters_seen.clear()
    # A fresh stamp would normally make this run delta-eligible — force it.
    assert _run(root, full_compile=True) == 0

    assert detect_spy.n_calls == 2, "full_compile=True must force whole-corpus"
    touched_scopes = {
        "alpha" if any("alpha" in p for p in cl) else "beta"
        for cl in detect_spy.clusters_seen
    }
    assert touched_scopes == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# AC4 — deletion-only delta: a removed file is a changed path for its prior
# cluster.
# ---------------------------------------------------------------------------


def test_deletion_only_delta_recompiles_prior_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    root = _seed_root(tmp_path)
    _write_am(root, "alpha", "project_x2.md", "The sky is blue and pretty clear today.")
    _cache_dir(root, monkeypatch)
    assert _run(root) == 0  # baseline with 3 alpha members + 2 beta members

    detect_spy.n_calls = 0
    detect_spy.clusters_seen.clear()
    # Delete one alpha member (member removal) — no other file touched.
    (root / "raw" / "auto-memory" / "alpha" / "project_x2.md").unlink()
    assert _run(root) == 0

    assert detect_spy.n_calls == 1, "the alpha cluster must recompile on deletion"
    (only_cluster,) = detect_spy.clusters_seen
    assert all("alpha" in p for p in only_cluster)
    assert len(only_cluster) == 2, "the deleted member must be gone from the cluster"


# ---------------------------------------------------------------------------
# AC5 — no prior auto-memory manifest: first run falls back to whole-corpus
# and writes the baseline stamp.
# ---------------------------------------------------------------------------


def test_first_run_no_manifest_is_whole_corpus_and_writes_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    root = _seed_root(tmp_path)
    cache = _cache_dir(root, monkeypatch)
    manifest_path = cache / AUTO_MEMORY_MANIFEST_NAME
    stamp_path = cache / FULL_COMPILE_STAMP_NAME
    assert not manifest_path.exists()
    assert not stamp_path.exists()

    assert _run(root) == 0

    assert detect_spy.n_calls == 2, "no baseline -> whole-corpus (both clusters)"

    manifest = _load_auto_memory_manifest(manifest_path)
    assert manifest is not None
    assert set(manifest) == {
        "raw/auto-memory/alpha/project_x0.md",
        "raw/auto-memory/alpha/project_x1.md",
        "raw/auto-memory/beta/project_y0.md",
        "raw/auto-memory/beta/project_y1.md",
    }

    stamp = _load_full_compile_stamp(stamp_path)
    assert stamp is not None
    assert stamp["head"] is not None


# ---------------------------------------------------------------------------
# Config resolvers (issue athenaeum#463)
# ---------------------------------------------------------------------------


class TestResolveLiveDeltaEnabled:
    def test_default_true(self) -> None:
        from athenaeum.config import resolve_live_delta_enabled

        assert resolve_live_delta_enabled(None) is True
        assert resolve_live_delta_enabled({}) is True

    def test_explicit_false(self) -> None:
        from athenaeum.config import resolve_live_delta_enabled

        cfg = {"librarian": {"delta": {"live_client": False}}}
        assert resolve_live_delta_enabled(cfg) is False

    def test_non_bool_falls_through(self) -> None:
        from athenaeum.config import resolve_live_delta_enabled

        cfg = {"librarian": {"delta": {"live_client": "nope"}}}
        assert resolve_live_delta_enabled(cfg) is True


class TestResolveFullCompileEveryDays:
    def test_default_seven(self) -> None:
        from athenaeum.config import resolve_full_compile_every_days

        assert resolve_full_compile_every_days(None) == 7
        assert resolve_full_compile_every_days({}) == 7

    def test_explicit_override(self) -> None:
        from athenaeum.config import resolve_full_compile_every_days

        assert (
            resolve_full_compile_every_days({"librarian": {"full_compile_every_days": 3}})
            == 3
        )

    def test_bool_rejected(self) -> None:
        from athenaeum.config import resolve_full_compile_every_days

        cfg = {"librarian": {"full_compile_every_days": True}}
        assert resolve_full_compile_every_days(cfg) == 7

    def test_non_positive_rejected(self) -> None:
        from athenaeum.config import resolve_full_compile_every_days

        assert (
            resolve_full_compile_every_days({"librarian": {"full_compile_every_days": 0}})
            == 7
        )
        assert (
            resolve_full_compile_every_days(
                {"librarian": {"full_compile_every_days": -3}}
            )
            == 7
        )

    def test_wrong_key_location_ignored(self) -> None:
        # This key lives directly under `librarian`, NOT under `librarian.delta`.
        from athenaeum.config import resolve_full_compile_every_days

        cfg = {"librarian": {"delta": {"full_compile_every_days": 3}}}
        assert resolve_full_compile_every_days(cfg) == 7


# ---------------------------------------------------------------------------
# CLI --full-compile flag threading
# ---------------------------------------------------------------------------


def test_cli_full_compile_flag_threads_into_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import athenaeum.librarian as lib_mod
    from athenaeum import cli

    seen: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(lib_mod, "run", fake_run)
    rc = cli.main(["run", "--dry-run", "--full-compile"])
    assert rc == 0
    assert seen.get("full_compile") is True


def test_cli_full_compile_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    import athenaeum.librarian as lib_mod
    from athenaeum import cli

    seen: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(lib_mod, "run", fake_run)
    rc = cli.main(["run", "--dry-run"])
    assert rc == 0
    assert seen.get("full_compile") is False
