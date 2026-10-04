# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1951 -- ``north_star_cli``'s ``--cli-tool-passthrough``
preflight refusals and the flag's effect on :func:`harness.build_live_client`.

No live call anywhere here: every refusal fires before ``run_probe_all_arms``
or any client construction, and the "flag on" assertion stubs
``build_llm_client`` the same way ``test_north_star_cli.py`` already does.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.provider import ClaudeCliClient
from tests.evals import north_star_cli
from tests.evals.harness import build_live_client


def test_claude_cli_api_mode_without_flag_exits_2_before_any_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _exploding_run_probe_all_arms(*args, **kwargs):
        raise AssertionError("preflight refusal must never run a cell")

    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(["--mode", "api", "--dry-run"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--cli-tool-passthrough" in err
    assert not (tmp_path / "r.jsonl").exists()


def test_claude_cli_cli_mode_without_flag_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preflight is scoped to ``--mode api`` only: ``--mode cli`` spawns
    ``claude -p`` directly and never touches the opt-in tool passthrough, so
    it must not be refused for lacking the flag."""
    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(["--mode", "cli", "--dry-run"])

    assert exit_code == 0


def test_flag_with_phase2_exits_2_before_any_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _exploding_run_probe_all_arms(*args, **kwargs):
        raise AssertionError("preflight refusal must never run a cell")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(
        ["--mode", "api", "--cli-tool-passthrough", "--phase2", "--dry-run"]
    )

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--phase2" in err or "phase2" in err
    assert not (tmp_path / "r.jsonl").exists()


def test_api_provider_with_flag_and_no_phase2_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(["--mode", "api", "--cli-tool-passthrough", "--dry-run"])

    assert exit_code == 0


def test_build_live_client_returns_passthrough_enabled_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_client = ClaudeCliClient(binary="claude", timeout=12.0)
    monkeypatch.setattr("athenaeum.provider.build_llm_client", MagicMock(return_value=base_client))

    client = build_live_client(cli_tool_passthrough=True)

    assert isinstance(client, ClaudeCliClient)
    assert client.tool_passthrough is True
    assert client.binary == "claude"
    assert client.timeout == 12.0


def test_build_live_client_default_is_passthrough_off(monkeypatch: pytest.MonkeyPatch) -> None:
    base_client = ClaudeCliClient(binary="claude")
    monkeypatch.setattr("athenaeum.provider.build_llm_client", MagicMock(return_value=base_client))

    client = build_live_client()

    assert client is base_client
    assert client.tool_passthrough is False
