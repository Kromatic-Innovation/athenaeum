# SPDX-License-Identifier: Apache-2.0
"""Live-session guard for the move-then-retire pass (issue athenaeum#1728).

Contract: a raw auto-memory file is eligible for the move-then-retire pass
(:mod:`athenaeum.retire`) only when the Claude Code session that OWNS its
scope is closed. Retiring while that session is still live races an agent
that might still amend, correct, or contradict the very fact this pass is
about to move into the wiki and ``git rm`` from ``raw/``.

Two signals, checked in order, first hit wins:

1. **Session-end marker.** :func:`record_session_end` is called from
   :func:`athenaeum.librarian.session_end` after a session's SessionEnd hook
   completes, and stamps a small JSON marker under
   ``<cache_dir>/live-session-markers/<scope>.json`` naming the scope and the
   timestamp. A marker newer than the candidate file's mtime releases the
   hold REGARDLESS of transcript age -- the session that owned this memory
   has positively closed, so there is nothing left to race.
2. **Quiet window.** Failing that (no marker, or the marker is older than the
   file), the guard falls back to transcript liveness: any
   ``<projects_root>/<scope>/*.jsonl`` transcript modified within the
   configurable quiet window (default
   :data:`athenaeum.config.DEFAULT_LIVE_SESSION_GUARD_QUIET_WINDOW_SECONDS`,
   30 minutes) of "now" holds the file. No transcript activity within the
   window -- including no transcripts at all -- releases the hold.

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

Layering: L2 leaf. Imports only :mod:`athenaeum.atomic_io` and
:mod:`athenaeum.store` (both L0) -- no config, no LLM client. Callers
(:mod:`athenaeum.retire`) resolve config-driven defaults themselves and pass
plain values in.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from athenaeum.atomic_io import atomic_write_text
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
            json.dumps({"session": session_id, "ts": now_iso()}, sort_keys=True)
            + "\n",
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


def _marker_ts(cache_dir: Path, scope: str) -> float | None:
    """Read *scope*'s marker timestamp as a POSIX float, or ``None``.

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
    ts = data.get("ts")
    if not isinstance(ts, str):
        return None
    return _parse_ts(ts)


def is_live(
    scope: str,
    file_mtime: float,
    *,
    cache_dir: Path,
    projects_root: Path,
    quiet_window_seconds: int,
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
        now: Injectable current time (POSIX seconds); defaults to
            :func:`time.time`.

    Returns:
        ``(held, reason)``. ``held=True`` means the caller must NOT retire
        this file this pass; ``reason`` is a human-readable explanation for
        the run summary / dry-run report, empty when ``held=False``.
    """
    resolved_now = now if now is not None else time.time()

    marker_ts = _marker_ts(cache_dir, scope)
    if marker_ts is not None and marker_ts >= int(file_mtime):
        # The session that owned this scope has positively closed AFTER this
        # file was last written -- released regardless of transcript age.
        # ``int(file_mtime)`` floors to whole seconds to match the marker's
        # own second-precision timestamp (`athenaeum.store.now_iso`, issue
        # athenaeum#1348): without this, a marker and file written in the SAME
        # wall-clock second could compare unequal purely from the marker's
        # coarser precision, wrongly failing to release a hold that should
        # release.
        return False, ""

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
