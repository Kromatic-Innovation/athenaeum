"""Tests for the store seam's sync read side (athenaeum#2930), on real git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum.store import (
    changed_paths,
    common_ancestor,
    fetch_remote_branch,
    read_file_at_revision,
    remote_history_rewritten,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str, msg: str) -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bare remote, an install ``a`` and a second install ``b``, both cloned."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    a = tmp_path / "a"
    _git(tmp_path, "clone", "-q", str(remote), str(a))
    _git(a, "checkout", "-q", "-B", "main")
    _commit(a, "wiki/one.md", "v1\n", "first")
    _git(a, "push", "-q", "origin", "main")
    b = tmp_path / "b"
    _git(tmp_path, "clone", "-q", str(remote), str(b))
    return remote, a, b


def test_fetch_returns_remote_tip_without_moving_local_branch(pair) -> None:
    _, a, b = pair
    new = _commit(a, "wiki/two.md", "x\n", "second")
    _git(a, "push", "-q", "origin", "main")
    before = _git(b, "rev-parse", "HEAD")
    assert fetch_remote_branch(b, "main") == new
    assert _git(b, "rev-parse", "HEAD") == before
    assert not (b / "wiki" / "two.md").exists()


def test_fetch_failure_raises(pair) -> None:
    _, _, b = pair
    with pytest.raises(subprocess.CalledProcessError):
        fetch_remote_branch(b, "no-such-branch")


def test_common_ancestor_of_diverged_histories(pair) -> None:
    _, a, b = pair
    base = _git(a, "rev-parse", "HEAD")
    _commit(a, "wiki/a.md", "a\n", "a side")
    _git(a, "push", "-q", "origin", "main")
    local = _commit(b, "wiki/b.md", "b\n", "b side")
    tip = fetch_remote_branch(b, "main")
    assert common_ancestor(b, local, tip) == base


def test_common_ancestor_unrelated_is_none(pair) -> None:
    _, _, b = pair
    _git(b, "checkout", "-q", "--orphan", "other")
    _commit(b, "z.md", "z\n", "orphan")
    assert common_ancestor(b, "other", "main") is None


def test_read_file_at_old_revision_leaves_working_tree(pair) -> None:
    _, a, _ = pair
    old = _git(a, "rev-parse", "HEAD")
    _commit(a, "wiki/one.md", "v2\n", "edit")
    status_before = _git(a, "status", "--porcelain")
    assert read_file_at_revision(a, old, "wiki/one.md") == b"v1\n"
    assert (a / "wiki" / "one.md").read_text() == "v2\n"
    assert _git(a, "status", "--porcelain") == status_before
    assert read_file_at_revision(a, "HEAD", "wiki/one.md") == b"v2\n"


def test_read_file_absent_path_is_none_and_bad_rev_raises(pair) -> None:
    _, a, _ = pair
    assert read_file_at_revision(a, "HEAD", "wiki/missing.md") is None
    with pytest.raises(subprocess.CalledProcessError):
        read_file_at_revision(a, "deadbeef" * 5, "wiki/one.md")


def test_read_file_returns_binary_bytes_exactly(pair) -> None:
    _, a, _ = pair
    data = b"\x00\xff\r\n\x80"
    (a / "bin.dat").write_bytes(data)
    _git(a, "add", "-A")
    _git(a, "commit", "-q", "-m", "bin")
    assert read_file_at_revision(a, "HEAD", "bin.dat") == data


def test_changed_paths_add_modify_delete(pair) -> None:
    _, a, _ = pair
    old = _git(a, "rev-parse", "HEAD")
    _commit(a, "wiki/one.md", "v2\n", "edit")
    _commit(a, "wiki/new file.md", "n\n", "add")
    _git(a, "rm", "-q", "wiki/one.md")
    _git(a, "commit", "-q", "-m", "rm")
    assert changed_paths(a, old, "HEAD") == ["wiki/new file.md", "wiki/one.md"]
    assert changed_paths(a, "HEAD", "HEAD") == []


def test_rewritten_history_detected(pair) -> None:
    _, a, b = pair
    seen = fetch_remote_branch(b, "main")
    _git(a, "commit", "-q", "--amend", "-m", "rewritten", "--date=2001-01-01T00:00:00")
    _git(a, "push", "-q", "--force", "origin", "main")
    tip = fetch_remote_branch(b, "main")
    assert tip != seen
    assert remote_history_rewritten(b, seen, tip) is True


def test_fast_forward_is_not_rewritten(pair) -> None:
    _, a, b = pair
    seen = fetch_remote_branch(b, "main")
    _commit(a, "wiki/two.md", "x\n", "on top")
    _git(a, "push", "-q", "origin", "main")
    tip = fetch_remote_branch(b, "main")
    assert tip != seen
    assert remote_history_rewritten(b, seen, tip) is False


def test_unchanged_remote_and_first_sync_are_not_rewritten(pair) -> None:
    _, _, b = pair
    tip = fetch_remote_branch(b, "main")
    assert remote_history_rewritten(b, tip, tip) is False
    assert remote_history_rewritten(b, None, tip) is False
