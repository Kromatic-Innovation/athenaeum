# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1951 -- bridge round trip through a fake ``claude`` binary.

The fake binary (``tests/fixtures/cli_tool_bridge/fake_claude.py``) is not a
model: it reads ``--mcp-config`` out of its own argv exactly as
``ClaudeCliClient.run_tool_loop`` built it, launches the configured server
command itself, and drives it with a REAL MCP stdio client (``mcp.client.
stdio`` / ``mcp.ClientSession``). This proves the genuine
``list_tools``/``call_tool`` protocol round trip through
``athenaeum.cli_tool_bridge``'s child process end to end -- socket IPC on
the local machine, no network, no model call, no token spent.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from athenaeum.cli_tool_bridge import ToolBridgeHost
from athenaeum.provider import ClaudeCliClient

FIXTURE = Path(__file__).parent / "fixtures" / "cli_tool_bridge" / "fake_claude.py"


@pytest.fixture
def fake_claude_binary(tmp_path: Path) -> Path:
    """A copy of the fixture script with a shebang pointing at THIS
    interpreter (the venv that has ``athenaeum`` and ``mcp`` installed) --
    ``#!/usr/bin/env python3`` would resolve against PATH instead, which may
    not be this venv."""
    body = FIXTURE.read_text()
    _shebang, _nl, rest = body.partition("\n")
    script = tmp_path / "fake_claude.py"
    script.write_text(f"#!{sys.executable}\n{rest}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _recall_executor(name: str, tool_input: dict) -> str:
    if name == "mcp__athenaeum__recall":
        return f"recalled: {tool_input.get('query', '')}"
    return f"error: unknown tool {name!r}"


def test_bridge_round_trip_serves_specs_verbatim_and_calls_executor(
    fake_claude_binary: Path,
) -> None:
    client = ClaudeCliClient(binary=str(fake_claude_binary), tool_passthrough=True)
    instructions = json.dumps(
        {
            "calls": [{"name": "mcp__athenaeum__recall", "input": {"query": "pto policy"}}],
            "final_text": "the pto policy is...",
        }
    )
    # The fake binary reads instructions from stdin in place of a real
    # prompt -- see its own module docstring. ``run_tool_loop``'s
    # ``user_prompt`` argument IS what lands on stdin (never argv), so this
    # is a faithful stand-in for "whatever the model would have been asked".
    result = client.run_tool_loop(
        model="m-1",
        system="s",
        user_prompt=instructions,
        tools=[
            {
                "name": "mcp__athenaeum__recall",
                "description": "recall knowledge",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
        ],
        tool_executor=_recall_executor,
        max_turns=4,
    )

    assert result.turns_exhausted is False
    assert result.result["subtype"] == "success"

    tool_use_names = [
        block["name"]
        for event in result.events
        if event.get("type") == "assistant"
        for block in event["message"]["content"]
        if block.get("type") == "tool_use"
    ]
    # Mapped back to the SPEC name, never the transport's own namespacing.
    assert tool_use_names == ["mcp__athenaeum__recall"]

    tool_result_texts = [
        block["content"]
        for event in result.events
        if event.get("type") == "user"
        for block in event["message"]["content"]
        if block.get("type") == "tool_result"
    ]
    assert tool_result_texts == ["recalled: pto policy"]


def test_executor_exception_surfaces_as_api_mode_error_string(fake_claude_binary: Path) -> None:
    def _raising_executor(_name: str, _input: dict) -> str:
        raise KeyError("boom")

    client = ClaudeCliClient(binary=str(fake_claude_binary), tool_passthrough=True)
    instructions = json.dumps(
        {"calls": [{"name": "mcp__athenaeum__recall", "input": {"query": "x"}}], "final_text": "ok"}
    )
    result = client.run_tool_loop(
        model="m-1",
        system="s",
        user_prompt=instructions,
        tools=[
            {
                "name": "mcp__athenaeum__recall",
                "description": "d",
                "input_schema": {"type": "object"},
            }
        ],
        tool_executor=_raising_executor,
        max_turns=4,
    )
    tool_result_texts = [
        block["content"]
        for event in result.events
        if event.get("type") == "user"
        for block in event["message"]["content"]
        if block.get("type") == "tool_result"
    ]
    assert tool_result_texts == ["error: tool 'mcp__athenaeum__recall' raised KeyError: 'boom'"]


def _drive_bridge_child(
    tmp_path: Path, specs: list[dict], call_name: str, arguments: dict
) -> tuple[bool | None, list[str]]:
    """Run the bridge child and make ONE ``tools/call`` against it with a real
    stdio ``mcp.client`` / ``ClientSession``, returning ``(is_error, texts)``.

    Deliberately NOT routed through ``fake_claude.py``: that fixture reports
    only the text of each result, so ``is_error`` -- half of what the
    validation behaviour below is -- is not observable through it. This also
    calls the tool by the name ``tools/list`` actually SERVES, which is what a
    real ``claude -p`` does.
    """
    import asyncio

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    socket_path = tmp_path / "bridge.sock"
    tools_path = tmp_path / "tools.json"
    tools_path.write_text(json.dumps(specs), encoding="utf-8")

    async def _run() -> tuple[bool | None, list[str]]:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "athenaeum.cli_tool_bridge",
                "--socket",
                str(socket_path),
                "--tools",
                str(tools_path),
                "--server-name",
                "athenaeum",
            ],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(call_name, arguments)
                texts = [
                    block.text
                    for block in result.content
                    if getattr(block, "type", None) == "text"
                ]
                return getattr(result, "is_error", None), texts

    return asyncio.run(_run())


#: A realistic prefixed spec: ``tools/list`` serves the BARE segment
#: (``recall``) while the Host's executor expects the full spec name -- the
#: mapping ``provider._resolve_tool_naming`` produces for
#: ``mcp__athenaeum__recall``.
_RECALL_SPEC = [
    {
        "served_name": "recall",
        "spec_name": "mcp__athenaeum__recall",
        "description": "recall knowledge",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


def test_schema_violating_arguments_are_rejected_without_reaching_the_executor(
    tmp_path: Path,
) -> None:
    """athenaeum#1954: the bridge validates ``tools/call`` arguments against
    the schema it served, and a violation never reaches the Host.

    The pre-2.0 ``@server.call_tool()`` decorator did this for free
    (``validate_input=True`` by default). ``mcp`` 2.x's ``on_call_tool``
    callback does not -- its ``get_tool_input_schema`` hook is read only by
    ``Mcp-Param-*`` header validation on the Streamable-HTTP path, never by
    stdio ``tools/call`` dispatch -- so ``cli_tool_bridge`` performs it
    itself. Without this test the port could silently widen the Host's
    ``tool_executor`` contract to inputs it had never been handed before,
    which is invisible in every other test here: they all pass conforming
    arguments.
    """
    calls: list[tuple[str, dict]] = []

    def _recording_executor(name: str, tool_input: dict) -> str:
        calls.append((name, tool_input))
        return "executor should not have been reached"

    with ToolBridgeHost(str(tmp_path / "bridge.sock"), _recording_executor):
        is_error, texts = _drive_bridge_child(
            tmp_path, _RECALL_SPEC, "recall", {"unexpected": 1}
        )

    # Same message shape AND same is_error the pre-2.0 SDK produced, so a
    # model that learned to read it keeps reading it.
    assert is_error is True
    assert texts == ["Input validation error: 'query' is a required property"]
    # The point of the test: the Host was never contacted.
    assert calls == []


def test_conforming_arguments_reach_the_executor_under_the_spec_name(
    tmp_path: Path,
) -> None:
    """Positive control for the validation above: it must reject only what the
    schema actually rejects. Also pins the served-name -> spec-name mapping,
    which the validation lookup now shares -- the executor is handed the SPEC
    name even though the call arrived under the served one."""
    calls: list[tuple[str, dict]] = []

    def _recording_executor(name: str, tool_input: dict) -> str:
        calls.append((name, tool_input))
        return f"recalled: {tool_input['query']}"

    with ToolBridgeHost(str(tmp_path / "bridge.sock"), _recording_executor):
        is_error, texts = _drive_bridge_child(
            tmp_path, _RECALL_SPEC, "recall", {"query": "pto policy"}
        )

    assert is_error is False
    assert texts == ["recalled: pto policy"]
    assert calls == [("mcp__athenaeum__recall", {"query": "pto policy"})]


def test_bridge_round_trip_native_arm_naming(fake_claude_binary: Path) -> None:
    """The native-arm shape (bare ``grep``/``read``) resolves its bridge
    server as ``harness``, and ``run_tool_loop`` maps the transport's
    ``mcp__harness__grep`` model-visible name back to the bare spec name
    ``grep`` in ``events`` (the documented residual stays confined to the
    model-visible layer, never leaking into the recorded transcript)."""
    client = ClaudeCliClient(binary=str(fake_claude_binary), tool_passthrough=True)
    instructions = json.dumps(
        {"calls": [{"name": "grep", "input": {"pattern": "PTO"}}], "final_text": "found it"}
    )
    result = client.run_tool_loop(
        model="m-1",
        system="s",
        user_prompt=instructions,
        tools=[{"name": "grep", "description": "grep", "input_schema": {"type": "object"}}],
        tool_executor=lambda name, inp: f"matched: {inp.get('pattern')}",
        max_turns=4,
    )
    tool_use_names = [
        block["name"]
        for event in result.events
        if event.get("type") == "assistant"
        for block in event["message"]["content"]
        if block.get("type") == "tool_use"
    ]
    assert tool_use_names == ["grep"]
