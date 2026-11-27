# SPDX-License-Identifier: Apache-2.0
"""``access.can``: one predicate for whether a principal holds a right (athenaeum#3167)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import access, store_rights
from athenaeum.access import can

ISS = "https://git.example.net"
OWNER = (ISS, "owner")
EDITOR = (ISS, "ed")
MEMBER = (ISS, "mem")
STRANGER = (ISS, "nobody")
ALL = ("read", "propose", "compile", "answer")

DECL: dict[str, Any] = {
    "version": 1,
    "store_id": "st_q7m2xk4vwz5d",
    "groups": {"team": [{"iss": ISS, "sub": "mem"}]},
    "roles": {
        "editor": {"rights": ["read", "compile"], "members": [{"iss": ISS, "sub": "ed"}]},
        "contrib": {"rights": ["read", "propose"], "members": ["group:team"]},
    },
}


@pytest.fixture(autouse=True)
def _fresh() -> None:
    store_rights.reset_run_rights()


def _setup(tmp_path: Path, declaration: Any | None) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "k"
    (root / "wiki").mkdir(parents=True)
    config: dict[str, Any] = {"owner": {"asserter": {"iss": ISS, "sub": "owner"}}}
    if declaration is not None:
        d = root / "wiki" / "_store"
        d.mkdir()
        text = declaration if isinstance(declaration, str) else yaml.safe_dump(declaration)
        (d / "access.yaml").write_text(text, encoding="utf-8")
    return root, config


def _can(
    p: Any, right: str, root: Path, config: dict[str, Any], loc: str = "wiki-markdown-embedded"
) -> bool:
    return can(p, loc, right, config=config, knowledge_root=root)


def _rights(p: Any, root: Path, config: dict[str, Any]) -> set[str]:
    return {r for r in ALL if _can(p, r, root, config)}


def test_returns_bool(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path, DECL)
    assert _can(EDITOR, "read", root, cfg) is True
    assert _can(EDITOR, "answer", root, cfg) is False


def test_valid_declaration_union_direct_and_group(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path, DECL)
    assert _rights(EDITOR, root, cfg) == {"read", "compile"}
    assert _rights(MEMBER, root, cfg) == {"read", "propose"}
    assert _rights(STRANGER, root, cfg) == set()
    # the owner is not special when a declaration exists
    assert _rights(OWNER, root, cfg) == set()


def test_union_across_roles(tmp_path: Path) -> None:
    decl = {**DECL, "groups": {"team": [{"iss": ISS, "sub": "ed"}]}}
    root, cfg = _setup(tmp_path, decl)
    assert _rights(EDITOR, root, cfg) == {"read", "propose", "compile"}


def test_no_declaration_owner_holds_all_others_none(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path, None)
    assert _rights(OWNER, root, cfg) == set(ALL)
    assert _rights(STRANGER, root, cfg) == set()


def test_no_declaration_and_no_owner_nobody(tmp_path: Path) -> None:
    root, _ = _setup(tmp_path, None)
    assert _rights(OWNER, root, {}) == set()


def test_invalid_declaration_narrows_to_owner_read(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path, "version: [unclosed")
    assert _rights(OWNER, root, cfg) == {"read"}
    assert _rights(EDITOR, root, cfg) == set()
    assert _rights(STRANGER, root, cfg) == set()


def test_invalid_declaration_even_if_it_lists_principal(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path, {**DECL, "version": 99})
    assert _rights(EDITOR, root, cfg) == set()
    assert _rights(OWNER, root, cfg) == {"read"}


def test_directory_that_cannot_answer_narrows(tmp_path: Path) -> None:
    decl = {**DECL, "directory": "corp-idp"}
    root, cfg = _setup(tmp_path, decl)
    assert _rights(OWNER, root, cfg) == {"read"}
    assert _rights(MEMBER, root, cfg) == set()
    assert _rights(EDITOR, root, cfg) == set()


def test_unknown_location_and_right_raise(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path, DECL)
    with pytest.raises(ValueError):
        _can(EDITOR, "read", root, cfg, loc="no-such-location")
    with pytest.raises(ValueError):
        _can(EDITOR, "admin", root, cfg)
    with pytest.raises(ValueError):
        _can(None, "admin", root, cfg)


def test_empty_principal_false(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path, None)
    for p in (None, (), [], ""):
        assert _can(p, "read", root, cfg) is False


def test_no_code_path_keys_on_a_role() -> None:
    assert inspect.signature(can).return_annotation in (bool, "bool")
    tree = ast.parse(inspect.getsource(access))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not {"roles", "role"} & (attrs | names)


def test_resolution_held_per_process_for_any_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, cfg = _setup(tmp_path, DECL)
    calls: list[int] = []
    real = store_rights.resolve_surface_rights

    def counting(*a: Any, **k: Any) -> Any:
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(store_rights, "resolve_surface_rights", counting)
    for _ in range(3):
        _can(MEMBER, "read", root, cfg)
        _can(MEMBER, "propose", root, cfg)
    assert len(calls) == 1
    _can(EDITOR, "read", root, cfg)
    assert len(calls) == 2


def test_owner_path_unchanged(tmp_path: Path) -> None:
    root, _ = _setup(tmp_path, DECL)
    surface = root / "wiki"
    assert store_rights.declared_surface_rights(surface, {"iss": ISS, "sub": "ed"}) == frozenset(
        {"read", "compile"}
    )
    assert store_rights.declared_surface_rights(surface, {"iss": ISS, "sub": "x"}) == frozenset()


def test_not_on_recall_query_path() -> None:
    for mod in ("search", "mcp_server"):
        path = Path(access.__file__).parent / f"{mod}.py"
        if path.exists():
            assert "athenaeum.access" not in path.read_text(encoding="utf-8")


def test_granted_read_only_is_not_taken_for_narrowing(tmp_path: Path) -> None:
    decl = {
        **DECL,
        "roles": {"r": {"rights": ["read"], "members": [{"iss": ISS, "sub": "ed"}]}},
    }
    root, cfg = _setup(tmp_path, decl)
    assert _can(EDITOR, "read", root, cfg) is True
    assert _can(OWNER, "read", root, cfg) is False  # declaration answers, owner not special
