# SPDX-License-Identifier: Apache-2.0
"""Regression for athenaeum#2729: fixture ``git commit`` ignores ambient git config.

The hostile environment (``GIT_CONFIG_COUNT=1`` forcing ``core.hooksPath`` to a
directory whose ``pre-commit`` always fails) is set BEFORE a child pytest
starts, i.e. before any fixture runs. The same probe test is run twice:

* with ``tests/conftest.py`` loaded as a plugin -- the isolation fixture must
  neutralise the environment and the commit must succeed;
* without it (negative control) -- the commit must fail, proving the
  environment really is hostile and the passing run is down to the isolation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

_PROBE = '''
import os
import subprocess

def test_fixture_commit_succeeds(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test Runner")
    git("commit", "-q", "--allow-empty", "-m", "seed")

def test_body_env_wins_over_fixture(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Body Set")
    out = subprocess.run(["git", "config", "user.name"], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == "Body Set"
'''


@pytest.fixture
def hostile_env(tmp_path: Path) -> dict[str, str]:
    hooks = tmp_path / "hostile-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho hostile hook ran >&2\nexit 1\n")
    hook.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
    env.update(
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="core.hooksPath",
        GIT_CONFIG_VALUE_0=str(hooks),
        PYTHONPATH=str(REPO_ROOT),
    )
    return env


def _run_probe(
    tmp_path: Path, env: dict[str, str], *, isolated: bool
) -> subprocess.CompletedProcess[str]:
    run_dir = tmp_path / ("with-isolation" if isolated else "without-isolation")
    run_dir.mkdir()
    (run_dir / "test_probe.py").write_text(_PROBE)
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly"]
    if isolated:
        cmd += ["-p", "tests.conftest"]
    cmd += [str(run_dir / "test_probe.py")]
    return subprocess.run(
        cmd, cwd=run_dir, env=env, capture_output=True, text=True, timeout=300
    )


def test_hostile_hooks_env_does_not_reach_fixture_commits(
    tmp_path: Path, hostile_env: dict[str, str]
) -> None:
    result = _run_probe(tmp_path, hostile_env, isolated=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_negative_control_hostile_env_fails_without_isolation(
    tmp_path: Path, hostile_env: dict[str, str]
) -> None:
    result = _run_probe(tmp_path, hostile_env, isolated=False)
    assert result.returncode != 0
    assert "FAILED test_probe.py::test_fixture_commit_succeeds" in result.stdout
