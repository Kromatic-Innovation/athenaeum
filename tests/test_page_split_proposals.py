# SPDX-License-Identifier: Apache-2.0
"""Tests for the page-split proposal drafter (issue athenaeum#2018, athenaeum#719
Plan step 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.page_decompose import BulletPlan, DecomposeReport
from athenaeum.page_split_proposals import (
    PAGE_SPLIT_PROPOSALS_LEDGER_FILENAME,
    approve_page_split_proposal,
    default_page_split_proposals_ledger_path,
    draft_page_split_proposal,
    list_pending_page_split_proposals,
    proposal_item_id,
    read_page_split_proposals_ledger,
    reject_page_split_proposal,
    run_page_split_proposal_detection,
)


def _bullet(ordinal: int, uid: str, disposition: str = "attached") -> BulletPlan:
    return BulletPlan(
        ordinal=ordinal,
        id=f"b{ordinal}",
        raw=f"- bullet {ordinal}",
        subject=f"subject-{ordinal}",
        refs=["a"],
        disposition=disposition,
        uid=uid,
    )


def _report(
    *,
    source_uid: str = "tool-page",
    distinct_subjects: int = 5,
    source_path: str = "wiki/tool-page.md",
) -> DecomposeReport:
    bullets = [_bullet(i, f"company-{i}") for i in range(distinct_subjects)]
    return DecomposeReport(
        source_uid=source_uid,
        source_name="Tool Page",
        source_path=source_path,
        subject_until=r"^\S+",
        bullets=bullets,
    )


class TestDraftPageSplitProposal:
    def test_under_threshold_drafts_nothing(self) -> None:
        report = _report(distinct_subjects=2)
        draft = draft_page_split_proposal(
            report, config={"librarian": {"page_split_proposals": {"heterogeneity_threshold": 5}}}
        )
        assert draft is None

    def test_at_or_above_threshold_drafts(self) -> None:
        report = _report(distinct_subjects=5)
        draft = draft_page_split_proposal(
            report, config={"librarian": {"page_split_proposals": {"heterogeneity_threshold": 5}}}
        )
        assert draft is not None
        assert draft.heterogeneity == 5
        assert draft.source_uid == "tool-page"
        assert len(draft.subject_uids) == 5

    def test_duplicate_bullets_resolving_to_same_subject_do_not_inflate_heterogeneity(
        self,
    ) -> None:
        bullets = [_bullet(i, "same-company") for i in range(10)]
        report = DecomposeReport(
            source_uid="tool-page",
            source_name="Tool Page",
            source_path="wiki/tool-page.md",
            subject_until=r"^\S+",
            bullets=bullets,
        )
        draft = draft_page_split_proposal(
            report, config={"librarian": {"page_split_proposals": {"heterogeneity_threshold": 5}}}
        )
        assert draft is None  # 1 distinct subject, under any sane threshold


class TestProposalItemId:
    def test_same_page_different_plan_gets_different_id(self) -> None:
        id_a = proposal_item_id("tool-page", ("company-1", "company-2"))
        id_b = proposal_item_id("tool-page", ("company-1", "company-3"))
        assert id_a != id_b

    def test_order_independent(self) -> None:
        id_a = proposal_item_id("tool-page", ("company-1", "company-2"))
        id_b = proposal_item_id("tool-page", ("company-2", "company-1"))
        assert id_a == id_b


class TestRunPageSplitProposalDetection:
    def test_dry_run_drafts_but_does_not_persist(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        report = _report(distinct_subjects=6)
        summary = run_page_split_proposal_detection([report], wiki_root=wiki_root, dry_run=True)
        assert summary.proposed == 0
        assert len(summary.drafts) == 1
        assert not default_page_split_proposals_ledger_path(wiki_root).exists()

    def test_idempotent_pending(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        report = _report(distinct_subjects=6)
        first = run_page_split_proposal_detection([report], wiki_root=wiki_root)
        assert first.proposed == 1
        second = run_page_split_proposal_detection([report], wiki_root=wiki_root)
        assert second.proposed == 0
        assert second.skipped_pending == 1

    def test_rejected_is_permanently_suppressed(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        report = _report(distinct_subjects=6)
        run_page_split_proposal_detection([report], wiki_root=wiki_root)
        pending = list_pending_page_split_proposals(wiki_root)
        assert len(pending) == 1
        reject_page_split_proposal(wiki_root, proposal_id=pending[0]["id"])

        again = run_page_split_proposal_detection([report], wiki_root=wiki_root)
        assert again.proposed == 0
        assert again.skipped_suppressed == 1
        assert list_pending_page_split_proposals(wiki_root) == []


class TestApprovePageSplitProposalNeverSplits:
    """AC: approving a page-split proposal never performs the split."""

    def test_approve_never_touches_page_decompose_apply_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.page_decompose as page_decompose

        calls: list[object] = []

        def _fake_apply_report(*args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))
            raise AssertionError("apply_report must never be called by an approval")

        monkeypatch.setattr(page_decompose, "apply_report", _fake_apply_report)

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        source_page = wiki_root / "tool-page.md"
        original_bytes = b"---\ntype: tool\n---\n\n- original body\n"
        source_page.write_bytes(original_bytes)

        report = _report(distinct_subjects=6, source_path=str(source_page))
        run_page_split_proposal_detection([report], wiki_root=wiki_root)
        pending = list_pending_page_split_proposals(wiki_root)
        assert len(pending) == 1

        record = approve_page_split_proposal(wiki_root, proposal_id=pending[0]["id"])

        assert calls == []  # apply_report was never invoked
        assert record["kind"] == "approve"
        # The page on disk is byte-for-byte unchanged.
        assert source_page.read_bytes() == original_bytes

    def test_approve_unknown_id_raises(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        with pytest.raises(ValueError):
            approve_page_split_proposal(wiki_root, proposal_id="nope")

    def test_approve_already_resolved_raises(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        report = _report(distinct_subjects=6)
        run_page_split_proposal_detection([report], wiki_root=wiki_root)
        pending = list_pending_page_split_proposals(wiki_root)
        proposal_id = pending[0]["id"]
        approve_page_split_proposal(wiki_root, proposal_id=proposal_id)
        with pytest.raises(ValueError):
            approve_page_split_proposal(wiki_root, proposal_id=proposal_id)


class TestLedgerFilename:
    def test_filename_constant_matches_path_helper(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        assert (
            default_page_split_proposals_ledger_path(wiki_root)
            == wiki_root / PAGE_SPLIT_PROPOSALS_LEDGER_FILENAME
        )

    def test_read_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        report = _report(distinct_subjects=6)
        run_page_split_proposal_detection([report], wiki_root=wiki_root)
        ledger = default_page_split_proposals_ledger_path(wiki_root)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write('{"kind": "proposal", "id": "broken"')  # no closing brace/newline
        records = read_page_split_proposals_ledger(wiki_root)
        assert len(records) == 1
