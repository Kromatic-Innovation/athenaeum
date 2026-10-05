#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A fake ``claude`` binary for the athenaeum#1951 arm-end-to-end test.

Unlike ``fake_claude.py`` (which reads structured instructions off stdin),
this one treats stdin as an ordinary prompt: it discovers whatever single
tool the generated ``--mcp-config`` serves, calls it once with
``{"query": <the whole stdin prompt>}`` (the shape every recall-style tool
in this codebase expects), and emits a fixed final answer. It exists so a
real arm function (:func:`tests.evals.rollout.run_push_breadcrumb_pull_api`)
can be driven end to end through the real
:class:`athenaeum.provider.ClaudeCliClient` without the test needing to
control the exact prompt text the arm builds.

Issue athenaeum#1959: also answers the text-only ``--output-format json``
path (:meth:`athenaeum.provider.ClaudeCliClient._create`, what the NONE/
PUSH_PAGES_UPPER_BOUND/PUSH_BREADCRUMB/ORACLE single-shot arms call) with a
plain success envelope and no tool call at all -- the preflight's own group
run (:func:`tests.evals.rollout.run_probe_all_arms`) runs EVERY arm, not
just ``push_breadcrumb_pull``, and those four arms never pass
``--mcp-config`` in the first place, so there is no tool to call on this
path. Every EXISTING caller of this fixture drives it directly through
:func:`tests.evals.rollout.run_push_breadcrumb_pull_api` (tool-loop mode
only), so this branch is purely additive -- it was previously unreachable.
"""
from __future__ import annotations

import asyncio
import json
import sys


def _arg_value(argv: list[str], flag: str, default: str | None = None) -> str | None:
    if flag not in argv:
        return default
    return argv[argv.index(flag) + 1]


async def _drive(mcp_config_path: str, prompt: str) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    with open(mcp_config_path, encoding="utf-8") as f:
        mcp_config = json.load(f)
    servers = mcp_config["mcpServers"]
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
            tools = await session.list_tools()
            served_name = tools.tools[0].name
            model_visible = (
                f"mcp__{server_name}__{served_name}" if server_name != "harness" else served_name
            )
            events.append(
                {
                    "type": "assistant",
                    "message": {
                        "id": "msg_1",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_0",
                                "name": model_visible,
                                "input": {"query": prompt},
                            }
                        ],
                    },
                }
            )
            result = await session.call_tool(served_name, {"query": prompt})
            texts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
            events.append(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_0",
                                "content": "\n\n".join(texts),
                            }
                        ]
                    },
                }
            )

    events.append(
        {
            "type": "assistant",
            "message": {"id": "msg_2", "content": [{"type": "text", "text": "done"}]},
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
    prompt = sys.stdin.read()
    output_format = _arg_value(argv, "--output-format", "json")
    if output_format != "stream-json":
        # The single-shot text-only path: no --mcp-config is ever passed on
        # this argv shape, so there is no tool to call.
        envelope = {
            "subtype": "success",
            "is_error": False,
            "result": "no tool call needed",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        print(json.dumps(envelope))
        return 0
    mcp_config_path = _arg_value(argv, "--mcp-config")
    asyncio.run(_drive(mcp_config_path, prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
