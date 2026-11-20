# SPDX-License-Identifier: Apache-2.0
"""The index is separable by surface (athenaeum#2990, federation-store-access §9).

Every derived row (FTS5 search row, vector embedding) records the surface it
came from; one surface's derived data can be listed and removed without
reading or rewriting another surface's rows.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from athenaeum.search import (
    _DB_NAME,
    _FTS5_MANIFEST,
    _VECTOR_DIR,
    _VECTOR_MANIFEST,
    FTS5Backend,
    VectorBackend,
    list_surface_derived_data,
    remove_surface_derived_data,
    surface_of,
)


def _page(root: Path, rel: str, name: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")


@pytest.fixture()
def corpus(tmp_path: Path) -> dict[str, Path]:
    wiki = tmp_path / "wiki"
    notes = tmp_path / "notes"
    shared = tmp_path / "shared"
    # ``alice`` exists on the wiki AND on two other surfaces (same name/stem).
    _page(wiki, "alice.md", "Alice Quartz", "wikiquartz lives here")
    _page(wiki, "only-wiki.md", "Wiki Only", "wikionlyword")
    _page(notes, "alice.md", "Alice Quartz", "notesquartz lives here")
    _page(notes, "only-notes.md", "Notes Only", "notesonlyword")
    _page(shared, "alice.md", "Alice Quartz", "sharedquartz lives here")
    return {"wiki": wiki, "notes": notes, "shared": shared, "cache": tmp_path / "cache"}


def _build(c: dict[str, Path], **kw: Any) -> FTS5Backend:
    backend = FTS5Backend()
    backend.build_index(c["wiki"], c["cache"], extra_roots=[c["notes"], c["shared"]], **kw)
    return backend


def _rows(cache: Path, where: str = "1", params: tuple[str, ...] = ()) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(cache / _DB_NAME))
    try:
        return conn.execute(
            f"SELECT rowid, * FROM wiki WHERE {where} ORDER BY rowid", params
        ).fetchall()
    finally:
        conn.close()


def _recall(backend: FTS5Backend, cache: Path, q: str) -> list[tuple[str, str, float]]:
    return backend.query(q, cache, n=20)


def test_surface_of() -> None:
    assert surface_of("alice.md") == "wiki"
    assert surface_of("notes/sub/alice.md") == "notes"


def test_every_row_records_its_surface_and_can_be_listed(corpus: dict[str, Path]) -> None:
    backend = _build(corpus)
    cache = corpus["cache"]
    assert backend.list_surface(cache, "wiki") == ["alice.md", "only-wiki.md"]
    assert backend.list_surface(cache, "notes") == ["notes/alice.md", "notes/only-notes.md"]
    assert backend.list_surface(cache, "shared") == ["shared/alice.md"]
    assert backend.list_surface(cache, "absent") == []
    surfaces = {r[-1] for r in _rows(cache)}
    assert surfaces == {"wiki", "notes", "shared"}


def test_remove_surface_leaves_other_rows_byte_identical(corpus: dict[str, Path]) -> None:
    backend = _build(corpus)
    cache = corpus["cache"]
    before = _rows(cache, "surface != ?", ("notes",))
    manifest_before = json.loads((cache / _FTS5_MANIFEST).read_text())

    assert backend.remove_surface(cache, "notes") == 2

    assert _rows(cache, "surface != ?", ("notes",)) == before  # rowids and values
    assert backend.list_surface(cache, "notes") == []
    manifest = json.loads((cache / _FTS5_MANIFEST).read_text())
    assert not [k for k in manifest["hashes"] if k.startswith("notes/")]
    assert {k: v for k, v in manifest_before["hashes"].items() if not k.startswith("notes/")} == (
        manifest["hashes"]
    )


def test_recall_over_remaining_surfaces_unchanged(corpus: dict[str, Path]) -> None:
    backend = _build(corpus)
    cache = corpus["cache"]
    queries = ["wikionlyword", "wikiquartz", "sharedquartz", "alice quartz"]
    expect = {
        q: [h for h in _recall(backend, cache, q) if not h[0].startswith("notes/")]
        for q in queries
    }
    backend.remove_surface(cache, "notes")
    for q in queries:
        got = _recall(backend, cache, q)
        assert not any(h[0].startswith("notes/") for h in got)
        assert [h[0] for h in got] == [h[0] for h in expect[q]]


def test_same_name_on_two_surfaces_survives_removal_of_one(corpus: dict[str, Path]) -> None:
    backend = _build(corpus)
    cache = corpus["cache"]
    backend.remove_surface(cache, "notes")
    assert "alice.md" in [h[0] for h in _recall(backend, cache, "wikiquartz")]
    assert "shared/alice.md" in [h[0] for h in _recall(backend, cache, "sharedquartz")]
    assert "notes/alice.md" not in [h[0] for h in _recall(backend, cache, "notesquartz")]
    # and the reverse: removing the wiki leaves the same-named extra recallable
    backend.remove_surface(cache, "wiki")
    assert "shared/alice.md" in [h[0] for h in _recall(backend, cache, "sharedquartz")]


def test_removed_surface_is_reindexed_by_next_incremental_build(
    corpus: dict[str, Path],
) -> None:
    backend = _build(corpus)
    cache = corpus["cache"]
    backend.remove_surface(cache, "notes")
    _build(corpus)  # incremental, nothing on disk changed
    assert backend.list_surface(cache, "notes") == ["notes/alice.md", "notes/only-notes.md"]


def test_one_surface_install_unchanged(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _page(wiki, "a.md", "Alpha", "alphaword")
    cache = tmp_path / "cache"
    backend = FTS5Backend()
    assert backend.build_index(wiki, cache) == 1
    assert [r[-1] for r in _rows(cache)] == ["wiki"]
    assert [h[0] for h in backend.query("alphaword", cache)] == ["a.md"]


def test_pre_surface_index_is_rebuilt_on_next_run_without_a_manual_step(
    corpus: dict[str, Path],
) -> None:
    # An index built before this change: v6 shape (no ``surface`` column), a
    # manifest arming the incremental path. The next ordinary build must
    # rebuild it, populating ``surface`` for EVERY page, not only changed ones.
    cache = corpus["cache"]
    cache.mkdir(parents=True)
    conn = sqlite3.connect(str(cache / _DB_NAME))
    conn.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5(filename, name, tags, aliases, "
        "description, body, audience UNINDEXED, type UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    conn.execute("PRAGMA user_version = 6")
    conn.execute(
        "INSERT INTO wiki VALUES (?,?,?,?,?,?,?,?)",
        ("alice.md", "Alice Quartz", "", "", "", "old", "|_public|", ""),
    )
    conn.commit()
    conn.close()
    (cache / _FTS5_MANIFEST).write_text(
        json.dumps({"version": 1, "hashes": {"alice.md": "x"}}), encoding="utf-8"
    )

    backend = _build(corpus)  # incremental=True default, no flags

    assert FTS5Backend._db_schema_version(cache / _DB_NAME) == FTS5Backend._SCHEMA_VERSION
    assert backend.list_surface(cache, "wiki") == ["alice.md", "only-wiki.md"]
    assert backend.list_surface(cache, "notes") == ["notes/alice.md", "notes/only-notes.md"]
    assert [h[0] for h in backend.query("wikiquartz", cache)] == ["alice.md"]


def test_pre_surface_vector_manifest_forces_full_rebuild(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / _VECTOR_DIR).mkdir(parents=True)
    stored = {"embedding_model": VectorBackend().embedding_model, "metadata_schema_version": 3}
    assert VectorBackend().incremental_reuse_blocker(cache, stored) is not None
    stored["metadata_schema_version"] = VectorBackend._METADATA_SCHEMA_VERSION
    assert VectorBackend().incremental_reuse_blocker(cache, stored) is None


# --- vector backend ---------------------------------------------------------
# chromadb is an optional extra and may be absent. The backend logic is covered
# twice: against an in-memory stand-in (always runs) and against the real
# chromadb (skipped when it is not installed).


class _FakeCollection:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, str]] = {}

    def add(self, ids: list[str], documents: list[str], metadatas: list[dict[str, str]]) -> None:
        for i, m in zip(ids, metadatas, strict=True):
            self.rows[i] = dict(m)

    def get(self, where: dict[str, str] | None = None, include: Any = None) -> dict[str, Any]:
        ids = [
            i
            for i, m in self.rows.items()
            if not where or all(m.get(k) == v for k, v in where.items())
        ]
        return {"ids": ids}

    def delete(self, ids: list[str]) -> None:
        for i in ids:
            self.rows.pop(i)


@pytest.fixture()
def fake_chromadb(monkeypatch: pytest.MonkeyPatch) -> _FakeCollection:
    coll = _FakeCollection()
    client = types.SimpleNamespace(get_collection=lambda *a, **k: coll)
    chroma = types.ModuleType("chromadb")
    chroma.PersistentClient = lambda path: client  # type: ignore[attr-defined]
    api = types.ModuleType("chromadb.api")
    api_client = types.ModuleType("chromadb.api.client")
    api_client.SharedSystemClient = types.SimpleNamespace(  # type: ignore[attr-defined]
        clear_system_cache=lambda: None
    )
    monkeypatch.setitem(sys.modules, "chromadb", chroma)
    monkeypatch.setitem(sys.modules, "chromadb.api", api)
    monkeypatch.setitem(sys.modules, "chromadb.api.client", api_client)
    return coll


def _records(c: dict[str, Path]) -> list[Any]:
    out = []
    for name, root in (
        ("alice.md", c["wiki"]),
        ("only-wiki.md", c["wiki"]),
        ("notes/alice.md", c["notes"]),
        ("notes/only-notes.md", c["notes"]),
        ("shared/alice.md", c["shared"]),
    ):
        path = root / name.split("/")[-1]
        out.append((name, path, "h", path.read_text(), {"name": "x"}, ("v", "")))
    return out


def test_vector_rows_record_surface_list_and_remove(
    corpus: dict[str, Path], fake_chromadb: _FakeCollection
) -> None:
    backend = VectorBackend()
    backend._add_records(fake_chromadb, _records(corpus))
    cache = corpus["cache"]
    (cache / _VECTOR_DIR).mkdir(parents=True)
    (cache / _VECTOR_MANIFEST).write_text(
        json.dumps({"version": 3, "hashes": {"alice.md": "a", "notes/alice.md": "b"}})
    )
    assert {m["surface"] for m in fake_chromadb.rows.values()} == {"wiki", "notes", "shared"}
    assert backend.list_surface(cache, "notes") == ["notes/alice.md", "notes/only-notes.md"]

    before = {k: dict(v) for k, v in fake_chromadb.rows.items() if v["surface"] != "notes"}
    assert backend.remove_surface(cache, "notes") == 2
    assert fake_chromadb.rows == before
    assert backend.list_surface(cache, "shared") == ["shared/alice.md"]
    assert json.loads((cache / _VECTOR_MANIFEST).read_text())["hashes"] == {"alice.md": "a"}


def test_combined_list_and_remove(corpus: dict[str, Path], fake_chromadb: _FakeCollection) -> None:
    _build(corpus)
    cache = corpus["cache"]
    VectorBackend()._add_records(fake_chromadb, _records(corpus))
    (cache / _VECTOR_DIR).mkdir(parents=True)
    listed = list_surface_derived_data(cache, "notes")
    assert listed["fts5"] == listed["vector"] == ["notes/alice.md", "notes/only-notes.md"]
    assert remove_surface_derived_data(cache, "notes") == {"fts5": 2, "vector": 2}
    assert list_surface_derived_data(cache, "notes") == {"fts5": [], "vector": []}
    assert list_surface_derived_data(cache, "shared")["vector"] == ["shared/alice.md"]


def test_real_chromadb_build_list_remove(corpus: dict[str, Path]) -> None:
    pytest.importorskip("chromadb")
    backend = VectorBackend()
    cache = corpus["cache"]
    backend.build_index(corpus["wiki"], cache, extra_roots=[corpus["notes"], corpus["shared"]])
    assert backend.list_surface(cache, "notes") == ["notes/alice.md", "notes/only-notes.md"]
    assert backend.remove_surface(cache, "notes") == 2
    assert backend.list_surface(cache, "notes") == []
    assert backend.list_surface(cache, "shared") == ["shared/alice.md"]
    hits = [h[0] for h in backend.query("sharedquartz", cache, n=10)]
    assert "shared/alice.md" in hits
    # next incremental build restores the still-mounted surface
    backend.build_index(corpus["wiki"], cache, extra_roots=[corpus["notes"], corpus["shared"]])
    assert backend.list_surface(cache, "notes") == ["notes/alice.md", "notes/only-notes.md"]
