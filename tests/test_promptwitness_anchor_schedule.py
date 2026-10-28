# SPDX-License-Identifier: Apache-2.0
"""Tests for `promptwitness.anchor_schedule` (issue athenaeum#2156): the
periodic in-session anchor due-check and spawn guard the
``UserPromptSubmit`` hook path uses to decide whether THIS turn should be
the one that spawns a detached ``promptwitness anchor`` process.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from promptwitness import anchor_schedule, ledger


def _seed_turn_line(ledger_dir: Path) -> None:
    """A minimal non-empty ledger — `periodic_anchor_due` only cares about
    `anchor` lines, but a real hook call always has at least a `turn` line
    present by the time it checks."""
    ledger.append_line(
        ledger_dir,
        kind="turn",
        harness="claude-code",
        harness_version="",
        hook_version="test",
        session="s1",
        turn=1,
        prompt_id=None,
        prompt_kind="human",
        prompt_digest="sha256:" + "0" * 64,
        raw_digest="sha256:" + "0" * 64,
        prompt_chars=5,
        segments=[],
        channel="interactive",
        resumed=False,
        signals={},
    )


def _seed_anchor_line(ledger_dir: Path, *, ts: str) -> None:
    ledger.append_line(
        ledger_dir,
        kind="anchor",
        harness="claude-code",
        harness_version="",
        hook_version="test",
        ts=ts,
        extra={
            "subject": "witness:x:1",
            "digest": "sha256:" + "1" * 64,
            "op": "anchor",
            "attestation": {
                "source": "local-clock",
                "status": "complete",
                "claimed_time": ts,
                "proof_path": None,
                "detail": {},
            },
        },
    )


class TestPeriodicAnchorDue:
    def test_disabled_interval_is_never_due(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        assert anchor_schedule.periodic_anchor_due(tmp_path, 0) is False
        # Even with no prior anchor at all (which would otherwise be "due
        # immediately") -- 0 means disabled, full stop.
        assert anchor_schedule.periodic_anchor_due(tmp_path, -5) is False

    def test_no_prior_anchor_is_due_immediately(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        assert anchor_schedule.periodic_anchor_due(tmp_path, 15) is True

    def test_recent_anchor_is_not_due(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        _seed_anchor_line(tmp_path, ts=now.isoformat())
        assert anchor_schedule.periodic_anchor_due(tmp_path, 15, now=now) is False

    def test_old_anchor_past_interval_is_due(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        old = now - timedelta(minutes=20)
        _seed_anchor_line(tmp_path, ts=old.isoformat())
        assert anchor_schedule.periodic_anchor_due(tmp_path, 15, now=now) is True

    def test_last_claim_counts_as_recent_activity(self, tmp_path: Path) -> None:
        """A pending claim not yet reflected as a real anchor line still
        counts -- this is exactly what makes the concurrent-spawn guard
        work (the detached child has not appended anything yet)."""
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        assert anchor_schedule.periodic_anchor_due(tmp_path, 15, now=now, last_claim=now) is False

    def test_old_last_claim_is_due(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        old_claim = now - timedelta(minutes=20)
        assert (
            anchor_schedule.periodic_anchor_due(tmp_path, 15, now=now, last_claim=old_claim) is True
        )


class TestClaimPeriodicAnchorSlot:
    def test_disabled_interval_never_claims_and_touches_no_guard_file(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 0) is False
        assert not (tmp_path / anchor_schedule._GUARD_NAME).exists()

    def test_first_call_on_empty_ledger_claims(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15) is True

    def test_second_call_in_the_same_interval_does_not_claim(self, tmp_path: Path) -> None:
        """The AC: two concurrent hook firings in the same interval spawn
        at most one anchor process. Simulated here as two sequential calls
        with the SAME `now` -- the detached child from the first call has
        not appended an `anchor` line yet, so without the guard file both
        calls would independently see 'due'."""
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        first = anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now)
        second = anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now)
        assert (first, second) == (True, False)

    def test_a_later_call_past_the_interval_claims_again(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now) is True
        later = now + timedelta(minutes=20)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=later) is True

    def test_claim_is_visible_as_a_recorded_last_claim_timestamp(self, tmp_path: Path) -> None:
        """Positive control on the guard-file mechanism itself: the guard
        file's content IS the claim timestamp, usable by
        `periodic_anchor_due`'s own `last_claim` parameter."""
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now) is True
        guard_path = tmp_path / anchor_schedule._GUARD_NAME
        assert guard_path.is_file()
        recorded = anchor_schedule._parse_iso(guard_path.read_text(encoding="utf-8"))
        assert recorded == now

    def test_real_ledger_anchor_line_also_suppresses_a_fresh_claim(self, tmp_path: Path) -> None:
        """A genuinely written `anchor` line (as the detached child would
        have produced) is just as effective at suppressing a further claim
        as the guard file's own pending-claim record."""
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        _seed_anchor_line(tmp_path, ts=now.isoformat())
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now) is False


def _forbid_full_read(monkeypatch) -> None:
    """Any whole-ledger reader reached from here fails the test."""

    def _boom(*_a, **_k):
        raise AssertionError("full ledger read on the hot path")

    monkeypatch.setattr(ledger, "iter_lines", _boom)
    monkeypatch.setattr(ledger, "_iter_raw", _boom)


def _record_reads(monkeypatch) -> list[tuple[str, int, int]]:
    """Wrap the incremental reader; records ``(partition, offset, n_bytes)``."""
    reads: list[tuple[str, int, int]] = []
    real = anchor_schedule._read_from

    def _spy(path: Path, offset: int) -> bytes:
        data = real(path, offset)
        reads.append((path.name, offset, len(data)))
        return data

    monkeypatch.setattr(anchor_schedule, "_read_from", _spy)
    return reads


def _warm(ledger_dir: Path, now: datetime) -> None:
    """One claim that has to consult the ledger, writing the pointer."""
    assert anchor_schedule.claim_periodic_anchor_slot(ledger_dir, 15, now=now) is True
    assert (ledger_dir / anchor_schedule._STATE_NAME).is_file()


class TestHotPathDoesNotReadTheWholeLedger:
    """PR athenaeum#2176 review: the due check parsed every partition on
    every prompt. It must now read only what was appended since."""

    def test_recent_claim_answers_without_touching_the_ledger(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        _warm(tmp_path, now)
        _forbid_full_read(monkeypatch)
        reads = _record_reads(monkeypatch)

        later = now + timedelta(minutes=5)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=later) is False
        assert reads == []

    def test_due_check_reads_only_the_appended_bytes(self, tmp_path: Path, monkeypatch) -> None:
        now = datetime.now(timezone.utc)
        for _ in range(50):
            _seed_turn_line(tmp_path)
        _warm(tmp_path, now)
        (partition,) = ledger.list_partitions(tmp_path)
        size_at_warm = (tmp_path / partition).stat().st_size
        _seed_turn_line(tmp_path)
        appended = (tmp_path / partition).stat().st_size - size_at_warm

        _forbid_full_read(monkeypatch)
        reads = _record_reads(monkeypatch)
        later = now + timedelta(minutes=20)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=later) is True
        assert reads == [(partition, size_at_warm, appended)]

    def test_anchor_appended_after_the_pointer_is_still_seen(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        _warm(tmp_path, now)
        later = now + timedelta(minutes=20)
        _seed_anchor_line(tmp_path, ts=later.isoformat())

        _forbid_full_read(monkeypatch)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=later) is False

    def test_anchor_in_a_new_month_partition_is_seen(self, tmp_path: Path, monkeypatch) -> None:
        now = datetime(2026, 10, 31, 23, 50, tzinfo=timezone.utc)
        _seed_anchor_line(tmp_path, ts="2026-10-31T23:00:00.000Z")
        _warm(tmp_path, now)
        rollover = datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc)
        _seed_anchor_line(tmp_path, ts="2026-11-01T00:25:00.000Z")
        assert ledger.list_partitions(tmp_path) == ["2026-10.jsonl", "2026-11.jsonl"]

        _forbid_full_read(monkeypatch)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=rollover) is False
        assert (
            anchor_schedule.claim_periodic_anchor_slot(
                tmp_path, 15, now=rollover + timedelta(minutes=20)
            )
            is True
        )

    def test_torn_tail_is_not_consumed(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        (partition,) = ledger.list_partitions(tmp_path)
        size = (tmp_path / partition).stat().st_size
        with open(tmp_path / partition, "ab") as fh:
            fh.write(b'{"torn":')
        _warm(tmp_path, datetime.now(timezone.utc))
        state = json.loads((tmp_path / anchor_schedule._STATE_NAME).read_text())
        assert state["partitions"][partition]["offset"] == size


class TestPointerFallback:
    """A missing, corrupt or stale pointer must degrade to the full read
    and still give the right answer."""

    def _ledger_with_recent_anchor(self, tmp_path: Path) -> datetime:
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        _seed_anchor_line(tmp_path, ts=now.isoformat())
        return now

    def _stale_state(self, tmp_path: Path, mutate) -> None:
        """Write a pointer claiming the WHOLE ledger was scanned and held
        no anchor, then break it with *mutate*. Trusted as-is, it would
        hide the recent anchor and the claim would (wrongly) succeed."""
        partitions = ledger.list_partitions(tmp_path)
        anchor_schedule._save_state(
            tmp_path, {name: (tmp_path / name).stat().st_size for name in partitions}, None
        )
        path = tmp_path / anchor_schedule._STATE_NAME
        state = json.loads(path.read_text())
        mutate(state)
        path.write_text(json.dumps(state), encoding="utf-8")

    def test_missing_pointer_reads_everything(self, tmp_path: Path) -> None:
        now = self._ledger_with_recent_anchor(tmp_path)
        assert not (tmp_path / anchor_schedule._STATE_NAME).exists()
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now) is False

    def test_corrupt_pointer_reads_everything(self, tmp_path: Path) -> None:
        now = self._ledger_with_recent_anchor(tmp_path)
        (tmp_path / anchor_schedule._STATE_NAME).write_text("{not json", encoding="utf-8")
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now) is False

    def test_untampered_pointer_is_trusted(self, tmp_path: Path) -> None:
        """Positive control for the stale-pointer cases below."""
        now = self._ledger_with_recent_anchor(tmp_path)
        self._stale_state(tmp_path, lambda s: None)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now) is True

    def test_fingerprint_mismatch_reads_everything(self, tmp_path: Path) -> None:
        now = self._ledger_with_recent_anchor(tmp_path)

        def mutate(state):
            for entry in state["partitions"].values():
                entry["fingerprint"] = "0" * 64

        self._stale_state(tmp_path, mutate)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now) is False

    def test_offset_past_end_reads_everything(self, tmp_path: Path) -> None:
        now = self._ledger_with_recent_anchor(tmp_path)

        def mutate(state):
            for entry in state["partitions"].values():
                entry["offset"] += 10_000

        self._stale_state(tmp_path, mutate)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now) is False

    def test_vanished_partition_reads_everything(self, tmp_path: Path) -> None:
        now = self._ledger_with_recent_anchor(tmp_path)

        def mutate(state):
            state["partitions"]["1999-01.jsonl"] = {"offset": 0, "fingerprint": "x"}

        self._stale_state(tmp_path, mutate)
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now) is False

    def test_wrong_version_reads_everything(self, tmp_path: Path) -> None:
        now = self._ledger_with_recent_anchor(tmp_path)
        self._stale_state(tmp_path, lambda s: s.update(v=999))
        assert anchor_schedule.claim_periodic_anchor_slot(tmp_path, 15, now=now) is False

    def test_standalone_due_check_never_writes_the_pointer(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        assert anchor_schedule.periodic_anchor_due(tmp_path, 15) is True
        assert not (tmp_path / anchor_schedule._STATE_NAME).exists()
