# SPDX-License-Identifier: Apache-2.0
"""Claude Code harness adapter for the promptwitness ledger (athenaeum#2149).

Turns the ``UserPromptSubmit``/``SessionEnd`` hook's stdin JSON and process
env into one witness-ledger line, per ``docs/design/witness-ledger.md`` §
"Prompt kind and channel (Claude Code)" and the issue's own verified/assumed
table. Zero ``athenaeum`` imports (package-boundary rule, enforced by
``tests/test_promptwitness_boundary.py``).

**Prompt kind** (:data:`WRAPPERS` plus the two override checks in
:func:`classify_prompt_kind`) and **channel** (:func:`detect_channel`) are
classified independently of each other and of ``athenaeum`` -- neither
reaches outside this module except for stdlib and sibling ``promptwitness``
modules.

**Caller contract.** :func:`record_turn` and :func:`session_close` raise on
genuine failure; they are NOT fail-safe by themselves. The silent no-op
guard (append ``{ts, error}`` to the hook's own error log, never block the
turn) lives in the caller -- ``athenaeum.claude_code_adapter`` -- because
only that module knows the athenaeum cache dir to log to, and because a
promptwitness-side try/except would hide the one thing
``tests/test_claude_code_adapter.py``'s simulated-``ImportError`` AC needs
to observe: a REAL exception crossing the package boundary.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from promptwitness import __version__, canon, ledger
from promptwitness.ledger import Segment

__all__ = [
    "WrapperRule",
    "WRAPPERS",
    "classify_prompt_kind",
    "pasted_segments",
    "slash_args_segment",
    "detect_channel",
    "record_turn",
    "session_close",
]

#: ``hook_version`` recorded on every line this module writes -- the
#: promptwitness package's own version, not athenaeum's (this module has no
#: way to know athenaeum's version, and shouldn't need to).
_HOOK_VERSION = f"promptwitness {__version__}"

_PASTE_OPEN_RE = re.compile(r'<pasted_content\s+id=["\']?([^"\'>\s]+)["\']?[^>]*>')
_PASTE_CLOSE = "</pasted_content>"
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)

#: The raw line a live ``UserPromptSubmit`` hook receives for a slash command
#: (athenaeum#2161): ``/<name>`` optionally followed by whitespace and the
#: args, BEFORE claude expands it into ``<command-name>`` wrapper text. The
#: name stops at the first character outside the set, so a path-like typed
#: prompt such as ``/usr/bin is missing`` does not match.
_RAW_SLASH_RE = re.compile(r"/[A-Za-z][A-Za-z0-9_:.-]*(?:\s+(.*))?\Z", re.DOTALL)


# ---------------------------------------------------------------------------
# Prompt kind
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WrapperRule:
    """One row of the issue's "Leading text -> prompt_kind" table."""

    prefix: str
    kind: str
    human: bool


#: Checked in order, against the first non-whitespace characters of the
#: prompt, after the ``subagent``/``launch-argument`` overrides in
#: :func:`classify_prompt_kind` (which run first and take precedence over
#: every row here) and before the "not ``<``-prefixed" / "unrecognized
#: ``<tag``" defaults (``human`` and ``unknown`` respectively, also handled
#: in :func:`classify_prompt_kind`, since neither is a fixed-prefix match).
WRAPPERS: tuple[WrapperRule, ...] = (
    WrapperRule(prefix="<pasted_content", kind="human", human=True),
    WrapperRule(prefix="<command-message", kind="slash-command", human=True),
    WrapperRule(prefix="<command-name", kind="slash-command", human=True),
    WrapperRule(prefix="<task-notification", kind="task-notification", human=False),
    WrapperRule(prefix="<local-command-", kind="local-command", human=False),
    WrapperRule(prefix="<system-reminder", kind="system", human=False),
)


def classify_prompt_kind(
    prompt: str,
    *,
    agent_id: str | None,
    transcript_path: str | None,
    is_launch_argument: bool,
) -> tuple[str, bool]:
    """Classify *prompt* into ``(prompt_kind, is_human)``.

    Precedence, highest first (each overrides every row below it, per the
    issue's table): ``launch-argument`` > ``subagent`` > a raw ``/name args``
    line (``slash-command``, human; the name is one segment, so ``/tmp`` or
    ``/etc is broken`` also match -- a documented false miss that is still
    counted) > the :data:`WRAPPERS` rows, in order > the two prefix-less
    defaults (``human`` for plain text, ``unknown`` -- non-human,
    default-deny -- for an unrecognized ``<tag``).
    """
    if is_launch_argument:
        return "launch-argument", False
    if agent_id or (transcript_path is not None and "/subagents/" in transcript_path):
        return "subagent", False
    stripped = prompt.lstrip()
    if _RAW_SLASH_RE.match(stripped):
        return "slash-command", True
    if not stripped.startswith("<"):
        return "human", True
    for rule in WRAPPERS:
        if stripped.startswith(rule.prefix):
            return rule.kind, rule.human
    return "unknown", False


# ---------------------------------------------------------------------------
# Segments: typed/pasted spans, slash-args span
# ---------------------------------------------------------------------------


def _typed_segment(prompt: str, start: int, end: int) -> Segment | None:
    if end <= start:
        return None
    return Segment(kind="typed", start=start, end=end, digest=canon.digest(prompt[start:end]))


def pasted_segments(prompt: str) -> tuple[list[Segment], bool]:
    """Split *prompt* into typed/pasted :class:`~promptwitness.ledger.Segment`
    spans around every ``<pasted_content id=...>...</pasted_content>`` tag.

    Returns ``(segments, segments_truncated)``. An unterminated tag (no
    matching ``</pasted_content>`` before the end of the string) yields one
    ``pasted`` segment running to the end of *prompt* and sets
    ``segments_truncated`` -- nothing after an unterminated tag can be
    distinguished from paste content, so scanning stops there. Zero pastes
    yields a single ``typed`` segment covering the whole prompt (or no
    segments at all for an empty prompt); a leading/trailing empty typed
    span (e.g. a prompt that starts or ends exactly on a paste boundary) is
    omitted rather than recorded as a zero-length segment.
    """
    segments: list[Segment] = []
    pos = 0
    truncated = False
    length = len(prompt)
    while True:
        match = _PASTE_OPEN_RE.search(prompt, pos)
        if match is None:
            break
        typed = _typed_segment(prompt, pos, match.start())
        if typed is not None:
            segments.append(typed)
        content_start = match.end()
        close_idx = prompt.find(_PASTE_CLOSE, content_start)
        if close_idx == -1:
            segments.append(
                Segment(
                    kind="pasted",
                    start=content_start,
                    end=length,
                    digest=canon.digest(prompt[content_start:]),
                    paste_id=match.group(1),
                )
            )
            truncated = True
            pos = length
            break
        segments.append(
            Segment(
                kind="pasted",
                start=content_start,
                end=close_idx,
                digest=canon.digest(prompt[content_start:close_idx]),
                paste_id=match.group(1),
            )
        )
        pos = close_idx + len(_PASTE_CLOSE)
    trailing = _typed_segment(prompt, pos, length)
    if trailing is not None:
        segments.append(trailing)
    return segments, truncated


def slash_args_segment(prompt: str) -> Segment | None:
    """The slash-command arguments as one ``slash-args``
    :class:`~promptwitness.ledger.Segment`: the
    ``<command-args>...</command-args>`` span when the wrapper is present,
    else the text after the command name of a raw ``/name args`` line (the
    interactive form, which carries no wrapper). ``None`` when there is no
    such span or it is empty/whitespace-only (operator ruling 4: a slash
    command with no args records none)."""
    match = _COMMAND_ARGS_RE.search(prompt)
    if match is None:
        match = _RAW_SLASH_RE.match(prompt.lstrip())
        if match is None or match.group(1) is None:
            return None
        offset = len(prompt) - len(prompt.lstrip())
        start, end = offset + match.start(1), offset + match.end(1)
        content = prompt[start:end]
        if not content.strip():
            return None
        return Segment(kind="slash-args", start=start, end=end, digest=canon.digest(content))
    start, end = match.start(1), match.end(1)
    content = prompt[start:end]
    if not content.strip():
        return None
    return Segment(kind="slash-args", start=start, end=end, digest=canon.digest(content))


# ---------------------------------------------------------------------------
# Channel detection
# ---------------------------------------------------------------------------


def _tty_from_proc_stat(pid: str) -> bool | None:
    """Cheap, fork-free controlling-tty probe via ``/proc/<pid>/stat``
    (Linux only). Returns ``None`` when the file is unreadable or
    unparseable -- the caller falls back to ``ps`` in that case."""
    try:
        text = Path("/proc", pid, "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The ``comm`` field (2nd) is parenthesized and may itself contain
    # spaces/parens, so skip past the LAST ')' rather than splitting the
    # whole line positionally.
    close_idx = text.rfind(")")
    if close_idx == -1:
        return None
    rest = text[close_idx + 1 :].split()
    # `rest[0]` is field 3 (state); field 7 (tty_nr) is `rest[4]`.
    if len(rest) < 5:
        return None
    try:
        tty_nr = int(rest[4])
    except ValueError:
        return None
    return tty_nr != 0


def _probe_has_tty(pid: str | None) -> bool | None:
    """Controlling-tty probe for *pid*. ``None`` means "could not be
    determined" and is treated the same as "no tty" by callers. Prefers
    ``/proc/<pid>/stat`` on Linux (no fork); falls back to ``ps -o tty=``
    elsewhere, per the issue's own probe description. Windows has no tty
    probe at all (the issue's own note), so this always returns ``None``
    there."""
    if not pid:
        return None
    if sys.platform == "win32":
        return None
    if sys.platform.startswith("linux"):
        result = _tty_from_proc_stat(pid)
        if result is not None:
            return result
    try:
        proc = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tty = proc.stdout.strip()
    if not tty or tty in ("?", "??"):
        return False
    return True


def detect_channel(
    env: Mapping[str, str],
    *,
    pid: str | None,
    tty_probe: Callable[[str | None], bool | None] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Classify the entry channel, per the issue's channel table.

    Returns ``(channel, signals)``. ``signals`` always carries the raw
    values this decision was made from (never the bridge session id's
    actual value -- only whether one was present), so a later rule change
    can re-derive the channel without a new probe.
    """
    probe = tty_probe or _probe_has_tty
    entrypoint = env.get("CLAUDE_CODE_ENTRYPOINT")
    attended = env.get("CLAUDE_CODE_SESSION_ATTENDED")
    child_session = env.get("CLAUDE_CODE_CHILD_SESSION")
    bridge_id = env.get("CLAUDE_CODE_BRIDGE_SESSION_ID")
    has_tty = probe(pid)
    signals: dict[str, Any] = {
        "entrypoint": entrypoint,
        "attended": attended,
        "child_session": child_session,
        "has_tty": has_tty,
        "bridge_present": bool(bridge_id),
    }
    if bridge_id:
        # Checked first: a bridge session still owns a TTY, so the
        # interactive check below would otherwise misclassify it.
        return "bridge", signals
    # ``child_session`` is recorded in ``signals`` but is NOT a classifier
    # input: a live claude 2.1.326 sets ``CLAUDE_CODE_CHILD_SESSION=1`` in the
    # env of EVERY hook it spawns, a top-level typed prompt included
    # (athenaeum#2161), so it cannot tell a child from a top-level session.
    # Sub-agent prompts are told apart by ``agent_id`` in the hook stdin.
    if entrypoint == "cli" and attended == "1" and has_tty is True:
        return "interactive", signals
    if (entrypoint or "").startswith("sdk") or attended == "0":
        return "headless", signals
    return "unknown", signals


# ---------------------------------------------------------------------------
# launch-argument detection
# ---------------------------------------------------------------------------


def _probe_proc_cmdline(pid: str) -> str | None:
    """Exact argv from ``/proc/<pid>/cmdline`` (NUL-separated), or ``None``
    where ``/proc`` is unavailable or unreadable (macOS, a gone process)."""
    if not pid.isdigit():
        return None
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    return text if text.strip("\0") else None


def _probe_argv(pid: str | None) -> str | None:
    """Raw command line of *pid*. Exact (NUL-separated argv) from ``/proc``
    when available; otherwise the lossy ``ps -o args=`` rendering, which has
    no argument boundaries and mangles a newline in argv (``?`` on Linux
    procps, ``\\012`` on macOS)."""
    if not pid:
        return None
    exact = _probe_proc_cmdline(str(pid))
    if exact is not None:
        return exact
    try:
        proc = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out or None


def _ps_tokens(args_line: str) -> list[str]:
    try:
        return shlex.split(args_line)
    except ValueError:
        return args_line.split()


_PS_ESCAPE_RE = re.compile(r"\\(\\|[0-7]{3})")


def _decode_ps_escapes(args_line: str) -> str:
    """Decode the octal escapes macOS ``ps`` writes for non-printing argv
    bytes (``\\012`` newline, ``\\011`` tab, ...) and its ``\\\\`` for a
    backslash, so a launch prompt containing them still matches
    (athenaeum#2745)."""

    def _one(match: re.Match[str]) -> str:
        body = match.group(1)
        return "\\" if body == "\\" else chr(int(body, 8))

    return _PS_ESCAPE_RE.sub(_one, args_line)


def _executable_end(words: list[str]) -> int:
    """Index just past the ``claude`` executable in a ``ps`` word list: the
    first word whose basename is ``claude``, so an interpreter
    (``node /x/claude``) or an unquoted path with spaces
    (``/Apps/My Tools/claude``) is not read as positional arguments
    (athenaeum#2745). ``1`` when no such word exists."""
    for idx, word in enumerate(words):
        if word.rsplit("/", 1)[-1] in ("claude", "claude.exe"):
            return idx + 1
    return 1


#: claude CLI flags (claude 2.1.325 ``--help``) taking several values: every
#: following non-flag token is a value, as claude itself parses them.
_VARIADIC_FLAGS = frozenset(
    {
        "--add-dir",
        "--allowedTools",
        "--allowed-tools",
        "--betas",
        "--disallowedTools",
        "--disallowed-tools",
        "--file",
        "--mcp-config",
        "--tools",
    }
)

#: claude CLI flags taking exactly one required value.
_VALUE_FLAGS = frozenset(
    {
        "--agent",
        "--agents",
        "--append-system-prompt",
        "--autocompact",
        "--debug-file",
        "--effort",
        "--environment",
        "--fallback-model",
        "--input-format",
        "--json-schema",
        "--max-budget-usd",
        "--model",
        "-n",
        "--name",
        "--output-format",
        "--permission-mode",
        "--permission-prompts",
        "--plugin-dir",
        "--plugin-url",
        "--remote-control-session-name-prefix",
        "--session-id",
        "--setting-sources",
        "--settings",
        "--system-prompt",
        "--system-prompt-snapshot",
    }
)

#: claude CLI flags taking no value: the token after one is positional.
_BOOLEAN_FLAGS = frozenset(
    {
        "--allow-dangerously-skip-permissions",
        "--ax-screen-reader",
        "--bg",
        "--background",
        "--bare",
        "--brief",
        "--chrome",
        "-c",
        "--continue",
        "--dangerously-skip-permissions",
        "--desktop",
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
        "--fork-session",
        "--forward-subagent-text",
        "-h",
        "--help",
        "--ide",
        "--include-hook-events",
        "--include-partial-messages",
        "--no-chrome",
        "--no-session-persistence",
        "-p",
        "--print",
        "--replay-user-messages",
        "--restricted",
        "--safe-mode",
        "--strict-mcp-config",
        "--tmux",
        "--verbose",
        "-v",
        "--version",
    }
)


def _ps_positional_mask(tokens: list[str]) -> list[str]:
    """Classify each token after the executable as ``"flag"``, ``"value"``
    (certainly a flag's value: after a :data:`_VALUE_FLAGS` or
    :data:`_VARIADIC_FLAGS` flag), ``"maybe-value"`` (directly after a flag
    whose arity is unknown or optional, such as ``--resume [id]``) or
    ``"positional"``. After ``--`` every token is positional."""
    mask: list[str] = []
    pending = ""  # "one", "many", "maybe" or ""
    rest_positional = False
    for token in tokens:
        if rest_positional:
            mask.append("positional")
        elif token == "--":
            mask.append("flag")
            rest_positional = True
            pending = ""
        elif token.startswith("-") and len(token) > 1:
            mask.append("flag")
            name, has_eq, _ = token.partition("=")
            if has_eq:
                pending = "many" if name in _VARIADIC_FLAGS else ""
            elif name in _VARIADIC_FLAGS:
                pending = "many"
            elif name in _VALUE_FLAGS:
                pending = "one"
            elif name in _BOOLEAN_FLAGS:
                pending = ""
            else:
                pending = "maybe"
        elif pending in ("one", "many"):
            mask.append("value")
            if pending == "one":
                pending = ""
        elif pending == "maybe":
            mask.append("maybe-value")
            pending = ""
        else:
            mask.append("positional")
    return mask


def _launch_argument_check(
    prompt: str,
    *,
    pid: str | None,
    is_first_turn: bool,
    argv_probe: Callable[[str | None], str | None] | None = None,
) -> tuple[bool, str | None]:
    """:func:`_is_launch_argument` plus the ``signals["argv_probe"]`` value:
    ``None`` when no check ran (a later turn, an empty prompt), ``"exact"``
    or ``"ps"`` for the source that was read, ``"failed"`` when no argv was
    available (no ``CLAUDE_PID``, or an empty probe result)."""
    if not is_first_turn:
        return False, None
    target = " ".join(prompt.split())
    if not target:
        return False, None
    if not pid:
        # No process to probe: no evidence either way, classified from text.
        return False, "failed"
    probe = argv_probe or _probe_argv
    args_line = probe(pid)
    if not args_line:
        # CLAUDE_PID names a claude process, so a launch prompt may exist and
        # cannot be ruled out: fail closed (athenaeum#2745).
        return True, "failed"
    if "\0" in args_line:
        found = any(" ".join(arg.split()) == target for arg in args_line.split("\0")[1:])
        return found, "exact"
    args_line = _decode_ps_escapes(args_line)
    tokens = _ps_tokens(args_line)
    tokens = tokens[_executable_end(tokens) :]
    mask = _ps_positional_mask(tokens)
    if any(
        kind != "value" and " ".join(token.split()) == target
        for token, kind in zip(tokens, mask, strict=True)
    ):
        return True, "ps"
    words = args_line.split()
    words = words[_executable_end(words) :]
    target_words = target.split(" ")
    size = len(target_words)
    if size > 1:
        # A one-word prompt is fully covered by the token match above, which
        # skips flag values; a word run would match a value such as `opus`.
        first = target_words[0]
        for start in range(len(words) - size + 1):
            if words[start] == first and words[start : start + size] == target_words:
                return True, "ps"
    return "positional" in mask, "ps"


def _is_launch_argument(
    prompt: str,
    *,
    pid: str | None,
    is_first_turn: bool,
    argv_probe: Callable[[str | None], str | None] | None = None,
) -> bool:
    """``True`` only on a claude process's first turn (a new session, or the
    first turn after ``--resume``/``--continue``, athenaeum#2744), when
    *prompt* is a positional launch argument of ``$CLAUDE_PID``
    (``claude "<prompt>"``, ``claude --resume <id> "<prompt>"``).
    Never stores or returns argv text, only the boolean.

    Comparison is whitespace-normalised and linear in argv size:

    * exact argv (``/proc/<pid>/cmdline``, NUL-separated): whole arguments
      only; an argument that matches or does not is certain.
    * ``ps`` fallback (no argument boundaries): octal escapes such as
      ``\\012``/``\\011`` are decoded, words up to the ``claude`` executable
      are skipped, then whole shell-split tokens that are not a known flag's
      value are compared, plus (for a multi-word prompt) runs of exactly the
      prompt's word count. Fails closed: with no match, a positional token
      that cannot be ruled out (a prompt mangled by ``ps``, unparsable
      quoting) counts as a launch argument.

    Also fails closed when ``CLAUDE_PID`` is set but the probe returns
    nothing. With no ``CLAUDE_PID`` there is no evidence and this is False.
    """
    found, _status = _launch_argument_check(
        prompt, pid=pid, is_first_turn=is_first_turn, argv_probe=argv_probe
    )
    return found


# ---------------------------------------------------------------------------
# Per-session state cache (non-authoritative; the chain is authoritative)
# ---------------------------------------------------------------------------


def _state_path(ledger_dir: Path, session: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session) or "unknown"
    return ledger_dir / "state" / f"{safe}.json"


def _load_state(ledger_dir: Path, session: str) -> dict[str, Any] | None:
    path = _state_path(ledger_dir, session)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _save_state(ledger_dir: Path, session: str, data: Mapping[str, Any]) -> None:
    """Best-effort cache write. ``state/`` is explicitly non-authoritative
    (the chain is), so a write failure here must never raise -- the line
    this call follows has already been durably appended either way."""
    path = _state_path(ledger_dir, session)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(data), sort_keys=True), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# record_turn / session_close
# ---------------------------------------------------------------------------


def _sanitise_launcher(raw: str | None) -> str | None:
    """Sanitise a ``PROMPTWITNESS_LAUNCHER`` value for ``signals["launcher"]``
    (athenaeum#2172). Free text -- no allow-list, no vocabulary check.

    Order matters: strip every character in Unicode category ``C*``
    (control and format, including newlines, tabs and NUL), then strip
    surrounding whitespace, then cap at 64 characters, then return ``None``
    if nothing is left."""
    if raw is None:
        return None
    cleaned = "".join(ch for ch in raw if not unicodedata.category(ch).startswith("C"))
    cleaned = cleaned.strip()[:64]
    return cleaned or None


def record_turn(raw: str, env: Mapping[str, str]) -> ledger.WitnessLine | None:
    """Parse one ``UserPromptSubmit`` hook stdin payload and append a
    ``kind="turn"`` witness line.

    Tolerant of malformed/empty stdin (degrades to an empty prompt, mirroring
    ``athenaeum.claude_code_adapter``'s own stdin tolerance) -- but otherwise
    raises on a genuine failure (a locked ledger, a filesystem error, an
    unexpected bug). See the module docstring's "Caller contract": the
    silent no-op guard around THIS call is the caller's job, not this
    function's.

    Returns ``None``, writing nothing, when ``ledger.lock`` is not taken
    within :data:`promptwitness.ledger.HOOK_LOCK_TIMEOUT` seconds: a stuck
    lock holder must not block the prompt (athenaeum#2745).
    """
    try:
        payload: dict[str, Any] = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    prompt = str(payload.get("prompt") or "")
    session = str(payload.get("session_id") or env.get("CLAUDE_CODE_SESSION_ID") or "unknown")
    prompt_id = payload.get("prompt_id")
    transcript_path = payload.get("transcript_path")
    agent_id = payload.get("agent_id")
    agent_type = payload.get("agent_type")

    ledger_dir = ledger.resolve_ledger_dir("claude-code")
    state = _load_state(ledger_dir, session)
    claude_pid = env.get("CLAUDE_PID")
    prior_pid = state.get("claude_pid") if state else None
    # The launch-argument check runs on the first hook call of each claude
    # PROCESS, not only of each session (athenaeum#2744): `claude --resume
    # <id> "prompt"` and `claude -c "prompt"` start a new process on a session
    # that already has state, and their scripted first prompt is a launch
    # argument exactly as on a new session. A process is told apart by
    # `CLAUDE_PID`, which the cache records on every line; a different (or
    # newly present) pid means this is that process's first turn. The resume
    # flags in argv are not consulted: the pid change covers every resume
    # form, and argv alone cannot say whether this is the process's first turn.
    is_first_turn = state is None or prior_pid != claude_pid
    resumed = bool(state is not None and prior_pid and claude_pid and prior_pid != claude_pid)
    prior_turn = int(state.get("turn", 0)) if state else 0
    turn = prior_turn + 1

    raw_digest = canon.digest(prompt)
    prompt_digest = canon.digest(canon.normalize(prompt))

    is_launch_argument, argv_probe_status = _launch_argument_check(
        prompt, pid=claude_pid, is_first_turn=is_first_turn
    )
    prompt_kind, _is_human = classify_prompt_kind(
        prompt,
        agent_id=agent_id,
        transcript_path=transcript_path,
        is_launch_argument=is_launch_argument,
    )
    channel, channel_signals = detect_channel(env, pid=claude_pid)

    segments: list[Segment] = []
    truncated = False
    if prompt_kind == "human":
        segments, truncated = pasted_segments(prompt)
    elif prompt_kind == "slash-command":
        slash_segment = slash_args_segment(prompt)
        if slash_segment is not None:
            segments = [slash_segment]

    signals: dict[str, Any] = dict(channel_signals)
    signals["agent_id"] = agent_id
    signals["agent_type"] = agent_type
    launcher = _sanitise_launcher(env.get("PROMPTWITNESS_LAUNCHER"))
    if launcher:
        signals["launcher"] = launcher
    if truncated:
        signals["segments_truncated"] = True
    if argv_probe_status is not None:
        signals["argv_probe"] = argv_probe_status

    line = ledger.try_append_line(
        ledger_dir,
        kind="turn",
        harness="claude-code",
        harness_version=str(env.get("CLAUDE_CODE_VERSION") or ""),
        hook_version=_HOOK_VERSION,
        session=session,
        turn=turn,
        prompt_id=str(prompt_id) if prompt_id else None,
        prompt_kind=prompt_kind,
        prompt_digest=prompt_digest,
        raw_digest=raw_digest,
        prompt_chars=len(prompt),
        segments=segments,
        channel=channel,
        resumed=resumed,
        signals=signals,
    )

    if line is None:
        # No line was written, so the turn count does not advance, but this
        # process's launch-argument check has run: cache its pid so the next
        # turn is not checked again as the process's first (a typed second
        # turn would otherwise fail closed on the `ps` fallback).
        skipped: dict[str, Any] = {"turn": prior_turn, "claude_pid": claude_pid}
        if state and "last_seq" in state:
            skipped["last_seq"] = state["last_seq"]
        _save_state(ledger_dir, session, skipped)
        return None
    _save_state(ledger_dir, session, {"turn": turn, "claude_pid": claude_pid, "last_seq": line.seq})
    return line


def session_close(env: Mapping[str, str]) -> ledger.WitnessLine | None:
    """Append a ``kind="session-close"`` witness line for the session named
    by ``CLAUDE_CODE_SESSION_ID`` in *env*.

    Returns ``None`` (no-op, no line written) when that env var is absent --
    athenaeum's ``session-end`` runs unconditionally, including for
    non-Claude-Code callers that have no such session to close. ``turns``,
    ``last_seq`` and ``turns_witnessed`` are derived by scanning the ledger
    (the chain is authoritative; there is no per-session cache for these
    SessionEnd-only counts). Also ``None`` when ``ledger.lock`` is not taken
    within :data:`promptwitness.ledger.HOOK_LOCK_TIMEOUT` seconds.
    """
    session = env.get("CLAUDE_CODE_SESSION_ID")
    if not session:
        return None
    ledger_dir = ledger.resolve_ledger_dir("claude-code")
    turns = 0
    last_seq = 0
    turns_witnessed = 0
    for line in ledger.iter_lines(ledger_dir):
        if line.kind != "turn" or line.session != session:
            continue
        turns += 1
        if line.seq > last_seq:
            last_seq = line.seq
        if ledger.LedgerIndex.is_witnessable_human(line):
            turns_witnessed += 1
    return ledger.try_append_line(
        ledger_dir,
        kind="session-close",
        harness="claude-code",
        harness_version=str(env.get("CLAUDE_CODE_VERSION") or ""),
        hook_version=_HOOK_VERSION,
        session=session,
        turns=turns,
        last_seq=last_seq,
        turns_witnessed=turns_witnessed,
    )
