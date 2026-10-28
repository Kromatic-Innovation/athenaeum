# SPDX-License-Identifier: Apache-2.0
"""Tests for promptwitness.verify (athenaeum#2155).

Every path builds filesystem paths from ``tmp_path`` only -- never a
hardcoded machine-specific home-directory literal (the public-safe-lint
gate rejects that shape even in test fixtures).
"""

from __future__ import annotations

import json
from collections.abc import Callable
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


class TestVerifyTranscriptPromptKind:
    """athenaeum#2175 review finding #2: ``LedgerIndex.by_prompt_id`` /
    ``by_session_digest`` index every ``kind == "turn"`` line regardless of
    ``prompt_kind``, so a ``role="human"`` transcript record that happens to
    match a task-notification/subagent/launch-argument line's identity and
    digest must NOT verify as witnessed -- that line was never a human turn
    in the first place, whatever the transcript claims about it."""

    @pytest.mark.parametrize("kind", ["task-notification", "subagent", "launch-argument"])
    def test_rejected_prompt_kind_is_not_witnessed(self, tmp_path: Path, kind: str) -> None:
        _append_turn(
            tmp_path,
            prompt_id="p1",
            session="sA",
            text="hi",
            channel="interactive",
            prompt_kind=kind,
        )
        idx = ledger.LedgerIndex.build(tmp_path)
        records = [{"role": "human", "text": "hi", "session": "sA", "prompt_id": "p1"}]
        verdicts = verify.verify_transcript(records, idx)
        assert verdicts[0].verdict == verify.VERDICT_MACHINE_AUTHORED
        assert verdicts[0].is_human is True
        assert verdicts[0].detail == kind

    def test_rejected_prompt_kind_fails_all_human_turns_witnessed(self, tmp_path: Path) -> None:
        _append_turn(
            tmp_path,
            prompt_id="p1",
            session="sA",
            text="hi",
            channel="interactive",
            prompt_kind="task-notification",
        )
        idx = ledger.LedgerIndex.build(tmp_path)
        records = [{"role": "human", "text": "hi", "session": "sA", "prompt_id": "p1"}]
        verdicts = verify.verify_transcript(records, idx)
        assert verify.all_human_turns_witnessed(verdicts) is False

    def test_human_and_slash_command_kinds_are_unaffected(self, tmp_path: Path) -> None:
        _append_turn(
            tmp_path,
            prompt_id="p1",
            session="sA",
            text="hi",
            channel="interactive",
            prompt_kind="slash-command",
        )
        idx = ledger.LedgerIndex.build(tmp_path)
        records = [{"role": "human", "text": "hi", "session": "sA", "prompt_id": "p1"}]
        verdicts = verify.verify_transcript(records, idx)
        assert verdicts[0].verdict == verify.VERDICT_WITNESSED


def _rewrite_only_partition(tmp_path: Path, mutate: Callable[[list[dict]], None]) -> None:
    """Load the single ``*.jsonl`` partition under *tmp_path*, apply
    *mutate* to the list of parsed raw line dicts in place, and write it
    back -- the shared plumbing both chain-break attack tests use to tamper
    with an already-appended ledger line."""
    partitions = sorted(tmp_path.glob("*.jsonl"))
    assert len(partitions) == 1
    path = partitions[0]
    raw_lines = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
    ]
    mutate(raw_lines)
    path.write_text(
        "\n".join(json.dumps(obj, sort_keys=True) for obj in raw_lines) + "\n",
        encoding="utf-8",
    )


class TestVerifyTranscriptChainBroken:
    def test_genesis_prev_mismatch_flags_both_lines(self, tmp_path: Path) -> None:
        """A mutation of the very FIRST line's own ``prev`` (the genesis
        marker) has no "physically preceding line" to separately suspect --
        ``first_break_seq`` already covers it via ``seq >= first_break_seq``,
        since the break is reported at that same first line."""
        _append_turn(tmp_path, prompt_id="p1", session="sA", text="first", turn=1)
        _append_turn(tmp_path, prompt_id="p2", session="sA", text="second", turn=2)

        def mutate(raw_lines: list[dict]) -> None:
            assert raw_lines[0]["seq"] == 1
            raw_lines[0]["prev"] = "sha256:" + "0" * 64

        _rewrite_only_partition(tmp_path, mutate)

        idx = ledger.LedgerIndex.build(tmp_path)
        assert idx.chain_verification is not None
        assert idx.chain_verification.ok is False
        assert idx.chain_verification.first_break_prev_seq is None
        records = [
            {"role": "human", "text": "first", "session": "sA", "prompt_id": "p1"},
            {"role": "human", "text": "second", "session": "sA", "prompt_id": "p2"},
        ]
        verdicts = verify.verify_transcript(records, idx)
        assert all(v.verdict.startswith("chain-broken-at-seq-") for v in verdicts)
        assert verify.is_chain_broken_verdict(verdicts[0].verdict)

    def test_content_mutation_of_the_tampered_line_itself_is_not_witnessed(
        self, tmp_path: Path
    ) -> None:
        """Reproduces athenaeum#2175 review finding #1's concrete bypass: a
        forger flips the MIDDLE line's own `channel` from `headless` to
        `interactive` (turning a headless injection into an apparently-human
        turn) without touching `prev` at all -- `prompt_digest` is
        untouched, so a naive digest check alone would pass it.

        Per `ChainVerification`'s own docstring, a content mutation of line
        N surfaces as a break reported at N+1, not at N itself -- so a
        verifier that only flagged `seq >= first_break_seq` would miss
        exactly the line that was actually forged. `verify_transcript` must
        flag it anyway, via `first_break_prev_seq`, while leaving the
        untouched FIRST line witnessed.
        """
        _append_turn(tmp_path, prompt_id="p1", session="sA", text="first", turn=1)
        _append_turn(
            tmp_path, prompt_id="p2", session="sA", text="second", turn=2, channel="headless"
        )
        _append_turn(tmp_path, prompt_id="p3", session="sA", text="third", turn=3)

        def mutate(raw_lines: list[dict]) -> None:
            middle = next(line for line in raw_lines if line["seq"] == 2)
            assert middle["channel"] == "headless"
            middle["channel"] = "interactive"  # the forged edit under test

        _rewrite_only_partition(tmp_path, mutate)

        idx = ledger.LedgerIndex.build(tmp_path)
        assert idx.chain_verification is not None
        assert idx.chain_verification.ok is False
        # The break is detected on the line AFTER the one that was actually
        # edited (seq 2 was mutated; the mismatch is reported at seq 3's own
        # `prev`) -- the exact "break at N+1" shape the review finding cites.
        assert idx.chain_verification.first_break_seq != 2
        assert idx.chain_verification.first_break_prev_seq == 2

        records = [
            {"role": "human", "text": "first", "session": "sA", "prompt_id": "p1"},
            {"role": "human", "text": "second", "session": "sA", "prompt_id": "p2"},
            {"role": "human", "text": "third", "session": "sA", "prompt_id": "p3"},
        ]
        verdicts = verify.verify_transcript(records, idx)
        # seq 1 (untouched, strictly before the break and its predecessor):
        # still genuinely witnessed.
        assert verdicts[0].verdict == verify.VERDICT_WITNESSED
        # seq 2 (the actually-tampered line): must NOT verify as witnessed --
        # this is the bug. A vacuous assertion here (e.g. "verdict != witnessed
        # OR line.seq happens to be >= first_break_seq") would not catch a
        # regression back to the old `>=`-only check; this asserts the
        # positive fail-safe outcome directly.
        assert verdicts[1].verdict != verify.VERDICT_WITNESSED
        assert verify.is_chain_broken_verdict(verdicts[1].verdict)
        # seq 3 (downstream of the break): also flagged, correctly.
        assert verdicts[2].verdict != verify.VERDICT_WITNESSED


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

    def test_detail_overrides_the_machine_authored_parenthetical(self) -> None:
        assert (
            verify.verdict_label(verify.VERDICT_MACHINE_AUTHORED, "subagent")
            == "machine-authored (subagent)"
        )

    def test_empty_detail_keeps_the_default_machine_authored_label(self) -> None:
        assert (
            verify.verdict_label(verify.VERDICT_MACHINE_AUTHORED, "")
            == "machine-authored (task-notification)"
        )

    def test_detail_is_ignored_for_other_verdicts(self) -> None:
        assert verify.verdict_label(verify.VERDICT_WITNESSED, "subagent") == "witnessed"


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
