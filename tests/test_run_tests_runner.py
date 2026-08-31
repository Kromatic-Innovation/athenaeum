# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/run-tests.sh (athenaeum#1105 — make "the tests passed"
a checkable claim: a pipefail-safe runner entrypoint + a committed
known-fail baseline keyed on nodeids, diffed on every run).

Exercised against a throwaway scratch pytest project (never this repo's
own suite — the point is to test the runner's diff/exit-code logic in
isolation, with a fast, controlled failing test rather than depending on
this host's own 40-item environment-bound baseline).

Covers AC4 directly:
  - a newly-introduced failing test (not in the baseline) is caught by the
    diff and makes the runner exit non-zero;
  - a piped invocation of the runner still reports a non-zero exit code
    (via `${PIPESTATUS[0]}`, since a bare `$?` after an external pipe is a
    property of the calling shell, not something an invoked script can
    override — see the runner's own header comment).
Plus controls proving the baseline mechanism actually discriminates
(a nodeid IN the baseline does not fail the gate) and that
`--update-baseline` records the diff it applies.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
RUNNER = REPO_ROOT / "scripts" / "run-tests.sh"


def _make_scratch_project(tmp_path: Path, *, failing: bool) -> Path:
    """A minimal, self-contained pytest project with one test, independent
    of athenaeum's own suite/deps/conftest so it runs in milliseconds."""
    project = tmp_path / "scratch"
    project.mkdir()
    (project / "tests").mkdir()
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
    assertion = "assert False" if failing else "assert True"
    (project / "tests" / "test_sample.py").write_text(
        f"def test_something():\n    {assertion}\n"
    )
    return project


def _run_runner(
    project: Path, *extra_args: str, baseline: Path | None = None
) -> subprocess.CompletedProcess[str]:
    baseline = baseline if baseline is not None else project / "known-fail-baseline.txt"
    return subprocess.run(
        ["bash", str(RUNNER), "--baseline", str(baseline), "tests/", *extra_args],
        cwd=project,
        capture_output=True,
        text=True,
        # Use the interpreter running *this* test (which has pytest
        # installed) rather than the runner's own `python3` default,
        # which on a bare system PATH may have no pytest at all.
        env={**os.environ, "ATHENAEUM_PYTHON": sys.executable},
    )


def test_runner_exists_and_is_executable() -> None:
    assert RUNNER.is_file()
    assert RUNNER.stat().st_mode & 0o111


def test_runner_passes_on_clean_suite(tmp_path: Path) -> None:
    project = _make_scratch_project(tmp_path, failing=False)
    result = _run_runner(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unrecognized:     0" in result.stdout


def test_runner_catches_a_newly_introduced_failure(tmp_path: Path) -> None:
    """AC4, first half: a failing nodeid absent from the (empty) baseline
    is caught by the diff and fails the gate, even though nothing was ever
    recorded about it before."""
    project = _make_scratch_project(tmp_path, failing=True)
    baseline = project / "known-fail-baseline.txt"
    baseline.write_text("# empty baseline -- nothing known-failing yet\n")

    result = _run_runner(project, baseline=baseline)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "UNRECOGNIZED" in result.stdout
    assert "tests/test_sample.py::test_something" in result.stdout


def test_runner_does_not_fail_on_a_baselined_failure(tmp_path: Path) -> None:
    """Control for the previous test: the exact same failing test passes
    the gate once its nodeid is present in the baseline -- proving the
    mechanism discriminates known-and-tracked failures from new ones,
    rather than failing on any failure unconditionally."""
    project = _make_scratch_project(tmp_path, failing=True)
    baseline = project / "known-fail-baseline.txt"
    baseline.write_text("tests/test_sample.py::test_something\n")

    result = _run_runner(project, baseline=baseline)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "known (baseline): 1" in result.stdout
    assert "unrecognized:     0" in result.stdout


def test_update_baseline_records_the_added_nodeid(tmp_path: Path) -> None:
    project = _make_scratch_project(tmp_path, failing=True)
    baseline = project / "known-fail-baseline.txt"
    baseline.write_text("# nothing yet\n")

    result = _run_runner(project, "--update-baseline", baseline=baseline)
    assert "baseline updated" in result.stdout
    assert "tests/test_sample.py::test_something" in result.stdout
    assert "tests/test_sample.py::test_something" in baseline.read_text()

    # Re-running without --update-baseline now passes: the baseline caught up.
    rerun = _run_runner(project, baseline=baseline)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr


def test_missing_pytest_is_reported_as_a_failure_not_masked(tmp_path: Path) -> None:
    """Instance 1 from the issue: pytest itself absent (or a collection
    crash) must never look like a clean, baseline-only run just because it
    produced zero `FAILED` nodeids."""
    project = _make_scratch_project(tmp_path, failing=False)
    baseline = project / "known-fail-baseline.txt"
    baseline.write_text("# empty\n")

    result = subprocess.run(
        ["bash", str(RUNNER), "--baseline", str(baseline), "tests/"],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "ATHENAEUM_PYTHON": "athenaeum-python-does-not-exist"},
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "no FAILED nodeids parsed" in (result.stdout + result.stderr)


def test_piped_invocation_still_reports_a_nonzero_exit_code_via_pipestatus(
    tmp_path: Path,
) -> None:
    """AC4, second half. Recreates the issue's Instance 1 shape (the
    runner's output piped through another command) and shows the real
    exit code is still recoverable — via `${PIPESTATUS[0]}`, which is the
    bash-correct way to read a non-last command's status out of a pipeline
    (a bare `$?` after `cmd | tee log` is `tee`'s status by construction,
    in any shell, and no program on either end of that pipe can change
    that from the inside — seen directly in this test's second assertion
    below, which is the ORIGINAL bug reproduced deliberately as a
    negative control)."""
    project = _make_scratch_project(tmp_path, failing=True)
    baseline = project / "known-fail-baseline.txt"
    baseline.write_text("# empty -- the sample failure is unrecognized\n")

    script = (
        f'bash "{RUNNER}" --baseline "{baseline}" tests/ | tee "{tmp_path}/piped.log" '
        '>/dev/null; echo "PIPESTATUS0=${PIPESTATUS[0]}"'
    )
    env = {**os.environ, "ATHENAEUM_PYTHON": sys.executable}
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "PIPESTATUS0=1" in result.stdout, result.stdout + result.stderr

    # Negative control: a bare `$?` after the same pipe reports `tee`'s
    # status (0), not the runner's -- proving this is a real, external
    # shell mechanic the runner cannot suppress by itself, not a defect in
    # this script.
    bare_script = (
        f'bash "{RUNNER}" --baseline "{baseline}" tests/ | tee "{tmp_path}/piped2.log" '
        '>/dev/null; echo "BARE=$?"'
    )
    bare_result = subprocess.run(
        ["bash", "-c", bare_script],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "BARE=0" in bare_result.stdout, bare_result.stdout + bare_result.stderr
