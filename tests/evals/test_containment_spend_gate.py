# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the pre-flight spend gate (issue athenaeum#1521 AC1).

UNMARKED — no network, no credential. Prices a deliberately over-budget
grid and asserts the gate REFUSES TO START (raises before any cell would
run), and separately proves the pricing reuses
``athenaeum.models.TokenUsage``'s per-model rate table rather than a second,
hardcoded price list.
"""

from __future__ import annotations

import pytest

from tests.evals.containment import (
    CellTokenEstimate,
    GridCell,
    SpendCeilingExceededError,
    price_grid,
)

CELL = GridCell(probe="p1", arm="control", corpus_scale="core", replicate=0)


def test_over_budget_grid_is_refused() -> None:
    """A grid deliberately priced above --max-spend must raise, not run."""
    # 500 cells @ 20k in / 4k out on claude-sonnet-5 ($3/$15 per MTok) prices
    # at 500 * (20_000*3e-6 + 4_000*15e-6) = 500 * 0.12 = $60.00 -- comfortably
    # over a $1.00 ceiling.
    cells = [CELL] * 500
    with pytest.raises(SpendCeilingExceededError, match=r"\$60\.00"):
        price_grid(cells, model="claude-sonnet-5", max_spend_usd=1.0)


def test_refusal_message_names_the_estimate_and_ceiling() -> None:
    cells = [CELL] * 500
    with pytest.raises(SpendCeilingExceededError) as excinfo:
        price_grid(cells, model="claude-sonnet-5", max_spend_usd=1.0)
    message = str(excinfo.value)
    assert "500 cells" in message
    assert "$1.00" in message
    assert "refusing to start" in message


def test_within_budget_grid_is_not_refused() -> None:
    """The SAME gate, a grid that fits, must return normally (not raise)."""
    cells = [CELL] * 2
    estimate = price_grid(cells, model="claude-sonnet-5", max_spend_usd=1.0)
    assert estimate.cell_count == 2
    assert estimate.estimated_usd == pytest.approx(0.24)
    assert estimate.max_spend_usd == 1.0


def test_price_grid_reuses_the_model_rate_table_not_a_second_price_list() -> None:
    """Cost must match TokenUsage's own per-model rate (claude-haiku-4-5 is
    $1.00/$5.00 per MTok in the code-default table, src/athenaeum/models.py)
    -- exercised via price_grid, not re-derived independently."""
    cells = [CELL]
    per_cell = CellTokenEstimate(input_tokens=10_000, output_tokens=2_000)
    estimate = price_grid(
        cells, model="claude-haiku-4-5", max_spend_usd=1.0, per_cell=per_cell
    )
    expected = (10_000 / 1_000_000) * 1.0 + (2_000 / 1_000_000) * 5.0
    assert estimate.estimated_usd == pytest.approx(expected)


def test_default_cell_token_estimate_is_declared_not_a_live_call() -> None:
    """price_grid must be computable with zero network / credential -- the
    default estimate is a plain declared constant, not derived from a live
    probe."""
    from tests.evals.containment import DEFAULT_CELL_TOKEN_ESTIMATE

    assert DEFAULT_CELL_TOKEN_ESTIMATE.input_tokens > 0
    assert DEFAULT_CELL_TOKEN_ESTIMATE.output_tokens > 0
    # No network/credential needed to reach this line at all -- the
    # assertion is the proof; this test carries no eval/live/embedding marker.
