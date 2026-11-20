# SPDX-License-Identifier: Apache-2.0
"""The search index is built from configured storage surfaces (athenaeum#2616).

One resolver (:func:`athenaeum.storage.resolve_indexed_surfaces`) derives the
non-wiki roots the index scans; config resolution refuses overlap with a
surface that is not indexed, and refuses colliding prefixes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from athenaeum.mcp_server import _resolve_hit_path, recall_search
from athenaeum.search import (
    FTS5Backend,
    get_backend,
    query_fts5_index,
    query_vector_index,
)
from athenaeum.storage import (
    IndexedSurface,
    IndexedSurfaceError,
    StorageConfigError,
    resolve_indexed_surfaces,
)


def _page(directory: Path, name: str, marker: str, extra_fm: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        f"---\nname: {name[:-3]}\ntype: note\ndescription: {marker} page\n"
        f"{extra_fm}---\n\nThis page mentions {marker} exactly once.\n"
    )
    return path


def _cfg(adapters: dict[str, Any], extra: list[str] | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {"storage": {"adapters": adapters}}
    if extra is not None:
        cfg["recall"] = {"extra_intake_roots": extra}
    return cfg


def _surface(root: str, embedded: bool) -> dict[str, Any]:
    return {
        "backing_store": "markdown",
        "surface_root": root,
        "corpus_policy": {"embedded": embedded},
    }


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir()
    return tmp_path


def _names(cache: Path, backend: str = "fts5-manifest.json") -> set[str]:
    import json

    return set(json.loads((cache / backend).read_text())["hashes"])


def _stable_manifest_bytes(path: Path) -> bytes:
    """The manifest bytes minus the wall-clock ``last_full_rehash_at`` stamp."""
    import json

    data = json.loads(path.read_text())
    data.pop("last_full_rehash_at", None)
    return json.dumps(data, sort_keys=False).encode()


class TestResolver:
    def test_no_extra_surfaces_is_just_the_extra_roots(self, kb: Path) -> None:
        extra = kb / "raw" / "auto-memory"
        extra.mkdir(parents=True)
        got = resolve_indexed_surfaces(kb, {"recall": {"extra_intake_roots": ["raw/auto-memory"]}})
        assert got == [IndexedSurface(prefix="auto-memory", root=extra.resolve())]

    def test_embedded_surface_uses_surface_name_as_prefix(self, kb: Path) -> None:
        root = kb / "projects-notes"
        root.mkdir()
        cfg = _cfg({"projects": _surface("projects-notes", True)})
        assert resolve_indexed_surfaces(kb, cfg, extra_roots=[]) == [
            IndexedSurface(prefix="projects", root=root.resolve())
        ]

    def test_non_embedded_surface_is_not_indexed(self, kb: Path) -> None:
        (kb / "vault").mkdir()
        cfg = _cfg({"vault": _surface("vault", False)})
        assert resolve_indexed_surfaces(kb, cfg, extra_roots=[]) == []

    def test_surface_also_listed_as_extra_root_is_indexed_once(self, kb: Path) -> None:
        root = kb / "shared"
        root.mkdir()
        cfg = _cfg({"shared": _surface("shared", True)}, extra=["shared"])
        assert [s.prefix for s in resolve_indexed_surfaces(kb, cfg)] == ["shared"]

    def test_non_embedded_surface_rooted_at_wiki_is_accepted(self, kb: Path) -> None:
        # The athenaeum#532 shape: a non-embedded surface whose root IS wiki/,
        # excluded per page class by the scan-time is_embedded filter. It must
        # not be refused as an overlap with the default wiki surface.
        cfg = _cfg({"notes-unembedded": _surface("wiki", False)})
        assert resolve_indexed_surfaces(kb, cfg, extra_roots=[]) == []

    def test_non_embedded_surface_inside_wiki_is_accepted(self, kb: Path) -> None:
        (kb / "wiki" / "private").mkdir()
        cfg = _cfg({"private": _surface("wiki/private", False)})
        assert resolve_indexed_surfaces(kb, cfg, extra_roots=[]) == []

    def test_embedded_surface_inside_wiki_is_not_listed_twice(self, kb: Path) -> None:
        # The wiki walk already scans wiki/recipes flat under bare names.
        (kb / "wiki" / "recipes").mkdir()
        cfg = _cfg({"recipes": _surface("wiki/recipes", True)})
        assert resolve_indexed_surfaces(kb, cfg, extra_roots=[]) == []


class TestExclusionGuards:
    def test_extra_root_equal_to_excluded_surface_refused(self, kb: Path) -> None:
        (kb / "excluded").mkdir()
        cfg = _cfg({}, extra=["excluded"])
        with pytest.raises(IndexedSurfaceError):
            resolve_indexed_surfaces(kb, cfg)

    def test_extra_root_equal_to_custom_excluded_root_refused(self, kb: Path) -> None:
        (kb / "contacts").mkdir()
        cfg = _cfg({"contacts": _surface("contacts", False)}, extra=["contacts"])
        with pytest.raises(IndexedSurfaceError):
            resolve_indexed_surfaces(kb, cfg)

    def test_indexed_root_contains_excluded_root_refused(self, kb: Path) -> None:
        (kb / "area" / "private").mkdir(parents=True)
        cfg = _cfg(
            {"area": _surface("area", True), "private": _surface("area/private", False)}
        )
        with pytest.raises(IndexedSurfaceError):
            resolve_indexed_surfaces(kb, cfg)

    def test_indexed_root_inside_excluded_root_refused(self, kb: Path) -> None:
        (kb / "vault" / "open").mkdir(parents=True)
        cfg = _cfg({"vault": _surface("vault", False), "open": _surface("vault/open", True)})
        with pytest.raises(IndexedSurfaceError):
            resolve_indexed_surfaces(kb, cfg)

    def test_extra_root_containing_excluded_root_refused(self, kb: Path) -> None:
        (kb / "excluded").mkdir()
        cfg = _cfg({}, extra=["."])
        with pytest.raises(IndexedSurfaceError):
            resolve_indexed_surfaces(kb, cfg)

    def test_error_is_a_storage_config_error(self) -> None:
        assert issubclass(IndexedSurfaceError, StorageConfigError)

    def test_same_prefix_refused(self, kb: Path) -> None:
        (kb / "a" / "notes").mkdir(parents=True)
        (kb / "b" / "notes").mkdir(parents=True)
        cfg = _cfg({}, extra=["a/notes", "b/notes"])
        with pytest.raises(IndexedSurfaceError, match="share the index prefix"):
            resolve_indexed_surfaces(kb, cfg)

    def test_extra_root_basename_colliding_with_surface_name_refused(self, kb: Path) -> None:
        (kb / "one").mkdir()
        (kb / "elsewhere" / "ledger").mkdir(parents=True)
        cfg = _cfg({"ledger": _surface("one", True)}, extra=["elsewhere/ledger"])
        with pytest.raises(IndexedSurfaceError):
            resolve_indexed_surfaces(kb, cfg)


class TestIndexContents:
    def test_excluded_page_never_enters_either_index(self, kb: Path) -> None:
        pytest.importorskip("chromadb")  # the vector half needs the real backend
        _page(kb / "wiki", "ok.md", "publicmarker")
        _page(kb / "excluded", "secret.md", "hushmarker")
        cfg = _cfg({})
        roots = resolve_indexed_surfaces(kb, cfg, extra_roots=[])
        for backend in ("fts5", "vector"):
            cache = kb / f"cache-{backend}"
            get_backend(backend).build_index(kb / "wiki", cache, extra_roots=roots, config=cfg)
        assert query_fts5_index("hushmarker", kb / "cache-fts5") == []
        hits = query_vector_index("hushmarker", kb / "cache-vector", n=5)
        assert all("secret" not in name for name, *_ in hits)
        assert _names(kb / "cache-fts5") == {"ok.md"}
        assert _names(kb / "cache-vector", "vector-manifest.json") == {"ok.md"}

    def test_pii_flag_second_layer_on_indexed_surface(self, kb: Path) -> None:
        _page(kb / "notes", "flagged.md", "flagmarker", extra_fm="pii: true\n")
        cfg = _cfg({"notes": _surface("notes", True)})
        roots = resolve_indexed_surfaces(kb, cfg, extra_roots=[])
        FTS5Backend().build_index(kb / "wiki", kb / "cache", extra_roots=roots, config=cfg)
        assert query_fts5_index("flagmarker", kb / "cache") == []

    @pytest.mark.parametrize("backend", ["fts5", "vector"])
    def test_indexed_surface_outside_wiki_is_indexed_and_recalled(
        self, kb: Path, backend: str
    ) -> None:
        if backend == "vector":
            pytest.importorskip("chromadb")
        _page(kb / "wiki", "ok.md", "publicmarker")
        _page(kb / "projects-notes" / "sub", "roadmap.md", "zephyrmarker")
        cfg = _cfg({"projects": _surface("projects-notes", True)})
        roots = resolve_indexed_surfaces(kb, cfg, extra_roots=[])
        cache = kb / "cache"
        get_backend(backend).build_index(kb / "wiki", cache, extra_roots=roots, config=cfg)
        manifest = "fts5-manifest.json" if backend == "fts5" else "vector-manifest.json"
        assert "projects/sub/roadmap.md" in _names(cache, manifest)
        rendered = recall_search(
            kb / "wiki",
            "zephyrmarker",
            top_k=3,
            search_backend=backend,
            cache_dir=cache,
            extra_roots=roots,
            config=cfg,
        )
        assert "zephyrmarker" in rendered
        assert "projects/sub/roadmap.md" in rendered

    def test_hit_path_resolves_by_surface_prefix_not_folder_name(self, kb: Path) -> None:
        root = kb / "projects-notes"
        root.mkdir()
        surfaces = [IndexedSurface(prefix="projects", root=root)]
        path, label = _resolve_hit_path("projects/a.md", kb / "wiki", surfaces)
        assert path == root / "a.md"
        assert label == "projects/a.md"
        assert _resolve_hit_path("projects-notes/a.md", kb / "wiki", surfaces)[0] is None
        # A bare Path keeps the historical folder-basename prefix.
        assert _resolve_hit_path("projects-notes/a.md", kb / "wiki", [root])[0] == root / "a.md"


class TestByteIdenticalManifestWithoutSurfaces:
    @pytest.mark.parametrize("backend", ["fts5", "vector"])
    def test_manifest_unchanged_by_the_resolver(self, kb: Path, backend: str) -> None:
        if backend == "vector":
            pytest.importorskip("chromadb")
        _page(kb / "wiki", "alpha.md", "alphamarker")
        extra = kb / "raw" / "auto-memory"
        _page(extra, "note.md", "notemarker")
        cfg: dict[str, Any] = {"recall": {"extra_intake_roots": ["raw/auto-memory"]}}
        manifest = "fts5-manifest.json" if backend == "fts5" else "vector-manifest.json"

        legacy = kb / "cache-legacy"
        get_backend(backend).build_index(
            kb / "wiki", legacy, extra_roots=[extra.resolve()], config=cfg
        )
        resolved = kb / "cache-resolved"
        roots = resolve_indexed_surfaces(kb, cfg)
        get_backend(backend).build_index(kb / "wiki", resolved, extra_roots=roots, config=cfg)

        assert _stable_manifest_bytes(legacy / manifest) == _stable_manifest_bytes(
            resolved / manifest
        )
        assert _names(resolved, manifest) == {"alpha.md", "auto-memory/note.md"}


class TestReloadedClassIsStillASurface:
    """A test that reloads ``athenaeum.storage`` mints a second
    ``IndexedSurface`` class; the normalizers must not turn an instance of it
    into a bad ``Path`` (seen as an order-dependent full-suite failure)."""

    def test_lookalike_surface_passes_through_as_indexed_surface(self, tmp_path: Path) -> None:
        from dataclasses import dataclass

        from athenaeum.storage import as_indexed_surface

        @dataclass(frozen=True)
        class OtherIndexedSurface:  # stands in for the reloaded class
            prefix: str
            root: Path

        item = OtherIndexedSurface(prefix="recipes", root=tmp_path)
        assert as_indexed_surface(item) is item  # type: ignore[arg-type]
        assert as_indexed_surface(tmp_path) == IndexedSurface(prefix=tmp_path.name, root=tmp_path)
        assert as_indexed_surface(str(tmp_path)).root == tmp_path  # type: ignore[arg-type]

    def test_build_fts5_index_accepts_a_lookalike_surface(self, kb: Path) -> None:
        from dataclasses import dataclass

        from athenaeum.search import build_fts5_index

        @dataclass(frozen=True)
        class OtherIndexedSurface:
            prefix: str
            root: Path

        root = kb / "notes-store"
        _page(root, "n1.md", "zebrafinch")
        cache = kb / "cache"
        build_fts5_index(
            kb / "wiki",
            cache,
            extra_roots=[OtherIndexedSurface(prefix="notes", root=root)],  # type: ignore[list-item]
        )
        assert "notes/n1.md" in _names(cache)
