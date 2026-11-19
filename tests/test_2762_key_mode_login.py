# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2762 -- ``key_mode`` and ``init`` must confirm a usable
Claude Code login, not just the ``claude`` binary.

A fake ``claude`` script on PATH stands in for the real CLI: it answers
``claude auth status`` the way the real one does when logged out (exit 1,
"Not logged in" on stdout). Nothing here spawns the real CLI or spends a
model call.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from athenaeum import doctor
from athenaeum.cli import main as cli_main

_CLAUDE_CFG = {"llm": {"provider": "claude-cli"}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATHENAEUM_CLAUDE_CLI_BIN", raising=False)
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ATHENAEUM_TOPIC_LLM_PROVIDER", raising=False)


def _fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def _no_claude_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _b, *a, **k: None)
    monkeypatch.setattr(os.path, "exists", lambda _b: False)


_LOGGED_OUT = 'echo "Not logged in · Please run /login"; exit 1'
_LOGGED_IN = 'echo \'{"loggedIn": true}\'; exit 0'


def test_binary_present_but_logged_out_fails_key_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(tmp_path, monkeypatch, _LOGGED_OUT)
    result = doctor.check_key_mode(_CLAUDE_CFG, tmp_path / "cache")
    assert result.status == "fail"
    assert "not logged in" in result.detail
    assert result.fix == "run `claude` and `/login`, or set ANTHROPIC_API_KEY"


def test_binary_present_and_logged_in_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(tmp_path, monkeypatch, _LOGGED_IN)
    result = doctor.check_key_mode(_CLAUDE_CFG, tmp_path / "cache")
    assert result.status == "ok"
    assert "login confirmed" in result.detail


def test_logged_in_false_json_with_exit_zero_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(tmp_path, monkeypatch, 'echo \'{"loggedIn": false}\'; exit 0')
    result = doctor.check_key_mode(_CLAUDE_CFG, tmp_path / "cache")
    assert result.status == "fail"


def test_unanswerable_probe_does_not_fail_a_working_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(tmp_path, monkeypatch, "echo not-json; exit 0")
    result = doctor.check_key_mode(_CLAUDE_CFG, tmp_path / "cache")
    assert result.status == "ok"


def test_init_reports_missing_login_and_omits_the_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_claude(tmp_path, monkeypatch, _LOGGED_OUT)
    rc = cli_main(["init", "--path", str(tmp_path / "knowledge")])
    assert rc == 0
    captured = capsys.readouterr()
    assert "usage limits" not in captured.out
    assert "LLM provider:" not in captured.out
    assert "not logged in" in captured.err
    assert "run `claude` and `/login`, or set ANTHROPIC_API_KEY" in captured.err


def test_init_with_confirmed_login_still_prints_the_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_claude(tmp_path, monkeypatch, _LOGGED_IN)
    rc = cli_main(["init", "--path", str(tmp_path / "knowledge")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-cli" in out
    assert "usage limits" in out


def test_no_binary_no_key_names_binary_install_and_that_compiles_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_claude_binary(monkeypatch)
    result = doctor.check_key_mode(None, tmp_path / "cache")
    assert result.status == "fail"
    assert "`claude` binary" in result.detail
    assert "compiles cannot run" in result.detail
    assert "topic extraction" not in result.detail
    assert "install Claude Code" in result.fix

    rc = cli_main(["init", "--path", str(tmp_path / "knowledge")])
    assert rc == 0
    err = capsys.readouterr().err
    assert "`claude` binary" in err
    assert "compiles cannot run" in err
    assert "install Claude Code" in err


def test_claude_cli_provider_with_missing_binary_says_compiles_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_claude_binary(monkeypatch)
    result = doctor.check_key_mode(_CLAUDE_CFG, tmp_path / "cache")
    assert result.status == "fail"
    assert "compiles cannot run" in result.detail
    assert "topic extraction" not in result.detail
