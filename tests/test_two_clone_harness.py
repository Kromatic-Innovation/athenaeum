"""Tests for the two-clone sync harness (athenaeum#2932)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from athenaeum.store import PushFailureReason
from tests.fixtures.two_clone_harness import TwoCloneHarness, git, make_two_clone_harness


@pytest.fixture
def harness(tmp_path: Path) -> TwoCloneHarness:
    return make_two_clone_harness(tmp_path)


def test_fixture_builds_bare_remote_and_two_separate_installs(
    harness: TwoCloneHarness, tmp_path: Path
) -> None:
    assert git(harness.remote, "rev-parse", "--is-bare-repository") == "true"
    a, b = harness.first, harness.second
    assert a.root != b.root and a.config_dir != b.config_dir
    for inst in (a, b):
        assert tmp_path in inst.root.parents and tmp_path in inst.config_dir.parents
        assert (inst.root / ".git").exists() and (inst.root / "wiki" / "base.md").exists()
        assert Path(git(inst.root, "remote", "get-url", "origin")) == harness.remote
        assert inst.head() == harness.remote_tip()
        assert git(inst.root, "config", "user.name") == f"install {inst.name}"
        assert (inst.config_dir / "install.txt").read_text() == f"install={inst.name}\n"
    assert git(a.root, "config", "user.email") != git(b.root, "config", "user.email")


def test_installs_have_independent_working_trees(harness: TwoCloneHarness) -> None:
    harness.first.commit("wiki/only-first.md", "x\n")
    assert not (harness.second.root / "wiki" / "only-first.md").exists()


def test_diverge_then_fetch_merge_push_through_the_seam(harness: TwoCloneHarness) -> None:
    first_sha, second_sha = harness.diverge()
    assert first_sha != second_sha
    base = harness.remote_tip()

    assert harness.first.push().ok
    tip = harness.second.fetch()
    assert tip == first_sha
    assert git(harness.second.root, "merge-base", first_sha, second_sha) == base
    merged = harness.second.merge(tip, {"wiki/first.md": b"first\n"}, "merge first")
    assert git(harness.second.root, "rev-list", "--parents", "-n1", merged).split() == [
        merged,
        second_sha,
        first_sha,
    ]
    assert harness.second.push().ok
    assert harness.remote_tip() == merged

    harness.first.merge(harness.first.fetch(), {})  # converge: first takes the merge
    assert harness.first.head() == merged == harness.second.head()


def test_second_push_rejected_after_first_pushed_reports_remote_moved(
    harness: TwoCloneHarness,
) -> None:
    first_sha, _ = harness.diverge_and_first_pushes()
    result = harness.second.push()
    assert not result.ok
    assert result.reason is PushFailureReason.REJECTED_REMOTE_MOVED
    assert harness.remote_tip() == first_sha  # the remote was not forced


def test_harness_uses_local_transport_and_blocks_the_network(harness: TwoCloneHarness) -> None:
    # The remote is a plain directory path: no URL scheme, so no network transport.
    assert "://" not in git(harness.second.root, "remote", "get-url", "origin")
    # The suite's autouse guard refuses outbound sockets, and the whole flow
    # above runs under it, so a pass here means it never needed one.
    with pytest.raises(Exception, match="blocked"):
        socket.create_connection(("203.0.113.1", 9), timeout=0.1)
