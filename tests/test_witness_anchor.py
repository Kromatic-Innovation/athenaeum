# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum.witness_anchor` (issue athenaeum#2156): the
SessionEnd anchor step -- anchoring the witness chain head and changed
receipt partitions, lock-free, bounded to a wall-clock budget, with
nothing under ``wiki/`` ever written.

``opentimestamps`` is not installed in this environment (declared only in
the ``witness-anchor`` extra), so the real
``promptwitness.anchor.OpenTimestampsAnchorer.anchor()`` degrades to a
``status: "failed"`` attestation via a real, fast `ImportError` -- no
stub needed for the default-path tests below (mirrors
``tests/test_promptwitness_anchor.py``'s ``TestMissingDependency`` note).
The "hung calendar" tests inject a FAKE ``OpenTimestampsAnchorer`` that
advances an injected fake clock instead, so the budget-enforcement
property is provable without any real sleep or network access.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
SRC = str(_REPO / "src")


@pytest.fixture()
def pw_modules():
    """Import the promptwitness modules this suite touches directly, and
    the module under test."""
    sys.path.insert(0, SRC)
    try:
        from athenaeum import witness_anchor
        from promptwitness import anchor as anchor_mod
        from promptwitness import ledger as pw_ledger

        yield witness_anchor, anchor_mod, pw_ledger
    finally:
        sys.path.remove(SRC)


def _seed_turn_line(ledger_dir: Path, pw_ledger) -> None:
    pw_ledger.append_line(
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


def _anchor_lines(pw_ledger, ledger_dir: Path):
    return [line for line in pw_ledger.iter_lines(ledger_dir) if line.kind == "anchor"]


def _write_receipts_partition(wiki_root: Path, month: str, lines: list[str]) -> Path:
    from athenaeum import receipt_sealers

    path = receipt_sealers.partition_path(wiki_root, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestHeadAnchoring:
    def test_anchors_the_head_with_local_clock_and_opentimestamps(
        self, tmp_path: Path, pw_modules
    ) -> None:
        witness_anchor, anchor_mod, pw_ledger = pw_modules
        ledger_dir = tmp_path / "ledger"
        wiki_root = tmp_path / "wiki"
        _seed_turn_line(ledger_dir, pw_ledger)

        witness_anchor.run_session_end_anchor(ledger_dir, wiki_root)

        anchors = _anchor_lines(pw_ledger, ledger_dir)
        assert len(anchors) == 2  # local-clock + opentimestamps (no rfc3161 configured)
        sources = {a.extra["attestation"]["source"] for a in anchors}
        assert sources == {"local-clock", "opentimestamps"}
        subjects = {a.extra["subject"] for a in anchors}
        assert len(subjects) == 1
        (subject,) = subjects
        assert subject.startswith("witness:")
        local = next(a for a in anchors if a.extra["attestation"]["source"] == "local-clock")
        assert local.extra["attestation"]["status"] == "complete"

    def test_empty_ledger_anchors_nothing(self, tmp_path: Path, pw_modules) -> None:
        witness_anchor, _anchor_mod, pw_ledger = pw_modules
        ledger_dir = tmp_path / "ledger"
        wiki_root = tmp_path / "wiki"

        # Never even initialised -- `_head_subject_and_digest` returns
        # (None, None) and there is nothing to anchor.
        witness_anchor.run_session_end_anchor(ledger_dir, wiki_root)

        assert not ledger_dir.exists() or pw_ledger.list_partitions(ledger_dir) == []


class TestReceiptsPartitionAnchoring:
    def test_changed_partition_is_anchored(self, tmp_path: Path, pw_modules) -> None:
        witness_anchor, _anchor_mod, pw_ledger = pw_modules
        ledger_dir = tmp_path / "ledger"
        wiki_root = tmp_path / "wiki"
        _seed_turn_line(ledger_dir, pw_ledger)
        _write_receipts_partition(wiki_root, "2026-10", ["line one", "line two"])

        witness_anchor.run_session_end_anchor(ledger_dir, wiki_root)

        anchors = _anchor_lines(pw_ledger, ledger_dir)
        receipts_anchors = [a for a in anchors if a.extra["subject"].startswith("receipts:")]
        assert len(receipts_anchors) == 2  # local-clock + opentimestamps
        assert all(a.extra["subject"] == "receipts:2026-10:2" for a in receipts_anchors)

    def test_unchanged_partition_is_not_re_anchored(self, tmp_path: Path, pw_modules) -> None:
        witness_anchor, _anchor_mod, pw_ledger = pw_modules
        ledger_dir = tmp_path / "ledger"
        wiki_root = tmp_path / "wiki"
        _seed_turn_line(ledger_dir, pw_ledger)
        _write_receipts_partition(wiki_root, "2026-10", ["line one", "line two"])

        witness_anchor.run_session_end_anchor(ledger_dir, wiki_root)
        first_count = len(
            [
                a
                for a in _anchor_lines(pw_ledger, ledger_dir)
                if a.extra["subject"].startswith("receipts:")
            ]
        )
        assert first_count == 2

        # Second pass, SAME partition content -- unchanged, must not add
        # any new receipts anchor lines.
        witness_anchor.run_session_end_anchor(ledger_dir, wiki_root)
        second_count = len(
            [
                a
                for a in _anchor_lines(pw_ledger, ledger_dir)
                if a.extra["subject"].startswith("receipts:")
            ]
        )
        assert second_count == first_count

    def test_a_subsequent_change_is_anchored_again(self, tmp_path: Path, pw_modules) -> None:
        """Positive control on the unchanged-skip mechanism: it is not
        simply always skipping after the first pass."""
        witness_anchor, _anchor_mod, pw_ledger = pw_modules
        ledger_dir = tmp_path / "ledger"
        wiki_root = tmp_path / "wiki"
        _seed_turn_line(ledger_dir, pw_ledger)
        _write_receipts_partition(wiki_root, "2026-10", ["line one"])

        witness_anchor.run_session_end_anchor(ledger_dir, wiki_root)
        _write_receipts_partition(wiki_root, "2026-10", ["line one", "line two"])
        witness_anchor.run_session_end_anchor(ledger_dir, wiki_root)

        receipts_anchors = [
            a
            for a in _anchor_lines(pw_ledger, ledger_dir)
            if a.extra["subject"].startswith("receipts:")
        ]
        subjects = {a.extra["subject"] for a in receipts_anchors}
        assert subjects == {"receipts:2026-10:1", "receipts:2026-10:2"}

    def test_empty_partition_file_is_skipped(self, tmp_path: Path, pw_modules) -> None:
        witness_anchor, _anchor_mod, pw_ledger = pw_modules
        ledger_dir = tmp_path / "ledger"
        wiki_root = tmp_path / "wiki"
        _seed_turn_line(ledger_dir, pw_ledger)
        from athenaeum import receipt_sealers

        path = receipt_sealers.partition_path(wiki_root, "2026-10")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

        witness_anchor.run_session_end_anchor(ledger_dir, wiki_root)

        receipts_anchors = [
            a
            for a in _anchor_lines(pw_ledger, ledger_dir)
            if a.extra["subject"].startswith("receipts:")
        ]
        assert receipts_anchors == []


class TestNothingUnderWikiIsWritten:
    def test_wiki_tree_is_byte_identical_after_the_anchor_step(
        self, tmp_path: Path, pw_modules
    ) -> None:
        witness_anchor, _anchor_mod, pw_ledger = pw_modules
        ledger_dir = tmp_path / "ledger"
        wiki_root = tmp_path / "wiki"
        _seed_turn_line(ledger_dir, pw_ledger)
        _write_receipts_partition(wiki_root, "2026-10", ["line one", "line two"])
        (wiki_root / "some-page.md").write_text("decoy content\n", encoding="utf-8")

        def _snapshot() -> dict[str, bytes]:
            return {
                str(p.relative_to(wiki_root)): p.read_bytes()
                for p in sorted(wiki_root.rglob("*"))
                if p.is_file()
            }

        before = _snapshot()
        witness_anchor.run_session_end_anchor(ledger_dir, wiki_root)
        after = _snapshot()

        assert after == before


class TestBudgetAndHungCalendar:
    def test_already_over_budget_detaches_instead_of_anchoring_in_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pw_modules
    ) -> None:
        witness_anchor, _anchor_mod, pw_ledger = pw_modules
        ledger_dir = tmp_path / "ledger"
        wiki_root = tmp_path / "wiki"
        _seed_turn_line(ledger_dir, pw_ledger)

        spawned: list[Path] = []
        monkeypatch.setattr(
            witness_anchor, "_spawn_detached_head_anchor", lambda d: spawned.append(d)
        )

        # A negative budget guarantees the FIRST deadline check already
        # reads "over budget" -- no fake clock needed.
        witness_anchor.run_session_end_anchor(ledger_dir, wiki_root, budget_seconds=-1.0)

        assert spawned == [ledger_dir]
        assert _anchor_lines(pw_ledger, ledger_dir) == []

    def test_hung_calendar_does_not_push_past_budget_or_cascade_to_receipts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pw_modules
    ) -> None:
        """A FAKE `OpenTimestampsAnchorer` that always consumes its ENTIRE
        given `timeout` (simulating a calendar that hangs right up to the
        deadline every single call) must not be allowed to run twice
        within one SessionEnd pass -- proving the deadline check between
        subjects, not just the per-call timeout, is what bounds this."""
        witness_anchor, anchor_mod, pw_ledger = pw_modules
        ledger_dir = tmp_path / "ledger"
        wiki_root = tmp_path / "wiki"
        _seed_turn_line(ledger_dir, pw_ledger)
        _write_receipts_partition(wiki_root, "2026-10", ["line one"])

        class _FakeClock:
            def __init__(self) -> None:
                self.value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = _FakeClock()

        class _HangingOts:
            calls = 0

            def __init__(self, ledger_dir: object, *, block_source: object = None) -> None:
                pass

            def anchor(self, digest_bytes: bytes, subject: str, *, timeout: float) -> object:
                _HangingOts.calls += 1
                clock.value += timeout  # "used the entire budget it was given"
                return anchor_mod.Attestation(
                    source="opentimestamps",
                    status="failed",
                    claimed_time="t",
                    proof_path=None,
                    detail={"reason": "simulated-hang"},
                )

        monkeypatch.setattr(anchor_mod, "OpenTimestampsAnchorer", _HangingOts)

        witness_anchor.run_session_end_anchor(
            ledger_dir, wiki_root, budget_seconds=1.0, monotonic=clock
        )

        # Exactly one hanging OTS call (the head subject's) -- the
        # receipts-partition loop must have seen the deadline already
        # passed and broken BEFORE calling it a second time.
        assert _HangingOts.calls == 1
        anchors = _anchor_lines(pw_ledger, ledger_dir)
        receipts_anchors = [a for a in anchors if a.extra["subject"].startswith("receipts:")]
        assert receipts_anchors == []
        # The head itself still got its (failed) attestation recorded --
        # the hang degrades the evidence, not the bookkeeping.
        head_ots = [
            a
            for a in anchors
            if a.extra["subject"].startswith("witness:")
            and a.extra["attestation"]["source"] == "opentimestamps"
        ]
        assert len(head_ots) == 1
        assert head_ots[0].extra["attestation"]["status"] == "failed"
