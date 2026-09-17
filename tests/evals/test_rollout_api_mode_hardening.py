# SPDX-License-Identifier: Apache-2.0
"""Hardening tests from the Quine review of issue athenaeum#1733:
symlink-safety, the never-raise tool-executor contract, truncation-wiring
pins, tool-leak/tool_result-shape mutants, path confinement, the grep byte
bound, ``run_probe_all_arms(mode="api")`` end-to-end routing, and per-turn
token accounting across every turn of a multi-turn loop.

Offline, ``rollout``-marked, no network and no subprocess spawn -- same
discipline as ``tests/evals/test_rollout_api_mode.py``, which this module
supplements rather than duplicates.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage
from tests.evals.corpus import build_corpus
from tests.evals.harness import EvalSession
from tests.evals.rollout import (
    NATIVE_INDEX_MAX_BYTES,
    NATIVE_INDEX_MAX_LINES,
    RECALL_TOOL_NAME,
    RolloutRecord,
    _native_read_executor,
    _resolve_under_memory_dir,
    run_api_tool_loop,
    run_native_grep_api,
    run_native_index_api,
    run_probe_all_arms,
    run_pull_api,
    truncate_native_index,
)

pytestmark = pytest.mark.rollout


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(*, id: str, name: str, input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def _usage(input_tokens: int = 10, output_tokens: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


@dataclasses.dataclass
class _RecordedTurn:
    content: list[SimpleNamespace]
    stop_reason: str
    usage: SimpleNamespace = dataclasses.field(default_factory=_usage)


class _QueuedApiClient:
    def __init__(self, turns: list[_RecordedTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

        class _Messages:
            def create(inner_self, **params: Any) -> SimpleNamespace:
                # Snapshot ``messages`` at call time -- the real SDK
                # serializes the request immediately, but the loop's
                # ``messages`` list is the SAME mutable object across turns,
                # so capturing a bare reference would let a LATER turn's
                # append silently rewrite what an EARLIER call "received".
                snapshot = {**params, "messages": list(params.get("messages") or [])}
                self.calls.append(snapshot)
                assert self._turns, "queued client exhausted -- recorded too few turns"
                turn = self._turns.pop(0)
                return SimpleNamespace(
                    content=turn.content, usage=turn.usage, stop_reason=turn.stop_reason
                )

        self.messages = _Messages()


def _pto_probe():
    corpus = build_corpus("core")
    probe = next(p for p in corpus.probes if p.id == "pto_allowance")
    return probe, corpus


# ---------------------------------------------------------------------------
# MUST 2a: symlinked memory dir must not crash the read executor
# ---------------------------------------------------------------------------


def test_native_read_executor_survives_a_symlinked_memory_dir(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "page.md").write_text("hello world", encoding="utf-8")
    symlinked_dir = tmp_path / "link"
    symlinked_dir.symlink_to(real_dir)

    loaded: dict[str, str] = {}
    # Pre-fix, this raised ValueError from an unresolved-vs-resolved
    # relative_to() mismatch (macOS's own /tmp -> /private/tmp is exactly
    # this shape) instead of returning the file's contents.
    result = _native_read_executor(symlinked_dir, {"path": "page.md"}, loaded=loaded)

    assert result == "hello world"
    assert loaded == {"page.md": "hello world"}


# ---------------------------------------------------------------------------
# MUST 2b: run_api_tool_loop never raises on a misbehaving executor
# ---------------------------------------------------------------------------


def test_run_api_tool_loop_survives_a_raising_tool_executor() -> None:
    def _raising_executor(name: str, tool_input: dict[str, Any]) -> str:
        raise RuntimeError("boom")

    turns = [
        _RecordedTurn(
            content=[_tool_use_block(id="t1", name="grep", input={"pattern": "x"})],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("done")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    answer, tool_calls, turn_tokens, turn_count, transcript = run_api_tool_loop(
        user_prompt="q",
        system="test system prompt",
        tools=[],
        tool_executor=_raising_executor,
        client=client,
        session=session,
        model="test-model",
    )

    assert answer == "done"
    assert turn_count == 2
    tool_result_event = transcript[2]
    block = tool_result_event["message"]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "t1"
    assert "boom" in block["content"]
    assert "RuntimeError" in block["content"]


def test_tool_result_carries_tool_use_id_and_is_appended_to_messages() -> None:
    turns = [
        _RecordedTurn(
            content=[_tool_use_block(id="toolu_abc", name="grep", input={"pattern": "x"})],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("done")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    run_api_tool_loop(
        user_prompt="q",
        system="test system prompt",
        tools=[],
        tool_executor=lambda name, ti: "grep result",
        client=client,
        session=session,
        model="test-model",
    )

    # The SECOND client.messages.create call must have received the FULL
    # role sequence in order: the original user prompt, the assistant's
    # tool_use turn appended verbatim, THEN the tool_result -- not just the
    # tail (Quine review, issue athenaeum#1733 SHOULD item 6: a mutant that
    # dropped the assistant append, or appended tool_result before the
    # assistant turn, would still pass a tail-only check).
    second_call_messages = client.calls[1]["messages"]
    assert [m["role"] for m in second_call_messages] == ["user", "assistant", "user"]

    original_user_message = second_call_messages[0]
    assert original_user_message["content"] == "q"

    assistant_message = second_call_messages[1]
    assistant_blocks = assistant_message["content"]
    assert len(assistant_blocks) == 1
    assert assistant_blocks[0]["type"] == "tool_use"
    assert assistant_blocks[0]["id"] == "toolu_abc"
    assert assistant_blocks[0]["name"] == "grep"

    tool_result_message = second_call_messages[2]
    block = tool_result_message["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_abc"
    assert block["content"] == "grep result"


# ---------------------------------------------------------------------------
# MUST 4: truncation wiring
# ---------------------------------------------------------------------------


def test_truncate_native_index_char_cap_binds_before_line_cap_with_long_lines() -> None:
    """Long lines can overflow the 25KB CHARACTER cap well before 200 lines
    accumulate -- the char cap must bind FIRST in that case, not merely be
    checked second and never actually engage. Cut by Python ``str`` length,
    not UTF-8 encoded bytes (Quine review, issue athenaeum#1733) -- ASCII-only
    content here so char count and byte count coincide numerically, but the
    assertions below are written against ``len(str)``, the actual cap this
    module enforces, not a byte-encoding proxy for it."""
    long_line = "- " + ("x" * 300) + "\n"  # ~303 chars/line
    text = long_line * 100  # 100 lines (< 200-line cap), ~30300 chars (> 25KB char cap)
    assert len(text) > NATIVE_INDEX_MAX_BYTES
    assert len(text.splitlines()) < NATIVE_INDEX_MAX_LINES

    truncated = truncate_native_index(text)

    assert len(truncated) <= NATIVE_INDEX_MAX_BYTES
    truncated_lines = truncated.splitlines()
    assert len(truncated_lines) < 100  # the char cap bound it, not the (uncrossed) line cap
    for line in truncated_lines:
        assert line == long_line.rstrip("\n")  # never a partial line


def test_native_index_api_injects_the_truncated_text_as_the_first_user_turn(
    tmp_path: Path,
) -> None:
    """Pins that the model actually RECEIVES the truncated+warning-marked
    text, not the full untruncated MEMORY.md -- detects the regression of
    injecting the untruncated index by mistake."""
    probe, _ = _pto_probe()
    turns = [_RecordedTurn(content=[_text_block("answer")], stop_reason="end_turn")]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    record = run_native_index_api(
        probe, tmp_path, "medium", client=client, session=session, model="test-model"
    )

    native_memory = record.transcript[0]["native_memory"]
    assert native_memory["truncated_by_harness"] is True
    assert native_memory["truncated_by_claude_code"] is False
    assert native_memory["index_lines_loaded"] < native_memory["index_lines_written"]

    sent_first_message = client.calls[0]["messages"][0]["content"]
    injected_index_text = native_memory["loaded_index_text"]
    assert injected_index_text in sent_first_message
    assert "> WARNING:" in injected_index_text
    assert "> WARNING:" in sent_first_message


def test_native_index_api_no_warning_when_the_index_fits_under_the_cap(
    tmp_path: Path,
) -> None:
    """The `core` corpus's index is well under both caps -- truncation must
    NOT engage, and neither must the WARNING marker (Quine review, issue
    athenaeum#1733 SHOULD item 7)."""
    probe, _ = _pto_probe()
    turns = [_RecordedTurn(content=[_text_block("answer")], stop_reason="end_turn")]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    record = run_native_index_api(
        probe, tmp_path, "core", client=client, session=session, model="test-model"
    )

    native_memory = record.transcript[0]["native_memory"]
    assert native_memory["truncated_by_harness"] is False
    assert native_memory["truncated_by_claude_code"] is False
    assert native_memory["index_lines_loaded"] == native_memory["index_lines_written"]
    assert "WARNING:" not in native_memory["loaded_index_text"]

    sent_first_message = client.calls[0]["messages"][0]["content"]
    assert "WARNING:" not in sent_first_message


# ---------------------------------------------------------------------------
# SHOULD 5: tool leak, from_payload back-compat, path confinement, grep bytes,
# run_probe_all_arms(mode="api") end-to-end routing, per-turn token accounting
# ---------------------------------------------------------------------------


def test_pull_api_offers_only_the_recall_tool(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    wiki_root = corpus.materialize(tmp_path)

    turns = [_RecordedTurn(content=[_text_block("answer")], stop_reason="end_turn")]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    run_pull_api(
        probe,
        wiki_root,
        tmp_path / "cache",
        "core",
        client=client,
        session=session,
        search_backend="keyword",
    )

    offered_tool_names = {t["name"] for t in client.calls[0]["tools"]}
    assert offered_tool_names == {RECALL_TOOL_NAME}


def test_native_grep_api_offers_only_grep_and_read_no_recall_leak(tmp_path: Path) -> None:
    probe, _ = _pto_probe()
    turns = [_RecordedTurn(content=[_text_block("answer")], stop_reason="end_turn")]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    run_native_grep_api(probe, tmp_path, "core", client=client, session=session)

    offered_tool_names = {t["name"] for t in client.calls[0]["tools"]}
    assert offered_tool_names == {"grep", "read"}
    assert RECALL_TOOL_NAME not in offered_tool_names


def test_from_payload_defaults_mode_to_cli_when_key_absent() -> None:
    payload = {
        "arm": "pull",
        "probe_id": "pto_allowance",
        "probe_class": "single_hop",
        "corpus_scale": "core",
        "answer": "x",
        # no "mode" key at all -- simulates a row persisted before athenaeum#1733
    }
    record = RolloutRecord.from_payload(payload)
    assert record.mode == "cli"


def test_resolve_under_memory_dir_refuses_a_path_traversal_escape(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")

    result = _resolve_under_memory_dir(memory_dir, "../secret.txt")

    assert result is None


def test_native_grep_executor_bounds_output_by_bytes_not_only_match_count(
    tmp_path: Path,
) -> None:
    from tests.evals.rollout import _NATIVE_GREP_MAX_TOTAL_BYTES, _native_grep_executor

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    # 40 long matching lines: each entry clips to _NATIVE_GREP_MAX_LINE_CHARS
    # (300) so per-entry size is bounded regardless of source line length --
    # 40 such entries (~310 bytes each, ~12.4KB total) is well under the
    # match-COUNT cap (50) but over the 8KB total-bytes cap, so the byte
    # bound must be what stops it here.
    long_line = "x" * 500
    (memory_dir / "page.md").write_text("\n".join([f"match {long_line}"] * 40), encoding="utf-8")

    result = _native_grep_executor(memory_dir, {"pattern": "match"})

    assert len(result.encode("utf-8")) <= _NATIVE_GREP_MAX_TOTAL_BYTES + 200  # + truncation marker
    assert "truncated" in result


def test_run_probe_all_arms_mode_api_routes_every_tool_using_arm_to_api_runners(
    tmp_path: Path,
) -> None:
    """End-to-end (no injected runner stubs): mode="api" alone must be
    enough to route PULL/PUSH_BREADCRUMB_PULL/NATIVE_INDEX/NATIVE_GREP
    through their api runners, each recording mode == "api", using nothing
    but a plain single-response FakeLLMClient (every arm's first turn here
    has no tool_use block, so each tool-using arm's loop stops after one
    turn -- this test is about ROUTING, not multi-turn behavior)."""
    session = EvalSession()
    client = FakeLLMClient(
        response=make_llm_response("ok", usage=make_llm_usage(input_tokens=5, output_tokens=2))
    )

    records = run_probe_all_arms(
        "pto_allowance",
        "core",
        session=session,
        materialize_root=tmp_path,
        search_backend="keyword",
        client=client,
        mode="api",
        breadcrumb_context_fn=lambda *a, **k: "",
    )

    for arm_value in ("pull", "push_breadcrumb_pull", "native_index", "native_grep"):
        assert records[arm_value].mode == "api", f"{arm_value} did not route through an api runner"
    # The single-shot arms are unaffected by *mode* and remain "api" too
    # (they always call the client directly, per RolloutRecord.mode's own
    # docstring).
    for arm_value in ("none", "push_pages_upper_bound", "push_breadcrumb", "oracle"):
        assert records[arm_value].mode == "api"


def test_run_api_tool_loop_accounts_tokens_for_every_turn_not_just_the_last() -> None:
    turns = [
        _RecordedTurn(
            content=[_tool_use_block(id="t1", name="grep", input={"pattern": "x"})],
            stop_reason="tool_use",
            usage=_usage(input_tokens=100, output_tokens=10),
        ),
        _RecordedTurn(
            content=[_tool_use_block(id="t2", name="grep", input={"pattern": "y"})],
            stop_reason="tool_use",
            usage=_usage(input_tokens=200, output_tokens=20),
        ),
        _RecordedTurn(
            content=[_text_block("done")],
            stop_reason="end_turn",
            usage=_usage(input_tokens=300, output_tokens=30),
        ),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    answer, tool_calls, turn_tokens, turn_count, transcript = run_api_tool_loop(
        user_prompt="q",
        system="test system prompt",
        tools=[],
        tool_executor=lambda name, ti: "ok",
        client=client,
        session=session,
        model="test-model",
    )

    assert turn_count == 3
    assert [t.turn for t in turn_tokens] == [1, 2, 3]
    assert [t.input_tokens for t in turn_tokens] == [100, 200, 300]
    assert [t.output_tokens for t in turn_tokens] == [10, 20, 30]
    # The session's running totals reflect ALL three turns, not just the last.
    assert session.input_tokens == 600
    assert session.output_tokens == 60
