# SPDX-License-Identifier: Apache-2.0
"""Tests for ``librarian-run-summary`` reading/writing (issues athenaeum#713, athenaeum#1102)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from athenaeum.librarian import RUN_SUMMARY_PREFIX
from athenaeum.run_summary_log import (
    RUN_SUMMARY_LEDGER_VERSION,
    build_run_summary_ledger_record,
    default_run_summary_ledger_path,
    entity_phase_wall_clock_per_file,
    parse_run_summary_line,
    parse_run_summary_log,
    parse_run_summary_text,
    read_run_summary_ledger,
    write_run_summary_record,
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


class TestReasonFieldParsesGenerically(object):
    """Issue athenaeum#1102 AC1: every phase now carries a ``reason=`` token.

    No parser change was needed for this — ``_KV_RE`` already captures any
    ``key=value`` token generically — this pins that the existing parser
    picks it up for free.
    """

    def test_reason_token_parses_like_any_other_field(self) -> None:
        line = (
            "librarian-run-summary total_secs=4.3 | "
            "entity secs=4.2 calls=6 files=3 reason=entity-share | "
            "auto-memory secs=0.1 reason=completed"
        )
        rec = parse_run_summary_line(line)
        assert rec is not None
        assert rec.phases["entity"]["reason"] == "entity-share"
        assert rec.phases["auto-memory"]["reason"] == "completed"


# ---------------------------------------------------------------------------
# Durable ledger (issue athenaeum#1102 AC2)
# ---------------------------------------------------------------------------

_PROFILE: "list[tuple[str, float, dict]]" = [
    ("wiki-dedup", 0.1, {"reason": "completed"}),
    (
        "entity",
        4.2,
        {
            "calls": 6,
            "created": 2,
            "updated": 1,
            "escalated": 0,
            "files": 3,
            "reason": "entity-share",
        },
    ),
    (
        "auto-memory",
        7.8,
        {"detector_haiku": 4, "resolver_opus": 1, "reason": "completed"},
    ),
]


class TestBuildRunSummaryLedgerRecord:
    def test_shape_and_json_native_types(self) -> None:
        record = build_run_summary_ledger_record(_PROFILE)
        assert record["v"] == RUN_SUMMARY_LEDGER_VERSION
        assert record["total_secs"] == 0.1 + 4.2 + 7.8
        assert record["phases"]["entity"]["secs"] == 4.2
        # JSON-native (not a stringified "6") -- the AC2 distinction from
        # the prose line's key=value tokens.
        assert record["phases"]["entity"]["calls"] == 6
        assert isinstance(record["phases"]["entity"]["calls"], int)
        assert record["phases"]["entity"]["reason"] == "entity-share"
        assert record["phases"]["auto-memory"]["reason"] == "completed"

    def test_ts_defaults_to_now_utc_and_is_iso(self) -> None:
        record = build_run_summary_ledger_record(_PROFILE)
        # Round-trips through fromisoformat (after the Z -> +00:00 swap the
        # reader itself does) without raising.
        parsed = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
        assert parsed.tzinfo is not None

    def test_explicit_ts_used_verbatim(self) -> None:
        ts = datetime(2026, 8, 24, 3, 0, 0, tzinfo=timezone.utc)
        record = build_run_summary_ledger_record(_PROFILE, ts=ts)
        assert record["ts"] == "2026-08-24T03:00:00Z"


class TestWriteAndReadRunSummaryLedger:
    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "run_summary.jsonl"
        ts = datetime(2026, 8, 24, 3, 0, 0, tzinfo=timezone.utc)
        wrote = write_run_summary_record(_PROFILE, ledger_path=ledger_path, ts=ts)
        assert wrote is True
        records = read_run_summary_ledger(ledger_path)
        assert len(records) == 1
        assert records[0]["total_secs"] == 0.1 + 4.2 + 7.8
        assert records[0]["phases"]["entity"]["reason"] == "entity-share"

    def test_multiple_runs_append_multiple_lines(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "run_summary.jsonl"
        write_run_summary_record(_PROFILE, ledger_path=ledger_path)
        write_run_summary_record(_PROFILE, ledger_path=ledger_path)
        records = read_run_summary_ledger(ledger_path)
        assert len(records) == 2

    def test_empty_profile_is_a_no_op(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "run_summary.jsonl"
        wrote = write_run_summary_record([], ledger_path=ledger_path)
        assert wrote is False
        assert not ledger_path.exists()

    def test_missing_ledger_reads_as_empty(self, tmp_path: Path) -> None:
        assert read_run_summary_ledger(tmp_path / "does-not-exist.jsonl") == []

    def test_torn_trailing_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "run_summary.jsonl"
        write_run_summary_record(_PROFILE, ledger_path=ledger_path)
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write('{"v": 1, "ts": "2026-08-2')  # torn, no trailing newline
        records = read_run_summary_ledger(ledger_path)
        assert len(records) == 1

    def test_since_until_bounds_filter_by_ts(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "run_summary.jsonl"
        early = datetime(2026, 8, 20, tzinfo=timezone.utc)
        late = datetime(2026, 8, 24, tzinfo=timezone.utc)
        write_run_summary_record(_PROFILE, ledger_path=ledger_path, ts=early)
        write_run_summary_record(_PROFILE, ledger_path=ledger_path, ts=late)

        only_late = read_run_summary_ledger(
            ledger_path, since=late - timedelta(hours=1)
        )
        assert len(only_late) == 1
        assert only_late[0]["ts"] == "2026-08-24T00:00:00Z"

        only_early = read_run_summary_ledger(ledger_path, until=late)
        assert len(only_early) == 1
        assert only_early[0]["ts"] == "2026-08-20T00:00:00Z"

    def test_write_failure_is_swallowed_and_returns_false(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def _boom(*_a, **_k):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr("athenaeum.run_summary_log.append_line_durable", _boom)
        wrote = write_run_summary_record(_PROFILE, ledger_path=tmp_path / "x.jsonl")
        assert wrote is False


class TestDefaultRunSummaryLedgerPath:
    def test_default_lives_under_cache_dir(self, tmp_path: Path) -> None:
        path = default_run_summary_ledger_path(cache_dir=tmp_path)
        assert path == tmp_path / "run_summary.jsonl"

    def test_expanduser_applied(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        path = default_run_summary_ledger_path(cache_dir=Path("~"))
        assert path == tmp_path / "run_summary.jsonl"
