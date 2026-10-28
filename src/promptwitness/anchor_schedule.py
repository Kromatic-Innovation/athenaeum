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

**No full-ledger read per prompt (PR athenaeum#2176 review).** Two layers:

1. A pending claim younger than the interval already answers "not due"
   (due needs BOTH the newest anchor and the last claim to be old), so
   :func:`claim_periodic_anchor_slot` returns before touching the ledger
   at all -- nearly every prompt takes this path.
2. When the ledger must be consulted, :func:`_newest_anchor_ts` resumes
   from a small pointer file (:data:`_STATE_NAME`): per partition, the
   byte offset up to the last complete line already scanned and a
   fingerprint of the bytes just before it, plus the newest anchor ``ts``
   seen so far. Only bytes appended since are read. The ledger is
   append-only, so this is exact; a pointer that is missing, unparseable,
   names a partition that is gone or shorter than its offset, or whose
   fingerprint no longer matches, is discarded and the whole ledger is
   read once, the way it always was. The pointer is written only under
   the guard lock, by temp file plus ``os.replace``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from promptwitness import ledger

__all__ = ["periodic_anchor_due", "claim_periodic_anchor_slot"]

_GUARD_NAME = "anchor_spawn.guard"
_STATE_NAME = "anchor_schedule.state.json"
_STATE_VERSION = 1
#: Bytes immediately before a partition's recorded offset that are hashed
#: to detect a partition replaced (not appended to) since it was scanned.
_FINGERPRINT_BYTES = 64


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


def _newest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _newest_anchor_in(complete: bytes) -> datetime | None:
    """Newest ``anchor`` line ``ts`` among the complete lines in
    *complete*, with the same tolerance as :func:`ledger.iter_lines`:
    unparseable or schema-invalid lines are skipped."""
    newest: datetime | None = None
    for chunk in complete.split(b"\n"):
        if not chunk.strip():
            continue
        try:
            obj = json.loads(chunk.decode("utf-8"))
            line = ledger.WitnessLine.from_dict(obj)
        except (UnicodeDecodeError, ValueError, TypeError, AttributeError, KeyError):
            continue
        if line.kind == "anchor":
            newest = _newest(newest, _parse_iso(line.ts))
    return newest


def _read_from(path: Path, offset: int) -> bytes:
    """Bytes of *path* from *offset* to EOF (the only ledger read on the
    incremental path; a seam for tests)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read()


def _fingerprint(path: Path, offset: int) -> str:
    start = max(0, offset - _FINGERPRINT_BYTES)
    with open(path, "rb") as fh:
        fh.seek(start)
        return hashlib.sha256(fh.read(offset - start)).hexdigest()


def _load_state(
    ledger_dir: Path, partitions: list[str]
) -> tuple[dict[str, int], datetime | None] | None:
    """``(offsets, newest)`` from the pointer file, or ``None`` when it is
    missing or cannot be trusted -- the caller then reads everything."""
    try:
        state = json.loads((ledger_dir / _STATE_NAME).read_text(encoding="utf-8"))
        if state.get("v") != _STATE_VERSION:
            return None
        recorded = state["partitions"]
        offsets: dict[str, int] = {}
        for name, entry in recorded.items():
            offset, fp = entry["offset"], entry["fingerprint"]
            if name not in partitions or not isinstance(offset, int) or offset < 0:
                return None
            path = ledger_dir / name
            if path.stat().st_size < offset or _fingerprint(path, offset) != fp:
                return None
            offsets[name] = offset
        raw_newest = state["newest"]
        newest = _parse_iso(raw_newest) if raw_newest is not None else None
        if raw_newest is not None and newest is None:
            return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    return offsets, newest


def _save_state(ledger_dir: Path, offsets: dict[str, int], newest: datetime | None) -> None:
    """Best effort: a pointer that fails to save only costs one full read
    on a later call."""
    try:
        payload = {
            "v": _STATE_VERSION,
            "newest": newest.isoformat() if newest is not None else None,
            "partitions": {
                name: {"offset": off, "fingerprint": _fingerprint(ledger_dir / name, off)}
                for name, off in offsets.items()
            },
        }
        tmp = ledger_dir / "{}.{}.tmp".format(_STATE_NAME, os.getpid())
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp, ledger_dir / _STATE_NAME)
    except OSError:
        pass


def _newest_anchor_ts(ledger_dir: Path, *, persist: bool = False) -> datetime | None:
    """Newest ``anchor`` line ``ts`` across the whole ledger, reading only
    what was appended since the pointer file was last written (see the
    module docstring). *persist* writes the advanced pointer back; only
    the guard-locked caller passes it."""
    partitions = ledger.list_partitions(ledger_dir)
    loaded = _load_state(ledger_dir, partitions)
    offsets, newest = loaded if loaded is not None else ({}, None)
    for name in partitions:
        start = offsets.get(name, 0)
        tail = _read_from(ledger_dir / name, start)
        # Never consume a torn (unterminated) final fragment: it is either
        # mid-append now or garbage, and is re-read next time either way.
        complete = tail[: tail.rfind(b"\n") + 1]
        newest = _newest(newest, _newest_anchor_in(complete))
        offsets[name] = start + len(complete)
    if persist and partitions:
        _save_state(ledger_dir, offsets, newest)
    return newest


def _due(
    ledger_dir: Path,
    interval_minutes: int,
    *,
    now: datetime,
    last_claim: datetime | None,
    persist: bool,
) -> bool:
    if interval_minutes <= 0:
        return False
    interval = timedelta(minutes=interval_minutes)
    # A recent claim alone settles it: due needs every activity to be old.
    if last_claim is not None and now - last_claim < interval:
        return False
    newest = _newest(_newest_anchor_ts(ledger_dir, persist=persist), last_claim)
    if newest is None:
        return True
    return now - newest >= interval


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
    return _due(
        Path(ledger_dir),
        interval_minutes,
        now=now or datetime.now(timezone.utc),
        last_claim=last_claim,
        persist=False,
    )


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
        if not _due(ledger_dir, interval_minutes, now=now, last_claim=last_claim, persist=True):
            return False
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, now.isoformat().encode("utf-8"))
        except OSError:
            pass
        return True
