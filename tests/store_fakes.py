# SPDX-License-Identifier: Apache-2.0
"""Reusable in-memory :class:`~athenaeum.store.Store` fake (issue athenaeum#976).

Deliberately not buried inside one test module: per
``docs/whole-store-adapter-design.md`` §9.2, S2 (athenaeum#977) needs a
latency-injecting variant of this fake for its op-count benchmark (P5), and S7
(athenaeum#982) runs ``quarantine.py``'s existing test suite against it
alongside :class:`~athenaeum.store.FilesystemStore`. Import it as::

    from tests.store_fakes import InMemoryStore

This module is a test helper, not itself a test module (no ``test_`` prefix,
so pytest does not collect it directly); ``tests/test_store_conformance.py``
is what exercises it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass

from athenaeum.models import parse_frontmatter
from athenaeum.store import (
    OPERATIONAL_SCOPES,
    PERSISTENCE_CLASSES,
    Lease,
    LeaseHeldError,
    ObjectMeta,
    Record,
    Store,
    StoreCapabilities,
    StoreConflictError,
    StoreKey,
)


@dataclass
class _Object:
    data: bytes
    version: str


class InMemoryStore:
    """A ``dict``-backed :class:`~athenaeum.store.Store` fake — no filesystem at all.

    Declares the same capability profile as
    :class:`~athenaeum.store.FilesystemStore` for everything but ``versioned``
    (``False`` — no git tree to snapshot) so the conformance suite exercises
    identical behavior on both implementations. ``local_path_for`` is
    ``None`` (design note §6.2 D2: non-``None`` only on the filesystem
    adapter). Version tokens are a content hash rather than
    :class:`~athenaeum.store.FilesystemStore`'s ``mtime_ns:size`` — both are
    valid per D3 ("opaque; equality only"), and using a different scheme here
    is a deliberate cross-check that no test or caller accidentally parses a
    version token instead of comparing it.

    ``lease`` (issue athenaeum#979, S4) is real here too, but — deliberately —
    NOT implemented the same way as :class:`~athenaeum.store.FilesystemStore`'s
    ``flock``-backed one. There is no kernel to release a mutex when a holder
    dies, so ``ttl_seconds`` does genuine work: a lease name maps to
    ``(token, expiry)`` in :attr:`_leases`, and it is available again once
    ``time.monotonic()`` passes ``expiry`` — the design note's other-adapter
    half of "mapping onto flock for the filesystem adapter and onto a lease
    row / conditional put elsewhere" (§4.6). This is the non-filesystem lease
    path ``tests/test_store_conformance.py``'s expiry test exercises.
    """

    def __init__(self) -> None:
        self._objects: dict[StoreKey, _Object] = {}
        self._leases: dict[str, tuple[str, float]] = {}
        self.capabilities = StoreCapabilities(
            classes=PERSISTENCE_CLASSES,
            operational_scopes=OPERATIONAL_SCOPES,
            versioned=False,
            purgeable=True,
            compare_and_swap=True,
            leases=True,
            append=True,
            bulk_list=True,
            bulk_read=True,
            cheap_local_scan=True,
            local_path_for=None,
        )

    @staticmethod
    def _version_for(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # -- reads ------------------------------------------------------------

    def read(self, key: StoreKey) -> bytes:
        return self._objects[key].data

    def read_many(self, keys: Sequence[StoreKey]) -> Mapping[StoreKey, bytes]:
        out: dict[StoreKey, bytes] = {}
        for key in keys:
            obj = self._objects.get(key)
            if obj is not None:
                out[key] = obj.data
        return out

    def iter_meta(self, surface: str, prefix: str = "") -> Iterator[ObjectMeta]:
        for key, obj in sorted(self._objects.items(), key=lambda kv: (kv[0].surface, kv[0].key)):
            if key.surface != surface or not key.key.startswith(prefix):
                continue
            yield ObjectMeta(key=key, version=obj.version, size=len(obj.data))

    def iter_records(self, surface: str, prefix: str = "") -> Iterator[Record]:
        for meta in self.iter_meta(surface, prefix):
            frontmatter, body = parse_frontmatter(self.read(meta.key).decode("utf-8"))
            yield Record(meta=meta, frontmatter=frontmatter, body=body)

    # -- writes -------------------------------------------------------------

    def put(self, key: StoreKey, data: bytes, *, expect: str | None = None) -> str:
        current = self._objects.get(key)
        current_version = current.version if current is not None else None
        if expect is None:
            if current is not None:
                raise StoreConflictError(
                    f"put({key.surface}:{key.key}, expect=None) refused: object "
                    "already exists (expect=None is exclusive create)"
                )
        elif current_version != expect:
            raise StoreConflictError(
                f"put({key.surface}:{key.key}, expect={expect!r}) refused: "
                f"current version is {current_version!r}"
            )
        version = self._version_for(data)
        self._objects[key] = _Object(data=data, version=version)
        return version

    def append(self, key: StoreKey, line: bytes) -> None:
        current = self._objects.get(key)
        data = (current.data if current is not None else b"") + line
        self._objects[key] = _Object(data=data, version=self._version_for(data))

    def delete(self, key: StoreKey, *, expect: str | None = None) -> bool:
        current = self._objects.get(key)
        if current is None:
            if expect is not None:
                raise StoreConflictError(
                    f"delete({key.surface}:{key.key}, expect={expect!r}) refused: "
                    "object does not exist"
                )
            return False
        if expect is not None and current.version != expect:
            raise StoreConflictError(
                f"delete({key.surface}:{key.key}, expect={expect!r}) refused: "
                f"current version is {current.version!r}"
            )
        del self._objects[key]
        return True

    def move(self, src: StoreKey, dst: StoreKey) -> None:
        if src not in self._objects:
            raise FileNotFoundError(f"move source {src.surface}:{src.key} does not exist")
        if dst in self._objects:
            raise StoreConflictError(f"move destination {dst.surface}:{dst.key} already exists")
        self._objects[dst] = self._objects.pop(src)

    # -- recoverability / concurrency / lifecycle ------------------------

    def snapshot(self, label: str) -> str | None:
        return None

    def lease(self, name: str, ttl_seconds: float) -> AbstractContextManager[Lease]:
        return _InMemoryLease(self, name, ttl_seconds)

    def bootstrap(self) -> None:
        """No directory concept to create; matches
        :meth:`~athenaeum.store.FilesystemStore.bootstrap`'s non-destructive
        ``mkdir(exist_ok=True)`` by doing nothing to existing state."""


class _InMemoryLease:
    """The ``AbstractContextManager[Lease]`` :meth:`InMemoryStore.lease` returns.

    Genuinely TTL-driven (unlike :class:`athenaeum.store.FileLease`, which
    ignores ``ttl_seconds`` because the kernel already reclaims a dead
    holder's ``flock`` — see that class's docstring): a name is unavailable
    only until ``time.monotonic()`` passes the expiry this instance set on
    acquire, so a holder that stops calling :meth:`heartbeat` (or crashes,
    with nothing to release it) is naturally reclaimable once its TTL lapses
    — the "lease row" half of design note §4.6, as opposed to the "flock"
    half.
    """

    def __init__(self, store: InMemoryStore, name: str, ttl_seconds: float) -> None:
        self._store = store
        self._name = name
        self._ttl = ttl_seconds
        self._token: str | None = None

    def __enter__(self) -> Lease:
        now = time.monotonic()
        held = self._store._leases.get(self._name)
        if held is not None and held[1] > now:
            raise LeaseHeldError(
                self._name, {"expires_in_seconds": f"{held[1] - now:.3f}"}
            )
        token = f"{now:.9f}"
        self._store._leases[self._name] = (token, now + self._ttl)
        self._token = token
        return Lease(name=self._name, token=token)

    def heartbeat(self) -> None:
        """Renew: push this lease's expiry another ``ttl_seconds`` out. No-op
        if not currently held (mirrors ``FileLease.heartbeat``)."""
        if self._token is None:
            return
        current = self._store._leases.get(self._name)
        if current is not None and current[0] == self._token:
            self._store._leases[self._name] = (self._token, time.monotonic() + self._ttl)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._token is None:
            return
        current = self._store._leases.get(self._name)
        if current is not None and current[0] == self._token:
            del self._store._leases[self._name]
        self._token = None


class NoRecoveryStore(InMemoryStore):
    """An :class:`InMemoryStore` declaring NEITHER ``versioned`` nor
    ``purgeable`` (design note §4.4 R1; issue athenaeum#978, slice S3).

    The fake R1's honest-refusal rule is written against: "a destructive
    store operation MUST either (i) run against a surface whose adapter
    declares ``versioned`` ... or (ii) refuse." A store with neither flag
    set is the starkest case — there is no declared alternative at all — so
    injecting this into a Tier-A/Tier-B call site's ``store=`` parameter
    proves the call refuses rather than silently degrading to an
    unrecoverable delete, without needing a real (non-)git-repo fixture.
    Everything else is inherited unchanged from :class:`InMemoryStore`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.capabilities = dataclasses.replace(
            self.capabilities, versioned=False, purgeable=False
        )


class NoCheapScanStore(InMemoryStore):
    """An :class:`InMemoryStore` declaring ``cheap_local_scan=False`` (design
    note §7 honest-refusal rule; issue athenaeum#981, slice S6).

    Stands in for a non-filesystem adapter (remote or encrypted) where a
    full-corpus scan-on-query is a genuine per-query round-trip rather than a
    free local re-read. Injecting this into :meth:`~athenaeum.search.KeywordBackend.query`'s
    ``store=`` parameter proves the backend refuses rather than silently
    paying that cost, without needing a real non-filesystem adapter.
    Everything else is inherited unchanged from :class:`InMemoryStore`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.capabilities = dataclasses.replace(
            self.capabilities, cheap_local_scan=False
        )
class LatencyInjectingStore:
    """Wraps any :class:`~athenaeum.store.Store` implementation, counting
    ``iter_meta``/``read_many`` CALLS and optionally sleeping on each one.

    This is the fake design note §3.5 P5 asks for: "The guard is an
    operation-count assertion against a latency-injecting fake adapter."
    Counting calls (not objects/entries) is what makes the S2 (athenaeum#977) op-count
    guard meaningful — a per-page ``stat()``/``read()`` walk costs nothing
    extra on a local filesystem test, so a naive test could pass even with an
    O(N) round-trip design. Injecting a real, non-zero ``latency_seconds`` per
    call and asserting wall-clock time proves the bound holds even when each
    call has a genuine cost: ``iter_meta_calls`` stays 1 and
    ``read_many_calls`` stays at most 1 (batching every changed key)
    regardless of corpus size ``N`` — see
    ``tests/benchmarks/test_index_build_opcount.py``.

    Every other :class:`~athenaeum.store.Store` method delegates unchanged to
    *inner* — this fake only instruments the two bulk primitives the design
    note's P1/P3 constraints are about.
    """

    def __init__(self, inner: Store, *, latency_seconds: float = 0.0) -> None:
        self._inner = inner
        self.latency_seconds = latency_seconds
        self.iter_meta_calls = 0
        self.read_many_calls = 0
        self.capabilities = inner.capabilities

    def _sleep(self) -> None:
        if self.latency_seconds:
            time.sleep(self.latency_seconds)

    # -- instrumented bulk primitives (design note P1/P3) ------------------

    def iter_meta(self, surface: str, prefix: str = "") -> Iterator[ObjectMeta]:
        self.iter_meta_calls += 1
        self._sleep()
        yield from self._inner.iter_meta(surface, prefix)

    def read_many(self, keys: Sequence[StoreKey]) -> Mapping[StoreKey, bytes]:
        self.read_many_calls += 1
        self._sleep()
        return self._inner.read_many(keys)

    # -- everything else: unmodified passthrough ---------------------------

    def read(self, key: StoreKey) -> bytes:
        return self._inner.read(key)

    def iter_records(self, surface: str, prefix: str = "") -> Iterator[Record]:
        return self._inner.iter_records(surface, prefix)

    def put(self, key: StoreKey, data: bytes, *, expect: str | None = None) -> str:
        return self._inner.put(key, data, expect=expect)

    def append(self, key: StoreKey, line: bytes) -> None:
        self._inner.append(key, line)

    def delete(self, key: StoreKey, *, expect: str | None = None) -> bool:
        return self._inner.delete(key, expect=expect)

    def move(self, src: StoreKey, dst: StoreKey) -> None:
        self._inner.move(src, dst)

    def snapshot(self, label: str) -> str | None:
        return self._inner.snapshot(label)

    def lease(self, name: str, ttl_seconds: float) -> AbstractContextManager[Lease]:
        return self._inner.lease(name, ttl_seconds)

    def bootstrap(self) -> None:
        self._inner.bootstrap()
