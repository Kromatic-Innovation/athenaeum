# SPDX-License-Identifier: Apache-2.0
"""Tests for the backlog price sheet + sensitivity table (issue athenaeum#713, artifact 2)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from athenaeum import backlog_price_sheet as bps
from athenaeum.run_summary_log import parse_run_summary_text


def _write_raw_files(raw_root: Path, source: str, count: int) -> None:
    src_dir = raw_root / source
    src_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (src_dir / f"2026080{i}T000000Z-{'a1b2c3d' + str(i % 10)}.md").write_text(f"content {i}")


def _write_ledger(records: list[dict]) -> None:
    """Write *records* to the ACTIVE ledger path.

    ``athenaeum.spend.resolve_ledger_path`` checks ``ATHENAEUM_SPEND_LEDGER``
    before any explicit ``cache_dir`` argument (by design — see
    ``resolve_spend_ledger_path``'s docstring, a test/relocation seam), and
    ``tests/conftest.py``'s autouse ``_isolate_cache_dir`` fixture always sets
    that env var to a per-test tmp path. So the ledger a test seeds must be
    written to THAT resolved path, not an arbitrary ``cache_dir`` a test
    invents — otherwise ``build_price_sheet``'s read silently misses it.
    """
    from athenaeum.spend import resolve_ledger_path

    path = resolve_ledger_path(None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))


def _ledger_record(
    *, files_processed: int, api_calls: int, input_tokens: int, output_tokens: int
) -> dict:
    return {
        "v": 1,
        "ts": "2026-07-01T00:00:00Z",
        "run_type": "librarian",
        "provider": "anthropic",
        "files_processed": files_processed,
        "api_calls": api_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_usd": 0.0,
    }


class TestSensitivityTable:
    def test_matches_issue_worked_example(self) -> None:
        rows = bps.sensitivity_table(3644, rates_per_100=(10, 50))
        by_rate = {r.rate_per_100: r for r in rows}
        assert by_rate[10].decisions == 364
        assert by_rate[10].days_to_terminal_disposition == 18
        assert by_rate[50].decisions == 1822
        assert by_rate[50].days_to_terminal_disposition == 91

    def test_flags_six_month_breach(self) -> None:
        rows = bps.sensitivity_table(
            100_000, rates_per_100=(5,), human_daily_budget=20, six_month_days=182
        )
        assert rows[0].breaches_six_month_horizon is True

    def test_zero_backlog_is_zero_days(self) -> None:
        rows = bps.sensitivity_table(0, rates_per_100=(10,))
        assert rows[0].decisions == 0
        assert rows[0].days_to_terminal_disposition == 0
        assert rows[0].breaches_six_month_horizon is False

    def test_zero_daily_budget_is_infinite_days(self) -> None:
        rows = bps.sensitivity_table(100, rates_per_100=(10,), human_daily_budget=0)
        assert math.isinf(rows[0].days_to_terminal_disposition)

    def test_default_range_covers_5_to_50(self) -> None:
        assert bps.DEFAULT_INFLOW_RATES_PER_100[0] == 5
        assert bps.DEFAULT_INFLOW_RATES_PER_100[-1] == 50


class TestBuildPriceSheet:
    def test_recounts_backlog_from_raw_tree(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        _write_raw_files(root / "raw", "source-a", 7)
        result = bps.build_price_sheet(root)
        assert result.backlog_count == 7

    def test_calls_and_tokens_per_file_come_from_ledger_when_present(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "knowledge"
        _write_raw_files(root / "raw", "source-a", 2)
        _write_ledger(
            [_ledger_record(files_processed=2, api_calls=40, input_tokens=2000, output_tokens=200)],
        )
        result = bps.build_price_sheet(root)
        assert result.calls_per_file == 20.0
        assert result.calls_per_file_source == "ledger"
        assert result.avg_input_tokens_per_file == 1000.0
        assert result.tokens_source == "ledger"

    def test_falls_back_honestly_when_no_ledger_history(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        _write_raw_files(root / "raw", "source-a", 1)
        result = bps.build_price_sheet(root)
        assert result.calls_per_file is None
        assert "none" in result.calls_per_file_source
        assert "NOT a measured figure" in result.tokens_source
        assert result.avg_input_tokens_per_file == bps.DEFAULT_AVG_INPUT_TOKENS_PER_FILE

    def test_wall_clock_is_n_a_without_a_summary_log(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        _write_raw_files(root / "raw", "source-a", 1)
        result = bps.build_price_sheet(root)
        assert result.wall_clock_per_file_seconds is None
        assert result.wall_clock_without_prefilter_seconds is None

    def test_wall_clock_derived_from_summary_log_when_given(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        _write_raw_files(root / "raw", "source-a", 10)
        log_text = "librarian-run-summary total_secs=100 | entity secs=100 files=5"
        records = parse_run_summary_text(log_text)
        result = bps.build_price_sheet(
            root, summary_log_records=records
        )
        assert result.wall_clock_per_file_seconds == 20.0
        assert result.wall_clock_without_prefilter_seconds == 200.0  # 20 * 10 files
        assert result.wall_clock_source == "run-summary log (entity phase)"

    def test_prefilter_column_is_n_a_without_operator_supplied_fraction(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "knowledge"
        _write_raw_files(root / "raw", "source-a", 3)
        result = bps.build_price_sheet(root)
        assert result.prefilter_excluded_fraction is None
        assert result.cost_with_prefilter_usd is None

    def test_prefilter_column_computed_when_fraction_supplied(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        _write_raw_files(root / "raw", "source-a", 100)
        result = bps.build_price_sheet(
            root, prefilter_excluded_fraction=0.5
        )
        assert result.cost_with_prefilter_usd is not None
        assert result.cost_with_prefilter_usd == pytest.approx(
            result.cost_without_prefilter_usd * 0.5, rel=0.01
        )

    def test_render_includes_both_narrative_notes_and_the_table(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        _write_raw_files(root / "raw", "source-a", 5)
        result = bps.build_price_sheet(root)
        text = bps.render_snapshot_entry(result)
        assert "QUEUE CAPACITY" in text
        assert "Triage valve" in text
        assert "| rate/100 compiled | decisions | days | breaches 6mo |" in text
        assert "raw_backlog_count: 5 (re-counted, not copied)" in text


class TestWriteSnapshot:
    def test_refuses_on_empty_backlog(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = bps.build_price_sheet(root)
        with pytest.raises(ValueError, match="backlog_count=0"):
            bps.write_snapshot(result, docs_path=tmp_path / "measurements.md")

    def test_writes_snapshot(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        _write_raw_files(root / "raw", "source-a", 4)
        result = bps.build_price_sheet(root)
        docs_path = tmp_path / "docs" / "measurements.md"
        bps.write_snapshot(result, docs_path=docs_path)
        text = docs_path.read_text(encoding="utf-8")
        assert bps.SECTION_HEADING in text
        assert "raw_backlog_count: 4" in text
