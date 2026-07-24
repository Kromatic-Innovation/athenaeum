# SPDX-License-Identifier: Apache-2.0
"""Tests for the tier audit sampler + calibration ledger (issue #438).

The calibration loop for the tiered reasoning pass: a random audit share of
T1 rejects and T2 approvals is surfaced for human confirm/overturn review.

Acceptance criteria under test:

1. With seeded (deterministic) randomness the sampler selects the expected
   items; sampled audit items are distinguishable from ordinary escalations
   in ``list_pending_decisions`` (``type: "audit"``).
2. A human overturn on an audit item is recorded and reflected in the
   calibration summary; a confirm leaves the original decision untouched.
3. Sampling rates are config-resolvable with the standard ``librarian.*``
   precedence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.calibration import (
    audit_item_id,
    calibration_summary,
    list_pending_audit,
    read_calibration_ledger,
    record_audit_review,
    sample_probability,
    sample_tier_decision,
    should_sample,
)
from athenaeum.config import (
    resolve_audit_sample_rate_t1_rejects,
    resolve_audit_sample_rate_t2_approvals,
)
from athenaeum.decisions import list_pending_decisions


def _sample_all_config() -> dict:
    return {
        "librarian": {
            "audit_sample_rate_t1_rejects": 1.0,
            "audit_sample_rate_t2_approvals": 1.0,
        }
    }


# ---------------------------------------------------------------------------
# AC 1 — deterministic sampling selects the expected items; watched verdicts.
# ---------------------------------------------------------------------------


class TestSampling:
    def test_probability_is_deterministic_and_bounded(self) -> None:
        p = sample_probability("T1", "prop-1")
        assert p == sample_probability("T1", "prop-1")
        assert 0.0 <= p < 1.0
        # Tier is part of the key, so the same proposal differs across tiers.
        assert sample_probability("T1", "prop-1") != sample_probability("T2", "prop-1")

    def test_rate_zero_samples_nothing_rate_one_samples_all(self, tmp_path: Path) -> None:
        assert should_sample("T1", "x", rate=0.0) is False
        assert should_sample("T1", "x", rate=1.0) is True

    def test_sampler_selects_exactly_the_expected_items(self, tmp_path: Path) -> None:
        # "Seeded randomness": at a fixed rate, sample_tier_decision must fire
        # for exactly those proposals should_sample marks — no more, no less.
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        rate = 0.5
        config = {"librarian": {"audit_sample_rate_t1_rejects": rate}}
        expected_sampled = set()
        for i in range(40):
            pid = f"prop-{i}"
            rec = sample_tier_decision(
                wiki, tier="T1", verdict="reject", proposal_id=pid, reason="r", config=config
            )
            if should_sample("T1", pid, rate=rate):
                assert rec is not None, pid
                expected_sampled.add(pid)
            else:
                assert rec is None, pid
        # Ledger holds exactly the expected set.
        ledger_ids = {r["proposal_id"] for r in read_calibration_ledger(wiki)}
        assert ledger_ids == expected_sampled
        # And a non-trivial split actually happened (guards a degenerate all/none).
        assert 0 < len(expected_sampled) < 40

    def test_only_watched_verdicts_are_sampled(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        config = _sample_all_config()  # rate 1.0 — sample everything watched
        # Watched: T1 reject, T2 approve.
        assert sample_tier_decision(
            wiki, tier="T1", verdict="reject", proposal_id="a", reason="r", config=config
        ) is not None
        assert sample_tier_decision(
            wiki, tier="T2", verdict="approve", proposal_id="b", reason="r", config=config
        ) is not None
        # Not watched: T1 pass_up, T2 escalate/amend/draft.
        for tier, verdict in (
            ("T1", "pass_up"),
            ("T2", "escalate"),
            ("T2", "amend"),
            ("T2", "draft"),
        ):
            assert sample_tier_decision(
                wiki, tier=tier, verdict=verdict, proposal_id="z", reason="r", config=config
            ) is None

    def test_sampling_is_idempotent(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        config = _sample_all_config()
        first = sample_tier_decision(
            wiki, tier="T2", verdict="approve", proposal_id="p", reason="r", config=config
        )
        assert first is not None
        second = sample_tier_decision(
            wiki, tier="T2", verdict="approve", proposal_id="p", reason="r", config=config
        )
        assert second is None  # already sampled
        assert len([r for r in read_calibration_ledger(wiki) if r["kind"] == "audit"]) == 1

    def test_audit_item_surfaces_in_decisions_distinct_from_escalations(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # An ordinary escalation (a pending question) coexists in the queue.
        (wiki / "_pending_questions.md").write_text(
            "# Pending Questions\n\n"
            '## [2026-07-01] Entity: "Acme" (from s/x.md)\n'
            "- [ ] Is Acme Series A?\n"
            "**Conflict type**: principled\n"
            "**Description**: two conflicting statements\n",
            encoding="utf-8",
        )
        sample_tier_decision(
            wiki,
            tier="T2",
            verdict="approve",
            proposal_id="prop-9",
            reason="looked safe",
            config=_sample_all_config(),
        )
        decisions = list_pending_decisions(wiki)
        types = {d["type"] for d in decisions}
        assert "question" in types and "audit" in types  # distinguishable
        audit = next(d for d in decisions if d["type"] == "audit")
        assert audit["payload"]["tier"] == "T2"
        assert audit["payload"]["verdict"] == "approve"
        assert audit["payload"]["proposal_id"] == "prop-9"
        assert audit["id"] == audit_item_id("T2", "prop-9")
        assert audit["confidence"] is None


# ---------------------------------------------------------------------------
# AC 2 — overturn recorded + reflected in summary; confirm leaves untouched.
# ---------------------------------------------------------------------------


class TestReviewAndSummary:
    def _seed_audit(self, wiki: Path, *, tier: str, verdict: str, pid: str) -> str:
        rec = sample_tier_decision(
            wiki, tier=tier, verdict=verdict, proposal_id=pid, reason="r",
            config=_sample_all_config(),
        )
        assert rec is not None
        return rec["id"]

    def test_overturn_recorded_and_in_summary(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        audit_id = self._seed_audit(wiki, tier="T2", verdict="approve", pid="p1")
        # Human disagrees with the approve → overturn.
        review = record_audit_review(wiki, audit_id=audit_id, human_verdict="reject")
        assert review["overturned"] is True
        assert review["tier"] == "T2"

        summary = calibration_summary(wiki)
        assert summary["T2"] == {"sampled": 1, "reviewed": 1, "overturned": 1}
        assert summary["T1"] == {"sampled": 0, "reviewed": 0, "overturned": 0}
        # A reviewed item leaves the pending queue.
        assert list_pending_audit(wiki) == []

    def test_confirm_leaves_original_untouched(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        audit_id = self._seed_audit(wiki, tier="T1", verdict="reject", pid="p2")
        review = record_audit_review(wiki, audit_id=audit_id, human_verdict="reject")
        assert review["overturned"] is False
        summary = calibration_summary(wiki)
        assert summary["T1"] == {"sampled": 1, "reviewed": 1, "overturned": 0}
        # No merge sidecars were written — a review is a calibration signal only.
        assert not (wiki / "_pending_merges.md").exists()

    def test_double_review_and_unknown_id_raise(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        audit_id = self._seed_audit(wiki, tier="T2", verdict="approve", pid="p3")
        record_audit_review(wiki, audit_id=audit_id, human_verdict="approve")
        with pytest.raises(ValueError):
            record_audit_review(wiki, audit_id=audit_id, human_verdict="reject")
        with pytest.raises(ValueError):
            record_audit_review(wiki, audit_id="nope", human_verdict="reject")

    def test_summary_empty_ledger_is_zeroed_both_tiers(self, tmp_path: Path) -> None:
        assert calibration_summary(tmp_path / "wiki") == {
            "T1": {"sampled": 0, "reviewed": 0, "overturned": 0},
            "T2": {"sampled": 0, "reviewed": 0, "overturned": 0},
        }


# ---------------------------------------------------------------------------
# AC 3 — sampling rates config-resolvable (env > yaml > default), clamped.
# ---------------------------------------------------------------------------


class TestConfigResolution:
    def test_default_is_midpoint_of_band(self) -> None:
        assert resolve_audit_sample_rate_t1_rejects(None) == 0.075
        assert resolve_audit_sample_rate_t2_approvals(None) == 0.075

    def test_yaml_overrides_default(self) -> None:
        cfg = {"librarian": {"audit_sample_rate_t2_approvals": 0.1}}
        assert resolve_audit_sample_rate_t2_approvals(cfg) == 0.1

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_AUDIT_SAMPLE_RATE_T1_REJECTS", "0.2")
        cfg = {"librarian": {"audit_sample_rate_t1_rejects": 0.1}}
        assert resolve_audit_sample_rate_t1_rejects(cfg) == 0.2

    def test_out_of_range_is_clamped(self) -> None:
        assert resolve_audit_sample_rate_t1_rejects(
            {"librarian": {"audit_sample_rate_t1_rejects": 5.0}}
        ) == 1.0
        assert resolve_audit_sample_rate_t2_approvals(
            {"librarian": {"audit_sample_rate_t2_approvals": -1.0}}
        ) == 0.0

    def test_bool_and_garbage_fall_through_to_default(self) -> None:
        assert resolve_audit_sample_rate_t1_rejects(
            {"librarian": {"audit_sample_rate_t1_rejects": True}}
        ) == 0.075
        assert resolve_audit_sample_rate_t2_approvals(
            {"librarian": {"audit_sample_rate_t2_approvals": "nan-nope"}}
        ) == 0.075


# ---------------------------------------------------------------------------
# Ledger reader tolerance.
# ---------------------------------------------------------------------------


class TestLedgerReader:
    def test_missing_ledger_returns_empty(self, tmp_path: Path) -> None:
        assert read_calibration_ledger(tmp_path / "wiki") == []

    def test_reader_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_calibration.jsonl").write_text(
            '{"kind":"audit","id":"a"}\n{"kind":"audit","id":',  # torn 2nd line
            encoding="utf-8",
        )
        recs = read_calibration_ledger(wiki)
        assert len(recs) == 1
        assert recs[0]["id"] == "a"
