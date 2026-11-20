# SPDX-License-Identifier: Apache-2.0
"""The per-repository sync lease inside the git directory (athenaeum#2997).

The two-process cases are deterministic: a child process takes the lease, prints a
line when it holds it, and releases it when its stdin closes. The parent waits for
that line and closes stdin; nothing races a sleep.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from athenaeum import librarian
from athenaeum.store import git_dir
from athenaeum.sync_integrate import SyncSettings, push_after_compile, sync_before_compile
from athenaeum.sync_lease import (
    LEASE_FILE,
    LEASE_HELD,
    LEASE_TAKEN,
    acquire_repo_lease,
    release_all_leases,
    release_repo_lease,
)
from tests.fixtures.two_clone_harness import Install, git, make_two_clone_harness
from tests.test_sync_integrate import _diverge, _run_second, _wired, declaration_text

_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from athenaeum.sync_lease import LEASE_TAKEN, acquire_repo_lease
    status, _ = acquire_repo_lease(Path(sys.argv[1]))
    print(status, flush=True)
    sys.stdin.read()
    """
)


@contextmanager
def other_process_holds(repo: Path) -> Iterator[None]:
    """Another process holds *repo*'s lease for the duration of the block."""
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(repo)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == LEASE_TAKEN  # blocks until it holds it
        yield
    finally:
        assert child.stdin is not None
        child.stdin.close()  # the child exits, the kernel drops its flock
        child.wait(timeout=30)


@pytest.fixture(autouse=True)
def _no_leases_left() -> Iterator[None]:
    yield
    release_all_leases()


@pytest.fixture
def install(tmp_path: Path) -> Install:
    return make_two_clone_harness(tmp_path).second


def test_lease_file_is_inside_the_git_directory_and_never_tracked(install: Install) -> None:
    assert acquire_repo_lease(install.root)[0] == LEASE_TAKEN
    lock = git_dir(install.root) / LEASE_FILE
    assert lock.is_file()
    assert git(install.root, "status", "--porcelain") == ""
    install.commit("note.md", "x\n")
    commit_all = git(install.root, "log", "--all", "--name-only", "--format=")
    assert LEASE_FILE not in commit_all
    assert LEASE_FILE not in git(install.root, "ls-files")


def test_sync_before_compile_takes_the_lease_and_push_releases_it(install: Install) -> None:
    report = sync_before_compile(install.root, install.branch, SyncSettings())
    assert ("lease", "ok") == next((s.step, s.outcome) for s in report.steps if s.step == "lease")
    # held by this process now: another process cannot take it
    with pytest.raises(AssertionError):
        with other_process_holds(install.root):
            pass
    assert push_after_compile(install.root, install.branch, SyncSettings(), report).ok
    with other_process_holds(install.root):  # released by the push
        pass


def test_a_second_process_never_integrates_while_the_lease_is_held(tmp_path: Path) -> None:
    h = make_two_clone_harness(tmp_path)
    h.first.commit("a.md", "a\n")
    assert h.first.push().ok
    head = h.second.head()
    (h.second.root / "wip.md").write_text("uncommitted\n")
    with other_process_holds(h.second.root):
        report = sync_before_compile(h.second.root, h.second.branch, SyncSettings())
    rows = {s.step: s for s in report.steps}
    assert (rows["lease"].outcome, rows["lease"].reason) == ("skipped", "lease-held")
    assert rows["integrate"].outcome == "skipped"
    assert "snapshot" not in rows and "fetch" not in rows
    assert h.second.head() == head  # nothing was snapshotted, fetched or merged
    assert "wip.md" in git(h.second.root, "status", "--porcelain")
    # once the holder has gone the same call goes through
    again = sync_before_compile(h.second.root, h.second.branch, SyncSettings())
    assert next(s for s in again.steps if s.step == "integrate").outcome == "ok"
    assert h.second.head() != head


def test_a_push_is_not_made_while_another_process_holds_the_lease(tmp_path: Path) -> None:
    h = make_two_clone_harness(tmp_path)
    h.second.commit("b.md", "b\n")
    before = h.remote_tip()
    with other_process_holds(h.second.root):
        from athenaeum.sync_integrate import SyncReport

        report = SyncReport()
        outcome = push_after_compile(h.second.root, h.second.branch, SyncSettings(), report)
    assert not outcome.ok and outcome.reason == "lease-held" and outcome.attempts == 0
    assert report.steps[-1].step == "push" and report.steps[-1].reason == "lease-held"
    assert h.remote_tip() == before


def test_separate_clones_do_not_block_each_other(tmp_path: Path) -> None:
    h = make_two_clone_harness(tmp_path)
    assert git_dir(h.first.root) != git_dir(h.second.root)
    with other_process_holds(h.first.root):
        report = sync_before_compile(h.second.root, h.second.branch, SyncSettings())
    assert next(s for s in report.steps if s.step == "lease").outcome == "ok"
    assert next(s for s in report.steps if s.step == "integrate").outcome == "ok"


def test_same_process_reentry_is_not_a_self_deadlock(install: Install) -> None:
    assert acquire_repo_lease(install.root)[0] == LEASE_TAKEN
    assert acquire_repo_lease(install.root)[0] == LEASE_TAKEN
    release_repo_lease(install.root)
    with other_process_holds(install.root):
        assert acquire_repo_lease(install.root)[0] == LEASE_HELD


def test_integrate_run_without_push_still_releases_the_lease(tmp_path: Path) -> None:
    h, kr, cfg = _wired(tmp_path, declaration_text(), None)
    _diverge(h)
    state = librarian.SurfaceSyncState()
    librarian._maybe_pull_before_run(
        kr, config=cfg, pull_before_run=True, dry_run=False, surface_sync=state
    )
    assert any(r["op"] == "lease" for r in state.results)
    librarian._maybe_push_after_run(
        kr, config=cfg, push_after_run=False, dry_run=False, head_at_start=None, surface_sync=state
    )
    with other_process_holds(h.second.root):
        pass


def test_fast_forward_repository_is_unaffected(tmp_path: Path) -> None:
    h, kr, cfg = _wired(tmp_path, declaration_text(), "fast_forward")
    _diverge(h)
    state = _run_second(kr, cfg)
    assert not (git_dir(h.second.root) / LEASE_FILE).exists()
    assert not any(r["op"] == "lease" for r in state.results)


def test_an_exception_mid_integrate_releases_the_lease(
    install: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum import sync_integrate

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("integrate crashed")

    monkeypatch.setattr(sync_integrate, "integrate_remote", boom)
    with pytest.raises(RuntimeError, match="integrate crashed"):
        sync_before_compile(install.root, install.branch, SyncSettings())
    with other_process_holds(install.root):  # the lease was dropped, not leaked
        pass
