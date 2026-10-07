# SPDX-License-Identifier: Apache-2.0
"""A Jev-routed proposal is human-review-only regardless of confidence —
checked BEFORE the per-action threshold and before the correct_*/forget_*
authorship short-circuit (issue athenaeum#1997).

Mirrors ``tests/test_auto_apply_threshold.py``'s ``tier4_escalate``
integration pattern, with ``jev_routed=True`` on each proposal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.models import EscalationItem
from athenaeum.resolutions import ResolutionProposal
from athenaeum.tiers import tier4_escalate


def _jev_proposal(action: str, confidence: float) -> ResolutionProposal:
    return ResolutionProposal(
        recommended_winner="a" if action in ("keep_a", "correct_a", "forget_b") else "b",
        action=action,  # type: ignore[arg-type]
        rationale=f"jev: chose `{action}`",
        confidence=confidence,
        source_precedence_used=[],
        jev_routed=True,
    )


def _escalation(
    name: str, proposal: ResolutionProposal, members: list[str] | None = None
) -> EscalationItem:
    return EscalationItem(
        raw_ref=f"wiki/{name.lower()}.md",
        entity_name=name,
        conflict_type="factual",
        description=f"conflict for {name}",
        proposal=proposal,
        members=members,
    )


@pytest.mark.parametrize(
    "action",
    ["keep_a", "keep_b", "not_a_conflict", "forget_a", "forget_b", "correct_a", "correct_b"],
)
def test_jev_routed_never_auto_applies_at_confidence_1_0(action: str, tmp_path: Path) -> None:
    """At confidence 1.0 -- which would clear every per-action threshold,
    including the 0.95 destructive bar -- a jev_routed proposal still stays
    open for human review."""
    pending = tmp_path / "_pending_questions.md"
    member_a = tmp_path / "member_a.md"
    member_b = tmp_path / "member_b.md"
    member_a.write_text("a-claim", encoding="utf-8")
    member_b.write_text("b-claim", encoding="utf-8")
    cfg = {"resolve": {"auto_apply": True}}

    item = _escalation(
        f"Jev{action}Entity",
        _jev_proposal(action, 1.0),
        members=[str(member_a), str(member_b)],
    )
    tier4_escalate([item], pending, config=cfg)
    text = pending.read_text(encoding="utf-8")

    assert "- [ ]" in text
    assert "**Auto-resolved**" not in text
    # Enacting actions (forget_*/correct_*) must not delete either member.
    assert member_a.exists()
    assert member_b.exists()


def test_non_jev_proposal_at_same_confidence_does_auto_apply(tmp_path: Path) -> None:
    """Sanity control: the SAME action/confidence WITHOUT jev_routed does
    auto-apply -- proves the gate added for athenaeum#1997 is what's blocking
    the jev_routed cases above, not some unrelated fixture problem."""
    pending = tmp_path / "_pending_questions.md"
    cfg = {"resolve": {"auto_apply": True}}
    proposal = ResolutionProposal(
        recommended_winner="a",
        action="keep_a",
        rationale="text-model chose keep_a",
        confidence=1.0,
        source_precedence_used=[],
        jev_routed=False,
    )
    item = _escalation("TextModelEntity", proposal)
    tier4_escalate([item], pending, config=cfg)
    text = pending.read_text(encoding="utf-8")

    assert "- [x]" in text
    assert "**Auto-resolved**: true" in text
