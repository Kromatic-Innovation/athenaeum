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

Since athenaeum#2223 the decision is STRUCTURAL rather than path-based
(design of record: `docs/design/eval-trigger-surface.md` §5.1, §5.3, §5.4).
A surface entry may name a single symbol — `path::Name`,
`path::Class.method` or `path::CONSTANT` — and the gate asks whether that
symbol's *structure* differs between the merge base and the head, not
whether its file was touched. `fingerprint_entry` hashes a
docstring-stripped `ast.dump`, so a comment, a docstring or a reformat
cannot fire the gate, while a one-literal change inside a designated
function does. The whole-file and `dir/` prefix forms keep their meaning,
so the list is back-compatible and a rollback to whole-file entries needs
no code change.

`FIRE` is that predicate (`fire`): an eval is required iff some entry in
the UNION of the designations at the merge base and at the head has a
different fingerprint at the two commits. The union is what stops a PR
from hiding a change by deleting its own designation. Everything the
fingerprint reads comes from `git show <sha>:<path>` through injectable
lookups, so it needs no install and no import of the package under test —
the stdlib only, inside the existing `eval-receipt-check.yml` job.

CLI entry point is only used by the workflow; the importable functions are
the unit under test.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
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


#: Separates a path from the symbol it designates in a surface entry
#: (`src/athenaeum/tiers.py::parse_merge_ops_response`). Two colons, not
#: one, so a Windows-style drive letter or a `#`-comment colon can never be
#: mistaken for a designation.
SURFACE_SYMBOL_SEP = "::"


@dataclass(frozen=True)
class SurfaceEntry:
    """One parsed line of `.github/llm-surface.txt`.

    Three forms, all legal and all back-compatible with the pre-athenaeum#2223
    list (which had only the first two):

    * ``src/athenaeum/prompts/`` — a prefix: every path under it.
    * ``src/athenaeum/tiers.py`` — one whole file.
    * ``src/athenaeum/tiers.py::parse_merge_ops_response`` — one symbol
      inside a file: a top-level ``def``/``class``, a ``Class.method``, or a
      module-level assignment target (a constant).
    """

    raw: str
    path: str
    symbol: str | None

    @property
    def is_prefix(self) -> bool:
        return self.symbol is None and self.path.endswith("/")


def parse_surface_entry(entry: str) -> SurfaceEntry:
    """Split a surface line into its path and (optional) symbol.

    The ONE parser for the surface grammar: `tests/test_llm_surface.py` (the
    rot check) imports this rather than re-implementing it, so the two
    readers the surface file serves cannot drift apart.
    """
    path, sep, symbol = entry.partition(SURFACE_SYMBOL_SEP)
    if not sep:
        return SurfaceEntry(raw=entry, path=entry, symbol=None)
    return SurfaceEntry(raw=entry, path=path, symbol=symbol or None)


def parse_surface_text(text: str) -> list[str]:
    """Surface entries in `text`: one per line, `#` comments and blanks skipped."""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def load_surface(path: Path) -> list[str]:
    """Read `.github/llm-surface.txt`: one entry per line, `#` comments and blanks skipped.

    Entries are returned verbatim (`path`, `dir/` or `path::symbol`);
    `parse_surface_entry` is what interprets one.
    """
    return parse_surface_text(path.read_text(encoding="utf-8"))


def _matches(changed_path: str, surface_entry: str) -> bool:
    """Does `changed_path` fall on `surface_entry`'s FILE?

    Path-level only: a `path::symbol` entry matches any change to that path.
    That is deliberately coarser than `fire` — this predicate feeds the
    human-readable "touched surface files" list in a failure message (and
    the rot check's prompt-file coverage test), where naming the file is
    the useful thing. Whether an eval is actually required is
    `fire`/`fingerprint_entry`'s decision, not this one's.
    """
    entry = parse_surface_entry(surface_entry)
    if entry.is_prefix:
        return changed_path == entry.path.rstrip("/") or changed_path.startswith(entry.path)
    return changed_path == entry.path


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


#: Fingerprint of an entry whose path, or whose named symbol, does not exist
#: at that commit. Compared like any other fingerprint, so a rename or a
#: deletion reads as a change (fail closed) rather than as "nothing to see".
MISSING = "MISSING"

#: Fingerprint of a `.py` path that does not parse at that commit. At the
#: HEAD side this always counts as changed — see `fire`.
UNPARSEABLE = "UNPARSEABLE"

#: Node types that own a docstring and can be named by a surface entry.
_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def git_read_blob(sha: str, path: str) -> bytes | None:
    """Default blob lookup: `git show <sha>:<path>`, raw bytes, `None` if absent.

    Bytes, not text: `ast.parse` handles a source encoding declaration
    itself, and a non-`.py` entry is hashed byte-for-byte.
    """
    result = subprocess.run(
        ["git", "show", f"{sha}:{path}"], capture_output=True, check=False
    )
    if result.returncode != 0:
        return None
    return result.stdout


def git_list_tree(sha: str, prefix: str) -> list[str] | None:
    """Default prefix lookup: every path under `prefix` at `sha`, or `None` if unreadable."""
    result = _git("ls-tree", "-r", "--name-only", sha, "--", prefix)
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def _strip_docstrings(node: ast.AST) -> ast.AST:
    """Remove module, class and function docstrings, in place, recursively.

    A docstring is the leading string-constant expression of one of those
    bodies. Removing it is what makes the fingerprint ignore prose: comments
    and formatting never reach the AST in the first place, but a docstring
    does. A body that was ONLY a docstring becomes `pass`, so the symbol
    still fingerprints to something stable.
    """
    if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = list(body[1:]) or [ast.Pass()]
    for child in ast.iter_child_nodes(node):
        _strip_docstrings(child)
    return node


def _symbol_nodes(body: list[ast.stmt], name: str) -> list[ast.stmt]:
    """Statements in `body` that define `name`: a def/class, or an assignment to it.

    A name can be defined more than once — an overload pair, or a constant
    built up in several statements (`prompt_registry.py:_META_ROWS`). All of
    them are returned, in source order, and all of them are hashed: a change
    to any one is a change to the designated symbol.
    """
    found: list[ast.stmt] = []
    for stmt in body:
        if isinstance(stmt, _DEF_NODES) and stmt.name == name:
            found.append(stmt)
        elif isinstance(stmt, ast.Assign):
            if any(_target_names(target) & {name} for target in stmt.targets):
                found.append(stmt)
        elif isinstance(stmt, ast.AnnAssign | ast.AugAssign):
            if _target_names(stmt.target) & {name}:
                found.append(stmt)
    return found


def _target_names(target: ast.expr) -> set[str]:
    """Plain names bound by an assignment target, including tuple unpacking."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Tuple | ast.List):
        names: set[str] = set()
        for element in target.elts:
            names |= _target_names(element)
        return names
    return set()


def _resolve_symbol(module: ast.Module, symbol: str) -> list[ast.stmt] | None:
    """Nodes designated by `symbol`, or `None` when it does not resolve.

    `Name` looks in the module body; `Outer.inner` looks in the body of the
    top-level class `Outer`. Deeper dotted paths are not part of the
    grammar, so they never resolve (and therefore read as `MISSING`, which
    the D5 rot check in athenaeum#2224 turns into a CI failure rather than a
    silent pass).
    """
    head, sep, tail = symbol.partition(".")
    if not sep:
        return _symbol_nodes(module.body, head) or None
    if "." in tail:
        return None
    for stmt in module.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == head:
            return _symbol_nodes(stmt.body, tail) or None
    return None


def _sha256_hex(*chunks: bytes) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
        digest.update(b"\x00")
    return digest.hexdigest()


def fingerprint_entry(
    entry: str,
    sha: str,
    blob_lookup: Callable[[str, str], bytes | None] = git_read_blob,
    tree_lookup: Callable[[str, str], list[str] | None] = git_list_tree,
) -> str:
    """Structural fingerprint of one surface entry at one commit.

    * `path::symbol` in a `.py` file — sha256 of the docstring-stripped
      `ast.dump(..., include_attributes=False)` of every statement that
      defines the symbol. No line numbers (that is what
      `include_attributes=False` buys), no docstrings, no comments, no
      formatting: only structure.
    * a whole `.py` file — the same, over the whole module.
    * a whole non-`.py` file — the byte hash.
    * a `dir/` prefix — the byte hash of every file under it, keyed by path,
      so an added, removed or edited file all show up.

    Returns `MISSING` or `UNPARSEABLE` (above) rather than raising. Hashes
    are only ever compared with other hashes computed in the SAME process:
    `ast.dump` output is not stable across Python versions, so a persisted
    fingerprint would be a false-change generator (design §5.3).
    """
    parsed = parse_surface_entry(entry)
    if parsed.is_prefix:
        paths = tree_lookup(sha, parsed.path)
        if not paths:
            return MISSING
        chunks: list[bytes] = []
        for path in sorted(paths):
            data = blob_lookup(sha, path)
            chunks.append(path.encode("utf-8"))
            chunks.append(MISSING.encode("ascii") if data is None else _sha256_hex(data).encode())
        return _sha256_hex(*chunks)

    data = blob_lookup(sha, parsed.path)
    if data is None:
        return MISSING
    if not parsed.path.endswith(".py"):
        return _sha256_hex(data)
    try:
        module = ast.parse(data)
    except (SyntaxError, ValueError):
        return UNPARSEABLE
    if parsed.symbol is None:
        dumped = ast.dump(_strip_docstrings(module), include_attributes=False)
        return _sha256_hex(dumped.encode("utf-8"))
    nodes = _resolve_symbol(module, parsed.symbol)
    if nodes is None:
        return MISSING
    return _sha256_hex(
        *(
            ast.dump(_strip_docstrings(node), include_attributes=False).encode("utf-8")
            for node in nodes
        )
    )


def git_merge_base(base_sha: str, head_sha: str) -> str | None:
    """`git merge-base base head` — where this branch left the base ref, or `None`.

    The same three-dot semantics `_git_changed_files` relies on, named
    explicitly because `fire` has to READ files at that commit, not just
    diff against it.
    """
    result = _git("merge-base", base_sha, head_sha)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def fire(
    merge_base_sha: str,
    head_sha: str,
    base_surface: list[str],
    head_surface: list[str],
    fingerprint: Callable[[str, str], str] = fingerprint_entry,
) -> tuple[bool, list[str]]:
    """FIRE (design §5.4): is an eval required for this merge base -> head?

    Required iff some entry in the UNION of the designations at the merge
    base and at the head has a different fingerprint at the two commits.

    The union is the anti-gaming rule: dropping an entry from
    `.github/llm-surface.txt` in the same PR that changes the symbol it
    named leaves the entry in `D(merge_base)`, so the change still fires.
    `UNPARSEABLE` at head always counts as different — a head that does not
    parse has no structure to compare, and ordinary CI will fail it anyway.

    Returns `(required, changed_entries)`; `changed_entries` is sorted, so
    the failure message and athenaeum#2226's exemption set check see one
    canonical order.
    """
    changed: list[str] = []
    for entry in sorted(set(base_surface) | set(head_surface)):
        head_fp = fingerprint(entry, head_sha)
        if head_fp == UNPARSEABLE or fingerprint(entry, merge_base_sha) != head_fp:
            changed.append(entry)
    return bool(changed), changed


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
    *,
    merge_base_sha: str | None = None,
    base_surface: list[str] | None = None,
    fingerprint: Callable[[str, str], str] = fingerprint_entry,
) -> ReceiptCheck:
    """Full decision: no reasoning-surface change passes trivially; otherwise require a receipt.

    With `merge_base_sha` given (what `main` always does), the decision is
    `fire` — a structural comparison of every designated symbol between the
    merge base and the head, which ignores comments, docstrings and
    formatting.

    WITHOUT it the decision degrades to the pre-athenaeum#2223 path
    intersection over `changed_files`. That degradation is deliberate and
    safe in one direction only: a path match is strictly COARSER than a
    fingerprint comparison, so the gate asks for more receipts, never
    fewer. It is the path the offline unit tests drive (they have fake shas
    and no git repo to read), and the fallback when `git merge-base` cannot
    be resolved at all.
    """
    paths_touched = intersect_surface(changed_files, surface)
    if merge_base_sha is None:
        required, changed_entries = bool(paths_touched), []
    else:
        required, changed_entries = fire(
            merge_base_sha,
            head_sha,
            surface if base_surface is None else base_surface,
            surface,
            fingerprint,
        )
    if not required:
        return ReceiptCheck(True, "no LLM reasoning surface changed — no receipt needed.")

    result = check_receipt(
        pr_body, head_sha, this_repo, run_lookup, surface, paths_changed_lookup
    )
    if result.ok:
        return result
    detail = ""
    if changed_entries:
        listed = "\n".join(f"  - {entry}" for entry in changed_entries)
        detail = f"Changed reasoning surface:\n{listed}\n\n"
    elif paths_touched:
        listed = "\n".join(f"  - {h}" for h in paths_touched)
        detail = f"Touched surface files:\n{listed}\n\n"
    return ReceiptCheck(
        False,
        "PR changes the LLM reasoning surface but carries no valid eval "
        f"receipt.\n\n{detail}"
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

    # FIRE reads the designations at BOTH ends (design §5.4's union rule), so
    # the merge base's own copy of the surface file is read out of git rather
    # than assumed to equal the head's. An unreadable merge base, or a merge
    # base with no surface file yet, falls back to the head designations —
    # coarser, never weaker (see `evaluate`).
    merge_base_sha = git_merge_base(args.base_sha, args.head_sha)
    base_surface: list[str] | None = None
    if merge_base_sha is not None:
        base_blob = git_read_blob(merge_base_sha, str(args.surface_file))
        if base_blob is not None:
            base_surface = parse_surface_text(base_blob.decode("utf-8", errors="replace"))

    result = evaluate(
        changed_files,
        surface,
        pr_body,
        args.head_sha,
        args.repo,
        merge_base_sha=merge_base_sha,
        base_surface=base_surface,
    )
    print(result.message)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
