# SPDX-License-Identifier: Apache-2.0
"""Tests for the fingerprint-keyed not-a-conflict adapter (issue athenaeum#1679, §3.7).

Proves this is an ADAPTER over the existing verdict ledger
(:mod:`athenaeum.verdicts`) -- not a second store: writes land in the same
``_verdicts/<month>.jsonl`` partitions every other verdict writer uses, reads
go through the same ``get_verdict_status``, and the only new behavior is the
fingerprint keying and the TTL decay layered on top.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from athenaeum.fingerprint import claim_pair_fingerprint
from athenaeum.not_a_conflict import (
    DECIDED_BY,
    NOT_A_CONFLICT_VERDICT,
    is_not_a_conflict,
    mark_not_a_conflict,
    not_a_conflict_key,
)
from athenaeum.runlock import RunLock
from athenaeum.verdicts import LockNotHeld, iter_live_entries, ledger_dir, make_pair_key

TEXT_A = "The API rate limit is 100 requests per minute."
TEXT_B = "The API rate limit is 100 requests per minute (soft, bursts to 150)."


class TestKeying:
    def test_not_a_conflict_key_matches_claim_pair_fingerprint(self) -> None:
        assert not_a_conflict_key(TEXT_A, TEXT_B, "factual") == claim_pair_fingerprint(
            TEXT_A, TEXT_B, "factual"
        )

    def test_key_is_order_independent(self) -> None:
        assert not_a_conflict_key(TEXT_A, TEXT_B, "factual") == not_a_conflict_key(
            TEXT_B, TEXT_A, "factual"
        )

    def test_key_structurally_disjoint_from_make_pair_key(self) -> None:
        """A fingerprint is a bare hex digest; make_pair_key always joins two
        ids with a literal '+' -- the two key spaces can never collide."""
        fp = not_a_conflict_key(TEXT_A, TEXT_B, "factual")
        assert "+" not in fp
        assert make_pair_key("alpha", "beta") != fp


class TestAdapterOverExistingStore:
    def test_write_lands_in_the_real_verdict_ledger_partitions(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            fp = mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock)
        assert ledger_dir(wiki_root).is_dir()
        live = iter_live_entries(wiki_root)
        assert any(e.pair == fp for _month, e in live)

    def test_no_second_ledger_file_created(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock)
        # Everything under wiki_root is the pre-existing _verdicts/ layout --
        # no sibling directory this module invented.
        children = {p.name for p in wiki_root.iterdir()}
        assert children == {"_verdicts"}

    def test_write_requires_acquired_lock(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)  # never acquired
        with pytest.raises(LockNotHeld):
            mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock)

    def test_entry_uses_distinct_verdict_and_decided_by_stamp(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            fp = mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock)
        entry = next(e for _month, e in iter_live_entries(wiki_root) if e.pair == fp)
        assert entry.verdict == NOT_A_CONFLICT_VERDICT
        assert entry.decided_by == DECIDED_BY


class TestIsNotAConflict:
    def test_false_before_any_adjudication(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        assert is_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual") is False

    def test_true_after_marking(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock)
        assert is_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual") is True

    def test_true_regardless_of_side_order(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock)
        assert is_not_a_conflict(wiki_root, TEXT_B, TEXT_A, "factual") is True

    def test_false_for_a_different_page_carrying_a_materially_different_claim(
        self, tmp_path: Path
    ) -> None:
        """Same subject, materially different wording -> different fingerprint
        -> re-escalates. This is the C4 fingerprint contract, unchanged."""
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock)
        other = "A completely different claim."
        assert is_not_a_conflict(wiki_root, TEXT_A, other, "factual") is False

    def test_false_for_different_conflict_type_same_texts(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock)
        assert is_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "stance") is False

    def test_cosmetic_edit_does_not_break_the_match(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock)
        assert is_not_a_conflict(wiki_root, f"  {TEXT_A.upper()}  ", TEXT_B, "factual") is True


class TestTtlDecay:
    def test_ttl_none_defers_to_ledger_freshness_only(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        old_at = (date.today() - timedelta(days=10_000)).isoformat()
        with lock:
            mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock, at=old_at)
        assert is_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", ttl_days=None) is True

    def test_within_ttl_window_is_fresh(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        decided_at = (date.today() - timedelta(days=5)).isoformat()
        with lock:
            mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock, at=decided_at)
        assert is_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", ttl_days=90) is True

    def test_past_ttl_window_expires_even_though_ledger_never_marked_it_stale(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        decided_at = (date.today() - timedelta(days=200)).isoformat()
        with lock:
            fp = mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock, at=decided_at)
        # The ledger's OWN stale flag was never touched -- confirms the decay
        # is this adapter's addition, not a side effect of the store itself.
        entry = next(e for _month, e in iter_live_entries(wiki_root) if e.pair == fp)
        assert entry.stale is False
        assert is_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", ttl_days=90) is False

    def test_ttl_boundary_is_inclusive(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        decided_at = (date.today() - timedelta(days=90)).isoformat()
        with lock:
            mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock, at=decided_at)
        assert is_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", ttl_days=90) is True

    def test_unparseable_at_fails_open_toward_expired(self, tmp_path: Path, monkeypatch) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            fp = mark_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", lock=lock)

        import athenaeum.not_a_conflict as nac_mod

        real_get_status = nac_mod.get_verdict_status

        def _garbled_status(root, pair_key):
            status = dict(real_get_status(root, pair_key))
            if pair_key == fp:
                status["at"] = "not-a-real-date"
            return status

        monkeypatch.setattr(nac_mod, "get_verdict_status", _garbled_status)
        assert is_not_a_conflict(wiki_root, TEXT_A, TEXT_B, "factual", ttl_days=30) is False
