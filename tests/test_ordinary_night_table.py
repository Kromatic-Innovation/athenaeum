# SPDX-License-Identifier: Apache-2.0
"""Tests for the ordinary-night steady-state table (issue athenaeum#713, artifact 3)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum import ordinary_night_table as ont
from athenaeum.run_summary_log import parse_run_summary_text


def _hex8(i: int) -> str:
    return f"{i:08x}"


def _write_raw_file_at(raw_root: Path, source: str, ts: datetime, idx: int) -> None:
    src_dir = raw_root / source
    src_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    (src_dir / f"{stamp}-{_hex8(idx)}.md").write_text("content")


def _write_ledger(records: list[dict]) -> None:
    """Write *records* to the ACTIVE ledger path — see the identical helper
    (and its docstring explaining why) in ``tests/test_backlog_price_sheet.py``."""
    from athenaeum.spend import resolve_ledger_path

    path = resolve_ledger_path(None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))


class TestMeasureFilesPerDay:
    def test_counts_only_files_in_window(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        # 3 files within the last 3 days, 1 file 30 days old (outside a 14d window).
        _write_raw_file_at(root / "raw", "s", now - timedelta(days=1), 1)
        _write_raw_file_at(root / "raw", "s", now - timedelta(days=2), 2)
        _write_raw_file_at(root / "raw", "s", now - timedelta(days=3), 3)
        _write_raw_file_at(root / "raw", "s", now - timedelta(days=30), 4)

        rate, count = ont.measure_files_per_day(root, window_days=14, now=now)
        assert count == 3
        assert rate == pytest.approx(3 / 14)

    def test_unparseable_timestamps_are_excluded(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        src = root / "raw" / "s"
        src.mkdir(parents=True)
        (src / "not-a-timestamped-file.md").write_text("x")
        rate, count = ont.measure_files_per_day(root, window_days=14)
        assert count == 0
        assert rate == 0.0

    def test_zero_window_days_is_zero_rate(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        _write_raw_file_at(root / "raw", "s", now, 1)
        rate, count = ont.measure_files_per_day(root, window_days=0, now=now)
        assert rate == 0.0


class TestWaveDutyCycle:
    def test_computes_ratio(self) -> None:
        assert ont.wave_duty_cycle(5, 20) == 0.25

    def test_none_when_either_input_missing(self) -> None:
        assert ont.wave_duty_cycle(None, 20) is None
        assert ont.wave_duty_cycle(5, None) is None
        assert ont.wave_duty_cycle(5, 0) is None


class TestClosureVerdict:
    def test_closes_when_within_both_budgets(self) -> None:
        verdict = ont.closure_verdict(
            nightly_calls_total=500,
            nightly_call_budget=800,
            nightly_seconds_total=3000,
            nightly_window_seconds=3600,
        )
        assert verdict == "closes"

    def test_does_not_close_when_calls_exceed_budget(self) -> None:
        verdict = ont.closure_verdict(
            nightly_calls_total=900,
            nightly_call_budget=800,
            nightly_seconds_total=100,
            nightly_window_seconds=3600,
        )
        assert verdict == "does-not-close"

    def test_does_not_close_when_wall_clock_exceeds_window(self) -> None:
        verdict = ont.closure_verdict(
            nightly_calls_total=10,
            nightly_call_budget=800,
            nightly_seconds_total=5000,
            nightly_window_seconds=3600,
        )
        assert verdict == "does-not-close"

    def test_indeterminate_when_inputs_unmeasured(self) -> None:
        verdict = ont.closure_verdict(
            nightly_calls_total=None,
            nightly_call_budget=800,
            nightly_seconds_total=100,
            nightly_window_seconds=3600,
        )
        assert verdict == "indeterminate"


class TestAmortizedLoadAssumptions:
    def test_all_zero_default(self) -> None:
        a = ont.AmortizedLoadAssumptions()
        assert a.total_calls_per_night == 0.0
        assert a.total_seconds_per_night == 0.0

    def test_totals_combine_all_four_terms(self) -> None:
        a = ont.AmortizedLoadAssumptions(
            comparator_pairs_per_night=10,
            comparator_calls_per_pair=2,
            comparator_seconds_per_pair=1.5,
            ttl_recheck_calls_per_night=5,
            ttl_recheck_seconds_per_night=5,
            invalidation_wave_calls_per_night=3,
            invalidation_wave_seconds_per_night=3,
            audit_sampling_calls_per_night=2,
            audit_sampling_seconds_per_night=2,
        )
        assert a.total_calls_per_night == 10 * 2 + 5 + 3 + 2  # 30
        assert a.total_seconds_per_night == 10 * 1.5 + 5 + 3 + 2  # 25.0


class TestBuildOrdinaryNightTable:
    def test_indeterminate_without_ledger_or_log(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = ont.build_ordinary_night_table(root)
        assert result.verdict == "indeterminate"
        assert result.nightly_call_budget == 800  # DEFAULT_MAX_API_CALLS
        assert result.nightly_window_seconds == 3600  # DEFAULT_MAX_RUNTIME

    def test_closes_with_light_measured_load(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        for i in range(2):
            _write_raw_file_at(root / "raw", "s", now - timedelta(days=1), i)
        _write_ledger(
            [
                {
                    "v": 1,
                    "ts": "2026-07-01T00:00:00Z",
                    "run_type": "librarian",
                    "provider": "anthropic",
                    "files_processed": 2,
                    "api_calls": 4,
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "total_tokens": 110,
                    "estimated_cost_usd": 0.0,
                }
            ],
        )
        log_records = parse_run_summary_text(
            "librarian-run-summary total_secs=10 | entity secs=10 files=2"
        )
        result = ont.build_ordinary_night_table(
            root,
            summary_log_records=log_records,
            intake_window_days=14,
            now=now,
        )
        assert result.calls_per_file == 2.0
        assert result.wall_clock_per_file_seconds == 5.0
        assert result.verdict == "closes"

    def test_does_not_close_with_heavy_amortized_load(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        for i in range(2):
            _write_raw_file_at(root / "raw", "s", now - timedelta(days=1), i)
        _write_ledger(
            [
                {
                    "v": 1,
                    "ts": "2026-07-01T00:00:00Z",
                    "run_type": "librarian",
                    "provider": "anthropic",
                    "files_processed": 2,
                    "api_calls": 4,
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "total_tokens": 110,
                    "estimated_cost_usd": 0.0,
                }
            ],
        )
        log_records = parse_run_summary_text(
            "librarian-run-summary total_secs=10 | entity secs=10 files=2"
        )
        heavy = ont.AmortizedLoadAssumptions(
            comparator_pairs_per_night=10_000,
            comparator_calls_per_pair=1,
        )
        result = ont.build_ordinary_night_table(
            root,
            summary_log_records=log_records,
            amortized=heavy,
            now=now,
        )
        assert result.verdict == "does-not-close"

    def test_duty_cycle_flows_through(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = ont.build_ordinary_night_table(
            root, nights_in_wave=1, total_nights=10
        )
        assert result.duty_cycle == 0.1


class TestRenderSnapshotEntry:
    def test_non_closing_verdict_lists_documented_options_but_does_not_pick_one(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = ont.build_ordinary_night_table(root)
        assert result.verdict == "indeterminate"
        text = ont.render_snapshot_entry(result)
        for option in ont.DOCUMENTED_NON_CLOSURE_OPTIONS:
            assert option in text
        assert "operator decision required" in text

    def test_closing_verdict_omits_the_options_menu(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        _write_raw_file_at(root / "raw", "s", now - timedelta(days=1), 0)
        _write_ledger(
            [
                {
                    "v": 1,
                    "ts": "2026-07-01T00:00:00Z",
                    "run_type": "librarian",
                    "provider": "anthropic",
                    "files_processed": 1,
                    "api_calls": 1,
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "estimated_cost_usd": 0.0,
                }
            ],
        )
        log_records = parse_run_summary_text(
            "librarian-run-summary total_secs=1 | entity secs=1 files=1"
        )
        result = ont.build_ordinary_night_table(
            root, summary_log_records=log_records, now=now
        )
        assert result.verdict == "closes"
        text = ont.render_snapshot_entry(result)
        assert "Documented options" not in text


class TestWriteSnapshot:
    def test_writes_even_when_indeterminate(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = ont.build_ordinary_night_table(root)
        docs_path = tmp_path / "docs" / "measurements.md"
        ont.write_snapshot(result, docs_path=docs_path)
        text = docs_path.read_text(encoding="utf-8")
        assert ont.SECTION_HEADING in text
        assert "INDETERMINATE" in text


class TestOperatorSuppliedOverrides:
    """Issue athenaeum#1095 AC5: calls/file, files/day and wall-clock/file
    must be explicit named inputs, defaulting to the existing derived path
    with unchanged provenance when omitted."""

    def test_omitting_overrides_preserves_the_derived_path(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = ont.build_ordinary_night_table(root)
        assert result.files_per_day == 0.0
        assert result.files_per_day_source == "measured (trailing window, lower bound)"
        assert result.calls_per_file is None
        assert result.wall_clock_per_file_seconds is None

    def test_calls_per_file_override_takes_effect(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = ont.build_ordinary_night_table(root, calls_per_file=7.5)
        assert result.calls_per_file == 7.5
        assert result.calls_per_file_source == "operator-supplied"

    def test_files_per_day_override_takes_effect(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = ont.build_ordinary_night_table(root, files_per_day=3.0)
        assert result.files_per_day == 3.0
        assert result.files_per_day_source == "operator-supplied"
        assert result.files_per_day_sample_count == 0

    def test_wall_clock_per_file_seconds_override_takes_effect(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = ont.build_ordinary_night_table(
            root, calls_per_file=1.0, files_per_day=2.0, wall_clock_per_file_seconds=10.0
        )
        assert result.wall_clock_per_file_seconds == 10.0
        assert result.wall_clock_source == "operator-supplied"
        assert result.ordinary_seconds_total == 20.0  # 10.0 * 2.0 files/day

    def test_overrides_flow_into_the_closure_verdict(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = ont.build_ordinary_night_table(
            root,
            calls_per_file=1.0,
            files_per_day=1.0,
            wall_clock_per_file_seconds=1.0,
        )
        assert result.verdict == "closes"
        assert result.nightly_calls_total == 1.0
        assert result.nightly_seconds_total == 1.0

    def test_override_provenance_visible_in_rendered_snapshot(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        result = ont.build_ordinary_night_table(root, files_per_day=6.5)
        text = ont.render_snapshot_entry(result)
        assert "files_per_day: 6.500 [operator-supplied]" in text
