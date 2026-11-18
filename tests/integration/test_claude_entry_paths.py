# SPDX-License-Identifier: Apache-2.0
"""Live entry-path integration harness for the Claude Code witness hook
(issue athenaeum#2235, split from athenaeum#2161, epic athenaeum#2144).

Drives the REAL ``claude`` binary across every entry path in the table
below and asserts the promptwitness hook's (athenaeum#2149) classification
plus the witness ledger line it writes, and the athenaeum intake verdict
(athenaeum#2158, :func:`athenaeum.witness_attestation.attest`) over the
produced transcript + ledger.

**This module is never run here.** It is marked :func:`pytest.mark.live`
(registered, NOT in ``addopts``'s deselect list -- see ``pyproject.toml``)
plus a module-level ``skipif`` gated on ``PROMPTWITNESS_LIVE_CLAUDE=1``, so
the default CI job COLLECTS and SKIPS every case here rather than hiding
them (the mistake a ``rollout`` marker would have made). Running this
against the real binary, the claude.ai bridge case, recording observations
into ``docs/measurements/``, and fixing any adapter behaviour a live run
turns up are ALL athenaeum#2161 -- the operator step this harness exists to
make repeatable, not something this lane does.

**Verified vs assumed (read this before running athenaeum#2161).** Every
row's expected ``prompt_kind``/``channel`` is taken directly from
``promptwitness.harness.claude_code`` and ``docs/design/witness-ledger.md``
-- those are VERIFIED against the shipped classifier. What is NOT verified
(no real ``claude`` binary was ever run to build this module) is whether
each driver below actually PRODUCES the entry path it claims to -- the
exact TUI idle/ready signal, whether a real terminal's bracketed-paste
sequence reaches the hook the same way over a raw pty, whether the slash
command named in :data:`_SLASH_COMMAND_LINE` accepts freeform
``<command-args>``, and whether a sub-agent/background-task prompt
actually fires the hook at all (the issue's own open question -- see rows
``subagent-agent-tool`` and ``background-task-notification`` below, and
:func:`EntryPathObservations` which exists specifically to record what the
live run found). A case failing for one of these reasons is an
athenaeum#2161 finding about the DRIVER, not necessarily about the
adapter -- read the assertion that failed before filing anything against
``claude_code_adapter.py`` or ``promptwitness.harness.claude_code``.

**Authenticated, hook-isolated sessions (athenaeum#2588).** Every interactive
row runs under an ISOLATED ``CLAUDE_CONFIG_DIR`` (so the host's own
``settings.json`` hooks are never loaded next to the harness's ``--settings``
hook, and exactly one hook fires per turn). An isolated dir starts logged out
and at onboarding, so :func:`seed_isolated_config_dir` hands it a login
through one documented route, tried in this order (the first present wins):

1. ``CLAUDE_CODE_OAUTH_TOKEN`` in the environment -- a long-lived token from
   ``claude setup-token``. **The route to use on macOS**, where the host login
   lives in the Keychain, not in a file this harness could copy.
2. ``ANTHROPIC_API_KEY`` in the environment (its last 20 characters are
   pre-approved in the isolated ``.claude.json`` so no key-approval dialog
   blocks the TUI).
3. ``<host config dir>/.credentials.json`` copied byte-for-byte (the Linux
   credential-file layout). The host dir is
   :data:`HOST_CONFIG_DIR_ENV`, else ``CLAUDE_CONFIG_DIR``, else ``~/.claude``.

ONLY that credential (token env var, key env var, or the one credentials file)
crosses into the isolated dir -- never ``settings.json``, hooks, or any other
host file; the seeded ``.claude.json`` carries just onboarding-complete and
folder-trust flags. With none of the three present the live run fails loudly
before spawning anything rather than producing a dead, logged-out session.
Managed (system-wide) settings, if the host has them, still load; the harness
cannot isolate those.

**Prompt submission (athenaeum#2588).** The previous driver wrote the text and
``\\r`` back to back; ``\\r``, ``\\n`` and CSI ``13u`` all failed to submit
on a live host. The best-justified cause is that the TUI's input layer treats
one stdin read carrying several characters including a carriage return as a
PASTE (the CR becomes a newline inside the box), so Enter must arrive as its
own read after a settle delay (:func:`submit_into_pty`); the pty also now has a
real window size (:func:`spawn_claude_pty`) and ``TERM`` is set. None of that
is provable offline, so submission is never assumed: :func:`submit_and_confirm`
re-presses Enter until the session TRANSCRIPT contains a user record carrying
the submitted prompt, and raises (with the tail of the screen) if it never
does. Every row therefore asserts a transcript line for its own prompt, and a
row that expects NO ledger line additionally asserts its parent session
submitted a turn (:func:`assert_parent_turn_submitted`), so a dead session
cannot pass. If Enter still does not submit on a live host, that is an
athenaeum#2161 driver finding, now reported as such rather than as a pass.

**Environment scrub (athenaeum#2588).** ``CLAUDE_CODE_*``, ``CLAUDECODE``,
``CLAUDE_PID`` and ``CLAUDE_EFFORT`` inherited from an enclosing Claude Code
session are removed from the child env (:func:`scrub_inherited_claude_env`),
otherwise an inherited ``CLAUDE_CODE_BRIDGE_SESSION_ID`` classifies every row
as ``bridge``. ``CLAUDE_CODE_OAUTH_TOKEN`` is the one deliberate survivor.

**Bridge row.** Has no local driver; it is always SKIPPED with a recorded
reason (visible under ``pytest -rs``), never a hard failure.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from athenaeum.transcript_verify import (
    _non_tool_result_text,
    _parse_session_text,
    _user_authored_text,
)
from athenaeum.witness_attestation import AttestationIndex, AttestationOutcome, attest
from promptwitness import canon, ledger
from promptwitness.harness import claude_code

__all__ = [
    "PROMPTWITNESS_LIVE_CLAUDE_ENV",
    "CLAUDE_AI_BRIDGE_LIVE_ENV",
    "HOOK_COMMAND_NAME",
    "EntryPathCase",
    "ENTRY_PATH_CASES",
    "EntryPathObservations",
    "PtySession",
    "promptwitness_hook_settings",
    "write_settings_file",
    "resolve_hook_command",
    "build_child_env",
    "scrub_inherited_claude_env",
    "seed_isolated_config_dir",
    "HarnessAuthError",
    "HOST_CONFIG_DIR_ENV",
    "submit_into_pty",
    "submit_and_confirm",
    "transcript_has_prompt",
    "wait_for_transcript_prompt",
    "assert_parent_turn_submitted",
    "write_observations",
    "spawn_claude_pty",
    "type_into_pty",
    "send_bracketed_paste",
    "read_pty_until_idle",
    "find_transcript_record",
    "find_transcript_record_by_text",
    "transcript_record_fields",
    "claude_ai_bridge_skip_reason",
]

# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

#: Set to ``"1"`` to actually spawn the real ``claude`` binary. Never set by
#: this lane, never set in CI -- the live run belongs to athenaeum#2161.
PROMPTWITNESS_LIVE_CLAUDE_ENV = "PROMPTWITNESS_LIVE_CLAUDE"

#: The claude.ai-bridge row cannot be driven from a local pty at all (no
#: local process carries ``CLAUDE_CODE_BRIDGE_SESSION_ID`` on its own), so it
#: is always skipped with a recorded reason (athenaeum#2588). Setting this is
#: acknowledged in that reason but does not change the outcome.
CLAUDE_AI_BRIDGE_LIVE_ENV = "PROMPTWITNESS_LIVE_CLAUDE_AI_BRIDGE"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get(PROMPTWITNESS_LIVE_CLAUDE_ENV) != "1",
        reason=(
            f"live claude-binary harness (athenaeum#2235); set "
            f"{PROMPTWITNESS_LIVE_CLAUDE_ENV}=1 to run it -- the real run "
            "against the real binary is athenaeum#2161's operator step, "
            "never this lane's"
        ),
    ),
]

# ---------------------------------------------------------------------------
# Settings-file / child-env plumbing (unit-tested in
# tests/test_claude_entry_paths_plumbing.py -- these three functions never
# touch the real claude binary and never mutate os.environ).
# ---------------------------------------------------------------------------

#: The console script `pyproject.toml`'s ``[project.scripts]`` installs for
#: ``athenaeum.claude_code_adapter:main`` (issue athenaeum#1621). Resolved
#: via :func:`resolve_hook_command` rather than hardcoded, so a settings
#: file this harness writes always points at THIS venv's installed script.
HOOK_COMMAND_NAME = "athenaeum-claude-hook"

#: Matches the shape ``examples/claude-code/settings-snippet.json`` already
#: documents for ``UserPromptSubmit`` -- same ``hooks`` nesting, same
#: ``type``/``command``/``timeout`` keys -- so a reader can diff this
#: harness's generated file against that example by eye.
_DEFAULT_HOOK_TIMEOUT_SECONDS = 15


def promptwitness_hook_settings(
    hook_command: str, *, timeout: int = _DEFAULT_HOOK_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Build the ``--settings`` payload registering ONLY the promptwitness
    ``UserPromptSubmit`` hook (``hook_command``, normally
    :func:`resolve_hook_command`'s return value) -- no SessionStart/recall
    hooks from ``examples/claude-code/settings-snippet.json`` are included,
    since this harness asserts on the witness ledger, not on recall output.
    """
    return {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command,
                            "timeout": timeout,
                        }
                    ]
                }
            ]
        }
    }


def write_settings_file(
    tmp_path: Path, hook_command: str, *, timeout: int = _DEFAULT_HOOK_TIMEOUT_SECONDS
) -> Path:
    """Write :func:`promptwitness_hook_settings`'s payload to
    ``<tmp_path>/claude-settings.json`` and return its path.

    Always derived from *tmp_path* -- never a hardcoded operator-home-style
    absolute literal -- so this satisfies ``public-safe-lint.sh``'s
    absolute-path rejection even though it IS a synthetic fixture path.
    """
    payload = promptwitness_hook_settings(hook_command, timeout=timeout)
    settings_path = tmp_path / "claude-settings.json"
    settings_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return settings_path


def resolve_hook_command() -> str:
    """Resolve the installed :data:`HOOK_COMMAND_NAME` console script.

    Three tiers, matching ``tests/evals/rollout.py``'s
    ``_resolve_adapter_console_script`` precedent for resolving this SAME
    console script (deliberately NOT a new fallback shape): :func:`shutil.
    which` first (wherever the active venv's ``PATH`` entry puts it); then
    a sibling of the running interpreter (``sys.executable``'s own
    directory -- where a venv's console scripts and its ``python`` binary
    always live side by side, even on a ``PATH`` that does not include the
    venv's ``bin/`` directly -- Seer finding on athenaeum#2235's review:
    the athenaeum#2161 operator session will very plausibly invoke pytest
    without the venv's ``bin`` on ``PATH``, and this harness's hook
    registration must not hard-fail at setup just because of that); then
    the bare command name, so a spawn attempt fails loudly (a
    :class:`FileNotFoundError` from the ``claude`` subprocess call, or a
    clearly-broken hook command in the generated settings file) rather
    than this function raising at setup time over a condition the two
    earlier tiers already cover in every environment that actually matters.
    """
    found = shutil.which(HOOK_COMMAND_NAME)
    if found:
        return found
    sibling = Path(sys.executable).parent / HOOK_COMMAND_NAME
    if sibling.exists():
        return str(sibling)
    return HOOK_COMMAND_NAME


#: Env vars an enclosing Claude Code session exports (athenaeum#2588): any
#: ``CLAUDE_CODE_*`` name plus these exact ones. Left in the child env they
#: make it a *child session* of the enclosing one and, via
#: ``CLAUDE_CODE_BRIDGE_SESSION_ID``, classify every row as ``bridge``.
_INHERITED_CLAUDE_ENV_PREFIX = "CLAUDE_CODE_"
_INHERITED_CLAUDE_ENV_EXACT = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"})

#: The credential hand-off env var that survives the scrub on purpose.
_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
_API_KEY_ENV = "ANTHROPIC_API_KEY"

#: Env var naming the host config dir to copy ``.credentials.json`` from.
HOST_CONFIG_DIR_ENV = "PROMPTWITNESS_LIVE_CLAUDE_HOST_CONFIG_DIR"
_CREDENTIALS_FILENAME = ".credentials.json"


def scrub_inherited_claude_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """Copy of *base_env* without the enclosing session's ``CLAUDE_CODE_*`` /
    ``CLAUDECODE`` / ``CLAUDE_PID`` / ``CLAUDE_EFFORT`` variables.

    ``CLAUDE_CODE_OAUTH_TOKEN`` is kept: it is the documented credential
    hand-off (see the module docstring), not session state. *base_env* is
    never mutated.
    """
    return {
        key: value
        for key, value in base_env.items()
        if key == _OAUTH_TOKEN_ENV
        or not (key.startswith(_INHERITED_CLAUDE_ENV_PREFIX) or key in _INHERITED_CLAUDE_ENV_EXACT)
    }


def build_child_env(
    base_env: Mapping[str, str],
    *,
    promptwitness_dir: Path,
    claude_config_dir: Path | None = None,
    launcher: str | None = None,
    athenaeum_cache_dir: Path | None = None,
) -> dict[str, str]:
    """Build the env dict for the SPAWNED ``claude`` child only.

    Returns a new ``dict`` -- *base_env* (normally ``os.environ`` itself)
    is never mutated, so the operator's real witness ledger and config stay
    untouched by running this harness. Sets:

    - ``ATHENAEUM_WITNESS_LEDGER_ENABLED=true`` (:func:`athenaeum.
      claude_code_adapter._witness_ledger_enabled`'s exact-string gate,
      default off);
    - ``PROMPTWITNESS_DIR`` to *promptwitness_dir* (:func:`promptwitness.
      ledger.resolve_ledger_dir`'s override precedent);
    - ``CLAUDE_CONFIG_DIR`` to *claude_config_dir* when given, so the
      session transcript this harness needs to read
      (:func:`athenaeum.transcript_verify.default_projects_root`, issue
      athenaeum#723) lands inside the same tmp tree instead of the
      operator's real ``~/.claude``;
    - ``PROMPTWITNESS_LAUNCHER`` to *launcher* when given (athenaeum#2172,
      a free-text label only -- never affects classification);
    - ``ATHENAEUM_CACHE_DIR`` to *athenaeum_cache_dir*, defaulting to an
      ``athenaeum-cache-dir`` sibling of *promptwitness_dir* -- ALWAYS set,
      overriding any inherited value (athenaeum#2709). Without it the child's
      hook resolves the operator's REAL cache dir: it reads the real
      ``config.env`` and writes ``witness_hook_errors.jsonl``, hook-health
      records, recall telemetry and seen-files there;
    - ``AUTO_RECALL=false``, overriding any inherited value (athenaeum#2709),
      so the child's hook never searches the operator's real knowledge base.
      This does not change classification: the hook writes its witness
      ledger line BEFORE it checks ``AUTO_RECALL``
      (:func:`athenaeum.claude_code_adapter.main`), and a ``config.env``
      value can never override it (process env wins in
      :func:`athenaeum.claude_code_adapter._load_config_env`);
    - ``TERM=xterm-256color`` (a pty child otherwise inherits whatever the
      test runner has, possibly ``dumb``).

    Inherited ``CLAUDE_CODE_*`` / ``CLAUDECODE`` variables are scrubbed first
    (:func:`scrub_inherited_claude_env`, athenaeum#2588).
    """
    child_env = scrub_inherited_claude_env(base_env)
    child_env["TERM"] = "xterm-256color"
    child_env["ATHENAEUM_WITNESS_LEDGER_ENABLED"] = "true"
    child_env["PROMPTWITNESS_DIR"] = str(promptwitness_dir)
    if claude_config_dir is not None:
        child_env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    if launcher is not None:
        child_env["PROMPTWITNESS_LAUNCHER"] = launcher
    if athenaeum_cache_dir is None:
        athenaeum_cache_dir = promptwitness_dir.parent / "athenaeum-cache-dir"
    child_env["ATHENAEUM_CACHE_DIR"] = str(athenaeum_cache_dir)
    child_env["AUTO_RECALL"] = "false"
    return child_env


class HarnessAuthError(RuntimeError):
    """No credential hand-off route is available for the isolated config dir."""


def _host_config_dir(env: Mapping[str, str]) -> Path:
    explicit = env.get(HOST_CONFIG_DIR_ENV) or env.get("CLAUDE_CONFIG_DIR")
    return Path(explicit) if explicit else Path.home() / ".claude"


def seed_isolated_config_dir(config_dir: Path, *, workdir: Path, env: Mapping[str, str]) -> str:
    """Make *config_dir* an authenticated, onboarded, folder-trusting isolated
    Claude config dir WITHOUT carrying any host settings or hooks (athenaeum#2588).

    *env* is the UNSCRUBBED harness environment (normally ``os.environ``),
    read only to find the credential. Returns the route used:
    ``"oauth-token-env"``, ``"api-key-env"`` or ``"credentials-file"`` (the
    order of precedence; see the module docstring). Raises
    :class:`HarnessAuthError` when none is available.

    Writes only ``<config_dir>/.claude.json`` (onboarding done, *workdir*
    trusted, API key pre-approved when that route is used) and, for the file
    route, ``<config_dir>/.credentials.json`` (mode 0600).
    """
    state: dict[str, Any] = {
        "hasCompletedOnboarding": True,
        "theme": "dark",
        "projects": {
            str(path): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}
            for path in {workdir, workdir.resolve()}
        },
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    if env.get(_OAUTH_TOKEN_ENV):
        method = "oauth-token-env"
    elif env.get(_API_KEY_ENV):
        method = "api-key-env"
        state["customApiKeyResponses"] = {"approved": [env[_API_KEY_ENV][-20:]], "rejected": []}
    else:
        source = _host_config_dir(env) / _CREDENTIALS_FILENAME
        if not source.is_file():
            raise HarnessAuthError(
                "no credential for the isolated claude config dir: set "
                f"{_OAUTH_TOKEN_ENV} (from `claude setup-token`; the macOS route), "
                f"or {_API_KEY_ENV}, or point {HOST_CONFIG_DIR_ENV} at a config dir "
                f"holding {_CREDENTIALS_FILENAME}"
            )
        target = config_dir / _CREDENTIALS_FILENAME
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
        method = "credentials-file"
    (config_dir / ".claude.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    return method


# ---------------------------------------------------------------------------
# Observations JSON writer (unit-tested with a synthetic input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryPathObservations:
    """What one live run of this harness learned, for athenaeum#2161 to
    copy into ``docs/measurements/`` -- never written by this lane.

    ``claude_version`` and ``child_session_top_level`` are the two
    "verified vs assumed" items the issue's Motivation section names
    explicitly (``CLAUDE_CODE_CHILD_SESSION`` in a top-level interactive
    hook, and whether sub-agent prompts fire the hook at all --
    ``subagent_fires_hook``). ``notes`` is free-form, for anything else a
    live run turns up that does not deserve its own field yet.
    """

    claude_version: str
    child_session_top_level: str | None
    subagent_fires_hook: bool | None
    notes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claude_version": self.claude_version,
            "child_session_top_level": self.child_session_top_level,
            "subagent_fires_hook": self.subagent_fires_hook,
            "notes": dict(self.notes),
        }


def claude_ai_bridge_skip_reason(env: Mapping[str, str] | None = None) -> str:
    """The explicit, non-empty skip reason for the claude.ai bridge row.

    The row is ALWAYS skipped (athenaeum#2588): it has no local driver, so a
    hard failure carried no information. The reason is the recorded outcome
    (``pytest -rs``); when *env* has :data:`CLAUDE_AI_BRIDGE_LIVE_ENV` set it
    also says that opt-in is acknowledged but there is still no driver.
    """
    reason = (
        "the claude.ai bridge case cannot be driven from a local pty (no "
        "local process can set CLAUDE_CODE_BRIDGE_SESSION_ID the way the "
        "real bridge does); observe it manually from inside an actual "
        "claude.ai remote session (athenaeum#2161) and record the ledger line"
    )
    if env is not None and env.get(CLAUDE_AI_BRIDGE_LIVE_ENV) == "1":
        reason += (
            f"; {CLAUDE_AI_BRIDGE_LIVE_ENV}=1 is set but this harness still has no "
            "bridge driver, so the row is skipped, not failed"
        )
    return reason


def write_observations(path: Path, observations: EntryPathObservations) -> Path:
    """Write *observations* as JSON to *path* and print *path* (so a live
    run's output names exactly where athenaeum#2161 should look), returning
    *path* unchanged for chaining.
    """
    path.write_text(json.dumps(observations.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"athenaeum#2235 entry-path observations written to: {path}")
    return path


# ---------------------------------------------------------------------------
# PTY driver (interactive paths only; headless uses plain subprocess)
# ---------------------------------------------------------------------------


@dataclass
class PtySession:
    """A ``claude`` child process attached to a pty's slave end, with the
    master fd held open by the harness for reading/writing like a human
    terminal would."""

    master_fd: int
    process: subprocess.Popen[bytes]
    #: Rolling tail of everything :meth:`drain` has read, for failure messages.
    screen_tail: str = ""

    def drain(self, *, idle_seconds: float = 0.3, overall_timeout: float = 2.0) -> str:
        """Read pending pty output, keeping the last ~4000 chars in
        :attr:`screen_tail`; returns what was read."""
        text = read_pty_until_idle(
            self.master_fd, idle_seconds=idle_seconds, overall_timeout=overall_timeout
        )
        self.screen_tail = (self.screen_tail + text)[-4000:]
        return text

    def close(self, *, timeout: float = 5.0) -> None:
        """Best-effort teardown: SIGTERM the WHOLE process group, wait
        briefly, SIGKILL the group, close the master fd last so no FD leak
        survives even a stuck child.

        Seer finding on athenaeum#2235's review: signaling only
        ``self.process`` (the leader) leaves a descendant the ``claude``
        session itself spawned (a sub-agent, a background-task runner) as
        an orphan. Mirrors ``tests/test_shell_hooks.py``'s own
        ``os.killpg(os.getpgid(proc.pid), ...)`` cleanup precedent, kept as
        this class's existing SIGTERM-then-SIGKILL two-step rather than
        that precedent's single SIGKILL.

        This is ONLY safe because :func:`spawn_claude_pty` passes
        ``start_new_session=True`` -- that makes the child a session AND
        process-group leader in its own right (its pgid equals its own
        pid), so ``killpg`` here can never reach the pytest runner's own
        group. If that spawn-time flag ever changes, this method must
        change with it (see that function's own docstring).
        """

        # athenaeum#2588: the launch-argument row errored here on a live
        # host. A group whose leader already exited (or is a zombie) can
        # make getpgid/killpg raise ProcessLookupError OR, on macOS,
        # PermissionError (EPERM), and a SIGKILLed child can still miss the
        # wait deadline; teardown must absorb all of those -- it is cleanup,
        # never an assertion.
        def _killpg(sig: signal.Signals) -> None:
            try:
                os.killpg(os.getpgid(self.process.pid), sig)
            except (ProcessLookupError, PermissionError):
                pass

        if self.process.poll() is None:
            _killpg(signal.SIGTERM)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _killpg(signal.SIGKILL)
                try:
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass


_PTY_ROWS = 50
_PTY_COLS = 200


def _become_session_leader_with_ctty() -> None:
    """``preexec_fn`` for :func:`spawn_claude_pty`: new session, then adopt
    the pty on fd 0 as the controlling terminal."""
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def spawn_claude_pty(argv: list[str], *, env: Mapping[str, str], cwd: Path) -> PtySession:
    """Spawn *argv* (normally ``["claude", ...]``) attached to a fresh pty.

    The child becomes a session leader (``setsid``) and the pty slave is
    made its CONTROLLING terminal (``TIOCSCTTY``), mirroring a real
    interactive launch. ``start_new_session=True`` alone is not enough:
    ``setsid`` detaches from any tty but does not acquire one, so the child
    had stdin on a tty yet ``tty_nr == 0`` in ``/proc/<pid>/stat`` and the
    hook classified every interactive row as ``unknown`` (athenaeum#2161 live
    run, Linux container) --
    :func:`promptwitness.harness.claude_code._probe_has_tty` reads the
    CHILD's ``/proc/<pid>/stat`` tty field, not this process's. It also
    makes the child the leader of its own process group, which is exactly
    what lets :meth:`PtySession.close` ``killpg`` the whole group (the
    ``claude`` process plus any descendant it spawns) without any risk of
    reaching the pytest runner's own group -- the two are a matched pair;
    see that method's own docstring.
    """
    master_fd, slave_fd = pty.openpty()
    # A real window size (athenaeum#2588): a fresh pty reports 0x0, which a
    # TUI may treat as "no terminal" / unrenderable.
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", _PTY_ROWS, _PTY_COLS, 0, 0))
    try:
        process = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(cwd),
            env=dict(env),
            preexec_fn=_become_session_leader_with_ctty,
        )
    finally:
        os.close(slave_fd)
    return PtySession(master_fd=master_fd, process=process)


#: Pause between the text and the Enter that submits it. The TUI's input layer
#: appears to treat one stdin read carrying text plus a CR as a paste (the CR
#: becomes a newline in the box), so Enter must land as a separate read
#: (athenaeum#2588; a hypothesis only a live run can confirm).
_SUBMIT_SETTLE_SECONDS = 0.6


def submit_into_pty(
    master_fd: int, text: str, *, settle: float = _SUBMIT_SETTLE_SECONDS, press_enter: bool = True
) -> None:
    """Write *text*, wait *settle* seconds, then write ``\\r`` as its OWN
    write so the receiver sees Enter as a distinct keypress, not the tail of
    a paste-shaped chunk."""
    os.write(master_fd, text.encode("utf-8"))
    if press_enter:
        time.sleep(settle)
        os.write(master_fd, b"\r")


def type_into_pty(master_fd: int, text: str, *, press_enter: bool = True) -> None:
    """Write *text* to the pty as a human typing would, optionally followed
    by a separately-delivered Enter (see :func:`submit_into_pty`)."""
    submit_into_pty(master_fd, text, press_enter=press_enter)


#: Bracketed-paste escape sequences (DEC private mode 2004) a terminal
#: emulator wraps pasted text in. ASSUMED, not verified here: whether
#: Claude Code's input layer treats a raw pty write wrapped in these the
#: same way it treats a real terminal emulator's paste is exactly the kind
#: of thing only athenaeum#2161's live run can confirm -- see the module
#: docstring's "Verified vs assumed" note.
_BRACKETED_PASTE_START = "\x1b[200~"
_BRACKETED_PASTE_END = "\x1b[201~"


def send_bracketed_paste(master_fd: int, text: str) -> None:
    """Write *text* wrapped in bracketed-paste start/end sequences, with no
    trailing Enter -- a paste alone does not submit a turn."""
    os.write(master_fd, (_BRACKETED_PASTE_START + text + _BRACKETED_PASTE_END).encode("utf-8"))


def read_pty_until_idle(
    master_fd: int, *, idle_seconds: float = 2.0, overall_timeout: float = 60.0
) -> str:
    """Read from *master_fd* until no new output has arrived for
    *idle_seconds* (a generic "the turn finished" signal that does not rely
    on matching any particular TUI banner text -- the brief's own guidance
    against inline magic strings applies doubly to a UI string this harness
    was never run against).

    Returns everything read. Never raises on a closed/dead pty -- an EOF
    (``os.read`` returning ``b""``) just ends the read loop early.
    """
    deadline = time.monotonic() + overall_timeout
    buf = b""
    last_activity = time.monotonic()
    while time.monotonic() < deadline:
        remaining_idle = idle_seconds - (time.monotonic() - last_activity)
        if remaining_idle <= 0:
            break
        ready, _, _ = select.select([master_fd], [], [], max(remaining_idle, 0.05))
        if not ready:
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        last_activity = time.monotonic()
    return buf.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Transcript reading (read-only; mirrors athenaeum.transcript_verify's own
# record shape rather than re-deriving it -- see that module's docstring).
# ---------------------------------------------------------------------------


def find_transcript_record(session_jsonl: Path, *, prompt_id: str | None) -> dict[str, Any] | None:
    """Find the record in *session_jsonl* whose top-level ``promptId``
    equals *prompt_id*, or (when *prompt_id* is ``None``, e.g. a plain
    typed turn that was never queued) the LAST human-authored record --
    the most recently appended turn is always the one a single-turn driver
    just produced.

    Returns the raw record ``dict``, or ``None`` when the file is absent or
    no record matches. Read-only, same restriction as
    :mod:`athenaeum.transcript_verify`: never writes the transcript.
    """
    try:
        text = session_jsonl.read_text(encoding="utf-8")
    except OSError:
        return None
    records = _parse_session_text(text)
    if prompt_id is not None:
        for record in records:
            if isinstance(record, dict) and record.get("promptId") == prompt_id:
                return record
        return None
    for record in reversed(records):
        if isinstance(record, dict) and _user_authored_text(record):
            return record
    return None


def find_transcript_record_by_text(
    session_jsonl: Path, *, prompt_digest: str
) -> dict[str, Any] | None:
    """Find the record in *session_jsonl* whose user-authored text hashes to
    *prompt_digest* (``canon.digest(canon.normalize(text))``) -- how intake
    finds a record, by what was typed rather than by id. The hook's
    ``prompt_id`` is a hint the harness does not keep unique (a queued prompt
    reuses the id of the turn it queued behind), so it cannot name one record.

    Returns the first matching record ``dict``, or ``None`` when the file is
    absent or no record's text matches. Read-only.
    """
    try:
        text = session_jsonl.read_text(encoding="utf-8")
    except OSError:
        return None
    for record in _parse_session_text(text):
        if not isinstance(record, dict):
            continue
        record_text = _user_authored_text(record)
        if record_text and canon.digest(canon.normalize(record_text)) == prompt_digest:
            return record
    return None


def transcript_record_fields(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pull the four fields :func:`athenaeum.witness_attestation.attest`
    needs out of a raw transcript *record* -- the single place this harness
    knows that shape, so every case driver below calls this instead of
    re-reading ``record["origin"]["kind"]`` etc. inline."""
    if record is None:
        return {
            "prompt_id": None,
            "matched_record_text": "",
            "transcript_origin_kind": None,
            "transcript_prompt_source": None,
            "transcript_is_sidechain": False,
        }
    origin = record.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, Mapping) else None
    return {
        "prompt_id": record.get("promptId"),
        "matched_record_text": _user_authored_text(record),
        "transcript_origin_kind": origin_kind,
        "transcript_prompt_source": record.get("promptSource"),
        "transcript_is_sidechain": bool(record.get("isSidechain")),
    }


def transcript_has_prompt(config_dir: Path, needle: str) -> bool:
    """``True`` when any session transcript under ``<config_dir>/projects/``
    holds a ``user`` record, other than a tool result, whose text contains
    *needle* -- i.e. the prompt was really submitted (athenaeum#2588).

    Scans every ``*.jsonl`` rather than one session id: a resumed session may
    continue the same file or fork a new one, and either is a submission.
    """
    for session_jsonl in (config_dir / "projects").glob("*/*.jsonl"):
        try:
            text = session_jsonl.read_text(encoding="utf-8")
        except OSError:
            continue
        for record in _parse_session_text(text):
            if (
                isinstance(record, dict)
                and record.get("type") == "user"
                and needle in _non_tool_result_text(record)
            ):
                return True
    return False


def wait_for_transcript_prompt(
    config_dir: Path, needle: str, *, timeout: float = 15.0, poll: float = 0.5
) -> bool:
    """Poll :func:`transcript_has_prompt` until it holds or *timeout* lapses."""
    deadline = time.monotonic() + timeout
    while True:
        if transcript_has_prompt(config_dir, needle):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def assert_parent_turn_submitted(config_dir: Path, needle: str) -> None:
    """Fail unless the parent session's transcript shows *needle* submitted.

    A row that expects NO ledger line (sub-agent, ...) calls this so a dead,
    logged-out or never-submitted session -- which also writes no line --
    cannot pass as evidence (athenaeum#2588).
    """
    assert transcript_has_prompt(config_dir, needle), (
        f"the parent session never submitted a turn containing {needle!r}; "
        "a missing ledger line is not evidence about this row"
    )


#: One piece of a prompt: ``("type", text)`` is typed, ``("paste", text)`` is
#: wrapped in bracketed-paste sequences.
PromptPart = tuple[str, str]


def submit_and_confirm(
    session: PtySession,
    config_dir: Path,
    parts: Sequence[PromptPart] | str,
    *,
    needle: str,
    attempts: int = 3,
    timeout: float = 15.0,
) -> None:
    """Enter *parts* into *session*, press Enter, and CONFIRM via the
    transcript that a user record containing *needle* now exists.

    Enter is re-pressed up to *attempts* times (a stray Enter on an empty
    box is a no-op), each followed by up to *timeout* seconds of waiting.
    Raises :class:`AssertionError` carrying the screen tail when nothing is
    submitted -- submission is never assumed (athenaeum#2588).
    """
    if isinstance(parts, str):
        parts = [("type", parts)]
    for kind, text in parts:
        if kind == "paste":
            send_bracketed_paste(session.master_fd, text)
        else:
            os.write(session.master_fd, text.encode("utf-8"))
        time.sleep(_SUBMIT_SETTLE_SECONDS)
    for _ in range(attempts):
        if session.process.poll() is not None:
            break
        os.write(session.master_fd, b"\r")
        if wait_for_transcript_prompt(config_dir, needle, timeout=timeout):
            return
        session.drain()
    session.drain()
    exited = session.process.poll()
    raise AssertionError(
        f"prompt containing {needle!r} was never submitted (no transcript record after "
        f"{attempts} Enter presses; claude exit status: {exited}); screen tail:\n"
        f"{session.screen_tail}"
    )


# ---------------------------------------------------------------------------
# The ten entry-path rows (issue table, verbatim column order)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryPathCase:
    """One row of the issue's entry-path table.

    ``expected_prompt_kind``/``expected_channel``/``expected_segment_kinds``
    are the three ledger-line fields :func:`promptwitness.harness.
    claude_code.record_turn` would write for this entry path (names and
    values taken from that module, never guessed -- see the module
    docstring). ``expect_resumed`` is checked against the line's own
    ``resumed`` field. ``intake_hit`` plus ``intake_miss_reason_prefix``
    describe the expected :func:`athenaeum.witness_attestation.attest`
    outcome: a hit (``intake_hit=True``, prefix ``None``) for every
    human/witnessable row, or the fixed :data:`athenaeum.
    witness_attestation.MISS_REASONS`-shaped prefix otherwise (a prefix,
    not the full string, since e.g. ``channel-not-interactive:<channel>``
    carries a runtime-observed suffix).
    """

    id: str
    description: str
    expected_prompt_kind: str
    expected_channel: str
    expected_segment_kinds: tuple[str, ...]
    expect_resumed: bool
    intake_hit: bool
    intake_miss_reason_prefix: str | None


#: A custom slash command the driver writes into the isolated config dir.
#: The built-in ``/compact`` was the first choice but the live run
#: (athenaeum#2161) showed local built-in commands never fire
#: ``UserPromptSubmit``; only a command that expands into a model prompt does.
_SLASH_COMMAND_NAME = "entrypath-echo"
_SLASH_COMMAND_FILE_BODY = "Reply with the single word 'ack'. Ignore these arguments: $ARGUMENTS\n"
_SLASH_COMMAND_LINE = f"/{_SLASH_COMMAND_NAME} keep the recent discussion about entry paths"

#: A prompt asking Claude to use its own Agent/Task tool. Whether this
#: reliably triggers a sub-agent turn (vs. Claude answering directly) is
#: exactly the kind of model-behavior nondeterminism
#: :data:`EntryPathObservations.subagent_fires_hook` exists to record
#: rather than assume.
_SUBAGENT_TRIGGER_PROMPT = (
    "Use the Agent tool to spawn a sub-agent that replies with the single "
    "word 'ack', then relay its reply to me."
)

#: A prompt that starts a background Bash task and waits for it to report
#: back via a task-notification. ASSUMED mechanism -- see the module
#: docstring.
_BACKGROUND_TASK_TRIGGER_PROMPT = "Run `sleep 2 && echo done` as a background task."

# Substrings each row's submitted prompt must leave in the transcript
# (athenaeum#2588) -- one per prompt text a driver submits.
_ACK_PROMPT = "please reply with the single word 'ack'"
_ACK_AGAIN_PROMPT = "please reply with the single word 'ack' again"
_QUEUED_FIRST_PROMPT = "count slowly from 1 to 20, one number per line"
_QUEUED_SECOND_PROMPT = "also tell me a short fact about lighthouses"
_SLASH_NEEDLE = "keep the recent discussion about entry paths"
_PASTE_BODY = "\n".join(
    f"reference line {i}: the quick brown fox jumps over the lazy dog" for i in range(1, 41)
)
_PASTE_NEEDLE = "reference line 1:"
_SUBAGENT_NEEDLE = "spawn a sub-agent that replies"
_BACKGROUND_NEEDLE = "as a background task"
_HEADLESS_PROMPT = "reply with the word 'ack'"

ENTRY_PATH_CASES: tuple[EntryPathCase, ...] = (
    EntryPathCase(
        id="interactive-typed",
        description="interactive typed",
        expected_prompt_kind="human",
        expected_channel="interactive",
        expected_segment_kinds=("typed",),
        expect_resumed=False,
        intake_hit=True,
        intake_miss_reason_prefix=None,
    ),
    # Live observation (athenaeum#2161, claude 2.1.326): a prompt queued
    # behind a running turn fires the hook with the SAME ``prompt_id`` as the
    # turn it queued behind, not its own. Intake resolves the prompt by that
    # id. ``LedgerIndex`` keeps every line per id and resolves a shared id by
    # digest (athenaeum#2759), so BOTH prompts of the row attest -- the
    # harness asserts the earlier prompt as well as the queued one.
    EntryPathCase(
        id="queued-while-turn-runs",
        description="queued (typed while a turn runs)",
        expected_prompt_kind="human",
        expected_channel="interactive",
        expected_segment_kinds=("typed",),
        expect_resumed=False,
        intake_hit=True,
        intake_miss_reason_prefix=None,
    ),
    # Live observation (athenaeum#2161, claude 2.1.326): the hook classifies
    # and records the slash line correctly, but the transcript record of a
    # slash command carries NO ``promptSource`` (``origin.kind`` is
    # ``human``), so intake reports a clean ``transcript-promptsource:absent``
    # miss -- never a false "witnessed". The row pins that safe miss; flip it
    # to a hit once intake accepts a slash record without ``promptSource``.
    EntryPathCase(
        id="slash-command-with-args",
        description="slash command with args",
        expected_prompt_kind="slash-command",
        expected_channel="interactive",
        expected_segment_kinds=("slash-args",),
        expect_resumed=False,
        intake_hit=False,
        intake_miss_reason_prefix="transcript-promptsource:absent",
    ),
    EntryPathCase(
        id="pasted-content",
        description="pasted content",
        expected_prompt_kind="human",
        expected_channel="interactive",
        expected_segment_kinds=("typed", "pasted"),
        expect_resumed=False,
        intake_hit=True,
        intake_miss_reason_prefix=None,
    ),
    # The ``-p`` prompt IS a launch argument, so once ``ps`` output is matched
    # as contiguous words (athenaeum#2161) the first headless turn classifies
    # ``launch-argument``; the channel stays ``headless`` and the prompt is
    # never human-countable either way.
    EntryPathCase(
        id="headless-claude-p",
        description="headless `claude -p`",
        expected_prompt_kind="launch-argument",
        expected_channel="headless",
        expected_segment_kinds=(),
        expect_resumed=False,
        intake_hit=False,
        intake_miss_reason_prefix="kind-not-human",
    ),
    EntryPathCase(
        id="resumed-session",
        description="resumed session (`--resume`)",
        expected_prompt_kind="human",
        expected_channel="interactive",
        expected_segment_kinds=("typed",),
        expect_resumed=True,
        intake_hit=True,
        intake_miss_reason_prefix=None,
    ),
    EntryPathCase(
        id="subagent-agent-tool",
        description="sub-agent (Agent tool)",
        expected_prompt_kind="subagent",
        expected_channel="interactive",
        expected_segment_kinds=(),
        expect_resumed=False,
        intake_hit=False,
        intake_miss_reason_prefix="kind-not-human",
    ),
    EntryPathCase(
        id="claude-ai-bridge",
        description="claude.ai bridge",
        expected_prompt_kind="human",
        expected_channel="bridge",
        expected_segment_kinds=("typed",),
        expect_resumed=False,
        intake_hit=False,
        intake_miss_reason_prefix="channel-not-interactive",
    ),
    EntryPathCase(
        id="background-task-notification",
        description="background task completion",
        expected_prompt_kind="task-notification",
        expected_channel="interactive",
        expected_segment_kinds=(),
        expect_resumed=False,
        intake_hit=False,
        intake_miss_reason_prefix="kind-not-human",
    ),
    EntryPathCase(
        id="launch-argument",
        description='`claude "<prompt>"` launch',
        expected_prompt_kind="launch-argument",
        expected_channel="interactive",
        expected_segment_kinds=(),
        expect_resumed=False,
        intake_hit=False,
        intake_miss_reason_prefix="kind-not-human",
    ),
)

assert len(ENTRY_PATH_CASES) == 10, "one case per issue table row"
assert len({c.id for c in ENTRY_PATH_CASES}) == 10, "case ids must be unique"


# ---------------------------------------------------------------------------
# Fixture plumbing (live-only; these two functions ARE exercised once the
# gate opens, but never under PROMPTWITNESS_LIVE_CLAUDE unset)
# ---------------------------------------------------------------------------


@dataclass
class _LiveHarnessPaths:
    promptwitness_dir: Path
    claude_config_dir: Path
    settings_path: Path
    child_env: dict[str, str]
    auth_method: str = ""


def _build_live_harness_paths(tmp_path: Path) -> _LiveHarnessPaths:
    """Assemble every tmp-rooted path and the child env for one live case,
    using ONLY the unit-tested helpers above -- this function is itself
    thin glue, not a fourth place that invents env/settings shape."""
    promptwitness_dir = tmp_path / "promptwitness-dir"
    promptwitness_dir.mkdir()
    claude_config_dir = tmp_path / "claude-config-dir"
    claude_config_dir.mkdir()
    athenaeum_cache_dir = tmp_path / "athenaeum-cache-dir"
    athenaeum_cache_dir.mkdir()
    hook_command = resolve_hook_command()
    settings_path = write_settings_file(tmp_path, hook_command)
    auth_method = seed_isolated_config_dir(claude_config_dir, workdir=tmp_path, env=os.environ)
    child_env = build_child_env(
        os.environ,
        promptwitness_dir=promptwitness_dir,
        claude_config_dir=claude_config_dir,
        launcher="athenaeum-2235-entry-path-harness",
        athenaeum_cache_dir=athenaeum_cache_dir,
    )
    return _LiveHarnessPaths(
        promptwitness_dir=promptwitness_dir,
        claude_config_dir=claude_config_dir,
        settings_path=settings_path,
        child_env=child_env,
        auth_method=auth_method,
    )


def _ledger_lines_since(promptwitness_dir: Path, since_seq: int) -> list[ledger.WitnessLine]:
    return [
        line
        for line in ledger.iter_lines(promptwitness_dir)
        if line.kind == "turn" and line.seq > since_seq
    ]


def _max_seq(promptwitness_dir: Path) -> int:
    return max((line.seq for line in ledger.iter_lines(promptwitness_dir)), default=0)


def _find_session_jsonl(claude_config_dir: Path, session_id: str) -> Path | None:
    """Locate ``<session_id>.jsonl`` under ``<claude_config_dir>/projects/``
    (:func:`athenaeum.transcript_verify.default_projects_root`'s own
    ``CLAUDE_CONFIG_DIR``-relative layout) -- the scope directory name is
    an opaque, cwd-derived hash this harness does not try to recompute, so
    it globs for the session file by name instead."""
    projects_root = claude_config_dir / "projects"
    if not projects_root.is_dir():
        return None
    matches = list(projects_root.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _attest_line(
    paths: _LiveHarnessPaths, line: ledger.WitnessLine
) -> tuple[AttestationOutcome, dict[str, Any]]:
    """Resolve the matching transcript record for *line* and run
    :func:`athenaeum.witness_attestation.attest` over it -- the "athenaeum
    intake (athenaeum#2158)" step each case asserts on."""
    session_jsonl = _find_session_jsonl(paths.claude_config_dir, str(line.session))
    record = None
    if session_jsonl is not None:
        # By text first, as intake does; the id is only the fallback for a
        # line whose digest cannot match its record (a slash command: the
        # hook digests ``/name args``, the transcript holds expanded text).
        record = find_transcript_record_by_text(session_jsonl, prompt_digest=line.prompt_digest)
        if record is None:
            record = find_transcript_record(session_jsonl, prompt_id=line.prompt_id)
    fields = transcript_record_fields(record)
    index = AttestationIndex.load(paths.promptwitness_dir)
    utterance_digest = canon.digest(canon.normalize(fields["matched_record_text"])).removeprefix(
        "sha256:"
    )
    claim = fields["matched_record_text"]
    if line.prompt_kind == "slash-command":
        # A memory quotes what the human typed, which for a slash command is
        # its args -- intake checks the claim against the args span.
        args_segment = claude_code.slash_args_segment(claim)
        if args_segment is not None:
            claim = claim[args_segment.start : args_segment.end]
    outcome = attest(
        index,
        prompt_id=fields["prompt_id"],
        origin_session_id=str(line.session),
        claim=claim,
        utterance_digest=utterance_digest,
        matched_record_text=fields["matched_record_text"],
        transcript_origin_kind=fields["transcript_origin_kind"],
        transcript_prompt_source=fields["transcript_prompt_source"],
        transcript_is_sidechain=fields["transcript_is_sidechain"],
    )
    return outcome, fields


# ---------------------------------------------------------------------------
# Per-case drivers
# ---------------------------------------------------------------------------


def _drive_interactive_typed(paths: _LiveHarnessPaths, tmp_path: Path) -> ledger.WitnessLine:
    session = spawn_claude_pty(
        ["claude", "--settings", str(paths.settings_path)],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(session.master_fd)  # banner/startup
        before = _max_seq(paths.promptwitness_dir)
        submit_and_confirm(session, paths.claude_config_dir, _ACK_PROMPT, needle=_ACK_PROMPT)
        read_pty_until_idle(session.master_fd, idle_seconds=3.0)
    finally:
        session.close()
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    assert len(lines) == 1, f"expected exactly one new turn line, got {len(lines)}"
    return lines[0]


def _drive_queued_while_turn_runs(paths: _LiveHarnessPaths, tmp_path: Path) -> ledger.WitnessLine:
    session = spawn_claude_pty(
        ["claude", "--settings", str(paths.settings_path)],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(session.master_fd)
        before = _max_seq(paths.promptwitness_dir)
        submit_and_confirm(
            session, paths.claude_config_dir, _QUEUED_FIRST_PROMPT, needle=_QUEUED_FIRST_PROMPT
        )
        # Deliberately NOT waiting idle before the second turn: typing while
        # the first is still running is the queued path the issue names. The
        # queued record is only written when the turn dequeues, so wait long
        # and press Enter once.
        submit_and_confirm(
            session,
            paths.claude_config_dir,
            _QUEUED_SECOND_PROMPT,
            needle=_QUEUED_SECOND_PROMPT,
            attempts=1,
            timeout=120.0,
        )
        read_pty_until_idle(session.master_fd, idle_seconds=4.0, overall_timeout=90.0)
    finally:
        session.close()
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    assert len(lines) == 2, f"expected two turn lines (initial + queued), got {len(lines)}"
    queued = lines[1]
    assert queued.prompt_id, "the queued turn is expected to carry a prompt_id"
    assert lines[0].prompt_id == queued.prompt_id, (
        "the queued turn is expected to share the earlier turn's prompt_id"
    )
    return queued


def _drive_slash_command_with_args(paths: _LiveHarnessPaths, tmp_path: Path) -> ledger.WitnessLine:
    command_dir = paths.claude_config_dir / "commands"
    command_dir.mkdir(parents=True, exist_ok=True)
    (command_dir / f"{_SLASH_COMMAND_NAME}.md").write_text(
        _SLASH_COMMAND_FILE_BODY, encoding="utf-8"
    )
    session = spawn_claude_pty(
        ["claude", "--settings", str(paths.settings_path)],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(session.master_fd)
        before = _max_seq(paths.promptwitness_dir)
        submit_and_confirm(
            session, paths.claude_config_dir, _SLASH_COMMAND_LINE, needle=_SLASH_NEEDLE
        )
        read_pty_until_idle(session.master_fd, idle_seconds=4.0, overall_timeout=90.0)
    finally:
        session.close()
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    assert len(lines) == 1, f"expected exactly one new turn line, got {len(lines)}"
    return lines[0]


def _drive_pasted_content(paths: _LiveHarnessPaths, tmp_path: Path) -> ledger.WitnessLine:
    session = spawn_claude_pty(
        ["claude", "--settings", str(paths.settings_path)],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(session.master_fd)
        before = _max_seq(paths.promptwitness_dir)
        submit_and_confirm(
            session,
            paths.claude_config_dir,
            [
                ("type", "here is some context: "),
                ("paste", _PASTE_BODY),
                ("type", " -- please summarize it in one word"),
            ],
            needle=_PASTE_NEEDLE,
        )
        read_pty_until_idle(session.master_fd, idle_seconds=4.0, overall_timeout=90.0)
    finally:
        session.close()
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    assert len(lines) == 1, f"expected exactly one new turn line, got {len(lines)}"
    return lines[0]


def _drive_headless_claude_p(paths: _LiveHarnessPaths, tmp_path: Path) -> ledger.WitnessLine:
    before = _max_seq(paths.promptwitness_dir)
    subprocess.run(
        ["claude", "--settings", str(paths.settings_path), "-p", _HEADLESS_PROMPT],
        env=paths.child_env,
        cwd=str(tmp_path),
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert wait_for_transcript_prompt(paths.claude_config_dir, _HEADLESS_PROMPT, timeout=10.0), (
        "the headless prompt left no transcript record"
    )
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    assert len(lines) == 1, f"expected exactly one new turn line, got {len(lines)}"
    return lines[0]


def _drive_resumed_session(paths: _LiveHarnessPaths, tmp_path: Path) -> ledger.WitnessLine:
    first = spawn_claude_pty(
        ["claude", "--settings", str(paths.settings_path)],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(first.master_fd)
        before_first = _max_seq(paths.promptwitness_dir)
        submit_and_confirm(first, paths.claude_config_dir, _ACK_PROMPT, needle=_ACK_PROMPT)
        read_pty_until_idle(first.master_fd, idle_seconds=3.0)
    finally:
        first.close()
    first_lines = _ledger_lines_since(paths.promptwitness_dir, before_first)
    assert len(first_lines) == 1
    session_id = str(first_lines[0].session)

    before_resume = _max_seq(paths.promptwitness_dir)
    second = spawn_claude_pty(
        ["claude", "--settings", str(paths.settings_path), "--resume", session_id],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(second.master_fd)
        submit_and_confirm(
            second, paths.claude_config_dir, _ACK_AGAIN_PROMPT, needle=_ACK_AGAIN_PROMPT
        )
        read_pty_until_idle(second.master_fd, idle_seconds=3.0)
    finally:
        second.close()
    resumed_lines = _ledger_lines_since(paths.promptwitness_dir, before_resume)
    assert len(resumed_lines) == 1
    return resumed_lines[0]


def _drive_subagent_agent_tool(
    paths: _LiveHarnessPaths, tmp_path: Path
) -> ledger.WitnessLine | None:
    """Returns the sub-agent's own witness line if the hook fired for it,
    or ``None`` if it did not -- the issue's own open question
    (:data:`EntryPathObservations.subagent_fires_hook` records which)."""
    session = spawn_claude_pty(
        ["claude", "--settings", str(paths.settings_path)],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(session.master_fd)
        before = _max_seq(paths.promptwitness_dir)
        submit_and_confirm(
            session, paths.claude_config_dir, _SUBAGENT_TRIGGER_PROMPT, needle=_SUBAGENT_NEEDLE
        )
        read_pty_until_idle(session.master_fd, idle_seconds=5.0, overall_timeout=120.0)
    finally:
        session.close()
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    # The top-level turn line is always present; a second line means the
    # sub-agent's own prompt also fired the hook.
    subagent_lines = [line for line in lines if line.prompt_kind == "subagent"]
    return subagent_lines[0] if subagent_lines else None


def _drive_background_task_notification(
    paths: _LiveHarnessPaths, tmp_path: Path
) -> ledger.WitnessLine:
    session = spawn_claude_pty(
        ["claude", "--settings", str(paths.settings_path)],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(session.master_fd)
        # Captured BEFORE the trigger prompt: the notification can land while
        # the trigger turn is still settling, so a marker taken after it
        # races the very line this row asserts on (athenaeum#2161 live run).
        before = _max_seq(paths.promptwitness_dir)
        submit_and_confirm(
            session,
            paths.claude_config_dir,
            _BACKGROUND_TASK_TRIGGER_PROMPT,
            needle=_BACKGROUND_NEEDLE,
        )
        # Give the background task time to finish and push its
        # task-notification back into the session.
        read_pty_until_idle(session.master_fd, idle_seconds=6.0, overall_timeout=45.0)
    finally:
        session.close()
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    notifications = [ln for ln in lines if ln.prompt_kind == "task-notification"]
    assert notifications, (
        "expected the background task's completion to fire the hook; "
        f"saw prompt kinds {[ln.prompt_kind for ln in lines]}"
    )
    return notifications[-1]


def _drive_launch_argument(paths: _LiveHarnessPaths, tmp_path: Path) -> ledger.WitnessLine:
    before = _max_seq(paths.promptwitness_dir)
    session = spawn_claude_pty(
        [
            "claude",
            "--settings",
            str(paths.settings_path),
            _ACK_PROMPT,
        ],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(session.master_fd, idle_seconds=4.0, overall_timeout=60.0)
        # The launch argument submits itself; the transcript still proves it.
        assert wait_for_transcript_prompt(paths.claude_config_dir, _ACK_PROMPT, timeout=30.0), (
            "the launch-argument prompt left no transcript record"
        )
    finally:
        session.close()
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    assert len(lines) == 1, f"expected exactly one new turn line, got {len(lines)}"
    return lines[0]


_DRIVERS: dict[str, Callable[[_LiveHarnessPaths, Path], ledger.WitnessLine | None]] = {
    "interactive-typed": _drive_interactive_typed,
    "queued-while-turn-runs": _drive_queued_while_turn_runs,
    "slash-command-with-args": _drive_slash_command_with_args,
    "pasted-content": _drive_pasted_content,
    "headless-claude-p": _drive_headless_claude_p,
    "resumed-session": _drive_resumed_session,
    "subagent-agent-tool": _drive_subagent_agent_tool,
    "background-task-notification": _drive_background_task_notification,
    "launch-argument": _drive_launch_argument,
}


# ---------------------------------------------------------------------------
# The parametrized test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_child_written_ledger_protection(tmp_path: Path) -> Any:
    """The hook runs in a CHILD process, so this process's append-only
    registry never sees the ledger it writes; clear the flag under this
    case's tmp dir at teardown or the append-only guard (athenaeum#2335)
    errors every live row."""
    yield
    ledger.clear_append_only_protection_under(tmp_path)


@pytest.mark.parametrize("case", ENTRY_PATH_CASES, ids=[c.id for c in ENTRY_PATH_CASES])
def test_entry_path(case: EntryPathCase, tmp_path: Path) -> None:
    """One case per issue table row. Collected and (under the module gate)
    always skipped by default -- see the module docstring."""
    if case.id == "claude-ai-bridge":
        # No local driver exists: skipped with the reason recorded in the
        # pytest report (-rs), never a hard failure (athenaeum#2588).
        pytest.skip(claude_ai_bridge_skip_reason(os.environ))

    try:
        paths = _build_live_harness_paths(tmp_path)
    except HarnessAuthError as exc:
        pytest.fail(str(exc), pytrace=False)
    driver = _DRIVERS[case.id]
    line = driver(paths, tmp_path)

    observations_path = tmp_path / "observations.json"
    if case.id == "subagent-agent-tool":
        fired = line is not None
        write_observations(
            observations_path,
            EntryPathObservations(
                claude_version="",
                child_session_top_level=None,
                subagent_fires_hook=fired,
            ),
        )
        if not fired:
            # No line at all is itself the documented alternative outcome
            # for this row -- confirm intake reports it as a clean miss
            # (never a false "witnessed") and stop here -- but only after
            # proving the parent session really submitted a turn, so a dead
            # session cannot pass on "no line" (athenaeum#2588).
            assert_parent_turn_submitted(paths.claude_config_dir, _SUBAGENT_NEEDLE)
            index = AttestationIndex.load(paths.promptwitness_dir)
            outcome = attest(
                index,
                prompt_id=None,
                origin_session_id="unknown",
                claim="",
                utterance_digest=canon.digest("").removeprefix("sha256:"),
                matched_record_text="",
                transcript_origin_kind=None,
                transcript_prompt_source=None,
                transcript_is_sidechain=False,
            )
            assert outcome.hit is False
            return

    assert line is not None
    assert line.prompt_kind == case.expected_prompt_kind
    assert line.channel == case.expected_channel
    assert tuple(segment.kind for segment in line.segments) == case.expected_segment_kinds
    assert line.resumed is case.expect_resumed
    if case.id == "interactive-typed":
        # The live hook env carries CLAUDE_CODE_CHILD_SESSION=1 even in a
        # top-level interactive session, which is why it is not a classifier
        # input (athenaeum#2161).
        assert line.signals["child_session"] == "1"

    outcome, fields = _attest_line(paths, line)
    assert outcome.hit is case.intake_hit
    if case.intake_hit:
        assert outcome.miss_reason is None
    else:
        assert outcome.miss_reason is not None
        expected_reason = case.intake_miss_reason_prefix
        assert expected_reason is not None
        if ":" in expected_reason:
            # A full reason (e.g. ``transcript-promptsource:absent``): exact.
            assert outcome.miss_reason == expected_reason
        else:
            assert outcome.miss_reason.split(":", 1)[0] == expected_reason

    # prompt_id / prompt_digest parity against the transcript record (issue
    # AC: "prompt_id joined to the transcript record's promptId, and
    # prompt_digest parity with the transcript record").
    if case.id != "queued-while-turn-runs":
        assert line.prompt_id == fields["prompt_id"]
    expected_digest = canon.digest(canon.normalize(fields["matched_record_text"]))
    # Not for slash commands: the hook sees the raw ``/name args`` line while
    # the transcript holds the expanded ``<command-*>`` text, so the two
    # digests differ by construction (athenaeum#2161). Not for the queued row
    # either: it shares the earlier prompt's id by design (athenaeum#2759),
    # so only the id-parity assertion is dropped for it.
    if line.prompt_kind == "human" and fields["matched_record_text"]:
        assert line.prompt_digest == expected_digest

    if case.id == "queued-while-turn-runs":
        # The earlier prompt of the pair shares the queued line's id and must
        # attest too; record both ids so the next live run can tell whether
        # the transcript's own ``promptId`` differs from the hook's.
        earlier_lines = [
            other
            for other in _ledger_lines_since(paths.promptwitness_dir, 0)
            if other.session == line.session and other.seq < line.seq
        ]
        assert earlier_lines, "expected the earlier prompt's line in the same session"
        earlier_outcome, earlier_fields = _attest_line(paths, earlier_lines[-1])
        assert earlier_outcome.hit is True, earlier_outcome.miss_reason
        write_observations(
            observations_path,
            EntryPathObservations(
                claude_version=str(line.harness_version),
                child_session_top_level=None,
                subagent_fires_hook=None,
                notes={
                    "entry_path": case.id,
                    "line_prompt_id": line.prompt_id,
                    "queued_record_prompt_id": fields["prompt_id"],
                    "earlier_record_prompt_id": earlier_fields["prompt_id"],
                },
            ),
        )

    if case.id == "interactive-typed":
        write_observations(
            observations_path,
            EntryPathObservations(
                claude_version=str(line.harness_version),
                child_session_top_level=line.signals.get("child_session")
                if isinstance(line.signals, Mapping)
                else None,
                subagent_fires_hook=None,
                notes={"entry_path": case.id},
            ),
        )
