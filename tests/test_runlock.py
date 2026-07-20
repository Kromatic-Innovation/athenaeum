# SPDX-License-Identifier: Apache-2.0
"""Tests for the single-machine run lock and atomic sidecar appends (#309)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from athenaeum import runlock
from athenaeum.atomic_io import atomic_write_text
from athenaeum.cli import main
from athenaeum.config import (
    resolve_lock_break_stale_after,
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
    """Recovery for an ALIVE-but-wedged holder (issue #397)."""

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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            monkeypatch.setattr(
                runlock, "heartbeat_age_seconds", lambda _lockfile: 0.1
            )
            monkeypatch.setattr(runlock, "_pid_alive", lambda _pid: True)

            lock2 = RunLock(tmp_path, break_stale_after=1.0)
            with pytest.raises(LockHeld):
                lock2.acquire()
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

    def test_dry_run_does_not_acquire_lock(self, tmp_path: Path) -> None:
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
        def _boom(src: str, dst: str) -> None:  # noqa: ARG001
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
