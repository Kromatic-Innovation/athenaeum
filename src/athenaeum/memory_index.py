# SPDX-License-Identifier: Apache-2.0
"""Per-scope ``MEMORY.md`` index maintenance (issue #388).

Each ``raw/auto-memory/<scope>/`` directory carries a curated ``MEMORY.md``
index — one ``- [Title](file.md) — hook`` pointer per raw member. Unlike the
compiled ``wiki/auto-*.md`` pages, ``MEMORY.md`` is loaded into **every** Claude
Code session's context for its scope, so a stale line keeps asserting a fact
long after the file it points at is gone.

Move-then-retire (:mod:`athenaeum.retire`) ``git rm``\\s a retired member but
historically never rewrote the sibling index, so every retirement left a
dangling pointer behind. This module supplies the two pieces that close that
gap:

- :func:`index_line_target` / :func:`rewrite_index` — the pure parsing
  primitives used *inline* by the retire pass to drop a just-retired member's
  pointer in the same commit as the deletion (prevents NEW dangling pointers).
- :func:`build_dangling_report` / :func:`apply_prune_index` — a one-shot
  backfill over EXISTING dangling pointers (``athenaeum auto-memory
  prune-index``), committed separately so its diff stays reviewable.

Both paths only ever *drop* a sibling pointer whose target ``.md`` is gone;
non-pointer lines and pointers into other trees (``../wiki/x.md``, URLs) are
preserved verbatim. Rewrites are git-tracked and therefore recoverable.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("athenaeum")

# The per-scope index file name (mirrors the ``MEMORY.md`` skip-name every
# intake scanner already excludes — see ``librarian._AUTO_MEMORY_SKIP_NAMES``
# and ``search._INTAKE_SKIP_NAMES``).
INDEX_FILENAME = "MEMORY.md"

# A ``MEMORY.md`` pointer line references a sibling raw member with a markdown
# link whose target is a bare ``<file>.md`` (no directory, no anchor). The
# writing convention is ``- [Title](file.md) — hook`` but we only require the
# ``](<target>.md)`` link so table rows and other shapes still parse.
_LINK_RE = re.compile(r"\]\(([^)]+?\.md)\)")


def index_line_target(line: str) -> str | None:
    """Return the bare *sibling* ``.md`` target a MEMORY.md line points at, else None.

    Only sibling pointers — a bare filename with no path separator, URL scheme,
    or anchor fragment — are returned, because those are the raw members that
    retirement removes from the same directory. A link that already points
    elsewhere (``../wiki/x.md``, ``https://…``, ``foo.md#frag``) is deliberately
    left alone: it is not a dangling sibling pointer and must never be swept.
    """
    m = _LINK_RE.search(line)
    if m is None:
        return None
    target = m.group(1).strip()
    # Sibling pointers only: reject anything with a path separator, a scheme /
    # anchor (``:`` covers ``http:`` and Windows drive refs; ``#`` an anchor).
    if "/" in target or ":" in target or "#" in target:
        return None
    return target


def rewrite_index(
    text: str, should_drop: Callable[[str], bool]
) -> tuple[str, list[str]]:
    """Drop index lines whose sibling target satisfies ``should_drop``.

    Returns ``(new_text, dropped_targets)``. A memory is one line per the
    ``MEMORY.md`` convention, so a matching line is removed whole. Non-pointer
    lines (headings, prose, blank lines) are preserved verbatim, and the file's
    line-ending shape is preserved because we split with ``keepends=True``.
    """
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    dropped: list[str] = []
    for line in lines:
        target = index_line_target(line)
        if target is not None and should_drop(target):
            dropped.append(target)
            continue
        kept.append(line)
    return "".join(kept), dropped


# ---------------------------------------------------------------------------
# Backfill: prune EXISTING dangling pointers (issue #388, one-shot sweep)
# ---------------------------------------------------------------------------


@dataclass
class ScopeIndexPrune:
    """One scope's ``MEMORY.md`` with the dangling pointers it would lose."""

    index_path: Path
    dangling: list[str]
    total_pointers: int
    new_text: str


@dataclass
class DanglingReport:
    """Outcome of a prune-index pass (dry-run or apply)."""

    scopes: list[ScopeIndexPrune] = field(default_factory=list)
    scanned_indexes: int = 0
    applied: bool = False
    committed: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def total_dangling(self) -> int:
        return sum(len(s.dangling) for s in self.scopes)


def build_dangling_report(intake_roots: list[Path]) -> DanglingReport:
    """Scan every ``<scope>/MEMORY.md`` for sibling pointers with no file on disk.

    A pointer is *dangling* when its bare ``<file>.md`` target does not exist in
    the same scope directory — exactly the residue move-then-retire left behind
    before #388. Scopes with no dangling pointer are omitted from the report
    (nothing to rewrite). Unreadable indexes are recorded as errors and skipped
    (conservative: never rewrite what we could not fully read).
    """
    report = DanglingReport()
    for root in intake_roots:
        if not root.is_dir():
            continue
        for scope_dir in sorted(root.iterdir()):
            if not scope_dir.is_dir():
                continue
            index_path = scope_dir / INDEX_FILENAME
            if not index_path.is_file():
                continue
            report.scanned_indexes += 1
            try:
                text = index_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                report.errors.append(f"{scope_dir.name}/{INDEX_FILENAME}: unreadable ({exc})")
                continue
            total = sum(
                1 for line in text.splitlines() if index_line_target(line) is not None
            )
            new_text, dropped = rewrite_index(
                text, lambda t, d=scope_dir: not (d / t).exists()
            )
            if dropped:
                report.scopes.append(
                    ScopeIndexPrune(index_path, dropped, total, new_text)
                )
    return report


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def apply_prune_index(knowledge_root: Path, report: DanglingReport) -> DanglingReport:
    """Rewrite the dangling-carrying indexes and commit them in one labeled commit.

    Mirrors the move-then-retire / auto-memory-prune safety contract: refuses to
    act without a writable git repo (the rewrite must be recoverable), stages
    and commits ONLY the rewritten index paths (a scoped pathspec, so unrelated
    working-tree edits can never be swept into the "prune index pointers"
    commit), and no-ops when there is nothing dangling. Mutates and returns
    *report*.
    """
    if not report.scopes:
        log.info("prune-index: no dangling pointers — nothing to rewrite")
        return report

    if not (knowledge_root / ".git").exists():
        msg = (
            f"no .git in {knowledge_root} — refusing to prune index (the "
            "rewrite must be git-recoverable)"
        )
        log.warning("prune-index: %s", msg)
        report.errors.append(msg)
        return report

    kr = knowledge_root.resolve()
    rel_paths: list[str] = []
    for scope in report.scopes:
        try:
            rel = str(scope.index_path.resolve().relative_to(kr))
        except ValueError:
            report.errors.append(
                f"{scope.index_path.parent.name}/{INDEX_FILENAME}: outside "
                "knowledge_root — not pruned"
            )
            continue
        scope.index_path.write_text(scope.new_text, encoding="utf-8")
        rel_paths.append(rel)

    if not rel_paths:
        return report

    total = sum(len(s.dangling) for s in report.scopes)
    try:
        _git(knowledge_root, "add", "--", *rel_paths)
        _git(
            knowledge_root,
            "commit",
            "-m",
            f"chore(auto-memory): prune {total} dangling MEMORY.md pointer(s) "
            f"across {len(rel_paths)} scope(s) (#388)",
            "--",
            *rel_paths,
        )
    except subprocess.CalledProcessError as exc:
        msg = (
            "git operation failed during prune-index "
            f"({' '.join(exc.cmd)!r}): {exc.stderr or exc}"
        )
        log.error("prune-index: %s", msg)
        report.errors.append(msg)
        return report
    report.applied = True
    report.committed = True
    log.info(
        "prune-index: rewrote %d index file(s) dropping %d dangling pointer(s); committed",
        len(rel_paths),
        total,
    )
    return report
