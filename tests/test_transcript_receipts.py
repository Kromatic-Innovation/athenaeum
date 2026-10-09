# SPDX-License-Identifier: Apache-2.0
"""Tests for the durable transcript-receipt ledger (issue athenaeum#753, Option A).

Covers:

* :mod:`athenaeum.transcript_receipts` — schema, no-plaintext, single-appender
  (``RunLock``), idempotent append, lookup by ``(origin_scope, memory_digest)``,
  the prefix-digest tamper/resume signal, and the sealer seam (noop + fake).
* ``athenaeum.config.resolve_transcript_receipts_enabled`` — default off,
  yaml, env precedence.
* ``resolutions._transcript_authorizes_correct`` — the receipt-aware gate,
  including the flag-off byte-identical pin and the athenaeum#752
  frontmatter-forgery pin extended to the receipt path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.config import resolve_transcript_receipts_enabled
from athenaeum.resolutions import ResolutionProposal, _transcript_authorizes_correct
from athenaeum.runlock import RunLock
from athenaeum.transcript_receipts import (
    LockNotHeld,
    NoopSealer,
    ReceiptEntry,
    SealRecord,
    append_receipt,
    classify_for_correct_gate,
    compute_transcript_prefix,
    iter_receipts,
    lookup_receipt,
    memory_digest,
    prefix_matches_receipt,
    verify_seals,
    write_receipt_for_origin,
)

# ---------------------------------------------------------------------------
# Fixture builders (mirrors tests/test_correct_authorship_gate.py's shapes)
# ---------------------------------------------------------------------------


def _write_member(
    scope_dir: Path,
    filename: str,
    *,
    body: str,
    session_id: str | None = "sess1",
    turn: int | None = 3,
    source_type: str | None = None,
) -> Path:
    scope_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if session_id is not None:
        lines.append(f"originSessionId: {session_id}")
    if turn is not None:
        lines.append(f"originTurn: {turn}")
    if source_type is not None:
        lines.append(f"source_type: {source_type}")
    lines.append("---")
    lines.append("")
    path = scope_dir / filename
    path.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")
    return path


def _write_transcript(
    projects_root: Path, scope: str, session_id: str, records: list[dict[str, object]]
) -> Path:
    scope_dir = projects_root / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _user_record(text: str) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _proposal(action: str, winner: str, confidence: float = 0.95) -> ResolutionProposal:
    return ResolutionProposal(
        recommended_winner=winner,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        rationale=f"test-{action}",
        confidence=confidence,
        source_precedence_used=["a:user > b:unsourced"],
    )


# ---------------------------------------------------------------------------
# Schema + no plaintext
# ---------------------------------------------------------------------------


class TestReceiptSchema:
    def test_round_trip(self) -> None:
        entry = ReceiptEntry(
            origin_scope="scopeA",
            memory_digest=memory_digest("the winning claim"),
            channel="user-stated",
            origin_session_id="sess1",
            origin_turn=3,
            utterance_digest="deadbeef",
            transcript_prefix_digest="cafef00d",
            transcript_line_count=1,
            recorded_at="2026-10-09T00:00:00Z",
        )
        rebuilt = ReceiptEntry.from_dict(entry.to_dict())
        assert rebuilt == entry

    def test_no_plaintext_in_any_field(self, tmp_path: Path) -> None:
        claim = "a very specific secret claim nobody else should see"
        utterance = "the user literally said: " + claim
        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "sess1", [_user_record(utterance)])
        lock = RunLock(tmp_path)
        with lock:
            entry = write_receipt_for_origin(
                tmp_path / "wiki",
                origin_scope="scopeA",
                origin_session_id="sess1",
                origin_turn=1,
                claim=claim,
                projects_root=pr,
                lock=lock,
            )
        assert entry is not None
        serialized = json.dumps(entry.to_dict())
        assert claim not in serialized
        assert claim.lower() not in serialized.lower()
        assert utterance not in serialized
        assert utterance.lower() not in serialized.lower()


# ---------------------------------------------------------------------------
# Single-appender enforced
# ---------------------------------------------------------------------------


class TestLockEnforcement:
    def test_append_without_acquired_lock_raises(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)  # never acquired
        entry = ReceiptEntry(
            origin_scope="s", memory_digest="d", channel="user-stated", recorded_at="2026-10-09"
        )
        with pytest.raises(LockNotHeld):
            append_receipt(tmp_path / "wiki", entry, lock=lock)

    def test_append_with_acquired_lock_succeeds(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        entry = ReceiptEntry(
            origin_scope="s", memory_digest="d", channel="user-stated", recorded_at="2026-10-09"
        )
        with lock:
            path = append_receipt(tmp_path / "wiki", entry, lock=lock)
        assert path.exists()


# ---------------------------------------------------------------------------
# Intake discovery never treats the ledger as intake
# ---------------------------------------------------------------------------


def test_ledger_lives_outside_auto_memory_intake_roots(tmp_path: Path) -> None:
    from athenaeum.intake import discover_auto_memory_files

    knowledge_root = tmp_path
    wiki = knowledge_root / "wiki"
    lock = RunLock(knowledge_root)
    with lock:
        append_receipt(
            wiki,
            ReceiptEntry(
                origin_scope="s", memory_digest="d", channel="user-stated", recorded_at="2026-10"
            ),
            lock=lock,
        )
    assert (wiki / "_transcript_receipts" / "2026-10.jsonl").exists()
    # discover_auto_memory_files only scans raw/auto-memory/*, never wiki/.
    found = discover_auto_memory_files(knowledge_root, config={})
    assert found == []


# ---------------------------------------------------------------------------
# Writer entrypoint
# ---------------------------------------------------------------------------


class TestWriteReceiptForOrigin:
    def test_unavailable_transcript_writes_no_receipt(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        pr.mkdir()
        lock = RunLock(tmp_path)
        with lock:
            entry = write_receipt_for_origin(
                tmp_path / "wiki",
                origin_scope="scopeA",
                origin_session_id="sess1",
                origin_turn=1,
                claim="claim",
                projects_root=pr,
                lock=lock,
            )
        assert entry is None
        assert iter_receipts(tmp_path / "wiki") == []

    def test_no_session_id_writes_no_receipt(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            entry = write_receipt_for_origin(
                tmp_path / "wiki",
                origin_scope="scopeA",
                origin_session_id=None,
                origin_turn=None,
                claim="claim",
                projects_root=tmp_path / "projects",
                lock=lock,
            )
        assert entry is None

    def test_user_stated_writes_receipt_findable_by_digest(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            entry = write_receipt_for_origin(
                wiki,
                origin_scope="scopeA",
                origin_session_id="sess1",
                origin_turn=3,
                claim="the winning claim",
                projects_root=pr,
                lock=lock,
            )
        assert entry is not None
        assert entry.channel == "user-stated"
        found = lookup_receipt(wiki, "scopeA", memory_digest("the winning claim"))
        assert found == entry

    def test_rerun_is_idempotent_no_duplicate_line(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            write_receipt_for_origin(
                wiki,
                origin_scope="scopeA",
                origin_session_id="sess1",
                origin_turn=3,
                claim="the winning claim",
                projects_root=pr,
                lock=lock,
            )
            write_receipt_for_origin(
                wiki,
                origin_scope="scopeA",
                origin_session_id="sess1",
                origin_turn=3,
                claim="the winning claim",
                projects_root=pr,
                lock=lock,
            )
        assert len(iter_receipts(wiki)) == 1


# ---------------------------------------------------------------------------
# Prefix digest: tamper vs. resumed session
# ---------------------------------------------------------------------------


class TestPrefixDigest:
    def test_resumed_session_lines_appended_still_matches(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        path = _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])
        digest, count = compute_transcript_prefix(path)
        receipt = ReceiptEntry(
            origin_scope="scopeA",
            memory_digest="d",
            channel="user-stated",
            transcript_prefix_digest=digest,
            transcript_line_count=count,
        )
        # Resume: append more lines after the receipt was recorded.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_user_record("a later turn")) + "\n")
        assert prefix_matches_receipt(path, receipt) is True

    def test_edited_line_within_prefix_is_modified(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        path = _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])
        digest, count = compute_transcript_prefix(path)
        receipt = ReceiptEntry(
            origin_scope="scopeA",
            memory_digest="d",
            channel="user-stated",
            transcript_prefix_digest=digest,
            transcript_line_count=count,
        )
        # Tamper: rewrite the one line the receipt covers.
        path.write_text(json.dumps(_user_record("a forged claim")) + "\n", encoding="utf-8")
        assert prefix_matches_receipt(path, receipt) is False

    def test_truncated_transcript_is_modified(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        path = _write_transcript(
            pr,
            "scopeA",
            "sess1",
            [_user_record("line one"), _user_record("line two")],
        )
        digest, count = compute_transcript_prefix(path)
        assert count == 2
        receipt = ReceiptEntry(
            origin_scope="scopeA",
            memory_digest="d",
            channel="user-stated",
            transcript_prefix_digest=digest,
            transcript_line_count=count,
        )
        path.write_text(json.dumps(_user_record("line one")) + "\n", encoding="utf-8")
        assert prefix_matches_receipt(path, receipt) is False


# ---------------------------------------------------------------------------
# Sealer seam
# ---------------------------------------------------------------------------


class _FakeSealer:
    def __init__(self) -> None:
        self.seal_calls: list[tuple[Path, int, int, list[str]]] = []
        self.verify_calls: list[tuple[Path, int, int, SealRecord | None]] = []

    def seal(self, partition_path, start_line, end_line, lines):
        self.seal_calls.append((partition_path, start_line, end_line, list(lines)))
        return SealRecord(sealer="fake", detail={"n": end_line})

    def verify(self, partition_path, start_line, end_line, seal):
        self.verify_calls.append((partition_path, start_line, end_line, seal))
        return seal is not None and seal.sealer == "fake"


class TestSealerSeam:
    def test_noop_sealer_writes_nothing_beyond_the_ledger_line(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        entry = ReceiptEntry(
            origin_scope="s", memory_digest="d", channel="user-stated", recorded_at="2026-10"
        )
        with lock:
            append_receipt(wiki, entry, lock=lock, sealer=NoopSealer())
        ledger_dir = wiki / "_transcript_receipts"
        assert [p.name for p in ledger_dir.iterdir()] == ["2026-10.jsonl"]

    def test_fake_sealer_receives_exact_appended_range(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        fake = _FakeSealer()
        entry = ReceiptEntry(
            origin_scope="s", memory_digest="d1", channel="user-stated", recorded_at="2026-10"
        )
        with lock:
            append_receipt(wiki, entry, lock=lock, sealer=fake)
        assert len(fake.seal_calls) == 1
        path, start, end, lines = fake.seal_calls[0]
        assert path == wiki / "_transcript_receipts" / "2026-10.jsonl"
        assert start == end == 1
        assert len(lines) == 1

    def test_verify_seals_delegates_to_sealer(self, tmp_path: Path) -> None:
        fake = _FakeSealer()
        seal = SealRecord(sealer="fake", detail={})
        wiki = tmp_path / "wiki"
        assert verify_seals(wiki, "2026-10", 1, 1, seal, sealer=fake) is True
        assert fake.verify_calls == [(wiki / "_transcript_receipts" / "2026-10.jsonl", 1, 1, seal)]


# ---------------------------------------------------------------------------
# Config resolver
# ---------------------------------------------------------------------------


class TestConfigResolver:
    def test_default_off(self) -> None:
        assert resolve_transcript_receipts_enabled(None) is False
        assert resolve_transcript_receipts_enabled({}) is False

    def test_yaml_true(self) -> None:
        assert (
            resolve_transcript_receipts_enabled(
                {"librarian": {"transcript_receipts_enabled": True}}
            )
            is True
        )

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_TRANSCRIPT_RECEIPTS_ENABLED", "true")
        assert (
            resolve_transcript_receipts_enabled(
                {"librarian": {"transcript_receipts_enabled": False}}
            )
            is True
        )


# ---------------------------------------------------------------------------
# classify_for_correct_gate — the decision table
# ---------------------------------------------------------------------------


class TestClassifyForCorrectGate:
    def test_flag_off_matches_live_classification(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])
        authorized, ref = classify_for_correct_gate(
            "scopeA",
            "sess1",
            1,
            "the winning claim",
            projects_root=pr,
            wiki_root=tmp_path / "wiki",
            receipts_enabled=False,
        )
        assert authorized is True
        assert ref.startswith("user-stated")

    def test_absent_transcript_user_stated_receipt_authorizes(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        digest = memory_digest("the winning claim")
        with lock:
            append_receipt(
                wiki,
                ReceiptEntry(
                    origin_scope="scopeA",
                    memory_digest=digest,
                    channel="user-stated",
                    origin_session_id="sess1",
                    origin_turn=1,
                    recorded_at="2026-10-01T00:00:00Z",
                ),
                lock=lock,
            )
        pr = tmp_path / "projects"
        pr.mkdir()  # rolled off — no transcript file.
        authorized, ref = classify_for_correct_gate(
            "scopeA",
            "sess1",
            1,
            "the winning claim",
            projects_root=pr,
            wiki_root=wiki,
            receipts_enabled=True,
        )
        assert authorized is True
        assert ref.startswith("receipt")

    def test_absent_transcript_no_receipt_refuses(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        pr.mkdir()
        authorized, ref = classify_for_correct_gate(
            "scopeA",
            "sess1",
            1,
            "the winning claim",
            projects_root=pr,
            wiki_root=tmp_path / "wiki",
            receipts_enabled=True,
        )
        assert authorized is False
        assert ref.startswith("unavailable")

    def test_absent_transcript_digest_mismatch_refuses(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            append_receipt(
                wiki,
                ReceiptEntry(
                    origin_scope="scopeA",
                    memory_digest=memory_digest("the ORIGINAL claim"),
                    channel="user-stated",
                    origin_session_id="sess1",
                    origin_turn=1,
                    recorded_at="2026-10-01T00:00:00Z",
                ),
                lock=lock,
            )
        pr = tmp_path / "projects"
        pr.mkdir()
        # Memory was edited since the receipt was written — digest differs.
        authorized, ref = classify_for_correct_gate(
            "scopeA",
            "sess1",
            1,
            "an EDITED claim",
            projects_root=pr,
            wiki_root=wiki,
            receipts_enabled=True,
        )
        assert authorized is False
        assert ref.startswith("unavailable")

    def test_present_transcript_matching_prefix_unchanged(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        path = _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])
        digest, count = compute_transcript_prefix(path)
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            append_receipt(
                wiki,
                ReceiptEntry(
                    origin_scope="scopeA",
                    memory_digest=memory_digest("the winning claim"),
                    channel="user-stated",
                    origin_session_id="sess1",
                    origin_turn=1,
                    transcript_prefix_digest=digest,
                    transcript_line_count=count,
                    recorded_at="2026-10-01T00:00:00Z",
                ),
                lock=lock,
            )
        authorized, ref = classify_for_correct_gate(
            "scopeA",
            "sess1",
            1,
            "the winning claim",
            projects_root=pr,
            wiki_root=wiki,
            receipts_enabled=True,
        )
        assert authorized is True
        assert ref.startswith("user-stated")

    def test_present_transcript_resumed_session_not_flagged(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        path = _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])
        digest, count = compute_transcript_prefix(path)
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            append_receipt(
                wiki,
                ReceiptEntry(
                    origin_scope="scopeA",
                    memory_digest=memory_digest("the winning claim"),
                    channel="user-stated",
                    origin_session_id="sess1",
                    origin_turn=1,
                    transcript_prefix_digest=digest,
                    transcript_line_count=count,
                    recorded_at="2026-10-01T00:00:00Z",
                ),
                lock=lock,
            )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_user_record("a later turn")) + "\n")
        authorized, ref = classify_for_correct_gate(
            "scopeA",
            "sess1",
            1,
            "the winning claim",
            projects_root=pr,
            wiki_root=wiki,
            receipts_enabled=True,
        )
        assert authorized is True
        assert not ref.startswith("transcript-modified")

    def test_present_transcript_tampered_prefix_refuses(self, tmp_path: Path) -> None:
        pr = tmp_path / "projects"
        path = _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])
        digest, count = compute_transcript_prefix(path)
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            append_receipt(
                wiki,
                ReceiptEntry(
                    origin_scope="scopeA",
                    memory_digest=memory_digest("the winning claim"),
                    channel="user-stated",
                    origin_session_id="sess1",
                    origin_turn=1,
                    transcript_prefix_digest=digest,
                    transcript_line_count=count,
                    recorded_at="2026-10-01T00:00:00Z",
                ),
                lock=lock,
            )
        # Tamper the line the receipt covers, keeping it a user-stated match
        # (so without the prefix check this would still authorize).
        path.write_text(
            json.dumps(_user_record("the winning claim -- edited by an agent")) + "\n",
            encoding="utf-8",
        )
        authorized, ref = classify_for_correct_gate(
            "scopeA",
            "sess1",
            1,
            "the winning claim",
            projects_root=pr,
            wiki_root=wiki,
            receipts_enabled=True,
        )
        assert authorized is False
        assert ref.startswith("transcript-modified")

    def test_no_frontmatter_session_recovered_receipt_reaches_transcript_check(
        self, tmp_path: Path
    ) -> None:
        pr = tmp_path / "projects"
        path = _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])
        digest, count = compute_transcript_prefix(path)
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            append_receipt(
                wiki,
                ReceiptEntry(
                    origin_scope="scopeA",
                    memory_digest=memory_digest("the winning claim"),
                    channel="user-stated",
                    origin_session_id="sess1",  # recovered, never in frontmatter
                    origin_turn=1,
                    transcript_prefix_digest=digest,
                    transcript_line_count=count,
                    recorded_at="2026-10-01T00:00:00Z",
                ),
                lock=lock,
            )
        authorized, ref = classify_for_correct_gate(
            "scopeA",
            None,
            None,
            "the winning claim",
            projects_root=pr,
            wiki_root=wiki,
            receipts_enabled=True,
        )
        assert authorized is True
        assert ref.startswith("user-stated")

    def test_no_frontmatter_session_no_receipt_refuses(self, tmp_path: Path) -> None:
        authorized, ref = classify_for_correct_gate(
            "scopeA",
            None,
            None,
            "claim",
            projects_root=tmp_path / "projects",
            wiki_root=tmp_path / "wiki",
            receipts_enabled=True,
        )
        assert authorized is False
        assert "no origin session" in ref


# ---------------------------------------------------------------------------
# resolutions._transcript_authorizes_correct — flag-off byte-identical pin
# and the athenaeum#752 forgery pin extended to the receipt path.
# ---------------------------------------------------------------------------


class TestGateFlagOffByteIdentical:
    def test_flag_off_with_wiki_root_matches_flag_off_without(self, tmp_path: Path) -> None:
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(scope_dir, "a.md", body="the winning claim")
        b = _write_member(scope_dir, "b.md", body="the losing claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        pr.mkdir()  # no transcript — would be "unavailable" either way.

        without = _transcript_authorizes_correct(
            _proposal("correct_a", "a"), [a, b], projects_root=pr
        )
        with_wiki_root = _transcript_authorizes_correct(
            _proposal("correct_a", "a"), [a, b], projects_root=pr, wiki_root=tmp_path / "wiki"
        )
        assert without == with_wiki_root

    def test_flag_off_ignores_a_planted_receipt(self, tmp_path: Path) -> None:
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(scope_dir, "a.md", body="the winning claim")
        b = _write_member(scope_dir, "b.md", body="the losing claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        pr.mkdir()  # rolled off.
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            append_receipt(
                wiki,
                ReceiptEntry(
                    origin_scope="scopeA",
                    memory_digest=memory_digest("the winning claim"),
                    channel="user-stated",
                    origin_session_id="sess1",
                    origin_turn=3,
                    recorded_at="2026-10-01T00:00:00Z",
                ),
                lock=lock,
            )
        # config=None -> flag resolves off regardless of the planted receipt.
        authorized, channel_ref = _transcript_authorizes_correct(
            _proposal("correct_a", "a"), [a, b], config=None, projects_root=pr, wiki_root=wiki
        )
        assert authorized is False
        assert channel_ref.startswith("unavailable")


class TestGateFrontmatterForgeryPinExtended:
    def test_forged_source_type_no_receipt_no_transcript_refuses(self, tmp_path: Path) -> None:
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(scope_dir, "a.md", body="the winning claim", source_type="user-stated")
        b = _write_member(scope_dir, "b.md", body="the losing claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        pr.mkdir()
        authorized, channel_ref = _transcript_authorizes_correct(
            _proposal("correct_a", "a"),
            [a, b],
            config={"librarian": {"transcript_receipts_enabled": True}},
            projects_root=pr,
            wiki_root=tmp_path / "wiki",
        )
        assert authorized is False
        assert channel_ref.startswith("unavailable")

    def test_flag_on_rolled_off_memory_authorized_by_real_receipt(self, tmp_path: Path) -> None:
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(scope_dir, "a.md", body="the winning claim")
        b = _write_member(scope_dir, "b.md", body="the losing claim", session_id=None, turn=None)
        wiki = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            # Build the receipt directly via the writer against a transcript
            # that exists now, then simulate roll-off by pointing the gate
            # at an EMPTY projects_root.
            pr_live = tmp_path / "projects_live"
            _write_transcript(pr_live, "scopeA", "sess1", [_user_record("the winning claim")])
            entry = write_receipt_for_origin(
                wiki,
                origin_scope="scopeA",
                origin_session_id="sess1",
                origin_turn=3,
                claim="the winning claim",
                projects_root=pr_live,
                lock=lock,
            )
        assert entry is not None

        pr_rolled_off = tmp_path / "projects_rolled_off"
        pr_rolled_off.mkdir()
        authorized, channel_ref = _transcript_authorizes_correct(
            _proposal("correct_a", "a"),
            [a, b],
            config={"librarian": {"transcript_receipts_enabled": True}},
            projects_root=pr_rolled_off,
            wiki_root=wiki,
        )
        assert authorized is True
        assert channel_ref.startswith("receipt")
