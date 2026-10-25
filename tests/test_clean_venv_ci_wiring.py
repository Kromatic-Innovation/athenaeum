# SPDX-License-Identifier: Apache-2.0
"""The `clean-venv` CI job is wired, and wired the way it has to be (athenaeum#2136).

A job that exists but is not in `CI Required`'s `needs:` blocks nothing, and a
job that quietly acquires `-c requirements-ci.lock` or the `[vector]` extra
answers a question some other job already answers. Both regressions are a
one-line edit away and neither shows up as a test failure anywhere else, so
they are asserted here — the same reason
`tests/evals/test_containment_ci_wiring.py` pins the eval/rollout split
mechanically instead of trusting a comment.

Offline and hermetic: reads the committed workflow file, nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JOB = "clean-venv"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def job(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert JOB in jobs, f"`{JOB}` job is missing from ci.yml"
    return jobs[JOB]


def _run_script(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_the_required_aggregate_gates_on_it(workflow: dict[str, Any]) -> None:
    # Without both halves -- the `needs:` edge AND the aggregate's own check --
    # a red clean-venv job leaves `CI Required` green and merges anyway.
    ci_required = workflow["jobs"]["ci-required"]
    assert JOB in ci_required["needs"]
    script = _run_script(ci_required)
    assert "$CLEAN_VENV_RESULT" in script
    assert "clean-venv:$CLEAN_VENV_RESULT" in script


def test_the_attestation_is_not_published_without_it(workflow: dict[str, Any]) -> None:
    # An attestation says "this tree was tested green", and a later identical
    # tree skips the whole matrix on the strength of it.
    publish = workflow["jobs"]["publish_attestation"]
    assert JOB in publish["needs"]
    assert "needs.clean-venv.result == 'success'" in publish["if"]


def test_it_installs_the_unconstrained_contributor_path(job: dict[str, Any]) -> None:
    script = _run_script(job)
    assert 'pip install -e ".[dev]"' in script
    # The two ways this job silently stops testing anything new.
    assert "requirements-ci.lock" not in script, (
        "the clean-venv job must resolve pyproject's PUBLISHED ranges; "
        "constraining it to the lock makes it a duplicate of the `test` job"
    )
    assert "[dev,vector]" not in script, (
        "the clean-venv job reproduces the `[dev]`-only contributor install; "
        "widening the extras deletes the environment-contract half of the job"
    )


def test_it_runs_the_three_checks_the_issue_asks_for(job: dict[str, Any]) -> None:
    script = _run_script(job)
    assert "scripts/check_declared_imports.py" in script  # undeclared, by name
    assert "--collect-only" in script  # the bar the httpx break failed
    assert "pytest -q" in script  # a smoke subset actually executes


def test_contract_skips_are_failures_in_this_job(job: dict[str, Any]) -> None:
    # "skipped" must not be able to mask "undeclared" (athenaeum#2136 AC2).
    smoke = [s for s in job["steps"] if "smoke" in (s.get("name") or "").lower()]
    assert len(smoke) == 1, "expected exactly one smoke-subset step"
    assert smoke[0].get("env", {}).get("ATHENAEUM_TEST_ENV_CONTRACT") == "strict"


def test_every_smoke_target_exists(job: dict[str, Any]) -> None:
    # A renamed or deleted module would otherwise make the step red at the
    # worst possible moment, with a pytest usage error rather than a diff.
    targets = [
        token
        for token in _run_script(job).split()
        if token.startswith("tests/") and token.endswith(".py")
    ]
    assert targets, "the smoke step names no test module"
    missing = [t for t in targets if not (REPO_ROOT / t).is_file()]
    assert not missing, f"smoke subset names modules that do not exist: {missing}"
