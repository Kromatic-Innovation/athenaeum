# SPDX-License-Identifier: Apache-2.0
"""The rollout token ceiling: separately named, separately sessioned (athenaeum#1521).

The token-ceiling collision this exists to avoid: ``tests/evals/harness.py``'s
``EVAL_TOKEN_CEILING`` (250,000) is asserted at ``EvalSession`` teardown over
the ACCUMULATED component-eval suite (detector/resolver/recall/classify/
merge/write-tier-compare — all single-call-shaped). Four arms x probes x
replicates of AGENT ROLLOUTS is a completely different order of magnitude
and would blow that ceiling immediately if it accumulated into the same
session — and the first grid run would then die on a budget guard rather
than on an actual finding.

The fix is not a bigger ``EVAL_TOKEN_CEILING`` (that would just make the
component-eval guard looser for everyone) — it is that rollout usage must
never reach that accumulator at all. This module is deliberately tiny: a
separately-named ceiling constant plus one assertion helper, factored out
of ``tests/evals/conftest.py``'s ``eval_session`` teardown shape (see that
fixture) so the SAME assertion the ``rollout_session`` pytest fixture runs
at teardown is also directly callable from an offline test — proving the
separation property does not require actually running a live rollout.

Deliberately does NOT touch ``tests/evals/harness.py``: ``EvalSession`` is
reused UNMODIFIED as the accumulator type for a rollout session too (the
class already does exactly the token bookkeeping a rollout session needs
— see ``harness.EvalSession.observe_response``/``add``); what makes a
rollout session "its own session" is that the caller constructs a SEPARATE
``EvalSession()`` INSTANCE for rollouts and never lets it or the component
``eval_session`` fixture's instance share state. Two instances of the same
class are still two sessions.
"""

from __future__ import annotations

from tests.evals.harness import EvalSession

#: Separately named from ``harness.EVAL_TOKEN_CEILING`` (250,000) on purpose
#: — the two must never be confused for the same budget. Sized for
#: multi-turn agent rollouts, not single component-eval calls: rollouts are
#: orders of magnitude more expensive per case, so a shared ceiling would
#: either be too loose for component evals or too tight for a single arm's
#: worth of rollouts. Like ``EVAL_TOKEN_CEILING``, this does NOT auto-scale
#: with the grid — a grid priced over budget must be caught by the
#: pre-flight spend gate (``tests/evals/containment.py``'s ``price_grid``)
#: BEFORE it starts; this ceiling is the in-run backstop for when the
#: actual usage diverges from the pre-flight estimate.
ROLLOUT_TOKEN_CEILING = 2_000_000


def assert_rollout_ceiling(session: EvalSession) -> None:
    """Assert *session*'s accumulated tokens are within :data:`ROLLOUT_TOKEN_CEILING`.

    Mirrors ``tests/evals/conftest.py``'s ``eval_session`` teardown shape
    exactly (same ``input_tokens + output_tokens`` total, same "fail loudly
    rather than silently overspend" intent) but against the separate
    rollout ceiling and — critically — against whatever *session* instance
    the caller passes, never ``harness``'s own component-eval accumulator.
    """
    total_tokens = session.input_tokens + session.output_tokens
    assert total_tokens <= ROLLOUT_TOKEN_CEILING, (
        f"rollout run exceeded token ceiling ({total_tokens} > "
        f"{ROLLOUT_TOKEN_CEILING}) — shrink the grid, the --scale tier, or "
        "raise ROLLOUT_TOKEN_CEILING deliberately"
    )
