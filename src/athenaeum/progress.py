# SPDX-License-Identifier: Apache-2.0
"""Per-phase progress heartbeat (issue #398).

The librarian's T3 entity-merge phase and its post-compile phases (C4
contradiction detection, the #290 wiki-dedup pass, and the #188 re-resolve
pass) previously emitted NO per-unit progress logging. When a run wedges in
one of these phases — e.g. a hung ``claude -p`` detector/resolver subprocess
— it produces zero log output for hours, so a stall is invisible in the log
and undetectable by a watchdog (a real 3.5h silent wedge occurred 2026-07-19).

:class:`PhaseHeartbeat` is a small, dependency-free helper that emits a
periodic, machine-detectable progress line while a phase iterates over its
units of work (clusters, wiki-dedupe candidates, pending questions, ...).
Every emitted line carries the stable ``librarian-heartbeat`` prefix so a
watchdog can ``grep`` the log for liveness without parsing prose.
"""

from __future__ import annotations

import logging
import time

_DEFAULT_LOGGER_NAME = "athenaeum"


class PhaseHeartbeat:
    """Emit periodic ``librarian-heartbeat`` progress lines for one phase.

    Usage::

        hb = PhaseHeartbeat("merge-write", total=len(entries), interval_s=60.0)
        hb.start()
        for entry in entries:
            ...
            hb.tick(entry.cluster_id, compiled=1)
        hb.done()

    The constructor never raises and logging is best-effort: a logging
    hiccup must never break a run.
    """

    def __init__(
        self,
        phase: str,
        *,
        total: int | None = None,
        interval_s: float = 60.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.phase = phase
        self.total = total
        self.interval_s = interval_s
        self.logger = logger if logger is not None else logging.getLogger(_DEFAULT_LOGGER_NAME)

        self.done_count = 0
        self.compiled = 0
        self.unchanged = 0
        self.error = 0

        self._start_monotonic: float | None = None
        self._last_emit_monotonic: float | None = None
        self._done_emitted = False

    def _elapsed(self) -> float:
        if self._start_monotonic is None:
            return 0.0
        return time.monotonic() - self._start_monotonic

    def _emit(self, status: str, unit_id: str | None) -> None:
        total_str = "?" if self.total is None else str(self.total)
        unit_str = unit_id if unit_id else "-"
        self.logger.info(
            "librarian-heartbeat phase=%s status=%s done=%d total=%s "
            "compiled=%d unchanged=%d error=%d unit=%s elapsed=%.1fs",
            self.phase,
            status,
            self.done_count,
            total_str,
            self.compiled,
            self.unchanged,
            self.error,
            unit_str,
            self._elapsed(),
        )

    def start(self) -> None:
        """Log the ``status=start`` line. Call once when the phase begins."""
        now = time.monotonic()
        self._start_monotonic = now
        self._last_emit_monotonic = now
        self._emit("start", None)

    def tick(
        self,
        unit_id: str | None = None,
        *,
        compiled: int = 0,
        unchanged: int = 0,
        error: int = 0,
    ) -> None:
        """Record progress on one unit of work.

        Increments the internal ``done`` counter by 1 and accumulates the
        per-call ``compiled``/``unchanged``/``error`` deltas into running
        totals. Emits a ``status=tick`` line only if at least ``interval_s``
        seconds have elapsed since the last emitted line, OR this is the
        first tick — so a fast phase emits roughly start+done while a
        slow/wedged phase still emits a tick at least every ``interval_s``.
        """
        if self._start_monotonic is None:
            # Defensive: tolerate a tick before start() so a caller mistake
            # never raises mid-run.
            self.start()

        self.done_count += 1
        self.compiled += compiled
        self.unchanged += unchanged
        self.error += error

        now = time.monotonic()
        first_tick = self.done_count == 1
        elapsed_since_emit = (
            now - self._last_emit_monotonic if self._last_emit_monotonic is not None else None
        )
        should_emit = first_tick or (
            elapsed_since_emit is not None and elapsed_since_emit >= self.interval_s
        )
        if should_emit:
            self._last_emit_monotonic = now
            self._emit("tick", unit_id)

    def done(self) -> None:
        """Log the final ``status=done`` summary line. Idempotent-safe."""
        if self._done_emitted:
            return
        if self._start_monotonic is None:
            self.start()
        self._done_emitted = True
        self._emit("done", None)
