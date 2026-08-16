# SPDX-License-Identifier: Apache-2.0
"""Deterministic decay sweep for expired ``bucket: daily`` wiki pages (issue athenaeum#904, AC6).

The other half of athenaeum#904's decay-bucket slice: intake/`remember()`/shape-rules
can tag a page ``bucket: daily`` and suggest a ``valid_until`` (`athenaeum.models`,
`athenaeum.mcp_server.remember_write`, `athenaeum.rules`); recall's currency
ranking (`athenaeum.mcp_server._is_deprioritized_for_currency`) deprioritizes
an EXPIRED one so it stops competing with current facts. This module is the
THIRD leg: a periodic, **fully deterministic, zero-LLM-call** sweep that
actually removes an expired daily-bucket page from the live wiki tree —
"a rapidly-overwritten daily status collapses to its latest value plus git
history, instead of N stale pages competing in recall" (issue text).

**No LLM calls — structurally, not just in practice.** Every function in this
module has NO ``client``/``provider``/model parameter anywhere in its
signature — there is nothing here for an LLM call to hang off of. Contrast
with :mod:`athenaeum.merge`'s C4 contradiction detector or the reasoning
tiers, which thread an explicit ``client: LLMBackend | None`` through their
call chains; this module has no such parameter because it makes no such
call. ``tests/test_decay_sweep.py::TestNoLLMCalls`` asserts this via
``inspect.signature``.

**Archive (git-rm), not tombstone.** AC6 says "archives or tombstones" — this
module picks git-rm removal, for three reasons:

1. It is the EXISTING precedent this codebase already uses for exactly this
   shape of removal (:func:`athenaeum.auto_memory_prune.apply_prune`,
   :func:`athenaeum.corrections.retire_batch`, and the freshest example,
   :func:`athenaeum.pending_merges._apply_fold_into_existing`, brought up to
   the two-commit convention in athenaeum#947). Following it here, rather than
   inventing a tombstone shape, is what "do not invent a new one" (the
   athenaeum#904 design brief) asks for.
2. A tombstone (an in-tree stub page, e.g. ``archived: true``) would still be
   a candidate row for the FTS5/vector/keyword index and would need its own
   new filtering logic threaded through every recall backend to keep it from
   ever surfacing as a hit — exactly the "second storage surface" /
   parallel-mechanism sprawl the issue's Out of scope section forbids ("Same
   wiki, marked differently — there is no second store").
3. "Collapses to its latest value plus git history" (the issue's own framing
   of the intended effect) literally describes removal-from-tree +
   git-recoverable history, not an in-tree marker.

**Two-commit + refuse-without-git**, mirroring
:func:`athenaeum.pending_merges._apply_fold_into_existing` exactly (issue
athenaeum#947 is the freshest instance of this discipline in the repo):
Commit A snapshots the kill-list's CURRENT on-disk content (in case a page
was written/edited since its last commit) before anything is touched; Commit
B is the ``git rm`` + removal commit. :func:`apply_sweep` refuses outright —
never degrades to a bare ``Path.unlink()`` — when ``knowledge_root`` is not a
git repository, exactly like :func:`athenaeum.auto_memory_prune.apply_prune`.

**Sweeps ONLY expired ``daily``-bucket pages** (AC6 is explicit). ``weekly``/
``durable``/unbucketed pages are never touched — this module has no code
path that can select one. "Expired" reuses the EXISTING athenaeum#308
:func:`athenaeum.models.valid_until_expired` predicate — the same one recall's
currency ranking uses — never a parallel validity concept.

Layering: L4 domain/pipeline, mirroring :mod:`athenaeum.auto_memory_prune`
exactly (dry-run-by-default report + separate apply, ``git rm`` in one
labeled commit pair, refuse-without-git).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from athenaeum.models import parse_bucket, parse_frontmatter, valid_until_expired

log = logging.getLogger(__name__)


@dataclass
class SweepCandidate:
    """One expired ``bucket: daily`` page slated for archival, with its reason."""

    path: Path
    reason: str


@dataclass
class SweepReport:
    """Outcome of a sweep pass (dry-run or apply)."""

    kill: list[SweepCandidate] = field(default_factory=list)
    retained: list[tuple[Path, str]] = field(default_factory=list)
    scanned: int = 0
    applied: bool = False
    committed: bool = False
    errors: list[str] = field(default_factory=list)


def discover_daily_bucket_pages(wiki_root: Path) -> list[Path]:
    """Return sorted wiki pages carrying ``bucket: daily`` in frontmatter.

    Shallow scan (mirrors :mod:`athenaeum.auto_memory_prune`'s ``wiki/*.md``
    convention) — the underscore-prefixed operational subtree
    (``_pending_questions.md`` etc.) is never a candidate.
    """
    if not wiki_root.is_dir():
        return []
    candidates: list[Path] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _body = parse_frontmatter(text)
        if parse_bucket(meta) == "daily":
            candidates.append(path)
    return candidates


def build_sweep_report(
    wiki_root: Path,
    *,
    as_of: date | None = None,
) -> SweepReport:
    """Classify every ``bucket: daily`` page into kill vs retained (issue athenaeum#904, AC6).

    A page joins the kill-list only when it is BOTH ``bucket: daily`` AND
    expired (:func:`athenaeum.models.valid_until_expired` against *as_of*,
    default today) — a daily-bucket page with no ``valid_until`` (or one not
    yet passed) is retained, exactly matching the fail-open athenaeum#308 posture
    ("absent valid_until => open upper bound => currently valid").

    Makes ZERO LLM calls — no ``client``/model parameter exists on this
    function to make one with (see module docstring).
    """
    report = SweepReport()
    for path in discover_daily_bucket_pages(wiki_root):
        report.scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append(f"{path.name}: unreadable ({exc})")
            report.retained.append((path, "unreadable - retained for safety"))
            continue
        meta, _body = parse_frontmatter(text)
        if valid_until_expired(meta, as_of):
            reason = (
                f"bucket: daily, expired (valid_until={meta.get('valid_until')!r})"
            )
            report.kill.append(SweepCandidate(path, reason))
        else:
            report.retained.append((path, "bucket: daily, not yet expired"))
    return report


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` with ``cwd=root``. ``check=False`` — callers inspect
    ``.returncode`` themselves (matches :func:`athenaeum.corrections._git` /
    :func:`athenaeum.pending_merges._git`, both of which need this because
    ``git diff --cached --quiet``'s DELIBERATE nonzero-on-diff exit code
    would otherwise raise on the exact call this module needs to inspect,
    not treat as a failure).
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def apply_sweep(
    knowledge_root: Path,
    report: SweepReport,
) -> SweepReport:
    """Archive the kill-list via a two-commit git-rm (issue athenaeum#904, AC6/AC7).

    Mirrors :func:`athenaeum.pending_merges._apply_fold_into_existing`'s
    Commit A (provenance snapshot) / Commit B (``git rm`` + removal) shape —
    see the module docstring for why this convention rather than a new one.
    Refuses to act (never degrades to a bare ``unlink``, exactly like
    :func:`athenaeum.auto_memory_prune.apply_prune`) when *knowledge_root* is
    not a git repository. A no-op (no commit) when the kill-list is empty.
    Mutates and returns *report*.
    """
    if not report.kill:
        log.info("decay-sweep: kill-list empty - nothing to archive")
        return report

    if not (knowledge_root / ".git").exists():
        msg = (
            f"no .git in {knowledge_root} - refusing to sweep (archival is "
            "git-only for recoverability, issue athenaeum#904 AC7)"
        )
        log.warning("decay-sweep: %s", msg)
        report.errors.append(msg)
        return report

    kr = knowledge_root.resolve()
    rel_paths: list[str] = []
    for cand in report.kill:
        try:
            rel_paths.append(str(cand.path.resolve().relative_to(kr)))
        except ValueError:
            report.errors.append(
                f"{cand.path.name}: outside knowledge_root - not swept"
            )
    if not rel_paths:
        return report

    # Commit A — provenance snapshot BEFORE any removal (issue athenaeum#947
    # convention): stages exactly the kill-list paths (never `git add -A`, so
    # an operator's unrelated pre-staged work is never swept in under this
    # commit's message) and commits only if something is actually staged —
    # the common case, a page already fully committed from a prior run, is a
    # legitimate no-op here, not an error.
    add_result = _git(knowledge_root, "add", "--", *rel_paths)
    if add_result.returncode != 0:
        msg = f"git add failed during decay sweep: {add_result.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return report
    staged = _git(knowledge_root, "diff", "--cached", "--quiet", "--", *rel_paths)
    if staged.returncode != 0:
        commit_a = _git(
            knowledge_root,
            "commit",
            "-m",
            f"chore(decay-sweep): provenance snapshot before archiving "
            f"{len(rel_paths)} expired daily-bucket page(s) (athenaeum#904)",
            "--",
            *rel_paths,
        )
        if commit_a.returncode != 0:
            msg = f"provenance-snapshot commit failed: {commit_a.stderr.strip()}"
            log.error("decay-sweep: %s", msg)
            report.errors.append(msg)
            return report

    # Commit B — the archival itself.
    rm_result = _git(knowledge_root, "rm", "--quiet", "--", *rel_paths)
    if rm_result.returncode != 0:
        msg = f"git rm failed during decay sweep: {rm_result.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return report
    commit_b = _git(
        knowledge_root,
        "commit",
        "-m",
        f"chore(decay-sweep): archive {len(rel_paths)} expired "
        f"daily-bucket page(s) (athenaeum#904)",
        "--",
        *rel_paths,
    )
    if commit_b.returncode != 0:
        msg = f"archive commit failed: {commit_b.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return report
    report.applied = True
    report.committed = True
    log.info(
        "decay-sweep: git-archived %d expired daily-bucket page(s); committed",
        len(rel_paths),
    )
    return report
