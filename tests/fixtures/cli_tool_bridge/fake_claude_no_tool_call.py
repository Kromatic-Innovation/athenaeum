#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A fake ``claude`` binary for athenaeum#1959's preflight AC (b): answers
every call -- both the single-shot text path (``--output-format json``,
used by the NONE/PUSH_PAGES_UPPER_BOUND/PUSH_BREADCRUMB/ORACLE arms'
``ClaudeCliClient._create``) and the tool-passthrough stream-json path
(used by the PUSH_BREADCRUMB_PULL/PULL/NATIVE_INDEX/NATIVE_GREP arms'
``ClaudeCliClient.run_tool_loop``) -- WITHOUT ever calling a tool.

This exercises ``tests.evals.rollout.parse_stream``'s own contract that
NOT calling a tool is a legitimate, never-erroring outcome, all the way
through the real :class:`athenaeum.provider.ClaudeCliClient` bridge
guards (connected + ``apiKeySource``), which this fake satisfies cleanly
(``apiKeySource: "none"``) so that the preflight's OWN "recall was never
called" check -- not a bridge guard -- is what fails the run.

No real MCP round trip is needed (no tool is ever called), so this script
reads ``--mcp-config`` only to discover the server name its init event
must report connected -- it never actually launches that server.
"""
from __future__ import annotations

import json
import sys


def _arg_value(argv: list[str], flag: str, default: str | None = None) -> str | None:
    if flag not in argv:
        return default
    return argv[argv.index(flag) + 1]


def _mcp_server_name(mcp_config_path: str) -> str:
    with open(mcp_config_path, encoding="utf-8") as f:
        mcp_config = json.load(f)
    return next(iter(mcp_config["mcpServers"]))


def main() -> int:
    argv = sys.argv[1:]
    sys.stdin.read()  # the prompt -- never used, this fake never calls a tool
    output_format = _arg_value(argv, "--output-format", "json")

    if output_format == "stream-json":
        mcp_config_path = _arg_value(argv, "--mcp-config")
        server_name = _mcp_server_name(mcp_config_path) if mcp_config_path else "harness"
        events = [
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "none",
                "mcp_servers": [{"name": server_name, "status": "connected"}],
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [{"type": "text", "text": "no tool call needed"}],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 2,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        ]
        for event in events:
            print(json.dumps(event), flush=True)
        return 0

    envelope = {
        "subtype": "success",
        "is_error": False,
        "result": "no tool call needed",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
