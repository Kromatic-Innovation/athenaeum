# SPDX-License-Identifier: Apache-2.0
"""Recorder / replay round-trip tests for the eval harness (issue #684).

The pre-#684 recorder read ``response.content[0].text`` and silently wrote
``response_text=""`` when the first block was not a text block — which is
exactly what a thinking-enabled stage (the resolver, wired to adaptive
thinking by #578) returns: one or more leading ``ThinkingBlock`` objects with
no ``.text`` before the text block. 6 of 8 resolver fixtures recorded empty and
passed every check until replay.

These tests pin the fix WITHOUT the live API:

* the recorder extracts through the ONE shared walker
  (``provider.response_text``) and records the block SEQUENCE;
* a response with no usable text RAISES instead of writing an empty fixture;
* the replay stub reconstructs the recorded block sequence, so a
  thinking-prefixed response is actually exercised — the shape a synthetic
  single text block could never reproduce.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from athenaeum.provider import response_text as provider_response_text
from tests.evals import harness
from tests.evals.harness import (
    LAYER_RESOLVER,
    EmptyRecordingError,
    RecordedResponse,
    RecordingClient,
    recorded_path,
    replay_client,
    save_recorded,
)

# A thinking-enabled stage's response begins with a ThinkingBlock (no .text)
# before the text block. SimpleNamespace faithfully models that: accessing
# ``.text`` on the thinking block raises AttributeError, exactly like the SDK.
_THINKING_BLOCK = SimpleNamespace(type="thinking")
_TEXT_BLOCK = SimpleNamespace(type="text", text='{"decision": "merge"}')


def _response(*blocks: Any, output_tokens: int = 229) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        usage=SimpleNamespace(
            input_tokens=305,
            output_tokens=output_tokens,
            cache_read_input_tokens=4390,
        ),
    )


class _FakeInner:
    """A minimal Anthropic-client double: ``.messages.create`` returns a canned
    response regardless of params (the harness passes params through)."""

    def __init__(self, response: Any) -> None:
        self.messages = SimpleNamespace(create=lambda **params: response)


@pytest.fixture(autouse=True)
def _isolate_recorded_root(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect fixture writes to a tmp dir so tests never touch the committed
    ``tests/fixtures/recorded/`` tree."""
    monkeypatch.setattr(harness, "RECORDED_ROOT", tmp_path / "recorded")


def _record_one(response: Any, case_id: str = "case-1") -> None:
    client = RecordingClient(_FakeInner(response), record=True, layer=LAYER_RESOLVER)
    client.start_case(case_id)
    client.messages.create(
        model="claude-opus-5",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
    )


class TestRecorder:
    def test_records_text_block_of_thinking_prefixed_response(self) -> None:
        # AC: a [thinking_block, text_block] response records the TEXT block's
        # content (not the leading thinking block), pinned.
        _record_one(_response(_THINKING_BLOCK, _TEXT_BLOCK))
        path = recorded_path(LAYER_RESOLVER, "case-1")
        assert path.is_file()
        payload = json.loads(path.read_text())
        assert payload["response_text"] == '{"decision": "merge"}'
        # The block SEQUENCE is recorded, thinking block first, so replay can
        # reconstruct the real shape.
        assert payload["content_blocks"] == [
            {"type": "thinking"},
            {"type": "text", "text": '{"decision": "merge"}'},
        ]

    def test_no_text_block_raises_and_writes_nothing(self) -> None:
        # AC: a response with NO text block raises rather than writing an empty
        # fixture — the actual defect behind #684.
        with pytest.raises(EmptyRecordingError):
            _record_one(_response(_THINKING_BLOCK))
        assert not recorded_path(LAYER_RESOLVER, "case-1").exists()

    def test_empty_content_raises_and_writes_nothing(self) -> None:
        with pytest.raises(EmptyRecordingError):
            _record_one(_response())
        assert not recorded_path(LAYER_RESOLVER, "case-1").exists()

    def test_empty_text_raises_and_writes_nothing(self) -> None:
        # A text block whose text is blank is still a failed recording.
        blank = SimpleNamespace(type="text", text="   ")
        with pytest.raises(EmptyRecordingError):
            _record_one(_response(_THINKING_BLOCK, blank))
        assert not recorded_path(LAYER_RESOLVER, "case-1").exists()

    def test_recording_disabled_writes_nothing(self) -> None:
        client = RecordingClient(
            _FakeInner(_response(_TEXT_BLOCK)), record=False, layer=LAYER_RESOLVER
        )
        client.start_case("case-1")
        client.messages.create(model="m", system="s", messages=[])
        assert not recorded_path(LAYER_RESOLVER, "case-1").exists()


class TestReplayReconstructsBlocks:
    def test_replay_round_trips_thinking_prefixed_sequence(self) -> None:
        # AC: a replay fixture round-trips a thinking-block-prefixed response and
        # the replay client exercises it — the case that was unreachable before.
        _record_one(_response(_THINKING_BLOCK, _TEXT_BLOCK))
        client = replay_client(LAYER_RESOLVER, "case-1")
        response = client.messages.create(
            model="claude-opus-5",
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
        )
        # The reconstructed content is the real shape: a leading thinking block
        # with NO .text, then the text block.
        assert response.content[0].type == "thinking"
        assert not hasattr(response.content[0], "text")
        assert response.content[1].type == "text"
        # The SAME walker production uses extracts the text, skipping thinking.
        assert provider_response_text(response) == '{"decision": "merge"}'

    def test_from_json_backward_compat_synthesizes_single_text_block(self) -> None:
        # A pre-#684 fixture carried only response_text; from_json synthesises a
        # single text block so replay still reconstructs a faithful response.
        legacy = {
            "case_id": "c",
            "layer": LAYER_RESOLVER,
            "model": "m",
            "prompt_hash": "h",
            "response_text": "legacy answer",
            "usage": {},
            "recorded_at": "",
        }
        rec = RecordedResponse.from_json(legacy)
        assert rec.content_blocks == [{"type": "text", "text": "legacy answer"}]

    def test_recorded_response_json_round_trips_content_blocks(self) -> None:
        rec = RecordedResponse(
            case_id="c",
            layer=LAYER_RESOLVER,
            model="m",
            prompt_hash="h",
            response_text="answer",
            usage={"output_tokens": 5},
            recorded_at="",
            content_blocks=[{"type": "thinking"}, {"type": "text", "text": "answer"}],
        )
        save_recorded(rec)
        loaded = RecordedResponse.from_json(
            json.loads(recorded_path(LAYER_RESOLVER, "c").read_text())
        )
        assert loaded.content_blocks == rec.content_blocks
        assert loaded.response_text == "answer"
