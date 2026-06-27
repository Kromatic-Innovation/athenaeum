# SPDX-License-Identifier: Apache-2.0
"""Prune existing operational ``wiki/auto-*.md`` pages (issue #278, Part 2).

Part 1 (the ephemeral-intake classifier) stops NEW operational session notes
from becoming durable wiki entities. This driver retires the ones already on
disk: it builds a precise kill-list of operational ``type: auto-memory`` pages
using the SAME classifier (:func:`athenaeum.ephemeral.classify_ephemeral_page`)
-- NOT loose single-keyword matching -- and, on ``--apply``, ``git rm``s only
the listed files in one labeled commit so the removal is fully git-recoverable.

Dry-run is the DEFAULT (mirrors ``repair`` / ``dedupe``): it prints the full
kill-list AND the retained-list with per-page reasons for human sign-off and
writes nothing.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from athenaeum.ephemeral import classify_ephemeral_page
from athenaeum.models import parse_frontmatter

log = logging.getLogger("athenaeum")

# Compiled merged auto-memory pages are named ``auto-<topic-slug>.md`` (mirrors
# :data:`athenaeum.merge.AUTO_WIKI_PREFIX`). Re-declared here so the prune
# driver does not import the whole merge module just for one constant.
AUTO_WIKI_PREFIX = "auto-"

AUTO_MEMORY_TYPE = "auto-memory"


@dataclass
class PruneCandidate:
    """One ``wiki/auto-*.md`` page slated for removal, with its reason."""

    path: Path
    reason: str


@dataclass
class PruneReport:
    """Outcome of a prune pass (dry-run or apply)."""

    kill: list[PruneCandidate] = field(default_factory=list)
    retained: list[tuple[Path, str]] = field(default_factory=list)
    scanned: int = 0
    applied: bool = False
    committed: bool = False
    errors: list[str] = field(default_factory=list)


def discover_auto_pages(wiki_root: Path) -> list[Path]:
    """Return sorted ``wiki/auto-*.md`` files (shallow; skips subdirs)."""
    if not wiki_root.is_dir():
        return []
    return sorted(p for p in wiki_root.glob(f"{AUTO_WIKI_PREFIX}*.md") if p.is_file())


def build_prune_report(
    wiki_root: Path,
    *,
    ephemeral_scopes: list[str],
    operational_markers: list[str],
) -> PruneReport:
    """Classify every ``wiki/auto-*.md`` page into kill vs retained lists.

    A page joins the kill-list only when
    :func:`athenaeum.ephemeral.classify_ephemeral_page` returns a reason
    (explicit ``ephemeral: true`` flag, ALL origin scopes ephemeral, or a
    multi-signal operational-marker match). Everything else -- including any
    page that is not ``type: auto-memory`` -- is retained, with a reason.
    """
    report = PruneReport()
    for path in discover_auto_pages(wiki_root):
        report.scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append(f"{path.name}: unreadable ({exc})")
            report.retained.append((path, "unreadable - retained for safety"))
            continue
        meta, body = parse_frontmatter(text)
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("type") != AUTO_MEMORY_TYPE:
            report.retained.append(
                (path, f"not type:auto-memory (type={meta.get('type')!r})")
            )
            continue
        reason = classify_ephemeral_page(
            meta,
            body,
            ephemeral_scopes=ephemeral_scopes,
            operational_markers=operational_markers,
        )
        if reason is not None:
            report.kill.append(PruneCandidate(path, reason))
        else:
            report.retained.append((path, "no ephemeral/operational signal"))
    return report


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


def apply_prune(
    knowledge_root: Path,
    report: PruneReport,
) -> PruneReport:
    """``git rm`` the kill-list in one labeled commit (issue #278).

    Removal is git-only (recoverable). Refuses to act without a writable git
    repo -- mirrors the move-then-retire safety contract. A no-op (no commit)
    when the kill-list is empty. Mutates and returns *report*.
    """
    if not report.kill:
        log.info("prune: kill-list empty - nothing to remove")
        return report

    if not (knowledge_root / ".git").exists():
        msg = (
            f"no .git in {knowledge_root} - refusing to prune (removal is "
            "git-only for recoverability)"
        )
        log.warning("prune: %s", msg)
        report.errors.append(msg)
        return report

    kr = knowledge_root.resolve()
    rel_paths: list[str] = []
    for cand in report.kill:
        try:
            rel_paths.append(str(cand.path.resolve().relative_to(kr)))
        except ValueError:
            report.errors.append(
                f"{cand.path.name}: outside knowledge_root - not pruned"
            )
    if not rel_paths:
        return report

    _git(knowledge_root, "rm", "--quiet", "--", *rel_paths)
    _git(
        knowledge_root,
        "commit",
        "-m",
        f"chore(auto-memory): prune {len(rel_paths)} operational "
        f"auto-memory page(s) (#278)",
    )
    report.applied = True
    report.committed = True
    log.info(
        "prune: git-removed %d operational auto-memory page(s); committed",
        len(rel_paths),
    )
    return report
