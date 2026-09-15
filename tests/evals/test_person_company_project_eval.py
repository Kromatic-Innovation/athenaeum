# SPDX-License-Identifier: Apache-2.0
"""Person / company / project relatedness + fact-placement (issue athenaeum#1658).

A child of athenaeum#1600 AC3: for a client engagement, are person, company
and project all present and mutually linked, and does the financial fact sit
on the project (or a sub-page linked from it) rather than the person?
``tests/evals/data/corpus/core/09-person-company-project.yaml`` is the
fixture; ``tests/evals/data/corpus/ground_truth/relatedness.yaml`` carries its
``person-company-project`` cluster, including the ``fact_placements`` entry
this module's second half grades.

Deliberately UNMARKED, same reasoning as
``tests/evals/test_relatedness_writer_eval.py``: no Anthropic call, no
embedding model, so this runs in the default CI selection
(``-m 'not eval and not embedding and not rollout'``).

Two halves, because the issue names two orthogonal things to grade:

* **Edges** (AC3) -- scored with :mod:`tests.evals.relatedness`, the same
  measure ``quiet-handover`` uses, pinned with the same three controls:
  link-nothing fails recall, link-everything fails precision, the ideal
  (ground-truth) corpus passes -- and the ORDERING is what is asserted, not a
  tuned constant.
* **Fact placement** (AC4) -- scored with the new
  :mod:`tests.evals.fact_placement`. The committed fixture starts in the
  defect state (fact on the person), and -- the assertion this module exists
  to make explicit -- the SHIPPED athenaeum#1576 relatedness writer does not
  fix it. That writer only ever proposes ``related:`` edges; it has no
  mechanism that moves or rewrites body text, so replaying it over this
  cluster leaves the payment fact exactly where the fixture put it. This is
  an intentional expected-negative: whoever ships a fact-placement writer
  must come back and update this test, not the fixture.
"""

from __future__ import annotations

import pytest

from tests.evals import fact_placement as FP
from tests.evals import relatedness as R
from tests.evals.corpus import build_corpus, load_unlinked_clusters
from tests.evals.relatedness_writer import compile_corpus


@pytest.fixture(scope="module")
def corpus_pages():
    return build_corpus(scale="core").pages


@pytest.fixture(scope="module")
def cluster():
    for c in load_unlinked_clusters():
        if c.id == "person-company-project":
            return c
    raise AssertionError("issue athenaeum#1658's relatedness ground truth is missing")


@pytest.fixture(scope="module")
def placement(cluster):
    (only,) = cluster.fact_placements
    return only


# ---------------------------------------------------------------------------
# AC1 -- the three pages exist, unlinked
# ---------------------------------------------------------------------------


def test_cluster_is_a_person_company_project_triangle(cluster):
    assert len(cluster.members) == 3


def test_cluster_carries_no_edges_at_all(corpus_pages, cluster):
    """Same reasoning as ``quiet-handover``: a corpus that already carries
    the edges cannot tell a librarian that writes them from one that does
    not, so this is what keeps AC3 below non-vacuous."""
    by_uid = {page.uid: page for page in corpus_pages}
    for uid in cluster.members:
        assert by_uid[uid].related == (), (
            f"{uid} carries edges; the cluster must start unlinked or the "
            "relatedness measure can pass without anything having run"
        )


# ---------------------------------------------------------------------------
# AC3 -- edges: link-nothing / link-everything / ideal, ordering only
# ---------------------------------------------------------------------------


def test_committed_corpus_fails_because_the_edges_are_missing(corpus_pages, cluster):
    score = R.score_cluster(corpus_pages, cluster)
    assert score.found == 0 and score.f1 == 0.0, score.summary()


def test_link_nothing_fails_recall(corpus_pages, cluster):
    score = R.score_cluster(R.link_nothing(corpus_pages), cluster)
    assert score.recall == 0.0, score.summary()


def test_link_everything_fails_precision(corpus_pages, cluster):
    score = R.score_cluster(R.link_everything(corpus_pages), cluster)
    assert score.recall == 1.0, "adversary should find every edge"
    assert score.spurious, "adversary should be penalised for the rest"


def test_ideal_tree_passes(corpus_pages, cluster):
    ideal_pages = R.link_ground_truth(corpus_pages, [cluster])
    score = R.score_cluster(ideal_pages, cluster)
    assert score.f1 == 1.0, score.summary()


def test_the_three_corpora_are_strictly_ordered(corpus_pages, cluster):
    """The ordering is the assertion, not a tuned constant (per issue
    athenaeum#1658's AC3): a correct measure cannot be satisfied by any fixed
    f1 threshold, only by nothing < everything < ideal holding for THIS
    cluster's controls."""
    nothing = R.score_cluster(R.link_nothing(corpus_pages), cluster).f1
    everything = R.score_cluster(R.link_everything(corpus_pages), cluster).f1
    ideal = R.score_cluster(R.link_ground_truth(corpus_pages, [cluster]), cluster).f1
    assert nothing < everything < ideal
    assert ideal > everything
    assert ideal > nothing


# ---------------------------------------------------------------------------
# AC2 / AC4 -- fact placement
# ---------------------------------------------------------------------------


def test_fact_is_misplaced_as_committed(corpus_pages, placement):
    """The fixture starts in the defect state athenaeum#1600 measured: the
    payment fact is on the person, not the project."""
    score = FP.score_fact_placement(corpus_pages, placement)
    assert score.on_misplaced_page is True, score.summary()
    assert score.on_correct_page is False, score.summary()
    assert score.satisfied is False, score.summary()


def test_shipped_relatedness_writer_does_not_satisfy_fact_placement(corpus_pages, placement):
    """Expected-negative, asserted deliberately (issue athenaeum#1658 AC4).

    The athenaeum#1576 writer (``athenaeum.relatedness.stamp_related_edges``,
    replayed here via ``tests.evals.relatedness_writer.compile_corpus``) only
    ever APPENDS ``related:`` edges between existing pages -- see that
    module's docstring: "Given the wiki as it stands and a page being
    compiled, propose the edges that page should carry OUT to pages already
    in the wiki." It has no mechanism that reads, moves, or rewrites a
    page's BODY text, so it cannot retract the misplaced fact from the
    person or add it to the project. Replaying it here must therefore still
    fail fact placement.

    THIS IS INTENTIONAL, not a known bug being tolerated silently: if a
    future change gives the writer (or a successor) a fact-placement
    mechanism, this assertion is the one that will go red, and whoever lands
    that change must flip it to assert ``satisfied is True`` instead of
    deleting it -- the reason string above is what tells them why it was
    written this way.
    """
    written = compile_corpus(corpus_pages)
    score = FP.score_fact_placement(written.pages, placement)
    assert score.satisfied is False, (
        "the shipped athenaeum#1576 relatedness writer satisfied fact "
        f"placement it was never built to satisfy -- it only writes "
        f"`related:` edges, never body text. If this now passes, either the "
        f"writer gained a fact-placement mechanism (update this test to "
        f"assert `satisfied is True` and say so) or the fixture regressed "
        f"to a state where the fact is no longer misplaced (fix the "
        f"fixture instead). {score.summary()}"
    )
    # The negative half, spelled out: the writer's own edges for this
    # cluster must not be mistaken for fact placement -- it may have written
    # SOME edges (that's ``test_relatedness_writer_eval`` territory for
    # Cluster A; this cluster is not asserted to reach recall 1.0 by the
    # writer), but no edge-writing is a route to moving body text.
    assert score.on_misplaced_page is True, (
        "the writer retracted the fact from the person page without any "
        f"mechanism that could do so -- investigate before trusting this "
        f"result. {score.summary()}"
    )
