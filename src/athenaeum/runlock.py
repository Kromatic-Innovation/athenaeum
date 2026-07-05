# SPDX-License-Identifier: Apache-2.0
"""Single-machine run lock for mutating ``athenaeum`` commands (issue #309).

Overlapping runs (a nightly cron plus a manual invocation, or two editor
sessions) race the librarian's wiki writes, interleave block appends to the
``wiki/_pending_*.md`` sidecars, double-spend the per-run API-call budget, and
race the move-then-retire git ops. :class:`RunLock` serializes those mutating
commands on a single machine via an advisory :func:`fcntl.flock` on
``<knowledge_root>/.athenaeum.lock``.

**Scope is single-machine only.** ``flock`` is advisory and its cross-host
behavior over network filesystems (NFS/SMB) is unreliable, so this guard makes
no attempt at multi-machine coordination — that is explicitly out of scope.

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

The lockfile carries the holder's PID, an ISO-8601 UTC timestamp, and the
hostname (one ``key: value`` per line) purely for diagnostics; mutual exclusion
comes from the kernel ``flock``, not the file's contents. The kernel releases an
``flock`` when the holding process dies, so a crashed run never wedges the lock
permanently — the stale *content* only affects the diagnostic message.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

try:  # pragma: no cover - exercised via monkeypatch in tests
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: Lockfile basename created directly under ``knowledge_root``.
LOCKFILE_NAME = ".athenaeum.lock"

#: Poll interval (seconds) while blocking for the lock under ``--wait``.
_POLL_INTERVAL = 0.25


class LockHeld(RuntimeError):
    """Raised when the run lock is held and could not be acquired.

    Carries the parsed holder metadata (``pid``/``timestamp``/``host``) when
    available so the CLI can print a clear, actionable message.
    """

    def __init__(self, lockfile: Path, holder: dict[str, str] | None) -> None:
        self.lockfile = lockfile
        self.holder = holder or {}
        super().__init__(self._render())

    def _render(self) -> str:
        pid = self.holder.get("pid")
        host = self.holder.get("host")
        ts = self.holder.get("timestamp")
        parts = []
        if pid:
            parts.append(f"PID {pid}")
        if host:
            parts.append(f"host {host}")
        age = _age_str(ts)
        if age:
            parts.append(f"held {age}")
        who = ", ".join(parts) if parts else "another athenaeum process"
        return (
            f"another athenaeum run holds the lock ({who}); "
            f"lockfile: {self.lockfile}. "
            f"Retry, pass --wait <seconds> to block, or --force to break a "
            f"stale lock."
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


def read_holder(lockfile: Path) -> dict[str, str] | None:
    """Parse the ``key: value`` holder metadata from *lockfile*.

    Returns ``None`` when the file is absent or carries no parseable metadata.
    """
    try:
        text = lockfile.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    holder: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            holder[key.strip()] = value.strip()
    return holder or None


def _pid_alive(pid: int) -> bool:
    """True if *pid* names a live process on this machine."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — still a live process.
        return True
    except OSError:
        return False
    return True


def is_stale(lockfile: Path) -> bool:
    """True if *lockfile* names a PID that is no longer alive (issue #309).

    A crashed run leaves its metadata behind even though the kernel has already
    released the ``flock``. This is a DIAGNOSTIC only — it does not gate
    ``--force`` (which breaks the lock unconditionally); it is used to label the
    audit-log line and by callers that want to report staleness. A lockfile with
    no parseable PID is treated as NOT stale (conservative).
    """
    holder = read_holder(lockfile)
    if not holder:
        return False
    pid_raw = holder.get("pid")
    if not pid_raw:
        return False
    try:
        pid = int(pid_raw)
    except ValueError:
        return False
    return not _pid_alive(pid)


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
    ) -> None:
        self.knowledge_root = Path(knowledge_root)
        self.lockfile = self.knowledge_root / LOCKFILE_NAME
        self.wait = max(0.0, float(wait))
        self.force = bool(force)
        self._fd: int | None = None
        self._acquired = False

    # -- internals ---------------------------------------------------------

    def _write_metadata(self, fd: int) -> None:
        """Truncate the lockfile and write this holder's diagnostics."""
        payload = (
            f"pid: {os.getpid()}\n"
            f"timestamp: {datetime.now(timezone.utc).isoformat()}\n"
            f"host: {socket.gethostname()}\n"
        )
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)

    def _open_fd(self) -> int:
        self.knowledge_root.mkdir(parents=True, exist_ok=True)
        return os.open(self.lockfile, os.O_RDWR | os.O_CREAT, 0o644)

    def _try_flock(self, fd: int) -> bool:
        """Non-blocking ``flock`` attempt; True on success."""
        assert fcntl is not None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    # -- public API --------------------------------------------------------

    def acquire(self) -> RunLock:
        """Acquire the lock, honoring *wait* / *force*. Returns ``self``.

        Raises :class:`LockHeld` when contended beyond the wait budget.
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

        fd = self._open_fd()
        if self._try_flock(fd):
            self._finish_acquire(fd)
            return self

        # Contended. --force breaks the lock UNCONDITIONALLY (even a live
        # holder). Log who we're overriding — PID + age — so it's auditable.
        if self.force:
            holder = read_holder(self.lockfile)
            if holder:
                pid = holder.get("pid", "?")
                age = _age_str(holder.get("timestamp")) or "unknown age"
                stale = "stale" if is_stale(self.lockfile) else "LIVE"
                log.warning(
                    "runlock: --force breaking %s lock held by PID %s (held %s) "
                    "on %s",
                    stale,
                    pid,
                    age,
                    self.lockfile,
                )
            else:
                log.warning(
                    "runlock: --force breaking lock with no holder metadata on %s",
                    self.lockfile,
                )
            os.close(fd)
            self._break_lock()
            fd = self._open_fd()
            if self._try_flock(fd):
                self._finish_acquire(fd)
                return self
            # A live holder re-grabbed the fresh inode between unlink and open.
            os.close(fd)
            raise LockHeld(self.lockfile, read_holder(self.lockfile))

        if self.wait > 0:
            deadline = time.monotonic() + self.wait
            while time.monotonic() < deadline:
                time.sleep(_POLL_INTERVAL)
                if self._try_flock(fd):
                    self._finish_acquire(fd)
                    return self

        holder = read_holder(self.lockfile)
        os.close(fd)
        raise LockHeld(self.lockfile, holder)

    def _break_lock(self) -> None:
        """Unlink the lockfile so a fresh ``flock`` inode can be acquired."""
        try:
            os.unlink(self.lockfile)
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - unusual FS error
            log.warning("runlock: could not unlink stale lockfile: %s", exc)

    def _finish_acquire(self, fd: int) -> None:
        self._fd = fd
        self._acquired = True
        try:
            self._write_metadata(fd)
        except OSError as exc:  # pragma: no cover - diagnostics only
            log.warning("runlock: could not write lock metadata: %s", exc)

    def release(self) -> None:
        """Release the lock (idempotent). Safe to call when never acquired."""
        if not self._acquired:
            return
        self._acquired = False
        fd = self._fd
        self._fd = None
        if fd is None:  # no-fcntl degrade path held no fd
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:  # pragma: no cover
            log.warning("runlock: error releasing flock: %s", exc)
        finally:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover
                pass

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
