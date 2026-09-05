# SPDX-License-Identifier: Apache-2.0
"""Op-count assertion for the S2 index-build scan (issue athenaeum#977).

Design note (``docs/extending/whole-store-adapter-design.md``) §3.5, constraint P5:

    "Op-count budgets are tested, wall-clock is not. Adapter-latency
    regressions are invisible to a wall-clock benchmark run on a local disk.
    The guard is an operation-count assertion against a latency-injecting
    fake adapter, in ``tests/benchmarks/`` alongside the existing p95
    harness."

This proves the round-trip bound ``search._scan_surface`` gives the
FTS5/vector index build (the migration §9.2's S2 slice performs on
``search._scan_indexed_records``): ONE ``store.iter_meta`` call — never a
per-page ``stat()`` — plus, when anything changed, exactly ONE batched
``store.read_many`` call sized to ``c`` (the changed/added count) — never a
per-page ``read()``, and never proportional to the corpus size ``N``.

Deliberately does NOT depend on ``pytest-benchmark``: this is a call-COUNT
(and, in one test, a wall-clock-under-injected-latency) assertion, not a
timing benchmark, so it needs no benchmark fixture and runs under plain
``pytest``. It lives in ``tests/benchmarks/`` purely to sit alongside the
existing p95 harness the design note points at (see
``test_search_bench.py``) — ``tests/benchmarks/`` is excluded from the
default collection (see ``pyproject.toml``'s ``addopts``), so run it
explicitly: ``pytest tests/benchmarks/test_index_build_opcount.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from athenaeum.search import _scan_surface
from athenaeum.store import StoreKey
from tests.store_fakes import InMemoryStore, LatencyInjectingStore

_SURFACE = "wiki"
_KEEP_ALL: Callable[[str], bool] = lambda _rel: True  # noqa: E731


def _seed(store: InMemoryStore, n: int) -> None:
    for i in range(n):
        store.put(StoreKey(surface=_SURFACE, key=f"page_{i:05d}.md"), f"body {i}".encode())


def _versions_of(store: InMemoryStore) -> dict[str, str]:
    return {meta.key.key: meta.version for meta in store.iter_meta(_SURFACE)}


@pytest.mark.parametrize("n", [1, 50, 2000])
def test_noop_rebuild_is_one_listing_call_and_zero_reads(n: int) -> None:
    """Nothing changed: exactly one ``iter_meta`` call, and ``read_many`` is
    never called at all — independent of corpus size ``N``."""
    inner = InMemoryStore()
    _seed(inner, n)
    prior_versions = _versions_of(inner)

    store = LatencyInjectingStore(inner)
    current, contents = _scan_surface(
        store, _SURFACE, keep=_KEEP_ALL, prior_versions=prior_versions
    )

    assert store.iter_meta_calls == 1
    assert store.read_many_calls == 0
    assert len(current) == n
    assert contents == {}


@pytest.mark.parametrize("n,c", [(10, 3), (500, 1), (2000, 50)])
def test_incremental_rebuild_is_one_listing_plus_one_batched_read(n: int, c: int) -> None:
    """``c`` changed/added objects out of ``N``: exactly one ``iter_meta``
    call and exactly one ``read_many`` call batching all ``c`` keys — never
    ``N`` calls of either, and never ``c`` individual ``read_many`` calls.
    This is design note P1 ("bulk listing is mandatory") and P3 ("bulk read
    is mandatory") made mechanical."""
    inner = InMemoryStore()
    _seed(inner, n)
    prior_versions = _versions_of(inner)
    # "Change" c of them: drop their prior version so _scan_surface treats
    # them as changed/added (absent from prior_versions never matches).
    for name in list(prior_versions)[:c]:
        del prior_versions[name]

    store = LatencyInjectingStore(inner)
    current, contents = _scan_surface(
        store, _SURFACE, keep=_KEEP_ALL, prior_versions=prior_versions
    )

    assert store.iter_meta_calls == 1
    assert store.read_many_calls == 1
    assert len(current) == n
    assert len(contents) == c


def test_filtered_objects_are_never_read() -> None:
    """``keep`` runs INSIDE the listing loop, before a key can enter the read
    set — a filtered-out object (wrong extension, an excluded name) never
    reaches ``read_many``, so filtering never costs a wasted round trip."""
    inner = InMemoryStore()
    _seed(inner, 20)
    # Also seed 5 objects that `keep` will reject.
    for i in range(5):
        inner.put(StoreKey(surface=_SURFACE, key=f"_skip_{i}.md"), b"skip me")

    store = LatencyInjectingStore(inner)
    current, contents = _scan_surface(
        store,
        _SURFACE,
        keep=lambda rel: not rel.startswith("_"),
        prior_versions={},  # everything kept is "changed" (nothing prior)
    )

    assert store.iter_meta_calls == 1
    assert store.read_many_calls == 1
    assert len(current) == 20  # the 5 filtered objects never entered `current`
    assert len(contents) == 20  # ...or the read set


def test_wall_clock_bounded_by_call_count_not_corpus_size() -> None:
    """Inject REAL per-call latency: wall time is bounded by the NUMBER of
    calls (here: 1 ``iter_meta`` + 1 ``read_many`` = 2), not by ``N`` — the
    whole point of P5's latency-injecting fake. A per-page walk would cost
    ``N * latency``; this scan costs a small constant regardless of ``N``.
    """
    import time

    inner = InMemoryStore()
    _seed(inner, 500)
    store = LatencyInjectingStore(inner, latency_seconds=0.05)

    start = time.monotonic()
    _scan_surface(store, _SURFACE, keep=_KEEP_ALL, prior_versions={})
    elapsed = time.monotonic() - start

    # 2 calls * 0.05s, with generous slack for scheduling jitter — nowhere
    # near 500 * 0.05s = 25s, which a per-page round trip would cost.
    assert elapsed < 2.0
    assert store.iter_meta_calls == 1
    assert store.read_many_calls == 1
