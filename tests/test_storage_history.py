# SPDX-License-Identifier: Apache-2.0
"""``storage.history`` — a declared history setting (athenaeum#3160)."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
from pathlib import Path

import pytest

from athenaeum import doctor
from athenaeum.config import StorageConfigError, load_config, resolve_storage_history
from athenaeum.init import init_knowledge_dir
from athenaeum.storage import StorageConfigError as StorageReexport
from athenaeum.storage import open_store


def _fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "git-calls.log"
    exe = bindir / "git"
    exe.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_resolver_default_and_values() -> None:
    assert resolve_storage_history(None) == "git"
    assert resolve_storage_history({}) == "git"
    assert resolve_storage_history({"storage": {"history": "none"}}) == "none"
    assert resolve_storage_history({"storage": {"history": "git"}}) == "git"
    assert StorageReexport is StorageConfigError


@pytest.mark.parametrize("bad", ["svn", "", None, True, 3])
def test_resolver_rejects_other_values(bad: object) -> None:
    with pytest.raises(StorageConfigError):
        resolve_storage_history({"storage": {"history": bad}})


def test_init_no_history_runs_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from athenaeum._cmd_lifecycle import cmd_init

    log = _fake_git(tmp_path, monkeypatch)
    root = tmp_path / "kb"
    args = argparse.Namespace(
        path=root, no_history=True, with_templates=False, templates_dest=None,
        force=False, with_rules=False, rules_dest=None, owner_name=None,
    )
    cmd_init(args)
    assert not log.exists(), log.read_text() if log.exists() else ""
    assert not (root / ".git").exists()
    assert (root / "wiki" / "_schema" / "types.md").is_file()
    assert resolve_storage_history(load_config(root)) == "none"


def test_init_default_still_creates_repo(tmp_path: Path) -> None:
    root = init_knowledge_dir(tmp_path / "kb")
    assert (root / ".git").exists()
    assert resolve_storage_history(load_config(root)) == "git"
    assert "storage:\n  history" not in (root / "athenaeum.yaml").read_text()


def test_capabilities_declared_history_vs_versioned(tmp_path: Path) -> None:
    root = init_knowledge_dir(tmp_path / "kb")
    store = open_store({"storage": {"history": "none"}}, root)
    assert store.capabilities.history == "none"
    assert store.capabilities.versioned is True
    assert open_store(None, root).capabilities.history == "git"


def test_snapshot_none_calls_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = init_knowledge_dir(tmp_path / "kb")
    (root / "wiki" / "x.md").write_text("changed\n")
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout
    store = open_store({"storage": {"history": "none"}}, root)  # construction may probe git
    plain = tmp_path / "plain"
    plain.mkdir()
    plain_store = open_store({"storage": {"history": "none"}}, plain)
    log = _fake_git(tmp_path, monkeypatch)
    assert store.snapshot("label") is None  # a repository exists
    assert plain_store.snapshot("l") is None
    assert not log.exists()
    monkeypatch.undo()
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout
    assert before == after


def _own_repo_config(repo: Path, **extra: object) -> dict:
    return {
        "storage": {
            "adapters": {
                "recipes": {
                    "backing_store": "markdown",
                    "surface_root": str(repo),
                    **extra,
                }
            }
        }
    }


def _init_repo(path: Path) -> None:
    path.mkdir()
    for cmd in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@x"]):
        subprocess.run(["git", *cmd], cwd=path, check=True, capture_output=True)
    (path / "a.md").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True, capture_output=True)


def test_per_location_override_keeps_its_own_history(tmp_path: Path) -> None:
    root = init_knowledge_dir(tmp_path / "kb")
    repo = tmp_path / "recipes"
    _init_repo(repo)
    (repo / "a.md").write_text("edited\n")
    cfg = _own_repo_config(repo, history="git")
    cfg["storage"]["history"] = "none"
    store = open_store(cfg, root)
    assert store.capabilities.history == "none"
    assert store.capabilities_for("recipes").history == "git"
    assert store.capabilities_for("recipes").versioned is True
    assert store.snapshot("l") is None
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert status == ""  # the location committed into its own repository


def test_per_location_none_under_git_installation(tmp_path: Path) -> None:
    root = init_knowledge_dir(tmp_path / "kb")
    repo = tmp_path / "recipes"
    _init_repo(repo)
    (repo / "a.md").write_text("edited\n")
    store = open_store(_own_repo_config(repo, history="none"), root)
    assert store.capabilities_for("recipes").history == "none"
    store.snapshot("l")
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "a.md" in status  # not committed
    assert open_store(_own_repo_config(repo), root).capabilities_for("recipes").history == "git"


def test_per_location_override_validated(tmp_path: Path) -> None:
    with pytest.raises(StorageConfigError, match="storage.adapters.recipes.history"):
        open_store(_own_repo_config(tmp_path / "r", history="svn"), tmp_path)


def test_doctor_reports_history(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    r = doctor.check_storage_history({}, root)
    assert r.status == "warn" and "destructive operations will be refused" in r.detail
    r = doctor.check_storage_history({"storage": {"history": "none"}}, root)
    assert r.status == "ok" and "history: none" in r.detail
    (root / ".git").mkdir()
    r = doctor.check_storage_history({}, root)
    assert r.status == "ok" and "history: git" in r.detail
    r = doctor.check_storage_history({"storage": {"history": "x"}}, root)
    assert r.status == "fail" and r.fix
