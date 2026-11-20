# SPDX-License-Identifier: Apache-2.0
"""Directory adapter, ``static`` adapter and rights resolution (athenaeum#2933)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from athenaeum import store_rights
from athenaeum.models import asserter_identity_key
from athenaeum.store_access import (
    StoreDeclaration,
    StoreDeclarationInvalid,
    parse_store_declaration,
)
from athenaeum.store_rights import (
    READER_ONLY,
    StaticDirectory,
    resolve_rights,
    resolve_surface_rights,
)

ISS = "https://git.example.net"
ALL = frozenset({"read", "propose", "compile", "answer"})

EXAMPLE: dict[str, Any] = {
    "version": 1,
    "store_id": "st_q7m2xk4vwz5d",
    "directory": "static",
    "groups": {
        "leadership": [{"iss": ISS, "sub": "1042"}],
        "heads": [{"iss": ISS, "sub": "1203"}],
    },
    "roles": {
        "staff": {"rights": ["read"], "members": ["*"]},
        "contributor": {"rights": ["propose"], "members": ["group:heads"]},
        "editor": {"rights": sorted(ALL), "members": ["group:leadership"]},
        "scribe": {"rights": ["read", "answer"], "members": [{"iss": ISS, "sub": "9"}]},
    },
}


def _decl(**over: Any) -> StoreDeclaration:
    data = copy.deepcopy(EXAMPLE)
    data.update(over)
    result = parse_store_declaration(yaml.safe_dump(data))
    assert isinstance(result, StoreDeclaration), result
    return result


def _who(sub: str) -> dict[str, object]:
    return {"iss": ISS, "sub": sub}


def test_static_adapter_resolves_group_from_declaration() -> None:
    adapter = StaticDirectory(_decl())
    assert adapter.members("leadership") == frozenset({asserter_identity_key(_who("1042"))})
    assert adapter.members("nope") is None


def test_rights_by_group_direct_and_star() -> None:
    decl = _decl()
    assert resolve_rights(decl, _who("1042")) == ALL
    assert resolve_rights(decl, _who("1203")) == frozenset({"read", "propose"})
    assert resolve_rights(decl, _who("9")) == frozenset({"read", "answer"})
    # "*" grants read to any install, named or not
    assert resolve_rights(decl, _who("5555")) == READER_ONLY
    assert resolve_rights(decl, None) == READER_ONLY


def test_no_rights_is_empty_and_result_is_never_a_role_name() -> None:
    decl = _decl(roles={"editor": {"rights": sorted(ALL), "members": ["group:leadership"]}})
    assert resolve_rights(decl, _who("5555")) == frozenset()
    got = resolve_rights(_decl(), _who("1042"))
    assert got <= ALL
    assert not got & {"staff", "editor", "contributor"}


def test_star_is_refused_in_a_role_holding_more_than_read() -> None:
    for rights in (["read", "propose"], ["propose"], ["read", "compile"], ["read", "answer"]):
        data = copy.deepcopy(EXAMPLE)
        data["roles"] = {"bad": {"rights": rights, "members": ["*"]}}
        result = parse_store_declaration(yaml.safe_dump(data))
        assert isinstance(result, StoreDeclarationInvalid)


def test_unanswerable_directory_narrows_to_reader_only() -> None:
    # declared adapter that is not registered
    decl = _decl(directory="ldap")
    assert resolve_rights(decl, _who("1042")) == READER_ONLY

    class Dead:
        def members(self, group: str) -> frozenset[tuple[str, ...]] | None:
            return None

    assert resolve_rights(decl, _who("1042"), {"ldap": Dead()}) == READER_ONLY
    # the direct-principal entry cannot widen past a failed directory either
    assert resolve_rights(decl, _who("9"), {"ldap": Dead()}) == READER_ONLY


def test_registered_adapter_answers() -> None:
    class Fixed:
        def members(self, group: str) -> frozenset[tuple[str, ...]] | None:
            if group != "leadership":
                return frozenset()
            return frozenset({asserter_identity_key(_who("7"))})

    decl = _decl(directory="ldap")
    assert resolve_rights(decl, _who("7"), {"ldap": Fixed()}) == ALL
    assert resolve_rights(decl, _who("1042"), {"ldap": Fixed()}) == READER_ONLY


def test_unknown_group_narrows() -> None:
    decl = _decl(roles={"r": {"rights": ["read"], "members": ["group:ghost"]}})
    assert resolve_rights(decl, _who("1042")) == READER_ONLY


def test_surface_resolution(tmp_path: Path) -> None:
    assert resolve_surface_rights(tmp_path, _who("1042")) is None
    (tmp_path / "_store").mkdir()
    (tmp_path / "_store" / "access.yaml").write_text(yaml.safe_dump(EXAMPLE))
    assert resolve_surface_rights(tmp_path, _who("1042")) == ALL
    (tmp_path / "_store" / "access.yaml").write_text("version: 99\n")
    assert resolve_surface_rights(tmp_path, _who("1042")) == READER_ONLY


def test_module_is_not_on_the_recall_path() -> None:
    import athenaeum.search as search

    assert "store_rights" not in (search.__doc__ or "")
    src = Path(search.__file__).read_text()
    assert "store_rights" not in src
    assert store_rights.__all__
