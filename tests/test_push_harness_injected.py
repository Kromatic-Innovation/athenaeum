# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#2914: no push on harness-injected pseudo-prompts.

The Claude Code adapter supplies the patterns; the engine
(``athenaeum.context``) owns the skip. Fixtures are invented.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum import push_metrics
from athenaeum.claude_code_adapter import _INJECTED_TEXT_PATTERNS
from athenaeum.context import (
    SKIP_REASON_HARNESS_INJECTED,
    build_context_for_turn,
    split_injected_text,
)

SRC = str(Path(__file__).resolve().parent.parent / "src")

TASK_NOTIFICATION = (
    "<task-notification>\n<task-id>bg-42</task-id>\n<status>completed</status>\n"
    "<summary>Background command finished: zebrafish migration</summary>\n"
    "</task-notification>"
)
AGENT_MESSAGE = (
    '<teammate-message teammate_id="reviewer-1" summary="zebrafish findings">\n'
    "The zebrafish migration looks fine to me.\n</teammate-message>"
)
SYSTEM_REMINDER = "<system-reminder>\nThe zebrafish tool was used recently.\n</system-reminder>"


def _build_index(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5(filename, name, tags, aliases, "
        "description, audience UNINDEXED, type UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    conn.executemany(
        "INSERT INTO wiki (filename, name, tags, aliases, description, audience, type) "
        "VALUES (?, ?, ?, '', ?, '|__access_open__|', 'reference')",
        [
            ("aaaa1111-zebrafish.md", "Zebrafish Migration", "zebrafish", "zebrafish notes"),
            ("bbbb2222-quokka.md", "Quokka Handbook", "quokka", "quokka care notes"),
        ],
    )
    conn.commit()
    conn.close()


def _turn(tmp_path: Path, prompt: str, **kw):  # type: ignore[no-untyped-def]
    return build_context_for_turn(
        prompt,
        "sess-x",
        cache_dir=tmp_path,
        search_backend="fts5",
        use_llm=False,
        injected_patterns=_INJECTED_TEXT_PATTERNS,
        **kw,
    )


@pytest.fixture()
def cache(tmp_path: Path) -> Path:
    _build_index(tmp_path / "wiki-index.db")
    return tmp_path


@pytest.mark.parametrize(
    "prompt",
    [
        TASK_NOTIFICATION,
        AGENT_MESSAGE,
        SYSTEM_REMINDER,
        TASK_NOTIFICATION + "\n" + SYSTEM_REMINDER,
    ],
    ids=["task-notification", "agent-message", "system-reminder-only", "stacked"],
)
def test_pseudo_prompt_skips_push_and_writes_one_skip_row(cache: Path, prompt: str) -> None:
    env = _turn(cache, prompt)
    assert env["candidates"] == []
    assert env["render"]["text"] == ""
    rows = push_metrics._read_jsonl(push_metrics.push_records_path(cache))
    assert len(rows) == 1
    assert rows[0]["skip_reason"] == SKIP_REASON_HARNESS_INJECTED
    assert rows[0]["pushed_count"] == 0
    assert rows[0]["items"] == []
    assert not (cache / "context-seen-sess-x.txt").exists()


def test_mixed_submission_queries_user_text_only(cache: Path) -> None:
    env = _turn(cache, SYSTEM_REMINDER + "\n" + TASK_NOTIFICATION + "\nquokka handbook please")
    names = [c["filename"] for c in env["candidates"]]
    assert names == ["bbbb2222-quokka.md"]  # zebrafish only appears in injected text
    assert env["query"] == "quokka handbook please"
    rows = push_metrics.read_push_records(cache)
    assert len(rows) == 1 and "skip_reason" not in rows[0]


def test_normal_prompt_unaffected(cache: Path) -> None:
    env = _turn(cache, "tell me about the zebrafish migration")
    assert [c["filename"] for c in env["candidates"]] == ["aaaa1111-zebrafish.md"]
    assert env["query"] == "tell me about the zebrafish migration"
    assert push_metrics.read_skip_records(cache) == []
    assert len(push_metrics.read_push_records(cache)) == 1


def test_literal_unclosed_tag_in_user_text_is_not_swallowed(cache: Path) -> None:
    prompt = "what does <system-reminder> mean for zebrafish"
    env = _turn(cache, prompt)
    assert env["query"] == prompt
    assert push_metrics.read_skip_records(cache) == []


def test_no_patterns_means_no_behaviour_change(cache: Path) -> None:
    env = build_context_for_turn(
        TASK_NOTIFICATION,
        "sess-x",
        cache_dir=cache,
        search_backend="fts5",
        use_llm=False,
    )
    assert env["candidates"]  # pre-change behaviour: pushed on the notice text
    assert push_metrics.read_skip_records(cache) == []


def test_split_helper_ignores_malformed_pattern() -> None:
    assert split_injected_text("hello world", ["("]) == ("hello world", False)


def test_skip_rows_do_not_leak_into_push_readers(cache: Path) -> None:
    _turn(cache, TASK_NOTIFICATION)
    _turn(cache, "tell me about the zebrafish migration")
    assert len(push_metrics.read_push_records(cache)) == 1
    assert len(push_metrics.read_push_records(cache, include_skips=True)) == 2
    assert len(push_metrics.read_skip_records(cache)) == 1
    baseline = push_metrics.compute_baseline(cache_dir=cache)
    assert baseline.push_record_count == 1


def test_ledger_without_skip_field_reads_cleanly(cache: Path) -> None:
    old = {"v": 1, "session_id": "s", "ts": "2026-01-01T00:00:00Z", "query_hash": "x",
           "backend": "fts5", "items": [], "pushed_count": 0}
    path = push_metrics.push_records_path(cache)
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    assert push_metrics.read_push_records(cache) == [old]


def test_adapter_end_to_end_pseudo_prompt(cache: Path) -> None:
    payload = json.dumps({"prompt": TASK_NOTIFICATION, "session_id": "sess-e2e"})
    r = subprocess.run(
        [sys.executable, "-m", "athenaeum.claude_code_adapter"],
        input=payload,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": SRC, "PATH": "/usr/bin:/bin", "ATHENAEUM_CACHE_DIR": str(cache)},
        timeout=60,
    )
    assert r.returncode == 0
    assert r.stdout == ""
    rows = push_metrics._read_jsonl(push_metrics.push_records_path(cache))
    assert [x.get("skip_reason") for x in rows] == [SKIP_REASON_HARNESS_INJECTED]
