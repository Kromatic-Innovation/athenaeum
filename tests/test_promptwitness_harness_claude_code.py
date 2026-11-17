# SPDX-License-Identifier: Apache-2.0
"""Tests for the Claude Code harness adapter (athenaeum#2149).

One test class per acceptance criterion in the issue, mirroring
``tests/test_claude_code_adapter.py``'s own per-AC convention.
"""

from __future__ import annotations

import sys
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

    def test_raw_slash_line_is_slash_command(self) -> None:
        """The live hook sees the unexpanded ``/name args`` line (athenaeum#2161)."""
        for prompt in ("/review fix the typo", "/review", "  /ns:cmd-1.x args"):
            kind, human = claude_code.classify_prompt_kind(prompt, **self._NO_OVERRIDE)
            assert (kind, human) == ("slash-command", True), prompt

    def test_single_segment_path_is_a_slash_command_false_miss(self) -> None:
        """Documented false miss: a one-segment path is indistinguishable from
        a command name, so it classifies ``slash-command`` (human, counted)."""
        for prompt in ("/etc is broken", "/tmp", "/help me fix this"):
            kind, human = claude_code.classify_prompt_kind(prompt, **self._NO_OVERRIDE)
            assert (kind, human) == ("slash-command", True), prompt

    def test_path_like_or_mid_sentence_slash_is_still_human(self) -> None:
        for prompt in ("/usr/bin is missing", "/ not a command", "see /review later", "//x"):
            kind, human = claude_code.classify_prompt_kind(prompt, **self._NO_OVERRIDE)
            assert (kind, human) == ("human", True), prompt

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
            transcript_path="/var/tmp/claude-projects/x/subagents/y.jsonl",
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

    def test_raw_slash_line_args_yield_one_segment(self) -> None:
        prompt = "/review fix the typo in README"
        segment = claude_code.slash_args_segment(prompt)
        assert segment is not None
        assert prompt[segment.start : segment.end] == "fix the typo in README"
        assert segment.digest == canon.digest("fix the typo in README")

    def test_raw_slash_line_without_args_yields_none(self) -> None:
        assert claude_code.slash_args_segment("/review") is None
        assert claude_code.slash_args_segment("/review   ") is None

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

    def test_cli_attended_tty_child_session_flag_is_not_a_classifier_input(self) -> None:
        """A live top-level interactive hook carries CHILD_SESSION=1
        (athenaeum#2161), so the flag must not demote it."""
        env = {
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1",
        }
        channel, _signals = claude_code.detect_channel(
            env, pid="123", tty_probe=lambda pid: True
        )
        assert channel == "interactive"
        assert _signals["child_session"] == "1"

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
# AC — athenaeum#2172: signals.launcher from PROMPTWITNESS_LAUNCHER, read by
# record_turn (the real hook-record path), is a label only and never affects
# channel / prompt_kind / human-ness.
# ---------------------------------------------------------------------------


class TestRecordTurnLauncherSignal:
    def test_headless_launcher_is_recorded_and_excluded_from_human_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        raw = (
            '{"prompt": "run the nightly sweep", "session_id": "s-launcher-headless",'
            ' "prompt_id": "pw-headless-1"}'
        )
        env = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "PROMPTWITNESS_LAUNCHER": "hestia"}

        line = claude_code.record_turn(raw, env)

        assert line.channel == "headless"
        assert line.signals["launcher"] == "hestia"
        assert ledger.LedgerIndex.is_witnessable_human(line) is False
        index = ledger.LedgerIndex.build(tmp_path)
        assert "pw-headless-1" not in {ln.prompt_id for ln in index.human_lines()}

    def test_interactive_negative_control_same_launcher_is_witnessable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control, required by the issue: the SAME launcher value
        on an interactive-signal call must still be witnessable, or the
        headless assertion above would be vacuous. ``launcher`` is a label
        only -- it neither grants nor removes human-ness. Mirrors
        ``TestLedgerIndexRejectsNonInteractive`` but drives the real
        hook-record path (``record_turn``) instead of ``append_line``
        directly, so the module-level tty probe needs stubbing (``record_turn``
        calls ``detect_channel`` without an explicit ``tty_probe``)."""
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        monkeypatch.setattr(claude_code, "_probe_has_tty", lambda pid: True)
        raw = (
            '{"prompt": "please help me with this", "session_id": "s-launcher-interactive",'
            ' "prompt_id": "pw-interactive-1"}'
        )
        env = {
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
            "PROMPTWITNESS_LAUNCHER": "hestia",
        }

        line = claude_code.record_turn(raw, env)

        assert line.channel == "interactive"
        assert line.signals["launcher"] == "hestia"
        assert ledger.LedgerIndex.is_witnessable_human(line) is True
        index = ledger.LedgerIndex.build(tmp_path)
        assert "pw-interactive-1" in {ln.prompt_id for ln in index.human_lines()}

    def test_unset_launcher_gives_no_launcher_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        raw = '{"prompt": "hello", "session_id": "s-launcher-unset"}'

        line = claude_code.record_turn(raw, {})

        assert "launcher" not in line.signals

    @pytest.mark.parametrize(
        "launcher_value",
        ["", "   \t  ", "\x00\x01\x02", "\n\n\t"],
        ids=["empty", "whitespace-only", "control-only", "newlines-and-tabs-only"],
    )
    def test_empty_or_control_only_launcher_gives_no_launcher_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher_value: str
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        raw = '{"prompt": "hello", "session_id": "s-launcher-empty"}'
        env = {"PROMPTWITNESS_LAUNCHER": launcher_value}

        line = claude_code.record_turn(raw, env)

        assert "launcher" not in line.signals

    def test_launcher_is_sanitised_through_record_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        raw = '{"prompt": "hello", "session_id": "s-launcher-sanitise"}'
        env = {"PROMPTWITNESS_LAUNCHER": "  hes\ntia\x00  "}

        line = claude_code.record_turn(raw, env)

        assert line.signals["launcher"] == "hestia"

    def test_launcher_longer_than_64_chars_is_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        raw = '{"prompt": "hello", "session_id": "s-launcher-cap"}'
        long_value = "x" * 200
        env = {"PROMPTWITNESS_LAUNCHER": long_value}

        line = claude_code.record_turn(raw, env)

        assert line.signals["launcher"] == long_value[:64]
        assert len(line.signals["launcher"]) == 64


class TestSanitiseLauncherHelper:
    """Unit-level pin on `_sanitise_launcher`'s own order of operations
    (strip C* chars, then whitespace, then cap at 64, then None-if-empty) --
    the ordering itself is an acceptance criterion, not just the end result."""

    def test_none_input_gives_none(self) -> None:
        assert claude_code._sanitise_launcher(None) is None

    def test_mixed_control_and_whitespace_sanitises_to_hestia(self) -> None:
        assert claude_code._sanitise_launcher("  hes\ntia\x00  ") == "hestia"

    def test_two_hundred_chars_caps_to_first_64(self) -> None:
        long_value = "x" * 200
        assert claude_code._sanitise_launcher(long_value) == long_value[:64]

    def test_control_characters_only_gives_none(self) -> None:
        assert claude_code._sanitise_launcher("\x00\x01\x02") is None


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
# athenaeum#2744 — a resumed session re-runs the launch-argument check on the
# first hook call of the resuming process, whether or not session state
# exists, and only on that call.
# ---------------------------------------------------------------------------


class TestResumedSessionLaunchArgument:
    SESSION = "resumed-sess"
    SCRIPTED = "continue the nightly sweep from where it stopped"

    def _raw(self, prompt: str) -> str:
        return f'{{"prompt": "{prompt}", "session_id": "{self.SESSION}"}}'

    def _seed_prior_process(self, monkeypatch: pytest.MonkeyPatch) -> ledger.WitnessLine:
        """A first, earlier process on the session: a typed prompt, so the
        session has state before the resume."""
        monkeypatch.setattr(claude_code, "_probe_argv", lambda pid: "claude")
        line = claude_code.record_turn(
            self._raw("typed in the first process"), {"CLAUDE_PID": "111"}
        )
        assert line.prompt_kind == "human"
        return line

    @pytest.mark.parametrize(
        "argv",
        [
            f'claude --resume {SESSION} "{SCRIPTED}"',
            f'claude -r {SESSION} "{SCRIPTED}"',
            f'claude -c "{SCRIPTED}"',
            f'claude --continue "{SCRIPTED}"',
            f"claude\0--resume\0{SESSION}\0{SCRIPTED}\0",
            f"claude\0-c\0{SCRIPTED}\0",
        ],
        ids=["resume-ps", "r-ps", "c-ps", "continue-ps", "resume-proc", "c-proc"],
    )
    def test_scripted_resume_prompt_is_a_launch_argument(
        self, argv: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        self._seed_prior_process(monkeypatch)
        monkeypatch.setattr(claude_code, "_probe_argv", lambda pid, a=argv: a)
        line = claude_code.record_turn(self._raw(self.SCRIPTED), {"CLAUDE_PID": "222"})
        assert line.resumed is True
        assert line.turn == 2
        assert line.prompt_kind == "launch-argument"
        assert ledger.LedgerIndex.is_witnessable_human(line) is False

    def test_resume_without_prior_state_is_still_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No state file (pruned cache, or a session begun before the hook was
        installed): the first call is checked as before."""
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        monkeypatch.setattr(claude_code, "_probe_argv", lambda pid: f'claude -c "{self.SCRIPTED}"')
        line = claude_code.record_turn(self._raw(self.SCRIPTED), {"CLAUDE_PID": "222"})
        assert line.prompt_kind == "launch-argument"

    @pytest.mark.parametrize(
        "argv",
        [f"claude --resume {SESSION}", "claude -c", "claude --continue", "claude"],
    )
    def test_resume_with_no_positional_prompt_and_typed_first_prompt_is_human(
        self, argv: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        self._seed_prior_process(monkeypatch)
        monkeypatch.setattr(claude_code, "_probe_argv", lambda pid, a=argv: a)
        line = claude_code.record_turn(self._raw("typed after the resume"), {"CLAUDE_PID": "222"})
        assert line.resumed is True
        assert line.prompt_kind == "human"

    def test_second_turn_after_resume_is_not_rechecked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The argv below would fail closed for any unmatched typed prompt
        (an unruled-out positional), so a ``human`` second turn proves the
        check did not run again."""
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        self._seed_prior_process(monkeypatch)
        calls: list[str | None] = []

        def probe(pid: str | None) -> str:
            calls.append(pid)
            return f'claude -c "{self.SCRIPTED}"'

        monkeypatch.setattr(claude_code, "_probe_argv", probe)
        first = claude_code.record_turn(self._raw(self.SCRIPTED), {"CLAUDE_PID": "222"})
        second = claude_code.record_turn(
            self._raw("typed on the second turn"), {"CLAUDE_PID": "222"}
        )
        assert first.prompt_kind == "launch-argument"
        assert second.prompt_kind == "human"
        assert second.resumed is False
        assert calls == ["222"]


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


class TestLaunchArgumentFromPsOutput:
    """``ps -o args=`` joins argv with single spaces and no quoting
    (athenaeum#2161), so a multi-word launch prompt spans several tokens."""

    def test_multi_word_prompt_matches_as_contiguous_run(self) -> None:
        prompt = "please reply with the single word 'ack'"
        assert claude_code._is_launch_argument(
            prompt,
            pid="1",
            is_first_turn=True,
            argv_probe=lambda _pid: f"claude --settings /x/s.json {prompt}",
        )

    def test_unmatched_positional_in_ps_output_fails_closed(self) -> None:
        """``ps`` cannot rule out that the positional is a mangled version of
        the prompt, so it counts as a launch argument (the exact-argv path,
        which can rule it out, is tested below)."""
        assert claude_code._is_launch_argument(
            "something typed later",
            pid="1",
            is_first_turn=True,
            argv_probe=lambda _pid: "claude --settings /x/s.json please reply",
        )

    def test_later_turn_is_never_a_launch_argument(self) -> None:
        assert not claude_code._is_launch_argument(
            "hello",
            pid="1",
            is_first_turn=False,
            argv_probe=lambda _pid: "claude hello",
        )

    def test_typed_prompt_with_flags_only_is_human(self) -> None:
        for line in ("claude", "claude --settings /x/s.json", "claude --verbose --model opus"):
            assert not claude_code._is_launch_argument(
                "typed by hand",
                pid="1",
                is_first_turn=True,
                argv_probe=lambda _pid, line=line: line,
            )

    def test_ps_mangled_multiline_prompt_fails_closed(self) -> None:
        prompt = "first line\nsecond line\n\nthird line"
        for rendered in (
            "first line?second line??third line",
            "first line\\012second line\\012\\012third line",
        ):
            assert claude_code._is_launch_argument(
                prompt,
                pid="1",
                is_first_turn=True,
                argv_probe=lambda _pid, r=rendered: f"claude --settings /x/s.json {r}",
            )

    def test_very_long_ps_prompt_is_a_launch_argument(self) -> None:
        prompt = " ".join(f"w{i}" for i in range(450))
        assert claude_code._is_launch_argument(
            prompt,
            pid="1",
            is_first_turn=True,
            argv_probe=lambda _pid: f"claude {prompt}",
        )
        # And a typed prompt is not confused with it.
        assert not claude_code._is_launch_argument(
            "typed by hand",
            pid="1",
            is_first_turn=True,
            argv_probe=lambda _pid: "claude --verbose",
        )

    def test_unparsable_ps_line_with_positional_fails_closed(self) -> None:
        # Unbalanced quote: shlex cannot parse it, a positional is present.
        assert claude_code._is_launch_argument(
            "something typed",
            pid="1",
            is_first_turn=True,
            argv_probe=lambda _pid: "claude 'half quoted launch brief",
        )

    def test_exact_argv_multiline_prompt_matches_whole_argument(self) -> None:
        prompt = "line one\nline  two\n\nline three"
        argv = "\0".join(["node", "/x/claude.js", "--settings", "/x/s.json", prompt]) + "\0"
        assert claude_code._is_launch_argument(
            prompt, pid="1", is_first_turn=True, argv_probe=lambda _pid: argv
        )
        # Whitespace-normalised: the hook may see different spacing.
        assert claude_code._is_launch_argument(
            "line one line two line three",
            pid="1",
            is_first_turn=True,
            argv_probe=lambda _pid: argv,
        )

    def test_exact_argv_long_prompt_matches(self) -> None:
        prompt = " ".join(f"w{i}" for i in range(500))
        argv = "\0".join(["claude", prompt]) + "\0"
        assert claude_code._is_launch_argument(
            prompt, pid="1", is_first_turn=True, argv_probe=lambda _pid: argv
        )

    def test_exact_argv_typed_prompt_is_human(self) -> None:
        argv = "\0".join(["claude", "--settings", "/x/s.json"]) + "\0"
        assert not claude_code._is_launch_argument(
            "typed by hand", pid="1", is_first_turn=True, argv_probe=lambda _pid: argv
        )

    def test_exact_argv_other_positional_is_ruled_out(self) -> None:
        argv = "\0".join(["claude", "a different launch prompt"]) + "\0"
        assert not claude_code._is_launch_argument(
            "typed by hand", pid="1", is_first_turn=True, argv_probe=lambda _pid: argv
        )

    def test_proc_cmdline_is_preferred_over_ps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(claude_code, "_probe_proc_cmdline", lambda pid: "claude\0hi there\0")

        def boom(*_a: object, **_k: object) -> None:
            raise AssertionError("ps must not run when /proc answers")

        monkeypatch.setattr(claude_code.subprocess, "run", boom)
        assert claude_code._probe_argv("5") == "claude\0hi there\0"

    def test_proc_cmdline_rejects_non_numeric_pid(self) -> None:
        assert claude_code._probe_proc_cmdline("../self") is None

    @pytest.mark.parametrize("size", [5000])
    def test_very_long_argv_is_classified_quickly(self, size: int) -> None:
        import time

        words = [f"w{i}" for i in range(size)]
        ps_line = "claude " + " ".join(words)
        exact = "\0".join(["claude", *words]) + "\0"
        start = time.monotonic()
        for probe in (ps_line, exact):
            claude_code._is_launch_argument(
                "typed by hand", pid="1", is_first_turn=True, argv_probe=lambda _p, p=probe: p
            )
            claude_code._is_launch_argument(
                " ".join(words[100:2600]),
                pid="1",
                is_first_turn=True,
                argv_probe=lambda _p, p=probe: p,
            )
        assert time.monotonic() - start < 2.0



# ---------------------------------------------------------------------------
# athenaeum#2745 — review follow-ups to the launch-prompt check: an empty
# probe fails closed, ps octal escapes decode, and typed prompts are not
# misread as launch arguments on macOS-shaped ps lines.
# ---------------------------------------------------------------------------


class TestEmptyArgvProbe:
    def test_empty_probe_with_claude_pid_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        monkeypatch.setattr(claude_code, "_probe_argv", lambda pid: None)
        raw = '{"prompt": "typed or launched, who knows", "session_id": "s-empty"}'
        line = claude_code.record_turn(raw, {"CLAUDE_PID": "999"})
        assert line is not None
        assert line.prompt_kind == "launch-argument"
        assert line.signals["argv_probe"] == "failed"
        assert ledger.LedgerIndex.is_witnessable_human(line) is False

    @pytest.mark.parametrize("empty", [None, ""])
    def test_empty_probe_helper_fails_closed(self, empty: str | None) -> None:
        assert claude_code._launch_argument_check(
            "hello", pid="1", is_first_turn=True, argv_probe=lambda _pid, e=empty: e
        ) == (True, "failed")

    def test_no_claude_pid_keeps_text_classification_and_records_signal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        raw = '{"prompt": "typed by hand", "session_id": "s-nopid"}'
        line = claude_code.record_turn(raw, {})
        assert line is not None
        assert line.prompt_kind == "human"
        assert line.signals["argv_probe"] == "failed"

    def test_successful_probe_records_its_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        monkeypatch.setattr(claude_code, "_probe_argv", lambda pid: "claude --verbose")
        raw = '{"prompt": "typed by hand", "session_id": "s-ps"}'
        first = claude_code.record_turn(raw, {"CLAUDE_PID": "999"})
        second = claude_code.record_turn(raw, {"CLAUDE_PID": "999"})
        assert first is not None and second is not None
        assert first.prompt_kind == "human"
        assert first.signals["argv_probe"] == "ps"
        # Later turns are not checked, so they carry no probe signal.
        assert "argv_probe" not in second.signals


class TestPsOctalEscapes:
    def test_tab_and_newline_escapes_match_without_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_fallback(_mask: object) -> bool:
            raise AssertionError("fell through to the fail-closed rule")

        prompt = "col1\tcol2\nsecond line"
        rendered = "claude --settings /x/s.json col1\\011col2\\012second line"
        # Proof the match is a real match: the fail-closed fallback is the
        # only path that reads `"positional" in mask`, and a list subclass
        # makes that path raise.
        real_mask = claude_code._ps_positional_mask

        class _NoContains(list[str]):
            def __contains__(self, item: object) -> bool:
                return no_fallback(item)

        monkeypatch.setattr(
            claude_code, "_ps_positional_mask", lambda tokens: _NoContains(real_mask(tokens))
        )
        assert claude_code._launch_argument_check(
            prompt, pid="1", is_first_turn=True, argv_probe=lambda _pid: rendered
        ) == (True, "ps")

    def test_decode_ps_escapes(self) -> None:
        assert claude_code._decode_ps_escapes("a\\011b\\012c\\\\d") == "a\tb\nc\\d"


class TestMacOsPsFalseMisses:
    @pytest.mark.parametrize(
        "line",
        [
            "node /x/claude",
            "node /x/claude --verbose",
            "/Applications/My Tools/claude",
            "/Applications/My Tools/claude --model opus",
            "claude --add-dir /a /b",
            "claude --add-dir /a /b --verbose",
            "node /opt/My Tools/claude --add-dir /a /b /c",
        ],
    )
    def test_typed_first_prompt_is_human(self, line: str) -> None:
        assert not claude_code._is_launch_argument(
            "typed by hand", pid="1", is_first_turn=True, argv_probe=lambda _pid, ln=line: ln
        )

    @pytest.mark.parametrize(
        ("line", "typed"),
        [("claude --model opus", "opus"), ("claude --add-dir /a /b", "/b")],
    )
    def test_one_word_prompt_equal_to_a_flag_value_is_human(self, line: str, typed: str) -> None:
        assert not claude_code._is_launch_argument(
            typed, pid="1", is_first_turn=True, argv_probe=lambda _pid, ln=line: ln
        )

    @pytest.mark.parametrize(
        "line",
        [
            'node /x/claude "launch me now"',
            "/Applications/My Tools/claude launch me now",
            "claude --add-dir /a /b -- launch me now",
            "claude -p launch me now",
            "claude --verbose launch me now",
        ],
    )
    def test_launch_prompt_still_matches(self, line: str) -> None:
        assert claude_code._is_launch_argument(
            "launch me now", pid="1", is_first_turn=True, argv_probe=lambda _pid, ln=line: ln
        )

    def test_unmatched_positional_after_boolean_flag_fails_closed(self) -> None:
        # `-p` takes no value, so the word after it is a prompt that does not
        # match the typed one: it cannot be ruled out.
        assert claude_code._is_launch_argument(
            "typed by hand",
            pid="1",
            is_first_turn=True,
            argv_probe=lambda _pid: "claude -p something else",
        )


class TestRecordTurnLedgerLockDeadline:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock holder")
    def test_held_lock_skips_the_witness_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fcntl
        import os

        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        monkeypatch.setattr(ledger, "HOOK_LOCK_TIMEOUT", 0.1)
        monkeypatch.setattr(claude_code, "_probe_argv", lambda pid: "claude")
        raw = '{"prompt": "typed by hand", "session_id": "s-locked"}'
        fd = os.open(tmp_path / "ledger.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            assert claude_code.record_turn(raw, {"CLAUDE_PID": "999"}) is None
            assert claude_code.session_close({"CLAUDE_CODE_SESSION_ID": "s-locked"}) is None
        finally:
            os.close(fd)
        assert list(ledger.iter_lines(tmp_path)) == []
        # No state was cached for the skipped turn, so the next one is turn 1.
        line = claude_code.record_turn(raw, {"CLAUDE_PID": "999"})
        assert line is not None and line.turn == 1
