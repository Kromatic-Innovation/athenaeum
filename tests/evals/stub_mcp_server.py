# SPDX-License-Identifier: Apache-2.0
"""A dependency-free stdio MCP server that exposes invented, unrelated tools.

Used by the host-config measurement (issue athenaeum#3057) to give a spawned
``claude -p`` session a realistic crowd of other MCP tools, so Claude Code
defers them behind ``ToolSearch``. The tools do nothing useful: every call
returns a fixed string. Usage: ``python stub_mcp_server.py <prefix> <count>``.
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2024-11-05"


def build_tools(prefix: str, count: int) -> list[dict[str, object]]:
    """*count* invented tools named ``<prefix>_op_<n>``."""
    tools = []
    for n in range(count):
        tools.append(
            {
                "name": f"{prefix}_op_{n}",
                "description": (
                    f"Invented stub operation {n} of the {prefix} service. It performs no "
                    "real work and exists only to add one more tool to the host's tool "
                    "list. Accepts an optional free-text argument and returns a fixed "
                    "acknowledgement string."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"arg": {"type": "string", "description": "Ignored."}},
                },
            }
        )
    return tools


def handle(message: dict[str, object], tools: list[dict[str, object]], prefix: str):
    """The JSON-RPC response for *message*, or ``None`` for a notification."""
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None
    if method == "initialize":
        result: dict[str, object] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"{prefix}-stub", "version": "0"},
        }
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "ok (stub)"}]}
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "n/a"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main(argv: list[str]) -> int:
    prefix = argv[1] if len(argv) > 1 else "stub"
    count = int(argv[2]) if len(argv) > 2 else 8
    tools = build_tools(prefix, count)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = handle(json.loads(line), tools, prefix)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
