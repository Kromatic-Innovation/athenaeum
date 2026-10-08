# SPDX-License-Identifier: Apache-2.0
"""Policy-pack-edit proposal drafter tests (issue athenaeum#2019, athenaeum#719 Plan step 6)."""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.decision_answers import VALID_DECISION_TYPES
from athenaeum.decision_framing import ANSWERABLE_AS, answerable_as
from athenaeum.policy_pack_edit_proposals import (
    REJECT_KIND,
    default_policy_pack_edit_proposals_ledger_path,
    draft_policy_pack_edit_proposal,
    list_pending_policy_pack_edit_proposals,
    proposal_item_id,
    read_policy_pack_edit_proposals_ledger,
    run_policy_pack_edit_proposal_drafting,
)
from athenaeum.verdict_effects import AUTO_APPLY_OPERATIONS


def _diff() -> dict:
    return {
        "memory_class": "fact",
        "data_class": "contact",
        "jurisdiction": "us",
        "old_action": "store-off-corpus",
        "new_action": "refuse-write",
    }


class TestDraft:
    def test_draft_is_deterministic(self) -> None:
        diff = _diff()
        d1 = draft_policy_pack_edit_proposal("us-default", diff, rationale="tighten gate")
        d2 = draft_policy_pack_edit_proposal("us-default", diff, rationale="tighten gate")
        assert d1.id == d2.id
        assert d1.proposed_diff == d2.proposed_diff

    def test_id_differs_for_different_pack_or_diff(self) -> None:
        diff = _diff()
        a = proposal_item_id("us-default", diff)
        b = proposal_item_id("eu-gdpr", diff)
        c = proposal_item_id("us-default", {**diff, "new_action": "demote-cold"})
        assert len({a, b, c}) == 3


class TestMisrouteGuard:
    """AC: policy-pack-edit is proposal-only, never auto-applied; the only
    path to an effective change is a future human 'approve' answer.
    """

    def test_not_in_answerable_as(self) -> None:
        assert "policy-pack-edit" not in ANSWERABLE_AS
        assert answerable_as("policy-pack-edit") is None

    def test_not_a_valid_inbound_decision_answer_type(self) -> None:
        assert "policy-pack-edit" not in VALID_DECISION_TYPES

    def test_not_an_auto_apply_operation(self) -> None:
        """auto_apply.py / verdict_effects.py's auto-apply vocabulary is
        keyed on comparator VERDICTS, not decision-queue TYPES -- this
        string can never appear there structurally. Asserted directly so
        the invariant is not just structural but tested.
        """
        assert "policy-pack-edit" not in AUTO_APPLY_OPERATIONS


class TestRunDrafting:
    def test_disabled_by_default_does_no_io(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        summary = run_policy_pack_edit_proposal_drafting(
            [("us-default", _diff(), "tighten gate")],
            wiki_root=wiki_root,
            config=None,
        )
        assert summary.candidates_seen == 0
        assert not default_policy_pack_edit_proposals_ledger_path(wiki_root).exists()

    def test_enabled_drafts_and_ledgers(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"policy_pack_edit_proposals_enabled": True}}
        summary = run_policy_pack_edit_proposal_drafting(
            [("us-default", _diff(), "tighten gate")],
            wiki_root=wiki_root,
            config=config,
        )
        assert summary.proposed == 1
        pending = list_pending_policy_pack_edit_proposals(wiki_root)
        assert len(pending) == 1
        assert pending[0]["pack_name"] == "us-default"

    def test_idempotent_on_second_pass(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"policy_pack_edit_proposals_enabled": True}}
        candidates = [("us-default", _diff(), "tighten gate")]
        run_policy_pack_edit_proposal_drafting(candidates, wiki_root=wiki_root, config=config)
        summary2 = run_policy_pack_edit_proposal_drafting(
            candidates, wiki_root=wiki_root, config=config
        )
        assert summary2.proposed == 0
        assert summary2.skipped_pending == 1

    def test_rejected_proposal_stays_suppressed(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"policy_pack_edit_proposals_enabled": True}}
        diff = _diff()
        run_policy_pack_edit_proposal_drafting(
            [("us-default", diff, "tighten gate")], wiki_root=wiki_root, config=config
        )
        item_id = proposal_item_id("us-default", diff)
        ledger_path = default_policy_pack_edit_proposals_ledger_path(wiki_root)
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 1, "kind": REJECT_KIND, "id": item_id}) + "\n")

        summary2 = run_policy_pack_edit_proposal_drafting(
            [("us-default", diff, "tighten gate")], wiki_root=wiki_root, config=config
        )
        assert summary2.proposed == 0
        assert summary2.skipped_suppressed == 1
        assert list_pending_policy_pack_edit_proposals(wiki_root) == []

    def test_dry_run_computes_but_does_not_ledger(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"policy_pack_edit_proposals_enabled": True}}
        summary = run_policy_pack_edit_proposal_drafting(
            [("us-default", _diff(), "tighten gate")],
            wiki_root=wiki_root,
            config=config,
            dry_run=True,
        )
        assert summary.proposed == 0
        assert len(summary.drafts) == 1
        assert not default_policy_pack_edit_proposals_ledger_path(wiki_root).exists()


class TestLedgerTolerance:
    def test_read_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        ledger_path = default_policy_pack_edit_proposals_ledger_path(wiki_root)
        ledger_path.write_text(
            '{"v": 1, "kind": "proposal", "id": "a"}\n{"v": 1, "kind": "pro', encoding="utf-8"
        )
        records = read_policy_pack_edit_proposals_ledger(wiki_root)
        assert len(records) == 1
        assert records[0]["id"] == "a"


class TestQueueVisibility:
    def test_policy_pack_edit_proposal_to_decision_shape(self) -> None:
        from athenaeum.decisions import policy_pack_edit_proposal_to_decision

        rec = {
            "id": "abc123",
            "created_at": "2026-10-08T00:00:00Z",
            "pack_name": "us-default",
            "proposed_diff": _diff(),
            "rationale": "tighten gate",
        }
        item = policy_pack_edit_proposal_to_decision(rec)
        assert item["type"] == "policy-pack-edit"
        assert item["confidence"] is None
        assert "us-default" in item["summary"]
        assert item["payload"]["pack_name"] == "us-default"

    def test_proposal_reaches_list_pending_decisions(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"policy_pack_edit_proposals_enabled": True}}
        run_policy_pack_edit_proposal_drafting(
            [("us-default", _diff(), "tighten gate")], wiki_root=wiki_root, config=config
        )

        items = list_pending_decisions(wiki_root)
        pp_items = [i for i in items if i["type"] == "policy-pack-edit"]
        assert len(pp_items) == 1
        assert pp_items[0]["payload"]["pack_name"] == "us-default"

    def test_restricted_caller_never_sees_policy_pack_edit_items(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"policy_pack_edit_proposals_enabled": True}}
        run_policy_pack_edit_proposal_drafting(
            [("us-default", _diff(), "tighten gate")], wiki_root=wiki_root, config=config
        )

        items = list_pending_decisions(wiki_root, caller_audience=set())
        assert not [i for i in items if i["type"] == "policy-pack-edit"]
