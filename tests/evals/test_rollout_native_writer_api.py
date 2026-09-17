# SPDX-License-Identifier: Apache-2.0
"""Offline coverage for the api-mode native writer runner (issue
athenaeum#1774).

Mirrors ``tests/evals/test_rollout_api_mode.py``'s queued-stub-client
pattern (a scripted sequence of Anthropic Messages API responses replayed
through ``_QueuedApiClient``) -- no network call, no subprocess spawn, no
token spent, so this file carries no ``rollout``/``eval`` marker, the same
"unmarked" discipline that file documents (issue athenaeum#1742). Live-model
fidelity against Claude Code's REAL auto-memory writer stays with the CLI
spot-check (``tests/evals/test_rollout_native_writer_spike.py``,
``pytest.mark.rollout``) -- these tests check the api-mode runner's
plumbing only: which tools it serves, that every one of them is confined to
the memory directory, and that the record it returns carries the same
shape :func:`tests.evals.rollout.run_native_writer` already produces.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from tests.evals.corpus import Observation
from tests.evals.harness import EvalSession
from tests.evals.rollout import (
    _WRITER_API_LOOP_MAX_TURNS,
    EDIT_TOOL_NAME,
    GREP_TOOL_NAME,
    LIST_TOOL_NAME,
    NATIVE_INDEX_MAX_CHARS,
    NATIVE_INDEX_MAX_LINES,
    READ_TOOL_NAME,
    WRITE_TOOL_NAME,
    NativeWriterResult,
    _native_writer_system_prompt,
    run_native_writer,
    run_native_writer_api,
    run_native_writer_dispatch,
)
from tests.evals.test_rollout_api_mode import (
    _QueuedApiClient,
    _RecordedTurn,
    _text_block,
    _tool_use_block,
)


def _obs(i: int, body: str = "") -> Observation:
    return Observation(
        uid=f"obs-{i:03d}",
        page_uid="page-x",
        source="sessions",
        timestamp=f"2026010{i}T000000Z",
        uuid8=f"aaaaaa{i:02d}",
        body=body or f"note number {i}",
    )


# ---------------------------------------------------------------------------
# 1. A write tool call lands a file inside the memory directory
# ---------------------------------------------------------------------------


def test_write_tool_call_lands_file_inside_memory_dir(tmp_path: Path) -> None:
    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_write_1",
                    name=WRITE_TOOL_NAME,
                    input={"path": "policy-notes.md", "content": "PTO allowance is 25 days."},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("noted.")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    result = run_native_writer_api(
        [_obs(1, "PTO note")], tmp_path, client=client, session=session, model="test-model"
    )

    assert result.mode == "api"
    written = result.memory_dir / "policy-notes.md"
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "PTO allowance is 25 days."
    assert result.memory_files == {"policy-notes.md": "PTO allowance is 25 days."}
    assert [c.name for c in result.sessions[0].tool_calls] == [WRITE_TOOL_NAME]
    # The system prompt actually mentions the memory directory (Quine-style
    # check, matching test_rollout_api_mode.py's own native-arm assertion).
    sent_system = client.calls[0]["system"]
    assert str(tmp_path) in sent_system
    # The write tools are actually offered, not merely the read pair.
    sent_tool_names = {t["name"] for t in client.calls[0]["tools"]}
    assert sent_tool_names == {
        READ_TOOL_NAME,
        GREP_TOOL_NAME,
        WRITE_TOOL_NAME,
        EDIT_TOOL_NAME,
        LIST_TOOL_NAME,
    }


# ---------------------------------------------------------------------------
# 2. An escape attempt is refused, not silently written elsewhere
# ---------------------------------------------------------------------------


def test_write_escape_attempt_is_refused(tmp_path: Path) -> None:
    outside_target = tmp_path / "outside.md"
    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_write_1",
                    name=WRITE_TOOL_NAME,
                    input={"path": "../outside.md", "content": "should never land here"},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("done.")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    result = run_native_writer_api(
        [_obs(1)], tmp_path, client=client, session=session, model="test-model"
    )

    assert not outside_target.exists()
    assert result.memory_files == {}
    # transcript[0] is the initial user prompt, transcript[1] the assistant
    # turn carrying the tool_use block, transcript[2] the tool_result reply.
    tool_result = result.sessions[0].transcript[2]["message"]["content"][0]
    assert "error" in str(tool_result["content"]).lower()
    assert "outside the memory directory" in str(tool_result["content"])


def test_edit_escape_attempt_is_refused(tmp_path: Path) -> None:
    """Same confinement, for the `edit` tool -- an absolute path elsewhere
    on disk must be refused exactly like a `..` traversal is."""
    escape_target = tmp_path.parent / "escape-target.md"
    escape_target.write_text("pre-existing content", encoding="utf-8")
    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_edit_1",
                    name=EDIT_TOOL_NAME,
                    input={
                        "path": str(escape_target),
                        "old_string": "pre-existing",
                        "new_string": "tampered",
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("done.")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    run_native_writer_api([_obs(1)], tmp_path, client=client, session=session, model="test-model")

    assert escape_target.read_text(encoding="utf-8") == "pre-existing content"


# ---------------------------------------------------------------------------
# 3. The record carries mode "api" and token counts
# ---------------------------------------------------------------------------


def test_record_carries_mode_api_and_token_counts(tmp_path: Path) -> None:
    turns = [
        _RecordedTurn(content=[_text_block("nothing worth saving.")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    result = run_native_writer_api(
        [_obs(1)], tmp_path, client=client, session=session, model="test-model"
    )

    assert result.mode == "api"
    assert len(result.sessions) == 1
    assert result.sessions[0].turn_tokens
    turn_usage = result.sessions[0].turn_tokens[0]
    assert turn_usage.input_tokens > 0
    assert result.total_tool_calls == 0


# ---------------------------------------------------------------------------
# 4. Multiple sessions share the same fresh memory directory
# ---------------------------------------------------------------------------


def test_multiple_sessions_share_one_memory_directory(tmp_path: Path) -> None:
    turns = [
        _RecordedTurn(content=[_text_block("noted 1.")], stop_reason="end_turn"),
        _RecordedTurn(content=[_text_block("noted 2.")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    result = run_native_writer_api(
        [_obs(1), _obs(2)], tmp_path, client=client, session=session, model="test-model"
    )

    assert [s.observation_uid for s in result.sessions] == ["obs-001", "obs-002"]
    assert result.memory_dir == tmp_path / "memory"


# ---------------------------------------------------------------------------
# 5. No REFERENCE_TAG_INSTRUCTION in the writer system prompt
# ---------------------------------------------------------------------------


def test_no_reference_tag_instruction_in_writer_prompt(tmp_path: Path) -> None:
    from tests.evals.rollout import REFERENCE_TAG_INSTRUCTION

    turns = [_RecordedTurn(content=[_text_block("noted.")], stop_reason="end_turn")]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    run_native_writer_api([_obs(1)], tmp_path, client=client, session=session, model="test-model")

    sent_system = client.calls[0]["system"]
    assert REFERENCE_TAG_INSTRUCTION not in sent_system


# ---------------------------------------------------------------------------
# 6. A payload round-trip: the record is JSON-safe, same as every other
#    RolloutRecord this module produces, even though NativeWriterResult
#    itself carries no to_payload/from_payload pair (unlike RolloutRecord --
#    issue athenaeum#1774's own AC leaves compute_write_path_stats
#    unchanged, and nothing in this module persists a NativeWriterResult
#    yet; that is items N/O's job). This pins the WEAKER, currently-true
#    property: every field a future persistence layer would serialize
#    already round-trips through plain JSON without loss.
# ---------------------------------------------------------------------------


def test_native_writer_result_is_json_round_trip_safe(tmp_path: Path) -> None:
    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_write_1",
                    name=WRITE_TOOL_NAME,
                    input={"path": "note.md", "content": "kept fact"},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("noted.")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    result = run_native_writer_api(
        [_obs(1)], tmp_path, client=client, session=session, model="test-model"
    )

    payload: dict[str, Any] = {
        "mode": result.mode,
        "memory_dir": str(result.memory_dir),
        "memory_files": result.memory_files,
        "sessions": [
            {
                "session_index": s.session_index,
                "observation_uid": s.observation_uid,
                "tool_calls": [dataclasses.asdict(c) for c in s.tool_calls],
                "turn_tokens": [dataclasses.asdict(t) for t in s.turn_tokens],
                "turn_count": s.turn_count,
                "transcript": s.transcript,
            }
            for s in result.sessions
        ],
    }
    round_tripped = json.loads(json.dumps(payload))

    assert round_tripped["mode"] == "api"
    assert round_tripped["memory_files"] == {"note.md": "kept fact"}
    assert round_tripped["sessions"][0]["observation_uid"] == "obs-001"
    assert round_tripped["sessions"][0]["tool_calls"][0]["name"] == WRITE_TOOL_NAME


# ---------------------------------------------------------------------------
# 7. Existing CLI-mode tests unchanged, and the dispatch seam picks the
#    right path for each mode.
# ---------------------------------------------------------------------------


def test_dispatch_picks_api_mode_and_requires_client_and_session(tmp_path: Path) -> None:
    turns = [_RecordedTurn(content=[_text_block("noted.")], stop_reason="end_turn")]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    result = run_native_writer_dispatch(
        [_obs(1)], tmp_path, mode="api", client=client, session=session, model="test-model"
    )

    assert isinstance(result, NativeWriterResult)
    assert result.mode == "api"


def test_dispatch_api_mode_without_client_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="requires both client and session"):
        run_native_writer_dispatch([_obs(1)], Path("/tmp/does-not-matter"), mode="api")


def test_dispatch_cli_mode_calls_run_native_writer_unchanged(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    from types import SimpleNamespace

    monkeypatch.setattr("tests.evals.rollout.shutil.which", lambda _binary: "/usr/bin/claude")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(argv)
        events = [
            {"type": "system", "subtype": "init", "mcp_servers": [], "tools": []},
            {
                "type": "assistant",
                "message": {"usage": {"input_tokens": 1, "output_tokens": 1}, "content": []},
            },
            {"type": "result", "result": ""},
        ]
        return SimpleNamespace(stdout="\n".join(_json.dumps(e) for e in events), stderr="")

    monkeypatch.setattr("tests.evals.rollout.subprocess.run", fake_run)

    result = run_native_writer_dispatch([_obs(1)], tmp_path, mode="cli", claude_binary="claude")

    assert result.mode == "cli"
    assert len(calls) == 1


def test_run_native_writer_cli_mode_still_sets_mode_field(tmp_path: Path, monkeypatch) -> None:
    """Issue athenaeum#1774 added `NativeWriterResult.mode` -- pin that the
    pre-existing CLI runner sets it to "cli", never left at some other
    default, so a reader of a mixed set of results can always tell them
    apart the same way `RolloutRecord.mode` already lets a reader do."""
    import json as _json
    from types import SimpleNamespace

    monkeypatch.setattr("tests.evals.rollout.shutil.which", lambda _binary: "/usr/bin/claude")

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        events = [
            {"type": "system", "subtype": "init", "mcp_servers": [], "tools": []},
            {
                "type": "assistant",
                "message": {"usage": {"input_tokens": 1, "output_tokens": 1}, "content": []},
            },
            {"type": "result", "result": ""},
        ]
        return SimpleNamespace(stdout="\n".join(_json.dumps(e) for e in events), stderr="")

    monkeypatch.setattr("tests.evals.rollout.subprocess.run", fake_run)

    result = run_native_writer([_obs(1)], tmp_path)

    assert result.mode == "cli"


# ---------------------------------------------------------------------------
# 8. The prompt states the real 200-line/25KB load cap (Quine review, Must 2)
# ---------------------------------------------------------------------------


def test_writer_system_prompt_states_the_real_index_load_cap(tmp_path: Path) -> None:
    """docs/design/native-memory-baseline.md §2: only the first 200 lines
    or 25KB of MEMORY.md are loaded, nothing past that -- the prompt must
    say so exactly, not claim the index is "loaded in full", so the model
    has the real incentive to keep important lines near the top."""
    prompt = _native_writer_system_prompt(tmp_path / "memory")

    assert str(NATIVE_INDEX_MAX_LINES) in prompt
    assert str(NATIVE_INDEX_MAX_CHARS) in prompt
    assert "loaded in full" not in prompt
    assert "near the top" in prompt


# ---------------------------------------------------------------------------
# 9. Contract test: CLI-mode and api-mode NativeWriterResult have the same
#    fields populated on the same fixture stream, differing only in mode
#    and prompt_fidelity (issue athenaeum#1774 AC, Should 1).
# ---------------------------------------------------------------------------


def test_cli_and_api_writer_results_have_the_same_fields_populated(
    tmp_path: Path, monkeypatch
) -> None:
    import json as _json
    from types import SimpleNamespace

    observations = [_obs(1, "first fact"), _obs(2, "second fact")]

    # -- CLI mode: stub subprocess.run, reusing the existing CLI stub shape
    # (tests/evals/test_rollout_native_writer.py's own _fake_stdout). --
    cli_root = tmp_path / "cli"
    monkeypatch.setattr("tests.evals.rollout.shutil.which", lambda _binary: "/usr/bin/claude")

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        # Stand in for the model's own Write tool call landing a file on
        # disk under the auto-memory directory this runner prepared -- same
        # shape as test_rollout_native_writer.py's own
        # test_memory_files_reads_back_whatever_the_model_actually_wrote.
        (cli_root / "memory").mkdir(parents=True, exist_ok=True)
        (cli_root / "memory" / "note.md").write_text("kept fact", encoding="utf-8")
        events = [
            {"type": "system", "subtype": "init", "mcp_servers": [], "tools": []},
            {
                "type": "assistant",
                "message": {
                    "usage": {"input_tokens": 12, "output_tokens": 4},
                    "content": [
                        {"type": "tool_use", "name": "Write", "input": {"file_path": "note.md"}},
                        {"type": "text", "text": "noted."},
                    ],
                },
            },
            {"type": "result", "result": "noted."},
        ]
        return SimpleNamespace(stdout="\n".join(_json.dumps(e) for e in events), stderr="")

    monkeypatch.setattr("tests.evals.rollout.subprocess.run", fake_run)
    cli_result = run_native_writer(observations, cli_root, claude_binary="claude")

    # -- api mode: stub client, one write tool call per session. --
    api_root = tmp_path / "api"
    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_write_1",
                    name=WRITE_TOOL_NAME,
                    input={"path": "note.md", "content": "kept fact"},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("noted.")], stop_reason="end_turn"),
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_write_2",
                    name=WRITE_TOOL_NAME,
                    input={"path": "note.md", "content": "kept fact, updated"},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("noted.")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()
    api_result = run_native_writer_api(
        observations, api_root, client=client, session=session, model="test-model"
    )

    # Same fields populated, same shape.
    assert [s.observation_uid for s in cli_result.sessions] == [
        s.observation_uid for s in api_result.sessions
    ]
    assert len(cli_result.sessions) == len(api_result.sessions) == 2
    for cli_session, api_session in zip(cli_result.sessions, api_result.sessions, strict=True):
        assert len(cli_session.tool_calls) == len(api_session.tool_calls) == 1
        assert cli_session.turn_tokens and api_session.turn_tokens
        assert cli_session.turn_count > 0
        assert api_session.turn_count > 0
        assert cli_session.transcript
        assert api_session.transcript
        # turns_exhausted is meaningful (and defaults False) on both --
        # neither fixture here exhausts the budget.
        assert cli_session.turns_exhausted is False
        assert api_session.turns_exhausted is False
    assert cli_result.memory_files and api_result.memory_files

    # Differ ONLY in mode and prompt_fidelity.
    assert cli_result.mode == "cli"
    assert api_result.mode == "api"
    assert cli_result.prompt_fidelity is None
    assert api_result.prompt_fidelity == "reconstructed"


# ---------------------------------------------------------------------------
# 10. turns_exhausted is set when the writer's own (larger) turn budget is
#     cut off mid-tool-use (issue athenaeum#1774 Quine review, Should 2).
# ---------------------------------------------------------------------------


def test_turns_exhausted_set_when_stub_outlasts_the_writer_budget(tmp_path: Path) -> None:
    # One more tool-use turn than the writer's budget allows, so the loop
    # is cut off with a pending tool call still in flight.
    turns = [
        _RecordedTurn(
            content=[_tool_use_block(id=f"toolu_list_{i}", name=LIST_TOOL_NAME, input={})],
            stop_reason="tool_use",
        )
        for i in range(_WRITER_API_LOOP_MAX_TURNS + 2)
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    result = run_native_writer_api(
        [_obs(1)], tmp_path, client=client, session=session, model="test-model"
    )

    assert result.sessions[0].turn_count == _WRITER_API_LOOP_MAX_TURNS
    assert result.sessions[0].turns_exhausted is True


def test_turns_exhausted_false_when_the_model_finishes_within_budget(tmp_path: Path) -> None:
    turns = [
        _RecordedTurn(
            content=[_tool_use_block(id="toolu_list_1", name=LIST_TOOL_NAME, input={})],
            stop_reason="tool_use",
        ),
        _RecordedTurn(content=[_text_block("nothing worth saving.")], stop_reason="end_turn"),
    ]
    client = _QueuedApiClient(turns)
    session = EvalSession()

    result = run_native_writer_api(
        [_obs(1)], tmp_path, client=client, session=session, model="test-model"
    )

    assert result.sessions[0].turns_exhausted is False
