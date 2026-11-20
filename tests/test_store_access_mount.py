# SPDX-License-Identifier: Apache-2.0
"""A declared store this install cannot read is not mounted (access S4, athenaeum#2989).

Not mounted means not indexed and not recalled; nothing on disk is deleted. A
store with no declaration mounts as before, and failure narrows to a reader.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import librarian, store_rights
from athenaeum.mcp_server import entity_read, recall_search
from athenaeum.search import get_backend
from athenaeum.storage import StorageConfigError, resolve_indexed_surfaces, resolve_locator_surfaces

ISS = "https://git.example.net"
ME = {"iss": ISS, "sub": "1"}
OTHER = {"iss": ISS, "sub": "2"}
STORE_ID = "st_q7m2xk4vwz5d"


def _decl(roles: dict[str, Any], store_id: str = STORE_ID, **extra: Any) -> str:
    return yaml.safe_dump({"version": 1, "store_id": store_id, "roles": roles, **extra})


READER = _decl({"reader": {"rights": ["read"], "members": [ME]}})
PROPOSER_ONLY = _decl({"member": {"rights": ["propose"], "members": [ME]}})
NOT_LISTED = _decl({"reader": {"rights": ["read"], "members": [OTHER]}})
EVERYONE = _decl({"reader": {"rights": ["read"], "members": ["*"]}})


def _page(path: Path, uid: str, name: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    head = f"---\nuid: {uid}\ntype: recipe\nname: {name}\ntags:\n  - base\n---\n"
    path.write_text(f"{head}\n# {name}\n\n{body}\n", encoding="utf-8")


def _kb(
    tmp_path: Path, declaration: str | None, *, adapter_store_id: str | None = None
) -> tuple[Path, dict]:
    (tmp_path / "wiki").mkdir()
    surface = tmp_path / "recipes"
    _page(surface / "loaf.md", "recipe-loaf", "Pistachio Loaf", "zephyrloaf")
    for i in range(5):  # a corpus of one page scores ~0 on bm25
        _page(surface / f"filler-{i}.md", f"recipe-filler{i}", f"Filler {i}", f"plain {i}")
    if declaration is not None:
        (surface / "_store").mkdir()
        (surface / "_store" / "access.yaml").write_text(declaration, encoding="utf-8")
    entry: dict[str, Any] = {
        "backing_store": "markdown",
        "surface_root": "recipes",
        "corpus_policy": {"embedded": True, "recallable": True, "merge_eligible": True},
        "read_only": True,
    }
    if adapter_store_id is not None:
        entry["store_id"] = adapter_store_id
    config = {
        "search_backend": "fts5",
        "owner": {"asserter": ME},
        "storage": {"adapters": {"recipes": entry}, "mapping": {"recipe": "recipes"}},
    }
    return tmp_path, config


@pytest.fixture(autouse=True)
def _fresh() -> None:
    store_rights.reset_run_rights()


def _prefixes(kr: Path, config: dict) -> list[str]:
    return [s.prefix for s in resolve_indexed_surfaces(kr, config)]


def _recall(kr: Path, config: dict) -> str:
    cache = kr / "cache"
    surfaces = resolve_indexed_surfaces(kr, config)
    get_backend("fts5").build_index(kr / "wiki", cache, extra_roots=surfaces, config=config)
    return recall_search(
        kr / "wiki", "zephyrloaf", top_k=5, search_backend="fts5",
        cache_dir=cache, extra_roots=[*surfaces], config=config,
    )


def test_a_reader_mounts_and_is_recalled(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, READER)
    assert _prefixes(kr, config) == ["recipes"]
    assert "Pistachio Loaf" in _recall(kr, config)


@pytest.mark.parametrize("decl", [PROPOSER_ONLY, NOT_LISTED], ids=["propose-only", "not-listed"])
def test_a_declared_store_without_read_is_not_indexed_or_recalled(
    tmp_path: Path, decl: str
) -> None:
    kr, config = _kb(tmp_path, decl)
    assert _prefixes(kr, config) == []
    assert resolve_locator_surfaces(kr, config) == []
    assert "Pistachio Loaf" not in _recall(kr, config)
    out = json.loads(entity_read(kr, "recipe-loaf", page_class="recipe", config=config))
    assert out.get("ok") is False or not out.get("content"), out


def test_refusal_is_reported_per_store_without_page_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    kr, config = _kb(tmp_path, PROPOSER_ONLY)
    with caplog.at_level(logging.WARNING, logger="athenaeum.store_rights"):
        _prefixes(kr, config)
        _prefixes(kr, config)
    lines = [r.getMessage() for r in caplog.records if "not mounted" in r.getMessage()]
    assert lines == ["store 'recipes' not mounted: no-read-right"]
    assert "zephyrloaf" not in caplog.text


def test_nothing_on_disk_is_deleted(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, READER)
    cache = kr / "cache"
    librarian.reindex(kr, config=config, cache_dir=cache, backend="fts5")
    before = sorted(p for p in kr.rglob("*") if p.is_file() and "cache" not in p.parts)
    # The grant is withdrawn: the install stops mounting the store.
    (kr / "recipes" / "_store" / "access.yaml").write_text(PROPOSER_ONLY, encoding="utf-8")
    store_rights.reset_run_rights()
    librarian.reindex(kr, config=config, cache_dir=cache, backend="fts5")
    after = sorted(p for p in kr.rglob("*") if p.is_file() and "cache" not in p.parts)
    assert after == before
    assert (cache).exists()  # existing derived data is left in place (removal is S4c)


def test_mismatched_store_id_is_refused(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, READER, adapter_store_id="st_aaaaaaaaaaaa")
    assert _prefixes(kr, config) == []


def test_matching_store_id_mounts(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, READER, adapter_store_id=STORE_ID)
    assert _prefixes(kr, config) == ["recipes"]


def test_store_id_without_a_declaration_mounts_as_today(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, None, adapter_store_id=STORE_ID)
    assert _prefixes(kr, config) == ["recipes"]


def test_a_non_string_store_id_is_a_config_error(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, None)
    config["storage"]["adapters"]["recipes"]["store_id"] = 7
    with pytest.raises(StorageConfigError, match="store_id"):
        _prefixes(kr, config)


def test_no_declaration_mounts_exactly_as_today(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, None)
    assert _prefixes(kr, config) == ["recipes"]
    assert "Pistachio Loaf" in _recall(kr, config)


def test_everyone_grants_read(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, EVERYONE)
    config["owner"] = {}
    assert _prefixes(kr, config) == ["recipes"]


@pytest.mark.parametrize("text", ["version: 99\n", "{{{ not yaml"])
def test_invalid_declaration_mounts_as_a_reader_and_reports(
    tmp_path: Path, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    kr, config = _kb(tmp_path, text)
    with caplog.at_level(logging.WARNING, logger="athenaeum.store_rights"):
        assert _prefixes(kr, config) == ["recipes"]
    assert "store 'recipes' mounted: declaration-invalid" in caplog.text


def test_directory_that_cannot_answer_mounts_as_a_reader_and_reports(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    decl = _decl(
        {"editor": {"rights": ["read", "compile"], "members": ["group:staff"]}},
        directory="corp-ldap",
        groups={"staff": [ME]},
    )
    kr, config = _kb(tmp_path, decl)
    with caplog.at_level(logging.WARNING, logger="athenaeum.store_rights"):
        assert _prefixes(kr, config) == ["recipes"]
    assert "directory-cannot-answer" in caplog.text


def test_an_unexpected_error_narrows_to_a_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kr, config = _kb(tmp_path, PROPOSER_ONLY)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("exploded")

    monkeypatch.setattr(store_rights, "_compute_mount", boom)
    assert _prefixes(kr, config) == ["recipes"]


def test_an_unmounted_store_is_not_smuggled_in_as_an_extra_intake_root(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, PROPOSER_ONLY)
    config["recall"] = {"extra_intake_roots": [str(kr / "recipes")]}
    assert _prefixes(kr, config) == []


def test_mount_state_is_held_not_recomputed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kr, config = _kb(tmp_path, READER)
    calls: list[Path] = []
    real = store_rights._compute_mount

    def counting(root: Path, *a: Any) -> Any:
        calls.append(root)
        return real(root, *a)

    monkeypatch.setattr(store_rights, "_compute_mount", counting)
    for _ in range(4):
        _prefixes(kr, config)
    assert len(calls) == 1


def test_run_summary_names_the_unmounted_store_and_reason(tmp_path: Path) -> None:
    kr, config = _kb(tmp_path, PROPOSER_ONLY)
    state = librarian.SurfaceSyncState()
    mounts = store_rights.run_start_mounts({"recipes": (kr / "recipes", None)}, ME)
    for name, m in mounts.items():
        if m.refusal:
            state.record((name,), "mount", "skipped", m.refusal)
    assert state.as_profile_fields()["unmounted"] == "recipes=no-read-right"


def test_librarian_run_reports_and_leaves_the_store_untouched(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    kr, config = _kb(tmp_path, PROPOSER_ONLY)
    (kr / "raw").mkdir()
    (kr / "athenaeum.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    for args in (
        ("init", "-b", "develop"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
        ("add", "-A"),
        ("commit", "-m", "seed"),
    ):
        subprocess.run(["git", *args], cwd=kr, check=True, capture_output=True)
    before = (kr / "recipes" / "loaf.md").read_bytes()
    with caplog.at_level(logging.INFO):
        librarian.run(
            raw_root=kr / "raw", wiki_root=kr / "wiki", knowledge_root=kr, merge_only=True
        )
    assert "store 'recipes' not mounted: no-read-right" in caplog.text
    assert "unmounted=recipes=no-read-right" in caplog.text
    assert (kr / "recipes" / "loaf.md").read_bytes() == before


def test_run_start_reports_only_surfaces_the_gather_checks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A non-embedded surface with a no-read declaration is never gathered, so it
    # is neither refused by the gather nor reported as unmounted at run start.
    from athenaeum.storage import gathered_surface_mounts

    kr, config = _kb(tmp_path, PROPOSER_ONLY)
    config["storage"]["adapters"]["recipes"]["corpus_policy"] = {"embedded": False}
    assert gathered_surface_mounts(kr, config) == {}
    (kr / "raw").mkdir()
    (kr / "athenaeum.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    for args in (
        ("init", "-b", "develop"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
        ("add", "-A"),
        ("commit", "-m", "seed"),
    ):
        subprocess.run(["git", *args], cwd=kr, check=True, capture_output=True)
    with caplog.at_level(logging.INFO):
        librarian.run(
            raw_root=kr / "raw", wiki_root=kr / "wiki", knowledge_root=kr, merge_only=True
        )
    assert "not mounted" not in caplog.text
    assert "unmounted=" not in caplog.text
