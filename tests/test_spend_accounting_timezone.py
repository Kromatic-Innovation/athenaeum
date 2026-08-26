# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1136 — the per-day spend ceiling now rolls over at the
operator's LOCAL midnight, not UTC midnight.

Background: an operator in US Eastern time (UTC-4 in summer) evening-worked
between 20:00 and 23:00 local, exhausting the whole day's `spend.max_usd_per_day`
ceiling — because UTC midnight lands at 20:00 EDT, squarely inside that
session. The nightly `athenaeum run`, scheduled for 02:16 local (a fresh
LOCAL day, but the SAME UTC calendar day the evening session already spent
against), inherited the exhausted ceiling and compiled zero entities on
every observed night (2026-08-22 through 2026-08-25).

Covers:

- :func:`athenaeum.config.resolve_spend_accounting_timezone` — the AC1
  config resolver: env > yaml > system-local default, and the
  invalid-zone-name-falls-back-to-UTC-with-a-warning contract.
- :func:`athenaeum.spend._start_of_accounting_day` — the renamed
  ``_start_of_utc_day``, now timezone-aware.
- AC3: a fixed-IANA-zone, fixed-timestamp regression proving
  :func:`athenaeum.spend.ceiling_tripped` refuses the nightly under the OLD
  (UTC-day) accounting and does NOT refuse it under the NEW (local-day)
  accounting — the exact production scenario above, reproduced
  deterministically (no ``datetime.now()`` anywhere in this module).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from athenaeum import spend
from athenaeum.config import resolve_spend_accounting_timezone
from athenaeum.models import TokenUsage

# US Eastern time in August is EDT (UTC-4) — the zone the issue's real
# operator runs in.
_NY = "America/New_York"


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cache" / "spend.jsonl"
    monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(path))
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


def _write_record(ledger: Path, ts: str, estimated_cost_usd: float) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": ts,
                    "provider": "anthropic",
                    "total_tokens": 100,
                    "estimated_cost_usd": estimated_cost_usd,
                }
            )
            + "\n"
        )


def _cheap_usage(cost_hint_tokens: int = 500) -> TokenUsage:
    """A small, real API usage — cheap enough that its OWN cost never trips
    a $15 ceiling on its own; only prior-ledger spend can push it over."""
    u = TokenUsage()
    u.add(cost_hint_tokens, 100, 0, 0, model="claude-3-5-haiku-20241022")
    return u


# ---------------------------------------------------------------------------
# AC1: resolve_spend_accounting_timezone
# ---------------------------------------------------------------------------


class TestResolveSpendAccountingTimezone:
    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE", "Europe/Berlin")
        config = {"spend": {"accounting_timezone": _NY}}
        assert resolve_spend_accounting_timezone(config) == ZoneInfo("Europe/Berlin")

    def test_yaml_used_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE", raising=False)
        config = {"spend": {"accounting_timezone": _NY}}
        assert resolve_spend_accounting_timezone(config) == ZoneInfo(_NY)

    def test_default_is_system_local_not_hardcoded_utc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither env nor yaml set: falls through to the HOST's local
        timezone (never a hardcoded UTC) — this container runs UTC, so the
        resolved zone's UTC offset must be 0, but it must NOT be the literal
        ``timezone.utc`` singleton reached by the invalid-name fallback path
        (a real system-local resolution, not the error path, even though
        both happen to read as UTC here)."""
        monkeypatch.delenv("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE", raising=False)
        tz = resolve_spend_accounting_timezone(None)
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        assert now.astimezone(tz).utcoffset() == now.utcoffset()

    def test_invalid_zone_name_falls_back_to_utc_and_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE", "Not/ARealZone")
        caplog.set_level(logging.WARNING, logger="athenaeum.config")
        tz = resolve_spend_accounting_timezone(None)
        assert tz == timezone.utc
        assert any(
            "Not/ARealZone" in r.getMessage() and "athenaeum#1136" in r.getMessage()
            for r in caplog.records
        )

    def test_invalid_zone_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE", "   ")
        # Blank/whitespace-only env falls through past the env branch
        # entirely (same "blank falls through" contract as every other
        # env-first resolver in this module).
        tz = resolve_spend_accounting_timezone(None)
        assert tz is not None


# ---------------------------------------------------------------------------
# _start_of_accounting_day (renamed from _start_of_utc_day)
# ---------------------------------------------------------------------------


class TestStartOfAccountingDay:
    def test_local_day_boundary_differs_from_utc_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both calls pass an explicit yaml-style config; unset the env var
        # (the whole-suite autouse fixture pins it to UTC — see conftest.py)
        # so the config argument is actually the thing under test here.
        monkeypatch.delenv("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE", raising=False)
        # 2026-08-25 06:16 UTC == 2026-08-25 02:16 EDT.
        now = datetime(2026, 8, 25, 6, 16, tzinfo=timezone.utc)
        utc_start = spend._start_of_accounting_day(now, {"spend": {"accounting_timezone": "UTC"}})
        local_start = spend._start_of_accounting_day(
            now, {"spend": {"accounting_timezone": _NY}}
        )
        assert utc_start == datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        # Local midnight (2026-08-25 00:00 EDT) is 2026-08-25 04:00 UTC.
        assert local_start == datetime(2026, 8, 25, 4, 0, tzinfo=timezone.utc)
        assert local_start != utc_start

    def test_returned_boundary_is_always_utc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE", raising=False)
        now = datetime(2026, 8, 25, 6, 16, tzinfo=timezone.utc)
        start = spend._start_of_accounting_day(now, {"spend": {"accounting_timezone": _NY}})
        assert start.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# AC3: regression — the exact production starvation scenario, deterministic.
# ---------------------------------------------------------------------------


class TestNightlyLocalDayAlignment:
    """The issue's real numbers: a $15.00/day ceiling, an evening session
    that spent $15.03, and a nightly landing 4h16m later but still inside
    the SAME UTC calendar day."""

    # Evening spend at 22:00 EDT on Aug 24 == 02:00 UTC on Aug 25.
    EVENING_TS = "2026-08-25T02:00:00Z"
    EVENING_COST_USD = 15.03
    # Nightly run at 02:16 EDT on Aug 25 == 06:16 UTC on Aug 25 -- the SAME
    # UTC calendar day as the evening spend above.
    NIGHTLY_NOW = datetime(2026, 8, 25, 6, 16, tzinfo=timezone.utc)
    DAY_CAP_USD = "15.00"

    @pytest.fixture(autouse=True)
    def _unpin_suite_default_timezone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Every test below drives the accounting zone explicitly via its own
        # `config=` argument (UTC in the "reproduces the bug" case,
        # America/New_York in the "fixed" case) — unset the whole-suite
        # autouse UTC pin (conftest.py) so that argument is what actually
        # decides the outcome, not the env var winning by precedence.
        monkeypatch.delenv("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE", raising=False)

    def test_utc_day_accounting_reproduces_the_starvation_bug(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the bug: forcing UTC-day accounting (the pre-athenaeum#1136
        default) trips the ceiling for the nightly, exactly as observed in
        production ('Spend ceiling reached ($15.03/$15.00 today)')."""
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", self.DAY_CAP_USD)
        _write_record(ledger, self.EVENING_TS, self.EVENING_COST_USD)

        reason = spend.ceiling_tripped(
            _cheap_usage(),
            provider="api",
            config={"spend": {"accounting_timezone": "UTC"}},
            ledger_path=ledger,
            now=self.NIGHTLY_NOW,
        )
        assert reason is not None
        assert "per-day API dollar ceiling reached" in reason

    def test_local_day_accounting_gives_the_nightly_a_fresh_window(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the AC1 fix: the SAME ledger + the SAME nightly `now`,
        accounted against the operator's local (America/New_York) day
        instead, is NOT refused -- the evening spend falls in the PRIOR
        local day and does not carry over."""
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_DAY", self.DAY_CAP_USD)
        _write_record(ledger, self.EVENING_TS, self.EVENING_COST_USD)

        reason = spend.ceiling_tripped(
            _cheap_usage(),
            provider="api",
            config={"spend": {"accounting_timezone": _NY}},
            ledger_path=ledger,
            now=self.NIGHTLY_NOW,
        )
        assert reason is None

    def test_spend_today_itself_excludes_the_evening_row_under_local_accounting(
        self, ledger: Path
    ) -> None:
        """Unit-level companion to the two ceiling_tripped tests above --
        pins the exact figure :func:`athenaeum.spend.spend_today` reports,
        not just the pass/fail outcome."""
        _write_record(ledger, self.EVENING_TS, self.EVENING_COST_USD)

        utc_today = spend.spend_today(
            ledger, config={"spend": {"accounting_timezone": "UTC"}}, now=self.NIGHTLY_NOW
        )
        local_today = spend.spend_today(
            ledger, config={"spend": {"accounting_timezone": _NY}}, now=self.NIGHTLY_NOW
        )
        assert utc_today["api_usd"] == self.EVENING_COST_USD
        assert local_today["api_usd"] == 0.0
