# SPDX-License-Identifier: Apache-2.0
"""Per-store opaque page file names (athenaeum#3077).

A ``storage.adapters`` entry with ``opaque_filenames: true`` makes NEW pages on
that surface land as ``<uid>.md`` (no slug of the name). Default off; pages
already on disk keep their names; every uid lookup still resolves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from athenaeum.intake_destination import destination_for_entity
from athenaeum.librarian import _apply_tier3_results
from athenaeum.mcp_server import recall_search
from athenaeum.models import (
    EntityIndex,
    ProcessingResult,
    RawFile,
    WikiEntity,
    parse_frontmatter,
)
from athenaeum.page_relations import resolve_page_meta_by_uid
from athenaeum.pii import read_entity
from athenaeum.push_metrics import opaque_push_id_from_filename
from athenaeum.search import get_backend
from athenaeum.storage import (
    StorageConfigError,
    available_adapters,
    resolve_indexed_surfaces,
    store_opaque_filenames,
)
from athenaeum.uid_collision_lint import scan_uid_collisions

UID = "a1b2c3d4"
NAME = "Jane Example"


def _cfg(opaque: Any = True) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "backing_store": "markdown",
        "surface_root": "hr",
        "corpus_policy": {"embedded": True, "recallable": True},
    }
    if opaque is not None:
        entry["opaque_filenames"] = opaque
    return {
        "storage": {"adapters": {"hr": entry}, "mapping": {"person": "hr", "note": "hr"}}
    }


def _raw(tmp_path: Path) -> RawFile:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "20260201T000000Z-aabbccdd.md"
    path.write_text("source\n", encoding="utf-8")
    return RawFile(
        path=path, source="sessions", timestamp="20260201T000000Z",
        uuid8="aabbccdd", _content="source\n",
    )


def _entity(uid: str = UID, name: str = NAME, etype: str = "person") -> WikiEntity:
    return WikiEntity(
        uid=uid, type=etype, name=name, access="open",
        created="2026-01-01", updated="2026-01-01",
        body=f"# {name}\n\nQuokkamarker onboarding record.\n", source="script:test",
    )


def _compile(tmp_path: Path, entities: list[WikiEntity], cfg: dict[str, Any] | None) -> EntityIndex:
    wiki = tmp_path / "wiki"
    wiki.mkdir(exist_ok=True)
    index = EntityIndex(wiki)
    _apply_tier3_results(
        ProcessingResult(raw_file=_raw(tmp_path)),
        new_entities=entities, pending_updates=[], updated_uids=[], escalations=[],
        wiki_root=wiki, index=index, config=cfg, raw=_raw(tmp_path),
    )
    return index


class TestConfig:
    def test_default_is_off(self) -> None:
        assert available_adapters(_cfg(None))["hr"].opaque_filenames is False
        assert store_opaque_filenames("hr", _cfg(None)) is False

    def test_opt_in(self) -> None:
        assert available_adapters(_cfg(True))["hr"].opaque_filenames is True
        assert store_opaque_filenames("hr", _cfg(True)) is True

    def test_default_wiki_surface_and_unknown_store_are_off(self) -> None:
        assert store_opaque_filenames("personal", _cfg(True)) is False
        assert store_opaque_filenames("ghost", _cfg(True)) is False

    @pytest.mark.parametrize("bad", ["yes", 1, "true", []])
    def test_non_boolean_is_rejected(self, bad: Any) -> None:
        with pytest.raises(StorageConfigError, match="opaque_filenames"):
            available_adapters(_cfg(bad))


class TestFilenameProperty:
    def test_default_name_unchanged(self) -> None:
        assert _entity().filename == f"{UID}-jane-example.md"

    def test_opaque_name_is_uid_only(self) -> None:
        e = _entity()
        e.opaque_filename = True
        assert e.filename == f"{UID}.md"
        assert "jane" not in e.filename

    def test_destination_carries_the_flag(self, tmp_path: Path) -> None:
        assert destination_for_entity("person", _cfg(True), tmp_path).opaque_filenames is True
        assert destination_for_entity("person", _cfg(None), tmp_path).opaque_filenames is False
        assert destination_for_entity("company", _cfg(True), tmp_path).opaque_filenames is False


class TestWrite:
    def test_opted_in_store_writes_uid_only_name(self, tmp_path: Path) -> None:
        index = _compile(tmp_path, [_entity()], _cfg(True))
        page = tmp_path / "hr" / f"{UID}.md"
        assert page.is_file()
        assert not list((tmp_path / "hr").glob("*jane*"))
        assert index.get_by_uid(UID) == page

    def test_opted_out_store_unchanged(self, tmp_path: Path) -> None:
        _compile(tmp_path, [_entity()], _cfg(None))
        assert (tmp_path / "hr" / f"{UID}-jane-example.md").is_file()

    def test_unconfigured_deployment_unchanged(self, tmp_path: Path) -> None:
        _compile(tmp_path, [_entity()], None)
        assert (tmp_path / "wiki" / f"{UID}-jane-example.md").is_file()

    def test_unmapped_type_on_default_surface_keeps_slug(self, tmp_path: Path) -> None:
        _compile(tmp_path, [_entity("b2c3d4e5", "Acme Corp", "company")], _cfg(True))
        assert (tmp_path / "wiki" / "b2c3d4e5-acme-corp.md").is_file()

    def test_existing_page_keeps_its_name_and_is_found_by_uid(self, tmp_path: Path) -> None:
        # A pre-opt-in page on the surface keeps its slugged name; the index
        # resolves it by frontmatter uid, so an update targets the same path.
        hr = tmp_path / "hr"
        hr.mkdir()
        old = hr / f"{UID}-jane-example.md"
        old.write_text(_entity().render(), encoding="utf-8")
        _compile(tmp_path, [_entity("c3d4e5f6", "John Sample")], _cfg(True))
        assert old.is_file()
        assert (hr / "c3d4e5f6.md").is_file()
        cfg = _cfg(True)
        surfaces = resolve_indexed_surfaces(tmp_path, cfg, extra_roots=[])
        index = EntityIndex(tmp_path / "wiki", surfaces)
        assert index.get_by_uid(UID) == old
        assert index.get_by_uid("c3d4e5f6") == hr / "c3d4e5f6.md"


class TestRoundTrip:
    def test_recall_finds_an_opaque_page(self, tmp_path: Path) -> None:
        cfg = _cfg(True)
        _compile(tmp_path, [_entity()], cfg)
        for i in range(5):
            (tmp_path / "wiki" / f"other{i}.md").write_text(
                f"---\nname: other{i}\ntype: note\n---\n\nUnrelated wiki page {i}.\n",
                encoding="utf-8",
            )
        roots = resolve_indexed_surfaces(tmp_path, cfg, extra_roots=[])
        cache = tmp_path / "cache"
        get_backend("fts5").build_index(tmp_path / "wiki", cache, extra_roots=roots, config=cfg)
        rendered = recall_search(
            tmp_path / "wiki", "quokkamarker", top_k=3, search_backend="fts5",
            cache_dir=cache, extra_roots=roots, config=cfg,
        )
        assert "quokkamarker" in rendered.lower()
        assert f"hr/{UID}.md" in rendered
        assert "jane-example" not in rendered

    def test_link_target_resolves_by_uid(self, tmp_path: Path) -> None:
        _compile(tmp_path, [_entity()], _cfg(True))
        meta = resolve_page_meta_by_uid(tmp_path / "hr", UID)
        assert meta is not None and meta["name"] == NAME

    def test_read_entity_resolves_an_opaque_page(self, tmp_path: Path) -> None:
        index = _compile(tmp_path, [_entity()], _cfg(True))
        got = read_entity(tmp_path, _cfg(True), UID, surface_class="person", entity_index=index)
        assert got is not None
        assert got.uid == UID

    def test_push_id_from_opaque_filename_carries_no_name(self) -> None:
        # Recorded whole: a uid-only name is already name-free.
        assert opaque_push_id_from_filename(f"{UID}.md") == f"{UID}.md"


class TestUidCollisionLint:
    def test_lint_reports_a_collision_between_opaque_and_slugged_pages(
        self, tmp_path: Path
    ) -> None:
        hr = tmp_path / "hr"
        hr.mkdir()
        (tmp_path / "wiki").mkdir()
        (hr / f"{UID}.md").write_text(_entity().render(), encoding="utf-8")
        (hr / f"{UID}-jane-example.md").write_text(_entity().render(), encoding="utf-8")
        report = scan_uid_collisions(tmp_path, config=_cfg(True))
        (c,) = report.collisions
        assert c.uid == UID and len(c.pages) == 2

    def test_lint_is_clean_on_opaque_pages(self, tmp_path: Path) -> None:
        _compile(tmp_path, [_entity(), _entity("c3d4e5f6", "John Sample")], _cfg(True))
        assert scan_uid_collisions(tmp_path, config=_cfg(True)).collisions == ()
        text = (tmp_path / "hr" / f"{UID}.md").read_text(encoding="utf-8")
        assert parse_frontmatter(text)[0]["uid"] == UID
