"""Tests for the opt-in post-run ``git push`` hook (issue #284).

Closes the move-then-retire recovery gap: scheduled nightly runs commit
locally but, without this opt-in, never push — so origin silently drifts
and the documented git-only recovery only holds on one machine.

Covers each acceptance criterion:

- Default OFF (regression test): a normal run behaves byte-identically to
  pre-#284 (no push).
- Enabled + commits → push invoked (subprocess mocked).
- ``--dry-run`` → no push even when enabled.
- No new commits → no push (the run that processed zero files).
- Push failure → non-fatal warning, commits intact, run still returns 0.
- CLI ``--push`` flag wins over the yaml toggle.
- yaml ``librarian.push_after_run: true`` enables without the flag.
- ``librarian.push_remote`` / ``librarian.push_branch`` drive the refspec.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Self-contained git-init helpers (parallel to test_librarian_auto_memory.py)
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
    _git(root, "config", "user.name", "Push Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial: seed raw intake")


def _write_am(
    scope_dir: Path,
    filename: str,
    *,
    name: str,
    session: str,
    turn: int,
    body: str,
) -> Path:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        "---\n"
        f"name: {name}\n"
        "type: feedback\n"
        f"originSessionId: {session}\n"
        f"originTurn: {turn}\n"
        "sources:\n"
        f"  - session: {session}\n"
        f"    turn: {turn}\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def _write_clusters(knowledge_root: Path, rows: list[dict[str, Any]]) -> None:
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )


def _write_config(knowledge_root: Path, extra: str = "") -> None:
    base = "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n"
    (knowledge_root / "athenaeum.yaml").write_text(base + extra, encoding="utf-8")


@pytest.fixture
def push_root(tmp_path: Path) -> Path:
    """A git-init knowledge root whose ``merge_only=True`` run produces a commit.

    Mirrors the ``retire_root`` shape used elsewhere: one singleton auto-
    memory cluster, no Anthropic client, ``merge_only=True`` triggers the
    move-then-retire pass and a final ``git_snapshot`` that commits.
    """
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "-Users-tristankromer-Code-home"
    _write_am(
        scope,
        "user_tristan_berlin_address.md",
        name="Tristan Berlin address",
        session="sess-berlin",
        turn=3,
        body="Tristan lives in Berlin, Germany.",
    )
    _write_clusters(
        knowledge_root,
        [
            {
                "cluster_id": "home-0001",
                "member_paths": [
                    "-Users-tristankromer-Code-home/user_tristan_berlin_address.md"
                ],
                "centroid_score": 1.0,
                "rationale": "singleton",
            }
        ],
    )
    _write_config(knowledge_root)
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
    _git_init(knowledge_root)
    return knowledge_root


@pytest.fixture
def quiet_root(tmp_path: Path) -> Path:
    """A git-init knowledge root with no raw files — a run produces no commit."""
    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "raw" / "auto-memory").mkdir(parents=True, exist_ok=True)
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
    _write_clusters(knowledge_root, [])
    _write_config(knowledge_root)
    _git_init(knowledge_root)
    return knowledge_root


# ---------------------------------------------------------------------------
# git_push() — unit tests on the helper
# ---------------------------------------------------------------------------


class TestGitPushUnit:
    def test_returns_false_without_git_repo(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.librarian import git_push

        caplog.set_level("WARNING", logger="athenaeum")
        assert git_push(tmp_path) is False
        assert any("skipping git push" in r.message for r in caplog.records)

    def test_failed_push_logs_distinct_warning_and_returns_false(
        self, push_root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No remote 'origin' is configured by ``_git_init``, so a real
        # ``git push origin`` will fail. This both proves the failure path
        # AND exercises it without mocking.
        from athenaeum.librarian import git_push

        caplog.set_level("WARNING", logger="athenaeum")
        ok = git_push(push_root, remote="origin")
        assert ok is False
        assert any(
            "athenaeum-push-failed" in r.message for r in caplog.records
        ), "push failure must surface with a distinct, greppable log marker"

    def test_successful_push_calls_subprocess_with_remote_and_branch(
        self, push_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(librarian.subprocess, "run", fake_run)
        ok = librarian.git_push(push_root, remote="backup", branch="develop")
        assert ok is True
        assert captured == [["git", "push", "backup", "develop"]]


# ---------------------------------------------------------------------------
# run() — gating tests
# ---------------------------------------------------------------------------


def _patch_git_push(
    monkeypatch: pytest.MonkeyPatch,
    *,
    succeed: bool = True,
) -> list[dict[str, Any]]:
    """Spy on ``librarian.git_push`` and return the calls list."""
    from athenaeum import librarian

    calls: list[dict[str, Any]] = []

    def fake_push(
        knowledge_root: Path,
        remote: str = "origin",
        branch: str | None = None,
    ) -> bool:
        calls.append(
            {"knowledge_root": knowledge_root, "remote": remote, "branch": branch}
        )
        return succeed

    monkeypatch.setattr(librarian, "git_push", fake_push)
    return calls


class TestPushAfterRunGating:
    def test_default_off_no_push(
        self, push_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression test for acceptance criterion: byte-identical to today
        # when push_after_run is unset.
        from athenaeum.librarian import run

        calls = _patch_git_push(monkeypatch)
        rc = run(
            raw_root=push_root / "raw",
            wiki_root=push_root / "wiki",
            knowledge_root=push_root,
            merge_only=True,
        )
        assert rc == 0
        assert calls == []

    def test_enabled_with_commits_pushes(
        self, push_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import run

        calls = _patch_git_push(monkeypatch)
        rc = run(
            raw_root=push_root / "raw",
            wiki_root=push_root / "wiki",
            knowledge_root=push_root,
            merge_only=True,
            push_after_run=True,
        )
        assert rc == 0
        assert len(calls) == 1
        assert calls[0]["knowledge_root"] == push_root
        assert calls[0]["remote"] == "origin"
        assert calls[0]["branch"] is None

    def test_dry_run_never_pushes(
        self, push_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import run

        calls = _patch_git_push(monkeypatch)
        rc = run(
            raw_root=push_root / "raw",
            wiki_root=push_root / "wiki",
            knowledge_root=push_root,
            merge_only=True,
            dry_run=True,
            push_after_run=True,
        )
        assert rc == 0
        assert calls == []

    def test_no_commits_no_push(
        self, quiet_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The seed commit from _git_init means the pre-processing snapshot
        # is also a no-op. A truly idle run must not push.
        from athenaeum.librarian import run

        calls = _patch_git_push(monkeypatch)
        rc = run(
            raw_root=quiet_root / "raw",
            wiki_root=quiet_root / "wiki",
            knowledge_root=quiet_root,
            merge_only=True,
            push_after_run=True,
        )
        assert rc == 0
        assert calls == []

    def test_push_failure_is_non_fatal(
        self, push_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Acceptance criterion: a push failure does NOT roll back the
        # committed run — commits remain locally and the run still exits 0.
        from athenaeum.librarian import run

        head_before = _git(push_root, "rev-parse", "HEAD").stdout.strip()
        calls = _patch_git_push(monkeypatch, succeed=False)
        rc = run(
            raw_root=push_root / "raw",
            wiki_root=push_root / "wiki",
            knowledge_root=push_root,
            merge_only=True,
            push_after_run=True,
        )
        assert rc == 0
        assert len(calls) == 1
        # The run committed (HEAD moved) and stayed committed: commits
        # are intact despite the simulated push failure.
        head_after = _git(push_root, "rev-parse", "HEAD").stdout.strip()
        assert head_after != head_before

    def test_yaml_enables_without_flag(
        self, push_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import run

        _write_config(
            push_root,
            extra="librarian:\n  push_after_run: true\n",
        )
        calls = _patch_git_push(monkeypatch)
        rc = run(
            raw_root=push_root / "raw",
            wiki_root=push_root / "wiki",
            knowledge_root=push_root,
            merge_only=True,
            # No explicit push_after_run — must resolve via yaml.
        )
        assert rc == 0
        assert len(calls) == 1

    def test_yaml_remote_and_branch_drive_refspec(
        self, push_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import run

        _write_config(
            push_root,
            extra=(
                "librarian:\n"
                "  push_after_run: true\n"
                "  push_remote: backup\n"
                "  push_branch: main\n"
            ),
        )
        calls = _patch_git_push(monkeypatch)
        rc = run(
            raw_root=push_root / "raw",
            wiki_root=push_root / "wiki",
            knowledge_root=push_root,
            merge_only=True,
        )
        assert rc == 0
        assert calls == [
            {"knowledge_root": push_root, "remote": "backup", "branch": "main"}
        ]

    def test_explicit_flag_overrides_yaml_false(
        self, push_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CLI --push must win over an explicit yaml `push_after_run: false`.
        from athenaeum.librarian import run

        _write_config(
            push_root,
            extra="librarian:\n  push_after_run: false\n",
        )
        calls = _patch_git_push(monkeypatch)
        rc = run(
            raw_root=push_root / "raw",
            wiki_root=push_root / "wiki",
            knowledge_root=push_root,
            merge_only=True,
            push_after_run=True,
        )
        assert rc == 0
        assert len(calls) == 1
