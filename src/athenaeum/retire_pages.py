# SPDX-License-Identifier: Apache-2.0
"""Generic wiki-page retirement by explicit uid list (issue athenaeum#1625).

``athenaeum decay-sweep`` (:mod:`athenaeum.decay_sweep`) has exactly the
right archive mechanics — dry-run default, a two-commit provenance
snapshot then ``git rm``, a recall-index rebuild — but the wrong selector:
it only ever picks expired ``bucket: daily`` pages
(:func:`athenaeum.decay_sweep.discover_daily_bucket_pages`). This module is
the generic counterpart: an operator names pages explicitly, by uid, and
this module retires exactly those.

**Reuse, not copy.** The two-commit git-archive mechanics are the SAME
function :func:`athenaeum.decay_sweep.apply_sweep` calls —
:func:`athenaeum.decay_sweep.archive_via_two_commit_git_rm`, factored out
of that function's body by this issue specifically so both callers share
one implementation. This module adds two things decay-sweep's kill-list
archive does not need: withdrawing pending-merge proposals that reference
a retired page (:func:`athenaeum.pending_merges.withdraw_pending_merges_for_retired_pages`)
and rebuilding ``wiki/_index.md`` (:func:`athenaeum.librarian.rebuild_index`)
— both folded into the SAME commit as the ``git rm`` (via
``archive_via_two_commit_git_rm``'s ``after_remove`` hook) so the whole
retirement still lands in exactly two commits total: Commit A (provenance
snapshot) and Commit B (git rm + index rebuild + pending-merge withdrawal).

**Recovery is git-only.** Exactly like decay-sweep (issue athenaeum#904 AC7):
a retired page's content lives in Commit A's tree — ``git show
<sha>:<path>`` recovers it — and this module writes no second store, no
tombstone, no ledger of its own. The recovery command is documented in the
CLI ``--help`` text (:mod:`athenaeum._cmd_retire`), per this issue's Plan
step 7.

**Unknown/ambiguous uids abort before any commit.** :func:`resolve_uids`
is pure read-only frontmatter scanning — it runs to completion (collecting
every bad uid, not just the first) before :func:`apply_retirement` ever
touches git, so a caller can refuse the whole batch up front.

Layering: L4 domain/pipeline, same layer as :mod:`athenaeum.decay_sweep`,
:mod:`athenaeum.pending_merges`, and :mod:`athenaeum.librarian` — all three
are imported here directly (same-layer imports are allowed; see
``tests/test_layer_boundary.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from athenaeum.decay_sweep import _git, archive_via_two_commit_git_rm
from athenaeum.librarian import rebuild_index
from athenaeum.models import parse_frontmatter
from athenaeum.pending_merges import (
    WithdrawnMergeProposal,
    withdraw_pending_merges_for_retired_pages,
)

log = logging.getLogger(__name__)


@dataclass
class RetireCandidate:
    """One uid resolved to exactly one wiki page."""

    uid: str
    path: Path


@dataclass
class UidResolutionError:
    """One uid that failed to resolve — either unknown or ambiguous."""

    uid: str
    reason: str  # "unknown" | "ambiguous"
    matches: list[Path] = field(default_factory=list)

    def __str__(self) -> str:
        if self.reason == "ambiguous":
            names = ", ".join(p.name for p in self.matches)
            return f"{self.uid}: ambiguous — matches {names}"
        return f"{self.uid}: unknown — no wiki page carries this uid"


@dataclass
class RetireReport:
    """Outcome of a retirement pass (dry-run or apply)."""

    kill: list[RetireCandidate] = field(default_factory=list)
    withdrawn_merges: list[WithdrawnMergeProposal] = field(default_factory=list)
    index_lines_removed: list[str] = field(default_factory=list)
    applied: bool = False
    committed: bool = False
    errors: list[str] = field(default_factory=list)


def resolve_uids(
    wiki_root: Path, uids: list[str]
) -> tuple[list[RetireCandidate], list[UidResolutionError]]:
    """Resolve every uid in *uids* to exactly one wiki page.

    A shallow ``wiki/*.md`` scan (mirrors
    :func:`athenaeum.decay_sweep.discover_daily_bucket_pages`'s convention;
    the underscore-prefixed operational subtree is never a candidate),
    reading each page's frontmatter ``uid:`` directly rather than going
    through :class:`athenaeum.models.EntityIndex` — that index silently
    skips a page with no ``name:`` field (see its ``_load`` docstring),
    which would make an otherwise-real uid look "unknown" for no reason a
    retirement operator could see. Every uid is checked, even after an
    earlier one fails, so a caller can report every bad uid in one pass
    (issue athenaeum#1625 Plan step 2: "refuse unknown or ambiguous uids
    before touching anything").

    Returns ``(candidates, errors)``. *candidates* has exactly one entry
    per uid that resolved uniquely, in *uids* order (duplicates in *uids*
    collapse to one candidate). *errors* is empty iff every uid resolved.
    """
    by_uid: dict[str, list[Path]] = {}
    if wiki_root.is_dir():
        for path in sorted(wiki_root.glob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _body = parse_frontmatter(text)
            uid = meta.get("uid")
            if isinstance(uid, str) and uid:
                by_uid.setdefault(uid, []).append(path)

    candidates: list[RetireCandidate] = []
    errors: list[UidResolutionError] = []
    seen: set[str] = set()
    for uid in uids:
        if uid in seen:
            continue
        seen.add(uid)
        matches = by_uid.get(uid, [])
        if not matches:
            errors.append(UidResolutionError(uid, "unknown"))
        elif len(matches) > 1:
            errors.append(UidResolutionError(uid, "ambiguous", matches=matches))
        else:
            candidates.append(RetireCandidate(uid=uid, path=matches[0]))
    return candidates, errors


def build_retire_report(knowledge_root: Path, candidates: list[RetireCandidate]) -> RetireReport:
    """Dry-run preview: the kill-list and the pending-merge proposals that
    reference each page — computed WITHOUT touching git or writing
    anything (issue athenaeum#1625 Plan step 3: "Dry run prints the
    kill-list, the pending merge proposals that reference each page, and
    the index lines that would be removed").

    Reuses :func:`athenaeum.pending_merges.withdraw_pending_merges_for_retired_pages`
    in its own dry-run mode (``apply=False``) rather than re-deriving the
    match logic, so the preview can never drift from what ``--apply``
    actually withdraws.
    """
    report = RetireReport(kill=list(candidates))
    wiki_root = knowledge_root / "wiki"
    retired_paths = [c.path.resolve() for c in candidates]
    merges_path = wiki_root / "_pending_merges.md"
    withdrawal = withdraw_pending_merges_for_retired_pages(
        merges_path, retired_paths, reason="(dry run preview)", apply=False
    )
    report.withdrawn_merges = withdrawal.withdrawn
    report.index_lines_removed = [c.path.name for c in candidates]
    return report


def apply_retirement(
    knowledge_root: Path,
    candidates: list[RetireCandidate],
    *,
    reason: str,
) -> RetireReport:
    """Retire *candidates* via the shared two-commit git-archive mechanics.

    Commit A snapshots the current on-disk content of every retired page
    (:func:`athenaeum.decay_sweep.archive_via_two_commit_git_rm`'s
    ``snapshot_message``). Commit B removes them (``git rm``) AND, in the
    same commit, withdraws every pending-merge proposal referencing a
    retired page and rebuilds ``wiki/_index.md`` — both performed by the
    ``after_remove`` hook, which runs after the pages are gone from disk
    (so :func:`athenaeum.librarian.rebuild_index`'s directory scan
    naturally excludes them) and stages its own changes before Commit B
    runs, so the whole retirement still lands in exactly two commits.

    A no-op (``report.committed is False``, no errors) when *candidates*
    is empty. Refuses (never mutates) when *knowledge_root* is not a git
    repository — see :func:`athenaeum.decay_sweep.archive_via_two_commit_git_rm`.
    """
    report = RetireReport(kill=list(candidates))
    if not candidates:
        return report

    wiki_root = knowledge_root / "wiki"
    kr = knowledge_root.resolve()
    pairs: list[tuple[RetireCandidate, str]] = []
    for cand in candidates:
        try:
            rel = str(cand.path.resolve().relative_to(kr))
        except ValueError:
            report.errors.append(f"{cand.path.name}: outside knowledge_root - not retired")
            continue
        pairs.append((cand, rel))
    if not pairs:
        return report
    rel_paths = [rel for _, rel in pairs]
    retired_paths = [cand.path.resolve() for cand, _ in pairs]
    merges_path = wiki_root / "_pending_merges.md"

    def _after_remove() -> tuple[list[str], str | None]:
        extra: list[str] = []

        try:
            withdrawal = withdraw_pending_merges_for_retired_pages(
                merges_path,
                retired_paths,
                reason=reason,
                apply=True,
            )
        except OSError as exc:
            return [], f"pending-merge withdrawal failed ({type(exc).__name__}): {exc}"
        report.withdrawn_merges = withdrawal.withdrawn
        if withdrawal.applied:
            extra.append(str(merges_path.resolve().relative_to(kr)))
            archive_path = merges_path.parent / "_pending_merges_archive.md"
            if archive_path.exists():
                extra.append(str(archive_path.resolve().relative_to(kr)))

        # Rebuild the wiki index now the retired pages are gone from disk
        # (`git rm` above already removed them) — mirrors the rebuild
        # `_cmd_decay._rebuild_recall_index` triggers, but for the entity
        # index rather than the recall search index (that one is rebuilt
        # separately, outside git — see `_cmd_retire.py`).
        rebuild_index(wiki_root)
        report.index_lines_removed = [p.name for p in retired_paths]
        extra.append(str((wiki_root / "_index.md").resolve().relative_to(kr)))

        add_result = _git(knowledge_root, "add", "--", *extra)
        if add_result.returncode != 0:
            return [], (
                f"git add failed for index/pending-merge updates: "
                f"{add_result.stderr.strip()}"
            )
        return extra, None

    archive_result = archive_via_two_commit_git_rm(
        knowledge_root,
        rel_paths,
        snapshot_message=(
            f"chore(retire-pages): provenance snapshot before retiring "
            f"{len(rel_paths)} page(s) (athenaeum#1625)"
        ),
        archive_message=(
            f"chore(retire-pages): retire {len(rel_paths)} page(s): {reason} "
            f"(athenaeum#1625)"
        ),
        after_remove=_after_remove,
    )
    report.errors.extend(archive_result.errors)
    if archive_result.committed:
        report.applied = True
        report.committed = True
        log.info(
            "retire-pages: retired %d page(s); committed",
            len(rel_paths),
        )
    return report
