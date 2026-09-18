# SPDX-License-Identifier: Apache-2.0
"""Tests for the Phase 2 CLI flags + sibling store (issue athenaeum#1785).

Covers, offline (no network, no subprocess spawn, no token spent): the CLI
flag parsing/validation, the sibling-JSONL round trip and its
(system, corpus_scale) resume-skip contract, the combined Phase 1 + Phase 2
spend gate (both the refusal and the zero-call dry-run projection), a
partial (exit 75) athenaeum compile recorded (never silently pooled), and
report rendering with real WriteCost/WritePathStats rows. Both Phase 2
producers (``compile_observation_stream``, ``run_native_writer_dispatch``)
are monkeypatched to stubs; ``generate_core_observations`` itself is real
(pure, deterministic, no network) per this issue's own acceptance criterion
("a tiny fixture stream").

Also covers the ``evals.yml`` workflow_dispatch wiring for issue
athenaeum#1786, mirroring ``tests/evals/test_north_star_max_tokens.py``'s
own grep-based idiom.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.evals import north_star_cli
from tests.evals.corpus import generate_core_observations
from tests.evals.harness import EvalSession
from tests.evals.north_star_report import WriteCost, WritePathStats, build_report
from tests.evals.rollout import NativeWriterResult
from tests.evals.write_path import CompileOutcome

# ---------------------------------------------------------------------------
# Stubs for the two Phase 2 producers
# ---------------------------------------------------------------------------


def _stub_compile_observation_stream(
    stream: Any,
    knowledge_root: Path,
    *,
    client: Any,
    model: str,
    session: EvalSession | None = None,
    run_kwargs: dict[str, Any] | None = None,
    exit_code: int = 0,
) -> tuple[dict[str, str], WriteCost, CompileOutcome]:
    store_files = {"page.md": "Internal reference tag: stubtoken\n"}
    write_cost = WriteCost(
        system="athenaeum", corpus_scale=stream.scale, input_tokens=100, output_tokens=20
    )
    outcome = CompileOutcome(exit_code=exit_code, partial=exit_code == 75)
    return store_files, write_cost, outcome


def _stub_run_native_writer_dispatch(
    observations: Any,
    materialize_root: Path,
    *,
    mode: str = "api",
    client: Any | None = None,
    session: EvalSession | None = None,
    model: str = "test-model",
    claude_binary: str = "claude",
    timeout: float = 120.0,
) -> NativeWriterResult:
    return NativeWriterResult(
        sessions=[],
        memory_dir=materialize_root,
        memory_files={"note.md": "some note text"},
        mode=mode,
        prompt_fidelity="reconstructed" if mode == "api" else None,
    )


def _make_args(**overrides: Any) -> Any:
    argv = ["--scale", "smoke"]
    return north_star_cli.build_arg_parser().parse_args(argv + overrides.pop("extra_argv", []))


# ---------------------------------------------------------------------------
# 1. Flag parsing / validation
# ---------------------------------------------------------------------------


def test_phase2_off_by_default() -> None:
    args = north_star_cli.build_arg_parser().parse_args(["--scale", "smoke"])
    assert args.phase2 is False
    assert args.phase2_scales is None
    assert args.phase2_store is None
    assert args.phase2_systems is None


def test_resolve_phase2_scales_defaults_to_medium() -> None:
    args = north_star_cli.build_arg_parser().parse_args(["--scale", "smoke"])
    assert north_star_cli._resolve_phase2_scales(args) == ["medium"]


def test_resolve_phase2_scales_rejects_unknown() -> None:
    args = north_star_cli.build_arg_parser().parse_args(
        ["--scale", "smoke", "--phase2-scales", "not-a-scale"]
    )
    with pytest.raises(ValueError, match="not-a-scale"):
        north_star_cli._resolve_phase2_scales(args)


def test_resolve_phase2_systems_defaults_to_both() -> None:
    args = north_star_cli.build_arg_parser().parse_args(["--scale", "smoke"])
    assert north_star_cli._resolve_phase2_systems(args) == ["athenaeum", "native"]


def test_resolve_phase2_systems_rejects_unknown() -> None:
    args = north_star_cli.build_arg_parser().parse_args(
        ["--scale", "smoke", "--phase2-systems", "bogus"]
    )
    with pytest.raises(ValueError, match="bogus"):
        north_star_cli._resolve_phase2_systems(args)


def test_resolve_phase2_store_path_default_derives_from_store(tmp_path: Path) -> None:
    args = north_star_cli.build_arg_parser().parse_args(
        ["--scale", "smoke", "--store", str(tmp_path / "results.jsonl")]
    )
    assert north_star_cli._resolve_phase2_store_path(args) == Path(
        str(tmp_path / "results.jsonl") + ".phase2.jsonl"
    )


def test_resolve_phase2_store_path_honours_explicit_flag(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.phase2.jsonl"
    args = north_star_cli.build_arg_parser().parse_args(
        ["--scale", "smoke", "--phase2-store", str(explicit)]
    )
    assert north_star_cli._resolve_phase2_store_path(args) == explicit


# ---------------------------------------------------------------------------
# 2. Sibling-store round trip + resume skip
# ---------------------------------------------------------------------------


def test_run_phase2_group_athenaeum_writes_the_completion_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        north_star_cli, "compile_observation_stream", _stub_compile_observation_stream
    )
    stream = generate_core_observations(scale="medium")
    rows = north_star_cli._run_phase2_group(
        "athenaeum",
        "medium",
        observations=stream.observations,
        stream=stream,
        materialize_root=tmp_path,
        client=object(),
        session=EvalSession(),
        model="test-model",
        mode="api",
        claude_binary="claude",
    )
    kinds = {row["kind"] for row in rows}
    assert kinds == {"write_path", "write_cost", "meta"}
    write_cost_row = next(r for r in rows if r["kind"] == "write_cost")
    assert write_cost_row["system"] == "athenaeum"
    assert write_cost_row["corpus_scale"] == "medium"
    assert write_cost_row["input_tokens"] == 100
    meta_row = next(r for r in rows if r["kind"] == "meta")
    assert meta_row["exit_code"] == 0
    assert meta_row["partial"] is False


def test_run_phase2_group_native_writes_the_completion_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        north_star_cli, "run_native_writer_dispatch", _stub_run_native_writer_dispatch
    )
    stream = generate_core_observations(scale="medium")
    rows = north_star_cli._run_phase2_group(
        "native",
        "medium",
        observations=stream.observations,
        stream=stream,
        materialize_root=tmp_path,
        client=object(),
        session=EvalSession(),
        model="test-model",
        mode="api",
        claude_binary="claude",
    )
    kinds = {row["kind"] for row in rows}
    assert kinds == {"write_path", "write_cost", "meta"}
    write_cost_row = next(r for r in rows if r["kind"] == "write_cost")
    assert write_cost_row["system"] == "native"
    assert write_cost_row["corpus_scale"] == "medium"
    meta_row = next(r for r in rows if r["kind"] == "meta")
    assert meta_row["mode"] == "api"
    assert meta_row["prompt_fidelity"] == "reconstructed"
    assert meta_row["sessions"] == 0


def test_phase2_sibling_store_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "store.jsonl.phase2.jsonl"
    stats = WritePathStats(
        system="athenaeum",
        corpus_scale="medium",
        pages_targeted=3,
        pages_written=3,
        answer_tokens_total=2,
        answer_tokens_retained=2,
        observations_total=10,
        observations_measured=2,
        observations_dropped=0,
    )
    cost = WriteCost(system="athenaeum", corpus_scale="medium", input_tokens=50, output_tokens=10)
    north_star_cli._phase2_append_rows(
        path,
        [
            north_star_cli._write_path_stats_row(stats),
            north_star_cli._write_cost_row(cost),
            {"kind": "meta", "system": "athenaeum", "corpus_scale": "medium", "exit_code": 0},
        ],
    )
    loaded_stats, loaded_costs = north_star_cli.load_phase2_results(path)
    assert loaded_stats == [stats]
    assert loaded_costs == [cost]


def test_phase2_completed_keys_requires_both_rows() -> None:
    rows = [
        {"kind": "write_path", "system": "athenaeum", "corpus_scale": "medium"},
        # No matching write_cost row for native -- must not count as done.
        {"kind": "write_path", "system": "native", "corpus_scale": "medium"},
        {"kind": "write_cost", "system": "athenaeum", "corpus_scale": "medium"},
    ]
    done = north_star_cli._phase2_completed_keys(rows)
    assert done == {("athenaeum", "medium")}


def test_run_phase2_resume_skips_completed_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def _spy_group(system: str, scale: str, **_kwargs: Any) -> list[dict[str, Any]]:
        calls.append((system, scale))
        return [
            {"kind": "write_path", "system": system, "corpus_scale": scale},
            {"kind": "write_cost", "system": system, "corpus_scale": scale},
            {"kind": "meta", "system": system, "corpus_scale": scale},
        ]

    monkeypatch.setattr(north_star_cli, "_run_phase2_group", _spy_group)
    phase2_store_path = tmp_path / "results.jsonl.phase2.jsonl"
    # Pre-populate the athenaeum/medium pair as already-done.
    north_star_cli._phase2_append_rows(
        phase2_store_path,
        [
            {"kind": "write_path", "system": "athenaeum", "corpus_scale": "medium"},
            {"kind": "write_cost", "system": "athenaeum", "corpus_scale": "medium"},
        ],
    )
    args = north_star_cli.build_arg_parser().parse_args(
        ["--scale", "smoke", "--phase2", "--phase2-scales", "medium"]
    )
    north_star_cli.run_phase2(
        args,
        client=object(),
        session=EvalSession(),
        materialize_root=tmp_path / "mat",
        phase2_store_path=phase2_store_path,
    )
    # Only the native group ran; athenaeum/medium was skipped as resumed.
    assert calls == [("native", "medium")]


# ---------------------------------------------------------------------------
# 3. main() end to end, offline
# ---------------------------------------------------------------------------


def _stub_run_probe_all_arms(probe_id: str, corpus_scale: str, **_kwargs: Any) -> dict[str, Any]:
    from tests.evals.rollout import ALL_ARMS, RolloutRecord

    return {
        arm.value: RolloutRecord(
            arm=arm,
            probe_id=probe_id,
            probe_class="single_hop",
            corpus_scale=corpus_scale,
            answer=f"stub answer for {arm.value}",
        )
        for arm in ALL_ARMS
    }


def test_main_phase2_end_to_end_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        north_star_cli, "compile_observation_stream", _stub_compile_observation_stream
    )
    monkeypatch.setattr(
        north_star_cli, "run_native_writer_dispatch", _stub_run_native_writer_dispatch
    )
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "build_live_client", lambda: object())

    store_path = tmp_path / "results.jsonl"
    out_dir = tmp_path / "measurements"
    exit_code = north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--store",
            str(store_path),
            "--phase2",
            "--phase2-scales",
            "medium",
            "--max-spend",
            "100",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(out_dir),
        ]
    )
    assert exit_code == 0
    phase2_store_path = Path(str(store_path) + ".phase2.jsonl")
    assert phase2_store_path.exists()
    rows = north_star_cli._phase2_read_rows(phase2_store_path)
    kinds_by_system = {(r["system"], r["kind"]) for r in rows if r.get("kind") != "meta"}
    assert ("athenaeum", "write_cost") in kinds_by_system
    assert ("native", "write_cost") in kinds_by_system

    [report_path] = list(out_dir.glob("north-star-*.md"))
    report_text = report_path.read_text(encoding="utf-8")
    assert "phase2: on" in report_text
    assert "native prompt_fidelity=reconstructed" in report_text
    assert "_no Phase 2 write-path data in this run_" not in report_text


def test_main_phase2_dry_run_makes_zero_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run must never run Phase 2 or construct a client")

    monkeypatch.setattr(north_star_cli, "compile_observation_stream", _explode)
    monkeypatch.setattr(north_star_cli, "run_native_writer_dispatch", _explode)
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _explode)
    monkeypatch.setattr(north_star_cli, "build_live_client", _explode)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(
        ["--scale", "smoke", "--phase2", "--max-spend", "100", "--dry-run"]
    )
    assert exit_code == 0
    assert not (tmp_path / "r.jsonl").exists()


def test_main_phase2_spend_gate_refuses_combined_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A default --max-spend that covers the read grid alone must still
    refuse once Phase 2's projected cost is added in -- the combined gate
    this issue's brief requires, not two independent gates that can each
    pass while their sum overshoots."""

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a refused run must never spend")

    monkeypatch.setattr(north_star_cli, "compile_observation_stream", _explode)
    monkeypatch.setattr(north_star_cli, "run_native_writer_dispatch", _explode)
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _explode)
    monkeypatch.setattr(north_star_cli, "build_live_client", _explode)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")

    exit_code = north_star_cli.main(["--scale", "smoke", "--phase2"])  # default --max-spend $1.00

    assert exit_code == 1
    assert not (tmp_path / "r.jsonl").exists()
    err = capsys.readouterr().err
    assert "Phase 2" in err
    assert "--max-spend" in err


def test_main_phase2_partial_compile_recorded_not_silently_pooled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 75 (EXIT_GRACEFUL_PARTIAL) athenaeum compile still writes its real
    write_path/write_cost rows -- CompileOutcome's own docstring: the store
    is valid to measure -- but the meta row's partial flag must be visible,
    never silently dropped (issue athenaeum#1785 brief)."""

    def _partial_compile(*args: Any, **kwargs: Any) -> Any:
        return _stub_compile_observation_stream(*args, **kwargs, exit_code=75)

    monkeypatch.setattr(north_star_cli, "compile_observation_stream", _partial_compile)
    monkeypatch.setattr(
        north_star_cli, "run_native_writer_dispatch", _stub_run_native_writer_dispatch
    )
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _stub_run_probe_all_arms)
    monkeypatch.setattr(north_star_cli, "build_live_client", lambda: object())

    store_path = tmp_path / "results.jsonl"
    exit_code = north_star_cli.main(
        [
            "--scale",
            "smoke",
            "--store",
            str(store_path),
            "--phase2",
            "--phase2-scales",
            "medium",
            "--phase2-systems",
            "athenaeum",
            "--max-spend",
            "100",
            "--materialize-root",
            str(tmp_path / "mat"),
            "--out-dir",
            str(tmp_path / "measurements"),
        ]
    )
    assert exit_code == 0
    phase2_store_path = Path(str(store_path) + ".phase2.jsonl")
    rows = north_star_cli._phase2_read_rows(phase2_store_path)
    meta_row = next(r for r in rows if r["kind"] == "meta")
    assert meta_row["exit_code"] == 75
    assert meta_row["partial"] is True
    # The numbers are NOT withheld -- write_path/write_cost rows still land.
    assert any(r["kind"] == "write_cost" for r in rows)
    assert any(r["kind"] == "write_path" for r in rows)


# ---------------------------------------------------------------------------
# 4. Report rendering with real WriteCost/WritePathStats rows
# ---------------------------------------------------------------------------


def test_build_report_renders_write_path_section_and_phase2_summary() -> None:
    stats = WritePathStats(
        system="athenaeum",
        corpus_scale="medium",
        pages_targeted=5,
        pages_written=5,
        answer_tokens_total=3,
        answer_tokens_retained=3,
        observations_total=20,
        observations_measured=3,
        observations_dropped=0,
    )
    cost = WriteCost(system="athenaeum", corpus_scale="medium", input_tokens=500, output_tokens=80)
    report = north_star_cli.build_report(
        [],
        write_path_stats=[stats],
        write_costs=[cost],
        phase2_summary="on (scales=medium, systems=athenaeum,native)",
    )
    rendered = north_star_cli.write_report is not None  # sanity import check
    assert rendered
    from tests.evals.north_star_report import render_report

    text = render_report(report)
    assert "phase2: on (scales=medium, systems=athenaeum,native)" in text
    assert "| athenaeum | medium | 5 | 5 |" in text
    assert "_no Phase 2 write-path data in this run_" not in text


def test_build_report_phase2_summary_defaults_to_empty_and_renders_nothing() -> None:
    report = build_report([])
    assert report.phase2_summary == ""
    from tests.evals.north_star_report import render_report

    text = render_report(report)
    assert "- phase2:" not in text


# ---------------------------------------------------------------------------
# 5. evals.yml wiring (issue athenaeum#1786, mirrors
#    test_north_star_max_tokens.py's own idiom)
# ---------------------------------------------------------------------------


def test_evals_yml_wires_the_phase2_inputs_to_the_flags() -> None:
    repo_root = Path(north_star_cli.__file__).resolve().parents[2]
    evals_yml = (repo_root / ".github" / "workflows" / "evals.yml").read_text(encoding="utf-8")

    assert "north_star_phase2:" in evals_yml
    assert "north_star_phase2_scales:" in evals_yml
    assert "north_star_phase2_systems:" in evals_yml
    assert "NORTH_STAR_PHASE2: ${{ github.event.inputs.north_star_phase2 }}" in evals_yml
    assert (
        "NORTH_STAR_PHASE2_SCALES: ${{ github.event.inputs.north_star_phase2_scales }}" in evals_yml
    )
    assert (
        "NORTH_STAR_PHASE2_SYSTEMS: ${{ github.event.inputs.north_star_phase2_systems }}"
        in evals_yml
    )
    assert 'if [ "${NORTH_STAR_PHASE2:-false}" = "true" ]; then' in evals_yml
    assert "PHASE2_FLAG=(--phase2)" in evals_yml
    assert '"${PHASE2_FLAG[@]}"' in evals_yml
    assert 'PHASE2_SCALES_FLAG=(--phase2-scales "$NORTH_STAR_PHASE2_SCALES")' in evals_yml
    assert '"${PHASE2_SCALES_FLAG[@]}"' in evals_yml
    assert 'PHASE2_SYSTEMS_FLAG=(--phase2-systems "$NORTH_STAR_PHASE2_SYSTEMS")' in evals_yml
    assert '"${PHASE2_SYSTEMS_FLAG[@]}"' in evals_yml
    assert "measurements/north-star-store.jsonl.phase2.jsonl" in evals_yml
    # AC5: the new inputs must not have grown the job a push trigger -- the
    # north-star job stays dispatch-only and opt-in, same gate as before.
    assert (
        "github.event_name == 'workflow_dispatch' " "&& github.event.inputs.north_star == 'true'"
    ) in evals_yml


def test_evals_yml_no_push_trigger_gained_by_north_star_job() -> None:
    repo_root = Path(north_star_cli.__file__).resolve().parents[2]
    evals_yml = (repo_root / ".github" / "workflows" / "evals.yml").read_text(encoding="utf-8")
    # The workflow's only `push:` trigger remains `branches: [main]`, gating
    # only `embedding-suite` -- neither `eval` nor `north-star` reads it.
    assert evals_yml.count("push:") == 1
