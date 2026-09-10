# SPDX-License-Identifier: Apache-2.0
"""Offline relatedness measure for the synthetic corpus (issue athenaeum#1570).

No model call, no network, no index build: this reads ``related:`` edges off
the pages a corpus already holds and compares them to the ground truth in
``data/corpus/ground_truth/relatedness.yaml``.

What makes this measure non-trivial
-----------------------------------

The obvious measure -- "how many of the ground-truth edges are present" --
rewards linking more, and is therefore maximised by a librarian that links
every page to every other page. That is not a hypothetical failure mode, it is
the specific one the viewer already documents:

    One hop, same session: a pulled page counts as breadcrumbed only if some
    page PUSHED in this session names it in ``related:``. Transitive closure
    over a 25k-page corpus would make nearly everything a breadcrumb and the
    colour would stop carrying information.
    -- ``src/athenaeum/_cmd_viewer.py:577-580``

Breadcrumb colour degrades because EVERY page becomes a breadcrumb, not
because a cluster got over-linked internally. So the measure counts spurious
edges against **the whole corpus**, not just against the other members of the
cluster:

* an edge is CONSIDERED when its source is a cluster member -- pages outside
  the cluster are not this cluster's business;
* it is a HIT when ``(source, target)`` is in the ground truth;
* it is SPURIOUS when it is anything else, wherever in the corpus it points.

That scoping is what makes both AC5 directions fail from opposite sides:
link-nothing drives recall to 0, link-everything drives precision to ~1/N.
Scoping spurious edges to intra-cluster pairs instead would cap a
link-everything corpus at precision 0.5, and the pass threshold would then
have to be hand-tuned to make the adversary lose -- a threshold reverse-
engineered from the attack is not a measure.

Roles are reported, not graded
------------------------------

An edge is matched on ``(source, target)`` only. The ground truth records a
role for each edge and :attr:`RelatednessScore.role_matches` reports how many
agreed, but the score does not depend on it. The consumers of this measure
(the librarian relatedness writer, and athenaeum#1577) have not chosen a role
vocabulary yet, and grading against one this corpus invented unilaterally
would make this eval a moving target for the issues it exists to grade.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from tests.evals.corpus import Page, RelatedEdge, UnlinkedCluster

#: Directed edge as the measure compares them: ``(source_uid, target_uid)``.
Edge = tuple[str, str]


@dataclass(frozen=True)
class RelatednessScore:
    """One cluster's relatedness result.

    ``f1`` is reported rather than a bare recall because recall alone is the
    measure this module's docstring exists to reject. Both components are kept
    on the record so a result can say WHICH way a corpus failed -- a librarian
    that wrote nothing and one that wrote everything are different bugs with
    different fixes, and a single number would blur them exactly the way the
    push-outcome ladder in ``metrics.py`` refuses to.
    """

    cluster_id: str
    expected: int
    found: int
    missing: tuple[Edge, ...]
    spurious: tuple[Edge, ...]
    #: Of the ``found`` edges, how many also carried the ground-truth role.
    #: Reported, never graded -- see the module docstring.
    role_matches: int

    @property
    def recall(self) -> float:
        return self.found / self.expected if self.expected else 0.0

    @property
    def precision(self) -> float:
        considered = self.found + len(self.spurious)
        return self.found / considered if considered else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def summary(self) -> str:
        return (
            f"{self.cluster_id}: found {self.found}/{self.expected} "
            f"ground-truth edges, {len(self.spurious)} spurious | "
            f"recall={self.recall:.3f} precision={self.precision:.3f} "
            f"f1={self.f1:.3f} (roles agreed on {self.role_matches}/{self.found})"
        )


def edges_of(pages: Iterable[Page]) -> dict[str, tuple[RelatedEdge, ...]]:
    """``uid -> outgoing edges`` for every page in *pages*."""
    return {page.uid: page.related for page in pages}


def score_cluster(pages: Iterable[Page], cluster: UnlinkedCluster) -> RelatednessScore:
    """Score one :class:`~tests.evals.corpus.UnlinkedCluster` against *pages*.

    *pages* is the corpus AS IT STANDS -- the committed fixture, or a corpus a
    librarian has written edges into, or one of the two adversarial rewrites in
    :func:`link_nothing` / :func:`link_everything`.
    """
    by_uid = edges_of(pages)
    members = set(cluster.members)

    expected_roles = {(s, t): role for s, t, role in cluster.expected_edges}
    expected: set[Edge] = set(expected_roles)

    found: set[Edge] = set()
    spurious: list[Edge] = []
    role_matches = 0

    for source in sorted(members):
        for edge in by_uid.get(source, ()):
            pair = (source, edge.uid)
            if pair in expected:
                found.add(pair)
                if edge.role == expected_roles[pair]:
                    role_matches += 1
            else:
                spurious.append(pair)

    return RelatednessScore(
        cluster_id=cluster.id,
        expected=len(expected),
        found=len(found),
        missing=tuple(sorted(expected - found)),
        spurious=tuple(sorted(spurious)),
        role_matches=role_matches,
    )


# --------------------------------------------------------------------------
# The two adversarial corpora AC5 pins the measure against
# --------------------------------------------------------------------------


def link_nothing(pages: Iterable[Page]) -> list[Page]:
    """Every page stripped of its edges: the "no librarian ran" corpus.

    This is the failure the live wiki is in -- 7.9% of pages carry a non-empty
    ``related:`` (athenaeum#1568). A relatedness measure that passes here
    measures nothing.
    """
    from dataclasses import replace

    return [replace(page, related=()) for page in pages]


def link_everything(pages: Iterable[Page]) -> list[Page]:
    """Every page linked to every other page: the "more edges is better" corpus.

    The adversary a recall-only measure cannot reject, and the one the viewer's
    one-hop caution is about. Deliberately corpus-wide rather than
    cluster-wide: a cluster-scoped version would be a much weaker attack and
    would let a hand-tuned threshold survive.
    """
    from dataclasses import replace

    materialized = list(pages)
    uids = [page.uid for page in materialized]
    return [
        replace(
            page,
            related=tuple(
                RelatedEdge(uid=other, role="related") for other in uids if other != page.uid
            ),
        )
        for page in materialized
    ]


def link_ground_truth(pages: Iterable[Page], clusters: Iterable[UnlinkedCluster]) -> list[Page]:
    """The corpus a correct librarian would produce: exactly the expected edges.

    The positive control. Without it the two adversarial directions prove only
    that the measure can fail, not that it can also pass -- and a measure that
    fails everything is as useless as one that passes everything.
    """
    from dataclasses import replace

    additions: dict[str, list[RelatedEdge]] = {}
    for cluster in clusters:
        for source, target, role in cluster.expected_edges:
            additions.setdefault(source, []).append(RelatedEdge(uid=target, role=role))

    return [
        (
            replace(page, related=page.related + tuple(additions[page.uid]))
            if page.uid in additions
            else page
        )
        for page in pages
    ]
