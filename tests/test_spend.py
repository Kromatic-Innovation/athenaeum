"""Tests for the durable LLM-spend ledger (issue #378).

Covers the ledger writer, reader, summariser, the `athenaeum spend` command,
the spend ceiling, and the config resolvers — pinning the invariants that
matter: the two cost paths (subscription tokens vs API dollars) are NEVER
blended, subscription rows carry $0, the four token counters stay separate,
the ledger tolerates a torn trailing line, and the ceiling halts on breach.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from athenaeum import spend
from athenaeum.cli import main
from athenaeum.config import (
    resolve_spend_ledger_enabled,
    resolve_spend_ledger_path,
    resolve_spend_max_tokens_per_day,
    resolve_spend_max_tokens_per_run,
    resolve_spend_max_usd_per_day,
    resolve_spend_max_usd_per_run,
)
from athenaeum.models import TokenUsage

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ledger path isolated to tmp via ATHENAEUM_SPEND_LEDGER."""
    path = tmp_path / "cache" / "spend.jsonl"
    monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(path))
    # Clear any ambient ceiling env so tests are hermetic.
    for var in (
        "ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN",
        "ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY",
        "ATHENAEUM_SPEND_MAX_USD_PER_RUN",
        "ATHENAEUM_SPEND_MAX_USD_PER_DAY",
        "ATHENAEUM_SPEND_LEDGER_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    return path


def _sub_usage(model: str = "claude-sonnet-4-6") -> TokenUsage:
    u = TokenUsage()
    u.subscription_covered = True
    u.add(1000, 200, 50, 300, model=model)
    return u


def _api_usage(model: str = "claude-opus-4") -> TokenUsage:
    u = TokenUsage()
    u.add(100_000, 100_000, 0, 0, model=model)
    return u


# ---------------------------------------------------------------------------
# build_record / record_spend — provider tagging + never-blend invariants
# ---------------------------------------------------------------------------


class TestRecordSpend:
    def test_subscription_record_carries_zero_usd(self, ledger: Path) -> None:
        assert spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        rec = spend.read_ledger(ledger)[0]
        assert rec["provider"] == "claude-cli"
        assert rec["subscription_covered"] is True
        assert rec["estimated_cost_usd"] == 0.0
        # four counters kept separate
        assert rec["input_tokens"] == 1000
        assert rec["output_tokens"] == 200
        assert rec["cache_creation_input_tokens"] == 50
        assert rec["cache_read_input_tokens"] == 300
        assert rec["models"] == ["claude-sonnet-4-6"]

    def test_api_record_carries_real_usd(self, ledger: Path) -> None:
        assert spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        rec = spend.read_ledger(ledger)[0]
        assert rec["provider"] == "anthropic"
        assert rec["subscription_covered"] is False
        assert rec["estimated_cost_usd"] > 0.0

    def test_api_usd_tagged_zero_when_provider_is_cli_even_if_flag_unset(
        self, ledger: Path
    ) -> None:
        # A run whose accumulator did NOT set subscription_covered but whose
        # provider is claude-cli must STILL record $0 — the ledger tags by
        # provider, not by the accumulator flag.
        u = TokenUsage()  # subscription_covered defaults False
        u.add(1000, 500, 0, 0, model="claude-sonnet-4-6")
        assert spend.record_spend(u, run_type="librarian", provider="claude-cli")
        rec = spend.read_ledger(ledger)[0]
        assert rec["estimated_cost_usd"] == 0.0

    def test_empty_usage_writes_nothing(self, ledger: Path) -> None:
        assert spend.record_spend(TokenUsage(), run_type="librarian", provider="api") is False
        assert spend.read_ledger(ledger) == []

    def test_disabled_writes_nothing(self, ledger: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER_ENABLED", "false")
        wrote = spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        assert wrote is False
        assert spend.read_ledger(ledger) == []

    def test_write_never_raises_on_bad_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A ledger path that cannot be created must be swallowed, never raised.
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", "/proc/nonexistent/cannot/spend.jsonl")
        wrote = spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        assert wrote is False

    def test_write_failure_logs_loudly_at_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Issue #568 (H1): a failed ledger write used to be invisible at
        # log.debug, blinding the cumulative drain ceiling (and reporting $0 to
        # the #487 cross-repo accounting contract). It must now be LOUD.
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", "/proc/nonexistent/cannot/spend.jsonl")
        with caplog.at_level(logging.WARNING, logger="athenaeum"):
            wrote = spend.record_spend(
                _sub_usage(), run_type="librarian", provider="claude-cli"
            )
        assert wrote is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("spend ledger write FAILED" in r.getMessage() for r in warnings)

    def test_appends_multiple_records(self, ledger: Path) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        assert len(spend.read_ledger(ledger)) == 2


# ---------------------------------------------------------------------------
# read_ledger — crash-safety + filtering
# ---------------------------------------------------------------------------


class TestReadLedger:
    def test_tolerates_torn_trailing_line(self, ledger: Path) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write('{"partial": tru')  # crash mid-write
        recs = spend.read_ledger(ledger)
        assert len(recs) == 1

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert spend.read_ledger(tmp_path / "nope.jsonl") == []

    def test_since_filter(self, ledger: Path) -> None:
        old = {
            "ts": "2020-01-01T00:00:00Z",
            "provider": "anthropic",
            "total_tokens": 5,
            "estimated_cost_usd": 1.0,
        }
        new = {
            "ts": "2999-01-01T00:00:00Z",
            "provider": "anthropic",
            "total_tokens": 7,
            "estimated_cost_usd": 2.0,
        }
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
        since = spend.parse_since("1d", now=datetime(2999, 1, 2, tzinfo=timezone.utc))
        recs = spend.read_ledger(ledger, since=since)
        assert len(recs) == 1
        assert recs[0]["total_tokens"] == 7


# ---------------------------------------------------------------------------
# parse_since
# ---------------------------------------------------------------------------


class TestParseSince:
    def test_windows(self) -> None:
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        assert spend.parse_since("7d", now=now) == datetime(
            2026, 7, 8, 12, 0, tzinfo=timezone.utc
        )
        assert spend.parse_since("24h", now=now) == datetime(
            2026, 7, 14, 12, 0, tzinfo=timezone.utc
        )
        assert spend.parse_since("30m", now=now) == datetime(
            2026, 7, 15, 11, 30, tzinfo=timezone.utc
        )
        assert spend.parse_since("2w", now=now) == datetime(
            2026, 7, 1, 12, 0, tzinfo=timezone.utc
        )

    def test_iso_date(self) -> None:
        assert spend.parse_since("2026-07-01") == datetime(2026, 7, 1, tzinfo=timezone.utc)

    def test_iso_datetime(self) -> None:
        assert spend.parse_since("2026-07-01T09:30:00Z") == datetime(
            2026, 7, 1, 9, 30, tzinfo=timezone.utc
        )

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            spend.parse_since("banana")


# ---------------------------------------------------------------------------
# summarize / format_summary — never blend the paths
# ---------------------------------------------------------------------------


class TestSummarize:
    def test_paths_separated(self, ledger: Path) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        s = spend.summarize(spend.read_ledger(ledger))
        assert s["subscription"]["total_tokens"] == 1200
        assert s["subscription"]["estimated_cost_usd"] == 0.0  # never dollars
        assert s["api"]["estimated_cost_usd"] > 0.0
        assert s["api"]["total_tokens"] == 200_000

    def test_by_model_and_by_provider(self, ledger: Path) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        s = spend.summarize(spend.read_ledger(ledger), by_model=True, by_provider=True)
        assert "claude-sonnet-4-6" in s["by_model"]
        assert "claude-opus-4" in s["by_model"]
        assert "librarian" in s["by_run_type"]
        assert "query-topics" in s["by_run_type"]

    def test_format_summary_has_both_rows(self, ledger: Path) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        out = spend.format_summary(
            spend.summarize(spend.read_ledger(ledger)), since_label="7d"
        )
        assert "Subscription" in out
        assert "tokens" in out
        assert "API" in out
        assert "$" in out

    def test_format_summary_breakdowns(self, ledger: Path) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        summary = spend.summarize(
            spend.read_ledger(ledger), by_model=True, by_provider=True
        )
        out = spend.format_summary(
            summary, since_label="7d", by_model=True, by_provider=True
        )
        assert "By run type:" in out
        assert "By model:" in out
        assert "claude-opus-4" in out
        assert "query-topics" in out


# ---------------------------------------------------------------------------
# ceiling_tripped + spend_today
# ---------------------------------------------------------------------------


class TestCeiling:
    def test_no_ceiling_configured_returns_none(self, ledger: Path) -> None:
        assert spend.ceiling_tripped(_api_usage(), provider="api") is None
        assert spend.ceiling_tripped(_sub_usage(), provider="claude-cli") is None

    def test_subscription_per_run_token_ceiling(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN", "1000")
        big = TokenUsage()
        big.add(900, 200, 0, 0)  # 1100 >= 1000
        assert spend.ceiling_tripped(big, provider="claude-cli") is not None
        small = TokenUsage()
        small.add(100, 50, 0, 0)
        assert spend.ceiling_tripped(small, provider="claude-cli") is None

    def test_api_per_run_dollar_ceiling(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "0.001")
        assert spend.ceiling_tripped(_api_usage(), provider="api") is not None

    def test_api_per_day_dollar_ceiling_counts_prior_ledger(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps(
                {
                    "ts": "2026-07-15T01:00:00Z",
                    "provider": "anthropic",
                    "total_tokens": 100,
                    "estimated_cost_usd": 4.0,
                }
            )
            + "\n"
        )
        assert spend.spend_today(ledger, now=now)["api_usd"] == 4.0
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "4.5")
        # prior $4.00 + this run's ~$3.00 (_api_usage on opus) >= $4.50 -> trip.
        assert spend.ceiling_tripped(_api_usage(), provider="api", now=now) is not None

    def test_subscription_ceiling_ignores_api_path(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A subscription TOKEN ceiling must not gate an API-path run.
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN", "1")
        assert spend.ceiling_tripped(_api_usage(), provider="api") is None

    def test_per_day_ceiling_counts_prior_ledger(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        # Prior subscription spend today: 1200 tokens (from _sub_usage).
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps(
                {
                    "ts": "2026-07-15T01:00:00Z",
                    "provider": "claude-cli",
                    "total_tokens": 1200,
                    "estimated_cost_usd": 0.0,
                }
            )
            + "\n"
        )
        today = spend.spend_today(ledger, now=now)
        assert today["subscription_tokens"] == 1200.0
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY", "1500")
        # Current run adds 500 -> 1700 >= 1500 -> tripped.
        cur = TokenUsage()
        cur.add(400, 100, 0, 0)
        assert spend.ceiling_tripped(cur, provider="claude-cli", now=now) is not None
        # A small run staying under the day cap does not trip.
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY", "5000")
        assert spend.ceiling_tripped(cur, provider="claude-cli", now=now) is None


# ---------------------------------------------------------------------------
# Config resolvers
# ---------------------------------------------------------------------------


class TestConfigResolvers:
    def test_ledger_enabled_default_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_LEDGER_ENABLED", raising=False)
        assert resolve_spend_ledger_enabled(None) is True
        assert resolve_spend_ledger_enabled({"spend": {"ledger_enabled": False}}) is False

    def test_ledger_enabled_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER_ENABLED", "0")
        assert resolve_spend_ledger_enabled({"spend": {"ledger_enabled": True}}) is False

    def test_ledger_path_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_LEDGER", raising=False)
        assert resolve_spend_ledger_path(None) is None
        got = resolve_spend_ledger_path({"spend": {"ledger_path": "/x/y.jsonl"}})
        assert got == Path("/x/y.jsonl")

    def test_ceilings_default_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN",
            "ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY",
            "ATHENAEUM_SPEND_MAX_USD_PER_RUN",
            "ATHENAEUM_SPEND_MAX_USD_PER_DAY",
        ):
            monkeypatch.delenv(var, raising=False)
        assert resolve_spend_max_tokens_per_run(None) is None
        assert resolve_spend_max_tokens_per_day(None) is None
        assert resolve_spend_max_usd_per_run(None) is None
        assert resolve_spend_max_usd_per_day(None) is None

    def test_ceiling_yaml_and_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN", raising=False)
        assert resolve_spend_max_tokens_per_run({"spend": {"max_tokens_per_run": 5000}}) == 5000
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN", "9000")
        assert resolve_spend_max_tokens_per_run({"spend": {"max_tokens_per_run": 5000}}) == 9000

    def test_ceiling_rejects_bool_and_nonpositive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", raising=False)
        # bool must not coerce to 1; zero/negative fall through to None.
        assert resolve_spend_max_usd_per_day({"spend": {"max_usd_per_day": True}}) is None
        assert resolve_spend_max_usd_per_day({"spend": {"max_usd_per_day": 0}}) is None
        assert resolve_spend_max_usd_per_day({"spend": {"max_usd_per_day": -5}}) is None
        assert resolve_spend_max_usd_per_day({"spend": {"max_usd_per_day": 2.5}}) == 2.5


# ---------------------------------------------------------------------------
# `athenaeum spend` CLI command
# ---------------------------------------------------------------------------


class TestSpendCommand:
    def test_json_output_shape(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        rc = main(["spend", "--since", "30d", "--json", "--ledger", str(ledger)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["subscription"]["total_tokens"] == 1200
        assert payload["subscription"]["estimated_cost_usd"] == 0.0
        assert payload["api"]["estimated_cost_usd"] > 0.0
        assert "since" in payload
        assert payload["ledger_path"] == str(ledger)

    def test_human_output(self, ledger: Path, capsys: pytest.CaptureFixture[str]) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        rc = main(["spend", "--since", "30d", "--ledger", str(ledger)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Subscription" in out
        assert "API" in out

    def test_invalid_since_returns_2(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["spend", "--since", "banana", "--ledger", str(ledger)])
        assert rc == 2
        assert "Invalid --since" in capsys.readouterr().err

    def test_empty_ledger_ok(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["spend", "--ledger", str(tmp_path / "none.jsonl"), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["record_count"] == 0


# ---------------------------------------------------------------------------
# Schema v2 (issue #487) — per-model attribution, billing_mode, notional_usd,
# and the unpriceable pre-v2 contract. cwc#1629 accounting conformance.
# ---------------------------------------------------------------------------


def _mixed_model_api_usage() -> TokenUsage:
    """A metered run spanning two models, as a librarian pass does — Haiku for
    the tier-2 classify and Sonnet for the tier-3 write — each tagged so the
    accumulator carries per-model attribution (#247). No batch traffic, so the
    row reprices cleanly from input/output/cache alone."""
    u = TokenUsage()
    u.add(300_000, 40_000, 100_000, 5_000, model="claude-haiku-4-5-20251001")
    u.add(80_000, 120_000, 0, 0, model="claude-sonnet-4-6")
    return u


class TestSchemaV2:
    def test_two_model_run_is_repriceable_per_model(self, ledger: Path) -> None:
        """A mixed Haiku/Sonnet run writes per-model token counts, and the row
        can be repriced per model — the defect a flat aggregate row cannot fix
        (issue #487 acceptance #1). Exercises the real write path end to end:
        record_spend -> build_record -> _append_line -> read_ledger off disk."""
        assert spend.record_spend(_mixed_model_api_usage(), run_type="librarian", provider="api")
        rec = spend.read_ledger(ledger)[0]

        assert rec["v"] == 2
        tbm = rec["tokens_by_model"]
        assert set(tbm) == {"claude-haiku-4-5-20251001", "claude-sonnet-4-6"}
        # Hestia cost-ledger.ts core shape: {input, output, total}.
        assert tbm["claude-haiku-4-5-20251001"]["input"] == 300_000
        assert tbm["claude-haiku-4-5-20251001"]["output"] == 40_000
        assert tbm["claude-haiku-4-5-20251001"]["total"] == 340_000
        assert tbm["claude-sonnet-4-6"]["input"] == 80_000
        assert tbm["claude-sonnet-4-6"]["total"] == 200_000
        # The two models carry genuinely different attribution — a blended
        # single total could not recover this.
        assert tbm["claude-haiku-4-5-20251001"]["input"] != tbm["claude-sonnet-4-6"]["input"]

        # Repriceable per model: reconstruct each model's spend from ITS row
        # entry and price it at ITS own rate; the sum reproduces the row's
        # notional (no untagged remainder, no batch). A flat row cannot do this.
        def _reprice(model: str, entry: dict[str, Any]) -> float:
            u = TokenUsage()
            u.add(
                entry["input"],
                entry["output"],
                entry["cache_creation_input_tokens"],
                entry["cache_read_input_tokens"],
                model=model,
            )
            return u.estimated_cost_usd

        per_model_sum = sum(_reprice(m, e) for m, e in tbm.items())
        assert rec["notional_usd"] > 0.0
        assert round(per_model_sum, 6) == rec["notional_usd"]

    def test_tokens_by_model_preserves_cache_and_batch_splits(self, ledger: Path) -> None:
        """The per-model entry is a SUPERSET of hestia's core shape — it keeps
        athenaeum's cache/batch splits (#487 scope; #239/#236 cost relevance)."""
        u = TokenUsage()
        u.add(1_000, 500, 200, 50, model="claude-sonnet-4-6")
        u.add_batch_tokens(400, 100, 0, 0, model="claude-sonnet-4-6")
        assert spend.record_spend(u, run_type="librarian", provider="api")
        entry = spend.read_ledger(ledger)[0]["tokens_by_model"]["claude-sonnet-4-6"]
        # input/output include the batch share (folded into the scalar counters).
        assert entry["input"] == 1_400
        assert entry["output"] == 600
        assert entry["cache_creation_input_tokens"] == 200
        assert entry["cache_read_input_tokens"] == 50
        assert entry["batch_input_tokens"] == 400
        assert entry["batch_output_tokens"] == 100

    def test_billing_mode_and_real_vs_notional_never_summed(self, ledger: Path) -> None:
        """Every row carries billing_mode; real API dollars and subscription
        notional are two separate metrics (#487 acceptance #2)."""
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        sub_rec, api_rec = spend.read_ledger(ledger)

        assert sub_rec["billing_mode"] == "subscription"
        assert sub_rec["estimated_cost_usd"] == 0.0  # nothing paid...
        assert sub_rec["notional_usd"] > 0.0  # ...but utilization is visible

        assert api_rec["billing_mode"] == "api"
        # On an api row the paid figure and the counterfactual coincide.
        assert api_rec["estimated_cost_usd"] > 0.0
        assert api_rec["estimated_cost_usd"] == api_rec["notional_usd"]

        # The invariant: a real-dollar total sums only api rows' estimated_cost,
        # never a subscription row's notional. They are never added together.
        real_dollars = sum(
            r["estimated_cost_usd"] for r in (sub_rec, api_rec) if r["billing_mode"] == "api"
        )
        assert real_dollars == api_rec["estimated_cost_usd"]

    def test_pre_v2_rows_readable_and_counted_unpriceable(self, ledger: Path) -> None:
        """A pre-v2 row (no per-model attribution) stays readable and is counted
        as unpriceable — never silently dropped or repriced (#487 acceptance
        #3, cwc#1627's failure mode)."""
        # A genuine v1 row, exactly as an older athenaeum wrote it (no
        # tokens_by_model / billing_mode / notional_usd).
        v1 = {
            "v": 1,
            "ts": "2026-07-20T00:00:00Z",
            "run_type": "librarian",
            "provider": "anthropic",
            "subscription_covered": False,
            "models": ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"],
            "api_calls": 500,
            "input_tokens": 2_000_000,
            "output_tokens": 500_000,
            "total_tokens": 2_500_000,
            "estimated_cost_usd": 12.5,
        }
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(v1, separators=(",", ":")) + "\n", encoding="utf-8")
        # A conforming v2 row appended after it.
        spend.record_spend(_mixed_model_api_usage(), run_type="librarian", provider="api")

        records = spend.read_ledger(ledger)
        assert len(records) == 2  # the v1 row is NOT dropped
        summary = spend.summarize(records)
        assert summary["record_count"] == 2
        assert summary["unpriceable_records"] == 1  # only the v1 row
        # The v1 row is still present in its billing bucket (its own historical
        # figure is retained; only its per-model repriceability is absent).
        assert summary["api"]["records"] == 2

    def test_spend_json_shape_is_additive(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`athenaeum spend --json` keeps its existing top-level shape (cwc#1218
        /good-morning must not regress) and only ADDS unpriceable_records
        (#487 acceptance #4)."""
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        rc = main(["spend", "--since", "30d", "--json", "--ledger", str(ledger)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        # Existing keys /good-morning consumes — unchanged.
        assert payload["subscription"]["total_tokens"] == 1200
        assert payload["subscription"]["estimated_cost_usd"] == 0.0
        assert payload["api"]["estimated_cost_usd"] > 0.0
        assert payload["record_count"] == 2
        assert "ledger_path" in payload  # surfaced for the cwc#1627 reader
        # Additive v2 field.
        assert payload["unpriceable_records"] == 0  # both rows are v2


# ---------------------------------------------------------------------------
# query_topics ledger integration — the metered hot path is recorded
# ---------------------------------------------------------------------------


class TestQueryTopicsLedger:
    def test_records_metered_spend(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from athenaeum import query_topics

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        class _FakeMessages:
            def create(self, **kwargs: Any) -> Any:
                return SimpleNamespace(
                    content=[SimpleNamespace(text='["Return Path"]')],
                    usage=SimpleNamespace(
                        input_tokens=120,
                        output_tokens=15,
                        cache_creation_input_tokens=0,
                        cache_read_input_tokens=0,
                    ),
                )

        class _FakeClient:
            def __init__(self, **kwargs: Any) -> None:
                self.messages = _FakeMessages()

        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)

        topics = query_topics.extract_topics("Tell me about Return Path please")
        assert topics == ["Return Path"]

        recs = spend.read_ledger(ledger)
        assert len(recs) == 1
        assert recs[0]["run_type"] == "query-topics"
        assert recs[0]["provider"] == "anthropic"  # metered API path
        assert recs[0]["input_tokens"] == 120
        assert recs[0]["estimated_cost_usd"] > 0.0
        # v2 conformance on a genuinely LLM-driven write (issue #487): the row
        # carries billing_mode, per-model attribution, and the notional figure.
        assert recs[0]["v"] == 2
        assert recs[0]["billing_mode"] == "api"
        assert recs[0]["notional_usd"] == recs[0]["estimated_cost_usd"]
        tbm = recs[0]["tokens_by_model"]
        assert list(tbm) and tbm[next(iter(tbm))]["input"] == 120
