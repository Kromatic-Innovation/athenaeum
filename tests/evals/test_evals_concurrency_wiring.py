# SPDX-License-Identifier: Apache-2.0
"""Offline proof that ``evals.yml``'s spend guard cannot cancel a token-free job.

Issue athenaeum#1918. ``evals.yml`` once carried a workflow-level
``concurrency`` block with ``cancel-in-progress: true``, justified purely by
Anthropic API spend. Since the 2026-09-16 operator decision both spending jobs
(``eval``, ``north-star``) are gated to ``workflow_dispatch``, so the only job a
push to ``main`` runs is ``embedding-suite`` -- which spends nothing. The guard
therefore protected no budget on a push, but it did put two runs of the same
sha in one group when ``main`` received two ref updates seconds apart, and the
newer run cancelled the older one mid-pytest. A ``cancelled`` conclusion is
indistinguishable from a real red to the weekly CI sweep, which filed it as a
failing-check chore.

These tests pin the corrected shape: the spend guard lives on the jobs that
spend, and ``embedding-suite`` is under no concurrency group at all, so a
``cancelled`` conclusion on it can only ever mean a genuine cancellation.

Parsed as YAML rather than matched as a substring on purpose -- a substring
assertion over a job body false-passes on the rationale comment that quotes the
same key, so the deletion this file exists to catch would slip through.

UNMARKED -- no network, no credential; reads the committed workflow file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "evals.yml"

#: The job that runs on a push to ``main`` and spends no API tokens.
TOKEN_FREE_JOB = "embedding-suite"

#: The jobs that make live Anthropic calls and therefore need the spend guard.
TOKEN_SPENDING_JOBS = ("eval", "north-star")


def _workflow() -> dict[str, Any]:
    assert EVALS_WORKFLOW.is_file(), f"expected workflow file not found: {EVALS_WORKFLOW}"
    loaded = yaml.safe_load(EVALS_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _jobs() -> dict[str, Any]:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def test_no_workflow_level_concurrency_group() -> None:
    """A workflow-level group would sweep the token-free job back in."""
    assert "concurrency" not in _workflow()


def test_token_free_job_is_under_no_concurrency_group() -> None:
    """``embedding-suite`` must never be cancellable by a sibling run."""
    job = _jobs()[TOKEN_FREE_JOB]
    assert "concurrency" not in job


def test_token_spending_jobs_keep_the_spend_guard() -> None:
    """The guard still collapses a double-dispatch, per spending job."""
    jobs = _jobs()
    groups = set()
    for name in TOKEN_SPENDING_JOBS:
        concurrency = jobs[name].get("concurrency")
        assert isinstance(concurrency, dict), f"{name} lost its spend guard"
        assert concurrency.get("cancel-in-progress") is True
        group = concurrency.get("group")
        assert isinstance(group, str) and group, f"{name} has no concurrency group"
        groups.add(group)
    assert len(groups) == len(TOKEN_SPENDING_JOBS), (
        "each spending job needs its own group, or one dispatch cancels the other"
    )


def test_only_the_token_free_job_runs_on_a_push() -> None:
    """The premise the scoping rests on: a push runs nothing that spends.

    If a spending job ever loses its ``workflow_dispatch``-only gate, a push to
    ``main`` starts costing tokens and the guard's placement must be revisited.
    """
    jobs = _jobs()
    for name in TOKEN_SPENDING_JOBS:
        condition = jobs[name].get("if")
        assert isinstance(condition, str), f"{name} must stay dispatch-gated"
        assert "github.event_name == 'workflow_dispatch'" in condition
    assert "if" not in jobs[TOKEN_FREE_JOB]
