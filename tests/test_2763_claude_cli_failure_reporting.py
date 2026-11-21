# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2763 — a failing ``claude`` is reported in one readable line.

Every test drives a fake ``claude`` executable on PATH (no real login, no real
call). The CLI prints its error on stdout and leaves stderr empty.
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

from athenaeum.cli import main as cli_main
from athenaeum.provider import ClaudeCliClient, ClaudeCliNotLoggedInError
from tests.test_session_end import _seed_knowledge_root

NOT_LOGGED_IN = "Not logged in · Please run /login"


def _fake_claude(bin_dir: Path, stdout_text: str, code: int = 1) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(
        f"#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{stdout_text}'\nexit {code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fake_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    return bin_dir


def _call() -> None:
    ClaudeCliClient().messages.create(
        model="claude-haiku-4-5",
        max_tokens=64,
        system="s",
        messages=[{"role": "user", "content": "hi"}],
    )


def test_stdout_error_is_surfaced_when_stderr_empty(fake_path: Path) -> None:
    _fake_claude(fake_path, "Credit balance is too low")
    with pytest.raises(RuntimeError) as ei:
        _call()
    msg = str(ei.value)
    assert "\n" not in msg
    assert "Credit balance is too low" in msg
    assert "(no stderr)" not in msg


def test_stdout_error_is_bounded(fake_path: Path) -> None:
    _fake_claude(fake_path, "x" * 5000)
    with pytest.raises(RuntimeError) as ei:
        _call()
    assert len(str(ei.value)) < 600


def test_not_logged_in_names_login_and_api_key(fake_path: Path) -> None:
    _fake_claude(fake_path, NOT_LOGGED_IN)
    with pytest.raises(ClaudeCliNotLoggedInError) as ei:
        _call()
    msg = str(ei.value)
    assert "\n" not in msg
    assert "/login" in msg
    assert "ANTHROPIC_API_KEY" in msg
    # The fix text leads; raw CLI output follows, marked as detail.
    assert msg.startswith("claude CLI is not logged in. Run `claude`")
    assert msg.index("/login") < msg.index("CLI output, for detail")


def test_not_logged_in_long_detail_cut_at_token_boundary(fake_path: Path) -> None:
    _fake_claude(fake_path, "Not logged in " + "word " * 200)
    with pytest.raises(ClaudeCliNotLoggedInError) as ei:
        _call()
    msg = str(ei.value)
    assert msg.index("ANTHROPIC_API_KEY") < msg.index("CLI output, for detail")
    assert "word ...')" in msg


def _seed_failing_run(tmp_path: Path) -> Path:
    root = _seed_knowledge_root(tmp_path)
    (root / "raw" / "sessions" / "2026-01-01T00-00-00-abcd1234.md").write_text(
        "Met Dana Whitfield from Acme about widgets.\n"
    )
    (root / "athenaeum.yaml").write_text("llm:\n  provider: claude-cli\n")
    return root


def test_run_logs_no_traceback_and_status_shows_why(
    tmp_path: Path,
    fake_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_claude(fake_path, NOT_LOGGED_IN)
    root = _seed_failing_run(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    with caplog.at_level(logging.INFO):
        cli_main(
            [
                "run",
                "--knowledge-root", str(root),
                "--raw-root", str(root / "raw"),
                "--wiki-root", str(root / "wiki"),
            ]
        )

    failed = [r for r in caplog.records if "Failed to process" in r.getMessage()]
    assert failed, "the per-file failure must still be logged"
    assert all(r.exc_info is None for r in failed)
    assert "/login" in failed[0].getMessage()
    assert "Traceback" not in caplog.text

    capsys.readouterr()
    cli_main(["status", "--path", str(root)])
    out = capsys.readouterr().out
    assert "Last compile:         FAILED" in out
    assert "/login" in out


def test_run_keeps_traceback_for_other_failures(
    tmp_path: Path,
    fake_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _fake_claude(fake_path, "Credit balance is too low")
    root = _seed_failing_run(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with caplog.at_level(logging.INFO):
        cli_main(
            [
                "run",
                "--knowledge-root", str(root),
                "--raw-root", str(root / "raw"),
                "--wiki-root", str(root / "wiki"),
            ]
        )
    failed = [r for r in caplog.records if "Failed to process" in r.getMessage()]
    assert failed and failed[0].exc_info is not None
    assert "Credit balance is too low" in caplog.text
