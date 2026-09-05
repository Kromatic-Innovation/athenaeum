# SPDX-License-Identifier: Apache-2.0
"""Tests for the correct_a/correct_b transcript-authorship gate (issue athenaeum#752).

`enact_resolution` DELETES the losing raw-memory member for `correct_a` /
`correct_b`. Pre-athenaeum#752 the only barrier was a confidence float. This
gates the destructive auto-apply on whether the WINNING claim traces to a
genuine human utterance in the origin-session transcript — a record the
model did not author — verified at ENACT time via
:func:`athenaeum.transcript_verify.classify_backfill_claim`.

Covers:

* ``resolutions._transcript_authorizes_correct`` (unit) — resolves the
  winning member from ``recommended_winner``, reads its origin fields, and
  classifies its claim against the transcript.
* ``tiers.tier4_escalate`` / ``tiers.reresolve_open_questions`` (integration)
  — the full auto-apply -> enact path is gated end-to-end.

AC mapping (see issue athenaeum#752):

1. auto-apply only on ``user-stated`` for the winning member.
2. re-derives from transcript, NEVER reads stored ``source_type``.
3. ``agent-observed`` / ``inferred`` / ``unavailable`` each escalate.
4. missing/rolled-off transcript (``unavailable``) escalates.
5. threshold not consulted for correct_* (see docs/reference/configuration.md).
6. ``forget_a``/``forget_b`` unchanged — no authorship check.
7. gate decision (channel + ref) logged for every correct_* verdict.
8. docs/design/conflict-resolution.md records the rule + known limits.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.models import EscalationItem
from athenaeum.resolutions import ResolutionProposal, _transcript_authorizes_correct
from athenaeum.tiers import reresolve_open_questions, tier4_escalate

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _proposal(action: str, winner: str, confidence: float = 0.95) -> ResolutionProposal:
    return ResolutionProposal(
        recommended_winner=winner,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        rationale=f"test-{action}",
        confidence=confidence,
        source_precedence_used=["a:user > b:unsourced"],
    )


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


def _tool_result_record(text: str) -> dict[str, object]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}],
        },
    }


def _item(
    name: str, proposal: ResolutionProposal, members: list[str], a_ref: str, b_ref: str
) -> EscalationItem:
    description = (
        "rationale line\n"
        "Passage 1: A says X.\n"
        "Passage 2: B says Y.\n"
        f"Members involved: {a_ref}, {b_ref}"
    )
    return EscalationItem(
        raw_ref=f"wiki/{name.lower()}.md",
        entity_name=name,
        conflict_type="factual",
        description=description,
        proposal=proposal,
        members=members,
    )


# ---------------------------------------------------------------------------
# Unit: _transcript_authorizes_correct
# ---------------------------------------------------------------------------


class TestTranscriptAuthorizesCorrectUnit:
    def test_ac1_user_stated_authorizes(self, tmp_path: Path) -> None:
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(scope_dir, "a.md", body="the winning claim")
        b = _write_member(scope_dir, "b.md", body="the losing claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])

        authorized, channel_ref = _transcript_authorizes_correct(
            _proposal("correct_a", "a"), [a, b], projects_root=pr
        )
        assert authorized is True
        assert channel_ref.startswith("user-stated")

    def test_ac2_frontmatter_source_type_never_trusted(self, tmp_path: Path) -> None:
        # A member carries source_type: user-stated in frontmatter but its
        # transcript has NO supporting user turn — the gate must re-derive
        # from the transcript and REFUSE, never reading the stored field.
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(
            scope_dir,
            "a.md",
            body="the winning claim",
            source_type="user-stated",
        )
        b = _write_member(scope_dir, "b.md", body="the losing claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        # Transcript exists but does NOT contain the claim as a user turn.
        _write_transcript(pr, "scopeA", "sess1", [_user_record("completely unrelated text")])

        authorized, channel_ref = _transcript_authorizes_correct(
            _proposal("correct_a", "a"), [a, b], projects_root=pr
        )
        assert authorized is False
        assert channel_ref.startswith("inferred")

    def test_ac3_agent_observed_escalates(self, tmp_path: Path) -> None:
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(scope_dir, "a.md", body="the winning claim")
        b = _write_member(scope_dir, "b.md", body="the losing claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "sess1", [_tool_result_record("the winning claim")])

        authorized, channel_ref = _transcript_authorizes_correct(
            _proposal("correct_a", "a"), [a, b], projects_root=pr
        )
        assert authorized is False
        assert channel_ref.startswith("agent-observed")

    def test_ac3_inferred_escalates(self, tmp_path: Path) -> None:
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(scope_dir, "a.md", body="the winning claim")
        b = _write_member(scope_dir, "b.md", body="the losing claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "sess1", [_user_record("nothing related at all")])

        authorized, channel_ref = _transcript_authorizes_correct(
            _proposal("correct_a", "a"), [a, b], projects_root=pr
        )
        assert authorized is False
        assert channel_ref.startswith("inferred")

    def test_ac4_unavailable_missing_transcript_escalates(self, tmp_path: Path) -> None:
        # Most likely production path: the transcript rolled off / was never
        # captured. Must escalate, never delete.
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(scope_dir, "a.md", body="the winning claim")
        b = _write_member(scope_dir, "b.md", body="the losing claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        pr.mkdir()  # projects_root exists, but no scopeA/sess1.jsonl.

        authorized, channel_ref = _transcript_authorizes_correct(
            _proposal("correct_a", "a"), [a, b], projects_root=pr
        )
        assert authorized is False
        assert channel_ref.startswith("unavailable")

    def test_correct_b_resolves_winner_as_side_b(self, tmp_path: Path) -> None:
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(scope_dir, "a.md", body="the losing claim", session_id=None, turn=None)
        b = _write_member(scope_dir, "b.md", body="the winning claim")
        pr = tmp_path / "projects"
        _write_transcript(pr, "scopeA", "sess1", [_user_record("the winning claim")])

        authorized, channel_ref = _transcript_authorizes_correct(
            _proposal("correct_b", "b"), [a, b], projects_root=pr
        )
        assert authorized is True
        assert channel_ref.startswith("user-stated")

    def test_no_origin_session_refuses(self, tmp_path: Path) -> None:
        scope_dir = tmp_path / "raw" / "scopeA"
        a = _write_member(scope_dir, "a.md", body="claim", session_id=None, turn=None)
        b = _write_member(scope_dir, "b.md", body="claim2", session_id=None, turn=None)
        authorized, channel_ref = _transcript_authorizes_correct(
            _proposal("correct_a", "a"), [a, b], projects_root=tmp_path / "projects"
        )
        assert authorized is False
        assert "no origin session" in channel_ref


# ---------------------------------------------------------------------------
# Integration: tier4_escalate end-to-end
# ---------------------------------------------------------------------------


class TestTier4CorrectGateIntegration:
    def test_correct_a_user_stated_deletes_wrong_side(self, tmp_path: Path) -> None:
        scope = tmp_path / "raw" / "scope"
        a = _write_member(scope, "right.md", body="the correct claim")
        b = _write_member(scope, "wrong.md", body="the wrong claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        _write_transcript(pr, "scope", "sess1", [_user_record("the correct claim")])
        pending = tmp_path / "_pending_questions.md"

        item = _item(
            "CorrectEntity",
            _proposal("correct_a", "a", confidence=0.60),  # low confidence — irrelevant now.
            members=[str(a), str(b)],
            a_ref="scope/right.md",
            b_ref="scope/wrong.md",
        )
        tier4_escalate(
            [item], pending, config={"resolve": {"auto_apply": True}}, projects_root=pr
        )

        assert a.exists()
        assert not b.exists()
        assert "**Auto-resolved**: true" in pending.read_text(encoding="utf-8")

    def test_correct_a_unavailable_transcript_refuses_and_escalates(
        self, tmp_path: Path
    ) -> None:
        scope = tmp_path / "raw" / "scope"
        a = _write_member(scope, "right.md", body="the correct claim")
        b = _write_member(scope, "wrong.md", body="the wrong claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        pr.mkdir()  # No transcript at all.
        pending = tmp_path / "_pending_questions.md"

        item = _item(
            "CorrectEntity",
            _proposal("correct_a", "a", confidence=0.99),  # high confidence — irrelevant now.
            members=[str(a), str(b)],
            a_ref="scope/right.md",
            b_ref="scope/wrong.md",
        )
        tier4_escalate(
            [item], pending, config={"resolve": {"auto_apply": True}}, projects_root=pr
        )

        # NOT enacted — both members survive, block left open for the human.
        assert a.exists()
        assert b.exists()
        text = pending.read_text(encoding="utf-8")
        assert "- [ ]" in text
        assert "**Auto-resolved**: true" not in text

    def test_ac5_threshold_not_consulted_for_correct_even_at_zero_confidence(
        self, tmp_path: Path
    ) -> None:
        # A near-zero confidence correct_a still auto-applies when the
        # transcript authorizes it — proving the confidence bar plays NO
        # role for correct_* (the authorship gate is the only gate).
        scope = tmp_path / "raw" / "scope"
        a = _write_member(scope, "right.md", body="the correct claim")
        b = _write_member(scope, "wrong.md", body="the wrong claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        _write_transcript(pr, "scope", "sess1", [_user_record("the correct claim")])
        pending = tmp_path / "_pending_questions.md"

        item = _item(
            "CorrectEntity",
            _proposal("correct_a", "a", confidence=0.01),
            members=[str(a), str(b)],
            a_ref="scope/right.md",
            b_ref="scope/wrong.md",
        )
        tier4_escalate(
            [item], pending, config={"resolve": {"auto_apply": True}}, projects_root=pr
        )

        assert a.exists()
        assert not b.exists()

    def test_ac6_forget_a_still_enacts_on_confidence_floor_no_authorship_check(
        self, tmp_path: Path
    ) -> None:
        # forget_* is explicitly OUT of scope — must keep enacting purely on
        # its configured confidence floor, with no transcript required at all.
        scope = tmp_path / "raw" / "scope"
        a = _write_member(scope, "transient.md", body="junk", session_id=None, turn=None)
        b = _write_member(scope, "keeper.md", body="keep", session_id=None, turn=None)
        pending = tmp_path / "_pending_questions.md"
        # No projects_root/transcript at all — must not matter for forget_*.

        item = _item(
            "ForgetEntity",
            _proposal("forget_a", "b", confidence=0.99),
            members=[str(a), str(b)],
            a_ref="scope/transient.md",
            b_ref="scope/keeper.md",
        )
        tier4_escalate([item], pending, config={"resolve": {"auto_apply": True}})

        assert not a.exists()
        assert b.exists()

    def test_ac7_gate_decision_logged_on_permit(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        scope = tmp_path / "raw" / "scope"
        a = _write_member(scope, "right.md", body="the correct claim")
        b = _write_member(scope, "wrong.md", body="the wrong claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        _write_transcript(pr, "scope", "sess1", [_user_record("the correct claim")])
        pending = tmp_path / "_pending_questions.md"

        item = _item(
            "CorrectEntity",
            _proposal("correct_a", "a", confidence=0.95),
            members=[str(a), str(b)],
            a_ref="scope/right.md",
            b_ref="scope/wrong.md",
        )
        with caplog.at_level(logging.INFO, logger="athenaeum.tiers"):
            tier4_escalate(
                [item], pending, config={"resolve": {"auto_apply": True}}, projects_root=pr
            )
        assert any(
            "authorship gate: PERMIT correct_a" in r.message for r in caplog.records
        )

    def test_ac7_gate_decision_logged_on_refuse(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        scope = tmp_path / "raw" / "scope"
        a = _write_member(scope, "right.md", body="the correct claim")
        b = _write_member(scope, "wrong.md", body="the wrong claim", session_id=None, turn=None)
        pr = tmp_path / "projects"
        pr.mkdir()
        pending = tmp_path / "_pending_questions.md"

        item = _item(
            "CorrectEntity",
            _proposal("correct_a", "a", confidence=0.99),
            members=[str(a), str(b)],
            a_ref="scope/right.md",
            b_ref="scope/wrong.md",
        )
        with caplog.at_level(logging.INFO, logger="athenaeum.tiers"):
            tier4_escalate(
                [item], pending, config={"resolve": {"auto_apply": True}}, projects_root=pr
            )
        assert any(
            "authorship gate: REFUSE correct_a" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Integration: reresolve_open_questions (the heal-pass enactment site)
# ---------------------------------------------------------------------------


def _fake_client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _payload(action: str, *, winner: str = "a", confidence: float = 0.95) -> str:
    return (
        f'{{"recommended_winner": "{winner}", "action": "{action}", '
        f'"confidence": {confidence}, '
        '"rationale": "test verdict rationale.", '
        '"source_precedence_used": ["a:user > b:unsourced"]}'
    )


class TestReresolveCorrectGateIntegration:
    def test_correct_a_unavailable_transcript_refuses_in_heal_pass(
        self, tmp_path: Path
    ) -> None:
        # The heal pass (reresolve_open_questions) is the 4th enactment site
        # for correct_*/forget_* — it must apply the SAME authorship gate as
        # tier4_escalate, not just record + blindly enact.
        knowledge_root = tmp_path
        scope_dir = knowledge_root / "raw" / "auto-memory" / "scope-x"
        scope_dir.mkdir(parents=True)
        a = scope_dir / "feedback_a.md"
        a.write_text(
            "---\nname: feedback_a\ntype: feedback\noriginSessionId: sess1\n"
            "originTurn: 1\n---\nTristan is German.\n",
            encoding="utf-8",
        )
        b = scope_dir / "feedback_b.md"
        b.write_text(
            "---\nname: feedback_b\ntype: feedback\n---\nTristan is NOT German.\n",
            encoding="utf-8",
        )
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        pending = wiki / "_pending_questions.md"
        description = (
            "Detector says these conflict.\n"
            "Passage 1: Tristan is German.\n"
            "Passage 2: Tristan is NOT German.\n"
            "Members involved: scope-x/feedback_a.md, scope-x/feedback_b.md"
        )
        tier4_escalate(
            [
                EscalationItem(
                    raw_ref="wiki/auto-tristan.md",
                    entity_name="Tristan",
                    conflict_type="factual",
                    description=description,
                )
            ],
            pending,
        )

        # No transcript at all under projects_root — correct_a must refuse
        # even at high confidence, and must NOT delete member b.
        client = _fake_client(_payload("correct_a", winner="a", confidence=0.99))
        pr = tmp_path / "projects"
        pr.mkdir()
        reresolve_open_questions(
            pending, client=client, config={}, projects_root=pr
        )

        assert a.exists()
        assert b.exists()

    def test_correct_a_user_stated_enacts_in_heal_pass(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path
        scope_dir = knowledge_root / "raw" / "auto-memory" / "scope-x"
        scope_dir.mkdir(parents=True)
        a = scope_dir / "feedback_a.md"
        a.write_text(
            "---\nname: feedback_a\ntype: feedback\noriginSessionId: sess1\n"
            "originTurn: 1\n---\nTristan is German.\n",
            encoding="utf-8",
        )
        b = scope_dir / "feedback_b.md"
        b.write_text(
            "---\nname: feedback_b\ntype: feedback\n---\nTristan is NOT German.\n",
            encoding="utf-8",
        )
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        pending = wiki / "_pending_questions.md"
        description = (
            "Detector says these conflict.\n"
            "Passage 1: Tristan is German.\n"
            "Passage 2: Tristan is NOT German.\n"
            "Members involved: scope-x/feedback_a.md, scope-x/feedback_b.md"
        )
        tier4_escalate(
            [
                EscalationItem(
                    raw_ref="wiki/auto-tristan.md",
                    entity_name="Tristan",
                    conflict_type="factual",
                    description=description,
                )
            ],
            pending,
        )

        pr = tmp_path / "projects"
        _write_transcript(pr, "scope-x", "sess1", [_user_record("Tristan is German.")])
        client = _fake_client(_payload("correct_a", winner="a", confidence=0.05))
        reresolve_open_questions(
            pending, client=client, config={}, projects_root=pr
        )

        assert a.exists()
        assert not b.exists()
