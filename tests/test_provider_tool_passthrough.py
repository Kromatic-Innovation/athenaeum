# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1951 -- opt-in, eval-only tool passthrough on ``ClaudeCliClient``.

Fixture-only, same discipline as ``tests/test_provider.py``: every
``subprocess.run`` is stubbed, or (for the bridge round trip) replaced with a
small fake ``claude`` binary script that talks real MCP stdio to the real
bridge child -- but that child, and everything this test drives, is local
IPC over a Unix socket. No test here makes a model call or spends a token.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from athenaeum.provider import (
    ClaudeCliClient,
    CliToolBridgeError,
    CliToolLoopResult,
    _resolve_tool_naming,
    build_llm_client,
)

RECALL_SPECS = [
    {"name": "mcp__athenaeum__recall", "description": "x" * 10, "input_schema": {"type": "object"}},
    {
        "name": "mcp__athenaeum__read_entity",
        "description": "y" * 10,
        "input_schema": {"type": "object"},
    },
]

NATIVE_SPECS = [
    {"name": "grep", "description": "g" * 10, "input_schema": {"type": "object"}},
    {"name": "read", "description": "r" * 10, "input_schema": {"type": "object"}},
]


def _init_event(server_name: str, api_key_source: str = "none") -> str:
    return json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": api_key_source,
            "mcp_servers": [{"name": server_name, "status": "connected"}],
        }
    )


def _success_result() -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "usage": {"output_tokens": 1}}
    )


def _max_turns_result() -> str:
    return json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True})


def _stub_tool_run(monkeypatch, *, stdout: str, returncode: int = 0, stderr: str = ""):
    capture: dict[str, Any] = {}

    def fake_run(argv, **kwargs):
        capture["argv"] = argv
        capture["kwargs"] = kwargs
        # Snapshot the written mcp/tools config BEFORE run_tool_loop's
        # finally clause removes the temp dir.
        cwd = kwargs.get("cwd")
        if cwd:
            mcp_path = os.path.join(cwd, "mcp.json")
            tools_path = os.path.join(cwd, "tools.json")
            if os.path.exists(mcp_path):
                capture["mcp_config"] = json.loads(Path(mcp_path).read_text())
            if os.path.exists(tools_path):
                capture["tools_json"] = json.loads(Path(tools_path).read_text())
            capture["tmpdir"] = cwd
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("athenaeum.provider.subprocess.run", fake_run)
    monkeypatch.setattr("athenaeum.provider.shutil.which", lambda _b: "/usr/bin/claude")
    return capture


def _noop_executor(_name: str, _input: dict[str, Any]) -> str:
    return "ok"


# ---------------------------------------------------------------------------
# AC: text-only argv unchanged
# ---------------------------------------------------------------------------


def test_text_only_argv_byte_identical_to_pre_1951():
    """Pinned against the literal list ``ClaudeCliClient()._build_argv("m-1",
    "s")`` produces on an unmodified origin/develop checkout (verified by
    hand before this issue's code landed, not reconstructed from reading
    the source)."""
    client = ClaudeCliClient()
    assert client._build_argv("m-1", "s") == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        "m-1",
        "--system-prompt",
        "s",
        "--tools",
        "",
        "--strict-mcp-config",
    ]


# ---------------------------------------------------------------------------
# AC: silent drop is gone
# ---------------------------------------------------------------------------


class TestFailLoudOnTools:
    def test_raises_without_passthrough(self, monkeypatch):
        capture = _stub_tool_run(monkeypatch, stdout="")
        client = ClaudeCliClient()
        with pytest.raises(ValueError, match="run_tool_loop"):
            client.messages.create(model="m-1", system="s", messages=[], tools=[{"name": "x"}])
        assert "argv" not in capture

    def test_raises_even_with_passthrough_enabled(self, monkeypatch):
        capture = _stub_tool_run(monkeypatch, stdout="")
        client = ClaudeCliClient(tool_passthrough=True)
        with pytest.raises(ValueError, match="run_tool_loop"):
            client.messages.create(model="m-1", system="s", messages=[], tools=[{"name": "x"}])
        assert "argv" not in capture

    def test_no_production_call_site_passes_tools_to_messages_create(self):
        """Mechanical grep: no ``messages.create(`` call under ``src/athenaeum/``
        passes ``tools=`` -- the only caller that does
        (``run_api_tool_loop``) lives under ``tests/evals/`` and is routed
        to ``run_tool_loop`` instead for a CLI client (issue athenaeum#1951)."""
        src_root = Path(__file__).resolve().parents[1] / "src" / "athenaeum"
        offenders = []
        for path in src_root.rglob("*.py"):
            if path.name == "provider.py":
                # provider.py is the backend itself, not a call site -- its
                # only `tools=`-shaped text is the fail-loud ValueError
                # message literal, not a messages.create(...) invocation.
                continue
            lines = path.read_text().splitlines()
            for line_no, line in enumerate(lines, start=1):
                if "messages.create(" not in line:
                    continue
                # Join a few lines so a multi-line call's kwargs are visible,
                # but stop expanding at the statement's closing paren.
                window = "\n".join(lines[line_no - 1 : line_no + 8])
                if "tools=" in window.split(")")[0] + ")":
                    offenders.append(f"{path}:{line_no}")
        assert offenders == [], f"messages.create(...) passing tools= in src/: {offenders}"

    def test_tool_passthrough_true_literal_appears_nowhere_in_src(self):
        """Issue athenaeum#1951 AC: the opt-in flag is never flipped on for a
        caller from within ``src/athenaeum/`` -- only a caller (the eval
        harness) may do that explicitly."""
        src_root = Path(__file__).resolve().parents[1] / "src" / "athenaeum"
        offenders = [
            str(path)
            for path in src_root.rglob("*.py")
            if "tool_passthrough=True" in path.read_text()
        ]
        assert offenders == [], f"tool_passthrough=True found in src/: {offenders}"


# ---------------------------------------------------------------------------
# AC: opt-in gate
# ---------------------------------------------------------------------------


class TestOptInGate:
    def test_run_tool_loop_raises_without_spawning(self, monkeypatch):
        capture = _stub_tool_run(monkeypatch, stdout="")
        client = ClaudeCliClient()  # tool_passthrough defaults False
        with pytest.raises(RuntimeError, match="opt-in"):
            client.run_tool_loop(
                model="m-1",
                system="s",
                user_prompt="hi",
                tools=RECALL_SPECS,
                tool_executor=_noop_executor,
                max_turns=4,
            )
        assert "argv" not in capture

    def test_build_llm_client_claude_cli_defaults_passthrough_off(self, monkeypatch):
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
        client = build_llm_client(None)
        assert isinstance(client, ClaudeCliClient)
        assert client.tool_passthrough is False


# ---------------------------------------------------------------------------
# AC: golden passthrough argv
# ---------------------------------------------------------------------------


class TestGoldenPassthroughArgv:
    @pytest.mark.parametrize(
        "specs,expected_server,expected_allowed",
        [
            (RECALL_SPECS, "athenaeum", ["mcp__athenaeum__recall", "mcp__athenaeum__read_entity"]),
            (NATIVE_SPECS, "harness", ["mcp__harness__grep", "mcp__harness__read"]),
        ],
    )
    def test_golden_argv_and_env_and_config(
        self, monkeypatch, specs, expected_server, expected_allowed
    ):
        server_name = expected_server
        capture = _stub_tool_run(
            monkeypatch,
            stdout="\n".join([_init_event(server_name), _success_result()]) + "\n",
        )
        monkeypatch.setenv("CLAUDE_CODE_SAFE_MODE", "1")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy-do-not-use")

        client = ClaudeCliClient(binary="claude", tool_passthrough=True)
        result = client.run_tool_loop(
            model="m-1",
            system="sys-prompt",
            user_prompt="the user prompt text",
            tools=specs,
            tool_executor=_noop_executor,
            max_turns=5,
        )
        assert isinstance(result, CliToolLoopResult)
        assert result.turns_exhausted is False

        argv = capture["argv"]
        assert argv[0] == "claude"
        assert argv[1:6] == [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-hook-events",
        ]
        assert "--model" in argv and argv[argv.index("--model") + 1] == "m-1"
        assert "--system-prompt" in argv and argv[argv.index("--system-prompt") + 1] == "sys-prompt"

        def _value_after(flag: str) -> str:
            i = argv.index(flag)
            return argv[i + 1]

        assert _value_after("--setting-sources") == ""
        assert "--no-session-persistence" in argv
        assert _value_after("--max-turns") == "5"
        assert _value_after("--tools") == ""

        # Each variadic flag is followed by a "--"-flag or is the last element.
        for flag in ("--tools", "--mcp-config", "--allowedTools"):
            i = argv.index(flag)
            # value(s) start right after; find the next flag-looking token.
            j = i + 1
            while j < len(argv) and not argv[j].startswith("--"):
                j += 1
            assert j == len(argv) or argv[j].startswith("--")

        mcp_config_path = _value_after("--mcp-config")
        assert argv[-1] == expected_allowed[-1]
        allowed_start = argv.index("--allowedTools") + 1
        assert argv[allowed_start:] == expected_allowed

        # Prompt only on stdin, never argv.
        assert "the user prompt text" not in argv
        assert capture["kwargs"]["input"] == "the user prompt text"

        env = capture["kwargs"]["env"]
        assert "CLAUDE_CODE_SAFE_MODE" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert env["MAX_THINKING_TOKENS"] == "0"
        assert env["CLAUDE_SUPPRESS_NOTIFY"] == "1"

        mcp_config = capture["mcp_config"]
        server_entry = mcp_config["mcpServers"][server_name]
        assert server_entry["type"] == "stdio"
        assert server_entry["command"] == sys.executable
        assert server_entry["args"][:2] == ["-m", "athenaeum.cli_tool_bridge"]
        assert "--socket" in server_entry["args"]
        assert "--tools" in server_entry["args"]
        assert server_entry["args"][server_entry["args"].index("--server-name") + 1] == server_name
        assert server_entry["env"] == {}
        assert mcp_config_path.endswith("mcp.json")

        # Temp dir is gone afterward.
        assert not os.path.exists(capture["tmpdir"])

    def test_mixed_naming_raises_value_error_without_spawning(self, monkeypatch):
        capture = _stub_tool_run(monkeypatch, stdout="")
        client = ClaudeCliClient(tool_passthrough=True)
        mixed = [RECALL_SPECS[0], NATIVE_SPECS[0]]
        with pytest.raises(ValueError, match="mixed tool naming"):
            client.run_tool_loop(
                model="m-1",
                system="s",
                user_prompt="hi",
                tools=mixed,
                tool_executor=_noop_executor,
                max_turns=3,
            )
        assert "argv" not in capture

    def test_resolve_tool_naming_helper_directly(self):
        server, served, visible = _resolve_tool_naming(["mcp__athenaeum__recall"])
        assert server == "athenaeum"
        assert served == {"mcp__athenaeum__recall": "recall"}
        assert visible == {"mcp__athenaeum__recall": "mcp__athenaeum__recall"}

        server, served, visible = _resolve_tool_naming(["grep"])
        assert server == "harness"
        assert served == {"grep": "grep"}
        assert visible == {"grep": "mcp__harness__grep"}


# ---------------------------------------------------------------------------
# AC: stream guards
# ---------------------------------------------------------------------------


class TestStreamGuards:
    def _run(self, monkeypatch, stdout: str):
        _stub_tool_run(monkeypatch, stdout=stdout)
        client = ClaudeCliClient(tool_passthrough=True)
        return client.run_tool_loop(
            model="m-1",
            system="s",
            user_prompt="hi",
            tools=RECALL_SPECS,
            tool_executor=_noop_executor,
            max_turns=3,
        )

    def test_server_not_connected(self, monkeypatch):
        stdout = (
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "apiKeySource": "none",
                    "mcp_servers": [{"name": "athenaeum", "status": "failed"}],
                }
            )
            + "\n"
            + _success_result()
            + "\n"
        )
        with pytest.raises(CliToolBridgeError, match="connected"):
            self._run(monkeypatch, stdout)

    def test_api_key_source_not_none(self, monkeypatch):
        stdout = (
            _init_event("athenaeum", api_key_source="/some/helper")
            + "\n"
            + _success_result()
            + "\n"
        )
        with pytest.raises(CliToolBridgeError, match="apiKeySource"):
            self._run(monkeypatch, stdout)

    def test_hook_started_event(self, monkeypatch):
        stdout = (
            _init_event("athenaeum")
            + "\n"
            + json.dumps({"type": "system", "subtype": "hook_started"})
            + "\n"
            + _success_result()
            + "\n"
        )
        with pytest.raises(CliToolBridgeError, match="hook"):
            self._run(monkeypatch, stdout)

    def test_missing_init_event(self, monkeypatch):
        stdout = _success_result() + "\n"
        with pytest.raises(CliToolBridgeError, match="init"):
            self._run(monkeypatch, stdout)


# ---------------------------------------------------------------------------
# AC: turn cap
# ---------------------------------------------------------------------------


def test_turn_cap_gives_turns_exhausted_true_no_exception(monkeypatch):
    stdout = (
        _init_event("athenaeum")
        + "\n"
        + json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]
                },
            }
        )
        + "\n"
        + _max_turns_result()
        + "\n"
    )
    _stub_tool_run(monkeypatch, stdout=stdout, returncode=1)
    client = ClaudeCliClient(tool_passthrough=True)
    result = client.run_tool_loop(
        model="m-1",
        system="s",
        user_prompt="hi",
        tools=RECALL_SPECS,
        tool_executor=_noop_executor,
        max_turns=1,
    )
    assert result.turns_exhausted is True
    assert result.result["subtype"] == "error_max_turns"


# ---------------------------------------------------------------------------
# AC: librarian unaffected
# ---------------------------------------------------------------------------


class TestLibrarianUnaffected:
    def _argv_env(self, monkeypatch, **create_kwargs):
        capture: dict[str, Any] = {}

        def fake_run(argv, **kwargs):
            capture["argv"] = argv
            capture["env"] = kwargs.get("env", {})
            import json as _json

            return SimpleNamespace(
                returncode=0,
                stdout=_json.dumps(
                    {"subtype": "success", "is_error": False, "result": "{}", "usage": {}}
                ),
                stderr="",
            )

        monkeypatch.setattr("athenaeum.provider.subprocess.run", fake_run)
        monkeypatch.setattr("athenaeum.provider.shutil.which", lambda _b: "/usr/bin/claude")
        client = ClaudeCliClient()
        client.messages.create(**create_kwargs)
        return capture

    @pytest.mark.parametrize("label", ["classify", "write", "resolve"])
    def test_classify_write_resolve_argv_and_env_unchanged(self, monkeypatch, label):
        capture = self._argv_env(
            monkeypatch,
            model="m-1",
            system=f"{label} prompt",
            messages=[{"role": "user", "content": "x"}],
        )
        argv = capture["argv"]
        assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
        assert "--strict-mcp-config" in argv
        i = argv.index("--tools")
        assert argv[i + 1] == ""
        env = capture["env"]
        assert env.get("CLAUDE_CODE_SAFE_MODE") == "1"
