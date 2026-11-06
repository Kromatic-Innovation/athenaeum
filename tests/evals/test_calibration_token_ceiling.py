# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the athenaeum#2343 calibration-ceiling resize.

UNMARKED -- no network, no credential. Constructs the accumulator object
directly (never through pytest's own fixture machinery, mirroring
tests/evals/test_rollout_ceiling_separation.py's approach), so this proves
both acceptance criteria without ever dispatching the live calibration
sweep:

1. ``CALIBRATION_TOKEN_CEILING`` has at least 1.5x headroom over the
   documented (machine-readable) upper estimate,
   ``CALIBRATION_TOKEN_UPPER_ESTIMATE``.
2. The harness's existing budget-error mechanism -- the same
   ``assert total_tokens <= CALIBRATION_TOKEN_CEILING`` shape the
   ``calibration_session`` fixture runs at teardown
   (``tests/evals/conftest.py``) -- fires when a synthetic token counter
   crosses the ceiling. This test reuses that mechanism rather than
   inventing a new one: it drives the same ``EvalSession`` accumulator the
   fixture wraps and asserts the identical inequality, so a change to one
   cannot silently diverge from the other.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_llm_response, make_llm_usage
from tests.evals.harness import (
    CALIBRATION_TOKEN_CEILING,
    CALIBRATION_TOKEN_UPPER_ESTIMATE,
    EvalSession,
)


def _fake_response(input_tokens: int, output_tokens: int = 0) -> object:
    return make_llm_response(
        "stub", usage=make_llm_usage(input_tokens=input_tokens, output_tokens=output_tokens)
    )


def _assert_calibration_ceiling(session: EvalSession) -> None:
    """Mirrors tests/evals/conftest.py's ``calibration_session`` fixture
    teardown assert verbatim -- the harness's actual budget-error
    mechanism for the calibration sweep -- without needing to run that
    fixture through pytest's own machinery."""
    total_tokens = session.input_tokens + session.output_tokens
    assert total_tokens <= CALIBRATION_TOKEN_CEILING, (
        f"calibration sweep exceeded its token ceiling "
        f"({total_tokens} > {CALIBRATION_TOKEN_CEILING}) -- re-measure and "
        "raise CALIBRATION_TOKEN_CEILING deliberately (never EVAL_TOKEN_CEILING)"
    )


def test_ceiling_has_at_least_1_5x_headroom_over_documented_estimate() -> None:
    assert isinstance(CALIBRATION_TOKEN_CEILING, int)
    assert isinstance(CALIBRATION_TOKEN_UPPER_ESTIMATE, int)
    assert CALIBRATION_TOKEN_CEILING >= 1.5 * CALIBRATION_TOKEN_UPPER_ESTIMATE, (
        f"CALIBRATION_TOKEN_CEILING ({CALIBRATION_TOKEN_CEILING}) must stay at least "
        f"1.5x CALIBRATION_TOKEN_UPPER_ESTIMATE ({CALIBRATION_TOKEN_UPPER_ESTIMATE}) "
        "so the ceiling stays a safety net, not a trip-wire"
    )


def test_ceiling_passes_under_the_documented_upper_estimate() -> None:
    session = EvalSession()
    session.observe_response("claude-sonnet-5", _fake_response(CALIBRATION_TOKEN_UPPER_ESTIMATE))
    _assert_calibration_ceiling(session)  # must not raise


def test_ceiling_fires_when_synthetic_counter_crosses_it() -> None:
    session = EvalSession()
    session.observe_response("claude-sonnet-5", _fake_response(CALIBRATION_TOKEN_CEILING + 1))
    with pytest.raises(AssertionError, match="calibration sweep exceeded its token ceiling"):
        _assert_calibration_ceiling(session)
