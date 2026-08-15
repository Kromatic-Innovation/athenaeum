# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#899 — zero-yield run alarm: calls spent, nothing committed.

The 2026-08-14 intake-architecture review found 406 of 856 all-time runs
processed zero files, and separately counted ~198 recent runs that hit their
900s timeout having spent 5-14 LLM calls each and produced zero files. The
adjacent instrumentation (athenaeum#669's entity-share yield, cron-fleet#94's
fleet-level cap exemption) stops just short of naming this specific pattern —
a run-level predicate at finalize that says "this run spent N calls and M
seconds and committed nothing" — so it was only visible by reading log
archives after the fact.

This suite covers the finalize-phase predicate (:func:`_zero_yield_tripped`),
its wiring into ``_run_finalize_phase`` (the WARNING line, the run-summary
counter, and the persisted cross-run state in :mod:`athenaeum.zero_yield`),
and the productive-run negative case — using an in-repo synthetic fixture
(``tests/fixtures/zero_yield/session_end_timeout.json``) that encodes the
observed 2026-08 SessionEnd shape. No host log archive is read by any test
here. All state is written under ``tmp_path``; no test touches the
operator's live ledger/knowledge store.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from athenaeum import zero_yield
from athenaeum.config import resolve_cache_dir
from athenaeum.librarian import (
    EXIT_GRACEFUL_PARTIAL,
    ZERO_YIELD_PREFIX,
    RunContext,
    _run_finalize_phase,
    _zero_yield_tripped,
)
from athenaeum.models import TokenUsage
from tests.test_librarian_run_phases import _make_ctx

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "zero_yield" / "session_end_timeout.json"
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _cache_dir() -> Path:
    """The cache dir ``_run_finalize_phase`` resolves internally for the
    zero-yield sidecar (``ATHENAEUM_CACHE_DIR``, redirected to a per-test tmp
    dir by the ``_isolate_cache_dir`` autouse fixture in conftest.py) — tests
    read/seed the SAME path via this resolver rather than hardcoding it."""
    return resolve_cache_dir()


def _finalize_ctx(tmp_path: Path, **overrides) -> RunContext:
    """A minimal, finalize-ready RunContext (mirrors ``_make_ctx``'s shape).

    ``push_after_run`` must be a concrete bool (finalize asserts it is not
    ``None``, matching every real caller — ``_resolve_run_config`` always
    resolves it before any phase runs); ``False`` here so finalize never
    attempts a real git push against ``tmp_path``.
    """
    ctx = _make_ctx(
        tmp_path,
        push_after_run=False,
        cluster_only=False,
        strict_budget=False,
    )
    (ctx.wiki_root).mkdir(parents=True, exist_ok=True)
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


# ---------------------------------------------------------------------------
# The predicate itself (AC 1)
# ---------------------------------------------------------------------------


class TestZeroYieldPredicate:
    def test_false_when_no_calls_spent(self, tmp_path: Path) -> None:
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=0)
        ctx.files_processed_count = 0
        ctx.deferred_refs = ["a.md"]
        assert _zero_yield_tripped(ctx, ["a.md"]) is False

    def test_false_when_files_were_committed(self, tmp_path: Path) -> None:
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=5)
        ctx.files_processed_count = 1
        ctx.deferred_refs = []
        assert _zero_yield_tripped(ctx, []) is False

    def test_false_when_deferral_set_progressed(self, tmp_path: Path) -> None:
        # Calls spent, nothing committed THIS run -- but a ref that was
        # deferred last run ("a.md") is no longer deferred, so the run made
        # progress against the backlog even though it drained zero files
        # itself this cycle (e.g. it failed instead of deferring).
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=3)
        ctx.files_processed_count = 0
        ctx.deferred_refs = ["b.md"]
        assert _zero_yield_tripped(ctx, ["a.md", "b.md"]) is False

    def test_true_when_calls_spent_nothing_committed_no_progress(
        self, tmp_path: Path
    ) -> None:
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=9)
        ctx.files_processed_count = 0
        ctx.deferred_refs = ["a.md", "b.md"]
        assert _zero_yield_tripped(ctx, ["a.md", "b.md"]) is True

    def test_true_when_deferred_set_grew_but_nothing_resolved(
        self, tmp_path: Path
    ) -> None:
        # A GROWING deferred set (a.md still stuck, b.md newly deferred) is
        # still "no progress" -- no PREVIOUSLY-deferred ref left the set.
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=4)
        ctx.files_processed_count = 0
        ctx.deferred_refs = ["a.md", "b.md"]
        assert _zero_yield_tripped(ctx, ["a.md"]) is True


# ---------------------------------------------------------------------------
# End-to-end through the finalize phase: WARNING line, run-summary counter,
# persisted cross-run state (AC 2, 3, 4)
# ---------------------------------------------------------------------------


class TestZeroYieldFinalizeIntegration:
    def test_synthetic_session_end_fixture_trips_the_alarm(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Replays the in-repo fixture (AC 5): calls spent, zero files
        committed, the same refs deferred as last run -- the alarm fires,
        the run summary carries the counter, and the persisted consecutive
        count increments across the run boundary."""
        fixture = _load_fixture()
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=fixture["api_calls"])
        ctx.raw_files = list(fixture["raw_file_refs"])
        ctx.deferred_refs = list(fixture["deferred_refs"])
        ctx.failed_files = list(fixture["failed_refs"])
        # The run also hit its wall-clock deadline (the observed SessionEnd
        # shape) -- set exactly like the real entity-loop per-file boundary
        # check does before falling through to finalize.
        ctx.deadline_tripped = True

        # Seed the PREVIOUS run's persisted state -- same deferred refs, and
        # already 2 consecutive zero-yield runs -- so this run's "no
        # progress" check and the consecutive-count increment both have a
        # real predecessor to compare against.
        zero_yield.write_state(
            _cache_dir(),
            consecutive=fixture["previous_consecutive"],
            deferred_refs=list(fixture["previous_deferred_refs"]),
        )

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum.librarian")
        rc = _run_finalize_phase(ctx)

        # A deadline-tripped run is still resumable-partial, not a crash --
        # the zero-yield alarm is additive observability, not a new failure
        # mode (out of scope: acting on the alarm).
        assert rc == EXIT_GRACEFUL_PARTIAL

        # AC 1: the predicate tripped and is recorded on the context.
        assert ctx.zero_yield_tripped is True
        assert ctx.zero_yield_consecutive == fixture["previous_consecutive"] + 1

        # AC 2: a WARNING-level, machine-greppable line naming calls spent,
        # seconds spent, and files committed.
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        zero_yield_lines = [w for w in warnings if ZERO_YIELD_PREFIX in w]
        assert len(zero_yield_lines) == 1
        line = zero_yield_lines[0]
        assert f"{fixture['api_calls']} LLM call" in line
        assert "committed 0 file" in line
        assert f"{fixture['previous_consecutive'] + 1} consecutive" in line

        # AC 3: the run summary carries the zero-yield counter.
        summary_lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("librarian-run-summary")
        ]
        assert len(summary_lines) == 1
        assert f"zero_yield={fixture['previous_consecutive'] + 1}" in summary_lines[0]

        # AC 4: the consecutive count is PERSISTED across runs.
        persisted = zero_yield.load_state(_cache_dir())
        assert persisted["consecutive"] == fixture["previous_consecutive"] + 1
        assert persisted["deferred_refs"] == sorted(fixture["deferred_refs"])

    def test_productive_run_does_not_trip_the_alarm(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A run that spends calls AND commits files must never trip the
        alarm, and must reset any prior consecutive-zero-yield streak."""
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=6)
        ctx.raw_files = ["a.md", "b.md", "c.md"]
        ctx.deferred_refs = []
        ctx.failed_files = []

        # A prior streak of 3 consecutive zero-yield runs -- this run breaks it.
        zero_yield.write_state(_cache_dir(), consecutive=3, deferred_refs=["a.md"])

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum.librarian")
        rc = _run_finalize_phase(ctx)

        assert rc == 0
        assert ctx.files_processed_count == 3  # nothing deferred/failed
        assert ctx.zero_yield_tripped is False
        assert ctx.zero_yield_consecutive == 0

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any(ZERO_YIELD_PREFIX in w for w in warnings)

        summary_lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("librarian-run-summary")
        ]
        assert len(summary_lines) == 1
        assert "zero_yield=0" in summary_lines[0]

        # The streak is reset, not merely left unincremented.
        persisted = zero_yield.load_state(_cache_dir())
        assert persisted["consecutive"] == 0
        assert persisted["deferred_refs"] == []

    def test_dry_run_never_evaluates_or_persists(self, tmp_path: Path) -> None:
        """A dry-run never unlinks a raw file, so ``files_processed_count``
        cannot be trusted as a "committed" signal -- the predicate must be
        skipped entirely (never fires, never persists a misleading state)."""
        ctx = _finalize_ctx(tmp_path, dry_run=True)
        ctx.usage = TokenUsage(api_calls=7)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = []
        ctx.failed_files = []

        rc = _run_finalize_phase(ctx)

        assert rc == 0
        assert ctx.zero_yield_tripped is None
        assert ctx.zero_yield_consecutive is None
        assert not (_cache_dir() / zero_yield.STATE_NAME).exists()
