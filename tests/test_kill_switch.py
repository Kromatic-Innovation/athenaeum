"""Tests for the kill switch — state file, env override, scopes, CLI (issue #379).

The kill switch is the single reversible way to stop athenaeum's background
work. These tests pin the contract every entry point relies on: the resolved
scope (env override > file > enabled), the per-aspect ``is_disabled`` semantics
(``all`` stops everything, ``compile`` stops only the expensive pass), and the
``disable`` / ``enable`` / ``status`` CLI surface. The shell-hook side of the
same contract is covered in ``test_shell_hooks.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum import killswitch
from athenaeum.cli import main


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the ambient ATHENAEUM_DISABLED / cache dir leak into a test."""
    monkeypatch.delenv("ATHENAEUM_DISABLED", raising=False)
    monkeypatch.delenv("ATHENAEUM_CACHE_DIR", raising=False)


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    return tmp_path / "cache"


# ---------------------------------------------------------------------------
# state_path resolution
# ---------------------------------------------------------------------------


class TestStatePath:
    def test_explicit_cache_dir_wins(self, cache: Path) -> None:
        assert killswitch.state_path(cache) == cache / "disabled"

    def test_env_cache_dir(self, monkeypatch: pytest.MonkeyPatch, cache: Path) -> None:
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
        assert killswitch.state_path() == cache / "disabled"

    def test_default_is_home_cache(self) -> None:
        assert killswitch.state_path() == Path.home() / ".cache" / "athenaeum" / "disabled"


# ---------------------------------------------------------------------------
# disable / enable / current_state
# ---------------------------------------------------------------------------


class TestDisableEnable:
    def test_disable_writes_all_scope(self, cache: Path) -> None:
        path = killswitch.disable(cache_dir=cache)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["scope"] == "all"
        assert data["since"]  # ISO timestamp recorded
        state = killswitch.current_state(cache)
        assert state.disabled and state.scope == "all" and state.source == "file"

    def test_disable_compile_scope(self, cache: Path) -> None:
        killswitch.disable(killswitch.SCOPE_COMPILE, cache_dir=cache)
        state = killswitch.current_state(cache)
        assert state.scope == "compile"

    def test_disable_records_reason(self, cache: Path) -> None:
        killswitch.disable(reason="token freeze", cache_dir=cache)
        assert killswitch.current_state(cache).reason == "token freeze"

    def test_disable_rejects_unknown_scope(self, cache: Path) -> None:
        with pytest.raises(ValueError):
            killswitch.disable("everything", cache_dir=cache)

    def test_disable_is_idempotent_and_renarrows(self, cache: Path) -> None:
        killswitch.disable(cache_dir=cache)
        killswitch.disable(killswitch.SCOPE_COMPILE, cache_dir=cache)
        assert killswitch.current_state(cache).scope == "compile"

    def test_enable_removes_file(self, cache: Path) -> None:
        killswitch.disable(cache_dir=cache)
        assert killswitch.enable(cache_dir=cache) is True
        assert not killswitch.state_path(cache).exists()
        assert killswitch.current_state(cache).disabled is False

    def test_enable_when_already_enabled(self, cache: Path) -> None:
        assert killswitch.enable(cache_dir=cache) is False

    def test_enabled_by_default(self, cache: Path) -> None:
        state = killswitch.current_state(cache)
        assert state.disabled is False and state.scope is None and state.source is None


# ---------------------------------------------------------------------------
# tolerant file reads — an emergency `touch $cache/disabled` must count
# ---------------------------------------------------------------------------


class TestFileTolerance:
    def test_empty_file_is_all_scope(self, cache: Path) -> None:
        killswitch.state_path(cache).parent.mkdir(parents=True)
        killswitch.state_path(cache).write_text("")
        assert killswitch.current_state(cache).scope == "all"

    def test_plain_text_compile_token(self, cache: Path) -> None:
        killswitch.state_path(cache).parent.mkdir(parents=True)
        killswitch.state_path(cache).write_text("compile\n")
        assert killswitch.current_state(cache).scope == "compile"

    def test_garbage_json_falls_back_to_all(self, cache: Path) -> None:
        killswitch.state_path(cache).parent.mkdir(parents=True)
        killswitch.state_path(cache).write_text("{not valid json")
        assert killswitch.current_state(cache).scope == "all"

    def test_unknown_scope_in_json_falls_back_to_all(self, cache: Path) -> None:
        killswitch.state_path(cache).parent.mkdir(parents=True)
        killswitch.state_path(cache).write_text('{"scope": "bogus"}')
        assert killswitch.current_state(cache).scope == "all"


# ---------------------------------------------------------------------------
# env override precedence
# ---------------------------------------------------------------------------


class TestEnvOverride:
    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "all", "ALL", " True "])
    def test_env_all_values(
        self, monkeypatch: pytest.MonkeyPatch, cache: Path, val: str
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_DISABLED", val)
        state = killswitch.current_state(cache)
        assert state.disabled and state.scope == "all" and state.source == "env"

    def test_env_compile(self, monkeypatch: pytest.MonkeyPatch, cache: Path) -> None:
        monkeypatch.setenv("ATHENAEUM_DISABLED", "compile")
        assert killswitch.current_state(cache).scope == "compile"

    @pytest.mark.parametrize("val", ["0", "false", "off", "", "garbage"])
    def test_env_off_defers_to_file(
        self, monkeypatch: pytest.MonkeyPatch, cache: Path, val: str
    ) -> None:
        killswitch.disable(cache_dir=cache)  # file says all
        monkeypatch.setenv("ATHENAEUM_DISABLED", val)
        # env is off/unrecognised -> file wins, still disabled
        assert killswitch.current_state(cache).source == "file"

    def test_env_wins_over_file(
        self, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        killswitch.disable(killswitch.SCOPE_ALL, cache_dir=cache)  # file says all
        monkeypatch.setenv("ATHENAEUM_DISABLED", "compile")  # env says compile
        state = killswitch.current_state(cache)
        assert state.scope == "compile" and state.source == "env"


# ---------------------------------------------------------------------------
# is_disabled per-aspect semantics — the core contract
# ---------------------------------------------------------------------------


class TestIsDisabled:
    def test_enabled_nothing_disabled(self, cache: Path) -> None:
        assert killswitch.is_disabled("compile", cache_dir=cache) is False
        assert killswitch.is_disabled("recall", cache_dir=cache) is False

    def test_scope_all_disables_everything(self, cache: Path) -> None:
        killswitch.disable(killswitch.SCOPE_ALL, cache_dir=cache)
        assert killswitch.is_disabled("compile", cache_dir=cache) is True
        assert killswitch.is_disabled("recall", cache_dir=cache) is True
        assert killswitch.is_disabled("capture", cache_dir=cache) is True
        assert killswitch.is_disabled(cache_dir=cache) is True  # default aspect

    def test_scope_compile_only_stops_compile(self, cache: Path) -> None:
        killswitch.disable(killswitch.SCOPE_COMPILE, cache_dir=cache)
        assert killswitch.is_disabled("compile", cache_dir=cache) is True
        assert killswitch.is_disabled("recall", cache_dir=cache) is False
        assert killswitch.is_disabled("capture", cache_dir=cache) is False


# ---------------------------------------------------------------------------
# format_status_line
# ---------------------------------------------------------------------------


class TestFormatStatusLine:
    def test_enabled(self, cache: Path) -> None:
        assert "enabled" in killswitch.format_status_line(cache)

    def test_disabled_all(self, cache: Path) -> None:
        killswitch.disable(reason="freeze", cache_dir=cache)
        line = killswitch.format_status_line(cache)
        assert "DISABLED" in line and "ALL background work OFF" in line
        assert "freeze" in line and "athenaeum enable" in line

    def test_disabled_compile(self, cache: Path) -> None:
        killswitch.disable(killswitch.SCOPE_COMPILE, cache_dir=cache)
        line = killswitch.format_status_line(cache)
        assert "recall ON" in line

    def test_env_forced_mentions_env(
        self, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_DISABLED", "1")
        assert "ATHENAEUM_DISABLED" in killswitch.format_status_line(cache)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestCli:
    def test_disable_then_enable(
        self, cache: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["disable", "--cache-dir", str(cache)]) == 0
        assert "all background work is off" in capsys.readouterr().out
        assert killswitch.current_state(cache).scope == "all"

        assert main(["enable", "--cache-dir", str(cache)]) == 0
        assert "restored" in capsys.readouterr().out
        assert killswitch.current_state(cache).disabled is False

    def test_disable_compile(
        self, cache: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["disable", "--compile", "--cache-dir", str(cache)]) == 0
        assert "compile" in capsys.readouterr().out
        assert killswitch.current_state(cache).scope == "compile"

    def test_enable_when_already_enabled(
        self, cache: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["enable", "--cache-dir", str(cache)]) == 0
        assert "already enabled" in capsys.readouterr().out

    def test_status_reports_killswitch(
        self, tmp_path: Path, cache: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        killswitch.disable(cache_dir=cache)
        # Knowledge dir absent -> rc 1, but the kill-switch line still prints.
        rc = main(["status", "--path", str(tmp_path / "nope"), "--cache-dir", str(cache)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "Kill switch:" in out and "DISABLED" in out

    def test_enable_warns_when_env_forces(
        self, monkeypatch: pytest.MonkeyPatch, cache: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_DISABLED", "1")
        assert main(["enable", "--cache-dir", str(cache)]) == 0
        assert "still forces disabled" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# session-end honours the compile aspect (the expensive pass, issue #379)
# ---------------------------------------------------------------------------


class TestSessionEndGuard:
    def test_session_end_noop_when_disabled_all(
        self, tmp_path: Path, cache: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        killswitch.disable(killswitch.SCOPE_ALL, cache_dir=cache)
        rc = main(
            ["session-end", "--path", str(tmp_path), "--cache-dir", str(cache)]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["noop"] is True and payload["reason"] == "disabled"
        assert payload["scope"] == "all"

    def test_session_end_noop_when_disabled_compile(
        self, tmp_path: Path, cache: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        killswitch.disable(killswitch.SCOPE_COMPILE, cache_dir=cache)
        rc = main(
            ["session-end", "--path", str(tmp_path), "--cache-dir", str(cache)]
        )
        assert rc == 0
        assert json.loads(capsys.readouterr().out.strip())["noop"] is True
