# SPDX-License-Identifier: Apache-2.0
"""Single-machine run lock for mutating ``athenaeum`` commands (issue athenaeum#309).

Overlapping runs (a nightly cron plus a manual invocation, or two editor
sessions) race the librarian's wiki writes, interleave block appends to the
``wiki/_pending_*.md`` sidecars, double-spend the per-run API-call budget, and
race the move-then-retire git ops. :class:`RunLock` serializes those mutating
commands on a single machine via an advisory :func:`fcntl.flock` on
``<knowledge_root>/.athenaeum.lock``.

**Scope is single-machine only.** ``flock`` is advisory and its cross-host
behavior over network filesystems (NFS/SMB) is unreliable, so this guard makes
no attempt at multi-machine coordination — that is explicitly out of scope.

**Reading a residual lockfile (issue athenaeum#763) — read this first if you are
debugging a "stale" lock.** Mutual exclusion comes from the kernel ``flock``,
**never** from the file's contents. Two consequences that routinely get
misread:

* A ``.athenaeum.lock`` file left on disk naming a PID that is no longer alive
  is the **normal, expected steady state after every run** — :meth:`RunLock.release`
  drops the ``flock`` and closes the fd but deliberately does NOT unlink the
  file (only ``--force`` / auto-break's :meth:`_break_lock` unlinks). That
  residual file **blocks nothing**: :meth:`RunLock.acquire` re-flocks the path
  and succeeds immediately, because the kernel released the dead holder's
  ``flock`` the instant it exited. It never inspects the file's contents to
  decide whether the lock is held. So "a dead-PID lockfile is present" is NOT a
  leak, NOT a wedge, and NOT the cause of any ``LockHeld`` — do not chase it.
* The ONE case that actually blocks and is actionable is the opposite: a holder
  that is still **alive** but has hung (its heartbeat has gone stale). That is
  the athenaeum#397 auto-break / ``warn_stale_after`` path below. A dead holder
  is benign; only an alive-but-stalled one needs intervention.

Behavior:

* **Default (``wait=0``, ``force=False``)** — non-blocking acquire. If the lock
  is already held, fail fast with :class:`LockHeld` naming the holder (PID +
  age), so the caller can exit non-zero.
* **``wait=<seconds>``** — block up to *wait* seconds (polling ``LOCK_NB``),
  then raise :class:`LockHeld` if still held.
* **``force=True``** — break the lock UNCONDITIONALLY: the lockfile is unlinked
  and re-created so a fresh acquire succeeds even when another process is still
  actively holding the ``flock`` on the old inode. Use only when you are certain
  the holder is hung/dead — and never run two ``--force`` invocations
  concurrently (they would both "break" and then both proceed, defeating the
  guard). Because the kernel releases an ``flock`` the moment its holder dies, a
  *truly* stale lock never blocks a normal acquire in the first place; ``force``
  exists precisely to override a LIVE-but-hung holder. The current holder is
  logged (PID + age via :func:`read_holder`) before the break so the override is
  auditable. :func:`is_stale` is a diagnostic only and does not gate the break.
* **No ``fcntl`` (Windows / exotic platforms)** — degrade gracefully: log a
  warning and run WITHOUT locking. The lock is a single-machine POSIX
  convenience, never a hard dependency.

The lockfile carries the holder's PID, an ISO-8601 UTC acquire ``timestamp``,
the hostname, and a refreshable ``heartbeat`` timestamp (one ``key: value`` per
line) purely for diagnostics; mutual exclusion comes from the kernel
``flock``, not the file's contents. The kernel releases an ``flock`` when the
holding process dies, so a crashed run never wedges the lock permanently —
the stale *content* only affects the diagnostic message.

**ALIVE-but-wedged recovery (issue athenaeum#397).** A crashed holder is already
handled — the kernel drops its ``flock`` the moment it dies. The gap is a
holder that is still alive (so ``is_stale``/the kernel see it as healthy) but
has hung and stopped making progress; it holds the ``flock`` indefinitely and
blocks every other writer until a human notices and runs ``--force``. Two
complementary mechanisms close that gap:

* **Heartbeat.** A background daemon thread (issue athenaeum#1271), started the
  moment :meth:`RunLock.acquire` succeeds and stopped by :meth:`RunLock.release`,
  calls :meth:`RunLock.heartbeat` on a fixed timer (:data:`HEARTBEAT_INTERVAL_SECONDS`,
  default 30s) to refresh the lockfile's ``heartbeat`` line, independent of
  whatever the caller's own run loop is doing. Callers may ALSO call
  :meth:`RunLock.heartbeat` themselves at phase/file boundaries (several do,
  predating this issue) — both paths serialize through the same internal lock,
  so they never corrupt the lockfile by racing each other; the timer thread
  just guarantees a bump even when the caller's own progress stalls for
  longer than one phase. Before athenaeum#1271, the ONLY bumps came from those
  caller-driven phase/file ticks, so a holder mid-way through one long phase
  (or one long LLM call) could go tens of minutes between bumps even while
  fully healthy — observationally indistinguishable from a wedged holder over
  any window shorter than the phase. :func:`heartbeat_age_seconds` reports how
  long it has been since the last refresh (falling back to ``timestamp`` for
  older lockfiles that predate this field). A wedged holder's thread dies
  with the rest of the process, so its heartbeat goes stale exactly when the
  process itself stops being alive to refresh it.
* **Auto-break + loud warning.** A contended :meth:`RunLock.acquire` with
  ``break_stale_after`` set will, once the holder's heartbeat age exceeds that
  threshold AND the holder PID is still alive, log a loud warning and break
  the lock automatically — the same unlink-and-reacquire path ``--force``
  uses, just gated on staleness instead of unconditional. Below that
  threshold (or with auto-break disabled), ``warn_stale_after`` independently
  logs a prominent "likely wedged" warning naming the holder so an operator
  can intervene with ``--force``, without changing the raised
  :class:`LockHeld`. Both are ``None``/``<=0``-disabled by default on the
  class; the CLI wires in concrete defaults (see
  :func:`athenaeum.config.resolve_lock_break_stale_after` and
  :func:`athenaeum.config.resolve_lock_warn_stale_after`).

**Staleness contract for a waiter reading the lockfile directly (issue
athenaeum#1271).** A human or agent that hits ``LockHeld`` (fail-fast or a
``--wait`` timeout) does not have to reason about phase lengths anymore: the
holder guarantees a ``heartbeat`` bump at least every
:data:`HEARTBEAT_INTERVAL_SECONDS` (default 30s) for as long as it is alive.
So ``heartbeat_age_seconds(lockfile)`` past roughly
:data:`LIKELY_ABANDONED_AFTER_SECONDS` (default 300s, 10x the bump interval —
generous enough to absorb a missed tick from GC/scheduler jitter or a single
slow ``fsync``, tight enough to resolve in minutes rather than hours) is
reasonable grounds to suspect the holder is gone. This threshold is
**advisory only** — reported in the ``LockHeld`` message and meant for a human
or agent judgment call, exactly like the pre-existing ``is_stale`` diagnostic
above. It does **not** gate anything: it is deliberately a different (much
tighter) number than ``break_stale_after``/``warn_stale_after``, which remain
the only thresholds that ever trigger an automatic break, and whose own
defaults are UNCHANGED by this issue — this module now bumps far more
reliably, but nothing here makes it any easier to break a live lock. See
``docs/configuration.md``'s "Run lock" section for the full contract written
up for operators.
``flock``, no other cooperating athenaeum process may proceed past
:meth:`RunLock.acquire` on the same ``knowledge_root`` — that is the entire
guard against interleaved wiki/sidecar writes. It breaks only three ways: (1)
the holder dies (kernel drops the ``flock`` automatically — safe, self-healing);
(2) an operator passes ``--force`` (unconditional break — safe only if the
holder is actually dead/hung, since two concurrent ``--force`` calls both
"succeed" and defeat the guard); (3) ``break_stale_after`` auto-breaks a
heartbeat-stale-but-alive holder (safe by construction, gated on staleness).
A process that mutates the knowledge root WITHOUT going through
:class:`RunLock` is invisible to this guard entirely — the lock only protects
cooperating callers.

Layering (revised, issue athenaeum#979, S4): this module now imports
:mod:`athenaeum.store` — a normal downward edge (this module is now a
CONSUMER of the store seam's ``lease`` primitive, never the reverse; that
module's own layering test, ``tests/test_store_layering.py``, mechanically
forbids it from ever importing this one back, so there is no cycle). What
used to be this module's own raw ``fcntl``/inode-race/heartbeat-write
mechanism is MOVED to :mod:`athenaeum.store` (:class:`athenaeum.store.FileLease`
and its supporting functions), generalized from this module's hardcoded
``knowledge_root/.athenaeum.lock`` to an arbitrary lockfile path — see that
module's docstring for the full explanation and why the two modules split the
work this way (this module keeps the wait/force/staleness-auto-break POLICY
and every existing exception/log message; ``athenaeum.store`` owns the raw
open/flock/inode-check/metadata-write mechanism). Otherwise unchanged: this
module owns the lock/heartbeat *policy* — it has no knowledge of what a "run"
does; that belongs to :mod:`athenaeum.librarian` and the CLI, which call
:meth:`RunLock.acquire` / :meth:`RunLock.heartbeat` around their own logic.

**Heartbeat audit (issue athenaeum#1230).** ``break_stale_after`` is only safe if
every holder that can plausibly run long enough to approach it actually calls
:meth:`RunLock.heartbeat`. athenaeum#1230 found one gap by observation (``athenaeum
ingest``, whose heartbeat age equalled its total run time); this table
records a full sweep of every acquisition site in ``src/athenaeum/`` so the
next holder that skips it is a deliberate, reviewed decision rather than a
rediscovery. "Bounded" below means: deterministic and/or capped work with no
plausible path to the 6h default threshold, not a formal proof.

* ``_cmd_run.py`` (``run``) — **Yes.** athenaeum#526 (H10) precedent: threads
  ``lock.heartbeat`` into ``run()``'s ``ctx.tick_heartbeat()`` phase/per-file
  ticks.
* ``_cmd_index.py`` (``ingest``) — **Yes** (athenaeum#1230 fix). Threads
  ``lock.heartbeat`` into ``ingest()``, which forwards it to ``run()`` exactly
  like the ``run`` command.
* ``_cmd_index.py`` (``session-end``) — **Yes** (athenaeum#1230 fix).
  ``session_end()`` forwards to ``ingest()`` then ``run()`` — same chain,
  same fix. The inner timeout defaults to 900s (well under 6h) but
  ``KNOWLEDGE_REBUILD_TIMEOUT`` is operator-configurable, so this is wired
  rather than left latent behind a larger outer timeout.
* ``_cmd_index.py`` (``reindex``/``rebuild-index``) — **No; bounded.**
  Deterministic FTS5/local-embedding index build, no LLM/network call —
  bounded by corpus size, not wall-clock-open-ended.
* ``_cmd_drain.py`` (``drain``) — **Yes** (athenaeum#1230 fix). The SAME class
  of gap as ``ingest``, found by this sweep: explicitly unbounded
  (``max_runtime=0``), batch mode block-polls the Anthropic Batch API (which
  can itself take hours), and ONE lock is held across every window of the
  whole drain.
* ``_cmd_merges.py`` (``merges recompare --apply``) — **Yes** (athenaeum#1230
  fix). Found by this sweep: an unbounded-proposal-count, per-pair LLM
  classify loop with no existing heartbeat tick despite already receiving
  the caller's lock. Ticked once per proposal in
  :func:`athenaeum.recompare.recompare_pending_merges`.
* ``_cmd_decay.py`` (``decay-sweep --apply``) — **No; bounded.** Deterministic
  file archive sweep, no LLM/network call.
* ``_cmd_repair.py`` (``repair --apply``, all modes) — **No; bounded.**
  Deterministic frontmatter/slug/bounce-fold rewrites, no LLM call.
* ``_cmd_curate.py`` (``dedupe``/``auto-memory prune*``) — **No; bounded.**
  Deterministic clustering/report-application passes, no LLM call at apply
  time (pairs are pre-computed in a separate dry-run step).
* ``_cmd_reconcile.py`` (``reconcile --apply``) — **No; bounded.**
  Deterministic raw-tree retirement, no LLM/network call.
* ``_cmd_pii_restore.py`` (``pii-restore --apply``) — **No; bounded.**
  Deterministic restore bounded by ``--limit``, no LLM call.
* ``_cmd_pending.py`` (``ingest-answers``) — **No; bounded.** Bounded by the
  operator/agent-paced pending-answer backlog (not the raw-intake backlog);
  occasional single LLM call per answer.
* ``_cmd_pending.py`` (``ingest-merges``) — **No; bounded.** Deterministic
  sidecar move/compact, no LLM call.
* ``_cmd_pending.py`` (``reresolve-questions``) — **No; bounded.** LLM-backed,
  but capped at ``resolve_max_per_run`` (default 250) resolver calls — stays
  well under 6h at normal API latency even at the cap. Re-audit if that
  default is ever raised materially.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from athenaeum.store import (
    FileLease,
    LeaseHeldError,
    _pid_alive,
    heartbeat_age_seconds,
    is_stale,
    lease_break_lockfile,
    lease_holds_current_inode,
    lease_open_fd,
    read_holder,
)

try:  # pragma: no cover - exercised via monkeypatch in tests
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: Lockfile basename created directly under ``knowledge_root``.
LOCKFILE_NAME = ".athenaeum.lock"

#: Poll interval (seconds) while blocking for the lock under ``--wait``.
_POLL_INTERVAL = 0.25

#: Guaranteed interval (seconds) at which the background heartbeat thread
#: refreshes the lockfile's ``heartbeat`` line while a lock is held, on top
#: of (never instead of) whatever phase/file-boundary bumps the caller's own
#: run loop makes (issue athenaeum#1271). 30s is short enough to give a waiter
#: sub-minute resolution on "is this holder still making progress" while
#: being cheap — one small file write every 30s is negligible even for a
#: multi-hour run. See :data:`LIKELY_ABANDONED_AFTER_SECONDS` for the paired
#: advisory threshold and the module docstring's "Staleness contract" section
#: for the full reasoning. Overridable per-instance via ``RunLock(...,
#: heartbeat_interval=...)`` and resolved from config/env by the CLI via
#: :func:`athenaeum.config.resolve_lock_heartbeat_interval`.
HEARTBEAT_INTERVAL_SECONDS = 30.0

#: Advisory-only threshold (seconds) past which a waiter reading
#: ``heartbeat_age_seconds`` off the raw lockfile may reasonably suspect the
#: holder is abandoned rather than merely between bumps (issue athenaeum#1271).
#: Chosen as 10x :data:`HEARTBEAT_INTERVAL_SECONDS`: the RATIO is what
#: matters (per the issue) — large enough that a handful of missed ticks
#: (GC pause, scheduler contention, one slow ``fsync``) is never misread as
#: death, small enough to resolve in minutes rather than the hours
#: ``break_stale_after``/``warn_stale_after`` are deliberately tuned to.
#: **Advisory only** — reported in :class:`LockHeld`'s message, never used to
#: gate an automatic break; see the module docstring's "Staleness contract"
#: section. Deliberately NOT wired to an env/yaml knob: it is display text
#: for a human/agent judgment call, not an operational policy switch like
#: ``break_stale_after``.
LIKELY_ABANDONED_AFTER_SECONDS = 300.0


class LockHeld(RuntimeError):
    """Raised when the run lock is held and could not be acquired.

    Carries the parsed holder metadata (``pid``/``timestamp``/``host``/
    ``heartbeat``) when available so the CLI can print a clear, actionable
    message — this is the message an operator sees both on immediate
    contention (fail-fast) and after a ``--wait`` timeout expires (issue
    athenaeum#1271, acceptance criterion: report holder pid, acquisition time,
    and last heartbeat rather than a bare "another athenaeum run holds the
    lock").
    """

    def __init__(self, lockfile: Path, holder: dict[str, str] | None) -> None:
        self.lockfile = lockfile
        self.holder = holder or {}
        super().__init__(self._render())

    def _render(self) -> str:
        pid = self.holder.get("pid")
        host = self.holder.get("host")
        ts = self.holder.get("timestamp")
        hb = self.holder.get("heartbeat")
        parts = []
        if pid:
            parts.append(f"PID {pid}")
        if host:
            parts.append(f"host {host}")
        acquired_age = _age_str(ts)
        if acquired_age:
            parts.append(f"acquired {acquired_age}")
        if hb:
            if hb == ts:
                parts.append("heartbeat never bumped past acquire")
            else:
                hb_age = _age_str(hb)
                if hb_age:
                    parts.append(f"last heartbeat {hb_age}")
        liveness = _liveness_str(self.holder)
        if liveness:
            parts.append(liveness)
        who = ", ".join(parts) if parts else "another athenaeum process"
        return (
            f"another athenaeum run holds the lock ({who}); "
            f"lockfile: {self.lockfile}. Holder bumps heartbeat every "
            f"~{HEARTBEAT_INTERVAL_SECONDS:.0f}s while alive; a heartbeat "
            f"idle past ~{LIKELY_ABANDONED_AFTER_SECONDS:.0f}s is advisory "
            f"grounds to suspect it is abandoned (this does not auto-break "
            f"anything). Retry, pass --wait <seconds> to block, or --force "
            f"to break a stale lock."
        )


def _liveness_str(holder: dict[str, str]) -> str | None:
    """Belt-and-braces ``os.kill(pid, 0)`` liveness note for a ``LockHeld``
    message (issue athenaeum#1271, proposal item 4) — independent of the
    heartbeat, so it still catches a crashed holder even if a heartbeat bump
    was missed.

    Only meaningful for a holder on THIS host: the lockfile's ``host:`` field
    is the sole cross-host signal this module has, and a bare PID number is
    only ever comparable to ``os.kill`` on the machine that minted it — a PID
    on a different host may coincidentally match a live *or* dead local PID
    that is an entirely unrelated process (pid-reuse is exactly this same
    hazard, just within one host instead of across two). Returns ``None``
    when there is nothing safe to say (no pid, or a foreign host).
    """
    pid_raw = holder.get("pid")
    if not pid_raw:
        return None
    try:
        pid = int(pid_raw)
    except ValueError:
        return None
    host = holder.get("host")
    local_host = socket.gethostname()
    if host and host != local_host:
        return f"pid liveness unchecked (holder host {host!r} != local {local_host!r})"
    if _pid_alive(pid):
        return "pid alive (os.kill probe)"
    # Same host, kernel flock contention, yet the pid looks dead: either a
    # narrow just-died race or pid reuse. Flagged, not asserted as fact — see
    # this function's docstring and the module docstring's "Reading a
    # residual lockfile" note for why a dead PID alone is never conclusive.
    return (
        "pid NOT alive locally (os.kill probe — possible pid reuse or a "
        "just-exited holder; investigate before --force)"
    )


def _age_str(iso_ts: str | None) -> str:
    """Human-friendly age of an ISO-8601 timestamp, or ``''`` if unparseable."""
    if not iso_ts:
        return ""
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - then
    secs = int(delta.total_seconds())
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


#: ``read_holder``, ``is_stale``, ``heartbeat_age_seconds``, ``_pid_alive`` are
#: imported from :mod:`athenaeum.store` above (issue athenaeum#979, S4 — MOVED,
#: not copied, generalized from this module's hardcoded lockfile path to an
#: arbitrary one; see that module's docstring). Re-exported here unchanged so
#: every existing caller of ``from athenaeum.runlock import read_holder`` (etc.)
#: and every ``monkeypatch.setattr(runlock, "_pid_alive", ...)`` /
#: ``monkeypatch.setattr(runlock, "heartbeat_age_seconds", ...)`` in
#: ``tests/test_runlock.py`` keeps working: the acquire() control-flow below
#: references these by bare name, which Python resolves against THIS module's
#: globals at call time regardless of where the function object was
#: originally defined, so patching ``runlock.<name>`` still intercepts it.


class RunLock:
    """Advisory single-machine run lock over ``<knowledge_root>/.athenaeum.lock``.

    Usable as a context manager or via explicit :meth:`acquire` / :meth:`release`::

        with RunLock(knowledge_root, wait=30):
            ...  # mutate the knowledge base

    Acquisition raises :class:`LockHeld` when the lock is contended and cannot
    be obtained within the *wait* budget (and ``force`` is not set).
    """

    def __init__(
        self,
        knowledge_root: Path | str,
        *,
        wait: float = 0,
        force: bool = False,
        break_stale_after: float | None = None,
        warn_stale_after: float | None = None,
        heartbeat_interval: float | None = None,
    ) -> None:
        self.knowledge_root = Path(knowledge_root)
        self.lockfile = self.knowledge_root / LOCKFILE_NAME
        self.wait = max(0.0, float(wait))
        self.force = bool(force)
        self.break_stale_after = (
            break_stale_after if break_stale_after and break_stale_after > 0 else None
        )
        self.warn_stale_after = (
            warn_stale_after if warn_stale_after and warn_stale_after > 0 else None
        )
        # Issue athenaeum#1271: guaranteed background bump interval. Falls back to
        # the module default for any non-positive/unset value — unlike
        # break_stale_after/warn_stale_after there is no "disable" mode here;
        # a caller that truly wants no timer-driven heartbeat can still avoid
        # calling acquire() through a lock at all, but a live lock always
        # gets one (safety default, never opt-out-by-accident).
        self.heartbeat_interval = (
            heartbeat_interval
            if heartbeat_interval and heartbeat_interval > 0
            else HEARTBEAT_INTERVAL_SECONDS
        )
        self._fd: int | None = None
        self._acquired = False
        self._lease: FileLease | None = None
        self._heartbeat_write_lock = threading.Lock()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()

    # -- internals -----------------------------------------------------------
    #
    # These three delegate to athenaeum.store (issue athenaeum#979, S4 — MOVED,
    # not copied: see that module's docstring). ``acquire``/``release``/
    # ``heartbeat`` below no longer call them directly (they go through
    # athenaeum.store.FileLease instead, the same engine these delegate to) —
    # they are kept, unchanged in name/behavior, because tests exercise them
    # directly (``lock._open_fd()``, ``lock._holds_current_inode(fd)``,
    # ``lock._break_lock()``) as this module's own test-observable surface.

    def _open_fd(self) -> int:
        return lease_open_fd(self.lockfile)

    def _holds_current_inode(self, fd: int) -> bool:
        """True if *fd* refers to the inode currently at the lock path (issue athenaeum#526)."""
        return lease_holds_current_inode(fd, self.lockfile)

    def _break_lock(self) -> None:
        """Unlink the lockfile so a fresh ``flock`` inode can be acquired."""
        try:
            lease_break_lockfile(self.lockfile)
        except OSError as exc:  # pragma: no cover - unusual FS error
            log.warning("runlock: could not unlink stale lockfile: %s", exc)

    # -- public API --------------------------------------------------------

    @property
    def acquired(self) -> bool:
        """True if this instance currently holds the lock (issue athenaeum#712).

        Read-only mirror of the private ``_acquired`` flag, added so a
        caller (e.g. :mod:`athenaeum.verdicts`'s single-appender guard) can
        assert a lock is genuinely held without reaching into a private
        attribute. Purely additive — no existing behavior changes.
        """
        return self._acquired

    def acquire(self) -> RunLock:
        """Acquire the lock, honoring *wait* / *force*. Returns ``self``.

        Raises :class:`LockHeld` when contended beyond the wait budget.

        Issue athenaeum#979 (S4): the actual open/flock/inode-check/metadata-write
        mechanics below are :class:`athenaeum.store.FileLease` — a fresh
        instance per attempt, each doing its own single non-blocking (or
        forced) try. This method keeps every bit of its former CONTROL FLOW
        and every log message unchanged; only the leaf-level "try to grab the
        lock right now" step moved. See the module docstring.
        """
        if self._acquired:
            return self

        if fcntl is None:
            log.warning(
                "runlock: fcntl unavailable on this platform; running WITHOUT a "
                "run lock. Concurrent athenaeum runs are not guarded here."
            )
            self._acquired = True
            return self

        def _attempt(*, force: bool) -> FileLease | None:
            lease = FileLease(self.lockfile, force=force)
            try:
                lease.__enter__()
            except LeaseHeldError:
                return None
            return lease

        lease = _attempt(force=False)
        if lease is not None:
            self._finish_acquire(lease)
            return self

        # Contended. --force breaks the lock UNCONDITIONALLY (even a live
        # holder). Log who we're overriding — PID + age — so it's auditable.
        if self.force:
            holder = read_holder(self.lockfile)
            if holder:
                pid = holder.get("pid", "?")
                age = _age_str(holder.get("timestamp")) or "unknown age"
                # Issue athenaeum#763: name the two cases distinctly. A dead PID is
                # a BENIGN residual — the kernel already dropped its flock, so
                # this --force is just tidying a file that blocked nothing. A
                # LIVE holder is the real override: an active/hung run whose
                # flock this break supersedes.
                if is_stale(self.lockfile):
                    disposition = (
                        "residual (dead PID — kernel already released its flock; "
                        "this file blocked nothing)"
                    )
                else:
                    disposition = "LIVE (active/hung holder — override supersedes it)"
                log.warning(
                    "runlock: --force breaking %s lock held by PID %s (held %s) "
                    "on %s",
                    disposition,
                    pid,
                    age,
                    self.lockfile,
                )
            else:
                log.warning(
                    "runlock: --force breaking lock with no holder metadata on %s",
                    self.lockfile,
                )
            lease = _attempt(force=True)
            if lease is not None:
                self._finish_acquire(lease)
                return self
            # A live holder re-grabbed the fresh inode between unlink and open.
            raise LockHeld(self.lockfile, read_holder(self.lockfile))

        if self.wait > 0:
            deadline = time.monotonic() + self.wait
            while time.monotonic() < deadline:
                time.sleep(_POLL_INTERVAL)
                # Issue athenaeum#526 (M6): each poll's ``_attempt`` opens a
                # FRESH fd and verifies the inode it flocked is still the one
                # currently at the lock path (FileLease.__enter__ does this
                # internally) — never reuses a descriptor across polls, so a
                # concurrent break (--force or an auto-break from another
                # waiter) rotating the lockfile mid-wait can never leave this
                # waiter holding a stale orphan inode.
                lease = _attempt(force=False)
                if lease is not None:
                    self._finish_acquire(lease)
                    return self

        # Still contended. Determine the holder's heartbeat age once and reuse
        # it for both the auto-break and the loud-warning checks below
        # (issue athenaeum#397 — recovery for an ALIVE-but-wedged holder).
        # Named distinctly from the `age` string above (`_age_str` return) —
        # this is the numeric seconds value, not a human-friendly string.
        heartbeat_age = heartbeat_age_seconds(self.lockfile)
        holder = read_holder(self.lockfile)
        holder_pid: int | None = None
        if holder and holder.get("pid"):
            try:
                holder_pid = int(holder["pid"])
            except ValueError:
                holder_pid = None
        holder_alive = holder_pid is not None and _pid_alive(holder_pid)

        # Option 1: auto-break a wedged-but-alive holder once its heartbeat is
        # stale beyond the configured threshold. Breaks exactly like --force
        # (unlink + re-create + reflock) but gated on staleness, not
        # unconditional, and does not loop.
        if (
            self.break_stale_after is not None
            and heartbeat_age is not None
            and heartbeat_age > self.break_stale_after
            and holder_alive
        ):
            log.warning(
                "runlock: auto-breaking wedged lock held by PID %s — heartbeat "
                "stale %.0fs (> threshold %.0fs); holder alive but making no "
                "progress",
                holder_pid,
                heartbeat_age,
                self.break_stale_after,
            )
            lease = _attempt(force=True)
            if lease is not None:
                self._finish_acquire(lease)
                return self
            # A live holder re-grabbed the fresh inode between unlink and open.
            raise LockHeld(self.lockfile, read_holder(self.lockfile))

        # Option 2: even when auto-break is off or below threshold, loudly
        # warn that the holder looks wedged so an operator can --force it.
        if (
            self.warn_stale_after is not None
            and heartbeat_age is not None
            and heartbeat_age > self.warn_stale_after
            and holder_alive
        ):
            log.warning(
                "runlock: holder alive but lock age %.0fs (PID %s) — likely "
                "wedged; break with --force or lower "
                "librarian.lock_break_stale_after",
                heartbeat_age,
                holder_pid,
            )

        raise LockHeld(self.lockfile, holder)

    def _finish_acquire(self, lease: FileLease) -> None:
        self._lease = lease
        self._fd = lease.fd
        self._acquired = True
        self._start_heartbeat_thread()

    def _start_heartbeat_thread(self) -> None:
        """Start the background bump-on-a-timer thread (issue athenaeum#1271).

        Daemon so it can never keep the process alive on its own — if the
        holder crashes or exits without calling :meth:`release`, this thread
        dies with it rather than wedging shutdown. Runs entirely on top of
        (never in place of) any caller-driven :meth:`heartbeat` calls; both
        funnel through the same :attr:`_heartbeat_write_lock` in
        :meth:`heartbeat` so they can never interleave a partial write.
        """
        self._heartbeat_stop.clear()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            name="athenaeum-runlock-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()

    def _heartbeat_loop(self) -> None:
        # Event.wait(timeout) returns True the instant the event is set (stop
        # requested) and False only after the full timeout elapses with no
        # stop — so this bumps at most once per interval and exits promptly
        # (no extra bump) the moment release() calls _stop_heartbeat_thread.
        while not self._heartbeat_stop.wait(self.heartbeat_interval):
            self.heartbeat()

    def _stop_heartbeat_thread(self) -> None:
        """Stop and join the background thread before the lease is torn down.

        Called from :meth:`release` BEFORE the lease is released/fd closed —
        joining here (rather than merely signaling) guarantees no in-flight
        :meth:`heartbeat` call can ever race a write against a closed fd, so
        no extra synchronization is needed between this and ``release``'s own
        teardown.
        """
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        self._heartbeat_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():  # pragma: no cover - defensive only
                log.warning(
                    "runlock: heartbeat thread did not stop within 5s on release"
                )

    def heartbeat(self) -> None:
        """Refresh the lockfile's ``heartbeat`` line (issue athenaeum#397).

        Keeps the original ``pid``/``timestamp``/``host`` intact and rewrites
        only ``heartbeat`` to now. Called two ways, both safe to mix (issue
        athenaeum#1271): the background timer thread calls this on its own
        every :attr:`heartbeat_interval` seconds regardless of caller
        progress, and a long-running holder may ALSO call it directly at
        phase/file boundaries (several already do). Both paths serialize
        through :attr:`_heartbeat_write_lock` so two bumps can never
        interleave and corrupt the lockfile. A WEDGED holder's process (and
        therefore its thread) is gone, so nothing refreshes the heartbeat,
        which is what lets a contended acquire tell "still working" apart
        from "hung but alive". No-op (safe, no raise) when the lock was never
        acquired or the no-fcntl degrade path left no fd. Failures are
        diagnostics-only (logged, not raised) — a heartbeat write must never
        take down the run it is trying to protect.
        """
        if not self._acquired or self._lease is None:
            return
        with self._heartbeat_write_lock:
            try:
                self._lease.heartbeat()
            except OSError as exc:  # pragma: no cover - diagnostics only
                log.warning("runlock: could not refresh heartbeat: %s", exc)

    def release(self) -> None:
        """Release the lock (idempotent). Safe to call when never acquired.

        "Release" means the kernel ``flock`` is dropped and the fd is closed —
        it does **NOT** remove the lockfile. ``release`` contains no
        ``os.unlink``; the only code that unlinks is :meth:`_break_lock`
        (reached solely via ``--force`` or auto-break). So a residual
        ``.athenaeum.lock`` on disk naming a now-exited PID is the **normal,
        expected steady state after every run**, and it is harmless: mutual
        exclusion comes from the kernel ``flock`` (dropped the instant this
        process exits), never from the file's contents, so that residual file
        blocks nothing. A reader who sees the lockfile still present after a run
        must NOT conclude the release failed — see the module docstring's
        "Reading a residual lockfile" note (issue athenaeum#763).
        """
        if not self._acquired:
            return
        # Issue athenaeum#1271: stop the background bump thread FIRST, before
        # any lease teardown below — see _stop_heartbeat_thread's docstring
        # for why joining here removes the need for any other cross-thread
        # synchronization around the fd close that follows.
        self._stop_heartbeat_thread()
        self._acquired = False
        lease = self._lease
        self._lease = None
        self._fd = None
        if lease is None:  # no-fcntl degrade path held no lease
            return
        # Issue athenaeum#763: deliberately NO os.unlink here — FileLease.__exit__
        # only drops the flock and closes the fd, matching this module's
        # original no-unlink policy exactly (see below for why: unlinking on
        # release would reintroduce the athenaeum#526 orphan-inode race).
        try:
            lease.__exit__(None, None, None)
        except OSError as exc:  # pragma: no cover
            log.warning("runlock: error releasing flock: %s", exc)

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> RunLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
