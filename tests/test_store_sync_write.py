"""Tests for the store seam's sync write side (athenaeum#2931), on real git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import athenaeum.store as store
from athenaeum.store import (
    PushFailureReason,
    StoreCapabilities,
    WorkingTreeNotCleanError,
    fetch_remote_branch,
    push_branch,
    write_merge_commit,
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
    """A bare remote, install ``a`` and install ``b``, both cloned."""
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


@pytest.fixture
def diverged(pair):
    """``b`` has a local commit; ``a`` pushed another; ``b`` fetched. Returns (b, local, remote)."""
    _, a, b = pair
    _commit(a, "wiki/a.md", "a\n", "a side")
    _git(a, "push", "-q", "origin", "main")
    local = _commit(b, "wiki/b.md", "b\n", "b side")
    remote_tip = fetch_remote_branch(b, "main")
    return b, local, remote_tip


_MERGED = {"wiki/a.md": b"a\n", "wiki/merged.md": b"both\n", "wiki/one.md": None}


# -- AC1: one two-parent merge commit, branch moved last --------------------


def test_merge_commit_has_two_parents_and_merged_tree(diverged) -> None:
    b, local, remote_tip = diverged
    sha = write_merge_commit(b, "main", remote_tip, _MERGED, "merge remote")
    assert _git(b, "rev-parse", "main") == sha == _git(b, "rev-parse", "HEAD")
    assert _git(b, "rev-list", "--parents", "-n1", sha).split() == [sha, local, remote_tip]
    assert _git(b, "log", "-1", "--format=%s", sha) == "merge remote"
    assert (b / "wiki" / "merged.md").read_text() == "both\n"
    assert (b / "wiki" / "a.md").read_text() == "a\n"
    assert (b / "wiki" / "b.md").read_text() == "b\n"  # untouched path keeps the local side
    assert not (b / "wiki" / "one.md").exists()
    assert _git(b, "status", "--porcelain") == ""


def test_merge_commit_on_branch_not_checked_out(diverged) -> None:
    b, local, remote_tip = diverged
    _git(b, "checkout", "-q", "-b", "side")
    sha = write_merge_commit(b, "main", remote_tip, _MERGED, "merge")
    assert _git(b, "rev-parse", "main") == sha
    assert _git(b, "rev-parse", "HEAD") != sha  # the checked-out branch is not moved
    assert _git(b, "rev-list", "--parents", "-n1", sha).split()[1:] == [local, remote_tip]


def test_merge_commit_refuses_dirty_checked_out_branch(diverged) -> None:
    b, local, remote_tip = diverged
    (b / "wiki" / "b.md").write_text("edited, uncommitted\n")
    with pytest.raises(WorkingTreeNotCleanError):
        write_merge_commit(b, "main", remote_tip, _MERGED, "merge")
    assert _git(b, "rev-parse", "main") == local
    assert (b / "wiki" / "b.md").read_text() == "edited, uncommitted\n"


# -- AC2: a failure before the branch moves changes nothing -----------------


def _snapshot(repo: Path) -> tuple[str, str, dict[str, bytes]]:
    files = {
        str(p.relative_to(repo)): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }
    return _git(repo, "rev-parse", "main"), _git(repo, "status", "--porcelain"), files


@pytest.mark.parametrize("failing_step", ["hash-object", "write-tree", "commit-tree"])
def test_failure_before_ref_move_leaves_branch_and_tree_untouched(
    diverged, monkeypatch, failing_step: str
) -> None:
    b, local, remote_tip = diverged
    before = _snapshot(b)
    real_run = subprocess.run

    def flaky(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", failing_step]:
            raise subprocess.CalledProcessError(128, cmd, b"", b"injected failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(store.subprocess, "run", flaky)
    with pytest.raises(subprocess.CalledProcessError):
        write_merge_commit(b, "main", remote_tip, _MERGED, "merge")
    monkeypatch.undo()

    assert _snapshot(b) == before
    assert _git(b, "rev-parse", "main") == local
    for _path, data in before[2].items():
        assert b"<<<<<<<" not in data and b">>>>>>>" not in data


def test_failure_before_ref_move_never_attaches_commit(diverged, monkeypatch) -> None:
    b, local, remote_tip = diverged

    def boom(*_a, **_k):
        raise RuntimeError("crash after commit built, before branch moved")

    monkeypatch.setattr(store, "_move_branch", boom)
    with pytest.raises(RuntimeError):
        write_merge_commit(b, "main", remote_tip, _MERGED, "merge")
    monkeypatch.undo()
    assert _git(b, "rev-parse", "main") == local
    assert _git(b, "status", "--porcelain") == ""
    assert not (b / "wiki" / "merged.md").exists()


# -- AC3: never forced; failure reasons distinguishable ---------------------


def test_push_fast_forward_succeeds(pair) -> None:
    remote, _, b = pair
    sha = _commit(b, "wiki/b.md", "b\n", "b side")
    result = push_branch(b, "main")
    assert result.ok and result.reason is None
    assert _git(remote, "rev-parse", "main") == sha


def test_push_rejected_because_remote_moved_is_not_forced(pair) -> None:
    remote, a, b = pair
    theirs = _commit(a, "wiki/a.md", "a\n", "a side")
    _git(a, "push", "-q", "origin", "main")
    _commit(b, "wiki/b.md", "b\n", "b side")
    result = push_branch(b, "main")
    assert not result.ok
    assert result.reason is PushFailureReason.REJECTED_REMOTE_MOVED
    assert _git(remote, "rev-parse", "main") == theirs  # remote untouched


def test_push_rejected_after_fetch_is_still_remote_moved(diverged) -> None:
    b, _, _ = diverged  # objects fetched, so git says non-fast-forward not fetch-first
    result = push_branch(b, "main")
    assert result.reason is PushFailureReason.REJECTED_REMOTE_MOVED


def test_push_to_unreachable_remote_is_other(pair, tmp_path: Path) -> None:
    _, _, b = pair
    _commit(b, "wiki/b.md", "b\n", "b side")
    _git(b, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    result = push_branch(b, "main")
    assert not result.ok
    assert result.reason is PushFailureReason.OTHER
    assert result.detail


def test_push_without_permission_is_other(pair) -> None:
    remote, _, b = pair
    _commit(b, "wiki/b.md", "b\n", "b side")
    dirs = [remote, *(p for p in remote.rglob("*") if p.is_dir())]
    for d in dirs:
        d.chmod(0o555)
    try:
        result = push_branch(b, "main")
    finally:
        for d in dirs:
            d.chmod(0o755)
    assert not result.ok
    assert result.reason is PushFailureReason.OTHER


def test_push_ignores_a_forcing_refspec_in_config(pair) -> None:
    remote, a, b = pair
    theirs = _commit(a, "wiki/a.md", "a\n", "a side")
    _git(a, "push", "-q", "origin", "main")
    _commit(b, "wiki/b.md", "b\n", "b side")
    _git(b, "config", "remote.origin.push", "+refs/heads/main:refs/heads/main")
    assert push_branch(b, "main").reason is PushFailureReason.REJECTED_REMOTE_MOVED
    assert _git(remote, "rev-parse", "main") == theirs


# -- AC4: multi_writer capability invariant ---------------------------------


def _caps(**over) -> StoreCapabilities:
    base = dict(
        classes=frozenset({"source"}),
        operational_scopes=frozenset(),
        versioned=True,
        purgeable=True,
        compare_and_swap=True,
        leases=True,
        append=True,
        bulk_list=True,
        bulk_read=True,
        cheap_local_scan=True,
    )
    base.update(over)
    return StoreCapabilities(**base)  # type: ignore[arg-type]


def test_multi_writer_defaults_false() -> None:
    assert _caps().multi_writer is False


def test_multi_writer_requires_git_and_remote() -> None:
    assert _caps(multi_writer=True, has_remote=True).multi_writer is True
    with pytest.raises(ValueError, match="multi_writer"):
        _caps(multi_writer=True, has_remote=True, versioned=False)
    with pytest.raises(ValueError, match="multi_writer"):
        _caps(multi_writer=True, has_remote=False)
    with pytest.raises(ValueError, match="multi_writer"):
        _caps(multi_writer=True)


def test_filesystem_store_declares_multi_writer_only_with_git_and_remote(
    pair, tmp_path: Path
) -> None:
    _, _, b = pair
    fs = store.FilesystemStore(b, {"wiki": b / "wiki"})
    assert fs.capabilities.versioned and fs.capabilities.multi_writer
    _git(b, "remote", "remove", "origin")
    no_remote = store.FilesystemStore(b, {"wiki": b / "wiki"})
    assert no_remote.capabilities.versioned and not no_remote.capabilities.multi_writer
    plain = tmp_path / "plain"
    plain.mkdir()
    not_git = store.FilesystemStore(plain, {"wiki": plain / "wiki"})
    assert not not_git.capabilities.multi_writer


# -- AC5: nothing rebases or rewrites an existing commit --------------------


def test_existing_commits_preserved_through_merge_and_push(diverged) -> None:
    b, local, remote_tip = diverged
    history_before = _git(b, "rev-list", "main").split()
    sha = write_merge_commit(b, "main", remote_tip, _MERGED, "merge")
    assert push_branch(b, "main").ok
    history_after = _git(b, "rev-list", "main").split()
    assert set(history_before) <= set(history_after)
    # the remote's commit and the merge are the only additions
    assert history_after[0] == sha and len(history_after) == len(history_before) + 2
    assert _git(b, "merge-base", "--is-ancestor", local, sha) == ""
    assert _git(b, "merge-base", "--is-ancestor", remote_tip, sha) == ""


def test_no_rewriting_git_command_is_ever_run(diverged, monkeypatch) -> None:
    b, _, remote_tip = diverged
    seen: list[list[str]] = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            seen.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(store.subprocess, "run", spy)
    write_merge_commit(b, "main", remote_tip, _MERGED, "merge")
    push_branch(b, "main")
    verbs = {c[1] for c in seen}
    assert verbs.isdisjoint({"rebase", "reset", "filter-branch", "replace", "cherry-pick"})
    flat = [arg for c in seen for arg in c]
    assert "--amend" not in flat
    assert not any(a in ("--force", "-f", "--force-with-lease") or a.startswith("+") for a in flat)
