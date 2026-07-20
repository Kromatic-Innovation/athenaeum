# SPDX-License-Identifier: Apache-2.0
"""Tests for the progress-heartbeat helper (issue #398).

Covers :class:`athenaeum.progress.PhaseHeartbeat` and
:func:`athenaeum.config.resolve_heartbeat_interval`.
"""

from __future__ import annotations

import logging

import pytest

from athenaeum.config import resolve_heartbeat_interval
from athenaeum.progress import PhaseHeartbeat


def _heartbeat_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [rec.message for rec in caplog.records if "librarian-heartbeat" in rec.message]


class TestPhaseHeartbeatStartDone:
    def test_start_always_emits(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("merge-write", total=5)
        hb.start()
        lines = _heartbeat_lines(caplog)
        assert len(lines) == 1
        assert "phase=merge-write" in lines[0]
        assert "status=start" in lines[0]
        assert "done=0" in lines[0]
        assert "total=5" in lines[0]

    def test_done_always_emits_even_with_zero_units(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("wiki-dedupe", total=0)
        hb.start()
        hb.done()
        lines = _heartbeat_lines(caplog)
        assert len(lines) == 2
        assert "status=start" in lines[0]
        assert "status=done" in lines[1]
        assert "done=0" in lines[1]
        assert "total=0" in lines[1]

    def test_done_is_idempotent(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("reresolve")
        hb.start()
        hb.done()
        hb.done()
        hb.done()
        lines = [line for line in _heartbeat_lines(caplog) if "status=done" in line]
        assert len(lines) == 1

    def test_total_none_renders_as_question_mark(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("merge-detect", total=None)
        hb.start()
        assert "total=?" in _heartbeat_lines(caplog)[0]


class TestPhaseHeartbeatTick:
    def test_first_tick_always_emits(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("merge-detect", total=10, interval_s=9999.0)
        hb.start()
        hb.tick("cluster-1")
        ticks = [line for line in _heartbeat_lines(caplog) if "status=tick" in line]
        assert len(ticks) == 1
        assert "unit=cluster-1" in ticks[0]
        assert "done=1" in ticks[0]

    def test_large_interval_throttles_subsequent_ticks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("merge-detect", total=10, interval_s=9999.0)
        hb.start()
        for i in range(5):
            hb.tick(f"cluster-{i}")
        ticks = [line for line in _heartbeat_lines(caplog) if "status=tick" in line]
        # Only the first tick emits; the rest are throttled by the huge interval.
        assert len(ticks) == 1

    def test_interval_zero_emits_every_tick(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("merge-detect", total=3, interval_s=0.0)
        hb.start()
        hb.tick("a")
        hb.tick("b")
        hb.tick("c")
        ticks = [line for line in _heartbeat_lines(caplog) if "status=tick" in line]
        assert len(ticks) == 3

    def test_running_counts_accumulate(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("merge-write", total=3, interval_s=0.0)
        hb.start()
        hb.tick("a", compiled=1)
        hb.tick("b", unchanged=1)
        hb.tick("c", error=1)
        hb.done()
        assert hb.done_count == 3
        assert hb.compiled == 1
        assert hb.unchanged == 1
        assert hb.error == 1
        done_lines = [line for line in _heartbeat_lines(caplog) if "status=done" in line]
        assert "compiled=1" in done_lines[0]
        assert "unchanged=1" in done_lines[0]
        assert "error=1" in done_lines[0]
        assert "done=3" in done_lines[0]

    def test_tick_without_unit_id_uses_placeholder(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("reresolve", interval_s=0.0)
        hb.start()
        hb.tick(None)
        ticks = [line for line in _heartbeat_lines(caplog) if "status=tick" in line]
        assert "unit=-" in ticks[0]

    def test_tick_before_start_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("reresolve", interval_s=0.0)
        hb.tick("q-1")  # no explicit start() call
        assert hb.done_count == 1


class TestPhaseHeartbeatFieldFormat:
    def test_prefix_and_fields_present(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="athenaeum")
        hb = PhaseHeartbeat("wiki-dedupe", total=2, interval_s=0.0)
        hb.start()
        hb.tick("cluster-x", compiled=1)
        hb.done()
        for line in _heartbeat_lines(caplog):
            assert line.startswith("librarian-heartbeat ")
            assert "phase=wiki-dedupe" in line
            assert "status=" in line
            assert "done=" in line
            assert "total=" in line
            assert "compiled=" in line
            assert "unchanged=" in line
            assert "error=" in line
            assert "unit=" in line
            assert "elapsed=" in line
            assert line.rstrip().endswith("s")

    def test_custom_logger_is_used(self) -> None:
        custom_logger = logging.getLogger("athenaeum.progress.test-custom")
        records: list[logging.LogRecord] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Handler()
        custom_logger.addHandler(handler)
        custom_logger.setLevel(logging.INFO)
        custom_logger.propagate = False
        try:
            hb = PhaseHeartbeat("merge-detect", logger=custom_logger)
            hb.start()
            hb.done()
        finally:
            custom_logger.removeHandler(handler)
        assert len(records) == 2
        assert all("librarian-heartbeat" in r.getMessage() for r in records)


class TestResolveHeartbeatInterval:
    def test_default(self) -> None:
        assert resolve_heartbeat_interval(None) == 60.0
        assert resolve_heartbeat_interval({}) == 60.0
        assert resolve_heartbeat_interval({"librarian": {}}) == 60.0

    def test_yaml_value_wins(self) -> None:
        assert resolve_heartbeat_interval({"librarian": {"heartbeat_interval": 30}}) == 30.0

    def test_yaml_le_zero_returns_zero_not_default(self) -> None:
        assert resolve_heartbeat_interval({"librarian": {"heartbeat_interval": 0}}) == 0.0
        assert resolve_heartbeat_interval({"librarian": {"heartbeat_interval": -5}}) == 0.0

    def test_yaml_non_numeric_falls_back_to_default(self) -> None:
        assert (
            resolve_heartbeat_interval({"librarian": {"heartbeat_interval": "nope"}}) == 60.0
        )

    def test_yaml_bool_falls_back_to_default(self) -> None:
        assert resolve_heartbeat_interval({"librarian": {"heartbeat_interval": True}}) == 60.0

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_HEARTBEAT_INTERVAL", "15")
        assert (
            resolve_heartbeat_interval({"librarian": {"heartbeat_interval": 300}}) == 15.0
        )
        monkeypatch.delenv("ATHENAEUM_HEARTBEAT_INTERVAL", raising=False)

    def test_env_le_zero_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_HEARTBEAT_INTERVAL", "0")
        assert resolve_heartbeat_interval(None) == 0.0
        monkeypatch.setenv("ATHENAEUM_HEARTBEAT_INTERVAL", "-10")
        assert resolve_heartbeat_interval(None) == 0.0
        monkeypatch.delenv("ATHENAEUM_HEARTBEAT_INTERVAL", raising=False)

    def test_env_non_numeric_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_HEARTBEAT_INTERVAL", "notanumber")
        assert resolve_heartbeat_interval(None) == 60.0
        monkeypatch.delenv("ATHENAEUM_HEARTBEAT_INTERVAL", raising=False)
