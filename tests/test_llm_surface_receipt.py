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
    """A ci.yml dispatch at the right SHA — must still be rejected (athenaeum#1745 review)."""
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
        paths_changed_lookup=lambda receipt, head: ["src/athenaeum/tiers.py"],
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


# --- content-based receipt validity -----------------------------------------
#
# A receipt run at commit X satisfies head H when no LLM-surface path differs
# between X and H (a direct tree diff), so a merge-wrapper bump, a develop
# merge that leaves the surface alone, or a rebase does not demand a fresh
# paid eval dispatch.

RECEIPT_SHA = "b" * 40  # what _stale_sha_lookup reports as the run's headSha


def _changed(paths: list[str] | None):
    def lookup(receipt_sha: str, head_sha: str) -> list[str] | None:
        assert (receipt_sha, head_sha) == (RECEIPT_SHA, HEAD_SHA)
        return paths

    return lookup


def _evaluate_with(paths: list[str] | None, run_lookup=_stale_sha_lookup):
    return evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body=f"Evals: {RUN_URL}",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=run_lookup,
        paths_changed_lookup=_changed(paths),
    )


def test_receipt_covers_head_with_only_non_surface_changes() -> None:
    result = _evaluate_with(["pyproject.toml", "CHANGELOG.md"])
    assert result.ok, result.message
    assert "no LLM-surface file differs" in result.message


def test_receipt_is_stale_when_a_surface_file_changed_since() -> None:
    result = _evaluate_with(["CHANGELOG.md", "src/athenaeum/tiers.py"])
    assert not result.ok
    assert "stale" in result.message.lower()
    assert "src/athenaeum/tiers.py" in result.message


def test_receipt_is_stale_when_the_receipt_commit_cannot_be_read() -> None:
    result = _evaluate_with(None)
    assert not result.ok
    assert "could not be compared" in result.message


def test_failed_run_with_identical_surface_still_fails() -> None:
    def failed(repo: str, run_id: str) -> dict[str, str]:
        return {
            "headSha": RECEIPT_SHA,
            "event": "workflow_dispatch",
            "workflowName": "Evals",
            "conclusion": "failure",
        }

    result = _evaluate_with([], run_lookup=failed)
    assert not result.ok
    assert "failure" in result.message


def _rev_parse(cwd: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_file(cwd: Path, name: str, text: str, msg: str) -> str:
    (cwd / name).write_text(text)
    _run_git(cwd, "add", name)
    _run_git(cwd, "commit", "-m", msg)
    return _rev_parse(cwd)


@pytest.fixture
def content_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A develop line and a feature branch carrying a receipted surface change.

    Returns named shas. `receipted` is the head the eval ran at; every other
    head is a later state of the same PR.
    """
    _init_fixture_repo(tmp_path)
    shas: dict[str, str] = {}
    shas["base"] = _commit_file(tmp_path, "pyproject.toml", 'version = "0.22.51"\n', "base")
    _commit_file(tmp_path, "tiers.py", "v0\n", "surface file on develop")
    shas["fork"] = _rev_parse(tmp_path)

    _run_git(tmp_path, "checkout", "-b", "feature")
    shas["receipted"] = _commit_file(tmp_path, "tiers.py", "v1\n", "PR's surface change")

    # (1) merge-wrapper bump on top of the receipted head.
    shas["bump"] = _commit_file(
        tmp_path, "pyproject.toml", 'version = "0.22.52"\n', "chore: bump version"
    )

    # (2) develop advances WITHOUT touching the surface; merged into the PR.
    _run_git(tmp_path, "checkout", "develop")
    _commit_file(tmp_path, "README.md", "docs\n", "docs on develop")
    shas["develop_docs"] = _rev_parse(tmp_path)
    _run_git(tmp_path, "checkout", "feature")
    _run_git(tmp_path, "merge", "--no-ff", "-m", "merge develop (docs)", "develop")
    shas["merge_docs"] = _rev_parse(tmp_path)

    # (3) develop then changes a surface file; merged into the PR.
    _run_git(tmp_path, "checkout", "develop")
    _commit_file(tmp_path, "provider.py", "develop-side prompt change\n", "surface on develop")
    _run_git(tmp_path, "checkout", "feature")
    _run_git(tmp_path, "merge", "--no-ff", "-m", "merge develop (surface)", "develop")
    shas["merge_surface"] = _rev_parse(tmp_path)

    # (4) the PR rebased onto develop_docs: a NON-ancestor of `receipted`,
    # with identical surface content.
    _run_git(tmp_path, "checkout", "-b", "rebased", shas["develop_docs"])
    _run_git(tmp_path, "cherry-pick", shas["receipted"])
    shas["rebased"] = _rev_parse(tmp_path)

    monkeypatch.chdir(tmp_path)
    return shas


CONTENT_SURFACE = ["tiers.py", "provider.py"]


def _covers(shas: dict[str, str], head: str) -> tuple[bool, list[str]]:
    return check_llm_surface_receipt.receipt_covers_head(
        shas["receipted"],
        shas[head],
        CONTENT_SURFACE,
        check_llm_surface_receipt.git_paths_changed_between,
    )


def test_bump_only_head_is_covered(content_repo: dict[str, str]) -> None:
    assert _covers(content_repo, "bump") == (True, [])


def test_develop_merge_without_surface_change_is_covered(content_repo: dict[str, str]) -> None:
    assert _covers(content_repo, "merge_docs") == (True, [])


def test_develop_merge_with_surface_change_is_stale(content_repo: dict[str, str]) -> None:
    assert _covers(content_repo, "merge_surface") == (False, ["provider.py"])


def test_non_ancestor_receipt_with_identical_surface_is_covered(
    content_repo: dict[str, str],
) -> None:
    rebased, receipted = content_repo["rebased"], content_repo["receipted"]
    is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", receipted, rebased], check=False
    ).returncode
    assert is_ancestor != 0  # the premise: a rebase orphaned the receipted commit
    assert _covers(content_repo, "rebased") == (True, [])


def test_unknown_receipt_commit_is_not_covered(content_repo: dict[str, str]) -> None:
    covered, hits = check_llm_surface_receipt.receipt_covers_head(
        "0" * 40,
        content_repo["bump"],
        CONTENT_SURFACE,
        check_llm_surface_receipt.git_paths_changed_between,
    )
    assert (covered, hits) == (False, [])


def test_main_accepts_receipt_across_a_develop_merge(content_repo: dict[str, str]) -> None:
    """End to end through main(): receipt at `receipted`, head `merge_docs`
    passes; head `merge_surface` fails."""
    cwd = Path.cwd()
    surface_file = cwd / "llm-surface.txt"
    surface_file.write_text("tiers.py\nprovider.py\n")
    pr_body_file = cwd / "pr-body.txt"
    pr_body_file.write_text(f"Evals: {RUN_URL}\n")
    run = {
        "headSha": content_repo["receipted"],
        "event": "workflow_dispatch",
        "workflowName": "Evals",
        "conclusion": "success",
    }
    real_evaluate = check_llm_surface_receipt.evaluate

    def run_main(head: str) -> int:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                check_llm_surface_receipt,
                "evaluate",
                lambda *a, **k: real_evaluate(*a, run_lookup=lambda repo, rid: run, **k),
            )
            return check_llm_surface_receipt.main(
                [
                    "--surface-file", str(surface_file),
                    "--base-sha", content_repo["fork"],
                    "--head-sha", content_repo[head],
                    "--pr-body-file", str(pr_body_file),
                    "--repo", REPO,
                ]
            )

    assert run_main("merge_docs") == 0
    assert run_main("merge_surface") == 1


# --- symbol-level surface grammar and the structural fingerprint ------------
#
# athenaeum#2223 / design `docs/design/eval-trigger-surface.md` §5.1, §5.3,
# §5.4. Real throwaway git repos, per the ephemeral-git-fixture convention
# the content-based cases above already follow: the fingerprint reads through
# `git show`, so stubbing git would leave the thing under test untested.

parse_surface_entry = check_llm_surface_receipt.parse_surface_entry
fingerprint_entry = check_llm_surface_receipt.fingerprint_entry
fire = check_llm_surface_receipt.fire
MISSING = check_llm_surface_receipt.MISSING
UNPARSEABLE = check_llm_surface_receipt.UNPARSEABLE

#: The module every symbol case below designates one function inside of.
_MOD_V0 = '''"""Module docstring."""

CONSTANT = 7


def designated(value):
    """What this does."""
    # an explanatory comment
    return value + 1


def undesignated(value):
    return value - 1


class Holder:
    """A class with a designated method."""

    def method(self):
        return "held"
'''

SYMBOL_SURFACE = [
    "mod.py::designated",
    "mod.py::CONSTANT",
    "mod.py::Holder.method",
    "whole.py",
    "notes.txt",
    "prompts/",
]


def _parse_forms(entry: str) -> tuple[str, str | None]:
    parsed = parse_surface_entry(entry)
    return parsed.path, parsed.symbol


def test_surface_grammar_splits_the_three_symbol_forms() -> None:
    assert _parse_forms("src/athenaeum/tiers.py::parse_merge_ops_response") == (
        "src/athenaeum/tiers.py",
        "parse_merge_ops_response",
    )
    assert _parse_forms("src/athenaeum/search.py::VectorBackend.build_index") == (
        "src/athenaeum/search.py",
        "VectorBackend.build_index",
    )
    assert _parse_forms("src/athenaeum/prompt_registry.py::_META_ROWS") == (
        "src/athenaeum/prompt_registry.py",
        "_META_ROWS",
    )


def test_surface_grammar_keeps_the_pre_existing_forms() -> None:
    """Back-compatibility: whole-file and `dir/` entries parse as they always did."""
    assert _parse_forms("src/athenaeum/tiers.py") == ("src/athenaeum/tiers.py", None)
    assert _parse_forms("src/athenaeum/prompts/") == ("src/athenaeum/prompts/", None)
    assert parse_surface_entry("src/athenaeum/prompts/").is_prefix
    assert not parse_surface_entry("src/athenaeum/tiers.py").is_prefix


def test_a_symbol_entry_still_matches_its_file_for_the_touched_list() -> None:
    """`_matches` is path-level on purpose — `fire` makes the symbol decision."""
    assert check_llm_surface_receipt._matches("mod.py", "mod.py::designated")
    assert not check_llm_surface_receipt._matches("other.py", "mod.py::designated")


@pytest.fixture
def symbol_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `develop` carrying `mod.py`, a whole-file module, a text file and a prompt dir."""
    _init_fixture_repo(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "mod.py").write_text(_MOD_V0)
    (tmp_path / "whole.py").write_text("WHOLE = 1  # a comment\n")
    (tmp_path / "notes.txt").write_text("plain text\n")
    (tmp_path / "prompts" / "one.md").write_text("prompt one\n")
    _run_git(tmp_path, "add", "-A")
    _run_git(tmp_path, "commit", "-m", "develop base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _branch_with(
    repo: Path, name: str, files: dict[str, str], *, surface: list[str] | None = None
) -> tuple[bool, list[str]]:
    """Branch off `develop`, apply `files`, and return FIRE(merge_base, head).

    `surface` defaults to `SYMBOL_SURFACE` at both ends; passing a different
    list models a PR that also edits `.github/llm-surface.txt` (the head
    designations), which is what the union rule is about.
    """
    _run_git(repo, "checkout", "-q", "develop")
    merge_base = _rev_parse(repo, "develop")
    _run_git(repo, "checkout", "-q", "-b", name)
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", f"case: {name}")
    head = _rev_parse(repo)
    return fire(
        merge_base,
        head,
        SYMBOL_SURFACE,
        SYMBOL_SURFACE if surface is None else surface,
    )


def test_comment_only_edit_in_a_designated_function_does_not_fire(symbol_repo: Path) -> None:
    """(a) Comments never reach the AST, so they cannot cost a paid eval run."""
    edited = _MOD_V0.replace("# an explanatory comment", "# a differently worded comment")
    assert edited != _MOD_V0
    assert _branch_with(symbol_repo, "case-a", {"mod.py": edited}) == (False, [])


def test_docstring_only_edit_does_not_fire(symbol_repo: Path) -> None:
    """(b) Docstrings DO reach the AST — `_strip_docstrings` is what removes them."""
    edited = _MOD_V0.replace('"""What this does."""', '"""What this does, explained better."""')
    assert edited != _MOD_V0
    assert _branch_with(symbol_repo, "case-b", {"mod.py": edited}) == (False, [])


def test_changing_one_literal_in_a_designated_function_fires(symbol_repo: Path) -> None:
    """(c) The case the whole gate exists for."""
    edited = _MOD_V0.replace("return value + 1", "return value + 2")
    required, changed = _branch_with(symbol_repo, "case-c", {"mod.py": edited})
    assert required
    assert changed == ["mod.py::designated"]


def test_editing_an_undesignated_function_in_the_same_file_does_not_fire(
    symbol_repo: Path,
) -> None:
    """(d) The precision the file-level surface could not express."""
    edited = _MOD_V0.replace("return value - 1", "return value - 99")
    assert _branch_with(symbol_repo, "case-d", {"mod.py": edited}) == (False, [])


def test_renaming_a_designated_function_fires_as_missing(symbol_repo: Path) -> None:
    """(e) A rename leaves the entry unresolved at head: MISSING != the base hash."""
    edited = _MOD_V0.replace("def designated(", "def renamed(")
    required, changed = _branch_with(symbol_repo, "case-e", {"mod.py": edited})
    assert required
    assert changed == ["mod.py::designated"]


def test_removing_the_entry_while_editing_the_symbol_still_fires(symbol_repo: Path) -> None:
    """(f) The union rule: a PR cannot un-designate its way out of an eval."""
    edited = _MOD_V0.replace("return value + 1", "return value + 3")
    head_surface = [e for e in SYMBOL_SURFACE if e != "mod.py::designated"]
    required, changed = _branch_with(
        symbol_repo, "case-f", {"mod.py": edited}, surface=head_surface
    )
    assert required
    assert changed == ["mod.py::designated"]


def test_whole_file_entry_fires_on_a_non_comment_change(symbol_repo: Path) -> None:
    """(g) Whole-file entries behave as before."""
    required, changed = _branch_with(symbol_repo, "case-g", {"whole.py": "WHOLE = 2\n"})
    assert required
    assert changed == ["whole.py"]


def test_whole_file_entry_ignores_a_comment_only_change(symbol_repo: Path) -> None:
    """The one way a whole-file entry is NOT as before: strictly coarser, minus prose.

    Design §11's rollback property — restoring a whole-file list degrades
    FIRE to "the module's AST changed", which is still never WEAKER than the
    old path match except for comments and docstrings.
    """
    assert _branch_with(
        symbol_repo, "case-g2", {"whole.py": "WHOLE = 1  # reworded comment\n"}
    ) == (False, [])


def test_a_module_level_constant_is_a_designatable_symbol(symbol_repo: Path) -> None:
    edited = _MOD_V0.replace("CONSTANT = 7", "CONSTANT = 8")
    required, changed = _branch_with(symbol_repo, "case-const", {"mod.py": edited})
    assert (required, changed) == (True, ["mod.py::CONSTANT"])


def test_a_class_method_is_a_designatable_symbol(symbol_repo: Path) -> None:
    edited = _MOD_V0.replace('return "held"', 'return "released"')
    required, changed = _branch_with(symbol_repo, "case-method", {"mod.py": edited})
    assert (required, changed) == (True, ["mod.py::Holder.method"])


def test_a_non_py_entry_is_byte_hashed(symbol_repo: Path) -> None:
    required, changed = _branch_with(symbol_repo, "case-txt", {"notes.txt": "plain text!\n"})
    assert (required, changed) == (True, ["notes.txt"])


def test_a_prefix_entry_fires_on_an_added_file(symbol_repo: Path) -> None:
    """A prefix is hashed path-by-path, so an ADDED prompt fires, not just an edited one."""
    required, changed = _branch_with(symbol_repo, "case-prefix", {"prompts/two.md": "prompt two\n"})
    assert (required, changed) == (True, ["prompts/"])


def test_an_unparseable_head_always_counts_as_changed(symbol_repo: Path) -> None:
    """Fail closed: a head with no parseable structure has nothing to compare."""
    required, changed = _branch_with(symbol_repo, "case-syntax", {"whole.py": "def oops(:\n"})
    assert required
    assert "whole.py" in changed
    head = _rev_parse(symbol_repo)
    assert fingerprint_entry("whole.py", head) == UNPARSEABLE


def test_fingerprint_is_missing_for_an_absent_path_and_an_absent_symbol(
    symbol_repo: Path,
) -> None:
    head = _rev_parse(symbol_repo, "develop")
    assert fingerprint_entry("nosuchfile.py", head) == MISSING
    assert fingerprint_entry("mod.py::nosuchsymbol", head) == MISSING
    assert fingerprint_entry("nosuchdir/", head) == MISSING
    # A dotted path deeper than `Class.member` is not part of the grammar.
    assert fingerprint_entry("mod.py::Holder.method.inner", head) == MISSING


def test_fingerprint_of_a_designated_symbol_ignores_the_rest_of_the_file(
    symbol_repo: Path,
) -> None:
    """The property every case above rests on, asserted directly."""
    base = _rev_parse(symbol_repo, "develop")
    before = fingerprint_entry("mod.py::designated", base)
    (symbol_repo / "mod.py").write_text(_MOD_V0.replace("return value - 1", "return value - 5"))
    _run_git(symbol_repo, "add", "mod.py")
    _run_git(symbol_repo, "commit", "-m", "edit the undesignated sibling")
    after = fingerprint_entry("mod.py::designated", _rev_parse(symbol_repo))
    assert before == after
    assert before not in {MISSING, UNPARSEABLE}


# --- the dispatch rule in the failure text (athenaeum#2228) -----------------
#
# Design §7: the gate's failure message is what a dispatching agent reads at
# decision time, so the rule has to be IN it. The old text ("the dispatch is
# stale, re-run it") is the most likely driver of the measured per-push
# re-dispatch churn, so its absence is pinned too.

DISPATCH_RULE = check_llm_surface_receipt.DISPATCH_RULE
_RULE_KEY_PHRASE = "dispatch once, after review is complete, at the final head"


def test_the_dispatch_rule_says_once_at_the_final_reviewed_head() -> None:
    assert _RULE_KEY_PHRASE in DISPATCH_RULE.lower()
    assert "tests/evals/README.md" in DISPATCH_RULE


def test_stale_receipt_message_carries_the_dispatch_rule_and_not_re_run_it() -> None:
    result = _evaluate_with(["CHANGELOG.md", "src/athenaeum/tiers.py"])
    assert not result.ok
    assert "stale" in result.message.lower()
    assert _RULE_KEY_PHRASE in result.message.lower()
    assert "re-run it" not in result.message.lower()


def test_missing_receipt_message_carries_the_dispatch_rule() -> None:
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body="a PR description with no receipt line",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_unreached_lookup,
    )
    assert not result.ok
    assert _RULE_KEY_PHRASE in result.message.lower()
    assert "re-run it" not in result.message.lower()


def test_no_failure_message_tells_the_reader_to_re_dispatch_per_push() -> None:
    """Every path that asks for a dispatch asks for exactly ONE, at the final head."""
    messages = [
        _evaluate_with(["CHANGELOG.md", "src/athenaeum/tiers.py"]).message,
        _evaluate_with(None).message,
        evaluate(
            changed_files=["src/athenaeum/tiers.py"],
            surface=SURFACE,
            pr_body="no receipt",
            head_sha=HEAD_SHA,
            this_repo=REPO,
            run_lookup=_unreached_lookup,
        ).message,
    ]
    for message in messages:
        assert "re-run" not in message.lower(), message
        assert _RULE_KEY_PHRASE in message.lower(), message


# --- COVERS: a receipt that survives a develop merge (athenaeum#2225) -------
#
# Design §5.4. A passing run at X satisfies head H iff, for every designated
# symbol, either X and H agree, or the PR changed it at NEITHER point (so the
# difference arrived from `develop`, where it was evaluated under its own PR).
# Real throwaway git repos: the predicate reads three commits per symbol, so
# there is nothing meaningful left to test once git is stubbed out.

receipt_covers_head = check_llm_surface_receipt.receipt_covers_head

_COVERS_SURFACE = ["mod.py::designated", "mod.py::other"]


def _mod(*, designated_body: str, other_body: str, comment: str = "# comment") -> str:
    return (
        '"""Module."""\n'
        "\n"
        "\n"
        "def designated(value):\n"
        '    """Doc."""\n'
        f"    {comment}\n"
        f"    {designated_body}\n"
        "\n"
        "\n"
        "def other(value):\n"
        f"    {other_body}\n"
    )


_V0 = _mod(designated_body="return value + 1", other_body="return value * 2")
_PR_CHANGE = _mod(designated_body="return value + 7", other_body="return value * 2")
_DEVELOP_CHANGED_OTHER = _mod(designated_body="return value + 1", other_body="return value * 9")
_DEVELOP_CHANGED_DESIGNATED = _mod(
    designated_body="return value + 1 + 100", other_body="return value * 2"
)
#: What a clean rebase of the PR onto `_DEVELOP_CHANGED_DESIGNATED` produces:
#: both edits live in the same function.
_REBASED_ONTO_BOTH = _mod(designated_body="return value + 7 + 100", other_body="return value * 2")


@pytest.fixture
def covers_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A develop line, a receipted PR head, and the four later states of it."""
    _init_fixture_repo(tmp_path)
    shas: dict[str, str] = {}
    (tmp_path / "pyproject.toml").write_text('version = "0.22.51"\n')
    (tmp_path / "mod.py").write_text(_V0)
    _run_git(tmp_path, "add", "-A")
    _run_git(tmp_path, "commit", "-m", "develop base")
    shas["d1"] = _rev_parse(tmp_path)

    # The PR, and the commit the eval ran at.
    _run_git(tmp_path, "checkout", "-q", "-b", "feature")
    shas["receipted"] = _commit_file(tmp_path, "mod.py", _PR_CHANGE, "PR changes designated()")

    # (e) a comment-only follow-up commit inside the designated function.
    shas["comment_only"] = _commit_file(
        tmp_path,
        "mod.py",
        _mod(
            designated_body="return value + 7",
            other_body="return value * 2",
            comment="# reworded comment",
        ),
        "comment-only follow-up",
    )

    # the merge wrapper's version bump, on top of the receipted head.
    _run_git(tmp_path, "checkout", "-q", "-B", "bump", shas["receipted"])
    shas["version_bump"] = _commit_file(
        tmp_path, "pyproject.toml", 'version = "0.22.52"\n', "chore: bump version"
    )

    # (d) a new commit that changes a designated symbol again.
    _run_git(tmp_path, "checkout", "-q", "-B", "more", shas["receipted"])
    shas["changed_again"] = _commit_file(
        tmp_path,
        "mod.py",
        _mod(designated_body="return value + 8", other_body="return value * 2"),
        "change designated() again",
    )

    # (a) develop advances without touching the surface; merged into the PR.
    _run_git(tmp_path, "checkout", "-q", "develop")
    _commit_file(tmp_path, "README.md", "docs\n", "docs on develop")
    shas["d2_docs"] = _rev_parse(tmp_path)
    _run_git(tmp_path, "checkout", "-q", "-B", "merged_docs", shas["receipted"])
    _run_git(tmp_path, "merge", "--no-ff", "-m", "merge develop (docs)", shas["d2_docs"])
    shas["merged_docs"] = _rev_parse(tmp_path)

    # (b) develop changes a designated symbol this PR never touched; the PR
    #     rebases onto it.
    _run_git(tmp_path, "checkout", "-q", "develop")
    shas["d3_other"] = _commit_file(
        tmp_path, "mod.py", _DEVELOP_CHANGED_OTHER, "develop changes other()"
    )
    _run_git(tmp_path, "checkout", "-q", "-B", "rebased_other", shas["d3_other"])
    shas["rebased_other"] = _commit_file(
        tmp_path,
        "mod.py",
        _mod(designated_body="return value + 7", other_body="return value * 9"),
        "PR change, rebased onto develop's other() change",
    )

    # (c) develop changes the SAME symbol the PR changed; the PR rebases onto
    #     it, so the head carries both edits.
    _run_git(tmp_path, "checkout", "-q", "develop")
    shas["d4_designated"] = _commit_file(
        tmp_path, "mod.py", _DEVELOP_CHANGED_DESIGNATED, "develop changes designated()"
    )
    _run_git(tmp_path, "checkout", "-q", "-B", "rebased_same", shas["d4_designated"])
    shas["rebased_same"] = _commit_file(
        tmp_path, "mod.py", _REBASED_ONTO_BOTH, "PR change, rebased onto develop's designated()"
    )

    monkeypatch.chdir(tmp_path)
    return shas


def _covers_at(shas: dict[str, str], head: str, base: str) -> tuple[bool, list[str]]:
    return receipt_covers_head(
        shas["receipted"],
        shas[head],
        _COVERS_SURFACE,
        check_llm_surface_receipt.git_paths_changed_between,
        base_sha=shas[base],
    )


def test_covers_a_develop_merge_with_no_surface_change(covers_repo: dict[str, str]) -> None:
    """(a) The case athenaeum#2206 already handled, at symbol granularity."""
    assert _covers_at(covers_repo, "merged_docs", "d2_docs") == (True, [])


def test_covers_a_rebase_onto_a_symbol_this_pr_never_touched(
    covers_repo: dict[str, str],
) -> None:
    """(b) The refinement athenaeum#2225 exists for — athenaeum#2206 called this stale."""
    assert _covers_at(covers_repo, "rebased_other", "d3_other") == (True, [])
    # Positive control: without `base_sha` the predicate degrades to
    # athenaeum#2206's path diff, which CANNOT tell this PR's change from
    # develop's and so calls the same head stale. If that came back covered
    # too, the assertion above would be proving nothing.
    assert receipt_covers_head(
        covers_repo["receipted"],
        covers_repo["rebased_other"],
        _COVERS_SURFACE,
        check_llm_surface_receipt.git_paths_changed_between,
    ) == (False, ["mod.py"])  # the fallback names paths, not symbols


def test_does_not_cover_a_rebase_where_develop_changed_the_same_symbol(
    covers_repo: dict[str, str],
) -> None:
    """(c) Clause 2 requires the PR to have changed `s` at NEITHER point."""
    covered, stale = _covers_at(covers_repo, "rebased_same", "d4_designated")
    assert not covered
    assert stale == ["mod.py::designated"]


def test_does_not_cover_a_new_commit_changing_a_designated_symbol(
    covers_repo: dict[str, str],
) -> None:
    """(d) The receipt is genuinely stale; this is the case that must still fail."""
    covered, stale = _covers_at(covers_repo, "changed_again", "d1")
    assert not covered
    assert stale == ["mod.py::designated"]


def test_covers_a_comment_only_follow_up_commit(covers_repo: dict[str, str]) -> None:
    """(e) Comments never reach the AST, so they cannot cost a re-dispatch."""
    assert _covers_at(covers_repo, "comment_only", "d1") == (True, [])


def test_does_not_cover_an_unreadable_receipt_commit(covers_repo: dict[str, str]) -> None:
    """(f) Fail closed: an unknown receipt commit proves nothing."""
    covered, stale = receipt_covers_head(
        "0" * 40,
        covers_repo["merged_docs"],
        _COVERS_SURFACE,
        check_llm_surface_receipt.git_paths_changed_between,
        base_sha=covers_repo["d2_docs"],
    )
    assert (covered, stale) == (False, [])
    # ...and with the fetch stubbed out as failing, likewise — the real
    # `git_ensure_commit` call above has no reachable `origin` in the fixture,
    # so this pins the refusal on the injected lookup rather than on luck.
    assert receipt_covers_head(
        covers_repo["receipted"],
        covers_repo["merged_docs"],
        _COVERS_SURFACE,
        check_llm_surface_receipt.git_paths_changed_between,
        base_sha=covers_repo["d2_docs"],
        ensure_commit=lambda sha: False,
    ) == (False, [])


def test_covers_a_version_bump_only_head(covers_repo: dict[str, str]) -> None:
    """athenaeum#2206's behaviour, preserved: `pyproject.toml` is not designated."""
    assert _covers_at(covers_repo, "version_bump", "d1") == (True, [])


def test_covers_fails_closed_when_a_merge_base_cannot_be_resolved(
    covers_repo: dict[str, str],
) -> None:
    assert receipt_covers_head(
        covers_repo["receipted"],
        covers_repo["merged_docs"],
        _COVERS_SURFACE,
        check_llm_surface_receipt.git_paths_changed_between,
        base_sha=covers_repo["d2_docs"],
        merge_base_lookup=lambda base, sha: None,
    ) == (False, [])


def test_covers_uses_the_union_of_both_ends_designations(covers_repo: dict[str, str]) -> None:
    """A PR that drops a designation cannot thereby cover a change to it."""
    covered, stale = receipt_covers_head(
        covers_repo["receipted"],
        covers_repo["changed_again"],
        ["mod.py::other"],  # D(head): the PR deleted its own designation
        check_llm_surface_receipt.git_paths_changed_between,
        base_sha=covers_repo["d1"],
        receipt_surface=_COVERS_SURFACE,  # D(receipt) still has it
    )
    assert not covered
    assert stale == ["mod.py::designated"]


def test_the_stale_message_names_the_symbols(covers_repo: dict[str, str]) -> None:
    """athenaeum#2225: the failure text names WHAT made the receipt stale."""
    run = {
        "headSha": covers_repo["receipted"],
        "event": "workflow_dispatch",
        "workflowName": "Evals",
        "conclusion": "success",
    }
    result = check_llm_surface_receipt.check_receipt(
        pr_body=f"Evals: {RUN_URL}",
        head_sha=covers_repo["changed_again"],
        this_repo=REPO,
        run_lookup=lambda repo, run_id: run,
        surface=_COVERS_SURFACE,
        base_sha=covers_repo["d1"],
    )
    assert not result.ok
    assert "mod.py::designated" in result.message
    assert "mod.py::other" not in result.message


def test_the_dispatch_rule_appears_exactly_once_in_a_failure_message() -> None:
    """Seer review on PR athenaeum#2244: it was attached twice for a stale receipt.

    athenaeum#2228 put `DISPATCH_RULE` in `check_receipt`'s stale branch AND in
    `evaluate`'s trailing instruction, so the stale path printed the rule
    twice. `evaluate` is the one place that attaches it now; the stale branch
    states only the diagnosis.
    """
    for message in (
        _evaluate_with(["CHANGELOG.md", "src/athenaeum/tiers.py"]).message,
        _evaluate_with(None).message,
        evaluate(
            changed_files=["src/athenaeum/tiers.py"],
            surface=SURFACE,
            pr_body="no receipt",
            head_sha=HEAD_SHA,
            this_repo=REPO,
            run_lookup=_unreached_lookup,
        ).message,
    ):
        assert message.lower().count(_RULE_KEY_PHRASE) == 1, message
