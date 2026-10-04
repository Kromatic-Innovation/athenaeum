# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1951 -- rollout-level ACs for the opt-in tool passthrough:
shape parity with the api loop, token totals, an arm end to end on
fixtures, and provenance round-tripping. All fixture-driven: the only
process ever spawned is a fake ``claude`` binary (local IPC, no model, no
spend) or nothing at all (every other test here stubs
``ClaudeCliClient.run_tool_loop`` directly).
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.provider import ClaudeCliClient, CliToolLoopResult
from tests.evals.corpus import build_corpus
from tests.evals.harness import EvalSession
from tests.evals.rollout import (
    READ_ENTITY_TOOL_NAME,
    RECALL_TOOL_NAME,
    Arm,
    RolloutRecord,
    ToolCall,
    TurnTokenUsage,
    run_api_tool_loop,
    run_probe_all_arms,
    run_push_breadcrumb_pull_api,
)
from tests.evals.test_rollout_api_mode import (
    _QueuedApiClient,
    _RecordedTurn,
    _text_block,
    _tool_use_block,
    _usage,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "cli_tool_bridge"


@pytest.fixture
def fake_claude_auto_recall(tmp_path: Path) -> Path:
    body = (FIXTURE_DIR / "fake_claude_auto_recall.py").read_text()
    _shebang, _nl, rest = body.partition("\n")
    script = tmp_path / "fake_claude_auto_recall.py"
    script.write_text(f"#!{sys.executable}\n{rest}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ---------------------------------------------------------------------------
# AC: shape parity + token totals
# ---------------------------------------------------------------------------


def _canned_cli_events() -> list[dict]:
    return [
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "none",
            "mcp_servers": [{"name": "athenaeum", "status": "connected"}],
        },
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": RECALL_TOOL_NAME,
                        "input": {"query": "a"},
                    }
                ],
                "usage": {
                    "input_tokens": 40,
                    "output_tokens": 4,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": READ_ENTITY_TOOL_NAME,
                        "input": {"query": "b"},
                    }
                ],
                "usage": {
                    "input_tokens": 40,
                    "output_tokens": 12,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result a"}
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_2", "content": "result b"}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "msg_2",
                "content": [{"type": "text", "text": "final answer"}],
                "usage": {
                    "input_tokens": 60,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    ]


def test_cli_tool_loop_shape_and_tokens_match_the_api_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    cli_client = ClaudeCliClient(tool_passthrough=True)
    monkeypatch.setattr(
        cli_client,
        "run_tool_loop",
        lambda **_kwargs: CliToolLoopResult(
            events=_canned_cli_events()[1:],  # init event is consumed inside run_tool_loop
            turns_exhausted=False,
            result={
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        ),
    )
    cli_session = EvalSession()
    cli_result = run_api_tool_loop(
        user_prompt="the prompt",
        system="sys",
        tools=[{"name": RECALL_TOOL_NAME}, {"name": READ_ENTITY_TOOL_NAME}],
        tool_executor=lambda name, inp: "unused",
        client=cli_client,
        session=cli_session,
        model="m-1",
    )

    api_turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(id="toolu_1", name=RECALL_TOOL_NAME, input={"query": "a"}),
                _tool_use_block(id="toolu_2", name=READ_ENTITY_TOOL_NAME, input={"query": "b"}),
            ],
            stop_reason="tool_use",
            usage=_usage(input_tokens=40, output_tokens=12),
        ),
        _RecordedTurn(
            content=[_text_block("final answer")],
            stop_reason="end_turn",
            # The REAL api-mode usage is already the true per-message total --
            # no snapshot/topup concept exists on that path, so this is the
            # value the cli-mode topup is designed to converge on: 20 - 12 = 8.
            usage=_usage(input_tokens=60, output_tokens=8),
        ),
    ]
    api_client = _QueuedApiClient(api_turns)
    api_session = EvalSession()

    def _api_executor(name: str, tool_input: dict) -> str:
        return "result a" if name == RECALL_TOOL_NAME else "result b"

    api_result = run_api_tool_loop(
        user_prompt="the prompt",
        system="sys",
        tools=[{"name": RECALL_TOOL_NAME}, {"name": READ_ENTITY_TOOL_NAME}],
        tool_executor=_api_executor,
        client=api_client,
        session=api_session,
        model="m-1",
    )

    cli_answer, cli_tool_calls, cli_turn_tokens, cli_turn_count, cli_transcript = cli_result
    api_answer, api_tool_calls, api_turn_tokens, api_turn_count, api_transcript = api_result

    assert cli_answer == api_answer == "final answer"
    assert (
        cli_tool_calls
        == api_tool_calls
        == [
            ToolCall(name=RECALL_TOOL_NAME, query="a"),
            ToolCall(name=READ_ENTITY_TOOL_NAME, query="b"),
        ]
    )
    assert cli_turn_count == api_turn_count == 2
    assert (
        cli_turn_tokens
        == api_turn_tokens
        == [
            TurnTokenUsage(turn=1, input_tokens=40, output_tokens=12),
            TurnTokenUsage(turn=2, input_tokens=60, output_tokens=8),
        ]
    )
    assert cli_transcript == api_transcript

    # Token totals: per-cell sums equal the respective usage sources, and
    # the EvalSession totals grew by the same amount on both paths.
    assert sum(t.output_tokens for t in cli_turn_tokens) == 20
    assert cli_session.output_tokens == 20
    assert cli_session.input_tokens == 100
    assert sum(t.output_tokens for t in api_turn_tokens) == 20
    assert api_session.output_tokens == 20


# ---------------------------------------------------------------------------
# AC: turn cap through _run_cli_tool_loop / _api_loop_turns_exhausted
# ---------------------------------------------------------------------------


def test_turn_cap_propagates_through_the_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.evals.rollout import _API_LOOP_MAX_TURNS, _api_loop_turns_exhausted

    cli_client = ClaudeCliClient(tool_passthrough=True)
    events = [
        {
            "type": "assistant",
            "message": {
                "id": f"msg_{i}",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu_{i}",
                        "name": RECALL_TOOL_NAME,
                        "input": {"query": "x"},
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
        for i in range(_API_LOOP_MAX_TURNS)
    ]
    # One tool_result per turn, interleaved (stream order).
    interleaved: list[dict] = []
    for i in range(_API_LOOP_MAX_TURNS):
        interleaved.append(events[i])
        interleaved.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": "ok"}
                    ]
                },
            }
        )
    monkeypatch.setattr(
        cli_client,
        "run_tool_loop",
        lambda **_kwargs: CliToolLoopResult(
            events=interleaved,
            turns_exhausted=True,
            result={"type": "result", "subtype": "error_max_turns", "is_error": True},
        ),
    )
    answer, tool_calls, turn_tokens, turn_count, transcript = run_api_tool_loop(
        user_prompt="p",
        system="s",
        tools=[{"name": RECALL_TOOL_NAME}],
        tool_executor=lambda name, inp: "ok",
        client=cli_client,
        session=EvalSession(),
        model="m-1",
    )
    assert turn_count == _API_LOOP_MAX_TURNS
    assert _api_loop_turns_exhausted(turn_count, _API_LOOP_MAX_TURNS, transcript) is True


# ---------------------------------------------------------------------------
# AC: arm end to end on fixtures
# ---------------------------------------------------------------------------


def test_push_breadcrumb_pull_api_end_to_end_over_passthrough_client(
    fake_claude_auto_recall: Path, tmp_path: Path
) -> None:
    corpus = build_corpus("core")
    probe = corpus.probes[0]
    knowledge_root = tmp_path / "knowledge"
    corpus.materialize(knowledge_root)

    client = ClaudeCliClient(binary=str(fake_claude_auto_recall), tool_passthrough=True)
    record = run_push_breadcrumb_pull_api(
        probe,
        knowledge_root,
        tmp_path / "hook-home",
        tmp_path / "cache",
        "core",
        client=client,
        session=EvalSession(),
        model="test-model",
        search_backend="keyword",
        context_fn=lambda *_args: "",
    )
    assert record.recall_called is True
    assert record.mode == "api"
    assert any(c.name == RECALL_TOOL_NAME for c in record.tool_calls)
    assert record.transcript[0] == {"pushed_context": ""}


# ---------------------------------------------------------------------------
# AC: provenance
# ---------------------------------------------------------------------------


def test_llm_provider_round_trips_through_payload() -> None:
    record = RolloutRecord(
        arm=Arm.PULL,
        probe_id="p1",
        probe_class="c",
        corpus_scale="core",
        answer="a",
        llm_provider="claude-cli",
    )
    payload = record.to_payload()
    assert payload["llm_provider"] == "claude-cli"
    decoded = RolloutRecord.from_payload(payload)
    assert decoded.llm_provider == "claude-cli"

    payload_without_key = dict(payload)
    del payload_without_key["llm_provider"]
    decoded_absent = RolloutRecord.from_payload(payload_without_key)
    assert decoded_absent.llm_provider is None


def test_run_probe_all_arms_stamps_llm_provider_by_client_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_probe_all_arms`` stamps ``llm_provider`` from the RESOLVED
    client's type alone -- ``"claude-cli"`` for a :class:`ClaudeCliClient`,
    ``"api"`` for anything else -- on every record it returns, single-shot
    arms included (issue athenaeum#1951)."""
    from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage

    api_client = FakeLLMClient(
        response=make_llm_response("ok", usage=make_llm_usage(input_tokens=5, output_tokens=2))
    )
    api_records = run_probe_all_arms(
        "pto_allowance",
        "core",
        session=EvalSession(),
        materialize_root=tmp_path / "api",
        search_backend="keyword",
        client=api_client,
        mode="api",
        breadcrumb_context_fn=lambda *a, **k: "",
    )
    for record in api_records.values():
        assert record.llm_provider == "api"

    cli_client = ClaudeCliClient(tool_passthrough=True)
    # The four single-shot arms (NONE / PUSH_PAGES_UPPER_BOUND /
    # PUSH_BREADCRUMB / ORACLE) call ``client.messages.create(...)`` --
    # i.e. ``_create`` -- directly, never ``run_tool_loop``. Stub the
    # subprocess boundary the same way ``tests/test_provider.py`` does, so
    # this test exercises the stamping logic without needing a real
    # ``claude`` binary on PATH (absent in CI).
    monkeypatch.setattr("athenaeum.provider.shutil.which", lambda _b: "/usr/bin/claude")
    monkeypatch.setattr(
        "athenaeum.provider.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"subtype": "success", "is_error": False, "result": "ok", "usage": {}}
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        cli_client,
        "run_tool_loop",
        lambda **_kwargs: CliToolLoopResult(
            events=[
                {
                    "type": "assistant",
                    "message": {
                        "id": "msg_1",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {
                            "input_tokens": 5,
                            "output_tokens": 2,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                }
            ],
            turns_exhausted=False,
            result={
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "usage": {"output_tokens": 2},
            },
        ),
    )
    cli_records = run_probe_all_arms(
        "pto_allowance",
        "core",
        session=EvalSession(),
        materialize_root=tmp_path / "cli",
        search_backend="keyword",
        client=cli_client,
        mode="api",
        breadcrumb_context_fn=lambda *a, **k: "",
    )
    for record in cli_records.values():
        assert record.llm_provider == "claude-cli"
