# SPDX-License-Identifier: Apache-2.0
"""A storage surface declared ``read_only`` is pulled and indexed, never written
(athenaeum#2621).

Every write path refuses a page on such a surface with the one named
:class:`~athenaeum.storage.ReadOnlySurfaceError`; the install pulls the mount
before each index build and never pushes to it. Remotes are local bare
repositories under ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from athenaeum import librarian
from athenaeum.contact_record_fold import fold_duplicate_record
from athenaeum.contact_record_repair import ContactRepairPlan, apply_contact_record_repair
from athenaeum.dedupe import DuplicatePair, merge_duplicate_persons
from athenaeum.entity_rename import RenamePreview, apply_rename
from athenaeum.fold_target_repair import repair_dangling_fold_targets
from athenaeum.mcp_server import entity_read, recall_search
from athenaeum.models import EntityIndex, ProcessingResult, RawFile
from athenaeum.pending_merges import _apply_fold_into_existing, resolve_merge
from athenaeum.repair import repair_tag_indent
from athenaeum.retire_pages import RetireCandidate, apply_retirement
from athenaeum.schema_migrate import PageOutcome, apply_migrations, build_migrate_report
from athenaeum.search import get_backend
from athenaeum.storage import (
    ReadOnlySurfaceError,
    StorageAdapter,
    StorageConfigError,
    available_adapters,
    is_read_only_path,
    open_store,
    require_writable_path,
    resolve_locator_surfaces,
)
from athenaeum.storage_migrate import NameEmailRenamePlan, apply_name_email_rename
from athenaeum.store import StoreKey


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "ada@example.com")
    _git(path, "config", "user.name", "Ada Example")
    _git(path, "commit", "-q", "--allow-empty", "-m", "seed")
    return path


def _with_remote(repo: Path, tmp_path: Path, name: str) -> Path:
    bare = tmp_path / f"{name}-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return bare


def _config(read_only: Any = True, root: str = "recipes") -> dict[str, Any]:
    return {
        "storage": {
            "adapters": {
                "recipes": {
                    "backing_store": "markdown",
                    "surface_root": root,
                    "corpus_policy": {"embedded": True, "recallable": True, "merge_eligible": True},
                    "read_only": read_only,
                },
            },
            "mapping": {"recipe": "recipes"},
        },
        "librarian": {
            "structured_upsert": {
                "recipe": {"sections": ["Notes"], "append_sections": ["Notes"]}
            }
        },
    }


def _page(path: Path, uid: str, name: str, body: str = "body", typ: str = "recipe") -> Path:
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


@pytest.fixture
def ro_page(kb: Path) -> Path:
    return _page(kb / "recipes" / "loaf.md", "recipe-loaf", "Pistachio Loaf", "zephyrloaf")


class TestDeclaration:
    def test_default_is_writable(self) -> None:
        cfg = _config(read_only=False)
        assert available_adapters(cfg)["recipes"].read_only is False
        cfg["storage"]["adapters"]["recipes"].pop("read_only")
        assert available_adapters(cfg)["recipes"].read_only is False
        assert StorageAdapter.__dataclass_fields__["read_only"].default is False

    def test_true_is_read(self) -> None:
        assert available_adapters(_config())["recipes"].read_only is True

    @pytest.mark.parametrize("bad", ["yes", "true", 1, "no", [], {}])
    def test_non_bool_is_rejected(self, bad: Any) -> None:
        with pytest.raises(StorageConfigError, match="read_only"):
            available_adapters(_config(read_only=bad))

    def test_path_predicate(self, kb: Path, ro_page: Path) -> None:
        cfg = _config()
        assert is_read_only_path(ro_page, cfg, kb)
        assert not is_read_only_path(kb / "wiki" / "x.md", cfg, kb)
        assert not is_read_only_path(ro_page, _config(read_only=False), kb)
        assert not is_read_only_path(ro_page, None, kb)  # no athenaeum.yaml: nothing declared


class TestStoreRefuses:
    def test_put_append_delete_move_refuse(self, kb: Path, ro_page: Path) -> None:
        store = open_store(_config(), kb)
        before = ro_page.read_bytes()
        key = StoreKey(surface="recipes", key="loaf.md")
        with pytest.raises(ReadOnlySurfaceError):
            store.put(key, b"x")
        with pytest.raises(ReadOnlySurfaceError):
            store.put(StoreKey(surface="recipes", key="new.md"), b"x")
        with pytest.raises(ReadOnlySurfaceError):
            store.append(key, b"x")
        with pytest.raises(ReadOnlySurfaceError):
            store.delete(key)
        with pytest.raises(ReadOnlySurfaceError):
            store.move(key, StoreKey(surface="recipes", key="other.md"))
        with pytest.raises(ReadOnlySurfaceError):
            store.move(
                StoreKey(surface="wiki-markdown-embedded", key="a.md"),
                StoreKey(surface="recipes", key="b.md"),
            )
        assert ro_page.read_bytes() == before
        assert not (kb / "recipes" / "new.md").exists()
        # reads and other surfaces are untouched
        assert store.read(key) == before
        store.put(StoreKey(surface="wiki-markdown-embedded", key="ok.md"), b"fine")

    def test_writable_declaration_still_writes(self, kb: Path, ro_page: Path) -> None:
        store = open_store(_config(read_only=False), kb)
        store.put(StoreKey(surface="recipes", key="new.md"), b"x")
        assert (kb / "recipes" / "new.md").read_bytes() == b"x"

    def test_error_is_named_and_a_capability_error(self, kb: Path, ro_page: Path) -> None:
        from athenaeum.store import StoreCapabilityError

        assert issubclass(ReadOnlySurfaceError, StoreCapabilityError)
        with pytest.raises(ReadOnlySurfaceError, match="read_only"):
            require_writable_path(ro_page, kb, _config())
        require_writable_path(kb / "wiki" / "x.md", kb, _config())  # off the mount: fine


class TestEveryWritePathRefuses:
    """One test per path: the refusal is shown and the page is byte-identical."""

    def test_compile_update(
        self, kb: Path, ro_page: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = _config()
        before = ro_page.read_bytes()
        result = ProcessingResult(
            raw_file=RawFile(path=kb / "raw" / "r.md", source="r", timestamp="", uuid8="")
        )
        index = EntityIndex(kb / "wiki", resolve_locator_surfaces(kb, cfg))
        with caplog.at_level("WARNING"):
            librarian._apply_tier3_results(
                result,
                new_entities=[],
                pending_updates=[
                    (ro_page, "---\nuid: recipe-loaf\ntype: recipe\nname: Loaf\n---\n\nX\n")
                ],
                updated_uids=["recipe-loaf"],
                escalations=[],
                wiki_root=kb / "wiki",
                index=index,
                config=cfg,
            )
        assert ro_page.read_bytes() == before
        assert "ReadOnlySurfaceError" in caplog.text
        assert result.updated == []
        # The refusal is counted and the raw is KEPT (never consumed).
        assert result.retain_raw is True
        assert result.read_only_refused == 1
        assert result.skipped == ["read-only-surface"]

    def test_compile_create_keeps_the_raw(self, kb: Path) -> None:
        from athenaeum.models import WikiEntity

        cfg = _config(root="wiki")  # the whole wiki/ is declared read-only
        raw_path = kb / "raw" / "r.md"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text("a fact worth keeping\n", encoding="utf-8")
        result = ProcessingResult(
            raw_file=RawFile(path=raw_path, source="r", timestamp="", uuid8="")
        )
        entity = WikiEntity(
            uid="concept-new", type="concept", name="New Loaf", aliases=[], access="open",
            tags=[], body="# New Loaf\n\nbody\n",
        )
        librarian._apply_tier3_results(
            result,
            new_entities=[entity],
            pending_updates=[],
            updated_uids=[],
            escalations=[],
            wiki_root=kb / "wiki",
            index=EntityIndex(kb / "wiki"),
            config=cfg,
        )
        assert not (kb / "wiki" / entity.filename).exists()
        assert result.created == [] and result.retain_raw is True
        assert result.read_only_refused == 1
        assert raw_path.exists()

    def test_tier0_upsert_keeps_the_raw(self, kb: Path) -> None:
        cfg = _config()
        cfg.pop("librarian")  # the structured-upsert step would claim the raw first
        page = _page(kb / "recipes" / "loaf.md", "recipe-loaf", "Pistachio Loaf")
        before = page.read_bytes()
        raw_path = kb / "raw" / "seed.md"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text(
            "---\nuid: recipe-loaf\ntype: recipe\nname: Pistachio Loaf\n"
            "domains:\n  - loaf.example\nhandles_verified: '2026-07-27'\n---\n\n# Loaf\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="seed", timestamp="", uuid8="")
        result = librarian.process_one(
            raw,
            EntityIndex(kb / "wiki", resolve_locator_surfaces(kb, cfg)),
            kb / "wiki",
            MagicMock(),
            valid_types=["recipe"],
            valid_tags=[],
            valid_access=["open"],
            config=cfg,
        )
        assert page.read_bytes() == before
        assert result.retain_raw is True and result.read_only_refused == 1
        assert result.handled_no_op is False
        assert result.updated == []
        assert raw_path.exists()

    def test_run_never_deletes_a_raw_refused_by_a_read_only_surface(
        self, kb: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anthropic as anthropic_mod

        cfg = _config()
        cfg.pop("librarian")
        (kb / "athenaeum.yaml").write_text(json.dumps(cfg), encoding="utf-8")
        page = _page(kb / "recipes" / "loaf.md", "recipe-loaf", "Pistachio Loaf")
        before = page.read_bytes()
        raw_path = kb / "raw" / "sessions" / "20260410T120000Z-aabbccdd.md"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text(
            "---\nuid: recipe-loaf\ntype: recipe\nname: Pistachio Loaf\n"
            "domains:\n  - loaf.example\nhandles_verified: '2026-07-27'\n---\n\n# Loaf\n",
            encoding="utf-8",
        )
        _init_repo(kb)
        _git(kb, "add", "-A")
        _git(kb, "commit", "-q", "-m", "seed")
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: MagicMock())
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(kb / "cache"))
        librarian.run(raw_root=kb / "raw", wiki_root=kb / "wiki", knowledge_root=kb)
        assert raw_path.exists(), "a read-only refusal must never consume the raw"
        assert page.read_bytes() == before

    def test_structured_upsert(self, kb: Path, ro_page: Path) -> None:
        cfg = _config()
        before = ro_page.read_bytes()
        raw_path = kb / "raw" / "r.md"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text(
            "---\nuid: recipe-loaf\ntype: recipe\nname: Pistachio Loaf\n---\n\n"
            "## Notes\n\nTry less sugar.\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="r", timestamp="", uuid8="")
        index = EntityIndex(kb / "wiki", resolve_locator_surfaces(kb, cfg))
        out = librarian.tier0_structured_upsert(
            raw, index, kb / "wiki", ["recipe", "concept"], config=cfg
        )
        assert out is not None and out.status == "refused"
        assert out.reason == "read-only-surface"
        assert ro_page.read_bytes() == before
        assert raw_path.exists()

    def test_merge_person_dedupe(self, kb: Path, ro_page: Path) -> None:
        cfg = _config()
        other = _page(kb / "recipes" / "loaf2.md", "recipe-loaf2", "Pistachio Loaf Two")
        before = (ro_page.read_bytes(), other.read_bytes())
        pair = DuplicatePair(
            canonical_uid="recipe-loaf",
            absorbed_uid="recipe-loaf2",
            canonical_path=str(ro_page),
            absorbed_path=str(other),
            match_signal="name_exact",
        )
        (kb / "athenaeum.yaml").write_text(json.dumps(cfg), encoding="utf-8")
        report = merge_duplicate_persons([pair], apply=True, wiki_root=kb / "wiki")
        assert any("ReadOnlySurfaceError" in e for e in report.errors), report.errors
        assert report.merged == 0
        assert (ro_page.read_bytes(), other.read_bytes()) == before

    def test_merge_resolution_and_fold(self, kb: Path, ro_page: Path) -> None:
        (kb / "athenaeum.yaml").write_text(json.dumps(_config()), encoding="utf-8")
        other = _page(kb / "recipes" / "loaf2.md", "recipe-loaf2", "Pistachio Loaf Two")
        before = (ro_page.read_bytes(), other.read_bytes())
        pm = type(
            "PM",
            (),
            {"sources": [str(ro_page), str(other)], "write_kind": "fold-into-existing"},
        )()
        res = _apply_fold_into_existing(
            pm,  # type: ignore[arg-type]
            target_path=ro_page,
            target_slug="loaf",
            wiki_root=kb / "wiki",
            repo_root=kb,
            cache_dir=None,
            search_backend=None,
            embedding_model=None,
        )
        assert res["ok"] is False and res["error_code"] == "read_only_surface"
        assert "ReadOnlySurfaceError" in res["message"]
        assert (ro_page.read_bytes(), other.read_bytes()) == before
        assert resolve_merge is not None

    def test_repair(self, kb: Path, ro_page: Path) -> None:
        # Repair walks wiki/; a page of a read-only surface placed there by a
        # bad layout is still refused at the single write helper.
        cfg = _config(root="wiki")
        (kb / "athenaeum.yaml").write_text(json.dumps(cfg), encoding="utf-8")
        page = kb / "wiki" / "p.md"
        page.write_text("---\nuid: a\ntags:\n- x\n---\nbody\n", encoding="utf-8")
        before = page.read_bytes()
        with pytest.raises(ReadOnlySurfaceError):
            repair_tag_indent(kb / "wiki", apply=True)
        assert page.read_bytes() == before

    def test_fold_target_repair(self, kb: Path) -> None:
        cfg = _config(root="wiki")
        (kb / "athenaeum.yaml").write_text(json.dumps(cfg), encoding="utf-8")
        target = _page(kb / "wiki" / "uid1-real.md", "uid1", "Real")
        tomb = kb / "wiki" / "old.md"
        tomb.write_text(
            "---\nuid: uid2\ntype: recipe\nname: Old\nstatus: folded\nfolded_into: real\n"
            "embedded: false\n---\n\nbody\n",
            encoding="utf-8",
        )
        before = (tomb.read_bytes(), target.read_bytes())
        with pytest.raises(ReadOnlySurfaceError):
            repair_dangling_fold_targets(kb / "wiki", apply=True)
        assert (tomb.read_bytes(), target.read_bytes()) == before

    def test_contact_fold_and_repair(self, kb: Path) -> None:
        cfg = _config(root="excluded")
        (kb / "athenaeum.yaml").write_text(json.dumps(cfg), encoding="utf-8")
        a = _page(kb / "excluded" / "a.md", "u1", "A")
        b = _page(kb / "excluded" / "b.md", "u1", "A")
        before = (a.read_bytes(), b.read_bytes())
        with pytest.raises(ReadOnlySurfaceError):
            fold_duplicate_record(kb / "excluded", "u1", b, a)
        with pytest.raises(ReadOnlySurfaceError):
            apply_contact_record_repair(ContactRepairPlan(), kb / "excluded")
        assert (a.read_bytes(), b.read_bytes()) == before

    def test_retire(self, kb: Path, ro_page: Path) -> None:
        cfg = _config()
        _init_repo(kb)
        _git(kb, "add", "-A")
        _git(kb, "commit", "-q", "-m", "init")
        cand = RetireCandidate(uid="recipe-loaf", path=ro_page)
        report = apply_retirement(kb, [cand], reason="test", config=cfg)
        assert any("ReadOnlySurfaceError" in e for e in report.errors), report.errors
        assert ro_page.exists() and not report.committed

    def test_migration_schema_rename_and_storage(self, kb: Path, ro_page: Path) -> None:
        cfg = _config()
        (kb / "athenaeum.yaml").write_text(json.dumps(cfg), encoding="utf-8")
        before = ro_page.read_bytes()
        # schema migration
        report = build_migrate_report(kb / "wiki")
        report.outcomes.append(
            PageOutcome(path=ro_page, to_version=1, fields={"x": "y"}, reason="migrated")
        )
        with pytest.raises(ReadOnlySurfaceError):
            apply_migrations(report, kb)
        # entity rename
        preview = RenamePreview(
            page_path=ro_page,
            new_page_path=ro_page,
            rewritten_text="changed",
            uid="recipe-loaf",
            old_name="Pistachio Loaf",
            new_name="New",
            old_slug="loaf",
            new_slug="loaf",
            reslug=False,
        )
        with pytest.raises(ReadOnlySurfaceError):
            apply_rename(kb / "wiki", preview, by="t")
        # storage migration
        plan = NameEmailRenamePlan(
            page_path=ro_page,
            email="ada@example.com",
            confident=True,
            new_page_path=kb / "recipes" / "renamed.md",
            new_slug="renamed",
            rewritten_page_text="changed",
            excluded_page_path=kb / "excluded" / "x.md",
            excluded_page_text="x",
        )
        with pytest.raises(ReadOnlySurfaceError):
            apply_name_email_rename(plan, kb / "wiki")
        assert ro_page.read_bytes() == before
        assert not (kb / "recipes" / "renamed.md").exists()
        assert not (kb / "excluded" / "x.md").exists()


class TestPullAndPush:
    def _mount(self, tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
        kr = tmp_path / "kr"
        (kr / "wiki").mkdir(parents=True)
        repo = _init_repo(tmp_path / "shared")
        _page(repo / "loaf.md", "recipe-loaf", "Pistachio Loaf", "zephyrloaf")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "recipes")
        bare = _with_remote(repo, tmp_path, "shared")
        cfg = _config(root=str(repo))
        return kr, repo, bare, cfg

    def _advance_remote(self, tmp_path: Path, bare: Path, name: str, body: str) -> None:
        other = tmp_path / f"other-{name}"
        _git(tmp_path, "clone", "-q", str(bare), str(other))
        _git(other, "config", "user.email", "bob@example.com")
        _git(other, "config", "user.name", "Bob Example")
        _page(other / f"{name}.md", f"recipe-{name}", f"Fresh {name}", body)
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "more")
        _git(other, "push", "-q", "origin", "main")

    def test_pulled_before_each_index_build(self, tmp_path: Path) -> None:
        kr, repo, bare, cfg = self._mount(tmp_path)
        self._advance_remote(tmp_path, bare, "quokka", "quokkabread")
        cache = tmp_path / "cache"
        backend, pages = librarian.reindex(kr, config=cfg, cache_dir=cache, backend="fts5")
        assert (repo / "quokka.md").exists()  # pulled first
        assert pages == 2

    def test_cli_reindex_pulls_and_reports_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        kr, repo, bare, cfg = self._mount(tmp_path)
        (kr / "athenaeum.yaml").write_text(json.dumps(cfg), encoding="utf-8")
        cache = tmp_path / "cache"
        self._advance_remote(tmp_path, bare, "quokka", "quokkabread")
        assert main(["reindex", "--path", str(kr), "--cache-dir", str(cache)]) == 0
        assert (repo / "quokka.md").exists()
        # The remote disappears: the pull fails, is reported, and the last good copy is served.
        _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
        capsys.readouterr()
        assert main(["reindex", "--path", str(kr), "--cache-dir", str(cache), "--full"]) == 0
        err = capsys.readouterr().err
        assert "pull of read-only surface 'recipes' failed" in err
        hits = get_backend("fts5").query(
            "quokkabread", cache,
            wiki_root=kr / "wiki",
        )
        assert hits

    def test_failed_pull_keeps_last_good_copy_searchable(self, tmp_path: Path) -> None:
        kr, repo, bare, cfg = self._mount(tmp_path)
        _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
        outcome = librarian.pull_read_only_surfaces(kr, cfg)
        assert outcome == {"recipes": False}
        cache = tmp_path / "cache"
        _, pages = librarian.reindex(kr, config=cfg, cache_dir=cache, backend="fts5")
        assert pages == 1

    def test_no_read_only_surface_pulls_nothing(self, tmp_path: Path) -> None:
        kr, repo, bare, cfg = self._mount(tmp_path)
        assert librarian.pull_read_only_surfaces(kr, _config(read_only=False, root=str(repo))) == {}
        assert librarian.pull_read_only_surfaces(kr, {}) == {}

    def test_never_pushed_never_committed(self, tmp_path: Path) -> None:
        kr, repo, bare, cfg = self._mount(tmp_path)
        before = _git(bare, "rev-parse", "main")
        state = librarian.SurfaceSyncState()
        librarian._capture_surface_heads(kr, config=cfg, surface_sync=state)
        # A stray local edit and a local commit in the mount must still not be pushed.
        (repo / "stray.md").write_text("stray")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "local")
        open_store(cfg, kr).snapshot("run")
        librarian._maybe_push_after_run(
            kr, config=cfg, push_after_run=True, dry_run=False,
            head_at_start=None, surface_sync=state,
        )
        assert _git(bare, "rev-parse", "main") == before
        assert state.results == []
        store = open_store(cfg, kr)
        assert repo in store.sync_repos()
        assert store.push_repos() == {}
        # a dirty tree in the mount is not committed by a snapshot either
        (repo / "dirty.md").write_text("dirty")
        assert open_store(cfg, kr).snapshot_surfaces("run") == {}
        assert _git(repo, "status", "--porcelain") == "?? dirty.md"

    def test_writable_surface_is_still_pushed(self, tmp_path: Path) -> None:
        kr, repo, bare, cfg = self._mount(tmp_path)
        cfg = _config(read_only=False, root=str(repo))
        state = librarian.SurfaceSyncState()
        librarian._capture_surface_heads(kr, config=cfg, surface_sync=state)
        (repo / "mine.md").write_text("m")
        open_store(cfg, kr).snapshot("run")
        librarian._maybe_push_after_run(
            kr, config=cfg, push_after_run=True, dry_run=False,
            head_at_start=None, surface_sync=state,
        )
        assert _git(bare, "log", "-1", "--format=%s", "main") == "run"


class TestReadOnlyMountIsServed:
    def test_install_with_only_a_read_only_mount_answers_recall(
        self, kb: Path, ro_page: Path
    ) -> None:
        cfg = _config()
        surfaces = resolve_locator_surfaces(kb, cfg)
        assert [s.prefix for s in surfaces] == ["recipes"]
        cache = kb / "cache"
        get_backend("fts5").build_index(kb / "wiki", cache, extra_roots=surfaces, config=cfg)
        rendered = recall_search(
            kb / "wiki", "zephyrloaf", top_k=5, search_backend="fts5",
            cache_dir=cache, extra_roots=[*surfaces], config=cfg,
        )
        assert "Pistachio Loaf" in rendered
        assert "uncompiled" not in rendered.lower()
        typed = recall_search(
            kb / "wiki", "zephyrloaf", top_k=5, search_backend="fts5",
            cache_dir=cache, extra_roots=[*surfaces], config=cfg, type_filter="recipe",
        )
        assert "Pistachio Loaf" in typed
        out = json.loads(entity_read(kb, "recipe-loaf", page_class="recipe", config=cfg))
        assert out.get("ok") is not False, out
