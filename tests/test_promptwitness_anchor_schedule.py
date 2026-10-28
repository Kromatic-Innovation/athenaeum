# SPDX-License-Identifier: Apache-2.0
"""Tests for `promptwitness.anchor_schedule` (issue athenaeum#2156): the
periodic in-session anchor due-check and spawn guard the
``UserPromptSubmit`` hook path uses to decide whether THIS turn should be
the one that spawns a detached ``promptwitness anchor`` process.
"""

from __future__ import annotations

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
        assert (
            anchor_schedule.periodic_anchor_due(tmp_path, 15, now=now, last_claim=now)
            is False
        )

    def test_old_last_claim_is_due(self, tmp_path: Path) -> None:
        _seed_turn_line(tmp_path)
        now = datetime.now(timezone.utc)
        old_claim = now - timedelta(minutes=20)
        assert (
            anchor_schedule.periodic_anchor_due(tmp_path, 15, now=now, last_claim=old_claim)
            is True
        )


class TestClaimPeriodicAnchorSlot:
    def test_disabled_interval_never_claims_and_touches_no_guard_file(
        self, tmp_path: Path
    ) -> None:
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
