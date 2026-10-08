# SPDX-License-Identifier: Apache-2.0
"""Tier-movement proposal drafter tests (issue athenaeum#2019, athenaeum#719 Plan step 6)."""

from __future__ import annotations

from pathlib import Path

from athenaeum.tier_movement_proposals import (
    _RETIRED_TIER_TOKENS,
    PROPOSED_ACTION_ADVISORY_REVIEW,
    REJECT_KIND,
    default_tier_movement_proposals_ledger_path,
    draft_tier_movement_proposal,
    list_pending_tier_movement_proposals,
    proposal_item_id,
    read_tier_movement_proposals_ledger,
    run_tier_movement_proposal_drafting,
)
from athenaeum.usage_report import ClaimUsage


def _usage(id_: str = "claim-1", pushed: int = 10, referenced: int = 0) -> ClaimUsage:
    return ClaimUsage(
        id=id_,
        pushed_count=pushed,
        referenced_count=referenced,
        last_pushed="2026-01-01T00:00:00+00:00",
        last_referenced=None,
    )


class TestDraft:
    def test_draft_is_deterministic(self) -> None:
        usage = _usage()
        d1 = draft_tier_movement_proposal(usage, window_days=30, pushed_min=5, referenced_max=0)
        d2 = draft_tier_movement_proposal(usage, window_days=30, pushed_min=5, referenced_max=0)
        assert d1 == d2

    def test_proposed_action_is_advisory_only(self) -> None:
        d = draft_tier_movement_proposal(_usage(), window_days=30, pushed_min=5, referenced_max=0)
        assert d.proposed_action == PROPOSED_ACTION_ADVISORY_REVIEW

    def test_ledger_record_never_carries_retired_tier_vocabulary(self) -> None:
        """athenaeum#1514 retired hot/warm/cold/refused -- a drafted record must
        never resurrect that vocabulary (module docstring's own regression).
        """
        d = draft_tier_movement_proposal(_usage(), window_days=30, pushed_min=5, referenced_max=0)
        record = d.to_ledger_record()
        serialized = str(sorted(record.items()))
        for token in _RETIRED_TIER_TOKENS:
            assert token not in serialized.lower().split()
            # Belt-and-suspenders: no value in the record equals a retired token.
            for value in record.values():
                if isinstance(value, str):
                    assert value.lower() != token

    def test_id_is_stable_for_same_inputs(self) -> None:
        usage = _usage()
        assert proposal_item_id(usage, window_days=30) == proposal_item_id(usage, window_days=30)

    def test_id_differs_for_different_window(self) -> None:
        usage = _usage()
        assert proposal_item_id(usage, window_days=30) != proposal_item_id(usage, window_days=60)


class TestRunDrafting:
    def test_disabled_by_default_does_no_io(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        summary = run_tier_movement_proposal_drafting(
            wiki_root=wiki_root,
            config=None,
            usages={"claim-1": _usage()},
        )
        assert summary.candidates_seen == 0
        assert summary.proposed == 0
        assert not default_tier_movement_proposals_ledger_path(wiki_root).exists()

    def test_enabled_drafts_and_ledgers_crossing_claims(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {
            "librarian": {
                "tier_movement_proposals_enabled": True,
                "tier_movement_pushed_min": 5,
                "tier_movement_referenced_max": 0,
            }
        }
        usages = {
            "low-value": _usage("low-value", pushed=10, referenced=0),
            "well-used": _usage("well-used", pushed=10, referenced=8),
            "rarely-pushed": _usage("rarely-pushed", pushed=2, referenced=0),
        }
        summary = run_tier_movement_proposal_drafting(
            wiki_root=wiki_root, config=config, usages=usages
        )
        assert summary.candidates_seen == 3
        assert summary.proposed == 1
        assert summary.skipped_below_threshold == 2
        assert [d.claim_id for d in summary.drafts] == ["low-value"]

        pending = list_pending_tier_movement_proposals(wiki_root)
        assert len(pending) == 1
        assert pending[0]["claim_id"] == "low-value"

    def test_idempotent_on_second_pass(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"tier_movement_proposals_enabled": True}}
        usages = {"low-value": _usage("low-value", pushed=10, referenced=0)}
        run_tier_movement_proposal_drafting(wiki_root=wiki_root, config=config, usages=usages)
        summary2 = run_tier_movement_proposal_drafting(
            wiki_root=wiki_root, config=config, usages=usages
        )
        assert summary2.proposed == 0
        assert summary2.skipped_pending == 1
        assert len(list_pending_tier_movement_proposals(wiki_root)) == 1

    def test_rejected_proposal_stays_suppressed(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"tier_movement_proposals_enabled": True}}
        usage = _usage("low-value", pushed=10, referenced=0)
        usages = {"low-value": usage}
        run_tier_movement_proposal_drafting(wiki_root=wiki_root, config=config, usages=usages)
        item_id = proposal_item_id(usage, window_days=30)

        ledger_path = default_tier_movement_proposals_ledger_path(wiki_root)
        import json

        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 1, "kind": REJECT_KIND, "id": item_id}) + "\n")

        summary2 = run_tier_movement_proposal_drafting(
            wiki_root=wiki_root, config=config, usages=usages
        )
        assert summary2.proposed == 0
        assert summary2.skipped_suppressed == 1
        assert list_pending_tier_movement_proposals(wiki_root) == []

    def test_dry_run_computes_but_does_not_ledger(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"tier_movement_proposals_enabled": True}}
        usages = {"low-value": _usage("low-value", pushed=10, referenced=0)}
        summary = run_tier_movement_proposal_drafting(
            wiki_root=wiki_root, config=config, usages=usages, dry_run=True
        )
        assert summary.proposed == 0
        assert len(summary.drafts) == 1
        assert not default_tier_movement_proposals_ledger_path(wiki_root).exists()


class TestLedgerTolerance:
    def test_read_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        ledger_path = default_tier_movement_proposals_ledger_path(wiki_root)
        ledger_path.write_text(
            '{"v": 1, "kind": "proposal", "id": "a"}\n{"v": 1, "kind": "pro', encoding="utf-8"
        )
        records = read_tier_movement_proposals_ledger(wiki_root)
        assert len(records) == 1
        assert records[0]["id"] == "a"


class TestQueueVisibility:
    def test_tier_movement_proposal_to_decision_shape(self) -> None:
        from athenaeum.decisions import tier_movement_proposal_to_decision

        rec = {
            "id": "abc123",
            "created_at": "2026-10-08T00:00:00Z",
            "claim_id": "low-value",
            "pushed_count": 10,
            "referenced_count": 0,
            "window_days": 30,
            "pushed_min": 5,
            "referenced_max": 0,
            "proposed_action": PROPOSED_ACTION_ADVISORY_REVIEW,
            "rationale": "low value",
        }
        item = tier_movement_proposal_to_decision(rec)
        assert item["type"] == "tier-movement-proposal"
        assert item["confidence"] is None
        assert "low-value" in item["summary"]
        assert item["payload"]["claim_id"] == "low-value"

    def test_proposal_reaches_list_pending_decisions(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"tier_movement_proposals_enabled": True}}
        run_tier_movement_proposal_drafting(
            wiki_root=wiki_root,
            config=config,
            usages={"low-value": _usage("low-value", pushed=10, referenced=0)},
        )

        items = list_pending_decisions(wiki_root)
        tm_items = [i for i in items if i["type"] == "tier-movement-proposal"]
        assert len(tm_items) == 1
        assert tm_items[0]["payload"]["claim_id"] == "low-value"

    def test_restricted_caller_never_sees_tier_movement_items(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        config = {"librarian": {"tier_movement_proposals_enabled": True}}
        run_tier_movement_proposal_drafting(
            wiki_root=wiki_root,
            config=config,
            usages={"low-value": _usage("low-value", pushed=10, referenced=0)},
        )

        items = list_pending_decisions(wiki_root, caller_audience=set())
        assert not [i for i in items if i["type"] == "tier-movement-proposal"]
