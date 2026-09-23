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

Issue athenaeum#1177 adds ``TestAllCallsFailedRegression`` below: the mandatory
regression test for the four-day incident where credits were exhausted and
every entity-phase LLM call raised ``BadRequestError`` — the predicate
above stayed untripped (``api_calls`` only ever incremented on SUCCESS, so
an all-failing run read as "0 calls", indistinguishable from idle) and the
entity phase's ``reason`` read ``"completed"``. That test drives a REAL
``athenaeum run()`` end to end with a fake Anthropic client that raises on
every call, rather than stubbing ``process_one`` out (the pattern most
other suites use) — the fix lives inside the real call path
(``tiers._timed_llm_call``), so a test that bypasses it would not exercise
what changed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import anthropic
import httpx
import pytest

from athenaeum import zero_yield
from athenaeum.config import resolve_cache_dir
from athenaeum.librarian import (
    DEFAULT_ZERO_YIELD_ALERT_THRESHOLD,
    DEGRADED_NO_API_KEY_PREFIX,
    EXIT_GRACEFUL_PARTIAL,
    ZERO_YIELD_ALERT_PREFIX,
    ZERO_YIELD_PREFIX,
    RunContext,
    _auto_memory_reason,
    _run_finalize_phase,
    _zero_yield_tripped,
    librarian_zero_yield_alert_threshold,
    run,
)
from athenaeum.models import TokenUsage
from tests.conftest import FakeLLMClient
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
# librarian_zero_yield_alert_threshold — env > yaml > default (issue athenaeum#1177, AC2)
# ---------------------------------------------------------------------------


class TestResolveZeroYieldAlertThreshold:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_ZERO_YIELD_ALERT_THRESHOLD", raising=False)
        assert (
            librarian_zero_yield_alert_threshold(None) == DEFAULT_ZERO_YIELD_ALERT_THRESHOLD
        )
        assert (
            librarian_zero_yield_alert_threshold({})
            == DEFAULT_ZERO_YIELD_ALERT_THRESHOLD
        )

    def test_yaml_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_ZERO_YIELD_ALERT_THRESHOLD", raising=False)
        assert (
            librarian_zero_yield_alert_threshold(
                {"librarian": {"zero_yield_alert_threshold": 5}}
            )
            == 5
        )

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_ZERO_YIELD_ALERT_THRESHOLD", "7")
        assert (
            librarian_zero_yield_alert_threshold(
                {"librarian": {"zero_yield_alert_threshold": 5}}
            )
            == 7
        )

    def test_below_one_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_ZERO_YIELD_ALERT_THRESHOLD", raising=False)
        assert (
            librarian_zero_yield_alert_threshold(
                {"librarian": {"zero_yield_alert_threshold": 0}}
            )
            == DEFAULT_ZERO_YIELD_ALERT_THRESHOLD
        )

    def test_bool_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `zero_yield_alert_threshold: yes` parses as True (int subclass) --
        # must NOT become a threshold of 1.
        monkeypatch.delenv("ATHENAEUM_ZERO_YIELD_ALERT_THRESHOLD", raising=False)
        assert (
            librarian_zero_yield_alert_threshold(
                {"librarian": {"zero_yield_alert_threshold": True}}
            )
            == DEFAULT_ZERO_YIELD_ALERT_THRESHOLD
        )

    def test_non_numeric_env_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_ZERO_YIELD_ALERT_THRESHOLD", "not-a-number")
        assert (
            librarian_zero_yield_alert_threshold(None) == DEFAULT_ZERO_YIELD_ALERT_THRESHOLD
        )


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

    def test_true_when_every_call_attempted_but_none_succeeded(
        self, tmp_path: Path
    ) -> None:
        """athenaeum#1177: the shape a credits-exhausted run actually produces --
        ``api_calls`` stays 0 (only ever incremented on a SUCCESSFUL
        response) but ``attempted_calls`` is nonzero (bumped before
        dispatch, regardless of outcome). Before this fix, condition 1
        checked ``api_calls`` alone and this run read as idle, not
        wasteful -- the root cause of the four-day silent incident."""
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=0, attempted_calls=6)
        ctx.files_processed_count = 0
        ctx.deferred_refs = []
        assert _zero_yield_tripped(ctx, []) is True

    def test_false_when_neither_api_calls_nor_attempted_calls_set(
        self, tmp_path: Path
    ) -> None:
        """Companion negative case: a genuinely idle run (nothing attempted
        at all) must still read as idle, not wasteful."""
        ctx = _finalize_ctx(tmp_path)
        ctx.usage = TokenUsage(api_calls=0, attempted_calls=0)
        ctx.files_processed_count = 0
        ctx.deferred_refs = []
        assert _zero_yield_tripped(ctx, []) is False


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


# ---------------------------------------------------------------------------
# _auto_memory_reason (issue athenaeum#1177, AC3, auto-memory phase)
# ---------------------------------------------------------------------------


class TestAutoMemoryReason:
    def test_completed_when_nothing_attempted(self) -> None:
        """A genuinely idle auto-memory pass (nothing to detect/resolve this
        run) is a real completion, not a failure."""
        assert _auto_memory_reason({"haiku_calls": 0, "resolve_calls": 0}) == "completed"

    def test_completed_when_attempts_succeeded(self) -> None:
        assert (
            _auto_memory_reason(
                {
                    "haiku_calls": 4,
                    "haiku_calls_succeeded": 4,
                    "resolve_calls": 1,
                    "resolve_calls_succeeded": 1,
                }
            )
            == "completed"
        )

    def test_all_calls_failed_when_every_attempt_errored(self) -> None:
        """The shape a credits-exhausted run produces: attempts made,
        zero landed a response -- the ledger would show 0 tokens for all
        of them."""
        assert (
            _auto_memory_reason(
                {
                    "haiku_calls": 20,
                    "haiku_calls_succeeded": 0,
                    "resolve_calls": 0,
                    "resolve_calls_succeeded": 0,
                }
            )
            == "all-calls-failed"
        )

    def test_completed_when_some_but_not_all_attempts_succeeded(self) -> None:
        """A partial failure (some detections landed, some errored) is a
        genuine partial completion, not the all-failed case AC3 targets."""
        assert (
            _auto_memory_reason(
                {
                    "haiku_calls": 5,
                    "haiku_calls_succeeded": 2,
                    "resolve_calls": 0,
                    "resolve_calls_succeeded": 0,
                }
            )
            == "completed"
        )

    def test_missing_keys_default_to_zero(self) -> None:
        """A caller (e.g. the merge_only variant's out_stats dict) that has
        not populated the succeeded keys must not crash -- ``.get`` with a
        default, not a KeyError."""
        assert _auto_memory_reason({}) == "completed"

    def test_no_client_configured_is_not_all_calls_failed(self) -> None:
        """Issue athenaeum#1738: ``haiku_calls``/``resolve_calls`` count
        INTENTS -- incremented before the client is consulted -- so a
        keyless run reports attempts it could never have MADE. Reporting
        that as ``all-calls-failed`` emits the credits-exhausted incident
        signature for an unexported environment variable."""
        assert (
            _auto_memory_reason(
                {
                    "haiku_calls": 16,
                    "haiku_calls_succeeded": 0,
                    "resolve_calls": 0,
                    "resolve_calls_succeeded": 0,
                    "llm_client_configured": False,
                }
            )
            == "no-client-configured"
        )

    def test_all_calls_failed_survives_when_a_client_did_exist(self) -> None:
        """Issue athenaeum#1738 narrows ``all-calls-failed``, it does not
        retire it: a client WAS configured, calls were genuinely made, and
        every one of them errored -- still the athenaeum#1177 signature."""
        assert (
            _auto_memory_reason(
                {
                    "haiku_calls": 20,
                    "haiku_calls_succeeded": 0,
                    "resolve_calls": 0,
                    "resolve_calls_succeeded": 0,
                    "llm_client_configured": True,
                }
            )
            == "all-calls-failed"
        )

    def test_absent_client_flag_keeps_pre_1738_classification(self) -> None:
        """A stats dict from a caller that never populated the athenaeum#1738
        key (any older/partial out_stats) classifies exactly as it did
        before -- absence is not read as "no client"."""
        assert (
            _auto_memory_reason({"haiku_calls": 3, "haiku_calls_succeeded": 0})
            == "all-calls-failed"
        )

    def test_no_client_flag_does_not_mask_a_genuine_completion(self) -> None:
        """The flag only ever refines the attempted>0/succeeded==0 branch:
        a keyless run that made no attempts at all is still an
        unremarkable completion, not a degraded-phase report."""
        assert (
            _auto_memory_reason(
                {
                    "haiku_calls": 0,
                    "resolve_calls": 0,
                    "llm_client_configured": False,
                }
            )
            == "completed"
        )


# ---------------------------------------------------------------------------
# End-to-end regression test (issue athenaeum#1177's mandatory AC): a run
# where EVERY entity-phase LLM call errors.
# ---------------------------------------------------------------------------


def _bad_request_error() -> anthropic.BadRequestError:
    """A real anthropic BadRequestError (HTTP 400) -- NON-transient, so
    ``with_retry`` raises it straight through with no retry (mirrors
    ``tests/test_retry.py``'s helper of the same shape)."""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    return anthropic.BadRequestError("Bad request", response=resp, body=None)


class TestAllCallsFailedRegression:
    """The issue's mandatory AC: simulate a run where every call errors;
    assert the summary is not labelled completed AND the zero-yield
    counter advances. Drives a REAL ``run()`` (not a stubbed-out
    ``process_one``) with a fake Anthropic client that raises on every
    ``messages.create`` -- see the module docstring for why."""

    def test_all_entity_calls_failing_trips_zero_yield_and_avoids_completed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from tests.test_librarian_deadline import _seed_knowledge_root

        root = _seed_knowledge_root(tmp_path, n_files=2)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        fake_client = FakeLLMClient(raises=_bad_request_error())
        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake_client)

        # Seed a prior 2-consecutive-zero-yield streak (real predecessor
        # state, not the default fresh one) so this run's increment -- and
        # the AC2 alert threshold crossing at 3 -- are both observable.
        cache_dir = resolve_cache_dir()
        zero_yield.write_state(cache_dir, consecutive=2, deferred_refs=[])

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum")
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=1000,
        )

        # The existing "Failed files" exit-1 contract is unaffected by this
        # fix -- both raw files errored and are retried next run.
        assert rc == 1

        summary_lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("librarian-run-summary")
        ]
        assert len(summary_lines) == 1
        summary = summary_lines[0]
        # Isolate the entity phase's own segment -- wiki-dedup (a separate,
        # unrelated, genuinely-completed phase that never makes an LLM
        # call) legitimately still reads "reason=completed" on the SAME
        # line, so asserting against the whole line would false-positive.
        entity_segment = summary.split("| entity ")[1].split(" | ")[0]

        # AC3: the entity phase segment must NOT read as completed, and
        # must name the error class.
        assert "reason=completed" not in entity_segment
        assert "reason=all-calls-failed:BadRequestError" in entity_segment
        assert "calls=0" in entity_segment  # api_calls (successes) genuinely 0

        # AC1: the zero-yield counter actually advanced across the run
        # boundary -- 2 (seeded) + 1 = 3.
        assert "zero_yield=3" in summary
        persisted = zero_yield.load_state(cache_dir)
        assert persisted["consecutive"] == 3

        # AC2: the alert fires once the streak reaches the (default) N=3
        # threshold, as a SEPARATE, distinctly-prefixed ERROR line from the
        # per-trip WARNING.
        alert_lines = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.ERROR and ZERO_YIELD_ALERT_PREFIX in r.getMessage()
        ]
        assert len(alert_lines) == 1
        assert "3" in alert_lines[0]

        warning_lines = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and ZERO_YIELD_PREFIX in r.getMessage()
        ]
        assert len(warning_lines) == 1


# ---------------------------------------------------------------------------
# End-to-end regression test (issue athenaeum#1738): a KEYLESS dry run must
# announce itself instead of emitting degraded phases that read as findings
# about the corpus.
# ---------------------------------------------------------------------------


class TestKeylessDryRunAnnouncesItself:
    """Issue athenaeum#1738's mandatory AC: drive a REAL ``run(dry_run=True)``
    with no resolvable ``ANTHROPIC_API_KEY`` and assert both halves of the
    fix at once -- the single up-front WARNING (AC1/AC5) and the phase
    reason that no longer borrows athenaeum#1177's credits-exhausted
    signature (AC3/AC5) -- while the run still exits 0 and still does its
    non-LLM work (AC2).

    Drives the real entry point rather than ``_run_preconditions`` alone
    because the silent shape this guards against was a property of the
    whole run's OUTPUT, not of the gate in isolation.
    """

    def test_keyless_dry_run_warns_once_exits_zero_and_avoids_all_calls_failed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from tests.test_librarian_deadline import _seed_knowledge_root

        root = _seed_knowledge_root(tmp_path, n_files=2)
        # The real condition: the key simply is not exported into this
        # shell -- the normal state of any shell that has not sourced the
        # operator's config.env.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum")
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=True,
            max_api_calls=100,
            max_runtime=1000,
        )

        # AC2: the dry-run exemption is intact -- the run is still allowed,
        # still exits 0, and still performed its non-LLM phases (it got far
        # enough to emit a summary at all).
        assert rc == 0

        # AC1: exactly ONE warning names the run as degraded, and it names
        # both the variable and the phases the reader must discount. The
        # prefix is what makes "exactly one" assertable -- the per-cluster
        # contradiction detector warns about the same missing key too.
        degraded = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
            and DEGRADED_NO_API_KEY_PREFIX in r.getMessage()
        ]
        assert len(degraded) == 1
        assert "ANTHROPIC_API_KEY" in degraded[0]

        summary_lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("librarian-run-summary")
        ]
        assert len(summary_lines) == 1

        # ...and it precedes every phase's output, which is the point: a
        # reader hits it before the first degraded line, not after.
        warn_index = next(
            i
            for i, r in enumerate(caplog.records)
            if DEGRADED_NO_API_KEY_PREFIX in r.getMessage()
        )
        summary_indices = [
            i
            for i, r in enumerate(caplog.records)
            if r.getMessage().startswith("librarian-run-summary")
        ]
        assert summary_indices, "run emitted no summary line to order against"
        assert warn_index < summary_indices[0]

        # AC3: no phase on this run may borrow athenaeum#1177's
        # credits-exhausted signature for a missing environment variable.
        # (The positive assertion -- that the auto-memory phase reports
        # ``no-client-configured`` -- needs a corpus with a cluster in it;
        # see ``test_keyless_merge_only_dry_run_reports_no_client_configured``
        # below, which drives the same ``run()`` down the merge_only path.)
        assert "reason=all-calls-failed" not in summary_lines[0]

    def test_keyless_merge_only_dry_run_reports_no_client_configured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issue athenaeum#1738 AC3/AC5, end to end and POSITIVELY: a keyless
        dry run over a corpus that actually has a multi-member cluster
        reaches the C4 detector, counts the intent, lands nothing -- and
        the run summary's auto-memory phase must say WHY.

        ``merge_only`` is the cheapest real ``run()`` that gets there: it
        reads a cluster JSONL that already exists instead of recomputing
        embeddings, which is orthogonal to what this issue changed."""
        from tests.test_librarian_deadline import _seed_knowledge_root
        from tests.test_librarian_run_summary import (
            _write_am_file,
            _write_cluster_jsonl,
            _write_config,
        )

        root = _seed_knowledge_root(tmp_path, n_files=0)
        scope = root / "raw" / "auto-memory" / "-Users-tristankromer-Code"
        _write_am_file(
            scope,
            "feedback_v1.md",
            frontmatter_name="v1",
            origin_session_id="s-111",
            origin_turn=1,
            sources=[
                {"session": "s-111", "turn": 1, "date": "2026-04-10", "excerpt": "x"}
            ],
            body="Commit prior-session debris directly to develop.",
        )
        _write_am_file(
            scope,
            "feedback_v2.md",
            frontmatter_name="v2",
            origin_session_id="s-222",
            origin_turn=2,
            sources=[
                {"session": "s-222", "turn": 2, "date": "2026-04-11", "excerpt": "y"}
            ],
            body="Park prior-session debris on a WIP branch.",
        )
        _write_cluster_jsonl(
            root,
            [
                {
                    "cluster_id": "code-0001",
                    "member_paths": [
                        "-Users-tristankromer-Code/feedback_v1.md",
                        "-Users-tristankromer-Code/feedback_v2.md",
                    ],
                    "centroid_score": 0.62,
                    "rationale": "cosine >= 0.55",
                }
            ],
        )
        _write_config(root)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        caplog.clear()
        caplog.set_level(logging.INFO, logger="athenaeum")
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=True,
            merge_only=True,
            max_api_calls=100,
            max_runtime=1000,
        )
        assert rc == 0

        summary_lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("librarian-run-summary")
        ]
        assert len(summary_lines) == 1
        auto_memory_segment = summary_lines[0].split("| auto-memory ")[1].split(" | ")[0]
        # The detector intent WAS counted -- this is the exact shape that
        # used to read as the credits-exhausted incident signature.
        assert "detector_haiku=1" in auto_memory_segment
        assert "reason=no-client-configured" in auto_memory_segment
        assert "reason=all-calls-failed" not in auto_memory_segment
