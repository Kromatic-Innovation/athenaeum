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
from athenaeum.runlock import LockHeld, RunLock, is_stale, read_holder


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
