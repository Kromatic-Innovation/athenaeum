# SPDX-License-Identifier: Apache-2.0
"""Pytest wiring for the live-API eval suite (issue #331).

Adds three knobs:

- ``--record`` (CLI flag / ``ATHENAEUM_EVAL_RECORD=1`` env) — each eval
  case writes its raw response body to a fixture under
  ``tests/fixtures/recorded/<layer>/<case_id>.json``.
- ``--eval-summary=PATH`` — override the JSON summary output path (default
  ``eval-summary.json`` at repo root). The evals.yml workflow uploads
  this as a build artifact.
- Session-scoped :class:`EvalSession` fixture — accumulates per-case
  outcomes + ``TokenUsage`` for the run summary + budget guard.

The budget guard runs at session teardown: if the run's cumulative
input+output tokens exceed :data:`EVAL_TOKEN_CEILING`, the session fails
loudly rather than silently burning through spend on a golden set that
has grown unnoticed.
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


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("evals", "live-API eval suite (issue #331)")
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
    # Hard budget guard (issue #331 "hard budget guard" acceptance) — a
    # runaway golden set should fail the run loudly, not silently spend.
    total_tokens = session.input_tokens + session.output_tokens
    assert total_tokens <= EVAL_TOKEN_CEILING, (
        f"eval run exceeded token ceiling "
        f"({total_tokens} > {EVAL_TOKEN_CEILING}) — "
        "shrink the golden set or raise EVAL_TOKEN_CEILING deliberately"
    )
