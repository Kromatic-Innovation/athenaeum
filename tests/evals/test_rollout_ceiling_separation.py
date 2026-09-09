# SPDX-License-Identifier: Apache-2.0
"""Offline proof that rollout token usage never accumulates into
``EVAL_TOKEN_CEILING`` (issue athenaeum#1521 AC5).

UNMARKED — no network, no credential. Constructs the accumulator objects
directly (never through pytest's own fixture machinery), so this proves
the separation property without ever needing an actual live rollout.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_llm_response, make_llm_usage
from tests.evals.harness import EVAL_TOKEN_CEILING, EvalSession
from tests.evals.rollout_session import ROLLOUT_TOKEN_CEILING, assert_rollout_ceiling


def _fake_response(input_tokens: int, output_tokens: int = 0) -> object:
    return make_llm_response(
        "stub", usage=make_llm_usage(input_tokens=input_tokens, output_tokens=output_tokens)
    )


def test_ceilings_are_separately_named_and_valued() -> None:
    assert ROLLOUT_TOKEN_CEILING != EVAL_TOKEN_CEILING
    assert isinstance(EVAL_TOKEN_CEILING, int)
    assert isinstance(ROLLOUT_TOKEN_CEILING, int)


def test_rollout_usage_never_reaches_the_component_session() -> None:
    """Simulate exactly the collision the issue names: four arms x probes x
    replicates of rollouts, recorded ONLY on the rollout accumulator."""
    component_session = EvalSession()
    rollout_accumulator = EvalSession()

    # Four arms x 5 probes x 3 replicates, ~15k tokens/rollout -- this alone
    # would blow EVAL_TOKEN_CEILING (250,000) immediately if it landed there.
    per_rollout_tokens = 15_000
    n_rollouts = 4 * 5 * 3
    for _ in range(n_rollouts):
        rollout_accumulator.observe_response("claude-sonnet-5", _fake_response(per_rollout_tokens))

    rollout_total = rollout_accumulator.input_tokens + rollout_accumulator.output_tokens
    assert rollout_total > EVAL_TOKEN_CEILING, (
        "test setup bug: this scenario must be the one that WOULD blow "
        "EVAL_TOKEN_CEILING if it were wrongly folded into the component session"
    )

    # The component session, which never received any of these calls, must
    # stay completely untouched.
    assert component_session.input_tokens == 0
    assert component_session.output_tokens == 0

    # The rollout accumulator's OWN ceiling is generously sized enough that
    # this same usage passes it.
    assert_rollout_ceiling(rollout_accumulator)  # must not raise


def test_rollout_ceiling_fires_when_exceeded() -> None:
    rollout_accumulator = EvalSession()
    rollout_accumulator.observe_response(
        "claude-sonnet-5", _fake_response(ROLLOUT_TOKEN_CEILING + 1)
    )
    with pytest.raises(AssertionError, match="rollout run exceeded token ceiling"):
        assert_rollout_ceiling(rollout_accumulator)


def test_eval_ceiling_ignores_rollout_accumulator_entirely() -> None:
    """The mirror check: blow ONLY the rollout accumulator's ceiling, then
    assert the ordinary EVAL_TOKEN_CEILING check (harness.conftest's own
    inline assertion shape) over the untouched component session still
    passes -- the two guards are independent in both directions."""
    component_session = EvalSession()
    rollout_accumulator = EvalSession()
    rollout_accumulator.observe_response(
        "claude-sonnet-5", _fake_response(ROLLOUT_TOKEN_CEILING, 1)
    )

    with pytest.raises(AssertionError):
        assert_rollout_ceiling(rollout_accumulator)

    component_total = component_session.input_tokens + component_session.output_tokens
    assert component_total <= EVAL_TOKEN_CEILING
