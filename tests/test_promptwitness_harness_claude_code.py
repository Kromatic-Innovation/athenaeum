# SPDX-License-Identifier: Apache-2.0
"""Tests for the Claude Code harness adapter (athenaeum#2149).

One test class per acceptance criterion in the issue, mirroring
``tests/test_claude_code_adapter.py``'s own per-AC convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from promptwitness import canon, ledger
from promptwitness.harness import claude_code

# ---------------------------------------------------------------------------
# AC — one classifier test per WRAPPERS row, plus the two prefix-less
# defaults and the two override checks.
# ---------------------------------------------------------------------------


class TestClassifyPromptKind:
    _NO_OVERRIDE = {"agent_id": None, "transcript_path": None, "is_launch_argument": False}

    def test_plain_text_is_human(self) -> None:
        kind, human = claude_code.classify_prompt_kind("just type this in", **self._NO_OVERRIDE)
        assert (kind, human) == ("human", True)

    def test_pasted_content_prefix_is_human(self) -> None:
        kind, human = claude_code.classify_prompt_kind(
            '<pasted_content id="p1">hello</pasted_content>', **self._NO_OVERRIDE
        )
        assert (kind, human) == ("human", True)

    def test_command_message_prefix_is_slash_command(self) -> None:
        kind, human = claude_code.classify_prompt_kind(
            "<command-message>running /foo</command-message>", **self._NO_OVERRIDE
        )
        assert (kind, human) == ("slash-command", True)

    def test_command_name_prefix_is_slash_command(self) -> None:
        kind, human = claude_code.classify_prompt_kind(
            "<command-name>foo</command-name>", **self._NO_OVERRIDE
        )
        assert (kind, human) == ("slash-command", True)

    def test_task_notification_prefix_is_non_human(self) -> None:
        kind, human = claude_code.classify_prompt_kind(
            "<task-notification>done</task-notification>", **self._NO_OVERRIDE
        )
        assert (kind, human) == ("task-notification", False)

    def test_local_command_prefix_is_non_human(self) -> None:
        kind, human = claude_code.classify_prompt_kind(
            "<local-command-stdout>output</local-command-stdout>", **self._NO_OVERRIDE
        )
        assert (kind, human) == ("local-command", False)

    def test_system_reminder_prefix_is_non_human(self) -> None:
        kind, human = claude_code.classify_prompt_kind(
            "<system-reminder>context</system-reminder>", **self._NO_OVERRIDE
        )
        assert (kind, human) == ("system", False)

    def test_unrecognized_tag_is_unknown_and_non_human(self) -> None:
        """Counter-example this defeats: treating any unmatched `<tag` as
        human by falling through to the plain-text default -- the issue's
        own default-deny rule."""
        kind, human = claude_code.classify_prompt_kind(
            "<peer>something from an unconfirmed wrapper</peer>", **self._NO_OVERRIDE
        )
        assert (kind, human) == ("unknown", False)

    def test_leading_whitespace_is_stripped_before_matching(self) -> None:
        """Classification keys off the first NON-WHITESPACE characters."""
        kind, human = claude_code.classify_prompt_kind(
            "   <system-reminder>context</system-reminder>", **self._NO_OVERRIDE
        )
        assert (kind, human) == ("system", False)

    def test_agent_id_present_gives_subagent_overriding_plain_text(self) -> None:
        kind, human = claude_code.classify_prompt_kind(
            "ordinary looking text",
            agent_id="agent-123",
            transcript_path=None,
            is_launch_argument=False,
        )
        assert (kind, human) == ("subagent", False)

    def test_subagents_transcript_path_gives_subagent(self) -> None:
        kind, human = claude_code.classify_prompt_kind(
            "ordinary looking text",
            agent_id=None,
            transcript_path="/home/user/.claude/projects/x/subagents/y.jsonl",
            is_launch_argument=False,
        )
        assert (kind, human) == ("subagent", False)

    def test_launch_argument_overrides_subagent_too(self) -> None:
        """The issue's table: launch-argument overrides EVERY row above it,
        including the subagent override -- not just the wrapper rows."""
        kind, human = claude_code.classify_prompt_kind(
            "ordinary looking text",
            agent_id="agent-123",
            transcript_path=None,
            is_launch_argument=True,
        )
        assert (kind, human) == ("launch-argument", False)

    def test_launch_argument_overrides_a_wrapper_row(self) -> None:
        kind, human = claude_code.classify_prompt_kind(
            "<system-reminder>context</system-reminder>",
            agent_id=None,
            transcript_path=None,
            is_launch_argument=True,
        )
        assert (kind, human) == ("launch-argument", False)


# ---------------------------------------------------------------------------
# AC — segment offsets for zero, one, two pastes, and an unterminated tag.
# ---------------------------------------------------------------------------


class TestPastedSegments:
    def test_zero_pastes_yields_one_typed_segment_spanning_the_prompt(self) -> None:
        prompt = "no paste tags in here at all"
        segments, truncated = claude_code.pasted_segments(prompt)
        assert truncated is False
        assert len(segments) == 1
        seg = segments[0]
        assert (seg.kind, seg.start, seg.end) == ("typed", 0, len(prompt))
        assert seg.digest == canon.digest(prompt)

    def test_empty_prompt_yields_no_segments(self) -> None:
        segments, truncated = claude_code.pasted_segments("")
        assert segments == []
        assert truncated is False

    def test_one_paste_yields_typed_pasted_typed(self) -> None:
        prompt = 'intro <pasted_content id="p1">pasted body</pasted_content> outro'
        segments, truncated = claude_code.pasted_segments(prompt)
        assert truncated is False
        assert [s.kind for s in segments] == ["typed", "pasted", "typed"]

        typed1, pasted1, typed2 = segments
        assert prompt[typed1.start : typed1.end] == "intro "
        assert typed1.digest == canon.digest("intro ")
        assert pasted1.digest == canon.digest("pasted body")
        assert pasted1.paste_id == "p1"
        assert prompt[pasted1.start : pasted1.end] == "pasted body"
        assert prompt[typed2.start : typed2.end] == " outro"
        assert typed2.digest == canon.digest(" outro")

    def test_paste_at_start_omits_empty_leading_typed_segment(self) -> None:
        prompt = '<pasted_content id="p1">body</pasted_content> trailing text'
        segments, truncated = claude_code.pasted_segments(prompt)
        assert truncated is False
        assert [s.kind for s in segments] == ["pasted", "typed"]

    def test_two_pastes_yields_five_segments(self) -> None:
        prompt = (
            'a <pasted_content id="p1">one</pasted_content> b '
            '<pasted_content id="p2">two</pasted_content> c'
        )
        segments, truncated = claude_code.pasted_segments(prompt)
        assert truncated is False
        assert [s.kind for s in segments] == ["typed", "pasted", "typed", "pasted", "typed"]
        paste_ids = [s.paste_id for s in segments if s.kind == "pasted"]
        assert paste_ids == ["p1", "p2"]
        for seg in segments:
            assert seg.digest == canon.digest(prompt[seg.start : seg.end])

    def test_unterminated_tag_yields_one_pasted_segment_to_the_end_truncated(self) -> None:
        prompt = 'typed before <pasted_content id="p9">never closes, runs off the end'
        segments, truncated = claude_code.pasted_segments(prompt)
        assert truncated is True
        assert [s.kind for s in segments] == ["typed", "pasted"]
        typed, pasted = segments
        assert prompt[typed.start : typed.end] == "typed before "
        assert pasted.end == len(prompt)
        assert pasted.paste_id == "p9"
        assert pasted.digest == canon.digest(prompt[pasted.start :])


# ---------------------------------------------------------------------------
# AC — slash-args segment: exactly one when args are present, none when
# absent or empty.
# ---------------------------------------------------------------------------


class TestSlashArgsSegment:
    def test_command_args_present_yields_one_segment_with_its_own_digest(self) -> None:
        prompt = (
            "<command-message>running /review</command-message>"
            "<command-name>review</command-name>"
            "<command-args>fix the typo in README</command-args>"
        )
        segment = claude_code.slash_args_segment(prompt)
        assert segment is not None
        assert segment.kind == "slash-args"
        assert prompt[segment.start : segment.end] == "fix the typo in README"
        assert segment.digest == canon.digest("fix the typo in README")

    def test_no_command_args_tag_yields_none(self) -> None:
        prompt = (
            "<command-message>running /review</command-message>"
            "<command-name>review</command-name>"
        )
        assert claude_code.slash_args_segment(prompt) is None

    def test_empty_command_args_yields_none(self) -> None:
        prompt = (
            "<command-message>running /review</command-message>"
            "<command-name>review</command-name>"
            "<command-args></command-args>"
        )
        assert claude_code.slash_args_segment(prompt) is None

    def test_whitespace_only_command_args_yields_none(self) -> None:
        prompt = "<command-name>review</command-name><command-args>   </command-args>"
        assert claude_code.slash_args_segment(prompt) is None


# ---------------------------------------------------------------------------
# AC — the channel matrix: six named rows.
# ---------------------------------------------------------------------------


class TestDetectChannel:
    def test_cli_attended_tty_gives_interactive(self) -> None:
        env = {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ATTENDED": "1"}
        channel, signals = claude_code.detect_channel(
            env, pid="123", tty_probe=lambda pid: True
        )
        assert channel == "interactive"
        assert signals["has_tty"] is True
        assert signals["bridge_present"] is False

    def test_sdk_cli_unattended_gives_headless(self) -> None:
        env = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "CLAUDE_CODE_SESSION_ATTENDED": "0"}
        channel, _signals = claude_code.detect_channel(
            env, pid="123", tty_probe=lambda pid: True
        )
        assert channel == "headless"

    def test_missing_env_gives_unknown(self) -> None:
        channel, _signals = claude_code.detect_channel({}, pid=None, tty_probe=lambda pid: None)
        assert channel == "unknown"

    def test_cli_attended_no_tty_gives_unknown(self) -> None:
        env = {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ATTENDED": "1"}
        channel, _signals = claude_code.detect_channel(
            env, pid="123", tty_probe=lambda pid: False
        )
        assert channel == "unknown"

    def test_cli_attended_tty_child_session_gives_unknown(self) -> None:
        env = {
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1",
        }
        channel, _signals = claude_code.detect_channel(
            env, pid="123", tty_probe=lambda pid: True
        )
        assert channel == "unknown"

    def test_cli_attended_tty_bridge_gives_bridge(self) -> None:
        env = {
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
            "CLAUDE_CODE_BRIDGE_SESSION_ID": "bridge-session-xyz",
        }
        channel, signals = claude_code.detect_channel(
            env, pid="123", tty_probe=lambda pid: True
        )
        assert channel == "bridge"
        # Presence is recorded; the actual bridge id value never is.
        assert signals["bridge_present"] is True
        assert "bridge-session-xyz" not in str(signals)

    def test_bridge_checked_before_interactive_even_with_a_tty(self) -> None:
        """A bridge session still owns a TTY -- bridge must win anyway."""
        env = {
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
            "CLAUDE_CODE_BRIDGE_SESSION_ID": "bridge-session-xyz",
        }
        channel, _signals = claude_code.detect_channel(
            env, pid="123", tty_probe=lambda pid: True
        )
        assert channel == "bridge"


# ---------------------------------------------------------------------------
# AC — LedgerIndex human filter rejects headless/bridge lines.
# ---------------------------------------------------------------------------


class TestLedgerIndexRejectsNonInteractive:
    def test_headless_human_kind_line_is_not_witnessable(self, tmp_path: Path) -> None:
        line = ledger.append_line(
            tmp_path,
            kind="turn",
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
            session="s1",
            turn=1,
            prompt_id="p1",
            prompt_kind="human",
            channel="headless",
        )
        assert ledger.LedgerIndex.is_witnessable_human(line) is False

    def test_bridge_human_kind_line_is_not_witnessable(self, tmp_path: Path) -> None:
        line = ledger.append_line(
            tmp_path,
            kind="turn",
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
            session="s1",
            turn=1,
            prompt_id="p1",
            prompt_kind="human",
            channel="bridge",
        )
        assert ledger.LedgerIndex.is_witnessable_human(line) is False

    def test_interactive_human_kind_line_is_witnessable(self, tmp_path: Path) -> None:
        """Negative control: the same construction with channel=interactive
        MUST be witnessable, or the two tests above would pass vacuously."""
        line = ledger.append_line(
            tmp_path,
            kind="turn",
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
            session="s1",
            turn=1,
            prompt_id="p1",
            prompt_kind="human",
            channel="interactive",
        )
        assert ledger.LedgerIndex.is_witnessable_human(line) is True


# ---------------------------------------------------------------------------
# AC — subagent / launch-argument via record_turn: non-human, no argv text
# stored.
# ---------------------------------------------------------------------------


class TestRecordTurnSubagentAndLaunchArgument:
    def test_agent_id_in_stdin_gives_subagent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        raw = '{"prompt": "do the thing", "session_id": "s1", "agent_id": "sub-42"}'
        line = claude_code.record_turn(raw, {})
        assert line.prompt_kind == "subagent"
        assert ledger.LedgerIndex.is_witnessable_human(line) is False
        # The correlation id itself is legitimately stored under `signals`
        # (per the design's "raw values always go in signals" rule) -- it is
        # the PROMPT TEXT that must never appear anywhere on disk.
        assert line.signals.get("agent_id") == "sub-42"
        assert "do the thing" not in str(line.to_dict())

    def test_first_turn_digest_matching_argv_gives_launch_argument(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        prompt = "please help me with this specific task"
        raw = f'{{"prompt": "{prompt}", "session_id": "s-launch"}}'

        monkeypatch.setattr(
            claude_code,
            "_probe_argv",
            lambda pid: f'/usr/bin/claude "{prompt}"',
        )
        line = claude_code.record_turn(raw, {"CLAUDE_PID": "999"})
        assert line.prompt_kind == "launch-argument"
        assert ledger.LedgerIndex.is_witnessable_human(line) is False
        # Only a boolean effect (the kind) is observable -- no raw argv text
        # anywhere in the written line.
        assert "/usr/bin/claude" not in str(line.to_dict())

    def test_second_turn_same_session_is_not_launch_argument(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The launch-argument override only ever applies to the session's
        first turn."""
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        prompt = "please help me with this specific task"
        monkeypatch.setattr(
            claude_code, "_probe_argv", lambda pid: f'/usr/bin/claude "{prompt}"'
        )
        raw = f'{{"prompt": "{prompt}", "session_id": "s-launch2"}}'
        first = claude_code.record_turn(raw, {"CLAUDE_PID": "999"})
        second = claude_code.record_turn(raw, {"CLAUDE_PID": "999"})
        assert first.prompt_kind == "launch-argument"
        assert second.prompt_kind == "human"


# ---------------------------------------------------------------------------
# AC — resume detection: a second claude_pid on the same session marks the
# second line resumed=True.
# ---------------------------------------------------------------------------


class TestResumeDetection:
    def test_second_process_on_same_session_is_marked_resumed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        raw = '{"prompt": "hello there friend", "session_id": "resume-sess"}'

        first = claude_code.record_turn(raw, {"CLAUDE_PID": "111"})
        second = claude_code.record_turn(raw, {"CLAUDE_PID": "222"})
        third = claude_code.record_turn(raw, {"CLAUDE_PID": "222"})

        assert first.resumed is False
        assert second.resumed is True
        # Same pid as the immediately preceding line -- not a resume.
        assert third.resumed is False
        assert [first.turn, second.turn, third.turn] == [1, 2, 3]


# ---------------------------------------------------------------------------
# AC — record_turn end to end: digests present, no prompt text anywhere.
# ---------------------------------------------------------------------------


class TestRecordTurnEndToEnd:
    def test_human_prompt_round_trips_with_no_prompt_text_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        secret_prompt = "the secret sauce recipe is twelve herbs and spices"
        raw = f'{{"prompt": "{secret_prompt}", "session_id": "s-e2e", "prompt_id": "pr-1"}}'
        env = {
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
        }

        line = claude_code.record_turn(raw, env)

        assert line.prompt_kind == "human"
        assert line.prompt_digest == canon.digest(canon.normalize(secret_prompt))
        assert line.raw_digest == canon.digest(secret_prompt)
        assert line.prompt_chars == len(secret_prompt)

        for partition_name in ledger.list_partitions(tmp_path):
            on_disk = (tmp_path / partition_name).read_text(encoding="utf-8")
            assert secret_prompt not in on_disk
            for word in secret_prompt.split():
                assert word not in on_disk

    def test_malformed_stdin_degrades_to_empty_prompt_without_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        line = claude_code.record_turn("{not valid json", {})
        assert line.prompt_kind == "human"
        assert line.prompt_chars == 0


# ---------------------------------------------------------------------------
# AC — session_close writes turns/last_seq/turns_witnessed.
# ---------------------------------------------------------------------------


class TestSessionClose:
    def test_no_session_id_in_env_is_a_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        assert claude_code.session_close({}) is None
        assert ledger.list_partitions(tmp_path) == []

    def test_counts_turns_and_witnessed_turns_for_the_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        ledger.append_line(
            tmp_path,
            kind="turn",
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
            session="close-sess",
            turn=1,
            prompt_id="p1",
            prompt_kind="human",
            channel="interactive",
        )
        ledger.append_line(
            tmp_path,
            kind="turn",
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
            session="close-sess",
            turn=2,
            prompt_id="p2",
            prompt_kind="task-notification",
            channel="headless",
        )

        close_line = claude_code.session_close({"CLAUDE_CODE_SESSION_ID": "close-sess"})

        assert close_line is not None
        assert close_line.kind == "session-close"
        assert close_line.turns == 2
        assert close_line.turns_witnessed == 1
        assert close_line.last_seq == 2
