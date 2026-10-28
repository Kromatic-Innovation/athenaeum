# SPDX-License-Identifier: Apache-2.0
"""Tests for promptwitness.verify (athenaeum#2155).

Every path builds filesystem paths from ``tmp_path`` only -- never a
hardcoded machine-specific home-directory literal (the public-safe-lint
gate rejects that shape even in test fixtures).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from promptwitness import canon, ledger, verify

_KEY_SUFFIX = 0


def _append_turn(d: Path, **kwargs: object) -> ledger.WitnessLine:
    text = str(kwargs.pop("text", "hi"))
    defaults: dict[str, object] = {
        "kind": "turn",
        "harness": "claude-code",
        "harness_version": "2.1.308",
        "hook_version": "promptwitness-test 0.1.0",
        "session": "s1",
        "turn": 0,
        "prompt_id": "p0",
        "prompt_kind": "human",
        "prompt_digest": canon.digest(canon.normalize(text)),
        "raw_digest": canon.digest(text),
        "prompt_chars": len(text),
        "channel": "interactive",
        "resumed": False,
        "signals": {},
    }
    defaults.update(kwargs)
    return ledger.append_line(d, **defaults)  # type: ignore[arg-type]


class TestVerifyTranscriptMachineAuthored:
    def test_non_human_role_is_machine_authored_with_no_lookup(self, tmp_path: Path) -> None:
        idx = ledger.LedgerIndex.build(tmp_path)  # empty ledger
        records = [{"role": "machine", "text": "<task-notification>done</task-notification>"}]
        verdicts = verify.verify_transcript(records, idx)
        assert len(verdicts) == 1
        assert verdicts[0].verdict == verify.VERDICT_MACHINE_AUTHORED
        assert verdicts[0].is_human is False


class TestVerifyTranscriptWitnessed:
    def test_matching_prompt_id_and_digest_is_witnessed(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id="p1", session="sA", text="hello there")
        idx = ledger.LedgerIndex.build(tmp_path)
        records = [{"role": "human", "text": "hello there", "session": "sA", "prompt_id": "p1"}]
        verdicts = verify.verify_transcript(records, idx)
        assert verdicts[0].verdict == verify.VERDICT_WITNESSED
        assert verdicts[0].is_human is True

    def test_session_digest_fallback_when_no_prompt_id_given(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id="p1", session="sA", text="hello there")
        idx = ledger.LedgerIndex.build(tmp_path)
        records = [{"role": "human", "text": "hello there", "session": "sA"}]
        verdicts = verify.verify_transcript(records, idx)
        assert verdicts[0].verdict == verify.VERDICT_WITNESSED


class TestVerifyTranscriptNotWitnessedInserted:
    def test_no_matching_line_at_all_is_inserted(self, tmp_path: Path) -> None:
        idx = ledger.LedgerIndex.build(tmp_path)  # empty ledger
        records = [{"role": "human", "text": "brand new", "session": "sA", "prompt_id": "ghost"}]
        verdicts = verify.verify_transcript(records, idx)
        assert verdicts[0].verdict == verify.VERDICT_NOT_WITNESSED_INSERTED


class TestVerifyTranscriptDigestMismatchEdited:
    def test_matching_identity_but_different_text_is_edited(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id="p1", session="sA", text="original text")
        idx = ledger.LedgerIndex.build(tmp_path)
        records = [{"role": "human", "text": "tampered text", "session": "sA", "prompt_id": "p1"}]
        verdicts = verify.verify_transcript(records, idx)
        assert verdicts[0].verdict == verify.VERDICT_DIGEST_MISMATCH_EDITED


class TestVerifyTranscriptChannel:
    def test_headless_channel_overrides_digest_match(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id="p1", session="sA", text="hi", channel="headless")
        idx = ledger.LedgerIndex.build(tmp_path)
        records = [{"role": "human", "text": "hi", "session": "sA", "prompt_id": "p1"}]
        verdicts = verify.verify_transcript(records, idx)
        assert verdicts[0].verdict == verify.VERDICT_HEADLESS

    def test_bridge_channel_overrides_digest_match(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id="p1", session="sA", text="hi", channel="bridge")
        idx = ledger.LedgerIndex.build(tmp_path)
        records = [{"role": "human", "text": "hi", "session": "sA", "prompt_id": "p1"}]
        verdicts = verify.verify_transcript(records, idx)
        assert verdicts[0].verdict == verify.VERDICT_BRIDGE


class TestVerifyTranscriptChainBroken:
    def test_line_at_or_after_a_chain_break_is_chain_broken(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id="p1", session="sA", text="first", turn=1)
        _append_turn(tmp_path, prompt_id="p2", session="sA", text="second", turn=2)
        # Corrupt the first line's `prev` in place -- breaks the chain
        # starting at the SECOND line (p2), the same seq verify_chain
        # itself reports as first_break_seq.
        partitions = sorted(tmp_path.glob("*.jsonl"))
        path = partitions[0]
        raw_lines = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
        assert raw_lines[0]["seq"] == 1
        raw_lines[0]["prev"] = "sha256:" + "0" * 64
        path.write_text(
            "\n".join(json.dumps(obj, sort_keys=True) for obj in raw_lines) + "\n",
            encoding="utf-8",
        )
        idx = ledger.LedgerIndex.build(tmp_path)
        assert idx.chain_verification is not None
        assert idx.chain_verification.ok is False
        records = [
            {"role": "human", "text": "first", "session": "sA", "prompt_id": "p1"},
            {"role": "human", "text": "second", "session": "sA", "prompt_id": "p2"},
        ]
        verdicts = verify.verify_transcript(records, idx)
        # The genesis prev mismatch on seq 1 is reported as first_break_seq == 1
        # itself (not seq 2): both lines are at or after that seq.
        assert all(v.verdict.startswith("chain-broken-at-seq-") for v in verdicts)
        assert verify.is_chain_broken_verdict(verdicts[0].verdict)


class TestAllHumanTurnsWitnessed:
    def test_true_when_every_human_turn_witnessed(self) -> None:
        verdicts = [
            verify.TurnVerdict(index=0, is_human=False, verdict=verify.VERDICT_MACHINE_AUTHORED),
            verify.TurnVerdict(index=1, is_human=True, verdict=verify.VERDICT_WITNESSED),
        ]
        assert verify.all_human_turns_witnessed(verdicts) is True

    def test_false_when_any_human_turn_is_not_witnessed(self) -> None:
        verdicts = [
            verify.TurnVerdict(
                index=0, is_human=True, verdict=verify.VERDICT_NOT_WITNESSED_INSERTED
            ),
        ]
        assert verify.all_human_turns_witnessed(verdicts) is False

    def test_vacuously_true_with_no_human_turns(self) -> None:
        verdicts = [
            verify.TurnVerdict(index=0, is_human=False, verdict=verify.VERDICT_MACHINE_AUTHORED),
        ]
        assert verify.all_human_turns_witnessed(verdicts) is True


class TestVerdictLabel:
    @pytest.mark.parametrize(
        ("verdict_str", "label"),
        [
            (verify.VERDICT_WITNESSED, "witnessed"),
            (verify.VERDICT_NOT_WITNESSED_INSERTED, "not witnessed (inserted)"),
            (verify.VERDICT_DIGEST_MISMATCH_EDITED, "digest mismatch (edited)"),
            (verify.VERDICT_MACHINE_AUTHORED, "machine-authored (task-notification)"),
            (verify.VERDICT_HEADLESS, "headless"),
            (verify.VERDICT_BRIDGE, "bridge"),
        ],
    )
    def test_known_verdicts(self, verdict_str: str, label: str) -> None:
        assert verify.verdict_label(verdict_str) == label

    def test_chain_broken_renders_the_seq(self) -> None:
        assert verify.verdict_label(verify.chain_broken_verdict(7)) == "chain broken at seq 7"


class TestLoadTranscriptRecords:
    def test_loads_a_valid_array(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        path.write_text(
            json.dumps(
                [{"role": "human", "text": "hi", "session": "s1", "prompt_id": "p1"}]
            ),
            encoding="utf-8",
        )
        records = verify.load_transcript_records(path)
        assert records == [{"role": "human", "text": "hi", "session": "s1", "prompt_id": "p1"}]

    def test_missing_optional_fields_default(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        path.write_text(json.dumps([{"text": "hi"}]), encoding="utf-8")
        records = verify.load_transcript_records(path)
        assert records == [{"role": "human", "text": "hi", "session": None, "prompt_id": None}]

    def test_rejects_a_non_array_top_level(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        path.write_text(json.dumps({"role": "human"}), encoding="utf-8")
        with pytest.raises(ValueError, match="top-level JSON array"):
            verify.load_transcript_records(path)

    def test_rejects_a_non_object_element(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        path.write_text(json.dumps(["not an object"]), encoding="utf-8")
        with pytest.raises(ValueError, match="JSON object"):
            verify.load_transcript_records(path)
