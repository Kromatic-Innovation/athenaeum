"""A two-clone sync harness: one bare remote and two installs (athenaeum#2932).

Later sync slices drive the sequence of the multi-writer sync design note
(fetch, merge commit, push, retry) end to end against this fixture. Everything
lives under one temporary directory and talks over the local file transport, so
there is no network and no model call.

Each :class:`Install` has its own clone (its working tree), its own git identity
and its own ``config`` directory, so two installs never share state except
through the remote. All git traffic goes through the store seam
(``fetch_remote_branch``, ``write_merge_commit``, ``push_branch``); plain git is
used only to set the fixture up and to read results back.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from athenaeum.store import (
    PushResult,
    fetch_remote_branch,
    push_branch,
    write_merge_commit,
)

BRANCH = "main"


def git(cwd: Path, *args: str) -> str:
    """Run git in *cwd*, returning stripped stdout; raises on failure."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass(frozen=True)
class Install:
    """One install: a clone of the shared remote with its own configuration."""

    name: str
    root: Path  # the working tree
    config_dir: Path  # this install's own configuration directory
    branch: str = BRANCH

    def head(self) -> str:
        return git(self.root, "rev-parse", self.branch)

    def commit(self, path: str, text: str, message: str | None = None) -> str:
        """Write *path* in the working tree and commit it; returns the new SHA."""
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(self.root, "add", "-A", "--", path)
        git(self.root, "commit", "-q", "-m", message or f"{self.name}: {path}")
        return self.head()

    # -- the store seam ---------------------------------------------------

    def fetch(self) -> str:
        """Fetch the remote branch through the seam; returns the fetched tip."""
        return fetch_remote_branch(self.root, self.branch)

    def merge(
        self, remote_tip: str, changes: Mapping[str, bytes | None], message: str = "merge"
    ) -> str:
        """Write the merge commit of the local branch and *remote_tip* through the seam."""
        return write_merge_commit(self.root, self.branch, remote_tip, changes, message)

    def push(self) -> PushResult:
        """Push the branch through the seam (never forced)."""
        return push_branch(self.root, self.branch)


@dataclass(frozen=True)
class TwoCloneHarness:
    """A bare remote and two installs cloned from it."""

    remote: Path
    first: Install
    second: Install

    def diverge(
        self,
        first_change: tuple[str, str] = ("wiki/first.md", "first\n"),
        second_change: tuple[str, str] = ("wiki/second.md", "second\n"),
    ) -> tuple[str, str]:
        """Each install commits a different change; nothing is pushed or fetched.

        Returns ``(first_sha, second_sha)``.
        """
        return (self.first.commit(*first_change), self.second.commit(*second_change))

    def diverge_and_first_pushes(self) -> tuple[str, str]:
        """Diverge, then let the first install win the push race.

        Returns ``(first_sha, second_sha)``; the remote is at ``first_sha`` and
        the second install has not yet fetched.
        """
        first_sha, second_sha = self.diverge()
        pushed = self.first.push()
        assert pushed.ok, pushed
        return first_sha, second_sha

    def remote_tip(self) -> str:
        return git(self.remote, "rev-parse", f"refs/heads/{BRANCH}")


def _make_install(base: Path, remote: Path, name: str) -> Install:
    root = base / name / "tree"
    root.parent.mkdir(parents=True)
    git(base, "clone", "-q", str(remote), str(root))
    git(root, "checkout", "-q", "-B", BRANCH, f"origin/{BRANCH}")
    git(root, "config", "user.name", f"install {name}")
    git(root, "config", "user.email", f"{name}@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    config_dir = base / name / "config"
    config_dir.mkdir()
    (config_dir / "install.txt").write_text(f"install={name}\n")
    return Install(name=name, root=root, config_dir=config_dir)


def make_two_clone_harness(base: Path, seed: Mapping[str, str] | None = None) -> TwoCloneHarness:
    """Build the harness under *base*: seed commit on a bare remote, two clones of it."""
    remote = base / "remote.git"
    git(base, "init", "-q", "--bare", "-b", BRANCH, str(remote))
    seeder = base / "seed"
    git(base, "clone", "-q", str(remote), str(seeder))
    git(seeder, "checkout", "-q", "-B", BRANCH)
    git(seeder, "config", "user.name", "seed")
    git(seeder, "config", "user.email", "seed@example.invalid")
    git(seeder, "config", "commit.gpgsign", "false")
    for rel, text in (seed or {"wiki/base.md": "base\n"}).items():
        path = seeder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(seeder, "add", "-A")
    git(seeder, "commit", "-q", "-m", "seed")
    git(seeder, "push", "-q", "origin", BRANCH)
    return TwoCloneHarness(
        remote=remote,
        first=_make_install(base, remote, "first"),
        second=_make_install(base, remote, "second"),
    )
