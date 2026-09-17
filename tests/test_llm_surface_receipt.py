# SPDX-License-Identifier: Apache-2.0
"""Offline tests for `scripts/check_llm_surface_receipt.py` (issue athenaeum#1731).

Every case here drives the pure `evaluate`/`check_receipt` functions with an
injected `run_lookup` stub — no `gh` subprocess, no network, no real PR. This
is the offline exercise athenaeum#1731 AC3 asks for: docs-only passes,
`tiers.py`-without-receipt fails, and a valid receipt passes; plus the
near-misses the same machinery makes easy to add (wrong event, stale SHA,
wrong workflow, failed conclusion, wrong repo).

One test (`test_main_uses_three_dot_diff_and_reports_exit_code`) drives
`main()` itself against a real throwaway git repo instead of a stub, per
`dijkstra/engineering-conventions.md`'s ephemeral-git-fixture convention —
the two-dot-vs-three-dot diff mode and the CLI's exit code are otherwise
untested by the stubbed cases above.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = REPO_ROOT / "scripts" / "check_llm_surface_receipt.py"

# scripts/ is not an importable package (no __init__.py, not on sys.path by
# convention) — load the module directly by file path rather than adding
# scripts/ to sys.path, to avoid shadowing an unrelated top-level module.
_spec = importlib.util.spec_from_file_location("check_llm_surface_receipt", _SCRIPT_PATH)
assert _spec and _spec.loader
check_llm_surface_receipt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check_llm_surface_receipt
_spec.loader.exec_module(check_llm_surface_receipt)

evaluate = check_llm_surface_receipt.evaluate
intersect_surface = check_llm_surface_receipt.intersect_surface

SURFACE = ["src/athenaeum/tiers.py", "src/athenaeum/provider.py", "docs/design/prompts.md"]
REPO = "Kromatic-Innovation/athenaeum"
HEAD_SHA = "a" * 40
RUN_URL = "https://github.com/Kromatic-Innovation/athenaeum/actions/runs/123456789"


def _matching_lookup(repo: str, run_id: str) -> dict[str, str]:
    assert repo == REPO
    assert run_id == "123456789"
    return {
        "headSha": HEAD_SHA,
        "event": "workflow_dispatch",
        "workflowName": "Evals",
        "conclusion": "success",
    }


def _wrong_event_lookup(repo: str, run_id: str) -> dict[str, str]:
    return {
        "headSha": HEAD_SHA,
        "event": "push",
        "workflowName": "Evals",
        "conclusion": "success",
    }


def _stale_sha_lookup(repo: str, run_id: str) -> dict[str, str]:
    return {
        "headSha": "b" * 40,
        "event": "workflow_dispatch",
        "workflowName": "Evals",
        "conclusion": "success",
    }


def _wrong_workflow_lookup(repo: str, run_id: str) -> dict[str, str]:
    """A ci.yml dispatch at the right SHA — must still be rejected (Quine PR #1745 review)."""
    return {
        "headSha": HEAD_SHA,
        "event": "workflow_dispatch",
        "workflowName": "CI",
        "conclusion": "success",
    }


def _failed_conclusion_lookup(repo: str, run_id: str) -> dict[str, str]:
    return {
        "headSha": HEAD_SHA,
        "event": "workflow_dispatch",
        "workflowName": "Evals",
        "conclusion": "failure",
    }


def _non_raising_lookup(repo: str, run_id: str) -> dict[str, str]:
    """Never raises — a wrong-repo case must be rejected by the URL-repo check
    itself, before run_lookup is ever consulted. If this were called and its
    return value accepted, the test using it would prove the repo check had
    gone missing rather than merely that SOME failure occurred."""
    return {
        "headSha": HEAD_SHA,
        "event": "workflow_dispatch",
        "workflowName": "Evals",
        "conclusion": "success",
    }


def _unreached_lookup(repo: str, run_id: str) -> dict[str, str]:
    raise AssertionError("run_lookup must not be called when the intersection is empty")


def test_docs_only_pr_passes_without_receipt() -> None:
    result = evaluate(
        changed_files=["docs/README.md", "CHANGELOG.md"],
        surface=SURFACE,
        pr_body="no receipt line here",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_unreached_lookup,
    )
    assert result.ok, result.message


def test_tiers_py_without_receipt_fails() -> None:
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body="just a plain PR description",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_unreached_lookup,
    )
    assert not result.ok
    assert "tiers.py" in result.message
    assert "gh workflow run evals.yml" in result.message


def test_tiers_py_with_valid_receipt_passes() -> None:
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body=f"Some description.\n\nEvals: {RUN_URL}\n",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_matching_lookup,
    )
    assert result.ok, result.message


def test_not_needed_line_passes() -> None:
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body="Evals: not needed — CI-only change, no LLM call path touched",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_unreached_lookup,
    )
    assert result.ok, result.message


def test_bare_not_needed_without_reason_fails() -> None:
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body="Evals: not needed",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_unreached_lookup,
    )
    assert not result.ok


def test_receipt_with_wrong_event_fails() -> None:
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body=f"Evals: {RUN_URL}",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_wrong_event_lookup,
    )
    assert not result.ok
    assert "workflow_dispatch" in result.message


def test_receipt_with_stale_sha_fails() -> None:
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body=f"Evals: {RUN_URL}",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_stale_sha_lookup,
    )
    assert not result.ok
    assert "stale" in result.message.lower()


def test_receipt_pointing_at_a_different_repo_fails() -> None:
    # A non-raising lookup, on purpose: if the repo check ever regresses (is
    # accidentally removed or short-circuited), this stub would happily
    # return a fully valid run and the assertion below would catch that —
    # a raising stub would instead have its AssertionError swallowed by
    # check_receipt's own `except Exception` clause, silently "passing" the
    # test on a masked internal error rather than proving the repo check
    # itself did the rejecting.
    other_repo_url = "https://github.com/someone-else/fork/actions/runs/1"
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body=f"Evals: {other_repo_url}",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_non_raising_lookup,
    )
    assert not result.ok
    assert "points at" in result.message


def test_receipt_pointing_at_wrong_workflow_fails() -> None:
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body=f"Evals: {RUN_URL}",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_wrong_workflow_lookup,
    )
    assert not result.ok
    assert "Evals" in result.message
    assert "CI" in result.message


def test_receipt_with_failed_conclusion_fails() -> None:
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body=f"Evals: {RUN_URL}",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_failed_conclusion_lookup,
    )
    assert not result.ok
    assert "failure" in result.message


def test_intersect_surface_directory_prefix_matches() -> None:
    surface = ["tests/evals/"]
    hits = intersect_surface(["tests/evals/data/foo.json", "tests/test_search.py"], surface)
    assert hits == ["tests/evals/data/foo.json"]


def test_intersect_surface_exact_entry_does_not_substring_match() -> None:
    surface = ["src/athenaeum/search.py"]
    hits = intersect_surface(["tests/test_search.py"], surface)
    assert hits == []


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_fixture_repo(cwd: Path) -> None:
    """Ephemeral git-repo test fixture, per dijkstra/engineering-conventions.md:
    `git init -b develop` (never `-b main`), identity set in the fixture."""
    _run_git(cwd, "init", "-b", "develop")
    _run_git(cwd, "config", "user.email", "dijkstra@example.invalid")
    _run_git(cwd, "config", "user.name", "Dijkstra Test Fixture")


def test_main_uses_three_dot_diff_and_reports_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real throwaway git repo, not a mocked argv — proves the two-dot
    regression this script already hit once (fixed pre-review) stays fixed:
    a file touched only by `develop` advancing AFTER the branch point must
    not appear in `main()`'s changed-file set, even though a two-dot
    `base_sha..head_sha` diff would report it (reversed).
    """
    _init_fixture_repo(tmp_path)

    (tmp_path / "README.md").write_text("base\n")
    _run_git(tmp_path, "add", "README.md")
    _run_git(tmp_path, "commit", "-m", "base")
    fork_point = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    _run_git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "docs.md").write_text("docs-only change\n")
    _run_git(tmp_path, "add", "docs.md")
    _run_git(tmp_path, "commit", "-m", "docs-only change on feature")
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    # develop advances past the fork point, touching a SURFACE file this
    # feature branch never touched. A two-dot diff against this new develop
    # tip would report that file as changed (reversed); three-dot must not.
    _run_git(tmp_path, "checkout", "develop")
    (tmp_path / "tiers.py").write_text("unrelated develop-side change\n")
    _run_git(tmp_path, "add", "tiers.py")
    _run_git(tmp_path, "commit", "-m", "unrelated change lands on develop after the fork")
    new_base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert new_base_sha != fork_point

    surface_file = tmp_path / "llm-surface.txt"
    surface_file.write_text("tiers.py\n")
    pr_body_file = tmp_path / "pr-body.txt"
    pr_body_file.write_text("docs-only PR, nothing LLM-facing here\n")

    # _git_changed_files shells out to `git diff` in the process cwd (the
    # real workflow always runs from the checked-out repo root) — chdir into
    # the fixture repo so main() diffs it instead of this test's own repo.
    monkeypatch.chdir(tmp_path)
    exit_code = check_llm_surface_receipt.main(
        [
            "--surface-file", str(surface_file),
            "--base-sha", new_base_sha,
            "--head-sha", head_sha,
            "--pr-body-file", str(pr_body_file),
            "--repo", REPO,
        ]
    )

    # If this used two-dot diff, `tiers.py` (develop-only) would show up as
    # "changed", the surface intersection would be non-empty, no receipt is
    # present, and main() would return 1. Three-dot correctly sees this
    # feature branch as docs-only against the true merge-base.
    assert exit_code == 0
