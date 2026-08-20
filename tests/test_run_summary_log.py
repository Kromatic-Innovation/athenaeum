# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``librarian-run-summary`` log parser (issue athenaeum#713)."""

from __future__ import annotations

from pathlib import Path

from athenaeum.librarian import RUN_SUMMARY_PREFIX
from athenaeum.run_summary_log import (
    entity_phase_wall_clock_per_file,
    parse_run_summary_line,
    parse_run_summary_log,
    parse_run_summary_text,
)

_SAMPLE_LINE = (
    "2026-08-19T02:00:01Z INFO librarian-run-summary total_secs=12.3 "
    "schema_fragments=observation-filter:default zero_yield=0 | "
    "wiki-dedup secs=0.1 | "
    "entity secs=4.2 calls=6 created=2 updated=1 escalated=0 files=3 | "
    "auto-memory secs=7.8 detector_haiku=4 resolver_opus=1 sweep_pairs=0 | "
    "retire secs=0.1 | reresolve secs=0.05 calls=0"
)


class TestParseRunSummaryLine:
    def test_parses_head_and_phases(self) -> None:
        rec = parse_run_summary_line(_SAMPLE_LINE)
        assert rec is not None
        assert rec.total_secs == 12.3
        assert rec.head_fields["schema_fragments"] == "observation-filter:default"
        assert rec.head_fields["zero_yield"] == "0"
        assert set(rec.phases) == {"wiki-dedup", "entity", "auto-memory", "retire", "reresolve"}
        assert rec.phase_float("entity", "secs") == 4.2
        assert rec.phase_int("entity", "files") == 3
        assert rec.phase_int("entity", "calls") == 6

    def test_prefix_is_the_real_librarian_constant(self) -> None:
        # Guards against a silent prefix rename in librarian.py going
        # unnoticed here (mirrors athenaeum#734's "pin the exact name" discipline).
        assert RUN_SUMMARY_PREFIX == "librarian-run-summary"
        assert RUN_SUMMARY_PREFIX in _SAMPLE_LINE

    def test_non_matching_line_returns_none(self) -> None:
        assert parse_run_summary_line("2026-08-19T02:00:01Z INFO some other log line") is None

    def test_missing_total_secs_returns_none(self) -> None:
        assert parse_run_summary_line("librarian-run-summary | entity secs=1.0 files=1") is None

    def test_phase_float_and_int_return_none_when_absent(self) -> None:
        rec = parse_run_summary_line(_SAMPLE_LINE)
        assert rec is not None
        assert rec.phase_float("entity", "nonexistent") is None
        assert rec.phase_int("nonexistent-phase", "secs") is None


class TestParseRunSummaryText:
    def test_finds_every_matching_line_and_skips_noise(self) -> None:
        text = "\n".join(
            [
                "some noise",
                _SAMPLE_LINE,
                "more noise here",
                _SAMPLE_LINE.replace("total_secs=12.3", "total_secs=5.0").replace(
                    "files=3", "files=1"
                ),
            ]
        )
        records = parse_run_summary_text(text)
        assert len(records) == 2
        assert records[0].total_secs == 12.3
        assert records[1].total_secs == 5.0

    def test_empty_text_yields_empty_list(self) -> None:
        assert parse_run_summary_text("") == []


class TestParseRunSummaryLog:
    def test_reads_real_file(self, tmp_path: Path) -> None:
        log_path = tmp_path / "sweep.out.log"
        log_path.write_text(_SAMPLE_LINE + "\n")
        records = parse_run_summary_log(log_path)
        assert len(records) == 1
        assert records[0].total_secs == 12.3

    def test_missing_file_returns_empty_list_not_error(self, tmp_path: Path) -> None:
        assert parse_run_summary_log(tmp_path / "does-not-exist.log") == []


class TestEntityPhaseWallClockPerFile:
    def test_averages_across_records(self) -> None:
        records = parse_run_summary_text(
            "\n".join(
                [
                    _SAMPLE_LINE,  # entity secs=4.2 files=3
                    _SAMPLE_LINE.replace("secs=4.2 calls=6", "secs=1.8 calls=2").replace(
                        "files=3", "files=1"
                    ),  # entity secs=1.8 files=1
                ]
            )
        )
        result = entity_phase_wall_clock_per_file(records)
        assert result is not None
        seconds_per_file, total_files = result
        assert total_files == 4
        assert seconds_per_file == (4.2 + 1.8) / 4

    def test_none_when_no_usable_entity_data(self) -> None:
        records = parse_run_summary_text("librarian-run-summary total_secs=1.0 | retire secs=1.0")
        assert entity_phase_wall_clock_per_file(records) is None

    def test_none_for_empty_input(self) -> None:
        assert entity_phase_wall_clock_per_file([]) is None

    def test_skips_records_with_zero_files(self) -> None:
        records = parse_run_summary_text(
            _SAMPLE_LINE.replace("files=3", "files=0")
        )
        assert entity_phase_wall_clock_per_file(records) is None
