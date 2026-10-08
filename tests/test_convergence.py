# SPDX-License-Identifier: Apache-2.0
"""Tests for the quarterly convergence report (issue athenaeum#2020 AC1/AC2)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from athenaeum.convergence import (
    READINGS,
    classify,
    compute_convergence_report,
)
from athenaeum.dimension_proposals import (
    APPROVE_KIND,
    DIMENSION_PROPOSALS_LEDGER_VERSION,
    default_dimension_proposals_ledger_path,
)
from athenaeum.runlock import RunLock
from athenaeum.store import append_line_durable
from athenaeum.verdicts import Basis, append_verdict, build_verdict_entry

_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _wiki_root(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


def _basis() -> Basis:
    return Basis(
        content_hashes=["a", "b"],
        coords=[None, None],
        coord_origins={},
        registry_epoch=1,
        tree_epoch=1,
        authority_basis=None,
        predicate_instrument=[None, None],
        comparator_version="v1.gate2",
    )


def _seed_underdetermined(
    wiki_root: Path, *, id_a: str, id_b: str, missing: list[str], at: str
) -> None:
    entry = build_verdict_entry(
        id_a,
        id_b,
        "underdetermined",
        basis=_basis(),
        missing=missing,
        at=at,
        decided_by="comparator",
    )
    with RunLock(wiki_root.parent) as lock:
        append_verdict(wiki_root, entry, lock=lock)


def _seed_approve(
    wiki_root: Path, *, item_id: str, answered_at: str, name: str = "jurisdiction"
) -> None:
    record = {
        "v": DIMENSION_PROPOSALS_LEDGER_VERSION,
        "kind": APPROVE_KIND,
        "id": item_id,
        "created_at": answered_at,
        "answered_at": answered_at,
        "name": name,
        "note": "",
    }
    target = default_dimension_proposals_ledger_path(wiki_root)
    append_line_durable(target, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))


class TestQuarterBucketing:
    def test_dense_fill_zero_quarters(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _seed_approve(wiki_root, item_id="p1", answered_at="2026-01-15T00:00:00+00:00")
        _seed_approve(wiki_root, item_id="p2", answered_at="2026-07-15T00:00:00+00:00")
        report = compute_convergence_report(wiki_root, now=_NOW)
        # Q1 and Q3 of 2026 were seeded; Q2 must appear as a dense zero.
        assert report.quarters == ["2026-Q1", "2026-Q2", "2026-Q3"]
        assert report.supply == {"2026-Q1": 1, "2026-Q2": 0, "2026-Q3": 1}

    def test_current_in_progress_quarter_is_excluded(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _seed_approve(wiki_root, item_id="p1", answered_at="2026-01-15T00:00:00+00:00")
        # _NOW is in 2026-Q4; this approval lands in the SAME (in-progress)
        # quarter and must never appear in the series.
        _seed_approve(wiki_root, item_id="p2", answered_at="2026-10-05T00:00:00+00:00")
        report = compute_convergence_report(wiki_root, now=_NOW)
        assert "2026-Q4" not in report.quarters
        assert report.supply == {"2026-Q1": 1}

    def test_rename_outcome_counts_toward_supply(self, tmp_path: Path) -> None:
        # decision_answers._apply_dimension_proposal_answer writes the SAME
        # APPROVE_KIND record for a 'rename' verdict as a plain 'approve' —
        # this module must count both identically.
        wiki_root = _wiki_root(tmp_path)
        _seed_approve(
            wiki_root, item_id="p1", answered_at="2026-01-15T00:00:00+00:00", name="renamed-axis"
        )
        _seed_approve(wiki_root, item_id="p2", answered_at="2026-04-15T00:00:00+00:00")
        report = compute_convergence_report(wiki_root, now=_NOW)
        assert sum(report.supply.values()) == 2


class TestDemandSeries:
    def test_unregistered_missing_dimension_counts(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _seed_underdetermined(
            wiki_root, id_a="a", id_b="b", missing=["jurisdiction"], at="2026-01-10"
        )
        report = compute_convergence_report(wiki_root, now=_NOW)
        assert report.demand.get("2026-Q1", 0) == 1

    def test_registered_missing_dimension_is_excluded(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _seed_underdetermined(wiki_root, id_a="a", id_b="b", missing=["scope"], at="2026-01-10")
        report = compute_convergence_report(wiki_root, now=_NOW)
        # "scope" is a kernel dimension -- always registered -- so this
        # verdict never contributes to demand.
        assert report.demand == {}

    def test_unwindowed_history_is_not_filtered(self, tmp_path: Path) -> None:
        # Deliberately NOT athenaeum.signal_mining's windowed clustering:
        # an entry far outside any 30-day window must still count toward
        # its own quarter.
        wiki_root = _wiki_root(tmp_path)
        _seed_underdetermined(
            wiki_root, id_a="a", id_b="b", missing=["jurisdiction"], at="2024-02-01"
        )
        report = compute_convergence_report(wiki_root, now=_NOW)
        assert report.demand.get("2024-Q1", 0) == 1

    def test_resolved_pair_is_not_unresolved(self, tmp_path: Path) -> None:
        # Re-verdicting the SAME pair to something other than
        # 'underdetermined' removes it from list_by_verdict's live view --
        # this report must not count the stale underdetermined history.
        wiki_root = _wiki_root(tmp_path)
        _seed_underdetermined(
            wiki_root, id_a="a", id_b="b", missing=["jurisdiction"], at="2026-01-10"
        )
        entry = build_verdict_entry(
            "a", "b", "distinct", basis=_basis(), at="2026-02-10", decided_by="comparator"
        )
        with RunLock(wiki_root.parent) as lock:
            append_verdict(wiki_root, entry, lock=lock)
        report = compute_convergence_report(wiki_root, now=_NOW)
        assert report.demand == {}


class TestClassifier:
    def test_insufficient_data_with_fewer_than_two_quarters(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _seed_approve(wiki_root, item_id="p1", answered_at="2026-01-15T00:00:00+00:00")
        report = compute_convergence_report(wiki_root, now=_NOW)
        assert report.reading == "insufficient_data"

    def test_both_falling_is_convergence(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        for i in range(3):
            _seed_approve(wiki_root, item_id=f"p{i}", answered_at="2026-01-15T00:00:00+00:00")
        _seed_approve(wiki_root, item_id="p-last", answered_at="2026-04-15T00:00:00+00:00")
        _seed_underdetermined(
            wiki_root, id_a="a1", id_b="b1", missing=["jurisdiction"], at="2026-01-10"
        )
        _seed_underdetermined(
            wiki_root, id_a="a2", id_b="b2", missing=["jurisdiction"], at="2026-01-11"
        )
        report = compute_convergence_report(wiki_root, now=_NOW)
        assert report.reading == "convergence"
        assert report.supply_trend == "falling"
        assert report.demand_trend == "falling"

    def test_supply_falling_demand_rising_is_abandonment(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        for i in range(3):
            _seed_approve(wiki_root, item_id=f"p{i}", answered_at="2026-01-15T00:00:00+00:00")
        _seed_underdetermined(
            wiki_root, id_a="a1", id_b="b1", missing=["jurisdiction"], at="2026-01-10"
        )
        for i in range(3):
            _seed_underdetermined(
                wiki_root,
                id_a=f"a{i}-later",
                id_b=f"b{i}-later",
                missing=["jurisdiction"],
                at="2026-04-10",
            )
        report = compute_convergence_report(wiki_root, now=_NOW)
        assert report.reading == "abandonment"

    def test_rising_supply_is_cyc_failure_mode(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _seed_approve(wiki_root, item_id="p1", answered_at="2026-01-15T00:00:00+00:00")
        for i in range(3):
            _seed_approve(
                wiki_root, item_id=f"p-later-{i}", answered_at="2026-04-15T00:00:00+00:00"
            )
        report = compute_convergence_report(wiki_root, now=_NOW)
        assert report.reading == "cyc_failure_mode"

    def test_classify_is_exhaustive_over_the_trend_table(self) -> None:
        for supply_trend in ("falling", "rising", "flat"):
            for demand_trend in ("falling", "rising", "flat"):
                reading = classify(supply_trend, demand_trend, enough_history=True)
                assert reading in READINGS
                assert reading != "insufficient_data"
        assert classify("falling", "falling", enough_history=False) == "insufficient_data"

    def test_empty_history_is_insufficient_data_not_cyc(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        report = compute_convergence_report(wiki_root, now=_NOW)
        assert report.reading == "insufficient_data"
        assert report.quarters == []


class TestReportShape:
    def test_to_dict_round_trips_every_field(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _seed_approve(wiki_root, item_id="p1", answered_at="2026-01-15T00:00:00+00:00")
        report = compute_convergence_report(wiki_root, now=_NOW)
        d = report.to_dict()
        assert set(d) == {
            "quarters",
            "supply",
            "demand",
            "supply_trend",
            "demand_trend",
            "reading",
            "explanation",
        }
        assert isinstance(d["explanation"], str) and d["explanation"]
