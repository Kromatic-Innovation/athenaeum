# SPDX-License-Identifier: Apache-2.0
"""The "full index rebuild required" marker (issue athenaeum#3320).

A full index rebuild re-embeds the whole corpus. On a large wiki that takes
far longer than the SessionEnd deadline allows, so :func:`athenaeum.librarian.session_end`
never attempts one. When it finds the index cannot be reused (a metadata-schema
roll, an embedding-model swap, a missing index file), it records this marker
and returns. Before this marker existed, every session close started the
doomed rebuild again, was killed at the deadline, and left the index on the
old format for the next close to try again.

The full rebuild has one owner: :func:`athenaeum.librarian.reindex` when run
with no session deadline, which is what the nightly ``athenaeum run``
(``refresh_index_after_run``) and an explicit ``athenaeum reindex`` both
call. ``reindex`` records each attempt here, records a failure when one
raises, and deletes the marker once a rebuild completes. Later session ends
then go back to incremental work.

An attempt that was killed outright (a ``timeout``, a reboot) never reaches
the code that would record its failure. So :func:`record_attempt_started`
counts any earlier attempt that is still marked in progress as a failure
first. ``consecutive_failures`` therefore counts kills as well as errors.
``athenaeum doctor`` warns once it reaches
:data:`FAILURE_WARN_THRESHOLD`.

The marker lives at ``<cache_dir>/full-rebuild-required.json``, next to the
index it describes. Writes are atomic, and reads are tolerant: a missing or
corrupt file reads as "no marker", because the marker is advisory. The index
manifest stays the source of truth for whether the index is usable.

Layering: L2 leaf. Imports :mod:`athenaeum.atomic_io` and
:mod:`athenaeum.clock` only. Callers resolve the cache dir and pass it in.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.clock import now_iso

log = logging.getLogger(__name__)

#: File name of the marker under the index cache dir.
MARKER_NAME = "full-rebuild-required.json"

#: Consecutive failed attempts (errors or kills) at which ``athenaeum doctor``
#: stops reporting the pending rebuild as routine and warns.
FAILURE_WARN_THRESHOLD = 3

_FORMAT_VERSION = 1


@dataclass(frozen=True)
class FullRebuildMarker:
    """What the marker file records."""

    reason: str
    backend: str
    first_recorded_at: str
    last_deferred_at: str | None = None
    #: How many session ends skipped the rebuild because of this marker.
    deferrals: int = 0
    #: Rebuild attempts started since the marker was recorded.
    attempts: int = 0
    #: Attempts in a row that did not complete (raised, or were killed).
    consecutive_failures: int = 0
    #: Set while an attempt is running; still set later means it was killed.
    attempt_started_at: str | None = None
    last_failure_at: str | None = None
    last_failure: str | None = None

    @property
    def failing(self) -> bool:
        """True once failures have reached :data:`FAILURE_WARN_THRESHOLD`."""
        return self.consecutive_failures >= FAILURE_WARN_THRESHOLD


def marker_path(cache_dir: Path) -> Path:
    """Where the marker lives for the index under ``cache_dir``."""
    return cache_dir / MARKER_NAME


def read_marker(cache_dir: Path) -> FullRebuildMarker | None:
    """The marker under ``cache_dir``, or ``None`` if absent or unreadable."""
    path = marker_path(cache_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable full-rebuild marker at %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return FullRebuildMarker(
            reason=str(payload["reason"]),
            backend=str(payload.get("backend", "")),
            first_recorded_at=str(payload["first_recorded_at"]),
            last_deferred_at=_opt_str(payload.get("last_deferred_at")),
            deferrals=int(payload.get("deferrals", 0)),
            attempts=int(payload.get("attempts", 0)),
            consecutive_failures=int(payload.get("consecutive_failures", 0)),
            attempt_started_at=_opt_str(payload.get("attempt_started_at")),
            last_failure_at=_opt_str(payload.get("last_failure_at")),
            last_failure=_opt_str(payload.get("last_failure")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("ignoring malformed full-rebuild marker at %s: %s", path, exc)
        return None


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _write(cache_dir: Path, marker: FullRebuildMarker) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {"format_version": _FORMAT_VERSION, **asdict(marker)}
    atomic_write_text(marker_path(cache_dir), json.dumps(payload, indent=2) + "\n")


def record_deferred(cache_dir: Path, *, reason: str, backend: str) -> FullRebuildMarker:
    """Record that a session end skipped a required full rebuild.

    Creates the marker on the first deferral and bumps ``deferrals`` after
    that. The reason and backend are refreshed each time, since the blocker
    the session end saw is the current one.
    """
    now = now_iso()
    existing = read_marker(cache_dir)
    if existing is None:
        marker = FullRebuildMarker(
            reason=reason,
            backend=backend,
            first_recorded_at=now,
            last_deferred_at=now,
            deferrals=1,
        )
    else:
        marker = _replace(
            existing,
            reason=reason,
            backend=backend,
            last_deferred_at=now,
            deferrals=existing.deferrals + 1,
        )
    _write(cache_dir, marker)
    return marker


def record_attempt_started(cache_dir: Path, *, reason: str, backend: str) -> FullRebuildMarker:
    """Record that a full rebuild is starting.

    Creates the marker if none exists, so a rebuild the nightly run starts
    before any session end saw the problem is tracked too. If an earlier
    attempt is still marked in progress, that attempt never finished and
    never recorded an outcome, so it was killed: it counts as a failure.
    """
    now = now_iso()
    existing = read_marker(cache_dir)
    if existing is None:
        marker = FullRebuildMarker(
            reason=reason,
            backend=backend,
            first_recorded_at=now,
            attempts=1,
            attempt_started_at=now,
        )
    else:
        failures = existing.consecutive_failures
        last_failure_at = existing.last_failure_at
        last_failure = existing.last_failure
        if existing.attempt_started_at is not None:
            failures += 1
            last_failure_at = existing.attempt_started_at
            last_failure = (
                "the attempt started at "
                f"{existing.attempt_started_at} never finished (killed or timed out)"
            )
        marker = _replace(
            existing,
            reason=reason or existing.reason,
            backend=backend or existing.backend,
            attempts=existing.attempts + 1,
            consecutive_failures=failures,
            attempt_started_at=now,
            last_failure_at=last_failure_at,
            last_failure=last_failure,
        )
    _write(cache_dir, marker)
    return marker


def record_attempt_failed(cache_dir: Path, error: str) -> FullRebuildMarker | None:
    """Record that the running attempt raised ``error``."""
    existing = read_marker(cache_dir)
    if existing is None:
        return None
    marker = _replace(
        existing,
        consecutive_failures=existing.consecutive_failures + 1,
        attempt_started_at=None,
        last_failure_at=now_iso(),
        last_failure=error,
    )
    _write(cache_dir, marker)
    return marker


def clear(cache_dir: Path) -> bool:
    """Delete the marker. Returns True if there was one to delete."""
    try:
        marker_path(cache_dir).unlink()
    except FileNotFoundError:
        return False
    return True


def _replace(marker: FullRebuildMarker, **changes: Any) -> FullRebuildMarker:
    return FullRebuildMarker(**{**asdict(marker), **changes})


def describe(marker: FullRebuildMarker) -> str:
    """One-line human summary, shared by ``doctor`` and the session-end log."""
    parts = [f"full index rebuild pending since {marker.first_recorded_at} ({marker.reason})"]
    if marker.deferrals:
        parts.append(f"skipped by {marker.deferrals} session end(s)")
    if marker.consecutive_failures:
        parts.append(f"{marker.consecutive_failures} failed attempt(s) in a row")
        if marker.last_failure:
            parts.append(f"last: {marker.last_failure}")
    return "; ".join(parts)
