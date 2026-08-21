# SPDX-License-Identifier: Apache-2.0
"""Tests for the per-claim usage report (issue athenaeum#968, part 1).

Covers: aggregation from the push-metrics ledgers (pushed_count,
referenced_count, last_pushed, last_referenced), the ``since`` window, the
single-claim lookup interface issue athenaeum#718 must consume, ids-only output
shape, and that the report never mutates the underlying ledgers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from athenaeum import push_metrics
from athenaeum.usage_report import (
    compute_usage_report,
    get_claim_usage,
    usage_report_to_list,
)


def _seed_ledgers(cache_dir: Path) -> None:
    """Two pushes of id 'a' (sessions s1, s2), one push of id 'b' (s1); 'a'
    referenced once (s1 only), 'b' never referenced.
    """
    push_rec_1 = push_metrics.build_push_record(
        session_id="s1",
        query="q1",
        backend="fts5",
        hits=[("f.md", {"uid": "a"}, "snip"), ("g.md", {"uid": "b"}, "snip")],
    )
    push_metrics.record_push(push_rec_1, cache_dir=cache_dir)

    push_rec_2 = push_metrics.build_push_record(
        session_id="s2", query="q2", backend="fts5", hits=[("f.md", {"uid": "a"}, "snip")]
    )
    push_metrics.record_push(push_rec_2, cache_dir=cache_dir)

    ref_1 = push_metrics.ReferenceResult(
        session_id="s1",
        ts="2026-01-01T00:00:00Z",
        pushed_ids=["a", "b"],
        referenced_ids=["a"],
    )
    push_metrics.record_reference_result(ref_1, cache_dir=cache_dir)


class TestComputeUsageReport:
    def test_empty_ledgers_is_empty_report(self, tmp_path: Path) -> None:
        report = compute_usage_report(cache_dir=tmp_path)
        assert report == {}

    def test_aggregates_pushed_and_referenced_counts(self, tmp_path: Path) -> None:
        _seed_ledgers(tmp_path)
        report = compute_usage_report(cache_dir=tmp_path)

        assert report["a"].pushed_count == 2
        assert report["a"].referenced_count == 1
        assert report["b"].pushed_count == 1
        assert report["b"].referenced_count == 0

    def test_last_referenced_set_only_for_referenced_id(self, tmp_path: Path) -> None:
        _seed_ledgers(tmp_path)
        report = compute_usage_report(cache_dir=tmp_path)

        assert report["a"].last_referenced == "2026-01-01T00:00:00Z"
        assert report["b"].last_referenced is None

    def test_last_pushed_is_set_for_every_pushed_id(self, tmp_path: Path) -> None:
        _seed_ledgers(tmp_path)
        report = compute_usage_report(cache_dir=tmp_path)

        assert report["a"].last_pushed is not None
        assert report["b"].last_pushed is not None

    def test_id_pushed_but_never_referenced_has_honest_zero(self, tmp_path: Path) -> None:
        """Never a fabricated non-zero -- matches push_metrics' own
        "honest, not fabricated" convention for an unmeasured figure."""
        _seed_ledgers(tmp_path)
        report = compute_usage_report(cache_dir=tmp_path)
        assert report["b"].referenced_count == 0
        assert report["b"].last_referenced is None

    def test_since_window_excludes_old_records(self, tmp_path: Path) -> None:
        old_push = push_metrics.PushRecord(
            session_id="old",
            ts="2020-01-01T00:00:00Z",
            query_hash="deadbeef",
            backend="fts5",
            items=[
                push_metrics.PushedItem(
                    id="stale", tier="internal", scope="owner", token_cost=1
                )
            ],
        )
        push_metrics.record_push(old_push, cache_dir=tmp_path)

        report = compute_usage_report(
            cache_dir=tmp_path,
            since=datetime.now(tz=timezone.utc) - timedelta(days=1),
        )
        assert "stale" not in report

    def test_does_not_mutate_ledgers(self, tmp_path: Path) -> None:
        _seed_ledgers(tmp_path)
        before = push_metrics.read_push_records(tmp_path)
        compute_usage_report(cache_dir=tmp_path)
        after = push_metrics.read_push_records(tmp_path)
        assert before == after


class TestGetClaimUsage:
    def test_returns_usage_for_known_claim(self, tmp_path: Path) -> None:
        _seed_ledgers(tmp_path)
        usage = get_claim_usage("a", cache_dir=tmp_path)
        assert usage is not None
        assert usage.pushed_count == 2
        assert usage.referenced_count == 1

    def test_returns_none_for_unseen_claim(self, tmp_path: Path) -> None:
        _seed_ledgers(tmp_path)
        assert get_claim_usage("never-pushed", cache_dir=tmp_path) is None

    def test_empty_ledgers_returns_none(self, tmp_path: Path) -> None:
        assert get_claim_usage("a", cache_dir=tmp_path) is None


class TestUsageReportToList:
    def test_ids_only_shape(self, tmp_path: Path) -> None:
        _seed_ledgers(tmp_path)
        report = compute_usage_report(cache_dir=tmp_path)
        rows = usage_report_to_list(report)
        for row in rows:
            assert set(row) == {
                "id",
                "pushed_count",
                "referenced_count",
                "last_pushed",
                "last_referenced",
            }

    def test_sorted_by_id(self, tmp_path: Path) -> None:
        _seed_ledgers(tmp_path)
        report = compute_usage_report(cache_dir=tmp_path)
        rows = usage_report_to_list(report)
        assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
