# SPDX-License-Identifier: Apache-2.0
"""``--max-tokens``: one knob for dollars and tokens (issue athenaeum#1754).

Run 35200779015 was dispatched with ``--max-spend 75`` and died at 392 of
1392 cells on ``rollout run exceeded token ceiling (2018960 > 2000000)`` --
about $2 spent against $75 authorized. The USD ceiling the operator set had
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
    """Spends 2,000 tokens per group -- over a 1-token ceiling, under a large one."""
    session.observe_response(
        model,
        SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=1000)),
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


# ---------------------------------------------------------------------------
# AC1b: the flag reaches a real run
# ---------------------------------------------------------------------------


def test_a_one_token_ceiling_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(north_star_cli, "run_probe_all_arms", _burning_stub)
    assert north_star_cli.main([*_small_grid_args(tmp_path, tag="tiny"), "--max-tokens", "1"]) == 1


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
    north_star_cli.main([*_small_grid_args(tmp_path, tag="msg"), "--max-tokens", "1"])
    report = next((tmp_path / "measurements-msg").glob("north-star-*.md")).read_text(
        encoding="utf-8"
    )
    assert "--max-tokens" in report
    assert "exceeded token ceiling (2000 > 1)" in report


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
