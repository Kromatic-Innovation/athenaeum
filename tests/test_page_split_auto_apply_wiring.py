# SPDX-License-Identifier: Apache-2.0
"""End-to-end wiring tests for the page-split / auto-apply-threshold
decision types (issue athenaeum#2018, athenaeum#719 Plan step 6).

Mirrors athenaeum#2015's misroute-guard shape: proves both types are listed by
``list_pending_decisions`` (owner-only, same as proposed-rule/dimension-
proposal), answerable via ``apply_decision_answers`` end to end with a real
``raw/answers/*.md`` file, and that ``validate_answer`` accepts only
approve/reject for each.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.auto_apply_proposals import run_auto_apply_proposal_detection
from athenaeum.calibration import read_calibration_ledger, record_audit_review, sample_tier_decision
from athenaeum.decision_answers import apply_decision_answers, write_decision_answer
from athenaeum.decision_framing import validate_answer
from athenaeum.decisions import list_pending_decisions
from athenaeum.page_decompose import BulletPlan, DecomposeReport
from athenaeum.page_split_proposals import run_page_split_proposal_detection

_FULL_SAMPLE_CONFIG = {
    "librarian": {
        "audit_sample_rate_t1_rejects": 1.0,
        "audit_sample_rate_t2_approvals": 1.0,
    }
}

_AUTO_APPLY_CONFIG = {
    "librarian": {
        "audit_sample_rate_t1_rejects": 1.0,
        "audit_sample_rate_t2_approvals": 1.0,
        "auto_apply_proposals": {
            "disagreement_trigger": 0.2,
            "widen_step": 0.05,
        },
    }
}


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    return wiki


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    return raw


def _page_split_report(source_path: Path) -> DecomposeReport:
    bullets = [
        BulletPlan(
            ordinal=i,
            id=f"b{i}",
            raw=f"- bullet {i}",
            subject=f"subject-{i}",
            refs=["a"],
            disposition="attached",
            uid=f"company-{i}",
        )
        for i in range(6)
    ]
    return DecomposeReport(
        source_uid="tool-page",
        source_name="Tool Page",
        source_path=str(source_path),
        subject_until=r"^\S+",
        bullets=bullets,
    )


def _seed_page_split_pending(wiki_root: Path, source_path: Path) -> str:
    run_page_split_proposal_detection([_page_split_report(source_path)], wiki_root=wiki_root)
    from athenaeum.page_split_proposals import list_pending_page_split_proposals

    return list_pending_page_split_proposals(wiki_root)[0]["id"]


def _seed_auto_apply_pending(wiki_root: Path) -> str:
    for idx in range(4):
        human_verdict = "approve" if idx < 3 else "reject"
        proposal_id = f"prop-{idx}"
        sample_tier_decision(
            wiki_root,
            tier="T1",
            verdict="reject",
            proposal_id=proposal_id,
            reason="test",
            config=_FULL_SAMPLE_CONFIG,
        )
        audit_id = next(
            r["id"]
            for r in read_calibration_ledger(wiki_root)
            if r.get("kind") == "audit" and r.get("proposal_id") == proposal_id
        )
        record_audit_review(wiki_root, audit_id=audit_id, human_verdict=human_verdict)

    run_auto_apply_proposal_detection(
        ["not_a_conflict"], wiki_root=wiki_root, config=_AUTO_APPLY_CONFIG
    )
    from athenaeum.auto_apply_proposals import list_pending_auto_apply_threshold_proposals

    return list_pending_auto_apply_threshold_proposals(wiki_root)[0]["id"]


class TestListPendingDecisionsWiring:
    def test_owner_sees_both_types(self, wiki_root: Path) -> None:
        source_page = wiki_root / "tool-page.md"
        source_page.write_text("---\ntype: tool\n---\n\n- body\n", encoding="utf-8")
        _seed_page_split_pending(wiki_root, source_page)
        _seed_auto_apply_pending(wiki_root)

        decisions = list_pending_decisions(wiki_root, caller_audience=None)
        types = {d["type"] for d in decisions}
        assert "page-split" in types
        assert "auto-apply-threshold" in types

    def test_restricted_caller_sees_neither(self, wiki_root: Path) -> None:
        source_page = wiki_root / "tool-page.md"
        source_page.write_text("---\ntype: tool\n---\n\n- body\n", encoding="utf-8")
        _seed_page_split_pending(wiki_root, source_page)
        _seed_auto_apply_pending(wiki_root)

        decisions = list_pending_decisions(wiki_root, caller_audience=set())
        types = {d["type"] for d in decisions}
        assert "page-split" not in types
        assert "auto-apply-threshold" not in types


class TestValidateAnswerSchema:
    def test_page_split_accepts_approve_reject_only(self) -> None:
        assert validate_answer("page-split", {"verdict": "approve"}) == []
        assert validate_answer("page-split", {"verdict": "reject"}) == []
        assert validate_answer("page-split", {"verdict": "rename"}) != []
        assert validate_answer("page-split", {"verdict": "bogus"}) != []

    def test_auto_apply_threshold_accepts_approve_reject_only(self) -> None:
        assert validate_answer("auto-apply-threshold", {"verdict": "approve"}) == []
        assert validate_answer("auto-apply-threshold", {"verdict": "reject"}) == []
        assert validate_answer("auto-apply-threshold", {"verdict": "bogus"}) != []


class TestApplyDecisionAnswersEndToEnd:
    def test_page_split_approve_round_trips_and_page_is_unchanged(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        from athenaeum.page_split_proposals import list_pending_page_split_proposals

        source_page = wiki_root / "tool-page.md"
        original_bytes = b"---\ntype: tool\n---\n\n- body\n"
        source_page.write_bytes(original_bytes)
        item_id = _seed_page_split_pending(wiki_root, source_page)

        write_decision_answer(
            raw_root, decision_id=item_id, decision_type="page-split", verdict="approve"
        )
        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        assert report.skipped == 0
        assert list_pending_page_split_proposals(wiki_root) == []
        assert source_page.read_bytes() == original_bytes

    def test_page_split_reject_round_trips(self, wiki_root: Path, raw_root: Path) -> None:
        from athenaeum.page_split_proposals import list_pending_page_split_proposals

        source_page = wiki_root / "tool-page.md"
        source_page.write_bytes(b"---\ntype: tool\n---\n\n- body\n")
        item_id = _seed_page_split_pending(wiki_root, source_page)

        write_decision_answer(
            raw_root, decision_id=item_id, decision_type="page-split", verdict="reject"
        )
        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        assert list_pending_page_split_proposals(wiki_root) == []

    def test_page_split_unknown_id_is_fail_soft(self, wiki_root: Path, raw_root: Path) -> None:
        write_decision_answer(
            raw_root, decision_id="nope", decision_type="page-split", verdict="approve"
        )
        report = apply_decision_answers(wiki_root, raw_root)
        assert report.applied == 0
        assert report.skipped == 1
        assert report.outcomes[0].error_code == "id_not_found"

    def test_auto_apply_threshold_approve_round_trips(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        from athenaeum.auto_apply_proposals import list_pending_auto_apply_threshold_proposals
        from athenaeum.resolutions import resolve_auto_apply_threshold_for

        item_id = _seed_auto_apply_pending(wiki_root)
        before = resolve_auto_apply_threshold_for(
            _AUTO_APPLY_CONFIG, "not_a_conflict", wiki_root=wiki_root
        )

        write_decision_answer(
            raw_root, decision_id=item_id, decision_type="auto-apply-threshold", verdict="approve"
        )
        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        assert list_pending_auto_apply_threshold_proposals(wiki_root) == []
        after = resolve_auto_apply_threshold_for(
            _AUTO_APPLY_CONFIG, "not_a_conflict", wiki_root=wiki_root
        )
        assert after < before

    def test_auto_apply_threshold_reject_round_trips_and_does_not_widen(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        from athenaeum.auto_apply_proposals import list_pending_auto_apply_threshold_proposals
        from athenaeum.resolutions import resolve_auto_apply_threshold_for

        item_id = _seed_auto_apply_pending(wiki_root)
        before = resolve_auto_apply_threshold_for(
            _AUTO_APPLY_CONFIG, "not_a_conflict", wiki_root=wiki_root
        )

        write_decision_answer(
            raw_root, decision_id=item_id, decision_type="auto-apply-threshold", verdict="reject"
        )
        report = apply_decision_answers(wiki_root, raw_root)

        assert report.applied == 1
        assert list_pending_auto_apply_threshold_proposals(wiki_root) == []
        after = resolve_auto_apply_threshold_for(
            _AUTO_APPLY_CONFIG, "not_a_conflict", wiki_root=wiki_root
        )
        assert after == before

    def test_auto_apply_threshold_unknown_id_is_fail_soft(
        self, wiki_root: Path, raw_root: Path
    ) -> None:
        write_decision_answer(
            raw_root, decision_id="nope", decision_type="auto-apply-threshold", verdict="approve"
        )
        report = apply_decision_answers(wiki_root, raw_root)
        assert report.applied == 0
        assert report.skipped == 1
        assert report.outcomes[0].error_code == "id_not_found"
