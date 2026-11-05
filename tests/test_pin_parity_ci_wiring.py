# SPDX-License-Identifier: Apache-2.0
"""The `pin-parity` CI job is wired, and wired early (athenaeum#2324).

Mirrors `tests/test_clean_venv_ci_wiring.py`'s shape: a job that exists but
is missing from `CI Required`'s `needs:` blocks nothing, and the whole point
of this job (catch a pyproject.toml/requirements-ci.lock exact-pin mismatch
BY NAME before any of `test`/`types`/`clean-venv` pay for `pip install`) is
silently defeated if it ever grows an install step of its own, or is wired to
depend on one of those heavier jobs. Neither regression shows up as a test
failure anywhere else, so both are asserted here, sliced out of the real
`if:`/`run:` values rather than substring-matched against the whole file (a
structural workflow test that only matches its own rationale comment proves
nothing -- see athenaeum's own retro on that failure mode).

Offline and hermetic: reads the committed workflow file, nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JOB = "pin-parity"


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
    # Without both halves -- the `needs:` edge AND the aggregate's own
    # check -- a red pin-parity job leaves `CI Required` green and merges
    # anyway, which is exactly the silent-failure shape athenaeum#2324 is
    # about.
    ci_required = workflow["jobs"]["ci-required"]
    assert JOB in ci_required["needs"]
    script = _run_script(ci_required)
    assert "$PIN_PARITY_RESULT" in script
    # Sliced from the real aggregation loop's pair list, not a substring
    # match against the whole job (which would also match a rationale
    # comment quoting the same text).
    for_line = next(
        line for line in script.splitlines() if line.strip().startswith("for pair in")
    )
    assert "pin-parity:$PIN_PARITY_RESULT" in for_line


def test_the_attestation_is_not_published_without_it(workflow: dict[str, Any]) -> None:
    # An attestation says "this tree was tested green"; a mismatched pin
    # must not be attestable as green.
    publish = workflow["jobs"]["publish_attestation"]
    assert JOB in publish["needs"]
    assert "needs.pin-parity.result == 'success'" in publish["if"]


def test_it_runs_before_any_install_step(job: dict[str, Any]) -> None:
    # The entire point of a standalone, pre-install check (athenaeum#2324):
    # it must catch the mismatch BEFORE the `pip install` that died in
    # athenaeum#1922/#2284, not after one. A `pip install` step here would
    # mean the job stopped being cheap-and-early.
    script = _run_script(job)
    assert "pip install" not in script
    assert "pip" not in script, "pin-parity must not invoke pip at all"


def test_it_does_not_depend_on_the_expensive_jobs(job: dict[str, Any]) -> None:
    # Depending on `test`/`types`/`clean-venv` would make this job wait
    # behind the exact install step it exists to front-run.
    assert job["needs"] == ["check_duplicate"]


def test_it_invokes_the_real_checker_script(job: dict[str, Any]) -> None:
    script = _run_script(job)
    assert "scripts/check_exact_pin_parity.py" in script
    assert (REPO_ROOT / "scripts" / "check_exact_pin_parity.py").is_file()
