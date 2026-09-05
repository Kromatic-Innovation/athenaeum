# SPDX-License-Identifier: Apache-2.0
"""The public :class:`~athenaeum.store.Store` conformance harness (design note
``docs/extending/whole-store-adapter-design.md`` §6 preamble: "the same staging the
existing seam used"; suite authored in issue athenaeum#976, S1; published here as a
runnable third-party harness by issue athenaeum#983, S8).

A third-party adapter author who has written their own ``Store`` implementation
subclasses :class:`StoreConformanceTests`, overrides the ``store`` fixture to
construct their own implementation, and runs pytest against their own test file
— no athenaeum source or test file needs editing::

    import pytest
    from athenaeum.store import Store
    from athenaeum.store_conformance import StoreConformanceTests

    class TestMyAdapterConformance(StoreConformanceTests):
        @pytest.fixture
        def store(self) -> Store:
            return MyStore(...)  # your Store implementation

``pytest test_my_adapter.py -v`` then runs every test below against ``MyStore``
and reports a pass/fail conformance report, exactly as it does for athenaeum's
own two shipped implementations (:class:`~athenaeum.store.FilesystemStore` and
``tests.store_fakes.InMemoryStore``, both S1) — see
``tests/test_store_conformance.py::TestStoreConformance``, which is itself now
just a subclass of this class, parametrized over both. See
``docs/extending/store-contract.md`` for the prose contract this suite enforces, and
``tests/test_store_conformance_harness.py`` for a worked, from-scratch,
third-party-style example: a minimal dict-backed ``Store`` built using only
this module and :mod:`athenaeum.store`'s public names, exercised the same way
an external author's own implementation would be.

**Scope.** This covers the shared seam-level contract every full ``Store``
implementation must honor. Two things are deliberately NOT part of this
portable suite, and stay athenaeum-internal in ``tests/test_store_conformance.py``
instead:

* Fixture-specific capability VALUES. This suite exercises ``put``/``append``/
  ``delete``/``move``/``lease`` directly, so a conformant implementation under
  test must declare every operational capability ``True`` (``append``,
  ``compare_and_swap``, ``leases``, ``bulk_list``, ``bulk_read``,
  ``cheap_local_scan``, ``purgeable``) — the suite would fail against any of
  those regardless. ``versioned`` is the one flag genuinely left to the
  adapter (git-backed vs not), so only its *type* is checked here; asserting
  it is specifically ``False`` is athenaeum's own non-git S1 fixtures talking
  about themselves, not a portable requirement — see
  :meth:`StoreConformanceTests.test_capabilities_are_declared`. Likewise
  ``local_path_for`` is checked here only for internal consistency (``None``
  or a callable that resolves to a real ``Path``); pinning it to
  ``FilesystemStore`` specifically is athenaeum-internal.
* ``athenaeum.storage.resolve_store_for_class`` (routes ATHENAEUM's own
  entity-class config onto a surface — not a property of an arbitrary
  ``Store``) and ``InMemoryStore``'s genuinely-TTL-expiring lease semantics
  (a filesystem-backed adapter's lease has no independent TTL — see
  :class:`athenaeum.store.FileLease`'s docstring). Both stay in
  ``tests/test_store_conformance.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

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


class StoreConformanceTests:
    """Subclass this and override the ``store`` fixture with your own
    :class:`~athenaeum.store.Store` implementation.

    ``surface_name`` is the single surface every test below reads and writes
    under. It carries no meaning beyond "a valid surface string" — the
    contract does not require any particular surface to pre-exist (design
    note §6.2 D2/D4) — override the class attribute only if your fixture
    reserves ``"wiki"`` for something else.
    """

    surface_name: str = "wiki"

    @pytest.fixture
    def store(self) -> Iterator[Store] | Store:
        """Override in a subclass: construct and return (or yield) one fresh
        ``Store`` instance per test."""
        raise NotImplementedError(
            "StoreConformanceTests subclasses must override the `store` fixture "
            "with a factory for the Store implementation under test — see this "
            "module's docstring for the shape."
        )

    def _key(self, name: str, *, surface: str | None = None) -> StoreKey:
        return StoreKey(surface=surface or self.surface_name, key=name)

    # -- put / read -----------------------------------------------------

    def test_put_then_read_roundtrips(self, store: Store) -> None:
        key = self._key("alice.md")
        version = store.put(key, b"---\nname: alice\n---\n\nBody text.\n")
        assert isinstance(version, str) and version
        assert store.read(key) == b"---\nname: alice\n---\n\nBody text.\n"

    def test_put_expect_none_is_exclusive_create(self, store: Store) -> None:
        key = self._key("alice.md")
        store.put(key, b"first")
        with pytest.raises(StoreConflictError):
            store.put(key, b"second", expect=None)
        # The refused write must not have clobbered the original.
        assert store.read(key) == b"first"

    def test_put_expect_matching_version_succeeds(self, store: Store) -> None:
        key = self._key("alice.md")
        v1 = store.put(key, b"first")
        v2 = store.put(key, b"second", expect=v1)
        assert v2 != v1
        assert store.read(key) == b"second"

    def test_put_expect_stale_version_raises(self, store: Store) -> None:
        key = self._key("alice.md")
        v1 = store.put(key, b"first")
        store.put(key, b"second", expect=v1)
        with pytest.raises(StoreConflictError):
            store.put(key, b"third", expect=v1)  # v1 is now stale
        assert store.read(key) == b"second"

    def test_read_missing_key_raises(self, store: Store) -> None:
        with pytest.raises((FileNotFoundError, KeyError)):
            store.read(self._key("missing.md"))

    # -- read_many ------------------------------------------------------------

    def test_read_many_omits_missing_keys(self, store: Store) -> None:
        present = self._key("alice.md")
        missing = self._key("ghost.md")
        store.put(present, b"alice content")
        result = store.read_many([present, missing])
        assert result == {present: b"alice content"}

    def test_read_many_empty_input_is_empty_output(self, store: Store) -> None:
        assert dict(store.read_many([])) == {}

    # -- iter_meta / iter_records ---------------------------------------------

    def test_iter_meta_lists_every_object_under_surface(self, store: Store) -> None:
        store.put(self._key("alice.md"), b"a")
        store.put(self._key("bob.md"), b"b")
        metas = list(store.iter_meta(self.surface_name))
        assert {m.key.key for m in metas} == {"alice.md", "bob.md"}
        for meta in metas:
            assert isinstance(meta, ObjectMeta)
            assert meta.size == 1

    def test_iter_meta_prefix_filters(self, store: Store) -> None:
        store.put(self._key("people/alice.md"), b"a")
        store.put(self._key("people/bob.md"), b"b")
        store.put(self._key("concepts/gravity.md"), b"g")
        metas = list(store.iter_meta(self.surface_name, prefix="people/"))
        assert {m.key.key for m in metas} == {"people/alice.md", "people/bob.md"}

    def test_iter_meta_empty_surface_yields_nothing(self, store: Store) -> None:
        assert list(store.iter_meta(self.surface_name)) == []

    def test_iter_records_parses_frontmatter_and_body(self, store: Store) -> None:
        store.put(
            self._key("alice.md"),
            b"---\nname: alice\ntype: person\n---\n\nAlice is a person.\n",
        )
        records = list(store.iter_records(self.surface_name))
        assert len(records) == 1
        record = records[0]
        assert isinstance(record, Record)
        assert record.frontmatter.get("name") == "alice"
        assert record.frontmatter.get("type") == "person"
        assert "Alice is a person." in record.body

    # -- append -----------------------------------------------------------

    def test_append_creates_then_accumulates(self, store: Store) -> None:
        key = self._key("_ledger.jsonl")
        store.append(key, b'{"n": 1}\n')
        store.append(key, b'{"n": 2}\n')
        assert store.read(key) == b'{"n": 1}\n{"n": 2}\n'

    # -- delete -----------------------------------------------------------

    def test_delete_unconditional_removes_object(self, store: Store) -> None:
        key = self._key("alice.md")
        store.put(key, b"content")
        assert store.delete(key) is True
        with pytest.raises((FileNotFoundError, KeyError)):
            store.read(key)

    def test_delete_missing_key_unconditional_returns_false(self, store: Store) -> None:
        assert store.delete(self._key("ghost.md")) is False

    def test_delete_missing_key_with_expect_raises(self, store: Store) -> None:
        with pytest.raises(StoreConflictError):
            store.delete(self._key("ghost.md"), expect="anything")

    def test_delete_cas_mismatch_raises_and_keeps_object(self, store: Store) -> None:
        key = self._key("alice.md")
        store.put(key, b"content")
        with pytest.raises(StoreConflictError):
            store.delete(key, expect="not-the-real-version")
        assert store.read(key) == b"content"

    def test_delete_cas_match_succeeds(self, store: Store) -> None:
        key = self._key("alice.md")
        version = store.put(key, b"content")
        assert store.delete(key, expect=version) is True

    # -- move -------------------------------------------------------------

    def test_move_relocates_object(self, store: Store) -> None:
        src = self._key("_quarantine_candidates/alice.md")
        dst = self._key("_quarantine/alice.md")
        store.put(src, b"quarantined content")
        store.move(src, dst)
        assert store.read(dst) == b"quarantined content"
        with pytest.raises((FileNotFoundError, KeyError)):
            store.read(src)

    def test_move_missing_source_raises(self, store: Store) -> None:
        with pytest.raises(FileNotFoundError):
            store.move(self._key("ghost.md"), self._key("elsewhere.md"))

    def test_move_onto_existing_destination_refuses(self, store: Store) -> None:
        src = self._key("alice.md")
        dst = self._key("bob.md")
        store.put(src, b"alice content")
        store.put(dst, b"bob content")
        with pytest.raises(StoreConflictError):
            store.move(src, dst)
        # Neither side should have been touched by the refused move.
        assert store.read(src) == b"alice content"
        assert store.read(dst) == b"bob content"

    # -- capabilities (design note §6.1 D4) -----------------------------

    def test_capabilities_are_declared(self, store: Store) -> None:
        """This suite exercises every non-``versioned`` capability directly
        (put/append/delete/move/lease/bulk read+list), so a conformant
        implementation under test must declare each ``True`` — see the
        module docstring's "Scope" section. ``versioned`` is genuinely
        adapter-specific (git-backed or not); only its type is checked here.
        """
        caps = store.capabilities
        assert isinstance(caps, StoreCapabilities)
        assert isinstance(caps.versioned, bool)
        assert caps.leases is True
        assert caps.purgeable is True
        assert caps.compare_and_swap is True
        assert caps.append is True
        assert caps.bulk_list is True
        assert caps.bulk_read is True
        assert caps.cheap_local_scan is True
        assert caps.classes == {"source", "derived", "operational", "config"}
        assert caps.operational_scopes == {"store-durable", "machine-local"}

    def test_local_path_for_is_declared_consistently(self, store: Store) -> None:
        """``local_path_for`` (design note §6.2 D2's nullable escape hatch) is
        either ``None`` or a real, callable path resolver — checked generically
        here since which implementations set it is adapter-specific."""
        caps = store.capabilities
        if caps.local_path_for is None:
            return
        resolved = caps.local_path_for(self._key("alice.md"))
        assert isinstance(resolved, Path)

    # -- snapshot ---------------------------------------------------------

    def test_snapshot_before_any_write_returns_none(self, store: Store) -> None:
        """A fresh store with nothing written has nothing to commit, so
        ``snapshot`` returns ``None`` regardless of whether the implementation
        is git-backed (design note §6.2: "``None`` when there was nothing to
        commit")."""
        assert store.snapshot("pre-write") is None

    # -- lease --------------------------------------------------------------
    #
    # Acquiring, re-entrant contention, and release-then-reacquire are shared
    # across every implementation. Expiry/renewal semantics are deliberately
    # NOT part of this portable suite — see the module docstring.

    def test_lease_acquire_yields_a_lease(self, store: Store) -> None:
        with store.lease("build-index", ttl_seconds=30.0) as lease:
            assert isinstance(lease, Lease)
            assert lease.name == "build-index"
            assert lease.token

    def test_lease_contention_raises_while_held(self, store: Store) -> None:
        with store.lease("build-index", ttl_seconds=30.0):
            with pytest.raises(LeaseHeldError):
                with store.lease("build-index", ttl_seconds=30.0):
                    pass  # pragma: no cover - never reached

    def test_lease_available_again_after_release(self, store: Store) -> None:
        with store.lease("build-index", ttl_seconds=30.0):
            pass
        with store.lease("build-index", ttl_seconds=30.0) as lease:
            assert isinstance(lease, Lease)

    def test_lease_names_are_independent(self, store: Store) -> None:
        """Holding one lease name must never block a different one."""
        with store.lease("build-index", ttl_seconds=30.0):
            with store.lease("gc-sweep", ttl_seconds=30.0) as other:
                assert other.name == "gc-sweep"

    # -- bootstrap ----------------------------------------------------------

    def test_bootstrap_is_idempotent_and_non_destructive(self, store: Store) -> None:
        key = self._key("alice.md")
        store.put(key, b"content")
        store.bootstrap()
        store.bootstrap()  # idempotent
        assert store.read(key) == b"content"
