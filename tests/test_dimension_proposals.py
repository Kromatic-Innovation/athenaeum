# SPDX-License-Identifier: Apache-2.0
"""Tests for the dimension-proposal drafter (issue athenaeum#2015, athenaeum#719 Plan step 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.dimension_proposals import (
    DIMENSION_PROPOSALS_LEDGER_FILENAME,
    default_dimension_proposals_ledger_path,
    draft_dimension_proposal,
    enforce_ask_budget,
    list_pending_dimension_proposals,
    plan_backfill,
    proposal_item_id,
    read_dimension_proposals_ledger,
    run_dimension_proposal_drafting,
)
from athenaeum.signal_mining import MinedShape, ShapeKey


def _shape(
    *,
    missing: tuple[str, ...] = ("jurisdiction",),
    memory_classes: tuple[str | None, str | None] = ("fact", "fact"),
    scopes: tuple[str | None, str | None] = (None, None),
    count: int = 10,
    threshold: int = 5,
    window_days: int = 30,
    example_pairs: tuple[str, ...] = ("alpha+beta", "gamma+delta"),
) -> MinedShape:
    key = ShapeKey(
        verdict_type="underdetermined",
        missing_dimensions=missing,
        memory_classes=memory_classes,
        scopes=scopes,
    )
    return MinedShape(
        key=key,
        count=count,
        example_pairs=example_pairs,
        threshold=threshold,
        window_days=window_days,
    )


# ---------------------------------------------------------------------------
# plan_backfill: origin-is-provenance regression (AC)
# ---------------------------------------------------------------------------


class TestPlanBackfillOriginIsProvenance:
    def test_scope_is_never_auto_even_with_coord_origins_present(self) -> None:
        """A page whose basis.coord_origins already names "scope" looks like
        ready-made provenance for the scope coordinate -- the backfill plan
        must still mark it "ask", never "auto" (issue athenaeum#714's rule,
        restated for this drafter)."""
        plan = plan_backfill(("scope",), coord_origins={"scope": "answer:q_1"})
        assert plan == {"scope": "ask"}

    def test_scope_is_ask_with_no_provenance_either(self) -> None:
        plan = plan_backfill(("scope",), coord_origins=None)
        assert plan == {"scope": "ask"}

    def test_non_scope_dimension_is_auto_when_provenance_exists(self) -> None:
        plan = plan_backfill(("jurisdiction",), coord_origins={"jurisdiction": "answer:q_2"})
        assert plan == {"jurisdiction": "auto"}

    def test_non_scope_dimension_is_ask_with_no_provenance(self) -> None:
        plan = plan_backfill(("jurisdiction",), coord_origins=None)
        assert plan == {"jurisdiction": "ask"}

    def test_mixed_shape_scope_stays_ask_while_sibling_dimension_is_auto(self) -> None:
        plan = plan_backfill(
            ("scope", "jurisdiction"),
            coord_origins={"scope": "answer:q_1", "jurisdiction": "answer:q_2"},
        )
        assert plan == {"scope": "ask", "jurisdiction": "auto"}


# ---------------------------------------------------------------------------
# enforce_ask_budget
# ---------------------------------------------------------------------------


class TestEnforceAskBudget:
    def test_within_budget_is_unchanged(self) -> None:
        plan = {"jurisdiction": "ask"}
        final, ask_count, narrowed = enforce_ask_budget(plan, pair_count=5, budget_cap=20)
        assert final == plan
        assert ask_count == 5
        assert narrowed is False

    def test_all_auto_plan_costs_nothing(self) -> None:
        plan = {"jurisdiction": "auto"}
        final, ask_count, narrowed = enforce_ask_budget(plan, pair_count=500, budget_cap=20)
        assert ask_count == 0
        assert narrowed is False
        assert final == plan

    def test_over_budget_flips_non_scope_dims_to_auto_backfill_only(self) -> None:
        plan = {"jurisdiction": "ask"}
        final, ask_count, narrowed = enforce_ask_budget(plan, pair_count=50, budget_cap=20)
        assert narrowed is True
        assert final == {"jurisdiction": "auto"}
        assert ask_count == 0

    def test_over_budget_with_scope_narrows_applies_to_instead(self) -> None:
        """``scope`` can never flip to auto (plan_backfill's own invariant,
        reasserted here); the only remaining remedy is narrowing the
        affected-pair population so the remaining asks fit the cap."""
        plan = {"scope": "ask", "jurisdiction": "ask"}
        final, ask_count, narrowed = enforce_ask_budget(plan, pair_count=50, budget_cap=20)
        assert narrowed is True
        assert final["scope"] == "ask"
        assert final["jurisdiction"] == "auto"
        assert ask_count <= 20
        assert ask_count == 20  # budget_cap // 1 remaining ask dim ("scope")

    def test_ask_count_never_exceeds_budget_cap(self) -> None:
        plan = {"scope": "ask"}
        _final, ask_count, _narrowed = enforce_ask_budget(plan, pair_count=1000, budget_cap=7)
        assert ask_count <= 7


# ---------------------------------------------------------------------------
# draft_dimension_proposal
# ---------------------------------------------------------------------------


class TestDraftDimensionProposal:
    def test_draft_is_deterministic(self) -> None:
        shape = _shape()
        a = draft_dimension_proposal(shape, "jurisdiction")
        b = draft_dimension_proposal(shape, "jurisdiction")
        assert a == b

    def test_draft_id_depends_on_dimension_name_and_shape(self) -> None:
        shape = _shape(missing=("jurisdiction", "severity"))
        id_a = proposal_item_id("jurisdiction", shape.key)
        id_b = proposal_item_id("severity", shape.key)
        assert id_a != id_b

    def test_draft_never_calls_out_for_an_llm_summary(self) -> None:
        """No client/model knob anywhere on the draft path -- deliberately
        kept off the trigger path per athenaeum#719's own AC. (Evals: not
        needed -- there is no prompt or model knob in this module.)"""
        shape = _shape()
        draft = draft_dimension_proposal(shape, "jurisdiction")
        assert not hasattr(draft, "model")
        assert "model" not in draft.to_ledger_record()

    def test_ask_budget_is_enforced_at_proposal_time_not_discovered_later(self) -> None:
        shape = _shape(missing=("scope",), count=500)
        draft = draft_dimension_proposal(shape, "scope", config={})
        assert draft.ask_count <= 20  # default budget cap
        assert draft.applies_to_narrowed is True
        assert draft.backfill_plan["scope"] == "ask"

    def test_applies_to_derived_from_shape_key(self) -> None:
        shape = _shape(memory_classes=("fact", "decision"), scopes=("team-a", "team-a"))
        draft = draft_dimension_proposal(shape, "jurisdiction")
        assert draft.applies_to.get("memory_class") == ["decision", "fact"]
        assert draft.applies_to.get("scope") == ["team-a"]


# ---------------------------------------------------------------------------
# run_dimension_proposal_drafting: ledger + idempotency + dry run
# ---------------------------------------------------------------------------


class TestRunDimensionProposalDrafting:
    def test_untriggered_shape_is_never_drafted(self, tmp_path: Path) -> None:
        shape = _shape(count=1, threshold=5)
        summary = run_dimension_proposal_drafting([shape], wiki_root=tmp_path)
        assert summary.proposed == 0
        assert summary.shapes_seen == 0

    def test_triggered_shape_is_drafted_and_ledgered(self, tmp_path: Path) -> None:
        shape = _shape(count=10, threshold=5)
        summary = run_dimension_proposal_drafting([shape], wiki_root=tmp_path)
        assert summary.proposed == 1
        assert summary.shapes_seen == 1
        ledger = read_dimension_proposals_ledger(tmp_path)
        assert len(ledger) == 1
        assert ledger[0]["name"] == "jurisdiction"
        assert ledger[0]["kind"] == "proposal"

    def test_one_proposal_per_missing_dimension(self, tmp_path: Path) -> None:
        shape = _shape(missing=("jurisdiction", "severity"), count=10, threshold=5)
        summary = run_dimension_proposal_drafting([shape], wiki_root=tmp_path)
        assert summary.proposed == 2
        names = {rec["name"] for rec in read_dimension_proposals_ledger(tmp_path)}
        assert names == {"jurisdiction", "severity"}

    def test_dry_run_computes_drafts_but_writes_nothing(self, tmp_path: Path) -> None:
        shape = _shape(count=10, threshold=5)
        summary = run_dimension_proposal_drafting([shape], wiki_root=tmp_path, dry_run=True)
        assert summary.proposed == 0
        assert len(summary.drafts) == 1
        assert read_dimension_proposals_ledger(tmp_path) == []

    def test_a_second_run_does_not_redraft_the_same_shape(self, tmp_path: Path) -> None:
        shape = _shape(count=10, threshold=5)
        run_dimension_proposal_drafting([shape], wiki_root=tmp_path)
        summary2 = run_dimension_proposal_drafting([shape], wiki_root=tmp_path)
        assert summary2.proposed == 0
        assert summary2.skipped_pending == 1
        assert len(read_dimension_proposals_ledger(tmp_path)) == 1

    def test_a_rejected_shape_is_permanently_suppressed(self, tmp_path: Path) -> None:
        import json

        shape = _shape(count=10, threshold=5)
        run_dimension_proposal_drafting([shape], wiki_root=tmp_path)
        item_id = proposal_item_id("jurisdiction", shape.key)
        ledger_path = default_dimension_proposals_ledger_path(tmp_path)
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 1, "kind": "reject", "id": item_id}) + "\n")

        summary = run_dimension_proposal_drafting([shape], wiki_root=tmp_path)
        assert summary.proposed == 0
        assert summary.skipped_suppressed == 1
        assert list_pending_dimension_proposals(tmp_path) == []

    def test_provenance_threaded_from_example_pairs_enables_auto(self, tmp_path: Path) -> None:
        shape = _shape(missing=("jurisdiction",), count=10, threshold=5)
        summary = run_dimension_proposal_drafting(
            [shape],
            wiki_root=tmp_path,
            coord_origins_by_pair={"alpha+beta": {"jurisdiction": "answer:q_9"}},
        )
        assert summary.proposed == 1
        rec = read_dimension_proposals_ledger(tmp_path)[0]
        assert rec["backfill_plan"]["jurisdiction"] == "auto"

    def test_ledger_filename_matches_constant(self, tmp_path: Path) -> None:
        shape = _shape(count=10, threshold=5)
        run_dimension_proposal_drafting([shape], wiki_root=tmp_path)
        assert (tmp_path / DIMENSION_PROPOSALS_LEDGER_FILENAME).exists()


# ---------------------------------------------------------------------------
# decisions.py wiring: visible in the unified queue
# ---------------------------------------------------------------------------


class TestQueueVisibility:
    def test_dimension_proposal_to_decision_shape(self) -> None:
        from athenaeum.decisions import dimension_proposal_to_decision

        rec = {
            "id": "abc123",
            "created_at": "2026-10-08T00:00:00Z",
            "name": "jurisdiction",
            "count": 12,
            "window_days": 30,
            "ask_count": 12,
            "backfill_plan": {"jurisdiction": "ask"},
            "example_pairs": ["alpha+beta"],
        }
        item = dimension_proposal_to_decision(rec)
        assert item["type"] == "dimension-proposal"
        assert item["confidence"] is None
        assert "jurisdiction" in item["summary"]
        assert item["payload"]["name"] == "jurisdiction"

    def test_proposal_reaches_list_pending_decisions(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        shape = _shape(count=10, threshold=5)
        run_dimension_proposal_drafting([shape], wiki_root=wiki_root)

        items = list_pending_decisions(wiki_root)
        dp_items = [i for i in items if i["type"] == "dimension-proposal"]
        assert len(dp_items) == 1
        assert dp_items[0]["payload"]["name"] == "jurisdiction"

    def test_restricted_caller_never_sees_dimension_proposal_items(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        shape = _shape(count=10, threshold=5)
        run_dimension_proposal_drafting([shape], wiki_root=wiki_root)

        items = list_pending_decisions(wiki_root, caller_audience=set())
        assert not [i for i in items if i["type"] == "dimension-proposal"]


# ---------------------------------------------------------------------------
# Misroute guard: framed + visible, but NOT answerable (AC)
# ---------------------------------------------------------------------------


class TestMisrouteGuard:
    def test_type_is_in_type_framing(self) -> None:
        from athenaeum.decision_framing import _TYPE_FRAMING

        assert "dimension-proposal" in _TYPE_FRAMING

    def test_type_is_not_in_valid_decision_types(self) -> None:
        """The hard AC: this child must NOT add "dimension-proposal" to the
        applier's vocabulary -- ratification is the next child's job."""
        from athenaeum.decision_answers import VALID_DECISION_TYPES

        assert "dimension-proposal" not in VALID_DECISION_TYPES

    def test_answerable_as_returns_none(self) -> None:
        from athenaeum.decision_framing import answerable_as

        assert answerable_as("dimension-proposal") is None

    def test_cli_answer_refuses_cleanly_not_a_crash(self, tmp_path: Path) -> None:
        from athenaeum.cli import main as cli_main

        (tmp_path / "wiki").mkdir()
        (tmp_path / "raw").mkdir()
        rc = cli_main(
            [
                "decisions",
                "answer",
                "--path",
                str(tmp_path),
                "--id",
                "abc",
                "--type",
                "dimension-proposal",
                "--answer",
                '{"verdict": "approve"}',
            ]
        )
        assert rc != 0
        answers_dir = tmp_path / "raw" / "answers"
        written = sorted(answers_dir.glob("*.md")) if answers_dir.exists() else []
        assert written == []

    def test_load_decision_answer_rejects_an_unregistered_type(self, tmp_path: Path) -> None:
        """A hand-written answer file naming "dimension-proposal" must be
        refused as malformed, never silently dispatched to
        _apply_proposed_rule_answer (the dispatch's only remaining "else"
        branch)."""
        from athenaeum.decision_answers import MalformedDecisionAnswer, _load_decision_answer

        path = tmp_path / "answer.md"
        path.write_text(
            "---\n"
            "decision_id: abc\n"
            "decision_type: dimension-proposal\n"
            "verdict: approve\n"
            "---\n",
            encoding="utf-8",
        )
        with pytest.raises(MalformedDecisionAnswer):
            _load_decision_answer(path)

    def test_response_schema_is_three_way(self) -> None:
        from athenaeum.decision_framing import response_schema_for

        schema = response_schema_for("dimension-proposal")
        assert set(schema["properties"]["verdict"]["enum"]) == {"approve", "rename", "reject"}

    def test_validate_answer_requires_name_on_rename(self) -> None:
        from athenaeum.decision_framing import validate_answer

        errors = validate_answer("dimension-proposal", {"verdict": "rename"})
        assert errors

        errors = validate_answer("dimension-proposal", {"verdict": "rename", "name": "new-axis"})
        assert errors == []

    def test_validate_answer_accepts_plain_approve_and_reject(self) -> None:
        from athenaeum.decision_framing import validate_answer

        assert validate_answer("dimension-proposal", {"verdict": "approve"}) == []
        assert validate_answer("dimension-proposal", {"verdict": "reject"}) == []
