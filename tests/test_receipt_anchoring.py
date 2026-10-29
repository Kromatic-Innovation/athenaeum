# SPDX-License-Identifier: Apache-2.0
"""Tests for nightly witness/receipt anchoring and the off-machine mirror
(issue athenaeum#2040).

One class per acceptance criterion:

* :class:`TestAnchoringSealerIsASealer` — ``AnchoringSealer`` satisfies the
  ``transcript_receipts.Sealer`` Protocol, and the dependency still runs one
  way only (``transcript_receipts`` imports nothing from the sealer modules),
  which is what makes "``transcript_receipts.py`` has no diff" a structural
  property rather than a promise about one commit.
* :class:`TestAnchorsOnceThenNotAgain` — a changed partition is anchored
  once; a re-run with no change makes NO new stamp; an attestation the
  lock-free athenaeum#2156 path already obtained for the SAME digest is
  imported, not re-stamped.
* :class:`TestLockIsRequired` — every ``wiki/`` write this issue adds refuses
  an unacquired lock with ``LockNotHeld``.
* :class:`TestMirrorSchema` — mirror records carry exactly ``ledger_id``,
  ``seq``, ``line_hash`` and the attestations, and no prompt digest (nor any
  other prompt-derived field) ever reaches the pushed knowledge repo. Proofs
  land under ``proofs/``.
* :class:`TestSelectAnchorers` — the selection seam, including
  ``local-clock`` always being present and ``rfc3161`` being dropped with no
  TSA configured.
* :class:`TestUpgradePending` — a pending OpenTimestamps attestation is
  upgraded by the nightly pass, and an already-complete one is not re-offered.
* :class:`TestReceiptsAnchorCli` — ``athenaeum receipts anchor`` end to end
  under the real run lock.
* :class:`TestLibrarianCallSite` — the nightly wiring: gated off by default,
  reached when the flag is on, and never fatal.

Every network adapter is a fake. The real ones are covered by
``tests/test_promptwitness_anchor.py``; what is under test here is athenaeum's
selection, idempotency, lock and mirror behaviour, none of which should need a
calendar.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum import receipt_sealers, witness_anchor
from athenaeum.receipt_sealers import (
    AnchoringSealer,
    anchors_partition_path,
    iter_stored_anchors,
    partition_digest,
    receipts_subject,
    select_anchorers,
)
from athenaeum.runlock import RunLock
from athenaeum.transcript_receipts import (
    LockNotHeld,
    ReceiptEntry,
    Sealer,
    append_receipt,
    partition_path,
)

MONTH = "2026-10"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


class _FakeAnchorer:
    """A stand-in :class:`promptwitness.anchor.Anchorer`.

    Counts its own ``anchor`` calls — the "anchored once, re-run makes no new
    stamp" criterion is a statement about how many times a calendar is asked,
    so counting is the only way to assert it.
    """

    def __init__(self, source: str = "opentimestamps", status: str = "complete") -> None:
        self.source = source
        self.status = status
        self.anchor_calls: list[tuple[bytes, str]] = []
        self.upgrade_calls: list[str] = []
        self.verify_calls: list[bytes] = []

    def anchor(self, digest: bytes, subject: str, *, timeout: float = 10.0) -> Any:
        from promptwitness.anchor import Attestation

        self.anchor_calls.append((digest, subject))
        return Attestation(
            source=self.source,
            status=self.status,
            claimed_time="2026-10-29T00:00:00.000Z",
            proof_path=f"anchors/{subject.replace(':', '_')}.ots",
            detail={"calendars_tried": ["https://example.invalid"]},
        )

    def upgrade(self, att: Any) -> Any:
        from promptwitness.anchor import Attestation

        self.upgrade_calls.append(att.status)
        if att.status != "pending":
            return att
        return Attestation(
            source=self.source,
            status="complete",
            claimed_time=att.claimed_time,
            proof_path=att.proof_path,
            detail={"upgraded": True},
        )

    def verify(self, att: Any, digest: bytes) -> Any:
        from promptwitness.anchor import VerifyResult

        self.verify_calls.append(digest)
        return VerifyResult(
            status=att.status,
            verify_source=f"block-explorer:example.invalid/{self.source}",
            detail={},
        )


class _UnacquiredLock:
    """The failure case the lock rule exists for: a lock object that was
    never acquired. Production code reads only ``acquired``, so this is the
    whole surface."""

    acquired = False


class _HeldLock:
    acquired = True


def _receipt(wiki: Path, knowledge_root: Path, digest: str) -> None:
    """Append one real receipt line, through the real ledger writer."""
    lock = RunLock(knowledge_root)
    with lock:
        append_receipt(
            wiki,
            ReceiptEntry(
                origin_scope="scopeA",
                memory_digest=digest,
                channel="user-stated",
                recorded_at=f"{MONTH}-09T00:00:00Z",
            ),
            lock=lock,
        )


def _ledger_with_receipts(tmp_path: Path, count: int = 2) -> tuple[Path, Path, Path]:
    """``(knowledge_root, wiki_root, witness_ledger_dir)`` with *count*
    receipts already appended."""
    knowledge_root = tmp_path / "knowledge"
    wiki = knowledge_root / "wiki"
    for n in range(count):
        _receipt(wiki, knowledge_root, f"digest{n}")
    return knowledge_root, wiki, tmp_path / "witness"


def _sealer(wiki: Path, anchorers: list[Any], lock: Any = None) -> AnchoringSealer:
    return AnchoringSealer(
        anchorers=anchorers, lock=lock or _HeldLock(), wiki_root=wiki, timeout=1.0
    )


def _append_witness_anchor(
    ledger_dir: Path, *, subject: str, digest: str, source: str, status: str
) -> None:
    """Record an attestation in the witness ledger the way the lock-free
    athenaeum#2156 paths do — an ``anchor`` line and nothing else."""
    from promptwitness import ledger as pw_ledger

    pw_ledger.append_line(
        ledger_dir,
        kind="anchor",
        harness="claude-code",
        harness_version="",
        hook_version="promptwitness test",
        extra={
            "subject": subject,
            "digest": digest,
            "op": "anchor",
            "attestation": {
                "source": source,
                "status": status,
                "claimed_time": "2026-10-28T00:00:00.000Z",
                "proof_path": None,
                "detail": {"from": "session-end"},
            },
        },
    )


def _append_witness_turn(ledger_dir: Path, *, prompt_digest: str) -> None:
    """A real ``turn`` line — the thing whose digest must NEVER be mirrored."""
    from promptwitness import ledger as pw_ledger

    pw_ledger.append_line(
        ledger_dir,
        kind="turn",
        harness="claude-code",
        harness_version="1.0",
        hook_version="promptwitness test",
        session="session-abc",
        turn=1,
        prompt_id="prompt-1",
        prompt_kind="human",
        prompt_digest=prompt_digest,
        raw_digest="sha256:rawrawraw",
        prompt_chars=42,
        channel="interactive",
    )


# ---------------------------------------------------------------------------
# AC1 — the Sealer Protocol, and the one-way dependency
# ---------------------------------------------------------------------------


class TestAnchoringSealerIsASealer:
    def test_isinstance_of_the_protocol(self, tmp_path: Path) -> None:
        sealer = _sealer(tmp_path, [_FakeAnchorer()])
        assert isinstance(sealer, Sealer)

    def test_transcript_receipts_imports_nothing_from_the_sealers(self) -> None:
        """The dependency runs ONE way only.

        This is the durable half of "``transcript_receipts.py`` has no diff":
        the ledger module cannot acquire a reason to change when a sealer is
        added if it never names one. A counter-example (an
        ``import athenaeum.receipt_sealers`` landing in that module) fails
        here, where the reason is legible, rather than as a surprising import
        cycle later.
        """
        source = Path("src/athenaeum/transcript_receipts.py").read_text(encoding="utf-8")
        for forbidden in ("receipt_sealers", "witness_anchor"):
            assert f"import {forbidden}" not in source
            assert f"from athenaeum.{forbidden}" not in source

    def test_seal_record_names_the_anchor_sealer(self, tmp_path: Path) -> None:
        knowledge_root, wiki, _ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        partition = partition_path(wiki, MONTH)
        lines = partition.read_text(encoding="utf-8").splitlines()
        record = _sealer(wiki, [_FakeAnchorer()]).seal(partition, 1, len(lines), lines)
        assert record is not None
        assert record.sealer == receipt_sealers.ANCHOR_SEALER_NAME == "anchor"
        assert record.detail["subject"] == receipts_subject(MONTH, len(lines))
        assert record.detail["digest"] == partition_digest(lines)
        assert [att["source"] for att in record.detail["attestations"]] == ["opentimestamps"]

    def test_verify_accepts_the_live_range_and_records_verify_source(
        self, tmp_path: Path
    ) -> None:
        knowledge_root, wiki, _ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        partition = partition_path(wiki, MONTH)
        lines = partition.read_text(encoding="utf-8").splitlines()
        anchorer = _FakeAnchorer()
        sealer = _sealer(wiki, [anchorer])
        record = sealer.seal(partition, 1, len(lines), lines)
        assert record is not None

        assert sealer.verify(partition, 1, len(lines), record) is True
        assert [r["verify_source"] for r in sealer.last_verify_results] == [
            "block-explorer:example.invalid/opentimestamps"
        ]

    def test_verify_rejects_a_mutated_range(self, tmp_path: Path) -> None:
        knowledge_root, wiki, _ledger = _ledger_with_receipts(tmp_path)
        partition = partition_path(wiki, MONTH)
        lines = partition.read_text(encoding="utf-8").splitlines()
        sealer = _sealer(wiki, [_FakeAnchorer()])
        record = sealer.seal(partition, 1, len(lines), lines)
        assert record is not None

        _receipt(wiki, knowledge_root, "a-later-receipt")
        grown = partition.read_text(encoding="utf-8").splitlines()
        assert sealer.verify(partition, 1, len(grown), record) is False

    def test_verify_rejects_an_absent_seal(self, tmp_path: Path) -> None:
        sealer = _sealer(tmp_path, [_FakeAnchorer()])
        assert sealer.verify(tmp_path / "nope.jsonl", 1, 1, None) is False

    def test_verify_reports_an_unconfigured_source_as_unverified(
        self, tmp_path: Path
    ) -> None:
        """An operator who drops an adapter from ``adapters`` must not thereby
        turn its old records into passes."""
        knowledge_root, wiki, _ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        partition = partition_path(wiki, MONTH)
        lines = partition.read_text(encoding="utf-8").splitlines()
        record = _sealer(wiki, [_FakeAnchorer()]).seal(partition, 1, len(lines), lines)
        assert record is not None

        dropped = _sealer(wiki, [])
        assert dropped.verify(partition, 1, len(lines), record) is False
        assert dropped.last_verify_results == [
            {
                "status": "failed",
                "verify_source": "unconfigured",
                "detail": {"source": "opentimestamps"},
            }
        ]


# ---------------------------------------------------------------------------
# AC2 — anchored once, not re-stamped, and imported when already attested
# ---------------------------------------------------------------------------


class TestAnchorsOnceThenNotAgain:
    def test_changed_partition_anchored_once_and_rerun_makes_no_new_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        anchorer = _FakeAnchorer()
        monkeypatch.setattr(
            receipt_sealers, "select_anchorers", lambda config, ledger_dir=None: [anchorer]
        )

        first = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert first.anchored == [MONTH]
        receipts_stamps = [c for c in anchorer.anchor_calls if c[1].startswith("receipts:")]
        assert len(receipts_stamps) == 1

        records = iter_stored_anchors(wiki, MONTH)
        assert len(records) == 1
        assert records[0].op == "anchor"
        assert records[0].end_line == 2

        second = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert second.anchored == []
        assert second.unchanged == [MONTH]
        assert len(iter_stored_anchors(wiki, MONTH)) == 1
        # The criterion, stated as a count: the calendar was asked ONCE for
        # this partition's digest, across two passes.
        receipts_stamps = [c for c in anchorer.anchor_calls if c[1].startswith("receipts:")]
        assert len(receipts_stamps) == 1

    def test_a_grown_partition_is_anchored_again(self, tmp_path: Path) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        witness_anchor.run_nightly_anchor(ledger, wiki, config=None, lock=_HeldLock())
        _receipt(wiki, knowledge_root, "one-more")

        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert report.anchored == [MONTH]
        records = iter_stored_anchors(wiki, MONTH)
        assert [r.end_line for r in records] == [2, 3]

    def test_an_existing_2156_attestation_is_imported_not_restamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        partition = partition_path(wiki, MONTH)
        lines = partition.read_text(encoding="utf-8").splitlines()
        digest = partition_digest(lines)
        _append_witness_anchor(
            ledger,
            subject=receipts_subject(MONTH, len(lines)),
            digest=digest,
            source="opentimestamps",
            status="pending",
        )

        anchorer = _FakeAnchorer()
        monkeypatch.setattr(
            receipt_sealers, "select_anchorers", lambda config, ledger_dir=None: [anchorer]
        )

        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )

        assert report.imported == [MONTH]
        assert report.anchored == []
        records = iter_stored_anchors(wiki, MONTH)
        assert len(records) == 1
        assert records[0].op == "import"
        assert records[0].digest == digest
        assert records[0].attestations[0]["detail"] == {"from": "session-end"}
        # The whole point: no calendar round trip was spent for this subject.
        receipts_stamps = [
            call for call in anchorer.anchor_calls if call[1].startswith("receipts:")
        ]
        assert receipts_stamps == []

    def test_an_attestation_for_a_different_digest_is_not_imported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Import is keyed on the DIGEST, not the subject — an attestation
        for other content must never be lifted onto this partition."""
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        partition = partition_path(wiki, MONTH)
        lines = partition.read_text(encoding="utf-8").splitlines()
        _append_witness_anchor(
            ledger,
            subject=receipts_subject(MONTH, len(lines)),
            digest="sha256:" + "0" * 64,
            source="opentimestamps",
            status="complete",
        )

        anchorer = _FakeAnchorer()
        monkeypatch.setattr(
            receipt_sealers, "select_anchorers", lambda config, ledger_dir=None: [anchorer]
        )
        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert report.anchored == [MONTH]
        assert iter_stored_anchors(wiki, MONTH)[0].op == "anchor"

    def test_the_chain_head_is_anchored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        _append_witness_turn(ledger, prompt_digest="sha256:" + "a" * 64)

        anchorer = _FakeAnchorer()
        monkeypatch.setattr(
            receipt_sealers, "select_anchorers", lambda config, ledger_dir=None: [anchorer]
        )
        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert report.head_subject is not None
        assert report.head_subject.startswith("witness:")
        assert report.head_subject in [subject for _digest, subject in anchorer.anchor_calls]

    def test_an_empty_witness_ledger_anchors_no_head(self, tmp_path: Path) -> None:
        """Nothing to anchor, nothing anchored. The ledger must be empty
        after step 1 too, so this fixture has no receipt partitions
        either — step 1's own ``anchor`` lines are what give a run with
        receipts a head to anchor even when the hook never wrote a turn.
        """
        wiki = tmp_path / "knowledge" / "wiki"
        report = witness_anchor.run_nightly_anchor(
            tmp_path / "witness", wiki, config=None, lock=_HeldLock()
        )
        assert report.head_subject is None
        assert report.anchored == []
        assert report.mirrored == 0

    def test_receipts_but_no_turns_anchors_no_head(self, tmp_path: Path) -> None:
        """Step 1's own ``anchor`` lines are not witness CONTENT.

        With receipts but no turns there is nothing a chain-head anchor
        would bound, so the head is skipped even though step 1 moved it.
        """
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert report.anchored == [MONTH]
        assert report.head_subject is None

    def test_a_quiet_night_stamps_nothing_and_mirrors_nothing(
        self, tmp_path: Path
    ) -> None:
        """The churn case (Seer review on PR athenaeum#2185, finding 1).

        A second pass with no new receipts and no new turns must produce no
        anchor line, no mirror record and therefore no commit — the
        librarian only snapshots when something was anchored, imported or
        mirrored.
        """
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        _append_witness_turn(ledger, prompt_digest="sha256:" + "e" * 64)
        first = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert first.head_subject is not None

        second = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert second.to_dict() == {
            "anchored": [],
            "imported": [],
            "unchanged": [MONTH],
            "head_subject": None,
            "upgraded": [],
            "mirrored": 0,
        }

    def test_a_turn_after_the_last_head_anchor_is_anchored_again(
        self, tmp_path: Path
    ) -> None:
        """The gate is "content past the anchored head seq", not "newer than
        the last anchor LINE": step 1 writes ``receipts:`` anchor lines,
        which bound a receipt partition and say nothing about any witness
        line, so a genuine day of turns must still get its head anchored.
        """
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        _append_witness_turn(ledger, prompt_digest="sha256:" + "e" * 64)
        witness_anchor.run_nightly_anchor(ledger, wiki, config=None, lock=_HeldLock())
        _append_witness_turn(ledger, prompt_digest="sha256:" + "f" * 64)

        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert report.head_subject is not None

    def test_a_failed_attestation_is_retried_next_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seer review on PR athenaeum#2185, finding 2.

        Digest equality alone is not "already anchored". An offline pass
        records a ``failed`` attempt; keying the skip on the digest would
        make that failure permanent and the partition would never be
        offered to a calendar again — contradicting this module's own "the
        next pass retries" contract.
        """
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root

        class _Flaky(_FakeAnchorer):
            def __init__(self) -> None:
                super().__init__()
                self.statuses = ["failed", "complete"]

            def anchor(self, digest: bytes, subject: str, *, timeout: float = 10.0) -> Any:
                self.status = self.statuses.pop(0) if self.statuses else "complete"
                return super().anchor(digest, subject, timeout=timeout)

        anchorer = _Flaky()
        monkeypatch.setattr(
            receipt_sealers, "select_anchorers", lambda config, ledger_dir=None: [anchorer]
        )
        first = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert first.anchored == [MONTH]

        second = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert second.anchored == [MONTH], "a failed stamp must be retried"
        assert second.imported == [], "a failed attestation is not importable either"

        third = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert third.anchored == []
        assert third.unchanged == [MONTH], "once every source landed, leave it alone"

    def test_a_partial_import_anchors_instead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Held attestations must cover EVERY configured source to import.

        A partial import (local-clock held, OpenTimestamps not) would
        record the digest as handled and the missing source would never be
        attempted.
        """
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        partition = partition_path(wiki, MONTH)
        lines = partition.read_text(encoding="utf-8").splitlines()
        _append_witness_anchor(
            ledger,
            subject=receipts_subject(MONTH, len(lines)),
            digest=partition_digest(lines),
            source="local-clock",
            status="complete",
        )

        anchorer = _FakeAnchorer()  # source "opentimestamps"
        monkeypatch.setattr(
            receipt_sealers, "select_anchorers", lambda config, ledger_dir=None: [anchorer]
        )
        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert report.imported == []
        assert report.anchored == [MONTH]
        assert [c[1] for c in anchorer.anchor_calls] == [receipts_subject(MONTH, len(lines))]


# ---------------------------------------------------------------------------
# AC3 — every wiki/ write happens under an acquired RunLock
# ---------------------------------------------------------------------------


class TestLockIsRequired:
    def test_seal_refuses_an_unacquired_lock(self, tmp_path: Path) -> None:
        knowledge_root, wiki, _ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        partition = partition_path(wiki, MONTH)
        lines = partition.read_text(encoding="utf-8").splitlines()
        sealer = _sealer(wiki, [_FakeAnchorer()], lock=_UnacquiredLock())
        with pytest.raises(LockNotHeld):
            sealer.seal(partition, 1, len(lines), lines)
        assert iter_stored_anchors(wiki, MONTH) == []

    def test_append_stored_anchor_refuses_an_unacquired_lock(self, tmp_path: Path) -> None:
        record = receipt_sealers.StoredAnchor(
            month=MONTH, start_line=1, end_line=1, subject="s", digest="d", op="anchor"
        )
        with pytest.raises(LockNotHeld):
            receipt_sealers.append_stored_anchor(tmp_path, record, lock=_UnacquiredLock())
        assert not anchors_partition_path(tmp_path, MONTH).exists()

    def test_mirror_refuses_an_unacquired_lock(self, tmp_path: Path) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        _append_witness_anchor(
            ledger, subject="witness:x:0", digest="sha256:aa", source="local-clock",
            status="complete",
        )
        with pytest.raises(LockNotHeld):
            witness_anchor.mirror_witness_anchors(ledger, wiki, lock=_UnacquiredLock())
        assert not witness_anchor.witness_anchors_dir(wiki).exists()

    def test_the_nightly_pass_refuses_an_unacquired_lock(self, tmp_path: Path) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        with pytest.raises(LockNotHeld):
            witness_anchor.run_nightly_anchor(
                ledger, wiki, config=None, lock=_UnacquiredLock()
            )
        assert iter_stored_anchors(wiki, MONTH) == []
        assert not witness_anchor.witness_anchors_dir(wiki).exists()

    def test_a_real_runlock_released_mid_flight_is_refused(self, tmp_path: Path) -> None:
        """The property is "acquired", not "is a RunLock": a real lock that
        has been released must be refused exactly like the stub above."""
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        lock = RunLock(knowledge_root)
        lock.acquire()
        lock.release()
        with pytest.raises(LockNotHeld):
            witness_anchor.run_nightly_anchor(ledger, wiki, config=None, lock=lock)


# ---------------------------------------------------------------------------
# AC4 — the mirror's schema, and no prompt digest off-machine
# ---------------------------------------------------------------------------


def _mirror_records(wiki: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(witness_anchor.witness_anchors_dir(wiki).glob("*.jsonl")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                out.append(json.loads(raw))
    return out


class TestMirrorSchema:
    def test_records_hold_exactly_the_four_declared_keys(self, tmp_path: Path) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        _append_witness_turn(ledger, prompt_digest="sha256:" + "a" * 64)
        witness_anchor.run_nightly_anchor(ledger, wiki, config=None, lock=_HeldLock())

        records = _mirror_records(wiki)
        assert records
        for record in records:
            assert set(record) == set(witness_anchor.MIRROR_RECORD_KEYS)
            assert isinstance(record["ledger_id"], str) and record["ledger_id"]
            assert isinstance(record["seq"], int)
            assert record["line_hash"].startswith("sha256:")
            assert isinstance(record["attestations"], list) and record["attestations"]

    def test_no_prompt_digest_reaches_the_pushed_repo(self, tmp_path: Path) -> None:
        """The schema assertion the criterion names, done as a SCAN rather
        than a key check: the mirror lives in the pushed knowledge repo, and
        a short human utterance's digest is dictionary-recoverable, so the
        bar is that no prompt-derived value appears anywhere in the bytes —
        not merely that the keys look right.
        """
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        prompt_digest = "sha256:" + "a" * 64
        _append_witness_turn(ledger, prompt_digest=prompt_digest)
        witness_anchor.run_nightly_anchor(ledger, wiki, config=None, lock=_HeldLock())

        for path in witness_anchor.witness_anchors_dir(wiki).rglob("*"):
            if not path.is_file():
                continue
            raw = path.read_bytes()
            assert prompt_digest.encode() not in raw
            assert b"sha256:rawrawraw" not in raw
            assert b"session-abc" not in raw
            for forbidden in (b"prompt_digest", b"raw_digest", b"prompt_chars", b"segments"):
                assert forbidden not in raw

    def test_the_line_hash_mirrored_is_the_chain_authoritative_one(
        self, tmp_path: Path
    ) -> None:
        """A mirror whose hashes do not match the chain proves nothing.

        The authoritative value for a line is what the NEXT line records as
        its ``prev``, so that is what this compares against.
        """
        from promptwitness import ledger as pw_ledger

        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        witness_anchor.run_nightly_anchor(ledger, wiki, config=None, lock=_HeldLock())
        _append_witness_turn(ledger, prompt_digest="sha256:" + "b" * 64)

        raws = []
        for path in sorted(ledger.glob("*.jsonl")):
            raws.extend(json.loads(raw) for raw in path.read_text().splitlines() if raw.strip())
        prev_by_seq = {raws[i]["seq"]: raws[i + 1]["prev"] for i in range(len(raws) - 1)}
        assert pw_ledger.verify_chain(ledger).ok

        mirrored = {r["seq"]: r["line_hash"] for r in _mirror_records(wiki)}
        assert mirrored
        for seq, line_hash in mirrored.items():
            if seq in prev_by_seq:
                assert line_hash == prev_by_seq[seq]

    def test_rerunning_the_mirror_writes_nothing_new(self, tmp_path: Path) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        witness_anchor.run_nightly_anchor(ledger, wiki, config=None, lock=_HeldLock())
        before = _mirror_records(wiki)
        assert before

        again = witness_anchor.mirror_witness_anchors(ledger, wiki, lock=_HeldLock())
        assert again == 0
        assert _mirror_records(wiki) == before

    def test_proofs_are_copied_under_proofs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        anchorer = _FakeAnchorer()
        monkeypatch.setattr(
            receipt_sealers, "select_anchorers", lambda config, ledger_dir=None: [anchorer]
        )
        witness_anchor.run_nightly_anchor(ledger, wiki, config=None, lock=_HeldLock())

        # The fake names a proof path the way the real adapter does; write the
        # bytes it claims to have written so the copy has something to find.
        proof_rel = anchorer.anchor_calls[0][1].replace(":", "_")
        src = ledger / "anchors" / f"{proof_rel}.ots"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"ots-proof-bytes")

        witness_anchor.mirror_witness_anchors(ledger, wiki, lock=_HeldLock())
        # Re-anchor so a NEW line (and so a new mirror record) carries the
        # proof path now that the file exists.
        witness_anchor._anchor_subject_with(
            ledger, [anchorer], subject="witness:test:99", digest="sha256:" + "c" * 64,
            timeout=1.0,
        )
        src2 = ledger / "anchors" / "witness_test_99.ots"
        src2.write_bytes(b"ots-proof-bytes")
        witness_anchor.mirror_witness_anchors(ledger, wiki, lock=_HeldLock())

        copied = witness_anchor.witness_anchor_proofs_dir(wiki) / "witness_test_99.ots"
        assert copied.is_file()
        assert copied.read_bytes() == b"ots-proof-bytes"

    def test_a_local_clock_attestation_with_no_proof_still_mirrors(
        self, tmp_path: Path
    ) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        witness_anchor.run_nightly_anchor(ledger, wiki, config=None, lock=_HeldLock())
        records = _mirror_records(wiki)
        assert records
        assert all(att["source"] == "local-clock" for r in records for att in r["attestations"])
        assert not witness_anchor.witness_anchor_proofs_dir(wiki).exists()


# ---------------------------------------------------------------------------
# The selection seam
# ---------------------------------------------------------------------------


class TestSelectAnchorers:
    def test_default_is_local_clock_only(self, tmp_path: Path) -> None:
        anchorers = select_anchorers(None, tmp_path)
        assert [a.source for a in anchorers] == ["local-clock"]

    def test_local_clock_is_always_present(self, tmp_path: Path) -> None:
        config = {"librarian": {"receipt_anchor": {"adapters": ["opentimestamps"]}}}
        anchorers = select_anchorers(config, tmp_path)
        assert [a.source for a in anchorers] == ["opentimestamps", "local-clock"]

    def test_rfc3161_is_dropped_with_no_tsa_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PROMPTWITNESS_RFC3161_URL", raising=False)
        config = {"librarian": {"receipt_anchor": {"adapters": ["rfc3161", "local-clock"]}}}
        assert [a.source for a in select_anchorers(config, tmp_path)] == ["local-clock"]

    def test_rfc3161_is_offered_once_a_tsa_is_configured(self, tmp_path: Path) -> None:
        config = {
            "librarian": {
                "receipt_anchor": {
                    "adapters": ["rfc3161"],
                    "rfc3161_url": "https://tsa.example.invalid/tsr",
                }
            }
        }
        sources = [a.source for a in select_anchorers(config, tmp_path)]
        assert sources == ["rfc3161:tsa.example.invalid", "local-clock"]

    def test_an_unknown_adapter_raises(self, tmp_path: Path) -> None:
        from athenaeum.config import ReceiptAnchorConfigError

        config = {"librarian": {"receipt_anchor": {"adapters": ["carrier-pigeon"]}}}
        with pytest.raises(ReceiptAnchorConfigError):
            select_anchorers(config, tmp_path)

    def test_select_sealer_is_unchanged_by_anchoring(self) -> None:
        """The two seams are independent: anchoring must not have changed
        what ``select_sealer`` returns for a default config."""
        from athenaeum.transcript_receipts import NoopSealer

        assert isinstance(receipt_sealers.select_sealer(None), NoopSealer)


# ---------------------------------------------------------------------------
# Pending-proof upgrade
# ---------------------------------------------------------------------------


class TestUpgradePending:
    def test_a_pending_attestation_is_upgraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        _append_witness_anchor(
            ledger, subject="witness:pw-x:7", digest="sha256:" + "d" * 64,
            source="opentimestamps", status="pending",
        )
        anchorer = _FakeAnchorer()
        monkeypatch.setattr(
            receipt_sealers, "select_anchorers", lambda config, ledger_dir=None: [anchorer]
        )
        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert "witness:pw-x:7" in report.upgraded
        assert anchorer.upgrade_calls == ["pending"]

    def test_an_already_complete_attestation_is_not_upgraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        _append_witness_anchor(
            ledger, subject="witness:pw-x:7", digest="sha256:" + "d" * 64,
            source="opentimestamps", status="complete",
        )
        anchorer = _FakeAnchorer()
        monkeypatch.setattr(
            receipt_sealers, "select_anchorers", lambda config, ledger_dir=None: [anchorer]
        )
        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert report.upgraded == []
        assert anchorer.upgrade_calls == []

    def test_a_pending_attestation_with_no_configured_adapter_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        _append_witness_anchor(
            ledger, subject="witness:pw-x:7", digest="sha256:" + "d" * 64,
            source="opentimestamps", status="pending",
        )
        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert report.upgraded == []


# ---------------------------------------------------------------------------
# The CLI, under the real run lock
# ---------------------------------------------------------------------------


class TestReceiptsAnchorCli:
    def test_anchor_runs_under_the_real_lock_and_reports_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del wiki
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(ledger))

        rc = main(["receipts", "anchor", "--path", str(knowledge_root), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["anchored"] == [MONTH]
        assert payload["mirrored"] >= 1

        rc = main(["receipts", "anchor", "--path", str(knowledge_root), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["anchored"] == []
        assert payload["unchanged"] == [MONTH]

    def test_anchor_releases_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.cli import main

        knowledge_root, _wiki, ledger = _ledger_with_receipts(tmp_path)
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(ledger))
        assert main(["receipts", "anchor", "--path", str(knowledge_root), "--json"]) == 0

        # A second acquisition must succeed: a command that leaked the lock
        # would wedge every later mutating command on this machine.
        lock = RunLock(knowledge_root)
        lock.acquire()
        assert lock.acquired
        lock.release()

    def test_anchor_text_output_names_partitions_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(ledger))
        assert main(["receipts", "anchor", "--path", str(knowledge_root)]) == 0
        out = capsys.readouterr().out
        assert f"anchored:  {MONTH}" in out
        assert "receipts anchor:" in out
        # No receipt content, ever: the ledger's own line bytes must not
        # appear in a terminal transcript.
        for line in partition_path(wiki, MONTH).read_text(encoding="utf-8").splitlines():
            assert line not in out


# ---------------------------------------------------------------------------
# The nightly call site
# ---------------------------------------------------------------------------


class TestLibrarianCallSite:
    def _ctx(self, tmp_path: Path, *, config: dict[str, Any] | None, lock: Any) -> Any:
        from athenaeum.librarian import RunContext

        knowledge_root = tmp_path / "knowledge"
        ctx = RunContext.__new__(RunContext)
        ctx.knowledge_root = knowledge_root
        ctx.wiki_root = knowledge_root / "wiki"
        ctx.config = config
        ctx.dry_run = False
        ctx.lock = lock
        return ctx

    def test_off_by_default_does_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        monkeypatch.delenv("ATHENAEUM_WITNESS_LEDGER_ENABLED", raising=False)
        calls: list[Any] = []
        monkeypatch.setattr(
            witness_anchor, "run_nightly_anchor", lambda *a, **k: calls.append(a)
        )
        librarian._maybe_run_nightly_anchor(self._ctx(tmp_path, config=None, lock=_HeldLock()))
        assert calls == []

    def test_dry_run_does_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        calls: list[Any] = []
        monkeypatch.setattr(
            witness_anchor, "run_nightly_anchor", lambda *a, **k: calls.append(a)
        )
        ctx = self._ctx(
            tmp_path,
            config={"librarian": {"witness_ledger_enabled": True}},
            lock=_HeldLock(),
        )
        ctx.dry_run = True
        librarian._maybe_run_nightly_anchor(ctx)
        assert calls == []

    def test_no_lock_does_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        calls: list[Any] = []
        monkeypatch.setattr(
            witness_anchor, "run_nightly_anchor", lambda *a, **k: calls.append(a)
        )
        librarian._maybe_run_nightly_anchor(
            self._ctx(
                tmp_path,
                config={"librarian": {"witness_ledger_enabled": True}},
                lock=None,
            )
        )
        assert calls == []

    def test_flag_on_with_a_lock_runs_the_pass_and_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del wiki
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(ledger))
        snapshots: list[str] = []
        monkeypatch.setattr(
            librarian.FilesystemStore,
            "snapshot",
            lambda self, label: snapshots.append(label),
        )
        ctx = self._ctx(
            tmp_path,
            config={"librarian": {"witness_ledger_enabled": True}},
            lock=_HeldLock(),
        )
        ctx.knowledge_root = knowledge_root
        ctx.wiki_root = knowledge_root / "wiki"
        librarian._maybe_run_nightly_anchor(ctx)

        assert iter_stored_anchors(knowledge_root / "wiki", MONTH)
        assert _mirror_records(knowledge_root / "wiki")
        assert len(snapshots) == 1

    def test_a_failure_is_non_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("calendar on fire")

        monkeypatch.setattr(witness_anchor, "run_nightly_anchor", _boom)
        ctx = self._ctx(
            tmp_path,
            config={"librarian": {"witness_ledger_enabled": True}},
            lock=_HeldLock(),
        )
        librarian._maybe_run_nightly_anchor(ctx)  # must not raise

    def test_the_call_site_is_before_the_push(self) -> None:
        """Ordering is load-bearing: the mirror exists so the records LEAVE
        the machine, which only happens if they are written and committed
        before the post-run push."""
        source = Path("src/athenaeum/librarian.py").read_text(encoding="utf-8")
        anchor_at = source.index("    _maybe_run_nightly_anchor(ctx)")
        push_at = source.index("_maybe_push_after_run(", anchor_at)
        # Adjacent, not merely earlier: nothing between the two but the
        # call's own blank line.
        between = source[anchor_at + len("    _maybe_run_nightly_anchor(ctx)") : push_at]
        assert between.strip() == ""

    def test_a_still_pending_upgrade_appends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A calendar can stay pending for hours. A line per nightly per
        unconfirmed stamp would grow the ledger — and the off-machine
        mirror — while recording nothing new."""
        from promptwitness import ledger as pw_ledger

        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        _append_witness_anchor(
            ledger, subject="witness:pw-x:7", digest="sha256:" + "d" * 64,
            source="opentimestamps", status="pending",
        )

        class _StillPending(_FakeAnchorer):
            def upgrade(self, att: Any) -> Any:
                self.upgrade_calls.append(att.status)
                return att

        anchorer = _StillPending()
        monkeypatch.setattr(
            receipt_sealers, "select_anchorers", lambda config, ledger_dir=None: [anchorer]
        )
        before = len(list(pw_ledger.iter_lines(ledger)))
        report = witness_anchor.run_nightly_anchor(
            ledger, wiki, config=None, lock=_HeldLock()
        )
        assert report.upgraded == []
        assert anchorer.upgrade_calls == ["pending"]
        # Only the head anchor and the receipts anchor were appended — no
        # upgrade line.
        appended = [
            line
            for line in list(pw_ledger.iter_lines(ledger))[before:]
            if line.extra.get("op") == "upgrade"
        ]
        assert appended == []


class TestWholePartitionOnly:
    def test_a_sub_range_is_refused(self, tmp_path: Path) -> None:
        """The subject is ``receipts:<YYYY-MM>:<line_count>``, defined over
        the whole partition: a sub-range would record a line count that does
        not describe the digest beside it."""
        knowledge_root, wiki, _ledger = _ledger_with_receipts(tmp_path, count=3)
        del knowledge_root
        partition = partition_path(wiki, MONTH)
        lines = partition.read_text(encoding="utf-8").splitlines()
        sealer = _sealer(wiki, [_FakeAnchorer()])
        with pytest.raises(ValueError, match="whole partitions only"):
            sealer.seal(partition, 2, 3, lines[1:])
        assert iter_stored_anchors(wiki, MONTH) == []

    def test_select_sealer_can_never_name_this_sealer(self) -> None:
        """Why raising above is safe: intake cannot reach this sealer.

        ``librarian.receipt_sealer``'s vocabulary is ``noop``/``signing``;
        anything else raises in the resolver, so no configuration makes
        ``AnchoringSealer`` the sealer ``append_receipt`` is handed.
        """
        from athenaeum.config import ReceiptSealerConfigError

        with pytest.raises(ReceiptSealerConfigError):
            receipt_sealers.select_sealer({"librarian": {"receipt_sealer": "anchor"}})


class TestSessionEndIsUnchanged:
    """The nightly's new-content gate must NOT leak into athenaeum#2156.

    SessionEnd's contract is "anchor the chain head at EVERY SessionEnd",
    lock-free and inside a 10 second budget — it is a session boundary, not
    a periodic sweep, and it has no run lock with which to consult or write
    anything else. The two functions live in one module and their head
    reads look identical, which is exactly why this pin exists.
    """

    def test_session_end_anchors_the_head_with_no_new_content(
        self, tmp_path: Path
    ) -> None:
        from promptwitness import ledger as pw_ledger

        knowledge_root, wiki, ledger = _ledger_with_receipts(tmp_path)
        del knowledge_root
        _append_witness_anchor(
            ledger, subject="witness:pw-x:1", digest="sha256:" + "a" * 64,
            source="local-clock", status="complete",
        )
        before = len(list(pw_ledger.iter_lines(ledger)))

        witness_anchor.run_session_end_anchor(ledger, wiki, budget_seconds=5.0)

        after = list(pw_ledger.iter_lines(ledger))
        head_anchors = [
            line
            for line in after[before:]
            if line.kind == "anchor"
            and str(line.extra.get("subject", "")).startswith("witness:")
        ]
        assert head_anchors, "SessionEnd anchors the head unconditionally"
