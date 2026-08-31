# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1185 — exponential backoff between a stuck file's attempts.

Occam's pre-flight for this issue found the filed premise only partly true:
:mod:`athenaeum.quarantine` (issue athenaeum#898) and the athenaeum#663 stuck-file
ledger already give a persistently-failing raw file a bounded attempt count
(``DEFAULT_STUCK_FILE_THRESHOLD``, default 3) — once crossed, the file is
skipped every subsequent run, spending zero further LLM calls
(``tests/test_librarian_stuck_files.py::test_reliably_failing_file_becomes_stuck_and_is_then_skipped``
already proves this). That satisfies the issue's AC1 ("no longer attempted
after N failures") and most of AC3 ("appear in the run summary with a count
and a failure reason", via ``stuck=N`` + ``out_run_stats["stuck_files"]``).

The genuine gap this suite covers: nothing SPACED the attempts BEFORE that
threshold — a file failing on run 1 was immediately retry-eligible on run 2
(the very next ~30-minute cadence tick), all the way up to
``stuck_threshold``. AC2 ("retries between attempts are spaced by
exponential backoff, not by the run cadence") had no code behind it at all.
This file covers the fix: :func:`_stuck_backoff_seconds` /
:func:`_stuck_backoff_window_open` (the pure backoff math),
:func:`librarian_stuck_file_backoff_base_seconds` (env > yaml > default
precedence, mirroring every sibling threshold resolver), and the entity
loop's new backoff-skip branch, end to end through a real ``run()``.

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum.librarian import (
    DEFAULT_STUCK_FILE_BACKOFF_BASE_SECONDS,
    DEFAULT_STUCK_FILE_THRESHOLD,
    _load_stuck_ledger,
    _stuck_backoff_seconds,
    _stuck_backoff_window_open,
    librarian_stuck_file_backoff_base_seconds,
    run,
)
from tests.test_librarian_deadline import _seed_knowledge_root
from tests.test_librarian_stuck_files import _raising_process_one

# ---------------------------------------------------------------------------
# librarian_stuck_file_backoff_base_seconds — env > yaml > default
# ---------------------------------------------------------------------------


class TestResolveStuckFileBackoffBaseSeconds:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", raising=False)
        assert (
            librarian_stuck_file_backoff_base_seconds(None)
            == DEFAULT_STUCK_FILE_BACKOFF_BASE_SECONDS
        )
        assert (
            librarian_stuck_file_backoff_base_seconds({})
            == DEFAULT_STUCK_FILE_BACKOFF_BASE_SECONDS
        )

    def test_yaml_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", raising=False)
        assert (
            librarian_stuck_file_backoff_base_seconds(
                {"librarian": {"stuck_file_backoff_base_seconds": 600}}
            )
            == 600
        )

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "120")
        assert (
            librarian_stuck_file_backoff_base_seconds(
                {"librarian": {"stuck_file_backoff_base_seconds": 600}}
            )
            == 120
        )

    def test_zero_is_a_valid_override_disabling_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unlike the threshold resolvers (which reject 0/negative), 0 is a
        legitimate value here: it disables backoff outright, reverting to
        pre-athenaeum#1185 behavior (every cadence tick is retry-eligible)."""
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "0")
        assert librarian_stuck_file_backoff_base_seconds(None) == 0

    def test_negative_env_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "-5")
        assert (
            librarian_stuck_file_backoff_base_seconds(None)
            == DEFAULT_STUCK_FILE_BACKOFF_BASE_SECONDS
        )

    def test_bool_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", raising=False)
        assert (
            librarian_stuck_file_backoff_base_seconds(
                {"librarian": {"stuck_file_backoff_base_seconds": True}}
            )
            == DEFAULT_STUCK_FILE_BACKOFF_BASE_SECONDS
        )

    def test_non_numeric_env_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "not-a-number")
        assert (
            librarian_stuck_file_backoff_base_seconds(None)
            == DEFAULT_STUCK_FILE_BACKOFF_BASE_SECONDS
        )


# ---------------------------------------------------------------------------
# _stuck_backoff_seconds — the pure doubling formula
# ---------------------------------------------------------------------------


class TestStuckBackoffSeconds:
    def test_zero_failures_is_zero(self) -> None:
        assert _stuck_backoff_seconds(0, base_seconds=3600) == 0

    def test_negative_failures_is_zero(self) -> None:
        assert _stuck_backoff_seconds(-1, base_seconds=3600) == 0

    def test_first_failure_is_the_base(self) -> None:
        assert _stuck_backoff_seconds(1, base_seconds=3600) == 3600

    def test_doubles_each_failure(self) -> None:
        assert _stuck_backoff_seconds(2, base_seconds=3600) == 7200
        assert _stuck_backoff_seconds(3, base_seconds=3600) == 14400
        assert _stuck_backoff_seconds(4, base_seconds=3600) == 28800

    def test_zero_base_disables_backoff_regardless_of_failures(self) -> None:
        assert _stuck_backoff_seconds(5, base_seconds=0) == 0

    def test_negative_base_disables_backoff(self) -> None:
        assert _stuck_backoff_seconds(5, base_seconds=-1) == 0


# ---------------------------------------------------------------------------
# _stuck_backoff_window_open — the ledger-entry-vs-now check
# ---------------------------------------------------------------------------


class TestStuckBackoffWindowOpen:
    def test_open_immediately_after_failure(self) -> None:
        now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        entry = {"failures": 1, "last_failed": "2026-08-30T12:00:00Z"}
        assert _stuck_backoff_window_open(entry, base_seconds=3600, now=now) is True

    def test_closes_once_the_window_elapses(self) -> None:
        entry = {"failures": 1, "last_failed": "2026-08-30T12:00:00Z"}
        just_before = datetime(2026, 8, 30, 12, 59, 59, tzinfo=timezone.utc)
        just_after = datetime(2026, 8, 30, 13, 0, 1, tzinfo=timezone.utc)
        assert _stuck_backoff_window_open(entry, base_seconds=3600, now=just_before) is True
        assert _stuck_backoff_window_open(entry, base_seconds=3600, now=just_after) is False

    def test_later_failure_has_a_longer_window(self) -> None:
        # 2nd failure: window is 7200s, not 3600s -- 90 minutes after the
        # 2nd failure is still within the doubled window.
        entry = {"failures": 2, "last_failed": "2026-08-30T12:00:00Z"}
        ninety_min_later = datetime(2026, 8, 30, 13, 30, 0, tzinfo=timezone.utc)
        assert (
            _stuck_backoff_window_open(entry, base_seconds=3600, now=ninety_min_later)
            is True
        )

    def test_missing_last_failed_fails_open(self) -> None:
        now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        assert _stuck_backoff_window_open({"failures": 1}, base_seconds=3600, now=now) is (
            False
        )

    def test_unparseable_last_failed_fails_open(self) -> None:
        now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        entry = {"failures": 1, "last_failed": "not-a-timestamp"}
        assert _stuck_backoff_window_open(entry, base_seconds=3600, now=now) is False

    def test_zero_base_seconds_is_always_open(self) -> None:
        now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        entry = {"failures": 1, "last_failed": "2026-08-30T12:00:00Z"}
        assert _stuck_backoff_window_open(entry, base_seconds=0, now=now) is False


# ---------------------------------------------------------------------------
# End-to-end through a real run() (AC2 + AC5's mandatory bounded-attempts test)
# ---------------------------------------------------------------------------


class TestBackoffEndToEnd:
    def test_second_attempt_is_skipped_within_the_backoff_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: a run driven immediately after the first failure (well
        within the default 1-hour base window) must NOT re-attempt the
        file -- process_one is not called a second time."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_THRESHOLD", "5")  # keep it un-stuck
        monkeypatch.delenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", raising=False)

        fake_process_one, calls = _raising_process_one(action="update:BigPage")
        monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)

        t0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

        stats1: dict = {}
        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=0,
            out_run_stats=stats1,
            now=t0,
        )
        assert calls["n"] == 1
        assert stats1["backoff_skipped_files"] == []

        # 5 minutes later -- well within the default 3600s base window.
        stats2: dict = {}
        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=0,
            out_run_stats=stats2,
            now=t0 + timedelta(minutes=5),
        )
        assert calls["n"] == 1, "still in backoff -- process_one must NOT be called again"
        assert stats2["backoff_skipped_files"] != []
        ref = next(iter(_load_stuck_ledger(root / "wiki")))
        assert stats2["backoff_skipped_files"] == [ref]
        # Not yet "stuck" -- only 1 real failure recorded so far.
        assert stats2["stuck_files"] == []

    def test_attempt_resumes_once_the_backoff_window_elapses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The companion positive case: once enough time has passed, the
        file IS retry-eligible again."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_THRESHOLD", "5")
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "600")  # 10 min

        fake_process_one, calls = _raising_process_one(action="update:BigPage")
        monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)

        t0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=0,
            now=t0,
        )
        assert calls["n"] == 1

        # 15 minutes later -- past the 10-minute base window.
        stats: dict = {}
        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=0,
            out_run_stats=stats,
            now=t0 + timedelta(minutes=15),
        )
        assert calls["n"] == 2, "window elapsed -- process_one must be called again"
        assert stats["backoff_skipped_files"] == []

    def test_persistently_failing_file_is_attempted_a_bounded_number_of_times(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5 (the issue's mandatory test): drives the file through its
        FULL lifecycle -- attempt, backoff-skip, attempt again (crosses
        threshold), then permanently stuck -- across a run cadence spanning
        DAYS, and asserts the total attempt count never exceeds
        ``DEFAULT_STUCK_FILE_THRESHOLD`` no matter how many more runs
        follow. This is the concrete, quantified version of the issue's
        "$52.64/day unbounded retry churn" complaint: with this fix, the
        file costs at most ``DEFAULT_STUCK_FILE_THRESHOLD`` attempts, ever
        -- not one attempt per ~40 runs/day forever.
        """
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.delenv("ATHENAEUM_STUCK_FILE_THRESHOLD", raising=False)  # default 3
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "3600")

        fake_process_one, calls = _raising_process_one(action="update:BigPage")
        monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)

        t = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        run_kwargs = dict(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=0,
        )

        # Simulate ~40 runs/day (every 30 minutes) for 4 SIMULATED DAYS --
        # the exact cadence and duration the issue's incident describes.
        for _ in range(4 * 24 * 2):  # 4 days * 24h * 2 runs/hour
            t += timedelta(minutes=30)
            run(**run_kwargs, now=t)  # type: ignore[arg-type]

        # DEFAULT_STUCK_FILE_THRESHOLD attempts total -- not one per each of
        # the ~192 simulated runs above.
        assert calls["n"] == DEFAULT_STUCK_FILE_THRESHOLD
