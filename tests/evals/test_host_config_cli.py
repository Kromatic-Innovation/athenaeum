# SPDX-License-Identifier: Apache-2.0
"""Offline pins for the host-config measurement wiring (issue athenaeum#3057)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.evals import host_config_cli as hc
from tests.evals import stub_mcp_server
from tests.evals.rollout import (
    RECALL_TOOL_NAME,
    HostConfig,
    build_pull_argv,
    build_pull_mcp_config,
)


def _assistant(*names: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": n, "input": {}} for n in names]},
    }


def _init(*tools: str) -> dict:
    return {"type": "system", "subtype": "init", "tools": list(tools)}


def test_default_argv_and_config_unchanged_by_host_config_none() -> None:
    argv = build_pull_argv("claude", Path("/x/c.json"), "m")
    assert argv == build_pull_argv("claude", Path("/x/c.json"), "m", host_config=None)
    assert "--plugin-dir" not in argv
    cfg = build_pull_mcp_config(Path("/k"), Path("/c"), athenaeum_bin="ath")
    assert list(cfg["mcpServers"]) == ["athenaeum"]


def test_host_config_adds_servers_plugin_dir_and_approvals() -> None:
    host = HostConfig(
        name="C",
        extra_servers={"harbor": {"type": "stdio", "command": "x"}},
        plugin_dir=Path("/p"),
        allowed_tools=("mcp__plugin_woz_code",),
    )
    cfg = build_pull_mcp_config(Path("/k"), Path("/c"), athenaeum_bin="ath", host_config=host)
    assert list(cfg["mcpServers"]) == ["athenaeum", "harbor"]
    argv = build_pull_argv("claude", Path("/x/c.json"), "m", host_config=host)
    assert argv[argv.index("--plugin-dir") + 1] == "/p"
    assert "--strict-mcp-config" in argv
    assert "mcp__plugin_woz_code" in argv


def test_tool_search_counted_before_first_recall_only() -> None:
    names = ["ToolSearch", "ToolSearch", RECALL_TOOL_NAME, "ToolSearch", RECALL_TOOL_NAME]
    assert hc.count_tool_search_before_recall(names) == (2, 3, 2)
    assert hc.count_tool_search_before_recall(["ToolSearch"]) == (None, 1, 0)
    assert hc.count_tool_search_before_recall([RECALL_TOOL_NAME]) == (0, 0, 1)


def test_assistant_tool_uses_keeps_order() -> None:
    events = [_init("a"), _assistant("ToolSearch"), _assistant(RECALL_TOOL_NAME, "Read")]
    assert hc._assistant_tool_uses(events) == ["ToolSearch", RECALL_TOOL_NAME, "Read"]


@pytest.mark.parametrize(
    ("config", "tools", "ok"),
    [
        ("A", [RECALL_TOOL_NAME], True),
        ("A", ["Read"], False),
        ("B", ["ToolSearch", "Read"], True),
        ("B", ["ToolSearch", RECALL_TOOL_NAME], False),
        ("C", ["Read"], False),
    ],
)
def test_wiring_check(config: str, tools: list[str], ok: bool) -> None:
    assert hc.check_wiring(config, [_init(*tools)])[0] is ok


def test_wiring_check_fails_without_init() -> None:
    assert hc.check_wiring("A", [])[0] is False


def test_cost_from_result_event() -> None:
    assert hc.result_cost_usd([{"type": "result", "total_cost_usd": 0.25}]) == 0.25
    assert hc.result_cost_usd([{"type": "result"}]) is None


def test_aggregate_excludes_wiring_failures_and_divides_cost_by_correct() -> None:
    def run(recalls: int, correct: bool, cost: float, ok: bool = True) -> hc.RunMetrics:
        return hc.RunMetrics(
            "B", "p", recalls, 1 if recalls else None, 1, correct, cost, ok, "", None
        )

    row = hc.aggregate("B", [run(1, True, 1.0), run(0, False, 1.0), run(1, True, 2.0, ok=False)])
    assert (row.n_runs, row.n_excluded) == (2, 1)
    assert row.recall_rate == 0.5
    assert row.correct_rate == 0.5
    assert row.cost_per_correct == 2.0
    assert row.mean_tool_search_before_first_recall == 1.0
    assert hc.aggregate("A", []).cost_per_correct is None


def test_build_host_config_b_forces_deferral_and_c_requires_plugin(tmp_path: Path) -> None:
    stub = tmp_path / "s.py"
    a = hc.build_host_config("A", python="py", stub_script=stub, plugin_copy=None)
    assert not a.extra_servers and not a.force_tool_search
    b = hc.build_host_config("B", python="py", stub_script=stub, plugin_copy=None)
    assert set(b.extra_servers) == {p for p, _ in hc.STUB_SERVERS} and b.force_tool_search
    with pytest.raises(ValueError):
        hc.build_host_config("C", python="py", stub_script=stub, plugin_copy=None)


def test_plugin_servers_resolved_and_analytics_env_dropped(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "code": {
                        "command": "node",
                        "args": ["${CLAUDE_PLUGIN_ROOT}/s.js"],
                        "alwaysLoad": True,
                        "env": {"WOZCODE_POSTHOG_X": "t", "KEEP": "1"},
                    }
                }
            }
        )
    )
    entries = hc.plugin_server_entries(tmp_path)
    spec = entries["plugin_woz_code"]
    assert spec["args"] == [f"{tmp_path}/s.js"]
    assert spec["env"] == {"KEEP": "1"}
    assert "alwaysLoad" not in spec
    c = hc.build_host_config("C", python="py", stub_script=tmp_path / "s", plugin_copy=tmp_path)
    assert c.plugin_dir == tmp_path and c.allowed_tools == ("mcp__plugin_woz_code",)


def test_stub_server_speaks_minimal_mcp() -> None:
    tools = stub_mcp_server.build_tools("harbor", 3)
    assert [t["name"] for t in tools] == ["harbor_op_0", "harbor_op_1", "harbor_op_2"]
    init = stub_mcp_server.handle({"id": 1, "method": "initialize"}, tools, "harbor")
    assert init["result"]["capabilities"] == {"tools": {}}
    listed = stub_mcp_server.handle({"id": 2, "method": "tools/list"}, tools, "harbor")
    assert len(listed["result"]["tools"]) == 3
    assert stub_mcp_server.handle({"method": "notifications/initialized"}, tools, "harbor") is None
