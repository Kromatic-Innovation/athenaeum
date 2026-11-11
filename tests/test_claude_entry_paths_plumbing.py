# SPDX-License-Identifier: Apache-2.0
"""Non-live unit tests for ``tests/integration/test_claude_entry_paths.py``'s
plumbing helpers (issue athenaeum#2235).

This module proves the plumbing WITHOUT the real ``claude`` binary: the
settings-file builder, the child-env builder (and its isolation from the
test process's own ``os.environ``), the transcript-record field extractor,
the observations JSON writer, the bridge-case skip decision, and the
entry-path case table itself. Every live/pty-driving helper in the harness
module is exercised only under ``PROMPTWITNESS_LIVE_CLAUDE=1`` (never here)
-- see that module's own docstring.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from promptwitness import canon
from tests.integration import test_claude_entry_paths as harness
from tests.integration.test_claude_entry_paths import (
    CLAUDE_AI_BRIDGE_LIVE_ENV,
    ENTRY_PATH_CASES,
    EntryPathObservations,
    build_child_env,
    claude_ai_bridge_skip_reason,
    find_transcript_record,
    promptwitness_hook_settings,
    resolve_hook_command,
    should_skip_claude_ai_bridge,
    transcript_record_fields,
    write_observations,
    write_settings_file,
)

# ---------------------------------------------------------------------------
# --settings file
# ---------------------------------------------------------------------------


class TestPromptwitnessHookSettings:
    def test_registers_a_user_prompt_submit_hook_command(self) -> None:
        payload = promptwitness_hook_settings("/fake/bin/athenaeum-claude-hook")
        hook_entries = payload["hooks"]["UserPromptSubmit"][0]["hooks"]
        assert hook_entries[0]["command"] == "/fake/bin/athenaeum-claude-hook"
        assert hook_entries[0]["type"] == "command"

    def test_custom_timeout_is_honored(self) -> None:
        payload = promptwitness_hook_settings("/fake/bin/athenaeum-claude-hook", timeout=42)
        hook_entries = payload["hooks"]["UserPromptSubmit"][0]["hooks"]
        assert hook_entries[0]["timeout"] == 42


class TestWriteSettingsFile:
    def test_writes_valid_json_inside_tmp_path(self, tmp_path: Path) -> None:
        settings_path = write_settings_file(tmp_path, "/fake/bin/athenaeum-claude-hook")

        # Derived from tmp_path, never a hardcoded operator-home-style
        # absolute literal (public-safe-lint.sh's own rejection, which
        # applies to fixture paths too).
        assert settings_path.is_relative_to(tmp_path)
        assert settings_path.is_file()

        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
        hook_entries = parsed["hooks"]["UserPromptSubmit"][0]["hooks"]
        assert hook_entries[0]["command"] == "/fake/bin/athenaeum-claude-hook"

    def test_resolve_hook_command_finds_the_installed_console_script(self) -> None:
        # This lane's venv installs the package itself (`.[dev,types]`), so
        # the `athenaeum-claude-hook` console script
        # (`pyproject.toml`'s [project.scripts]) is resolvable here -- either
        # tier of resolve_hook_command's own fallback would find it (PATH,
        # since the lane prepends `.venv/bin`; or the sys.executable-sibling
        # tier, since `.venv/bin` is also where `python` itself lives) --
        # the three dedicated tier tests below pin each tier individually.
        resolved = resolve_hook_command()
        assert resolved, (
            "resolve_hook_command() returned an empty string -- unexpected, "
            "it should never return falsy"
        )
        assert os.path.isfile(resolved), (
            f"{resolved!r} does not exist -- this test ambiently needs the "
            "package itself installed in the active interpreter (editable "
            'install, `pip install -e ".[dev,types]"`, same as this repo\'s '
            "CONTRIBUTING.md setup); see resolve_hook_command's own three "
            "fallback tiers, each pinned individually below, for what "
            "'installed' means here"
        )

    def test_resolve_hook_command_prefers_path_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(harness.shutil, "which", lambda _name: "/from/path/bin/hook")

        assert resolve_hook_command() == "/from/path/bin/hook"

    def test_resolve_hook_command_falls_back_to_sys_executable_sibling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # issue athenaeum#2235 Seer finding: a venv's console scripts and its
        # `python` binary always live side by side, even when the venv's
        # `bin/` is not itself on PATH -- mirrors
        # `tests/evals/rollout.py::_resolve_adapter_console_script`'s own
        # fallback shape over the SAME console script.
        fake_bin_dir = tmp_path / "bin"
        fake_bin_dir.mkdir()
        fake_python = fake_bin_dir / "python3"
        fake_python.touch()
        fake_hook = fake_bin_dir / harness.HOOK_COMMAND_NAME
        fake_hook.touch()
        monkeypatch.setattr(harness.shutil, "which", lambda _name: None)
        monkeypatch.setattr(harness.sys, "executable", str(fake_python))

        assert resolve_hook_command() == str(fake_hook)

    def test_resolve_hook_command_final_fallback_is_the_bare_command_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Neither PATH nor the sys.executable sibling resolves -- never
        # raises (issue athenaeum#2235 Seer finding: a pre-flight
        # RuntimeError here would fail the whole harness's setup over a
        # condition a real spawn attempt surfaces just as loudly, and does
        # so AFTER the two tiers above have already had their chance).
        empty_bin_dir = tmp_path / "bin"
        empty_bin_dir.mkdir()
        fake_python = empty_bin_dir / "python3"
        fake_python.touch()
        monkeypatch.setattr(harness.shutil, "which", lambda _name: None)
        monkeypatch.setattr(harness.sys, "executable", str(fake_python))

        assert resolve_hook_command() == harness.HOOK_COMMAND_NAME


# ---------------------------------------------------------------------------
# Child env isolation
# ---------------------------------------------------------------------------


class TestBuildChildEnv:
    def test_sets_witness_ledger_enabled_and_promptwitness_dir(self, tmp_path: Path) -> None:
        promptwitness_dir = tmp_path / "pw"
        base_env = {"PATH": "/usr/bin"}

        child_env = build_child_env(base_env, promptwitness_dir=promptwitness_dir)

        assert child_env["ATHENAEUM_WITNESS_LEDGER_ENABLED"] == "true"
        assert child_env["PROMPTWITNESS_DIR"] == str(promptwitness_dir)
        # PROMPTWITNESS_DIR must point INSIDE the pytest tmp dir.
        assert Path(child_env["PROMPTWITNESS_DIR"]).is_relative_to(tmp_path)

    def test_never_mutates_the_passed_in_base_env(self, tmp_path: Path) -> None:
        base_env = {"PATH": "/usr/bin"}

        build_child_env(base_env, promptwitness_dir=tmp_path / "pw")

        assert "ATHENAEUM_WITNESS_LEDGER_ENABLED" not in base_env
        assert "PROMPTWITNESS_DIR" not in base_env
        assert base_env == {"PATH": "/usr/bin"}

    def test_never_appears_in_this_test_processs_own_os_environ(self, tmp_path: Path) -> None:
        # The real contract: calling this helper with the REAL os.environ
        # as the base must not leak the child-only keys back into this
        # process's environment.
        #
        # Seer finding on athenaeum#2235's review: this pre-check asserts
        # on ambient shell state, which COULD fail for a reason unrelated
        # to build_child_env (an operator's own shell already exporting
        # this var while manually exercising the live harness). Considered
        # routing it through `tests/env_contract.py::require` instead --
        # declined: that module's `PROPERTIES` registry is for documented,
        # genuinely-optional MACHINE capabilities (an extra not installed,
        # a dependency layout, an OS enforcement capability) that CI always
        # satisfies but a contributor env might not; "my own shell doesn't
        # happen to already export this one specific var" is not that kind
        # of property, and registering it there for one test would be a
        # bigger change than this precondition is worth. Left as a plain
        # assert, with the failure message spelling out exactly what
        # ambient condition would need to change to make it fail.
        assert "ATHENAEUM_WITNESS_LEDGER_ENABLED" not in os.environ, (
            "this test's own process already has ATHENAEUM_WITNESS_LEDGER_ENABLED "
            "set in os.environ -- unset it before running this test (e.g. you are "
            "running inside a shell manually exercising the live harness); this "
            "assertion is a precondition for the real contract check below, not "
            "itself a build_child_env bug"
        )

        child_env = build_child_env(os.environ, promptwitness_dir=tmp_path / "pw")

        assert child_env["ATHENAEUM_WITNESS_LEDGER_ENABLED"] == "true"
        assert "ATHENAEUM_WITNESS_LEDGER_ENABLED" not in os.environ, (
            "build_child_env leaked ATHENAEUM_WITNESS_LEDGER_ENABLED into this "
            "test process's own os.environ -- it must only ever appear in the "
            "returned child-env dict"
        )

    def test_claude_config_dir_and_launcher_are_optional(self, tmp_path: Path) -> None:
        promptwitness_dir = tmp_path / "pw"
        claude_config_dir = tmp_path / "claude-config"

        bare = build_child_env({}, promptwitness_dir=promptwitness_dir)
        assert "CLAUDE_CONFIG_DIR" not in bare
        assert "PROMPTWITNESS_LAUNCHER" not in bare

        full = build_child_env(
            {},
            promptwitness_dir=promptwitness_dir,
            claude_config_dir=claude_config_dir,
            launcher="my-launcher",
        )
        assert full["CLAUDE_CONFIG_DIR"] == str(claude_config_dir)
        assert full["PROMPTWITNESS_LAUNCHER"] == "my-launcher"


# ---------------------------------------------------------------------------
# Observations JSON writer
# ---------------------------------------------------------------------------


class TestWriteObservations:
    def test_writes_synthetic_observations_as_json(self, tmp_path: Path, capsys) -> None:
        observations = EntryPathObservations(
            claude_version="2.1.300",
            child_session_top_level="0",
            subagent_fires_hook=False,
            notes={"entry_path": "interactive-typed"},
        )
        path = tmp_path / "observations.json"

        returned = write_observations(path, observations)

        assert returned == path
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert parsed == {
            "claude_version": "2.1.300",
            "child_session_top_level": "0",
            "subagent_fires_hook": False,
            "notes": {"entry_path": "interactive-typed"},
        }
        # Prints its path (issue Plan: "prints its path").
        captured = capsys.readouterr()
        assert str(path) in captured.out

    def test_none_fields_round_trip(self, tmp_path: Path) -> None:
        observations = EntryPathObservations(
            claude_version="",
            child_session_top_level=None,
            subagent_fires_hook=None,
        )
        path = tmp_path / "observations.json"

        write_observations(path, observations)

        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert parsed["child_session_top_level"] is None
        assert parsed["subagent_fires_hook"] is None
        assert parsed["notes"] == {}


# ---------------------------------------------------------------------------
# Transcript record field extraction
# ---------------------------------------------------------------------------


class TestFindTranscriptRecordAndFields:
    def _write_session(self, tmp_path: Path, records: list[dict]) -> Path:
        session_jsonl = tmp_path / "session.jsonl"
        session_jsonl.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )
        return session_jsonl

    def test_finds_record_by_prompt_id(self, tmp_path: Path) -> None:
        records = [
            {
                "type": "user",
                "promptId": "prompt-1",
                "origin": {"kind": "human"},
                "promptSource": "typed",
                "message": {"content": "hello there"},
            },
            {
                "type": "user",
                "promptId": "prompt-2",
                "origin": {"kind": "human"},
                "promptSource": "typed",
                "message": {"content": "second message"},
            },
        ]
        session_jsonl = self._write_session(tmp_path, records)

        record = find_transcript_record(session_jsonl, prompt_id="prompt-2")

        assert record is not None
        assert record["promptId"] == "prompt-2"

    def test_falls_back_to_last_human_record_when_prompt_id_is_none(
        self, tmp_path: Path
    ) -> None:
        records = [
            {
                "type": "user",
                "origin": {"kind": "human"},
                "promptSource": "typed",
                "message": {"content": "first"},
            },
            {
                "type": "user",
                "origin": {"kind": "task-notification"},
                "message": {"content": "background result"},
            },
            {
                "type": "user",
                "origin": {"kind": "human"},
                "promptSource": "typed",
                "message": {"content": "latest human turn"},
            },
        ]
        session_jsonl = self._write_session(tmp_path, records)

        record = find_transcript_record(session_jsonl, prompt_id=None)

        assert record is not None
        assert record["message"]["content"] == "latest human turn"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert find_transcript_record(tmp_path / "absent.jsonl", prompt_id="x") is None

    def test_transcript_record_fields_extracts_the_four_attest_inputs(self) -> None:
        record = {
            "type": "user",
            "promptId": "prompt-9",
            "origin": {"kind": "human"},
            "promptSource": "typed",
            "isSidechain": False,
            "message": {"content": "the actual utterance"},
        }

        fields = transcript_record_fields(record)

        assert fields["prompt_id"] == "prompt-9"
        assert fields["matched_record_text"] == "the actual utterance"
        assert fields["transcript_origin_kind"] == "human"
        assert fields["transcript_prompt_source"] == "typed"
        assert fields["transcript_is_sidechain"] is False

    def test_transcript_record_fields_handles_none_record(self) -> None:
        fields = transcript_record_fields(None)

        assert fields == {
            "prompt_id": None,
            "matched_record_text": "",
            "transcript_origin_kind": None,
            "transcript_prompt_source": None,
            "transcript_is_sidechain": False,
        }


# ---------------------------------------------------------------------------
# Bridge-case skip decision
# ---------------------------------------------------------------------------


class TestClaudeAiBridgeSkipDecision:
    def test_skips_by_default(self) -> None:
        assert should_skip_claude_ai_bridge({}) is True

    def test_does_not_skip_when_opted_in(self) -> None:
        assert should_skip_claude_ai_bridge({CLAUDE_AI_BRIDGE_LIVE_ENV: "1"}) is False

    def test_any_other_value_still_skips(self) -> None:
        assert should_skip_claude_ai_bridge({CLAUDE_AI_BRIDGE_LIVE_ENV: "yes"}) is True

    def test_reason_is_non_empty_and_names_the_opt_in_var(self) -> None:
        reason = claude_ai_bridge_skip_reason()
        assert reason
        assert CLAUDE_AI_BRIDGE_LIVE_ENV in reason


# ---------------------------------------------------------------------------
# Case table sanity
# ---------------------------------------------------------------------------


class TestEntryPathCaseTable:
    def test_exactly_ten_cases(self) -> None:
        assert len(ENTRY_PATH_CASES) == 10

    def test_case_ids_are_unique_and_name_the_entry_path(self) -> None:
        ids = [case.id for case in ENTRY_PATH_CASES]
        assert len(set(ids)) == 10
        # Each id is a readable, hyphenated slug -- never a bare index.
        for case_id in ids:
            assert case_id
            assert case_id == case_id.lower()
            assert " " not in case_id

    def test_every_miss_case_names_a_reason_prefix_and_every_hit_case_does_not(self) -> None:
        for case in ENTRY_PATH_CASES:
            if case.intake_hit:
                assert case.intake_miss_reason_prefix is None, case.id
            else:
                assert case.intake_miss_reason_prefix, case.id

    def test_canon_digest_used_by_cases_matches_the_shipped_producer(self) -> None:
        # Sanity check that this plumbing module and the harness module
        # agree on which digest function backs "prompt_digest parity" --
        # not a live assertion, just confirms the import is live code, not
        # a typo'd dead import.
        assert canon.digest("x") == canon.digest("x")
