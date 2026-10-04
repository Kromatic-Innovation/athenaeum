#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A fake ``claude`` binary for the athenaeum#1951 bridge round-trip test.

Not a model: it reads ``--mcp-config`` out of its own argv (exactly as
``ClaudeCliClient.run_tool_loop`` built it), launches the configured bridge
server command itself, and drives it with a REAL MCP stdio client
(``mcp.client.stdio`` / ``mcp.ClientSession``) -- so the test exercises the
genuine ``list_tools``/``call_tool`` protocol round trip through
``athenaeum.cli_tool_bridge``'s child process, not a mocked one. It then
emits canned ``stream-json`` lines that embed whatever text the bridge
really returned, so the test can assert on that text having round-tripped
through the real tool call.

Instructions come from stdin as one JSON object:
``{"calls": [{"name": <served tool name>, "input": {...}}, ...],
  "final_text": <str>, "max_turns": <int>}``
-- *not* the full prompt the eval would send; this binary only exists to
drive the protocol, not to simulate a conversation.
"""
from __future__ import annotations

import asyncio
import json
import sys


def _arg_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _model_visible_name(server_name: str, served_name: str) -> str:
    """Mirror ``athenaeum.provider._resolve_tool_naming``'s model-visible
    naming rule: identity when the server is named after the tool's own
    namespace segment, ``mcp__harness__<name>`` otherwise."""
    if server_name == "harness":
        return f"mcp__harness__{served_name}"
    return served_name


async def _drive(mcp_config_path: str, calls: list[dict], final_text: str) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    with open(mcp_config_path, encoding="utf-8") as f:
        mcp_config = json.load(f)
    servers = mcp_config["mcpServers"]
    assert len(servers) == 1, f"fake claude expects exactly one mcp server, got {servers!r}"
    server_name, server_cfg = next(iter(servers.items()))

    params = StdioServerParameters(
        command=server_cfg["command"],
        args=server_cfg["args"],
        env=server_cfg.get("env") or None,
    )

    events: list[dict] = [
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "none",
            "mcp_servers": [{"name": server_name, "status": "connected"}],
        }
    ]

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_use_blocks = []
            for i, call in enumerate(calls):
                tool_use_blocks.append(
                    {
                        "type": "tool_use",
                        "id": f"toolu_{i}",
                        "name": _model_visible_name(server_name, call["name"]),
                        "input": call["input"],
                    }
                )
            events.append(
                {
                    "type": "assistant",
                    "message": {"id": "msg_1", "content": tool_use_blocks},
                }
            )
            tool_result_blocks = []
            for i, call in enumerate(calls):
                result = await session.call_tool(call["name"], call["input"])
                texts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": f"toolu_{i}",
                        "content": "\n\n".join(texts),
                    }
                )
            events.append({"type": "user", "message": {"content": tool_result_blocks}})

    events.append(
        {
            "type": "assistant",
            "message": {
                "id": "msg_2",
                "content": [{"type": "text", "text": final_text}],
            },
        }
    )
    events.append(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
    )
    for event in events:
        print(json.dumps(event), flush=True)


def main() -> int:
    argv = sys.argv[1:]
    mcp_config_path = _arg_value(argv, "--mcp-config")
    instructions = json.loads(sys.stdin.read() or "{}")
    calls = instructions.get("calls", [])
    final_text = instructions.get("final_text", "done")
    asyncio.run(_drive(mcp_config_path, calls, final_text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
