# SPDX-License-Identifier: Apache-2.0
"""Retire existing wiki entities minted from filenames / paths (issue athenaeum#680).

The write-side gate in :func:`athenaeum.tiers.is_code_artifact_name` stops NEW
code-artifact entities (``skill.md``, ``project-registry.yaml``,
``src/athenaeum/librarian.py``) from being created. This driver retires the
ones already on disk: it scans every top-level ``wiki/*.md`` entity page, reads
its frontmatter ``name``, and kills the page when that name is a code artifact
by the SAME predicate the creation gate uses — never a looser re-derivation.

Mirrors :mod:`athenaeum.auto_memory_prune` exactly (the "existing retire path"
issue athenaeum#680 asks for): dry-run is the DEFAULT (prints the kill-list with
per-page reasons and writes nothing); ``--apply`` ``git rm``\\s only the listed
files in one labeled, git-recoverable commit and refuses to act without a
writable git repo.

Layering: L4 driver. Reuses the L3 predicate in :mod:`athenaeum.tiers`; the
"is this a code artifact" rule lives THERE and must not be duplicated here.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.models import parse_frontmatter
from athenaeum.storage_migrate import iter_entity_pages
from athenaeum.tiers import classify_code_artifact_name

log = logging.getLogger(__name__)


@dataclass
class FilenameEntityCandidate:
    """One ``wiki/*.md`` entity page slated for removal, with its reason.

    ``rule`` is the matched-rule label from
    :func:`athenaeum.tiers.classify_code_artifact_name` (currently always
    ``"extension"`` — a bare path separator no longer kills, athenaeum#721) so a dry run
    can print, per entry, WHICH rule marked the page and an operator can audit
    the kill-list by class rather than by eyeball."""

    path: Path
    reason: str
    rule: str = "extension"


@dataclass
class FilenameEntityReport:
    """Outcome of a filename-entity prune pass (dry-run or apply)."""

    kill: list[FilenameEntityCandidate] = field(default_factory=list)
    retained: list[tuple[Path, str]] = field(default_factory=list)
    scanned: int = 0
    applied: bool = False
    committed: bool = False
    errors: list[str] = field(default_factory=list)


def _page_entity_name(meta: object, path: Path) -> str:
    """The entity name to test: frontmatter ``name`` if present, else the stem.

    Most entity pages carry a frontmatter ``name`` (``skill.md`` on
    ``919f0485-skill-md.md``). If it is missing/blank we fall back to the file
    stem so a page written without the field is still classifiable rather than
    silently retained."""
    if isinstance(meta, dict):
        name = meta.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return path.stem


def build_filename_entity_report(
    wiki_root: Path, *, config: dict[str, Any] | None = None
) -> FilenameEntityReport:
    """Classify every ``wiki/*.md`` entity page into kill vs retained lists.

    A page joins the kill-list only when its entity name is a code artifact per
    :func:`athenaeum.tiers.is_code_artifact_name` (the exact creation-gate
    predicate, so the operator allowlist / toggle apply identically). Unreadable
    pages are retained for safety and recorded as errors."""
    report = FilenameEntityReport()
    for path in iter_entity_pages(wiki_root):
        report.scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append(f"{path.name}: unreadable ({exc})")
            report.retained.append((path, "unreadable - retained for safety"))
            continue
        meta, _body = parse_frontmatter(text)
        name = _page_entity_name(meta, path)
        rule = classify_code_artifact_name(name, config)
        if rule is not None:
            report.kill.append(
                FilenameEntityCandidate(
                    path,
                    f"filename-derived entity (name={name!r}, rule={rule})",
                    rule=rule,
                )
            )
        else:
            report.retained.append((path, "not a code-artifact name"))
    return report


def kill_rule_counts(report: FilenameEntityReport) -> dict[str, int]:
    """Count kill-list entries per matched rule (issue athenaeum#721).

    The extension/non-extension split an operator confirms on the live dry run:
    a ``{"extension": N}`` breakdown of what ``prune-code-entities`` proposes to
    retire, by the rule that marked each page. Since athenaeum#721 removed the bare
    path-separator signal, ``extension`` is the only rule that ever fires — a
    non-``extension`` key appearing here would itself flag a regression."""
    counts: dict[str, int] = {}
    for cand in report.kill:
        counts[cand.rule] = counts.get(cand.rule, 0) + 1
    return counts


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def apply_filename_entity_prune(
    knowledge_root: Path, report: FilenameEntityReport
) -> FilenameEntityReport:
    """``git rm`` the kill-list in one labeled commit (issue athenaeum#680).

    Removal is git-only (recoverable) and refuses to act without a writable git
    repo — the same safety contract as :func:`athenaeum.auto_memory_prune.
    apply_prune`. A no-op when the kill-list is empty. The commit pathspec is
    scoped to the kill-list so unrelated staged changes are never swept in.
    Mutates and returns *report*."""
    if not report.kill:
        log.info("prune-filename-entities: kill-list empty - nothing to remove")
        return report

    if not (knowledge_root / ".git").exists():
        msg = (
            f"no .git in {knowledge_root} - refusing to prune (removal is "
            "git-only for recoverability)"
        )
        log.warning("prune-filename-entities: %s", msg)
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

    try:
        _git(knowledge_root, "rm", "--quiet", "--", *rel_paths)
        _git(
            knowledge_root,
            "commit",
            "-m",
            f"chore(wiki): retire {len(rel_paths)} filename-derived entity "
            f"page(s) (athenaeum#680)",
            "--",
            *rel_paths,
        )
    except subprocess.CalledProcessError as exc:
        msg = (
            "git operation failed during prune-filename-entities "
            f"({' '.join(exc.cmd)!r}): {exc.stderr or exc}"
        )
        log.error("prune-filename-entities: %s", msg)
        report.errors.append(msg)
        return report
    report.applied = True
    report.committed = True
    log.info(
        "prune-filename-entities: git-removed %d filename-derived entity "
        "page(s); committed",
        len(rel_paths),
    )
    return report
