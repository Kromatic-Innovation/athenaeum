# SPDX-License-Identifier: Apache-2.0
"""``backing_store`` is a closed vocabulary resolved through a registry (athenaeum#3165)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from athenaeum import storage
from athenaeum.storage import StorageConfigError, StoreBuildSpec
from athenaeum.store import FilesystemStore, Store
from athenaeum.store_conformance import StoreConformanceTests
from tests.store_fakes import InMemoryStore

_NAME = "fake-memory"


def _config(backing: str) -> dict[str, Any]:
    return {"storage": {"adapters": {"notes": {"backing_store": backing, "surface_root": "notes"}}}}


@pytest.fixture
def registered_fake() -> Iterator[list[StoreBuildSpec]]:
    """Register the in-memory fake and ALWAYS remove it, so it cannot leak."""
    specs: list[StoreBuildSpec] = []

    def factory(spec: StoreBuildSpec) -> Store:
        specs.append(spec)
        return InMemoryStore()

    storage.register_store_backend(_NAME, factory)
    try:
        yield specs
    finally:
        storage.unregister_store_backend(_NAME)


@pytest.mark.parametrize("name", ["markdown", "wiki-markdown"])
def test_builtin_names_resolve_to_the_filesystem_store(name: str, tmp_path: Path) -> None:
    assert isinstance(storage.open_store(_config(name), tmp_path), FilesystemStore)


def test_unknown_value_names_the_known_values_and_what_to_write(tmp_path: Path) -> None:
    with pytest.raises(StorageConfigError) as exc:
        storage.open_store(_config("sqlite"), tmp_path)
    msg = str(exc.value)
    assert "'sqlite'" in msg and "'markdown'" in msg and "'wiki-markdown'" in msg
    assert "Write 'markdown'" in msg


@pytest.mark.parametrize("name", ["markdown", "wiki-markdown"])
def test_builtin_names_cannot_be_shadowed(name: str) -> None:
    with pytest.raises(StorageConfigError, match="shadows a built-in"):
        storage.register_store_backend(name, lambda spec: InMemoryStore())


def test_duplicate_registration_raises(registered_fake: list[StoreBuildSpec]) -> None:
    with pytest.raises(StorageConfigError, match="already registered"):
        storage.register_store_backend(_NAME, lambda spec: InMemoryStore())


def test_registered_name_resolves_and_is_forgotten_after_teardown(
    registered_fake: list[StoreBuildSpec], tmp_path: Path
) -> None:
    store = storage.open_store(_config(_NAME), tmp_path)
    assert isinstance(store, InMemoryStore)
    assert _NAME in storage.known_backing_stores()


def test_registration_does_not_leak_across_tests(tmp_path: Path) -> None:
    assert _NAME not in storage.known_backing_stores()
    with pytest.raises(StorageConfigError):
        storage.open_store(_config(_NAME), tmp_path)


def test_locations_sharing_an_implementation_share_one_instance(
    registered_fake: list[StoreBuildSpec], tmp_path: Path
) -> None:
    config = _config(_NAME)
    config["storage"]["adapters"]["more"] = {"backing_store": _NAME, "surface_root": "more"}
    storage.open_store(config, tmp_path)
    assert len(registered_fake) == 1
    assert {"notes", "more"} <= set(registered_fake[0].roots)


def test_two_registered_implementations_in_one_store_are_refused(
    registered_fake: list[StoreBuildSpec], tmp_path: Path
) -> None:
    storage.register_store_backend("second-fake", lambda spec: InMemoryStore())
    try:
        config = _config(_NAME)
        config["storage"]["adapters"]["other"] = {
            "backing_store": "second-fake",
            "surface_root": "other",
        }
        with pytest.raises(StorageConfigError, match="more than one store implementation"):
            storage.open_store(config, tmp_path)
    finally:
        storage.unregister_store_backend("second-fake")


class TestRegisteredFakePassesConformance(StoreConformanceTests):
    """The published conformance suite, run against a store reached ONLY
    through the registry (``open_store`` with a ``backing_store`` naming it)."""

    @pytest.fixture
    def store(
        self, registered_fake: list[StoreBuildSpec], tmp_path: Path
    ) -> Iterator[Store]:
        yield storage.open_store(_config(_NAME), tmp_path)
