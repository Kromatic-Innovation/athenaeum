# SPDX-License-Identifier: Apache-2.0
"""Live-session guard for the move-then-retire pass (issue athenaeum#1728).

Contract: a raw auto-memory file is eligible for the move-then-retire pass
(:mod:`athenaeum.retire`) only when the Claude Code session that OWNS its
scope is closed. Retiring while that session is still live races an agent
that might still amend, correct, or contradict the very fact this pass is
about to move into the wiki and ``git rm`` from ``raw/``.

Two signals, checked in order, first hit wins:

1. **Session-end marker, scoped to the file's OWNING session.**
   :func:`record_session_end` is called from
   :func:`athenaeum.librarian.session_end` after a session's SessionEnd hook
   completes, and stamps a small JSON marker under
   ``<cache_dir>/live-session-markers/<scope>.json`` naming the scope, the
   ENDING session id, and the timestamp. That marker releases a candidate
   file's hold only when the marker's session id equals the file's OWNING
   session id (:func:`resolve_owning_session_id` -- ``originSessionId``
   frontmatter first, else the same :class:`~athenaeum.session_recovery.SessionRecoverer`
   join :func:`athenaeum.intake.discover_auto_memory_files` uses) AND the
   marker is newer than the file's mtime. A scope can hold more than one
   live session concurrently (two agents, two terminals, one project) --
   a marker recorded for session A saying "A has ended" must never be read
   as "this scope is quiet" for a file that session B, still live, owns.
   Regression: Quine's ``scenario_multi_session.py`` demonstrated the pass
   moving a file B owned, and its ``MEMORY.md`` pointer with it, off a
   session-A-only marker while B was still writing.
2. **Quiet window, over EVERY transcript in the scope.** Consulted whenever
   rung 1 does not release the hold -- no marker, a marker for a different
   session, a marker older than the file, OR the file's owning session could
   not be determined at all (an unresolved owner never reads as "known
   closed"; it falls through here instead of skipping straight to a bare
   release). Any ``<projects_root>/<scope>/*.jsonl`` transcript -- of ANY
   session sharing the scope, not just the (perhaps unknown) owner's -- has
   modified within the configurable quiet window (default
   :data:`athenaeum.config.DEFAULT_LIVE_SESSION_GUARD_QUIET_WINDOW_SECONDS`,
   30 minutes) of "now" holds the file. No transcript activity within the
   window across the WHOLE scope -- including no transcripts at all --
   releases the hold.

Scope naming mirrors :mod:`athenaeum.transcript_verify` and
:mod:`athenaeum.session_recovery` exactly: the raw auto-memory scope
directory name (``raw/auto-memory/<scope>/``) IS the Claude Code project
scope directory name (``<projects_root>/<scope>/``) -- no mapping table, no
inference (see :func:`athenaeum.intake.discover_auto_memory_files`).

Best-effort throughout: a missing/corrupt marker or an unreadable transcript
directory degrades to "no signal", never raises. A held file is NEVER
silently dropped -- :mod:`athenaeum.retire` counts and reports every hold
this guard produces (``RetireReport.held_live_session``), in both a real run
and ``--dry-run``.

Layering: L2 leaf. Imports :mod:`athenaeum.atomic_io` and
:mod:`athenaeum.store` (both L0), plus :mod:`athenaeum.models` (L1 hub, for
``parse_frontmatter``) and :mod:`athenaeum.session_recovery` (L0/L1-boundary
primitive, for the owner-recovery fallback) -- no config, no LLM client.
Callers (:mod:`athenaeum.retire`) resolve config-driven defaults themselves
and pass plain values in.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter
from athenaeum.session_recovery import SessionRecoverer, written_at_from_frontmatter
from athenaeum.store import now_iso

log = logging.getLogger(__name__)

#: Subdirectory of the cache dir holding one marker file per scope.
MARKER_DIRNAME = "live-session-markers"


def marker_path(cache_dir: Path, scope: str) -> Path:
    """Path to *scope*'s session-end marker under *cache_dir*."""
    return cache_dir / MARKER_DIRNAME / f"{scope}.json"


def record_session_end(
    session_id: str | None,
    *,
    cache_dir: Path,
    projects_root: Path,
) -> str | None:
    """Best-effort: stamp a session-end marker for *session_id*'s owning scope.

    Resolves the scope by finding the (unique) ``<projects_root>/<scope>/
    <session_id>.jsonl`` transcript -- the same join key
    :mod:`athenaeum.session_recovery` uses, run in reverse (session -> scope
    instead of scope -> session). Zero matches (transcript not yet flushed,
    or already rolled off) and multiple matches (should not happen -- a
    session id collision across scopes) are both treated as "cannot resolve"
    and produce no write, never a guess.

    Called from :func:`athenaeum.librarian.session_end`'s best-effort tail,
    mirroring :func:`athenaeum.push_metrics.run_reference_determination`'s
    contract: never raises, a failure here must not break ``session_end``.

    Returns the scope the marker was written for, or ``None`` on any
    no-op/failure path.
    """
    if not session_id:
        return None
    try:
        matches = sorted(projects_root.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    if len(matches) != 1:
        return None
    scope = matches[0].parent.name
    try:
        path = marker_path(cache_dir, scope)
        atomic_write_text(
            path,
            json.dumps({"session": session_id, "ts": now_iso()}, sort_keys=True) + "\n",
        )
    except OSError:
        log.warning(
            "live-session-guard: failed to write session-end marker for scope %s",
            scope,
        )
        return None
    return scope


def _parse_ts(raw: str) -> float | None:
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _read_marker(cache_dir: Path, scope: str) -> dict[str, object] | None:
    """Read *scope*'s marker as a ``{"session": ..., "ts": ...}`` dict, or ``None``.

    Fail-open: a missing, corrupt, or unreadable marker is exactly
    "no signal from this rung" -- it never raises and never blocks the
    fallback quiet-window check.
    """
    path = marker_path(cache_dir, scope)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def resolve_owning_session_id(
    member: Path,
    scope: str,
    recoverer: SessionRecoverer,
) -> str | None:
    """The Claude Code session that OWNS *member*, or ``None`` if undetermined.

    Mirrors :func:`athenaeum.intake.discover_auto_memory_files`'s own
    resolution order (``src/athenaeum/intake.py`` ~498-543) exactly, so the
    guard's notion of "owner" never disagrees with the provenance the
    compiled wiki page already carries for the same file:

    1. The file's own ``originSessionId`` frontmatter, when it declares one
       -- the file's own claim always outranks an inferred one.
    2. Failing that, :class:`~athenaeum.session_recovery.SessionRecoverer`'s
       write-cited / time-window ladder over *scope*'s transcripts (the same
       recoverer :func:`athenaeum.intake.discover_auto_memory_files` uses,
       passed in here so a caller iterating many members in one scope scans
       that scope's transcripts once, not once per member).

    Returns ``None`` when neither resolves -- an honest "cannot determine",
    never a guess. Unreadable file content degrades the same way.
    """
    try:
        text = member.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, _body = parse_frontmatter(text)
    origin_session_id = meta.get("originSessionId") if meta else None
    if origin_session_id is not None:
        return str(origin_session_id)
    recovered = recoverer.recover(member, scope, written_at=written_at_from_frontmatter(meta))
    return recovered.session_id if recovered is not None else None


def is_live(
    scope: str,
    file_mtime: float,
    *,
    cache_dir: Path,
    projects_root: Path,
    quiet_window_seconds: int,
    owner_session_id: str | None,
    now: float | None = None,
) -> tuple[bool, str]:
    """Decide whether *scope*'s owning session is still live.

    Args:
        scope: The origin-scope directory name (matches both
            ``raw/auto-memory/<scope>/`` and ``<projects_root>/<scope>/``).
        file_mtime: The candidate memory file's mtime (POSIX seconds) -- the
            session-end marker rung compares against this, not against
            "now", so a marker written before the file was last touched does
            NOT release the hold.
        cache_dir: Root holding :data:`MARKER_DIRNAME`.
        projects_root: Claude Code transcript home
            (``<projects_root>/<scope>/*.jsonl``).
        quiet_window_seconds: Fallback quiet window in seconds.
        owner_session_id: The candidate file's OWNING session id, from
            :func:`resolve_owning_session_id` -- ``None`` when it could not
            be determined. The marker rung is consulted only when this is
            not ``None`` AND the scope's marker was recorded for this exact
            session; an unresolved owner (or a marker for some OTHER session
            sharing the scope) skips straight to the quiet-window rung.
        now: Injectable current time (POSIX seconds); defaults to
            :func:`time.time`.

    Returns:
        ``(held, reason)``. ``held=True`` means the caller must NOT retire
        this file this pass; ``reason`` is a human-readable explanation for
        the run summary / dry-run report, empty when ``held=False``.
    """
    resolved_now = now if now is not None else time.time()

    if owner_session_id is not None:
        marker = _read_marker(cache_dir, scope)
        if marker is not None and marker.get("session") == owner_session_id:
            ts = marker.get("ts")
            marker_ts = _parse_ts(ts) if isinstance(ts, str) else None
            if marker_ts is not None and marker_ts >= int(file_mtime):
                # The file's OWNING session has positively closed AFTER this
                # file was last written -- released regardless of transcript
                # age, and regardless of any OTHER session still live in the
                # same scope. ``int(file_mtime)`` floors to whole seconds to
                # match the marker's own second-precision timestamp
                # (`athenaeum.store.now_iso`, issue athenaeum#1348): without this, a
                # marker and file written in the SAME wall-clock second could
                # compare unequal purely from the marker's coarser precision.
                return False, ""

    # Quiet window: no marker released the hold above (none exists, it names
    # a different session than this file's owner, it predates the file, or
    # the owner itself is unknown) -- fall back to whether ANY transcript in
    # the scope is still active, regardless of which session it belongs to.
    scope_dir = projects_root / scope
    if not scope_dir.is_dir():
        return False, ""
    try:
        transcripts = list(scope_dir.glob("*.jsonl"))
    except OSError:
        return False, ""
    for transcript in transcripts:
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            continue
        age = resolved_now - mtime
        if age < quiet_window_seconds:
            return True, (
                f"live session — transcript {transcript.name} modified "
                f"{age:.0f}s ago (quiet window {quiet_window_seconds}s)"
            )
    return False, ""
