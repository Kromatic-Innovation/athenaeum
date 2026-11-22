# SPDX-License-Identifier: Apache-2.0
"""Tests for carrying the previous turn into per-turn topic extraction
(issue athenaeum#3105, built from the athenaeum#3104 measurement's arm A2).

Covers the transcript reader (:mod:`athenaeum.turn_context`), the extractor
input (:func:`athenaeum.query_topics.extract_topics`), the switch
(:func:`athenaeum.config.resolve_push_context_carryover`) and the hook
(:mod:`athenaeum.claude_code_adapter`) end to end.
"""

from __future__ import annotations

import io
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from athenaeum import query_topics, turn_context
from athenaeum.config import resolve_push_context_carryover
from athenaeum.turn_context import (
    PREVIOUS_TURN_CHARS,
    TAIL_BYTES,
    PreviousTurn,
    previous_turn_for_hook,
    read_previous_turn,
)
from tests.conftest import FakeLLMClient
from tests.test_claude_code_adapter import _build_index, _run_adapter

# ---------------------------------------------------------------------------
# Transcript fixtures (the record shapes Claude Code writes)
# ---------------------------------------------------------------------------


def _user(text: str, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "origin": {"kind": "human"},
    }
    record.update(extra)
    return record


def _assistant(*texts: str, tool_use: bool = False, **extra: Any) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": t} for t in texts]
    if tool_use:
        content.append({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}})
    record: dict[str, Any] = {
        "type": "assistant",
        "message": {"role": "assistant", "content": content},
    }
    record.update(extra)
    return record


def _tool_result(text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}],
        },
    }


def _write_transcript(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# read_previous_turn
# ---------------------------------------------------------------------------


class TestReadPreviousTurn:
    def test_reads_previous_user_message_and_assistant_reply(self, tmp_path: Path) -> None:
        path = _write_transcript(
            tmp_path / "s.jsonl",
            [
                _user("Tell me about the Return Path engagement"),
                _assistant("Looking it up.", tool_use=True),
                _tool_result("raw file contents nobody typed"),
                _assistant("Return Path was a consulting client."),
            ],
        )
        turn = read_previous_turn(path, "close it")
        assert turn == PreviousTurn(
            user_message="Tell me about the Return Path engagement",
            assistant_reply="Looking it up.\nReturn Path was a consulting client.",
        )

    def test_skips_the_current_prompt_when_already_written(self, tmp_path: Path) -> None:
        """If the transcript already holds the new prompt (with no reply yet),
        the previous turn is the one before it, not the prompt itself."""
        path = _write_transcript(
            tmp_path / "s.jsonl",
            [
                _user("Tell me about Return Path"),
                _assistant("Return Path was a client."),
                _user("close it"),
            ],
        )
        turn = read_previous_turn(path, "close it")
        assert turn is not None
        assert turn.user_message == "Tell me about Return Path"
        assert turn.assistant_reply == "Return Path was a client."

    def test_skips_sidechain_meta_and_harness_records(self, tmp_path: Path) -> None:
        path = _write_transcript(
            tmp_path / "s.jsonl",
            [
                _user("What is the Lean Startup pilot?"),
                _assistant("A sub-agent result", isSidechain=True),
                _user("sub-agent prompt", isSidechain=True),
                _user(
                    "<system-reminder>injected</system-reminder>",
                    origin={"kind": "task-notification"},
                ),
                _user("meta text", isMeta=True),
                _assistant("The pilot ran in spring."),
            ],
        )
        turn = read_previous_turn(path, "and then?")
        assert turn == PreviousTurn(
            user_message="What is the Lean Startup pilot?",
            assistant_reply="The pilot ran in spring.",
        )

    def test_change_of_subject_carries_only_the_one_previous_turn(self, tmp_path: Path) -> None:
        """The measurement's winning arm carries ONE previous turn, never a
        running topic list: after the user changes subject, the topic of the
        turn before that is gone from the extractor's context."""
        path = _write_transcript(
            tmp_path / "s.jsonl",
            [
                _user("Summarise the Return Path engagement"),
                _assistant("Return Path was a consulting client."),
                _user("Now switch to the Acme Widgets pricing page"),
                _assistant("Acme Widgets lists three tiers."),
            ],
        )
        turn = read_previous_turn(path, "and the cheapest?")
        assert turn is not None
        assert "Acme Widgets" in turn.user_message
        assert "Return Path" not in turn.user_message
        assert "Return Path" not in turn.assistant_reply

    def test_each_piece_is_cut_to_1500_characters(self, tmp_path: Path) -> None:
        user_text = "U" * 2000 + "user-tail"
        reply_text = "reply-head" + "R" * 2000
        path = _write_transcript(
            tmp_path / "s.jsonl", [_user("user-head" + user_text), _assistant(reply_text)]
        )
        turn = read_previous_turn(path, "next")
        assert turn is not None
        assert len(turn.user_message) == PREVIOUS_TURN_CHARS == 1500
        assert len(turn.assistant_reply) == PREVIOUS_TURN_CHARS
        # The start of the user message, the END of the reply.
        assert turn.user_message.startswith("user-head")
        assert "user-tail" not in turn.user_message
        assert "reply-head" not in turn.assistant_reply
        assert turn.assistant_reply.endswith("R")

    def test_reply_without_a_user_message_in_the_window(self, tmp_path: Path) -> None:
        """A user message older than the read window is not found; the reply
        after it still is."""
        big = "x" * (TAIL_BYTES + 1024)
        path = _write_transcript(
            tmp_path / "s.jsonl",
            [_user("ancient question"), _tool_result(big), _assistant("the recent reply")],
        )
        turn = read_previous_turn(path, "follow-up")
        assert turn == PreviousTurn(user_message="", assistant_reply="the recent reply")

    @pytest.mark.parametrize("path_value", [None, ""])
    def test_no_path_is_none(self, path_value: str | None) -> None:
        assert read_previous_turn(path_value, "close it") is None

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert read_previous_turn(tmp_path / "absent.jsonl", "close it") is None

    def test_unreadable_path_is_none(self, tmp_path: Path) -> None:
        # A directory cannot be opened as a file: an unreadable transcript.
        assert read_previous_turn(tmp_path, "close it") is None

    def test_malformed_lines_only_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text('{not json\n[1, 2]\n"a string"\n\n', encoding="utf-8")
        assert read_previous_turn(path, "close it") is None

    def test_first_turn_of_a_session_is_none(self, tmp_path: Path) -> None:
        path = _write_transcript(tmp_path / "s.jsonl", [_user("close it")])
        assert read_previous_turn(path, "close it") is None

    def test_tail_read_never_exceeds_the_window(self, tmp_path: Path) -> None:
        """The bounded read: however long the transcript, at most
        TAIL_BYTES are read, so the cost per turn does not grow with it."""
        records = [_tool_result("y" * 4000) for _ in range(800)]  # ~3 MB
        path = _write_transcript(tmp_path / "s.jsonl", records)
        assert path.stat().st_size > 4 * TAIL_BYTES
        lines = turn_context._read_tail_lines(path)
        assert sum(len(line) + 1 for line in lines) <= TAIL_BYTES
        # The cut first line is dropped, so every line kept parses.
        assert all(json.loads(line) for line in lines)


# ---------------------------------------------------------------------------
# Latency (AC3)
# ---------------------------------------------------------------------------

#: The added per-turn latency the athenaeum#3104 measurement reported for the
#: winning arm A2 over prompt-only A0 on the Haiku extractor: 1,694 ms against
#: 1,611 ms per turn (docs/measurements/context-carryover-round1-2026-11-21.md,
#: section 4). Reading the transcript must fit well inside it.
MEASURED_A2_ADDED_MS = 1694 - 1611


class TestLatency:
    def test_worst_case_read_stays_within_the_measured_figure(self, tmp_path: Path) -> None:
        """Worst case: a long session whose read window holds no human
        message, so every line in the window is parsed. The median of five
        reads must stay under a quarter of the measured figure, leaving the
        rest of it for the larger extractor input the measurement timed."""
        records: list[dict[str, Any]] = [_user("an old question")]
        for _ in range(4000):  # ~8 MB of tool traffic after it
            records.append(_assistant("working", tool_use=True))
            records.append(_tool_result("z" * 2000))
        records.append(_assistant("the final reply"))
        path = _write_transcript(tmp_path / "s.jsonl", records)
        assert path.stat().st_size > 10 * TAIL_BYTES

        timings = []
        for _ in range(5):
            start = time.perf_counter()
            turn = read_previous_turn(path, "follow-up")
            timings.append((time.perf_counter() - start) * 1000)
        assert turn is not None and turn.assistant_reply.endswith("the final reply")
        median_ms = statistics.median(timings)
        assert median_ms < MEASURED_A2_ADDED_MS / 4, (
            f"previous-turn read took {median_ms:.1f} ms (median of 5); the "
            f"budget is a quarter of the measured {MEASURED_A2_ADDED_MS} ms"
        )


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


class TestSwitch:
    def test_default_is_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_CONTEXT_CARRYOVER", raising=False)
        assert resolve_push_context_carryover(None) is True

    @pytest.mark.parametrize(
        "value,expected",
        [("0", False), ("false", False), ("off", False), ("1", True), ("ON", True)],
    )
    def test_env(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
        monkeypatch.setenv("ATHENAEUM_PUSH_CONTEXT_CARRYOVER", value)
        assert (
            resolve_push_context_carryover({"recall": {"push_context_carryover": not expected}})
            is expected
        )

    def test_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_CONTEXT_CARRYOVER", raising=False)
        assert (
            resolve_push_context_carryover({"recall": {"push_context_carryover": False}}) is False
        )
        # A non-bool falls through to the default.
        assert resolve_push_context_carryover({"recall": {"push_context_carryover": "no"}}) is True

    def test_off_means_no_previous_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write_transcript(
            tmp_path / "s.jsonl", [_user("Return Path?"), _assistant("A client.")]
        )
        monkeypatch.setenv("ATHENAEUM_PUSH_CONTEXT_CARRYOVER", "0")
        assert previous_turn_for_hook(str(path), "close it") is None
        monkeypatch.setenv("ATHENAEUM_PUSH_CONTEXT_CARRYOVER", "1")
        assert previous_turn_for_hook(str(path), "close it") is not None


# ---------------------------------------------------------------------------
# extract_topics input
# ---------------------------------------------------------------------------


class TestExtractorInput:
    def _fake(
        self, monkeypatch: pytest.MonkeyPatch, text: str = '["Return Path"]'
    ) -> FakeLLMClient:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        fake = FakeLLMClient(text=text)
        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", fake)
        return fake

    def test_previous_turn_reaches_the_extractor_in_one_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = self._fake(monkeypatch)
        topics = query_topics.extract_topics(
            "close it",
            previous_user="Tell me about the Return Path engagement",
            assistant_reply="Return Path was a consulting client.",
        )
        assert topics == ["Return Path"]
        assert len(fake.calls) == 1
        content = fake.calls[0]["messages"][0]["content"]
        assert content == query_topics._CONTEXT_USER_TEMPLATE.format(
            previous_user="Tell me about the Return Path engagement",
            assistant_reply="Return Path was a consulting client.",
            prompt="close it",
        )
        assert fake.calls[0]["system"] == query_topics._SYSTEM_PROMPT

    def test_without_context_the_request_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = self._fake(monkeypatch)
        query_topics.extract_topics("Tell me about Return Path")
        assert fake.calls[0]["messages"][0]["content"] == query_topics._USER_TEMPLATE.format(
            prompt="Tell me about Return Path"
        )

    def test_missing_piece_is_marked_not_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake(monkeypatch)
        query_topics.extract_topics("and then?", assistant_reply="The pilot ran in spring.")
        content = fake.calls[0]["messages"][0]["content"]
        assert "Previous user message:\n(not available)" in content
        assert "The pilot ran in spring." in content

    def test_short_prompt_is_sent_only_with_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self._fake(monkeypatch)
        assert query_topics.extract_topics("yes") == []
        assert fake.calls == []
        assert query_topics.extract_topics("yes", previous_user="Merge the Return Path PR?") == [
            "Return Path"
        ]
        assert len(fake.calls) == 1
        assert query_topics.extract_topics("   ", previous_user="anything") == []
        assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# The hook, end to end
# ---------------------------------------------------------------------------

_RETURN_PATH_ROW = (
    "return-path.md",
    "Return Path",
    "client",
    "",
    "Return Path consulting engagement",
    "|__access_open__|",
    "reference",
    "warm",
)


@pytest.fixture()
def hook_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("KNOWLEDGE_ROOT", str(tmp_path / "knowledge"))
    monkeypatch.delenv("ATHENAEUM_PUSH_CONTEXT_CARRYOVER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_BACKEND", "fts5")
    _build_index(tmp_path / "wiki-index.db", 3, extra_rows=[_RETURN_PATH_ROW])
    return tmp_path


def _run_main(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> str:
    from athenaeum import claude_code_adapter

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert claude_code_adapter.main() == 0
    return capsys.readouterr().out


def _model_like_extractor(captured: list[dict[str, Any]]):
    """Stands in for the topic model: the topic of a follow-up comes from
    the earlier turn it refers to."""

    def _extract(prompt: str, timeout: float = 3.0, config: Any = None, **kwargs: Any) -> list[str]:
        captured.append({"prompt": prompt, **kwargs})
        context = kwargs.get("previous_user", "") + kwargs.get("assistant_reply", "")
        return ["Return Path"] if "Return Path" in prompt + context else []

    return _extract


class TestHook:
    def test_follow_up_prompt_picks_up_the_previous_topic(
        self, hook_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(
            hook_env / "s.jsonl",
            [
                _user("Tell me about the Return Path engagement"),
                _assistant("It was a consulting client."),
            ],
        )
        captured: list[dict[str, Any]] = []
        monkeypatch.setattr(query_topics, "extract_topics", _model_like_extractor(captured))

        out = _run_main(
            monkeypatch,
            {
                "prompt": "please close that one out",
                "session_id": "s1",
                "transcript_path": str(transcript),
            },
            capsys,
        )
        assert captured[0]["previous_user"] == "Tell me about the Return Path engagement"
        assert captured[0]["assistant_reply"] == "It was a consulting client."
        assert "Return Path" in json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def test_switch_off_is_prompt_only(
        self, hook_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(
            hook_env / "s.jsonl",
            [_user("Tell me about the Return Path engagement"), _assistant("A client.")],
        )
        monkeypatch.setenv("ATHENAEUM_PUSH_CONTEXT_CARRYOVER", "0")
        captured: list[dict[str, Any]] = []
        monkeypatch.setattr(query_topics, "extract_topics", _model_like_extractor(captured))

        out = _run_main(
            monkeypatch,
            {
                "prompt": "please close that one out",
                "session_id": "s2",
                "transcript_path": str(transcript),
            },
            capsys,
        )
        assert "previous_user" not in captured[0]
        assert "Return Path" not in out

    @pytest.mark.parametrize("transcript_path", ["", "/nonexistent/athenaeum/s.jsonl", "DIR"])
    def test_missing_or_unreadable_transcript_falls_back_to_prompt_only(
        self,
        hook_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        transcript_path: str,
    ) -> None:
        if transcript_path == "DIR":
            transcript_path = str(hook_env)  # a directory: exists, unreadable as a file
        captured: list[dict[str, Any]] = []
        monkeypatch.setattr(query_topics, "extract_topics", _model_like_extractor(captured))

        out = _run_main(
            monkeypatch,
            {
                "prompt": "what about the Return Path page",
                "session_id": "s3",
                "transcript_path": transcript_path,
            },
            capsys,
        )
        assert len(captured) == 1
        assert "previous_user" not in captured[0], "no previous turn, so no context argument"
        # Prompt-only extraction still pushes the page the prompt itself names.
        assert "Return Path" in out

    def test_change_of_subject_does_not_reach_back_two_turns(
        self, hook_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(
            hook_env / "s.jsonl",
            [
                _user("Tell me about the Return Path engagement"),
                _assistant("It was a consulting client."),
                _user("Different question: how do I rotate the backup disks?"),
                _assistant("Swap the disks weekly."),
            ],
        )
        captured: list[dict[str, Any]] = []
        monkeypatch.setattr(query_topics, "extract_topics", _model_like_extractor(captured))

        out = _run_main(
            monkeypatch,
            {
                "prompt": "and the offsite copy?",
                "session_id": "s4",
                "transcript_path": str(transcript),
            },
            capsys,
        )
        assert "Return Path" not in captured[0]["previous_user"] + captured[0]["assistant_reply"]
        assert "Return Path" not in out


class TestShortPrompts:
    """Subprocess runs with no credential: the regex fallback searches the
    prompt itself, so the 7-character prompt matches its fixture row only if
    the hook lets it through to a search at all."""

    _ROW = (
        "gadget7.md",
        "gadgetx",
        "gadgetx",
        "",
        "gadgetx",
        "|__access_open__|",
        "reference",
        "warm",
    )

    def test_short_prompt_with_a_readable_previous_turn_produces_a_search(
        self, tmp_path: Path
    ) -> None:
        _build_index(tmp_path / "wiki-index.db", 0, extra_rows=[self._ROW])
        transcript = _write_transcript(
            tmp_path / "s.jsonl",
            [_user("Which gadget should we ship?"), _assistant("Two candidates.")],
        )
        payload = json.dumps(
            {"prompt": "gadgetx", "session_id": "s", "transcript_path": str(transcript)}
        )
        result = _run_adapter(payload, cache_dir=tmp_path)
        assert result.returncode == 0
        assert "gadgetx" in result.stdout

    def test_short_prompt_with_no_previous_turn_produces_no_output(self, tmp_path: Path) -> None:
        _build_index(tmp_path / "wiki-index.db", 0, extra_rows=[self._ROW])
        transcript = _write_transcript(tmp_path / "s.jsonl", [_user("gadgetx")])
        for path_value in (str(transcript), str(tmp_path / "absent.jsonl"), ""):
            payload = json.dumps(
                {"prompt": "gadgetx", "session_id": "s", "transcript_path": path_value}
            )
            result = _run_adapter(payload, cache_dir=tmp_path)
            assert result.returncode == 0
            assert result.stdout == "", path_value

    def test_short_prompt_with_the_switch_off_produces_no_output(self, tmp_path: Path) -> None:
        _build_index(tmp_path / "wiki-index.db", 0, extra_rows=[self._ROW])
        transcript = _write_transcript(
            tmp_path / "s.jsonl",
            [_user("Which gadget should we ship?"), _assistant("Two candidates.")],
        )
        payload = json.dumps(
            {"prompt": "gadgetx", "session_id": "s", "transcript_path": str(transcript)}
        )
        result = _run_adapter(
            payload, cache_dir=tmp_path, extra_env={"ATHENAEUM_PUSH_CONTEXT_CARRYOVER": "0"}
        )
        assert result.returncode == 0
        assert result.stdout == ""
