# SPDX-License-Identifier: Apache-2.0
"""``--max-tokens``: one knob for dollars and tokens (issue athenaeum#1754).

Run 35200779015 was dispatched with ``--max-spend 75`` and died at 392 of
1392 cells on ``rollout run exceeded token ceiling (2018960 > 2000000)`` --
about $3.35 spent against $75 authorized (2,018,960 tokens priced on Haiku
4.5 at the INHERITED 5:1 input/output split; that run recorded only the
total, so the split, and therefore the dollar figure, is an assumption --
see ``NORTH_STAR_CELL_TOKEN_ESTIMATE``). The USD ceiling the operator set had
no bearing on the token ceiling that actually stopped the run, and the
dry-run projection (24,000 tokens/cell) was nearly five times the measured
figure, so nothing warned anyone beforehand.

The four properties here are that failure mode's inverse:

1. The ceiling comes from ``--max-tokens`` when given, from ``--max-spend``
   when it is not, and from ``ROLLOUT_TOKEN_CEILING`` only when neither is
   -- and the ``--max-spend 75`` case derives a ceiling far ABOVE the
   constant, which is exactly what would have let that run finish.
2. A 1-token ceiling aborts a real ``main()`` run; a generous one does not.
   Same grid, same stub, only the flag differs.
3. ``--dry-run`` prints the ceiling that will apply and refuses when the
   projection exceeds it, naming both numbers -- up front, not 392 cells in.
4. The per-cell estimate is the measured one, and the wall-clock line the
   projection is sized against still prints.

**Deliberately unmarked** (issue athenaeum#1742): every cell runs through a
stub, no LLM client is constructed, no ``claude`` binary is spawned, so this
module costs no tokens and must run in ``ci.yml``'s default job.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.evals import north_star_cli
from tests.evals.containment import (
    NORTH_STAR_CELL_TOKEN_ESTIMATE,
    tokens_for_spend,
)
from tests.evals.harness import EvalSession
from tests.evals.rollout import ALL_ARMS, RolloutRecord, TurnTokenUsage
from tests.evals.rollout_session import ROLLOUT_TOKEN_CEILING

#: Comfortably above ``small``'s priced total, so a test that means to
#: exercise the TOKEN ceiling is never actually stopped by the USD one.
GENEROUS_MAX_SPEND = "100.0"

#: ``--scale small`` is 24 cells, so main's pre-flight projects
#: 24 * 5,150 = 123,600 tokens. Any test that wants to reach the MID-GRID
#: ceiling must sit above that, or the pre-flight refuses first and the run
#: never starts (which is a different property, tested separately below).
SMALL_GRID_PROJECTION = 24 * NORTH_STAR_CELL_TOKEN_ESTIMATE.total_tokens

#: Above the projection, below one group's stub spend (200,000) -- so the
#: run starts and then trips at the first group boundary.
MID_GRID_CEILING = "150000"


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


def _small_grid_args(tmp_path: Path, *, tag: str) -> list[str]:
    return [
        "--scale",
        "small",
        "--max-spend",
        GENEROUS_MAX_SPEND,
        "--workers",
        "1",
        "--materialize-root",
        str(tmp_path / f"mat-{tag}"),
        "--out-dir",
        str(tmp_path / f"measurements-{tag}"),
        "--store",
        str(tmp_path / f"store-{tag}.jsonl"),
    ]


def _burning_stub(
    probe_id: str,
    corpus_scale: str,
    *,
    session: EvalSession,
    materialize_root: Any,
    model: str,
    search_backend: str,
    claude_binary: str,
    replicate: int,
    mode: str = "cli",
    should_stop: Any = None,
) -> dict[str, RolloutRecord]:
    """Spends 200,000 tokens per group -- over :data:`MID_GRID_CEILING`, under a large one."""
    session.observe_response(
        model,
        SimpleNamespace(usage=SimpleNamespace(input_tokens=100_000, output_tokens=100_000)),
    )
    return _stub_records(probe_id, corpus_scale)


def _heavy_stub(
    probe_id: str,
    corpus_scale: str,
    *,
    session: EvalSession,
    materialize_root: Any,
    model: str,
    search_backend: str,
    claude_binary: str,
    replicate: int,
    mode: str = "cli",
    should_stop: Any = None,
) -> dict[str, RolloutRecord]:
    """Spends 700,000 tokens per group: 2.8M over the 4-group small grid.

    Sized to straddle :data:`ROLLOUT_TOKEN_CEILING` -- above the 2,000,000
    constant, below a 3,000,000 ``--max-tokens`` -- so a teardown assert that
    forgot to pass the run's own ceiling fails loudly.
    """
    session.observe_response(
        model,
        SimpleNamespace(usage=SimpleNamespace(input_tokens=350_000, output_tokens=350_000)),
    )
    return _stub_records(probe_id, corpus_scale)


def _parse(argv: list[str]) -> Any:
    return north_star_cli.build_arg_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# AC1: the ceiling's provenance
# ---------------------------------------------------------------------------


def test_max_tokens_flag_is_the_ceiling_verbatim() -> None:
    ceiling, source = north_star_cli.resolve_token_ceiling(_parse(["--max-tokens", "12345"]))
    assert ceiling == 12345
    assert "--max-tokens" in source


def test_max_spend_derives_the_ceiling_when_max_tokens_is_absent() -> None:
    args = _parse(["--max-spend", "75"])
    ceiling, source = north_star_cli.resolve_token_ceiling(args)
    assert ceiling == tokens_for_spend(
        75.0, model=args.model, per_cell=NORTH_STAR_CELL_TOKEN_ESTIMATE
    )
    assert "--max-spend" in source
    # The point of the issue: $75 buys far more than the constant that
    # actually stopped run 35200779015 with $73 still authorized.
    assert ceiling > ROLLOUT_TOKEN_CEILING


def test_max_tokens_wins_over_max_spend() -> None:
    ceiling, source = north_star_cli.resolve_token_ceiling(
        _parse(["--max-spend", "75", "--max-tokens", "500"])
    )
    assert (ceiling, "--max-tokens" in source) == (500, True)


def test_the_constant_applies_only_when_neither_flag_is_given() -> None:
    ceiling, source = north_star_cli.resolve_token_ceiling(_parse([]))
    assert ceiling == ROLLOUT_TOKEN_CEILING
    assert "ROLLOUT_TOKEN_CEILING" in source


def test_omitting_max_spend_still_prices_at_the_default() -> None:
    """Dropping ``--max-spend``'s argparse default to ``None`` must not
    change what an omitted flag SPENDS -- only what ceiling it derives."""
    assert north_star_cli.resolve_max_spend(_parse([])) == north_star_cli.DEFAULT_MAX_SPEND_USD
    assert north_star_cli.resolve_max_spend(_parse(["--max-spend", "9.5"])) == 9.5


def test_tokens_for_spend_is_the_inverse_of_the_price_table() -> None:
    """Haiku 4.5 is $1/$5 per MTok; the measured mix is 4,300 in / 850 out,
    so one cell costs $0.00855 and $1 buys about 602,000 tokens."""
    derived = tokens_for_spend(
        1.0, model="claude-haiku-4-5", per_cell=NORTH_STAR_CELL_TOKEN_ESTIMATE
    )
    assert 590_000 < derived < 615_000
    # Linear in the dollars, and zero (not a ceiling of zero) for no budget.
    assert tokens_for_spend(
        10.0, model="claude-haiku-4-5", per_cell=NORTH_STAR_CELL_TOKEN_ESTIMATE
    ) == pytest.approx(derived * 10, rel=1e-6)
    assert tokens_for_spend(0.0, model="claude-haiku-4-5") == 0


def test_deriving_a_ceiling_for_an_unpriced_model_is_refused() -> None:
    """``_rates_for_model`` falls back to a BLENDED rate for an unknown id,
    so a derived ceiling would be an authoritative-looking number computed
    from a guess. Refuse, and name the model (Quine review of PR
    athenaeum#1757)."""
    with pytest.raises(ValueError, match="totally-made-up-model"):
        tokens_for_spend(75.0, model="totally-made-up-model")
    with pytest.raises(ValueError, match="--max-tokens"):
        north_star_cli.resolve_token_ceiling(
            _parse(["--max-spend", "75", "--model", "totally-made-up-model"])
        )
    # Naming the ceiling outright still works on an unpriced model: only the
    # DERIVATION is refused, not the run.
    ceiling, _ = north_star_cli.resolve_token_ceiling(
        _parse(["--model", "totally-made-up-model", "--max-tokens", "777"])
    )
    assert ceiling == 777


def test_a_zero_max_spend_reports_its_provenance_honestly() -> None:
    """``--max-spend 0`` authorizes nothing and so derives nothing -- but the
    provenance must not then claim no spend flag was given, which is a
    different situation an operator would debug differently."""
    ceiling, source = north_star_cli.resolve_token_ceiling(_parse(["--max-spend", "0"]))
    assert ceiling == ROLLOUT_TOKEN_CEILING
    assert "--max-spend $0.00 derives no ceiling" in source
    assert "neither" not in source


def test_a_non_positive_max_tokens_is_rejected_at_parse_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same shape as ``--workers 0`` (issue athenaeum#1751): told at parse
    time, not discovered from a mid-grid abort."""

    def _exploding(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an invalid --max-tokens must never run a cell")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding)
    monkeypatch.setattr(north_star_cli, "_default_store_path", lambda: tmp_path / "r.jsonl")
    assert north_star_cli.main(["--max-tokens", "0"]) == 1
    assert "--max-tokens must be >= 1" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AC1b: the flag reaches a real run
# ---------------------------------------------------------------------------


def test_a_one_token_ceiling_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1-token ceiling never runs a cell: the pre-flight catches it."""
    def _exploding(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a ceiling below the projection must never run a cell")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding)
    assert north_star_cli.main([*_small_grid_args(tmp_path, tag="tiny"), "--max-tokens", "1"]) == 1


def test_a_live_run_refuses_before_the_first_paid_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a dry-run property (Quine review of PR athenaeum#1757).

    A real run whose projection already exceeds its ceiling would otherwise
    pay for every cell up to the mid-grid trip and then abort -- exactly the
    athenaeum#1754 failure. The pre-flight knows before the first call, so it
    refuses there, naming both numbers.
    """
    def _exploding(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the pre-flight must refuse before any cell runs")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding)
    assert (
        north_star_cli.main([*_small_grid_args(tmp_path, tag="live"), "--max-tokens", "1000"])
        == 1
    )
    err = capsys.readouterr().err
    assert f"projected tokens {SMALL_GRID_PROJECTION}" in err
    assert "token ceiling 1000" in err


def test_the_teardown_assert_uses_the_runs_own_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting ``ceiling=token_ceiling`` from main's teardown
    ``assert_rollout_ceiling`` must fail here.

    The stub burns 2.8M tokens under a 3M ``--max-tokens``: legal for this
    run, illegal against the 2,000,000 constant. Every mid-grid check passes
    (each group's running total stays under 3M), so the ONLY thing that can
    turn this run red is a teardown assert still measuring against the
    constant -- which is what makes a green result here load-bearing.
    """
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _heavy_stub)
    assert (
        north_star_cli.main(
            [*_small_grid_args(tmp_path, tag="teardown"), "--max-tokens", "3000000"]
        )
        == 0
    )


def test_a_large_ceiling_does_not_abort_the_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the pair: identical grid, identical stub, identical
    token spend -- only the flag differs, so a green result here proves the
    abort above was the CEILING and not the harness."""
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _burning_stub)
    assert (
        north_star_cli.main([*_small_grid_args(tmp_path, tag="big"), "--max-tokens", "10000000"])
        == 0
    )


def test_the_abort_message_names_the_flag_not_the_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mid-grid refusal is where an operator learns what to change.
    Pointing them at a source constant when a flag governs the run is the
    advice that produced athenaeum#1754 in the first place."""
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _burning_stub)
    north_star_cli.main(
        [*_small_grid_args(tmp_path, tag="msg"), "--max-tokens", MID_GRID_CEILING]
    )
    report = next((tmp_path / "measurements-msg").glob("north-star-*.md")).read_text(
        encoding="utf-8"
    )
    assert "--max-tokens" in report
    assert f"exceeded token ceiling (200000 > {MID_GRID_CEILING})" in report


# ---------------------------------------------------------------------------
# AC2: --dry-run prints the ceiling and refuses up front
# ---------------------------------------------------------------------------


def test_dry_run_prints_the_ceiling_that_will_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        north_star_cli.main(
            [*_small_grid_args(tmp_path, tag="dry"), "--dry-run", "--max-tokens", "10000000"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "token ceiling: 10000000 (--max-tokens)" in out
    # The wall-clock line the ceiling line sits beside must still print.
    assert "projected wall clock:" in out
    assert "estimated=$" in out


def test_dry_run_refuses_when_the_projection_exceeds_the_ceiling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point: refused up front, not aborted 392 cells in."""
    assert (
        north_star_cli.main(
            [*_small_grid_args(tmp_path, tag="refuse"), "--dry-run", "--max-tokens", "10"]
        )
        == 1
    )
    captured = capsys.readouterr()
    # BOTH numbers, so the operator can size the fix without re-running.
    assert "10" in captured.err
    cells = north_star_cli._build_cells(
        _parse(_small_grid_args(tmp_path, tag="refuse"))
    )
    projected = len(cells) * NORTH_STAR_CELL_TOKEN_ESTIMATE.total_tokens
    assert str(projected) in captured.err
    assert "refusing to start" in captured.err
    # And the projection lines still printed before the refusal.
    assert "projected wall clock:" in captured.out
    assert f"projected {projected} tokens" in captured.out


def test_dry_run_derives_the_ceiling_from_max_spend_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The configuration CI actually runs.

    ``evals.yml`` always passes ``--max-spend`` and leaves ``--max-tokens``
    blank by default, so the derived branch — not the flag — is the
    production path. Every other ``main()`` test here names the ceiling
    explicitly, which would leave that path proven only in unit tests of
    :func:`resolve_token_ceiling`.
    """
    args = [
        *_small_grid_args(tmp_path, tag="derived"),
        "--dry-run",
    ]
    args[args.index("--max-spend") + 1] = "75"
    assert north_star_cli.main(args) == 0
    out = capsys.readouterr().out
    assert "derived from --max-spend $75.00" in out
    expected, _ = north_star_cli.resolve_token_ceiling(_parse(args))
    assert f"token ceiling: {expected}" in out


def test_dry_run_makes_no_paid_call_on_the_refusal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _exploding(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused dry run must never run a cell")

    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _exploding)
    assert (
        north_star_cli.main(
            [*_small_grid_args(tmp_path, tag="nopay"), "--dry-run", "--max-tokens", "10"]
        )
        == 1
    )


# ---------------------------------------------------------------------------
# AC3/AC4: the measured estimate, and the workflow wiring
# ---------------------------------------------------------------------------


def test_the_per_cell_estimate_is_the_measured_one() -> None:
    """2,018,960 tokens over 392 cells = 5,150/cell (run 35200779015).
    Within 2x of reality is the bar athenaeum#1754 set; the old 24,000 was
    4.7x over."""
    assert NORTH_STAR_CELL_TOKEN_ESTIMATE.total_tokens == 5_150
    measured_per_cell = 2_018_960 / 392
    assert 0.5 < NORTH_STAR_CELL_TOKEN_ESTIMATE.total_tokens / measured_per_cell < 2.0


def expected_full_grid_cells(probe_count: int) -> int:
    """Cells a full-scale dry run produces for *probe_count* probes.

    Mirrors ``north_star_cli._build_cells``'s multiplication (probes *
    corpus_scales * arms, with the ``--replicates`` default being a single
    replicate so it drops out of the product) without invoking the CLI, so
    the ``cells=`` literal pinned below can be DERIVED from the current
    probe count instead of hand-typed after every probe-count change --
    Quine review of PR athenaeum#1808 (athenaeum#1779). The hand-typed
    literal assertion stays alongside this as a second, independent pin: a
    change to ``DEFAULT_CORPUS_SCALES``/``DEFAULT_ARMS`` (not just probe
    count) would move this helper's output without anyone noticing unless
    the literal below still has to be updated to match.
    """
    return (
        probe_count
        * len(north_star_cli.DEFAULT_CORPUS_SCALES)
        * len(north_star_cli.DEFAULT_ARMS)
    )


def test_the_full_grid_dry_run_prices_at_the_measured_mix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pins ``main``'s ``price_grid(per_cell=...)`` argument, which no other
    test reaches: 1584 cells at 4,300 in / 850 out on Haiku 4.5 ($1/$5 per
    MTok) is 1584 * $0.00855 = $13.54. Reverting to the shared
    ``DEFAULT_CELL_TOKEN_ESTIMATE`` mix would price the same grid at a much
    higher figure and fail this band (Quine review of PR athenaeum#1757).
    Repinned from 1392 to 1584 cells by athenaeum#1779's four new
    single_hop probes on the long-page tier -- the full grid's cell count
    is `probes * scales * arms`, so a probe-count change always shifts it,
    same class of expected repin as `Corpus.fingerprint()`. The expected
    count is now cross-checked against :func:`expected_full_grid_cells`,
    derived from the live probe count, rather than trusted on the literal
    alone."""
    assert (
        expected_full_grid_cells(len(north_star_cli.DEFAULT_PROBES)) == 1584
    ), "expected_full_grid_cells drifted from the pinned literal below -- update both together"
    assert (
        north_star_cli.main(
            [
                "--scale",
                "full",
                "--dry-run",
                "--max-spend",
                "50",
                "--max-tokens",
                "10000000",
                "--out-dir",
                str(tmp_path / "m"),
                "--store",
                str(tmp_path / "s.jsonl"),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "cells=1584" in out
    priced = float(out.split("estimated=$")[1].split()[0])
    assert 13.50 < priced < 13.60, out


def test_the_estimate_names_its_source_run() -> None:
    source = (Path(north_star_cli.__file__).parent / "containment.py").read_text(encoding="utf-8")
    marker = source.index("NORTH_STAR_CELL_TOKEN_ESTIMATE = ")
    provenance = source[max(0, marker - 1400) : marker]
    assert "35200779015" in provenance


def test_evals_yml_wires_the_max_tokens_input_to_the_flag() -> None:
    repo_root = Path(north_star_cli.__file__).resolve().parents[2]
    evals_yml = (repo_root / ".github" / "workflows" / "evals.yml").read_text(encoding="utf-8")
    assert "north_star_max_tokens:" in evals_yml
    assert "NORTH_STAR_MAX_TOKENS: ${{ github.event.inputs.north_star_max_tokens }}" in evals_yml
    assert 'MAX_TOKENS_FLAG=(--max-tokens "$NORTH_STAR_MAX_TOKENS")' in evals_yml
    assert '"${MAX_TOKENS_FLAG[@]}"' in evals_yml
    # Blank input must leave the flag off entirely, so the CLI derives the
    # ceiling from --max-spend rather than the workflow pinning a second one.
    assert 'if [ -n "${NORTH_STAR_MAX_TOKENS:-}" ]; then' in evals_yml
    # AC4: the new input must not have grown the job a push trigger. The
    # north-star job stays dispatch-only and opt-in.
    assert (
        "github.event_name == 'workflow_dispatch' "
        "&& github.event.inputs.north_star == 'true'"
    ) in evals_yml
