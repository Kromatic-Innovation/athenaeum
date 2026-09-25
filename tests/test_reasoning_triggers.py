# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#909 — reasoning-tier triggers.

Three concerns, class-per-concern (mirrors ``tests/test_ingest_reindex.py``):

- ``TestDiscoverRawBacklogBytes`` — the byte-aggregate backlog helper
  (:func:`athenaeum.intake.discover_raw_backlog_bytes`), the "M bytes" half
  of the backlog-depth trigger.
- ``TestResolveReasoningTrigger*`` — the four config resolvers under
  ``librarian.reasoning_triggers.*``, mirroring
  ``test_live_delta_cadence.py::TestResolveFullCompileEveryDays``'s shape.
- ``TestEvaluateTriggers`` — the pure evaluator
  (:func:`athenaeum.reasoning_triggers.evaluate_triggers`): one test per
  trigger reason (AC1/AC2/AC3), the nightly backstop or precedence over it
  (AC7), and disabled/None-config behavior.

CLI-level coverage (the ``--if-triggered`` flag on ``athenaeum ingest``,
AC3/AC4/AC8) lives in ``tests/test_ingest_reindex.py``. C4-scoping /
full-sweep coverage (AC5/AC6) lives in ``tests/test_contradiction_sweep.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum.intake import discover_raw_backlog_bytes
from athenaeum.quiesce import QuiesceState
from athenaeum.reasoning_triggers import TriggerDecision, evaluate_triggers

# ---------------------------------------------------------------------------
# discover_raw_backlog_bytes
# ---------------------------------------------------------------------------


class TestDiscoverRawBacklogBytes:
    def test_empty_backlog_is_zero(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        raw_root.mkdir()
        assert discover_raw_backlog_bytes(raw_root) == 0

    def test_missing_raw_root_is_zero(self, tmp_path: Path) -> None:
        assert discover_raw_backlog_bytes(tmp_path / "does-not-exist") == 0

    def test_sums_discovered_file_sizes(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        source_dir = raw_root / "sessions"
        source_dir.mkdir(parents=True)
        one = source_dir / "20240101T000000Z-aaaaaaaa.md"
        two = source_dir / "20240101T000001Z-bbbbbbbb.md"
        one.write_text("a" * 100, encoding="utf-8")
        two.write_text("b" * 250, encoding="utf-8")

        assert discover_raw_backlog_bytes(raw_root) == 350

    def test_tolerant_of_vanished_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_root = tmp_path / "raw"
        source_dir = raw_root / "sessions"
        source_dir.mkdir(parents=True)
        (source_dir / "20240101T000000Z-aaaaaaaa.md").write_text(
            "a" * 100, encoding="utf-8"
        )
        (source_dir / "20240101T000001Z-bbbbbbbb.md").write_text(
            "b" * 250, encoding="utf-8"
        )

        real_stat = Path.stat

        def flaky_stat(self: Path, *a: object, **kw: object) -> object:
            if self.name.endswith("bbbbbbbb.md"):
                raise OSError("vanished mid-scan")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        # Only the file whose stat() succeeds counts; the flaky one is
        # skipped rather than raising.
        assert discover_raw_backlog_bytes(raw_root) == 100


# ---------------------------------------------------------------------------
# Config resolvers (issue athenaeum#909)
# ---------------------------------------------------------------------------


class TestResolveReasoningTriggerBacklogFiles:
    def test_default_disabled(self) -> None:
        from athenaeum.config import resolve_reasoning_trigger_backlog_files

        assert resolve_reasoning_trigger_backlog_files(None) is None
        assert resolve_reasoning_trigger_backlog_files({}) is None

    def test_explicit_override(self) -> None:
        from athenaeum.config import resolve_reasoning_trigger_backlog_files

        cfg = {"librarian": {"reasoning_triggers": {"backlog_files": 25}}}
        assert resolve_reasoning_trigger_backlog_files(cfg) == 25

    def test_bool_rejected(self) -> None:
        from athenaeum.config import resolve_reasoning_trigger_backlog_files

        cfg = {"librarian": {"reasoning_triggers": {"backlog_files": True}}}
        assert resolve_reasoning_trigger_backlog_files(cfg) is None

    def test_non_positive_rejected(self) -> None:
        from athenaeum.config import resolve_reasoning_trigger_backlog_files

        cfg = {"librarian": {"reasoning_triggers": {"backlog_files": 0}}}
        assert resolve_reasoning_trigger_backlog_files(cfg) is None
        cfg = {"librarian": {"reasoning_triggers": {"backlog_files": -5}}}
        assert resolve_reasoning_trigger_backlog_files(cfg) is None


class TestResolveReasoningTriggerBacklogBytes:
    def test_default_disabled(self) -> None:
        from athenaeum.config import resolve_reasoning_trigger_backlog_bytes

        assert resolve_reasoning_trigger_backlog_bytes(None) is None
        assert resolve_reasoning_trigger_backlog_bytes({}) is None

    def test_explicit_override(self) -> None:
        from athenaeum.config import resolve_reasoning_trigger_backlog_bytes

        cfg = {"librarian": {"reasoning_triggers": {"backlog_bytes": 5242880}}}
        assert resolve_reasoning_trigger_backlog_bytes(cfg) == 5242880

    def test_bool_and_non_positive_rejected(self) -> None:
        from athenaeum.config import resolve_reasoning_trigger_backlog_bytes

        cfg = {"librarian": {"reasoning_triggers": {"backlog_bytes": True}}}
        assert resolve_reasoning_trigger_backlog_bytes(cfg) is None
        cfg = {"librarian": {"reasoning_triggers": {"backlog_bytes": 0}}}
        assert resolve_reasoning_trigger_backlog_bytes(cfg) is None


class TestResolveReasoningTriggerIntervalHours:
    def test_default_disabled(self) -> None:
        from athenaeum.config import resolve_reasoning_trigger_interval_hours

        assert resolve_reasoning_trigger_interval_hours(None) is None
        assert resolve_reasoning_trigger_interval_hours({}) is None

    def test_explicit_override(self) -> None:
        from athenaeum.config import resolve_reasoning_trigger_interval_hours

        cfg = {"librarian": {"reasoning_triggers": {"interval_hours": 6}}}
        assert resolve_reasoning_trigger_interval_hours(cfg) == 6

    def test_bool_and_non_positive_rejected(self) -> None:
        from athenaeum.config import resolve_reasoning_trigger_interval_hours

        cfg = {"librarian": {"reasoning_triggers": {"interval_hours": True}}}
        assert resolve_reasoning_trigger_interval_hours(cfg) is None
        cfg = {"librarian": {"reasoning_triggers": {"interval_hours": -1}}}
        assert resolve_reasoning_trigger_interval_hours(cfg) is None


class TestResolveReasoningTriggerNightlyBackstopHours:
    def test_default_24(self) -> None:
        from athenaeum.config import (
            resolve_reasoning_trigger_nightly_backstop_hours,
        )

        assert resolve_reasoning_trigger_nightly_backstop_hours(None) == 24
        assert resolve_reasoning_trigger_nightly_backstop_hours({}) == 24

    def test_explicit_override(self) -> None:
        from athenaeum.config import (
            resolve_reasoning_trigger_nightly_backstop_hours,
        )

        cfg = {"librarian": {"reasoning_triggers": {"nightly_backstop_hours": 12}}}
        assert resolve_reasoning_trigger_nightly_backstop_hours(cfg) == 12

    def test_bool_and_non_positive_rejected(self) -> None:
        from athenaeum.config import (
            resolve_reasoning_trigger_nightly_backstop_hours,
        )

        cfg = {"librarian": {"reasoning_triggers": {"nightly_backstop_hours": True}}}
        assert resolve_reasoning_trigger_nightly_backstop_hours(cfg) == 24
        cfg = {"librarian": {"reasoning_triggers": {"nightly_backstop_hours": 0}}}
        assert resolve_reasoning_trigger_nightly_backstop_hours(cfg) == 24


# ---------------------------------------------------------------------------
# evaluate_triggers — the pure evaluator (D1)
# ---------------------------------------------------------------------------


_NO_TRIGGERS_CFG: dict = {"librarian": {"reasoning_triggers": {}}}


class TestEvaluateTriggers:
    def test_on_demand_always_fires(self) -> None:
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=timedelta(seconds=1),
            on_demand=True,
            config=_NO_TRIGGERS_CFG,
        )
        assert decision == TriggerDecision(fired=True, reason="on-demand")

    def test_backlog_files_fires_at_threshold(self) -> None:
        cfg = {"librarian": {"reasoning_triggers": {"backlog_files": 10}}}
        decision = evaluate_triggers(
            backlog_files=10,
            backlog_bytes=0,
            since_last_run=timedelta(seconds=1),
            on_demand=False,
            config=cfg,
        )
        assert decision == TriggerDecision(fired=True, reason="backlog-files")

    def test_backlog_files_below_threshold_does_not_fire(self) -> None:
        cfg = {"librarian": {"reasoning_triggers": {"backlog_files": 10}}}
        decision = evaluate_triggers(
            backlog_files=9,
            backlog_bytes=0,
            since_last_run=timedelta(seconds=1),
            on_demand=False,
            config=cfg,
        )
        assert decision == TriggerDecision(fired=False, reason="none")

    def test_backlog_files_unset_never_fires_on_count(self) -> None:
        decision = evaluate_triggers(
            backlog_files=1_000_000,
            backlog_bytes=0,
            since_last_run=timedelta(seconds=1),
            on_demand=False,
            config=_NO_TRIGGERS_CFG,
        )
        assert decision.fired is False

    def test_backlog_bytes_fires_at_threshold(self) -> None:
        cfg = {"librarian": {"reasoning_triggers": {"backlog_bytes": 5_000_000}}}
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=5_000_000,
            since_last_run=timedelta(seconds=1),
            on_demand=False,
            config=cfg,
        )
        assert decision == TriggerDecision(fired=True, reason="backlog-bytes")

    def test_backlog_bytes_below_threshold_does_not_fire(self) -> None:
        cfg = {"librarian": {"reasoning_triggers": {"backlog_bytes": 5_000_000}}}
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=4_999_999,
            since_last_run=timedelta(seconds=1),
            on_demand=False,
            config=cfg,
        )
        assert decision.fired is False

    def test_interval_fires_when_elapsed_meets_threshold(self) -> None:
        cfg = {"librarian": {"reasoning_triggers": {"interval_hours": 6}}}
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=timedelta(hours=6),
            on_demand=False,
            config=cfg,
        )
        assert decision == TriggerDecision(fired=True, reason="interval")

    def test_interval_does_not_fire_before_threshold(self) -> None:
        cfg = {"librarian": {"reasoning_triggers": {"interval_hours": 6}}}
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=timedelta(hours=5, minutes=59),
            on_demand=False,
            config=cfg,
        )
        assert decision.fired is False

    def test_interval_unset_never_fires(self) -> None:
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=timedelta(days=365),
            on_demand=False,
            config=_NO_TRIGGERS_CFG,
        )
        # Only the always-on nightly backstop (default 24h) can fire here.
        assert decision == TriggerDecision(fired=True, reason="nightly-backstop")

    def test_nightly_backstop_fires_when_nothing_else_did(self) -> None:
        # AC7: the backstop fires at its own threshold when no other trigger
        # is even configured.
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=timedelta(hours=24),
            on_demand=False,
            config=_NO_TRIGGERS_CFG,
        )
        assert decision == TriggerDecision(fired=True, reason="nightly-backstop")

    def test_nightly_backstop_does_not_fire_before_threshold(self) -> None:
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=timedelta(hours=23, minutes=59),
            on_demand=False,
            config=_NO_TRIGGERS_CFG,
        )
        assert decision == TriggerDecision(fired=False, reason="none")

    def test_nightly_backstop_is_superseded_by_an_earlier_trigger(self) -> None:
        # AC7 literal wording: the backstop fires "when no other trigger has
        # fired". Here BOTH the backlog trigger and the (elapsed) backstop
        # threshold are satisfied simultaneously — the higher-priority
        # backlog trigger must win, never "nightly-backstop".
        cfg = {
            "librarian": {
                "reasoning_triggers": {
                    "backlog_files": 5,
                    "nightly_backstop_hours": 24,
                }
            }
        }
        decision = evaluate_triggers(
            backlog_files=5,
            backlog_bytes=0,
            since_last_run=timedelta(hours=48),
            on_demand=False,
            config=cfg,
        )
        assert decision == TriggerDecision(fired=True, reason="backlog-files")

    def test_nightly_backstop_is_superseded_by_interval(self) -> None:
        cfg = {
            "librarian": {
                "reasoning_triggers": {
                    "interval_hours": 6,
                    "nightly_backstop_hours": 24,
                }
            }
        }
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=timedelta(hours=48),
            on_demand=False,
            config=cfg,
        )
        assert decision == TriggerDecision(fired=True, reason="interval")

    def test_no_prior_run_is_infinitely_overdue_for_interval(self) -> None:
        # Design decision (documented in evaluate_triggers' own docstring):
        # a never-recorded last run is treated as "infinitely overdue" for
        # BOTH the interval and backstop checks, mirroring this codebase's
        # existing missing-stamp-means-due convention
        # (_load_full_compile_stamp -> full_compile_due).
        cfg = {"librarian": {"reasoning_triggers": {"interval_hours": 6}}}
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=None,
            on_demand=False,
            config=cfg,
        )
        assert decision == TriggerDecision(fired=True, reason="interval")

    def test_no_prior_run_is_infinitely_overdue_for_backstop(self) -> None:
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=None,
            on_demand=False,
            config=_NO_TRIGGERS_CFG,
        )
        assert decision == TriggerDecision(fired=True, reason="nightly-backstop")

    def test_on_demand_wins_over_everything(self) -> None:
        cfg = {
            "librarian": {
                "reasoning_triggers": {
                    "backlog_files": 1,
                    "backlog_bytes": 1,
                    "interval_hours": 1,
                }
            }
        }
        decision = evaluate_triggers(
            backlog_files=1000,
            backlog_bytes=1000,
            since_last_run=timedelta(days=1),
            on_demand=True,
            config=cfg,
        )
        assert decision == TriggerDecision(fired=True, reason="on-demand")

    def test_nothing_configured_and_fresh_stamp_is_a_true_no_op(self) -> None:
        decision = evaluate_triggers(
            backlog_files=3,
            backlog_bytes=1024,
            since_last_run=timedelta(minutes=1),
            on_demand=False,
            config=_NO_TRIGGERS_CFG,
        )
        assert decision == TriggerDecision(fired=False, reason="none")


# ---------------------------------------------------------------------------
# evaluate_triggers(quiesce=...) — the operator quiesce sentinel (issue
# athenaeum#1898, AC1/AC2/AC3). QuiesceState is a plain frozen dataclass with
# no behavior of its own (all its I/O lives in :mod:`athenaeum.quiesce`), so
# constructing one directly here for the "present" branch needs no real
# sentinel file on disk.
# ---------------------------------------------------------------------------

_ACTIVE_QUIESCE = QuiesceState(
    holder="alice@laptop",
    reason="backfilling person pages",
    created_at=datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc),
    expires_at=datetime(2026, 9, 25, 14, 0, 0, tzinfo=timezone.utc),
)


class TestEvaluateTriggersQuiesce:
    def test_quiesce_present_suppresses_a_trigger_that_would_otherwise_fire(
        self,
    ) -> None:
        # AC1: an active quiesce blocks the scheduler even when a configured
        # trigger's threshold is clearly met.
        cfg = {"librarian": {"reasoning_triggers": {"backlog_files": 1}}}
        decision = evaluate_triggers(
            backlog_files=1000,
            backlog_bytes=0,
            since_last_run=timedelta(days=1),
            on_demand=False,
            config=cfg,
            quiesce=_ACTIVE_QUIESCE,
        )
        assert decision == TriggerDecision(fired=False, reason="quiesced")

    def test_quiesce_present_suppresses_the_nightly_backstop_too(self) -> None:
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=None,  # infinitely overdue -- would otherwise backstop-fire
            on_demand=False,
            config=_NO_TRIGGERS_CFG,
            quiesce=_ACTIVE_QUIESCE,
        )
        assert decision == TriggerDecision(fired=False, reason="quiesced")

    def test_on_demand_wins_over_an_active_quiesce(self) -> None:
        # AC3: a direct operator run (on_demand=True, i.e. `athenaeum ingest`
        # with no --if-triggered) is unaffected by the sentinel -- on_demand
        # is checked BEFORE quiesce.
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=timedelta(hours=1),
            on_demand=True,
            config=_NO_TRIGGERS_CFG,
            quiesce=_ACTIVE_QUIESCE,
        )
        assert decision == TriggerDecision(fired=True, reason="on-demand")

    def test_quiesce_none_is_the_default_and_behaves_as_before(self) -> None:
        # AC2 (release/expiry branch): the caller passes quiesce=None once
        # the sentinel is released or has expired (see
        # `athenaeum.quiesce.read_quiesce_state`'s "expired == absent" rule)
        # -- evaluation then proceeds exactly like every pre-athenaeum#1898 test
        # above, which is what makes leaving `quiesce` off those calls valid.
        decision = evaluate_triggers(
            backlog_files=0,
            backlog_bytes=0,
            since_last_run=timedelta(hours=24),
            on_demand=False,
            config=_NO_TRIGGERS_CFG,
            quiesce=None,
        )
        assert decision == TriggerDecision(fired=True, reason="nightly-backstop")
