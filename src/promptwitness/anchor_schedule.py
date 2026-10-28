# SPDX-License-Identifier: Apache-2.0
"""Periodic in-session anchor scheduling and spawn guard (athenaeum#2156).

Layering: L1.5 (depends only on :mod:`promptwitness.ledger`, stdlib). No
``athenaeum`` import, no third-party import, ever
(``tests/test_promptwitness_boundary.py`` covers this file too).

This module answers exactly one question for the ``UserPromptSubmit`` hook
path -- "should THIS turn be the one that spawns a detached
``promptwitness anchor`` process?" -- and never spawns anything itself:
spawning the detached child is the caller's job
(:mod:`athenaeum.claude_code_adapter`), so this module stays
harness-agnostic and reusable by a future harness adapter. It never does
network I/O and never touches the ledger's own append path; the only
file it writes is its own small guard file.

**Why a guard file, not just "is the newest anchor line old enough".**
Checking the ledger's newest ``anchor`` line's age alone is NOT enough to
bound two concurrent sessions to at most one spawn in the same interval:
the detached child that would write a NEW anchor line has not run yet by
the time a second, near-simultaneous hook invocation asks "is it due" --
both would see the same (stale) newest anchor line and would both answer
yes. :func:`claim_periodic_anchor_slot` additionally records the
wall-clock instant of the last *claim* (not just the last *anchor*) in a
guard file, read and written under a non-blocking exclusive lock, so a
second invocation sees the first's claim even though the first's child has
not yet appended anything to the ledger itself.

**Zero added latency, including the guard itself.** The lock is always
acquired non-blocking (``LOCK_NB``): a guard file already held by a truly
simultaneous call is treated as "someone else is handling it", not an
error worth waiting out, and :func:`claim_periodic_anchor_slot` returns
``False`` immediately in that case.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from promptwitness import ledger

__all__ = ["periodic_anchor_due", "claim_periodic_anchor_slot"]

_GUARD_NAME = "anchor_spawn.guard"


def _lock_nonblocking(fd: int) -> bool:
    """Try to take an exclusive, NON-blocking lock on *fd*. ``True`` on
    success, ``False`` if already held by someone else -- never blocks and
    never raises."""
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _parse_iso(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _newest_anchor_ts(ledger_dir: Path) -> datetime | None:
    newest: datetime | None = None
    for line in ledger.iter_lines(ledger_dir):
        if line.kind != "anchor":
            continue
        ts = _parse_iso(line.ts)
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    return newest


def periodic_anchor_due(
    ledger_dir: str | os.PathLike[str],
    interval_minutes: int,
    *,
    now: datetime | None = None,
    last_claim: datetime | None = None,
) -> bool:
    """``True`` when the newest known anchor activity -- whichever is more
    recent of the ledger's newest ``anchor`` line and *last_claim* (a
    pending claim this interval, not yet reflected in the ledger as an
    actual anchor line) -- is at least *interval_minutes* old.

    ``interval_minutes <= 0`` always returns ``False`` (disabled). No
    prior anchor activity at all (an empty ledger, or one with no
    ``anchor`` line yet, and no *last_claim*) always returns ``True`` --
    due immediately.
    """
    if interval_minutes <= 0:
        return False
    now = now or datetime.now(timezone.utc)
    candidates = [t for t in (_newest_anchor_ts(Path(ledger_dir)), last_claim) if t is not None]
    if not candidates:
        return True
    newest = max(candidates)
    return now - newest >= timedelta(minutes=interval_minutes)


@contextmanager
def _guard_fd(ledger_dir: Path) -> Iterator[int | None]:
    """Open and non-blocking-lock the guard file. Yields ``None`` (never
    raises) when the lock is already held by a concurrent caller."""
    guard_path = ledger_dir / _GUARD_NAME
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(guard_path), os.O_CREAT | os.O_RDWR, 0o644)
    if not _lock_nonblocking(fd):
        os.close(fd)
        yield None
        return
    try:
        yield fd
    finally:
        _unlock(fd)
        os.close(fd)


def claim_periodic_anchor_slot(
    ledger_dir: str | os.PathLike[str],
    interval_minutes: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Atomically check-and-claim the right to spawn ONE detached anchor
    process for the current interval. Returns ``True`` only when the
    caller should spawn.

    ``interval_minutes <= 0`` always returns ``False`` without touching
    the guard file at all -- the disabled case never writes anything.
    """
    if interval_minutes <= 0:
        return False
    ledger_dir = Path(ledger_dir)
    now = now or datetime.now(timezone.utc)
    with _guard_fd(ledger_dir) as fd:
        if fd is None:
            return False
        try:
            raw = os.read(fd, 4096).decode("utf-8", "replace")
        except OSError:
            raw = ""
        last_claim = _parse_iso(raw)
        if not periodic_anchor_due(ledger_dir, interval_minutes, now=now, last_claim=last_claim):
            return False
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, now.isoformat().encode("utf-8"))
        except OSError:
            pass
        return True
