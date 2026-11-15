# SPDX-License-Identifier: Apache-2.0
"""One page locator across indexed surfaces (athenaeum#2617).

``wiki/`` plus the configured indexed storage surfaces are one lookup space for
``EntityIndex``, the single-uid cache readers, ``read_entity``, recall link
rendering and the structured upsert. A surface that is not indexed is never
visible, and a uid on two surfaces is a loud, named error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum.entity_index_cache import (
    entity_index_for_uid,
    load_entity_index,
    locate_entity_page,
)
from athenaeum.librarian import tier0_structured_upsert
from athenaeum.mcp_server import _LinkNameIndex, entity_read, recall_search
from athenaeum.models import (
    AmbiguousPageUidError,
    EntityIndex,
    PageLocator,
    RawFile,
    parse_frontmatter,
)
from athenaeum.search import get_backend
from athenaeum.storage import resolve_locator_surfaces

CONFIG: dict[str, Any] = {
    "storage": {
        "adapters": {
            "recipes": {
                "backing_store": "markdown",
                "surface_root": "recipes",
                "corpus_policy": {"embedded": True},
            },
            "vault": {
                "backing_store": "markdown",
                "surface_root": "vault",
                "corpus_policy": {"embedded": False},
            },
        },
    },
    "librarian": {
        "structured_upsert": {
            "recipe": {
                "sections": ["Notes"],
                "append_sections": ["Notes"],
                "union_keys": ["tags"],
            }
        }
    },
}


def _write(path: Path, uid: str, name: str, body: str = "body", typ: str = "recipe") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nuid: {uid}\ntype: {typ}\nname: {name}\ntags:\n  - base\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir()
    return tmp_path


class TestLocator:
    def test_resolves_uid_and_name_on_an_indexed_surface(self, kb: Path) -> None:
        page = _write(kb / "recipes" / "loaf.md", "recipe-loaf", "Pistachio Loaf")
        _write(kb / "wiki" / "w.md", "concept-w", "Wiki Thing", typ="concept")
        surfaces = resolve_locator_surfaces(kb, CONFIG)
        loc = PageLocator(kb / "wiki", surfaces)
        assert loc.locate_uid("recipe-loaf") == page.resolve()
        assert loc.locate_name("pistachio loaf") == page.resolve()
        assert loc.locate_uid("concept-w") == kb / "wiki" / "w.md"
        assert loc.surface_of(page.resolve()) == "recipes"
        assert loc.surface_of(kb / "wiki" / "w.md") == "wiki"

    def test_non_indexed_surface_is_never_returned(self, kb: Path) -> None:
        _write(kb / "vault" / "secret.md", "recipe-secret", "Secret Sauce")
        _write(kb / "recipes" / "ok.md", "recipe-ok", "Fine")
        surfaces = resolve_locator_surfaces(kb, CONFIG)
        assert [s.prefix for s in surfaces] == ["recipes"]
        loc = PageLocator(kb / "wiki", surfaces)
        assert loc.locate_uid("recipe-secret") is None
        assert loc.locate_name("Secret Sauce") is None
        assert EntityIndex(kb / "wiki", surfaces).get_by_uid("recipe-secret") is None

    def test_same_uid_on_two_surfaces_is_a_named_error(self, kb: Path) -> None:
        _write(kb / "wiki" / "a.md", "recipe-dup", "Dup One")
        _write(kb / "recipes" / "b.md", "recipe-dup", "Dup Two")
        surfaces = resolve_locator_surfaces(kb, CONFIG)
        with pytest.raises(AmbiguousPageUidError) as exc:
            EntityIndex(kb / "wiki", surfaces)
        assert exc.value.uid == "recipe-dup"
        assert set(exc.value.locations) == {"wiki", "recipes"}
        assert "recipe-dup" in str(exc.value)
        with pytest.raises(AmbiguousPageUidError):
            PageLocator(kb / "wiki", surfaces).locate_uid("recipe-dup")
        with pytest.raises(AmbiguousPageUidError):
            locate_entity_page(kb / "wiki", "recipe-dup", cache_dir=kb / "cache", surfaces=surfaces)

    def test_surface_page_never_displaces_a_wiki_name(self, kb: Path) -> None:
        # Seer review on PR athenaeum#2678: a surface page that shares a name or
        # alias with an existing wiki page must not re-route that lookup.
        wiki = _write(kb / "wiki" / "w.md", "concept-w", "Shared Name", typ="concept")
        _write(kb / "recipes" / "r.md", "recipe-r", "Shared Name")
        surfaces = resolve_locator_surfaces(kb, CONFIG)
        idx = EntityIndex(kb / "wiki", surfaces)
        hit = idx.lookup("shared name")
        assert hit is not None and hit.path == wiki
        assert idx.get_by_uid("recipe-r") is not None  # still reachable by uid
        cached = load_entity_index(kb / "wiki", cache_dir=kb / "cache", surfaces=surfaces)
        hit = cached.lookup("shared name")
        assert hit is not None and hit.path == wiki

    def test_no_extra_surfaces_behaves_as_before(self, kb: Path) -> None:
        _write(kb / "wiki" / "a.md", "recipe-a", "A")
        _write(kb / "recipes" / "b.md", "recipe-b", "B")  # not configured
        assert resolve_locator_surfaces(kb, {}) == []
        idx = EntityIndex(kb / "wiki")
        assert idx.get_by_uid("recipe-a") == kb / "wiki" / "a.md"
        assert idx.get_by_uid("recipe-b") is None
        assert len(idx) == 1

    def test_cached_index_covers_surfaces(self, kb: Path) -> None:
        page = _write(kb / "recipes" / "loaf.md", "recipe-loaf", "Pistachio Loaf")
        surfaces = resolve_locator_surfaces(kb, CONFIG)
        cache = kb / "cache"
        for _ in range(2):  # cold, then warm
            idx = load_entity_index(kb / "wiki", cache, surfaces)
            assert idx.get_by_uid("recipe-loaf") == page.resolve()
        by_uid = entity_index_for_uid(
            kb / "wiki", "recipe-loaf", cache_dir=cache, surfaces=surfaces
        )
        assert by_uid.get_by_uid("recipe-loaf") == page.resolve()
        found = locate_entity_page(
            kb / "wiki", "recipe-loaf", cache_dir=cache, surfaces=surfaces
        )
        assert found == page.resolve()


class TestReadPath:
    def test_read_entity_finds_a_surface_page(self, kb: Path) -> None:
        _write(kb / "recipes" / "loaf.md", "recipe-loaf", "Pistachio Loaf")
        out = json.loads(entity_read(kb, "recipe-loaf", page_class="recipe", config=CONFIG))
        assert out.get("ok") is not False, out
        assert out["frontmatter"]["name"] == "Pistachio Loaf"

    def test_read_entity_ignores_a_non_indexed_surface(self, kb: Path) -> None:
        _write(kb / "vault" / "secret.md", "recipe-secret", "Secret Sauce")
        out = json.loads(entity_read(kb, "recipe-secret", page_class="recipe", config=CONFIG))
        assert out["ok"] is False

    def test_read_entity_names_a_cross_surface_uid(self, kb: Path) -> None:
        _write(kb / "wiki" / "a.md", "recipe-dup", "Dup One")
        _write(kb / "recipes" / "b.md", "recipe-dup", "Dup Two")
        out = json.loads(entity_read(kb, "recipe-dup", page_class="recipe", config=CONFIG))
        assert out["ok"] is False and "recipe-dup" in out["error"]
        assert "more than one indexed surface" in out["error"]

    def test_type_filtered_recall_returns_a_surface_page(self, kb: Path) -> None:
        _write(
            kb / "recipes" / "loaf.md",
            "project-loaf",
            "Zephyrloaf Pistachio",
            body="zephyrloaf",
            typ="project",
        )
        _write(kb / "wiki" / "c.md", "concept-c", "Zephyrloaf Other", typ="concept")
        surfaces = resolve_locator_surfaces(kb, CONFIG)
        cache = kb / "cache"
        get_backend("fts5").build_index(kb / "wiki", cache, extra_roots=surfaces)
        rendered = recall_search(
            kb / "wiki",
            "zephyrloaf",
            top_k=5,
            search_backend="fts5",
            cache_dir=cache,
            extra_roots=[*surfaces],
            config=CONFIG,
            type_filter="project",
        )
        assert "Zephyrloaf Pistachio" in rendered
        assert "Zephyrloaf Other" not in rendered

    def test_link_names_resolve_a_surface_target(self, kb: Path) -> None:
        _write(kb / "recipes" / "recipe-loaf-pistachio.md", "recipe-loaf", "Pistachio Loaf")
        surfaces = resolve_locator_surfaces(kb, CONFIG)
        assert _LinkNameIndex(kb / "wiki", surfaces).name_for("recipe-loaf") == "Pistachio Loaf"
        assert _LinkNameIndex(kb / "wiki").name_for("recipe-loaf") is None


class TestStructuredUpsert:
    def test_upsert_finds_and_updates_a_surface_page(self, kb: Path) -> None:
        page = _write(kb / "recipes" / "loaf.md", "recipe-loaf", "Pistachio Loaf")
        raw_path = kb / "raw" / "r.md"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text(
            "---\nuid: recipe-loaf\ntype: recipe\nname: Pistachio Loaf\ntags:\n  - new\n---\n\n"
            "## Notes\n\nTry less sugar.\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="r", timestamp="", uuid8="")
        index = EntityIndex(kb / "wiki", resolve_locator_surfaces(kb, CONFIG))
        out = tier0_structured_upsert(raw, index, kb / "wiki", ["recipe", "concept"], config=CONFIG)
        assert out is not None and out.status == "applied", out
        meta, body = parse_frontmatter(page.read_text(encoding="utf-8"))
        assert meta["tags"] == ["base", "new"]
        assert "Try less sugar." in body
