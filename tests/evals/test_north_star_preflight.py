# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1959: the default-on behavioural preflight -- one
``push_breadcrumb_pull``/``core`` group, run serially before the first grid
worker starts, that refuses (exit 2, zero grid cells run) when that cell
recorded no ``recall`` tool call, or when the claude-cli transport's own
guards fire.

Four groups of coverage, matching the issue's own AC list:

* flags/dry-run arithmetic -- no client, no subprocess, mirrors
  ``test_north_star_cli.py``'s own dry-run tests.
* the claude-cli + ``--cli-tool-passthrough`` end-to-end cases (a)-(d),
  driving REAL fake ``claude`` binaries under
  ``tests/fixtures/cli_tool_bridge/`` through the real
  :class:`athenaeum.provider.ClaudeCliClient` -- the only way to prove the
  real bridge guards (``CliToolBridgeError``) actually propagate through
  ``main`` unmodified, which a monkeypatched ``run_probe_all_arms`` could
  not show.
* the same pass/fail pair on the ``anthropic``/``--mode api`` path with a
  stub Messages-API client (no network, no ``claude`` binary) -- proves
  the behavioural check itself is provider-independent.
* the sibling-store isolation AC, with a monkeypatched ``run_probe_all_arms``
  (no client needed at all for this one -- it is a resume/sidecar proof).

No test here makes a model call, needs a key, or needs a logged-in `claude`
CLI -- CI spend is zero. UNMARKED (no ``pytest.mark.eval``/``.rollout``/
``.embedding``) so it runs in ci.yml's default job -- see
``test_containment_ci_wiring.py`` for why that convention matters.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from athenaeum.models import TokenUsage
from tests.evals import north_star_cli
from tests.evals.containment import NORTH_STAR_CELL_TOKEN_ESTIMATE, ResultStore, read_planned_cells
from tests.evals.rollout import (
    ALL_ARMS,
    DEFAULT_ROLLOUT_MODEL,
    RECALL_TOOL_NAME,
    RolloutRecord,
    ToolCall,
    TurnTokenUsage,
)
from tests.evals.test_rollout_api_mode import _text_block, _tool_use_block, _usage

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "cli_tool_bridge"


def _install_fake_claude(tmp_path: Path, filename: str) -> Path:
    body = (FIXTURE_DIR / filename).read_text()
    _shebang, _nl, rest = body.partition("\n")
    script = tmp_path / filename
    script.write_text(f"#!{sys.executable}\n{rest}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ---------------------------------------------------------------------------
# Flags + dry-run arithmetic -- no client, no subprocess
# ---------------------------------------------------------------------------


def test_preflight_flags_default_on_with_no_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATHENAEUM_PREFLIGHT_PROBE", raising=False)
    monkeypatch.delenv("ATHENAEUM_NO_PREFLIGHT", raising=False)
    args = north_star_cli.build_arg_parser().parse_args([])
    assert args.no_preflight is False
    assert args.preflight_probe is None


def test_dry_run_prints_preflight_projection_and_makes_zero_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _exploding_run_probe_all_arms(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run must never run a cell, preflight included")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(["--dry-run"])

    assert exit_code == 0
    assert not (tmp_path / "r.jsonl").exists()
    out = capsys.readouterr().out
    assert f"preflight: {len(ALL_ARMS)} cells would run" in out
    assert "zero calls made" in out


def test_no_preflight_flag_skips_the_projection_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _exploding_run_probe_all_arms(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run must never run a cell")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(["--dry-run", "--no-preflight"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "preflight: skipped (--no-preflight)" in out
    assert "cells would run" not in out


def _price_for_cells(n_cells: int, model: str) -> float:
    usage = TokenUsage()
    for _ in range(n_cells):
        usage.add_tokens(
            NORTH_STAR_CELL_TOKEN_ESTIMATE.input_tokens,
            NORTH_STAR_CELL_TOKEN_ESTIMATE.output_tokens,
            model=model,
        )
    return usage.estimated_cost_usd


def test_token_ceiling_check_counts_the_preflights_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue athenaeum#1959 AC: "the preflight's cells count toward ... the
    token ceiling." A ceiling sized to clear the smoke-scale READ GRID alone
    (``len(ALL_ARMS)`` cells) but not the grid PLUS the preflight's own
    full group (another ``len(ALL_ARMS)`` cells) must pass with
    ``--no-preflight`` and refuse without it -- proving the ceiling check
    (``projected_tokens > token_ceiling``) actually sums the two, not just
    the read grid."""

    def _exploding_run_probe_all_arms(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run must never run a cell")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    cell_tokens = NORTH_STAR_CELL_TOKEN_ESTIMATE.total_tokens
    grid_tokens = len(ALL_ARMS) * cell_tokens
    combined_tokens = 2 * len(ALL_ARMS) * cell_tokens
    max_tokens = (grid_tokens + combined_tokens) // 2
    assert grid_tokens <= max_tokens < combined_tokens  # the threshold actually separates them

    grid_only = north_star_cli.main(
        ["--dry-run", "--no-preflight", "--max-tokens", str(max_tokens)]
    )
    assert grid_only == 0

    with_preflight = north_star_cli.main(["--dry-run", "--max-tokens", str(max_tokens)])
    assert with_preflight == 1
    err = capsys.readouterr().err
    assert f"projected tokens {combined_tokens} exceed the token ceiling {max_tokens}" in err


def test_max_spend_pricing_counts_the_preflights_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue athenaeum#1959 AC: "the preflight's cells count toward ... the
    --max-spend pricing." Same shape as the token-ceiling test above, but
    against the SEPARATE dollar gate (``combined_estimated_usd >
    max_spend``, checked before the token-ceiling gate) -- a budget that
    clears the read grid's own price alone but not the grid plus the
    preflight's price must pass with ``--no-preflight`` and refuse without
    it."""

    def _exploding_run_probe_all_arms(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run must never run a cell")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    grid_price = _price_for_cells(len(ALL_ARMS), DEFAULT_ROLLOUT_MODEL)
    combined_price = _price_for_cells(2 * len(ALL_ARMS), DEFAULT_ROLLOUT_MODEL)
    max_spend = (grid_price + combined_price) / 2
    assert grid_price <= max_spend < combined_price  # the threshold actually separates them

    grid_only = north_star_cli.main(
        ["--dry-run", "--no-preflight", "--max-spend", str(max_spend)]
    )
    assert grid_only == 0

    with_preflight = north_star_cli.main(["--dry-run", "--max-spend", str(max_spend)])
    assert with_preflight == 1
    err = capsys.readouterr().err
    assert "exceeding --max-spend" in err
    assert "plus preflight" in err


# ---------------------------------------------------------------------------
# claude-cli + --cli-tool-passthrough end to end, real fake `claude` binaries
# ---------------------------------------------------------------------------


def _claude_cli_args(tmp_path: Path, fake_claude: Path, *, workers: int = 1) -> list[str]:
    return [
        "--mode", "api",
        "--cli-tool-passthrough",
        "--search-backend", "keyword",
        "--scale", "smoke",
        "--workers", str(workers),
        "--store", str(tmp_path / "results.jsonl"),
        "--materialize-root", str(tmp_path / "mat"),
        "--out-dir", str(tmp_path / "measurements"),
    ]


def test_case_a_auto_recall_fake_passes_preflight_and_grid_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_claude = _install_fake_claude(tmp_path, "fake_claude_auto_recall.py")
    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
    monkeypatch.setenv("ATHENAEUM_CLAUDE_CLI_BIN", str(fake_claude))

    exit_code = north_star_cli.main(_claude_cli_args(tmp_path, fake_claude))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "preflight: ok provider=claude-cli" in out
    assert "arm=push_breadcrumb_pull recall_called=true" in out
    assert "apiKeySource=none" in out

    store_path = tmp_path / "results.jsonl"
    grid_lines = store_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(grid_lines) == len(ALL_ARMS)  # the real grid group ran, not skipped

    preflight_store_path = tmp_path / "results.jsonl.preflight.jsonl"
    preflight_lines = preflight_store_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(preflight_lines) == len(ALL_ARMS)


def test_case_b_no_tool_call_fake_exits_2_before_any_grid_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_claude = _install_fake_claude(tmp_path, "fake_claude_no_tool_call.py")
    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
    monkeypatch.setenv("ATHENAEUM_CLAUDE_CLI_BIN", str(fake_claude))

    exit_code = north_star_cli.main(_claude_cli_args(tmp_path, fake_claude))

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "push_breadcrumb_pull" in err
    assert "provider=claude-cli" in err
    assert "tool_calls=0" in err
    assert not (tmp_path / "results.jsonl").exists()  # no grid row written


def test_case_c_bad_api_key_source_fake_exits_2_before_any_grid_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_claude = _install_fake_claude(tmp_path, "fake_claude_bad_api_key_source.py")
    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
    monkeypatch.setenv("ATHENAEUM_CLAUDE_CLI_BIN", str(fake_claude))

    exit_code = north_star_cli.main(_claude_cli_args(tmp_path, fake_claude))

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "apiKeySource" in err
    assert "api_key" in err
    assert not (tmp_path / "results.jsonl").exists()


def test_case_d_bad_api_key_source_fake_with_workers_4_still_exits_2_zero_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue athenaeum#1959 Motivation note: ``_run_cells`` does not cancel
    queued groups on an ordinary per-group exception, only on an interrupt
    or a ceiling trip -- so a higher ``--workers`` must not let any grid
    group start before the (strictly serial) preflight has had its say."""
    fake_claude = _install_fake_claude(tmp_path, "fake_claude_bad_api_key_source.py")
    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
    monkeypatch.setenv("ATHENAEUM_CLAUDE_CLI_BIN", str(fake_claude))

    exit_code = north_star_cli.main(_claude_cli_args(tmp_path, fake_claude, workers=4))

    assert exit_code == 2
    assert not (tmp_path / "results.jsonl").exists()


# ---------------------------------------------------------------------------
# Provider-independence: the same pass/fail pair on --mode api / anthropic,
# with a stub Messages-API client -- no network, no claude binary.
# ---------------------------------------------------------------------------


class _RepeatingToolStubClient:
    """``client.messages.create(**params)``: on a tool-loop's first turn
    (``tools`` given, no ``tool_result`` yet in the message history) returns
    ONE tool_use call for the first tool offered when *call_tool* is True
    (plain final text when False); every later turn, and every single-shot
    call (no ``tools`` at all), returns a final text answer.

    Deliberately NOT ``tests.evals.test_rollout_api_mode._QueuedApiClient``:
    that stub pops one scripted turn per call and raises once exhausted,
    which would force this test to hand-count exactly how many
    ``.messages.create()`` calls ``run_probe_all_arms`` makes across all
    eight arms (and stay in sync with that count forever after). This
    stub answers from the CURRENT call's own shape instead, so it never
    exhausts and is robust to the arm count changing.
    """

    def __init__(self, *, call_tool: bool) -> None:
        self._call_tool = call_tool
        self.calls: list[dict[str, Any]] = []

        class _Messages:
            def create(inner_self, **params: Any) -> SimpleNamespace:
                self.calls.append(params)
                tools = params.get("tools") or []
                messages = params.get("messages") or []
                already_called = any(
                    isinstance(m, dict)
                    and isinstance(m.get("content"), list)
                    and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in m["content"]
                    )
                    for m in messages
                )
                if self._call_tool and tools and not already_called:
                    tool_name = str(tools[0]["name"])
                    return SimpleNamespace(
                        content=[
                            _tool_use_block(id="toolu_1", name=tool_name, input={"query": "x"})
                        ],
                        usage=_usage(),
                        stop_reason="tool_use",
                    )
                return SimpleNamespace(
                    content=[_text_block("final answer")],
                    usage=_usage(),
                    stop_reason="end_turn",
                )

        self.messages = _Messages()


def _api_mode_args(tmp_path: Path) -> list[str]:
    return [
        "--mode", "api",
        "--search-backend", "keyword",
        "--scale", "smoke",
        "--store", str(tmp_path / "results.jsonl"),
        "--materialize-root", str(tmp_path / "mat"),
        "--out-dir", str(tmp_path / "measurements"),
    ]


def test_anthropic_provider_stub_client_preflight_passes_when_recall_is_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    stub_client = _RepeatingToolStubClient(call_tool=True)
    monkeypatch.setattr("tests.evals.rollout.build_live_client", lambda **_kw: stub_client)

    exit_code = north_star_cli.main(_api_mode_args(tmp_path))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "preflight: ok provider=api" in out
    assert "arm=push_breadcrumb_pull recall_called=true" in out
    assert "apiKeySource" not in out  # claude-cli-only field
    grid_lines = (tmp_path / "results.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(grid_lines) == len(ALL_ARMS)


def test_anthropic_provider_stub_client_preflight_fails_when_recall_is_not_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    stub_client = _RepeatingToolStubClient(call_tool=False)
    monkeypatch.setattr("tests.evals.rollout.build_live_client", lambda **_kw: stub_client)

    exit_code = north_star_cli.main(_api_mode_args(tmp_path))

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "push_breadcrumb_pull" in err
    assert "provider=api" in err
    assert "tool_calls=0" in err
    assert not (tmp_path / "results.jsonl").exists()


# ---------------------------------------------------------------------------
# Sibling-store isolation: the grid still runs every planned cell, and
# read_planned_cells is unchanged by a preflight that happens to share the
# grid's own (probe, corpus_scale, replicate) key (the default smoke-scale
# shape -- both default to the same first probe, "core", replicate 0).
# ---------------------------------------------------------------------------


def _stub_records_with_recall(probe_id: str, corpus_scale: str) -> dict[str, RolloutRecord]:
    records: dict[str, RolloutRecord] = {}
    for arm in ALL_ARMS:
        is_pull_arm = arm.value == "push_breadcrumb_pull"
        records[arm.value] = RolloutRecord(
            arm=arm,
            probe_id=probe_id,
            probe_class="single_hop",
            corpus_scale=corpus_scale,
            answer=f"stub answer for {arm.value}",
            turn_tokens=[TurnTokenUsage(turn=1, input_tokens=10, output_tokens=5)],
            tool_calls=[ToolCall(name=RECALL_TOOL_NAME, query="x")] if is_pull_arm else [],
            recall_called=is_pull_arm,
            turn_count=1,
            transcript=[{"answer": f"stub answer for {arm.value}"}],
            llm_provider="api",
        )
    return records


def _stub_run_probe_all_arms_with_recall(
    probe_id: str,
    corpus_scale: str,
    *,
    session: Any,
    materialize_root: Any,
    model: str,
    search_backend: str,
    claude_binary: str,
    replicate: int,
    mode: str = "api",
    should_stop: Any = None,
) -> dict[str, RolloutRecord]:
    return _stub_records_with_recall(probe_id, corpus_scale)


def test_grid_runs_every_planned_cell_despite_a_same_key_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        north_star_cli, "run_probe_all_arms", _stub_run_probe_all_arms_with_recall
    )
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")
    args = [
        "--scale", "smoke",
        "--materialize-root", str(tmp_path / "mat"),
        "--out-dir", str(tmp_path / "measurements"),
    ]

    exit_code = north_star_cli.main(args)

    assert exit_code == 0
    store = ResultStore(tmp_path / "r.jsonl")
    grid_lines = (tmp_path / "r.jsonl").read_text(encoding="utf-8").strip().splitlines()
    # The real grid group ran in full -- NOT skipped as "already done" by
    # the preflight's own identically-keyed group (same default probe,
    # corpus_scale "core", replicate 0 -- smoke scale and the preflight
    # default coincide exactly, which is what makes this the real risk the
    # sibling store exists to prevent, not a contrived edge case).
    assert len(grid_lines) == len(ALL_ARMS)
    # read_planned_cells is UNCHANGED by the preflight -- it counts only the
    # real grid's own cells, never the preflight's.
    assert read_planned_cells(store) == len(ALL_ARMS)

    preflight_store = ResultStore(tmp_path / "r.jsonl.preflight.jsonl")
    preflight_lines = (
        (tmp_path / "r.jsonl.preflight.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )
    assert len(preflight_lines) == len(ALL_ARMS)
    # The preflight's own sibling store carries no planned-cell sidecar at
    # all -- nothing in this driver ever calls write_planned_cells for it.
    assert read_planned_cells(preflight_store) is None


def test_resumed_grid_still_runs_every_planned_cell_without_double_appending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        north_star_cli, "run_probe_all_arms", _stub_run_probe_all_arms_with_recall
    )
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")
    args = [
        "--scale", "smoke",
        "--materialize-root", str(tmp_path / "mat"),
        "--out-dir", str(tmp_path / "measurements"),
    ]

    first = north_star_cli.main(args)
    assert first == 0
    first_grid_lines = (tmp_path / "r.jsonl").read_text(encoding="utf-8").strip().splitlines()
    first_preflight_lines = (
        (tmp_path / "r.jsonl.preflight.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )

    second = north_star_cli.main(args)
    assert second == 0
    second_grid_lines = (tmp_path / "r.jsonl").read_text(encoding="utf-8").strip().splitlines()
    second_preflight_lines = (
        (tmp_path / "r.jsonl.preflight.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )

    # The resumed grid still ran (did not skip) every planned cell, and
    # did not double-append it -- same contract test_north_star_cli.py's
    # own test_rerun_does_not_double_append_a_completed_group pins for the
    # pre-athenaeum#1959 driver.
    assert second_grid_lines == first_grid_lines
    assert len(second_grid_lines) == len(ALL_ARMS)
    # The preflight re-runs every time (it is not itself resume-aware --
    # there is no reason for it to be, it is a cheap one-group check), so
    # its sibling store simply grows by one more full group.
    assert len(second_preflight_lines) == 2 * len(first_preflight_lines)
    assert read_planned_cells(ResultStore(tmp_path / "r.jsonl")) == len(ALL_ARMS)
