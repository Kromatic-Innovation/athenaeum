# SPDX-License-Identifier: Apache-2.0
"""Tests for the pull-diagnosis reader (issue athenaeum#3032).

Every ledger row and transcript record here is a hand-authored synthetic
fixture. No real transcript content enters this file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from athenaeum import cli, pull_diagnosis, push_metrics

MEM = "cfg/.claude/projects/-scope-hash/memory"


def _seed(cache: Path, rows: list[dict]) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    path = push_metrics.push_records_path(cache)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _push(session_id: str, ts: str, *, source: str | None = "sidecar") -> dict:
    row = {"ts": ts, "session_id": session_id, "items": [{"id": "aaa11111"}]}
    if source is not None:
        row["source"] = source
    return row


def _transcript(projects: Path, session_id: str, records: list[dict]) -> None:
    scope = projects / "-scope-hash"
    scope.mkdir(parents=True, exist_ok=True)
    (scope / f"{session_id}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _prompt(ts: str, text: str = "please do the thing", entrypoint: str = "cli") -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "entrypoint": entrypoint,
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": text},
    }


def _tool(ts: str, name: str, tool_input: dict, entrypoint: str = "cli") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "entrypoint": entrypoint,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"tu-{ts}", "name": name, "input": tool_input}],
        },
    }


def _deferred(ts: str, names: list[str], *, failed: list[str] | None = None) -> dict:
    return {
        "type": "attachment",
        "timestamp": ts,
        "attachment": {
            "type": "deferred_tools_delta",
            "addedNames": names,
            "failedMcpServers": failed or [],
            "pendingMcpServers": [],
            "needsAuthMcpServers": [],
        },
    }


def _report(tmp_path: Path, **kw: object) -> dict:
    return pull_diagnosis.build_pull_diagnosis_report(
        cache_dir=tmp_path / "cache", projects_root=tmp_path / "projects", **kw
    ).to_dict()


class TestWindows:
    def test_only_windows_holding_a_proactive_push_count(self, tmp_path: Path) -> None:
        _seed(tmp_path / "cache", [_push("s1", "2026-01-01T00:00:05Z")])
        _transcript(
            tmp_path / "projects",
            "s1",
            [
                _prompt("2026-01-01T00:00:00Z"),
                _tool("2026-01-01T00:00:10Z", "mcp__athenaeum__recall", {"query": "x"}),
                _prompt("2026-01-01T00:01:00Z"),
                _tool("2026-01-01T00:01:10Z", "mcp__athenaeum__recall", {"query": "y"}),
            ],
        )
        ch = _report(tmp_path)["channels"]["interactive"]
        assert ch["sessions"] == 1
        assert ch["windows"] == 1
        assert ch["windows_with"]["recall_call"] == 1
        assert ch["calls"]["recall_call"] == 1  # the second window's call is out of scope

    def test_same_second_push_belongs_to_its_own_prompt(self, tmp_path: Path) -> None:
        # The ledger stamps whole seconds; the prompt record carries milliseconds.
        _seed(tmp_path / "cache", [_push("s1", "2026-01-01T00:00:00Z")])
        _transcript(
            tmp_path / "projects",
            "s1",
            [
                _prompt("2026-01-01T00:00:00.700Z"),
                _tool("2026-01-01T00:00:05Z", "mcp__athenaeum__recall", {"query": "x"}),
                _prompt("2026-01-01T00:01:00.300Z"),
            ],
        )
        report = _report(tmp_path)
        assert report["sessions_without_pushed_window"] == 0
        ch = report["channels"]["interactive"]
        assert ch["windows"] == 1
        assert ch["windows_with"]["recall_call"] == 1

    def test_mcp_recall_rows_are_not_pushes(self, tmp_path: Path) -> None:
        _seed(tmp_path / "cache", [_push("s1", "2026-01-01T00:00:05Z", source=None)])
        _transcript(tmp_path / "projects", "s1", [_prompt("2026-01-01T00:00:00Z")])
        report = _report(tmp_path)
        assert report["sessions_in_ledger"] == 0
        assert report["channels"]["interactive"]["windows"] == 0

    def test_before_cutoff_drops_later_pushes(self, tmp_path: Path) -> None:
        _seed(
            tmp_path / "cache",
            [_push("s1", "2026-01-01T00:00:05Z"), _push("s2", "2026-02-01T00:00:05Z")],
        )
        _transcript(tmp_path / "projects", "s1", [_prompt("2026-01-01T00:00:00Z")])
        _transcript(tmp_path / "projects", "s2", [_prompt("2026-02-01T00:00:00Z")])
        report = _report(tmp_path, before=datetime(2026, 1, 15, tzinfo=UTC))
        assert report["sessions_in_ledger"] == 1
        assert report["channels"]["interactive"]["windows"] == 1
        assert report["before"] == "2026-01-15T00:00:00+00:00"

    def test_naive_before_is_treated_as_utc(self, tmp_path: Path) -> None:
        _seed(tmp_path / "cache", [_push("s1", "2026-01-01T00:00:05Z")])
        _transcript(tmp_path / "projects", "s1", [_prompt("2026-01-01T00:00:00Z")])
        report = _report(tmp_path, before=datetime(2026, 1, 15))
        assert report["channels"]["interactive"]["windows"] == 1

    def test_missing_transcript_is_counted_not_guessed(self, tmp_path: Path) -> None:
        _seed(tmp_path / "cache", [_push("gone", "2026-01-01T00:00:05Z")])
        report = _report(tmp_path)
        assert report["sessions_skipped_no_transcript"] == 1
        assert all(c["sessions"] == 0 for c in report["channels"].values())

    def test_push_outside_every_window_is_counted(self, tmp_path: Path) -> None:
        _seed(tmp_path / "cache", [_push("s1", "2025-12-31T00:00:00Z")])
        _transcript(tmp_path / "projects", "s1", [_prompt("2026-01-01T00:00:00Z")])
        report = _report(tmp_path)
        assert report["sessions_without_pushed_window"] == 1

    def test_headless_first_window_only_by_default(self, tmp_path: Path) -> None:
        _seed(
            tmp_path / "cache",
            [_push("h1", "2026-01-01T00:00:05Z"), _push("h1", "2026-01-01T00:01:05Z")],
        )
        recs = [
            _prompt("2026-01-01T00:00:00Z", entrypoint="sdk-cli"),
            _prompt("2026-01-01T00:01:00Z", entrypoint="sdk-cli"),
        ]
        _transcript(tmp_path / "projects", "h1", recs)
        assert _report(tmp_path)["channels"]["headless"]["windows"] == 1
        all_windows = _report(tmp_path, headless_first_window_only=False)
        assert all_windows["channels"]["headless"]["windows"] == 2

    def test_wrapper_only_prompt_falls_back_and_is_counted(self, tmp_path: Path) -> None:
        _seed(tmp_path / "cache", [_push("h1", "2026-01-01T00:00:05Z")])
        wrapper = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "entrypoint": "sdk-cli",
            "message": {"role": "user", "content": "<command-message>x</command-message>"},
        }
        _transcript(tmp_path / "projects", "h1", [wrapper])
        ch = _report(tmp_path)["channels"]["headless"]
        assert ch["windows"] == 1
        assert ch["fallback_prompt_sessions"] == 1


class TestSignals:
    def _one_window(self, tmp_path: Path, tools: list[dict]) -> dict:
        _seed(tmp_path / "cache", [_push("s1", "2026-01-01T00:00:01Z")])
        _transcript(tmp_path / "projects", "s1", [_prompt("2026-01-01T00:00:00Z"), *tools])
        return _report(tmp_path)["channels"]["interactive"]

    def test_native_topic_index_and_bash_reads_are_separated(self, tmp_path: Path) -> None:
        ch = self._one_window(
            tmp_path,
            [
                _tool("2026-01-01T00:00:02Z", "Read", {"file_path": f"{MEM}/topic-a.md"}),
                _tool("2026-01-01T00:00:03Z", "Read", {"file_path": f"{MEM}/MEMORY.md"}),
                _tool("2026-01-01T00:00:04Z", "Grep", {"pattern": "x", "path": MEM}),
                _tool("2026-01-01T00:00:05Z", "Bash", {"command": f"cat {MEM}/topic-b.md"}),
                _tool("2026-01-01T00:00:06Z", "Read", {"file_path": "/repo/src/main.py"}),
            ],
        )
        assert ch["calls"]["native_topic_read"] == 2
        assert ch["calls"]["native_topic_read_builtin"] == 2
        assert ch["calls"]["native_topic_read_other"] == 0
        assert ch["calls"]["native_index_read"] == 1
        assert ch["calls"]["native_bash_read"] == 1
        assert ch["windows_with"]["native_topic_read"] == 1
        assert ch["windows_with"]["athenaeum_pull"] == 0
        assert ch["window_rates"]["native_topic_read"] == 1.0

    def test_plugin_file_tool_path_in_a_string_field(self, tmp_path: Path) -> None:
        # athenaeum#3056: a plugin's file tool reading a topic file is a native read.
        ch = self._one_window(
            tmp_path,
            [_tool("2026-01-01T00:00:02Z", "mcp__plugin_x__Read", {"path": f"/r/{MEM}/t.md"})],
        )
        assert ch["calls"]["native_topic_read"] == 1
        assert ch["calls"]["native_topic_read_other"] == 1
        assert ch["calls"]["native_topic_read_builtin"] == 0
        assert ch["windows_with"]["native_topic_read"] == 1
        assert ch["windows_with"]["native_topic_read_other"] == 1

    def test_plugin_search_tool_path_in_a_glob_list(self, tmp_path: Path) -> None:
        ch = self._one_window(
            tmp_path,
            [
                _tool(
                    "2026-01-01T00:00:02Z",
                    "mcp__plugin_x__Search",
                    {"query": "x", "globs": ["src/**/*.py", "cfg/.claude/projects/*/memory/*.md"]},
                ),
                _tool(
                    "2026-01-01T00:00:03Z",
                    "mcp__plugin_x__Search",
                    {"options": {"paths": [{"root": f"{MEM}/sub"}]}},
                ),
            ],
        )
        assert ch["calls"]["native_topic_read_other"] == 2
        assert ch["calls"]["native_topic_read"] == 2

    def test_absolute_path_with_a_space_in_the_home_dir_counts(self, tmp_path: Path) -> None:
        spaced = "/opt/Shared Docs/.claude/projects/-scope-hash/memory/t.md"
        ch = self._one_window(
            tmp_path,
            [
                _tool("2026-01-01T00:00:02Z", "mcp__p__Read", {"path": spaced}),
                _tool(
                    "2026-01-01T00:00:03Z",
                    "mcp__p__Read",
                    {"path": f"D:\\Shared Docs\\{MEM}/t.md"},
                ),
            ],
        )
        assert ch["calls"]["native_topic_read_other"] == 2

    def test_builtin_and_other_split_and_window_total(self, tmp_path: Path) -> None:
        ch = self._one_window(
            tmp_path,
            [
                _tool("2026-01-01T00:00:02Z", "Read", {"file_path": f"{MEM}/a.md"}),
                _tool("2026-01-01T00:00:03Z", "mcp__p__Search", {"path": f"{MEM}/b.md"}),
            ],
        )
        assert ch["calls"]["native_topic_read"] == 2
        assert ch["calls"]["native_topic_read_builtin"] == 1
        assert ch["calls"]["native_topic_read_other"] == 1
        # One window held both kinds: the total counts it once.
        assert ch["windows_with"]["native_topic_read"] == 1
        assert ch["windows_with"]["native_topic_read_builtin"] == 1
        assert ch["windows_with"]["native_topic_read_other"] == 1

    @pytest.mark.parametrize(
        "name",
        ["Edit", "Write", "MultiEdit", "NotebookEdit", "mcp__plugin_x__Edit", "write_file"],
    )
    def test_write_capable_tool_naming_a_memory_path_is_not_a_read(
        self, tmp_path: Path, name: str
    ) -> None:
        ch = self._one_window(
            tmp_path,
            [_tool("2026-01-01T00:00:02Z", name, {"file_path": f"{MEM}/t.md", "content": "x"})],
        )
        assert ch["calls"]["native_topic_read"] == 0
        assert ch["windows_with"]["native_topic_read"] == 0

    def test_other_tool_naming_only_the_index_or_prose_is_not_a_topic_read(
        self, tmp_path: Path
    ) -> None:
        ch = self._one_window(
            tmp_path,
            [
                _tool("2026-01-01T00:00:02Z", "mcp__p__Read", {"path": f"{MEM}/MEMORY.md"}),
                _tool(
                    "2026-01-01T00:00:03Z",
                    "Agent",
                    {"prompt": f"Look at {MEM}/t.md and summarise it"},
                ),
                _tool("2026-01-01T00:00:04Z", "mcp__p__Read", {"path": "/repo/src/main.py"}),
                _tool("2026-01-01T00:00:05Z", "SendNote", {"body": f"{MEM}/t.md\nsee above"}),
            ],
        )
        assert ch["calls"]["native_topic_read"] == 0
        assert ch["calls"]["native_index_read"] == 0

    def test_read_entity_counts_as_athenaeum_pull(self, tmp_path: Path) -> None:
        ch = self._one_window(
            tmp_path, [_tool("2026-01-01T00:00:02Z", "mcp__athenaeum__read_entity", {"uid": "u"})]
        )
        assert ch["windows_with"]["read_entity_call"] == 1
        assert ch["windows_with"]["athenaeum_pull"] == 1
        assert ch["windows_with"]["recall_call"] == 0

    def test_non_mcp_tool_named_recall_is_ignored(self, tmp_path: Path) -> None:
        ch = self._one_window(tmp_path, [_tool("2026-01-01T00:00:02Z", "recall", {})])
        assert ch["calls"]["recall_call"] == 0

    def test_tool_search_for_recall(self, tmp_path: Path) -> None:
        ch = self._one_window(
            tmp_path,
            [
                _tool(
                    "2026-01-01T00:00:02Z",
                    "ToolSearch",
                    {"query": "select:mcp__athenaeum__recall"},
                ),
                _tool("2026-01-01T00:00:03Z", "ToolSearch", {"query": "slack send"}),
            ],
        )
        assert ch["calls"]["tool_search_for_recall"] == 1

    def test_no_window_rate_without_windows(self, tmp_path: Path) -> None:
        report = _report(tmp_path)
        assert report["channels"]["headless"]["window_rates"]["recall_call"] is None


class TestWriteCapableTool:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Read", False),
            ("Grep", False),
            ("mcp__plugin_x__Search", False),
            ("read_text_file", False),
            ("Edit", True),
            ("NotebookEdit", True),
            ("mcp__fs__create_directory", True),
            ("move-file", True),
            (None, False),
        ],
    )
    def test_names(self, name: object, expected: bool) -> None:
        assert pull_diagnosis.is_write_capable_tool(name) is expected


class TestSessionStates:
    def _states(self, tmp_path: Path, extra: list[dict]) -> dict:
        _seed(tmp_path / "cache", [_push("s1", "2026-01-01T00:00:01Z")])
        _transcript(tmp_path / "projects", "s1", [*extra, _prompt("2026-01-01T00:00:00Z")])
        return _report(tmp_path)["channels"]["interactive"]["session_states"]

    def test_deferred_recall(self, tmp_path: Path) -> None:
        states = self._states(
            tmp_path, [_deferred("2026-01-01T00:00:00Z", ["WebSearch", "mcp__mem__recall"])]
        )
        assert states["recall_deferred"] == 1
        assert states["not_observed"] == 0

    def test_failed_server_of_the_recall_tool(self, tmp_path: Path) -> None:
        states = self._states(
            tmp_path,
            [_deferred("2026-01-01T00:00:00Z", ["mcp__mem__recall"], failed=["mem"])],
        )
        assert states["recall_server_failed"] == 1

    def test_failed_athenaeum_server_without_a_tool_name(self, tmp_path: Path) -> None:
        states = self._states(
            tmp_path, [_deferred("2026-01-01T00:00:00Z", [], failed=["athenaeum"])]
        )
        assert states["recall_server_failed"] == 1
        assert states["recall_deferred"] == 0

    def test_nothing_recorded_is_not_observed(self, tmp_path: Path) -> None:
        states = self._states(tmp_path, [])
        assert states == {
            "recall_deferred": 0,
            "recall_server_failed": 0,
            "recall_called": 0,
            "not_observed": 1,
        }


class TestPrivacy:
    def test_output_holds_no_identifiers(self, tmp_path: Path) -> None:
        _seed(tmp_path / "cache", [_push("secret-session-zz", "2026-01-01T00:00:01Z")])
        _transcript(
            tmp_path / "projects",
            "secret-session-zz",
            [
                _prompt("2026-01-01T00:00:00Z", text="secret prompt text qq"),
                _tool("2026-01-01T00:00:02Z", "Read", {"file_path": f"{MEM}/zqxfile.md"}),
                _tool("2026-01-01T00:00:03Z", "mcp__athenaeum__recall", {"query": "secret q"}),
            ],
        )
        blob = json.dumps(_report(tmp_path))
        for needle in ("secret", "aaa11111", "scope-hash", "cfg/", "zqxfile"):
            assert needle not in blob


class TestCli:
    def test_json_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(tmp_path / "cache", [_push("s1", "2026-01-01T00:00:01Z")])
        _transcript(tmp_path / "projects", "s1", [_prompt("2026-01-01T00:00:00Z")])
        rc = cli.main(
            [
                "push-metrics",
                "pull-diagnosis",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--projects-root",
                str(tmp_path / "projects"),
                "--before",
                "2026-11-15T00:00:00Z",
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["v"] == 1
        assert payload["channels"]["interactive"]["windows"] == 1

    def test_text_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(
            [
                "push-metrics",
                "pull-diagnosis",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--projects-root",
                str(tmp_path / "projects"),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "channel interactive: sessions=0 windows=0" in out
        assert "windows_with recall_call: 0 (rate n/a)" in out

    def test_bad_before_is_a_usage_error(self, tmp_path: Path) -> None:
        rc = cli.main(
            ["push-metrics", "pull-diagnosis", "--cache-dir", str(tmp_path), "--before", "nope"]
        )
        assert rc == 2
