# SPDX-License-Identifier: Apache-2.0
"""Decomposition verdict on the shipped librarian (issue athenaeum#1581).

Grades the three cases the DETERMINISTIC size gate decides -- A, B and C --
plus the consolidate/decompose boundary the operator ruled on. Case D is
decided by the classify tier, needs a real model call, and is graded in
``tests/evals/test_decomposition_eval.py``.

Deliberately UNMARKED, so it runs in the default CI selection
(``-m 'not eval and not embedding and not rollout'``). That is a property of
the signal, not an oversight, and it is the same reasoning
``tests/evals/test_relatedness_writer_eval.py`` records: the size gate is a
``len()`` and a heading regex. No API key, no embedding model, no network.

AC3 is asserted as a RELATIONSHIP, not as two independent facts
---------------------------------------------------------------

``assert case_a_did_not_decompose`` would pass today and go RED the day
someone fixes the librarian -- precisely backwards for an anti-vacuity gate.
So :func:`test_the_gate_separates_size_from_facets` asserts the *shape of the
gap*: C is decided correctly and A is not, and the two verdicts differ for a
reason the fixture states (A is multi-facet and small; C is single-facet and
large). A librarian that never decomposes fails A. One that decomposes
everything fails C. Only one that reads facets rather than size passes both,
and the day that lands, the assertion that has to be updated is the recorded
baseline in ``docs/measurements/decomposition-baseline-2026-09-10.md`` --
which is where a changed measurement belongs.

Nothing here touches the operator's live corpus: every fixture materializes
into ``tmp_path`` and :func:`~tests.evals.decomposition.assert_disposable`
refuses to proceed against anything under ``~/knowledge``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.name_structure import (
    merged_body_within_page_size_threshold,
    scan_qualified_name_splits,
)
from athenaeum.tiers import DEFAULT_PAGE_SIZE_THRESHOLD_CHARS, resolve_page_size_threshold_chars
from tests.evals.corpus import build_corpus, load_redundant_clusters
from tests.evals.decomposition import (
    ROLE_SPLIT_FROM,
    ROLE_SPLIT_INTO,
    TIER_SIZE_GATE,
    boundary_verdict,
    contains_fact,
    facet_alignment,
    load_cases,
    load_oversize_family,
    observe_default_disposition,
    run_split_disposition,
    score_case,
    split_edges,
)

CASE_A = "multifacet_under_threshold"
CASE_B = "multifacet_oversize"
CASE_C = "singlefacet_oversize"
CASE_D = "decomposed_hub_new_intake"


@pytest.fixture(scope="module")
def cases():
    return {case.id: case for case in load_cases()}


def _outcome(case, tmp_path: Path):
    return run_split_disposition(case, tmp_path)


# ---------------------------------------------------------------------------
# The fixture says what it claims to say
# ---------------------------------------------------------------------------


def test_the_fixture_sizes_are_the_ones_the_cases_claim(cases):
    """Asserted rather than assumed, because every verdict below depends on it.

    If a later edit trimmed Case B's padding under the threshold, every
    assertion about "the gate fires here" would still pass -- by testing the
    wrong thing. The sizes ARE the fixture.
    """
    threshold = DEFAULT_PAGE_SIZE_THRESHOLD_CHARS
    assert len(cases[CASE_A].page.body()) < threshold
    assert len(cases[CASE_B].page.body()) > threshold
    assert len(cases[CASE_C].page.body()) > threshold
    assert len(cases[CASE_D].page.body()) < threshold


def test_the_fixture_facet_counts_are_the_ones_the_cases_claim(cases):
    """A and B are multi-facet; C is single-facet. That is the other axis.

    Held apart from size deliberately: the two axes crossed is what makes
    the set non-vacuous. A(multi, small) and C(single, large) are the two
    cells today's size-only trigger cannot tell apart from their diagonals.
    """
    assert len(cases[CASE_A].facets) >= 3
    assert len(cases[CASE_B].facets) >= 3
    assert len(cases[CASE_C].facets) == 1


def test_padding_carries_no_ground_truth(cases):
    """No fact key may be satisfiable by the declared filler.

    The fixture pads to reach a size threshold. If a fact key happened to
    appear in the pad line, a facet would score as "placed" on every child
    page and :func:`facet_alignment` would silently stop measuring.
    """
    from tests.evals.decomposition import _PAD_LINE

    for case in cases.values():
        for keys in case.facets.values():
            for key in keys:
                assert not contains_fact(_PAD_LINE, key), (
                    f"{case.id}: fact key {key!r} hides in the padding"
                )


# ---------------------------------------------------------------------------
# AC4 -- proposals, never applications
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", [CASE_A, CASE_B, CASE_C])
def test_the_shipped_default_proposes_and_never_applies(cases, tmp_path, case_id):
    """athenaeum#1581 AC4, ``docs/north-star.md`` §2.7-2.8.

    At the shipped default (``oversize_page_action: review``) no page is
    restructured for any of these cases. The over-threshold ones raise an
    ``oversize_page`` escalation for the pending queue; the under-threshold
    one raises nothing at all. ``observe_default_disposition`` re-reads the
    page after the call and fails if a single byte moved, so this is an
    assertion about the disk, not about the return value.
    """
    outcome = observe_default_disposition(cases[case_id], tmp_path)
    assert outcome.decomposed is False
    assert not outcome.children
    over_threshold = len(cases[case_id].page.body()) > DEFAULT_PAGE_SIZE_THRESHOLD_CHARS
    if over_threshold:
        assert outcome.escalation is not None
        assert outcome.escalation.conflict_type == "oversize_page"
    else:
        assert outcome.escalation is None


# ---------------------------------------------------------------------------
# AC2 -- which tier decided
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", [CASE_A, CASE_B, CASE_C])
def test_every_verdict_here_is_reached_without_a_model_call(cases, tmp_path, case_id):
    """athenaeum#1581 AC2, and it is a finding rather than a formality.

    Whatever the librarian does with A, B and C, it decides it by comparing
    a character count to a constant. No reasoning tier is consulted about
    whether a page has grown several facets, because there is no such tier
    to consult -- which is the whole content of Case A's failure below.
    """
    assert cases[case_id].deciding_tier == TIER_SIZE_GATE
    assert _outcome(cases[case_id], tmp_path).deciding_tier == TIER_SIZE_GATE


# ---------------------------------------------------------------------------
# AC3 -- the anti-vacuity gate
# ---------------------------------------------------------------------------


def test_the_gate_separates_size_from_facets(cases, tmp_path):
    """athenaeum#1581 AC3, asserted as the RELATIONSHIP between A and C.

    The observed baseline: Case C (single facet, over threshold) is decided
    correctly and Case A (three facets, under threshold) is not. Both are
    decided by the same ``len(body) > threshold`` comparison, which is why
    the pair pins the gap rather than either case alone.
    """
    a_passed, a_detail = score_case(cases[CASE_A], _outcome(cases[CASE_A], tmp_path / "a"))
    c_passed, c_detail = score_case(cases[CASE_C], _outcome(cases[CASE_C], tmp_path / "c"))

    assert c_passed, c_detail
    assert not a_passed, (
        "Case A now reaches its ground-truth verdict -- the shipped librarian has "
        "gained a facet-aware decomposition path. That is the outcome this eval "
        "exists to detect: update docs/measurements/decomposition-baseline-"
        f"2026-09-10.md with the new observation. ({a_detail})"
    )


def test_case_a_fails_for_the_stated_reason(cases, tmp_path):
    """Not merely that A fails, but that it fails by never being considered.

    A test that only asserted "A does not pass" would keep passing if A
    started failing for an unrelated reason -- a corrupted fixture, an
    exception swallowed somewhere. The observed shape is specific: the gate
    returns ``None``, no escalation is raised, no child page is written, and
    every one of A's three facets is therefore ``missing``.
    """
    outcome = _outcome(cases[CASE_A], tmp_path)
    assert outcome.escalation is None
    assert outcome.children == {}
    score = facet_alignment(outcome, cases[CASE_A].facets)
    assert set(score.missing) == set(cases[CASE_A].facets), score.summary()


def test_case_c_is_not_passing_by_accident(cases, tmp_path):
    """C is over the threshold, so it is genuinely offered to the split path.

    A negative control that the mechanism never even reaches is not a
    control. The gate DOES fire on C -- it escalates -- and declines to
    decompose it, which is the correct verdict for the wrong reason (no
    heading to cut on, rather than one facet). Both halves are pinned so a
    future facet-aware path is not credited with a result the heading check
    is producing.
    """
    outcome = _outcome(cases[CASE_C], tmp_path)
    assert len(cases[CASE_C].page.body()) > DEFAULT_PAGE_SIZE_THRESHOLD_CHARS
    assert outcome.escalation is not None
    assert outcome.escalation.conflict_type == "oversize_page"
    assert outcome.children == {}


# ---------------------------------------------------------------------------
# Case B -- where the split actually cuts
# ---------------------------------------------------------------------------


def test_case_b_decomposes_along_its_facets(cases, tmp_path):
    """The oversize page splits, and its children line up with the facets.

    True here because Case B's top-level headings ARE its facets, one to
    one. That is the favourable arrangement, stated as such in the fixture:
    the result says the mechanism preserves a facet boundary it is handed,
    not that it can find one.
    """
    outcome = _outcome(cases[CASE_B], tmp_path)
    passed, detail = score_case(cases[CASE_B], outcome)
    assert passed, detail
    assert len(outcome.children) == len(cases[CASE_B].facets)


def test_case_b_edges_name_the_split_and_not_term_overlap(cases, tmp_path):
    """The hub/sub-page edges must be gradeable as SPLIT edges specifically.

    Since athenaeum#1576 a compile-time writer stamps ``role: term-overlap``
    on ordinary pages, and a hub and its facet children are the pages most
    likely to attract one. Every edge is therefore checked by role name:
    ``split-into`` from the hub to each child, ``split-from`` from each child
    back to the hub, and nothing in the split's own output carrying any other
    role.
    """
    outcome = _outcome(cases[CASE_B], tmp_path)
    hub = cases[CASE_B].page.uid
    edges = split_edges(outcome.edges)

    assert {t for s, t, r in edges if s == hub and r == ROLE_SPLIT_INTO} == set(outcome.children)
    assert {s for s, t, r in edges if t == hub and r == ROLE_SPLIT_FROM} == set(outcome.children)
    assert len(edges) == len(outcome.edges), (
        "the split replay wrote an edge with a role outside SPLIT_ROLES: "
        f"{sorted(set(outcome.edges) - set(edges))}"
    )


def test_no_original_content_is_dropped(cases, tmp_path):
    """athenaeum#1248's own invariant, re-checked from this fixture's side.

    Every ground-truth fact key survives somewhere -- hub or child. A
    decomposition that produced tidy sub-pages by losing a facet's facts
    would score well on alignment and be catastrophic in production.
    """
    outcome = _outcome(cases[CASE_B], tmp_path)
    everywhere = outcome.hub_body + "\n".join(outcome.children.values())
    for facet, keys in cases[CASE_B].facets.items():
        for key in keys:
            assert contains_fact(everywhere, key), f"{facet}: {key!r} was dropped by the split"


# ---------------------------------------------------------------------------
# The consolidate / decompose boundary (operator design input, 2026-09-10)
# ---------------------------------------------------------------------------


def test_the_boundary_separates_cluster_b_from_a_long_family():
    """One threshold, two families of the IDENTICAL name shape, opposite verdicts.

    This is the operator's ruling made checkable. athenaeum#1570 Cluster B is
    ``Keelbridge`` / ``Keelbridge (rollout)`` -- a phase qualifier, and a
    merge. ``oversize_family`` is ``Halstow Junction`` /
    ``Halstow Junction (resignalling)`` -- also a phase qualifier, and NOT a
    merge, because the folded page would immediately be one the oversize gate
    wants to split back apart.

    No rule reading the string can separate them; the size does. Cluster B is
    read here READ-ONLY, out of the corpus as committed -- stating the
    boundary required no edit to it, which was the open question this issue
    was asked to answer.
    """
    pages = {page.uid: page for page in build_corpus(scale="core").pages}
    cluster = next(c for c in load_redundant_clusters() if c.id == "keelbridge")
    short = boundary_verdict("1570 Cluster B", [pages[uid].body for uid in cluster.merge])

    bare, qualified = load_oversize_family()
    long = boundary_verdict("1581 oversize family", [bare.body(), qualified.body()])

    assert short.verdict == "consolidate", short.summary()
    assert long.verdict == "decompose", long.summary()
    # Order of magnitude, not a hair: a boundary this fixture only just
    # clears would be a tuned constant wearing a rule's clothes.
    assert short.merged_chars * 4 < short.threshold, short.summary()
    assert long.merged_chars > long.threshold * 1.1, long.summary()


def test_the_boundary_reuses_the_oversize_gate_constant():
    """Not a second threshold -- the same one, at a different application point.

    ``check_page_size_gate`` measures ONE page as it stands;
    ``merged_body_within_page_size_threshold`` measures the PROSPECTIVE SUM
    of a fold. Sharing the constant is what makes consolidation and
    decomposition inverses rather than two tunables that can disagree, so
    the sharing is pinned rather than left to a comment.
    """
    assert boundary_verdict("x", [""]).threshold == resolve_page_size_threshold_chars(None)
    config = {"librarian": {"page_size_threshold_chars": 40}}
    assert merged_body_within_page_size_threshold(["a" * 20, "b" * 20], config=config)
    assert not merged_body_within_page_size_threshold(["a" * 20, "b" * 21], config=config)


def test_the_shipped_scan_over_proposes_on_the_long_family(tmp_path):
    """athenaeum#1577's scan and this eval CONTRADICT each other today.

    The scan is default-ON and reads name shape alone, so it proposes
    ``Halstow Junction (resignalling)`` for a fold into ``Halstow Junction``
    -- the exact merge the boundary above says is wrong, on the exact page
    pair. That contradiction is the highest-value thing this layer records,
    so it is asserted as an OBSERVED BASELINE rather than left as prose.

    Nothing is changed about the scan here. ``merged_body_within_page_size_
    threshold`` ships deliberately unwired (see its docstring): this issue
    states and grades the boundary, and wiring it into a default-ON scan is a
    behaviour change that wants its own issue and its own measurement on the
    live corpus.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    bare, qualified = load_oversize_family()
    bare.write(wiki)
    qualified.write(wiki)

    splits = scan_qualified_name_splits(wiki)
    assert [s.qualified_name for s in splits] == [qualified.name], (
        "athenaeum#1577's scan no longer proposes the long family -- if it has "
        "learned the size boundary, update the baseline doc and retire this "
        "assertion rather than loosening it."
    )
    assert boundary_verdict("long", [bare.body(), qualified.body()]).verdict == "decompose"
