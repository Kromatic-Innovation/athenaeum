# SPDX-License-Identifier: Apache-2.0
"""Compile writes a new page to the surface its type maps to (athenaeum#2618).

Entity creation (``_apply_tier3_results``), batch apply and page recompile ask
``storage.page_write_root_for_class`` where a type lives and write there. A
type mapped to a surface that is not indexed is refused with a named error. A
structured upsert rewrites the page where the locator found it. A deployment
with no mapping writes the same bytes to the same paths as before.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from athenaeum.librarian import _apply_tier3_results, tier0_structured_upsert
from athenaeum.mcp_server import recall_search
from athenaeum.models import (
    EntityIndex,
    ProcessingResult,
    RawFile,
    WikiEntity,
    parse_frontmatter,
)
from athenaeum.page_recompile_apply import write_apply
from athenaeum.search import get_backend
from athenaeum.storage import (
    StorageConfigError,
    SurfaceNotIndexedError,
    open_store,
    page_write_root_for_class,
    resolve_indexed_surfaces,
)

RECIPES_CFG: dict[str, Any] = {
    "storage": {
        "adapters": {
            "recipes": {
                "backing_store": "markdown",
                "surface_root": "recipes",
                "corpus_policy": {"embedded": True, "recallable": True},
            },
        },
        "mapping": {"concept": "recipes", "recipe": "recipes", "reference": "excluded"},
    },
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


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


def _entity(etype: str, name: str = "Pistachio Loaf") -> WikiEntity:
    return WikiEntity(
        uid="recipe-0001",
        type=etype,
        name=name,
        access="open",
        created="2026-01-01",
        updated="2026-01-01",
        body=f"# {name}\n\nPistachio zephyrmarker loaf.\n",
        source="script:test",
    )


def _compile(tmp_path: Path, entity: WikiEntity, config: dict[str, Any] | None) -> EntityIndex:
    wiki = tmp_path / "wiki"
    wiki.mkdir(exist_ok=True)
    index = EntityIndex(wiki)
    _apply_tier3_results(
        ProcessingResult(raw_file=_raw(tmp_path)),
        new_entities=[entity],
        pending_updates=[],
        updated_uids=[],
        escalations=[],
        wiki_root=wiki,
        index=index,
        config=config,
        raw=_raw(tmp_path),
    )
    return index


class TestPageWriteRoot:
    def test_unmapped_class_is_the_wiki(self, tmp_path: Path) -> None:
        assert page_write_root_for_class("company", None, tmp_path) == tmp_path / "wiki"
        assert page_write_root_for_class("company", RECIPES_CFG, tmp_path) == tmp_path / "wiki"
        assert page_write_root_for_class(None, RECIPES_CFG, tmp_path) == tmp_path / "wiki"

    def test_mapped_indexed_class_is_its_surface(self, tmp_path: Path) -> None:
        assert page_write_root_for_class("concept", RECIPES_CFG, tmp_path) == tmp_path / "recipes"

    def test_non_indexed_surface_is_refused_by_name(self, tmp_path: Path) -> None:
        with pytest.raises(SurfaceNotIndexedError) as exc:
            page_write_root_for_class("reference", RECIPES_CFG, tmp_path)
        assert isinstance(exc.value, StorageConfigError)
        assert "excluded" in str(exc.value)


class TestEntityCreation:
    def test_page_lands_on_surface_is_snapshotted_and_recalled(self, tmp_path: Path) -> None:
        # The surface is its own git repo (S2): the snapshot commits it there.
        recipes = tmp_path / "recipes"
        recipes.mkdir()
        _git(recipes, "init", "-q", "-b", "main")
        _git(recipes, "config", "user.email", "ada@example.com")
        _git(recipes, "config", "user.name", "Ada Example")
        _git(recipes, "commit", "-q", "--allow-empty", "-m", "seed")

        entity = _entity("concept")
        index = _compile(tmp_path, entity, RECIPES_CFG)

        page = recipes / entity.filename
        assert page.is_file()
        assert not (tmp_path / "wiki" / entity.filename).exists()
        assert index.get_by_uid(entity.uid) == page

        before = _git(recipes, "rev-parse", "HEAD")
        open_store(RECIPES_CFG, tmp_path).snapshot("compile")
        assert _git(recipes, "rev-parse", "HEAD") != before
        assert entity.filename in _git(recipes, "show", "--name-only", "--format=", "HEAD")

        # Unrelated pages: bm25 scores a rare term ~0 in a corpus this small.
        for i in range(5):
            (tmp_path / "wiki" / f"other{i}.md").write_text(
                f"---\nname: other{i}\ntype: note\n---\n\nUnrelated wiki page {i}.\n",
                encoding="utf-8",
            )
        roots = resolve_indexed_surfaces(tmp_path, RECIPES_CFG, extra_roots=[])
        cache = tmp_path / "cache"
        get_backend("fts5").build_index(
            tmp_path / "wiki", cache, extra_roots=roots, config=RECIPES_CFG
        )
        rendered = recall_search(
            tmp_path / "wiki",
            "zephyrmarker",
            top_k=3,
            search_backend="fts5",
            cache_dir=cache,
            extra_roots=roots,
            config=RECIPES_CFG,
        )
        assert "zephyrmarker" in rendered
        assert f"recipes/{entity.filename}" in rendered

    def test_non_indexed_surface_is_refused_and_nothing_is_written(self, tmp_path: Path) -> None:
        entity = _entity("reference", "Vault Thing")
        with pytest.raises(SurfaceNotIndexedError):
            _compile(tmp_path, entity, RECIPES_CFG)
        assert not list(tmp_path.rglob(entity.filename))

    def test_no_mapping_writes_identical_bytes_to_the_same_path(self, tmp_path: Path) -> None:
        plain, mapped = tmp_path / "plain", tmp_path / "mapped"
        plain.mkdir()
        mapped.mkdir()
        entity = _entity("company")
        _compile(plain, entity, None)
        _compile(mapped, entity, RECIPES_CFG)  # a mapping for OTHER types only
        def stable(root: Path) -> bytes:
            # ``recorded_at`` is stamped from the wall clock on every write.
            lines = (root / "wiki" / entity.filename).read_text(encoding="utf-8").splitlines()
            return "\n".join(ln for ln in lines if not ln.startswith("recorded_at:")).encode()

        assert stable(plain) == stable(mapped)
        for root in (plain, mapped):
            assert sorted(p.relative_to(root).as_posix() for p in (root / "wiki").glob("*")) == [
                f"wiki/{entity.filename}"
            ]
        assert not (mapped / "recipes").exists()


class TestStructuredUpsertStaysWhereLocated:
    def test_upsert_rewrites_the_page_on_its_surface(self, tmp_path: Path) -> None:
        cfg: dict[str, Any] = {
            **RECIPES_CFG,
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
        (tmp_path / "wiki").mkdir()
        page = tmp_path / "recipes" / "loaf.md"
        page.parent.mkdir()
        page.write_text(
            "---\nuid: recipe-loaf\ntype: recipe\nname: Loaf\ntags:\n  - base\n---\n\n"
            "# Loaf\n\nbody\n",
            encoding="utf-8",
        )
        raw_path = tmp_path / "raw" / "20260201T000000Z-aabbccdd.md"
        raw_path.parent.mkdir()
        content = (
            "---\nuid: recipe-loaf\ntype: recipe\nname: Loaf\ntags:\n  - extra\n---\n\n"
            "## Notes\n\nproof overnight\n"
        )
        raw_path.write_text(content, encoding="utf-8")
        raw = RawFile(
            path=raw_path, source="sessions", timestamp="20260201T000000Z",
            uuid8="aabbccdd", _content=content,
        )
        index = EntityIndex(tmp_path / "wiki", [s for s in resolve_indexed_surfaces(
            tmp_path, cfg, extra_roots=[])])
        outcome = tier0_structured_upsert(raw, index, tmp_path / "wiki", {"recipe"}, config=cfg)
        assert outcome is not None
        meta, body = parse_frontmatter(page.read_text(encoding="utf-8"))
        assert "extra" in meta["tags"]
        assert "proof overnight" in body
        assert not list((tmp_path / "wiki").glob("*.md"))


class TestPageRecompile:
    def _setup(self, tmp_path: Path, etype: str) -> Path:
        from tests.test_page_recompile_apply import _simple_plan, _write_page

        wiki = tmp_path / "knowledge" / "wiki"
        _write_page(wiki, uid="page0001", name="Acme", body=_body(), type=etype)
        _simple_plan(wiki, body=_body())
        return wiki

    def test_sub_page_lands_on_the_surface_its_type_maps_to(self, tmp_path: Path) -> None:
        cfg = _cfg("reference", "recipes")
        wiki = self._setup(tmp_path, "reference")
        outcome = write_apply(wiki, uid="page0001", client=None, config=cfg)
        assert outcome.ok, outcome.issues
        sub_uid = outcome.created_uids[0]
        assert next((wiki.parent / "recipes").glob(f"{sub_uid}*.md"))
        assert not list(wiki.glob(f"{sub_uid}*.md"))

    def test_non_indexed_surface_is_refused_as_a_plan_issue(self, tmp_path: Path) -> None:
        cfg = _cfg("reference", "excluded")
        wiki = self._setup(tmp_path, "reference")
        before = sorted(p.name for p in wiki.iterdir())
        outcome = write_apply(wiki, uid="page0001", client=None, config=cfg)
        assert not outcome.ok
        assert any(i.code == "surface-not-indexed" for i in outcome.issues)
        assert sorted(p.name for p in wiki.iterdir()) == before
        assert not (wiki.parent / "excluded").exists()

    def test_no_mapping_writes_the_sub_page_into_wiki(self, tmp_path: Path) -> None:
        wiki = self._setup(tmp_path, "reference")
        outcome = write_apply(wiki, uid="page0001", client=None, config=_cfg("other", "recipes"))
        assert outcome.ok, outcome.issues
        assert next(wiki.glob(f"{outcome.created_uids[0]}*.md"))
        assert not (wiki.parent / "recipes").exists()


def _body() -> str:
    from tests.test_page_recompile_apply import _BODY

    return _BODY


def _cfg(etype: str, surface: str) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "storage": {
            "adapters": {
                "recipes": {
                    "backing_store": "markdown",
                    "surface_root": "recipes",
                    "corpus_policy": {"embedded": True},
                }
            },
            "mapping": {etype: surface},
        }
    }
    return cfg


class TestBatchApply:
    def _run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        yaml_cfg: str | None,
    ) -> Path:
        from athenaeum import librarian  # noqa: F401
        from tests import test_batch_mode as tbm

        root = tbm._seed_root(tmp_path, name, ["Standalone fact about WidgetAlpha gadget.\n"])
        if yaml_cfg is not None:
            (root / "athenaeum.yaml").write_text(yaml_cfg, encoding="utf-8")
        tbm._clean_env(monkeypatch)
        tbm._freeze_recorded_at(monkeypatch)
        client = tbm._FakeClient(tbm._scripted_responder, allow_sync=False)
        monkeypatch.setattr(tbm.anthropic_mod, "Anthropic", lambda **kw: client)
        tbm._patch_uids(monkeypatch)
        rc = tbm.run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert rc == 0
        return root

    _SURFACE_YAML = (
        "storage:\n  adapters:\n    notes:\n      backing_store: markdown\n"
        "      surface_root: notes\n      corpus_policy:\n        embedded: true\n"
        "  mapping:\n    concept: notes\n"
    )

    def test_created_page_lands_on_the_mapped_surface(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = self._run(tmp_path, monkeypatch, "mapped", self._SURFACE_YAML)
        assert list((root / "notes").glob("*.md"))
        assert not [p for p in (root / "wiki").glob("*.md") if "widget" in p.name.lower()]

    def test_unrelated_mapping_is_byte_identical_to_no_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = self._run(tmp_path, monkeypatch, "plain", None)
        other = self._run(
            tmp_path,
            monkeypatch,
            "other",
            self._SURFACE_YAML.replace("concept: notes", "recipe: notes"),
        )

        def snap(root: Path) -> dict[str, bytes]:
            return {
                p.relative_to(root).as_posix(): p.read_bytes()
                for p in (root / "wiki").rglob("*.md")
            }

        assert snap(plain) and snap(plain) == snap(other)
        assert not (other / "notes").exists()


