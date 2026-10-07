# SPDX-License-Identifier: Apache-2.0
"""Tests for the Jev decision-backend wiring in :mod:`athenaeum.resolutions`
and the human-review-only auto-apply gate in :mod:`athenaeum.tiers` (issue
athenaeum#1997).

No network calls: the fake Jev backend below implements
:class:`~athenaeum.decision_provider.DecisionBackend` directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.decision_provider import DecisionResult
from athenaeum.models import AutoMemoryFile
from athenaeum.resolutions import (
    PROPOSE_MERGE_ACTION,
    MergeProposal,
    ResolutionProposal,
    _jev_criteria,
    propose_resolution,
)


class _FakeJev:
    """A fake :class:`DecisionBackend` recording its last `decide()` call."""

    def __init__(self, result: DecisionResult) -> None:
        self.result = result
        self.last_criteria: object = None
        self.calls = 0

    def decide(self, *, question_id, kind, instructions, state, criteria=None):
        self.calls += 1
        self.last_criteria = criteria
        return self.result


def _write_am(
    scope_dir: Path, filename: str, body: str, *, source: str | None = None
) -> AutoMemoryFile:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    fm_lines = ["---", "name: probe", "type: feedback"]
    if source is not None:
        fm_lines.append(f"source: {source}")
    fm_lines.append("---")
    path.write_text("\n".join(fm_lines) + "\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(path=path, origin_scope="scope-x", memory_type="feedback", name="probe")


def _detected(members: list[AutoMemoryFile]) -> ContradictionResult:
    return ContradictionResult(
        detected=True,
        conflict_type="factual",
        members_involved=[f"{m.origin_scope}/{m.path.name}" for m in members[:2]],
        conflicting_passages=["Member A passage.", "Member B passage."],
        rationale="test conflict",
    )


def test_jev_criteria_never_includes_propose_merge() -> None:
    assert PROPOSE_MERGE_ACTION not in _jev_criteria()


def test_jev_routed_choice_becomes_resolution_proposal(tmp_path: Path) -> None:
    a = _write_am(tmp_path / "a", "a.md", "Alice is in Berlin.", source="user")
    b = _write_am(tmp_path / "b", "b.md", "Alice is in Munich.", source="user")
    members = [a, b]
    fake = _FakeJev(
        DecisionResult(choice="keep_a", probability=0.91, probabilities={"keep_a": 0.91})
    )

    proposal = propose_resolution(_detected(members), members, client=None, decision_backend=fake)

    assert isinstance(proposal, ResolutionProposal)
    assert proposal.action == "keep_a"
    assert proposal.recommended_winner == "a"
    assert proposal.jev_routed is True
    assert proposal.confidence == pytest.approx(0.91)
    assert fake.calls == 1
    assert PROPOSE_MERGE_ACTION not in fake.last_criteria


def test_jev_choice_out_of_set_falls_back(tmp_path: Path) -> None:
    a = _write_am(tmp_path / "a", "a.md", "Alice is in Berlin.", source="user")
    b = _write_am(tmp_path / "b", "b.md", "Alice is in Munich.", source="user")
    members = [a, b]
    fake = _FakeJev(DecisionResult(choice=PROPOSE_MERGE_ACTION, probability=1.0))
    # Jev returning something outside its own offered criteria must fall
    # back, never become a MergeProposal.
    result = propose_resolution(_detected(members), members, client=None, decision_backend=fake)
    assert isinstance(result, ResolutionProposal)
    assert not isinstance(result, MergeProposal)
    assert result.confidence == 0.0
    assert result.jev_routed is False  # the deterministic fallback, not a Jev proposal


@pytest.mark.parametrize(
    ("action", "expected_winner"),
    [
        ("keep_a", "a"),
        ("keep_b", "b"),
        ("correct_a", "a"),
        ("correct_b", "b"),
        ("forget_a", "b"),
        ("forget_b", "a"),
        ("merge", "merge"),
        ("not_a_conflict", "neither"),
        ("deprecate_both", "neither"),
        ("attribute_both", "neither"),
    ],
)
def test_jev_action_winner_mapping(tmp_path: Path, action: str, expected_winner: str) -> None:
    a = _write_am(tmp_path / "a", "a.md", "Alice is in Berlin.", source="user")
    b = _write_am(tmp_path / "b", "b.md", "Alice is in Munich.", source="user")
    members = [a, b]
    fake = _FakeJev(DecisionResult(choice=action, probability=0.8))
    proposal = propose_resolution(_detected(members), members, client=None, decision_backend=fake)
    assert isinstance(proposal, ResolutionProposal)
    assert proposal.action == action
    assert proposal.recommended_winner == expected_winner
    assert proposal.jev_routed is True
