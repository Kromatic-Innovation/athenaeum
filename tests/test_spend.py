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
    resolve_spend_warning_threshold_pct,
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
# run_type vocabulary (issue athenaeum#1136)
# ---------------------------------------------------------------------------


class TestIsLibrarianRunType:
    def test_bare_literal_is_a_family_member(self) -> None:
        assert spend.is_librarian_run_type(spend.RUN_TYPE_LIBRARIAN) is True

    def test_nightly_is_a_family_member(self) -> None:
        assert spend.is_librarian_run_type(spend.RUN_TYPE_LIBRARIAN_NIGHTLY) is True

    def test_unrelated_run_type_is_not_a_member(self) -> None:
        assert spend.is_librarian_run_type(spend.RUN_TYPE_ANSWERS) is False
        assert spend.is_librarian_run_type(spend.RUN_TYPE_QUERY_TOPICS) is False

    def test_a_value_merely_containing_librarian_is_not_a_member(self) -> None:
        # Must be an exact match or a "librarian-" PREFIX, not a substring
        # anywhere -- "not-librarian" must not false-positive.
        assert spend.is_librarian_run_type("not-librarian") is False

    def test_non_string_input_returns_false_not_raise(self) -> None:
        assert spend.is_librarian_run_type(None) is False
        assert spend.is_librarian_run_type(42) is False
        assert spend.is_librarian_run_type({"run_type": "librarian"}) is False


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

    def test_cache_only_subscription_usage_is_not_dropped(self, ledger: Path) -> None:
        """athenaeum#1137: the no-op guard is
        ``usage.api_calls == 0 and usage.total_tokens == 0`` -- a subscription
        usage that only ever went through ``add_tokens`` (the callee side of
        the athenaeum#239 attempt-counting split, where the CALLER bumps
        ``api_calls`` separately) genuinely has ``api_calls == 0`` on the
        object record_spend sees. With cache-only traffic (zero input, zero
        output), ``total_tokens`` was ALSO 0, so the AND-gate read "nothing
        happened" and silently dropped a run that actually spent 1.5M
        subscription tokens -- undercounting spend_today the same way the
        ceiling comparisons did. The guard must use billable_tokens."""
        u = TokenUsage()
        u.add_tokens(0, 0, 500_000, 1_000_000, model="claude-sonnet-4-6")
        assert u.api_calls == 0
        assert u.total_tokens == 0  # the old guard's condition was satisfied
        assert u.billable_tokens == 1_500_000  # ...but real spend happened
        assert spend.record_spend(u, run_type="librarian", provider="claude-cli") is True
        rec = spend.read_ledger(ledger)[0]
        assert rec["cache_creation_input_tokens"] == 500_000
        assert rec["cache_read_input_tokens"] == 1_000_000

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

    def test_by_provider_attributes_nightly_separately_from_session(
        self, ledger: Path
    ) -> None:
        """Issue athenaeum#1136 AC2: `athenaeum spend --by-provider` (which,
        despite its name, groups by ``run_type`` -- see the docstring above)
        must attribute a scheduled nightly's burn SEPARATELY from an
        interactive/session run's, once the nightly starts tagging itself
        ``spend.RUN_TYPE_LIBRARIAN_NIGHTLY`` instead of the bare
        ``spend.RUN_TYPE_LIBRARIAN`` a session run keeps using."""
        session_usage = TokenUsage()
        session_usage.add(1000, 200, 0, 0, model="claude-opus-4")
        nightly_usage = TokenUsage()
        nightly_usage.add(50_000, 10_000, 0, 0, model="claude-opus-4")

        spend.record_spend(
            session_usage, run_type=spend.RUN_TYPE_LIBRARIAN, provider="api"
        )
        spend.record_spend(
            nightly_usage, run_type=spend.RUN_TYPE_LIBRARIAN_NIGHTLY, provider="api"
        )

        s = spend.summarize(spend.read_ledger(ledger), by_provider=True)
        assert spend.RUN_TYPE_LIBRARIAN in s["by_run_type"]
        assert spend.RUN_TYPE_LIBRARIAN_NIGHTLY in s["by_run_type"]
        session_row = s["by_run_type"][spend.RUN_TYPE_LIBRARIAN]["api"]
        nightly_row = s["by_run_type"][spend.RUN_TYPE_LIBRARIAN_NIGHTLY]["api"]
        # Distinct, non-blended figures -- the nightly's much larger burn
        # does not leak into the session's bucket or vice versa.
        assert session_row["total_tokens"] == 1200
        assert nightly_row["total_tokens"] == 60_000
        assert nightly_row["estimated_cost_usd"] > session_row["estimated_cost_usd"]

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

    def test_cache_heavy_run_trips_the_per_run_ceiling_it_used_to_evade(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4 (athenaeum#1137): regression fixture is the recorded 2026-08-06
        claude-cli run this issue is about — 254 input, 59,916 output,
        1,169,154 cache-creation, 2,144,653 cache-read (real consumption
        3,373,977 tokens; total_tokens reads only 60,170, a 56.1x
        undercount). A 1,000,000-token per-run ceiling must trip against
        the real figure -- and provably would NOT have tripped under the
        old total_tokens-gated comparison, proving this is a behavior
        change, not a no-op test."""
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN", "1000000")
        usage = TokenUsage()
        usage.add(254, 59_916, 1_169_154, 2_144_653, model="claude-sonnet-4-6")

        assert usage.total_tokens == 60_170  # would NOT have tripped a 1M cap
        assert usage.billable_tokens == 3_373_977

        reason = spend.ceiling_tripped(usage, provider="claude-cli")
        assert reason is not None
        assert "per-run subscription token ceiling reached" in reason
        assert "3,373,977" in reason

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
        # input_tokens/output_tokens set explicitly (a real ledger row
        # always carries them, per build_record) so this fixture stays
        # accurate under the cache-inclusive basis (issue athenaeum#1137)
        # even though it has no cache traffic of its own.
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps(
                {
                    "ts": "2026-07-15T01:00:00Z",
                    "provider": "claude-cli",
                    "input_tokens": 1200,
                    "output_tokens": 0,
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

    def test_spend_today_sums_cache_traffic_off_a_real_ledger_row(
        self, ledger: Path
    ) -> None:
        """AC3 (athenaeum#1137): spend_today's subscription_tokens figure
        must be computed on the same cache-inclusive basis as the ceiling
        it feeds -- proven here with a REAL ledger row (written via
        record_spend -> build_record, not a hand-rolled dict) carrying
        genuine cache traffic, so the four-field read off the record is
        exercised end to end."""
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        usage = TokenUsage()
        usage.add(254, 59_916, 1_169_154, 2_144_653, model="claude-sonnet-4-6")
        usage.subscription_covered = True
        with_patch = spend.build_record(
            usage,
            run_type="librarian",
            provider="claude-cli",
            ts=datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc),
        )
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(with_patch) + "\n")

        today = spend.spend_today(ledger, now=now)
        assert today["subscription_tokens"] == 3_373_977.0  # NOT the 60,170 total_tokens

    def test_older_row_missing_cache_fields_degrades_to_input_plus_output(
        self, ledger: Path
    ) -> None:
        """AC3: a pre-cache-tracking ledger row (no cache_creation_input_tokens
        / cache_read_input_tokens fields at all) must default those to 0
        rather than crash or misread -- degrading gracefully to exactly
        today's (pre-athenaeum#1137) figure."""
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps(
                {
                    "ts": "2026-07-15T01:00:00Z",
                    "provider": "claude-cli",
                    "input_tokens": 800,
                    "output_tokens": 400,
                    "total_tokens": 1200,
                    "estimated_cost_usd": 0.0,
                    # cache_creation_input_tokens / cache_read_input_tokens
                    # deliberately absent.
                }
            )
            + "\n"
        )
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        today = spend.spend_today(ledger, now=now)
        assert today["subscription_tokens"] == 1200.0


# ---------------------------------------------------------------------------
# Budget window (issue athenaeum#1135 AC2/6) — today's spend vs. the per-day
# ceiling, ADDITIVE to the --since-window report, never replacing it.
# ---------------------------------------------------------------------------


class TestBudgetWindowStatus:
    def test_neither_path_configured(self, ledger: Path) -> None:
        result = spend.budget_window_status(None, ledger_path=ledger)
        assert result["api"] == {
            "configured": False,
            "cap_usd": None,
            "consumed_usd": 0.0,
            "remaining_usd": None,
            "fraction_consumed": None,
        }
        assert result["subscription"] == {
            "configured": False,
            "cap_tokens": None,
            "consumed_tokens": 0.0,
            "remaining_tokens": None,
            "fraction_consumed": None,
        }

    def test_api_path_reports_today_against_the_day_cap(
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
                    "estimated_cost_usd": 15.33,
                }
            )
            + "\n"
        )
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "15.00")
        result = spend.budget_window_status(None, ledger_path=ledger, now=now)
        assert result["api"]["configured"] is True
        assert result["api"]["cap_usd"] == 15.00
        assert result["api"]["consumed_usd"] == 15.33
        assert result["api"]["remaining_usd"] == pytest.approx(-0.33)
        assert result["subscription"]["configured"] is False

    def test_subscription_path_reports_today_against_the_day_cap(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        # input_tokens/output_tokens set explicitly (issue athenaeum#1137's
        # cache-inclusive basis reads those fields, not total_tokens
        # directly) — a real ledger row always carries them.
        ledger.write_text(
            json.dumps(
                {
                    "ts": "2026-07-15T01:00:00Z",
                    "provider": "claude-cli",
                    "input_tokens": 4000,
                    "output_tokens": 0,
                    "total_tokens": 4000,
                    "estimated_cost_usd": 0.0,
                }
            )
            + "\n"
        )
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY", "5000")
        result = spend.budget_window_status(None, ledger_path=ledger, now=now)
        assert result["subscription"]["configured"] is True
        assert result["subscription"]["cap_tokens"] == 5000
        assert result["subscription"]["consumed_tokens"] == 4000.0
        assert result["subscription"]["remaining_tokens"] == 1000.0
        assert result["subscription"]["fraction_consumed"] == pytest.approx(0.8)
        assert result["api"]["configured"] is False

    def test_never_blended_both_paths_independent(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": "2026-07-15T01:00:00Z",
                        "provider": "anthropic",
                        "total_tokens": 100,
                        "estimated_cost_usd": 3.0,
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "ts": "2026-07-15T02:00:00Z",
                        "provider": "claude-cli",
                        # input_tokens/output_tokens set explicitly (issue
                        # athenaeum#1137's cache-inclusive basis) — a real
                        # ledger row always carries them.
                        "input_tokens": 2000,
                        "output_tokens": 0,
                        "total_tokens": 2000,
                        "estimated_cost_usd": 0.0,
                    }
                )
                + "\n"
            )
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "10.00")
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY", "9000")
        result = spend.budget_window_status(None, ledger_path=ledger, now=now)
        assert result["api"]["consumed_usd"] == 3.0
        assert result["subscription"]["consumed_tokens"] == 2000.0


class TestFormatBudgetWindow:
    def test_none_when_nothing_configured(self) -> None:
        result = spend.budget_window_status(None, ledger_path=Path("/nonexistent"))
        assert spend.format_budget_window(result) is None

    def test_renders_configured_api_slot(self) -> None:
        window = {
            "api": {
                "configured": True,
                "cap_usd": 15.0,
                "consumed_usd": 7.5,
                "remaining_usd": 7.5,
                "fraction_consumed": 0.5,
            },
            "subscription": {
                "configured": False,
                "cap_tokens": None,
                "consumed_tokens": 0.0,
                "remaining_tokens": None,
                "fraction_consumed": None,
            },
        }
        rendered = spend.format_budget_window(window)
        assert rendered is not None
        assert "$7.50" in rendered
        assert "$15.00" in rendered
        assert "50%" in rendered


# ---------------------------------------------------------------------------
# Spend headroom + the warning that fires before a ceiling trips (athenaeum#926)
# ---------------------------------------------------------------------------


class TestHeadroom:
    """A run at 99% of a cap and a run at 1% must stop reading identically
    (issue athenaeum#926's whole point) — these pin the boundaries in both
    directions, plus the unset-ceiling and which-cap-names-itself contracts.

    Every test injects ``ledger_path=ledger`` explicitly (on top of the
    ``ledger`` fixture's env-var isolation) — this is regression class
    athenaeum#776: the ledger writer/reader must never touch the operator's live
    ``~/.cache/athenaeum/spend.jsonl``.
    """

    #: 1 input token == $1 exactly, so test dollar figures are exact integers
    #: instead of an approximation of real per-model pricing.
    _RATE = {"warn-test-model": (1_000_000.0, 0.0)}

    def _usage(self, dollars: int) -> TokenUsage:
        from athenaeum.models import configure_model_rates

        configure_model_rates(self._RATE)
        u = TokenUsage()
        u.add(dollars, 0, 0, 0, model="warn-test-model")
        return u

    # -- spend_headroom() itself -------------------------------------------

    def test_headroom_reports_remaining_and_fraction_for_configured_cap(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "100")
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", raising=False)
        headroom = spend.spend_headroom(self._usage(40), ledger_path=ledger)
        slot = headroom["per_run"]
        assert slot["configured"] is True
        assert slot["cap_usd"] == 100.0
        assert slot["consumed_usd"] == pytest.approx(40.0)
        assert slot["remaining_usd"] == pytest.approx(60.0)
        assert slot["fraction_consumed"] == pytest.approx(0.40)

    def test_headroom_unset_cap_is_a_distinct_value_not_zero(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", raising=False)
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", raising=False)
        headroom = spend.spend_headroom(self._usage(9999), ledger_path=ledger)
        for slot in (headroom["per_run"], headroom["per_day"]):
            assert slot["configured"] is False
            assert slot["cap_usd"] is None
            # Distinct from 0.0 -- an unset ceiling is not an exhausted one,
            # and a 0.0 fraction/remaining would be indistinguishable from a
            # configured-but-fully-untouched cap.
            assert slot["remaining_usd"] is None
            assert slot["fraction_consumed"] is None

    # -- boundaries, both directions (issue athenaeum#926 AC4) --------------

    def test_just_below_threshold_is_silent(
        self,
        ledger: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "100")
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", raising=False)
        caplog.set_level(logging.WARNING, logger="athenaeum.spend")
        usage = self._usage(74)  # 74% < the default 75% threshold
        assert spend.spend_headroom_warning(usage, ledger_path=ledger) is None
        assert spend.ceiling_tripped(usage, provider="api", ledger_path=ledger) is None
        assert "headroom" not in caplog.text

    def test_between_warning_and_ceiling_warns_but_does_not_trip(
        self,
        ledger: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "100")
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", raising=False)
        caplog.set_level(logging.WARNING, logger="athenaeum.spend")
        usage = self._usage(80)  # 80%: at/above warning threshold, below ceiling
        warning = spend.spend_headroom_warning(usage, ledger_path=ledger)
        assert warning is not None
        assert "per-run" in warning
        assert spend.ceiling_tripped(usage, provider="api", ledger_path=ledger) is None
        assert "headroom" in caplog.text

    def test_at_the_ceiling_trips_and_still_warns(
        self,
        ledger: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "100")
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", raising=False)
        caplog.set_level(logging.WARNING, logger="athenaeum.spend")
        usage = self._usage(100)  # exactly at the ceiling
        reason = spend.ceiling_tripped(usage, provider="api", ledger_path=ledger)
        assert reason is not None  # trips ...
        assert "per-run" in reason
        assert "headroom" in caplog.text  # ... and still warns, not only trips

    def test_unset_ceiling_does_not_warn(
        self,
        ledger: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", raising=False)
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", raising=False)
        caplog.set_level(logging.WARNING, logger="athenaeum.spend")
        usage = self._usage(999_999)  # huge spend, but no ceiling configured
        assert spend.spend_headroom_warning(usage, ledger_path=ledger) is None
        assert spend.ceiling_tripped(usage, provider="api", ledger_path=ledger) is None
        assert "headroom" not in caplog.text

    # -- the warning names WHICH cap, and by how much (issue athenaeum#926 AC3) --

    def test_warning_names_per_run_cap_and_amounts(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "100")
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", raising=False)
        warning = spend.spend_headroom_warning(self._usage(90), ledger_path=ledger)
        assert warning is not None
        assert "per-run" in warning
        assert "per-day" not in warning
        assert "$90.00" in warning
        assert "$100.00" in warning
        assert "$10.00 remaining" in warning

    def test_warning_names_per_day_cap_separately_from_per_run(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", raising=False)
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "100")
        warning = spend.spend_headroom_warning(self._usage(80), ledger_path=ledger)
        assert warning is not None
        assert "per-day" in warning
        assert "per-run" not in warning

    def test_both_caps_near_threshold_names_both(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "100")
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "100")
        warning = spend.spend_headroom_warning(self._usage(90), ledger_path=ledger)
        assert warning is not None
        assert "per-run" in warning
        assert "per-day" in warning

    # -- configurable threshold -----------------------------------------

    def test_custom_warning_threshold_via_env(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "100")
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", raising=False)
        usage = self._usage(60)  # silent at the default 75%, warns at 50%
        monkeypatch.delenv("ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT", raising=False)
        assert spend.spend_headroom_warning(usage, ledger_path=ledger) is None
        monkeypatch.setenv("ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT", "50")
        assert spend.spend_headroom_warning(usage, ledger_path=ledger) is not None

    def test_day_headroom_counts_prior_ledger_spend(
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
                    "estimated_cost_usd": 70.0,
                }
            )
            + "\n"
        )
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", raising=False)
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "100")
        headroom = spend.spend_headroom(self._usage(10), ledger_path=ledger, now=now)
        slot = headroom["per_day"]
        assert slot["consumed_usd"] == pytest.approx(80.0)  # 70 prior + 10 this run
        assert slot["fraction_consumed"] == pytest.approx(0.80)
        warning = spend.spend_headroom_warning(self._usage(10), ledger_path=ledger, now=now)
        assert warning is not None
        assert "per-day" in warning


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
        # input_tokens/output_tokens set explicitly (issue athenaeum#1137's
        # cache-inclusive basis) — a real ledger row always carries them.
        ledger.write_text(
            json.dumps(
                {
                    "ts": "2026-07-15T01:00:00Z",
                    "provider": "claude-cli",
                    "input_tokens": 45_000,
                    "output_tokens": 0,
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

    def test_warning_threshold_defaults_to_75(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT", raising=False)
        assert resolve_spend_warning_threshold_pct(None) == 75.0

    def test_warning_threshold_yaml_and_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT", raising=False)
        assert (
            resolve_spend_warning_threshold_pct({"spend": {"warning_threshold_pct": 60}}) == 60
        )
        monkeypatch.setenv("ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT", "90")
        assert (
            resolve_spend_warning_threshold_pct({"spend": {"warning_threshold_pct": 60}}) == 90
        )

    def test_warning_threshold_rejects_bool_and_nonpositive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT", raising=False)
        # bool must not coerce to 1; zero/negative fall through to the default,
        # never a threshold that would warn on a run that spent nothing.
        assert (
            resolve_spend_warning_threshold_pct({"spend": {"warning_threshold_pct": True}})
            == 75.0
        )
        assert (
            resolve_spend_warning_threshold_pct({"spend": {"warning_threshold_pct": 0}}) == 75.0
        )
        assert (
            resolve_spend_warning_threshold_pct({"spend": {"warning_threshold_pct": -5}})
            == 75.0
        )


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

    def test_json_billable_tokens_agrees_with_the_guarded_figure(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC5 (athenaeum#1137): the REPORTED subscription consumption must
        equal the figure the ceiling actually GUARDS -- a ceiling the report
        cannot corroborate is exactly the defect this issue closes.
        total_tokens stays present and cache-exclusive (AC2, additive)."""
        usage = TokenUsage()
        usage.add(254, 59_916, 1_169_154, 2_144_653, model="claude-sonnet-4-6")
        usage.subscription_covered = True
        assert spend.record_spend(usage, run_type="librarian", provider="claude-cli")

        rc = main(["spend", "--since", "30d", "--json", "--ledger", str(ledger)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)

        # The guarded figure -- exactly what ceiling_tripped compares.
        assert usage.billable_tokens == 3_373_977
        assert payload["subscription"]["billable_tokens"] == 3_373_977
        # total_tokens is unchanged and stays present alongside it (AC2).
        assert payload["subscription"]["total_tokens"] == 60_170

    def test_human_output_headline_is_billable_not_total(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC5: the human-readable ``Subscription`` line reports the same
        cache-inclusive figure as the JSON contract and the ceiling guard,
        not the cache-exclusive total_tokens (which would read as a mere
        60k here against a real 3.37M-token run)."""
        usage = TokenUsage()
        usage.add(254, 59_916, 1_169_154, 2_144_653, model="claude-sonnet-4-6")
        usage.subscription_covered = True
        spend.record_spend(usage, run_type="librarian", provider="claude-cli")

        rc = main(["spend", "--since", "30d", "--ledger", str(ledger)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "3.4M" in out  # _fmt_tokens(3_373_977) -- the billable figure
        assert "60k" not in out  # NOT the cache-exclusive total_tokens figure

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

    def test_json_output_includes_budget_window_when_configured(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Issue athenaeum#1135 AC2/6: an ADDITIVE ``budget_window`` key,
        alongside the existing --since-window totals -- never replacing
        them (the --since default stays 7d)."""
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "15.00")
        spend.record_spend(_api_usage(), run_type="librarian", provider="api")
        rc = main(["spend", "--since", "30d", "--json", "--ledger", str(ledger)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        # The existing --since-window totals are untouched.
        assert payload["api"]["estimated_cost_usd"] > 0.0
        # The additive figure.
        assert payload["budget_window"]["api"]["configured"] is True
        assert payload["budget_window"]["api"]["cap_usd"] == 15.00
        assert payload["budget_window"]["api"]["consumed_usd"] > 0.0
        assert payload["budget_window"]["subscription"]["configured"] is False

    def test_human_output_includes_budget_window_line(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", "15.00")
        spend.record_spend(_api_usage(), run_type="librarian", provider="api")
        rc = main(["spend", "--since", "30d", "--ledger", str(ledger)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Budget window" in out

    def test_human_output_omits_budget_window_when_unconfigured(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["spend", "--since", "30d", "--ledger", str(ledger)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Budget window" not in out


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

    def test_total_tokens_unchanged_and_excludes_cache_for_a_cache_heavy_run(
        self, ledger: Path
    ) -> None:
        """AC2 (athenaeum#1137): billable_tokens (the new cache-inclusive
        counter) must NOT change total_tokens's definition or value anywhere
        it is written or read -- total_tokens is the hestia cost-ledger.ts
        contract (tokens_by_model's {input, output, total}) and stays
        cache-exclusive so hestia's reader keeps reading byte-identical
        values. Uses the recorded 56x-undercount shape (254 input, 59,916
        output, 1,169,154 cache-creation, 2,144,653 cache-read) so a real
        regression here (total_tokens accidentally absorbing cache) would
        be caught at a figure large enough to be unmissable."""
        u = TokenUsage()
        u.add(254, 59_916, 1_169_154, 2_144_653, model="claude-sonnet-4-6")
        u.subscription_covered = True
        assert spend.record_spend(u, run_type="librarian", provider="claude-cli")
        rec = spend.read_ledger(ledger)[0]

        # Row-level total_tokens: input + output only, exactly as before.
        assert rec["total_tokens"] == 60_170
        assert rec["input_tokens"] == 254
        assert rec["output_tokens"] == 59_916
        assert rec["cache_creation_input_tokens"] == 1_169_154
        assert rec["cache_read_input_tokens"] == 2_144_653

        # tokens_by_model's hestia-contract {input, output, total} shape:
        # total excludes cache, matching the row-level figure above.
        entry = rec["tokens_by_model"]["claude-sonnet-4-6"]
        assert entry["total"] == 60_170
        assert entry["total"] == entry["input"] + entry["output"]

        # The bucket-level report (athenaeum spend / --json) keeps
        # total_tokens at the SAME cache-exclusive figure -- billable_tokens
        # is ADDITIVE, not a replacement.
        summary = spend.summarize([rec])
        assert summary["subscription"]["total_tokens"] == 60_170
        assert summary["subscription"]["billable_tokens"] == 3_373_977

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
    def test_ledger_version_stamps_current_schema(self, ledger: Path) -> None:
        # Not hardcoded to 3: athenaeum#1289 bumped LEDGER_VERSION to 4 for the
        # ADDITIVE tokens_by_surface field below -- a knob-tagged row still
        # carries tokens_by_knob regardless of which version stamps it. See
        # TestSchemaV4 for the v4-specific tokens_by_surface assertions.
        assert spend.record_spend(_mixed_knob_api_usage(), run_type="librarian", provider="api")
        rec = spend.read_ledger(ledger)[0]
        assert rec["v"] == spend.LEDGER_VERSION

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
# Schema v4 (issue athenaeum#1289) — per-SURFACE input-token attribution,
# tokens_by_surface, and the surface-unattributed pre-v4 contract. Mirrors
# TestSchemaV3 one field down, PLUS the "unattributed" synthesized remainder
# tokens_by_knob does not have (see spend.tokens_by_surface's docstring).
# ---------------------------------------------------------------------------


def _six_surface_api_usage() -> TokenUsage:
    """A metered run touching four of the six declared surfaces (the two
    zero-LLM-call surfaces -- tier0_passthrough / tier1_programmatic_match --
    never appear in per_surface by construction) PLUS ordinary untagged
    batched tier-2/tier-3 traffic, so the synthesized "unattributed"
    remainder is exercised too."""
    from athenaeum.models import (
        SURFACE_C4_CONTRADICTION,
        SURFACE_SAME_PAGE_MULTI_MERGE,
        SURFACE_TIER2_TRUNCATION_RETRY,
        SURFACE_TIER3_FULL_ECHO_FALLBACK,
    )

    u = TokenUsage()
    u.add(
        1_000, 200,
        model="claude-haiku-4-5-20251001", knob="classify",
        surface=SURFACE_C4_CONTRADICTION,
    )
    u.add(
        2_000, 400,
        model="claude-opus-4-7", knob="resolve",
        surface=SURFACE_C4_CONTRADICTION,
    )
    u.add(
        3_000, 600,
        model="claude-sonnet-4-6", knob="write",
        surface=SURFACE_SAME_PAGE_MULTI_MERGE,
    )
    u.add(
        4_000, 800,
        model="claude-haiku-4-5-20251001", knob="classify",
        surface=SURFACE_TIER2_TRUNCATION_RETRY,
    )
    u.add(
        5_000, 1_000,
        model="claude-sonnet-4-6", knob="write",
        surface=SURFACE_TIER3_FULL_ECHO_FALLBACK,
    )
    # Untagged (batched) traffic -- this is the remainder tokens_by_surface
    # must still account for under "unattributed".
    u.add_batch_tokens(6_000, 1_200, model="claude-haiku-4-5-20251001", knob="classify")
    return u


class TestSchemaV4:
    def test_ledger_version_is_4(self, ledger: Path) -> None:
        assert spend.LEDGER_VERSION == 4
        assert spend.record_spend(_six_surface_api_usage(), run_type="librarian", provider="api")
        rec = spend.read_ledger(ledger)[0]
        assert rec["v"] == 4

    def test_tokens_by_surface_is_a_sibling_of_tokens_by_knob(self, ledger: Path) -> None:
        """AC: tokens_by_surface carries the right surface per source, and
        tokens_by_knob/tokens_by_model keep their EXISTING shapes byte-for-byte."""
        from athenaeum.models import (
            SURFACE_C4_CONTRADICTION,
            SURFACE_SAME_PAGE_MULTI_MERGE,
            SURFACE_TIER2_TRUNCATION_RETRY,
            SURFACE_TIER3_FULL_ECHO_FALLBACK,
            SURFACE_UNATTRIBUTED,
        )

        assert spend.record_spend(_six_surface_api_usage(), run_type="librarian", provider="api")
        rec = spend.read_ledger(ledger)[0]

        tbs = rec["tokens_by_surface"]
        assert set(tbs) == {
            SURFACE_C4_CONTRADICTION,
            SURFACE_SAME_PAGE_MULTI_MERGE,
            SURFACE_TIER2_TRUNCATION_RETRY,
            SURFACE_TIER3_FULL_ECHO_FALLBACK,
            SURFACE_UNATTRIBUTED,
        }
        assert tbs[SURFACE_C4_CONTRADICTION]["input"] == 3_000  # 1_000 + 2_000
        assert tbs[SURFACE_C4_CONTRADICTION]["output"] == 600  # 200 + 400
        assert tbs[SURFACE_SAME_PAGE_MULTI_MERGE]["input"] == 3_000
        assert tbs[SURFACE_TIER2_TRUNCATION_RETRY]["input"] == 4_000
        assert tbs[SURFACE_TIER3_FULL_ECHO_FALLBACK]["input"] == 5_000
        assert tbs[SURFACE_UNATTRIBUTED]["input"] == 6_000  # the untagged batch call

        # tokens_by_knob / tokens_by_model are untouched by the surface
        # addition -- same shape as schema v3 produced.
        tbk = rec["tokens_by_knob"]
        assert set(tbk) == {"classify", "resolve", "write"}
        tbm = rec["tokens_by_model"]
        assert set(tbm) == {
            "claude-haiku-4-5-20251001", "claude-opus-4-7", "claude-sonnet-4-6",
        }

    def test_tokens_by_surface_conservation_property(self, ledger: Path) -> None:
        """AC: the sum of every tokens_by_surface entry's 'input' equals
        usage.input_tokens exactly -- the conservation property that makes
        the "unattributed" bucket meaningful rather than a silent gap."""
        usage = _six_surface_api_usage()
        by_surface = spend.tokens_by_surface(usage)
        assert sum(v["input"] for v in by_surface.values()) == usage.input_tokens
        assert sum(v["output"] for v in by_surface.values()) == usage.output_tokens

    def test_tokens_by_surface_all_unattributed_when_no_surface_tagged(self) -> None:
        """A run that tags no surface at all (every current non-six call
        site) still gets a tokens_by_surface entry -- unlike tokens_by_knob,
        which would be empty."""
        from athenaeum.models import SURFACE_UNATTRIBUTED

        usage = TokenUsage()
        usage.add(500, 100, model="claude-opus-4-7", knob="topic")
        by_surface = spend.tokens_by_surface(usage)
        assert set(by_surface) == {SURFACE_UNATTRIBUTED}
        assert by_surface[SURFACE_UNATTRIBUTED]["input"] == 500
        assert by_surface[SURFACE_UNATTRIBUTED]["output"] == 100

    def test_pre_v4_rows_readable_and_counted_surface_unattributed(self, ledger: Path) -> None:
        """A pre-v4 row (no per-surface attribution at all) stays readable
        and is counted as surface-unattributed -- never silently dropped --
        exactly as a pre-v3 row is counted knob-unattributed (athenaeum#1289)."""
        # A genuine v3 row, as athenaeum#781 wrote it: tokens_by_knob present,
        # tokens_by_surface absent entirely.
        v3 = {
            "v": 3,
            "ts": "2026-08-25T00:00:00Z",
            "run_type": "librarian",
            "provider": "anthropic",
            "billing_mode": "api",
            "subscription_covered": False,
            "models": ["claude-sonnet-4-6"],
            "tokens_by_model": {"claude-sonnet-4-6": {"input": 1000, "output": 200, "total": 1200}},
            "tokens_by_knob": {"write": {"input": 1000, "output": 200, "total": 1200}},
            "api_calls": 10,
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "estimated_cost_usd": 0.15,
            "notional_usd": 0.15,
        }
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(v3, separators=(",", ":")) + "\n", encoding="utf-8")
        # A conforming v4 row appended after it.
        spend.record_spend(_six_surface_api_usage(), run_type="librarian", provider="api")

        records = spend.read_ledger(ledger)
        assert len(records) == 2  # the v3 row is NOT dropped
        summary = spend.summarize(records)
        assert summary["record_count"] == 2
        assert summary["surface_unattributed_records"] == 1  # only the v3 row
        # knob_unattributed_records is unaffected -- both rows carry per-knob
        # attribution, this is purely the per-surface dimension.
        assert summary["knob_unattributed_records"] == 0
        # The v3 row is still present in its billing bucket.
        assert summary["api"]["records"] == 2

    def test_by_surface_summarize_and_format(self, ledger: Path) -> None:
        from athenaeum.models import SURFACE_C4_CONTRADICTION, SURFACE_UNATTRIBUTED

        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_six_surface_api_usage(), run_type="librarian", provider="api")
        summary = spend.summarize(spend.read_ledger(ledger), by_surface=True)
        assert SURFACE_C4_CONTRADICTION in summary["by_surface"]
        assert SURFACE_UNATTRIBUTED in summary["by_surface"]
        out = spend.format_summary(summary, since_label="7d", by_surface=True)
        assert "By surface:" in out
        assert SURFACE_C4_CONTRADICTION in out

    def test_by_surface_keeps_subscription_api_split_never_blended(self, ledger: Path) -> None:
        """AC: --by-surface reports tokens AND dollars per surface, but the
        two cost paths inside each surface bucket are never summed together."""
        from athenaeum.models import SURFACE_C4_CONTRADICTION

        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_six_surface_api_usage(), run_type="librarian", provider="api")
        summary = spend.summarize(spend.read_ledger(ledger), by_surface=True)
        slot = summary["by_surface"][SURFACE_C4_CONTRADICTION]
        assert "subscription" in slot
        assert "api" in slot
        assert slot["subscription"]["estimated_cost_usd"] == 0.0
        assert slot["api"]["estimated_cost_usd"] > 0.0

    def test_cli_by_surface_flag(self, ledger: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from athenaeum.models import SURFACE_C4_CONTRADICTION, SURFACE_UNATTRIBUTED

        spend.record_spend(_six_surface_api_usage(), run_type="librarian", provider="api")
        rc = main(["spend", "--since", "30d", "--by-surface", "--json", "--ledger", str(ledger)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert SURFACE_C4_CONTRADICTION in payload["by_surface"]
        assert SURFACE_UNATTRIBUTED in payload["by_surface"]

        rc = main(["spend", "--since", "30d", "--by-surface", "--ledger", str(ledger)])
        assert rc == 0
        human = capsys.readouterr().out
        assert "By surface:" in human

    def test_summarize_existing_shape_unchanged_by_surface_opt_in(self, ledger: Path) -> None:
        """AC: summarize()'s existing shape is unchanged when by_surface is
        not requested -- it only ADDS keys, mirroring by_knob's own test."""
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        summary = spend.summarize(spend.read_ledger(ledger))
        for key in ("subscription", "api", "unknown", "record_count"):
            assert key in summary
        assert "by_surface" not in summary
        assert "surface_unattributed_records" in summary  # additive, always present


# ---------------------------------------------------------------------------
# Per-knob provider routing (issue athenaeum#786) — AC2/AC7: two knobs on
# DIFFERENT providers in one session, each ledger row carrying the provider
# that ACTUALLY served it (resolved via provider.resolve_provider(config,
# knob=...), not hardcoded), and --by-knob showing the split. This is the
# same "recall sidecar on the subscription while the resolver runs on the
# metered API" shape the issue's motivation names -- driven through the two
# real call sites athenaeum#786 wired independently: query_topics.py
# ("topic") and the ingest-answers/reresolve-questions CLI path
# ("resolve"), via athenaeum.provider.resolve_provider directly (no live
# network needed -- resolve_provider is a pure config/env lookup).
# ---------------------------------------------------------------------------


class TestPerKnobProviderRoutingLedger:
    def _topic_usage(self) -> TokenUsage:
        u = TokenUsage()
        u.add(50, 20, 0, 0, model="claude-haiku-4-5-20251001", knob="topic")
        return u

    def _resolve_usage(self) -> TokenUsage:
        u = TokenUsage()
        u.add(2_000, 800, 0, 0, model="claude-opus-4", knob="resolve")
        return u

    def test_two_knobs_on_different_providers_resolve_independently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.provider import resolve_provider

        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_TOPIC_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_RESOLVE_LLM_PROVIDER", raising=False)
        config = {
            "llm": {
                "provider": "api",  # global default
                "providers": {"topic": "claude-cli"},  # recall sidecar only
            }
        }
        assert resolve_provider(config, knob="topic") == "claude-cli"
        assert resolve_provider(config, knob="resolve") == "api"  # inherits global

    def test_ledger_rows_carry_the_actually_served_provider(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: with the ``topic`` knob pinned to claude-cli and every other
        knob on the global ``api`` default, each command's own ledger row
        (query_topics's and the resolve CLI path's) is tagged with the
        provider ITS knob actually resolved to -- mirroring exactly how
        query_topics.py / answers.py now call
        ``resolve_provider(config, knob=...)`` before ``record_spend``."""
        from athenaeum.provider import resolve_provider

        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_TOPIC_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_RESOLVE_LLM_PROVIDER", raising=False)
        config = {
            "llm": {"provider": "api", "providers": {"topic": "claude-cli"}}
        }

        # Mirrors query_topics.extract_topics's own record_spend call.
        assert spend.record_spend(
            self._topic_usage(),
            run_type="query-topics",
            provider=resolve_provider(config, knob="topic"),
            config=config,
        )
        # Mirrors answers.ingest_answers's own record_spend call.
        assert spend.record_spend(
            self._resolve_usage(),
            run_type="answers",
            provider=resolve_provider(config, knob="resolve"),
            config=config,
        )

        records = spend.read_ledger(ledger)
        assert len(records) == 2
        topic_row = next(r for r in records if r["run_type"] == "query-topics")
        resolve_row = next(r for r in records if r["run_type"] == "answers")

        assert topic_row["provider"] == "claude-cli"
        assert topic_row["billing_mode"] == spend.BILLING_MODE_SUBSCRIPTION
        assert topic_row["estimated_cost_usd"] == 0.0
        assert topic_row["tokens_by_knob"]["topic"]["total"] == 70

        assert resolve_row["provider"] == "anthropic"
        assert resolve_row["billing_mode"] == spend.BILLING_MODE_API
        assert resolve_row["estimated_cost_usd"] > 0.0
        assert resolve_row["tokens_by_knob"]["resolve"]["total"] == 2_800

    def test_by_knob_shows_the_provider_split_ac7(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC7: ``athenaeum spend --by-knob`` (spend.summarize(by_knob=True))
        shows ``topic`` in the subscription bucket and ``resolve`` in the api
        bucket -- the split athenaeum#786 makes reachable via per-knob config,
        requiring NO ledger-schema change (tokens_by_knob + the row-level
        billing_mode this test's sibling above verifies were already
        sufficient -- see athenaeum#786's PR for the read-side verification)."""
        from athenaeum.provider import resolve_provider

        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_TOPIC_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_RESOLVE_LLM_PROVIDER", raising=False)
        config = {
            "llm": {"provider": "api", "providers": {"topic": "claude-cli"}}
        }
        spend.record_spend(
            self._topic_usage(),
            run_type="query-topics",
            provider=resolve_provider(config, knob="topic"),
            config=config,
        )
        spend.record_spend(
            self._resolve_usage(),
            run_type="answers",
            provider=resolve_provider(config, knob="resolve"),
            config=config,
        )

        summary = spend.summarize(spend.read_ledger(ledger), by_knob=True)
        assert summary["by_knob"]["topic"]["subscription"]["total_tokens"] == 70
        assert summary["by_knob"]["topic"]["api"]["total_tokens"] == 0
        assert summary["by_knob"]["resolve"]["api"]["total_tokens"] == 2_800
        assert summary["by_knob"]["resolve"]["subscription"]["total_tokens"] == 0

        out = spend.format_summary(summary, since_label="7d", by_knob=True)
        assert "topic" in out
        assert "resolve" in out

    def test_config_with_no_per_knob_keys_both_knobs_land_on_global_ac6(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC6: no ``llm.providers`` section -> both knobs resolve to the
        SAME global provider, and both ledger rows land in the same billing
        bucket -- exactly the pre-athenaeum#786 single-provider shape."""
        from athenaeum.provider import resolve_provider

        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_TOPIC_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_RESOLVE_LLM_PROVIDER", raising=False)
        config = {"llm": {"provider": "claude-cli"}}

        spend.record_spend(
            self._topic_usage(),
            run_type="query-topics",
            provider=resolve_provider(config, knob="topic"),
            config=config,
        )
        spend.record_spend(
            self._resolve_usage(),
            run_type="answers",
            provider=resolve_provider(config, knob="resolve"),
            config=config,
        )
        records = spend.read_ledger(ledger)
        assert {r["provider"] for r in records} == {"claude-cli"}
        assert {r["billing_mode"] for r in records} == {
            spend.BILLING_MODE_SUBSCRIPTION
        }


class TestRecordSpendPerKnobProvider:
    """``record_spend_per_knob_provider`` (issue athenaeum#841 AC2) — splits ONE
    librarian run's usage into one ledger row per DISTINCT provider its
    knobs actually resolved to, instead of one row assuming a single
    provider for the whole run. Mirrors
    ``TestPerKnobProviderRoutingLedger`` above (the athenaeum#786 precedent for
    query_topics/answers, which get their own SEPARATE record_spend calls
    outside the librarian's single run) but for ONE run whose knobs
    genuinely mix providers."""

    def _mixed_usage(self) -> TokenUsage:
        u = TokenUsage()
        # classify + resolve + reasoning_t1 + reasoning_t2 stay on the
        # global "api" default; "write" is overridden to "claude-cli".
        # ``add_tokens`` (not ``add``) so ``api_calls`` is set explicitly
        # below, matching how a real call-site loop counts attempts
        # separately from token accumulation (models.TokenUsage.add_tokens's
        # own documented convention).
        u.add_tokens(1_000, 400, 0, 0, model="claude-haiku-4-5", knob="classify")
        u.add_tokens(2_000, 800, 0, 0, model="claude-opus-4", knob="resolve")
        u.add_tokens(500, 100, 0, 0, model="claude-haiku-4-5", knob="reasoning_t1")
        u.add_tokens(500, 100, 0, 0, model="claude-opus-4", knob="reasoning_t2")
        u.add_tokens(3_000, 1_200, 0, 0, model="claude-sonnet-4-6", knob="write")
        u.api_calls = 5
        return u

    def _knob_providers_mixed(self) -> dict[str, str]:
        return {
            "classify": "api",
            "resolve": "api",
            "reasoning_t1": "api",
            "reasoning_t2": "api",
            "write": "claude-cli",
        }

    def _knob_models_mixed(self) -> dict[str, str]:
        return {
            "classify": "claude-haiku-4-5",
            "resolve": "claude-opus-4",
            "reasoning_t1": "claude-haiku-4-5",
            "reasoning_t2": "claude-opus-4",
            "write": "claude-sonnet-4-6",
        }

    def test_single_provider_writes_one_row_identical_to_record_spend(
        self, ledger: Path
    ) -> None:
        """AC6: every knob resolving to the SAME provider (the default, no
        overrides) writes exactly the row ``record_spend`` would have
        written -- no behavior change for the common case."""
        usage = TokenUsage()
        usage.add_tokens(1_000, 400, model="claude-haiku-4-5", knob="classify")
        usage.add_tokens(2_000, 800, model="claude-opus-4", knob="write")
        usage.api_calls = 3
        knob_providers = {"classify": "api", "write": "api"}
        knob_models = {"classify": "claude-haiku-4-5", "write": "claude-opus-4"}

        assert spend.record_spend_per_knob_provider(
            usage,
            knob_providers,
            knob_models,
            run_type="librarian",
            default_provider="api",
            files_processed=7,
        )
        records = spend.read_ledger(ledger)
        assert len(records) == 1
        row = records[0]
        assert row["provider"] == "anthropic"
        assert row["billing_mode"] == spend.BILLING_MODE_API
        assert row["api_calls"] == 3
        assert row["files_processed"] == 7
        assert row["tokens_by_knob"]["classify"]["total"] == 1_400
        assert row["tokens_by_knob"]["write"]["total"] == 2_800

    def test_mixed_providers_write_two_rows_with_correct_billing_mode(
        self, ledger: Path
    ) -> None:
        """AC2: two knobs on different providers in ONE run each record
        their own provider and correct billing_mode."""
        assert spend.record_spend_per_knob_provider(
            self._mixed_usage(),
            self._knob_providers_mixed(),
            self._knob_models_mixed(),
            run_type="librarian",
            default_provider="api",
            files_processed=4,
        )
        records = spend.read_ledger(ledger)
        assert len(records) == 2

        api_row = next(r for r in records if r["provider"] == "anthropic")
        cli_row = next(r for r in records if r["provider"] == "claude-cli")

        assert api_row["billing_mode"] == spend.BILLING_MODE_API
        assert api_row["estimated_cost_usd"] > 0.0
        assert set(api_row["tokens_by_knob"]) == {
            "classify",
            "resolve",
            "reasoning_t1",
            "reasoning_t2",
        }
        assert "write" not in api_row["tokens_by_knob"]

        assert cli_row["billing_mode"] == spend.BILLING_MODE_SUBSCRIPTION
        assert cli_row["estimated_cost_usd"] == 0.0
        assert set(cli_row["tokens_by_knob"]) == {"write"}
        assert cli_row["tokens_by_knob"]["write"]["total"] == 4_200

    def test_api_calls_and_files_processed_only_on_default_row(
        self, ledger: Path
    ) -> None:
        """api_calls/files_processed are run-level, not knob-attributed —
        they must land on exactly ONE row (the default-provider one), never
        split or duplicated across rows."""
        spend.record_spend_per_knob_provider(
            self._mixed_usage(),
            self._knob_providers_mixed(),
            self._knob_models_mixed(),
            run_type="librarian",
            default_provider="api",
            files_processed=4,
        )
        records = spend.read_ledger(ledger)
        api_row = next(r for r in records if r["provider"] == "anthropic")
        cli_row = next(r for r in records if r["provider"] == "claude-cli")

        assert api_row["api_calls"] == 5
        assert api_row["files_processed"] == 4
        assert cli_row["api_calls"] == 0
        assert "files_processed" not in cli_row

    def test_untagged_remainder_rides_on_default_provider_row(
        self, ledger: Path
    ) -> None:
        """Tokens accumulated WITHOUT a knob= tag (e.g. a call site that
        forgot to tag one) must not silently vanish from a mixed-provider
        split -- they land on the default-provider row."""
        usage = self._mixed_usage()
        # Untagged remainder: total scalar counters exceed the per-knob-
        # tagged subset by (500, 50, 0, 0).
        usage.input_tokens += 500
        usage.output_tokens += 50

        spend.record_spend_per_knob_provider(
            usage,
            self._knob_providers_mixed(),
            self._knob_models_mixed(),
            run_type="librarian",
            default_provider="api",
        )
        records = spend.read_ledger(ledger)
        api_row = next(r for r in records if r["provider"] == "anthropic")
        cli_row = next(r for r in records if r["provider"] == "claude-cli")

        # Total input/output tokens across both rows equal the run's true
        # totals -- nothing lost, nothing double-counted.
        assert api_row["input_tokens"] + cli_row["input_tokens"] == usage.input_tokens
        assert (
            api_row["output_tokens"] + cli_row["output_tokens"]
            == usage.output_tokens
        )

    def test_unmapped_knob_falls_back_to_default_provider(
        self, ledger: Path
    ) -> None:
        """A knob with tokens but no entry in knob_providers (defensive —
        should not happen for a caller that resolves every knob it tags)
        falls back to default_provider rather than raising or vanishing."""
        usage = TokenUsage()
        usage.api_calls = 1
        usage.add(100, 50, model="claude-haiku-4-5", knob="mystery-knob")
        usage.add(3_000, 1_200, model="claude-sonnet-4-6", knob="write")

        assert spend.record_spend_per_knob_provider(
            usage,
            {"write": "claude-cli"},  # "mystery-knob" deliberately absent
            {"write": "claude-sonnet-4-6"},
            run_type="librarian",
            default_provider="api",
        )
        records = spend.read_ledger(ledger)
        api_row = next(r for r in records if r["provider"] == "anthropic")
        assert "mystery-knob" in api_row["tokens_by_knob"]

    def test_returns_false_when_nothing_spent(self) -> None:
        assert (
            spend.record_spend_per_knob_provider(
                TokenUsage(),
                {},
                {},
                run_type="librarian",
                default_provider="api",
            )
            is False
        )

    def test_cache_only_usage_is_not_dropped(self, ledger: Path) -> None:
        """athenaeum#1137: this function has its OWN copy of record_spend's
        no-op guard, checked BEFORE the per-provider split -- a cache-only
        knob (zero input, zero output) must not be silently dropped here
        either, independent of record_spend's own guard being fixed."""
        usage = TokenUsage()
        usage.add_tokens(
            0, 0, 200_000, 900_000, model="claude-sonnet-4-6", knob="write"
        )
        assert usage.api_calls == 0
        assert usage.total_tokens == 0

        assert (
            spend.record_spend_per_knob_provider(
                usage,
                {"write": "claude-cli"},
                {"write": "claude-sonnet-4-6"},
                run_type="librarian",
                default_provider="claude-cli",
            )
            is True
        )
        rec = spend.read_ledger(ledger)[0]
        assert rec["cache_creation_input_tokens"] == 200_000
        assert rec["cache_read_input_tokens"] == 900_000


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
        assert recs[0]["v"] == spend.LEDGER_VERSION
        assert recs[0]["billing_mode"] == "api"
        assert recs[0]["notional_usd"] == recs[0]["estimated_cost_usd"]
        tbm = recs[0]["tokens_by_model"]
        assert list(tbm) and tbm[next(iter(tbm))]["input"] == 120
        # athenaeum#781: the real call site (query_topics.extract_topics) threads the
        # ``topic`` knob end to end into the ledger row -- one of the six
        # knobs attributable per the acceptance criterion.
        assert recs[0]["tokens_by_knob"]["topic"]["input"] == 120

    def test_topic_knob_override_writes_a_subscription_row(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue athenaeum#786: with ``llm.providers.topic: claude-cli`` and the
        GLOBAL default left on ``api``, the real ``extract_topics`` call
        writes a SUBSCRIPTION ledger row (not an api/dollars one) -- the
        client construction (test_query_topics.py's sibling test) and the
        ledger tag are driven by the SAME per-knob resolution, so they can't
        drift apart."""
        from athenaeum import provider, query_topics
        from tests.conftest import FakeLLMClient

        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_TOPIC_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")

        fake_cli = FakeLLMClient(
            response=make_llm_response(
                '["Return Path"]',
                usage=make_llm_usage(input_tokens=50, output_tokens=10),
            )
        )
        monkeypatch.setattr(provider, "ClaudeCliClient", fake_cli)

        topics = query_topics.extract_topics(
            "Tell me about Return Path please",
            config={
                "llm": {"provider": "api", "providers": {"topic": "claude-cli"}}
            },
        )
        assert topics == ["Return Path"]

        recs = spend.read_ledger(ledger)
        assert len(recs) == 1
        assert recs[0]["run_type"] == "query-topics"
        assert recs[0]["provider"] == "claude-cli"
        assert recs[0]["billing_mode"] == spend.BILLING_MODE_SUBSCRIPTION
        assert recs[0]["estimated_cost_usd"] == 0.0
        assert recs[0]["tokens_by_knob"]["topic"]["input"] == 50


# ---------------------------------------------------------------------------
# Repricing (issue athenaeum#788) — `athenaeum spend --reprice`
#
# tokens_by_model (athenaeum#487) exists SO THAT history can be repriced; before
# athenaeum#788 nothing consumed it, so correcting a rate (athenaeum#777's 6.67x Fable
# under-report) or making rates config-owned (athenaeum#783) bought nothing
# retroactively. These pin the door: read-only, unpriceable rows reported
# rather than dropped or zeroed, and the two cost paths never blended.
# ---------------------------------------------------------------------------


def _write_raw_row(ledger: Path, record: dict[str, Any]) -> None:
    """Append a hand-built row, bypassing build_record.

    The only way to produce a genuinely pre-v2 row (no ``tokens_by_model``) —
    the writer always emits the field now, so the unpriceable path cannot be
    exercised through ``record_spend``.
    """
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def _pre_v2_api_row(usd: float = 4.25) -> dict[str, Any]:
    """A pre-athenaeum#487 API row: real stored dollars, NO per-model attribution."""
    return {
        "v": 1,
        "ts": "2026-01-01T00:00:00Z",
        "run_type": "librarian",
        "provider": "anthropic",
        "session_id": None,
        "models": [],
        "api_calls": 3,
        "input_tokens": 500_000,
        "output_tokens": 100_000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 600_000,
        "estimated_cost_usd": usd,
    }


def _knowledge_dir(tmp_path: Path, pricing_yaml: str = "") -> Path:
    """A knowledge dir for ``--path``, optionally carrying a ``pricing:`` block.

    Always passed explicitly by the CLI reprice tests: ``--path`` defaults to
    the operator's real ``~/knowledge``, so a test that omits it would resolve
    ITS pricing section and stop being hermetic.
    """
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "athenaeum.yaml").write_text(pricing_yaml, encoding="utf-8")
    return knowledge


class TestReprice:
    def test_reprice_reports_new_figure_while_stored_row_is_untouched(
        self, ledger: Path
    ) -> None:
        """athenaeum#788 AC5 (and the heart of AC1): write a row under one rate
        table, change the rate, and --reprice reports the NEW figure while the
        stored row keeps the old one."""
        from athenaeum.models import configure_model_rates

        assert spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        stored_row = spend.read_ledger(ledger)[0]
        stored_usd = stored_row["estimated_cost_usd"]
        assert stored_usd > 0.0

        # Double the rate for the model this row is tagged with.
        configure_model_rates({"claude-opus-4": (10.0, 50.0)})
        repriced = spend.reprice(spend.read_ledger(ledger))

        assert repriced["api"]["repriced_usd"] == pytest.approx(stored_usd * 2)
        assert repriced["api"]["stored_usd"] == pytest.approx(stored_usd)
        assert repriced["api"]["delta_usd"] == pytest.approx(stored_usd)
        # The ROW on disk still carries the original figure — repricing reports,
        # it does not rewrite.
        assert spend.read_ledger(ledger)[0]["estimated_cost_usd"] == stored_usd

    def test_ledger_file_is_byte_identical_after_a_reprice_run(
        self, ledger: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """athenaeum#788 AC2: the ledger is append-only; a reprice must leave the
        file byte-for-byte unchanged. Asserted against the CLI, the surface an
        operator actually runs."""
        from athenaeum.models import configure_model_rates

        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        _write_raw_row(ledger, _pre_v2_api_row())

        before = ledger.read_bytes()
        before_mtime = ledger.stat().st_mtime_ns

        configure_model_rates({"claude-opus-4": (99.0, 99.0)})
        rc = main(
            ["spend", "--since", "3650d", "--reprice",
             "--ledger", str(ledger), "--path", str(_knowledge_dir(tmp_path))]
        )
        assert rc == 0
        capsys.readouterr()

        assert ledger.read_bytes() == before
        assert ledger.stat().st_mtime_ns == before_mtime

    def test_unpriceable_rows_are_counted_never_dropped_never_zeroed(
        self, ledger: Path
    ) -> None:
        """athenaeum#788 AC3: a row with no tokens_by_model is reported as
        unpriceable with a count — it stays in record_count, its stored dollars
        are still reported, and it is never silently priced at zero."""
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        _write_raw_row(ledger, _pre_v2_api_row(usd=4.25))

        repriced = spend.reprice(spend.read_ledger(ledger))

        # Not dropped.
        assert repriced["record_count"] == 2
        assert repriced["api"]["records"] == 2
        # Counted, at both levels.
        assert repriced["unpriceable_records"] == 1
        assert repriced["api"]["unpriceable_records"] == 1
        assert repriced["repriced_records"] == 1
        # Not zeroed — the stored dollars repricing could not touch are
        # reported explicitly.
        assert repriced["api"]["unpriceable_stored_usd"] == pytest.approx(4.25)
        # ...and they are excluded from the like-for-like delta base, so the
        # delta reflects the repriced row alone rather than the unpriceable
        # row's stored value masquerading as a rate change.
        assert repriced["api"]["stored_usd"] == pytest.approx(
            repriced["api"]["stored_usd_priceable"] + 4.25
        )
        assert repriced["api"]["delta_usd"] == pytest.approx(
            repriced["api"]["repriced_usd"] - repriced["api"]["stored_usd_priceable"]
        )

    def test_reprice_record_returns_none_not_zero_for_an_unattributed_row(self) -> None:
        """The unit-level distinction AC3 rests on: unknown price is None, which
        a caller can tell apart from a genuine $0."""
        assert spend.reprice_record(_pre_v2_api_row()) is None
        assert spend.reprice_record({"tokens_by_model": {}}) is None
        priced = spend.reprice_record(
            {"tokens_by_model": {"claude-opus-4": {"input": 1_000_000, "output": 0}}}
        )
        assert priced == pytest.approx(5.0)

    def test_subscription_and_api_are_never_blended(self, ledger: Path) -> None:
        """athenaeum#788 AC4: repricing preserves the billing-mode split — a
        subscription row never becomes real dollars, and the two paths are
        reported in separate buckets."""
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")

        repriced = spend.reprice(spend.read_ledger(ledger))

        assert repriced["subscription"]["records"] == 1
        assert repriced["api"]["records"] == 1
        # A subscription row carries $0 real dollars stored AND repriced —
        # repricing must never turn subscription notional into money owed.
        assert repriced["subscription"]["stored_usd"] == 0.0
        assert repriced["subscription"]["repriced_usd"] == 0.0
        assert repriced["subscription"]["delta_usd"] == 0.0
        # Its notional IS repriced — that is the figure that means something on
        # this path.
        assert repriced["subscription"]["repriced_notional_usd"] > 0.0
        # And the api bucket carries only the api row's dollars: no subscription
        # tokens leaked in.
        assert repriced["api"]["repriced_usd"] == pytest.approx(
            spend.reprice_record(spend.read_ledger(ledger)[1])
        )

    def test_unknown_billing_mode_stays_in_its_own_bucket(self, ledger: Path) -> None:
        """athenaeum#694's distinct-state contract survives repricing: an
        undeterminable row is never folded into api."""
        _write_raw_row(
            ledger,
            {
                "v": 2,
                "ts": "2026-01-01T00:00:00Z",
                "run_type": "librarian",
                "provider": "mystery-proxy",
                "estimated_cost_usd": 1.0,
                "notional_usd": 1.0,
                "tokens_by_model": {"claude-opus-4": {"input": 1_000_000, "output": 0}},
            },
        )
        repriced = spend.reprice(spend.read_ledger(ledger))
        assert repriced["unknown"]["records"] == 1
        assert repriced["unknown"]["repriced_usd"] == pytest.approx(5.0)
        assert repriced["api"]["records"] == 0
        assert repriced["api"]["repriced_usd"] == 0.0

    def test_reprice_uses_the_writers_own_cache_and_batch_arithmetic(
        self, ledger: Path
    ) -> None:
        """A row with cache and batch traffic must reprice through the SAME
        multipliers that wrote it (athenaeum#239's 1.25x/0.10x, athenaeum#236's 50%
        batch discount) — at an UNCHANGED rate table, reprice must reproduce
        the stored figure exactly. This is what catches a reimplemented formula
        drifting from TokenUsage._cost_for."""
        u = TokenUsage()
        u.add(100_000, 20_000, 40_000, 90_000, model="claude-sonnet-4-6")
        u.add_batch_tokens(200_000, 30_000, 10_000, 5_000, model="claude-haiku-4-5")
        assert spend.record_spend(u, run_type="librarian", provider="api")

        rec = spend.read_ledger(ledger)[0]
        assert spend.reprice_record(rec) == pytest.approx(rec["estimated_cost_usd"])

        repriced = spend.reprice([rec])
        assert repriced["api"]["delta_usd"] == pytest.approx(0.0)

    def test_reprice_at_unchanged_rates_reports_a_zero_delta(self, ledger: Path) -> None:
        """The no-op case: nothing has changed, so the report says so."""
        spend.record_spend(_mixed_model_api_usage(), run_type="librarian", provider="api")
        repriced = spend.reprice(spend.read_ledger(ledger))
        assert repriced["api"]["delta_usd"] == pytest.approx(0.0)
        assert repriced["api"]["repriced_records"] == 1
        assert repriced["unpriceable_records"] == 0

    def test_empty_ledger_reprices_to_an_empty_report(self, ledger: Path) -> None:
        repriced = spend.reprice([])
        assert repriced["record_count"] == 0
        assert repriced["repriced_records"] == 0
        assert repriced["unpriceable_records"] == 0
        for bucket in ("subscription", "api", "unknown"):
            assert repriced[bucket]["delta_usd"] == 0.0

    def test_format_reprice_shows_stored_repriced_and_delta(self, ledger: Path) -> None:
        from athenaeum.models import configure_model_rates

        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        _write_raw_row(ledger, _pre_v2_api_row())
        configure_model_rates({"claude-opus-4": (10.0, 50.0)})

        out = spend.format_reprice(
            spend.reprice(spend.read_ledger(ledger)), since_label="30d"
        )
        assert "reprice" in out.lower()
        assert "ledger NOT modified" in out
        assert "repriced" in out
        assert "delta" in out
        assert "unpriceable" in out
        # The stored dollars repricing could not touch are NAMED, not just
        # counted -- "not zeroed" has to be legible in the human report too.
        assert "4.2500" in out
        # The unknown row is absent, so its line is suppressed -- mirroring
        # format_summary, which surfaces unknown only when present.
        assert "Unknown" not in out

    def test_format_reprice_surfaces_an_unknown_row_when_present(
        self, ledger: Path
    ) -> None:
        """athenaeum#694's distinct state must be VISIBLE in the report, not just
        in the JSON — an undeterminable row a reader never sees is one they
        will mistake for no activity."""
        _write_raw_row(
            ledger,
            {
                "v": 2,
                "ts": "2026-01-01T00:00:00Z",
                "run_type": "librarian",
                "provider": "mystery-proxy",
                "estimated_cost_usd": 1.0,
                "notional_usd": 1.0,
                "tokens_by_model": {"claude-opus-4": {"input": 1_000_000, "output": 0}},
            },
        )
        out = spend.format_reprice(
            spend.reprice(spend.read_ledger(ledger)), since_label="30d"
        )
        assert "Unknown" in out
        assert "undeterminable" in out

    def test_a_malformed_per_model_entry_is_skipped_not_fatal(self) -> None:
        """The ledger reader already tolerates a torn/hand-edited line; the
        repricer must not become the thing that crashes on one. A non-dict
        bucket is skipped, and the row still reprices from its sound entries."""
        priced = spend.reprice_record(
            {
                "tokens_by_model": {
                    "claude-opus-4": {"input": 1_000_000, "output": 0},
                    "claude-sonnet-4-6": "corrupt",
                }
            }
        )
        assert priced == pytest.approx(5.0)


class TestRepriceCommand:
    def test_cli_reprice_json_shape(
        self, ledger: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """athenaeum#788 AC1 over the CLI: recomputed alongside stored, with the
        delta, in the machine-readable shape."""
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        stored = spend.read_ledger(ledger)[0]["estimated_cost_usd"]
        knowledge = _knowledge_dir(
            tmp_path, "pricing:\n  claude-opus-4: [10.0, 50.0]\n"
        )

        rc = main(
            ["spend", "--since", "30d", "--reprice", "--json",
             "--ledger", str(ledger), "--path", str(knowledge)]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["ledger_path"] == str(ledger)
        api = payload["reprice"]["api"]
        assert api["stored_usd"] == pytest.approx(stored)
        assert api["repriced_usd"] == pytest.approx(stored * 2)
        assert api["delta_usd"] == pytest.approx(stored)

    def test_cli_reprice_human_output(
        self, ledger: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        rc = main(
            ["spend", "--since", "30d", "--reprice",
             "--ledger", str(ledger), "--path", str(_knowledge_dir(tmp_path))]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "API" in out
        assert "Subscription" in out
        assert "delta" in out

    def test_cli_reprice_honours_the_yaml_pricing_section(
        self, ledger: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """athenaeum#788: "at CURRENT rates" means the operator's CONFIGURED
        rates (athenaeum#783), not the code-default table — the whole reason
        configurable pricing was a blocker for this issue. Pinned by contrast:
        the SAME ledger reprices differently under two different configs, and
        an in-process rate table cannot override what the config says.
        """
        from athenaeum.models import configure_model_rates

        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        stored = spend.read_ledger(ledger)[0]["estimated_cost_usd"]

        # An ambient process table must NOT leak into the report: the command
        # resolves rates from config, so its answer is reproducible from the
        # operator's yaml alone.
        configure_model_rates({"claude-opus-4": (1000.0, 1000.0)})

        override = _knowledge_dir(
            tmp_path / "with", "pricing:\n  claude-opus-4: [10.0, 50.0]\n"
        )
        rc = main(
            ["spend", "--since", "30d", "--reprice", "--json",
             "--ledger", str(ledger), "--path", str(override)]
        )
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["reprice"]["api"][
            "repriced_usd"
        ] == pytest.approx(stored * 2)

        # No pricing: section -> the code-default table, so an unchanged rate
        # reprices to the stored figure.
        plain = _knowledge_dir(tmp_path / "without")
        rc = main(
            ["spend", "--since", "30d", "--reprice", "--json",
             "--ledger", str(ledger), "--path", str(plain)]
        )
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["reprice"]["api"][
            "repriced_usd"
        ] == pytest.approx(stored)

    def test_cli_default_report_is_unchanged_by_the_new_flag(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--reprice is strictly additive: without it, the existing report and
        its JSON shape (what /good-morning consumes) are untouched."""
        spend.record_spend(_api_usage(), run_type="query-topics", provider="api")
        rc = main(["spend", "--since", "30d", "--json", "--ledger", str(ledger)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert "reprice" not in payload
        for key in ("subscription", "api", "unknown", "record_count"):
            assert key in payload


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


# ---------------------------------------------------------------------------
# durable_ledger_path / resolve_ledger_path(wiki_root=...) — issue athenaeum#980
# AC4: the R3 operational/store-durable relocation seam. NOT wired to any
# production caller in this slice (see athenaeum.store.ARTIFACT_REGISTRY's
# "spend-ledger" entry) — these tests cover the resolver capability itself.
# ---------------------------------------------------------------------------


class TestDurableLedgerPath:
    def test_fresh_store_resolves_to_wiki_root(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        cache_dir = tmp_path / "cache"
        resolved = spend.durable_ledger_path(wiki_root, cache_dir=cache_dir)
        assert resolved == wiki_root / spend.LEDGER_FILENAME

    def test_already_migrated_store_resolves_to_wiki_root(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        (wiki_root / spend.LEDGER_FILENAME).write_text('{"v":1}\n', encoding="utf-8")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / spend.LEDGER_FILENAME).write_text('{"v":1}\n', encoding="utf-8")
        resolved = spend.durable_ledger_path(wiki_root, cache_dir=cache_dir)
        assert resolved == wiki_root / spend.LEDGER_FILENAME

    def test_legacy_store_falls_back_to_cache_dir(self, tmp_path: Path) -> None:
        """An existing installation with ONLY the legacy cache-dir ledger
        keeps resolving there — never silently orphaned by this slice."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        legacy = cache_dir / spend.LEDGER_FILENAME
        legacy.write_text('{"v":1}\n', encoding="utf-8")
        resolved = spend.durable_ledger_path(wiki_root, cache_dir=cache_dir)
        assert resolved == legacy

    def test_resolve_ledger_path_without_wiki_root_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller that does not opt in gets byte-identical resolution to
        before issue athenaeum#980 — no existing caller's behavior changes."""
        monkeypatch.delenv("ATHENAEUM_SPEND_LEDGER", raising=False)
        cache_dir = tmp_path / "cache"
        assert spend.resolve_ledger_path(cache_dir=cache_dir) == spend.default_ledger_path(
            cache_dir
        )

    def test_resolve_ledger_path_with_wiki_root_prefers_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The session/function-scoped isolation fixtures (tests/conftest.py)
        # already pin ATHENAEUM_SPEND_LEDGER for hermeticity; clear it here so
        # THIS test's config-level override is what's actually exercised.
        monkeypatch.delenv("ATHENAEUM_SPEND_LEDGER", raising=False)
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        override = tmp_path / "explicit-override.jsonl"
        config = {"spend": {"ledger_path": str(override)}}
        assert spend.resolve_ledger_path(config, wiki_root=wiki_root) == override

    def test_no_split_brain_on_a_fresh_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The production WRITE path (record_spend/record_spend_per_knob_provider,
        as librarian.py calls them) and the production READ path
        (resolve_ledger_path + read_ledger, as status.py/drain.py/
        backlog_price_sheet.py/etc. call them) must agree on where a fresh
        store's ledger lives — issue athenaeum#980 AC4. This is the assertion
        that makes the cutover safe rather than merely intended: a write with
        wiki_root= that a read without the matching wiki_root= would miss is
        exactly the split-brain hazard flagged during review.
        """
        monkeypatch.delenv("ATHENAEUM_SPEND_LEDGER", raising=False)
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        cache_dir = tmp_path / "cache"

        usage = TokenUsage()
        usage.add(10, 5, 0, 0, model="split-brain-probe-model")
        assert (
            spend.record_spend(
                usage,
                run_type="librarian",
                provider="claude-cli",
                cache_dir=cache_dir,
                wiki_root=wiki_root,
            )
            is True
        )

        # The write must have landed BEHIND THE SEAM, not in the cache dir.
        assert (wiki_root / spend.LEDGER_FILENAME).exists()
        assert not (cache_dir / spend.LEDGER_FILENAME).exists()

        # The production read path, given the SAME wiki_root, must see it.
        read_path = spend.resolve_ledger_path(cache_dir=cache_dir, wiki_root=wiki_root)
        records = spend.read_ledger(read_path)
        assert any(r.get("models") == ["split-brain-probe-model"] for r in records)

        # A read that forgets wiki_root= (the un-migrated-caller shape) must
        # NOT silently see the same records via the old cache-dir default —
        # that would mean the two paths aren't actually the same location,
        # which is a different bug than split-brain but worth pinning too.
        stale_read = spend.read_ledger(spend.resolve_ledger_path(cache_dir=cache_dir))
        assert stale_read == []


# ---------------------------------------------------------------------------
# Issue athenaeum#1147 AC9 — `athenaeum spend` surfaces committed-but-unbilled
# batch spend.
#
# A batch is paid for the moment it is submitted, but `add_batch_tokens` only
# books it at COLLECT. Between a submit run and its collect run there is real
# money the spend ledger cannot see, and an operator should not have to
# hand-trace the reservation ledger to find it.
# ---------------------------------------------------------------------------


class TestSpendReportsOutstandingReservations:
    def _report(self, target: Path, cache_dir: Path, *, as_json: bool) -> str:
        import contextlib
        import io

        args = ["spend", "--path", str(target), "--cache-dir", str(cache_dir)]
        if as_json:
            args.append("--json")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(args)
        assert rc == 0
        return buf.getvalue()

    def _seed(self, tmp_path: Path) -> tuple[Path, Path]:
        target = tmp_path / "knowledge"
        (target / "wiki").mkdir(parents=True)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        return target, cache_dir

    def test_json_reports_outstanding_reservations(self, tmp_path: Path) -> None:
        import json as _json

        target, cache_dir = self._seed(tmp_path)
        spend.record_reservation(
            target / "wiki",
            batch_id="msgbatch_inflight",
            knob="write",
            est_input_tokens=500_000,
            est_output_tokens=50_000,
            est_usd=2.25,
            config=None,
            cache_dir=cache_dir,
        )

        payload = _json.loads(self._report(target, cache_dir, as_json=True))

        assert payload["outstanding_reservations"]["count"] == 1
        assert payload["outstanding_reservations"]["est_usd"] == pytest.approx(2.25)
        assert [
            b["batch_id"] for b in payload["outstanding_reservations"]["batches"]
        ] == ["msgbatch_inflight"]

    def test_text_report_names_the_in_flight_batch(self, tmp_path: Path) -> None:
        target, cache_dir = self._seed(tmp_path)
        spend.record_reservation(
            target / "wiki",
            batch_id="msgbatch_inflight",
            knob="write",
            est_input_tokens=500_000,
            est_output_tokens=50_000,
            est_usd=2.25,
            config=None,
            cache_dir=cache_dir,
        )

        out = self._report(target, cache_dir, as_json=False)

        assert "committed but not yet billed" in out
        assert "msgbatch_inflight" in out
        assert "$2.25" in out

    def test_a_settled_reservation_drops_out_of_the_report(
        self, tmp_path: Path
    ) -> None:
        """A settlement supersedes its reservation; both stay on the ledger."""
        import json as _json

        target, cache_dir = self._seed(tmp_path)
        spend.record_reservation(
            target / "wiki",
            batch_id="msgbatch_done",
            knob="classify",
            est_input_tokens=10,
            est_output_tokens=5,
            est_usd=0.01,
            config=None,
            cache_dir=cache_dir,
        )
        spend.record_settlement(
            target / "wiki",
            batch_id="msgbatch_done",
            knob="classify",
            actual_input_tokens=12,
            actual_output_tokens=4,
            actual_usd=0.011,
            est_usd=0.01,
            config=None,
            cache_dir=cache_dir,
        )

        payload = _json.loads(self._report(target, cache_dir, as_json=True))
        assert payload["outstanding_reservations"]["count"] == 0
        # Both records are still there — the delta is the point of a ledger.
        records = spend.read_reservation_ledger(target / "wiki", cache_dir=cache_dir)
        assert [r["state"] for r in records] == ["reserved", "settled"]
        assert records[1]["delta_usd"] == pytest.approx(0.001)

    def test_a_clean_report_is_unchanged_when_nothing_is_in_flight(
        self, tmp_path: Path
    ) -> None:
        target, cache_dir = self._seed(tmp_path)
        out = self._report(target, cache_dir, as_json=False)
        assert "committed but not yet billed" not in out


# ---------------------------------------------------------------------------
# Ceiling backtest (issue athenaeum#1407) — replay candidate ceilings
# ---------------------------------------------------------------------------


def _sub_row(ts: str, *, billable: int, **overrides: Any) -> dict[str, Any]:
    """A synthetic subscription-path row whose four cache-inclusive counters
    sum to *billable* exactly (all of it as input, for simplicity)."""
    base = {
        "v": spend.LEDGER_VERSION,
        "ts": ts,
        "provider": "claude-cli",
        "billing_mode": "subscription",
        "input_tokens": billable,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": billable,
        "estimated_cost_usd": 0.0,
    }
    base.update(overrides)
    return base


def _api_row(ts: str, *, usd: float, **overrides: Any) -> dict[str, Any]:
    base = {
        "v": spend.LEDGER_VERSION,
        "ts": ts,
        "provider": "anthropic",
        "billing_mode": "api",
        "input_tokens": 100,
        "output_tokens": 0,
        "total_tokens": 100,
        "estimated_cost_usd": usd,
    }
    base.update(overrides)
    return base


class TestCeilingBacktest:
    def test_no_data_on_empty_ledger_is_distinct_from_zero_trips(self, ledger: Path) -> None:
        # AC6 / athenaeum#724 failure mode: "nothing to measure" must never
        # read like "measured it, 0% trips."
        report = spend.ceiling_backtest([], {"max_tokens_per_run": 1000})
        assert report["no_data"] is True
        out = spend.format_ceiling_backtest(report)
        assert "no ledger data" in out.lower()
        assert "0%" not in out
        assert "0.0%" not in out

    def test_no_data_when_no_candidate_supplied(self, ledger: Path) -> None:
        records = [_sub_row("2026-08-01T00:00:00Z", billable=1000)]
        report = spend.ceiling_backtest(records, {})
        assert report["no_data"] is True

    def test_candidate_below_every_observed_value_trips_every_run(self, ledger: Path) -> None:
        records = [
            _sub_row("2026-08-01T01:00:00Z", billable=1_000),
            _sub_row("2026-08-01T02:00:00Z", billable=2_000),
            _sub_row("2026-08-02T01:00:00Z", billable=3_000),
        ]
        report = spend.ceiling_backtest(records, {"max_tokens_per_run": 1})
        knob = report["knobs"]["max_tokens_per_run"]
        assert knob["runs_tripped"] == knob["runs_evaluated"] == 3
        assert knob["run_trip_rate"] == 1.0

    def test_candidate_above_every_observed_value_trips_no_run(self, ledger: Path) -> None:
        records = [
            _sub_row("2026-08-01T01:00:00Z", billable=1_000),
            _sub_row("2026-08-01T02:00:00Z", billable=2_000),
            _sub_row("2026-08-02T01:00:00Z", billable=3_000),
        ]
        report = spend.ceiling_backtest(records, {"max_tokens_per_run": 10_000_000})
        knob = report["knobs"]["max_tokens_per_run"]
        assert knob["runs_tripped"] == 0
        assert knob["run_trip_rate"] == 0.0

    def test_reuses_the_production_predicate_not_a_reimplementation(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: a backtest that agrees with the real gate only by coincidence
        must fail this test. Changing ceiling_tripped's behaviour behind the
        backtest must change the report."""
        records = [_sub_row("2026-08-01T01:00:00Z", billable=1_000)]

        baseline = spend.ceiling_backtest(records, {"max_tokens_per_run": 5_000})
        assert baseline["knobs"]["max_tokens_per_run"]["runs_tripped"] == 0

        def _always_trips(*args: Any, **kwargs: Any) -> str:
            return "forced trip for test"

        monkeypatch.setattr(spend, "ceiling_tripped", _always_trips)
        patched = spend.ceiling_backtest(records, {"max_tokens_per_run": 5_000})
        assert patched["knobs"]["max_tokens_per_run"]["runs_tripped"] == 1

    def test_day_bucketing_agrees_with_production_local_day(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3: two runs straddling a LOCAL-day boundary (but not a UTC one)
        must land in the SAME accounting day under a local-timezone config,
        and in DIFFERENT UTC days under a UTC-forced config -- a fixture that
        doesn't straddle a boundary would pass either way and prove nothing.

        23:30 and 00:30 UTC on consecutive UTC dates are the same New York
        LOCAL calendar day (UTC-4 in August): 19:30 and 20:30 New York time,
        both August 1st.

        The env var takes precedence over the config dict (same resolver
        precedence as everywhere else -- env > yaml), and conftest.py's
        autouse ``_pin_spend_accounting_timezone_utc`` fixture already sets
        ``ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE=UTC`` for every test, so the
        NY case must override it explicitly via monkeypatch, not just via
        the *config* argument.
        """
        records = [
            _sub_row("2026-08-01T23:30:00Z", billable=100),
            _sub_row("2026-08-02T00:30:00Z", billable=100),
        ]
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE", "America/New_York")
        ny_report = spend.ceiling_backtest(
            records,
            {"max_tokens_per_day": 150},
            config={"spend": {"accounting_timezone": "America/New_York"}},
        )
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE", "UTC")
        utc_report = spend.ceiling_backtest(
            records,
            {"max_tokens_per_day": 150},
            config={"spend": {"accounting_timezone": "UTC"}},
        )
        assert ny_report["days_evaluated"] == 1  # same NY calendar day
        assert utc_report["days_evaluated"] == 2  # different UTC calendar days

        # Same day (NY): the second run's day-total (200) crosses 150 -> trips.
        assert ny_report["knobs"]["max_tokens_per_day"]["runs_tripped"] == 1
        # Different days (UTC): neither run's OWN day ever reaches 150 alone.
        assert utc_report["knobs"]["max_tokens_per_day"]["runs_tripped"] == 0

    def test_performs_no_write_ledger_untouched(self, ledger: Path, tmp_path: Path) -> None:
        """AC5: mtime and byte content of the real ledger are unchanged
        across an invocation, and no config file is touched."""
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        before_bytes = ledger.read_bytes()
        before_mtime = ledger.stat().st_mtime_ns

        config_path = tmp_path / "athenaeum.yaml"
        assert not config_path.exists()

        records = spend.read_ledger(ledger)
        report = spend.ceiling_backtest(records, {"max_tokens_per_run": 10})
        assert report["knobs"]["max_tokens_per_run"]["runs_tripped"] == 1

        assert ledger.read_bytes() == before_bytes
        assert ledger.stat().st_mtime_ns == before_mtime
        assert not config_path.exists()

    def test_an_empty_or_absent_ledger_produces_explicit_no_data(self, ledger: Path) -> None:
        # The ledger file itself doesn't even exist yet.
        assert not ledger.exists()
        records = spend.read_ledger(ledger)
        report = spend.ceiling_backtest(records, {"max_tokens_per_run": 10})
        assert report["no_data"] is True

    def test_token_and_usd_knobs_are_isolated_per_billing_path(self, ledger: Path) -> None:
        records = [
            _sub_row("2026-08-01T01:00:00Z", billable=50),
            _api_row("2026-08-01T02:00:00Z", usd=999.0),
        ]
        report = spend.ceiling_backtest(
            records,
            {"max_tokens_per_run": 10, "max_usd_per_run": 1.0},
        )
        # The token candidate never trips against the (huge, but dollar-priced)
        # API run, and the dollar candidate never trips against the (huge,
        # relative to its own tiny cap, but token-priced) subscription run.
        tok = report["knobs"]["max_tokens_per_run"]
        usd = report["knobs"]["max_usd_per_run"]
        assert tok["runs_tripped"] == 1  # only the subscription row qualifies
        assert usd["runs_tripped"] == 1  # only the API row qualifies
        assert tok["metric"]["count"] == 1
        assert usd["metric"]["count"] == 1

    def test_weekly_pct_pair_requires_both_halves(self, ledger: Path) -> None:
        records = [_sub_row("2026-08-01T01:00:00Z", billable=100)]
        # Only one half of the pair supplied -> not a candidate at all,
        # mirroring ceiling_tripped's own opt-in-together contract.
        only_weekly = spend.ceiling_backtest(records, {"weekly_token_limit": 700})
        only_pct = spend.ceiling_backtest(records, {"max_pct_per_day": 50})
        assert only_weekly["no_data"] is True
        assert only_pct["no_data"] is True

        both = spend.ceiling_backtest(records, {"weekly_token_limit": 700, "max_pct_per_day": 10})
        assert spend._WEEKLY_PCT_KNOB in both["knobs"]

    def test_reads_synthetic_fixtures_only_never_the_real_ledger(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC7: this test itself must never touch the operator's real
        ~/.cache/athenaeum/spend.jsonl -- the `ledger` fixture redirects
        ATHENAEUM_SPEND_LEDGER, and ceiling_backtest is given records
        in-memory rather than a path, so there is nothing here that could
        fall back to a default path."""
        real_home_ledger = spend.default_ledger_path()
        assert str(real_home_ledger) != str(ledger)
        records = [_sub_row("2026-08-01T01:00:00Z", billable=100)]
        spend.ceiling_backtest(records, {"max_tokens_per_run": 10})
        # The fixture's isolated ledger was never written to by the backtest.
        assert not ledger.exists() or ledger.read_bytes() == b""

    def test_cli_ceiling_backtest_json_shape(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        rc = main(
            [
                "spend",
                "--since",
                "30d",
                "--json",
                "--ledger",
                str(ledger),
                "--ceiling-backtest",
                "--candidate-max-tokens-per-run",
                "10",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        cb = payload["ceiling_backtest"]
        assert cb["no_data"] is False
        assert cb["knobs"]["max_tokens_per_run"]["runs_tripped"] == 1

    def test_cli_ceiling_backtest_human_output_names_no_ceiling_armed(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        rc = main(
            [
                "spend",
                "--since",
                "30d",
                "--ledger",
                str(ledger),
                "--ceiling-backtest",
                "--candidate-max-tokens-per-run",
                "10",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "nothing armed" in out
        assert "max_tokens_per_run" in out

    def test_cli_ceiling_backtest_does_not_write_the_ledger(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        spend.record_spend(_sub_usage(), run_type="librarian", provider="claude-cli")
        before = ledger.read_bytes()
        rc = main(
            [
                "spend",
                "--since",
                "30d",
                "--ledger",
                str(ledger),
                "--ceiling-backtest",
                "--candidate-max-tokens-per-run",
                "10",
            ]
        )
        assert rc == 0
        capsys.readouterr()
        assert ledger.read_bytes() == before
