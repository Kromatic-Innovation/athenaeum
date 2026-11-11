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
"""

from __future__ import annotations

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from athenaeum.transcript_verify import _parse_session_text, _user_authored_text
from athenaeum.witness_attestation import AttestationIndex, AttestationOutcome, attest
from promptwitness import canon, ledger

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
    "write_observations",
    "spawn_claude_pty",
    "type_into_pty",
    "send_bracketed_paste",
    "read_pty_until_idle",
    "find_transcript_record",
    "transcript_record_fields",
    "should_skip_claude_ai_bridge",
    "claude_ai_bridge_skip_reason",
]

# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

#: Set to ``"1"`` to actually spawn the real ``claude`` binary. Never set by
#: this lane, never set in CI -- the live run belongs to athenaeum#2161.
PROMPTWITNESS_LIVE_CLAUDE_ENV = "PROMPTWITNESS_LIVE_CLAUDE"

#: The claude.ai-bridge case cannot be driven from a local pty at all (no
#: local process carries ``CLAUDE_CODE_BRIDGE_SESSION_ID`` on its own). This
#: second, independent opt-in lets athenaeum#2161 run that ONE case from
#: inside an actual claude.ai remote session without also needing a local
#: TTY -- see ``test_claude_ai_bridge_case_skips_outside_remote_session``.
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
    """Resolve the installed :data:`HOOK_COMMAND_NAME` console script's
    absolute path via :func:`shutil.which`.

    Raises :class:`RuntimeError` when it is not on ``PATH`` -- this harness
    has nothing useful to do without it, and a loud failure here is better
    than a settings file pointing at a command that silently never runs.
    """
    resolved = shutil.which(HOOK_COMMAND_NAME)
    if resolved is None:
        raise RuntimeError(
            f"{HOOK_COMMAND_NAME!r} is not on PATH -- install this package "
            "(`pip install -e .`) in the environment running this harness"
        )
    return resolved


def build_child_env(
    base_env: Mapping[str, str],
    *,
    promptwitness_dir: Path,
    claude_config_dir: Path | None = None,
    launcher: str | None = None,
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
      a free-text label only -- never affects classification).
    """
    child_env = dict(base_env)
    child_env["ATHENAEUM_WITNESS_LEDGER_ENABLED"] = "true"
    child_env["PROMPTWITNESS_DIR"] = str(promptwitness_dir)
    if claude_config_dir is not None:
        child_env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    if launcher is not None:
        child_env["PROMPTWITNESS_LAUNCHER"] = launcher
    return child_env


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


def should_skip_claude_ai_bridge(env: Mapping[str, str]) -> bool:
    """``True`` unless *env* opts into the claude.ai bridge case
    (:data:`CLAUDE_AI_BRIDGE_LIVE_ENV`). Factored out of
    :func:`test_entry_path` so the decision itself -- not just its effect
    inside a skipped pytest module -- is unit-testable."""
    return env.get(CLAUDE_AI_BRIDGE_LIVE_ENV) != "1"


def claude_ai_bridge_skip_reason() -> str:
    """The explicit, non-empty skip reason for the claude.ai bridge case
    (issue AC: "the bridge case skips with an explicit reason outside a
    claude.ai remote session")."""
    return (
        "the claude.ai bridge case cannot be driven from a local pty (no "
        "local process can set CLAUDE_CODE_BRIDGE_SESSION_ID the way the "
        f"real bridge does) -- set {CLAUDE_AI_BRIDGE_LIVE_ENV}=1 from inside "
        "an actual claude.ai remote session to run it (athenaeum#2161 runs "
        "it there, not here)"
    )


def write_observations(path: Path, observations: EntryPathObservations) -> Path:
    """Write *observations* as JSON to *path* and print *path* (so a live
    run's output names exactly where athenaeum#2161 should look), returning
    *path* unchanged for chaining.
    """
    path.write_text(
        json.dumps(observations.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
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

    def close(self, *, timeout: float = 5.0) -> None:
        """Best-effort teardown: SIGTERM, wait briefly, SIGKILL, close the
        master fd last so no FD leak survives even a stuck child."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=timeout)
        try:
            os.close(self.master_fd)
        except OSError:
            pass


def spawn_claude_pty(argv: list[str], *, env: Mapping[str, str], cwd: Path) -> PtySession:
    """Spawn *argv* (normally ``["claude", ...]``) attached to a fresh pty.

    ``start_new_session=True`` gives the child its own session/controlling
    terminal, mirroring a real interactive launch rather than inheriting
    this test process's own (nonexistent, under pytest) controlling tty --
    :func:`promptwitness.harness.claude_code._probe_has_tty` reads the
    CHILD's ``/proc/<pid>/stat`` tty field, not this process's.
    """
    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(cwd),
            env=dict(env),
            start_new_session=True,
        )
    finally:
        os.close(slave_fd)
    return PtySession(master_fd=master_fd, process=process)


def type_into_pty(master_fd: int, text: str, *, press_enter: bool = True) -> None:
    """Write *text* to the pty as a human typing would, optionally followed
    by Enter (``\\r`` -- what a terminal in cooked/raw mode sends, not
    ``\\n``)."""
    os.write(master_fd, text.encode("utf-8"))
    if press_enter:
        os.write(master_fd, b"\r")


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


#: `/compact` is a real built-in slash command that accepts freeform
#: trailing args. ASSUMED, not verified here, that its args wrap in
#: `<command-args>` the same way every OTHER slash command's args do --
#: see the module docstring's "Verified vs assumed" note; swap this
#: constant if athenaeum#2161 finds a better-behaved built-in.
_SLASH_COMMAND_LINE = "/compact keep the recent discussion about entry paths"

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
    EntryPathCase(
        id="slash-command-with-args",
        description="slash command with args",
        expected_prompt_kind="slash-command",
        expected_channel="interactive",
        expected_segment_kinds=("slash-args",),
        expect_resumed=False,
        intake_hit=True,
        intake_miss_reason_prefix=None,
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
    EntryPathCase(
        id="headless-claude-p",
        description="headless `claude -p`",
        expected_prompt_kind="human",
        expected_channel="headless",
        expected_segment_kinds=("typed",),
        expect_resumed=False,
        intake_hit=False,
        intake_miss_reason_prefix="channel-not-interactive",
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


def _build_live_harness_paths(tmp_path: Path) -> _LiveHarnessPaths:
    """Assemble every tmp-rooted path and the child env for one live case,
    using ONLY the unit-tested helpers above -- this function is itself
    thin glue, not a fourth place that invents env/settings shape."""
    promptwitness_dir = tmp_path / "promptwitness-dir"
    promptwitness_dir.mkdir()
    claude_config_dir = tmp_path / "claude-config-dir"
    claude_config_dir.mkdir()
    hook_command = resolve_hook_command()
    settings_path = write_settings_file(tmp_path, hook_command)
    child_env = build_child_env(
        os.environ,
        promptwitness_dir=promptwitness_dir,
        claude_config_dir=claude_config_dir,
        launcher="athenaeum-2235-entry-path-harness",
    )
    return _LiveHarnessPaths(
        promptwitness_dir=promptwitness_dir,
        claude_config_dir=claude_config_dir,
        settings_path=settings_path,
        child_env=child_env,
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
    record = (
        find_transcript_record(session_jsonl, prompt_id=line.prompt_id)
        if session_jsonl is not None
        else None
    )
    fields = transcript_record_fields(record)
    index = AttestationIndex.load(paths.promptwitness_dir)
    utterance_digest = canon.digest(canon.normalize(fields["matched_record_text"])).removeprefix(
        "sha256:"
    )
    outcome = attest(
        index,
        prompt_id=fields["prompt_id"],
        origin_session_id=str(line.session),
        claim=fields["matched_record_text"],
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
        type_into_pty(session.master_fd, "please reply with the single word 'ack'")
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
        type_into_pty(session.master_fd, "count slowly from 1 to 20, one number per line")
        # Deliberately NOT waiting idle before the second turn: typing while
        # the first is still running is the queued path the issue names.
        time.sleep(0.5)
        type_into_pty(session.master_fd, "also tell me a short fact about lighthouses")
        read_pty_until_idle(session.master_fd, idle_seconds=4.0, overall_timeout=90.0)
    finally:
        session.close()
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    assert len(lines) == 2, f"expected two turn lines (initial + queued), got {len(lines)}"
    queued = lines[1]
    assert queued.prompt_id, "the queued turn is expected to carry its own prompt_id"
    return queued


def _drive_slash_command_with_args(
    paths: _LiveHarnessPaths, tmp_path: Path
) -> ledger.WitnessLine:
    session = spawn_claude_pty(
        ["claude", "--settings", str(paths.settings_path)],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(session.master_fd)
        before = _max_seq(paths.promptwitness_dir)
        type_into_pty(session.master_fd, _SLASH_COMMAND_LINE)
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
        type_into_pty(session.master_fd, "here is some context: ", press_enter=False)
        send_bracketed_paste(session.master_fd, "pasted block of reference text")
        type_into_pty(session.master_fd, " -- please summarize it in one word")
        read_pty_until_idle(session.master_fd, idle_seconds=4.0, overall_timeout=90.0)
    finally:
        session.close()
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    assert len(lines) == 1, f"expected exactly one new turn line, got {len(lines)}"
    return lines[0]


def _drive_headless_claude_p(paths: _LiveHarnessPaths, tmp_path: Path) -> ledger.WitnessLine:
    before = _max_seq(paths.promptwitness_dir)
    subprocess.run(
        ["claude", "--settings", str(paths.settings_path), "-p", "reply with the word 'ack'"],
        env=paths.child_env,
        cwd=str(tmp_path),
        capture_output=True,
        timeout=90,
        check=False,
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
        type_into_pty(first.master_fd, "please reply with the single word 'ack'")
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
        type_into_pty(second.master_fd, "please reply with the single word 'ack' again")
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
        type_into_pty(session.master_fd, _SUBAGENT_TRIGGER_PROMPT)
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
        type_into_pty(session.master_fd, _BACKGROUND_TASK_TRIGGER_PROMPT)
        read_pty_until_idle(session.master_fd, idle_seconds=3.0)
        before = _max_seq(paths.promptwitness_dir)
        # Give the background task time to finish and push its
        # task-notification back into the session.
        read_pty_until_idle(session.master_fd, idle_seconds=6.0, overall_timeout=30.0)
    finally:
        session.close()
    lines = _ledger_lines_since(paths.promptwitness_dir, before)
    assert len(lines) >= 1, "expected the background task's completion to fire the hook"
    return lines[-1]


def _drive_launch_argument(paths: _LiveHarnessPaths, tmp_path: Path) -> ledger.WitnessLine:
    before = _max_seq(paths.promptwitness_dir)
    session = spawn_claude_pty(
        [
            "claude",
            "--settings",
            str(paths.settings_path),
            "please reply with the single word 'ack'",
        ],
        env=paths.child_env,
        cwd=tmp_path,
    )
    try:
        read_pty_until_idle(session.master_fd, idle_seconds=4.0, overall_timeout=60.0)
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


@pytest.mark.parametrize("case", ENTRY_PATH_CASES, ids=[c.id for c in ENTRY_PATH_CASES])
def test_entry_path(case: EntryPathCase, tmp_path: Path) -> None:
    """One case per issue table row. Collected and (under the module gate)
    always skipped by default -- see the module docstring."""
    if case.id == "claude-ai-bridge":
        if should_skip_claude_ai_bridge(os.environ):
            pytest.skip(claude_ai_bridge_skip_reason())
        pytest.fail(
            "running the claude.ai bridge case requires driving it from inside "
            "a real claude.ai remote session -- this harness has no local "
            "driver for it by design (see the skip branch above)"
        )

    paths = _build_live_harness_paths(tmp_path)
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
            # (never a false "witnessed") and stop here.
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

    outcome, fields = _attest_line(paths, line)
    assert outcome.hit is case.intake_hit
    if case.intake_hit:
        assert outcome.miss_reason is None
    else:
        assert outcome.miss_reason is not None
        assert outcome.miss_reason.split(":", 1)[0] == case.intake_miss_reason_prefix

    # prompt_id / prompt_digest parity against the transcript record (issue
    # AC: "prompt_id joined to the transcript record's promptId, and
    # prompt_digest parity with the transcript record").
    assert line.prompt_id == fields["prompt_id"]
    expected_digest = canon.digest(canon.normalize(fields["matched_record_text"]))
    if line.prompt_kind in ("human", "slash-command") and fields["matched_record_text"]:
        assert line.prompt_digest == expected_digest

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
