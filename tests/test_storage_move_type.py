# SPDX-License-Identifier: Apache-2.0
"""``athenaeum storage move-type``: move a type's pages into its own-repo store (athenaeum#2620)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import storage_move
from athenaeum.cli import main
from athenaeum.config import load_config
from athenaeum.librarian import tier0_structured_upsert
from athenaeum.mcp_server import entity_read, recall_search
from athenaeum.models import EntityIndex, RawFile, parse_frontmatter
from athenaeum.storage import resolve_indexed_surfaces, resolve_locator_surfaces

CONFIG: dict[str, Any] = {
    "search_backend": "fts5",
    "storage": {
        "adapters": {
            "recipes": {
                "backing_store": "markdown",
                "surface_root": "recipes",
                "corpus_policy": {"embedded": True, "recallable": True},
                "visibility": 1,
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


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")


def _page(path: Path, uid: str, typ: str, name: str, body: str = "", related: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nuid: {uid}\ntype: {typ}\nname: {name}\ntags:\n  - base\n{related}---\n\n"
        f"# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


@pytest.fixture
def kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text("[user]\n\tname = t\n\temail = t@example.invalid\n")
    root = tmp_path / "kb"
    (root / "wiki").mkdir(parents=True)
    _init_repo(root)
    _init_repo(_recipes(root))
    cfg = json.loads(json.dumps(CONFIG))
    cfg["storage"]["adapters"]["recipes"]["surface_root"] = str(_recipes(root))
    (root / "athenaeum.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    # Two recipes; a private note that links TO a recipe (allowed direction).
    _page(root / "wiki" / "loaf.md", "recipe-loaf", "recipe", "Pistachio Loaf", "zephyrloaf body")
    _page(root / "wiki" / "soup.md", "recipe-soup", "recipe", "Leek Soup", "soup body")
    _page(
        root / "wiki" / "diary.md",
        "note-diary",
        "note",
        "Diary",
        "I baked [[Pistachio Loaf]] today.",
        related="related:\n  - uid: recipe-soup\n",
    )
    (_recipes(root) / "README.md").write_text("recipes store\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    _git(_recipes(root), "add", "-A")
    _git(_recipes(root), "commit", "-qm", "seed")
    return root


def _cfg(kb: Path) -> dict[str, Any]:
    return load_config(kb)


def _recipes(kb: Path) -> Path:
    """The recipes store: its own git repository OUTSIDE the knowledge root."""
    return kb.parent / "recipes-repo"


def _run(kb: Path, *args: str) -> int:
    return main(["storage", "move-type", *args, "--path", str(kb)])


class TestDryRun:
    def test_lists_pages_and_writes_nothing(
        self, kb: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = (_tree_digest(kb), _git(kb, "rev-parse", "HEAD"))
        assert _run(kb, "recipe", "--to", "recipes") == 0
        out = capsys.readouterr().out
        assert "2 page(s)" in out and "loaf.md" in out and "soup.md" in out
        assert "diary.md" not in out
        assert "storage.mapping does not route" in out
        assert (_tree_digest(kb), _git(kb, "rev-parse", "HEAD")) == before
        assert not (_recipes(kb) / "loaf.md").exists()

    def test_lists_a_link_that_would_break_the_store_rule(
        self, kb: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A moved recipe linking to a private (rank 0) wiki note.
        _page(kb / "wiki" / "loaf.md", "recipe-loaf", "recipe", "Pistachio Loaf", "see [[Diary]]")
        plan = storage_move.plan_move(_cfg(kb), kb, "recipe", "recipes")
        assert [v.source_uid for v in plan.violations] == ["recipe-loaf"]
        assert plan.violations[0].target_uid == "note-diary"
        assert plan.violations[0].target_surface == "wiki"
        assert not plan.can_apply
        assert _run(kb, "recipe", "--to", "recipes") == 1
        out = capsys.readouterr().out
        assert "1 link(s) would break" in out and "recipe-loaf" in out and "note-diary" in out

    def test_a_private_page_linking_to_moved_pages_is_fine(self, kb: Path) -> None:
        plan = storage_move.plan_move(_cfg(kb), kb, "recipe", "recipes")
        assert plan.violations == ()  # diary -> loaf / soup points at a MORE shared store
        assert plan.can_apply

    def test_moving_a_page_that_a_shared_page_links_to_is_judged_too(self, kb: Path) -> None:
        cfg = _cfg(kb)
        cfg["storage"]["adapters"]["shared"] = {
            "backing_store": "markdown",
            "surface_root": str(kb.parent / "shared-repo"),
            "corpus_policy": {"embedded": True},
            "visibility": 2,
        }
        _init_repo(kb.parent / "shared-repo")
        _page(kb.parent / "shared-repo" / "menu.md", "menu-1", "menu", "Menu", "[[Leek Soup]]")
        plan = storage_move.plan_move(cfg, kb, "recipe", "recipes")
        assert [(v.source_uid, v.target_uid) for v in plan.violations] == [
            ("menu-1", "recipe-soup")
        ]


class TestApply:
    def test_refused_while_a_link_remains(
        self, kb: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _page(kb / "wiki" / "loaf.md", "recipe-loaf", "recipe", "Pistachio Loaf", "see [[Diary]]")
        _git(kb, "add", "-A")
        _git(kb, "commit", "-qm", "link")
        head = _git(kb, "rev-parse", "HEAD")
        assert _run(kb, "recipe", "--to", "recipes", "--apply") == 1
        assert "refused" in capsys.readouterr().err
        assert _git(kb, "rev-parse", "HEAD") == head
        assert (kb / "wiki" / "loaf.md").exists() and not (_recipes(kb) / "loaf.md").exists()

    def test_snapshots_moves_and_commits_both_repos(
        self, kb: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Uncommitted work in both repos is captured by the snapshot first.
        (kb / "wiki" / "scratch.md").write_text("---\ntype: note\n---\nwip\n")
        (_recipes(kb) / "wip.txt").write_text("wip\n")
        wiki_before = _git(kb, "rev-parse", "HEAD")
        recipes_before = _git(_recipes(kb), "rev-parse", "HEAD")
        assert _run(kb, "recipe", "--to", "recipes", "--apply") == 0
        out = capsys.readouterr().out
        assert "--undo" in out and "reindexed" in out

        log = _git(kb, "log", "--format=%s").splitlines()
        assert log[0].startswith("athenaeum: storage move-type recipe")
        assert log[1].startswith("athenaeum: snapshot before storage move-type")
        rlog = _git(_recipes(kb), "log", "--format=%s").splitlines()
        assert rlog[0].startswith("athenaeum: storage move-type recipe")
        assert rlog[1].startswith("athenaeum: snapshot before storage move-type")
        assert _git(kb, "status", "--porcelain") == ""
        assert _git(_recipes(kb), "status", "--porcelain") == ""
        assert _git(kb, "rev-parse", "HEAD~2") == wiki_before
        assert _git(_recipes(kb), "rev-parse", "HEAD~2") == recipes_before
        assert (_recipes(kb) / "loaf.md").is_file() and (_recipes(kb) / "soup.md").is_file()
        assert not (kb / "wiki" / "loaf.md").exists()
        assert (kb / "wiki" / "diary.md").is_file()
        assert (kb / "wiki" / "scratch.md").is_file()

    def test_nothing_to_move_is_refused(self, kb: Path) -> None:
        plan = storage_move.plan_move(_cfg(kb), kb, "ghost", "recipes")
        with pytest.raises(storage_move.MoveRefused, match="no page of type"):
            storage_move.apply_move(plan, _cfg(kb), kb)

    def test_destination_collision_is_refused(self, kb: Path) -> None:
        _page(_recipes(kb) / "loaf.md", "other", "recipe", "Other")
        plan = storage_move.plan_move(_cfg(kb), kb, "recipe", "recipes")
        assert plan.collisions and not plan.can_apply
        with pytest.raises(storage_move.MoveRefused, match="already exist"):
            storage_move.apply_move(plan, _cfg(kb), kb)

    def test_failure_midway_restores_both_repos(
        self, kb: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        digests = (_tree_digest(kb / "wiki"), _tree_digest(_recipes(kb)))
        calls = {"n": 0}
        real = storage_move.shutil.move

        def flaky(src: str, dst: str) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real(src, dst)

        monkeypatch.setattr(storage_move.shutil, "move", flaky)
        plan = storage_move.plan_move(_cfg(kb), kb, "recipe", "recipes")
        with pytest.raises(OSError):
            storage_move.apply_move(plan, _cfg(kb), kb)
        assert (_tree_digest(kb / "wiki"), _tree_digest(_recipes(kb))) == digests

    def test_failed_commit_restores_both_repos_and_writes_no_record(
        self, kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        digests = (_tree_digest(kb), _tree_digest(_recipes(kb)))
        heads = (_git(kb, "rev-parse", "HEAD"), _git(_recipes(kb), "rev-parse", "HEAD"))
        calls = {"n": 0}
        real = storage_move.commit_repo

        def flaky(repo: Path, label: str) -> str | None:
            calls["n"] += 1
            if calls["n"] == 4:  # 2 snapshots, then wiki commit ok, target commit fails
                raise subprocess.CalledProcessError(1, ["git", "commit"])
            return real(repo, label)

        monkeypatch.setattr(storage_move, "commit_repo", flaky)
        plan = storage_move.plan_move(_cfg(kb), kb, "recipe", "recipes")
        with pytest.raises(subprocess.CalledProcessError):
            storage_move.apply_move(plan, _cfg(kb), kb)
        assert calls["n"] == 4
        assert (_tree_digest(kb), _tree_digest(_recipes(kb))) == digests
        assert (_git(kb, "rev-parse", "HEAD"), _git(_recipes(kb), "rev-parse", "HEAD")) == heads
        assert _git(kb, "status", "--porcelain") == ""
        assert not list((tmp_path / "cache").glob("storage_moves/*.json"))


class TestAfterTheMove:
    @pytest.fixture
    def moved(self, kb: Path) -> Path:
        assert _run(kb, "recipe", "--to", "recipes", "--apply") == 0
        return kb

    def test_uids_are_unchanged(self, moved: Path) -> None:
        meta, _ = parse_frontmatter((_recipes(moved) / "loaf.md").read_text(encoding="utf-8"))
        assert meta["uid"] == "recipe-loaf"
        meta, _ = parse_frontmatter((_recipes(moved) / "soup.md").read_text(encoding="utf-8"))
        assert meta["uid"] == "recipe-soup"

    def test_links_from_private_pages_still_resolve(self, moved: Path) -> None:
        cfg = _cfg(moved)
        index = EntityIndex(moved / "wiki", resolve_locator_surfaces(moved, cfg))
        assert index.get_by_uid("recipe-soup") == (_recipes(moved) / "soup.md").resolve()
        hit = index.lookup("Pistachio Loaf")
        assert hit is not None and Path(hit.path) == (_recipes(moved) / "loaf.md").resolve()
        # ...and the store-link lint is clean: private -> shared is the allowed direction.
        assert main(["store-link-lint", "--path", str(moved)]) == 0

    def test_read_entity_works(self, moved: Path) -> None:
        out = json.loads(entity_read(moved, "recipe-loaf", page_class="recipe", config=_cfg(moved)))
        assert out.get("ok") is not False, out
        assert out["frontmatter"]["name"] == "Pistachio Loaf"

    def test_structured_upsert_works(self, moved: Path) -> None:
        raw_path = moved / "raw" / "r.md"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text(
            "---\nuid: recipe-loaf\ntype: recipe\nname: Pistachio Loaf\ntags:\n  - new\n---\n\n"
            "## Notes\n\nTry less sugar.\n",
            encoding="utf-8",
        )
        cfg = _cfg(moved)
        raw = RawFile(path=raw_path, source="r", timestamp="", uuid8="")
        index = EntityIndex(moved / "wiki", resolve_locator_surfaces(moved, cfg))
        out = tier0_structured_upsert(raw, index, moved / "wiki", ["recipe", "note"], config=cfg)
        assert out is not None and out.status == "applied", out
        meta, body = parse_frontmatter((_recipes(moved) / "loaf.md").read_text(encoding="utf-8"))
        assert meta["tags"] == ["base", "new"] and "Try less sugar." in body
        assert not (moved / "wiki" / "loaf.md").exists()

    def test_recall_finds_the_moved_page_after_apply_reindexed(
        self, moved: Path, tmp_path: Path
    ) -> None:
        cfg = _cfg(moved)
        rendered = recall_search(
            moved / "wiki",
            "zephyrloaf",
            top_k=3,
            search_backend="fts5",
            cache_dir=tmp_path / "cache",
            extra_roots=resolve_indexed_surfaces(moved, cfg, extra_roots=[]),
            config=cfg,
        )
        assert "Pistachio Loaf" in rendered
        manifest = json.loads((tmp_path / "cache" / "fts5-manifest.json").read_text())["hashes"]
        assert "recipes/loaf.md" in manifest and "loaf.md" not in manifest


class TestUndo:
    def _apply(self, kb: Path, capsys: pytest.CaptureFixture[str]) -> str:
        assert _run(kb, "recipe", "--to", "recipes", "--apply") == 0
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if "--undo" in ln)
        return line.split("--undo ")[1].split()[0]

    def test_undo_restores_both_repos_byte_for_byte(
        self, kb: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki_digest, recipes_digest = _tree_digest(kb / "wiki"), _tree_digest(_recipes(kb))
        heads = (_git(kb, "rev-parse", "HEAD"), _git(_recipes(kb), "rev-parse", "HEAD"))
        record_id = self._apply(kb, capsys)
        assert _tree_digest(_recipes(kb)) != recipes_digest
        assert _run(kb, "--undo", record_id) == 0
        assert _tree_digest(kb / "wiki") == wiki_digest
        assert _tree_digest(_recipes(kb)) == recipes_digest
        assert (_git(kb, "rev-parse", "HEAD"), _git(_recipes(kb), "rev-parse", "HEAD")) == heads
        assert _git(kb, "status", "--porcelain") == ""
        assert _git(_recipes(kb), "status", "--porcelain") == ""

    def test_undo_by_record_file_path(self, kb: Path, capsys: pytest.CaptureFixture[str]) -> None:
        record_id = self._apply(kb, capsys)
        path = storage_move.record_path(record_id)
        assert path.is_file()
        record = json.loads(path.read_text())
        assert set(record["repos"]) == {"wiki", "target"}
        assert _run(kb, "--undo", str(path)) == 0
        assert (kb / "wiki" / "loaf.md").is_file()

    def test_undo_refused_after_the_repo_moved_on(
        self, kb: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        record_id = self._apply(kb, capsys)
        (_recipes(kb) / "new.md").write_text("later work\n")
        _git(_recipes(kb), "add", "-A")
        _git(_recipes(kb), "commit", "-qm", "later")
        assert _run(kb, "--undo", record_id) == 1
        assert "moved on" in capsys.readouterr().err
        assert (_recipes(kb) / "loaf.md").is_file()
        assert _run(kb, "--undo", record_id, "--force") == 0
        assert (kb / "wiki" / "loaf.md").is_file()

    def test_undo_refused_with_uncommitted_changes(
        self, kb: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        record_id = self._apply(kb, capsys)
        (kb / "wiki" / "dirty.md").write_text("x\n")
        assert _run(kb, "--undo", record_id) == 1
        assert "uncommitted" in capsys.readouterr().err

    def test_help_documents_undo(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            main(["storage", "move-type", "--help"])
        out = capsys.readouterr().out
        assert "UNDO" in out and "--undo" in out and "--apply" in out


class TestTargetRefusals:
    def test_unconfigured_surface(self, kb: Path) -> None:
        with pytest.raises(storage_move.MoveRefused, match="not a configured target"):
            storage_move.plan_move(_cfg(kb), kb, "recipe", "nope")

    def test_surface_not_indexed(self, kb: Path) -> None:
        with pytest.raises(storage_move.MoveRefused, match="not indexed"):
            storage_move.plan_move(_cfg(kb), kb, "recipe", "excluded")

    def test_surface_not_its_own_repo(self, kb: Path) -> None:
        cfg = _cfg(kb)
        cfg["storage"]["adapters"]["plain"] = {
            "backing_store": "markdown",
            "surface_root": "plain",
            "corpus_policy": {"embedded": True},
        }
        (kb / "plain").mkdir()
        with pytest.raises(storage_move.MoveRefused, match="own git repository"):
            storage_move.plan_move(cfg, kb, "recipe", "plain")

    def test_wiki_not_in_git(self, kb: Path) -> None:
        import shutil

        shutil.rmtree(kb / ".git")
        with pytest.raises(storage_move.MoveRefused, match="wiki is not in a git"):
            storage_move.plan_move(_cfg(kb), kb, "recipe", "recipes")

    def test_default_wiki_surface_is_not_a_target(self, kb: Path) -> None:
        with pytest.raises(storage_move.MoveRefused, match="not a configured target"):
            storage_move.plan_move(_cfg(kb), kb, "recipe", "wiki-markdown-embedded")

    def test_mapping_note_absent_once_routed(self, kb: Path) -> None:
        cfg = _cfg(kb)
        cfg["storage"]["mapping"] = {"recipe": "recipes"}
        assert storage_move.plan_move(cfg, kb, "recipe", "recipes").mapping_routes_type

    def test_cli_requires_type_and_surface(
        self, kb: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _run(kb) == 2
        assert "required" in capsys.readouterr().err


class TestReadOnlyTarget:
    """move-type never writes, commits or resets a repository held by a read-only
    surface (athenaeum#2621)."""

    @staticmethod
    def _make_read_only(kb: Path) -> None:
        cfg = yaml.safe_load((kb / "athenaeum.yaml").read_text(encoding="utf-8"))
        cfg["storage"]["adapters"]["recipes"]["read_only"] = True
        (kb / "athenaeum.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    def test_plan_and_apply_are_refused_and_nothing_is_touched(
        self, kb: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._make_read_only(kb)
        (_recipes(kb) / "wip.txt").write_text("uncommitted\n")  # must stay uncommitted
        heads = (_git(kb, "rev-parse", "HEAD"), _git(_recipes(kb), "rev-parse", "HEAD"))
        digest = _tree_digest(kb / "wiki")
        for extra in ((), ("--apply",)):
            assert _run(kb, "recipe", "--to", "recipes", *extra) == 1
            assert "read_only" in capsys.readouterr().err
        assert (_git(kb, "rev-parse", "HEAD"), _git(_recipes(kb), "rev-parse", "HEAD")) == heads
        assert _git(_recipes(kb), "status", "--porcelain") == "?? wip.txt"
        assert _tree_digest(kb / "wiki") == digest

    def test_apply_move_rechecks_the_current_config(self, kb: Path) -> None:
        from athenaeum import storage_move
        from athenaeum.storage import ReadOnlySurfaceError

        plan = storage_move.plan_move(_cfg(kb), kb, "recipe", "recipes")  # writable then
        self._make_read_only(kb)
        (_recipes(kb) / "wip.txt").write_text("uncommitted\n")
        heads = (_git(kb, "rev-parse", "HEAD"), _git(_recipes(kb), "rev-parse", "HEAD"))
        with pytest.raises(ReadOnlySurfaceError):
            storage_move.apply_move(plan, _cfg(kb), kb)
        assert (_git(kb, "rev-parse", "HEAD"), _git(_recipes(kb), "rev-parse", "HEAD")) == heads
        assert (kb / "wiki" / "loaf.md").exists()  # no commit_repo, no page moved

    def test_undo_into_a_since_read_only_surface_is_refused(
        self, kb: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _run(kb, "recipe", "--to", "recipes", "--apply") == 0
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if "--undo" in ln)
        record_id = line.split("--undo ")[1].split()[0]
        self._make_read_only(kb)
        heads = (_git(kb, "rev-parse", "HEAD"), _git(_recipes(kb), "rev-parse", "HEAD"))
        for extra in ((), ("--force",)):
            assert _run(kb, "--undo", record_id, *extra) == 1
            assert "read_only" in capsys.readouterr().err
        assert (_git(kb, "rev-parse", "HEAD"), _git(_recipes(kb), "rev-parse", "HEAD")) == heads
        assert (_recipes(kb) / "loaf.md").is_file()
