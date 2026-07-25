"""Tests for the backlog-drain ETA advisor + `athenaeum drain` (issue #470).

Covers the pure estimators (rate from ledger, fallback path, tokens/file, the
price table with the #236 batch discount), the advisor threshold logic, the
drain pre-flight guards (missing API key, batch+finite-deadline, missing --yes),
the cumulative-cost drain loop (empty backlog / drains-to-empty / ceiling stop /
zero-progress stop), the ledger `files_processed` roundtrip, the
`librarian.drain_warn_days` resolver, and the `athenaeum status` surfacing.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum import drain, spend
from athenaeum.cli import main
from athenaeum.config import resolve_drain_warn_days
from athenaeum.models import TokenUsage

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_environ() -> None:
    """Snapshot/restore os.environ — run_drain mutates it directly (per-window
    ceiling + forced provider), which would otherwise leak across tests."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _ledger_record(
    *,
    files_processed: int | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    provider: str = "anthropic",
    run_type: str = "librarian",
    estimated_cost_usd: float = 0.0,
    ts: datetime | None = None,
) -> dict:
    rec = {
        "v": 1,
        "ts": (ts or datetime(2026, 7, 1, tzinfo=timezone.utc))
        .isoformat()
        .replace("+00:00", "Z"),
        "run_type": run_type,
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }
    if files_processed is not None:
        rec["files_processed"] = files_processed
    return rec


def _write_ledger(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Estimators — rate from ledger / fallback / none
# ---------------------------------------------------------------------------


class TestEstimateFilesPerNight:
    def test_rate_from_ledger(self) -> None:
        records = [
            _ledger_record(files_processed=10),
            _ledger_record(files_processed=20),
            _ledger_record(files_processed=30),
        ]
        rate, source = drain.estimate_files_per_night(records)
        assert source == "ledger"
        assert rate == 20.0  # (10 + 20 + 30) / 3

    def test_ignores_non_librarian_and_missing_files(self) -> None:
        records = [
            _ledger_record(files_processed=10),
            _ledger_record(files_processed=99, run_type="answers"),  # wrong type
            _ledger_record(input_tokens=5),  # no files_processed (pre-#470)
            _ledger_record(files_processed=0),  # zero — not usable
        ]
        rate, source = drain.estimate_files_per_night(records)
        assert source == "ledger"
        assert rate == 10.0

    def test_fallback_to_this_run(self) -> None:
        rate, source = drain.estimate_files_per_night([], this_run_files=7)
        assert source == "this-run"
        assert rate == 7.0

    def test_none_when_no_history_and_no_this_run(self) -> None:
        rate, source = drain.estimate_files_per_night([], this_run_files=0)
        assert source == "none"
        assert rate == 0.0

    def test_respects_max_history_window(self) -> None:
        records = [_ledger_record(files_processed=100)] + [
            _ledger_record(files_processed=10) for _ in range(3)
        ]
        rate, source = drain.estimate_files_per_night(records, max_history=3)
        assert source == "ledger"
        assert rate == 10.0  # oldest (100) excluded by the window


class TestEstimateEtaNights:
    def test_basic(self) -> None:
        assert drain.estimate_eta_nights(202, 11) == 19  # ceil(202/11)

    def test_empty_backlog_is_zero(self) -> None:
        assert drain.estimate_eta_nights(0, 11) == 0

    def test_unknown_rate_is_inf(self) -> None:
        assert drain.estimate_eta_nights(50, 0) == math.inf


class TestObservedTokensPerFile:
    def test_averages_over_history(self) -> None:
        records = [
            _ledger_record(files_processed=2, input_tokens=100, output_tokens=10),
            _ledger_record(files_processed=3, input_tokens=200, output_tokens=20),
        ]
        avg_in, avg_out = drain.observed_tokens_per_file(records)
        assert avg_in == 60.0  # 300 tokens / 5 files
        assert avg_out == 6.0  # 30 tokens / 5 files

    def test_none_when_no_history(self) -> None:
        assert drain.observed_tokens_per_file([]) is None


class TestEstimateDrainCostUsd:
    def test_price_table_with_batch_discount(self) -> None:
        # sonnet-4 rates: $3/MTok in, $15/MTok out.
        full = drain.estimate_drain_cost_usd(
            backlog=1,
            avg_input_per_file=1_000_000,
            avg_output_per_file=1_000_000,
            model="claude-sonnet-4",
            batch=False,
        )
        assert full == pytest.approx(18.0)  # 3 + 15
        batched = drain.estimate_drain_cost_usd(
            backlog=1,
            avg_input_per_file=1_000_000,
            avg_output_per_file=1_000_000,
            model="claude-sonnet-4",
            batch=True,
        )
        assert batched == pytest.approx(9.0)  # half of 18

    def test_unknown_model_uses_blended_rate(self) -> None:
        cost = drain.estimate_drain_cost_usd(
            backlog=1,
            avg_input_per_file=1_000_000,
            avg_output_per_file=1_000_000,
            model="some-proxy-model",
            batch=False,
        )
        assert cost == pytest.approx(9.0)  # blended 1.5 in + 7.5 out

    def test_scales_with_backlog(self) -> None:
        cost = drain.estimate_drain_cost_usd(
            backlog=100,
            avg_input_per_file=1000,
            avg_output_per_file=100,
            model="claude-haiku-4",  # $1/MTok in, $5/MTok out
            batch=True,
        )
        # per file = (1000*1 + 100*5)/1e6 = 0.0015 ; *100 = 0.15 ; *0.5 = 0.075
        assert cost == pytest.approx(0.075)


class TestRoundUpBudget:
    @pytest.mark.parametrize(
        "value,expected",
        [(0.3, 0.5), (0.6, 1.0), (1.2, 2.0), (3.0, 5.0), (7.0, 10.0), (12.0, 20.0)],
    )
    def test_nice_rounding(self, value: float, expected: float) -> None:
        assert drain._round_up_budget(value) == expected

    def test_zero(self) -> None:
        assert drain._round_up_budget(0) == 0.0


# ---------------------------------------------------------------------------
# Advisor — threshold logic + command
# ---------------------------------------------------------------------------


class TestBuildAdvisory:
    def test_empty_backlog_is_silent(self) -> None:
        assert (
            drain.build_advisory(backlog=0, ledger_records=[], warn_days=3) is None
        )

    def test_below_threshold_is_silent(self) -> None:
        records = [_ledger_record(files_processed=50)]  # 50/night
        # backlog 100 -> 2 nights, threshold 3 -> silent.
        assert (
            drain.build_advisory(backlog=100, ledger_records=records, warn_days=3)
            is None
        )

    def test_above_threshold_warns_with_command(self) -> None:
        records = [
            _ledger_record(files_processed=10, input_tokens=20_000, output_tokens=1_500)
        ]
        adv = drain.build_advisory(backlog=200, ledger_records=records, warn_days=3)
        assert adv is not None
        assert adv.eta_nights == 20  # ceil(200/10)
        assert adv.rate_source == "ledger"
        assert adv.line.startswith(drain.DRAIN_ADVISOR_PREFIX + ":")
        assert "athenaeum drain --max-usd" in adv.command
        assert "200 deferred file(s)" in adv.summary
        assert adv.suggested_max_usd > 0

    def test_unknown_rate_still_warns(self) -> None:
        adv = drain.build_advisory(
            backlog=42, ledger_records=[], warn_days=3, this_run_files=0
        )
        assert adv is not None
        assert adv.eta_nights == math.inf
        assert adv.rate_source == "none"
        assert "unknown number of nights" in adv.summary


# ---------------------------------------------------------------------------
# Drain pre-flight guards
# ---------------------------------------------------------------------------


class TestGuards:
    def test_check_api_key_missing(self) -> None:
        assert drain.check_api_key(env={}) is not None
        assert "ANTHROPIC_API_KEY" in drain.check_api_key(env={})

    def test_check_api_key_present(self) -> None:
        assert drain.check_api_key(env={"ANTHROPIC_API_KEY": "sk-x"}) is None

    def test_check_batch_deadline_finite_refused(self) -> None:
        err = drain.check_batch_deadline(max_runtime=300)
        assert err is not None and "cwc#615" in err

    def test_check_batch_deadline_unbounded_ok(self) -> None:
        assert drain.check_batch_deadline(max_runtime=0) is None
        assert drain.check_batch_deadline(max_runtime=-1) is None

    def test_resolve_drain_runtime_default_unbounded(self) -> None:
        assert drain.resolve_drain_runtime(None, env={}) == 0

    def test_resolve_drain_runtime_env_wins(self) -> None:
        assert (
            drain.resolve_drain_runtime(
                {"librarian": {"max_runtime": 999}}, env={"ATHENAEUM_MAX_RUNTIME": "300"}
            )
            == 300
        )

    def test_resolve_drain_runtime_yaml(self) -> None:
        assert (
            drain.resolve_drain_runtime({"librarian": {"max_runtime": 120}}, env={})
            == 120
        )


# ---------------------------------------------------------------------------
# drain_spend_usd + run_drain loop (injected fakes)
# ---------------------------------------------------------------------------


class TestDrainSpendUsd:
    def test_sums_api_dollars_since(self, tmp_path: Path) -> None:
        ledger = tmp_path / "spend.jsonl"
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        _write_ledger(
            ledger,
            [
                _ledger_record(
                    estimated_cost_usd=2.0, ts=start - timedelta(hours=1)
                ),  # before drain start — excluded
                _ledger_record(
                    estimated_cost_usd=3.0, ts=start + timedelta(minutes=5)
                ),
                _ledger_record(
                    estimated_cost_usd=99.0,
                    provider="claude-cli",
                    ts=start + timedelta(minutes=6),
                ),  # subscription — $0
                _ledger_record(
                    estimated_cost_usd=1.5, ts=start + timedelta(minutes=7)
                ),
            ],
        )
        assert drain.drain_spend_usd(ledger, since=start) == pytest.approx(4.5)


class TestRunDrainLoop:
    def _harness(self, tmp_path: Path, backlog_start: int, per_window):
        """Return (kwargs, calls) for run_drain with a scripted fake run_fn.

        per_window: list of (files_drained, cost_usd) applied per window.
        """
        ledger = tmp_path / "spend.jsonl"
        raw_root = tmp_path / "raw"
        backlog = [backlog_start]
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        calls = {"n": 0}

        def fake_run(**_kwargs):
            i = calls["n"]
            calls["n"] += 1
            drained, cost = per_window[i] if i < len(per_window) else (0, 0.0)
            backlog[0] = max(0, backlog[0] - drained)
            if cost:
                with open(ledger, "a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            _ledger_record(
                                estimated_cost_usd=cost,
                                ts=start + timedelta(minutes=i + 1),
                            )
                        )
                        + "\n"
                    )
            return 0

        kwargs = dict(
            knowledge_root=tmp_path,
            raw_root=raw_root,
            wiki_root=tmp_path / "wiki",
            max_files=50,
            ledger_path=ledger,
            run_fn=fake_run,
            backlog_fn=lambda _root: backlog[0],
            now=start,
        )
        return kwargs, calls, backlog

    def test_empty_backlog_returns_immediately(self, tmp_path: Path) -> None:
        kwargs, calls, _ = self._harness(tmp_path, 0, [])
        assert drain.run_drain(max_usd=10.0, **kwargs) == 0
        assert calls["n"] == 0  # never ran a window

    def test_drains_to_empty(self, tmp_path: Path) -> None:
        kwargs, calls, backlog = self._harness(
            tmp_path, 100, [(50, 0.0), (50, 0.0)]
        )
        assert drain.run_drain(max_usd=10.0, **kwargs) == 0
        assert calls["n"] == 2
        assert backlog[0] == 0

    def test_cumulative_ceiling_stops(self, tmp_path: Path) -> None:
        # Each window drains 10 files at $6; ceiling $10 trips after window 2.
        kwargs, calls, backlog = self._harness(
            tmp_path, 100, [(10, 6.0), (10, 6.0), (10, 6.0)]
        )
        assert drain.run_drain(max_usd=10.0, **kwargs) == 0
        assert calls["n"] == 2  # stopped once cumulative $12 >= $10
        assert backlog[0] == 80  # not fully drained

    def test_zero_progress_stops_loudly(self, tmp_path: Path) -> None:
        kwargs, calls, backlog = self._harness(tmp_path, 100, [(0, 0.0)])
        assert drain.run_drain(max_usd=10.0, **kwargs) == 1  # loud nonzero
        assert calls["n"] == 1

    def test_forces_provider_api(self, tmp_path: Path) -> None:
        os.environ["ATHENAEUM_LLM_PROVIDER"] = "claude-cli"
        kwargs, _calls, _ = self._harness(tmp_path, 50, [(50, 0.0)])
        drain.run_drain(max_usd=10.0, **kwargs)
        assert os.environ["ATHENAEUM_LLM_PROVIDER"] == "api"


# ---------------------------------------------------------------------------
# Config resolver
# ---------------------------------------------------------------------------


class TestResolveDrainWarnDays:
    def test_default(self) -> None:
        assert resolve_drain_warn_days(None) == 3
        assert resolve_drain_warn_days({}) == 3
        assert resolve_drain_warn_days({"librarian": {}}) == 3

    def test_yaml_value(self) -> None:
        assert resolve_drain_warn_days({"librarian": {"drain_warn_days": 7}}) == 7

    def test_rejects_bool_and_nonpositive(self) -> None:
        assert resolve_drain_warn_days({"librarian": {"drain_warn_days": True}}) == 3
        assert resolve_drain_warn_days({"librarian": {"drain_warn_days": 0}}) == 3
        assert resolve_drain_warn_days({"librarian": {"drain_warn_days": -1}}) == 3


class TestTemplateAdvertisesDrainKey:
    def test_template_documents_drain_warn_days(self) -> None:
        from athenaeum.config import _DEFAULT_CONFIG_CONTENT

        assert "#   drain_warn_days: 3" in _DEFAULT_CONFIG_CONTENT


# ---------------------------------------------------------------------------
# Ledger files_processed roundtrip (throughput plumbing)
# ---------------------------------------------------------------------------


class TestLedgerFilesProcessed:
    def test_build_record_includes_files_processed(self) -> None:
        u = TokenUsage()
        u.add(100, 50, model="claude-sonnet-4")
        rec = spend.build_record(
            u, run_type="librarian", provider="api", files_processed=7
        )
        assert rec["files_processed"] == 7

    def test_build_record_omits_when_not_given(self) -> None:
        u = TokenUsage()
        u.add(100, 50, model="claude-sonnet-4")
        rec = spend.build_record(u, run_type="answers", provider="api")
        assert "files_processed" not in rec

    def test_record_spend_roundtrip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger = tmp_path / "spend.jsonl"
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(ledger))
        monkeypatch.delenv("ATHENAEUM_SPEND_LEDGER_ENABLED", raising=False)
        u = TokenUsage()
        u.add(1000, 200, model="claude-sonnet-4")
        assert spend.record_spend(
            u, run_type="librarian", provider="api", files_processed=12
        )
        records = spend.read_ledger(ledger)
        assert records[-1]["files_processed"] == 12


# ---------------------------------------------------------------------------
# CLI — pre-flight refusals + empty backlog (never runs a real drain)
# ---------------------------------------------------------------------------


def _seed_knowledge(tmp_path: Path, *, raw_files: int) -> Path:
    kr = tmp_path / "knowledge"
    (kr / "wiki").mkdir(parents=True)
    src = kr / "raw" / "test"
    src.mkdir(parents=True)
    for i in range(raw_files):
        (src / f"2026010{i % 9 + 1}T000000Z-abcd12{i:02d}.md").write_text(
            "raw note", encoding="utf-8"
        )
    return kr


class TestDrainCli:
    def test_missing_api_key_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        kr = _seed_knowledge(tmp_path, raw_files=2)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        rc = main(["drain", "--max-usd", "5", "--path", str(kr)])
        assert rc == 1
        assert "ANTHROPIC_API_KEY" in capsys.readouterr().err

    def test_batch_finite_deadline_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        kr = _seed_knowledge(tmp_path, raw_files=2)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.setenv("ATHENAEUM_MAX_RUNTIME", "300")
        rc = main(["drain", "--max-usd", "5", "--path", str(kr)])
        assert rc == 1
        assert "cwc#615" in capsys.readouterr().err

    def test_missing_yes_non_interactive_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        kr = _seed_knowledge(tmp_path, raw_files=2)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.delenv("ATHENAEUM_MAX_RUNTIME", raising=False)
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(tmp_path / "spend.jsonl"))
        # pytest's captured stdin is not a TTY, so no --yes must refuse.
        rc = main(["drain", "--max-usd", "5", "--path", str(kr)])
        assert rc == 1
        assert "--yes" in capsys.readouterr().err

    def test_empty_backlog_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        kr = _seed_knowledge(tmp_path, raw_files=0)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.delenv("ATHENAEUM_MAX_RUNTIME", raising=False)
        rc = main(["drain", "--max-usd", "5", "--yes", "--path", str(kr)])
        assert rc == 0
        assert "empty" in capsys.readouterr().out.lower()

    def test_requires_max_usd(self, tmp_path: Path) -> None:
        kr = _seed_knowledge(tmp_path, raw_files=1)
        with pytest.raises(SystemExit) as exc:
            main(["drain", "--path", str(kr)])
        assert exc.value.code == 2  # argparse: required flag missing

    def test_success_path_acquires_lock_and_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        kr = _seed_knowledge(tmp_path, raw_files=3)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.delenv("ATHENAEUM_MAX_RUNTIME", raising=False)
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(tmp_path / "spend.jsonl"))
        called = {}

        def fake_run_drain(**kwargs):
            called.update(kwargs)
            return 0

        monkeypatch.setattr(drain, "run_drain", fake_run_drain)
        rc = main(["drain", "--max-usd", "50", "--yes", "--path", str(kr)])
        assert rc == 0
        assert called["max_usd"] == 50.0
        out = capsys.readouterr().out
        assert "estimated cost" in out

    def test_estimate_exceeds_ceiling_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        kr = _seed_knowledge(tmp_path, raw_files=50)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.delenv("ATHENAEUM_MAX_RUNTIME", raising=False)
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(tmp_path / "spend.jsonl"))
        monkeypatch.setattr(drain, "run_drain", lambda **kwargs: 0)
        # A tiny ceiling below the coarse-default estimate for 50 files.
        rc = main(["drain", "--max-usd", "0.001", "--yes", "--path", str(kr)])
        assert rc == 0
        assert "exceeds the ceiling" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# status surfacing
# ---------------------------------------------------------------------------


class TestStatusSurfacing:
    def test_status_surfaces_advisory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.status import format_status, status

        kr = _seed_knowledge(tmp_path, raw_files=100)
        ledger = tmp_path / "spend.jsonl"
        _write_ledger(ledger, [_ledger_record(files_processed=5)])  # slow: 5/night
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(ledger))
        info = status(kr)
        assert info["drain_advisory"] is not None
        assert "athenaeum drain" in info["drain_advisory"]
        assert "Backlog drain:" in format_status(info)

    def test_status_silent_below_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.status import format_status, status

        kr = _seed_knowledge(tmp_path, raw_files=2)
        ledger = tmp_path / "spend.jsonl"
        _write_ledger(ledger, [_ledger_record(files_processed=50)])  # fast
        monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(ledger))
        info = status(kr)
        assert info["drain_advisory"] is None
        assert "Backlog drain:" not in format_status(info)
