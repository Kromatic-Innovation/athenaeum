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
        # input_tokens/output_tokens set explicitly (issue athenaeum#1137's
        # cache-inclusive basis reads those fields, not total_tokens
        # directly) — a real ledger row always carries them.
        _write_ledger_record(
            ledger,
            provider="claude-cli",
            input_tokens=4000,
            output_tokens=0,
            total_tokens=4000,
        )

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


# ---------------------------------------------------------------------------
# athenaeum#1136 AC2 — the resolved ``ctx.run_type`` reaches the actual
# ledger write in ``_run_finalize_phase`` (the normal end-of-run site),
# not just the RunContext field.
# ---------------------------------------------------------------------------


class TestRunTypeThreadedIntoLedgerWrite:
    def test_nightly_run_type_is_written_to_the_ledger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger = tmp_path / "spend.jsonl"
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(ledger))

        ctx = _finalize_ctx(tmp_path)
        ctx.provider = "api"
        ctx.config = {}
        ctx.run_type = "librarian-nightly"
        ctx.usage = TokenUsage()
        ctx.usage.add(500, 100, 0, 0, model="claude-3-5-haiku-20241022")

        rc = _run_finalize_phase(ctx)

        assert rc == 0
        records = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["run_type"] == "librarian-nightly"

    def test_unset_run_type_still_writes_the_unchanged_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Belt-and-suspenders: even if a caller somehow reaches this phase
        with ``ctx.run_type`` still ``None`` (bypassing ``_resolve_run_config``,
        e.g. a hand-built RunContext in a future test), the ledger write
        falls back to ``RUN_TYPE_LIBRARIAN`` rather than writing a literal
        ``None`` or raising."""
        ledger = tmp_path / "spend.jsonl"
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(ledger))

        ctx = _finalize_ctx(tmp_path)
        ctx.provider = "api"
        ctx.config = {}
        assert ctx.run_type is None
        ctx.usage = TokenUsage()
        ctx.usage.add(500, 100, 0, 0, model="claude-3-5-haiku-20241022")

        rc = _run_finalize_phase(ctx)

        assert rc == 0
        records = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert records[0]["run_type"] == "librarian"


# ---------------------------------------------------------------------------
# athenaeum#1136 AC4 — coordinate with athenaeum#1135 rather than duplicate it.
# The athenaeum#1135 refusal mechanism (EXIT_LIBRARIAN_REFUSAL + the
# librarian-run-degraded marker) is untouched by athenaeum#1136 -- this
# asserts the INTERACTION: a nightly run that a spend-ceiling trip starved
# (the exact athenaeum#1136 bug, reproduced under forced UTC-day accounting
# in tests/test_spend_accounting_timezone.py::TestNightlyLocalDayAlignment)
# still exits EXIT_LIBRARIAN_REFUSAL with the marker line, tagged with the
# NIGHTLY run_type -- proving athenaeum#1135's exit-code contract and
# athenaeum#1136's run_type attribution compose cleanly, without a second
# refusal mechanism.
# ---------------------------------------------------------------------------


class TestNightlyInteractionWithRefusalMechanism:
    def test_nightly_starved_by_a_spend_ceiling_still_exits_refusal_with_marker(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = _finalize_ctx(tmp_path)
        ctx.run_type = "librarian-nightly"
        ctx.usage = TokenUsage(api_calls=0)
        ctx.raw_files = ["a.md", "b.md"]
        ctx.deferred_refs = ["a.md", "b.md"]
        # The reason a real ceiling_tripped() call would have returned under
        # the OLD (pre-athenaeum#1136) UTC-day accounting for a nightly landing
        # inside an evening session's exhausted UTC day -- see
        # spend.ceiling_tripped's "per-day API dollar ceiling reached" branch.
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
        assert markers == ["librarian-run-degraded reason=spend-ceiling files=0"]


# ---------------------------------------------------------------------------
# Issue athenaeum#1283 — the athenaeum#1135 refusal verdict is computed ONCE and
# persisted, so ``athenaeum status`` (a SEPARATE process, run between
# librarian runs) can see it. Before this, the verdict existed only as the
# ``librarian-run-degraded`` log line above and the process exit code --
# both die with the run. This closes that gap WITHOUT touching athenaeum#1135's
# own predicate, exit codes, or marker-line text (pinned unchanged by the
# suite above).
# ---------------------------------------------------------------------------


class TestSingleVerdictSite:
    """``_librarian_run_refusal_tripped`` must be evaluated exactly ONCE per
    run and reused -- not re-derived at the marker-line site and again at
    the exit-code check, the pre-athenaeum#1283 shape."""

    def test_predicate_called_exactly_once_on_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as librarian_mod

        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=0)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = ["a.md"]
        ctx.entity_exit_reason = "spend-ceiling"
        ctx.spend_ceiling_tripped = True

        calls: list[RunContext] = []
        real = librarian_mod._librarian_run_refusal_tripped

        def _spy(c: RunContext) -> bool:
            calls.append(c)
            return real(c)

        monkeypatch.setattr(librarian_mod, "_librarian_run_refusal_tripped", _spy)
        rc = _run_finalize_phase(ctx)

        assert rc == EXIT_LIBRARIAN_REFUSAL
        assert len(calls) == 1, "predicate re-derived instead of reusing ctx.librarian_refusal"
        assert ctx.librarian_refusal is True

    def test_predicate_called_exactly_once_on_a_clean_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as librarian_mod

        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=4)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = []
        ctx.entity_exit_reason = "completed"
        ctx.files_processed_count = 1

        calls: list[RunContext] = []
        real = librarian_mod._librarian_run_refusal_tripped

        def _spy(c: RunContext) -> bool:
            calls.append(c)
            return real(c)

        monkeypatch.setattr(librarian_mod, "_librarian_run_refusal_tripped", _spy)
        rc = _run_finalize_phase(ctx)

        assert rc == 0
        assert len(calls) == 1
        assert ctx.librarian_refusal is False


class TestRefusalPersistedToLedger:
    """The verdict lands in the athenaeum#1102 run-summary ledger (no new state
    file), via the SAME ``ctx.librarian_refusal`` :class:`TestSingleVerdictSite`
    just pinned as single-source.

    THREE record shapes matter, not two -- see ``run_summary_log.py``'s
    ``refusal_in_record`` docstring for the full contract this class pins
    the writer side of: a tripped refusal writes ``{"tripped": True, ...}``,
    an EVALUATED clean run writes ``{"tripped": False}`` (present, not
    omitted), and only a run whose verdict was NEVER evaluated
    (``ctx.librarian_refusal is None`` — the ``stop_on_deadline`` path,
    covered separately below in ``TestUnevaluatedVerdictOmitsRefusalField``)
    omits the key entirely."""

    def test_refusal_run_writes_the_refusal_field(self, tmp_path: Path) -> None:
        from athenaeum.config import resolve_cache_dir
        from athenaeum.run_summary_log import (
            default_run_summary_ledger_path,
            read_run_summary_ledger,
        )

        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=0)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = ["a.md"]
        ctx.entity_exit_reason = "budget"
        # write_run_summary_record no-ops on an empty profile (issue athenaeum#1102's
        # deliberate "nothing yet to report" gate — see its own docstring); a
        # real run always appends at least one phase segment (the entity
        # phase itself, via ``_run_entity_tier_phase``), so a fabricated
        # finalize-only context has to supply one to exercise the ledger
        # write at all. Mirrors ``_PROFILE`` in ``test_run_summary_log.py``.
        ctx.run_profile = [("entity", 1.0, {"reason": "budget", "files": 0})]

        rc = _run_finalize_phase(ctx)
        assert rc == EXIT_LIBRARIAN_REFUSAL

        ledger_path = default_run_summary_ledger_path(resolve_cache_dir())
        records = read_run_summary_ledger(ledger_path)
        assert len(records) == 1
        assert records[0]["refusal"] == {
            "tripped": True,
            "reason": "budget",
            "files": 0,
        }
        assert records[0]["v"] == 3

    def test_evaluated_clean_run_writes_tripped_false_not_an_omission(
        self, tmp_path: Path
    ) -> None:
        # The athenaeum#1283 correctness fix, at the real call site: a run
        # that reaches ``_run_finalize_phase`` and is evaluated as NOT a
        # refusal (``ctx.librarian_refusal is False``, not ``None``) must
        # write a PRESENT ``{"tripped": False}`` record -- an omitted key
        # here would be indistinguishable from a run whose verdict was
        # never evaluated at all (see ``TestUnevaluatedVerdictOmitsRefusalField``
        # below), which is exactly the bug this follow-up closes.
        from athenaeum.config import resolve_cache_dir
        from athenaeum.run_summary_log import (
            default_run_summary_ledger_path,
            read_run_summary_ledger,
        )

        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=4)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = []
        ctx.entity_exit_reason = "completed"
        ctx.files_processed_count = 1
        ctx.run_profile = [("entity", 1.0, {"reason": "completed", "files": 1})]

        rc = _run_finalize_phase(ctx)
        assert rc == 0
        assert ctx.librarian_refusal is False

        ledger_path = default_run_summary_ledger_path(resolve_cache_dir())
        records = read_run_summary_ledger(ledger_path)
        assert len(records) == 1
        assert records[0]["refusal"] == {"tripped": False}


class TestUnevaluatedVerdictOmitsRefusalField:
    """The regression this follow-up review named: a run whose verdict was
    NEVER evaluated (``ctx.librarian_refusal`` still ``None`` when
    ``emit_run_summary`` writes the ledger record) must write a record
    ``refusal_in_record`` reads as ``None`` ("cannot speak"), never
    ``False`` ("confirmed clean").

    Driven through the REAL ``RunContext.stop_on_deadline`` code path (not
    a synthetic direct-``emit_run_summary`` call) — this is the concrete,
    non-hypothetical case named in the review: a wall-clock deadline trip
    in a pre-entity phase (wiki-dedup boundary, merge_only/auto-memory
    catch, post-compile boundary — every call site routes through
    ``stop_on_deadline``, per its own docstring) calls ``emit_run_summary``
    and returns ``EXIT_GRACEFUL_PARTIAL`` straight to ``run()``'s caller,
    entirely BEFORE ``_run_finalize_phase`` -- where ``ctx.librarian_refusal``
    is set -- ever runs. ``ctx.dry_run = True`` sidesteps
    ``stop_on_deadline``'s ``FilesystemStore(...).snapshot()`` /
    ``_maybe_push_after_run`` calls (which need a real git repo) without
    touching the code path under test — those calls are gated on
    ``if not self.dry_run`` and sit BEFORE the ``emit_run_summary()`` call
    this test cares about, so skipping them changes nothing about what is
    being verified.
    """

    def test_stop_on_deadline_writes_an_unevaluated_not_a_clean_record(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.config import resolve_cache_dir
        from athenaeum.run_summary_log import (
            default_run_summary_ledger_path,
            read_run_summary_ledger,
            refusal_in_record,
        )

        ctx = _finalize_ctx(tmp_path)
        ctx.dry_run = True
        ctx.max_runtime = 3600  # stop_on_deadline's WARNING renders this with %d
        ctx.run_profile = [("wiki-dedup", 1.0, {"reason": "completed"})]

        assert ctx.librarian_refusal is None  # never evaluated -- the whole point

        rc = ctx.stop_on_deadline("wiki-dedup boundary (issue athenaeum#396)")

        assert rc == EXIT_GRACEFUL_PARTIAL
        # _run_finalize_phase was never reached, so the verdict is STILL
        # unevaluated after the call -- this is what makes the ledger
        # record's omitted key an honest "cannot speak", not a stale read.
        assert ctx.librarian_refusal is None

        ledger_path = default_run_summary_ledger_path(resolve_cache_dir())
        records = read_run_summary_ledger(ledger_path)
        assert len(records) == 1
        assert "refusal" not in records[0]
        assert records[0]["v"] == 3
        # The actual reader contract, exercised end to end: an omitted key
        # on a v3 record reads as None (cannot speak), NOT False (clean).
        assert refusal_in_record(records[0]) is None


class TestRefusalVisibleInStatusAcrossRuns:
    """The actual regression this issue names: a run with ``api_calls == 0``
    AND ``attempted_calls == 0`` -- the exact case athenaeum#899's zero-yield
    counter excludes by design -- must still make ``athenaeum status`` read
    non-healthy. Drives the SAME finalize call the process-level
    ``athenaeum run`` entry point uses, then reads status back exactly as a
    separate ``athenaeum status`` invocation would (via the athenaeum#1102 ledger
    under the cache dir the ``_isolate_cache_dir`` autouse fixture redirects
    per test) -- proving the two are actually connected, not merely that
    each half works in isolation.
    """

    def test_zero_calls_refusal_visible_in_status_while_zero_yield_stays_zero(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.config import resolve_cache_dir
        from athenaeum.status import format_status, status
        from athenaeum.zero_yield import load_state as load_zero_yield_state

        ctx = _finalize_ctx(tmp_path)
        (ctx.knowledge_root / "raw").mkdir(parents=True, exist_ok=True)

        # The Motivation's exact reproduction: a spend-ceiling refusal that
        # made NO calls and ATTEMPTED none -- calls=0 alone is already
        # excluded by athenaeum#899's ``api_calls > 0`` leg, but this pins the
        # stronger claim: even WITH the athenaeum#1177 ``attempted_calls``
        # widening, a true zero-call refusal still never trips zero-yield.
        ctx.usage = TokenUsage(api_calls=0)
        assert ctx.usage.attempted_calls == 0
        ctx.raw_files = ["a.md", "b.md"]
        ctx.deferred_refs = ["a.md", "b.md"]
        ctx.entity_exit_reason = "spend-ceiling"
        ctx.spend_ceiling_tripped = True
        ctx.run_profile = [("entity", 1.0, {"reason": "spend-ceiling", "files": 0})]

        rc = _run_finalize_phase(ctx)
        assert rc == EXIT_LIBRARIAN_REFUSAL

        # Negative control: athenaeum#899's own predicate is UNCHANGED and does
        # NOT catch this run -- pins that the new signal, not a change to
        # zero-yield's semantics, is what closes the gap.
        assert ctx.zero_yield_tripped is False
        assert (
            load_zero_yield_state(resolve_cache_dir())["consecutive"] == 0
        )

        # This is the defect from the issue's Motivation: before athenaeum#1283,
        # nothing here would show a problem at all.
        info = status(ctx.knowledge_root)
        assert info["zero_yield_consecutive"] == 0
        assert info["librarian_refusal_consecutive"] == 1
        assert info["librarian_refusal_reason"] == {
            "tripped": True,
            "reason": "spend-ceiling",
            "files": 0,
        }

        rendered = format_status(info)
        assert "librarian-run-refusal" in rendered
        assert "spend-ceiling" in rendered
        assert "Zero-yield" not in rendered

    def test_two_refusals_in_a_row_streak_to_two(self, tmp_path: Path) -> None:
        from athenaeum.status import status

        for _ in range(2):
            ctx = _finalize_ctx(tmp_path)
            (ctx.knowledge_root / "raw").mkdir(parents=True, exist_ok=True)
            ctx.usage = TokenUsage(api_calls=0)
            ctx.raw_files = ["a.md"]
            ctx.deferred_refs = ["a.md"]
            ctx.entity_exit_reason = "spend-ceiling"
            ctx.spend_ceiling_tripped = True
            ctx.run_profile = [("entity", 1.0, {"reason": "spend-ceiling", "files": 0})]
            rc = _run_finalize_phase(ctx)
            assert rc == EXIT_LIBRARIAN_REFUSAL

        info = status(ctx.knowledge_root)
        assert info["librarian_refusal_consecutive"] == 2

    def test_a_later_clean_run_resets_status_to_quiet(self, tmp_path: Path) -> None:
        from athenaeum.status import status

        ctx = _finalize_ctx(tmp_path)
        (ctx.knowledge_root / "raw").mkdir(parents=True, exist_ok=True)
        ctx.usage = TokenUsage(api_calls=0)
        ctx.raw_files = ["a.md"]
        ctx.deferred_refs = ["a.md"]
        ctx.entity_exit_reason = "spend-ceiling"
        ctx.spend_ceiling_tripped = True
        assert _run_finalize_phase(ctx) == EXIT_LIBRARIAN_REFUSAL

        # Same ``tmp_path`` -> same default ``knowledge_root`` as ``ctx``
        # above (``_finalize_ctx``/``_make_ctx`` derive it from ``tmp_path``
        # alone), so this is a second run against the SAME knowledge base.
        ctx2 = _finalize_ctx(tmp_path)
        ctx2.usage = TokenUsage(api_calls=4)
        ctx2.raw_files = ["b.md"]
        ctx2.deferred_refs = []
        ctx2.entity_exit_reason = "completed"
        ctx2.files_processed_count = 1
        assert _run_finalize_phase(ctx2) == 0

        info = status(ctx.knowledge_root)
        assert info["librarian_refusal_consecutive"] == 0
        assert info["librarian_refusal_reason"] is None

    def test_status_never_raises_when_ledger_is_missing(self, tmp_path: Path) -> None:
        # No librarian run has ever finalized against this knowledge base --
        # ``status.py``'s documented read-only/side-effect-free contract
        # (module docstring) must hold: a missing ledger reads as "no
        # history", never an exception.
        from athenaeum.status import status

        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        info = status(root)
        assert info["librarian_refusal_consecutive"] == 0
        assert info["librarian_refusal_reason"] is None
