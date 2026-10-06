# SPDX-License-Identifier: Apache-2.0
"""Fold-graph invariant lint (issue athenaeum#716).

The tombstone fold representation (issue athenaeum#716 lane A:
:func:`athenaeum.models.stamp_tombstone` / :func:`athenaeum.models.is_tombstone`
/ :func:`athenaeum.models.tombstone_target`) encodes a ``folded_into`` edge
from a tombstoned source page to its canonical. Two structural invariants
over that edge set must hold across the whole live store:

1. **Acyclic.** No page's ``folded_into`` chain ever loops back to a page
   already in the chain (including a direct self-loop).
2. **Exactly one live canonical per fold set.** Following every edge in one
   connected fold set (a tombstone, the chain of pages it was folded
   through, and the terminal canonical) must land on exactly ONE page that
   is NOT itself a tombstone. Zero live members means every page in the set
   is tombstoned (or the terminal canonical is simply missing from disk) —
   an orphaned fold set with nothing left to read. More than one live member
   means two different pages both present as "the" canonical for the same
   fold lineage — an ambiguous merge.

This module is **read-only by default** (issue athenaeum#716 AC) — it only
ever reads frontmatter and reports; it never rewrites a page, never un-folds
anything, and never deletes or creates a file. See
:mod:`athenaeum._cmd_fold_lint` for the CLI surface; see :mod:`athenaeum.unfold`
for the module that actually REPAIRS a tombstone (a wholly separate concern
from detecting a graph-shape violation here).

Layering: L4 domain/pipeline. Imports only :mod:`athenaeum.models` (L1) at
module scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from athenaeum.models import is_tombstone, parse_frontmatter, slugify, tombstone_target

#: :class:`FoldGraphViolation.kind` values.
VIOLATION_CYCLE = "cycle"
VIOLATION_MISSING_LIVE_CANONICAL = "missing_live_canonical"
VIOLATION_AMBIGUOUS_LIVE_CANONICAL = "ambiguous_live_canonical"


@dataclass(frozen=True)
class FoldGraphViolation:
    """One violation of either fold-graph invariant.

    ``members`` is the sorted list of slugs involved (the cycle itself, or
    the whole connected fold set for a canonical-count violation).
    ``detail`` is a human-readable one-line explanation.
    """

    kind: str
    members: list[str]
    detail: str


@dataclass
class FoldGraphReport:
    """Result of one :func:`scan_fold_graph` pass."""

    pages_scanned: int = 0
    tombstones: int = 0
    violations: list[FoldGraphViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """``True`` iff no violation of either invariant was found."""
        return not self.violations


def _scan_pages(wiki_root: Path) -> tuple[int, dict[str, Path], dict[str, bool], dict[str, str]]:
    """Walk every top-level ``*.md`` page once.

    Returns ``(pages_scanned, slug_to_path, slug_is_tombstone, edges)`` where
    ``edges`` is ``{tombstone_slug: folded_into_target_slug}`` for every
    tombstone that names a target. A page unreadable or without usable
    frontmatter is counted in ``pages_scanned`` but contributes no edge and
    is never treated as a tombstone (fail open: a page this scan cannot even
    parse is not evidence of a fold-graph violation).
    """
    slug_to_path: dict[str, Path] = {}
    slug_is_tombstone: dict[str, bool] = {}
    edges: dict[str, str] = {}
    pages_scanned = 0
    if not wiki_root.exists():
        return pages_scanned, slug_to_path, slug_is_tombstone, edges
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        pages_scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        slug = slugify(path.stem)
        slug_to_path[slug] = path
        tomb = is_tombstone(meta)
        slug_is_tombstone[slug] = tomb
        if tomb:
            target = tombstone_target(meta)
            if target:
                edges[slug] = target
    return pages_scanned, slug_to_path, slug_is_tombstone, edges


def _find_cycles(edges: dict[str, str]) -> list[FoldGraphViolation]:
    """Detect every cycle in *edges* (a functional graph: out-degree <= 1 per
    node). Each distinct cycle is reported once regardless of which member
    the walk started from."""
    violations: list[FoldGraphViolation] = []
    state: dict[str, str] = {}  # slug -> "visiting" | "done"
    reported: set[frozenset[str]] = set()
    for start in sorted(edges):
        if state.get(start) == "done":
            continue
        path: list[str] = []
        node = start
        while True:
            if node not in edges:
                for p in path:
                    state[p] = "done"
                break
            if state.get(node) == "visiting":
                idx = path.index(node)
                cycle_nodes = path[idx:]
                key = frozenset(cycle_nodes)
                if key not in reported:
                    reported.add(key)
                    chain = " -> ".join([*cycle_nodes, node])
                    violations.append(
                        FoldGraphViolation(
                            kind=VIOLATION_CYCLE,
                            members=sorted(cycle_nodes),
                            detail=f"folded_into cycle: {chain}",
                        )
                    )
                for p in path:
                    state[p] = "done"
                break
            if state.get(node) == "done":
                for p in path:
                    state[p] = "done"
                break
            state[node] = "visiting"
            path.append(node)
            node = edges[node]
    return violations


def _find_canonical_violations(
    edges: dict[str, str],
    slug_to_path: dict[str, Path],
    slug_is_tombstone: dict[str, bool],
) -> list[FoldGraphViolation]:
    """Detect every connected fold set with zero or more-than-one live member."""
    adjacency: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for src, dst in edges.items():
        nodes.add(src)
        nodes.add(dst)
        adjacency.setdefault(src, set()).add(dst)
        adjacency.setdefault(dst, set()).add(src)

    violations: list[FoldGraphViolation] = []
    seen: set[str] = set()
    for node in sorted(nodes):
        if node in seen:
            continue
        component: list[str] = []
        stack = [node]
        seen.add(node)
        while stack:
            cur = stack.pop()
            component.append(cur)
            for neighbor in sorted(adjacency.get(cur, ())):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        live_members = sorted(
            m for m in component if m in slug_to_path and not slug_is_tombstone.get(m, False)
        )
        members_sorted = sorted(component)
        if not live_members:
            violations.append(
                FoldGraphViolation(
                    kind=VIOLATION_MISSING_LIVE_CANONICAL,
                    members=members_sorted,
                    detail=(
                        "fold set {" + ", ".join(members_sorted) + "} has no live "
                        "canonical page (every member is a tombstone or missing from disk)"
                    ),
                )
            )
        elif len(live_members) > 1:
            violations.append(
                FoldGraphViolation(
                    kind=VIOLATION_AMBIGUOUS_LIVE_CANONICAL,
                    members=members_sorted,
                    detail=(
                        "fold set {" + ", ".join(members_sorted) + "} has "
                        f"{len(live_members)} live pages ({', '.join(live_members)}) "
                        "-- exactly one is required"
                    ),
                )
            )
    return violations


def scan_fold_graph(wiki_root: Path) -> FoldGraphReport:
    """Scan the live store under *wiki_root* for both fold-graph invariant
    violations. Read-only — see the module docstring."""
    wiki_root = Path(wiki_root)
    pages_scanned, slug_to_path, slug_is_tombstone, edges = _scan_pages(wiki_root)
    violations = _find_cycles(edges) + _find_canonical_violations(
        edges, slug_to_path, slug_is_tombstone
    )
    return FoldGraphReport(
        pages_scanned=pages_scanned, tombstones=len(edges), violations=violations
    )


__all__ = [
    "VIOLATION_CYCLE",
    "VIOLATION_MISSING_LIVE_CANONICAL",
    "VIOLATION_AMBIGUOUS_LIVE_CANONICAL",
    "FoldGraphViolation",
    "FoldGraphReport",
    "scan_fold_graph",
]
