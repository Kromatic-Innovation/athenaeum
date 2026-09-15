# SPDX-License-Identifier: Apache-2.0
"""Stale-page review queue + bounded nightly re-audit (issue athenaeum#1630).

Covers: the stale-page report's staleness rules (missing/aged/behind-version
``last_audited``/``audit_version``) and sort order (never-audited first,
then oldest, ties broken by usage); the nightly drain's page cap, its
spend-share budget cutoff (stops early, records the remainder skipped for
budget), and that it does not run at all when its config key is absent;
and the ``athenaeum audit --stale`` CLI report (table + JSON). All fixtures
— no test reads or writes a live knowledge store.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from athenaeum import push_metrics
from athenaeum.audit import AUDIT_VERSION
from athenaeum.audit_queue import (
    NightlyDrainSummary,
    compute_stale_pages,
    estimate_page_audit_cost_usd,
    render_stale_table,
    run_nightly_drain,
)
from athenaeum.config import DEFAULT_CLASSIFY_MODEL
from athenaeum.models import parse_frontmatter

_NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def _page(root: Path, name: str, uid: str, *, extra: str = "", body: str = "Body.\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nuid: {uid}\ntype: concept\nname: {uid}\n{extra}---\n{body}",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "wiki"
    root.mkdir(parents=True)
    return root


# --------------------------------------------------------------------------- #
# AC1/AC2/AC3: stale-page report -- staleness rules + sort order
# --------------------------------------------------------------------------- #


class TestComputeStalePages:
    def test_never_audited_and_aged_are_listed_fresh_is_not(self, wiki: Path) -> None:
        """AC1: pages with no last_audited, one 400-day-old last_audited, and
        one 2-day-old last_audited, stale_after_days=30 -- the report lists
        the first two, never-audited first, and excludes the fresh one."""
        _page(wiki, "never.md", "never1")
        _page(
            wiki,
            "old.md",
            "old1",
            extra=f"last_audited: '2025-08-11T00:00:00Z'\naudit_version: {AUDIT_VERSION}\n",
        )
        _page(
            wiki,
            "fresh.md",
            "fresh1",
            extra=f"last_audited: '2026-09-13T00:00:00Z'\naudit_version: {AUDIT_VERSION}\n",
        )

        entries = compute_stale_pages(
            wiki, stale_after_days=30, now=_NOW, cache_dir=wiki.parent / "cache"
        )

        assert [e.uid for e in entries] == ["never1", "old1"]
        assert entries[0].reason == "never-audited"
        assert entries[1].reason == "stale-age"

    def test_stale_version_listed_even_with_recent_last_audited(self, wiki: Path) -> None:
        """AC3: audit_version behind AUDIT_VERSION is stale regardless of
        how recent last_audited is."""
        _page(
            wiki,
            "behind.md",
            "behind1",
            extra="last_audited: '2026-09-14T00:00:00Z'\naudit_version: audit-v0\n",
        )
        _page(
            wiki,
            "current.md",
            "current1",
            extra=f"last_audited: '2026-09-14T00:00:00Z'\naudit_version: {AUDIT_VERSION}\n",
        )

        entries = compute_stale_pages(
            wiki, stale_after_days=30, now=_NOW, cache_dir=wiki.parent / "cache"
        )

        assert [e.uid for e in entries] == ["behind1"]
        assert entries[0].reason == "stale-version"

    def test_tie_broken_by_higher_referenced_count_first(self, wiki: Path) -> None:
        """AC2: among two pages with the SAME last_audited, the one with the
        higher referenced_count sorts first."""
        _page(
            wiki,
            "a.md",
            "a1",
            extra="last_audited: '2025-01-01T00:00:00Z'\naudit_version: audit-v1\n",
        )
        _page(
            wiki,
            "b.md",
            "b1",
            extra="last_audited: '2025-01-01T00:00:00Z'\naudit_version: audit-v1\n",
        )
        cache_dir = wiki.parent / "cache"
        # a1 referenced twice, b1 referenced once.
        push_metrics.record_push(
            push_metrics.build_push_record(
                session_id="s1",
                query="q",
                backend="fts5",
                hits=[
                    ("a.md", {"uid": "a1"}, push_metrics.estimate_tokens("x")),
                ]
            ),
            cache_dir=cache_dir,
        )
        push_metrics.record_reference_result(
            push_metrics.ReferenceResult(
                session_id="s1", ts="2026-01-01T00:00:00Z", pushed_ids=["a1"], referenced_ids=["a1"]
            ),
            cache_dir=cache_dir,
        )
        push_metrics.record_reference_result(
            push_metrics.ReferenceResult(
                session_id="s2", ts="2026-01-02T00:00:00Z", pushed_ids=["a1"], referenced_ids=["a1"]
            ),
            cache_dir=cache_dir,
        )
        push_metrics.record_reference_result(
            push_metrics.ReferenceResult(
                session_id="s3", ts="2026-01-01T00:00:00Z", pushed_ids=["b1"], referenced_ids=["b1"]
            ),
            cache_dir=cache_dir,
        )

        entries = compute_stale_pages(
            wiki, stale_after_days=30, now=_NOW, cache_dir=cache_dir
        )

        assert [e.uid for e in entries] == ["a1", "b1"]
        assert entries[0].referenced_count == 2
        assert entries[1].referenced_count == 1

    def test_never_audited_pages_sort_before_aged_ones(self, wiki: Path) -> None:
        _page(
            wiki,
            "old.md",
            "old1",
            extra="last_audited: '2020-01-01T00:00:00Z'\naudit_version: audit-v1\n",
        )
        _page(wiki, "never.md", "never1")

        entries = compute_stale_pages(
            wiki, stale_after_days=30, now=_NOW, cache_dir=wiki.parent / "cache"
        )
        assert [e.uid for e in entries] == ["never1", "old1"]

    def test_render_stale_table_empty(self) -> None:
        assert render_stale_table([]) == "0 stale page(s)"

    def test_render_stale_table_lists_every_entry(self, wiki: Path) -> None:
        _page(wiki, "never.md", "never1")
        entries = compute_stale_pages(
            wiki, stale_after_days=30, now=_NOW, cache_dir=wiki.parent / "cache"
        )
        table = render_stale_table(entries)
        assert "never1" in table
        assert "reason=never-audited" in table

    def test_to_dict_is_json_serializable(self, wiki: Path) -> None:
        _page(wiki, "never.md", "never1")
        entries = compute_stale_pages(
            wiki, stale_after_days=30, now=_NOW, cache_dir=wiki.parent / "cache"
        )
        json.dumps([e.to_dict() for e in entries])  # raises if not serializable


# --------------------------------------------------------------------------- #
# Fake batch transport -- same shape as tests/test_audit.py's own fixture
# --------------------------------------------------------------------------- #


def _make_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class _FakeBatches:
    def __init__(self) -> None:
        self.submitted: list[list[dict[str, Any]]] = []

    def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
        self.submitted.append(list(requests))
        return SimpleNamespace(id=f"batch{len(self.submitted)}", processing_status="ended")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=batch_id, processing_status="ended")

    def results(self, batch_id: str):
        idx = int(batch_id.replace("batch", "")) - 1
        for req in self.submitted[idx]:
            yield SimpleNamespace(
                custom_id=req["custom_id"],
                result=SimpleNamespace(
                    type="succeeded",
                    message=_make_response(
                        json.dumps({"retirement_candidate": False, "retirement_reason": ""})
                    ),
                ),
            )


class _FakeBatchClient:
    def __init__(self) -> None:
        self.batches = _FakeBatches()
        self.messages = SimpleNamespace(batches=self.batches)


def _seed_stale_pages(wiki: Path, n: int) -> None:
    for i in range(n):
        _page(wiki, f"p{i}.md", f"uid{i}")


# --------------------------------------------------------------------------- #
# AC "does not run when config key is absent"
# --------------------------------------------------------------------------- #


class TestNightlyDrainGate:
    def test_returns_none_when_nightly_max_pages_absent(self, wiki: Path) -> None:
        _seed_stale_pages(wiki, 3)
        summary = run_nightly_drain(
            wiki,
            client=_FakeBatchClient(),
            model=DEFAULT_CLASSIFY_MODEL,
            config={},
            now=_NOW,
            cache_dir=wiki.parent / "cache",
        )
        assert summary is None

    def test_returns_none_when_config_is_none(self, wiki: Path) -> None:
        _seed_stale_pages(wiki, 3)
        summary = run_nightly_drain(
            wiki,
            client=_FakeBatchClient(),
            model=DEFAULT_CLASSIFY_MODEL,
            config=None,
            now=_NOW,
            cache_dir=wiki.parent / "cache",
        )
        assert summary is None


# --------------------------------------------------------------------------- #
# AC4: nightly_max_pages=3, queue of 10 -> exactly 3 re-audited
# --------------------------------------------------------------------------- #


class TestNightlyDrainMaxPages:
    def test_drains_exactly_max_pages(self, wiki: Path) -> None:
        _seed_stale_pages(wiki, 10)
        config = {"audit": {"nightly_max_pages": 3}}
        summary = run_nightly_drain(
            wiki,
            client=_FakeBatchClient(),
            model=DEFAULT_CLASSIFY_MODEL,
            config=config,
            now=_NOW,
            cache_dir=wiki.parent / "cache",
        )
        assert summary is not None
        assert summary.stale_queue_size == 10
        assert summary.reaudited == 3
        assert summary.skipped_budget == 0
        assert summary.stale_remaining == 7

        stamped = 0
        for i in range(10):
            meta, _body = parse_frontmatter((wiki / f"p{i}.md").read_text(encoding="utf-8"))
            if meta.get("last_audited"):
                stamped += 1
                assert meta.get("audit_version") == AUDIT_VERSION
        assert stamped == 3

    def test_no_client_reports_no_client_and_stamps_nothing(self, wiki: Path) -> None:
        _seed_stale_pages(wiki, 3)
        config = {"audit": {"nightly_max_pages": 3}}
        summary = run_nightly_drain(
            wiki,
            client=None,
            model=DEFAULT_CLASSIFY_MODEL,
            config=config,
            now=_NOW,
            cache_dir=wiki.parent / "cache",
        )
        assert summary is not None
        assert summary.reason == "no-client"
        assert summary.reaudited == 0
        for i in range(3):
            meta, _body = parse_frontmatter((wiki / f"p{i}.md").read_text(encoding="utf-8"))
            assert meta.get("last_audited") is None

    def test_empty_queue_reports_empty_queue(self, wiki: Path) -> None:
        config = {"audit": {"nightly_max_pages": 3}}
        summary = run_nightly_drain(
            wiki,
            client=_FakeBatchClient(),
            model=DEFAULT_CLASSIFY_MODEL,
            config=config,
            now=_NOW,
            cache_dir=wiki.parent / "cache",
        )
        assert summary is not None
        assert summary.reason == "empty-queue"
        assert summary.reaudited == 0


# --------------------------------------------------------------------------- #
# AC5: a budget share that allows only 1 page -> 1 re-audited, rest skipped
# --------------------------------------------------------------------------- #


class TestNightlyDrainBudget:
    def test_budget_allows_exactly_one_page(self, wiki: Path) -> None:
        _seed_stale_pages(wiki, 5)
        meta = {"uid": "uid0", "type": "concept", "name": "uid0"}
        one_page_cost = estimate_page_audit_cost_usd(
            meta, "Body.\n", model=DEFAULT_CLASSIFY_MODEL, max_tokens=1024
        )
        config = {
            "audit": {"nightly_max_pages": 5, "nightly_spend_share": 1.0},
            "spend": {"max_usd_per_day": one_page_cost * 1.5},
        }
        summary = run_nightly_drain(
            wiki,
            client=_FakeBatchClient(),
            model=DEFAULT_CLASSIFY_MODEL,
            config=config,
            now=_NOW,
            cache_dir=wiki.parent / "cache",
        )
        assert summary is not None
        assert summary.reaudited == 1
        assert summary.skipped_budget == 4
        assert summary.stale_remaining == 4

    def test_zero_budget_share_of_zero_cap_skips_everything(self, wiki: Path) -> None:
        """A daily cap so small no page fits -- every candidate is skipped
        for budget, none re-audited, and nothing is stamped."""
        _seed_stale_pages(wiki, 4)
        config = {
            "audit": {"nightly_max_pages": 4, "nightly_spend_share": 1.0},
            "spend": {"max_usd_per_day": 0.0000001},
        }
        summary = run_nightly_drain(
            wiki,
            client=_FakeBatchClient(),
            model=DEFAULT_CLASSIFY_MODEL,
            config=config,
            now=_NOW,
            cache_dir=wiki.parent / "cache",
        )
        assert summary is not None
        assert summary.reaudited == 0
        assert summary.skipped_budget == 4
        for i in range(4):
            meta, _body = parse_frontmatter((wiki / f"p{i}.md").read_text(encoding="utf-8"))
            assert meta.get("last_audited") is None

    def test_no_daily_ceiling_configured_means_unlimited(self, wiki: Path) -> None:
        """Unset spend.max_usd_per_day -- the nightly_spend_share knob does
        nothing (mirrors every other share-of-an-unset-ceiling knob)."""
        _seed_stale_pages(wiki, 3)
        config = {"audit": {"nightly_max_pages": 3, "nightly_spend_share": 0.0001}}
        summary = run_nightly_drain(
            wiki,
            client=_FakeBatchClient(),
            model=DEFAULT_CLASSIFY_MODEL,
            config=config,
            now=_NOW,
            cache_dir=wiki.parent / "cache",
        )
        assert summary is not None
        assert summary.reaudited == 3
        assert summary.skipped_budget == 0


# --------------------------------------------------------------------------- #
# Estimate helper
# --------------------------------------------------------------------------- #


class TestEstimatePageAuditCost:
    def test_longer_body_costs_more(self) -> None:
        meta = {"uid": "u", "type": "concept", "name": "u"}
        short = estimate_page_audit_cost_usd(
            meta, "short.\n", model=DEFAULT_CLASSIFY_MODEL, max_tokens=1024
        )
        long_body = "word " * 2000
        long = estimate_page_audit_cost_usd(
            meta, long_body, model=DEFAULT_CLASSIFY_MODEL, max_tokens=1024
        )
        assert long > short

    def test_positive_for_a_normal_page(self) -> None:
        meta = {"uid": "u", "type": "concept", "name": "u"}
        cost = estimate_page_audit_cost_usd(
            meta, "Body.\n", model=DEFAULT_CLASSIFY_MODEL, max_tokens=1024
        )
        assert cost > 0.0


# --------------------------------------------------------------------------- #
# NightlyDrainSummary.to_dict()
# --------------------------------------------------------------------------- #


class TestNightlyDrainSummary:
    def test_to_dict_is_json_serializable(self) -> None:
        summary = NightlyDrainSummary(
            stale_queue_size=5,
            candidates_considered=3,
            reaudited=2,
            skipped_budget=1,
            failed=0,
            stale_remaining=3,
            cost_usd=0.001,
            reason="completed",
        )
        json.dumps(summary.to_dict())
