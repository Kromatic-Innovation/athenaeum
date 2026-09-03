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

**S8 (issue athenaeum#983):** the portable share of this suite — every test that
does not depend on which of athenaeum's two S1 fixtures is under test — now
lives in the published harness, :mod:`athenaeum.store_conformance`.
``TestStoreConformance`` below is itself just a subclass of
``athenaeum.store_conformance.StoreConformanceTests``, parametrized over both
S1 implementations via the ``store`` fixture; it adds only the handful of
assertions that are genuinely specific to THESE two fixtures (both non-git,
only one filesystem-backed) rather than portable across any conformant
``Store``. See that module's docstring for the exact scope split, and
``tests/test_store_conformance_harness.py`` for a third, from-scratch
implementation proving the harness runs standalone.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum import storage
from athenaeum.storage import StorageConfigError
from athenaeum.store import (
    FilesystemStore,
    Lease,
    LeaseHeldError,
    Store,
    StoreKey,
    StoreKeyError,
    append_line_durable,
    now_iso,
)
from athenaeum.store_conformance import StoreConformanceTests
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


class TestStoreConformance(StoreConformanceTests):
    """Runs the full portable harness (``athenaeum.store_conformance.
    StoreConformanceTests``) against BOTH ``FilesystemStore`` and
    ``InMemoryStore``, via the ``store`` fixture below — overriding it here
    (rather than leaving the base class's abstract one) is what parametrizes
    every inherited ``test_*`` method over both S1 implementations. The two
    methods below are ADDITIONAL, athenaeum-fixture-specific assertions the
    portable harness deliberately leaves generic — see
    ``athenaeum.store_conformance``'s module docstring for the scope split.
    """

    surface_name = _WIKI_SURFACE

    @pytest.fixture(params=sorted(_IMPLEMENTATIONS))
    def store(self, request: pytest.FixtureRequest, tmp_path: Path) -> Store:
        """One ``Store``, parametrized over every S1 implementation."""
        factory = _IMPLEMENTATIONS[request.param]
        return factory(tmp_path)

    def test_versioned_is_false_for_non_git_s1_fixtures(self, store: Store) -> None:
        """``versioned`` is False here because neither fixture is a git repo
        (``FilesystemStore``'s ``versioned`` is a real, constructor-time
        check — see ``tests/test_no_git_shelling_outside_store.py`` and the
        S3-specific ``FilesystemStore`` git-snapshot tests for the
        real-commit path; ``InMemoryStore`` never has a git tree to
        snapshot, so its ``versioned`` is always False). The portable harness
        only checks this flag's TYPE (``StoreConformanceTests.
        test_capabilities_are_declared``), since a third-party git-backed
        adapter may legitimately declare ``True``."""
        assert store.capabilities.versioned is False

    def test_local_path_for_set_only_on_filesystem_store(self, store: Store) -> None:
        """The portable harness only checks that ``local_path_for``, when
        set, resolves to a real ``Path``; this pins WHICH of the two S1
        fixtures sets it at all."""
        caps = store.capabilities
        if isinstance(store, FilesystemStore):
            assert caps.local_path_for is not None
            resolved = caps.local_path_for(self._key("alice.md"))
            assert resolved.name == "alice.md"
        else:
            assert caps.local_path_for is None


# ---------------------------------------------------------------------------
# Fake-adapter lease-expiry (issue athenaeum#979, S4, AC5) — the non-filesystem
# path. FilesystemStore's lease has no independent TTL-driven expiry (the
# kernel already reclaims a dead holder's flock — see FileLease's docstring),
# so this genuinely-expiring behavior is specific to InMemoryStore and is NOT
# part of the shared TestStoreConformance suite above.
# ---------------------------------------------------------------------------


class TestFakeAdapterLeaseExpiry:
    def test_lease_expires_after_ttl_without_renewal(self) -> None:
        store = InMemoryStore()
        with store.lease("nightly-compile", ttl_seconds=0.05) as first:
            token = first.token
        time.sleep(0.1)  # past the TTL; the holder above never renewed
        with store.lease("nightly-compile", ttl_seconds=0.05) as second:
            assert second.token != token  # a genuinely new holder, not a reuse

    def test_lease_contended_within_ttl_even_after_context_exit(self) -> None:
        """Exiting the ``with`` block above releases explicitly — this proves
        the EXPIRY case specifically: a lease still within its TTL is held
        even though nothing has renewed it recently."""
        store = InMemoryStore()
        cm = store.lease("nightly-compile", ttl_seconds=30.0)
        cm.__enter__()  # deliberately not released — simulates a crashed holder
        with pytest.raises(LeaseHeldError):
            with store.lease("nightly-compile", ttl_seconds=30.0):
                pass  # pragma: no cover - never reached

    def test_heartbeat_renews_and_blocks_expiry(self) -> None:
        store = InMemoryStore()
        cm = store.lease("nightly-compile", ttl_seconds=0.15)
        cm.__enter__()
        try:
            time.sleep(0.1)
            cm.heartbeat()  # renews for another 0.15s from now
            time.sleep(0.1)  # 0.2s since acquire, but only 0.1s since renewal
            with pytest.raises(LeaseHeldError):
                with store.lease("nightly-compile", ttl_seconds=0.15):
                    pass  # pragma: no cover - never reached
        finally:
            cm.__exit__(None, None, None)

    def test_lease_available_once_ttl_elapses_even_without_release(self) -> None:
        store = InMemoryStore()
        cm = store.lease("nightly-compile", ttl_seconds=0.05)
        cm.__enter__()  # never released — the fake-adapter equivalent of a crash
        time.sleep(0.1)
        with store.lease("nightly-compile", ttl_seconds=0.05) as lease:
            assert isinstance(lease, Lease)


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


# ---------------------------------------------------------------------------
# append_line_durable — issue athenaeum#980 (S5): the shared O_APPEND + fsync
# primitive :meth:`FilesystemStore.append` and every collapsed per-module
# ledger writer now delegate to, instead of each reimplementing it.
# ---------------------------------------------------------------------------


class TestAppendLineDurable:
    def test_creates_parent_dir_and_file(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "ledger.jsonl"
        append_line_durable(path, b'{"a":1}\n')
        assert path.read_bytes() == b'{"a":1}\n'

    def test_accumulates_across_calls(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        append_line_durable(path, b"first\n")
        append_line_durable(path, b"second\n")
        append_line_durable(path, b"third\n")
        assert path.read_bytes() == b"first\nsecond\nthird\n"

    def test_filesystem_store_append_matches_the_shared_primitive(self, tmp_path: Path) -> None:
        """FilesystemStore.append and the free function must be the SAME
        write, not two implementations that happen to agree today."""
        knowledge_root = tmp_path / "knowledge_root"
        (knowledge_root / "wiki").mkdir(parents=True)
        store = FilesystemStore(knowledge_root, roots={_WIKI_SURFACE: knowledge_root / "wiki"})
        key = StoreKey(surface=_WIKI_SURFACE, key="ledger.jsonl")
        store.append(key, b"via-store\n")

        direct_path = tmp_path / "direct" / "ledger.jsonl"
        append_line_durable(direct_path, b"via-store\n")

        assert (knowledge_root / "wiki" / "ledger.jsonl").read_bytes() == direct_path.read_bytes()


# ---------------------------------------------------------------------------
# now_iso — issue athenaeum#1348: the shared UTC-ISO second-precision
# rendering the thirteen module-private ``_now_iso()`` copies collapsed onto.
# ---------------------------------------------------------------------------


class TestNowIso:
    def test_renders_second_precision_no_fractional_seconds(self) -> None:
        dt = datetime(2026, 9, 3, 19, 12, 33, 123456, tzinfo=timezone.utc)
        assert now_iso(dt) == "2026-09-03T19:12:33Z"

    def test_naive_datetime_assumed_utc(self) -> None:
        naive = datetime(2026, 9, 3, 19, 12, 33)
        assert now_iso(naive) == "2026-09-03T19:12:33Z"

    def test_aware_non_utc_datetime_converted(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        dt = datetime(2026, 9, 3, 15, 12, 33, tzinfo=eastern)
        assert now_iso(dt) == "2026-09-03T19:12:33Z"

    def test_no_argument_uses_current_utc_time(self) -> None:
        before = datetime.now(timezone.utc)
        rendered = now_iso()
        after = datetime.now(timezone.utc)
        parsed = datetime.strptime(rendered, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        # Allow the second-precision truncation on either side of the call.
        assert before - timedelta(seconds=1) <= parsed <= after + timedelta(seconds=1)

    def test_matches_fingerprint_resolved_at_format(self) -> None:
        """The rendering must stay parseable by
        :data:`athenaeum.fingerprint._RESOLVED_AT_FORMAT` — the whole point
        of pinning second precision here (issue athenaeum#1348)."""
        from athenaeum.fingerprint import _RESOLVED_AT_FORMAT

        rendered = now_iso(datetime(2026, 9, 3, 19, 12, 33, tzinfo=timezone.utc))
        # Must not raise.
        datetime.strptime(rendered, _RESOLVED_AT_FORMAT)
