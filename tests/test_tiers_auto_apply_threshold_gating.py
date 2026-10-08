# SPDX-License-Identifier: Apache-2.0
"""Tests for threading the ledger-backed auto-apply-threshold override
(issue athenaeum#2018) through ``tiers.py``'s two production gating call
sites (issue athenaeum#2032): ``tier4_escalate`` and
``reresolve_open_questions``.

Both call sites now pass ``wiki_root`` so :func:`athenaeum.config.
auto_apply_threshold_ledger_override_for` is live on the gating path, not
just the drafter's read path. ``tier4_escalate`` always produces a FRESH
verdict and never passes ``as_of``. ``reresolve_open_questions`` re-decides
blocks that were ALREADY pending before "now" and threads the block's own
``raised_at`` as ``as_of`` so a proposal approved after the block was
raised cannot retroactively widen the floor that gates it -- the
athenaeum#2018 AC this issue closes out.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.answers import parse_pending_questions
from athenaeum.auto_apply_proposals import (
    AutoApplyThresholdProposalDraft,
    approve_auto_apply_threshold_proposal,
    default_auto_apply_proposals_ledger_path,
    proposal_item_id,
)
from athenaeum.models import EscalationItem
from athenaeum.resolutions import ResolutionProposal
from athenaeum.store import append_line_durable
from athenaeum.tiers import reresolve_open_questions, tier4_escalate

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _seed_approved_ledger_override(
    wiki_root: Path,
    *,
    action: str,
    current: float,
    proposed: float,
    approved_at: datetime,
    drafted_at: datetime | None = None,
) -> str:
    """Write a proposal + approve record directly (bypassing the T1
    disagreement-rate drafter, which this module's tests don't need) so the
    approve record's ``created_at`` can be pinned to an exact instant.
    """
    item_id = proposal_item_id(action, current, proposed)
    draft = AutoApplyThresholdProposalDraft(
        id=item_id,
        action=action,
        tier="T1",
        current_threshold=current,
        proposed_threshold=proposed,
        step=round(current - proposed, 6),
        disagreement_rate_value=0.5,
        trigger=0.2,
        sampled=10,
        reviewed=10,
        overturned=5,
    )
    ledger_path = default_auto_apply_proposals_ledger_path(wiki_root)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    append_line_durable(
        ledger_path,
        (json.dumps(draft.to_ledger_record(now=drafted_at), sort_keys=True) + "\n").encode("utf-8"),
    )
    approve_auto_apply_threshold_proposal(wiki_root, proposal_id=item_id, now=approved_at)
    return item_id


def _make_proposal(action: str, confidence: float) -> ResolutionProposal:
    return ResolutionProposal(
        recommended_winner="a",
        action=action,
        rationale="user > unsourced",
        confidence=confidence,
        source_precedence_used=["a:user > b:unsourced"],
    )


def _fake_client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _write_member(knowledge_root: Path, scope: str, filename: str, body: str) -> str:
    scope_dir = knowledge_root / "raw" / "auto-memory" / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        "---\nname: " + filename[:-3] + "\ntype: feedback\n---\n" + body + "\n",
        encoding="utf-8",
    )
    return f"{scope}/{filename}"


def _escalate_proposalless(
    knowledge_root: Path,
    *,
    entity: str = "Tristan",
    passage_a: str = "Tristan is German.",
    passage_b: str = "Tristan is NOT German.",
) -> Path:
    """Write a single proposal-less open ``[ ]`` block + its member files.

    Mirrors ``tests/test_reresolve_open_questions.py``'s fixture of the same
    name -- the degraded escalation (no resolver call at raise time) that
    ``reresolve_open_questions`` heals on a later pass.
    """
    wiki = knowledge_root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    ref_a = _write_member(knowledge_root, "scope-x", "feedback_a.md", passage_a)
    ref_b = _write_member(knowledge_root, "scope-x", "feedback_b.md", passage_b)
    description = (
        "Detector says these conflict.\n"
        f"Passage 1: {passage_a}\n"
        f"Passage 2: {passage_b}\n"
        f"Members involved: {ref_a}, {ref_b}"
    )
    pending = wiki / "_pending_questions.md"
    tier4_escalate(
        [
            EscalationItem(
                raw_ref="wiki/auto-tristan.md",
                entity_name=entity,
                conflict_type="factual",
                description=description,
            )
        ],
        pending,
    )
    return pending


def _inject_raised_at(pending: Path, raised_at_iso: str) -> None:
    """Splice a ``**Raised at**:`` line into the (sole) block in *pending*.

    ``athenaeum.answers``'s block parser recognizes this line on ANY block
    regardless of ``decision_kind`` (table-driven over
    ``_CONFIRMATION_FIELD_PREFIXES``, not gated on "confirmation" blocks) --
    see ``_match_confirmation_key``. Production detector-raised blocks never
    carry this line (only agent-raised confirmations do), so this helper
    manufactures the input needed to exercise the ``as_of`` plumbing itself,
    independent of whether today's detector path happens to populate it.
    """
    text = pending.read_text(encoding="utf-8")
    lines = text.splitlines()
    # Must land AFTER the checkbox line (inside the block the parser scans
    # for metadata keys) -- the parser only recognizes `_CONFIRMATION_FIELD_PREFIXES`
    # lines in `remaining = lines[checkbox_idx + 1:]`, past the header.
    insert_at = next(i for i, line in enumerate(lines) if line.strip().startswith("- ["))
    lines.insert(insert_at + 1, f"**Raised at**: {raised_at_iso}")
    pending.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _payload(action: str, *, confidence: float) -> str:
    return (
        f'{{"recommended_winner": "a", "action": "{action}", '
        f'"confidence": {confidence}, '
        '"rationale": "test verdict rationale.", '
        '"source_precedence_used": ["a:user > b:unsourced"]}'
    )


# ---------------------------------------------------------------------------
# tier4_escalate: fresh verdicts honor the ledger override directly
# ---------------------------------------------------------------------------


class TestTier4EscalateLedgerOverride:
    def test_without_override_stays_below_default_threshold(self, tmp_path: Path) -> None:
        """Baseline: confidence 0.87 < keep_a's 0.90 default -> stays open."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        pending = wiki / "_pending_questions.md"
        item = EscalationItem(
            raw_ref="wiki/a.md",
            entity_name="NoOverride",
            conflict_type="factual",
            description="d",
            proposal=_make_proposal("keep_a", 0.87),
        )
        tier4_escalate([item], pending, config={"resolve": {"auto_apply": True}})
        text = pending.read_text(encoding="utf-8")
        assert "- [ ]" in text
        assert "**Auto-resolved**" not in text

    def test_approved_ledger_override_is_read_on_the_gating_path(self, tmp_path: Path) -> None:
        """Issue athenaeum#2032 AC1: an approved ledger override widening
        keep_a from 0.90 to 0.85 is now consulted by ``tier4_escalate``
        itself -- confidence 0.87 falls BETWEEN the two floors, so this only
        auto-applies when the ledger layer is actually live on this path."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        pending = wiki / "_pending_questions.md"
        _seed_approved_ledger_override(
            wiki,
            action="keep_a",
            current=0.90,
            proposed=0.85,
            approved_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        item = EscalationItem(
            raw_ref="wiki/b.md",
            entity_name="WithOverride",
            conflict_type="factual",
            description="d",
            proposal=_make_proposal("keep_a", 0.87),
        )
        tier4_escalate([item], pending, config={"resolve": {"auto_apply": True}})
        text = pending.read_text(encoding="utf-8")
        assert "- [x]" in text
        assert "**Auto-resolved**: true" in text


# ---------------------------------------------------------------------------
# reresolve_open_questions: already-pending blocks stay gated by the
# pre-approval threshold (athenaeum#2018's AC, proven at the real call site)
# ---------------------------------------------------------------------------


class TestReresolveOpenQuestionsAsOfCutoff:
    def test_in_flight_block_stays_gated_by_pre_approval_threshold(self, tmp_path: Path) -> None:
        """A block raised at T0; the widen proposal is approved LATER, at
        T1 > T0. Re-resolving the block must NOT honor that override --
        confidence 0.87 is only >= the widened 0.85 floor, never the
        pre-approval 0.90 one, so the block must stay open."""
        pending = _escalate_proposalless(tmp_path)
        _inject_raised_at(pending, "2026-01-01T00:00:00Z")  # T0
        _seed_approved_ledger_override(
            tmp_path / "wiki",
            action="keep_a",
            current=0.90,
            proposed=0.85,
            approved_at=datetime(2026, 1, 2, 0, 0, 0),  # T1 > T0
        )
        client = _fake_client(_payload("keep_a", confidence=0.87))
        reresolve_open_questions(pending, client=client, config={})

        parsed = parse_pending_questions(pending)
        assert len(parsed) == 1
        assert parsed[0].answered is False
        assert "**Auto-resolved**" not in parsed[0].raw_block

    def test_block_raised_after_approval_can_honor_the_override(self, tmp_path: Path) -> None:
        """Mirror case: the approval (T1) predates the block's raise (T2 >
        T1) -- the widened floor was already in effect when the block was
        raised, so it is safe to honor and the block auto-applies."""
        pending = _escalate_proposalless(tmp_path)
        _seed_approved_ledger_override(
            tmp_path / "wiki",
            action="keep_a",
            current=0.90,
            proposed=0.85,
            approved_at=datetime(2026, 1, 1, 0, 0, 0),  # T1
        )
        _inject_raised_at(pending, "2026-01-02T00:00:00Z")  # T2 > T1

        client = _fake_client(_payload("keep_a", confidence=0.87))
        reresolve_open_questions(pending, client=client, config={})

        parsed = parse_pending_questions(pending)
        assert len(parsed) == 1
        assert parsed[0].answered is True
        assert "**Auto-resolved**: true" in parsed[0].raw_block

    def test_block_with_no_raised_at_fails_closed(self, tmp_path: Path) -> None:
        """The realistic case: a plain detector-raised block carries NO
        ``**Raised at**:`` line at all (``raised_at`` parses to ``""`` --
        only agent-raised confirmations populate it). With no raise
        timestamp to prove the approval predates the block, the ledger
        layer must be skipped entirely -- not trusted by default -- so the
        block stays gated by the pre-athenaeum#2018 default threshold."""
        pending = _escalate_proposalless(tmp_path)
        assert parse_pending_questions(pending)[0].raised_at == ""
        _seed_approved_ledger_override(
            tmp_path / "wiki",
            action="keep_a",
            current=0.90,
            proposed=0.85,
            approved_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        client = _fake_client(_payload("keep_a", confidence=0.87))
        reresolve_open_questions(pending, client=client, config={})

        parsed = parse_pending_questions(pending)
        assert parsed[0].answered is False
        assert "**Auto-resolved**" not in parsed[0].raw_block
