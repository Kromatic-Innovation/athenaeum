# SPDX-License-Identifier: Apache-2.0
"""One page type shared with a second person through its own repository, end to end
(athenaeum#3172).

The configuration is the guide's own. ``docs/guides/shared-page-type.md`` marks each
configuration block with ``<!-- fixture: NAME -->``; the first test fails the build
when a block differs from ``tests/fixtures/shared_page_type/NAME``, and the rest load
those same files to build two installs around a local bare repository.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import librarian, store_rights
from athenaeum.mcp_server import recall_search
from athenaeum.search import get_backend
from athenaeum.storage import (
    ReadOnlySurfaceError,
    open_store,
    resolve_indexed_surfaces,
    route_for,
)
from athenaeum.store import StoreKey

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "guides" / "shared-page-type.md"
FIXTURES = REPO / "tests" / "fixtures" / "shared_page_type"
_BLOCK = re.compile(r"<!-- fixture: (\S+) -->\n```\w*\n(.*?)```", re.DOTALL)


def _guide_blocks() -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _BLOCK.finditer(GUIDE.read_text(encoding="utf-8"))}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _config(name: str) -> dict[str, Any]:
    config = yaml.safe_load(_fixture(name))
    # Pinned: the shipped default is the vector backend, which needs an optional extra.
    config["search_backend"] = "fts5"
    return config


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def test_guide_blocks_equal_the_fixtures() -> None:
    blocks = _guide_blocks()
    assert sorted(blocks) == sorted(p.name for p in FIXTURES.iterdir())
    for name, text in blocks.items():
        assert text == _fixture(name), f"{name} in the guide has drifted from the fixture"


def _recall(kr: Path, config: dict[str, Any], query: str) -> str:
    cache = kr / "cache"
    surfaces = resolve_indexed_surfaces(kr, config)
    get_backend("fts5").build_index(kr / "wiki", cache, extra_roots=surfaces, config=config)
    return recall_search(
        kr / "wiki", query, top_k=5, search_backend="fts5",
        cache_dir=cache, extra_roots=[*surfaces], config=config,
    )


def _page(name: str, uid: str, body: str) -> bytes:
    return (
        f"---\nuid: {uid}\ntype: recipe\nname: {name}\n---\n\n# {name}\n\n{body}\n"
    ).encode()


@pytest.fixture(autouse=True)
def _fresh_rights() -> None:
    store_rights.reset_run_rights()


def test_two_installs_from_the_guide(tmp_path: Path) -> None:
    bare = tmp_path / "recipes.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)

    # Install 1: the owner. The repository is the location's own root.
    one = tmp_path / "one"
    (one / "wiki").mkdir(parents=True)
    owner_cfg = _config("owner-config.yaml")
    surface = one / "recipes"
    surface.mkdir()
    _git(surface, "init", "-q", "-b", "main")
    _git(surface, "config", "user.email", "jordan@example.com")
    _git(surface, "config", "user.name", "Jordan Reyes")
    _git(surface, "remote", "add", "origin", str(bare))
    (surface / "_store").mkdir()
    (surface / "_store" / "access.yaml").write_text(_fixture("access.yaml"), encoding="utf-8")

    # The rule, not a hand-built path, decides where a recipe goes.
    target = route_for("recipe", config=owner_cfg).targets
    assert target == ("recipes",)
    store = open_store(owner_cfg, one)
    store.put(StoreKey(target[0], "loaf.md"), _page("Pistachio Loaf", "recipe-loaf", "zephyrloaf"))
    for i in range(5):  # a corpus of one page scores ~0 on bm25
        filler = _page(f"Filler {i}", f"recipe-f{i}", "plain")
        store.put(StoreKey(target[0], f"filler-{i}.md"), filler)
    _git(surface, "add", "-A")
    _git(surface, "commit", "-q", "-m", "first recipes")
    _git(surface, "push", "-q", "-u", "origin", "main")
    # The location is ordinary: the owner finds the page too.
    assert "Pistachio Loaf" in _recall(one, owner_cfg, "zephyrloaf")

    # Install 2: a reader, with a clone it never pushes to.
    two = tmp_path / "two"
    (two / "wiki").mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(bare), str(two / "recipes"))
    reader_cfg = _config("reader-config.yaml")
    store_rights.reset_run_rights()
    assert "Pistachio Loaf" in _recall(two, reader_cfg, "zephyrloaf")

    # A page pushed later is invisible until a reindex has pulled it.
    store.put(StoreKey(target[0], "tart.md"), _page("Quince Tart", "recipe-tart", "quincetart"))
    _git(surface, "add", "-A")
    _git(surface, "commit", "-q", "-m", "second recipe")
    _git(surface, "push", "-q")
    assert "Quince Tart" not in _recall(two, reader_cfg, "quincetart")
    librarian.reindex(two, config=reader_cfg, cache_dir=two / "cache", backend="fts5")
    assert "Quince Tart" in _recall(two, reader_cfg, "quincetart")

    # A write on install 2 is refused, by the local flag...
    reader_store = open_store(reader_cfg, two)
    with pytest.raises(ReadOnlySurfaceError):
        reader_store.put(StoreKey("recipes", "mine.md"), _page("Mine", "recipe-mine", "x"))
    assert not (two / "recipes" / "mine.md").exists()

    # ...and, with the flag removed, by the declaration, which gives this reader no compile.
    del reader_cfg["storage"]["adapters"]["recipes"]["read_only"]
    store_rights.reset_run_rights()
    with pytest.raises(ReadOnlySurfaceError):
        open_store(reader_cfg, two).put(
            StoreKey("recipes", "mine.md"), _page("Mine", "recipe-mine", "x")
        )
