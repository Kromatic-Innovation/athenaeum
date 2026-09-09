# SPDX-License-Identifier: Apache-2.0
"""Offline proof that nothing this issue adds runs in ci.yml, becomes a
required check, or is selected by evals.yml (issue athenaeum#1521 AC6).

UNMARKED — no network, no credential; reads the committed workflow/config
files directly (the "most direct way the repo already uses" — see
``tests/test_retrieval_golden_1420.py``'s docstring, which reasons about
the same `pyproject.toml` addopts/markers mechanism in prose; this module
asserts it mechanically instead).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pytest_ini_options() -> dict[str, object]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    result = data["tool"]["pytest"]["ini_options"]
    assert isinstance(result, dict)
    return result


def test_rollout_marker_is_registered() -> None:
    markers = _pytest_ini_options()["markers"]
    assert any(str(m).startswith("rollout:") for m in markers)


def test_default_addopts_deselects_rollout() -> None:
    addopts = str(_pytest_ini_options()["addopts"])
    assert "not rollout" in addopts
    # Same for its siblings, so a regression here would also be caught by
    # existing behaviour -- belt and suspenders on the one line that matters.
    assert "not eval" in addopts
    assert "not embedding" in addopts


def test_ci_yml_test_job_uses_default_addopts_no_marker_override() -> None:
    """ci.yml's `test` job must invoke plain `pytest tests/ ...` with no `-m`
    override -- that is what makes the default addopts (which excludes
    `rollout`, proven above) the actual selection CI runs, rather than an
    independent `-m` expression that could silently diverge from it."""
    ci_yml = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r"Run tests\s*\n\s*run:\s*(.+)", ci_yml)
    assert match, "could not find the ci.yml 'Run tests' step"
    command = match.group(1)
    assert "pytest tests/" in command
    assert " -m " not in command, (
        f"ci.yml's test step now overrides -m directly: {command!r} -- if it "
        "ever adds 'rollout' to a positive selection this test must fail"
    )


def test_ci_yml_never_mentions_rollout_or_containment() -> None:
    ci_yml = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "rollout" not in ci_yml
    assert "containment" not in ci_yml


def test_evals_yml_never_selects_rollout_marker() -> None:
    """evals.yml's `eval` job selects `-m eval` only, and `embedding-suite`
    selects `-m embedding` only -- a rollout-marked test must never be
    reachable from EITHER, since neither is the required CI gate but both
    ARE workflows that spend real money."""
    evals_yml = (REPO_ROOT / ".github" / "workflows" / "evals.yml").read_text(encoding="utf-8")
    assert "-m rollout" not in evals_yml
    assert "rollout" not in evals_yml
    assert "containment" not in evals_yml
