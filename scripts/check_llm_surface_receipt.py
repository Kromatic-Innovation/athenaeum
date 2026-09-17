#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Token-free eval-receipt gate (issue athenaeum#1731).

Computes a PR's changed paths against the LLM surface list
(``.github/llm-surface.txt``); if the intersection is non-empty, requires
either an ``Evals: <run-url>`` receipt in the PR body pointing at a matching
``evals.yml`` ``workflow_dispatch`` run, or an explicit
``Evals: not needed — <reason>`` line.

Every network/`gh` call is isolated behind the ``run_lookup`` callable so
``tests/test_llm_surface_receipt.py`` can drive the whole decision offline —
see that module for the injected-stub pattern. The default lookup
(``gh_run_lookup``) shells out to ``gh run view --json headSha,event``,
which is a local read against the default ``GITHUB_TOKEN`` — no Anthropic
key, no push trigger, matching the athenaeum#1731 AC2 constraint.

CLI entry point is only used by the workflow; the importable functions are
the unit under test.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DISPATCH_HINT = "gh workflow run evals.yml --repo Kromatic-Innovation/athenaeum"

#: Matches an `Evals: <run-url>` receipt line. Anchored to line start
#: (tolerating leading whitespace) so it cannot be satisfied by a mention
#: buried mid-sentence.
_RECEIPT_URL_RE = re.compile(r"^\s*Evals:\s*(https?://\S+)\s*$", re.MULTILINE)

#: Matches an `Evals: not needed — <reason>` line. Accepts an em-dash, an
#: en-dash, or one/two hyphens as the separator, and requires a non-empty
#: reason — a bare "not needed" with nothing after it does not satisfy the
#: gate.
_NOT_NEEDED_RE = re.compile(
    r"^\s*Evals:\s*not needed\s*[-–—]+\s*(\S.*\S|\S)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

#: A run URL must point at THIS repo's actions/runs/<id> — a run pasted
#: from a fork or another repo does not satisfy the gate.
_RUN_URL_RE = re.compile(
    r"^https://github\.com/(?P<repo>[^/]+/[^/]+)/actions/runs/(?P<run_id>\d+)/?$"
)


@dataclass(frozen=True)
class ReceiptCheck:
    """Outcome of evaluating a PR body's receipt against the touched surface."""

    ok: bool
    message: str


def load_surface(path: Path) -> list[str]:
    """Read `.github/llm-surface.txt`: one path/prefix per line, `#` comments and blanks skipped."""
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def _matches(changed_path: str, surface_entry: str) -> bool:
    if surface_entry.endswith("/"):
        return changed_path == surface_entry.rstrip("/") or changed_path.startswith(surface_entry)
    return changed_path == surface_entry


def intersect_surface(changed_files: list[str], surface: list[str]) -> list[str]:
    """Changed paths that fall on the LLM surface, in `changed_files` order, de-duplicated."""
    hits: list[str] = []
    seen: set[str] = set()
    for changed in changed_files:
        if changed in seen:
            continue
        if any(_matches(changed, entry) for entry in surface):
            hits.append(changed)
            seen.add(changed)
    return hits


def gh_run_lookup(repo: str, run_id: str) -> dict[str, str]:
    """Default `run_lookup`: `gh run view --json headSha,event` against `repo`.

    Local `gh` read against the default `GITHUB_TOKEN` — no Anthropic key,
    no network beyond the GitHub API `gh` already talks to.
    """
    result = subprocess.run(
        ["gh", "run", "view", run_id, "--repo", repo, "--json", "headSha,event"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh run view {run_id} --repo {repo} failed: {result.stderr.strip()}")
    import json

    data = json.loads(result.stdout)
    return {"headSha": data["headSha"], "event": data["event"]}


def check_receipt(
    pr_body: str,
    head_sha: str,
    this_repo: str,
    run_lookup: Callable[[str, str], dict[str, str]],
) -> ReceiptCheck:
    """Decide whether `pr_body` carries a valid eval receipt for `head_sha`."""
    not_needed = _NOT_NEEDED_RE.search(pr_body)
    if not_needed:
        return ReceiptCheck(True, f"Evals not needed: {not_needed.group(1).strip()}")

    receipt = _RECEIPT_URL_RE.search(pr_body)
    if not receipt:
        return ReceiptCheck(
            False,
            "no `Evals:` receipt found in the PR body — add either "
            "`Evals: <evals.yml run-url>` or `Evals: not needed — <reason>`.",
        )

    url = receipt.group(1)
    m = _RUN_URL_RE.match(url)
    if not m:
        return ReceiptCheck(
            False,
            f"`Evals:` line does not point at a GitHub Actions run URL: {url!r}",
        )
    if m.group("repo") != this_repo:
        return ReceiptCheck(
            False,
            f"`Evals:` run URL points at {m.group('repo')!r}, not {this_repo!r}.",
        )
    run_id = m.group("run_id")

    try:
        run = run_lookup(this_repo, run_id)
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the operator
        return ReceiptCheck(False, f"could not look up run {run_id}: {exc}")

    if run.get("event") != "workflow_dispatch":
        return ReceiptCheck(
            False,
            f"run {run_id} was triggered by {run.get('event')!r}, not "
            "`workflow_dispatch` — dispatch a fresh run.",
        )
    if run.get("headSha") != head_sha:
        return ReceiptCheck(
            False,
            f"run {run_id}'s headSha ({run.get('headSha')!r}) does not match "
            f"this PR's head ({head_sha!r}) — the dispatch is stale, re-run it.",
        )
    return ReceiptCheck(True, f"valid eval receipt: run {run_id} at head {head_sha}")


def evaluate(
    changed_files: list[str],
    surface: list[str],
    pr_body: str,
    head_sha: str,
    this_repo: str,
    run_lookup: Callable[[str, str], dict[str, str]] = gh_run_lookup,
) -> ReceiptCheck:
    """Full decision: empty intersection passes trivially; otherwise require a receipt."""
    hits = intersect_surface(changed_files, surface)
    if not hits:
        return ReceiptCheck(True, "no LLM surface touched — no receipt needed.")

    result = check_receipt(pr_body, head_sha, this_repo, run_lookup)
    if result.ok:
        return result
    touched = "\n".join(f"  - {h}" for h in hits)
    return ReceiptCheck(
        False,
        "PR touches the LLM surface but carries no valid eval receipt.\n\n"
        f"Touched surface files:\n{touched}\n\n"
        f"{result.message}\n\n"
        "Dispatch the eval suite, then add `Evals: <run-url>` to the PR "
        "body, or add `Evals: not needed — <reason>` if this change cannot "
        f"move a result:\n\n  {DISPATCH_HINT}",
    )


def _git_changed_files(base_sha: str, head_sha: str) -> list[str]:
    """Paths changed on this branch since it diverged from `base_sha`.

    Three-dot (`base...head`), NOT two-dot: two-dot diffs `base_sha` against
    `head_sha` directly, so once `develop` (the PR's base ref) advances past
    the commit this branch was cut from, every commit that landed on
    `develop` in the meantime — and isn't in this branch — shows up too
    (as a reversal), spuriously widening the surface intersection for a PR
    that never touched those files. Three-dot diffs `merge-base(base, head)`
    against `head`, the same set GitHub's own path filters use. Requires
    `fetch-depth: 0` (or enough history to reach the merge-base) in the
    calling workflow's checkout step.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface-file", type=Path, default=Path(".github/llm-surface.txt"))
    parser.add_argument("--base-sha", required=True, help="PR base commit SHA")
    parser.add_argument("--head-sha", required=True, help="PR head commit SHA")
    parser.add_argument("--pr-body-file", type=Path, required=True)
    parser.add_argument(
        "--repo", required=True, help="owner/repo, e.g. Kromatic-Innovation/athenaeum"
    )
    args = parser.parse_args(argv)

    surface = load_surface(args.surface_file)
    changed_files = _git_changed_files(args.base_sha, args.head_sha)
    pr_body = args.pr_body_file.read_text(encoding="utf-8")

    result = evaluate(changed_files, surface, pr_body, args.head_sha, args.repo)
    print(result.message)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
