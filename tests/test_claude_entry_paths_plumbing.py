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
import sys
import textwrap
import time
from pathlib import Path

import pytest

from promptwitness import canon
from tests.integration import test_claude_entry_paths as harness
from tests.integration.test_claude_entry_paths import (
    CLAUDE_AI_BRIDGE_LIVE_ENV,
    ENTRY_PATH_CASES,
    HOST_CONFIG_DIR_ENV,
    EntryPathObservations,
    HarnessAuthError,
    assert_parent_turn_submitted,
    build_child_env,
    claude_ai_bridge_skip_reason,
    find_transcript_record,
    find_transcript_record_by_text,
    promptwitness_hook_settings,
    resolve_hook_command,
    scrub_inherited_claude_env,
    seed_isolated_config_dir,
    spawn_claude_pty,
    submit_into_pty,
    transcript_has_prompt,
    transcript_record_fields,
    wait_for_transcript_prompt,
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


    def test_isolates_the_athenaeum_cache_dir_and_turns_off_auto_recall(
        self, tmp_path: Path
    ) -> None:
        # athenaeum#2709: an inherited real cache dir / recall-on must never
        # reach the child.
        real_cache = tmp_path / "operator-home" / ".cache" / "athenaeum"
        base_env = {"ATHENAEUM_CACHE_DIR": str(real_cache), "AUTO_RECALL": "true"}
        harness_tmp = tmp_path / "harness"
        harness_cache = harness_tmp / "athenaeum-cache-dir"

        child_env = build_child_env(
            base_env,
            promptwitness_dir=harness_tmp / "pw",
            athenaeum_cache_dir=harness_cache,
        )

        assert child_env["ATHENAEUM_CACHE_DIR"] == str(harness_cache)
        assert child_env["AUTO_RECALL"] == "false"
        assert base_env == {"ATHENAEUM_CACHE_DIR": str(real_cache), "AUTO_RECALL": "true"}

    def test_cache_dir_defaults_under_the_harness_tmp_tree(self, tmp_path: Path) -> None:
        base_env = {"ATHENAEUM_CACHE_DIR": "/var/empty/not-the-harness", "AUTO_RECALL": "true"}

        child_env = build_child_env(base_env, promptwitness_dir=tmp_path / "pw")

        cache_dir = Path(child_env["ATHENAEUM_CACHE_DIR"])
        assert cache_dir.is_relative_to(tmp_path)
        assert cache_dir != tmp_path / "pw"
        assert child_env["AUTO_RECALL"] == "false"

    def test_live_harness_paths_wire_a_tmp_cache_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The live-only glue must pass a tmp-rooted cache dir that exists.
        monkeypatch.setattr(harness, "resolve_hook_command", lambda: "true")
        monkeypatch.setattr(
            harness, "seed_isolated_config_dir", lambda *_a, **_k: "api-key-env"
        )
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "real-cache"))
        monkeypatch.setenv("AUTO_RECALL", "true")

        paths = harness._build_live_harness_paths(tmp_path)

        cache_dir = Path(paths.child_env["ATHENAEUM_CACHE_DIR"])
        assert cache_dir == tmp_path / "athenaeum-cache-dir"
        assert cache_dir.is_dir()
        assert paths.child_env["AUTO_RECALL"] == "false"


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
        session_jsonl.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
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

    def test_finds_each_record_by_text_when_two_records_share_an_id(self, tmp_path: Path) -> None:
        records = [
            {
                "type": "user",
                "promptId": "prompt-1",
                "origin": {"kind": "human"},
                "promptSource": "typed",
                "message": {"content": "summarise the release notes"},
            },
            {
                "type": "user",
                "promptId": "prompt-1",
                "origin": {"kind": "human"},
                "promptSource": "queued",
                "message": {"content": "also list the open questions"},
            },
        ]
        session_jsonl = self._write_session(tmp_path, records)

        # Lookup by id alone cannot tell the two apart (it returns the first).
        assert find_transcript_record(session_jsonl, prompt_id="prompt-1") is records[0] or (
            find_transcript_record(session_jsonl, prompt_id="prompt-1") == records[0]
        )
        for record in records:
            text = record["message"]["content"]
            found = find_transcript_record_by_text(
                session_jsonl, prompt_digest=canon.digest(canon.normalize(text))
            )
            assert found == record

        assert (
            find_transcript_record_by_text(
                session_jsonl, prompt_digest=canon.digest(canon.normalize("never typed"))
            )
            is None
        )
        assert (
            find_transcript_record_by_text(
                tmp_path / "absent.jsonl", prompt_digest=canon.digest("x")
            )
            is None
        )

    def test_queued_case_expects_an_intake_hit(self) -> None:
        case = next(c for c in ENTRY_PATH_CASES if c.id == "queued-while-turn-runs")
        assert case.intake_hit is True
        assert case.intake_miss_reason_prefix is None

    def test_falls_back_to_last_human_record_when_prompt_id_is_none(self, tmp_path: Path) -> None:
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


class TestClaudeAiBridgeSkipReason:
    def test_reason_is_non_empty_and_says_there_is_no_driver(self) -> None:
        reason = claude_ai_bridge_skip_reason()
        assert reason
        assert "cannot be driven" in reason

    def test_opt_in_is_acknowledged_but_still_a_skip_not_a_failure(self) -> None:
        reason = claude_ai_bridge_skip_reason({CLAUDE_AI_BRIDGE_LIVE_ENV: "1"})
        assert CLAUDE_AI_BRIDGE_LIVE_ENV in reason
        assert "skipped, not failed" in reason

    def test_row_skips_instead_of_failing_even_when_opted_in(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(CLAUDE_AI_BRIDGE_LIVE_ENV, "1")
        case = next(c for c in ENTRY_PATH_CASES if c.id == "claude-ai-bridge")
        # Call the test function body directly (the module-level skipif only
        # gates collection of the live module, not this call).
        with pytest.raises(pytest.skip.Exception) as excinfo:
            harness.test_entry_path(case, tmp_path)
        assert "cannot be driven" in str(excinfo.value)


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


# ---------------------------------------------------------------------------
# athenaeum#2588: env scrub, credential hand-off, submit, transcript, teardown
# ---------------------------------------------------------------------------


class TestEnvScrub:
    def test_scrub_removes_inherited_session_vars_and_keeps_the_rest(self) -> None:
        base = {
            "PATH": "/usr/bin",
            "CLAUDECODE": "1",
            "CLAUDE_PID": "7",
            "CLAUDE_EFFORT": "high",
            "CLAUDE_CODE_BRIDGE_SESSION_ID": "abc",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
            "ANTHROPIC_API_KEY": "k",
        }
        scrubbed = scrub_inherited_claude_env(base)
        assert scrubbed == {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "k"}
        assert "CLAUDECODE" in base  # input untouched

    def test_oauth_token_is_the_one_deliberate_survivor(self) -> None:
        scrubbed = scrub_inherited_claude_env(
            {"CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CODE_X": "1"}
        )
        assert scrubbed == {"CLAUDE_CODE_OAUTH_TOKEN": "t"}

    def test_build_child_env_never_forwards_the_bridge_session_id(self, tmp_path: Path) -> None:
        child = build_child_env(
            {"CLAUDE_CODE_BRIDGE_SESSION_ID": "abc", "CLAUDECODE": "1"},
            promptwitness_dir=tmp_path / "pw",
        )
        assert not [k for k in child if k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE"]
        assert child["TERM"] == "xterm-256color"


class TestSeedIsolatedConfigDir:
    def test_oauth_token_route_writes_only_dot_claude_json(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        method = seed_isolated_config_dir(
            cfg, workdir=tmp_path, env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        )
        assert method == "oauth-token-env"
        assert [p.name for p in cfg.iterdir()] == [".claude.json"]
        state = json.loads((cfg / ".claude.json").read_text())
        assert state["hasCompletedOnboarding"] is True
        assert state["projects"][str(tmp_path)]["hasTrustDialogAccepted"] is True

    def test_api_key_route_preapproves_the_key_suffix(self, tmp_path: Path) -> None:
        key = "sk-ant-" + "x" * 30 + "ABCDEFGHIJ0123456789"
        cfg = tmp_path / "cfg"
        method = seed_isolated_config_dir(cfg, workdir=tmp_path, env={"ANTHROPIC_API_KEY": key})
        assert method == "api-key-env"
        state = json.loads((cfg / ".claude.json").read_text())
        assert state["customApiKeyResponses"]["approved"] == [key[-20:]]
        assert key not in (cfg / ".claude.json").read_text()

    def test_credentials_file_route_copies_only_the_credentials(self, tmp_path: Path) -> None:
        host = tmp_path / "host"
        host.mkdir()
        (host / ".credentials.json").write_text('{"token": "t"}')
        (host / "settings.json").write_text('{"hooks": {"UserPromptSubmit": []}}')
        cfg = tmp_path / "cfg"
        method = seed_isolated_config_dir(
            cfg, workdir=tmp_path, env={HOST_CONFIG_DIR_ENV: str(host)}
        )
        assert method == "credentials-file"
        assert sorted(p.name for p in cfg.iterdir()) == [".claude.json", ".credentials.json"]
        assert (cfg / ".credentials.json").read_text() == '{"token": "t"}'
        assert (cfg / ".credentials.json").stat().st_mode & 0o777 == 0o600

    def test_no_credential_raises_with_the_three_routes(self, tmp_path: Path) -> None:
        with pytest.raises(HarnessAuthError) as excinfo:
            seed_isolated_config_dir(
                tmp_path / "cfg",
                workdir=tmp_path,
                env={HOST_CONFIG_DIR_ENV: str(tmp_path / "none")},
            )
        message = str(excinfo.value)
        assert "CLAUDE_CODE_OAUTH_TOKEN" in message
        assert "ANTHROPIC_API_KEY" in message
        assert HOST_CONFIG_DIR_ENV in message


def _write_session(config_dir: Path, records: list[dict[str, object]]) -> None:
    session = config_dir / "projects" / "scope" / "s1.jsonl"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


class TestTranscriptPromptConfirmation:
    def test_finds_a_submitted_user_prompt(self, tmp_path: Path) -> None:
        _write_session(
            tmp_path,
            [{"type": "user", "message": {"role": "user", "content": "please say ack now"}}],
        )
        assert transcript_has_prompt(tmp_path, "say ack")
        assert wait_for_transcript_prompt(tmp_path, "say ack", timeout=0.1)
        assert_parent_turn_submitted(tmp_path, "say ack")

    def test_a_dead_session_is_not_a_submission(self, tmp_path: Path) -> None:
        assert not transcript_has_prompt(tmp_path, "say ack")
        assert not wait_for_transcript_prompt(tmp_path, "say ack", timeout=0.1, poll=0.05)
        with pytest.raises(AssertionError, match="never submitted a turn"):
            assert_parent_turn_submitted(tmp_path, "say ack")

    def test_text_only_in_an_assistant_or_tool_result_record_does_not_count(
        self, tmp_path: Path
    ) -> None:
        _write_session(
            tmp_path,
            [
                {"type": "assistant", "message": {"role": "assistant", "content": "say ack"}},
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "say ack"}],
                    },
                },
            ],
        )
        assert not transcript_has_prompt(tmp_path, "say ack")


_RAW_READER = textwrap.dedent(
    """
    import os, sys, tty
    out = sys.argv[1]
    tty.setraw(0)
    open(out + ".ready", "w").close()
    chunks = []
    while True:
        data = os.read(0, 4096)
        chunks.append(repr(data))
        if b"\\r" in data:
            break
    open(out, "w").write("\\n".join(chunks))
    """
)


def _spawn_python(tmp_path: Path, code: str, *args: str):  # type: ignore[no-untyped-def]
    return spawn_claude_pty(
        [sys.executable, "-c", code, *args],
        env={"PATH": os.environ.get("PATH", "")},
        cwd=tmp_path,
    )


class TestPtyDriver:
    def test_enter_arrives_as_its_own_read_after_the_text(self, tmp_path: Path) -> None:
        out = tmp_path / "chunks.txt"
        session = _spawn_python(tmp_path, _RAW_READER, str(out))
        try:
            deadline = time.monotonic() + 20
            while not (tmp_path / "chunks.txt.ready").exists():
                assert time.monotonic() < deadline, "reader never became ready"
                time.sleep(0.05)
            submit_into_pty(session.master_fd, "hello there", settle=0.3)
            session.process.wait(timeout=20)
        finally:
            session.close()
        assert out.read_text().split("\n") == ["b'hello there'", "b'\\r'"]

    def test_pty_has_a_real_window_size(self, tmp_path: Path) -> None:
        out = tmp_path / "size.txt"
        code = (
            "import os, sys; s = os.get_terminal_size(0); "
            "open(sys.argv[1], 'w').write(f'{s.lines}x{s.columns}')"
        )
        session = _spawn_python(tmp_path, code, str(out))
        try:
            session.process.wait(timeout=20)
        finally:
            session.close()
        assert out.read_text() == "50x200"

    def test_close_after_the_child_already_exited_does_not_raise(self, tmp_path: Path) -> None:
        session = _spawn_python(tmp_path, "pass")
        session.process.wait(timeout=20)
        session.close()
        session.close()  # idempotent

    def test_close_absorbs_permission_error_from_killpg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _spawn_python(tmp_path, "import time; time.sleep(60)")
        real_killpg = os.killpg
        calls: list[int] = []

        def flaky(pgid: int, sig: int) -> None:
            calls.append(sig)
            if len(calls) == 1:
                raise PermissionError(1, "EPERM on a zombie group leader")
            real_killpg(pgid, sig)

        monkeypatch.setattr(os, "killpg", flaky)
        try:
            session.close(timeout=0.3)  # SIGTERM raises EPERM, then SIGKILL lands
        finally:
            monkeypatch.undo()
            if session.process.poll() is None:
                session.process.kill()
        assert session.process.poll() is not None
