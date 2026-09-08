# SPDX-License-Identifier: Apache-2.0
"""Recover the originating session of a natively-written memory (issue athenaeum#1452).

Claude Code's *native* auto-memory writer emits a fixed frontmatter schema
(``name`` / ``description`` / ``metadata.type``). It carries no ``sources[]``
and no ``originSessionId``, so BOTH provenance paths into the librarian are
dead by construction for the memories it writes: ``merge._am_as_implicit_source``
returns ``None`` the moment ``origin_session_id`` is absent, and the compiled
wiki page lands with ``sources: []``.

This module closes that gap at INTAKE, not by scraping the memory body. It
recovers the session id the writer never wrote, from the two structural facts
Claude Code's own on-disk layout gives us:

1. A scope directory holds BOTH a project's session transcripts
   (``<projects_root>/<scope>/*.jsonl``) and that project's native memory
   files (``<projects_root>/<scope>/memory/*.md``). The scope name is the
   join key — no mapping table, no inference.
2. The raw intake copy under ``raw/auto-memory/<scope>/`` keeps the memory
   file's NAME, and (because intake hardlinks rather than copies — see
   :mod:`athenaeum.retire`) its MTIME is the real write time.

Recovery ladder — most direct first, first hit wins, never a guess:

``write-cited``
    A transcript in the scope contains a *writing* tool-use (``Write`` /
    ``Edit`` / ``MultiEdit`` / ``NotebookEdit``, including ``mcp__…__Edit``
    variants) whose file-path argument names this very memory file. That is
    an exact attribution: the session demonstrably wrote the file. When more
    than one session wrote it, the LATEST writer wins — the file's current
    content is what intake is attributing, and that is what the last write
    left behind.

``time-window``
    The memory's write timestamp falls inside the ``[first, last]`` record
    window of EXACTLY ONE session in the scope. Two overlapping candidates
    is an ambiguity, not a coin flip: we return ``None``.

Nothing else resolves. A scope with no transcripts, a rolled-off transcript,
or an ambiguous window all yield ``None`` — which is precisely today's
behavior, so a failed recovery can never be worse than not trying.

**What this module does NOT do.** It does not upgrade a claim's
``source_type``. A recovered session is an *origin*, not a verification: the
compiled source keeps the honest ``inferred`` default
(:data:`athenaeum.models.DEFAULT_SOURCE_TYPE`) unless
:func:`athenaeum.transcript_verify.verify_user_stated` later confirms the
claim against the transcript itself. Recovery only makes that existing
verification reachable — it never pre-empts its verdict. The
never-cite-the-raw-filename invariant is likewise untouched: the recovered
ref is session-anchored, and this module never emits a source entry at all.

Layering: L0/L1-boundary primitive, the same rung as
:mod:`athenaeum.transcript_verify`. Imports stdlib ONLY — no models, no
config, no LLM client, no writes anywhere. Factoring rule: this module owns
ONLY the "which session wrote this file" question. It must never read a
memory body, never classify a claim (that is ``transcript_verify``'s job and
it must stay the sole owner of ``source_type`` resolution), and never write,
mutate, or delete a transcript.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

#: Basis values reported on a :class:`RecoveredOrigin`.
BASIS_WRITE_CITED = "write-cited"
BASIS_TIME_WINDOW = "time-window"

#: Tool names that WRITE a file. A ``Read`` of a memory file proves nothing
#: about who authored it — and memory files are read constantly (recall
#: injection), so a read-inclusive match would attribute a memory to whichever
#: session happened to look at it. Matching is on the last ``__``-delimited
#: segment so MCP-namespaced equivalents (``mcp__plugin_woz_code__Edit``)
#: count as the same tool.
_WRITING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

#: Keys a tool-use ``input`` may use for its target path.
_PATH_KEYS = ("file_path", "notebook_path", "path")

#: Cheap per-line prefilter, and the fallback needle when no scope is known.
#: Only lines that could possibly mention a memory file get JSON-parsed, which
#: keeps the scan linear in bytes rather than in records for the overwhelming
#: majority of transcript lines. It must stay a SUPERSET of the anchored
#: ``/<scope>/memory/`` needle, or the prefilter would drop lines the match
#: would have accepted.
_MEMORY_HINT = "/memory/"

_TIMESTAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')

#: How many trailing lines to keep so the window's upper bound survives a
#: malformed or timestamp-less final line.
_TAIL_LINES = 8


@dataclass(frozen=True)
class RecoveredOrigin:
    """A recovered originating session for a memory file.

    Attributes:
        session_id: The session that wrote the memory file.
        basis: :data:`BASIS_WRITE_CITED` or :data:`BASIS_TIME_WINDOW` — which
            rung of the ladder resolved it, so an audit can tell an exact
            attribution from a windowed one.
        turn: Always ``None``. Claude Code's native writer emits no turn
            index and this module deliberately does not invent one: a
            fabricated ``#turnN`` ref would be a WRONG citation, which is
            strictly worse than the bare-session ref the absent turn yields
            (see ``transcript_verify._best_effort_ref``). The field exists so
            a future writer that DOES stamp a turn has somewhere to put it.
    """

    session_id: str
    basis: str
    turn: int | None = None


def default_projects_root() -> Path:
    """Resolve the transcript/memory home, honoring ``CLAUDE_CONFIG_DIR``.

    Mirrors :func:`athenaeum.transcript_verify.default_projects_root` exactly
    — same env var, same fallback, resolved at CALL time so a relocated
    ``~/.claude`` is honored with no per-caller plumbing. Duplicated rather
    than imported to keep this module's stdlib-only layering (importing
    ``transcript_verify`` would be harmless today but couples an L0 primitive
    to a sibling for four lines).
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    return base / "projects"


def _parse_timestamp(raw: str) -> datetime | None:
    """Parse an ISO-8601 transcript timestamp to an aware UTC datetime.

    Claude Code emits ``2026-09-08T03:42:52.760Z``. Returns ``None`` for
    anything unparseable — a malformed line must never abort a scan.
    """
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def written_at_from_frontmatter(meta: dict[str, object] | None) -> datetime | None:
    """Read a memory file's declared write time from its frontmatter.

    Claude Code stamps ``modified`` from 2.1.214 onward. It is ABSENT on
    every file in the corpus that motivated issue athenaeum#1452 (measured
    2026-09-08: 0 of 42 raw auto-memory files carry it), so this is the
    forward-looking signal, not the load-bearing one — callers fall back to
    the file's mtime, which the hardlinked intake copy preserves. Returns
    ``None`` when the key is absent or unparseable.
    """
    if not meta:
        return None
    raw = meta.get("modified")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str):
        return None
    return _parse_timestamp(raw)


def _tool_use_paths(record: object) -> list[str]:
    """Target paths of every WRITING tool-use in a transcript record.

    Returns ``[]`` for any other record shape — a non-dict, a record with no
    content list, a read-only tool, or a tool-use whose input names no path.
    """
    if not isinstance(record, dict):
        return []
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else record.get("content")
    if not isinstance(content, list):
        return []
    paths: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str):
            continue
        if name.rsplit("__", 1)[-1] not in _WRITING_TOOLS:
            continue
        payload = block.get("input")
        if not isinstance(payload, dict):
            continue
        for key in _PATH_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
    return paths


@dataclass(frozen=True)
class _ScopeIndex:
    """One scope's transcripts, reduced to the two facts recovery needs.

    Attributes:
        writes: ``{memory filename: (session id, write time)}`` — the LATEST
            writing tool-use naming that file, across every transcript in the
            scope.
        windows: ``[(first, last, session id)]`` — each session's record-time
            span.
    """

    writes: dict[str, tuple[str, datetime]]
    windows: list[tuple[datetime, datetime, str]]


def _scan_transcript(
    jsonl: Path, scope: str = ""
) -> tuple[dict[str, datetime], datetime | None, datetime | None]:
    """One linear pass over a transcript: memory writes + its time window.

    Returns ``({memory filename: latest write time}, first, last)``. An
    unreadable file yields empty results rather than raising — a transcript
    that rolled off mid-scan must degrade to "cannot recover", not to a
    crashed intake.

    ``scope`` narrows the accepted write targets to ``<scope>/memory/…`` —
    THIS scope's own memory directory. Without it, a session that edited some
    OTHER project's memory file (agents do reach across projects) would index
    that basename here, and a same-named memory in this scope would then be
    attributed to a session that never wrote it. Fabricated provenance is the
    worst failure this module can have, so the match is anchored rather than
    merely ``"/memory/" in target``. An empty ``scope`` keeps the loose match
    (used only where no scope is known).
    """
    needle = f"/{scope}/memory/" if scope else _MEMORY_HINT
    writes: dict[str, datetime] = {}
    first: datetime | None = None
    last: datetime | None = None
    tail: deque[str] = deque(maxlen=_TAIL_LINES)
    try:
        with jsonl.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                tail.append(line)
                if first is None:
                    match = _TIMESTAMP_RE.search(line)
                    if match:
                        first = _parse_timestamp(match.group(1))
                if _MEMORY_HINT not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                paths = _tool_use_paths(record)
                if not paths:
                    continue
                stamp = None
                if isinstance(record, dict):
                    raw_ts = record.get("timestamp")
                    if isinstance(raw_ts, str):
                        stamp = _parse_timestamp(raw_ts)
                for target in paths:
                    if needle not in target:
                        continue
                    name = target.rsplit("/", 1)[-1]
                    if not name:
                        continue
                    prior = writes.get(name)
                    # A later write supersedes an earlier one; a write with no
                    # readable timestamp only fills an otherwise-empty slot.
                    if prior is None:
                        writes[name] = stamp or datetime.min.replace(tzinfo=timezone.utc)
                    elif stamp is not None and stamp >= prior:
                        writes[name] = stamp
    except OSError:
        return {}, None, None
    # The window's upper bound: the newest timestamp among the trailing lines.
    for line in reversed(tail):
        match = _TIMESTAMP_RE.search(line)
        if match:
            last = _parse_timestamp(match.group(1))
            if last is not None:
                break
    return writes, first, last


class SessionRecoverer:
    """Recovers originating sessions for one intake pass.

    Holds a per-scope index so a run that discovers N memories in a scope
    scans that scope's transcripts ONCE, not N times. Construct one per
    intake pass and discard it — the index is a snapshot, and transcripts are
    appended to live.

    Args:
        projects_root: Transcript/memory home. Defaults to
            :func:`default_projects_root`; inject a temp dir in tests so a
            test never reads the operator's real sessions.
    """

    def __init__(self, projects_root: Path | None = None) -> None:
        self._root = projects_root if projects_root is not None else default_projects_root()
        self._cache: dict[str, _ScopeIndex] = {}

    def _index_for(self, scope: str) -> _ScopeIndex:
        cached = self._cache.get(scope)
        if cached is not None:
            return cached
        writes: dict[str, tuple[str, datetime]] = {}
        windows: list[tuple[datetime, datetime, str]] = []
        scope_dir = self._root / scope
        if scope_dir.is_dir():
            for jsonl in sorted(scope_dir.glob("*.jsonl")):
                session_id = jsonl.stem
                if not session_id:
                    continue
                found, first, last = _scan_transcript(jsonl, scope)
                for name, stamp in found.items():
                    prior = writes.get(name)
                    if prior is None or stamp >= prior[1]:
                        writes[name] = (session_id, stamp)
                if first is not None and last is not None and first <= last:
                    windows.append((first, last, session_id))
        index = _ScopeIndex(writes=writes, windows=windows)
        self._cache[scope] = index
        return index

    def recover(
        self,
        memory_path: Path,
        scope: str,
        written_at: datetime | None = None,
    ) -> RecoveredOrigin | None:
        """Recover the session that wrote ``memory_path``, or ``None``.

        Args:
            memory_path: The memory file. Only its NAME is read here — the
                body is never opened, and the path may be the raw intake copy
                rather than the native original (they share a name).
            scope: The scope directory name, verbatim (e.g.
                ``-Users-alice-Code-projectx`` or ``_unscoped``).
            written_at: The memory's write time, when the caller already
                parsed it from frontmatter (see
                :func:`written_at_from_frontmatter`). Falls back to the file's
                mtime, which the hardlinked intake copy preserves.

        Returns:
            A :class:`RecoveredOrigin`, or ``None`` when nothing resolves it
            unambiguously — never a guess.
        """
        if not scope:
            return None
        index = self._index_for(scope)

        cited = index.writes.get(memory_path.name)
        if cited is not None:
            return RecoveredOrigin(session_id=cited[0], basis=BASIS_WRITE_CITED)

        stamp = written_at
        if stamp is None:
            try:
                stamp = datetime.fromtimestamp(memory_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                return None
        elif stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        else:
            stamp = stamp.astimezone(timezone.utc)

        matches = [sid for first, last, sid in index.windows if first <= stamp <= last]
        if len(matches) != 1:
            # Zero candidates (transcript rolled off) and several overlapping
            # candidates (concurrent sessions in one project) are BOTH
            # unresolved. Picking one would fabricate provenance.
            return None
        return RecoveredOrigin(session_id=matches[0], basis=BASIS_TIME_WINDOW)
