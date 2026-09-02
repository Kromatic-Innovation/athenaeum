# SPDX-License-Identifier: Apache-2.0
"""Cluster-domain comparator adapter + dark candidate-pairs driver (athenaeum#1255)
— L4 domain/pipeline.

Step S2 of the athenaeum#715 phase-4 plan to retire ``merge.py``'s C4 detector
(:func:`athenaeum.contradictions.detect_contradictions`). Operator-verified at
source, the two detectors run over disjoint input domains today:
:func:`athenaeum.comparator.compare_pages` is called only from
``wiki_dedupe.py``, ``comparator.py`` (:func:`~athenaeum.comparator.record_comparison`)
and ``recompare.py`` — all PAIRWISE over already-compiled wiki pages.
``detect_contradictions`` is called only from ``merge.py`` — N-ARY over raw
auto-memory clusters. The comparator has no cluster-domain call site; this
module builds it, dark, so a later step (athenaeum#715 issue D) can measure
whether the comparator could replace C4 without spending a single model
call to find out how expensive that would be.

Two pieces:

- :func:`page_from_auto_memory_file` — the missing adapter from one
  :class:`~athenaeum.models.AutoMemoryFile` cluster member to a
  :class:`~athenaeum.comparator.ComparatorPage`, built on
  :func:`athenaeum.comparator.page_from_text` exactly as
  :func:`athenaeum.comparator.page_from_path` already does for wiki pages.
- :func:`run_cluster_comparator` — the driver. Given one auto-memory
  cluster's resolved members, it forms the same candidate-pair set
  :mod:`athenaeum.wiki_dedupe` forms over wiki-page clusters
  (:func:`itertools.combinations`, complete pairwise) and, ONLY when
  :func:`athenaeum.config.resolve_comparator_enabled` is on, runs
  :func:`athenaeum.comparator.compare_pages` over every pair. The pair
  count itself — the N-ary-to-pairwise multiplier issue D needs to size —
  is pure combinatorics (:func:`planned_pair_count`) and is always
  computed and recorded, gate on or off: sizing the multiplier never
  requires the gate to be on, let alone an LLM call.

**Dark by design.** Nothing in :mod:`athenaeum.librarian` calls
:func:`run_cluster_comparator` yet — this issue only builds the call site
that makes the cluster domain reachable at all. ``athenaeum run`` behaviour
is therefore byte-identical whether or not ``resolve_comparator_enabled``
is on: with the driver unwired, the flag controls nothing this module does
that any existing pipeline phase observes. ``src/athenaeum/merge.py`` is
untouched by this module — the C4 detector it drives keeps running exactly
as before until a later step (gated on the athenaeum#715 issue-D
go/no-go) decides otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import TYPE_CHECKING, Any

from athenaeum.comparator import ComparatorPage, CompareOutcome, compare_pages, page_from_text
from athenaeum.config import resolve_comparator_enabled
from athenaeum.models import AutoMemoryFile, TokenUsage
from athenaeum.verdicts import page_id_for_path

if TYPE_CHECKING:
    from athenaeum.provider import LLMBackend

__all__ = [
    "ClusterComparatorResult",
    "candidate_pairs",
    "page_from_auto_memory_file",
    "planned_pair_count",
    "run_cluster_comparator",
]


def page_from_auto_memory_file(member: AutoMemoryFile) -> ComparatorPage:
    """Adapt one auto-memory cluster member into a :class:`ComparatorPage`.

    Mirrors :func:`athenaeum.comparator.page_from_path` exactly, but for a
    member that is already resolved in memory rather than a bare path: the
    id is :func:`athenaeum.verdicts.page_id_for_path` (the SAME durable
    slug identity the verdict ledger already keys pairs on — reusing it
    here rather than inventing a second id space for the cluster domain),
    and the text is ``member.content`` (the full raw markdown, frontmatter
    included — :attr:`~athenaeum.models.AutoMemoryFile.content` lazily
    reads it from disk once and caches it, so repeated pairings of the
    same member across candidate pairs cost one read, not one per pair).
    """
    return page_from_text(page_id_for_path(member.path), member.content)


def candidate_pairs(
    members: list[AutoMemoryFile],
) -> list[tuple[AutoMemoryFile, AutoMemoryFile]]:
    """Every unordered candidate pair within one cluster's members.

    Complete pairwise (:func:`itertools.combinations`, size 2) — the same
    candidate-generation shape :mod:`athenaeum.wiki_dedupe` already uses
    for wiki-page clusters. A cluster of fewer than two members yields no
    pairs (nothing here special-cases the singleton: ``combinations``
    already returns an empty list).
    """
    return list(combinations(members, 2))


def planned_pair_count(members: list[AutoMemoryFile]) -> int:
    """The number of comparator calls one cluster WOULD issue, gate on or off.

    Pure combinatorics — ``len(candidate_pairs(members))`` — so this can be
    computed (and, via :meth:`ClusterComparatorResult.to_row`, recorded)
    with zero model spend and independently of
    :func:`athenaeum.config.resolve_comparator_enabled`. This is the number
    a later issue (athenaeum#715 issue D) needs to size the N-ary
    (:func:`athenaeum.contradictions.detect_contradictions`, one call per
    cluster) to pairwise (:func:`athenaeum.comparator.compare_pages`, this
    count per cluster) cost multiplier, without running a single comparison
    to find out.
    """
    return len(candidate_pairs(members))


@dataclass
class ClusterComparatorResult:
    """Outcome of running (or dry-sizing) the comparator over one cluster.

    ``pair_count`` is always populated, whether or not the gate was on —
    see :func:`planned_pair_count`. ``gate_enabled`` records which branch
    produced ``outcomes``: when ``False``, ``outcomes`` is always empty and
    no :func:`~athenaeum.comparator.compare_pages` call was made for this
    cluster at all.
    """

    cluster_id: str
    pair_count: int
    gate_enabled: bool
    outcomes: list[tuple[str, str, CompareOutcome]] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        """JSONL-shaped row for observability — mirrors
        :meth:`athenaeum.clusters.Cluster.to_row`'s convention. Not written
        anywhere by this dark module; provided for the caller that wires
        this driver in later.
        """
        return {
            "cluster_id": self.cluster_id,
            "pair_count": self.pair_count,
            "gate_enabled": self.gate_enabled,
            "outcomes": [
                {"a": id_a, "b": id_b, "verdict": outcome.verdict}
                for id_a, id_b, outcome in self.outcomes
            ],
        }


def run_cluster_comparator(
    members: list[AutoMemoryFile],
    client: "LLMBackend | None",
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
    *,
    cluster_id: str = "",
) -> ClusterComparatorResult:
    """Run (or dry-size) the comparator over one auto-memory cluster's members.

    Gated on :func:`athenaeum.config.resolve_comparator_enabled` — DEFAULT
    OFF, the SAME master switch the rest of the comparator subsystem
    already ships behind (mirrors :mod:`athenaeum.wiki_dedupe`'s posture:
    "not a new flag"). With it off, this returns immediately after
    computing ``pair_count`` — no adapter call, no
    :func:`~athenaeum.comparator.compare_pages` call, no LLM spend of any
    kind. This function has no caller in :mod:`athenaeum.librarian` yet
    (issue athenaeum#1255 is dark); a live caller is future work.

    Args:
        members: The cluster's resolved members (e.g.
            :attr:`athenaeum.merge.MergedWikiEntry.resolved_members`, or
            any other resolved :class:`~athenaeum.models.AutoMemoryFile`
            list for one cluster). Fewer than two members yields
            ``pair_count=0`` and, when the gate is on, an empty
            ``outcomes`` list — not an error.
        client: A live LLM client, or ``None``. Passed straight through to
            :func:`~athenaeum.comparator.compare_pages`, which never raises
            for an unavailable client (Gate 2 degrades to
            ``verdict=None``).
        config: Optional resolved ``athenaeum.yaml`` dict — read once here
            for the gate, then passed straight through to
            :func:`~athenaeum.comparator.compare_pages` for its own model
            resolution.
        usage: Optional run-level :class:`~athenaeum.models.TokenUsage`;
            accumulates across every pair in this cluster exactly as it
            does in :mod:`athenaeum.wiki_dedupe`/:mod:`athenaeum.recompare`.
        cluster_id: Carried onto the returned result for the caller's own
            bookkeeping; this function does not interpret it.

    Returns:
        A :class:`ClusterComparatorResult`.
    """
    pair_count = planned_pair_count(members)

    if not resolve_comparator_enabled(config):
        return ClusterComparatorResult(
            cluster_id=cluster_id, pair_count=pair_count, gate_enabled=False
        )

    outcomes: list[tuple[str, str, CompareOutcome]] = []
    for member_a, member_b in candidate_pairs(members):
        page_a = page_from_auto_memory_file(member_a)
        page_b = page_from_auto_memory_file(member_b)
        outcome = compare_pages(page_a, page_b, client=client, config=config, usage=usage)
        outcomes.append((page_a.id, page_b.id, outcome))

    return ClusterComparatorResult(
        cluster_id=cluster_id,
        pair_count=pair_count,
        gate_enabled=True,
        outcomes=outcomes,
    )
