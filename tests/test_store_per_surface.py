# SPDX-License-Identifier: Apache-2.0
"""Per-surface capabilities, snapshot, and git pull/push (design note §4.5 R2,
issue athenaeum#2615).

Every git repository here lives under ``tmp_path``; remotes are local bare
repositories, so nothing touches the network or the operator's own config.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum import librarian
from athenaeum.storage import open_store
from athenaeum.store import (
    FilesystemStore,
    Store,
    StoreCapabilityError,
    StoreKey,
    UnknownSurfaceError,
)
from athenaeum.store_conformance import StoreConformanceTests


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


@pytest.fixture()
def two_repo_store(tmp_path: Path) -> FilesystemStore:
    """knowledge root (plain dir) + one surface in its own git repo + one in a
    plain directory — two roots in two places, one versioned, one not."""
    kr = tmp_path / "kr"
    kr.mkdir()
    repo = _init_repo(tmp_path / "recipes")
    plain = tmp_path / "scratch"
    plain.mkdir()
    return FilesystemStore(kr, {"recipes": repo, "scratch": plain})


class TestCapabilitiesFor:
    def test_versioned_only_when_surface_root_is_in_a_git_tree(
        self, two_repo_store: FilesystemStore
    ) -> None:
        assert two_repo_store.capabilities_for("recipes").versioned is True
        assert two_repo_store.capabilities_for("scratch").versioned is False

    def test_store_level_capabilities_unchanged(self, two_repo_store: FilesystemStore) -> None:
        # The default (knowledge-root) profile: no .git there.
        assert two_repo_store.capabilities.versioned is False

    def test_unknown_surface_raises(self, two_repo_store: FilesystemStore) -> None:
        with pytest.raises(UnknownSurfaceError):
            two_repo_store.capabilities_for("nope")

    def test_surface_inside_knowledge_repo_is_versioned(self, tmp_path: Path) -> None:
        kr = _init_repo(tmp_path / "kr")
        (kr / "wiki").mkdir()
        store = FilesystemStore(kr, {"wiki": kr / "wiki"})
        assert store.capabilities_for("wiki").versioned is True
        assert store.capabilities.versioned is True
        assert store.own_repo_surfaces() == {}

    def test_ancestor_repo_of_unversioned_knowledge_root_is_ignored(
        self, tmp_path: Path
    ) -> None:
        outer = _init_repo(tmp_path / "outer")
        kr = outer / "kr"
        kr.mkdir()
        store = FilesystemStore(kr, {"wiki": kr / "wiki"})
        assert store.capabilities_for("wiki").versioned is False
        assert store.snapshot("x") is None


class TestSnapshot:
    def test_commits_every_changed_versioned_surface(
        self, two_repo_store: FilesystemStore, tmp_path: Path
    ) -> None:
        two_repo_store.put(StoreKey("recipes", "a.md"), b"a")
        two_repo_store.put(StoreKey("scratch", "b.md"), b"b")
        points = two_repo_store.snapshot_surfaces("snap")
        assert set(points) == {"recipes"}
        assert points["recipes"] == _git(tmp_path / "recipes", "rev-parse", "HEAD")
        assert _git(tmp_path / "recipes", "status", "--porcelain") == ""

    def test_clean_surface_has_no_restore_point(self, two_repo_store: FilesystemStore) -> None:
        assert two_repo_store.snapshot_surfaces("snap") == {}

    def test_plain_snapshot_also_commits_own_repo_surfaces(
        self, two_repo_store: FilesystemStore, tmp_path: Path
    ) -> None:
        two_repo_store.put(StoreKey("recipes", "a.md"), b"a")
        before = _git(tmp_path / "recipes", "rev-parse", "HEAD")
        assert two_repo_store.snapshot("snap") is None  # knowledge root is not git
        assert _git(tmp_path / "recipes", "rev-parse", "HEAD") != before

    def test_surfaces_in_one_repo_share_a_restore_point(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "shared")
        (repo / "x").mkdir()
        (repo / "y").mkdir()
        store = FilesystemStore(tmp_path, {"x": repo / "x", "y": repo / "y"})
        store.put(StoreKey("x", "a.md"), b"a")
        points = store.snapshot_surfaces("snap")
        assert set(points) == {"x", "y"} and points["x"] == points["y"]


class TestDestructiveRefusal:
    def _store(self, tmp_path: Path, *, versioned: bool) -> FilesystemStore:
        root = _init_repo(tmp_path / "s") if versioned else tmp_path / "s"
        root.mkdir(exist_ok=True)
        (root / "f.md").write_text("x")
        return FilesystemStore(tmp_path / "kr", {"s": root}, non_purgeable_surfaces={"s"})

    def test_neither_versioned_nor_purgeable_refuses_delete_and_move(
        self, tmp_path: Path
    ) -> None:
        store = self._store(tmp_path, versioned=False)
        with pytest.raises(StoreCapabilityError):
            store.delete(StoreKey("s", "f.md"))
        with pytest.raises(StoreCapabilityError):
            store.move(StoreKey("s", "f.md"), StoreKey("s", "g.md"))
        assert (tmp_path / "s" / "f.md").exists()

    def test_versioned_non_purgeable_is_allowed(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, versioned=True)
        assert store.delete(StoreKey("s", "f.md")) is True

    def test_default_surfaces_stay_purgeable(self, two_repo_store: FilesystemStore) -> None:
        two_repo_store.put(StoreKey("scratch", "b.md"), b"b")
        assert two_repo_store.delete(StoreKey("scratch", "b.md")) is True

    def test_adapter_config_declares_non_purgeable(self, tmp_path: Path) -> None:
        cfg = {
            "storage": {
                "adapters": {
                    "locked": {
                        "backing_store": "markdown",
                        "surface_root": str(tmp_path / "locked"),
                        "purgeable": False,
                    }
                }
            }
        }
        (tmp_path / "locked").mkdir()
        (tmp_path / "locked" / "f.md").write_text("x")
        store = open_store(cfg, tmp_path / "kr")
        assert store.capabilities_for("locked").purgeable is False
        with pytest.raises(StoreCapabilityError):
            store.delete(StoreKey("locked", "f.md"))


class TestNoExtraSurfacesUnchanged:
    def test_default_deployment_has_no_own_repos_and_same_snapshot(self, tmp_path: Path) -> None:
        kr = _init_repo(tmp_path / "kr")
        store = open_store(None, kr)
        assert store.own_repo_surfaces() == {}
        assert store.sync_repos() == {}
        assert store.capabilities.versioned is True
        assert store.snapshot("clean") is None
        (kr / "note.md").write_text("n")
        sha = store.snapshot("dirty")
        assert sha == _git(kr, "rev-parse", "HEAD")

    def test_sync_state_stays_empty(self, tmp_path: Path) -> None:
        kr = _init_repo(tmp_path / "kr")
        _with_remote(kr, tmp_path, "kr")
        state = librarian.SurfaceSyncState()
        librarian._maybe_pull_before_run(
            kr, config=None, pull_before_run=True, dry_run=False, surface_sync=state
        )
        librarian._capture_surface_heads(kr, config=None, surface_sync=state)
        librarian._maybe_push_after_run(
            kr,
            config=None,
            push_after_run=True,
            dry_run=False,
            head_at_start=_git(kr, "rev-parse", "HEAD"),
            surface_sync=state,
        )
        assert state.results == [] and state.heads_at_start == {}


class TestSurfaceSync:
    def _setup(self, tmp_path: Path) -> tuple[Path, Path, Path, dict]:
        kr = tmp_path / "kr"
        kr.mkdir()
        repo = _init_repo(tmp_path / "recipes")
        bare = _with_remote(repo, tmp_path, "recipes")
        cfg = {
            "storage": {
                "adapters": {
                    "recipes": {"backing_store": "markdown", "surface_root": str(repo)}
                }
            }
        }
        return kr, repo, bare, cfg

    def test_pull_then_push_per_surface(self, tmp_path: Path) -> None:
        kr, repo, bare, cfg = self._setup(tmp_path)
        # Someone else advances the remote.
        other = tmp_path / "other"
        _git(tmp_path, "clone", "-q", str(bare), str(other))
        _git(other, "config", "user.email", "bob@example.com")
        _git(other, "config", "user.name", "Bob Example")
        (other / "remote.txt").write_text("r")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "remote work")
        _git(other, "push", "-q", "origin", "main")

        state = librarian.SurfaceSyncState()
        librarian._maybe_pull_before_run(
            kr, config=cfg, pull_before_run=True, dry_run=False, surface_sync=state
        )
        assert (repo / "remote.txt").exists()
        librarian._capture_surface_heads(kr, config=cfg, surface_sync=state)

        # The run snapshots (through the store) and pushes.
        (repo / "mine.md").write_text("m")
        open_store(cfg, kr).snapshot("run")
        librarian._maybe_push_after_run(
            kr,
            config=cfg,
            push_after_run=True,
            dry_run=False,
            head_at_start=None,
            surface_sync=state,
        )
        assert _git(bare, "log", "-1", "--format=%s", "main") == "run"
        outcomes = {(r["surface"], r["op"]): r["outcome"] for r in state.results}
        assert outcomes == {("recipes", "pull"): "ok", ("recipes", "push"): "ok"}
        assert state.as_profile_fields()["reason"] == "completed"

    def test_knowledge_repo_push_branch_is_not_forced_onto_a_surface_repo(
        self, tmp_path: Path
    ) -> None:
        # librarian.push_branch names the KNOWLEDGE repo's branch; a surface
        # repo with its own branch layout syncs its own current branch.
        kr, repo, bare, cfg = self._setup(tmp_path)
        cfg["librarian"] = {"push_branch": "kb-only-branch"}
        state = librarian.SurfaceSyncState()
        librarian._maybe_pull_before_run(
            kr, config=cfg, pull_before_run=True, dry_run=False, surface_sync=state
        )
        librarian._capture_surface_heads(kr, config=cfg, surface_sync=state)
        (repo / "mine.md").write_text("m")
        open_store(cfg, kr).snapshot("run")
        librarian._maybe_push_after_run(
            kr,
            config=cfg,
            push_after_run=True,
            dry_run=False,
            head_at_start=None,
            surface_sync=state,
        )
        assert _git(bare, "log", "-1", "--format=%s", "main") == "run"
        outcomes = {(r["surface"], r["op"]): r["outcome"] for r in state.results}
        assert outcomes == {("recipes", "pull"): "ok", ("recipes", "push"): "ok"}

    def test_unchanged_head_is_reported_skipped_not_silent(self, tmp_path: Path) -> None:
        kr, repo, _bare, cfg = self._setup(tmp_path)
        state = librarian.SurfaceSyncState()
        librarian._capture_surface_heads(kr, config=cfg, surface_sync=state)
        librarian._maybe_push_after_run(
            kr,
            config=cfg,
            push_after_run=True,
            dry_run=False,
            head_at_start=None,
            surface_sync=state,
        )
        assert state.results == [
            {"surface": "recipes", "op": "push", "outcome": "skipped", "reason": "no-new-commits"}
        ]

    def test_failures_are_reported_per_surface(self, tmp_path: Path) -> None:
        kr, repo, bare, cfg = self._setup(tmp_path)
        state = librarian.SurfaceSyncState()
        librarian._capture_surface_heads(kr, config=cfg, surface_sync=state)
        (repo / "mine.md").write_text("m")
        open_store(cfg, kr).snapshot("run")
        _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
        librarian._maybe_push_after_run(
            kr,
            config=cfg,
            push_after_run=True,
            dry_run=False,
            head_at_start=None,
            surface_sync=state,
        )
        assert [(r["surface"], r["op"], r["outcome"]) for r in state.results] == [
            ("recipes", "push", "failed")
        ]
        fields = state.as_profile_fields()
        assert fields["failed"] == "recipes:push" and fields["reason"] == "failed"

    def test_pull_failure_is_reported(self, tmp_path: Path) -> None:
        kr, repo, _bare, cfg = self._setup(tmp_path)
        _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
        state = librarian.SurfaceSyncState()
        librarian._maybe_pull_before_run(
            kr, config=cfg, pull_before_run=True, dry_run=False, surface_sync=state
        )
        assert [(r["surface"], r["op"], r["outcome"]) for r in state.results] == [
            ("recipes", "pull", "failed")
        ]

    def test_repo_without_remote_is_not_synced(self, tmp_path: Path) -> None:
        kr = tmp_path / "kr"
        kr.mkdir()
        repo = _init_repo(tmp_path / "local-only")
        store = FilesystemStore(kr, {"r": repo})
        assert store.own_repo_surfaces() == {repo: ("r",)}
        assert store.sync_repos() == {}


# --- conformance: two roots in two repositories, one versioned, one not -----


def _two_root_store(tmp_path: Path) -> FilesystemStore:
    kr = tmp_path / "kr"
    kr.mkdir()
    versioned = _init_repo(tmp_path / "versioned-repo")
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    return FilesystemStore(kr, {"versioned": versioned, "plain": plain})


class TestConformanceVersionedRoot(StoreConformanceTests):
    surface_name = "versioned"

    @pytest.fixture()
    def store(self, tmp_path: Path) -> Store:
        return _two_root_store(tmp_path)

    def test_this_root_is_versioned_and_the_other_is_not(self, store: Store) -> None:
        assert isinstance(store, FilesystemStore)
        assert store.capabilities_for("versioned").versioned is True
        assert store.capabilities_for("plain").versioned is False


class TestConformancePlainRoot(StoreConformanceTests):
    surface_name = "plain"

    @pytest.fixture()
    def store(self, tmp_path: Path) -> Store:
        return _two_root_store(tmp_path)

    def test_this_root_is_not_versioned_and_the_other_is(self, store: Store) -> None:
        assert isinstance(store, FilesystemStore)
        assert store.capabilities_for("plain").versioned is False
        assert store.capabilities_for("versioned").versioned is True
