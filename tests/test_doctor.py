# SPDX-License-Identifier: Apache-2.0
"""``athenaeum doctor`` (issue athenaeum#2298).

Covers every acceptance criterion:

- AC1: each of the seven named checks is produced by :func:`run_checks`.
- AC2: a failed check's rendered report names what is wrong AND the fix,
  and the process exit code is non-zero.
- AC3: a test matrix breaks one link at a time and asserts (a) non-zero
  exit and (b) the rendered report names that specific link; a healthy
  fixture exits zero.
- AC4: the counter-example — a hook fully registered in ``settings.json``
  whose target script does not exist on disk — fails the hooks check; a
  shallow "present in settings" check would wrongly pass this.
- AC5: no writes anywhere (asserted directly on the knowledge root's file
  set and mtimes), and the offline/network-failure path for the
  package-version check is "skip", never "fail", and never flips the exit
  code.

Every fixture path is ``tmp_path``-derived (public-safe-lint: no home-
directory literals), and no fixture command references a real GitHub issue
number (docs-structure hygiene does not apply to tests, but there is no
reason to invite the question).
"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from athenaeum import doctor
from athenaeum.cli import main as cli_main


def _run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _fake_fetch_matching(version: str):
    def _fetch(url: str, timeout: float) -> bytes:
        return json.dumps({"info": {"version": version}}).encode("utf-8")

    return _fetch


def _fake_fetch_offline(url: str, timeout: float) -> bytes:
    raise OSError("network unreachable (test stub)")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _settings_with_hook(script_path: Path) -> dict:
    return {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{script_path} 2>/dev/null || true",
                            "timeout": 15,
                        }
                    ]
                }
            ]
        }
    }


def _pin_llm_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test that resolves ``check_key_mode``'s provider must pin this
    (issue athenaeum#2298 CI finding): ``resolve_provider``'s env > yaml >
    default chain means an AMBIENT ``ATHENAEUM_LLM_PROVIDER`` (or the
    per-knob ``ATHENAEUM_TOPIC_LLM_PROVIDER``) silently overrides whatever a
    test wrote to ``athenaeum.yaml``, or silently substitutes for the plain
    "neither is set" default this module's healthy fixture otherwise relies
    on. This is a pre-existing, documented hazard in this exact codebase —
    see ``tests/test_2112_text_or_truncated_sites.py``'s ``_pin_api_backend``
    fixture docstring and ``tests/test_tiers.py``'s
    ``test_thinking_only_none_stop_reason_still_raises`` comment, both of
    which name "a CI env exporting ATHENAEUM_LLM_PROVIDER=api" by exactly
    that description. Locally this container has neither env var set, so a
    test relying on the yaml value (or the bare default) alone passes here
    and can still flip on a CI runner that does export one — delete both,
    unconditionally, rather than assume either environment's ambient state.
    """
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ATHENAEUM_TOPIC_LLM_PROVIDER", raising=False)


@pytest.fixture
def healthy_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fully healthy install: knowledge root, cache dir with an fts5
    index, Claude config dir with the MCP server registered and one
    athenaeum hook whose target script exists, and an API key in the
    process env. Returns the four resolved paths as a dict.

    Pins the LLM-provider env (see :func:`_pin_llm_provider_env`) as a
    baseline for every test built on this fixture — a per-test override
    (several ``key_mode`` tests below also call it explicitly, after
    writing their own ``llm.provider`` into ``athenaeum.yaml``) re-pins the
    SAME two vars, which is harmless and documents the dependency at the
    call site that actually cares about it.
    """
    _pin_llm_provider_env(monkeypatch)
    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "wiki").mkdir(parents=True)
    (knowledge_root / ".git").mkdir()  # a healthy install keeps its history (athenaeum#3160)
    (knowledge_root / "athenaeum.yaml").write_text(
        "search_backend: fts5\n", encoding="utf-8"
    )

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    sqlite3.connect(cache_dir / "wiki-index.db").close()  # a real (empty) sqlite DB

    claude_dir = tmp_path / "claude-home"
    scripts_dir = tmp_path / "hooks" / "athenaeum"
    scripts_dir.mkdir(parents=True)
    hook_script = scripts_dir / "user-prompt-recall.sh"
    hook_script.write_text("#!/bin/sh\n", encoding="utf-8")

    _write_json(claude_dir / "settings.json", _settings_with_hook(hook_script))
    _write_json(
        claude_dir / ".claude.json",
        {"mcpServers": {"athenaeum": {"command": "athenaeum", "args": ["serve"]}}},
    )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")

    # issue athenaeum#2304: a fresh, never-used home dir so `check_schedule_state`
    # deterministically reports "not scheduled" regardless of the actual
    # runner's real home directory.
    home_dir = tmp_path / "schedule-home"

    return {
        "knowledge_root": knowledge_root,
        "cache_dir": cache_dir,
        "claude_dir": claude_dir,
        "home_dir": home_dir,
    }


def _run_healthy(healthy_install: dict, **overrides) -> list[doctor.CheckResult]:
    from athenaeum import __version__ as installed_version

    kwargs = {
        "cache_dir": healthy_install["cache_dir"],
        "claude_config_dir": healthy_install["claude_dir"],
        "fetch": _fake_fetch_matching(installed_version),
        "home": healthy_install["home_dir"],
    }
    kwargs.update(overrides)
    return doctor.run_checks(healthy_install["knowledge_root"], **kwargs)


# ---------------------------------------------------------------------------
# AC1 + AC3 healthy fixture: every named check present, exit code zero.
# ---------------------------------------------------------------------------


def test_healthy_install_exits_zero_and_reports_every_check(
    healthy_install: dict,
) -> None:
    results = _run_healthy(healthy_install)
    names = {r.name for r in results}
    # AC1: every named link is reported.
    assert names == {
        "package_version",
        "interpreter",
        "mcp",
        "hooks",
        "key_mode",
        "search_index",
        "last_compile",
        "schedule",
        "page_relations",
        "merge_rejected_with_targets",
        "removed_config_keys",
        "unmarked_vault_locations",
        "undeclared_vault_data",
        "storage_history",
        "multi_target_divergence",
        "raw_values_in_history",
    }
    failed = [r for r in results if r.status == "fail"]
    assert not failed, failed
    assert doctor.exit_code(results) == 0


def test_healthy_install_cli_exits_zero(healthy_install: dict, monkeypatch) -> None:
    from athenaeum import __version__ as installed_version

    monkeypatch.setattr(doctor, "_default_fetch", _fake_fetch_matching(installed_version))
    rc, out = _run_cli(
        [
            "doctor",
            "--path",
            str(healthy_install["knowledge_root"]),
            "--cache-dir",
            str(healthy_install["cache_dir"]),
            "--claude-config-dir",
            str(healthy_install["claude_dir"]),
        ]
    )
    assert rc == 0
    assert "All checks passed." in out


# ---------------------------------------------------------------------------
# AC2 + AC3: the matrix. Each test breaks exactly one link and asserts the
# failure both exits non-zero AND names that specific link + a fix.
# ---------------------------------------------------------------------------


def test_mcp_not_registered_is_pending_and_names_fix(healthy_install: dict) -> None:
    (healthy_install["claude_dir"] / ".claude.json").unlink()
    results = _run_healthy(healthy_install)
    assert doctor.exit_code(results) == 0
    mcp = next(r for r in results if r.name == "mcp")
    assert mcp.status == "warn"
    assert "claude mcp add" in mcp.fix


def test_mcp_entry_missing_from_existing_claude_json(healthy_install: dict) -> None:
    _write_json(healthy_install["claude_dir"] / ".claude.json", {"mcpServers": {}})
    results = _run_healthy(healthy_install)
    mcp = next(r for r in results if r.name == "mcp")
    assert mcp.status == "warn"
    assert doctor.exit_code(results) == 0


def test_claude_json_present_but_unparseable_fails_mcp(healthy_install: dict) -> None:
    (healthy_install["claude_dir"] / ".claude.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    results = _run_healthy(healthy_install)
    mcp = next(r for r in results if r.name == "mcp")
    assert mcp.status == "fail"
    assert "JSON" in mcp.detail
    assert doctor.exit_code(results) == 1


# ---------------------------------------------------------------------------
# athenaeum#2594: real Claude Code (measured on 2.1.325, no CLAUDE_CONFIG_DIR
# set) writes the user-scope `claude mcp add --scope user` registration to
# the HOME-LEVEL .claude.json (a sibling of .claude/, not a child of it),
# not to <claude_config_dir>/.claude.json. The check must find it there too.
# ---------------------------------------------------------------------------


def test_home_level_claude_json_registration_is_ok(healthy_install: dict) -> None:
    """AC1: a registration at the home-level `.claude.json` -- the exact
    layout the README's `claude mcp add --scope user` step produces on
    current Claude Code -- passes, even though `<claude_config_dir>/.claude.json`
    does not exist at all.
    """
    (healthy_install["claude_dir"] / ".claude.json").unlink()
    home_dir = healthy_install["home_dir"]
    home_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        home_dir / ".claude.json",
        {"mcpServers": {"athenaeum": {"command": "athenaeum", "args": ["serve"]}}},
    )
    results = _run_healthy(healthy_install)
    mcp = next(r for r in results if r.name == "mcp")
    assert mcp.status == "ok"
    assert str(home_dir / ".claude.json") in mcp.detail
    assert doctor.exit_code(results) == 0


def test_neither_claude_json_location_still_fails_mcp(healthy_install: dict) -> None:
    """AC2 counter-example: with NO registration at either real location
    (`<claude_config_dir>/.claude.json` nor the home-level `.claude.json`),
    the check must still FAIL -- this is the genuine "not registered" case
    the home-level search must not paper over.
    """
    (healthy_install["claude_dir"] / ".claude.json").unlink()
    assert not (healthy_install["home_dir"] / ".claude.json").exists()
    results = _run_healthy(healthy_install)
    mcp = next(r for r in results if r.name == "mcp")
    assert mcp.status == "warn"
    assert "claude mcp add" in mcp.fix
    assert doctor.exit_code(results) == 0


def test_claude_config_dir_location_still_wins_when_both_absent_of_home(
    healthy_install: dict,
) -> None:
    """The pre-existing `<claude_config_dir>/.claude.json` location (older
    Claude Code behavior) keeps working unchanged when the home-level file
    does not exist -- no regression for users on that layout.
    """
    results = _run_healthy(healthy_install)
    mcp = next(r for r in results if r.name == "mcp")
    assert mcp.status == "ok"
    assert str(healthy_install["claude_dir"] / ".claude.json") in mcp.detail


def test_malformed_hooks_block_shapes_are_tolerated(healthy_install: dict) -> None:
    """Every malformed nesting level in ``hooks`` (issue athenaeum#2298) is
    tolerated (treated as "nothing registered"), never a crash."""
    _write_json(
        healthy_install["claude_dir"] / "settings.json",
        {
            "hooks": {
                "UserPromptSubmit": "not-a-list",
                "SessionStart": [{"not-hooks-key": []}],
                "SessionEnd": [{"hooks": "not-a-list"}],
                "PreCompact": [{"hooks": ["not-a-dict"]}],
                "Stop": [{"hooks": [{"command": ""}]}],
            }
        },
    )
    results = _run_healthy(healthy_install)
    hooks = next(r for r in results if r.name == "hooks")
    assert hooks.status == "warn"
    assert doctor.exit_code(results) == 0


def test_bare_command_hook_resolved_via_path(
    healthy_install: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook command naming a bare console script (no path component),
    with a leading ``nohup`` and an env assignment token — resolved via
    PATH, same as the shell would resolve it."""
    _write_json(
        healthy_install["claude_dir"] / "settings.json",
        {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "nohup FOO=bar athenaeum-claude-hook >/dev/null 2>&1 &",
                            }
                        ]
                    }
                ]
            }
        },
    )
    def _fake_which(name: str) -> str | None:
        return "/fake/bin/" + name if name == "athenaeum-claude-hook" else None

    monkeypatch.setattr("shutil.which", _fake_which)
    results = _run_healthy(healthy_install)
    hooks = next(r for r in results if r.name == "hooks")
    assert hooks.status == "ok"


def test_no_hooks_registered_is_pending_and_names_fix(healthy_install: dict) -> None:
    _write_json(healthy_install["claude_dir"] / "settings.json", {"hooks": {}})
    results = _run_healthy(healthy_install)
    assert doctor.exit_code(results) == 0
    hooks = next(r for r in results if r.name == "hooks")
    assert hooks.status == "warn"
    assert "sidecar.md" in hooks.fix


def test_hook_registered_but_target_missing_fails(
    healthy_install: dict, tmp_path: Path
) -> None:
    """AC4's counter-example: the hook IS fully present in settings.json —
    only its target file is gone. A check that merely confirms presence in
    settings would wrongly pass this; it must FAIL."""
    missing_script = tmp_path / "hooks" / "athenaeum" / "user-prompt-recall.sh"
    missing_script.unlink()  # the healthy fixture created it; remove it now
    assert not missing_script.exists()
    _write_json(
        healthy_install["claude_dir"] / "settings.json",
        _settings_with_hook(missing_script),
    )
    results = _run_healthy(healthy_install)
    assert doctor.exit_code(results) == 1
    hooks = next(r for r in results if r.name == "hooks")
    assert hooks.status == "fail"
    assert "user-prompt-recall.sh" in hooks.detail
    assert "UserPromptSubmit" in hooks.detail


def _settings_with_command(event: str, command: str) -> dict:
    return {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": command}]}]
        }
    }


def _session_id_capture_command(script: Path) -> str:
    """The shape athenaeum#3021 was found on: an env assignment holding a
    command substitution (whose words include the flag ``-r``), then a
    ``;``-joined, env-prefixed ``nohup bash <script>``."""
    return (
        "SID=$(jq -r '.session_id // empty'); "
        f"FOO=bar nohup bash {script}"
    )


def test_env_prefix_command_substitution_hook_passes(
    healthy_install: dict, tmp_path: Path
) -> None:
    """athenaeum#3021 AC1: the command-substitution shape, script present,
    passes. Before the fix the ``-r`` inside ``$(jq -r ...)`` was read as
    the hook path and the check falsely failed."""
    script = tmp_path / "hooks" / "athenaeum" / "session-end.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    _write_json(
        healthy_install["claude_dir"] / "settings.json",
        _settings_with_command("SessionEnd", _session_id_capture_command(script)),
    )
    results = _run_healthy(healthy_install)
    hooks = next(r for r in results if r.name == "hooks")
    assert hooks.status == "ok", hooks.detail


def test_env_prefix_command_substitution_hook_missing_names_script(
    healthy_install: dict, tmp_path: Path
) -> None:
    """athenaeum#3021 AC2/AC3: the same command with the script removed
    still fails, and names the script path -- never the flag ``-r``."""
    script = tmp_path / "hooks" / "athenaeum" / "session-end.sh"
    assert not script.exists()
    _write_json(
        healthy_install["claude_dir"] / "settings.json",
        _settings_with_command("SessionEnd", _session_id_capture_command(script)),
    )
    results = _run_healthy(healthy_install)
    assert doctor.exit_code(results) == 1
    hooks = next(r for r in results if r.name == "hooks")
    assert hooks.status == "fail"
    assert f"SessionEnd -> {script}" in hooks.detail
    assert "-> -" not in hooks.detail


@pytest.mark.parametrize(
    "template",
    [
        "{script}; echo done",
        "true && {script}",
        "nohup bash {script} & disown",
        "nohup bash {script} >/dev/null 2>&1 & disown",
        "SID=`jq -r .session_id`; nohup bash {script} &",
        "bash -c '{script} --flag'",
    ],
)
def test_compound_hook_commands_resolve_to_the_script(
    template: str, healthy_install: dict, tmp_path: Path
) -> None:
    """athenaeum#3021: each simple command of a ``;``/``&&``/``&``-joined
    hook command is resolved separately; the target is the athenaeum
    script, and its absence is reported by path."""
    script = tmp_path / "hooks" / "athenaeum" / "pre-compact-save.sh"
    command = template.format(script=script)
    assert doctor._resolve_hook_target(command) == str(script)

    _write_json(
        healthy_install["claude_dir"] / "settings.json",
        _settings_with_command("PreCompact", command),
    )
    hooks = next(r for r in _run_healthy(healthy_install) if r.name == "hooks")
    assert hooks.status == "fail"
    assert f"PreCompact -> {script}" in hooks.detail

    script.write_text("#!/bin/sh\n", encoding="utf-8")
    hooks = next(r for r in _run_healthy(healthy_install) if r.name == "hooks")
    assert hooks.status == "ok", hooks.detail


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Combined short flags carrying ``-c``: the operand is a command string.
        ("bash -lc 'athenaeum-claude-hook --event Stop'", "athenaeum-claude-hook"),
        ("sh -ec 'athenaeum-claude-hook'", "athenaeum-claude-hook"),
        # An option that takes a value never has that value read as the script.
        ("bash -o pipefail {script}", "{script}"),
        ("python3 -W ignore {script}", "{script}"),
        ("node -r ./setup.js {script}", "{script}"),
        # Inline code / a module: the interpreter is the only on-disk target.
        ("python3 -c 'import athenaeum'", "python3"),
        ("python3 -m athenaeum.claude_code_adapter", "python3"),
        # ``exec`` wraps the command rather than interpreting a script.
        ("exec {script}", "{script}"),
        # A ``#`` inside a word is not a comment (``shlex.split`` semantics).
        ("{script} --tag=#nightly", "{script}"),
    ],
)
def test_interpreter_options_resolve_to_the_real_target(
    command: str, expected: str, tmp_path: Path
) -> None:
    """Seer review on athenaeum#3021's PR: combined ``-lc`` flags, option
    values, and ``#`` inside a word must not be read as the hook target."""
    script = str(tmp_path / "hooks" / "athenaeum" / "session-start-recall.sh")
    assert doctor._resolve_hook_target(command.format(script=script)) == expected.format(
        script=script
    )


def test_interpreter_with_missing_script_is_not_masked_by_the_interpreter(
    healthy_install: dict, tmp_path: Path
) -> None:
    """``bash <script>`` resolves to the script, not to ``bash`` -- which
    always exists and would hide a missing script."""
    script = tmp_path / "hooks" / "athenaeum" / "stop-hook-validate.sh"
    _write_json(
        healthy_install["claude_dir"] / "settings.json",
        _settings_with_command("Stop", f"bash {script}"),
    )
    hooks = next(r for r in _run_healthy(healthy_install) if r.name == "hooks")
    assert hooks.status == "fail"
    assert str(script) in hooks.detail


@pytest.mark.parametrize(
    "command",
    [
        "SID=$(jq -r '.session_id // empty'); FOO=bar nohup bash -x",
        "-r",
        "nohup -- athenaeum",
        "bash -e -u",
        "jq -r .x | athenaeum-claude-hook --event SessionEnd",
        "env -i FOO=bar -r",
        "$(command -v athenaeum-claude-hook) --quiet",
    ],
)
def test_no_flag_is_ever_reported_as_a_hook_target(command: str) -> None:
    """athenaeum#3021 AC3: a word starting with ``-`` is never a target."""
    target = doctor._resolve_hook_target(command)
    assert target is None or not target.startswith("-")


def test_undeterminable_compound_command_warns_rather_than_fails(
    healthy_install: dict,
) -> None:
    """athenaeum#3021: when no simple command identifies the hook with
    confidence, the check warns ("could not determine ...") instead of
    reporting a false FAIL -- and the doctor's exit code stays zero."""
    command = "cd athenaeum-work && ./run hook; ./run other"
    assert doctor._resolve_hook_target(command) is None
    _write_json(
        healthy_install["claude_dir"] / "settings.json",
        _settings_with_command("SessionEnd", command),
    )
    results = _run_healthy(healthy_install)
    hooks = next(r for r in results if r.name == "hooks")
    assert hooks.status == "warn"
    assert "could not determine hook target from a compound shell command" in hooks.detail
    assert doctor.exit_code(results) == 0


def test_missing_target_still_fails_alongside_an_undeterminable_one(
    healthy_install: dict, tmp_path: Path
) -> None:
    """A real missing script is never downgraded to a warning because a
    different command was undeterminable."""
    missing = tmp_path / "hooks" / "athenaeum" / "rebuild-index.sh"
    _write_json(
        healthy_install["claude_dir"] / "settings.json",
        {
            "hooks": {
                "SessionEnd": [
                    {
                        "hooks": [
                            {"type": "command", "command": "cd athenaeum-work && ./run a; ./run b"}
                        ]
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": str(missing)}]}],
            }
        },
    )
    hooks = next(r for r in _run_healthy(healthy_install) if r.name == "hooks")
    assert hooks.status == "fail"
    assert f"Stop -> {missing}" in hooks.detail


def test_settings_json_present_but_unparseable_fails_hooks(
    healthy_install: dict,
) -> None:
    (healthy_install["claude_dir"] / "settings.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    results = _run_healthy(healthy_install)
    hooks = next(r for r in results if r.name == "hooks")
    assert hooks.status == "fail"
    assert doctor.exit_code(results) == 1


def test_no_api_key_is_pending_key_mode_and_names_fix(
    healthy_install: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_llm_provider_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # A `claude` binary exists (the no-binary wording is covered in
    # tests/test_2762_key_mode_login.py), so this is the plain missing-key case.
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")
    results = _run_healthy(healthy_install)
    assert doctor.exit_code(results) == 0
    key_mode = next(r for r in results if r.name == "key_mode")
    assert key_mode.status == "warn"
    assert "regex" in key_mode.detail
    assert "supplying-an-api-key" in key_mode.fix


def test_cached_config_env_key_satisfies_key_mode(
    healthy_install: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key need not be in the process env — a cached
    ``<cache_dir>/config.env`` (what the shipped hooks themselves cache
    into) is equally valid."""
    _pin_llm_provider_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (healthy_install["cache_dir"] / "config.env").write_text(
        "ANTHROPIC_API_KEY=sk-cached-not-real\n", encoding="utf-8"
    )
    results = _run_healthy(healthy_install)
    key_mode = next(r for r in results if r.name == "key_mode")
    assert key_mode.status == "ok"


def test_claude_cli_provider_with_missing_binary_is_pending_key_mode(
    healthy_install: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    (healthy_install["knowledge_root"] / "athenaeum.yaml").write_text(
        "search_backend: fts5\nllm:\n  provider: claude-cli\n", encoding="utf-8"
    )
    _pin_llm_provider_env(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.delenv("ATHENAEUM_CLAUDE_CLI_BIN", raising=False)
    results = _run_healthy(healthy_install)
    key_mode = next(r for r in results if r.name == "key_mode")
    assert key_mode.status == "warn"
    assert doctor.exit_code(results) == 0


def test_invalid_llm_provider_fails_key_mode(
    healthy_install: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    (healthy_install["knowledge_root"] / "athenaeum.yaml").write_text(
        "search_backend: fts5\nllm:\n  provider: not-a-real-provider\n",
        encoding="utf-8",
    )
    _pin_llm_provider_env(monkeypatch)
    results = _run_healthy(healthy_install)
    key_mode = next(r for r in results if r.name == "key_mode")
    assert key_mode.status == "fail"
    assert "athenaeum.yaml" in key_mode.fix
    assert doctor.exit_code(results) == 1


def test_claude_cli_provider_with_binary_present_ok(
    healthy_install: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    (healthy_install["knowledge_root"] / "athenaeum.yaml").write_text(
        "search_backend: fts5\nllm:\n  provider: claude-cli\n", encoding="utf-8"
    )
    _pin_llm_provider_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(doctor, "_claude_login_missing", lambda: False)
    results = _run_healthy(healthy_install)
    key_mode = next(r for r in results if r.name == "key_mode")
    assert key_mode.status == "ok"
    assert "claude_code_login" in key_mode.detail or "login" in key_mode.detail


def test_missing_search_index_is_pending_and_names_fix(healthy_install: dict) -> None:
    (healthy_install["cache_dir"] / "wiki-index.db").unlink()
    results = _run_healthy(healthy_install)
    assert doctor.exit_code(results) == 0
    index = next(r for r in results if r.name == "search_index")
    assert index.status == "warn"
    assert "reindex" in index.fix


def test_vector_backend_missing_index_is_pending(
    healthy_install: dict,
) -> None:
    pytest.importorskip("chromadb")  # without it the vector backend falls back to fts5
    (healthy_install["knowledge_root"] / "athenaeum.yaml").write_text(
        "search_backend: vector\n", encoding="utf-8"
    )
    results = _run_healthy(healthy_install)
    index = next(r for r in results if r.name == "search_index")
    assert index.status == "warn"
    assert doctor.exit_code(results) == 0


def test_vector_backend_with_built_index_ok(healthy_install: dict) -> None:
    (healthy_install["knowledge_root"] / "athenaeum.yaml").write_text(
        "search_backend: vector\n", encoding="utf-8"
    )
    from athenaeum.search import _VECTOR_DIR

    vector_dir = healthy_install["cache_dir"] / _VECTOR_DIR
    vector_dir.mkdir(parents=True)
    (vector_dir / "chroma.sqlite3").write_text("fake", encoding="utf-8")
    results = _run_healthy(healthy_install)
    index = next(r for r in results if r.name == "search_index")
    assert index.status == "ok"


def test_keyword_backend_needs_no_index(healthy_install: dict) -> None:
    (healthy_install["knowledge_root"] / "athenaeum.yaml").write_text(
        "search_backend: keyword\n", encoding="utf-8"
    )
    (healthy_install["cache_dir"] / "wiki-index.db").unlink()
    results = _run_healthy(healthy_install)
    index = next(r for r in results if r.name == "search_index")
    assert index.status == "ok"


def test_last_compile_reports_latest_ledger_timestamp(tmp_path: Path) -> None:
    from athenaeum.run_summary_log import default_run_summary_ledger_path

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    ledger_path = default_run_summary_ledger_path(cache_dir)
    ledger_path.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "total_secs": 1.0, "phases": {}})
        + "\n"
        + json.dumps({"ts": "2026-02-02T00:00:00+00:00", "total_secs": 2.0, "phases": {}})
        + "\n",
        encoding="utf-8",
    )
    result = doctor.check_last_compile(cache_dir)
    assert result.status == "ok"
    assert "2026-02-02" in result.detail


def test_last_compile_handles_record_with_no_timestamp(tmp_path: Path) -> None:
    from athenaeum.run_summary_log import default_run_summary_ledger_path

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    ledger_path = default_run_summary_ledger_path(cache_dir)
    ledger_path.write_text(
        json.dumps({"total_secs": 1.0, "phases": {}}) + "\n", encoding="utf-8"
    )
    result = doctor.check_last_compile(cache_dir)
    assert result.status == "ok"
    assert "timestamp unavailable" in result.detail


def test_last_compile_no_runs_yet(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result = doctor.check_last_compile(cache_dir)
    assert result.status == "ok"
    assert "no compile run recorded yet" in result.detail


# ---------------------------------------------------------------------------
# AC4 (issue athenaeum#2304): `athenaeum doctor` reports BOTH the scheduled
# and the unscheduled state — never "fail" either way.
# ---------------------------------------------------------------------------


def test_schedule_unscheduled_is_ok_not_fail(tmp_path: Path) -> None:
    result = doctor.check_schedule_state(home=tmp_path / "empty-home")
    assert result.status == "ok"
    assert "not scheduled" in result.detail


def test_schedule_scheduled_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from athenaeum import schedule

    _pin_llm_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    home = tmp_path / "home"
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    schedule.install(
        knowledge_root,
        home=home,
        loader=lambda argv, input_text=None: schedule.LoaderResult(0, "", ""),
        platform_override="launchd",
        config={},
        cache_dir=tmp_path / "cache",
        athenaeum_bin=Path("/opt/example-knowledge/venv/bin/athenaeum"),
    )

    result = doctor.check_schedule_state(home=home, platform_override="launchd")
    assert result.status == "ok"
    assert "scheduled" in result.detail
    assert "not scheduled" not in result.detail


# ---------------------------------------------------------------------------
# AC7 (issue athenaeum#2535): one-sided parent/child page relation count.
# ---------------------------------------------------------------------------


def test_page_relations_missing_wiki_dir_is_ok(tmp_path: Path) -> None:
    result = doctor.check_page_relations(tmp_path / "no-such-wiki")
    assert result.status == "ok"
    assert "0" in result.detail


def test_page_relations_empty_wiki_is_ok(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    result = doctor.check_page_relations(wiki)
    assert result.status == "ok"


def test_page_relations_one_sided_pair_fails_and_names_fix(tmp_path: Path) -> None:
    from athenaeum.models import render_frontmatter

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "orphan-child.md").write_text(
        render_frontmatter(
            {"uid": "orphan01", "type": "reference", "name": "Orphan Child", "parent": "ghost01"}
        )
        + "\nBody.\n",
        encoding="utf-8",
    )

    result = doctor.check_page_relations(wiki)
    assert result.status == "fail"
    assert "1" in result.detail
    assert "athenaeum relate" in result.fix


# ---------------------------------------------------------------------------
# Issue athenaeum#2546 AC4: merge_rejected_with pointing at no existing page.
# ---------------------------------------------------------------------------


def test_merge_rejected_with_missing_wiki_dir_is_ok(tmp_path: Path) -> None:
    result = doctor.check_merge_rejected_with_targets(tmp_path / "no-such-wiki")
    assert result.status == "ok"
    assert "0" in result.detail


def test_merge_rejected_with_resolved_pair_is_ok(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "a.md").write_text(
        '---\nname: A\nmerge_rejected_with: ["B"]\n---\nbody\n', encoding="utf-8"
    )
    (wiki / "b.md").write_text("---\nname: B\n---\nbody\n", encoding="utf-8")

    result = doctor.check_merge_rejected_with_targets(wiki)
    assert result.status == "ok"
    assert "0" in result.detail


def test_merge_rejected_with_dangling_entry_fails_and_names_both(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "a.md").write_text(
        '---\nname: A\nmerge_rejected_with: ["Ghost Page"]\n---\nbody\n', encoding="utf-8"
    )

    result = doctor.check_merge_rejected_with_targets(wiki)
    assert result.status == "fail"
    assert "1" in result.detail
    assert "Ghost Page" in result.detail
    assert "revoke-merge-rejection" in result.fix


def test_merge_rejected_with_resolves_via_alias_after_rename(tmp_path: Path) -> None:
    """A link naming B's OLD name must not be flagged once B carries that
    old name as an alias (athenaeum#2488 rename primitive)."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "a.md").write_text(
        '---\nname: A\nmerge_rejected_with: ["Old B Name"]\n---\nbody\n', encoding="utf-8"
    )
    (wiki / "b.md").write_text(
        '---\nname: New B Name\naliases: ["Old B Name"]\n---\nbody\n', encoding="utf-8"
    )

    result = doctor.check_merge_rejected_with_targets(wiki)
    assert result.status == "ok"


def test_interpreter_below_floor_fails(healthy_install: dict) -> None:
    result = doctor.check_interpreter_version((3, 12, 0))
    assert result.status == "fail"
    assert "3.13" in result.fix


def test_interpreter_at_floor_ok() -> None:
    result = doctor.check_interpreter_version((3, 13, 0))
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# AC5: offline / network-failure package-version check is "skip", not
# "fail", and never flips the exit code.
# ---------------------------------------------------------------------------


def test_package_version_offline_is_skip_not_fail(healthy_install: dict) -> None:
    results = _run_healthy(healthy_install, fetch=_fake_fetch_offline)
    version = next(r for r in results if r.name == "package_version")
    assert version.status == "skip"
    assert doctor.exit_code(results) == 0


def test_package_version_outdated_is_still_ok(healthy_install: dict) -> None:
    """Being behind the latest PyPI release is informational, not a broken
    link (orchestrator ruling: the PATCH digit moves on every merge to
    develop, so flagging drift as a FAILURE would make nearly every
    install "fail" doctor for no actionable reason)."""
    results = _run_healthy(healthy_install, fetch=_fake_fetch_matching("999.0.0"))
    version = next(r for r in results if r.name == "package_version")
    assert version.status == "ok"
    assert "999.0.0" in version.detail


# ---------------------------------------------------------------------------
# AC5: no writes anywhere.
# ---------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, float]:
    return {
        str(p.relative_to(root)): p.stat().st_mtime
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_doctor_makes_no_writes_anywhere(healthy_install: dict, tmp_path: Path) -> None:
    before_knowledge = _snapshot(healthy_install["knowledge_root"])
    before_cache = _snapshot(healthy_install["cache_dir"])
    before_claude = _snapshot(healthy_install["claude_dir"])

    results = _run_healthy(healthy_install)
    assert doctor.exit_code(results) == 0

    assert _snapshot(healthy_install["knowledge_root"]) == before_knowledge
    assert _snapshot(healthy_install["cache_dir"]) == before_cache
    assert _snapshot(healthy_install["claude_dir"]) == before_claude


def test_doctor_cli_makes_no_writes_on_broken_install(
    healthy_install: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same proof, but on a BROKEN install (several checks fail) driven
    through the real CLI entry point — a failing check must still never
    write, only report."""
    monkeypatch.setattr(doctor, "_default_fetch", _fake_fetch_offline)
    (healthy_install["claude_dir"] / ".claude.json").write_text("{bad", encoding="utf-8")
    before_knowledge = _snapshot(healthy_install["knowledge_root"])
    before_cache = _snapshot(healthy_install["cache_dir"])

    rc, out = _run_cli(
        [
            "doctor",
            "--path",
            str(healthy_install["knowledge_root"]),
            "--cache-dir",
            str(healthy_install["cache_dir"]),
            "--claude-config-dir",
            str(healthy_install["claude_dir"]),
            # issue athenaeum#2594: pin a fake, never-used home so the mcp
            # check's home-level `.claude.json` search does not read the
            # real runner's actual home directory.
            "--home",
            str(healthy_install["home_dir"]),
        ]
    )
    assert rc == 1
    assert "mcp" in out
    assert "fix:" in out

    assert _snapshot(healthy_install["knowledge_root"]) == before_knowledge
    assert _snapshot(healthy_install["cache_dir"]) == before_cache


# ---------------------------------------------------------------------------
# format_report / exit_code unit coverage.
# ---------------------------------------------------------------------------


def test_format_report_prints_detail_and_fix_for_failures() -> None:
    results = [
        doctor.CheckResult(name="mcp", status="fail", detail="broken", fix="do X"),
        doctor.CheckResult(name="interpreter", status="ok", detail="fine"),
    ]
    text = doctor.format_report(results)
    assert "[FAIL] mcp: broken" in text
    assert "fix: do X" in text
    assert "[OK] interpreter: fine" in text
    assert "1 check(s) failed." in text


def test_format_report_appends_fresh_install_note_when_any_check_fails() -> None:
    """issue athenaeum#2450 AC: a report with at least one FAIL ends with a
    note saying FAILs are expected on a fresh/not-yet-configured install
    and points back at the fix: lines above it."""
    results = [
        doctor.CheckResult(name="mcp", status="fail", detail="broken", fix="do X"),
        doctor.CheckResult(name="interpreter", status="ok", detail="fine"),
    ]
    text = doctor.format_report(results)
    lines = text.splitlines()
    assert "fresh or not-yet-configured install" in lines[-1]
    assert "fix" in lines[-1]
    # The note comes after the fix: line it refers to, and after the
    # failure-count summary line.
    assert lines.index("1 check(s) failed.") < len(lines) - 1


def test_format_report_omits_fresh_install_note_when_all_ok_or_skip() -> None:
    """issue athenaeum#2450 AC: no FAIL anywhere means no note at all."""
    results = [
        doctor.CheckResult(name="a", status="ok", detail="fine"),
        doctor.CheckResult(name="b", status="skip", detail="offline"),
    ]
    text = doctor.format_report(results)
    assert "fresh or not-yet-configured install" not in text
    assert text.splitlines()[-1] == "All checks passed."


def test_exit_code_zero_with_only_ok_and_skip() -> None:
    results = [
        doctor.CheckResult(name="a", status="ok", detail=""),
        doctor.CheckResult(name="b", status="skip", detail=""),
    ]
    assert doctor.exit_code(results) == 0


def test_default_claude_config_dir_honors_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom-claude"))
    assert doctor.default_claude_config_dir() == tmp_path / "custom-claude"


def test_default_claude_config_dir_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert doctor.default_claude_config_dir() == Path.home() / ".claude"


# ---------------------------------------------------------------------------
# Removed config keys: a WARN row, never a FAIL (issue athenaeum#2686).
# ---------------------------------------------------------------------------


def test_removed_config_key_warns_in_doctor(healthy_install: dict) -> None:
    cfg_path = healthy_install["knowledge_root"] / "athenaeum.yaml"
    existing = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    cfg_path.write_text(
        existing
        + "\nlibrarian:\n  delta:\n    enabled: true\n  relatedness_writer: false\n",
        encoding="utf-8",
    )
    results = _run_healthy(healthy_install)
    row = next(r for r in results if r.name == "removed_config_keys")
    assert row.status == "warn"
    assert "librarian.delta.enabled" in row.detail
    assert "librarian.relatedness_writer" in row.detail
    assert "always on" in row.detail
    # A warning is not a failure: the exit code stays zero.
    assert doctor.exit_code(results) == 0
    text = doctor.format_report(results)
    assert "[WARN] removed_config_keys:" in text
    assert "fix: delete the listed key(s)" in text
    assert "All checks passed, with 1 warning(s)." in text


def test_no_removed_config_keys_is_ok(healthy_install: dict) -> None:
    row = next(
        r for r in _run_healthy(healthy_install) if r.name == "removed_config_keys"
    )
    assert row.status == "ok"


# ---------------------------------------------------------------------------
# athenaeum#2906: a fresh install reports its four first-run checks as
# "setup pending" (WARN, exit 0); present-but-broken state still FAILs.
# ---------------------------------------------------------------------------

_FIRST_RUN = ("mcp", "hooks", "key_mode", "search_index")


def test_fresh_install_first_run_checks_are_pending_warn_not_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing set up at all: every first-run check is WARN carrying
    "setup pending" and a fix, no check is FAIL, and the exit code is 0."""
    _pin_llm_provider_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(doctor, "_claude_login_missing", lambda: False)
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "athenaeum.yaml").write_text("search_backend: vector\n", encoding="utf-8")
    # API provider + no key is the plain missing-key case.
    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "api")
    results = doctor.run_checks(
        knowledge,
        cache_dir=tmp_path / "cache",
        claude_config_dir=tmp_path / "claude",
        home=tmp_path / "home",
        fetch=_fake_fetch_offline,
    )
    by_name = {r.name: r for r in results}
    for name in _FIRST_RUN:
        assert by_name[name].status == "warn", by_name[name]
        assert "setup pending" in by_name[name].detail
        assert by_name[name].fix
    assert not [r for r in results if r.status == "fail"]
    assert doctor.exit_code(results) == 0
    text = doctor.format_report(results)
    assert "[WARN] mcp" in text
    assert "with 5 warning(s)" in text  # four first-run checks + storage_history (no repository)


def test_corrupt_fts5_index_still_fails_search_index(healthy_install: dict) -> None:
    (healthy_install["cache_dir"] / "wiki-index.db").write_bytes(b"garbage" * 64)
    results = _run_healthy(healthy_install)
    index = next(r for r in results if r.name == "search_index")
    assert index.status == "fail"
    assert "unreadable" in index.detail
    assert "reindex" in index.fix
    assert doctor.exit_code(results) == 1
