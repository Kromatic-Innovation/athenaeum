# SPDX-License-Identifier: Apache-2.0
"""AC1 for the compile-time relatedness writer (issue athenaeum#1576).

Grades :mod:`athenaeum.relatedness` with issue athenaeum#1570's measure
(:mod:`tests.evals.relatedness`) against issue athenaeum#1570's corpus. The
writer is reached through :func:`tests.evals.relatedness_writer.compile_corpus`,
which replays a librarian run over the corpus via the SAME
``stamp_related_edges`` entry point ``librarian._apply_tier3_results`` calls --
so a divergence between what is graded here and what production writes is not
expressible.

Deliberately UNMARKED, so it runs in the default CI selection
(``-m 'not eval and not embedding and not rollout'``). That is a property of
the signal, not an oversight: the writer makes no API call and uses no
embedding model, so nothing here needs an Anthropic key or chromadb's ONNX
download. Had the MiniLM neighbourhood been adopted instead, AC1 would only
have been verified in the separate ``embedding`` job.

AC1 has three halves and all three are asserted, because #1570's measure
exists precisely to reject a result that only satisfies the first:

* Cluster A's three pages carry the ground-truth edges after compilation;
* the linked-nothing control still fails;
* the linked-everything control still fails.
"""

from __future__ import annotations

import pytest

from athenaeum.relatedness import ROLE_TERM_OVERLAP
from tests.evals.corpus import build_corpus, load_unlinked_clusters
from tests.evals.relatedness import (
    link_everything,
    link_ground_truth,
    link_nothing,
    score_cluster,
)
from tests.evals.relatedness_writer import compile_corpus


@pytest.fixture(scope="module")
def corpus_pages():
    return build_corpus(scale="core").pages


@pytest.fixture(scope="module")
def written_pages(corpus_pages):
    return compile_corpus(corpus_pages)


@pytest.fixture(scope="module")
def cluster():
    clusters = load_unlinked_clusters()
    assert clusters, "issue athenaeum#1570's relatedness ground truth is missing"
    return clusters[0]


def test_fixture_starts_with_no_edges(corpus_pages, cluster):
    """The measurement is only meaningful if the corpus starts unlinked.

    Asserted rather than assumed: if a future edit authors Cluster A's edges
    into the pages, every assertion below would pass without the writer having
    done anything, and this test would silently stop measuring.
    """
    before = score_cluster(corpus_pages, cluster)
    assert before.found == 0, before.summary()
    assert before.spurious == ()


def test_writer_recovers_every_ground_truth_edge(written_pages, cluster):
    """AC1, first half: the edges exist, and nothing else does."""
    after = score_cluster(written_pages.pages, cluster)
    assert after.recall == 1.0, after.summary()
    assert after.spurious == (), after.summary()
    assert after.precision == 1.0, after.summary()


def test_every_cluster_member_carries_an_edge(written_pages, cluster):
    """AC1's wording is about PAGES, not only about the edge count.

    The oldest page in a cluster writes nothing (everything it relates to is
    newer, so nothing it could link to existed when it was compiled) -- so
    "carries edges" is checked as membership in the cluster's edge set in
    either direction, not as a non-empty ``related:`` on all three.
    """
    by_uid = {page.uid: page for page in written_pages.pages}
    touched: set[str] = set()
    for uid in cluster.members:
        for edge in by_uid[uid].related:
            if edge.uid in cluster.members:
                touched.add(uid)
                touched.add(edge.uid)
    assert touched == set(cluster.members)


def test_both_controls_still_fail(written_pages, cluster):
    """AC1, second and third halves.

    Compared against the writer's own f1 rather than against a hand-chosen
    threshold: a bare number is what issue athenaeum#1570's module docstring
    rejects, since it can be reverse-engineered from the adversary.
    """
    writer = score_cluster(written_pages.pages, cluster)
    nothing = score_cluster(link_nothing(written_pages.pages), cluster)
    everything = score_cluster(link_everything(written_pages.pages), cluster)

    assert nothing.recall == 0.0, nothing.summary()
    assert nothing.f1 < writer.f1, nothing.summary()

    # link_everything reaches recall 1.0 -- that is the whole point of it --
    # and is rejected on precision alone.
    assert everything.recall == 1.0, everything.summary()
    assert everything.precision < 0.1, everything.summary()
    assert everything.f1 < writer.f1, everything.summary()


def test_writer_matches_the_positive_control(corpus_pages, written_pages, cluster):
    """The writer's score is the perfect-librarian score, not merely a good one."""
    ideal = score_cluster(link_ground_truth(corpus_pages, load_unlinked_clusters()), cluster)
    actual = score_cluster(written_pages.pages, cluster)
    assert actual.f1 == ideal.f1 == 1.0, f"{actual.summary()} vs {ideal.summary()}"


def test_every_written_edge_names_its_signal(corpus_pages, written_pages):
    """AC4: a later pass must be able to retract this class and only this class."""
    original = {(page.uid, edge.uid): edge.role for page in corpus_pages for edge in page.related}
    added = [
        (page.uid, edge)
        for page in written_pages.pages
        for edge in page.related
        if (page.uid, edge.uid) not in original
    ]
    assert added, "the writer wrote no edges at all"
    assert all(edge.role == ROLE_TERM_OVERLAP for _uid, edge in added)


def test_authored_edges_survive(corpus_pages, written_pages):
    """AC5's other half: the writer ADDS, it does not own the field.

    Cluster B and the rest of the corpus carry authored ``related:``/``links:``
    rows. A writer that replaced the field rather than appending to it would
    pass every assertion above while silently dropping them.
    """
    before = {page.uid: {(edge.uid, edge.role) for edge in page.related} for page in corpus_pages}
    after = {
        page.uid: {(edge.uid, edge.role) for edge in page.related} for page in written_pages.pages
    }
    assert any(before.values()), "the corpus authored no edges to preserve"
    for uid, edges in before.items():
        assert edges <= after[uid], uid


def test_edge_density_stays_far_below_the_adversary(written_pages):
    """The one-hop caution at ``_cmd_viewer.py:577-580``, as a number.

    The bound that matters is the ``max_edges`` cap, not the corpus size: the
    linked-everything adversary writes ``N - 1`` edges per page.
    """
    assert written_pages.edges_per_page < 4.0
    assert written_pages.edges_per_page < (len(written_pages.pages) - 1) / 10
