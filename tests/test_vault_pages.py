# SPDX-License-Identifier: Apache-2.0
"""Whole pages of a routed page type are written to a vault and read by uid (athenaeum#3174)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii
from athenaeum.intake_destination import destination_for_entity, vault_page_exists
from athenaeum.librarian import _apply_tier3_results
from athenaeum.mcp_server import entity_read
from athenaeum.models import EntityIndex, ProcessingResult, RawFile, WikiEntity
from athenaeum.search import FTS5Backend, query_fts5_index
from athenaeum.storage import (
    SurfaceNotIndexedError,
    note_vault_page,
    page_write_root_for_class,
    reset_vault_page_counts,
    resolve_indexed_surfaces,
    vault_page_counts,
    vault_route_for_page_type,
)
from athenaeum.store_links import admit_page_write
from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

OFF = {"embedded": False, "recallable": False, "merge_eligible": False}
MARKER = "zzvaultmarkerzz"


def _cfg(route: dict[str, Any] | None = None, **tables: Any) -> dict[str, Any]:
    storage: dict[str, Any] = {
        "adapters": {
            "vault": {
                "backing_store": "markdown",
                "surface_root": "vault",
                "corpus_policy": OFF,
                "sensitive": True,
            },
            "archive": {
                "backing_store": "markdown",
                "surface_root": "archive",
                "corpus_policy": OFF,
            },
        },
        "routes": [route if route is not None else {"page_type": "reference", "to": ["vault"]}],
        **tables,
    }
    return {"storage": storage}


def _raw(tmp_path: Path) -> RawFile:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "20260201T000000Z-aabbccdd.md"
    path.write_text("source\n", encoding="utf-8")
    return RawFile(
        path=path,
        source="sessions",
        timestamp="20260201T000000Z",
        uuid8="aabbccdd",
        _content="source\n",
    )


def _entity(etype: str = "reference", name: str = "Locker Combination", uid: str = "ref-0001"):
    return WikiEntity(
        uid=uid,
        type=etype,
        name=name,
        access="open",
        created="2026-01-01",
        updated="2026-01-01",
        body=f"# {name}\n\nThe {MARKER} is written here.\n",
        source="script:test",
    )


def _compile(tmp_path: Path, entities: list[WikiEntity], cfg: dict[str, Any] | None):
    wiki = tmp_path / "wiki"
    wiki.mkdir(exist_ok=True)
    index = EntityIndex(wiki)
    result = ProcessingResult(raw_file=_raw(tmp_path))
    _apply_tier3_results(
        result,
        new_entities=entities,
        pending_updates=[],
        updated_uids=[],
        escalations=[],
        wiki_root=wiki,
        index=index,
        config=cfg,
        raw=_raw(tmp_path),
    )
    return result, index


@pytest.fixture(autouse=True)
def _counts() -> None:
    reset_vault_page_counts()


class TestWriteGuard:
    def test_vault_target_through_a_page_type_rule_is_writable(self, tmp_path: Path) -> None:
        root = page_write_root_for_class("reference", _cfg(), tmp_path)
        assert root == tmp_path / "vault"
        assert vault_route_for_page_type("reference", _cfg()) == "vault"

    def test_non_sensitive_non_indexed_target_still_refused(self, tmp_path: Path) -> None:
        cfg = _cfg({"page_type": "reference", "to": ["archive"]})
        with pytest.raises(SurfaceNotIndexedError):
            page_write_root_for_class("reference", cfg, tmp_path)
        assert vault_route_for_page_type("reference", cfg) is None

    def test_storage_mapping_to_a_vault_is_still_refused(self, tmp_path: Path) -> None:
        cfg = _cfg({"page_type": "other", "to": ["vault"]}, mapping={"reference": "vault"})
        with pytest.raises(SurfaceNotIndexedError):
            page_write_root_for_class("reference", cfg, tmp_path)

    def test_a_field_rule_is_not_a_vault_route(self, tmp_path: Path) -> None:
        cfg = _cfg({"page_type": "reference", "fields": ["secret"], "to": ["vault"]})
        assert vault_route_for_page_type("reference", cfg) is None
        # whole pages of the type stay in the wiki: only the field is routed
        assert page_write_root_for_class("reference", cfg, tmp_path) == tmp_path / "wiki"

    def test_destination_names_the_vault(self, tmp_path: Path) -> None:
        dest = destination_for_entity("reference", _cfg(), tmp_path)
        assert dest.vault == "vault" and dest.root == tmp_path / "vault"
        assert destination_for_entity("concept", _cfg(), tmp_path).vault is None


class TestCompileWrite:
    def test_entity_creation_writes_to_the_vault_outside_every_index(self, tmp_path: Path) -> None:
        result, index = _compile(tmp_path, [_entity(), _entity("concept", "Plain", "c-1")], _cfg())
        assert len(list((tmp_path / "vault").glob("*.md"))) == 1
        assert not [p for p in (tmp_path / "wiki").glob("*.md") if "locker" in p.name.lower()]
        assert index.lookup("Locker Combination") is None  # not in the main name index
        assert index.get_by_uid("ref-0001") is None
        assert index.lookup("Plain") is not None
        assert vault_page_counts() == {"vault": 1}
        assert len(result.created) == 2

    def test_a_name_already_in_the_vault_is_an_update_not_a_second_page(
        self, tmp_path: Path
    ) -> None:
        _compile(tmp_path, [_entity()], _cfg())
        before = {p.name: p.read_bytes() for p in (tmp_path / "vault").glob("*.md")}
        result, _ = _compile(tmp_path, [_entity(uid="ref-0002")], _cfg())
        after = {p.name: p.read_bytes() for p in (tmp_path / "vault").glob("*.md")}
        assert before == after
        assert result.created == [] and result.retain_raw is True
        assert vault_page_counts() == {"vault": 1}

    def test_lookup_is_scoped_to_the_vault(self, tmp_path: Path) -> None:
        _compile(tmp_path, [_entity("concept", "Shared Name", "c-1")], _cfg())
        dest = destination_for_entity("reference", _cfg(), tmp_path)
        assert vault_page_exists(dest, "Shared Name") is False  # main page, not the vault's
        _compile(tmp_path, [_entity(name="Vault Only")], _cfg())
        assert vault_page_exists(dest, "Vault Only") is True
        assert EntityIndex(tmp_path / "wiki").lookup("Vault Only") is None
        plain = destination_for_entity("concept", _cfg(), tmp_path)
        assert vault_page_exists(plain, "Vault Only") is False

    def test_counts_hold_no_page_text(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG"):
            _compile(tmp_path, [_entity()], _cfg())
        assert MARKER not in caplog.text and "Locker Combination" not in caplog.text
        assert json.dumps(vault_page_counts()) == '{"vault": 1}'
        note_vault_page(None)
        assert vault_page_counts() == {"vault": 1}

    def test_batch_transport_writes_to_the_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_per_type_write_routing import TestBatchApply

        yaml_cfg = (
            "storage:\n  adapters:\n    vault:\n      backing_store: markdown\n"
            "      surface_root: vault\n      sensitive: true\n"
            "      corpus_policy:\n        embedded: false\n        recallable: false\n"
            "        merge_eligible: false\n"
            "  routes:\n    - page_type: concept\n      to: [vault]\n"
        )
        root = TestBatchApply()._run(tmp_path, monkeypatch, "vaulted", yaml_cfg)
        assert list((root / "vault").glob("*.md"))
        assert not [p for p in (root / "wiki").glob("*.md") if "widget" in p.name.lower()]
        assert vault_page_counts().get("vault", 0) >= 1

    def test_recompile_writes_a_new_page_to_the_vault(self, tmp_path: Path) -> None:
        from athenaeum.page_recompile_apply import write_apply
        from tests.test_page_recompile_apply import _BODY, _simple_plan, _write_page

        wiki = tmp_path / "knowledge" / "wiki"
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY, type="reference")
        _simple_plan(wiki, body=_BODY)
        outcome = write_apply(wiki, uid="page0001", client=None, config=_cfg())
        assert outcome.ok, outcome.issues
        assert next((wiki.parent / "vault").glob(f"{outcome.created_uids[0]}*.md"))
        assert vault_page_counts() == {"vault": 1}


class TestNotInAnyIndex:
    def test_keyword_index_merge_discovery_and_locator_skip_the_vault(
        self, tmp_path: Path
    ) -> None:
        cfg = _cfg()
        _compile(tmp_path, [_entity()], cfg)
        (tmp_path / "wiki" / "ok.md").write_text(
            "---\nname: Ok\ntype: concept\n---\n\nplain public page\n", encoding="utf-8"
        )
        roots = resolve_indexed_surfaces(tmp_path, cfg, extra_roots=[])
        assert all("vault" not in str(getattr(r, "root", r)) for r in roots)
        cache = tmp_path / "cache"
        FTS5Backend().build_index(tmp_path / "wiki", cache, extra_roots=roots, config=cfg)
        assert query_fts5_index(MARKER, cache) == []
        assert EntityIndex(tmp_path / "wiki").get_by_uid("ref-0001") is None
        found = discover_wiki_dedupe_candidates(tmp_path / "wiki", config=cfg)
        assert all(MARKER not in c.path.read_text() for c in found)

    def test_a_vault_typed_page_in_wiki_is_not_a_merge_candidate(self, tmp_path: Path) -> None:
        cfg = _cfg({"page_type": "project", "to": ["vault"]})
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "p.md").write_text(
            f"---\nname: P\ntype: project\n---\n\n{MARKER}\n", encoding="utf-8"
        )
        assert discover_wiki_dedupe_candidates(wiki, config=cfg) == []


class TestReadByUid:
    def _seed(self, tmp_path: Path) -> None:
        (tmp_path / "wiki").mkdir(exist_ok=True)
        _compile(tmp_path, [_entity()], _cfg())

    def test_read_with_the_flag_returns_the_page(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        got = pii.read_entity(
            tmp_path, _cfg(), "ref-0001", surface_class="reference", include_excluded=True
        )
        assert got is not None and not got.withheld
        assert MARKER in got.body and got.frontmatter["name"] == "Locker Combination"

    def test_read_without_the_flag_says_withheld_and_carries_no_value(
        self, tmp_path: Path
    ) -> None:
        self._seed(tmp_path)
        got = pii.read_entity(tmp_path, _cfg(), "ref-0001", surface_class="reference")
        assert got is not None and got.withheld is True
        payload = json.dumps(got.to_dict(), default=str)
        assert MARKER not in payload and "Locker" not in payload
        assert got.frontmatter == {} and got.body == "" and got.contact == {}
        assert got.to_dict()["withheld"] is True

    def test_unknown_uid_is_none_and_unrouted_type_never_reads_a_record_as_a_page(
        self, tmp_path: Path
    ) -> None:
        self._seed(tmp_path)
        assert pii.read_entity(tmp_path, _cfg(), "nope", surface_class="reference") is None
        # same files, but no page-type rule: a record found by uid is not a page
        cfg = {"storage": {"mapping": {"reference": "excluded"}}}
        assert (
            pii.read_entity(tmp_path, cfg, "ref-0001", surface_class="reference") is None
        )

    def test_a_person_page_in_a_vault_is_read_by_its_type(
        self, tmp_path: Path
    ) -> None:
        # A rule names the type; the read must find the page by the page's own
        # type. (athenaeum#3173: no built-in mapping gives ``person`` another
        # surface class any more, so the two names agree.)
        cfg = _cfg({"page_type": "person", "to": ["vault"]})
        _compile(tmp_path, [_entity("person", "Dana Example", "per-0001")], cfg)
        surface = pii.surface_class_for_page_class("person", cfg)
        got = pii.read_entity(
            tmp_path, cfg, "per-0001", surface_class=surface, include_excluded=True
        )
        assert got is not None and MARKER in got.body
        shut = pii.read_entity(tmp_path, cfg, "per-0001", surface_class=surface)
        assert shut is not None and shut.withheld is True
        via_mcp = json.loads(
            entity_read(
                tmp_path, "per-0001", page_class="person", include_excluded=True, config=cfg
            )
        )
        assert MARKER in via_mcp["body"]

    def test_mcp_entity_read_reaches_a_vault_page(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        cfg = _cfg()
        open_ = json.loads(
            entity_read(
                tmp_path, "ref-0001", page_class="reference", include_excluded=True, config=cfg
            )
        )
        assert MARKER in open_["body"]
        shut = json.loads(entity_read(tmp_path, "ref-0001", page_class="reference", config=cfg))
        assert shut["withheld"] is True and MARKER not in json.dumps(shut)
        restricted = json.loads(
            entity_read(
                tmp_path,
                "ref-0001",
                page_class="reference",
                include_excluded=True,
                caller_audience={"team"},
                config=cfg,
            )
        )
        assert MARKER not in json.dumps(restricted)
        missing = json.loads(entity_read(tmp_path, "nope", page_class="reference", config=cfg))
        assert missing["ok"] is False


class TestLinkRule:
    def test_a_link_from_the_main_store_to_a_vault_page_is_refused(self, tmp_path: Path) -> None:
        cfg = _cfg()
        _compile(tmp_path, [_entity()], cfg)
        wiki = tmp_path / "wiki"
        page = wiki / "main-page.md"
        text = (
            "---\nname: Main\ntype: concept\nuid: m-1\nrelated:\n- uid: ref-0001\n---\n\nBody\n"
        )
        assert admit_page_write(wiki, page, text, config=cfg, source="test") is False
        assert not page.exists()
        by_name = "---\nname: Main\ntype: concept\nuid: m-1\n---\n\nSee [[Locker Combination]].\n"
        assert admit_page_write(wiki, page, by_name, config=cfg, source="test") is False

    def test_unrelated_links_and_vault_to_vault_links_are_admitted(self, tmp_path: Path) -> None:
        cfg = _cfg()
        _compile(tmp_path, [_entity()], cfg)
        wiki = tmp_path / "wiki"
        ok = "---\nname: Main\ntype: concept\nuid: m-1\n---\n\nSee [[Somewhere Else]].\n"
        assert admit_page_write(wiki, wiki / "m.md", ok, config=cfg, source="test") is True
        vault_page = tmp_path / "vault" / "other.md"
        inside = "---\nname: Other\nuid: v-2\nrelated:\n- uid: ref-0001\n---\n\nx\n"
        assert admit_page_write(wiki, vault_page, inside, config=cfg, source="test") is True

    def test_no_vault_configured_changes_nothing(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        text = "---\nname: Main\nuid: m-1\nrelated:\n- uid: x\n---\n\nBody\n"
        assert admit_page_write(wiki, wiki / "m.md", text, config={}, source="test") is True


def test_record_placement_places_a_record_about_a_vault_page_in_its_vault(
    tmp_path: Path,
) -> None:
    # The page is in no index, but it is one entity: a record about it goes to
    # the vault its type is routed to, never "unplaceable".
    cfg = _cfg()
    _compile(tmp_path, [_entity()], cfg)
    got = pii.record_placement(tmp_path, cfg, "ref-0001")
    assert (got.kind, got.targets) == ("vault", ("vault",))
    assert pii.record_placement(tmp_path, cfg, "no-such-uid", field="x").kind == "unplaceable"
