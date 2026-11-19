# SPDX-License-Identifier: Apache-2.0
"""``_store/access.yaml`` parser and validator (athenaeum#2772)."""

from __future__ import annotations

import copy
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import store_access
from athenaeum.models import PageLocator, asserter_identity_key
from athenaeum.search import _scan_indexed_records
from athenaeum.storage import IndexedSurface, refuse_read_only_write
from athenaeum.store import ARTIFACT_REGISTRY
from athenaeum.store_access import (
    StoreDeclaration,
    StoreDeclarationInvalid,
    declared_rights,
    is_store_id,
    load_store_declaration,
    mint_store_id,
    parse_store_declaration,
)

ISS = "https://git.example.net"

EXAMPLE: dict[str, Any] = {
    "version": 1,
    "store_id": "st_q7m2xk4vwz5d",
    "directory": "static",
    "groups": {
        "leadership": [{"iss": ISS, "sub": "1042"}, {"iss": ISS, "sub": "1077"}],
        "department-heads": [{"iss": ISS, "sub": "1203"}],
    },
    "roles": {
        "staff": {"rights": ["read"], "members": ["*"]},
        "contributor": {"rights": ["propose"], "members": ["group:department-heads"]},
        "editor": {
            "rights": ["read", "propose", "compile", "answer"],
            "members": ["group:leadership"],
        },
    },
    "sync": {"mode": "integrate"},
    "locations": {
        "raw": "git@git.example.net:acme/announcements-raw.git",
        "bench": "git@git.example.net:acme/announcements-bench.git",
    },
    "reviews": [],
}


def _doc(**overrides: Any) -> str:
    data = copy.deepcopy(EXAMPLE)
    for key, value in overrides.items():
        if value is _DROP:
            data.pop(key, None)
        else:
            data[key] = value
    return yaml.safe_dump(data)


_DROP = object()


def _invalid(text: str) -> StoreDeclarationInvalid:
    result = parse_store_declaration(text)
    assert isinstance(result, StoreDeclarationInvalid), result
    return result


def _write(root: Path, text: str) -> None:
    path = root / "_store" / "access.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


REPO = Path(__file__).resolve().parent.parent


def _first_yaml_block(doc: Path, after: str) -> str:
    text = doc.read_text(encoding="utf-8")
    text = text[text.index(after) :]
    match = re.search(r"```yaml\n(.*?)```", text, re.S)
    assert match is not None
    return match.group(1)


class TestDocumentedExamples:
    def test_access_note_section_five_example_parses(self) -> None:
        block = _first_yaml_block(
            REPO / "docs/design/federation-store-access.md", "## 5. How a store declares"
        )
        result = parse_store_declaration(block)
        assert isinstance(result, StoreDeclaration), getattr(result, "messages", None)
        assert result.store_id == "st_q7m2xk4vwz5d"
        assert result.sync.mode == "integrate"
        assert result.sync.always_ask == ()
        assert sorted(r.name for r in result.roles) == ["contributor", "editor", "staff"]

    def test_reference_page_example_parses(self) -> None:
        block = _first_yaml_block(REPO / "docs/reference/store-declaration.md", "## Example")
        result = parse_store_declaration(block)
        assert isinstance(result, StoreDeclaration), getattr(result, "messages", None)
        assert result.sync.always_ask == ("hr-record",)

    def test_no_sync_mode_spelling_outside_the_local_narrowing_key(self) -> None:
        for note in ("federation-store-access.md", "federation-multi-writer-sync.md"):
            for line in (REPO / "docs/design" / note).read_text(encoding="utf-8").splitlines():
                if "sync_mode" in line:
                    assert "storage.adapters" in line or "local" in line.lower(), (note, line)


class TestValid:
    def test_example_parses_to_documented_values(self) -> None:
        result = parse_store_declaration(_doc())
        assert isinstance(result, StoreDeclaration)
        assert result.version == 1
        assert result.store_id == "st_q7m2xk4vwz5d"
        assert result.directory == "static"
        groups = {g.name: g for g in result.groups}
        assert sorted(groups) == ["department-heads", "leadership"]
        assert [p.sub for p in groups["leadership"].members] == ["1042", "1077"]
        roles = {r.name: r for r in result.roles}
        assert roles["staff"].rights == frozenset({"read"})
        assert roles["staff"].members[0].kind == "any"
        assert roles["contributor"].members[0].group == "department-heads"
        assert roles["editor"].rights == {"read", "propose", "compile", "answer"}
        assert result.sync.mode == "integrate"
        assert result.sync.always_ask == ()
        assert result.locations.raw == "git@git.example.net:acme/announcements-raw.git"
        assert result.locations.bench == "git@git.example.net:acme/announcements-bench.git"
        assert result.reviews == ()

    def test_absent_file_is_none(self, tmp_path: Path) -> None:
        assert load_store_declaration(tmp_path) is None

    def test_load_reads_the_file(self, tmp_path: Path) -> None:
        _write(tmp_path, _doc())
        assert isinstance(load_store_declaration(tmp_path), StoreDeclaration)

    def test_no_sync_block_defaults(self) -> None:
        result = parse_store_declaration(_doc(sync=_DROP))
        assert isinstance(result, StoreDeclaration)
        assert result.sync.mode == "fast_forward"
        assert result.sync.always_ask == ()

    def test_sync_integrate_with_always_ask(self) -> None:
        result = parse_store_declaration(
            _doc(sync={"mode": "integrate", "always_ask": ["hr-record"]})
        )
        assert isinstance(result, StoreDeclaration)
        assert result.sync.mode == "integrate"
        assert result.sync.always_ask == ("hr-record",)

    def test_directory_defaults_to_static(self) -> None:
        result = parse_store_declaration(_doc(directory=_DROP))
        assert isinstance(result, StoreDeclaration)
        assert result.directory == "static"

    def test_raw_single_string(self) -> None:
        result = parse_store_declaration(_doc())
        assert isinstance(result, StoreDeclaration)
        assert isinstance(result.locations.raw, str)

    def test_raw_list_of_two_entries(self) -> None:
        raw = [
            {"for": {"iss": ISS, "sub": "1203"}, "at": "git@git.example.net:acme/raw-1203.git"},
            {"for": {"iss": ISS, "sub": "1318"}, "at": "git@git.example.net:acme/raw-1318.git"},
        ]
        result = parse_store_declaration(_doc(locations={"raw": raw}))
        assert isinstance(result, StoreDeclaration)
        entries = result.locations.raw
        assert isinstance(entries, tuple) and len(entries) == 2
        assert entries[0].proposer.key == (ISS, "1203")
        assert entries[0].at == "git@git.example.net:acme/raw-1203.git"
        assert entries[1].proposer.sub == "1318"
        assert result.locations.bench is None

    def test_no_raw_location_is_valid(self) -> None:
        result = parse_store_declaration(_doc(locations={}))
        assert isinstance(result, StoreDeclaration)
        assert result.locations.raw is None
        result = parse_store_declaration(_doc(locations=_DROP))
        assert isinstance(result, StoreDeclaration)

    def test_review_purpose_member_and_reviews_shape(self) -> None:
        roles = {
            "reader": {
                "rights": ["read"],
                "members": [
                    "group:field-team",
                    {"principal": "group:leadership", "purpose": "review"},
                ],
            }
        }
        reviews = [
            {
                "name": "weekly-field-notes",
                "source": "st_f2n6rb5hjc3t",
                "every": "7d",
                "criteria": "_store/reviews/weekly-field-notes.md",
            }
        ]
        result = parse_store_declaration(_doc(roles=roles, reviews=reviews))
        assert isinstance(result, StoreDeclaration)
        assert result.roles[0].members[1].purpose == "review"
        assert result.roles[0].members[1].group == "leadership"
        assert result.reviews[0].name == "weekly-field-notes"

    def test_propose_without_read_is_valid(self) -> None:
        assert isinstance(parse_store_declaration(_doc()), StoreDeclaration)


def _bad(**overrides: Any) -> str:
    return _doc(**overrides)


def _role(rights: Any, members: Any = None) -> dict[str, Any]:
    return {"roles": {"r": {"rights": rights, "members": members or [{"iss": ISS, "sub": "1"}]}}}


COUNTER_EXAMPLES: list[tuple[str, str, str]] = [
    ("malformed-yaml", "version: 1\nstore_id: [unclosed", "_store/access.yaml"),
    ("not-a-mapping", "- a\n- b\n", "_store/access.yaml"),
    ("version-2", _bad(version=2), "version"),
    ("version-string", _bad(version="1"), "version"),
    ("version-bool", _bad(version=True), "version"),
    ("version-missing", _bad(version=_DROP), "version"),
    ("store-id-missing", _bad(store_id=_DROP), "store_id"),
    ("store-id-no-prefix", _bad(store_id="q7m2xk4vwz5d"), "store_id"),
    ("store-id-too-short", _bad(store_id="st_q7m2xk4vwz5"), "store_id"),
    ("store-id-too-long", _bad(store_id="st_q7m2xk4vwz5dd"), "store_id"),
    ("store-id-uppercase", _bad(store_id="st_Q7M2XK4VWZ5D"), "store_id"),
    ("store-id-digit-8", _bad(store_id="st_q7m2xk4vwz8d"), "store_id"),
    ("store-id-digit-9", _bad(store_id="st_q7m2xk4vwz9d"), "store_id"),
    ("store-id-digit-0", _bad(store_id="st_q7m2xk4vwz0d"), "store_id"),
    ("store-id-digit-1", _bad(store_id="st_q7m2xk4vwz1d"), "store_id"),
    ("directory-empty", _bad(directory=""), "directory"),
    ("rights-write", _bad(**_role(["write"])), "roles.r.rights"),
    ("rights-compile-without-read", _bad(**_role(["compile"])), "roles.r.rights"),
    ("rights-answer-without-read", _bad(**_role(["propose", "answer"])), "roles.r.rights"),
    ("rights-empty", _bad(**_role([])), "roles.r.rights"),
    ("rights-missing", _bad(roles={"r": {"members": []}}), "roles.r.rights"),
    ("rights-not-a-list", _bad(**_role("read")), "roles.r.rights"),
    (
        "role-unknown-key",
        _bad(roles={"r": {"rights": ["read"], "members": [], "x": 1}}),
        "roles.r.x",
    ),
    ("members-missing", _bad(roles={"r": {"rights": ["read"]}}), "roles.r.members"),
    ("sync-mode-merge", _bad(sync={"mode": "merge"}), "sync.mode"),
    ("sync-always-ask-not-list", _bad(sync={"always_ask": "hr"}), "sync.always_ask"),
    ("sync-unknown-key", _bad(sync={"mod": "integrate"}), "sync.mod"),
    ("star-with-propose", _bad(**_role(["read", "propose"], ["*"])), "roles.r.members[0]"),
    ("star-with-compile", _bad(**_role(["read", "compile"], ["*"])), "roles.r.members[0]"),
    ("star-in-propose-only", _bad(**_role(["propose"], ["*"])), "roles.r.members[0]"),
    ("grants-key", _bad(grants={"x": 1}), "grants"),
    ("compile-key", _bad(compile={"mode": "single"}), "compile"),
    ("sync-mode-key", _bad(sync_mode="fast_forward"), "sync_mode"),
    ("sync-mod-key", _bad(sync_mod="fast_forward"), "sync_mod"),
    ("locations-inbox", _bad(locations={"inbox": "x"}), "locations.inbox"),
    ("locations-unknown", _bad(locations={"cache": "x"}), "locations.cache"),
    ("locations-bench-empty", _bad(locations={"bench": ""}), "locations.bench"),
    (
        "raw-for-group",
        _bad(locations={"raw": [{"for": "group:department-heads", "at": "x"}]}),
        "locations.raw[0].for",
    ),
    (
        "raw-for-star",
        _bad(locations={"raw": [{"for": "*", "at": "x"}]}),
        "locations.raw[0].for",
    ),
    (
        "raw-same-principal-twice",
        _bad(
            locations={
                "raw": [
                    {"for": {"iss": ISS, "sub": "1"}, "at": "a"},
                    {"for": {"sub": "1", "iss": ISS}, "at": "b"},
                ]
            }
        ),
        "locations.raw[1].for",
    ),
    ("raw-empty-list", _bad(locations={"raw": []}), "locations.raw"),
    ("raw-empty-string", _bad(locations={"raw": ""}), "locations.raw"),
    ("raw-wrong-type", _bad(locations={"raw": 3}), "locations.raw"),
    (
        "raw-at-empty",
        _bad(locations={"raw": [{"for": {"iss": ISS, "sub": "1"}, "at": ""}]}),
        "locations.raw[0].at",
    ),
    (
        "principal-no-sub",
        _bad(groups={"g": [{"iss": ISS}]}),
        "groups.g[0].sub",
    ),
    (
        "principal-no-iss",
        _bad(**_role(["read"], [{"sub": "1"}])),
        "roles.r.members[0].iss",
    ),
    (
        "principal-unknown-key",
        _bad(groups={"g": [{"iss": ISS, "sub": "1", "email": "x"}]}),
        "groups.g[0].email",
    ),
    ("group-not-list", _bad(groups={"g": "x"}), "groups.g"),
    ("member-bad-string", _bad(**_role(["read"], ["everyone"])), "roles.r.members[0]"),
    (
        "purpose-without-read",
        _bad(**_role(["propose"], [{"principal": "group:g", "purpose": "review"}])),
        "roles.r.members[0].purpose",
    ),
    (
        "purpose-unknown",
        _bad(**_role(["read"], [{"principal": "group:g", "purpose": "audit"}])),
        "roles.r.members[0].purpose",
    ),
    ("review-missing-key", _bad(reviews=[{"name": "n"}]), "reviews[0]"),
    (
        "review-empty-source",
        _bad(reviews=[{"name": "n", "source": "", "every": "7d", "criteria": "c"}]),
        "reviews[0].source",
    ),
    ("reviews-not-a-list", _bad(reviews={}), "reviews"),
    ("top-level-intake-key", _bad(intake={}), "intake"),
]


class TestCounterExamples:
    @pytest.mark.parametrize(
        "text, key",
        [(t, k) for _, t, k in COUNTER_EXAMPLES],
        ids=[n for n, _, _ in COUNTER_EXAMPLES],
    )
    def test_invalid_names_the_key_and_does_not_raise(self, text: str, key: str) -> None:
        result = _invalid(text)
        assert key in result.keys, result.messages
        assert any(m.startswith(f"{key}:") for m in result.messages)

    def test_unknown_top_level_key_in_version_one_is_named(self) -> None:
        result = _invalid(_doc(intake={"x": 1}))
        assert "intake" in result.keys
        assert "higher version" in result.messages[0]

    def test_unreadable_file_is_invalid_not_a_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "_store" / "access.yaml"
        path.parent.mkdir()
        path.write_bytes(b"\xff\xfe\x00bad")
        result = load_store_declaration(tmp_path)
        assert isinstance(result, StoreDeclarationInvalid)

    def test_a_directory_in_place_of_the_file_is_invalid(self, tmp_path: Path) -> None:
        (tmp_path / "_store" / "access.yaml").mkdir(parents=True)
        assert isinstance(load_store_declaration(tmp_path), StoreDeclarationInvalid)

    def test_all_problems_are_reported_together(self) -> None:
        result = _invalid(_doc(version=2, store_id="nope", grants={}))
        assert {"version", "store_id", "grants"} <= set(result.keys)

    def test_invalid_is_a_distinct_type_exposing_no_rights(self) -> None:
        good = parse_store_declaration(_doc())
        bad = _invalid(_doc(sync={"mode": "merge"}))
        assert type(good) is not type(bad)
        assert not isinstance(bad, StoreDeclaration)
        assert not issubclass(StoreDeclarationInvalid, StoreDeclaration)
        for attr in ("roles", "rights", "groups", "sync", "locations", "store_id", "version"):
            assert not hasattr(bad, attr)
        with pytest.raises(AttributeError):
            declared_rights(bad)  # type: ignore[arg-type]


class TestRights:
    def test_union_of_rights_across_roles(self) -> None:
        member = {"iss": ISS, "sub": "1042"}
        roles = {
            "a": {"rights": ["read"], "members": [member]},
            "b": {"rights": ["read", "propose"], "members": [{"sub": "1042", "iss": ISS}]},
            "c": {"rights": ["propose"], "members": [{"iss": ISS, "sub": "9"}]},
        }
        result = parse_store_declaration(_doc(roles=roles))
        assert isinstance(result, StoreDeclaration)
        rights = declared_rights(result)
        assert rights[(ISS, "1042")] == frozenset({"read", "propose"})
        assert rights[(ISS, "9")] == frozenset({"propose"})

    def test_no_public_function_takes_or_returns_a_role_name(self) -> None:
        for name in store_access.__all__:
            obj = getattr(store_access, name)
            if not inspect.isfunction(obj):
                continue
            sig = inspect.signature(obj)
            assert not any("role" in p for p in sig.parameters), name
            assert "role" not in str(sig.return_annotation).lower(), name
        roles = {"weird-role-name": {"rights": ["read"], "members": [{"iss": ISS, "sub": "1"}]}}
        result = parse_store_declaration(_doc(roles=roles))
        assert isinstance(result, StoreDeclaration)
        assert all(isinstance(k, tuple) for k in declared_rights(result))
        assert "weird-role-name" not in str(list(declared_rights(result).items()))

    def test_key_order_does_not_change_identity(self) -> None:
        a = {"iss": ISS, "sub": "1042"}
        b = {"sub": "1042", "iss": ISS}
        roles = {
            "r1": {"rights": ["read"], "members": [a]},
            "r2": {"rights": ["propose"], "members": [b]},
        }
        result = parse_store_declaration(_doc(roles=roles))
        assert isinstance(result, StoreDeclaration)
        keys = [m.principal.key for r in result.roles for m in r.members if m.principal]
        assert keys[0] == keys[1] == asserter_identity_key(a) == asserter_identity_key(b)


class TestStoreId:
    def test_minted_ids_validate(self) -> None:
        seen = {mint_store_id() for _ in range(50)}
        assert len(seen) == 50
        for sid in seen:
            assert is_store_id(sid)
            assert re.fullmatch(r"st_[a-z2-7]{12}", sid)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "st_",
            "st_abc",
            "ST_q7m2xk4vwz5d",
            "st_q7m2xk4vwz8d",
            "st_q7m2xk4vwz0d",
            None,
            12,
            "st_q7m2xk4vwz5d ",
        ],
    )
    def test_bad_shapes(self, bad: object) -> None:
        assert not is_store_id(bad)


class TestRegistry:
    def test_declaration_resolves_to_config(self) -> None:
        by_name = {e.name: e for e in ARTIFACT_REGISTRY}
        entry = by_name["store-access-declaration"]
        assert entry.persistence_class == "config"
        assert store_access.DECLARATION_RELPATH in entry.location
        assert by_name["store-review-criteria"].persistence_class == "config"


def _surface(tmp_path: Path) -> tuple[Path, Path]:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    root = tmp_path / "pages"
    (root / "_store" / "reviews").mkdir(parents=True)
    (root / "topics").mkdir()
    fm = "---\nname: {n}\ndescription: d\n---\nbody {n}\n"
    (root / "_store" / "reviews" / "weekly-field-notes.md").write_text(fm.format(n="criteria"))
    (root / "ordinary.md").write_text(fm.format(n="ordinary"))
    (root / "topics" / "sub.md").write_text(fm.format(n="sub"))
    (root / "_store" / "nested").mkdir()
    (root / "_store" / "nested" / "deep.md").write_text(fm.format(n="deep"))
    return wiki, root


class TestUnderscoreDirectoryExclusion:
    def test_locator_skips_underscore_directory(self, tmp_path: Path) -> None:
        wiki, root = _surface(tmp_path)
        locator = PageLocator(wiki, [IndexedSurface(prefix="pages", root=root)])
        keys = [k for k, _ in locator.iter_surface_pages()]
        assert keys == ["pages/ordinary.md", "pages/topics/sub.md"]

    def test_index_build_skips_underscore_directory(self, tmp_path: Path) -> None:
        wiki, root = _surface(tmp_path)
        names = {
            n for n, *_ in _scan_indexed_records(wiki, [IndexedSurface(prefix="pages", root=root)])
        }
        assert names == {"pages/ordinary.md", "pages/topics/sub.md"}

    def test_ordinary_subdirectory_still_indexed(self, tmp_path: Path) -> None:
        wiki, root = _surface(tmp_path)
        names = {
            n for n, *_ in _scan_indexed_records(wiki, [IndexedSurface(prefix="pages", root=root)])
        }
        assert "pages/topics/sub.md" in names

    def test_raw_intake_root_keeps_underscore_directories(self, tmp_path: Path) -> None:
        wiki, root = _surface(tmp_path)
        names = {
            n
            for n, *_ in _scan_indexed_records(
                wiki, [IndexedSurface(prefix="pages", root=root, intake=True)]
            )
        }
        assert "pages/_store/reviews/weekly-field-notes.md" in names

    def test_rule_is_the_wiki_rule(self) -> None:
        from athenaeum.models import underscore_segment_excluded
        from athenaeum.search import _wiki_relpath_excluded

        for rel in ("a.md", "_q/a.md", "x/_q/a.md", "x/y/_a.md", "x/y/a.md"):
            assert _wiki_relpath_excluded(Path(rel)) == underscore_segment_excluded(Path(rel).parts)


class TestNoBehaviourChange:
    def test_read_only_declaration_does_not_stop_a_write(self, tmp_path: Path) -> None:
        wiki, root = _surface(tmp_path)
        _write(
            root,
            _doc(roles={"r": {"rights": ["read"], "members": [{"iss": ISS, "sub": "1"}]}}),
        )
        decl = load_store_declaration(root)
        assert isinstance(decl, StoreDeclaration)
        assert declared_rights(decl) == {(ISS, "1"): frozenset({"read"})}
        target = root / "topics" / "new.md"
        assert refuse_read_only_write(target, tmp_path) is None
        target.write_text("written\n", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "written\n"
