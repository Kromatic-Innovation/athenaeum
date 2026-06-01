# SPDX-License-Identifier: Apache-2.0
"""Tests for the disambiguation question mode (#166 follow-up).

When the resolver hits a FACT/identity conflict it cannot confidently
resolve (and which is NOT two sequential dated snapshots), it returns the
candidate values on ``ResolutionProposal.disambiguation_options`` instead
of silently picking a precedence winner. ``tier4_escalate`` then renders
an enumerated question:

    Which is correct: (a) <A>, (b) <B>, (c) both, (d) neither/other?

These tests cover the ``_disambiguation_question`` helper directly and the
end-to-end rendering through ``tier4_escalate``. The canonical example is
the user's morning "I am German" / evening "I am English" pair.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.models import EscalationItem
from athenaeum.resolutions import ResolutionProposal
from athenaeum.tiers import _disambiguation_question, tier4_escalate

# ---------------------------------------------------------------------------
# _disambiguation_question helper
# ---------------------------------------------------------------------------


class TestDisambiguationQuestion:
    def test_two_options_enumerated_with_both_and_neither(self) -> None:
        q = _disambiguation_question(["German", "English"])
        assert q == (
            "Which is correct: (a) German, (b) English, " "(c) both, (d) neither/other?"
        )

    def test_single_option_returns_none(self) -> None:
        # A single candidate is not a disambiguation — caller falls back
        # to the free-text question.
        assert _disambiguation_question(["German"]) is None

    def test_empty_returns_none(self) -> None:
        assert _disambiguation_question([]) is None

    def test_blank_entries_dropped(self) -> None:
        # Whitespace-only entries are not real candidates; if fewer than
        # two real values survive, no question is produced.
        assert _disambiguation_question(["German", "   "]) is None

    def test_newlines_flattened_to_single_line(self) -> None:
        q = _disambiguation_question(["multi\nline\nvalue", "other"])
        assert q is not None
        assert "\n" not in q
        assert "multi line value" in q

    def test_three_options_shifts_tail_letters(self) -> None:
        q = _disambiguation_question(["X", "Y", "Z"])
        assert q == (
            "Which is correct: (a) X, (b) Y, (c) Z, " "(d) both, (e) neither/other?"
        )


# ---------------------------------------------------------------------------
# tier4_escalate end-to-end rendering
# ---------------------------------------------------------------------------


def _disambig_proposal(options: list[str]) -> ResolutionProposal:
    """A proposal carrying disambiguation options.

    Disambiguation rides on the ``retain_both_with_context`` action with
    winner ``neither`` — the resolver explicitly declined to pick a side.
    """
    return ResolutionProposal(
        recommended_winner="neither",
        action="retain_both_with_context",
        rationale="unresolvable identity conflict; enumerate candidates",
        confidence=0.4,
        source_precedence_used=[],
        disambiguation_options=options,
    )


class TestTier4Disambiguation:
    def test_enumerated_question_rendered(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        item = EscalationItem(
            raw_ref="wiki/identity.md",
            entity_name="Nationality",
            conflict_type="factual",
            description="Morning note says German; evening note says English.",
            proposal=_disambig_proposal(["German", "English"]),
        )
        tier4_escalate([item], pending, config={"resolve": {"auto_apply": True}})
        text = pending.read_text(encoding="utf-8")

        assert (
            "- [ ] Which is correct: (a) German, (b) English, "
            "(c) both, (d) neither/other?" in text
        )
        # retain_both_with_context never auto-applies — the disambiguation
        # MUST stay open for the human.
        assert "**Auto-resolved**" not in text

    def test_falls_back_to_free_text_without_options(self, tmp_path: Path) -> None:
        pending = tmp_path / "_pending_questions.md"
        item = EscalationItem(
            raw_ref="wiki/identity.md",
            entity_name="Nationality",
            conflict_type="factual",
            description="Some free-text rationale line.",
            proposal=_disambig_proposal([]),  # no options
        )
        tier4_escalate([item], pending, config={"resolve": {"auto_apply": True}})
        text = pending.read_text(encoding="utf-8")

        assert "- [ ] Some free-text rationale line." in text
        assert "Which is correct:" not in text

    def test_no_proposal_uses_free_text(self, tmp_path: Path) -> None:
        # Legacy escalation with no proposal at all renders the free-text
        # question — disambiguation is strictly opt-in.
        pending = tmp_path / "_pending_questions.md"
        item = EscalationItem(
            raw_ref="wiki/x.md",
            entity_name="X",
            conflict_type="factual",
            description="Just a description.",
        )
        tier4_escalate([item], pending)
        text = pending.read_text(encoding="utf-8")

        assert "- [ ] Just a description." in text
        assert "Which is correct:" not in text
