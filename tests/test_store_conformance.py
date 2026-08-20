# SPDX-License-Identifier: Apache-2.0
"""Conformance suite for the ``Store`` protocol (issue athenaeum#976, S1 of the
whole-store adapter design lock, issue athenaeum#911).

Modelled on ``tests/test_storage_enforcement.py::TestAdapterExtensionPointContract``
— the same "one contract, exercised the same way regardless of which concrete
implementation backs it" shape, parametrized here over BOTH implementations
this slice ships: :class:`~athenaeum.store.FilesystemStore` and the in-memory
:class:`tests.store_fakes.InMemoryStore`. Every test in ``TestStoreConformance``
runs twice — once per implementation — via the ``store`` fixture below, so a
test that only accidentally passes against one backend is caught immediately.

Per the issue's acceptance criteria, this file covers ONLY the seam itself:
no existing caller (``search.py``, ``librarian.py``, ``quarantine.py``, ...) is
touched or migrated here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from athenaeum import storage
from athenaeum.storage import StorageConfigError
from athenaeum.store import (
    FilesystemStore,
    Lease,
    ObjectMeta,
    Record,
    Store,
    StoreCapabilities,
    StoreConflictError,
    StoreKey,
    StoreKeyError,
)
from tests.store_fakes import InMemoryStore

# ---------------------------------------------------------------------------
# Implementations under test
# ---------------------------------------------------------------------------

# A literal surface name for the standalone (no athenaeum.storage involved)
# FilesystemStore fixture below — this module deliberately never imports
# athenaeum.store.resolve_store_for_class because there is no such function
# any more (see athenaeum.store's module docstring); TestResolveStoreForClass
# further down exercises athenaeum.storage.resolve_store_for_class instead,
# which is the one place this suite touches storage-adapter names like
# storage.DEFAULT_ADAPTER_NAME.
_WIKI_SURFACE = "wiki"


def _make_filesystem_store(tmp_path: Path) -> Store:
    root = tmp_path / "knowledge_root"
    root.mkdir()
    (root / "wiki").mkdir()
    return FilesystemStore(root, roots={_WIKI_SURFACE: root / "wiki"})


def _make_in_memory_store(tmp_path: Path) -> Store:
    return InMemoryStore()


_IMPLEMENTATIONS: dict[str, Callable[[Path], Store]] = {
    "filesystem": _make_filesystem_store,
    "in-memory": _make_in_memory_store,
}


@pytest.fixture(params=sorted(_IMPLEMENTATIONS))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    """One ``Store``, parametrized over every S1 implementation."""
    factory = _IMPLEMENTATIONS[request.param]
    return factory(tmp_path)


def _key(name: str, *, surface: str = _WIKI_SURFACE) -> StoreKey:
    return StoreKey(surface=surface, key=name)


# ---------------------------------------------------------------------------
# StoreKey validation (design note §6.2 D2)
# ---------------------------------------------------------------------------


class TestStoreKeyValidation:
    def test_valid_relative_key_is_accepted(self) -> None:
        StoreKey(surface="wiki", key="alice.md")
        StoreKey(surface="wiki", key="sub/dir/alice.md")

    @pytest.mark.parametrize(
        "bad_key",
        ["", "/alice.md", "sub/../alice.md", "..", "sub//alice.md", "sub\\alice.md"],
    )
    def test_invalid_key_raises(self, bad_key: str) -> None:
        with pytest.raises(StoreKeyError):
            StoreKey(surface="wiki", key=bad_key)


# ---------------------------------------------------------------------------
# The conformance suite proper
# ---------------------------------------------------------------------------


class TestStoreConformance:
    """Every test here runs against BOTH ``FilesystemStore`` and ``InMemoryStore``."""

    # -- put / read ---------------------------------------------------------

    def test_put_then_read_roundtrips(self, store: Store) -> None:
        key = _key("alice.md")
        version = store.put(key, b"---\nname: alice\n---\n\nBody text.\n")
        assert isinstance(version, str) and version
        assert store.read(key) == b"---\nname: alice\n---\n\nBody text.\n"

    def test_put_expect_none_is_exclusive_create(self, store: Store) -> None:
        key = _key("alice.md")
        store.put(key, b"first")
        with pytest.raises(StoreConflictError):
            store.put(key, b"second", expect=None)
        # The refused write must not have clobbered the original.
        assert store.read(key) == b"first"

    def test_put_expect_matching_version_succeeds(self, store: Store) -> None:
        key = _key("alice.md")
        v1 = store.put(key, b"first")
        v2 = store.put(key, b"second", expect=v1)
        assert v2 != v1
        assert store.read(key) == b"second"

    def test_put_expect_stale_version_raises(self, store: Store) -> None:
        key = _key("alice.md")
        v1 = store.put(key, b"first")
        store.put(key, b"second", expect=v1)
        with pytest.raises(StoreConflictError):
            store.put(key, b"third", expect=v1)  # v1 is now stale
        assert store.read(key) == b"second"

    def test_read_missing_key_raises(self, store: Store) -> None:
        with pytest.raises((FileNotFoundError, KeyError)):
            store.read(_key("missing.md"))

    # -- read_many ------------------------------------------------------------

    def test_read_many_omits_missing_keys(self, store: Store) -> None:
        present = _key("alice.md")
        missing = _key("ghost.md")
        store.put(present, b"alice content")
        result = store.read_many([present, missing])
        assert result == {present: b"alice content"}

    def test_read_many_empty_input_is_empty_output(self, store: Store) -> None:
        assert dict(store.read_many([])) == {}

    # -- iter_meta / iter_records ---------------------------------------------

    def test_iter_meta_lists_every_object_under_surface(self, store: Store) -> None:
        store.put(_key("alice.md"), b"a")
        store.put(_key("bob.md"), b"b")
        metas = list(store.iter_meta(_WIKI_SURFACE))
        assert {m.key.key for m in metas} == {"alice.md", "bob.md"}
        for meta in metas:
            assert isinstance(meta, ObjectMeta)
            assert meta.size == 1

    def test_iter_meta_prefix_filters(self, store: Store) -> None:
        store.put(_key("people/alice.md"), b"a")
        store.put(_key("people/bob.md"), b"b")
        store.put(_key("concepts/gravity.md"), b"g")
        metas = list(store.iter_meta(_WIKI_SURFACE, prefix="people/"))
        assert {m.key.key for m in metas} == {"people/alice.md", "people/bob.md"}

    def test_iter_meta_empty_surface_yields_nothing(self, store: Store) -> None:
        assert list(store.iter_meta(_WIKI_SURFACE)) == []

    def test_iter_records_parses_frontmatter_and_body(self, store: Store) -> None:
        store.put(
            _key("alice.md"),
            b"---\nname: alice\ntype: person\n---\n\nAlice is a person.\n",
        )
        records = list(store.iter_records(_WIKI_SURFACE))
        assert len(records) == 1
        record = records[0]
        assert isinstance(record, Record)
        assert record.frontmatter.get("name") == "alice"
        assert record.frontmatter.get("type") == "person"
        assert "Alice is a person." in record.body

    # -- append -----------------------------------------------------------

    def test_append_creates_then_accumulates(self, store: Store) -> None:
        key = _key("_ledger.jsonl")
        store.append(key, b'{"n": 1}\n')
        store.append(key, b'{"n": 2}\n')
        assert store.read(key) == b'{"n": 1}\n{"n": 2}\n'

    # -- delete -----------------------------------------------------------

    def test_delete_unconditional_removes_object(self, store: Store) -> None:
        key = _key("alice.md")
        store.put(key, b"content")
        assert store.delete(key) is True
        with pytest.raises((FileNotFoundError, KeyError)):
            store.read(key)

    def test_delete_missing_key_unconditional_returns_false(self, store: Store) -> None:
        assert store.delete(_key("ghost.md")) is False

    def test_delete_missing_key_with_expect_raises(self, store: Store) -> None:
        with pytest.raises(StoreConflictError):
            store.delete(_key("ghost.md"), expect="anything")

    def test_delete_cas_mismatch_raises_and_keeps_object(self, store: Store) -> None:
        key = _key("alice.md")
        store.put(key, b"content")
        with pytest.raises(StoreConflictError):
            store.delete(key, expect="not-the-real-version")
        assert store.read(key) == b"content"

    def test_delete_cas_match_succeeds(self, store: Store) -> None:
        key = _key("alice.md")
        version = store.put(key, b"content")
        assert store.delete(key, expect=version) is True

    # -- move -------------------------------------------------------------

    def test_move_relocates_object(self, store: Store) -> None:
        src = _key("_quarantine_candidates/alice.md")
        dst = _key("_quarantine/alice.md")
        store.put(src, b"quarantined content")
        store.move(src, dst)
        assert store.read(dst) == b"quarantined content"
        with pytest.raises((FileNotFoundError, KeyError)):
            store.read(src)

    def test_move_missing_source_raises(self, store: Store) -> None:
        with pytest.raises(FileNotFoundError):
            store.move(_key("ghost.md"), _key("elsewhere.md"))

    def test_move_onto_existing_destination_refuses(self, store: Store) -> None:
        src = _key("alice.md")
        dst = _key("bob.md")
        store.put(src, b"alice content")
        store.put(dst, b"bob content")
        with pytest.raises(StoreConflictError):
            store.move(src, dst)
        # Neither side should have been touched by the refused move.
        assert store.read(src) == b"alice content"
        assert store.read(dst) == b"bob content"

    # -- capabilities (design note §6.1 D4) -----------------------------

    def test_capabilities_are_declared(self, store: Store) -> None:
        caps = store.capabilities
        assert isinstance(caps, StoreCapabilities)
        # S1 profile: neither snapshot nor lease is wired yet (S3/S4).
        assert caps.versioned is False
        assert caps.leases is False
        assert caps.purgeable is True
        assert caps.compare_and_swap is True
        assert caps.append is True
        assert caps.bulk_list is True
        assert caps.bulk_read is True
        assert caps.cheap_local_scan is True
        assert caps.classes == {"source", "derived", "operational", "config"}
        assert caps.operational_scopes == {"store-durable", "machine-local"}

    def test_local_path_for_only_on_filesystem_adapter(self, store: Store) -> None:
        caps = store.capabilities
        if isinstance(store, FilesystemStore):
            assert caps.local_path_for is not None
            key = _key("alice.md")
            resolved = caps.local_path_for(key)
            assert resolved.name == "alice.md"
        else:
            assert caps.local_path_for is None

    # -- snapshot / lease (declared-inert in S1) -------------------------

    def test_snapshot_returns_none_in_s1(self, store: Store) -> None:
        assert store.snapshot("pre-write") is None

    def test_lease_raises_not_implemented_in_s1(self, store: Store) -> None:
        with pytest.raises(NotImplementedError):
            with store.lease("test-lease", ttl_seconds=5.0) as lease:
                assert isinstance(lease, Lease)  # pragma: no cover - never reached

    # -- bootstrap ----------------------------------------------------------

    def test_bootstrap_is_idempotent_and_non_destructive(self, store: Store) -> None:
        key = _key("alice.md")
        store.put(key, b"content")
        store.bootstrap()
        store.bootstrap()  # idempotent
        assert store.read(key) == b"content"


# ---------------------------------------------------------------------------
# resolve_store_for_class (design note §6.4; issue athenaeum#976 acceptance
# criterion — "alongside surface_root_for_class in storage.py")
# ---------------------------------------------------------------------------


class TestResolveStoreForClass:
    def test_resolves_default_class_to_a_filesystem_store(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge_root"
        result = storage.resolve_store_for_class(None, None, knowledge_root)
        assert isinstance(result, FilesystemStore)

    def test_alongside_surface_root_for_class_in_storage_module(self, tmp_path: Path) -> None:
        """The issue's literal acceptance criterion: ``resolve_store_for_class``
        is importable from ``athenaeum.storage`` beside ``surface_root_for_class``,
        and its surface routing agrees with that function for the same
        class/config — proof it extends the seam rather than forking it (D5)."""
        knowledge_root = tmp_path / "knowledge_root"
        result = storage.resolve_store_for_class(None, None, knowledge_root)
        assert isinstance(result, FilesystemStore)
        expected_root = storage.surface_root_for_class(None, None, knowledge_root)
        assert result.capabilities.local_path_for is not None
        resolved_path = result.capabilities.local_path_for(
            StoreKey(surface=storage.DEFAULT_ADAPTER_NAME, key="probe.md")
        )
        assert resolved_path == expected_root / "probe.md"

    def test_unknown_mapped_adapter_raises_same_as_surface_root_for_class(
        self, tmp_path: Path
    ) -> None:
        config = {"storage": {"mapping": {"secret": "does-not-exist"}}}
        knowledge_root = tmp_path / "knowledge_root"
        with pytest.raises(StorageConfigError):
            storage.surface_root_for_class("secret", config, knowledge_root)
        with pytest.raises(StorageConfigError):
            storage.resolve_store_for_class("secret", config, knowledge_root)

    def test_unconfigured_class_matches_surface_root_for_class_default(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge_root"
        result = storage.resolve_store_for_class("secret", None, knowledge_root)
        assert isinstance(result, FilesystemStore)
        # No config: every class routes to the default wiki surface, matching
        # surface_root_for_class's byte-identical-default guarantee.
        expected_root = storage.surface_root_for_class("secret", None, knowledge_root)
        assert expected_root == knowledge_root / "wiki"
