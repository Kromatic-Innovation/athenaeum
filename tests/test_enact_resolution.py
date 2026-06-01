# SPDX-License-Identifier: Apache-2.0
"""Enactment lane (#166 follow-up): forget_*/correct_* actually mutate state.

Until this lane the auto-apply path only RECORDED a verdict — it flipped the
pending-question checkbox to ``[x]`` and stamped an ``**Auto-resolved**: true``
marker, but never touched a wiki/raw memory file. These tests prove the gap is
closed:

* ``enact_resolution`` (unit) deletes the correct side for each enacting action
  and no-ops for everything else.
* ``tier4_escalate`` (integration) deletes the target member file when a
  high-confidence ``forget_*`` / ``correct_*`` auto-applies, leaves it on a
  below-threshold verdict, and never deletes for a non-enacting action
  (``keep_a``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.models import EscalationItem
from athenaeum.resolutions import (
    ENACTING_ACTIONS,
    MergeProposal,
    ResolutionProposal,
    enact_resolution,
)
from athenaeum.tiers import tier4_escalate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proposal(action: str, confidence: float = 0.95) -> ResolutionProposal:
    # recommended_winner mirrors the action's surviving side so the proposal
    # is internally coherent (not load-bearing for enactment, which keys on
    # action alone).
    winner = {
        "forget_a": "b",
        "forget_b": "a",
        "correct_a": "a",
        "correct_b": "b",
        "keep_a": "a",
    }.get(action, "neither")
    return ResolutionProposal(
        recommended_winner=winner,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        rationale=f"test-{action}",
        confidence=confidence,
        source_precedence_used=["a:user > b:unsourced"],
    )


def _make_member(scope: Path, filename: str, body: str = "claim") -> Path:
    scope.mkdir(parents=True, exist_ok=True)
    path = scope / filename
    path.write_text(body, encoding="utf-8")
    return path


def _desc_with_members(a_ref: str, b_ref: str) -> str:
    """Mimic merge.py's contradictions description (a/b order preserved)."""
    return (
        "rationale line\n"
        "Passage 1: A says X.\n"
        "Passage 2: B says Y.\n"
        f"Members involved: {a_ref}, {b_ref}"
    )


def _item(
    name: str,
    proposal: ResolutionProposal,
    members: list[str],
    a_ref: str,
    b_ref: str,
) -> EscalationItem:
    return EscalationItem(
        raw_ref=f"wiki/{name.lower()}.md",
        entity_name=name,
        conflict_type="factual",
        description=_desc_with_members(a_ref, b_ref),
        proposal=proposal,
        members=members,
    )


# ---------------------------------------------------------------------------
# enact_resolution — unit
# ---------------------------------------------------------------------------


class TestEnactResolutionUnit:
    def test_enacting_actions_set(self) -> None:
        assert ENACTING_ACTIONS == frozenset(
            {"forget_a", "forget_b", "correct_a", "correct_b"}
        )

    def test_forget_a_deletes_side_a(self, tmp_path: Path) -> None:
        a = _make_member(tmp_path, "a.md")
        b = _make_member(tmp_path, "b.md")
        deleted = enact_resolution(_proposal("forget_a"), [a, b])
        assert deleted == a
        assert not a.exists()
        assert b.exists()

    def test_forget_b_deletes_side_b(self, tmp_path: Path) -> None:
        a = _make_member(tmp_path, "a.md")
        b = _make_member(tmp_path, "b.md")
        deleted = enact_resolution(_proposal("forget_b"), [a, b])
        assert deleted == b
        assert not b.exists()
        assert a.exists()

    def test_correct_a_deletes_the_wrong_side_b(self, tmp_path: Path) -> None:
        # a is correct → b's wrong claim is removed.
        a = _make_member(tmp_path, "a.md")
        b = _make_member(tmp_path, "b.md")
        deleted = enact_resolution(_proposal("correct_a"), [a, b])
        assert deleted == b
        assert not b.exists()
        assert a.exists()

    def test_correct_b_deletes_the_wrong_side_a(self, tmp_path: Path) -> None:
        a = _make_member(tmp_path, "a.md")
        b = _make_member(tmp_path, "b.md")
        deleted = enact_resolution(_proposal("correct_b"), [a, b])
        assert deleted == a
        assert not a.exists()
        assert b.exists()

    def test_accepts_string_paths(self, tmp_path: Path) -> None:
        a = _make_member(tmp_path, "a.md")
        b = _make_member(tmp_path, "b.md")
        deleted = enact_resolution(_proposal("forget_a"), [str(a), str(b)])
        assert deleted == a
        assert not a.exists()

    @pytest.mark.parametrize(
        "action", ["keep_a", "keep_b", "deprecate_both", "not_a_conflict"]
    )
    def test_non_enacting_actions_are_noops(self, action: str, tmp_path: Path) -> None:
        a = _make_member(tmp_path, "a.md")
        b = _make_member(tmp_path, "b.md")
        assert enact_resolution(_proposal(action), [a, b]) is None
        assert a.exists()
        assert b.exists()

    def test_merge_proposal_is_noop(self, tmp_path: Path) -> None:
        a = _make_member(tmp_path, "a.md")
        b = _make_member(tmp_path, "b.md")
        mp = MergeProposal(
            merge_target_name="m",
            rationale="r",
            draft_merged_body="body",
            confidence=1.0,
        )
        assert enact_resolution(mp, [a, b]) is None
        assert a.exists() and b.exists()

    def test_missing_member_list_no_crash(self, tmp_path: Path) -> None:
        assert enact_resolution(_proposal("forget_a"), None) is None
        assert enact_resolution(_proposal("forget_a"), []) is None

    def test_short_member_list_for_side_b_no_crash(self, tmp_path: Path) -> None:
        a = _make_member(tmp_path, "a.md")
        # forget_b needs index 1 — only one path supplied → no-op, no crash.
        assert enact_resolution(_proposal("forget_b"), [a]) is None
        assert a.exists()

    def test_already_absent_target_tolerated(self, tmp_path: Path) -> None:
        a = tmp_path / "a.md"  # never created
        b = _make_member(tmp_path, "b.md")
        assert enact_resolution(_proposal("forget_a"), [a, b]) is None
        assert b.exists()


# ---------------------------------------------------------------------------
# tier4_escalate — integration (the real auto-apply → enact path)
# ---------------------------------------------------------------------------


class TestTier4EnactsOnAutoApply:
    def test_forget_a_high_confidence_deletes_member(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        a = _make_member(scope, "transient.md")
        b = _make_member(scope, "keeper.md")
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True}}  # defaults → 0.90 floor.

        item = _item(
            "ForgetEntity",
            _proposal("forget_a", 0.95),
            members=[str(a), str(b)],
            a_ref="scope/transient.md",
            b_ref="scope/keeper.md",
        )
        tier4_escalate([item], pending, config=cfg)

        text = pending.read_text(encoding="utf-8")
        assert "- [x]" in text  # recorded
        assert "**Auto-resolved**: true" in text
        # ENACTED: the transient side a is actually deleted.
        assert not a.exists()
        assert b.exists()

    def test_correct_a_high_confidence_removes_wrong_claim(
        self, tmp_path: Path
    ) -> None:
        scope = tmp_path / "scope"
        a = _make_member(scope, "right.md", "the correct claim")
        b = _make_member(scope, "wrong.md", "the wrong claim")
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True}}

        item = _item(
            "CorrectEntity",
            _proposal("correct_a", 0.95),
            members=[str(a), str(b)],
            a_ref="scope/right.md",
            b_ref="scope/wrong.md",
        )
        tier4_escalate([item], pending, config=cfg)

        # correct_a: a is right, b's wrong claim is removed.
        assert a.exists()
        assert not b.exists()

    def test_below_threshold_does_not_enact(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        a = _make_member(scope, "a.md")
        b = _make_member(scope, "b.md")
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True}}

        item = _item(
            "LowConf",
            _proposal("forget_a", 0.50),  # below 0.90 floor.
            members=[str(a), str(b)],
            a_ref="scope/a.md",
            b_ref="scope/b.md",
        )
        tier4_escalate([item], pending, config=cfg)

        text = pending.read_text(encoding="utf-8")
        assert "- [ ]" in text  # left for the human
        # NOT enacted — both members survive.
        assert a.exists()
        assert b.exists()

    def test_keep_a_never_deletes(self, tmp_path: Path) -> None:
        # keep_a auto-applies (records) but is NOT an enacting action — both
        # members must survive (loser stays as superseded history).
        scope = tmp_path / "scope"
        a = _make_member(scope, "a.md")
        b = _make_member(scope, "b.md")
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True}}

        item = _item(
            "KeepEntity",
            _proposal("keep_a", 0.99),
            members=[str(a), str(b)],
            a_ref="scope/a.md",
            b_ref="scope/b.md",
        )
        tier4_escalate([item], pending, config=cfg)

        text = pending.read_text(encoding="utf-8")
        assert "- [x]" in text  # recorded
        assert a.exists()
        assert b.exists()

    def test_auto_apply_disabled_does_not_enact(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        a = _make_member(scope, "a.md")
        b = _make_member(scope, "b.md")
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": False}}

        item = _item(
            "DisabledEntity",
            _proposal("forget_a", 0.99),
            members=[str(a), str(b)],
            a_ref="scope/a.md",
            b_ref="scope/b.md",
        )
        tier4_escalate([item], pending, config=cfg)

        assert a.exists()
        assert b.exists()

    def test_dedup_collapse_enacts_once_with_best_proposal(
        self, tmp_path: Path
    ) -> None:
        # Two items on the SAME source pair collapse into one block. The
        # high-confidence forget proposal must enact exactly once and delete
        # the right target even though the first item's proposal was low.
        scope = tmp_path / "scope"
        a = _make_member(scope, "a.md")
        b = _make_member(scope, "b.md")
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True}}

        low = _item(
            "Alpha",
            _proposal("forget_a", 0.50),
            members=[str(a), str(b)],
            a_ref="scope/a.md",
            b_ref="scope/b.md",
        )
        high = _item(
            "Beta",
            _proposal("forget_a", 0.97),
            members=[str(a), str(b)],
            a_ref="scope/a.md",
            b_ref="scope/b.md",
        )
        tier4_escalate([low, high], pending, config=cfg)

        text = pending.read_text(encoding="utf-8")
        assert text.count("## [") == 1  # collapsed
        assert "- [x]" in text
        # Enacted via the best (high-conf) proposal.
        assert not a.exists()
        assert b.exists()

    def test_cross_batch_open_block_enacts(self, tmp_path: Path) -> None:
        # First batch leaves an open [ ] block (low conf). Second batch with
        # the same pair + high-conf forget must enact (delete target) when it
        # flips the existing block to [x].
        scope = tmp_path / "scope"
        a = _make_member(scope, "a.md")
        b = _make_member(scope, "b.md")
        pending = tmp_path / "_pending_questions.md"
        cfg = {"resolve": {"auto_apply": True}}

        first = _item(
            "Alpha",
            _proposal("forget_a", 0.50),
            members=[str(a), str(b)],
            a_ref="scope/a.md",
            b_ref="scope/b.md",
        )
        tier4_escalate([first], pending, config=cfg)
        assert "- [ ]" in pending.read_text(encoding="utf-8")
        assert a.exists()  # not yet enacted

        second = _item(
            "Beta",
            _proposal("forget_a", 0.97),
            members=[str(a), str(b)],
            a_ref="scope/a.md",
            b_ref="scope/b.md",
        )
        tier4_escalate([second], pending, config=cfg)

        text = pending.read_text(encoding="utf-8")
        assert "- [x]" in text
        assert not a.exists()  # enacted on the cross-batch flip
        assert b.exists()
