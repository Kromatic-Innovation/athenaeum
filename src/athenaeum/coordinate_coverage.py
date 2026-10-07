# SPDX-License-Identifier: Apache-2.0
"""Coordinate-coverage measurement (issue athenaeum#1944) — L4 domain/pipeline.

Read-only, zero-spend, builds no LLM client. Two independent measurements,
both driven by this module's own "Counting rule" (the issue's own text,
reproduced exactly):

- **Per-type coverage**: walk ``<knowledge_root>/wiki/**/*.md``, skipping
  ``_``-prefixed files and directories and never entering ``excluded/``.
  Parse each file's leading YAML frontmatter and bucket it by ``type:``. A
  key counts as present when its value is non-empty. ``subject`` is split
  into real id / :data:`athenaeum.dimensions.UNDETERMINABLE_SUBJECT` /
  absent. A file with no frontmatter at all is counted separately
  (``no_frontmatter``), never folded into a type bucket — a page WITH
  frontmatter but no (or empty) ``type:`` key gets its own bucket,
  :data:`NO_TYPE_BUCKET`, which is a DIFFERENT thing.
- **Gate 1 ``subject`` relation distribution** (EQUAL/UNKNOWN/DISJOINT),
  computed with :func:`athenaeum.comparator.gate1_separator_relations`
  against CURRENT page frontmatter, over two optional pair sources:
  :func:`subject_relation_counts_from_report`'s subject-population report
  (candidate, top-k) pairs, and :func:`subject_relation_counts_from_clusters`'s
  within-cluster member pairs from a raw auto-memory clusters JSONL file.

The ``measure coordinate-coverage`` CLI subcommand (:mod:`athenaeum.
_cmd_measure`) is this module's only caller; this module itself never
touches ``sys.argv``/argparse and prints nothing.

Layering: L4. Imports :mod:`athenaeum.comparator` (``gate1_separator_relations``),
:mod:`athenaeum.dimensions` (``DEFAULT_REGISTRY``, ``Relation``,
``UNDETERMINABLE_SUBJECT``), :mod:`athenaeum.models` (``EntityIndex``,
``parse_frontmatter``), and :mod:`athenaeum.subject_population`
(``read_decision_report`` — the report row shape's single source of truth,
shared with :mod:`athenaeum._cmd_subject_population`, L4/L5 peers of this
module's own layer or below).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

from athenaeum.comparator import gate1_separator_relations
from athenaeum.dimensions import DEFAULT_REGISTRY, UNDETERMINABLE_SUBJECT, Relation
from athenaeum.models import EntityIndex, parse_frontmatter

#: A page WITH frontmatter but no (or empty) ``type:`` key — distinct from
#: ``no_frontmatter`` (a file with no frontmatter block at all).
NO_TYPE_BUCKET = "(no type)"

#: The only three relations the ``subject`` IDENTITY-kind comparator can
#: ever return (see ``compare_identity`` — no CONTAINS/OVERLAPS branch
#: exists for IDENTITY). Used both to seed the zero-filled counters below
#: and to validate a relation before bucketing it.
_SUBJECT_RELATIONS: tuple[str, ...] = (Relation.EQUAL, Relation.UNKNOWN, Relation.DISJOINT)


def _zero_relation_counts() -> dict[str, int]:
    return {relation: 0 for relation in _SUBJECT_RELATIONS}


def _present(value: Any) -> bool:
    """"A key counts as present when its value is non-empty" (the issue's
    own counting rule, applied uniformly to every coordinate field)."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True  # a real non-string scalar (int/float/bool/date) is present


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, _body = parse_frontmatter(text)
    return meta if isinstance(meta, dict) and meta else None


def iter_wiki_files(wiki_root: Path) -> "list[Path]":
    """Every ``wiki_root/**/*.md`` file the counting rule includes: skips
    ``_``-prefixed files/directories, never enters ``excluded/``. Sorted
    for deterministic output."""
    if not wiki_root.is_dir():
        return []
    files: list[Path] = []
    for path in wiki_root.rglob("*.md"):
        rel_parts = path.relative_to(wiki_root).parts
        if any(part == "excluded" for part in rel_parts[:-1]):
            continue
        if any(part.startswith("_") for part in rel_parts):
            continue
        files.append(path)
    return sorted(files)


@dataclass
class TypeCoverage:
    """Per-type counts, over the pages the counting rule assigns to one
    ``type:`` bucket (or :data:`NO_TYPE_BUCKET`)."""

    pages: int = 0
    subject_real: int = 0
    subject_undeterminable: int = 0
    subject_absent: int = 0
    claimed_scope: int = 0
    valid_from: int = 0
    valid_until: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "pages": self.pages,
            "subject_real": self.subject_real,
            "subject_undeterminable": self.subject_undeterminable,
            "subject_absent": self.subject_absent,
            "claimed_scope": self.claimed_scope,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
        }


@dataclass
class CoordinateCoverageReport:
    """Counts only -- no page names or uids anywhere in this shape (issue
    athenaeum#1944 AC: "Output contains no page names or uids")."""

    by_type: dict[str, TypeCoverage] = field(default_factory=dict)
    no_frontmatter: int = 0
    pair_relation_counts: dict[str, int] | None = None
    cluster_relation_counts: dict[str, int] | None = None
    #: Issue athenaeum#1946: raw auto-memory cluster member ``subject``
    #: coverage (present / undeterminable / absent), same counting rule as
    #: :data:`TypeCoverage`'s own ``subject_*`` fields, deduped by resolved
    #: path across every cluster row in the clusters file.
    raw_member_subject_coverage: dict[str, int] | None = None

    def all_pages_total(self) -> TypeCoverage:
        """Sum of every ``by_type`` bucket (the issue's own table's "all" row)."""
        total = TypeCoverage()
        for cov in self.by_type.values():
            total.pages += cov.pages
            total.subject_real += cov.subject_real
            total.subject_undeterminable += cov.subject_undeterminable
            total.subject_absent += cov.subject_absent
            total.claimed_scope += cov.claimed_scope
            total.valid_from += cov.valid_from
            total.valid_until += cov.valid_until
        return total

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "by_type": {t: cov.to_dict() for t, cov in sorted(self.by_type.items())},
            "all": self.all_pages_total().to_dict(),
            "no_frontmatter": self.no_frontmatter,
        }
        if self.pair_relation_counts is not None:
            payload["pair_relation_counts"] = dict(self.pair_relation_counts)
        if self.cluster_relation_counts is not None:
            payload["cluster_relation_counts"] = dict(self.cluster_relation_counts)
        if self.raw_member_subject_coverage is not None:
            payload["raw_member_subject_coverage"] = dict(self.raw_member_subject_coverage)
        return payload


def measure_coordinate_coverage(wiki_root: Path) -> CoordinateCoverageReport:
    """Per-type dimension-coverage counts (the issue's "Counting rule"),
    read-only, zero-spend."""
    report = CoordinateCoverageReport()
    for path in iter_wiki_files(wiki_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _body = parse_frontmatter(text)
        if not isinstance(meta, dict) or not meta:
            report.no_frontmatter += 1
            continue

        raw_type = meta.get("type")
        type_key = (
            raw_type.strip()
            if isinstance(raw_type, str) and raw_type.strip()
            else NO_TYPE_BUCKET
        )
        cov = report.by_type.setdefault(type_key, TypeCoverage())
        cov.pages += 1

        subject = meta.get("subject")
        if not _present(subject):
            cov.subject_absent += 1
        elif isinstance(subject, str) and subject.strip() == UNDETERMINABLE_SUBJECT:
            cov.subject_undeterminable += 1
        else:
            cov.subject_real += 1

        if _present(meta.get("claimed_scope")):
            cov.claimed_scope += 1
        if _present(meta.get("valid_from")):
            cov.valid_from += 1
        if _present(meta.get("valid_until")):
            cov.valid_until += 1

    return report


def subject_relation_counts_from_report(wiki_root: Path, report_path: Path) -> dict[str, int]:
    """Gate 1 ``subject`` relation distribution over a subject-population
    report's (candidate, top-k) pairs, against CURRENT page frontmatter.

    Each decision's own page is compared against every uid in its
    ``top_k_uids`` -- the exact pairs the confirmer was shown (issue
    athenaeum#1944's own phrasing). A uid that no longer resolves to an
    indexed page (renamed, retired) is silently skipped -- the issue asks
    for a distribution over the pairs that CAN be read today, not a strict
    count that would need to error on drift.
    """
    from athenaeum.subject_population import read_decision_report

    decisions = read_decision_report(report_path)
    index = EntityIndex(wiki_root)
    counts = _zero_relation_counts()

    for decision in decisions:
        path_a = index.get_by_uid(decision.uid)
        if path_a is None:
            continue
        meta_a = _read_meta(path_a)
        if meta_a is None:
            continue
        for candidate_uid in decision.top_k_uids:
            path_b = index.get_by_uid(candidate_uid)
            if path_b is None:
                continue
            meta_b = _read_meta(path_b)
            if meta_b is None:
                continue
            relation = gate1_separator_relations(DEFAULT_REGISTRY, meta_a, meta_b).get(
                "subject"
            )
            if relation in counts:
                counts[relation] += 1

    return counts


def _resolve_cluster_member_path(knowledge_root: Path, member_path: str) -> Path:
    """A raw cluster row's ``member_paths`` entries are POSIX-relative to
    one of the clustering pass's ``extra_roots`` -- normally
    ``raw/auto-memory/`` (issue athenaeum#1944's own "members resolved
    under raw/auto-memory/") -- or, when no configured root matched at
    write time, an absolute path verbatim (see
    :mod:`athenaeum.clusters`'s own ``relpaths`` construction). Both shapes
    are handled: an absolute path is used as-is; anything else is joined
    under ``raw/auto-memory/``.
    """
    candidate = Path(member_path)
    if candidate.is_absolute():
        return candidate
    return knowledge_root / "raw" / "auto-memory" / candidate


def newest_clusters_file(knowledge_root: Path) -> Path | None:
    """The newest ``raw/_librarian-clusters-*.jsonl`` rotation under
    *knowledge_root*, or ``None`` if there isn't one.

    Rotation filenames are fixed-width ``%Y%m%dT%H%M%SZ``-stamped (see
    :mod:`athenaeum.clusters`'s ``prune_cluster_rotations`` docstring), so
    lexicographic sort order is chronological order -- the max by name is
    the newest.
    """
    candidates = sorted((knowledge_root / "raw").glob("_librarian-clusters-*.jsonl"))
    return candidates[-1] if candidates else None


def subject_relation_counts_from_clusters(
    knowledge_root: Path, clusters_path: Path | None = None
) -> dict[str, int]:
    """Gate 1 ``subject`` relation distribution over one raw auto-memory
    clusters file's within-cluster member pairs.

    *clusters_path* defaults to :func:`newest_clusters_file`. Each
    cluster's members are resolved per :func:`_resolve_cluster_member_path`
    and compared pairwise (complete pairwise, matching
    :func:`athenaeum.cluster_comparator.candidate_pairs`'s own candidate
    shape) -- a member path that cannot be read is silently skipped from
    every pair it would have formed.
    """
    path = clusters_path if clusters_path is not None else newest_clusters_file(knowledge_root)
    counts = _zero_relation_counts()
    if path is None or not path.is_file():
        return counts

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        row = json.loads(stripped)
        member_paths = row.get("member_paths") if isinstance(row, dict) else None
        if not isinstance(member_paths, list):
            continue

        metas: list[dict[str, Any]] = []
        for member_path in member_paths:
            resolved = _resolve_cluster_member_path(knowledge_root, str(member_path))
            meta = _read_meta(resolved)
            if meta is not None:
                metas.append(meta)

        for meta_a, meta_b in combinations(metas, 2):
            relation = gate1_separator_relations(DEFAULT_REGISTRY, meta_a, meta_b).get(
                "subject"
            )
            if relation in counts:
                counts[relation] += 1

    return counts


def raw_member_subject_coverage_from_clusters(
    knowledge_root: Path, clusters_path: Path | None = None
) -> dict[str, int]:
    """``subject`` coverage (present / undeterminable / absent) over every
    raw auto-memory cluster member named in one clusters JSONL file (issue
    athenaeum#1946, AC1) -- the raw-member analogue of
    :func:`measure_coordinate_coverage`'s per-type ``subject_*`` counts,
    using the SAME counting rule (:func:`_present`, and the literal
    :data:`~athenaeum.dimensions.UNDETERMINABLE_SUBJECT` sentinel split out
    from "present").

    Deduplicated by resolved path across every cluster row -- a member can
    legitimately recur across clusters (or within one, degenerately), and
    this reports each real file once. *clusters_path* defaults to
    :func:`newest_clusters_file`. A member path that cannot be read is
    silently skipped (mirrors :func:`subject_relation_counts_from_clusters`'s
    own fail-open posture).
    """
    path = clusters_path if clusters_path is not None else newest_clusters_file(knowledge_root)
    counts = {"present": 0, "undeterminable": 0, "absent": 0}
    if path is None or not path.is_file():
        return counts

    seen_paths: set[Path] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        row = json.loads(stripped)
        member_paths = row.get("member_paths") if isinstance(row, dict) else None
        if not isinstance(member_paths, list):
            continue
        for member_path in member_paths:
            resolved = _resolve_cluster_member_path(knowledge_root, str(member_path))
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            meta = _read_meta(resolved)
            if meta is None:
                continue
            subject = meta.get("subject")
            if not _present(subject):
                counts["absent"] += 1
            elif isinstance(subject, str) and subject.strip() == UNDETERMINABLE_SUBJECT:
                counts["undeterminable"] += 1
            else:
                counts["present"] += 1

    return counts


__all__ = [
    "NO_TYPE_BUCKET",
    "CoordinateCoverageReport",
    "TypeCoverage",
    "iter_wiki_files",
    "measure_coordinate_coverage",
    "newest_clusters_file",
    "raw_member_subject_coverage_from_clusters",
    "subject_relation_counts_from_clusters",
    "subject_relation_counts_from_report",
]
