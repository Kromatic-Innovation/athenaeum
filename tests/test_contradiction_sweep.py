# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#909 (D6/AC5/AC6) — C4-specific "since last completed sweep"
scoping and the explicit ``--full-contradiction-sweep`` escape hatch.

A NEW, ADDITIVE gate, orthogonal to the athenaeum#370/#463 auto-memory delta gate
C4 otherwise piggybacks on (see ``test_live_delta_cadence.py``, which that
gate's own tests live in). Reuses that file's harness pattern (a minimal
knowledge git repo with two semantically-distinct auto-memory clusters,
``athenaeum.merge.detect_contradictions`` stubbed to a deterministic,
call-counting fake, and a stable hashing-trick fallback embedding so
clustering is reproducible without chromadb) rather than importing it, since
this repo's test files do not share fixtures across modules without a
``conftest.py`` — duplicated here deliberately, matching existing convention.

Compatibility contract under test throughout: a config/CLI surface that never
sets ``librarian.reasoning_triggers.*`` or passes ``--full-contradiction-sweep``
must behave EXACTLY as it did before athenaeum#909 (the C4-since stamp never
exists, so :func:`athenaeum.merge.merge_clusters_to_wiki`'s ``c4_since``
branch never engages).
"""

from __future__ import annotations

import hashlib
import math
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.librarian import (
    CONTRADICTION_SWEEP_STAMP_NAME,
    _load_timestamp_stamp,
    _write_timestamp_stamp,
    run,
)

pytestmark = pytest.mark.usefixtures("_fake_api_key", "_deterministic_fallback_embeddings")


def _stable_fallback_embeddings(files):
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
    monkeypatch.setattr(
        "athenaeum.clusters._fallback_embeddings", _stable_fallback_embeddings
    )
    monkeypatch.setattr(
        "athenaeum.delta._fallback_embeddings", _stable_fallback_embeddings
    )


@pytest.fixture
def _fake_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")


def _write_am(root: Path, scope: str, name: str, body: str) -> Path:
    d = root / "raw" / "auto-memory" / scope
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(
        f"---\nname: {name[:-3]}\ntype: auto-memory\n---\n{body}\n", encoding="utf-8"
    )
    return path


def _seed_root(tmp_path: Path) -> Path:
    """Two semantically-distinct clusters/scopes: alpha ("sky") / beta ("pgvector")."""
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
    def __init__(self) -> None:
        self.n_calls = 0
        self.clusters_seen: list[tuple[str, ...]] = []

    def __call__(self, members, client, *, config=None, usage=None, wiki_root=None):
        self.n_calls += 1
        self.clusters_seen.append(tuple(sorted(str(m.path) for m in members)))
        return ContradictionResult(detected=False, rationale="stub-no-conflict")


@pytest.fixture
def detect_spy(monkeypatch: pytest.MonkeyPatch) -> _DetectSpy:
    spy = _DetectSpy()
    monkeypatch.setattr("athenaeum.merge.detect_contradictions", spy)
    return spy


def _scopes_touched(spy: _DetectSpy) -> set[str]:
    return {
        "alpha" if any("alpha" in p for p in cl) else "beta"
        for cl in spy.clusters_seen
    }


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


def _set_mtime(path: Path, at: datetime) -> None:
    ts = at.timestamp()
    import os

    os.utime(path, (ts, ts))


def _disable_delta_yaml(root: Path) -> None:
    """Force ``only_cluster_ids is None`` on every future run WITHOUT tripping
    ``full_compile_due`` — the "delta ineligible for a reason OTHER than the
    periodic reconciliation cadence" scenario the athenaeum#909 since-scope targets.
    """
    (root / "athenaeum.yaml").write_text(
        "recall:\n"
        "  extra_intake_roots:\n"
        "    - raw/auto-memory\n"
        "librarian:\n"
        "  delta:\n"
        "    live_client: false\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# AC6 — --full-contradiction-sweep / full_contradiction_sweep=True forces C4
# over every cluster and advances the C4 stamp.
# ---------------------------------------------------------------------------


def test_full_contradiction_sweep_forces_whole_corpus_and_writes_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    root = _seed_root(tmp_path)
    cache = _cache_dir(root, monkeypatch)
    stamp_path = cache / CONTRADICTION_SWEEP_STAMP_NAME
    assert not stamp_path.exists()

    assert _run(root, full_contradiction_sweep=True) == 0

    assert detect_spy.n_calls == 2, "both clusters must be examined"
    assert _scopes_touched(detect_spy) == {"alpha", "beta"}

    stamp = _load_timestamp_stamp(stamp_path)
    assert stamp is not None


# ---------------------------------------------------------------------------
# AC5 — once a C4 stamp exists, an otherwise-whole-corpus C4 pass (delta
# ineligible, but NOT full_compile_due) scopes to clusters touched since it.
# ---------------------------------------------------------------------------


def test_since_scope_narrows_an_otherwise_whole_corpus_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    root = _seed_root(tmp_path)
    cache = _cache_dir(root, monkeypatch)

    # Run 1: explicit full sweep establishes the C4 stamp baseline (the exact
    # timestamp value doesn't matter yet — pinned deterministically next).
    assert _run(root, full_contradiction_sweep=True) == 0
    stamp_path = cache / CONTRADICTION_SWEEP_STAMP_NAME
    assert _load_timestamp_stamp(stamp_path) is not None

    # Pin the stamp and every existing member's mtime to a controlled,
    # widely-separated timeline instead of relying on real elapsed wall-clock
    # time — the stamp truncates to whole SECONDS
    # (``_write_timestamp_stamp``), so a fast-running test could otherwise
    # land the stamp and a freshly-written file's mtime in the same second
    # and make the since-scope's ">=" comparison ambiguous.
    mark = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    _write_timestamp_stamp(stamp_path, mark)
    for scope, names in (
        ("alpha", ("project_x0.md", "project_x1.md")),
        ("beta", ("project_y0.md", "project_y1.md")),
    ):
        for name in names:
            _set_mtime(
                root / "raw" / "auto-memory" / scope / name,
                mark - timedelta(hours=1),
            )

    # Disable the live-client delta gate so future runs would otherwise be
    # WHOLE-CORPUS (only_cluster_ids is None) for a reason other than the
    # periodic full-compile cadence.
    _disable_delta_yaml(root)
    detect_spy.n_calls = 0
    detect_spy.clusters_seen.clear()

    # Only alpha changes, well AFTER the pinned stamp.
    new_file = _write_am(
        root, "alpha", "project_x2.md", "The sky is blue and pretty clear today."
    )
    _set_mtime(new_file, mark + timedelta(hours=1))
    assert _run(root) == 0

    # The since-scope narrowed C4 to alpha only — beta was untouched since
    # the stamp and must not be re-examined.
    assert detect_spy.n_calls == 1
    assert _scopes_touched(detect_spy) == {"alpha"}


# ---------------------------------------------------------------------------
# AC6 (negative) — full_compile_due ALWAYS disarms the since-scope: a real
# periodic whole-corpus reconciliation must never be silently narrowed.
# ---------------------------------------------------------------------------


def test_full_compile_due_bypasses_since_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    root = _seed_root(tmp_path)
    _cache_dir(root, monkeypatch)

    assert _run(root, full_contradiction_sweep=True) == 0  # establish C4 stamp
    _disable_delta_yaml(root)
    detect_spy.n_calls = 0
    detect_spy.clusters_seen.clear()

    _write_am(root, "alpha", "project_x2.md", "The sky is blue and pretty clear today.")
    # full_compile=True forces full_compile_due -> the since-scope must be
    # disarmed even though a fresher-than-everything C4 stamp exists.
    assert _run(root, full_compile=True) == 0

    assert detect_spy.n_calls == 2
    assert _scopes_touched(detect_spy) == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# D6 compatibility constraint — absent any C4 stamp (never explicitly swept),
# behavior is byte-identical to pre-athenaeum#909: a delta-ineligible run still
# examines the WHOLE corpus, exactly like it always did.
# ---------------------------------------------------------------------------


def test_no_stamp_yet_whole_corpus_behavior_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    root = _seed_root(tmp_path)
    cache = _cache_dir(root, monkeypatch)
    _disable_delta_yaml(root)
    assert not (cache / CONTRADICTION_SWEEP_STAMP_NAME).exists()

    assert _run(root) == 0

    assert detect_spy.n_calls == 2, "no C4 stamp yet -> whole-corpus, as before athenaeum#909"
    assert _scopes_touched(detect_spy) == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# The C4 stamp only advances when a pass actually examined the WHOLE corpus
# — a normal delta-scoped run (general athenaeum#370/#463 gate, unrelated to the
# since-scope) must not silently advance it.
# ---------------------------------------------------------------------------


def test_c4_stamp_does_not_advance_on_a_delta_scoped_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detect_spy: _DetectSpy
) -> None:
    root = _seed_root(tmp_path)
    cache = _cache_dir(root, monkeypatch)
    stamp_path = cache / CONTRADICTION_SWEEP_STAMP_NAME

    assert _run(root, full_contradiction_sweep=True) == 0  # establish baseline
    baseline = _load_timestamp_stamp(stamp_path)
    assert baseline is not None

    # A normal delta-eligible run (live-client delta stays ON here) that only
    # touches alpha — the general athenaeum#370/#463 gate scopes this, not the
    # athenaeum#909 since-scope.
    _write_am(root, "alpha", "project_x2.md", "The sky is blue and pretty clear today.")
    assert _run(root) == 0

    after = _load_timestamp_stamp(stamp_path)
    assert after == baseline, "a delta-scoped (not whole-corpus) pass must not advance it"


# ---------------------------------------------------------------------------
# CLI wiring — --full-contradiction-sweep parses to full_contradiction_sweep.
# ---------------------------------------------------------------------------


def test_cli_flag_parses_to_full_contradiction_sweep() -> None:
    from athenaeum.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["run", "--full-contradiction-sweep"])
    assert args.full_contradiction_sweep is True

    args_default = parser.parse_args(["run"])
    assert getattr(args_default, "full_contradiction_sweep", False) is False
