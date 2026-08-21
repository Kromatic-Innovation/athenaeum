# SPDX-License-Identifier: Apache-2.0
"""Proof that ``athenaeum.store_conformance`` (issue athenaeum#983, S8) is usable
exactly as an external adapter author would use it: import the public harness
and the public ``athenaeum.store`` protocol types, write your own ``Store``
implementation, subclass ``StoreConformanceTests``, override the ``store``
fixture, and let pytest collect and run every shared test against it —
without touching any athenaeum source or test file.

``_MinimalDictStore`` below is deliberately independent of
``tests/store_fakes.py``'s ``InMemoryStore`` (athenaeum's own internal test
double, used to exercise its two shipped S1 implementations in
``tests/test_store_conformance.py``). This is a THIRD, ad-hoc, intentionally
bare-bones implementation that exists only to demonstrate the harness runs
against something it has never seen before — the same shape an external
author's own store class would take.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

import pytest

from athenaeum.models import parse_frontmatter
from athenaeum.store import (
    Lease,
    LeaseHeldError,
    ObjectMeta,
    Record,
    Store,
    StoreCapabilities,
    StoreConflictError,
    StoreKey,
)
from athenaeum.store_conformance import StoreConformanceTests


class _MinimalDictStore:
    """The smallest ``Store`` that can pass the conformance suite: a plain
    in-process ``dict``, no persistence, no real concurrency primitive
    (Python-level locking stands in for a kernel/database lock). Written
    from scratch against nothing but the public ``athenaeum.store`` protocol
    types — no shared implementation with ``FilesystemStore`` or
    ``tests.store_fakes.InMemoryStore``."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._held_leases: set[str] = set()
        self.capabilities = StoreCapabilities(
            classes=frozenset({"source", "derived", "operational", "config"}),
            operational_scopes=frozenset({"store-durable", "machine-local"}),
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

    def _version(self, k: tuple[str, str]) -> str | None:
        n = self._versions.get(k)
        return None if n is None else str(n)

    # -- reads ------------------------------------------------------------

    def read(self, key: StoreKey) -> bytes:
        try:
            return self._objects[(key.surface, key.key)]
        except KeyError:
            raise FileNotFoundError(key.key) from None

    def read_many(self, keys: Sequence[StoreKey]) -> Mapping[StoreKey, bytes]:
        out: dict[StoreKey, bytes] = {}
        for key in keys:
            k = (key.surface, key.key)
            if k in self._objects:
                out[key] = self._objects[k]
        return out

    def iter_meta(self, surface: str, prefix: str = "") -> Iterator[ObjectMeta]:
        for (s, key), data in sorted(self._objects.items()):
            if s == surface and key.startswith(prefix):
                yield ObjectMeta(
                    key=StoreKey(surface=s, key=key),
                    version=self._version((s, key)) or "",
                    size=len(data),
                )

    def iter_records(self, surface: str, prefix: str = "") -> Iterator[Record]:
        for meta in self.iter_meta(surface, prefix):
            frontmatter, body = parse_frontmatter(self.read(meta.key).decode("utf-8"))
            yield Record(meta=meta, frontmatter=frontmatter, body=body)

    # -- writes -------------------------------------------------------------

    def put(self, key: StoreKey, data: bytes, *, expect: str | None = None) -> str:
        k = (key.surface, key.key)
        current = self._version(k)
        if expect is None:
            if current is not None:
                raise StoreConflictError(f"put({key.key}) refused: already exists")
        elif current != expect:
            raise StoreConflictError(f"put({key.key}) refused: current version is {current!r}")
        self._objects[k] = data
        self._versions[k] = self._versions.get(k, 0) + 1
        version = self._version(k)
        assert version is not None  # just wrote it
        return version

    def append(self, key: StoreKey, line: bytes) -> None:
        k = (key.surface, key.key)
        self._objects[k] = self._objects.get(k, b"") + line
        self._versions[k] = self._versions.get(k, 0) + 1

    def delete(self, key: StoreKey, *, expect: str | None = None) -> bool:
        k = (key.surface, key.key)
        current = self._version(k)
        if current is None:
            if expect is not None:
                raise StoreConflictError(f"delete({key.key}) refused: does not exist")
            return False
        if expect is not None and current != expect:
            raise StoreConflictError(f"delete({key.key}) refused: current version is {current!r}")
        del self._objects[k]
        del self._versions[k]
        return True

    def move(self, src: StoreKey, dst: StoreKey) -> None:
        sk, dk = (src.surface, src.key), (dst.surface, dst.key)
        if sk not in self._objects:
            raise FileNotFoundError(f"move source {src.key} does not exist")
        if dk in self._objects:
            raise StoreConflictError(f"move destination {dst.key} already exists")
        self._objects[dk] = self._objects.pop(sk)
        self._versions[dk] = self._versions.pop(sk)

    # -- recoverability / concurrency / lifecycle ------------------------

    def snapshot(self, label: str) -> str | None:
        return None

    @contextmanager
    def lease(self, name: str, ttl_seconds: float) -> Iterator[Lease]:
        if name in self._held_leases:
            raise LeaseHeldError(name)
        self._held_leases.add(name)
        try:
            yield Lease(name=name, token=str(time.monotonic()))
        finally:
            self._held_leases.discard(name)

    def bootstrap(self) -> None:
        pass


class TestMinimalDictStoreConformance(StoreConformanceTests):
    """Drives the full portable conformance suite
    (``athenaeum.store_conformance.StoreConformanceTests``) against
    ``_MinimalDictStore`` — proof the harness runs against an implementation
    it has never seen, exactly as an external adapter author's own store
    class would be exercised via ``pytest their_test_file.py``."""

    @pytest.fixture
    def store(self) -> Store:
        return _MinimalDictStore()
