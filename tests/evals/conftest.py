# SPDX-License-Identifier: Apache-2.0
"""Pytest wiring for the live-API eval suite (issue athenaeum#331).

Adds four knobs:

- ``--record`` (CLI flag / ``ATHENAEUM_EVAL_RECORD=1`` env) — each eval
  case writes its raw response body to a fixture under
  ``tests/fixtures/recorded/<layer>/<case_id>.json``.
- ``--eval-summary=PATH`` — override the JSON summary output path (default
  ``eval-summary.json`` at repo root). The evals.yml workflow uploads
  this as a build artifact.
- ``--rollout-summary=PATH`` — the same override, for
  ``rollout_session`` below (default ``rollout-summary.json`` at repo
  root). Deliberately mirrors ``--eval-summary`` rather than inventing a
  second mechanism (issue athenaeum#1521 PR review, finding 2).
- Session-scoped :class:`EvalSession` fixture — accumulates per-case
  outcomes + ``TokenUsage`` for the run summary + budget guard.

The budget guard runs at session teardown: if the run's cumulative
input+output tokens exceed :data:`EVAL_TOKEN_CEILING`, the session fails
loudly rather than silently burning through spend on a golden set that
has grown unnoticed.

Also adds a SEPARATE ``rollout_session`` fixture (issue athenaeum#1521) for
agent-rollout token usage, which must never accumulate into the
``eval_session``/``EVAL_TOKEN_CEILING`` guard above — see
``tests/evals/rollout_session.py`` for why. It is its own
:class:`EvalSession` instance, guarded at teardown by its own
``ROLLOUT_TOKEN_CEILING``, and is the fixture a future ``pytest.mark.rollout``
eval would depend on instead of ``eval_session``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.evals.harness import (
    EVAL_TOKEN_CEILING,
    REPO_ROOT,
    EvalSession,
)
from tests.evals.rollout_session import assert_rollout_ceiling


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("evals", "live-API eval suite (issue athenaeum#331)")
    group.addoption(
        "--record",
        action="store_true",
        default=False,
        help=(
            "Persist each eval case's response to tests/fixtures/recorded/ "
            "(also enabled by ATHENAEUM_EVAL_RECORD=1)."
        ),
    )
    group.addoption(
        "--eval-summary",
        action="store",
        default=None,
        help=(
            "Override the JSON summary output path "
            "(default: eval-summary.json at repo root)."
        ),
    )
    group.addoption(
        "--rollout-summary",
        action="store",
        default=None,
        help=(
            "Override the rollout-session JSON summary output path "
            "(default: rollout-summary.json at repo root)."
        ),
    )


def _record_enabled(config: pytest.Config) -> bool:
    if config.getoption("--record"):
        return True
    return os.environ.get("ATHENAEUM_EVAL_RECORD") == "1"


@pytest.fixture(scope="session")
def eval_record(request: pytest.FixtureRequest) -> bool:
    """Whether the eval run should persist responses as fixtures."""
    return _record_enabled(request.config)


@pytest.fixture(scope="session")
def eval_session(request: pytest.FixtureRequest) -> Any:
    """Session-scoped accumulator; teardown emits the JSON summary + guard."""
    session = EvalSession()
    yield session
    # Emit the summary artifact BEFORE the budget assertion so a failing
    # run still leaves the raw per-case results on disk for triage.
    summary_path_opt = request.config.getoption("--eval-summary")
    summary_path = (
        Path(summary_path_opt)
        if summary_path_opt
        else REPO_ROOT / "eval-summary.json"
    )
    session.emit_summary(summary_path)
    # Hard budget guard (issue athenaeum#331 "hard budget guard" acceptance) — a
    # runaway golden set should fail the run loudly, not silently spend.
    total_tokens = session.input_tokens + session.output_tokens
    assert total_tokens <= EVAL_TOKEN_CEILING, (
        f"eval run exceeded token ceiling "
        f"({total_tokens} > {EVAL_TOKEN_CEILING}) — "
        "shrink the golden set or raise EVAL_TOKEN_CEILING deliberately"
    )


@pytest.fixture(scope="session")
def rollout_session(request: pytest.FixtureRequest) -> Any:
    """Session-scoped accumulator for agent-rollout token usage (athenaeum#1521).

    A SEPARATE :class:`EvalSession` instance from ``eval_session`` above —
    never shares state with it — so rollout usage cannot accumulate into
    ``EVAL_TOKEN_CEILING``'s teardown assert. Emits its own summary artifact
    (default ``rollout-summary.json`` at repo root, overridable via
    ``--rollout-summary`` — mirrors ``eval_session``'s ``--eval-summary``
    configurability exactly rather than hardcoding the path) and is guarded
    by its own ``ROLLOUT_TOKEN_CEILING`` via
    :func:`tests.evals.rollout_session.assert_rollout_ceiling`.
    """
    session = EvalSession()
    yield session
    summary_path_opt = request.config.getoption("--rollout-summary")
    summary_path = (
        Path(summary_path_opt)
        if summary_path_opt
        else REPO_ROOT / "rollout-summary.json"
    )
    session.emit_summary(summary_path)
    assert_rollout_ceiling(session)
