"""Tests for the durable LLM-spend ledger (issue athenaeum#378).

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
    resolve_spend_max_pct_per_day,
    resolve_spend_max_tokens_per_day,
    resolve_spend_max_tokens_per_run,
    resolve_spend_max_usd_per_day,
    resolve_spend_max_usd_per_run,
    resolve_spend_weekly_token_limit,
)
from athenaeum.models import TokenUsage
from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage

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
        "ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT",
        "ATHENAEUM_SPEND_MAX_PCT_PER_DAY",
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
        # Issue athenaeum#568 (H1): a failed ledger write used to be invisible at
        # log.debug, blinding the cumulative drain ceiling (and reporting $0 to
        # the athenaeum#487 cross-repo accounting contract). It must now be LOUD.
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
# Unknown is a distinct state from zero (issue athenaeum#694, AC4)
# ---------------------------------------------------------------------------


def _row(**overrides: Any) -> dict[str, Any]:
    """A minimal well-formed ledger row, overridable per test."""
    base: dict[str, Any] = {
        "v": spend.LEDGER_VERSION,
        "ts": "2026-08-02T00:00:00Z",
        "provider": "anthropic",
        "billing_mode": "api",
        "total_tokens": 100,
        "input_tokens": 100,
        "output_tokens": 0,
        "api_calls": 1,
        "estimated_cost_usd": 0.5,
        "tokens_by_model": {"m": {}},
    }
    base.update(overrides)
    return base


class TestUnknownBillingDistinctFromZero:
    def test_resolve_billing_bucket(self) -> None:
        # billing_mode is authoritative; provider is the pre-v2 fallback; an
        # undeterminable row is "unknown", never silently "api".
        assert spend.resolve_billing_bucket(_row(billing_mode="api")) == "api"
        assert (
            spend.resolve_billing_bucket(_row(billing_mode="subscription"))
            == "subscription"
        )
        # pre-v2 row: no billing_mode, derived from provider.
        pre = _row()
        del pre["billing_mode"]
        assert spend.resolve_billing_bucket({**pre, "provider": "anthropic"}) == "api"
        assert (
            spend.resolve_billing_bucket({**pre, "provider": "claude-cli"})
            == "subscription"
        )
        # undeterminable: no billing_mode AND unrecognized/absent provider.
        assert spend.resolve_billing_bucket({**pre, "provider": "mystery"}) == "unknown"
        del pre["provider"]
        assert spend.resolve_billing_bucket(pre) == "unknown"

    def test_unknown_row_is_not_folded_into_api(self) -> None:
        # AC4: an undeterminable row's tokens land in `unknown`, never silently
        # in `api` (which would misattribute them) and never dropped.
        rows = [
            _row(provider="anthropic", billing_mode="api", total_tokens=100),
            {**_row(total_tokens=300, estimated_cost_usd=9.9), "provider": "mystery"},
        ]
        for r in rows:
            if r["provider"] == "mystery":
                r.pop("billing_mode", None)
        s = spend.summarize(rows)
        assert s["api"]["total_tokens"] == 100  # api did NOT absorb the unknown row
        assert s["unknown"]["total_tokens"] == 300
        assert s["unknown"]["records"] == 1
        assert s["record_count"] == 2  # the unknown row is counted, not dropped

    def test_unknown_bucket_always_present_even_when_empty(self) -> None:
        # AC4: "unknown" is a distinct STATE — present-and-zero, never absent, so
        # a consumer distinguishes "no undeterminable rows" from "field missing".
        s = spend.summarize([])
        assert "unknown" in s
        assert s["unknown"]["records"] == 0
        assert s["unknown"]["total_tokens"] == 0

    def test_format_summary_surfaces_unknown_only_when_present(self) -> None:
        clean = spend.format_summary(spend.summarize([_row()]), since_label="7d")
        assert "Unknown" not in clean  # no undeterminable rows → no noise line
        row = _row(provider="mystery")
        del row["billing_mode"]
        dirty = spend.format_summary(spend.summarize([row]), since_label="7d")
        assert "Unknown" in dirty
        assert "undeterminable" in dirty


# ---------------------------------------------------------------------------
# spend --json is a stable consumer contract (issue athenaeum#694)
# ---------------------------------------------------------------------------


class TestSpendJsonContract:
    def test_json_shape_documents_the_three_paths(self, ledger: Path) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        out = _run_spend_json(ledger)
        payload = json.loads(out)
        # The contract's stable top-level keys.
        for key in (
            "since",
            "ledger_path",
            "record_count",
            "unpriceable_records",
            "subscription",
            "api",
            "unknown",
        ):
            assert key in payload, key
        # AC3: api carries dollars, subscription carries tokens at $0 — the two
        # are separate objects and are NEVER summed into a blended total.
        assert payload["api"]["estimated_cost_usd"] > 0.0
        assert payload["subscription"]["estimated_cost_usd"] == 0.0
        assert payload["subscription"]["total_tokens"] == 1200

    def test_subscription_tokens_never_rendered_as_dollars(self, ledger: Path) -> None:
        # AC5: nowhere in `athenaeum spend` output is a subscription token count
        # rendered as a dollar figure. The subscription bucket's only dollar
        # field is a hard 0.0, in both JSON and the human report.
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        payload = json.loads(_run_spend_json(ledger))
        assert payload["subscription"]["estimated_cost_usd"] == 0.0
        human = spend.format_summary(
            spend.summarize(spend.read_ledger(ledger)), since_label="7d"
        )
        # The subscription line reports tokens, not a "$" figure.
        sub_line = next(ln for ln in human.splitlines() if "Subscription" in ln)
        assert "$" not in sub_line
        assert "tokens" in sub_line


def _run_spend_json(ledger: Path) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["spend", "--json", "--cache-dir", str(ledger.parent)])
    assert rc == 0
    return buf.getvalue()


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
# Weekly subscription token limit + max-percent-per-day (issue athenaeum#785)
# ---------------------------------------------------------------------------


class TestWeeklyPctCeiling:
    def test_both_set_trips_and_names_derived_figure_and_weekly_limit(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # weekly 700,000 / 7 * 50% -> effective daily cap 50,000 tokens.
        monkeypatch.setenv("ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT", "700000")
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_PCT_PER_DAY", "50")
        big = TokenUsage()
        big.add(40_000, 20_000, 0, 0)  # 60,000 >= 50,000
        reason = spend.ceiling_tripped(big, provider="claude-cli")
        assert reason is not None
        assert "50,000" in reason  # the derived daily ceiling
        assert "700,000" in reason  # the declared weekly limit it came from

    def test_both_set_under_ceiling_does_not_trip(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT", "700000")
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_PCT_PER_DAY", "50")
        small = TokenUsage()
        small.add(100, 50, 0, 0)
        assert spend.ceiling_tripped(small, provider="claude-cli") is None

    def test_only_weekly_limit_set_leaves_behavior_unchanged(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Weekly limit alone has no denominator to percent-of; no ceiling.
        monkeypatch.setenv("ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT", "1")
        huge = TokenUsage()
        huge.add(1_000_000, 0, 0, 0)
        assert spend.ceiling_tripped(huge, provider="claude-cli") is None

    def test_only_max_pct_set_leaves_behavior_unchanged(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A percentage with no weekly figure to take it of is a no-op.
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_PCT_PER_DAY", "1")
        huge = TokenUsage()
        huge.add(1_000_000, 0, 0, 0)
        assert spend.ceiling_tripped(huge, provider="claude-cli") is None

    def test_api_path_unaffected_by_pct_ceiling(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The percentage ceiling is token-denominated and subscription-only —
        # it must not gate an API-path (dollar) run (athenaeum#487, cwc#1629: the
        # ledger never blends subscription notional and api real dollars).
        monkeypatch.setenv("ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT", "1")
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_PCT_PER_DAY", "1")
        assert spend.ceiling_tripped(_api_usage(), provider="api") is None

    def test_counts_prior_ledger_spend_same_utc_day(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps(
                {
                    "ts": "2026-07-15T01:00:00Z",
                    "provider": "claude-cli",
                    "total_tokens": 45_000,
                    "estimated_cost_usd": 0.0,
                }
            )
            + "\n"
        )
        # weekly 700,000 / 7 * 50% -> effective daily cap 50,000 tokens.
        monkeypatch.setenv("ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT", "700000")
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_PCT_PER_DAY", "50")
        cur = TokenUsage()
        cur.add(4_000, 2_000, 0, 0)  # prior 45,000 + 6,000 = 51,000 >= 50,000
        assert spend.ceiling_tripped(cur, provider="claude-cli", now=now) is not None


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

    def test_weekly_token_limit_default_none_and_env_over_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT", raising=False)
        assert resolve_spend_weekly_token_limit(None) is None
        assert (
            resolve_spend_weekly_token_limit({"spend": {"weekly_token_limit": 700000}})
            == 700000
        )
        monkeypatch.setenv("ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT", "900000")
        assert (
            resolve_spend_weekly_token_limit({"spend": {"weekly_token_limit": 700000}})
            == 900000
        )

    def test_max_pct_per_day_default_none_and_env_over_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_PCT_PER_DAY", raising=False)
        assert resolve_spend_max_pct_per_day(None) is None
        assert resolve_spend_max_pct_per_day({"spend": {"max_pct_per_day": 50}}) == 50
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_PCT_PER_DAY", "25")
        assert resolve_spend_max_pct_per_day({"spend": {"max_pct_per_day": 50}}) == 25

    def test_weekly_and_pct_reject_bool_and_nonpositive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT", raising=False)
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_PCT_PER_DAY", raising=False)
        assert resolve_spend_weekly_token_limit({"spend": {"weekly_token_limit": True}}) is None
        assert resolve_spend_weekly_token_limit({"spend": {"weekly_token_limit": 0}}) is None
        assert resolve_spend_weekly_token_limit({"spend": {"weekly_token_limit": -5}}) is None
        assert resolve_spend_max_pct_per_day({"spend": {"max_pct_per_day": True}}) is None
        assert resolve_spend_max_pct_per_day({"spend": {"max_pct_per_day": 0}}) is None
        assert resolve_spend_max_pct_per_day({"spend": {"max_pct_per_day": -5}}) is None


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
# Schema v2 (issue athenaeum#487) — per-model attribution, billing_mode, notional_usd,
# and the unpriceable pre-v2 contract. cwc#1629 accounting conformance.
# ---------------------------------------------------------------------------


def _mixed_model_api_usage() -> TokenUsage:
    """A metered run spanning two models, as a librarian pass does — Haiku for
    the tier-2 classify and Sonnet for the tier-3 write — each tagged so the
    accumulator carries per-model attribution (athenaeum#247). No batch traffic, so the
    row reprices cleanly from input/output/cache alone."""
    u = TokenUsage()
    u.add(300_000, 40_000, 100_000, 5_000, model="claude-haiku-4-5-20251001")
    u.add(80_000, 120_000, 0, 0, model="claude-sonnet-4-6")
    return u


class TestSchemaV2:
    def test_two_model_run_is_repriceable_per_model(self, ledger: Path) -> None:
        """A mixed Haiku/Sonnet run writes per-model token counts, and the row
        can be repriced per model — the defect a flat aggregate row cannot fix
        (issue athenaeum#487 acceptance #1). Exercises the real write path end to end:
        record_spend -> build_record -> _append_line -> read_ledger off disk."""
        assert spend.record_spend(_mixed_model_api_usage(), run_type="librarian", provider="api")
        rec = spend.read_ledger(ledger)[0]

        # v3 (athenaeum#781) bumped the schema version; this test only cares that
        # tokens_by_model -- unchanged by that bump -- is still repriceable.
        assert rec["v"] == spend.LEDGER_VERSION
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
        athenaeum's cache/batch splits (athenaeum#487 scope; athenaeum#239/#236 cost relevance)."""
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
        notional are two separate metrics (athenaeum#487 acceptance #2)."""
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
        as unpriceable — never silently dropped or repriced (athenaeum#487 acceptance
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
        (athenaeum#487 acceptance #4)."""
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
# Schema v3 (issue athenaeum#781) — per-knob attribution, tokens_by_knob, and the
# knob-unattributed pre-v3 contract. Mirrors TestSchemaV2 one field down.
# ---------------------------------------------------------------------------


def _mixed_knob_api_usage() -> TokenUsage:
    """A metered run spanning two knobs -- Haiku for tier-2 classify and
    Sonnet for tier-3 write -- each tagged with BOTH model and knob so the
    accumulator carries per-knob attribution (athenaeum#781) as a SIBLING of the
    existing per-model attribution."""
    u = TokenUsage()
    u.add(
        300_000, 40_000, 100_000, 5_000,
        model="claude-haiku-4-5-20251001", knob="classify",
    )
    u.add(80_000, 120_000, 0, 0, model="claude-sonnet-4-6", knob="write")
    return u


class TestSchemaV3:
    def test_ledger_version_is_3(self, ledger: Path) -> None:
        assert spend.LEDGER_VERSION == 3
        assert spend.record_spend(_mixed_knob_api_usage(), run_type="librarian", provider="api")
        rec = spend.read_ledger(ledger)[0]
        assert rec["v"] == 3

    def test_tokens_by_knob_is_a_sibling_of_tokens_by_model(self, ledger: Path) -> None:
        """AC: tokens_by_knob carries the right knob per source, and
        tokens_by_model keeps its EXISTING shape byte-for-byte -- adding
        tokens_by_knob must never reshape it (athenaeum#781 notes-for-implementer)."""
        assert spend.record_spend(_mixed_knob_api_usage(), run_type="librarian", provider="api")
        rec = spend.read_ledger(ledger)[0]

        tbk = rec["tokens_by_knob"]
        assert set(tbk) == {"classify", "write"}
        assert tbk["classify"]["input"] == 300_000
        assert tbk["classify"]["output"] == 40_000
        assert tbk["classify"]["total"] == 340_000
        assert tbk["write"]["input"] == 80_000
        assert tbk["write"]["total"] == 200_000

        # tokens_by_model is untouched by the knob addition -- same shape,
        # same values as schema v2 produced (hestia's cost-ledger.ts reader
        # depends on this staying a superset of {input, output, total}).
        tbm = rec["tokens_by_model"]
        assert set(tbm) == {"claude-haiku-4-5-20251001", "claude-sonnet-4-6"}
        assert tbm["claude-haiku-4-5-20251001"]["input"] == 300_000
        assert tbm["claude-haiku-4-5-20251001"]["total"] == 340_000
        assert tbm["claude-sonnet-4-6"]["input"] == 80_000

    def test_tokens_by_knob_preserves_cache_and_batch_splits(self, ledger: Path) -> None:
        u = TokenUsage()
        u.add(1_000, 500, 200, 50, model="claude-sonnet-4-6", knob="resolve")
        u.add_batch_tokens(400, 100, 0, 0, model="claude-sonnet-4-6", knob="resolve")
        assert spend.record_spend(u, run_type="librarian", provider="api")
        entry = spend.read_ledger(ledger)[0]["tokens_by_knob"]["resolve"]
        assert entry["input"] == 1_400
        assert entry["output"] == 600
        assert entry["cache_creation_input_tokens"] == 200
        assert entry["cache_read_input_tokens"] == 50
        assert entry["batch_input_tokens"] == 400
        assert entry["batch_output_tokens"] == 100

    def test_pre_v3_rows_readable_and_counted_knob_unattributed(self, ledger: Path) -> None:
        """A pre-v3 row (no per-knob attribution) stays readable and is
        counted as knob-unattributed -- never silently dropped -- exactly as
        a pre-v2 row is counted unpriceable (athenaeum#781 AC)."""
        # A genuine v2 row, as athenaeum#487 wrote it: tokens_by_model present,
        # tokens_by_knob absent entirely.
        v2 = {
            "v": 2,
            "ts": "2026-08-01T00:00:00Z",
            "run_type": "librarian",
            "provider": "anthropic",
            "billing_mode": "api",
            "subscription_covered": False,
            "models": ["claude-sonnet-4-6"],
            "tokens_by_model": {"claude-sonnet-4-6": {"input": 1000, "output": 200, "total": 1200}},
            "api_calls": 10,
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "estimated_cost_usd": 0.15,
            "notional_usd": 0.15,
        }
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(v2, separators=(",", ":")) + "\n", encoding="utf-8")
        # A conforming v3 row appended after it.
        spend.record_spend(_mixed_knob_api_usage(), run_type="librarian", provider="api")

        records = spend.read_ledger(ledger)
        assert len(records) == 2  # the v2 row is NOT dropped
        summary = spend.summarize(records)
        assert summary["record_count"] == 2
        assert summary["knob_unattributed_records"] == 1  # only the v2 row
        # unpriceable_records is unaffected -- both rows carry per-model
        # attribution, this is purely the per-knob dimension.
        assert summary["unpriceable_records"] == 0
        # The v2 row is still present in its billing bucket.
        assert summary["api"]["records"] == 2

    def test_by_knob_summarize_and_format(self, ledger: Path) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_mixed_knob_api_usage(), run_type="librarian", provider="api")
        summary = spend.summarize(spend.read_ledger(ledger), by_knob=True)
        assert "classify" in summary["by_knob"]
        assert "write" in summary["by_knob"]
        out = spend.format_summary(summary, since_label="7d", by_knob=True)
        assert "By knob:" in out
        assert "classify" in out
        assert "write" in out

    def test_by_knob_keeps_subscription_api_split_never_blended(self, ledger: Path) -> None:
        """AC: --by-knob reports tokens AND dollars per knob, but the two
        cost paths inside each knob bucket are never summed together."""
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_mixed_knob_api_usage(), run_type="librarian", provider="api")
        summary = spend.summarize(spend.read_ledger(ledger), by_knob=True)
        write_slot = summary["by_knob"]["write"]
        assert "subscription" in write_slot
        assert "api" in write_slot
        assert write_slot["subscription"]["estimated_cost_usd"] == 0.0
        assert write_slot["api"]["estimated_cost_usd"] > 0.0

    def test_cli_by_knob_flag(self, ledger: Path, capsys: pytest.CaptureFixture[str]) -> None:
        spend.record_spend(_mixed_knob_api_usage(), run_type="librarian", provider="api")
        rc = main(["spend", "--since", "30d", "--by-knob", "--json", "--ledger", str(ledger)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert "classify" in payload["by_knob"]
        assert "write" in payload["by_knob"]

        rc = main(["spend", "--since", "30d", "--by-knob", "--ledger", str(ledger)])
        assert rc == 0
        human = capsys.readouterr().out
        assert "By knob:" in human

    def test_summarize_existing_shape_unchanged(self, ledger: Path) -> None:
        """AC: summarize()'s existing shape (subscription/api/unknown/
        record_count) is unchanged so cicero/good-morning does not regress --
        by_knob only ADDS keys."""
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        summary = spend.summarize(spend.read_ledger(ledger))
        for key in ("subscription", "api", "unknown", "record_count"):
            assert key in summary
        # by_knob is opt-in -- absent when not requested.
        assert "by_knob" not in summary


# ---------------------------------------------------------------------------
# query_topics ledger integration — the metered hot path is recorded
# ---------------------------------------------------------------------------


class TestQueryTopicsLedger:
    def test_records_metered_spend(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import query_topics

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # Issue athenaeum#554 (L11): repointed at the shared FakeLLMClient double.
        fake = FakeLLMClient(
            response=make_llm_response(
                '["Return Path"]',
                usage=make_llm_usage(input_tokens=120, output_tokens=15),
            )
        )

        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", fake)

        topics = query_topics.extract_topics("Tell me about Return Path please")
        assert topics == ["Return Path"]

        recs = spend.read_ledger(ledger)
        assert len(recs) == 1
        assert recs[0]["run_type"] == "query-topics"
        assert recs[0]["provider"] == "anthropic"  # metered API path
        assert recs[0]["input_tokens"] == 120
        assert recs[0]["estimated_cost_usd"] > 0.0
        # v2 conformance on a genuinely LLM-driven write (issue athenaeum#487): the row
        # carries billing_mode, per-model attribution, and the notional figure.
        assert recs[0]["v"] == 3
        assert recs[0]["billing_mode"] == "api"
        assert recs[0]["notional_usd"] == recs[0]["estimated_cost_usd"]
        tbm = recs[0]["tokens_by_model"]
        assert list(tbm) and tbm[next(iter(tbm))]["input"] == 120
        # athenaeum#781: the real call site (query_topics.extract_topics) threads the
        # ``topic`` knob end to end into the ledger row -- one of the six
        # knobs attributable per the acceptance criterion.
        assert recs[0]["tokens_by_knob"]["topic"]["input"] == 120


# ---------------------------------------------------------------------------
# Suite-wide ledger isolation (issue athenaeum#776)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _ledger_path_seen_at_session_scope() -> Path:
    """Resolve the ledger path from OUTSIDE any test function's fixture scope.

    This is the case the pre-existing function-scoped ``_isolate_cache_dir``
    (athenaeum#750) cannot cover: a session- or module-scoped fixture runs
    before/outside it, so before athenaeum#776 this resolved to the operator's
    real ledger. Session-scoped here on purpose — the scope IS the test.
    """
    return spend.resolve_ledger_path().resolve()


class TestSuiteLedgerIsolation:
    """The suite must never append to the operator's live spend ledger.

    Fixture model ids from ``tests/test_config_parity.py`` were found in 30
    rows of the operator's real ``~/.cache/athenaeum/spend.jsonl``, dated
    2026-07-15 through 2026-08-02. Synthetic rows inflate ``athenaeum spend``
    totals, are permanently unpriceable (no rate-table prefix matches them),
    and contaminate a live guardrail: ``spend_today()`` feeds
    ``ceiling_tripped()``, so a local suite run moves a ceiling that bounds
    REAL spend. These tests pin the redirect that stops it.
    """

    def _live_cache_dir(self) -> Path:
        return Path("~/.cache/athenaeum").expanduser().resolve()

    def test_resolve_ledger_path_is_not_the_live_ledger(self) -> None:
        """The headline invariant, asserted with no fixture setup at all.

        Deliberately requests neither ``ledger`` nor ``monkeypatch``: what is
        under test is the ambient state every other test in the suite runs
        under, so anything this test set up itself would be begging the
        question.
        """
        resolved = spend.resolve_ledger_path().resolve()
        live = self._live_cache_dir()
        assert resolved != live / spend.LEDGER_FILENAME
        assert live not in resolved.parents

    def test_default_cache_dir_is_not_the_live_cache_dir(self) -> None:
        """Same invariant one layer down, since the ledger path derives from it."""
        assert spend.default_cache_dir().resolve() != self._live_cache_dir()

    def test_ledger_write_lands_in_tmp_not_the_live_ledger(self) -> None:
        """An actual ``record_spend`` under ambient fixtures writes to tmp.

        Path resolution being right is necessary but not sufficient — this
        drives the real writer, which is what put rows in the live ledger.
        """
        usage = TokenUsage()
        usage.subscription_covered = True
        usage.add(10, 5, 0, 0, model="yaml-topic-model")

        assert spend.record_spend(usage, run_type="query-topics", provider="claude-cli") is True

        written_to = spend.resolve_ledger_path().resolve()
        assert self._live_cache_dir() not in written_to.parents
        assert written_to.exists()
        assert "yaml-topic-model" in written_to.read_text()

    def test_isolation_reaches_session_scoped_fixtures(
        self, _ledger_path_seen_at_session_scope: Path
    ) -> None:
        """The redirect covers code running outside a test function's scope.

        The function-scoped fixture alone leaves this hole open; only the
        session-scoped one closes it.
        """
        live = self._live_cache_dir()
        assert _ledger_path_seen_at_session_scope != live / spend.LEDGER_FILENAME
        assert live not in _ledger_path_seen_at_session_scope.parents

    def test_session_fixture_pins_both_env_vars(self) -> None:
        """Both knobs are set, not just the cache dir.

        ``ATHENAEUM_SPEND_LEDGER`` is set explicitly so a config carrying
        ``spend.ledger_path`` cannot route around the cache-dir redirect.
        """
        import os

        for var in ("ATHENAEUM_CACHE_DIR", "ATHENAEUM_SPEND_LEDGER"):
            value = os.environ.get(var)
            assert value, f"{var} must be set by the autouse isolation fixtures"
            assert self._live_cache_dir() != Path(value).expanduser().resolve()
