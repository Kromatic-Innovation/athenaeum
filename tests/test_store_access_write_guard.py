# SPDX-License-Identifier: Apache-2.0
"""The declared rights fold into the write guard (access S3, athenaeum#2988).

An owner who does not hold compile on a declared store is refused on that
store's pages, whatever the adapter's ``read_only`` says. The declaration can
only narrow local config; it never widens it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import store_rights
from athenaeum.storage import (
    ReadOnlySurfaceError,
    open_store,
    page_write_root_for_class,
    refuse_read_only_write,
    require_writable_path,
)
from athenaeum.store import StoreKey

ISS = "https://git.example.net"
ME = {"iss": ISS, "sub": "1"}
OTHER = {"iss": ISS, "sub": "2"}
STORE_ID = "st_q7m2xk4vwz5d"


def _decl(roles: dict[str, Any]) -> str:
    return yaml.safe_dump({"version": 1, "store_id": STORE_ID, "roles": roles})


COMPILER = _decl({"editor": {"rights": ["read", "compile"], "members": [ME]}})
READER = _decl({"reader": {"rights": ["read"], "members": [ME]}})
PROPOSER = _decl({"member": {"rights": ["read", "propose"], "members": [ME]}})
NOT_LISTED = _decl({"editor": {"rights": ["read", "compile"], "members": [OTHER]}})


def _kb(tmp_path: Path, declaration: str | None, *, read_only: bool = False) -> tuple[Path, dict]:
    (tmp_path / "wiki").mkdir()
    surface = tmp_path / "recipes"
    surface.mkdir()
    (surface / "loaf.md").write_text("---\nuid: r-1\n---\nbody\n", encoding="utf-8")
    if declaration is not None:
        (surface / "_store").mkdir()
        (surface / "_store" / "access.yaml").write_text(declaration, encoding="utf-8")
    config: dict[str, Any] = {
        "owner": {"asserter": ME},
        "storage": {
            "adapters": {
                "recipes": {
                    "backing_store": "markdown",
                    "surface_root": "recipes",
                    "corpus_policy": {"embedded": True, "recallable": True, "merge_eligible": True},
                    "read_only": read_only,
                }
            },
            "mapping": {"recipe": "recipes"},
        },
    }
    return tmp_path, config


@pytest.fixture(autouse=True)
def _fresh_rights() -> None:
    store_rights.reset_run_rights()


def _sub(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir()
    return d


def _writable(kr: Path, config: dict) -> bool:
    try:
        require_writable_path(kr / "recipes" / "loaf.md", kr, config)
    except ReadOnlySurfaceError:
        return False
    return True


def test_read_only_declaration_refuses_even_when_adapter_is_writable(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, READER, read_only=False)
    with pytest.raises(ReadOnlySurfaceError, match="does not grant this install compile"):
        require_writable_path(kr / "recipes" / "loaf.md", kr, config)


def test_compile_declaration_with_read_only_adapter_is_still_refused(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, COMPILER, read_only=True)
    with pytest.raises(ReadOnlySurfaceError, match="declared read_only"):
        require_writable_path(kr / "recipes" / "loaf.md", kr, config)


def test_compile_declaration_and_writable_adapter_writes(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, COMPILER)
    assert _writable(kr, config)


def test_no_declaration_behaves_as_today(tmp_path: Path) -> None:
    kr, config = _kb(_sub(tmp_path, "rw"), None)
    assert _writable(kr, config)
    kr2, config2 = _kb(_sub(tmp_path, "ro"), None, read_only=True)
    assert not _writable(kr2, config2)


@pytest.mark.parametrize("text", ["version: 99\n", "{{{ not yaml", "[]"])
def test_invalid_declaration_resolves_to_read_only(tmp_path: Path, text: str) -> None:
    kr, config = _kb(tmp_path, text)
    with pytest.raises(ReadOnlySurfaceError, match="rights held: read"):
        require_writable_path(kr / "recipes" / "loaf.md", kr, config)


def test_directory_that_cannot_answer_resolves_to_read_only(tmp_path: Path) -> None:
    decl = yaml.safe_dump(
        {
            "version": 1,
            "store_id": STORE_ID,
            "directory": "corp-ldap",
            "groups": {"staff": [ME]},
            "roles": {"editor": {"rights": ["read", "compile"], "members": ["group:staff"]}},
        }
    )
    kr, config = _kb(tmp_path, decl)
    with pytest.raises(ReadOnlySurfaceError, match="rights held: read"):
        require_writable_path(kr / "recipes" / "loaf.md", kr, config)


def test_a_resolution_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kr, config = _kb(tmp_path, COMPILER)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("directory exploded")

    monkeypatch.setattr(store_rights, "resolve_surface_rights", boom)
    with pytest.raises(ReadOnlySurfaceError):
        require_writable_path(kr / "recipes" / "loaf.md", kr, config)


def test_propose_does_not_grant_a_page_write(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, PROPOSER)
    with pytest.raises(ReadOnlySurfaceError, match="rights held: propose,read"):
        require_writable_path(kr / "recipes" / "loaf.md", kr, config)


def test_owner_not_listed_holds_nothing(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, NOT_LISTED)
    with pytest.raises(ReadOnlySurfaceError, match="rights held: none"):
        require_writable_path(kr / "recipes" / "loaf.md", kr, config)


def test_non_raising_form_reports_the_reason(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, READER)
    msg = refuse_read_only_write(kr / "recipes" / "loaf.md", kr, config)
    assert msg is not None and msg.startswith("ReadOnlySurfaceError:")


def test_store_seam_refuses_every_mutation(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, READER)
    store = open_store(config, kr)
    key = StoreKey("recipes", "loaf.md")
    with pytest.raises(ReadOnlySurfaceError, match="compile"):
        store.put(key, b"x")
    with pytest.raises(ReadOnlySurfaceError, match="compile"):
        store.delete(key)
    # The refused write left the page untouched.
    assert (kr / "recipes" / "loaf.md").read_text(encoding="utf-8").endswith("body\n")


def test_store_seam_writes_when_compile_is_held(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, COMPILER)
    open_store(config, kr).put(StoreKey("recipes", "new.md"), b"hello")
    assert (kr / "recipes" / "new.md").read_text(encoding="utf-8") == "hello"


def test_compile_create_route_refuses(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, READER)
    with pytest.raises(ReadOnlySurfaceError, match="compile"):
        page_write_root_for_class("recipe", config, kr)


def test_opening_the_store_and_reading_do_not_resolve_rights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kr, config = _kb(tmp_path, READER)

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("rights resolved off the write path")

    monkeypatch.setattr(store_rights, "resolve_surface_rights", boom)
    store = open_store(config, kr)
    assert store.read(StoreKey("recipes", "loaf.md")).endswith(b"body\n")


def test_rights_are_resolved_once_not_per_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kr, config = _kb(tmp_path, COMPILER)
    calls: list[Path] = []
    real = store_rights.resolve_surface_rights

    def counting(root: Path, *a: Any, **k: Any) -> Any:
        calls.append(root)
        return real(root, *a, **k)

    monkeypatch.setattr(store_rights, "resolve_surface_rights", counting)
    for _ in range(5):
        require_writable_path(kr / "recipes" / "loaf.md", kr, config)
    assert len(calls) == 1


def test_run_start_table_is_what_the_guard_reads(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, COMPILER)
    store_rights.record_run_start_rights([kr / "recipes"], ME)
    # The declaration changes after run start: the run keeps what it resolved.
    (kr / "recipes" / "_store" / "access.yaml").write_text(READER, encoding="utf-8")
    require_writable_path(kr / "recipes" / "loaf.md", kr, config)
    # The next run start picks the change up.
    store_rights.record_run_start_rights([kr / "recipes"], ME)
    with pytest.raises(ReadOnlySurfaceError):
        require_writable_path(kr / "recipes" / "loaf.md", kr, config)
