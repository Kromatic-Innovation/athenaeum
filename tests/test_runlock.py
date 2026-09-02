# SPDX-License-Identifier: Apache-2.0
"""Tests for the single-machine run lock and atomic sidecar appends (athenaeum#309)."""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from athenaeum import runlock
from athenaeum.atomic_io import atomic_write_text
from athenaeum.cli import main
from athenaeum.config import (
    resolve_lock_break_stale_after,
    resolve_lock_heartbeat_interval,
    resolve_lock_warn_stale_after,
)
from athenaeum.runlock import (
    LockHeld,
    RunLock,
    heartbeat_age_seconds,
    is_stale,
    read_holder,
)


class TestRunLockAcquireRelease:
    def test_second_acquire_fails_fast_while_held(self, tmp_path: Path) -> None:
        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            lock2 = RunLock(tmp_path)
            with pytest.raises(LockHeld) as excinfo:
                lock2.acquire()
            # Message names the holder (this process' PID).
            assert str(os.getpid()) in str(excinfo.value)
        finally:
            lock1.release()

    def test_releases_on_context_exit(self, tmp_path: Path) -> None:
        with RunLock(tmp_path):
            with pytest.raises(LockHeld):
                RunLock(tmp_path).acquire()
        # After the context exits the lock is free again.
        lock = RunLock(tmp_path)
        lock.acquire()
        lock.release()

    def test_lockfile_carries_pid_and_timestamp(self, tmp_path: Path) -> None:
        with RunLock(tmp_path):
            holder = read_holder(tmp_path / runlock.LOCKFILE_NAME)
        assert holder is not None
        assert holder["pid"] == str(os.getpid())
        assert holder["timestamp"]  # ISO-8601 stamp present
        assert holder["host"]

    def test_release_is_idempotent(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        lock.acquire()
        lock.release()
        lock.release()  # must not raise


class TestRunLockWait:
    def test_wait_blocks_then_succeeds_when_released(self, tmp_path: Path) -> None:
        holder = RunLock(tmp_path)
        holder.acquire()

        released = threading.Event()

        def _release_soon() -> None:
            time.sleep(0.4)
            holder.release()
            released.set()

        t = threading.Thread(target=_release_soon)
        t.start()
        try:
            waiter = RunLock(tmp_path, wait=5)
            start = time.monotonic()
            waiter.acquire()  # should block ~0.4s then succeed
            elapsed = time.monotonic() - start
            waiter.release()
            assert released.is_set()
            assert elapsed >= 0.3
        finally:
            t.join()

    def test_wait_times_out_when_still_held(self, tmp_path: Path) -> None:
        holder = RunLock(tmp_path)
        holder.acquire()
        try:
            waiter = RunLock(tmp_path, wait=0.5)
            with pytest.raises(LockHeld):
                waiter.acquire()
        finally:
            holder.release()


class TestRunLockForce:
    def test_force_breaks_held_lock(self, tmp_path: Path) -> None:
        holder = RunLock(tmp_path)
        holder.acquire()
        try:
            breaker = RunLock(tmp_path, force=True)
            breaker.acquire()  # unlinks + re-creates the lockfile inode
            try:
                holder_meta = read_holder(tmp_path / runlock.LOCKFILE_NAME)
                assert holder_meta is not None
                assert holder_meta["pid"] == str(os.getpid())
            finally:
                breaker.release()
        finally:
            holder.release()

    def test_is_stale_true_for_dead_pid(self, tmp_path: Path) -> None:
        lockfile = tmp_path / runlock.LOCKFILE_NAME
        # PID 999999 is exceedingly unlikely to be alive.
        lockfile.write_text(
            "pid: 999999\ntimestamp: 2020-01-01T00:00:00+00:00\nhost: ghost\n",
            encoding="utf-8",
        )
        assert is_stale(lockfile) is True

    def test_is_stale_false_for_live_pid(self, tmp_path: Path) -> None:
        lockfile = tmp_path / runlock.LOCKFILE_NAME
        lockfile.write_text(
            f"pid: {os.getpid()}\ntimestamp: 2020-01-01T00:00:00+00:00\nhost: me\n",
            encoding="utf-8",
        )
        assert is_stale(lockfile) is False

    def test_is_stale_false_when_no_metadata(self, tmp_path: Path) -> None:
        assert is_stale(tmp_path / "does-not-exist.lock") is False


class TestRunLockHeartbeat:
    def test_heartbeat_refreshes_time_but_preserves_acquire_fields(
        self, tmp_path: Path
    ) -> None:
        lock = RunLock(tmp_path)
        lock.acquire()
        try:
            lockfile = tmp_path / runlock.LOCKFILE_NAME
            original = read_holder(lockfile)
            assert original is not None
            time.sleep(0.05)
            lock.heartbeat()
            refreshed = read_holder(lockfile)
            assert refreshed is not None
            # pid/timestamp/host are untouched; only heartbeat moved forward.
            assert refreshed["pid"] == original["pid"]
            assert refreshed["timestamp"] == original["timestamp"]
            assert refreshed["host"] == original["host"]
            assert refreshed["heartbeat"] != original["heartbeat"]
        finally:
            lock.release()

    def test_heartbeat_is_noop_when_never_acquired(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        lock.heartbeat()  # must not raise
        assert not (tmp_path / runlock.LOCKFILE_NAME).exists()

    def test_heartbeat_is_noop_under_no_fcntl_degrade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runlock, "fcntl", None)
        lock = RunLock(tmp_path)
        lock.acquire()
        lock.heartbeat()  # no fd held in the degrade path; must not raise
        lock.release()


class TestRunLockHeartbeatTimerThread:
    """Issue athenaeum#1271: the real defect was a heartbeat that only bumped at
    phase/file boundaries -- a healthy holder mid-way through one long phase
    could go tens of minutes without a bump, byte-identical in the lockfile
    to a dead holder over that window. These tests cover the fix: a
    background thread bumps `heartbeat` on a timer, entirely independent of
    whether the caller ever calls `lock.heartbeat()` itself. A tiny injected
    interval keeps this fast and deterministic instead of sleeping the real
    30s default (never a flaky fixed-wall-clock wait).
    """

    def test_heartbeat_advances_on_its_own_without_any_caller_call(
        self, tmp_path: Path
    ) -> None:
        interval = 0.05
        lock = RunLock(tmp_path, heartbeat_interval=interval)
        lockfile = tmp_path / runlock.LOCKFILE_NAME
        lock.acquire()
        try:
            original = read_holder(lockfile)
            assert original is not None
            # Long enough for several timer bumps; the caller NEVER calls
            # lock.heartbeat() anywhere in this test.
            time.sleep(interval * 8)
            refreshed = read_holder(lockfile)
            assert refreshed is not None
            assert refreshed["heartbeat"] != original["heartbeat"]
            # pid/timestamp/host still untouched -- only heartbeat moved.
            assert refreshed["pid"] == original["pid"]
            assert refreshed["timestamp"] == original["timestamp"]
            assert refreshed["host"] == original["host"]
        finally:
            lock.release()

    def test_no_two_consecutive_heartbeat_bumps_exceed_a_bounded_gap(
        self, tmp_path: Path
    ) -> None:
        """The reworded acceptance criterion, directly: no two consecutive
        heartbeat observations on a live holder are more than N seconds
        apart, for a documented N (here N is a small, generous multiple of
        the injected interval -- the ratio is what the fix guarantees, not
        this test's absolute numbers)."""
        interval = 0.05
        bound_seconds = interval * 8
        lock = RunLock(tmp_path, heartbeat_interval=interval)
        lockfile = tmp_path / runlock.LOCKFILE_NAME
        lock.acquire()
        try:
            distinct_bumps: list[datetime] = []
            last_raw: str | None = None
            deadline = time.monotonic() + interval * 12
            while time.monotonic() < deadline:
                holder = read_holder(lockfile)
                # A bare read can transiently land in the microsecond
                # ftruncate-before-write window a refresh opens (pre-existing
                # to lease_refresh_heartbeat) and see an empty file; just
                # retry on the next poll rather than treating that as absence
                # of a bump.
                if holder is None:
                    time.sleep(interval / 5)
                    continue
                hb_raw = holder["heartbeat"]
                if hb_raw != last_raw:
                    distinct_bumps.append(datetime.fromisoformat(hb_raw))
                    last_raw = hb_raw
                time.sleep(interval / 5)
            # The background thread bumped multiple times unaided.
            assert len(distinct_bumps) >= 3
            for earlier, later in zip(distinct_bumps, distinct_bumps[1:]):
                gap = (later - earlier).total_seconds()
                assert gap <= bound_seconds
        finally:
            lock.release()

    def test_release_stops_the_background_thread_promptly(
        self, tmp_path: Path
    ) -> None:
        interval = 0.05
        lock = RunLock(tmp_path, heartbeat_interval=interval)
        lock.acquire()
        thread = lock._heartbeat_thread
        assert thread is not None
        assert thread.is_alive()
        lock.release()
        assert lock._heartbeat_thread is None
        assert not thread.is_alive()

    def test_manual_and_timer_heartbeat_calls_do_not_corrupt_the_lockfile(
        self, tmp_path: Path
    ) -> None:
        """Issue athenaeum#1271: a caller's own phase/file-boundary
        `lock.heartbeat()` calls (several already exist in the codebase) can
        race the background timer thread's own calls. Both funnel through
        the same write lock, so the lockfile must stay well-formed (never a
        partial/interleaved write) under concurrent callers."""
        interval = 0.01
        lock = RunLock(tmp_path, heartbeat_interval=interval)
        lockfile = tmp_path / runlock.LOCKFILE_NAME
        lock.acquire()
        try:
            stop = threading.Event()

            def _hammer() -> None:
                while not stop.is_set():
                    lock.heartbeat()

            hammerer = threading.Thread(target=_hammer)
            hammerer.start()
            try:
                deadline = time.monotonic() + 0.3
                saw_a_read = False
                while time.monotonic() < deadline:
                    holder = read_holder(lockfile)
                    # A bare (non-flock) read can transiently land in the
                    # microsecond ftruncate-before-write window a refresh
                    # opens (pre-existing to lease_refresh_heartbeat, not
                    # introduced here) and see an empty file -- read_holder
                    # correctly reports that as None. What must NEVER happen,
                    # from either writer racing the other, is a read landing
                    # on a PARTIAL/malformed record (e.g. some but not all of
                    # the four fields) -- every non-None read must be the
                    # complete, well-formed record.
                    if holder is None:
                        continue
                    saw_a_read = True
                    assert set(holder.keys()) == {
                        "pid",
                        "timestamp",
                        "host",
                        "heartbeat",
                    }
                    assert holder["pid"] == str(os.getpid())
                assert saw_a_read
            finally:
                stop.set()
                hammerer.join(timeout=5.0)
        finally:
            lock.release()

    def test_heartbeat_interval_falls_back_to_default_for_non_positive(
        self, tmp_path: Path
    ) -> None:
        lock = RunLock(tmp_path, heartbeat_interval=0)
        assert lock.heartbeat_interval == runlock.HEARTBEAT_INTERVAL_SECONDS
        lock2 = RunLock(tmp_path, heartbeat_interval=-5)
        assert lock2.heartbeat_interval == runlock.HEARTBEAT_INTERVAL_SECONDS


class TestHeartbeatAgeSeconds:
    def test_small_age_right_after_acquire(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        lock.acquire()
        try:
            age = heartbeat_age_seconds(tmp_path / runlock.LOCKFILE_NAME)
            assert age is not None
            assert 0 <= age < 5
        finally:
            lock.release()

    def test_falls_back_to_timestamp_when_no_heartbeat_line(
        self, tmp_path: Path
    ) -> None:
        lockfile = tmp_path / runlock.LOCKFILE_NAME
        lockfile.write_text(
            "pid: 123\ntimestamp: 2020-01-01T00:00:00+00:00\nhost: old\n",
            encoding="utf-8",
        )
        age = heartbeat_age_seconds(lockfile)
        assert age is not None
        assert age > 1_000_000  # ancient timestamp, no heartbeat line at all

    def test_none_for_missing_file(self, tmp_path: Path) -> None:
        assert heartbeat_age_seconds(tmp_path / "does-not-exist.lock") is None

    def test_none_for_garbage_timestamp(self, tmp_path: Path) -> None:
        lockfile = tmp_path / runlock.LOCKFILE_NAME
        lockfile.write_text(
            "pid: 123\ntimestamp: not-a-date\nheartbeat: also-not-a-date\nhost: x\n",
            encoding="utf-8",
        )
        assert heartbeat_age_seconds(lockfile) is None


class TestRunLockAutoBreakStaleHeartbeat:
    """Recovery for an ALIVE-but-wedged holder (issue athenaeum#397)."""

    def test_auto_break_acquires_wedged_lock(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            # Simulate a wedged-but-alive holder: heartbeat looks ancient even
            # though lock1's process (this test process) is very much alive.
            monkeypatch.setattr(
                runlock, "heartbeat_age_seconds", lambda _lockfile: 999_999.0
            )
            monkeypatch.setattr(runlock, "_pid_alive", lambda _pid: True)

            lock2 = RunLock(tmp_path, break_stale_after=1.0)
            with caplog.at_level("WARNING", logger="athenaeum.runlock"):
                lock2.acquire()  # breaks lock1's flock and succeeds
            try:
                assert any(
                    "auto-breaking wedged lock" in rec.message for rec in caplog.records
                )
                holder_meta = read_holder(tmp_path / runlock.LOCKFILE_NAME)
                assert holder_meta is not None
                assert holder_meta["pid"] == str(os.getpid())
            finally:
                lock2.release()
        finally:
            # lock1's underlying fd/flock was already invalidated by the
            # unlink+recreate; release() is still safe (idempotent close).
            lock1.release()

    def test_auto_break_does_not_fire_for_fresh_heartbeat(
        self, tmp_path: Path
    ) -> None:
        # Issue athenaeum#526 (H10): NON-VACUOUS. The old version monkeypatched
        # ``heartbeat_age_seconds`` to 0.1 — a value production could never
        # produce, because ``heartbeat()`` was never called in production. It
        # asserted the guard works under an input the system cannot generate.
        # Here the decision is driven off a REAL, freshly-refreshed heartbeat
        # line: the holder's ACQUIRE age is pushed past ``break_stale_after``,
        # but a real ``heartbeat()`` call keeps its PROGRESS age near zero, so
        # auto-break must NOT fire. No monkeypatch of the age function; and the
        # holder PID is genuinely this (alive) process, so ``holder_alive`` is
        # really True — the break's other precondition is satisfied for real.
        threshold = 0.3
        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            time.sleep(threshold + 0.2)  # acquire age now exceeds the threshold
            lock1.heartbeat()  # ...but progress (heartbeat) is fresh
            lock2 = RunLock(tmp_path, break_stale_after=threshold)
            with pytest.raises(LockHeld):
                lock2.acquire()
        finally:
            lock1.release()

    def test_stale_real_heartbeat_past_acquire_age_is_auto_broken(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The counterpart that proves the test above is not vacuous: with the
        # SAME real timing but NO heartbeat refresh, the holder's real heartbeat
        # age (falling back to acquire time) does exceed ``break_stale_after``,
        # so auto-break fires — driven entirely by real timestamps, no
        # monkeypatch of ``heartbeat_age_seconds``. Together the two tests show
        # the guard flips on the real heartbeat value, not a synthetic one.
        threshold = 0.3
        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            time.sleep(threshold + 0.2)  # acquire age exceeds threshold; no refresh
            lock2 = RunLock(tmp_path, break_stale_after=threshold)
            with caplog.at_level("WARNING", logger="athenaeum.runlock"):
                lock2.acquire()  # real heartbeat is stale → auto-break fires
            try:
                assert any(
                    "auto-breaking wedged lock" in rec.message
                    for rec in caplog.records
                )
                holder_meta = read_holder(tmp_path / runlock.LOCKFILE_NAME)
                assert holder_meta is not None
                assert holder_meta["pid"] == str(os.getpid())
            finally:
                lock2.release()
        finally:
            lock1.release()

    def test_auto_break_disabled_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            monkeypatch.setattr(
                runlock, "heartbeat_age_seconds", lambda _lockfile: 999_999.0
            )
            monkeypatch.setattr(runlock, "_pid_alive", lambda _pid: True)

            lock2 = RunLock(tmp_path)  # break_stale_after=None (default)
            with pytest.raises(LockHeld):
                lock2.acquire()
        finally:
            lock1.release()

    def test_auto_break_disabled_when_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            monkeypatch.setattr(
                runlock, "heartbeat_age_seconds", lambda _lockfile: 999_999.0
            )
            monkeypatch.setattr(runlock, "_pid_alive", lambda _pid: True)

            lock2 = RunLock(tmp_path, break_stale_after=0)
            with pytest.raises(LockHeld):
                lock2.acquire()
        finally:
            lock1.release()

    def test_no_auto_break_warning_when_disabled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            monkeypatch.setattr(
                runlock, "heartbeat_age_seconds", lambda _lockfile: 999_999.0
            )
            monkeypatch.setattr(runlock, "_pid_alive", lambda _pid: True)

            lock2 = RunLock(tmp_path)  # both thresholds disabled
            with caplog.at_level("WARNING", logger="athenaeum.runlock"):
                with pytest.raises(LockHeld):
                    lock2.acquire()
            assert not any(
                "auto-breaking wedged lock" in rec.message for rec in caplog.records
            )
        finally:
            lock1.release()


class TestWaiterStaleFdReopen:
    """Finding M6 (issue athenaeum#526): the wait loop must never acquire an orphan inode.

    A descriptor opened before contention refers to an orphan inode once a break
    (--force / auto-break) unlinks and re-creates the lockfile. Re-flocking that
    orphan "succeeds" while the real lock path is a different inode, so two
    processes each believe they hold the lock on two different inodes.
    """

    def test_holds_current_inode_true_for_live_fd(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        fd = lock._open_fd()
        try:
            assert lock._holds_current_inode(fd)
        finally:
            os.close(fd)

    def test_holds_current_inode_false_after_break(self, tmp_path: Path) -> None:
        lockfile = tmp_path / runlock.LOCKFILE_NAME
        lock = RunLock(tmp_path)
        fd = lock._open_fd()  # descriptor on inode X
        try:
            assert lock._holds_current_inode(fd)
            # Simulate a break: unlink + re-create → a new inode now at the path.
            lock._break_lock()
            fd2 = lock._open_fd()
            os.close(fd2)
            assert os.stat(lockfile).st_ino != os.fstat(fd).st_ino
            # The pre-break fd is now an orphan — the guard must reject it.
            assert not lock._holds_current_inode(fd)
        finally:
            os.close(fd)

    def test_holds_current_inode_false_when_path_unlinked(
        self, tmp_path: Path
    ) -> None:
        lock = RunLock(tmp_path)
        fd = lock._open_fd()
        try:
            lock._break_lock()  # unlink, leave nothing at the path
            assert not lock._holds_current_inode(fd)
        finally:
            os.close(fd)

    def test_wait_loop_reopens_fd_and_ignores_orphan_inode(
        self, tmp_path: Path
    ) -> None:
        # End-to-end: a waiter blocked in acquire()'s poll loop, whose lockfile
        # is rotated (unlink + fresh inode) by a concurrent break while it
        # waits, must acquire the CURRENT inode — never the orphan its
        # pre-contention fd pointed at. Under the pre-fix code the waiter
        # re-flocked its stale fd and finished on the orphan, so its held fd's
        # inode would differ from the file now at the lock path.
        lockfile = tmp_path / runlock.LOCKFILE_NAME
        holder = RunLock(tmp_path)
        holder.acquire()
        orphan_ino = os.stat(lockfile).st_ino

        acquired: dict[str, int] = {}
        errors: list[BaseException] = []

        def waiter() -> None:
            lk = RunLock(tmp_path, wait=5.0)
            try:
                lk.acquire()
                acquired["fd_ino"] = os.fstat(lk._fd).st_ino  # type: ignore[arg-type]
                acquired["path_ino"] = os.stat(lockfile).st_ino
                lk.release()
            except BaseException as exc:  # noqa: BLE001 — pragma: no cover - surface to main thread
                errors.append(exc)

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.4)  # let the waiter enter its poll loop on the orphan-to-be inode

        # A concurrent actor rotates the lockfile: unlink (orphaning the inode
        # the holder still flocks) then create a fresh inode at the path.
        os.unlink(lockfile)
        fresh = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o644)
        os.close(fresh)
        fresh_ino = os.stat(lockfile).st_ino
        assert fresh_ino != orphan_ino
        holder.release()  # drop the flock on the now-orphan inode

        t.join(timeout=10)
        assert not t.is_alive()
        assert not errors, errors
        # The waiter holds the CURRENT inode, not the orphan.
        assert acquired["fd_ino"] == acquired["path_ino"] == fresh_ino
        assert acquired["fd_ino"] != orphan_ino


class TestRunHeartbeatWiring:
    """H10 (issue athenaeum#526): librarian.run refreshes the lock heartbeat per phase."""

    def _seed_root(self, tmp_path: Path) -> Path:
        import subprocess

        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        (root / "athenaeum.yaml").write_text(
            "recall:\n  extra_intake_roots: []\n", encoding="utf-8"
        )
        subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
        return root

    def test_run_invokes_heartbeat_callback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Before this fix RunLock.heartbeat had no production caller at all, so
        # heartbeat_age_seconds reported ACQUIRE age forever. run() must now call
        # the threaded heartbeat at least once as it advances through its phases.
        from athenaeum.librarian import run

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
        root = self._seed_root(tmp_path)

        calls = {"n": 0}

        def hb() -> None:
            calls["n"] += 1

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=False,
            max_runtime=0,
            retire=False,
            heartbeat=hb,
        )
        assert rc == 0
        assert calls["n"] >= 1

    def test_run_without_heartbeat_callback_is_safe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The heartbeat seam is optional: a caller that passes no heartbeat (or
        # the --dry-run path, which holds no lock) must run without raising.
        from athenaeum.librarian import run

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
        root = self._seed_root(tmp_path)
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=False,
            max_runtime=0,
            retire=False,
        )
        assert rc == 0


class TestIngestPathHeartbeatWiring:
    """Issue athenaeum#1230: `ingest`/`session-end` thread the run lock's
    heartbeat through, the same way `run`'s H10 fix (athenaeum#526) does above —
    `ingest()`/`session_end()` forward it via **run_kwargs straight into
    `run()`, so this proves the WIRING actually reaches `run()`'s
    `ctx.tick_heartbeat()`, not just that the parameter exists.
    """

    def _seed_root(self, tmp_path: Path) -> Path:
        import subprocess

        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        (root / "athenaeum.yaml").write_text(
            "recall:\n  extra_intake_roots: []\n", encoding="utf-8"
        )
        subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
        return root

    def test_ingest_invokes_heartbeat_callback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import ingest

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
        root = self._seed_root(tmp_path)

        calls = {"n": 0}

        def hb() -> None:
            calls["n"] += 1

        # First call: no prior ingest-manifest exists, so the incremental
        # fast-no-op guard (`stored is not None and new_or_changed == 0`)
        # does not short-circuit — this reaches `run()` exactly like a real
        # on-demand `athenaeum ingest` invocation would on an empty backlog.
        result = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=tmp_path / "cache",
            dry_run=False,
            retire=False,
            install_signal_handlers=False,
            heartbeat=hb,
        )
        assert result.exit_code == 0
        assert calls["n"] >= 1

    def test_session_end_invokes_heartbeat_callback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import session_end

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
        root = self._seed_root(tmp_path)

        calls = {"n": 0}

        def hb() -> None:
            calls["n"] += 1

        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=tmp_path / "cache",
            dry_run=False,
            retire=False,
            install_signal_handlers=False,
            heartbeat=hb,
        )
        assert result.ingest.exit_code == 0
        assert calls["n"] >= 1


class TestIngestPathLongRunSurvivesContendedBreak:
    """AC2 (issue athenaeum#1230): pin the CHOSEN behaviour end to end — a
    holder that heartbeats while making progress past `break_stale_after`
    must not be broken by a contending `acquire`. Uses an injected clock (no
    real multi-hour sleep): `athenaeum.store` is where every ISO timestamp
    this module reads/writes actually comes from (`RunLock` re-exports its
    helpers — see the module docstring), so patching `store.datetime` moves
    both what `heartbeat()` writes and what `heartbeat_age_seconds` reads.
    """

    def _install_fake_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, datetime]:
        from athenaeum import store

        fake_now = {"t": datetime.now(timezone.utc)}

        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return fake_now["t"]

        monkeypatch.setattr(store, "datetime", _FakeDatetime)
        return fake_now

    def test_heartbeating_holder_survives_a_simulated_7h_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import timedelta

        fake_now = self._install_fake_clock(monkeypatch)

        holder = RunLock(tmp_path)
        holder.acquire()
        try:
            # Simulate the ingest path making progress for 7h (past the
            # deployment's 6h break_stale_after default) — heartbeating every
            # simulated 10 minutes, the same per-phase cadence run()'s
            # ctx.tick_heartbeat() uses. Total ACQUIRE age is now 7h, but the
            # heartbeat has never gone stale for more than 10 simulated
            # minutes at a stretch.
            for _ in range(42):
                fake_now["t"] = fake_now["t"] + timedelta(minutes=10)
                holder.heartbeat()

            # A contending acquire resolving the deployment default
            # (break_stale_after=6h) must NOT auto-break: the holder is
            # genuinely making progress, not wedged.
            contender = RunLock(tmp_path, break_stale_after=6 * 3600)
            with pytest.raises(LockHeld):
                contender.acquire()
        finally:
            holder.release()

    def test_non_heartbeating_holder_is_broken_under_the_same_clock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The counterpart proving the test above is not vacuous: SAME injected
        # 7h clock advance, but no heartbeat refresh (the pre-athenaeum#1230
        # `ingest` behaviour) — the holder's heartbeat age also reaches 7h, so
        # the contending acquire's break_stale_after (6h) DOES fire.
        from datetime import timedelta

        fake_now = self._install_fake_clock(monkeypatch)

        holder = RunLock(tmp_path)
        holder.acquire()
        try:
            fake_now["t"] = fake_now["t"] + timedelta(hours=7)

            contender = RunLock(tmp_path, break_stale_after=6 * 3600)
            contender.acquire()  # auto-breaks the wedged-looking holder
            try:
                holder_meta = read_holder(tmp_path / runlock.LOCKFILE_NAME)
                assert holder_meta is not None
                assert holder_meta["pid"] == str(os.getpid())
            finally:
                contender.release()
        finally:
            holder.release()  # already-broken flock; release() is still safe


class TestRunLockLoudStaleWarning:
    def test_warn_stale_after_logs_but_still_raises_lock_held(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            monkeypatch.setattr(
                runlock, "heartbeat_age_seconds", lambda _lockfile: 999_999.0
            )
            monkeypatch.setattr(runlock, "_pid_alive", lambda _pid: True)

            lock2 = RunLock(tmp_path, warn_stale_after=1.0)  # auto-break off
            with caplog.at_level("WARNING", logger="athenaeum.runlock"):
                with pytest.raises(LockHeld):
                    lock2.acquire()
            assert any(
                "holder alive but lock age" in rec.message for rec in caplog.records
            )
        finally:
            lock1.release()

    def test_warn_stale_after_disabled_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            monkeypatch.setattr(
                runlock, "heartbeat_age_seconds", lambda _lockfile: 999_999.0
            )
            monkeypatch.setattr(runlock, "_pid_alive", lambda _pid: True)

            lock2 = RunLock(tmp_path)  # warn_stale_after=None (default)
            with caplog.at_level("WARNING", logger="athenaeum.runlock"):
                with pytest.raises(LockHeld):
                    lock2.acquire()
            assert not any(
                "holder alive but lock age" in rec.message for rec in caplog.records
            )
        finally:
            lock1.release()


class TestLockHeldMessageDetail:
    """Issue athenaeum#1271, proposal item 3: an expired `--wait` (or an
    immediate fail-fast) must report the holder's pid, acquisition time, and
    last heartbeat instead of a bare "another athenaeum run holds the lock" —
    and item 4: a same-host `os.kill(pid, 0)` liveness note, independent of
    the heartbeat."""

    def test_message_reports_pid_acquisition_age_and_liveness(
        self, tmp_path: Path
    ) -> None:
        holder_lock = RunLock(tmp_path)
        holder_lock.acquire()
        try:
            waiter = RunLock(tmp_path)
            with pytest.raises(LockHeld) as excinfo:
                waiter.acquire()
            msg = str(excinfo.value)
            assert f"PID {os.getpid()}" in msg
            assert "acquired" in msg
            assert "heartbeat" in msg
            # Same-host, genuinely-live holder -> a positive liveness note.
            assert "pid alive (os.kill probe)" in msg
        finally:
            holder_lock.release()

    def test_message_reports_last_heartbeat_age_after_a_bump(
        self, tmp_path: Path
    ) -> None:
        holder_lock = RunLock(tmp_path, heartbeat_interval=0.02)
        holder_lock.acquire()
        try:
            time.sleep(0.08)  # let the timer thread bump at least once
            waiter = RunLock(tmp_path)
            with pytest.raises(LockHeld) as excinfo:
                waiter.acquire()
            msg = str(excinfo.value)
            assert "last heartbeat" in msg
            assert "heartbeat never bumped past acquire" not in msg
        finally:
            holder_lock.release()

    def test_message_never_raises_on_a_holder_with_no_metadata(
        self, tmp_path: Path
    ) -> None:
        # A lockfile the flock is held on but with unparseable/absent
        # metadata must still render a message, not raise from inside
        # LockHeld.__init__/._render.
        exc = LockHeld(tmp_path / runlock.LOCKFILE_NAME, None)
        assert "another athenaeum process" in str(exc)


class TestLivenessStrHelper:
    """Unit coverage for `runlock._liveness_str`, the belt-and-braces
    `os.kill(pid, 0)` signal (issue athenaeum#1271, proposal item 4) — kept
    independent of the heartbeat, and deliberately host-aware: a PID number
    is only meaningful on the machine that minted it."""

    def test_alive_local_pid_reports_alive(self) -> None:
        note = runlock._liveness_str(
            {"pid": str(os.getpid()), "host": socket.gethostname()}
        )
        assert note == "pid alive (os.kill probe)"

    def test_dead_local_pid_reports_not_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runlock, "_pid_alive", lambda _pid: False)
        note = runlock._liveness_str({"pid": "999999", "host": socket.gethostname()})
        assert note is not None
        assert "NOT alive locally" in note

    def test_foreign_host_pid_is_unchecked_not_guessed_dead_or_alive(self) -> None:
        # A pid number from a DIFFERENT host is never comparable to a local
        # os.kill() probe -- must not claim alive OR dead, just unchecked.
        note = runlock._liveness_str(
            {"pid": str(os.getpid()), "host": "some-other-machine.example"}
        )
        assert note is not None
        assert "unchecked" in note
        assert "some-other-machine.example" in note

    def test_missing_pid_returns_none(self) -> None:
        assert runlock._liveness_str({"host": socket.gethostname()}) is None

    def test_unparseable_pid_returns_none(self) -> None:
        assert (
            runlock._liveness_str({"pid": "not-a-number", "host": socket.gethostname()})
            is None
        )


class TestResolveLockBreakStaleAfter:
    def test_default_is_six_hours(self) -> None:
        assert resolve_lock_break_stale_after(None) == 21600.0
        assert resolve_lock_break_stale_after({}) == 21600.0
        assert resolve_lock_break_stale_after({"librarian": {}}) == 21600.0

    def test_yaml_value_wins(self) -> None:
        cfg = {"librarian": {"lock_break_stale_after": 300}}
        assert resolve_lock_break_stale_after(cfg) == 300.0

    def test_env_wins_over_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_LOCK_BREAK_STALE_AFTER", "600")
        cfg = {"librarian": {"lock_break_stale_after": 300}}
        assert resolve_lock_break_stale_after(cfg) == 600.0

    def test_zero_or_negative_disables(self) -> None:
        assert resolve_lock_break_stale_after({"librarian": {"lock_break_stale_after": 0}}) is None
        assert (
            resolve_lock_break_stale_after({"librarian": {"lock_break_stale_after": -5}})
            is None
        )

    def test_env_zero_or_negative_disables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_LOCK_BREAK_STALE_AFTER", "0")
        assert resolve_lock_break_stale_after(None) is None
        monkeypatch.setenv("ATHENAEUM_LOCK_BREAK_STALE_AFTER", "-1")
        assert resolve_lock_break_stale_after(None) is None

    def test_bool_and_non_numeric_fall_through(self) -> None:
        cfg = {"librarian": {"lock_break_stale_after": True}}
        assert resolve_lock_break_stale_after(cfg) == 21600.0
        cfg = {"librarian": {"lock_break_stale_after": "nope"}}
        assert resolve_lock_break_stale_after(cfg) == 21600.0


class TestResolveLockWarnStaleAfter:
    def test_default_is_two_hours(self) -> None:
        assert resolve_lock_warn_stale_after(None) == 7200.0
        assert resolve_lock_warn_stale_after({}) == 7200.0
        assert resolve_lock_warn_stale_after({"librarian": {}}) == 7200.0

    def test_yaml_value_wins(self) -> None:
        cfg = {"librarian": {"lock_warn_stale_after": 120}}
        assert resolve_lock_warn_stale_after(cfg) == 120.0

    def test_env_wins_over_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_LOCK_WARN_STALE_AFTER", "240")
        cfg = {"librarian": {"lock_warn_stale_after": 120}}
        assert resolve_lock_warn_stale_after(cfg) == 240.0

    def test_zero_or_negative_disables(self) -> None:
        assert resolve_lock_warn_stale_after({"librarian": {"lock_warn_stale_after": 0}}) is None
        assert (
            resolve_lock_warn_stale_after({"librarian": {"lock_warn_stale_after": -5}})
            is None
        )

    def test_env_zero_or_negative_disables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_LOCK_WARN_STALE_AFTER", "0")
        assert resolve_lock_warn_stale_after(None) is None
        monkeypatch.setenv("ATHENAEUM_LOCK_WARN_STALE_AFTER", "-1")
        assert resolve_lock_warn_stale_after(None) is None

    def test_bool_and_non_numeric_fall_through(self) -> None:
        cfg = {"librarian": {"lock_warn_stale_after": True}}
        assert resolve_lock_warn_stale_after(cfg) == 7200.0
        cfg = {"librarian": {"lock_warn_stale_after": "nope"}}
        assert resolve_lock_warn_stale_after(cfg) == 7200.0


class TestResolveLockHeartbeatInterval:
    """Issue athenaeum#1271."""

    def test_default_is_thirty_seconds(self) -> None:
        assert resolve_lock_heartbeat_interval(None) == 30.0
        assert resolve_lock_heartbeat_interval({}) == 30.0
        assert resolve_lock_heartbeat_interval({"librarian": {}}) == 30.0
        # Matches the module constant the RunLock class default falls back to.
        assert resolve_lock_heartbeat_interval(None) == runlock.HEARTBEAT_INTERVAL_SECONDS

    def test_yaml_value_wins(self) -> None:
        cfg = {"librarian": {"lock_heartbeat_interval": 10}}
        assert resolve_lock_heartbeat_interval(cfg) == 10.0

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_LOCK_HEARTBEAT_INTERVAL", "5")
        cfg = {"librarian": {"lock_heartbeat_interval": 10}}
        assert resolve_lock_heartbeat_interval(cfg) == 5.0

    def test_zero_or_negative_falls_back_to_default_not_disabled(self) -> None:
        # Unlike break/warn_stale_after, there is no disable mode: a live
        # lock should always get a timer-driven bump.
        assert (
            resolve_lock_heartbeat_interval({"librarian": {"lock_heartbeat_interval": 0}})
            == 30.0
        )
        assert (
            resolve_lock_heartbeat_interval({"librarian": {"lock_heartbeat_interval": -5}})
            == 30.0
        )

    def test_env_zero_or_negative_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_LOCK_HEARTBEAT_INTERVAL", "0")
        assert resolve_lock_heartbeat_interval(None) == 30.0
        monkeypatch.setenv("ATHENAEUM_LOCK_HEARTBEAT_INTERVAL", "-1")
        assert resolve_lock_heartbeat_interval(None) == 30.0

    def test_bool_and_non_numeric_fall_through(self) -> None:
        cfg = {"librarian": {"lock_heartbeat_interval": True}}
        assert resolve_lock_heartbeat_interval(cfg) == 30.0
        cfg = {"librarian": {"lock_heartbeat_interval": "nope"}}
        assert resolve_lock_heartbeat_interval(cfg) == 30.0


class TestNoFcntlDegrade:
    def test_acquire_without_fcntl_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runlock, "fcntl", None)
        lock = RunLock(tmp_path)
        lock.acquire()  # degrades to no-op, must not raise
        lock.release()

    def test_no_fcntl_does_not_serialize(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without fcntl there is no mutual exclusion — both "acquire".
        monkeypatch.setattr(runlock, "fcntl", None)
        a = RunLock(tmp_path)
        b = RunLock(tmp_path)
        a.acquire()
        b.acquire()  # no LockHeld because locking is skipped
        a.release()
        b.release()


class TestCommandWiring:
    def _make_knowledge_dir(self, tmp_path: Path) -> Path:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        return root

    def test_mutating_command_acquires_lock(self, tmp_path: Path) -> None:
        root = self._make_knowledge_dir(tmp_path)
        # ingest-merges is a mutating command; with no pending merges it is a
        # no-op that still takes (and leaves behind) the lockfile.
        rc = main(["ingest-merges", "--path", str(root)])
        assert rc == 0
        assert (root / runlock.LOCKFILE_NAME).exists()

    def test_readonly_command_does_not_acquire_lock(self, tmp_path: Path) -> None:
        root = self._make_knowledge_dir(tmp_path)
        rc = main(["status", "--path", str(root)])
        assert rc == 0
        assert not (root / runlock.LOCKFILE_NAME).exists()

    def test_dry_run_does_not_acquire_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Issue athenaeum#715: `dedupe wiki-pages` refuses to run at all while the
        # comparator subsystem is disabled (its own old algorithm is retired),
        # so enable it here — the assertion under test is about the LOCK, not
        # about the comparator gate.
        monkeypatch.setenv("ATHENAEUM_COMPARATOR_ENABLED", "1")
        root = self._make_knowledge_dir(tmp_path)
        # dedupe wiki-pages --dry-run must not take the lock.
        rc = main(["dedupe", "wiki-pages", "--path", str(root), "--dry-run"])
        assert rc == 0
        assert not (root / runlock.LOCKFILE_NAME).exists()

    def test_reresolve_questions_acquires_lock(self, tmp_path: Path) -> None:
        root = self._make_knowledge_dir(tmp_path)
        # Offline (no ANTHROPIC_API_KEY) is a no-op that still takes the lock.
        rc = main(["reresolve-questions", "--path", str(root)])
        assert rc == 0
        assert (root / runlock.LOCKFILE_NAME).exists()

    def test_reresolve_questions_fails_fast_when_held(self, tmp_path: Path) -> None:
        root = self._make_knowledge_dir(tmp_path)
        holder = RunLock(root)
        holder.acquire()
        try:
            rc = main(["reresolve-questions", "--path", str(root)])
            assert rc != 0  # EXIT_LOCK_HELD
        finally:
            holder.release()

    def test_mutating_command_fails_fast_when_held(self, tmp_path: Path) -> None:
        root = self._make_knowledge_dir(tmp_path)
        holder = RunLock(root)
        holder.acquire()
        try:
            rc = main(["ingest-merges", "--path", str(root)])
            assert rc != 0  # EXIT_LOCK_HELD
        finally:
            holder.release()


class TestAtomicSidecarAppends:
    def test_atomic_write_replaces_content(self, tmp_path: Path) -> None:
        target = tmp_path / "sidecar.md"
        atomic_write_text(target, "first\n")
        atomic_write_text(target, "second\n")
        assert target.read_text(encoding="utf-8") == "second\n"

    def test_crash_mid_append_leaves_original_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "sidecar.md"
        original = "# Pending\n\nblock-1\n"
        target.write_text(original, encoding="utf-8")

        # Simulate a crash after the temp file is written but before the
        # rename lands — os.replace raises.
        def _boom(src: str, dst: str) -> None:
            raise RuntimeError("simulated crash before rename")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(RuntimeError):
            atomic_write_text(target, original + "\n---\n\nblock-2\n")

        # Original file is byte-for-byte unchanged...
        assert target.read_text(encoding="utf-8") == original
        # ...and no stray temp file was left behind.
        leftovers = [p for p in tmp_path.iterdir() if p.name != "sidecar.md"]
        assert leftovers == []

    def test_mode_preserved_on_rewrite_of_existing_file(self, tmp_path: Path) -> None:
        import stat as _stat

        target = tmp_path / "sidecar.md"
        target.write_text("first\n", encoding="utf-8")
        os.chmod(target, 0o644)
        atomic_write_text(target, "second\n")
        mode = _stat.S_IMODE(target.stat().st_mode)
        # Without mode preservation, mkstemp's 0600 would narrow this to 0o600.
        assert mode == 0o644

    def test_sequential_appends_accumulate_blocks(self, tmp_path: Path) -> None:
        # NOTE: this exercises append ACCUMULATION only (two back-to-back calls
        # in one thread) — it does NOT test concurrency. The genuine
        # lost-update-under-concurrency guarantee is covered by
        # TestRunLockSerializesWriters below (the run lock, not
        # atomic_write_text, is what prevents a lost update).
        from athenaeum.pending_merges import (
            parse_pending_merges,
            write_pending_merge,
        )

        merges_path = tmp_path / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Topic A",
            sources=["wiki/a.md", "wiki/b.md"],
            rationale="dupes A",
            draft_merged_body="merged A body",
            confidence=0.9,
        )
        write_pending_merge(
            merges_path,
            merge_target_name="Topic C",
            sources=["wiki/c.md", "wiki/d.md"],
            rationale="dupes C",
            draft_merged_body="merged C body",
            confidence=0.8,
        )
        parsed = parse_pending_merges(merges_path)
        # Both blocks survive as distinct, parseable entries — no torn append.
        assert len(parsed) == 2
        names = {pm.merge_target_name for pm in parsed}
        assert names == {"Topic A", "Topic C"}


class TestRunLockSerializesWriters:
    """The run lock — not atomic_write_text — prevents a lost update.

    Two writers each do a read-modify-write of the SAME sidecar. Each holds the
    run lock across its whole critical section, so the lock serializes them and
    neither can os.replace away the other's block. A deliberate sleep between
    read and write widens the window that would lose an update without the lock;
    we assert the serialized-under-lock property (both blocks survive), which is
    deterministic (an unlocked repro would be flaky).
    """

    def test_two_lock_holders_do_not_lose_each_others_block(
        self, tmp_path: Path
    ) -> None:
        sidecar = tmp_path / "wiki" / "_pending_questions.md"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text("# Pending Questions\n", encoding="utf-8")

        errors: list[BaseException] = []

        def _append_under_lock(marker: str) -> None:
            try:
                with RunLock(tmp_path, wait=10):
                    current = sidecar.read_text(encoding="utf-8")
                    # Widen the read-modify-write window: without the lock the
                    # other thread's write would land here and be clobbered.
                    time.sleep(0.2)
                    atomic_write_text(
                        sidecar, current.rstrip("\n") + f"\n\n---\n\n{marker}\n"
                    )
            except BaseException as exc:  # noqa: BLE001 - surface to main thread
                errors.append(exc)

        t1 = threading.Thread(target=_append_under_lock, args=("block-ONE",))
        t2 = threading.Thread(target=_append_under_lock, args=("block-TWO",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []
        final = sidecar.read_text(encoding="utf-8")
        # Both writers' blocks survived — the lock serialized the RMW so neither
        # lost update occurred.
        assert "block-ONE" in final
        assert "block-TWO" in final
