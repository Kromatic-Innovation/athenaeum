# SPDX-License-Identifier: Apache-2.0
"""The eval-receipt gate runs from the PR's BASE commit (issue athenaeum#2227).

`.github/workflows/eval-receipt-check.yml` used to run
`python scripts/check_llm_surface_receipt.py` straight out of the
`pull_request` checkout, so a PR's own copy of the script decided that PR.
Since the merge wrapper treats any failed check as blocking, a one-line edit
to the script in the same PR would have bypassed the gate entirely (design
`docs/design/eval-trigger-surface.md` §5.6).

These tests do not paraphrase the workflow — they **extract its own shell
bodies and run them** against throwaway git repos, with `RUNNER_TEMP` and
`GITHUB_OUTPUT` set the way Actions sets them. A test that merely greps the
YAML for `git show` would pass against a recipe that does not work.

Offline, zero network: everything here is local git plus the gate script.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "eval-receipt-check.yml"
GATE_SCRIPT = REPO_ROOT / "scripts" / "check_llm_surface_receipt.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_MATERIALISE_STEP = "Materialise the gate from the PR's base commit"
_CHECK_STEP = "Check LLM surface + eval receipt"


def _steps() -> list[dict[str, object]]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"]["eval-receipt"]["steps"]


def _step(name: str) -> dict[str, object]:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"{WORKFLOW} has no step named {name!r}")


def _run_body(name: str) -> str:
    body = _step(name).get("run")
    assert isinstance(body, str), name
    return body


def _repo_local_imports(path: Path) -> set[str]:
    """Modules `path` imports that resolve to a file inside this repo.

    This is what AC3 turns on: if the gate ever grows a helper module, the
    workflow has to materialise that too, or the helper comes from the PR's
    own checkout and the hole reopens.
    """
    module = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    local: set[str] = set()
    for name in names:
        for candidate in (
            REPO_ROOT / f"{name}.py",
            REPO_ROOT / "scripts" / f"{name}.py",
            REPO_ROOT / "src" / name,
            REPO_ROOT / "src" / f"{name}.py",
        ):
            if candidate.exists():
                local.add(name)
    return local


def _gate_files_declared() -> list[str]:
    """The `GATE_FILES` list the materialise step declares."""
    for line in _run_body(_MATERIALISE_STEP).splitlines():
        stripped = line.strip()
        if stripped.startswith("GATE_FILES="):
            return stripped.split("=", 1)[1].strip('"').split()
    raise AssertionError("the materialise step declares no GATE_FILES")


# --- structure: the gate is not run out of the PR checkout -----------------


def test_the_check_step_runs_the_materialised_copy_not_the_checkout() -> None:
    body = _run_body(_CHECK_STEP)
    assert '"$GATE_SCRIPT"' in body
    assert "python scripts/check_llm_surface_receipt.py" not in body


def test_the_materialise_step_reads_the_base_sha_with_git_show() -> None:
    body = _run_body(_MATERIALISE_STEP)
    assert 'git show "$BASE_SHA:$f"' in body
    assert _step(_MATERIALISE_STEP)["env"]["BASE_SHA"] == (
        "${{ github.event.pull_request.base.sha }}"
    )


def test_the_workflow_materialises_every_repo_local_import_of_the_gate() -> None:
    """AC3: not only the entry script."""
    declared = _gate_files_declared()
    assert "scripts/check_llm_surface_receipt.py" in declared
    local_imports = _repo_local_imports(GATE_SCRIPT)
    declared_stems = {Path(f).stem for f in declared}
    missing = sorted(name for name in local_imports if name not in declared_stems)
    assert not missing, (
        f"{GATE_SCRIPT.name} imports {missing} from inside the repo, but "
        f"{WORKFLOW.name}'s GATE_FILES does not materialise them from the base "
        "commit — a helper taken from the PR's own checkout reopens the hole "
        "athenaeum#2227 closed. Add them to GATE_FILES."
    )


def test_the_gate_script_has_no_repo_local_imports_today() -> None:
    """Why a one-file GATE_FILES is sufficient right now, asserted rather than assumed."""
    assert _repo_local_imports(GATE_SCRIPT) == set()


def test_the_job_still_installs_nothing() -> None:
    """athenaeum#2223's constraint, re-checked here: the gate is stdlib only."""
    for step in _steps():
        body = step.get("run")
        if isinstance(body, str):
            assert "pip install" not in body
            assert "uv pip" not in body


def test_ci_yml_is_not_part_of_this_gate() -> None:
    """AC6: the check stays its own workflow, out of ci.yml's aggregate."""
    assert "check_llm_surface_receipt" not in CI_WORKFLOW.read_text(encoding="utf-8")


def test_the_header_explains_why_the_gate_runs_from_base() -> None:
    header = WORKFLOW.read_text(encoding="utf-8").split("on:", 1)[0]
    assert "RUNS FROM THE BASE COMMIT" in header
    assert "athenaeum#2227" in header


# --- behaviour: run the workflow's own shell bodies ------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _init(cwd: Path) -> None:
    _git(cwd, "init", "-b", "develop")
    _git(cwd, "config", "user.email", "dijkstra@example.invalid")
    _git(cwd, "config", "user.name", "Dijkstra Test Fixture")


def _commit(cwd: Path, message: str) -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-m", message)
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


#: A PR that weakens the gate: same CLI, always passes.
_SABOTAGED = """#!/usr/bin/env python3
import sys


def main(argv=None):
    print("nothing to see here")
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


@pytest.fixture
def pr_repo(tmp_path: Path) -> dict[str, object]:
    """A base commit with the REAL gate, and a head that sabotages it.

    The PR also changes a designated symbol with no receipt, so the real gate
    must fail it and the sabotaged one must not.
    """
    _init(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / "scripts" / "check_llm_surface_receipt.py").write_text(
        GATE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / ".github" / "llm-surface.txt").write_text(
        "# the designated symbol\nmod.py::designated\n", encoding="utf-8"
    )
    (tmp_path / "mod.py").write_text("def designated(value):\n    return value + 1\n")
    (tmp_path / "pr-body.txt").write_text("a PR description with no receipt line\n")
    base = _commit(tmp_path, "base: the real gate")

    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "mod.py").write_text("def designated(value):\n    return value + 99\n")
    (tmp_path / "scripts" / "check_llm_surface_receipt.py").write_text(
        _SABOTAGED, encoding="utf-8"
    )
    head = _commit(tmp_path, "head: change a designated symbol AND weaken the gate")
    return {"dir": tmp_path, "base": base, "head": head}


def _materialise(pr_repo: dict[str, object], base_sha: str) -> dict[str, str]:
    """Run the workflow's materialise step verbatim, and return its outputs."""
    cwd = pr_repo["dir"]
    assert isinstance(cwd, Path)
    runner_temp = cwd / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    github_output = cwd / "github-output"
    github_output.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _run_body(_MATERIALISE_STEP)],
        cwd=cwd,
        env={
            **os.environ,
            "BASE_SHA": base_sha,
            "HEAD_SHA": str(pr_repo["head"]),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(github_output),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    outputs = dict(
        line.split("=", 1)
        for line in github_output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    outputs["_stdout"] = result.stdout
    return outputs


def test_the_base_copy_judges_a_pr_that_weakened_the_gate(pr_repo: dict[str, object]) -> None:
    """AC2, end to end: the PR's own script would pass; the base one fails it."""
    cwd = pr_repo["dir"]
    assert isinstance(cwd, Path)
    args = [
        "--surface-file",
        ".github/llm-surface.txt",
        "--base-sha",
        str(pr_repo["base"]),
        "--head-sha",
        str(pr_repo["head"]),
        "--pr-body-file",
        "pr-body.txt",
        "--repo",
        "Kromatic-Innovation/athenaeum",
    ]

    # The premise: the PR's OWN copy passes, because the PR weakened it.
    sabotaged = subprocess.run(
        [sys.executable, "scripts/check_llm_surface_receipt.py", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert sabotaged.returncode == 0, sabotaged.stderr

    # The workflow's own recipe, and then the real verdict.
    outputs = _materialise(pr_repo, str(pr_repo["base"]))
    judged = subprocess.run(
        [sys.executable, outputs["script"], *args], cwd=cwd, capture_output=True, text=True
    )
    assert judged.returncode == 1, judged.stdout + judged.stderr
    assert "no valid eval receipt" in judged.stdout


def test_the_materialised_script_is_byte_identical_to_the_base_commits(
    pr_repo: dict[str, object],
) -> None:
    outputs = _materialise(pr_repo, str(pr_repo["base"]))
    assert Path(outputs["script"]).read_text(encoding="utf-8") == GATE_SCRIPT.read_text(
        encoding="utf-8"
    )


def test_the_merge_bases_surface_file_is_materialised_too(
    pr_repo: dict[str, object],
) -> None:
    """AC1: both surface files are passed, so the union rule still applies."""
    outputs = _materialise(pr_repo, str(pr_repo["base"]))
    assert "base_surface" in outputs
    assert "mod.py::designated" in Path(outputs["base_surface"]).read_text(encoding="utf-8")


def test_the_check_step_passes_the_base_surface_file_when_supported(
    pr_repo: dict[str, object],
) -> None:
    """The rollout guard: the flag is only passed if the BASE script knows it."""
    body = _run_body(_CHECK_STEP)
    assert 'grep -q -- "--base-surface-file" "$GATE_SCRIPT"' in body
    assert "--base-surface-file" in GATE_SCRIPT.read_text(encoding="utf-8")


def test_bootstrap_falls_back_to_the_head_copy_with_a_warning(tmp_path: Path) -> None:
    """AC4: a base commit that predates the gate must not wedge the check."""
    _init(tmp_path)
    (tmp_path / "README.md").write_text("no gate here yet\n")
    base = _commit(tmp_path, "base without the gate")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check_llm_surface_receipt.py").write_text(
        GATE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    head = _commit(tmp_path, "add the gate")

    outputs = _materialise({"dir": tmp_path, "head": head}, base)
    assert "::warning::" in outputs["_stdout"]
    assert "falling back to this PR's own copy" in outputs["_stdout"]
    assert Path(outputs["script"]).read_text(encoding="utf-8") == GATE_SCRIPT.read_text(
        encoding="utf-8"
    )
