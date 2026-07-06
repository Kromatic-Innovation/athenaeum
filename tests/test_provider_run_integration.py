# SPDX-License-Identifier: Apache-2.0
"""Issue #330 — provider wiring through ``librarian.run``.

Covers the two run-level guards the seam adds:
- ``claude-cli`` + batch mode is a LOUD startup error (rc 1, no silent
  fallback, no LLM call).
- ``claude-cli`` waives the ``ANTHROPIC_API_KEY`` requirement (subscription
  auth) — the run proceeds instead of exiting 1 on a missing key.

All LLM interaction is stubbed; no live API, no ``claude`` subprocess.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from athenaeum.librarian import run


def _seed_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "wiki").mkdir()
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "t"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    return root


class TestBatchCliConflict:
    def test_claude_cli_plus_batch_is_loud_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _seed_root(tmp_path)
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Make the binary probe pass deterministically (CI has no `claude`), so
        # the BATCH guard is what fires — not the missing-binary preflight.
        import athenaeum.provider as prov

        monkeypatch.setattr(prov.shutil, "which", lambda _b: "/usr/bin/claude")
        caplog.set_level(logging.ERROR, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            batch_mode=True,
        )

        assert rc == 1
        messages = [r.getMessage() for r in caplog.records]
        assert any("batch mode" in m and "claude-cli" in m for m in messages), messages


class TestClaudeCliWaivesApiKey:
    def test_missing_key_does_not_abort_for_claude_cli(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _seed_root(tmp_path)
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Binary probe passes deterministically (CI has no `claude`); we are
        # exercising the api-key WAIVER, not the binary preflight.
        import athenaeum.provider as prov

        monkeypatch.setattr(prov.shutil, "which", lambda _b: "/usr/bin/claude")

        # No raw files to process → the run completes cleanly (rc 0) WITHOUT
        # tripping the api-key guard that would fire for the ``api`` backend.
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )
        assert rc == 0


class TestClaudeCliMissingBinaryPreflight:
    def test_missing_binary_fails_loudly_at_startup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Issue #330: a claude-cli run with no `claude` binary must fail loudly
        # (rc 1) at startup, not silently defer every file to an rc-0 no-op.
        root = _seed_root(tmp_path)
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import athenaeum.provider as prov

        monkeypatch.setattr(prov.shutil, "which", lambda _b: None)
        monkeypatch.setattr(prov.os.path, "exists", lambda _b: False)
        caplog.set_level(logging.ERROR, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )
        assert rc == 1
        assert any("not found" in r.getMessage().lower() for r in caplog.records)
