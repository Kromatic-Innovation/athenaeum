# SPDX-License-Identifier: Apache-2.0
"""Tests for ``librarian-run-summary`` reading/writing (issues athenaeum#713, athenaeum#1102)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum.librarian import RUN_SUMMARY_PREFIX
from athenaeum.run_summary_log import (
    REFUSAL_FIELD,
    REGRESSION_ALERT_RATIO,
    REGRESSION_MIN_SAMPLES,
    RUN_SUMMARY_LEDGER_VERSION,
    build_economics_and_alerts,
    build_run_summary_ledger_record,
    compute_run_economics,
    default_run_summary_ledger_path,
    entity_phase_wall_clock_per_file,
    evaluate_regression_alerts,
    parse_run_summary_line,
    parse_run_summary_log,
    parse_run_summary_text,
    read_refusal_streak,
    read_run_summary_ledger,
    refusal_in_record,
    refusal_streak,
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
        assert set(rec.phases) == {
            "wiki-dedup",
            "entity",
            "auto-memory",
            "retire",
            "reresolve",
        }
        assert rec.phase_float("entity", "secs") == 4.2
        assert rec.phase_int("entity", "files") == 3
        assert rec.phase_int("entity", "calls") == 6

    def test_prefix_is_the_real_librarian_constant(self) -> None:
        # Guards against a silent prefix rename in librarian.py going
        # unnoticed here (mirrors athenaeum#734's "pin the exact name" discipline).
        assert RUN_SUMMARY_PREFIX == "librarian-run-summary"
        assert RUN_SUMMARY_PREFIX in _SAMPLE_LINE

    def test_non_matching_line_returns_none(self) -> None:
        assert (
            parse_run_summary_line("2026-08-19T02:00:01Z INFO some other log line")
            is None
        )

    def test_missing_total_secs_returns_none(self) -> None:
        assert (
            parse_run_summary_line("librarian-run-summary | entity secs=1.0 files=1")
            is None
        )

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
                    _SAMPLE_LINE.replace(
                        "secs=4.2 calls=6", "secs=1.8 calls=2"
                    ).replace(
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
        records = parse_run_summary_text(
            "librarian-run-summary total_secs=1.0 | retire secs=1.0"
        )
        assert entity_phase_wall_clock_per_file(records) is None

    def test_none_for_empty_input(self) -> None:
        assert entity_phase_wall_clock_per_file([]) is None

    def test_skips_records_with_zero_files(self) -> None:
        records = parse_run_summary_text(_SAMPLE_LINE.replace("files=3", "files=0"))
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


class TestComputeRunEconomics:
    """Issue athenaeum#1184: cost/matches/calls-per-file + echoed-chars/call."""

    def test_reports_both_attempted_and_acted_denominators(self) -> None:
        econ = compute_run_economics(
            files_processed=10,
            files_acted=4,
            matched=8,
            calls=12,
            merge_calls=6,
            merge_echoed_chars=6 * 15_000,
            cost_usd=2.0,
        )
        assert econ["files_processed"] == 10
        assert econ["files_acted"] == 4
        # The "measured trap" the issue names: attempted-denominator cost
        # UNDERSTATES the true per-acting-file figure whenever some
        # attempted files produced zero actions.
        assert econ["cost_per_file_processed"] == 0.2
        assert econ["cost_per_file_acted"] == 0.5
        assert econ["cost_per_file_acted"] > econ["cost_per_file_processed"]
        assert econ["matches_per_file_processed"] == 0.8
        assert econ["matches_per_file_acted"] == 2.0
        assert econ["calls_per_file_processed"] == 1.2
        assert econ["echoed_chars_per_call"] == 15_000.0

    def test_zero_denominator_is_none_not_zero_or_error(self) -> None:
        econ = compute_run_economics(
            files_processed=0,
            files_acted=0,
            matched=0,
            calls=0,
            merge_calls=0,
            merge_echoed_chars=0,
            cost_usd=0.0,
        )
        assert econ["cost_per_file_processed"] is None
        assert econ["cost_per_file_acted"] is None
        assert econ["matches_per_file_acted"] is None
        assert econ["echoed_chars_per_call"] is None


class TestEvaluateRegressionAlerts:
    """Issue athenaeum#1184 AC4: a synthetic inflated-fan-out run trips the
    ``matches_per_file`` alert — the regression test the whole issue is
    justified by ("the instrument that would have made the entire cost
    investigation unnecessary")."""

    def _baseline_economics(self, *, matches_per_file: float = 1.02) -> dict:
        files_processed = 100
        files_acted = 40
        matched = round(matches_per_file * files_acted)
        return compute_run_economics(
            files_processed=files_processed,
            files_acted=files_acted,
            matched=matched,
            calls=files_processed + matched,
            merge_calls=matched,
            merge_echoed_chars=matched * 15_000,
            cost_usd=0.05 * matched,
        )

    def test_inflated_fanout_trips_matches_per_file_alert(self) -> None:
        history = [
            self._baseline_economics() for _ in range(REGRESSION_MIN_SAMPLES + 5)
        ]
        # Fan-out drifted ~10x — exactly the athenaeum#1167 regression shape
        # (1.02 -> ~10 matches/file) this issue exists to catch.
        current = self._baseline_economics(matches_per_file=10.4)

        alerts = evaluate_regression_alerts(current, history)

        tripped = {a["metric"] for a in alerts}
        assert "matches_per_file_acted" in tripped
        matches_alert = next(
            a for a in alerts if a["metric"] == "matches_per_file_acted"
        )
        assert matches_alert["ratio"] > REGRESSION_ALERT_RATIO
        assert matches_alert["value"] == pytest.approx(
            current["matches_per_file_acted"]
        )

    def test_stable_history_does_not_trip(self) -> None:
        history = [
            self._baseline_economics() for _ in range(REGRESSION_MIN_SAMPLES + 5)
        ]
        current = self._baseline_economics(matches_per_file=1.05)  # normal noise
        assert evaluate_regression_alerts(current, history) == []

    def test_insufficient_history_never_trips(self) -> None:
        history = [
            self._baseline_economics() for _ in range(REGRESSION_MIN_SAMPLES - 1)
        ]
        current = self._baseline_economics(matches_per_file=50.0)
        assert evaluate_regression_alerts(current, history) == []

    def test_no_history_never_trips(self) -> None:
        current = self._baseline_economics(matches_per_file=50.0)
        assert evaluate_regression_alerts(current, []) == []

    def test_slow_ramp_still_trips_not_just_a_step(self) -> None:
        # The REAL athenaeum#1167 regression was not a step -- it was a compounding
        # ramp (1.02 -> 11.63 matches/file over ~90 nightly runs, near-linear
        # with corpus size). A trailing-mean baseline would chase this ramp
        # and never trip; the oldest-window-minimum baseline this function
        # uses must not. Build ~2.6%/run compounding growth across 40 runs
        # and confirm the LATEST run (compared to the OLDEST window) trips.
        n_runs = 60
        growth_per_run = 1.026
        history = [
            self._baseline_economics(matches_per_file=1.02 * (growth_per_run**i))
            for i in range(n_runs)
        ]
        current = history[-1]
        prior_history = history[:-1]

        alerts = evaluate_regression_alerts(current, prior_history)

        tripped = {a["metric"] for a in alerts}
        assert "matches_per_file_acted" in tripped
        assert "cost_per_file_acted" in tripped

    def test_zero_sample_in_baseline_window_does_not_permanently_disable_ratchet(
        self,
    ) -> None:
        # A MIN-based baseline is vulnerable to one legitimate 0.0 sample
        # (e.g. a fresh-entity night with matched == 0) permanently flooring
        # the baseline at 0 for the life of the ledger's genesis window --
        # excluding non-positive samples from the pool is what prevents that.
        history = [
            self._baseline_economics() for _ in range(REGRESSION_MIN_SAMPLES + 5)
        ]
        # Inject a real 0.0 sample for the RATCHETED cost_per_file_acted
        # metric specifically (e.g. a night that spent zero dollars while
        # still acting on files -- legitimate, not an error).
        zeroed_cost_run = dict(history[0])
        zeroed_cost_run["cost_per_file_acted"] = 0.0
        history_with_zero = [zeroed_cost_run] + history[1:]

        current = self._baseline_economics(matches_per_file=10.4)

        alerts = evaluate_regression_alerts(current, history_with_zero)

        tripped = {a["metric"] for a in alerts}
        assert "cost_per_file_acted" in tripped
        assert "matches_per_file_acted" in tripped

    def test_none_valued_metric_neither_trips_nor_pollutes_baseline(self) -> None:
        history = [
            compute_run_economics(
                files_processed=10,
                files_acted=0,  # -> matches_per_file_acted is None
                matched=0,
                calls=10,
                merge_calls=0,
                merge_echoed_chars=0,
                cost_usd=0.0,
            )
            for _ in range(REGRESSION_MIN_SAMPLES + 2)
        ]
        current = self._baseline_economics(matches_per_file=50.0)
        # No usable baseline samples for matches_per_file_acted (all None in
        # history) -- must not trip, and must not crash on the None values.
        alerts = evaluate_regression_alerts(current, history)
        assert not any(a["metric"] == "matches_per_file_acted" for a in alerts)


class TestBuildEconomicsAndAlerts:
    def test_reads_history_before_the_current_run_is_appended(
        self, tmp_path: Path
    ) -> None:
        ledger_path = tmp_path / "run_summary.jsonl"
        baseline_econ = compute_run_economics(
            files_processed=100,
            files_acted=40,
            matched=41,
            calls=141,
            merge_calls=41,
            merge_echoed_chars=41 * 15_000,
            cost_usd=2.0,
        )
        for _ in range(REGRESSION_MIN_SAMPLES + 3):
            write_run_summary_record(
                _PROFILE, ledger_path=ledger_path, economics=baseline_econ
            )

        economics, alerts = build_economics_and_alerts(
            files_processed=100,
            files_acted=40,
            matched=410,  # ~10x drift
            calls=510,
            merge_calls=410,
            merge_echoed_chars=410 * 15_000,
            cost_usd=20.0,
            ledger_path=ledger_path,
        )
        assert any(a["metric"] == "matches_per_file_acted" for a in alerts)
        assert economics["matched"] == 410

        # The current run's own record must not have been counted in its
        # own baseline (it hadn't been written yet when history was read).
        records = read_run_summary_ledger(ledger_path)
        assert len(records) == REGRESSION_MIN_SAMPLES + 3

    def test_corrupt_ledger_degrades_to_no_history_not_an_error(
        self, tmp_path: Path
    ) -> None:
        ledger_path = tmp_path / "run_summary.jsonl"
        ledger_path.write_text("not json at all\n")
        economics, alerts = build_economics_and_alerts(
            files_processed=5,
            files_acted=2,
            matched=3,
            calls=5,
            merge_calls=3,
            merge_echoed_chars=3000,
            cost_usd=0.5,
            ledger_path=ledger_path,
        )
        assert alerts == []
        assert economics["matched"] == 3


class TestRunSummaryLedgerRecordEconomicsField:
    def test_economics_and_alerts_round_trip_through_the_ledger(
        self, tmp_path: Path
    ) -> None:
        ledger_path = tmp_path / "run_summary.jsonl"
        econ = compute_run_economics(
            files_processed=10,
            files_acted=4,
            matched=8,
            calls=12,
            merge_calls=6,
            merge_echoed_chars=90_000,
            cost_usd=2.0,
        )
        alerts = [
            {
                "metric": "matches_per_file_acted",
                "value": 2.0,
                "baseline": 0.5,
                "ratio": 4.0,
            }
        ]
        write_run_summary_record(
            _PROFILE, ledger_path=ledger_path, economics=econ, alerts=alerts
        )
        records = read_run_summary_ledger(ledger_path)
        assert len(records) == 1
        assert records[0]["economics"]["matched"] == 8
        assert records[0]["alerts"][0]["metric"] == "matches_per_file_acted"

    def test_omitted_when_not_given_keeps_pre_1184_shape(self) -> None:
        record = build_run_summary_ledger_record(_PROFILE)
        assert "economics" not in record
        assert "alerts" not in record


# ---------------------------------------------------------------------------
# The athenaeum#1135 refusal verdict, persisted (issue athenaeum#1283).
#
# THREE ledger-record shapes matter here, not two -- a first cut of this
# issue collapsed "verdict never evaluated for this run" and "verdict
# evaluated, run was clean" into the same omitted ``refusal`` key, which
# made a real, non-hypothetical path (``RunContext.stop_on_deadline``, a
# wall-clock deadline trip in a pre-entity phase -- it calls
# ``emit_run_summary`` and returns BEFORE ``_run_finalize_phase`` ever sets
# ``ctx.librarian_refusal``) misread as a confirmed-clean run. The fix: the
# ``refusal`` field, when present, is a dict keyed on ``tripped`` -- so
# "evaluated and clean" writes ``{"tripped": False}`` (present, falsy
# ``tripped``) and only "never evaluated" omits the key entirely.
# ---------------------------------------------------------------------------


def _refusal_record(*, v: int = RUN_SUMMARY_LEDGER_VERSION) -> dict:
    """A v3 record whose run WAS a refusal (mirrors ``_record`` in
    ``test_intake_scheduling_fairness.py``'s starvation-streak tests, but for
    the single ``refusal`` field rather than a per-source token)."""
    return {
        "v": v,
        "ts": "2026-09-02T00:00:00Z",
        "phases": {},
        REFUSAL_FIELD: {"tripped": True, "reason": "spend-ceiling", "files": 0},
    }


def _evaluated_clean_record(*, v: int = RUN_SUMMARY_LEDGER_VERSION) -> dict:
    """A record whose run was evaluated and was NOT a refusal — the
    ``refusal`` key is PRESENT with a falsy ``tripped``, distinct from
    :func:`_unevaluated_record` below (key absent entirely). This is the
    shape a real clean run writes via ``RunContext.emit_run_summary`` once
    ``ctx.librarian_refusal`` is ``False`` (not ``None``)."""
    return {
        "v": v,
        "ts": "2026-09-02T00:00:00Z",
        "phases": {},
        REFUSAL_FIELD: {"tripped": False},
    }


def _unevaluated_record(*, v: int = RUN_SUMMARY_LEDGER_VERSION) -> dict:
    """A record whose ``refusal`` key is entirely ABSENT — either because
    ``v < 3`` (predates the field existing at all) or because ``v >= 3`` but
    the verdict was never evaluated for that particular run (e.g. the
    ``stop_on_deadline`` path named in the module docstring above). Both are
    "cannot speak", for different reasons — see :func:`refusal_in_record`.
    """
    return {"v": v, "ts": "2026-09-02T00:00:00Z", "phases": {}}


class TestRunSummaryLedgerRecordRefusalField:
    def test_refusal_round_trips_through_the_ledger(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "run_summary.jsonl"
        write_run_summary_record(
            _PROFILE,
            ledger_path=ledger_path,
            refusal={"tripped": True, "reason": "budget", "files": 0},
        )
        records = read_run_summary_ledger(ledger_path)
        assert len(records) == 1
        assert records[0]["refusal"] == {
            "tripped": True,
            "reason": "budget",
            "files": 0,
        }
        assert records[0]["v"] == RUN_SUMMARY_LEDGER_VERSION == 3

    def test_an_evaluated_clean_verdict_is_written_not_omitted(
        self, tmp_path: Path
    ) -> None:
        # The athenaeum#1283 correctness fix, pinned directly: an EVALUATED
        # clean run (``{"tripped": False}``, a truthy dict) is written, not
        # omitted -- distinct from the never-evaluated case below, which
        # omits the key entirely. Before this fix the two were
        # indistinguishable on disk.
        ledger_path = tmp_path / "run_summary.jsonl"
        write_run_summary_record(
            _PROFILE, ledger_path=ledger_path, refusal={"tripped": False}
        )
        records = read_run_summary_ledger(ledger_path)
        assert len(records) == 1
        assert records[0]["refusal"] == {"tripped": False}

    def test_omitted_only_when_the_verdict_was_never_evaluated(self) -> None:
        # This exercises the raw builder's own omission gate (unchanged by
        # this fix: ``build_run_summary_ledger_record`` still omits on any
        # falsy *refusal* argument). What changed is which argument a REAL
        # caller (``RunContext.emit_run_summary``) passes for a clean run:
        # it now passes the truthy ``{"tripped": False}`` (see the test
        # above), reserving ``None``/``{}`` for "never evaluated" only — so
        # this omission path should no longer be reached by an evaluated
        # run in practice, only by one that never reached the verdict.
        assert "refusal" not in build_run_summary_ledger_record(_PROFILE)
        assert "refusal" not in build_run_summary_ledger_record(
            _PROFILE, refusal=None
        )
        assert "refusal" not in build_run_summary_ledger_record(
            _PROFILE, refusal={}
        )

    def test_version_bumped_to_3(self) -> None:
        # Issue athenaeum#1283: the version bump itself is load-bearing, not
        # just additive bookkeeping -- it is what lets a reader distinguish
        # a v<3 record (cannot speak, full stop -- the field didn't exist)
        # from a v>=3 one, where an ABSENT ``refusal`` key means "never
        # evaluated" and a PRESENT one carries the actual verdict via its
        # ``tripped`` sub-field -- see TestRefusalInRecord below.
        assert RUN_SUMMARY_LEDGER_VERSION == 3
        assert build_run_summary_ledger_record(_PROFILE)["v"] == 3


class TestRefusalInRecord:
    def test_true_for_a_v3_refusal_record(self) -> None:
        assert refusal_in_record(_refusal_record()) is True

    def test_false_for_a_v3_evaluated_clean_record(self) -> None:
        assert refusal_in_record(_evaluated_clean_record()) is False

    def test_none_for_a_pre_v3_record_even_with_no_refusal_key(self) -> None:
        # The load-bearing case: a v1/v2 record's ABSENT ``refusal`` key must
        # not be read as "confirmed not a refusal" -- that version of the
        # code never evaluated the athenaeum#1135 predicate into the ledger at
        # all, so the record simply cannot speak to it.
        assert refusal_in_record(_unevaluated_record(v=2)) is None
        assert refusal_in_record(_unevaluated_record(v=1)) is None

    def test_none_for_a_v3_record_whose_verdict_was_never_evaluated(self) -> None:
        # The GAP this whole follow-up closes: a v3 record's ABSENT
        # ``refusal`` key is NOT "evaluated and clean" -- it is
        # ``RunContext.emit_run_summary`` having run before
        # ``ctx.librarian_refusal`` was ever set (the ``stop_on_deadline``
        # path; see this module's section docstring above and
        # ``test_librarian_run_refusal.py``'s real-code-path regression
        # test). Must read ``None``, never ``False``.
        assert refusal_in_record(_unevaluated_record()) is None
        assert refusal_in_record(_unevaluated_record(v=RUN_SUMMARY_LEDGER_VERSION)) is None

    def test_none_for_missing_or_unparseable_version(self) -> None:
        assert refusal_in_record({"ts": "x", "phases": {}}) is None
        assert refusal_in_record({"v": "not-a-number", "phases": {}}) is None

    def test_non_dict_refusal_value_reads_as_never_evaluated(self) -> None:
        # Defensive: a malformed/hand-edited record whose ``refusal`` key
        # exists but isn't a dict (so ``.get("tripped")`` would raise on a
        # naive implementation) must degrade to "cannot speak", not crash.
        assert refusal_in_record({"v": 3, "refusal": "not-a-dict"}) is None
        assert refusal_in_record({"v": 3, "refusal": True}) is None


class TestRefusalStreak:
    def test_no_history_is_zero(self) -> None:
        assert refusal_streak([]) == 0

    def test_single_trailing_refusal_scores_one(self) -> None:
        assert refusal_streak([_refusal_record()]) == 1

    def test_counts_consecutive_trailing_refusals(self) -> None:
        history = [_refusal_record() for _ in range(4)]
        assert refusal_streak(history) == 4

    def test_an_evaluated_clean_run_breaks_the_streak(self) -> None:
        history = [_refusal_record(), _refusal_record(), _evaluated_clean_record()]
        assert refusal_streak(history) == 0

    def test_only_the_trailing_run_of_refusals_counts(self) -> None:
        # Oldest-first: an older refusal, a clean run, then a fresh refusal.
        # Only the NEWEST trailing run of refusals counts.
        history = [
            _refusal_record(),
            _evaluated_clean_record(),
            _refusal_record(),
        ]
        assert refusal_streak(history) == 1

    def test_stops_at_a_pre_v3_record_rather_than_counting_through_it(self) -> None:
        # The AC this test exists for: a v<3 record (predates athenaeum#1283)
        # sitting between two confirmed refusals must NOT be bridged. If the
        # reader wrongly treated "cannot speak" as "was a refusal" and kept
        # walking past it, this would read 3; if it wrongly treated it as
        # "not a refusal" and reset, the visible result would happen to
        # match (1) in THIS shape, which is exactly why the assertion below
        # also pins that the older, pre-boundary refusal is invisible to
        # this call -- the stop must be genuine, not a lucky same-answer
        # reset.
        history = [_refusal_record(), _unevaluated_record(v=2), _refusal_record()]
        assert refusal_streak(history) == 1

    def test_stops_at_an_unevaluated_v3_record_the_same_way(self) -> None:
        # The SECOND source of ambiguity (issue athenaeum#1283's follow-up
        # fix): a v3 record whose verdict was simply never evaluated for
        # that run (e.g. a stop_on_deadline trip) must be stopped at
        # exactly like a pre-v3 record above -- never bridged past to reach
        # an older confirmed refusal.
        history = [_refusal_record(), _unevaluated_record(), _refusal_record()]
        assert refusal_streak(history) == 1

    def test_a_lone_pre_v3_record_is_zero_not_a_crash(self) -> None:
        assert refusal_streak([_unevaluated_record(v=1)]) == 0

    def test_a_lone_unevaluated_v3_record_is_zero_not_a_crash(self) -> None:
        assert refusal_streak([_unevaluated_record()]) == 0


class TestReadRefusalStreak:
    def test_reads_the_durable_ledger_with_reason_detail(self, tmp_path: Path) -> None:
        import json

        ledger = tmp_path / "run_summary.jsonl"
        ledger.write_text(
            "".join(json.dumps(_refusal_record()) + "\n" for _ in range(3)),
            encoding="utf-8",
        )
        streak, detail = read_refusal_streak(ledger_path=ledger)
        assert streak == 3
        assert detail == {"tripped": True, "reason": "spend-ceiling", "files": 0}

    def test_a_missing_ledger_degrades_to_no_history(self, tmp_path: Path) -> None:
        streak, detail = read_refusal_streak(ledger_path=tmp_path / "absent.jsonl")
        assert (streak, detail) == (0, None)

    def test_a_clean_ledger_reports_zero_and_no_detail(self, tmp_path: Path) -> None:
        import json

        ledger = tmp_path / "run_summary.jsonl"
        ledger.write_text(
            json.dumps(_evaluated_clean_record()) + "\n", encoding="utf-8"
        )
        assert read_refusal_streak(ledger_path=ledger) == (0, None)

    def test_an_unevaluated_ledger_reports_zero_and_no_detail(
        self, tmp_path: Path
    ) -> None:
        import json

        ledger = tmp_path / "run_summary.jsonl"
        ledger.write_text(json.dumps(_unevaluated_record()) + "\n", encoding="utf-8")
        assert read_refusal_streak(ledger_path=ledger) == (0, None)


class TestDefaultRunSummaryLedgerPath:
    def test_default_lives_under_cache_dir(self, tmp_path: Path) -> None:
        path = default_run_summary_ledger_path(cache_dir=tmp_path)
        assert path == tmp_path / "run_summary.jsonl"

    def test_expanduser_applied(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        path = default_run_summary_ledger_path(cache_dir=Path("~"))
        assert path == tmp_path / "run_summary.jsonl"
