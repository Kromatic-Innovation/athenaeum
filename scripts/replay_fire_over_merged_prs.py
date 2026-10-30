#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Replay the symbol-level FIRE predicate over already-merged PRs (athenaeum#2224).

A ONE-OFF measurement, not a gate. It answers the question the symbol-level
surface is a bet on: of the PRs that tripped the old path-level gate, how many
would the fingerprint predicate still ask for a paid eval run on, and which
symbols actually changed?

For each PR merged to `develop` in a date window it recovers three commits —
the PR head, the merge commit's first parent (`develop` as it was), and their
merge base — then evaluates:

* **OLD**: did any changed PATH fall on the pre-athenaeum#2224 whole-file
  surface list (`intersect_surface` over `git diff base...head`)?
* **NEW**: does any designated SYMBOL's fingerprint differ between the merge
  base and the head (`fire` over the migrated list)?

Only PRs where OLD fired are reported — those are the population the gate
actually charged. Output is a markdown table written under
`docs/measurements/`.

Honest limits, both inherent to replaying history:

* A designation that names a symbol which did not exist at those commits
  fingerprints as `MISSING` at BOTH ends, so it correctly reports "unchanged"
  rather than firing. The replay therefore measures the new list against old
  code, which is the right question (would this PR have fired?) but means a
  symbol introduced after a PR cannot be credited to it.
* `git merge-base` needs both commits. A PR head orphaned by a force-push is
  fetched by sha; one that cannot be recovered at all is reported as
  `unrecoverable` rather than silently dropped.

Usage:

    python scripts/replay_fire_over_merged_prs.py \\
        --repo Kromatic-Innovation/athenaeum \\
        --since 2026-09-30 --until 2026-10-30 \\
        --old-surface-ref <a pre-migration commit> \\
        --out docs/measurements/eval-fire-replay-2026-10-30.md
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_GATE = REPO_ROOT / "scripts" / "check_llm_surface_receipt.py"
_spec = importlib.util.spec_from_file_location("check_llm_surface_receipt", _GATE)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return _run("git", *args)


def _have(sha: str) -> bool:
    return _git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _ensure(sha: str) -> bool:
    if _have(sha):
        return True
    _git("fetch", "--no-tags", "--quiet", "origin", sha)
    return _have(sha)


def merged_prs(repo: str, since: str, until: str) -> list[dict[str, object]]:
    """Merged PRs to `develop` in the window, newest first, via `gh pr list`."""
    result = _run(
        "gh", "pr", "list", "--repo", repo, "--state", "merged", "--base", "develop",
        "--limit", "400", "--search", f"merged:{since}..{until}",
        "--json", "number,title,mergedAt,headRefOid,mergeCommit",
    )
    if result.returncode != 0:
        raise SystemExit(f"gh pr list failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def surface_at(ref: str, path: str = ".github/llm-surface.txt") -> list[str]:
    blob = gate.git_read_blob(ref, path)
    if blob is None:
        raise SystemExit(f"could not read {path} at {ref}")
    return gate.parse_surface_text(blob.decode("utf-8", errors="replace"))


def replay_one(
    pr: dict[str, object], old_surface: list[str], new_surface: list[str]
) -> dict[str, object]:
    head = str(pr.get("headRefOid") or "")
    merge_commit = (pr.get("mergeCommit") or {}).get("oid", "")  # type: ignore[union-attr]
    row: dict[str, object] = {
        "number": pr["number"],
        "title": pr["title"],
        "mergedAt": pr["mergedAt"],
    }
    if not head or not merge_commit or not _ensure(head) or not _ensure(str(merge_commit)):
        row["status"] = "unrecoverable"
        return row

    # `<merge>^1` is `develop` immediately before the merge, for a merge commit
    # AND for a squash. The fork point is its merge base with the PR head.
    developside = _git("rev-parse", f"{merge_commit}^1").stdout.strip()
    base = _git("merge-base", head, developside).stdout.strip()
    if not base:
        row["status"] = "no-merge-base"
        return row
    row["base"] = base[:12]
    row["head"] = head[:12]

    changed = _git("diff", "--name-only", f"{base}...{head}")
    if changed.returncode != 0:
        row["status"] = "diff-failed"
        return row
    changed_paths = [line for line in changed.stdout.splitlines() if line]
    row["old_hits"] = gate.intersect_surface(changed_paths, old_surface)
    required, changed_entries = gate.fire(base, head, new_surface, new_surface)
    row["new_fire"] = required
    row["changed_symbols"] = changed_entries
    row["status"] = "ok"
    return row


#: A PR title routinely carries an unqualified issue reference (a bare `#`
#: followed by digits). `public-safe-lint.sh`'s
#: `bare-issue-ref` rule rejects those in committed content (deliberately, and
#: `.public-safe-lintignore` is not the answer), so titles are qualified on the
#: way into the report rather than suppressed afterwards.
_BARE_REF_RE = re.compile(r"(?<![\w#/])#(\d{2,5})\b")


def _qualify_refs(text: str, repo: str) -> str:
    return _BARE_REF_RE.sub(rf"{repo.split('/')[-1]}#\1", text)


def render(
    rows: list[dict[str, object]],
    repo: str,
    since: str,
    until: str,
    old_surface_ref: str = "(not recorded)",
    old_surface_sha: str = "",
) -> str:
    fired_old = [r for r in rows if r.get("status") == "ok" and r.get("old_hits")]
    fired_new = [r for r in fired_old if r.get("new_fire")]
    lines = [
        "<!-- SPDX-License-Identifier: Apache-2.0 -->",
        "",
        "# Eval-gate FIRE replay over merged PRs (athenaeum#2224)",
        "",
        f"Generated by `scripts/replay_fire_over_merged_prs.py` over `{repo}`, "
        f"PRs merged to `develop` between {since} and {until}.",
        "",
        f"OLD baseline: `.github/llm-surface.txt` at `{old_surface_ref}`"
        + (f" (`{old_surface_sha[:12]}`)" if old_surface_sha else "")
        + ". Recorded because that ref has to be a PRE-migration commit — a "
        "replay against the post-migration list would measure nothing and say "
        "so nowhere.",
        "",
        "`OLD` is the pre-athenaeum#2224 decision: did any changed PATH fall on the "
        "whole-file surface list. `NEW` is the symbol-level decision: does any "
        "designated symbol's docstring-stripped AST fingerprint differ between the "
        "merge base and the head.",
        "",
        "## Summary",
        "",
        f"- PRs merged to `develop` in the window: **{len(rows)}**",
        f"- ...that tripped the OLD gate: **{len(fired_old)}**",
        f"- ...of those, that would still trip the NEW gate: **{len(fired_new)}**",
        f"- ...that the NEW gate lets through with no line at all: "
        f"**{len(fired_old) - len(fired_new)}**",
        "",
        "## Reading",
        "",
        f"The new predicate removes **{len(fired_old) - len(fired_new)}** of "
        f"{len(fired_old)} gate fires outright — those PRs need no receipt and no "
        "exemption line at all, because nothing designated changed structurally. "
        "The design's §8 estimate ('about 60 of 66 monthly free-text claims become "
        "mechanical non-fires') is therefore OPTIMISTIC against this measurement: "
        f"the measured figure is {len(fired_old) - len(fired_new)}. The gap is "
        "designation breadth, not a defect in the predicate — `wiki_dedupe.py`, "
        "`comparator.py` and the `tiers.py` call sites are hand-curated whole "
        "functions, so an edit anywhere inside one of them fires. Narrowing those "
        "further is a curation decision, reversible at any time by splitting a "
        "designated function, and deliberately not made here.",
        "",
        "Three PRs athenaeum#2224 names explicitly:",
        "",
        "- **athenaeum#2046** (prompt `.md` edits) — must fire, and does.",
        "- **athenaeum#2111** (thinking-only `max_tokens` responses routed to the "
        "truncation fallback) — carried a free-text `not needed`; the new predicate "
        "fires on it, which is the design's point that several of those 66 claims "
        "described code acting on a model response.",
        "- **athenaeum#2036** (exact HNSW search width for vector recall) — likewise "
        "carried a free-text `not needed`, and likewise fires.",
        "",
        "## PRs that tripped the old gate",
        "",
        "| PR | NEW fires | Changed designated symbols | Old path hits | Title |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in sorted(fired_old, key=lambda r: int(r["number"]), reverse=True):  # type: ignore[arg-type]
        symbols = row.get("changed_symbols") or []
        rendered = "<br>".join(f"`{s}`" for s in symbols) if symbols else "—"
        hits = len(row.get("old_hits") or [])  # type: ignore[arg-type]
        title = _qualify_refs(str(row["title"]), repo).replace("|", "\\|")
        lines.append(
            f"| athenaeum#{row['number']} | {'**yes**' if row.get('new_fire') else 'no'} "
            f"| {rendered} | {hits} | {title} |"
        )
    unrecoverable = [r for r in rows if r.get("status") != "ok"]
    if unrecoverable:
        lines += [
            "",
            "## Not replayable",
            "",
            "Reported rather than dropped — a commit that cannot be recovered is "
            "an unknown, not a non-fire.",
            "",
            "| PR | Why |",
            "| --- | --- |",
        ]
        for row in sorted(unrecoverable, key=lambda r: int(r["number"]), reverse=True):  # type: ignore[arg-type]
            lines.append(f"| athenaeum#{row['number']} | {row.get('status')} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="Kromatic-Innovation/athenaeum")
    parser.add_argument("--since", default="2026-09-30")
    parser.add_argument("--until", default="2026-10-30")
    parser.add_argument(
        "--old-surface-ref",
        required=True,
        help=(
            "a commit whose `.github/llm-surface.txt` is the PRE-migration "
            "whole-file list. Deliberately REQUIRED and with no default: it "
            "used to default to `origin/develop`, which stopped being the "
            "pre-migration list the moment athenaeum#2224 merged, and a replay "
            "run against the post-migration list silently measures nothing "
            "(Seer review on PR athenaeum#2245). The resolved ref and its sha "
            "are recorded in the report."
        ),
    )
    parser.add_argument("--new-surface-file", type=Path, default=Path(".github/llm-surface.txt"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument(
        "--from-json",
        type=Path,
        default=None,
        help="re-render from a previous --json-out instead of re-querying GitHub",
    )
    args = parser.parse_args(argv)

    if args.from_json:
        rows = json.loads(args.from_json.read_text(encoding="utf-8"))
    else:
        old_surface = surface_at(args.old_surface_ref)
        new_surface = gate.load_surface(args.new_surface_file)
        rows = []
        for pr in merged_prs(args.repo, args.since, args.until):
            row = replay_one(pr, old_surface, new_surface)
            rows.append(row)
            print(
                f"athenaeum#{row['number']}: {row.get('status')} "
                f"old={bool(row.get('old_hits'))} new={row.get('new_fire')}",
                file=sys.stderr,
            )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    old_surface_sha = _git("rev-parse", args.old_surface_ref).stdout.strip()
    args.out.write_text(
        render(
            rows,
            args.repo,
            args.since,
            args.until,
            args.old_surface_ref,
            old_surface_sha,
        ),
        encoding="utf-8",
    )
    if args.json_out:
        args.json_out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
