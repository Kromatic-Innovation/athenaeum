# SPDX-License-Identifier: Apache-2.0
"""p95 search-latency benchmark for Athenaeum backends.

Runs one benchmark per backend against a fixed synthetic wiki
(``conftest.bench_wiki``, 200 deterministic pages) and asserts p95 stays
within 20% of a recorded baseline.

The baselines in ``BASELINES_MS`` were measured on a local dev machine
(Darwin 25.4 / Apple Silicon, Python 3.14, pytest-benchmark 5.2). Numbers
will differ on CI or a different laptop — if they drift, update the
baseline in a dedicated "recalibrate" commit rather than silently
widening the 20% margin. See the PR body for the calibration method.

Run only these benchmarks:
    pytest tests/benchmarks/ --benchmark-only

They are NOT picked up by the default ``pytest`` invocation: pytest-benchmark
is an optional (dev/test) dep, and CI does not install it today. If the
package is missing the whole module skips cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_benchmark = pytest.importorskip("pytest_benchmark")

from athenaeum.search import FTS5Backend, KeywordBackend, VectorBackend  # noqa: E402

from .conftest import BENCH_QUERIES  # noqa: E402

# ---------------------------------------------------------------------------
# Baselines (milliseconds, p95 across BENCH_QUERIES on the synthetic wiki).
# Machine: Darwin 25.4, Apple Silicon, Python 3.14.
# Measured 2026-04-21 on the develop HEAD at issue #69 ingestion.
# ---------------------------------------------------------------------------
BASELINES_MS: dict[str, float] = {
    # Measured locally on Apple Silicon / Python 3.14 against the 200-page
    # synthetic wiki. The keyword number is high because it's
    # scan-on-query — no index — which is by design (it's the zero-setup
    # fallback documented in README known-limitations). FTS5 is the
    # recommended backend and lands ~250x faster.
    "keyword": 260.0,
    "fts5": 1.2,
    # Vector excluded by default — chromadb cold-start + model download
    # dominates the first build. Enable via ATHENAEUM_BENCH_VECTOR=1.
}

# 20% regression margin — the Session-2 recall budget from #67.
TOLERANCE = 1.20


def _p95_ms(benchmark: pytest_benchmark.fixture.BenchmarkFixture) -> float:
    """Return p95 of the benchmark's raw sample data, in milliseconds.

    pytest-benchmark stores seconds per iteration in ``benchmark.stats.stats.data``;
    we sort + index to avoid pulling in numpy for one percentile call.
    """
    data = sorted(benchmark.stats.stats.data)
    if not data:
        return 0.0
    idx = max(0, int(round(0.95 * (len(data) - 1))))
    return data[idx] * 1000.0


def _assert_within_budget(name: str, p95_ms: float) -> None:
    baseline = BASELINES_MS[name]
    budget = baseline * TOLERANCE
    assert p95_ms <= budget, (
        f"{name} p95={p95_ms:.2f}ms exceeded budget "
        f"(baseline={baseline:.2f}ms, tolerance={TOLERANCE:.2f}x, "
        f"budget={budget:.2f}ms)"
    )


@pytest.mark.benchmark(group="search")
def test_keyword_p95(benchmark, bench_wiki: Path, bench_cache: Path) -> None:
    """KeywordBackend scan-on-query p95 < 1.2x baseline."""
    backend = KeywordBackend()
    backend.build_index(bench_wiki, bench_cache)  # no-op, included for parity

    def run() -> None:
        for q in BENCH_QUERIES:
            backend.query(q, bench_cache, n=5, wiki_root=bench_wiki)

    benchmark(run)
    _assert_within_budget("keyword", _p95_ms(benchmark))


@pytest.mark.benchmark(group="search")
def test_fts5_p95(benchmark, bench_wiki: Path, bench_cache: Path) -> None:
    """FTS5Backend indexed-query p95 < 1.2x baseline."""
    backend = FTS5Backend()
    backend.build_index(bench_wiki, bench_cache)

    def run() -> None:
        for q in BENCH_QUERIES:
            backend.query(q, bench_cache, n=5)

    benchmark(run)
    _assert_within_budget("fts5", _p95_ms(benchmark))


@pytest.mark.benchmark(group="search")
def test_vector_p95(benchmark, bench_wiki: Path, bench_cache: Path) -> None:
    """VectorBackend semantic-query p95 — opt-in via env var.

    Skipped by default because the first build pulls the embedding model
    (~90MB) and a cold chromadb open dominates the run. Set
    ``ATHENAEUM_BENCH_VECTOR=1`` to include. No baseline is pinned —
    enabling the env var flips the test into "run and report" mode, and
    you must update ``BASELINES_MS["vector"]`` before turning the
    assertion on in a follow-up PR.
    """
    import os
    if not os.environ.get("ATHENAEUM_BENCH_VECTOR"):
        pytest.skip("set ATHENAEUM_BENCH_VECTOR=1 to run vector bench")

    pytest.importorskip("chromadb")
    backend = VectorBackend()
    backend.build_index(bench_wiki, bench_cache)

    def run() -> None:
        for q in BENCH_QUERIES:
            backend.query(q, bench_cache, n=5)

    benchmark(run)
    p95 = _p95_ms(benchmark)
    if "vector" in BASELINES_MS:
        _assert_within_budget("vector", p95)
    else:
        # Record-mode: print the measured p95 so the caller can pin it.
        print(f"\nvector p95={p95:.2f}ms (no baseline set — recording mode)")
