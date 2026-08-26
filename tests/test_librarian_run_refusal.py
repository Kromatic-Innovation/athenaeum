# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1135 — a run that compiles NOTHING must not read as success.

A run whose spend budget was already exhausted before the entity loop
claimed its first file runs every deterministic phase, commits (a no-op
git-side), and logs ``librarian-run-summary ... calls=0 created=0 ...
files=0 reason=budget`` — but exited 0, indistinguishable from a genuine
success. ``athenaeum drain`` already refuses loudly on the analogous "made
ZERO progress" condition; this suite covers bringing the plain
``athenaeum run`` entry path up to the same standard:

- The refusal predicate (:func:`_librarian_run_refusal_tripped`): an early-
  stop ``reason`` (``deadline`` / ``entity-share`` / ``budget`` /
  ``spend-ceiling``) AND zero files committed (AC1).
- The dedicated ``spend-ceiling`` reason, split out of the previously
  overloaded generic ``budget`` bucket so a metered/subscription spend-
  ceiling refusal is separately greppable from a plain API-call-count trip.
- ``EXIT_LIBRARIAN_REFUSAL`` (3), the default-on nonzero exit, and its
  ``--allow-degraded`` opt-out (AC3) — the marker line still fires either
  way.
- The ``librarian-run-degraded`` marker line, its AC2 ``spend=`` rendering,
  and the AC4 "files=0 + reason does not look like success" assertion.
- AC5: ``athenaeum drain``'s own loud zero-progress refusal (a DIFFERENT
  code path, ``src/athenaeum/drain.py``) is untouched by any of this — see
  ``TestDrainUnaffected`` at the bottom, which pins its existing behavior
  rather than re-testing it (full coverage lives in ``test_drain.py``).

Uses the same synthetic-``RunContext`` + direct ``_run_finalize_phase()``
call pattern ``test_librarian_zero_yield.py`` established (issue athenaeum#899)
rather than driving a full mocked-LLM ``run()`` — the predicate is a pure
finalize-phase concern, and this keeps every case here fast and
deterministic. All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from athenaeum import spend
from athenaeum.librarian import (
    EXIT_GRACEFUL_PARTIAL,
    EXIT_LIBRARIAN_REFUSAL,
    RunContext,
    _format_budget_window_spend,
    _librarian_run_refusal_tripped,
    _run_finalize_phase,
    run,
)
from athenaeum.models import TokenUsage
from tests.test_budget_deferred import _fake_process_one_factory, _seed_knowledge_root
from tests.test_librarian_run_phases import _make_ctx


def _finalize_ctx(tmp_path: Path, **overrides) -> RunContext:
    """A minimal, finalize-ready RunContext (mirrors
    ``test_librarian_zero_yield.py``'s ``_finalize_ctx``, duplicated rather
    than imported — same small, self-contained helper convention that
    module itself established over ``_make_ctx``).
    """
    ctx = _make_ctx(
        tmp_path,
        push_after_run=False,
        cluster_only=False,
        strict_budget=False,
    )
    ctx.wiki_root.mkdir(parents=True, exist_ok=True)
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


# ---------------------------------------------------------------------------
# The predicate itself (AC1) — the reason vocabulary found in librarian.py:
# "completed" (normal), "deadline", "entity-share", "budget" (the plain
# max_api_calls count), and "spend-ceiling" (this issue's new split-out of
# the metered-dollar/subscription-token ceiling trip).
# ---------------------------------------------------------------------------


class TestLibrarianRunRefusalPredicate:
    @pytest.mark.parametrize(
        "reason", ["deadline", "entity-share", "budget", "spend-ceiling"]
    )
    def test_true_for_every_early_stop_reason_with_zero_files(
        self, tmp_path: Path, reason: str
    ) -> None:
        ctx = _finalize_ctx(tmp_path)
        ctx.entity_exit_reason = reason
        ctx.files_processed_count = 0
        assert _librarian_run_refusal_tripped(ctx) is True

    @pytest.mark.parametrize(
        "reason", ["deadline", "entity-share", "budget", "spend-ceiling"]
    )
    def test_false_when_files_were_committed(
        self, tmp_path: Path, reason: str
    ) -> None:
        # Same early-stop reason, but the run DID commit something (e.g. an
        # entity-share yield after several files already processed) — not a
        # refusal.
        ctx = _finalize_ctx(tmp_path)
        ctx.entity_exit_reason = reason
        ctx.files_processed_count = 3
        assert _librarian_run_refusal_tripped(ctx) is False

    def test_false_when_reason_completed(self, tmp_path: Path) -> None:
        # A clean completion can still carry files_processed_count == 0 (an
        # empty backlog / idle run) -- "completed" is NOT an early-stop
        # reason and must never trip the predicate on its own.
        ctx = _finalize_ctx(tmp_path)
        ctx.entity_exit_reason = "completed"
        ctx.files_processed_count = 0
        assert _librarian_run_refusal_tripped(ctx) is False

    def test_false_when_entity_phase_never_ran(self, tmp_path: Path) -> None:
        # cluster_only / merge_only skip the entity phase entirely --
        # entity_exit_reason stays at its RunContext default (None).
        ctx = _finalize_ctx(tmp_path)
        assert ctx.entity_exit_reason is None
        ctx.files_processed_count = 0
        assert _librarian_run_refusal_tripped(ctx) is False


# ---------------------------------------------------------------------------
# AC2 — the budget-window spend figure the marker line renders.
# ---------------------------------------------------------------------------


class TestFormatBudgetWindowSpend:
    def test_none_when_no_per_day_ceiling_configured(self, tmp_path: Path) -> None:
        ctx = _finalize_ctx(tmp_path)
        ctx.provider = "api"
        ctx.config = {}
        assert _format_budget_window_spend(ctx) is None

    def test_renders_dollar_cap_on_the_api_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "15.00")
        ledger = tmp_path / "spend.jsonl"
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(ledger))
        _write_ledger_record(ledger, provider="anthropic", estimated_cost_usd=15.33)

        ctx = _finalize_ctx(tmp_path)
        ctx.provider = "api"
        ctx.config = {}
        rendered = _format_budget_window_spend(ctx)
        assert rendered == "$15.33/$15.00"

    def test_renders_token_cap_on_the_subscription_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY", "5000")
        ledger = tmp_path / "spend.jsonl"
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(ledger))
        _write_ledger_record(ledger, provider="claude-cli", total_tokens=4000)

        ctx = _finalize_ctx(tmp_path)
        ctx.provider = "claude-cli"
        ctx.config = {}
        rendered = _format_budget_window_spend(ctx)
        assert rendered == "4,000/5,000 tokens"

    def test_never_raises_when_ledger_read_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Best-effort contract: a reporting failure must never break or slow
        # the run it measures — mirrors spend.ceiling_tripped's own
        # headroom-warning try/except.
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "15.00")

        def _boom(*a, **k):
            raise RuntimeError("ledger read exploded")

        monkeypatch.setattr(spend, "spend_today", _boom)
        ctx = _finalize_ctx(tmp_path)
        ctx.provider = "api"
        ctx.config = {}
        assert _format_budget_window_spend(ctx) is None


def _write_ledger_record(ledger: Path, **fields: object) -> None:
    """Append one raw ledger record with a ``ts`` of right now (UTC), so
    :func:`athenaeum.spend.spend_today`'s "since start of today" window
    picks it up regardless of when the suite runs. Mirrors
    ``test_spend.py``'s own direct-ledger-write pattern (writing a raw
    record rather than a real ``TokenUsage`` sidesteps depending on the
    active per-model rate table for a pinned dollar/token figure).
    """
    record = {"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    record.update(fields)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# End-to-end through the finalize phase: exit code, marker line, and the
# --allow-degraded / --strict-budget interaction (AC1, AC3, AC4).
# ---------------------------------------------------------------------------


class TestLibrarianRunFinalizeIntegration:
    def test_spend_ceiling_refusal_exits_nonzero_and_logs_marker(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC1 + AC4: files=0 + an early-stop reason does not look like
        success -- assert on BOTH the exit code and the marker line."""
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=0)
        ctx.raw_files = ["a.md", "b.md"]
        ctx.deferred_refs = ["a.md", "b.md"]
        ctx.entity_exit_reason = "spend-ceiling"
        ctx.spend_ceiling_tripped = True

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum.librarian")
        rc = _run_finalize_phase(ctx)

        # The exit code alone must be distinguishable from a genuine success.
        assert rc == EXIT_LIBRARIAN_REFUSAL
        assert rc != 0

        markers = [
            r
            for r in caplog.records
            if r.getMessage().startswith("librarian-run-degraded")
        ]
        assert len(markers) == 1, [r.getMessage() for r in caplog.records]
        assert markers[0].levelno == logging.ERROR
        assert markers[0].getMessage() == "librarian-run-degraded reason=spend-ceiling files=0"

        # The plain success line must NOT also fire alongside the refusal.
        messages = [r.getMessage() for r in caplog.records]
        assert not any(m.startswith("Done:") for m in messages), messages

    def test_marker_includes_spend_window_when_configured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC2: reason=budget/spend-ceiling renders the budget window's
        spend alongside the configured cap."""
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "15.00")
        ledger = tmp_path / "spend.jsonl"
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(ledger))
        _write_ledger_record(ledger, provider="anthropic", estimated_cost_usd=15.33)

        ctx = _finalize_ctx(tmp_path)
        ctx.provider = "api"
        ctx.usage = TokenUsage(api_calls=0)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = ["a.md"]
        ctx.entity_exit_reason = "spend-ceiling"
        ctx.spend_ceiling_tripped = True

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum.librarian")
        rc = _run_finalize_phase(ctx)

        assert rc == EXIT_LIBRARIAN_REFUSAL
        markers = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("librarian-run-degraded")
        ]
        assert markers == [
            "librarian-run-degraded reason=spend-ceiling files=0 spend=$15.33/$15.00"
        ]

    def test_allow_degraded_exits_zero_but_marker_still_fires(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC3: --allow-degraded (ctx.allow_degraded) makes the run exit 0
        even when the refusal predicate holds -- but the marker line is
        STILL emitted."""
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=0)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = ["a.md"]
        ctx.entity_exit_reason = "budget"
        ctx.allow_degraded = True

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum.librarian")
        rc = _run_finalize_phase(ctx)

        assert rc == 0
        markers = [
            r
            for r in caplog.records
            if r.getMessage().startswith("librarian-run-degraded")
        ]
        assert len(markers) == 1, [r.getMessage() for r in caplog.records]
        assert markers[0].levelno == logging.ERROR

    def test_strict_budget_and_allow_degraded_together_strict_budget_wins(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When both flags are set, --strict-budget's broader "any
        deferral" check runs FIRST in the return-code cascade and wins
        (returns 1) -- --allow-degraded only ever waives the NARROWER
        athenaeum#1135 zero-files refusal, and never gets a chance to apply
        here. The marker line still fires either way."""
        ctx = _finalize_ctx(tmp_path, strict_budget=True)
        ctx.usage = TokenUsage(api_calls=0)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = ["a.md"]
        ctx.entity_exit_reason = "budget"
        ctx.allow_degraded = True

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum.librarian")
        rc = _run_finalize_phase(ctx)

        assert rc == 1
        markers = [
            r
            for r in caplog.records
            if r.getMessage().startswith("librarian-run-degraded")
        ]
        assert len(markers) == 1, [r.getMessage() for r in caplog.records]

    def test_deadline_trip_with_zero_files_still_returns_75_not_3(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A wall-clock deadline trip is ALREADY distinguishable from
        success by exit code (75, EXIT_GRACEFUL_PARTIAL) regardless of
        files committed -- EXIT_LIBRARIAN_REFUSAL must never override it.
        The marker line still fires (the predicate holds: reason=deadline,
        files=0) even though the RETURN code stays 75."""
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=0)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = ["a.md"]
        ctx.entity_exit_reason = "deadline"
        ctx.deadline_tripped = True

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum.librarian")
        rc = _run_finalize_phase(ctx)

        assert rc == EXIT_GRACEFUL_PARTIAL
        markers = [
            r
            for r in caplog.records
            if r.getMessage().startswith("librarian-run-degraded")
        ]
        assert len(markers) == 1, [r.getMessage() for r in caplog.records]
        assert "reason=deadline files=0" in markers[0].getMessage()

    def test_clean_run_no_marker_no_refusal(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Negative control: a genuinely clean run (reason=completed, files
        committed) never logs the marker and exits 0."""
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=4)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = []
        ctx.entity_exit_reason = "completed"
        ctx.files_processed_count = 1

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum.librarian")
        rc = _run_finalize_phase(ctx)

        assert rc == 0
        messages = [r.getMessage() for r in caplog.records]
        assert not any(m.startswith("librarian-run-degraded") for m in messages), messages


# ---------------------------------------------------------------------------
# Full end-to-end through the REAL entity loop (not the finalize-only
# fabricated-context tests above): a real ``spend.ceiling_tripped()`` breach
# at the very first file (calls=0), exercising the actual
# ``ctx.spend_ceiling_tripped`` flag site, the real ``manifest_reason``
# classification branch, and ``_write_deferred_manifest``'s new
# "spend-ceiling" header -- none of which the fabricated-RunContext tests
# above ever reach (they set ``ctx.entity_exit_reason`` directly rather
# than driving the loop that derives it).
# ---------------------------------------------------------------------------


class TestSpendCeilingRealEntityLoop:
    def test_spend_ceiling_trip_at_first_file_is_a_refusal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The issue's exact reproduction: budget already exhausted before
        the entity loop claims its first file (calls=0, files=0)."""
        root = _seed_knowledge_root(tmp_path, n_files=2)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _fake_process_one_factory()
        )
        monkeypatch.setattr(
            "athenaeum.librarian.spend.ceiling_tripped",
            lambda *a, **k: "per-day API dollar ceiling reached ($15.33/$15.00 today)",
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
        )

        assert rc == EXIT_LIBRARIAN_REFUSAL

        # The manifest is labelled "spend-ceiling", distinct from the plain
        # API-call-COUNT "budget" header (issue athenaeum#1135's Proposal 1
        # -- the two used to be indistinguishable).
        manifest_text = (root / "wiki" / "_deferred_work.md").read_text(
            encoding="utf-8"
        )
        assert "spend ceiling exhausted" in manifest_text
        assert "budget exhausted" not in manifest_text

        # The run-summary line's entity segment carries the distinct reason.
        messages = [r.getMessage() for r in caplog.records]
        summary_lines = [m for m in messages if m.startswith("librarian-run-summary")]
        assert len(summary_lines) == 1
        assert "calls=0" in summary_lines[0]
        assert "files=0" in summary_lines[0]
        assert "reason=spend-ceiling" in summary_lines[0]

        # The marker line fires with the SAME reason.
        markers = [m for m in messages if m.startswith("librarian-run-degraded")]
        assert markers == ["librarian-run-degraded reason=spend-ceiling files=0"]

    def test_allow_degraded_via_real_run_exits_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC3 end-to-end: the CLI-facing ``allow_degraded=True`` kwarg on
        the real ``run()`` entrypoint waives the exit code but not the
        marker line."""
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _fake_process_one_factory()
        )
        monkeypatch.setattr(
            "athenaeum.librarian.spend.ceiling_tripped",
            lambda *a, **k: "per-day API dollar ceiling reached (forced)",
        )
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            allow_degraded=True,
        )

        assert rc == 0
        messages = [r.getMessage() for r in caplog.records]
        markers = [m for m in messages if m.startswith("librarian-run-degraded")]
        assert markers, messages


# ---------------------------------------------------------------------------
# AC5 — athenaeum drain's existing loud zero-progress refusal is UNCHANGED.
# This module makes no edits to src/athenaeum/drain.py; this test simply
# pins that drain's own distinct log line still fires exactly as before,
# so a regression there would be caught here too (full coverage remains
# test_drain.py's job).
# ---------------------------------------------------------------------------


class TestDrainUnaffected:
    def test_drain_zero_progress_still_refuses_loudly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.drain import run_drain

        root = tmp_path / "knowledge"
        (root / "raw").mkdir(parents=True)
        (root / "wiki").mkdir(parents=True)
        ledger = tmp_path / "spend.jsonl"

        def _stub_run(**kwargs) -> int:
            return 0

        def _stub_backlog(_root: Path) -> int:
            return 3  # never drains -> zero progress every window

        caplog.clear()
        caplog.set_level(logging.ERROR, logger="athenaeum.drain")
        rc = run_drain(
            knowledge_root=root,
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            max_usd=100.0,
            max_files=None,
            ledger_path=ledger,
            run_fn=_stub_run,
            backlog_fn=_stub_backlog,
        )

        assert rc == 1
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "made ZERO progress" in m and "stopping loudly" in m for m in messages
        ), messages
