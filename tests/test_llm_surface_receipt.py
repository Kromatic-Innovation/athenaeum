# SPDX-License-Identifier: Apache-2.0
"""Offline tests for `scripts/check_llm_surface_receipt.py` (issue athenaeum#1731).

Every case here drives the pure `evaluate`/`check_receipt` functions with an
injected `run_lookup` stub — no `gh` subprocess, no network, no real PR. This
is the offline exercise athenaeum#1731 AC3 asks for: docs-only passes,
`tiers.py`-without-receipt fails, and a valid receipt passes; plus the two
free near-misses (wrong event, stale SHA) the same machinery makes easy to
add.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
    return {"headSha": HEAD_SHA, "event": "workflow_dispatch"}


def _wrong_event_lookup(repo: str, run_id: str) -> dict[str, str]:
    return {"headSha": HEAD_SHA, "event": "push"}


def _stale_sha_lookup(repo: str, run_id: str) -> dict[str, str]:
    return {"headSha": "b" * 40, "event": "workflow_dispatch"}


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
    other_repo_url = "https://github.com/someone-else/fork/actions/runs/1"
    result = evaluate(
        changed_files=["src/athenaeum/tiers.py"],
        surface=SURFACE,
        pr_body=f"Evals: {other_repo_url}",
        head_sha=HEAD_SHA,
        this_repo=REPO,
        run_lookup=_unreached_lookup,
    )
    assert not result.ok


def test_intersect_surface_directory_prefix_matches() -> None:
    surface = ["tests/evals/"]
    hits = intersect_surface(["tests/evals/data/foo.json", "tests/test_search.py"], surface)
    assert hits == ["tests/evals/data/foo.json"]


def test_intersect_surface_exact_entry_does_not_substring_match() -> None:
    surface = ["src/athenaeum/search.py"]
    hits = intersect_surface(["tests/test_search.py"], surface)
    assert hits == []
