#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Token-free eval-receipt gate (issue athenaeum#1731).

Computes a PR's changed paths against the LLM surface list
(``.github/llm-surface.txt``); if the intersection is non-empty, requires
either an ``Evals: <run-url>`` receipt in the PR body pointing at a matching
``evals.yml`` ``workflow_dispatch`` run that actually SUCCEEDED, or an
explicit ``Evals: not needed — <reason>`` line.

Every network/`gh` call is isolated behind the ``run_lookup`` callable so
``tests/test_llm_surface_receipt.py`` can drive the whole decision offline —
see that module for the injected-stub pattern. The default lookup
(``gh_run_lookup``) shells out to
``gh run view --json headSha,event,workflowName,conclusion``, which is a
local read against the default ``GITHUB_TOKEN`` — no Anthropic key, no push
trigger, matching the athenaeum#1731 AC2 constraint.

Four independent checks gate a receipt URL, each with its own failure
message (Quine's athenaeum#1745 review round): the run must be a
``workflow_dispatch`` of the ``Evals`` workflow (``workflowName`` —
otherwise a dispatch of e.g. ``ci.yml`` at the right SHA would satisfy the
gate), its ``headSha`` must match the PR head's LLM-surface content (below),
and its ``conclusion`` must
be ``success`` (a failed or cancelled eval run is not a receipt that
anything actually passed).

The ``headSha`` check is on content, not on the exact commit (cost: every
update-branch, merge-wrapper bump or rebase used to demand a fresh paid eval
dispatch even when nothing an eval reads had changed). A run at commit X
satisfies PR head H when X == H, or when ``git diff --name-only X H`` — a
direct tree-to-tree diff, so it holds for a non-ancestor X too (a rebase, a
force-push) — contains no path on the LLM surface: then every surface file
is byte-identical between what was evaluated and what would merge. That
covers the merge wrapper's PATCH bump, merges from ``develop`` that leave
the surface alone, and rebases. If X is not in the checkout (an old head a
force-push orphaned), it is fetched by sha; if it still cannot be read, the
receipt is treated as stale. See ``receipt_covers_head``.

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

#: The workflow this gate's receipt must point at (evals.yml's `name:`).
EVALS_WORKFLOW_NAME = "Evals"


def _dispatch_hint(repo: str) -> str:
    """Build the dispatch instructions from the caller's own `--repo`, not a hardcoded slug."""
    return f"gh workflow run evals.yml --repo {repo}"


#: Matches an `Evals: <run-url>` receipt line. Anchored to line start
#: (tolerating leading whitespace) so it cannot be satisfied by a mention
#: buried mid-sentence. Case-insensitive like the not-needed form below, so
#: `evals:`/`EVALS:` are honored the same as `Evals:`.
_RECEIPT_URL_RE = re.compile(r"^\s*Evals:\s*(https?://\S+)\s*$", re.MULTILINE | re.IGNORECASE)

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
    """Default `run_lookup`: `gh run view --json headSha,event,workflowName,conclusion`.

    Local `gh` read against the default `GITHUB_TOKEN` — no Anthropic key,
    no network beyond the GitHub API `gh` already talks to.
    """
    result = subprocess.run(
        [
            "gh", "run", "view", run_id, "--repo", repo,
            "--json", "headSha,event,workflowName,conclusion",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh run view {run_id} --repo {repo} failed: {result.stderr.strip()}")
    import json

    data = json.loads(result.stdout)
    return {
        "headSha": data["headSha"],
        "event": data["event"],
        "workflowName": data["workflowName"],
        "conclusion": data["conclusion"],
    }


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def git_paths_changed_between(receipt_sha: str, head_sha: str) -> list[str] | None:
    """Default `paths_changed_lookup`: ``git diff --name-only receipt head``.

    Two-dot on purpose: this compares the two TREES directly (what was
    evaluated vs. what would merge), so it is correct whether or not
    `receipt_sha` is an ancestor of `head_sha`. A receipt commit missing from
    the checkout (an old head orphaned by a force-push or rebase) is fetched
    by sha first. Returns ``None`` when either commit cannot be read, which
    the caller treats as stale.
    """
    if _git("cat-file", "-e", f"{receipt_sha}^{{commit}}").returncode != 0:
        _git("fetch", "--no-tags", "--quiet", "origin", receipt_sha)
    diff = _git("diff", "--name-only", receipt_sha, head_sha)
    if diff.returncode != 0:
        return None
    return [line for line in diff.stdout.splitlines() if line]


def receipt_covers_head(
    receipt_sha: str,
    head_sha: str,
    surface: list[str],
    paths_changed_lookup: Callable[[str, str], list[str] | None],
) -> tuple[bool, list[str]]:
    """Does a receipt at `receipt_sha` still describe `head_sha`?

    True when they are the same commit, or when no path changed between them
    is on the LLM surface. Returns ``(covered, surface_paths_changed)``; the
    second element names what made it stale (empty when covered, or when the
    diff could not be read at all).
    """
    if receipt_sha == head_sha:
        return True, []
    changed = paths_changed_lookup(receipt_sha, head_sha)
    if changed is None:
        return False, []
    hits = intersect_surface(changed, surface)
    return (not hits), hits


def check_receipt(
    pr_body: str,
    head_sha: str,
    this_repo: str,
    run_lookup: Callable[[str, str], dict[str, str]],
    surface: list[str] | None = None,
    paths_changed_lookup: Callable[[str, str], list[str] | None] = git_paths_changed_between,
) -> ReceiptCheck:
    """Decide whether `pr_body` carries a valid eval receipt for `head_sha`.

    The run must be at `head_sha`, or at a commit whose LLM-surface files are
    identical to `head_sha`'s (see `receipt_covers_head`). With no `surface`
    given, only the exact head is accepted.
    """
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

    if run.get("workflowName") != EVALS_WORKFLOW_NAME:
        return ReceiptCheck(
            False,
            f"run {run_id} belongs to workflow {run.get('workflowName')!r}, not "
            f"{EVALS_WORKFLOW_NAME!r} — the receipt must point at an evals.yml run.",
        )
    if run.get("event") != "workflow_dispatch":
        return ReceiptCheck(
            False,
            f"run {run_id} was triggered by {run.get('event')!r}, not "
            "`workflow_dispatch` — dispatch a fresh run.",
        )
    receipt_sha = run.get("headSha") or ""
    if receipt_sha != head_sha:
        if surface is None:
            covered, stale_hits = False, []
        else:
            covered, stale_hits = receipt_covers_head(
                receipt_sha, head_sha, surface, paths_changed_lookup
            )
        if not covered:
            why = (
                "LLM-surface files changed since that commit: " + ", ".join(stale_hits)
                if stale_hits
                else "the receipt commit could not be compared with the head"
            )
            return ReceiptCheck(
                False,
                f"run {run_id}'s headSha ({receipt_sha!r}) does not match "
                f"this PR's head ({head_sha!r}) and {why} — the dispatch is "
                "stale, re-run it.",
            )
    if run.get("conclusion") != "success":
        return ReceiptCheck(
            False,
            f"run {run_id} concluded {run.get('conclusion')!r}, not `success` "
            "— a failed or cancelled eval run is not a receipt.",
        )
    if receipt_sha != head_sha:
        return ReceiptCheck(
            True,
            f"valid eval receipt: run {run_id} at {receipt_sha}; no LLM-surface "
            f"file differs between it and head {head_sha} "
            f"(conclusion: {run.get('conclusion')})",
        )
    return ReceiptCheck(
        True,
        f"valid eval receipt: run {run_id} at head {head_sha} "
        f"(conclusion: {run.get('conclusion')})",
    )


def evaluate(
    changed_files: list[str],
    surface: list[str],
    pr_body: str,
    head_sha: str,
    this_repo: str,
    run_lookup: Callable[[str, str], dict[str, str]] = gh_run_lookup,
    paths_changed_lookup: Callable[[str, str], list[str] | None] = git_paths_changed_between,
) -> ReceiptCheck:
    """Full decision: empty intersection passes trivially; otherwise require a receipt."""
    hits = intersect_surface(changed_files, surface)
    if not hits:
        return ReceiptCheck(True, "no LLM surface touched — no receipt needed.")

    result = check_receipt(
        pr_body, head_sha, this_repo, run_lookup, surface, paths_changed_lookup
    )
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
        f"move a result:\n\n  {_dispatch_hint(this_repo)}",
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
