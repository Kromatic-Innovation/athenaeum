# SPDX-License-Identifier: Apache-2.0
"""Offline pins for the three defects issue athenaeum#1819 reports in the
``--mode cli`` fidelity spot-check:

1. A PULL-family ``claude -p`` spawn had ``mcp__athenaeum__recall`` available
   but not pre-approved, so a non-interactive session ended every tool-use
   turn on a permission request instead of a result.
2. A ``push_breadcrumb_pull`` cell with an empty breadcrumb was graded as an
   ordinary retrieval miss, even for a probe whose correct behaviour is NOT
   to abstain.
3. The spawn inherited the operator's own ``CLAUDE_CONFIG_DIR``/
   ``~/.claude.json`` when unset, firing the operator's live SessionStart
   hooks inside the eval.

Every test here is fully offline: ``shutil.which`` and ``subprocess.run``
are monkeypatched to fakes (the same pattern ``test_rollout_mode_labelling.py``
uses), and no test in this module spawns a real ``claude`` or spends a
token. UNMARKED for the same reason that module is unmarked (issue
athenaeum#1742): importing ``tests.evals.rollout`` is not itself a reason to
carry ``pytest.mark.rollout``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.evals.corpus import build_corpus
from tests.evals.rollout import (
    PULL_ALLOWED_TOOLS,
    READ_ENTITY_TOOL_NAME,
    RECALL_TOOL_NAME,
    _permission_request_harness_failure,
    _require_isolated_cli_config,
    build_breadcrumb_hook_env,
    build_pull_argv,
    build_push_breadcrumb_context,
    run_pull,
    run_push_breadcrumb_pull,
)

_CORPUS = build_corpus(scale="core")


def _probe(probe_id: str):
    for probe in _CORPUS.probes:
        if probe.id == probe_id:
            return probe
    raise KeyError(probe_id)


# ---------------------------------------------------------------------------
# Defect 1: --allowedTools pre-approval + permission-request detection
# ---------------------------------------------------------------------------


def test_build_pull_argv_pre_approves_the_recall_tools() -> None:
    argv = build_pull_argv("claude", Path("/tmp/example/mcp-config.json"), "claude-haiku-4-5")
    assert "--allowedTools" in argv
    idx = argv.index("--allowedTools")
    approved = argv[idx + 1 : idx + 1 + len(PULL_ALLOWED_TOOLS)]
    assert RECALL_TOOL_NAME in approved
    assert READ_ENTITY_TOOL_NAME in approved
    # Still exclusive to the scoped server -- pre-approval never widens WHICH
    # servers are visible, only whether their tools may run without a prompt.
    assert "--strict-mcp-config" in argv


@pytest.mark.parametrize(
    "answer",
    [
        "I need your permission to search your knowledge base before I can answer.",
        "This requires approval of the recall tool.",
        "Please approve the permission request to continue.",
    ],
)
def test_permission_request_harness_failure_detects_the_observed_shapes(answer: str) -> None:
    assert _permission_request_harness_failure(answer) is not None


def test_permission_request_harness_failure_is_none_for_a_real_answer() -> None:
    assert _permission_request_harness_failure("PTO allowance is 25 days per year.") is None


# ---------------------------------------------------------------------------
# Defect 3: refuse cli mode when CLAUDE_CONFIG_DIR is unset
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# athenaeum#1826 defect 2: the harness hook env must let its own interpreter
# import athenaeum -- offline, no bash/hook spawn.
# ---------------------------------------------------------------------------


def test_build_breadcrumb_hook_env_python_can_import_athenaeum(tmp_path: Path) -> None:
    """athenaeum#1826 AC2: the env this harness shells the hooks with must
    let ITS OWN interpreter ``import athenaeum`` -- reproduced offline
    (issue's own repro: ``PYTHON=~/.pyenv/versions/3.11.15/bin/python``,
    ``PYTHONPATH=None``) by asserting the built env dict, passed straight to
    a real subprocess, can do exactly that. This is the same isolation a
    hook subprocess actually runs under (no ambient PYTHONPATH inherited --
    ``build_breadcrumb_hook_env`` builds a full replacement env, not an
    overlay), so a pass here is not an artifact of the test runner's own
    sys.path.
    """
    env = build_breadcrumb_hook_env(tmp_path / "knowledge", tmp_path / "hook_home")
    assert "PYTHONPATH" in env
    assert Path(env["PYTHONPATH"]).is_absolute()
    proc = subprocess.run(
        [env["PYTHON"], "-c", "import athenaeum"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"


def test_build_breadcrumb_hook_env_pythonpath_is_derived_from_module_location(
    tmp_path: Path,
) -> None:
    """AC2: derived from this module's OWN file location, never cwd -- so a
    caller invoked from an unrelated working directory still gets a working
    PYTHONPATH. Simulated here by chdir-ing away before building the env.
    """
    import os

    previous_cwd = os.getcwd()
    try:
        os.chdir(str(tmp_path))
        env = build_breadcrumb_hook_env(tmp_path / "knowledge", tmp_path / "hook_home")
    finally:
        os.chdir(previous_cwd)
    assert Path(env["PYTHONPATH"]).name == "src"
    assert (Path(env["PYTHONPATH"]) / "athenaeum" / "__init__.py").is_file()


def test_require_isolated_cli_config_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with pytest.raises(RuntimeError, match="CLAUDE_CONFIG_DIR"):
        _require_isolated_cli_config()


def test_require_isolated_cli_config_returns_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    isolated = tmp_path / "claude-config-isolated"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated))
    assert _require_isolated_cli_config() == str(isolated)


# ---------------------------------------------------------------------------
# End-to-end (fake subprocess): run_pull / run_push_breadcrumb_pull
# ---------------------------------------------------------------------------

_PERMISSION_STDOUT = (
    '{"type": "result", "result": ' '"I need your permission to search your knowledge base."}\n'
)
_OK_STDOUT = '{"type": "result", "result": "fake cli answer"}\n'


def _completed(stdout: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    def _run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    return _run


@pytest.fixture
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("tests.evals.rollout.shutil.which", lambda _name: "/usr/bin/fake-claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config-isolated"))


def test_run_pull_marks_harness_failure_on_permission_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_config: None
) -> None:
    monkeypatch.setattr("tests.evals.rollout.subprocess.run", _completed(_PERMISSION_STDOUT))
    probe = _probe("pto_allowance")
    _CORPUS.materialize(tmp_path)
    record = run_pull(probe, tmp_path, tmp_path / "cache", "core")
    assert record.harness_failure is not None
    assert record.config_isolated is True


def test_run_pull_no_harness_failure_on_a_real_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_config: None
) -> None:
    monkeypatch.setattr("tests.evals.rollout.subprocess.run", _completed(_OK_STDOUT))
    probe = _probe("pto_allowance")
    _CORPUS.materialize(tmp_path)
    record = run_pull(probe, tmp_path, tmp_path / "cache", "core")
    assert record.harness_failure is None
    assert record.config_isolated is True


def test_run_pull_raises_without_isolated_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("tests.evals.rollout.shutil.which", lambda _name: "/usr/bin/fake-claude")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    spawned: list[bool] = []

    def _spy_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        spawned.append(True)
        return _completed(_OK_STDOUT)(*args, **kwargs)

    monkeypatch.setattr("tests.evals.rollout.subprocess.run", _spy_run)
    probe = _probe("pto_allowance")
    _CORPUS.materialize(tmp_path)
    with pytest.raises(RuntimeError, match="CLAUDE_CONFIG_DIR"):
        run_pull(probe, tmp_path, tmp_path / "cache", "core")
    assert spawned == []  # never reached the spawn


def test_push_breadcrumb_pull_marks_harness_failure_on_empty_breadcrumb_for_non_abstention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_config: None
) -> None:
    monkeypatch.setattr("tests.evals.rollout.subprocess.run", _completed(_OK_STDOUT))
    probe = _probe("pto_allowance")
    assert probe.expected_uids  # non-abstention: has ground-truth pages
    _CORPUS.materialize(tmp_path)
    record = run_push_breadcrumb_pull(
        probe,
        tmp_path,
        tmp_path / "hook_home",
        tmp_path / "cache",
        "core",
        context_fn=lambda *a, **k: "",  # simulate the hook producing nothing
    )
    assert record.harness_failure is not None
    assert record.transcript[0]["pushed_context"] == ""


def test_push_breadcrumb_pull_no_harness_failure_on_empty_breadcrumb_for_abstention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_config: None
) -> None:
    """An abstention probe legitimately has no ground-truth pages to
    breadcrumb -- an empty breadcrumb there is correct behaviour, not a
    harness failure (see ``_oracle_context``'s own empty result for the
    same class)."""
    monkeypatch.setattr("tests.evals.rollout.subprocess.run", _completed(_OK_STDOUT))
    probe = _probe("abstain_unknown_policy")
    assert not probe.expected_uids
    _CORPUS.materialize(tmp_path)
    record = run_push_breadcrumb_pull(
        probe,
        tmp_path,
        tmp_path / "hook_home",
        tmp_path / "cache",
        "core",
        context_fn=lambda *a, **k: "",
    )
    assert record.harness_failure is None


def test_push_breadcrumb_pull_no_harness_failure_when_breadcrumb_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_config: None
) -> None:
    monkeypatch.setattr("tests.evals.rollout.subprocess.run", _completed(_OK_STDOUT))
    probe = _probe("pto_allowance")
    _CORPUS.materialize(tmp_path)
    record = run_push_breadcrumb_pull(
        probe,
        tmp_path,
        tmp_path / "hook_home",
        tmp_path / "cache",
        "core",
        context_fn=lambda *a, **k: "- pto policy — PTO allowance details",
    )
    assert record.harness_failure is None


# ---------------------------------------------------------------------------
# Defect 2 root cause: build_push_breadcrumb_context silently swallows a
# nonzero USER_PROMPT_HOOK exit -- pinned so the mechanism this issue
# diagnoses (see the PR body) does not drift unnoticed.
# ---------------------------------------------------------------------------


def test_build_push_breadcrumb_context_silently_returns_empty_on_hook_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``USER_PROMPT_HOOK`` that exits nonzero with empty stdout is
    INDISTINGUISHABLE, from this function's return value alone, from a hook
    that legitimately declined to inject anything -- this is the mechanism
    by which the real 2026-09-18 spot-check's empty ``pushed_context`` rows
    could have happened silently. This function's contract (see its own
    docstring: "Returns '' (never raises)") is unchanged by this issue's
    fix; the harness-failure marking added in run_push_breadcrumb_pull is
    the mitigation, not a change here.
    """

    def _session_start_ok(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    def _user_prompt_fails(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="boom: no such interpreter"
        )

    calls = {"n": 0}

    def _dispatch(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _session_start_ok(*args, **kwargs)
        return _user_prompt_fails(*args, **kwargs)

    monkeypatch.setattr("tests.evals.rollout.subprocess.run", _dispatch)
    result = build_push_breadcrumb_context(tmp_path / "knowledge", tmp_path / "hook_home", "q")
    assert result == ""
