"""Tests for the durable LLM-spend ledger (issue #378).

Covers the ledger writer, reader, summariser, the `athenaeum spend` command,
the spend ceiling, and the config resolvers — pinning the invariants that
matter: the two cost paths (subscription tokens vs API dollars) are NEVER
blended, subscription rows carry $0, the four token counters stay separate,
the ledger tolerates a torn trailing line, and the ceiling halts on breach.
"""

from __future__ import annotations

import json
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
