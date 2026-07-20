"""Tests for the opt-in pre-run ``git pull`` hook (issue #399).

Symmetric to the post-run push (issue #284, ``tests/test_librarian_push.py``):
the knowledge repo should be git-synced around every librarian run — pulled
BEFORE so the run starts from origin's latest, pushed AFTER so raw intake +
compiled outcomes land on GitHub. This file covers the pull half only.

Covers each acceptance criterion:

- Default OFF (regression test): a normal run behaves byte-identically to
  pre-#399 (no pull).
- Enabled + real run → pull invoked (subprocess mocked) before head capture.
- ``--dry-run`` → no pull even when enabled.
- No ``.git`` → no pull, warning logged.
- Pull failure → non-fatal warning, run still returns 0.
- Dirty working tree → ``--autostash`` lets the pull succeed / is present
  in the argv.
- yaml ``librarian.pull_before_run: true`` enables without a CLI flag.
- Pull reuses the SAME ``push_remote`` / ``push_branch`` resolvers as push.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Self-contained git-init helpers (parallel to test_librarian_push.py)
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Pull Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial: seed raw intake")


def _write_config(knowledge_root: Path, extra: str = "") -> None:
    base = "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n"
    (knowledge_root / "athenaeum.yaml").write_text(base + extra, encoding="utf-8")


@pytest.fixture
def pull_root(tmp_path: Path) -> Path:
    """A git-init knowledge root suitable for a ``merge_only=True`` no-op run."""
    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "raw" / "auto-memory").mkdir(parents=True, exist_ok=True)
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
    _write_config(knowledge_root)
    (knowledge_root / "raw" / "_librarian-clusters.jsonl").write_text("\n", encoding="utf-8")
    _git_init(knowledge_root)
    return knowledge_root


@pytest.fixture
def origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' repo and a clone that is one commit behind it."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "develop", str(origin))

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "wiki").mkdir()
    (seed / "raw").mkdir()
    (seed / "wiki" / ".gitkeep").write_text("", encoding="utf-8")
    (seed / "raw" / ".gitkeep").write_text("", encoding="utf-8")
    _git_init(seed)
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "develop")

    clone_dir = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(clone_dir, "config", "user.email", "test@example.com")
    _git(clone_dir, "config", "user.name", "Pull Test")

    # Advance origin (via the seed checkout) so the clone is behind.
    (seed / "wiki" / "new.md").write_text("new content\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "advance origin")
    _git(seed, "push", "origin", "develop")

    return origin, clone_dir


# ---------------------------------------------------------------------------
# git_pull() — unit tests on the helper
# ---------------------------------------------------------------------------


class TestGitPullUnit:
    def test_returns_false_without_git_repo(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.librarian import git_pull

        caplog.set_level("WARNING", logger="athenaeum")
        assert git_pull(tmp_path) is False
        assert any("skipping git pull" in r.message for r in caplog.records)

    def test_failed_pull_logs_distinct_warning_and_returns_false(
        self, pull_root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No remote 'origin' is configured, so a real ``git pull origin``
        # fails. Exercises the failure path without mocking.
        from athenaeum.librarian import git_pull

        caplog.set_level("WARNING", logger="athenaeum")
        ok = git_pull(pull_root, remote="origin")
        assert ok is False
        assert any(
            "athenaeum-pull-failed" in r.message for r in caplog.records
        ), "pull failure must surface with a distinct, greppable log marker"

    def test_successful_pull_calls_subprocess_with_remote_and_branch(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(librarian.subprocess, "run", fake_run)
        ok = librarian.git_pull(pull_root, remote="backup", branch="develop")
        assert ok is True
        assert captured == [
            ["git", "pull", "--ff-only", "--autostash", "backup", "develop"]
        ]

    def test_default_remote_and_no_branch(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(librarian.subprocess, "run", fake_run)
        ok = librarian.git_pull(pull_root)
        assert ok is True
        assert captured == [["git", "pull", "--ff-only", "--autostash", "origin"]]

    def test_real_fast_forward_pull_succeeds(
        self, origin_and_clone: tuple[Path, Path]
    ) -> None:
        from athenaeum.librarian import git_pull

        _origin, clone_dir = origin_and_clone
        head_before = _git(clone_dir, "rev-parse", "HEAD").stdout.strip()
        ok = git_pull(clone_dir, remote="origin", branch="develop")
        assert ok is True
        head_after = _git(clone_dir, "rev-parse", "HEAD").stdout.strip()
        assert head_after != head_before
        assert (clone_dir / "wiki" / "new.md").exists()

    def test_dirty_working_tree_autostash_survives(
        self, origin_and_clone: tuple[Path, Path]
    ) -> None:
        from athenaeum.librarian import git_pull

        _origin, clone_dir = origin_and_clone
        dirty_file = clone_dir / "raw" / "uncommitted.md"
        dirty_file.write_text("uncommitted intake\n", encoding="utf-8")

        ok = git_pull(clone_dir, remote="origin", branch="develop")
        assert ok is True
        # The fast-forward landed...
        assert (clone_dir / "wiki" / "new.md").exists()
        # ...and the dirty, uncommitted change survived via --autostash.
        assert dirty_file.exists()
        assert dirty_file.read_text(encoding="utf-8") == "uncommitted intake\n"
        status = _git(clone_dir, "status", "--porcelain").stdout
        assert "uncommitted.md" in status


# ---------------------------------------------------------------------------
# _maybe_pull_before_run() — gating tests
# ---------------------------------------------------------------------------


def _patch_git_pull(
    monkeypatch: pytest.MonkeyPatch,
    *,
    succeed: bool = True,
) -> list[dict[str, Any]]:
    """Spy on ``librarian.git_pull`` and return the calls list."""
    from athenaeum import librarian

    calls: list[dict[str, Any]] = []

    def fake_pull(
        knowledge_root: Path,
        remote: str = "origin",
        branch: str | None = None,
    ) -> bool:
        calls.append(
            {"knowledge_root": knowledge_root, "remote": remote, "branch": branch}
        )
        return succeed

    monkeypatch.setattr(librarian, "git_pull", fake_pull)
    return calls


class TestMaybePullBeforeRunGating:
    def test_noop_when_disabled(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import _maybe_pull_before_run

        calls = _patch_git_pull(monkeypatch)
        _maybe_pull_before_run(
            pull_root, config=None, pull_before_run=False, dry_run=False
        )
        assert calls == []

    def test_noop_on_dry_run(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import _maybe_pull_before_run

        calls = _patch_git_pull(monkeypatch)
        _maybe_pull_before_run(
            pull_root, config=None, pull_before_run=True, dry_run=True
        )
        assert calls == []

    def test_noop_without_git_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import _maybe_pull_before_run

        calls = _patch_git_pull(monkeypatch)
        _maybe_pull_before_run(
            tmp_path, config=None, pull_before_run=True, dry_run=False
        )
        assert calls == []

    def test_calls_git_pull_with_resolved_remote_and_branch(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import _maybe_pull_before_run

        config = {"librarian": {"push_remote": "backup", "push_branch": "main"}}
        calls = _patch_git_pull(monkeypatch)
        _maybe_pull_before_run(
            pull_root, config=config, pull_before_run=True, dry_run=False
        )
        assert calls == [
            {"knowledge_root": pull_root, "remote": "backup", "branch": "main"}
        ]

    def test_defaults_to_origin_and_no_branch(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import _maybe_pull_before_run

        calls = _patch_git_pull(monkeypatch)
        _maybe_pull_before_run(
            pull_root, config=None, pull_before_run=True, dry_run=False
        )
        assert calls == [
            {"knowledge_root": pull_root, "remote": "origin", "branch": None}
        ]


# ---------------------------------------------------------------------------
# resolve_pull_before_run() — config resolution
# ---------------------------------------------------------------------------


class TestResolvePullBeforeRun:
    def test_default_false(self) -> None:
        from athenaeum.config import resolve_pull_before_run

        assert resolve_pull_before_run(None) is False
        assert resolve_pull_before_run({}) is False
        assert resolve_pull_before_run({"librarian": {}}) is False

    def test_yaml_true(self) -> None:
        from athenaeum.config import resolve_pull_before_run

        assert resolve_pull_before_run({"librarian": {"pull_before_run": True}}) is True

    def test_yaml_false(self) -> None:
        from athenaeum.config import resolve_pull_before_run

        assert resolve_pull_before_run({"librarian": {"pull_before_run": False}}) is False

    def test_non_bool_falls_through_to_default(self) -> None:
        from athenaeum.config import resolve_pull_before_run

        assert resolve_pull_before_run({"librarian": {"pull_before_run": "true"}}) is False
        assert resolve_pull_before_run({"librarian": {"pull_before_run": 1}}) is False


# ---------------------------------------------------------------------------
# run() — integration wiring
# ---------------------------------------------------------------------------


class TestRunPullIntegration:
    def test_default_off_no_pull(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression test: byte-identical to pre-#399 when unset.
        from athenaeum.librarian import run

        calls = _patch_git_pull(monkeypatch)
        rc = run(
            raw_root=pull_root / "raw",
            wiki_root=pull_root / "wiki",
            knowledge_root=pull_root,
            merge_only=True,
        )
        assert rc == 0
        assert calls == []

    def test_enabled_real_run_pulls_before_head_capture(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        order: list[str] = []

        def fake_pull(
            knowledge_root: Path,
            remote: str = "origin",
            branch: str | None = None,
        ) -> bool:
            order.append("pull")
            return True

        orig_capture_head = librarian._capture_head

        def spy_capture_head(knowledge_root: Path) -> str | None:
            order.append("capture_head")
            return orig_capture_head(knowledge_root)

        monkeypatch.setattr(librarian, "git_pull", fake_pull)
        monkeypatch.setattr(librarian, "_capture_head", spy_capture_head)

        rc = librarian.run(
            raw_root=pull_root / "raw",
            wiki_root=pull_root / "wiki",
            knowledge_root=pull_root,
            merge_only=True,
            pull_before_run=True,
        )
        assert rc == 0
        assert order == ["pull", "capture_head"]

    def test_dry_run_never_pulls(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import run

        calls = _patch_git_pull(monkeypatch)
        rc = run(
            raw_root=pull_root / "raw",
            wiki_root=pull_root / "wiki",
            knowledge_root=pull_root,
            merge_only=True,
            dry_run=True,
            pull_before_run=True,
        )
        assert rc == 0
        assert calls == []

    def test_pull_failure_is_non_fatal(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import run

        calls = _patch_git_pull(monkeypatch, succeed=False)
        rc = run(
            raw_root=pull_root / "raw",
            wiki_root=pull_root / "wiki",
            knowledge_root=pull_root,
            merge_only=True,
            pull_before_run=True,
        )
        assert rc == 0
        assert len(calls) == 1

    def test_yaml_enables_without_flag(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import run

        _write_config(
            pull_root,
            extra="librarian:\n  pull_before_run: true\n",
        )
        calls = _patch_git_pull(monkeypatch)
        rc = run(
            raw_root=pull_root / "raw",
            wiki_root=pull_root / "wiki",
            knowledge_root=pull_root,
            merge_only=True,
            # No explicit pull_before_run — must resolve via yaml.
        )
        assert rc == 0
        assert len(calls) == 1

    def test_explicit_flag_overrides_yaml_false(
        self, pull_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import run

        _write_config(
            pull_root,
            extra="librarian:\n  pull_before_run: false\n",
        )
        calls = _patch_git_pull(monkeypatch)
        rc = run(
            raw_root=pull_root / "raw",
            wiki_root=pull_root / "wiki",
            knowledge_root=pull_root,
            merge_only=True,
            pull_before_run=True,
        )
        assert rc == 0
        assert len(calls) == 1
