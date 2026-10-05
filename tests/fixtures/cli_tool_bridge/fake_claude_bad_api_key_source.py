#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A fake ``claude`` binary for athenaeum#1959's preflight AC (c): its
tool-passthrough init event reports an ``apiKeySource`` other than
``"none"`` -- the exact shape
:meth:`athenaeum.provider.ClaudeCliClient.run_tool_loop` raises
``CliToolBridgeError`` over (reused unchanged by the preflight, never
reimplemented). The single-shot text path (``--output-format json``)
answers cleanly, same as ``fake_claude_no_tool_call.py``, so the arms that
run BEFORE push_breadcrumb_pull in a group (NONE, PUSH_PAGES_UPPER_BOUND,
PUSH_BREADCRUMB) never fail for an unrelated reason -- the preflight must
fail on THIS guard specifically.

No real MCP round trip: the bridge guard raises as soon as the init event
is parsed, before any tool could be called, so nothing here ever needs to
connect to the real bridge server.
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
    sys.stdin.read()
    output_format = _arg_value(argv, "--output-format", "json")

    if output_format == "stream-json":
        mcp_config_path = _arg_value(argv, "--mcp-config")
        server_name = _mcp_server_name(mcp_config_path) if mcp_config_path else "harness"
        # ``connected: true`` so the EARLIER connected-check passes and the
        # apiKeySource check below is the one that actually fires --
        # anything else would mask the AC this fixture exists to drive.
        event = {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "api_key",
            "mcp_servers": [{"name": server_name, "status": "connected"}],
        }
        print(json.dumps(event), flush=True)
        return 0

    envelope = {
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
