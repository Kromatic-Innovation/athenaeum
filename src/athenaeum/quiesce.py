# SPDX-License-Identifier: Apache-2.0
"""Operator quiesce sentinel for scheduled reasoning-trigger runs (issue athenaeum#1898).

Scheduled ``athenaeum ingest --if-triggered`` runs (a LaunchAgent-style
``StartInterval`` poke, see :mod:`athenaeum.reasoning_triggers`) take the
corpus run lock (:mod:`athenaeum.runlock`) for anywhere from 2 to 29 minutes.
Before this module existed, an operator-driven corpus write lane had no
cooperative way to hold the scheduler off — the only recipe was breaking the
LaunchAgent's schedule out-of-band (``launchctl bootout``), which is fragile
(nothing records that it happened, and re-bootstrapping it is a second manual
step an interrupted operator can forget) and leaves no trace an evaluator can
read. This module is the first-class replacement: a small JSON sentinel,
``<knowledge_root>/.athenaeum-quiesce`` — a sibling of the run lock's own
``<knowledge_root>/.athenaeum.lock`` (:data:`athenaeum.runlock.LOCKFILE_NAME`)
— that a lane writes before it starts, and that ``ingest --if-triggered``
checks before it ever reaches the lock.

**This module owns ALL of the sentinel's I/O** — reading it, writing it,
releasing it, and treating an elapsed ``expires_at`` as absent. That split is
deliberate, not incidental: :mod:`athenaeum.reasoning_triggers` states,
repeatedly and emphatically in its own module docstring, that it is "pure and
side-effect-free" and "contains no I/O at all" — every fact it evaluates is
gathered by its caller and handed in. A quiesce sentinel is exactly such a
fact: :func:`athenaeum._cmd_index._evaluate_ingest_trigger` calls
:func:`read_quiesce_state` here, then passes the result into
:func:`athenaeum.reasoning_triggers.evaluate_triggers`'s ``quiesce=``
keyword — see that function's docstring for where the check sits in its
evaluation order (immediately after ``on_demand``, before every
threshold-driven trigger).

**Sentinel shape** — one JSON object, written atomically
(:func:`athenaeum.atomic_io.atomic_write_text`, matching every other
cache/knowledge-root sidecar in this codebase)::

    {
      "holder": "tristan@laptop",
      "reason": "backfilling person pages, hold the scheduler off",
      "created_at": "2026-09-25T14:00:00Z",
      "expires_at": "2026-09-25T16:00:00Z"
    }

**Expiry is read-time, not write-time.** :func:`read_quiesce_state` compares
``expires_at`` against "now" on every call and treats a past ``expires_at``
identically to a missing file: ``None``, "nothing is quiesced". Deliberately
a PURE read, mirroring this codebase's other tolerant stamp readers
(:func:`athenaeum.librarian._load_timestamp_stamp`,
:func:`athenaeum.librarian._load_full_compile_stamp`) — none of which mutate
anything on a read, even a malformed or stale one. An expired sentinel is
therefore left on disk (inert — the next read still ignores it) until either
:func:`release_quiesce` removes it explicitly or a fresh :func:`write_quiesce`
overwrites it; nothing here silently unlinks a file out from under a caller
that only asked to READ, which matters because :func:`read_quiesce_state` is
also reached by ``athenaeum ingest --evaluate-only``
(:func:`athenaeum._cmd_index._cmd_ingest_evaluate_only`), a mode documented
as never mutating anything.

Layering: L2 primitive, the same tier as :mod:`athenaeum.intake` (another L2
module whose whole job is filesystem I/O over the knowledge root). Imports
:mod:`athenaeum.atomic_io` (L0), :mod:`athenaeum.store` (L1, for
:func:`athenaeum.store.now_iso` — the single shared UTC-ISO timestamp
renderer) and :mod:`athenaeum.config` (L2, a peer, for
:func:`athenaeum.config.resolve_quiesce_max_hours`) — every import is at or
below this module's own declared layer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import resolve_quiesce_max_hours
from athenaeum.store import now_iso

logger = logging.getLogger(__name__)

#: Sentinel basename, created directly under ``knowledge_root`` — a sibling
#: of :data:`athenaeum.runlock.LOCKFILE_NAME` (``.athenaeum.lock``). Not
#: imported from :mod:`athenaeum.runlock` itself: the two files are related
#: only by convention (both live at the knowledge root, both name a
#: single-machine control signal), not by any shared code, and
#: :mod:`athenaeum.runlock` is UNDECLARED in the layer table
#: (``tests/fixtures/layer_declarations.py``) — this module has no need to
#: create a dependency on it just to borrow one string constant.
QUIESCE_FILENAME = ".athenaeum-quiesce"

#: ISO-8601 UTC, second precision — the exact shape :func:`athenaeum.store.now_iso`
#: renders and the one this module's own reader parses back.
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class QuiesceDurationExceeded(ValueError):
    """Raised by :func:`write_quiesce` when the requested duration exceeds
    the configured maximum (``librarian.quiesce.max_hours``, issue
    athenaeum#1898) — the cap that keeps a crashed or forgotten lane from
    pausing the scheduler indefinitely."""


@dataclass(frozen=True)
class QuiesceState:
    """One active operator quiesce sentinel, as read from disk (issue athenaeum#1898).

    ``created_at``/``expires_at`` are timezone-aware UTC :class:`datetime`
    instances (parsed from the sentinel's ISO-8601 strings) — never naive,
    so every comparison against "now" is unambiguous.
    """

    holder: str
    reason: str
    created_at: datetime
    expires_at: datetime


def quiesce_path(knowledge_root: Path) -> Path:
    """Resolve the sentinel path for ``knowledge_root`` (issue athenaeum#1898)."""
    return knowledge_root / QUIESCE_FILENAME


def read_quiesce_state(
    knowledge_root: Path, *, now: datetime | None = None
) -> QuiesceState | None:
    """Read the quiesce sentinel, or ``None`` when nothing is quiesced (issue athenaeum#1898).

    Tolerant, side-effect-free read: a missing file, an unreadable/malformed
    JSON body, a missing/non-string field, an unparsable timestamp, or an
    ``expires_at`` at or before ``now`` (defaults to the real current UTC
    instant; a caller may pin it for a deterministic test) all collapse to
    the same ``None`` — "nothing is quiesced right now". See this module's
    own docstring ("Expiry is read-time, not write-time") for why an expired
    sentinel is left on disk rather than unlinked here.
    """
    path = quiesce_path(knowledge_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    holder = data.get("holder")
    reason = data.get("reason")
    created_raw = data.get("created_at")
    expires_raw = data.get("expires_at")
    if not (
        isinstance(holder, str)
        and holder
        and isinstance(reason, str)
        and reason
        and isinstance(created_raw, str)
        and isinstance(expires_raw, str)
    ):
        return None

    try:
        created_at = _parse_timestamp(created_raw)
        expires_at = _parse_timestamp(expires_raw)
    except ValueError:
        return None

    effective_now = now if now is not None else datetime.now(timezone.utc)
    if expires_at <= effective_now:
        return None
    return QuiesceState(
        holder=holder, reason=reason, created_at=created_at, expires_at=expires_at
    )


def write_quiesce(
    knowledge_root: Path,
    *,
    holder: str,
    reason: str,
    for_duration: timedelta,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> QuiesceState:
    """Write a new quiesce sentinel, capped at the configured maximum (issue athenaeum#1898).

    Always OVERWRITES any existing sentinel (atomically —
    :func:`athenaeum.atomic_io.atomic_write_text`), whether or not it was
    still active — the same "last write wins, no merge" contract every other
    stamp writer in this codebase uses. ``for_duration`` must be a positive
    :class:`~datetime.timedelta` at or under
    :func:`athenaeum.config.resolve_quiesce_max_hours`'s resolved ceiling
    (default 6h); either violation raises rather than silently clamping —
    :exc:`ValueError` for a non-positive duration, :exc:`QuiesceDurationExceeded`
    (a :exc:`ValueError` subclass) for one over the cap, so a caller that
    only wants "was this rejected" can catch the parent class while a caller
    that wants to distinguish the two can catch the child.
    """
    if for_duration <= timedelta(0):
        raise ValueError(f"--for must be a positive duration, got {for_duration}")

    max_hours = resolve_quiesce_max_hours(config)
    max_duration = timedelta(hours=max_hours)
    if for_duration > max_duration:
        raise QuiesceDurationExceeded(
            f"requested duration {for_duration} exceeds the configured maximum "
            f"of {max_hours}h (librarian.quiesce.max_hours) — a crashed or "
            "forgotten lane must not be able to pause the scheduler "
            "indefinitely (issue athenaeum#1898). Release the sentinel and "
            "re-request in shorter increments, or raise the configured "
            "maximum if this ceiling is genuinely too low for your workflow."
        )

    effective_now = now if now is not None else datetime.now(timezone.utc)
    expires_at = effective_now + for_duration
    state = QuiesceState(
        holder=holder, reason=reason, created_at=effective_now, expires_at=expires_at
    )
    payload: dict[str, Any] = {
        "holder": state.holder,
        "reason": state.reason,
        "created_at": now_iso(state.created_at),
        "expires_at": now_iso(state.expires_at),
    }
    path = quiesce_path(knowledge_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return state


def release_quiesce(knowledge_root: Path) -> bool:
    """Remove the quiesce sentinel, if present (issue athenaeum#1898).

    Idempotent: releasing an already-absent (never written, already
    released, or manually deleted) sentinel is not an error — returns
    ``False`` rather than raising, so ``athenaeum quiesce --release`` can be
    run defensively without first checking ``--status``. Returns ``True``
    only when a file actually existed and was removed. Deliberately
    unconditional on expiry — this removes the file whether or not it was
    still active, unlike :func:`read_quiesce_state`'s "expired == absent"
    treatment, because an explicit ``--release`` is an operator asking to
    clean up the file itself, not merely to stop it from being honored.
    """
    path = quiesce_path(knowledge_root)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("failed to remove quiesce sentinel %s: %s", path, exc)
        return False
    return True


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp in :data:`_TIMESTAMP_FORMAT` shape.

    Mirrors :func:`athenaeum.librarian._load_timestamp_stamp`'s parse exactly
    (same format string, same ``ValueError`` propagation for the caller to
    catch) — the sentinel's timestamps are written by
    :func:`athenaeum.store.now_iso`, the identical renderer that stamp uses.
    """
    return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
