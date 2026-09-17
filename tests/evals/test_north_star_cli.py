# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the north-star CLI driver (issue athenaeum#1523).

Mirrors ``tests/evals/test_containment_cli.py``'s shape: the default-budget
arithmetic proof, and (this issue's own explicit AC) a dry-run test proving
zero paid calls — no model client construction, no ``claude -p`` spawn.
Group-granularity resume is exercised with a monkeypatched
``run_probe_all_arms`` stub so no live rollout is ever needed.

``rollout``-marked (imports ``tests.evals.rollout``, which is itself
rollout-suite machinery) and fully offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.evals import north_star_cli
from tests.evals.containment import DEFAULT_CELL_TOKEN_ESTIMATE, price_grid
from tests.evals.corpus import SCALES
from tests.evals.north_star_report import DEFAULT_VERDICT_ARM, load_rollout_rows
from tests.evals.rollout import ALL_ARMS, RolloutRecord, TurnTokenUsage

pytestmark = pytest.mark.rollout


def _stub_records(probe_id: str, corpus_scale: str) -> dict[str, RolloutRecord]:
    return {
        arm.value: RolloutRecord(
            arm=arm,
            probe_id=probe_id,
            probe_class="single_hop",
            corpus_scale=corpus_scale,
            answer=f"stub answer for {arm.value}",
            turn_tokens=[TurnTokenUsage(turn=1, input_tokens=10, output_tokens=5)],
            turn_count=1,
            transcript=[{"answer": f"stub answer for {arm.value}"}],
        )
        for arm in ALL_ARMS
    }


def _stub_run_probe_all_arms(
    probe_id: str,
    corpus_scale: str,
    *,
    session: Any,
    materialize_root: Any,
    model: str,
    search_backend: str,
    claude_binary: str,
    replicate: int,
    mode: str = "cli",
    should_stop: Any = None,
) -> dict[str, RolloutRecord]:
    return _stub_records(probe_id, corpus_scale)


# ---------------------------------------------------------------------------
# Default --max-spend arithmetic proof
# ---------------------------------------------------------------------------


def test_verdict_arm_flag_defaults_to_the_report_module_constant() -> None:
    """The CLI's own default must track ``DEFAULT_VERDICT_ARM`` (imported
    from ``north_star_report``), never a second, independently-drifting
    literal."""
    args = north_star_cli.build_arg_parser().parse_args([])
    assert args.verdict_arm == DEFAULT_VERDICT_ARM


def test_xlarge_is_a_known_scale_but_not_a_default_one() -> None:
    """Issue athenaeum#1735's opt-in requirement, pinned so a future edit
    that empties ``_OPT_IN_CORPUS_SCALES`` (or otherwise reverts
    ``DEFAULT_CORPUS_SCALES`` to ``tuple(sorted(SCALES))``) fails loudly
    instead of silently pulling the most expensive grid cell into every
    blank ``--corpus-scales`` run.
    """
    assert "xlarge" in SCALES
    assert "xlarge" not in north_star_cli.DEFAULT_CORPUS_SCALES


def test_corpus_scales_flag_rejects_an_unknown_scale() -> None:
    """AC3 ("evals.yml's grid dispatch input accepts xlarge") is only real
    if a typo'd scale is caught before any spend -- see
    :func:`north_star_cli._build_cells`'s validation, added alongside it."""
    args = north_star_cli.build_arg_parser().parse_args(["--corpus-scales", "bogus"])
    with pytest.raises(ValueError, match="unknown corpus scale"):
        north_star_cli._build_cells(args)


def test_corpus_scales_flag_accepts_xlarge_explicitly() -> None:
    args = north_star_cli.build_arg_parser().parse_args(["--corpus-scales", "xlarge"])
    cells = north_star_cli._build_cells(args)
    assert cells
    assert all(c.corpus_scale == "xlarge" for c in cells)


def test_default_max_spend_covers_smoke_scale() -> None:
    cells = north_star_cli._build_cells(north_star_cli.build_arg_parser().parse_args([]))
    estimate = price_grid(
        cells,
        model=north_star_cli.DEFAULT_ROLLOUT_MODEL,
        max_spend_usd=north_star_cli.DEFAULT_MAX_SPEND_USD,
        per_cell=DEFAULT_CELL_TOKEN_ESTIMATE,
    )  # must not raise
    assert estimate.estimated_usd < north_star_cli.DEFAULT_MAX_SPEND_USD
    # smoke caps probe/corpus_scale/replicate to 1 each, but every selected
    # group always expands to its real arms (see _build_cells) -- never
    # fewer, since run_probe_all_arms has no partial-arm mode.
    assert estimate.cell_count == len(ALL_ARMS)


# ---------------------------------------------------------------------------
# --dry-run makes zero paid calls
# ---------------------------------------------------------------------------


def test_dry_run_never_constructs_a_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _exploding_build_llm_client(*args: Any, **kwargs: Any) -> MagicMock:
        raise AssertionError("dry-run must never construct a live client")

    def _exploding_run_probe_all_arms(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run must never run a rollout cell")

    monkeypatch.setattr("athenaeum.provider.build_llm_client", _exploding_build_llm_client)
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(["--dry-run"])

    assert exit_code == 0
    assert not (tmp_path / "r.jsonl").exists()
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "zero paid calls" in out


def test_dry_run_full_scale_over_budget_is_refused_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _exploding_run_probe_all_arms(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused grid must never run a cell")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(["--scale", "full", "--max-spend", "0.0"])

    assert exit_code == 1
    assert not (tmp_path / "r.jsonl").exists()
    assert "refusing to start" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Group-granularity resume (probe, corpus_scale, replicate), not per arm cell
# ---------------------------------------------------------------------------


def test_smoke_run_persists_every_arm_for_one_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    # smoke caps arms to 1, but our driver always requests every arm for the
    # ONE (probe, corpus_scale, replicate) group smoke selects -- assert the
    # actual persisted rows, not the grid's own per-axis cap.
    exit_code = north_star_cli.main(
        [
            "--scale", "smoke",
            "--materialize-root", str(tmp_path / "mat"),
            "--out-dir", str(tmp_path / "measurements"),
        ]
    )

    assert exit_code == 0
    store_path = tmp_path / "r.jsonl"
    lines = store_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(ALL_ARMS)  # every arm, for the one smoke-selected group
    arms = {json.loads(line)["arm"] for line in lines}
    assert arms == {arm.value for arm in ALL_ARMS}


def test_rerun_does_not_double_append_a_completed_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")
    args = [
        "--scale", "smoke",
        "--materialize-root", str(tmp_path / "mat"),
        "--out-dir", str(tmp_path / "measurements"),
    ]

    first = north_star_cli.main(args)
    assert first == 0
    first_lines = (tmp_path / "r.jsonl").read_text(encoding="utf-8").strip().splitlines()

    second = north_star_cli.main(args)
    assert second == 0
    second_lines = (tmp_path / "r.jsonl").read_text(encoding="utf-8").strip().splitlines()

    assert second_lines == first_lines  # no group re-run, no duplicate rows


def test_smoke_run_writes_a_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(tmp_path / "measurements"),
        ]
    )

    assert exit_code == 0
    reports = list((tmp_path / "measurements").glob("north-star-*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "North-star report" in text


def test_verdict_arm_flag_threads_through_to_the_written_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--verdict-arm`` must reach ``build_report`` end to end -- this is
    the one path that would silently regress if a future edit dropped the
    kwarg anywhere along ``main`` -> ``build_report`` ->
    ``render_decision_block`` -> ``compute_verdicts``."""
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(
        [
            "--scale", "smoke",
            "--materialize-root", str(tmp_path / "mat"),
            "--out-dir", str(tmp_path / "measurements"),
            "--verdict-arm", "pull",
        ]
    )

    assert exit_code == 0
    reports = list((tmp_path / "measurements").glob("north-star-*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "**Verdict arm:** `pull`" in text


def test_store_rows_round_trip_through_the_report_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.evals.containment import ResultStore

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    north_star_cli.main(
        [
            "--scale", "smoke",
            "--materialize-root", str(tmp_path / "mat"),
            "--out-dir", str(tmp_path / "measurements"),
        ]
    )

    rows = load_rollout_rows(ResultStore(tmp_path / "r.jsonl"))
    assert len(rows) == len(ALL_ARMS)
    assert {row.record.arm for row in rows} == set(ALL_ARMS)


# ---------------------------------------------------------------------------
# --mode parsing + ATHENAEUM_EVAL_MODE fallback (Quine review, issue athenaeum#1733,
# SHOULD item 5)
# ---------------------------------------------------------------------------


def test_mode_flag_defaults_to_api_with_no_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATHENAEUM_EVAL_MODE", raising=False)
    args = north_star_cli.build_arg_parser().parse_args([])
    assert args.mode == "api"


def test_mode_flag_falls_back_to_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENAEUM_EVAL_MODE", "cli")
    args = north_star_cli.build_arg_parser().parse_args([])
    assert args.mode == "cli"


def test_mode_flag_explicit_wins_over_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENAEUM_EVAL_MODE", "cli")
    args = north_star_cli.build_arg_parser().parse_args(["--mode", "api"])
    assert args.mode == "api"


def test_mode_is_threaded_through_to_run_probe_all_arms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_modes: list[str] = []

    def _capturing_run_probe_all_arms(
        probe_id: str,
        corpus_scale: str,
        *,
        session: Any,
        materialize_root: Any,
        model: str,
        search_backend: str,
        claude_binary: str,
        replicate: int,
        # Deliberately NEITHER "api" NOR "cli" (Quine review, issue
        # athenaeum#1733 SHOULD item 5): if north_star_cli.py ever drops its
        # own `mode=mode` kwarg at the call site, this stub's default is what
        # `_run_cells` would silently fall back to -- and "sentinel-default"
        # can never match either value the test explicitly requests below,
        # so a dropped kwarg fails LOUDLY instead of coincidentally matching.
        mode: str = "sentinel-default",
        should_stop: Any = None,
    ) -> dict[str, RolloutRecord]:
        seen_modes.append(mode)
        return _stub_records(probe_id, corpus_scale)

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _capturing_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    north_star_cli.main(
        [
            "--scale", "smoke",
            "--mode", "cli",
            "--materialize-root", str(tmp_path / "mat"),
            "--out-dir", str(tmp_path / "measurements"),
        ]
    )

    assert seen_modes == ["cli"]
