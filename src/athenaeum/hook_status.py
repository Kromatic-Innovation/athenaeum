# SPDX-License-Identifier: Apache-2.0
"""Shipped-hook degradation status (issue athenaeum#2204).

A shipped Claude Code hook (``session-start-recall.sh``, the packaged
``athenaeum-claude-hook`` adapter) can run in a degraded mode for reasons a
user has no way to discover without running something by hand: no cached
``ANTHROPIC_API_KEY`` (LLM topic extraction falls back to a regex/stopword
extractor), the configured search backend's index has never been built
(recall is served from whatever IS ready, which may be nothing on a brand
new session), or a foreground step ran out of its remaining
:func:`athenaeum.config.resolve_hook_budget_seconds` budget and was skipped.
Before this issue the ONLY signal for any of this was a line printed to
stderr, and the shipped ``settings-snippet.json`` ends every hook command
with ``2>/dev/null || true`` -- the one channel that could have shown it was
also the one channel every shipped install throws away.

Three pieces:

- :func:`record_degradation` -- append one event. Called by
  ``session-start-recall.sh`` (via a ``python -c`` one-liner; it has no other
  way to reach this module) and by :mod:`athenaeum.claude_code_adapter`
  directly. Best-effort: never raises, matching every fail-open hook
  contract in this issue.
- :func:`read_latest_degradation` -- read it back. This is what
  :func:`athenaeum.status.status` surfaces under its ``hook_degradation``
  key (AC8), and what :func:`maybe_session_notice` below reads to decide
  whether there is anything to say at all.
- :func:`maybe_session_notice` -- the AC9 "at most once per session" gate.
  Both hooks that can detect a degradation call this with the hook's own
  ``session_id`` (every Claude Code hook payload carries one); whichever
  calls it FIRST for a given session wins the "show it" slot, and every
  later call for that same session (from either hook) sees ``None``. The
  caller is responsible for printing the returned text on its OWN
  ``additionalContext``/``hookSpecificOutput`` channel -- never stderr,
  which is exactly the channel the shipped snippet already discards (AC10).
  This module has no opinion on transport; it only decides WHETHER and
  WHAT, never HOW to deliver it.

Storage: append-only JSONL under ``<cache_dir>/hook_degradation.jsonl`` (AC7:
"a status file under the cache dir"), resolved by the CALLER via
:func:`athenaeum.config.resolve_cache_dir` -- this module never resolves a
cache dir itself, matching :mod:`athenaeum.push_metrics`'s own convention of
taking a *cache_dir* parameter rather than re-resolving it. Oldest-first, one
line per event, so "the latest degradation" (what AC8 asks for) is simply
the LAST line -- a scan, not a parse of a single mutated record, so an
operator debugging "recall felt weak last Tuesday" is not limited to
whatever the most recent run happened to overwrite. Every write goes through
:func:`athenaeum.atomic_io.atomic_write_text` (read-whole, append-line,
replace-whole): safe against a crash mid-write; a race between two
near-simultaneous writers can drop one event, an accepted trade-off for a
low-frequency diagnostic file (see that module's docstring for the atomicity
guarantee this builds on, and ``runlock.py``'s module docstring for why nothing
here reaches for a real lock -- a lost diagnostic line is cheap, not worth
serializing two hook processes over).

Layering: L3 service. Imports :mod:`athenaeum.atomic_io` (L0) at module
scope; nothing else.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from athenaeum.atomic_io import atomic_write_text

#: The three degradation kinds AC7 names, verbatim. A plain ``Literal``
#: rather than an ``Enum``: the shell hook reaches this module only via a
#: ``python -c`` one-liner passing a bare string on argv, so a stringly-typed
#: cross-language interface is what actually exists; the ``Literal`` keeps
#: mypy honest for the in-process caller (:mod:`athenaeum.claude_code_adapter`)
#: without pretending the shell side carries anything richer.
DegradationEvent = Literal["no_api_key", "index_not_ready", "step_timeout"]

_DEGRADATION_FILENAME = "hook_degradation.jsonl"
_NOTICE_SHOWN_DIRNAME = "hook_notice_shown"

#: How recent a degradation must be for :func:`maybe_session_notice` to
#: surface it to a NEW session. The JSONL log itself is history and is never
#: cleared (``athenaeum status`` still reports the latest event with its
#: timestamp); this window only stops a long-resolved event -- an
#: ``index_not_ready`` from before the index was built, a one-off
#: ``step_timeout`` -- from being injected into every later session. It
#: does not hide a degradation that is still live: every event kind is
#: re-recorded on each hook run while its condition persists
#: (``session-start-recall.sh`` re-checks the key and the index at every
#: session start), so a live condition always carries a fresh timestamp.
#: The window only has to span the gap between a session's start and its
#: first prompt, which is where the notice is claimed.
NOTICE_MAX_AGE = timedelta(hours=1)

_NOTICE_TEXT: dict[str, str] = {
    "no_api_key": (
        "[athenaeum] Recall is running without a cached ANTHROPIC_API_KEY -- "
        "topic extraction fell back to regex-only. Run `athenaeum status` "
        "for details."
    ),
    "index_not_ready": (
        "[athenaeum] The recall index hasn't finished building yet -- this "
        "session may see fewer results than usual. Run `athenaeum status` "
        "for details."
    ),
    "step_timeout": (
        "[athenaeum] A recall hook step ran out of its foreground budget and "
        "was skipped. Run `athenaeum status` for details."
    ),
}

_DEFAULT_NOTICE_TEXT = (
    "[athenaeum] A recall hook degraded this session. Run `athenaeum status` "
    "for details."
)


def _degradation_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / _DEGRADATION_FILENAME


def record_degradation(
    cache_dir: Path, hook: str, event: DegradationEvent, detail: str = ""
) -> None:
    """Append one degradation event for *hook* (issue athenaeum#2204 AC7).

    *hook* is a short, free-text label naming the caller (e.g.
    ``"session-start"``, ``"user-prompt"``) -- not validated against a fixed
    set, since new hooks may call this without a code change here. *detail*
    is a short human-readable sentence; never raises, never logs -- a
    degradation-tracking write must never itself become a second failure for
    a hook whose whole contract is "never block a turn/session."
    """
    path = _degradation_path(cache_dir)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hook": hook,
        "event": event,
        "detail": detail,
    }
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        existing = ""
    try:
        atomic_write_text(path, existing + json.dumps(record) + "\n")
    except OSError:
        pass


def read_latest_degradation(cache_dir: Path) -> dict[str, object] | None:
    """The most recently recorded degradation event, or ``None`` when the
    status file is missing, empty, or every line fails to parse.

    Best-effort, matching every other advisory read
    :func:`athenaeum.status.status` already performs -- a corrupt or
    half-written status file must never break ``athenaeum status``.
    """
    path = _degradation_path(cache_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def notice_text(latest: dict[str, object]) -> str:
    """Render the AC9 one-line notice for *latest* (the dict
    :func:`read_latest_degradation` returns). Falls back to a generic line
    for an ``event`` value this module doesn't recognise -- e.g. a future
    caller that records a new event kind before this table is extended --
    rather than raising or saying nothing.
    """
    event = latest.get("event")
    return _NOTICE_TEXT.get(str(event), _DEFAULT_NOTICE_TEXT)


def _is_stale(latest: dict[str, object], now: datetime) -> bool:
    """True when *latest*'s ``ts`` is older than :data:`NOTICE_MAX_AGE`.

    A missing or unparseable ``ts`` counts as fresh (fail-open, matching
    :func:`maybe_session_notice`'s own "a rare duplicate beats a notice that
    never fires" stance). :func:`record_degradation` always writes an
    ISO-8601 UTC ``ts``, so this only matters for a hand-edited or corrupt
    line.
    """
    raw = latest.get("ts")
    if not isinstance(raw, str):
        return False
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - ts > NOTICE_MAX_AGE


def _sanitize_session_id(session_id: str) -> str:
    """A session id reaches this module from untrusted hook stdin JSON; never
    use it as a bare path component. Claude Code session ids are UUIDs in
    practice, so this is defensive, not load-bearing."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in session_id)
    return safe or "unknown"


def maybe_session_notice(cache_dir: Path, session_id: str) -> str | None:
    """At most once per *session_id* (issue athenaeum#2204 AC9): if a
    degradation is on record, return its one-line notice text the FIRST
    time this is called for that session, and ``None`` on every later call
    for the same session -- or when nothing is degraded at all.

    Markers are one empty file per session id under
    ``<cache_dir>/hook_notice_shown/``, not a single "last shown" scalar:
    two sessions running concurrently, or several turns within the same
    session, each need the notice exactly once -- a scalar would make a
    second concurrent session either never see it (if it lost the race to
    write the scalar first) or make an EARLIER session see it again (if a
    later session's write overwrote the scalar). A marker-per-session avoids
    both failure modes. Unbounded growth over long-term use is a known,
    accepted cost (cheap, ephemeral cache-dir files, not VCS) -- cleanup is
    out of this issue's scope.

    Only a degradation recorded within :data:`NOTICE_MAX_AGE` counts; an
    older one is treated as "nothing degraded" and no marker is claimed, so
    a later, fresh degradation in the same session can still surface.
    """
    latest = read_latest_degradation(cache_dir)
    if latest is None or _is_stale(latest, datetime.now(timezone.utc)):
        return None
    marker_dir = Path(cache_dir) / _NOTICE_SHOWN_DIRNAME
    marker = marker_dir / _sanitize_session_id(session_id)
    if marker.is_file():
        return None
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=False)
    except OSError:
        # Couldn't claim the marker (lost a race against a concurrent
        # caller for the SAME session id, or a read-only cache dir) --
        # show the notice anyway. A rare duplicate is a smaller defect
        # than a notice that silently never fires.
        pass
    return notice_text(latest)
