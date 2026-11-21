# SPDX-License-Identifier: Apache-2.0
"""Pull diagnosis: in the turns that got a push, how often did the agent PULL,
from Athenaeum or from native auto-memory (issue athenaeum#3032)?

The 2026-11-15 real-session report measured a recall-through of 0.036 on
interactive turns and 0.000 on headless ones. That number says the agent
rarely pulls from Athenaeum. It does not say whether the agent pulls any more
often from native memory, and it does not say why. This module counts, over
the same turn windows, the pulls the transcript can show.

**Unit.** A turn window: one human-authored user prompt
(:func:`athenaeum.transcript_verify._is_human_record`) and every record up to
the next one. A window is in scope only when at least one PROACTIVE push
record (ledger ``source`` ``sidecar`` or ``hook``; an MCP ``recall`` row is
the agent's own pull, not a push) has a timestamp inside it. This is the
2026-11-15 report's own task definition. A session with no human-authored
record at all (a ``claude -p`` run whose prompt opens with a harness wrapper
tag) falls back to its first non-meta user prompt as the only window start,
and the report counts how many sessions did so. Headless sessions contribute
their first pushed window only by default, as in that report.

**Counted per window (never relevance-conditioned).** Relevance needs labels,
and the labels behind the 2026-11-15 report live outside this repository. So
these are raw pull counts, the same for both sources, which keeps the
Athenaeum and native columns comparable with each other:

- ``recall`` and ``read_entity`` tool calls (any MCP server namespace);
- a native auto-memory TOPIC read: a ``Read`` of a file under a
  ``projects/<scope>/memory/`` directory other than the ``MEMORY.md`` index,
  or a ``Grep``/``Glob`` whose path or pattern is under one (the BUILT-IN
  file tools); or a call to any other tool whose input names such a path in
  a string field or a list of strings, at any depth (issue athenaeum#3056:
  plugins add their own file tools, and the reader should not depend on which
  one the host agent uses). The two are also reported apart, as
  ``native_topic_read_builtin`` and ``native_topic_read_other``. Two guards
  keep the other-tool rule a READ count: a write-capable tool (its bare name
  carries an edit/write/create/delete/... word, see
  :func:`is_write_capable_tool`) is never counted, and a field counts only
  when it starts as a path (no whitespace before the memory directory), so a
  sub-agent prompt that mentions a memory path in prose is not a read;
- a native INDEX read (``Read`` of ``MEMORY.md``), reported apart because
  Claude Code already loads the index at session start;
- a ``Bash`` command naming a memory directory, reported apart because a shell
  command is a weaker signal than a structured read;
- a ``ToolSearch`` call whose query names ``recall`` or ``athenaeum`` — the
  call a session must make before it can call a DEFERRED MCP tool.

**Recall-tool state, per session.** Claude Code records which tools it
deferred behind tool search in ``deferred_tools_delta`` attachments, and which
MCP servers failed, are pending or need auth. A session is
``recall_deferred`` when a delta names an MCP ``*__recall`` tool, and
``recall_server_failed`` when a failed/pending/needs-auth list names the
server that tool belongs to (or any server whose name contains
``athenaeum``). A transcript written before these attachments existed shows
neither, which is ``not_observed``, never "loaded".

**Privacy.** The report holds counts and rates only: no session id, page id,
path, query or transcript text. Every intermediate value is discarded once it
has been counted. ``tests/test_pull_diagnosis.py`` scans the serialized JSON
for the fixture identifiers.

**No model calls, read-only.** It reads the push ledger and the transcripts
and writes nothing.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.follow_through` (L4
sibling, for the shared tool-name and channel helpers),
:mod:`athenaeum.push_metrics` (L3) and :mod:`athenaeum.transcript_verify`
(L1). Consumed by :mod:`athenaeum._cmd_push_metrics` (the ``pull-diagnosis``
subcommand).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from athenaeum import follow_through, push_metrics, transcript_verify

#: Ledger ``source`` values that mark a PROACTIVE push. A row with no source
#: was written by the MCP ``recall`` path, which is the agent pulling.
PROACTIVE_PUSH_SOURCES: frozenset[str] = frozenset({"sidecar", push_metrics.SOURCE_HOOK})

#: A path segment pair that marks a Claude Code auto-memory directory:
#: ``.../projects/<scope>/memory/...``. Matches under any config root, so a
#: ``CLAUDE_CONFIG_DIR`` deployment is covered too.
_MEMORY_DIR_RE = re.compile(r"(?:^|/)projects/[^/\s'\"]+/memory(?:/|$|[\s'\"])")
NATIVE_INDEX_FILENAME = "MEMORY.md"

_TOOL_RECALL = "recall"
_TOOL_READ_ENTITY = "read_entity"
_TOOL_READ = "Read"
_TOOL_SEARCH_NATIVE = frozenset({"Grep", "Glob"})
_TOOL_BASH = "Bash"
_TOOL_TOOL_SEARCH = "ToolSearch"
_TOOL_SEARCH_QUERY_RE = re.compile(r"recall|athenaeum", re.IGNORECASE)

#: Words in a tool's bare name that mark it write-capable. Such a tool naming
#: a memory path is writing (or moving) the file, not reading it, so it is
#: never counted as a native read (issue athenaeum#3056).
_WRITE_TOOL_WORDS: frozenset[str] = frozenset(
    {
        "append",
        "copy",
        "cp",
        "create",
        "delete",
        "edit",
        "insert",
        "mkdir",
        "move",
        "multiedit",
        "mv",
        "patch",
        "put",
        "remove",
        "rename",
        "replace",
        "rm",
        "save",
        "update",
        "write",
    }
)
_ABSOLUTE_PATH_RE = re.compile(r"(?:/|~/|[A-Za-z]:/)")
_NAME_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

#: Per-window boolean signals. Each is counted as "windows where it happened
#: at least once"; :data:`CALL_SIGNALS` are also summed as call totals.
SIGNAL_RECALL = "recall_call"
SIGNAL_READ_ENTITY = "read_entity_call"
SIGNAL_RECALL_OR_READ = "athenaeum_pull"
SIGNAL_NATIVE_TOPIC = "native_topic_read"
SIGNAL_NATIVE_TOPIC_BUILTIN = "native_topic_read_builtin"
SIGNAL_NATIVE_TOPIC_OTHER = "native_topic_read_other"
#: The breakdown of :data:`SIGNAL_NATIVE_TOPIC` by tool kind; a topic read is
#: classified as exactly one of these, and counts toward the total as well.
_NATIVE_TOPIC_PARTS = frozenset({SIGNAL_NATIVE_TOPIC_BUILTIN, SIGNAL_NATIVE_TOPIC_OTHER})
SIGNAL_NATIVE_INDEX = "native_index_read"
SIGNAL_NATIVE_BASH = "native_bash_read"
SIGNAL_TOOL_SEARCH = "tool_search_for_recall"
WINDOW_SIGNALS: tuple[str, ...] = (
    SIGNAL_RECALL,
    SIGNAL_READ_ENTITY,
    SIGNAL_RECALL_OR_READ,
    SIGNAL_NATIVE_TOPIC,
    SIGNAL_NATIVE_TOPIC_BUILTIN,
    SIGNAL_NATIVE_TOPIC_OTHER,
    SIGNAL_NATIVE_INDEX,
    SIGNAL_NATIVE_BASH,
    SIGNAL_TOOL_SEARCH,
)
CALL_SIGNALS: tuple[str, ...] = (
    SIGNAL_RECALL,
    SIGNAL_READ_ENTITY,
    SIGNAL_NATIVE_TOPIC,
    SIGNAL_NATIVE_TOPIC_BUILTIN,
    SIGNAL_NATIVE_TOPIC_OTHER,
    SIGNAL_NATIVE_INDEX,
    SIGNAL_NATIVE_BASH,
    SIGNAL_TOOL_SEARCH,
)

STATE_RECALL_DEFERRED = "recall_deferred"
STATE_RECALL_SERVER_FAILED = "recall_server_failed"
STATE_RECALL_CALLED = "recall_called"
STATE_NOT_OBSERVED = "not_observed"
SESSION_STATES: tuple[str, ...] = (
    STATE_RECALL_DEFERRED,
    STATE_RECALL_SERVER_FAILED,
    STATE_RECALL_CALLED,
    STATE_NOT_OBSERVED,
)

_SERVER_STATUS_KEYS = ("failedMcpServers", "pendingMcpServers", "needsAuthMcpServers")


def is_memory_dir_path(text: str) -> bool:
    """True when *text* names a path inside an auto-memory directory."""
    return bool(_MEMORY_DIR_RE.search(text.replace("\\", "/")))


def is_write_capable_tool(name: object) -> bool:
    """True when the tool's bare name carries a write word (``Edit``,
    ``Write``, ``MultiEdit``, ``NotebookEdit``, ``create_file``, ...).

    The name is split on ``_``/``-``/camel case and each lower-cased word is
    looked up in :data:`_WRITE_TOOL_WORDS`, so ``Read`` and ``Search`` are not
    write-capable while ``mcp__plugin__Edit`` and ``write_file`` are.
    """
    bare = follow_through._bare_tool_name(name)
    words = {
        w.lower() for part in re.split(r"[_\-\s.]+", bare) for w in _NAME_WORD_RE.findall(part)
    }
    return bool(words & _WRITE_TOOL_WORDS)


def _input_strings(value: object) -> Iterable[str]:
    """Every string in a tool input, through nested dicts and lists."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for inner in value.values():
            yield from _input_strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _input_strings(inner)


def _names_memory_topic_path(text: str) -> bool:
    """True when *text* starts as a path into an auto-memory directory and is
    not the ``MEMORY.md`` index.

    "Starts as a path": no line break at all, and either no whitespace before
    the memory directory or a field that opens as an absolute path (``/``,
    ``~`` or a drive letter), so ``/Users/Ann Lee/.claude/...`` counts. A field
    holding a path or a glob counts while prose that mentions one (a sub-agent
    prompt, a message) does not.
    """
    norm = text.strip().replace("\\", "/")
    if "\n" in norm:
        return False
    match = _MEMORY_DIR_RE.search(norm)
    if match is None:
        return False
    prefix = norm[: match.start()]
    if any(c.isspace() for c in prefix) and not _ABSOLUTE_PATH_RE.match(prefix):
        return False
    return PurePosixPath(norm).name != NATIVE_INDEX_FILENAME


def _content_blocks(rec: dict[str, Any]) -> list[Any]:
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else rec.get("content")
    return content if isinstance(content, list) else []


def _classify_tool_use(block: dict[str, Any]) -> str | None:
    """The :data:`CALL_SIGNALS` member one ``tool_use`` block counts as, or ``None``."""
    name = block.get("name")
    bare = follow_through._bare_tool_name(name)
    raw_input = block.get("input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    is_mcp = isinstance(name, str) and name.startswith("mcp__")
    if is_mcp and bare == _TOOL_RECALL:
        return SIGNAL_RECALL
    if is_mcp and bare == _TOOL_READ_ENTITY:
        return SIGNAL_READ_ENTITY
    if name == _TOOL_READ:
        path = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(path, str) and is_memory_dir_path(path):
            if PurePosixPath(path.replace("\\", "/")).name == NATIVE_INDEX_FILENAME:
                return SIGNAL_NATIVE_INDEX
            return SIGNAL_NATIVE_TOPIC_BUILTIN
        return None
    if name in _TOOL_SEARCH_NATIVE:
        for key in ("path", "pattern"):
            val = tool_input.get(key)
            if isinstance(val, str) and is_memory_dir_path(val):
                return SIGNAL_NATIVE_TOPIC_BUILTIN
        return None
    if name == _TOOL_BASH:
        cmd = tool_input.get("command")
        if isinstance(cmd, str) and is_memory_dir_path(cmd):
            return SIGNAL_NATIVE_BASH
        return None
    if name == _TOOL_TOOL_SEARCH:
        query = tool_input.get("query")
        if isinstance(query, str) and _TOOL_SEARCH_QUERY_RE.search(query):
            return SIGNAL_TOOL_SEARCH
        return None
    if is_write_capable_tool(name):
        return None
    if any(_names_memory_topic_path(s) for s in _input_strings(raw_input)):
        return SIGNAL_NATIVE_TOPIC_OTHER
    return None


def _session_recall_state(records: Sequence[object]) -> set[str]:
    """The :data:`SESSION_STATES` that apply to one session (several may)."""
    states: set[str] = set()
    recall_servers: set[str] = set()
    status_servers: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("type") == "attachment":
            att = rec.get("attachment")
            if not isinstance(att, dict):
                continue
            if att.get("type") == "deferred_tools_delta":
                added = att.get("addedNames")
                for tool in added if isinstance(added, list) else ():
                    if (
                        isinstance(tool, str)
                        and tool.startswith("mcp__")
                        and follow_through._bare_tool_name(tool) == _TOOL_RECALL
                    ):
                        states.add(STATE_RECALL_DEFERRED)
                        recall_servers.add(tool.split("__")[1])
            for key in _SERVER_STATUS_KEYS:
                vals = att.get(key)
                for server in vals if isinstance(vals, list) else ():
                    if isinstance(server, str):
                        status_servers.add(server)
                    elif isinstance(server, dict) and isinstance(server.get("name"), str):
                        status_servers.add(server["name"])
            continue
        for block in _content_blocks(rec):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if _classify_tool_use(block) == SIGNAL_RECALL:
                    states.add(STATE_RECALL_CALLED)
                    name = block.get("name")
                    if isinstance(name, str):
                        recall_servers.add(name.split("__")[1])
    if any(s in recall_servers or "athenaeum" in s.lower() for s in status_servers):
        states.add(STATE_RECALL_SERVER_FAILED)
    if not states:
        states.add(STATE_NOT_OBSERVED)
    return states


def _is_fallback_prompt(rec: object) -> bool:
    """A non-meta user record with text, for sessions with no human-authored one."""
    if not isinstance(rec, dict) or rec.get("isMeta") is True:
        return False
    if not transcript_verify._is_user_record(rec):
        return False
    return bool(transcript_verify._non_tool_result_text(rec).strip())


def _window_starts(records: Sequence[object]) -> tuple[list[int], bool]:
    """Record indexes that open a turn window, and whether the fallback was used."""
    starts = [i for i, rec in enumerate(records) if transcript_verify._is_human_record(rec)]
    if starts:
        return starts, False
    for i, rec in enumerate(records):
        if _is_fallback_prompt(rec):
            return [i], True
    return [], False


def _aware(ts: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC, so ledger and transcript times compare."""
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def _record_ts(rec: object) -> datetime | None:
    if not isinstance(rec, dict):
        return None
    return _aware(push_metrics._parse_ts(rec.get("timestamp")))


def _proactive_push_times(
    push_records: Iterable[dict[str, Any]], *, before: datetime | None
) -> dict[str, list[datetime]]:
    """``session_id -> sorted proactive push timestamps`` (before *before*, if set)."""
    by_session: dict[str, list[datetime]] = {}
    for rec in push_records:
        if rec.get("source") not in PROACTIVE_PUSH_SOURCES:
            continue
        sid = rec.get("session_id")
        ts = _aware(push_metrics._parse_ts(rec.get("ts")))
        if not isinstance(sid, str) or not sid or ts is None:
            continue
        if before is not None and ts >= before:
            continue
        by_session.setdefault(sid, []).append(ts)
    for times in by_session.values():
        times.sort()
    return by_session


@dataclass
class ChannelTally:
    """Counts for one channel bucket (interactive / headless / unknown)."""

    sessions: int = 0
    windows: int = 0
    window_counts: Counter[str] = field(default_factory=Counter)
    call_counts: Counter[str] = field(default_factory=Counter)
    session_states: Counter[str] = field(default_factory=Counter)
    fallback_prompt_sessions: int = 0

    def to_dict(self) -> dict[str, Any]:
        windows = {s: self.window_counts.get(s, 0) for s in WINDOW_SIGNALS}
        return {
            "sessions": self.sessions,
            "windows": self.windows,
            "windows_with": windows,
            "window_rates": {
                s: (n / self.windows if self.windows else None) for s, n in windows.items()
            },
            "calls": {s: self.call_counts.get(s, 0) for s in CALL_SIGNALS},
            "session_states": {s: self.session_states.get(s, 0) for s in SESSION_STATES},
            "fallback_prompt_sessions": self.fallback_prompt_sessions,
        }


@dataclass
class PullDiagnosisReport:
    """Counts/rates-only report. See :meth:`to_dict` for the JSON shape."""

    sessions_in_ledger: int
    sessions_skipped_no_transcript: int
    sessions_without_pushed_window: int
    before: str | None
    headless_first_window_only: bool
    channels: dict[str, ChannelTally]

    def to_dict(self) -> dict[str, Any]:
        """The documented JSON output::

            {
              "v": 1,
              "sessions_in_ledger": int,          # sessions with >=1 proactive push
              "sessions_skipped_no_transcript": int,
              "sessions_without_pushed_window": int,
              "before": str | null,               # push cutoff, ISO-8601
              "headless_first_window_only": bool,
              "channels": {
                "interactive" | "headless" | "unknown": {
                  "sessions": int, "windows": int,
                  "windows_with": {<signal>: int, ...},
                  "window_rates": {<signal>: float | null, ...},
                  "calls": {<signal>: int, ...},
                  "session_states": {"recall_deferred": int,
                                     "recall_server_failed": int,
                                     "recall_called": int,
                                     "not_observed": int},
                  "fallback_prompt_sessions": int
                }
              }
            }

        ``window_rates`` is ``null`` for a channel with no windows.
        ``native_topic_read`` is the sum of ``native_topic_read_builtin``
        (``Read``/``Grep``/``Glob``) and ``native_topic_read_other`` (any
        other non-writing tool) in ``calls``; in ``windows_with`` it is the
        windows with either, so it can be less than the two added. Session
        states may overlap (a session can defer ``recall`` and then call it),
        except ``not_observed``, which means none of the other three.
        """
        return {
            "v": 1,
            "sessions_in_ledger": self.sessions_in_ledger,
            "sessions_skipped_no_transcript": self.sessions_skipped_no_transcript,
            "sessions_without_pushed_window": self.sessions_without_pushed_window,
            "before": self.before,
            "headless_first_window_only": self.headless_first_window_only,
            "channels": {ch: self.channels[ch].to_dict() for ch in follow_through.CHANNEL_BUCKETS},
        }


def build_pull_diagnosis_report(
    *,
    cache_dir: Path | None = None,
    projects_root: Path | None = None,
    before: datetime | None = None,
    headless_first_window_only: bool = True,
) -> PullDiagnosisReport:
    """Build the report over the push ledger at *cache_dir* and the transcripts
    under *projects_root*.

    *before* drops proactive pushes at or after that instant (the 2026-11-15
    report used 2026-11-15T00:00:00Z). Only sessions in the push ledger are
    read, as in :func:`athenaeum.follow_through.build_follow_through_report`.
    """
    root = projects_root if projects_root is not None else transcript_verify.default_projects_root()
    before = _aware(before)
    pushes = _proactive_push_times(push_metrics.read_push_records(cache_dir), before=before)
    channels = {ch: ChannelTally() for ch in follow_through.CHANNEL_BUCKETS}
    skipped = 0
    no_window = 0

    for session_id, push_times in pushes.items():
        located = push_metrics._find_session_transcript(session_id, root)
        records = transcript_verify._iter_session_records(located[0], session_id) if located else []
        if not records:
            skipped += 1
            continue
        channel = follow_through._session_channel_bucket(records)
        starts, used_fallback = _window_starts(records)
        bounds = list(zip(starts, starts[1:] + [len(records)], strict=True))

        pushed_windows: list[tuple[int, int]] = []
        for lo, hi in bounds:
            t_lo = _record_ts(records[lo])
            if t_lo is None:
                continue
            t_hi = next((t for t in (_record_ts(r) for r in records[hi:]) if t is not None), None)
            # Push ``ts`` is stamped at whole seconds; transcript timestamps
            # carry milliseconds, and the hook fires within the same second as
            # its prompt. Compare at second resolution (as follow_through does)
            # so a push is never placed before the prompt that triggered it.
            lo_s = t_lo.replace(microsecond=0)
            hi_s = t_hi.replace(microsecond=0) if t_hi is not None else None
            if any(t >= lo_s and (hi_s is None or t < hi_s) for t in push_times):
                pushed_windows.append((lo, hi))
        if channel == follow_through.CHANNEL_HEADLESS and headless_first_window_only:
            pushed_windows = pushed_windows[:1]
        if not pushed_windows:
            no_window += 1
            continue

        tally = channels[channel]
        tally.sessions += 1
        tally.fallback_prompt_sessions += int(used_fallback)
        for state in _session_recall_state(records):
            tally.session_states[state] += 1
        for lo, hi in pushed_windows:
            tally.windows += 1
            seen: set[str] = set()
            for rec in records[lo:hi]:
                if not isinstance(rec, dict):
                    continue
                for block in _content_blocks(rec):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        signal = _classify_tool_use(block)
                        if signal is not None:
                            tally.call_counts[signal] += 1
                            seen.add(signal)
                            if signal in _NATIVE_TOPIC_PARTS:
                                tally.call_counts[SIGNAL_NATIVE_TOPIC] += 1
                                seen.add(SIGNAL_NATIVE_TOPIC)
            if seen & {SIGNAL_RECALL, SIGNAL_READ_ENTITY}:
                seen.add(SIGNAL_RECALL_OR_READ)
            for signal in seen:
                tally.window_counts[signal] += 1

    return PullDiagnosisReport(
        sessions_in_ledger=len(pushes),
        sessions_skipped_no_transcript=skipped,
        sessions_without_pushed_window=no_window,
        before=before.isoformat() if before is not None else None,
        headless_first_window_only=headless_first_window_only,
        channels=channels,
    )
