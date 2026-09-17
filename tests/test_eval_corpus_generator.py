# SPDX-License-Identifier: Apache-2.0
"""Contracts the synthetic eval corpus generator must hold.

Offline and free -- these run in the default suite. They exist because the
corpus is an *instrument*: a measurement taken against a corpus that is
non-reproducible, internally inconsistent, or not actually competitive for
rank is not a weaker result, it is a meaningless one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from tests.evals.corpus import (
    CONDITION_2_ENROLLED,
    SCALES,
    Page,
    Probe,
    RelatedEdge,
    _content_terms,
    _shares_stemmed_term,
    build_corpus,
    load_core_pages,
    load_probes,
    validate_core,
)
from tests.evals.north_star_report import _relationship_probe_ids


def test_core_corpus_is_internally_consistent() -> None:
    """Every probe's ground truth and every page link must resolve.

    A dangling ``expected_uids`` reference does not fail loudly at use time --
    it silently scores as a retrieval MISS, which reads as a model regression.
    A corpus error must never be able to masquerade as an eval result.
    """
    problems = validate_core(load_core_pages(), load_probes())
    assert not problems, "corpus inconsistencies:\n  " + "\n  ".join(problems)


def test_validate_core_rejects_enrolled_class_flagged_report_only() -> None:
    """issue athenaeum#1776: an enrolled class (here ``single_hop``, which
    is in ``CONDITION_2_ENROLLED``) flagged ``report_only: True`` is a
    corpus error -- demoting an enrolled class out of §7 condition 2 must be
    a ``CONDITION_2_ENROLLED`` edit, never a ``probes.yaml`` field.
    """
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TokenOne sits here.\n\nInternal reference tag: TokenOne.",
            tier="core",
        ),
    ]
    probe = Probe(
        id="probe-demoted",
        probe_class="single_hop",
        query="what is on page a?",
        expected_uids=("page-a",),
        answer_tokens=("TokenOne",),
        report_only=True,
    )
    problems = validate_core(pages, [probe])
    assert any(
        "probe-demoted" in p and "CONDITION_2_ENROLLED" in p and "report_only is True" in p
        for p in problems
    ), problems


def test_validate_core_rejects_non_enrolled_class_with_report_only_false() -> None:
    """issue athenaeum#1776, the mirror direction: a probe_class NOT in
    ``CONDITION_2_ENROLLED`` left ``report_only: False`` is also a corpus
    error -- promotion into §7 condition 2 is an operator ruling
    (athenaeum#1736), never a side effect of a ``probes.yaml`` edit.
    """
    assert "unprompted_push" not in CONDITION_2_ENROLLED
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TokenOne sits here.\n\nInternal reference tag: TokenOne.",
            tier="core",
        ),
    ]
    probe = Probe(
        id="probe-promoted",
        probe_class="unprompted_push",
        query="what is on page a?",
        expected_uids=("page-a",),
        answer_tokens=("TokenOne",),
        report_only=False,
    )
    problems = validate_core(pages, [probe])
    assert any(
        "probe-promoted" in p and "CONDITION_2_ENROLLED" in p and "report_only is False" in p
        for p in problems
    ), problems


def test_validate_core_rejects_unplantable_forbidden_token() -> None:
    """issue athenaeum#1772: a ``forbidden_tokens`` value that occurs on NO
    corpus page cannot be planted on a decoy page -- ``grade_harm`` would
    have nothing to check the answer against."""
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TokenOne sits here.\n\nInternal reference tag: TokenOne.",
            tier="core",
        ),
    ]
    probe = Probe(
        id="probe-harm",
        probe_class="single_hop",
        query="what is on page a?",
        expected_uids=("page-a",),
        answer_tokens=("TokenOne",),
        forbidden_tokens=("Ghostword",),
    )
    problems = validate_core(pages, [probe])
    assert any(
        "probe-harm" in p and "Ghostword" in p and "plantable" in p for p in problems
    ), problems


def test_validate_core_rejects_forbidden_token_colliding_with_answer_tokens() -> None:
    """issue athenaeum#1772: a ``forbidden_tokens`` value that collides with
    a probe's ``answer_tokens`` anywhere in the corpus is a corpus error --
    ``grade_correctness``'s abstention confabulation check reads the
    corpus-wide ``answer_tokens`` deny-list, so a shared value would make a
    legitimate answer read as harmful (or vice versa)."""
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TokenOne sits here.\n\nInternal reference tag: TokenOne.",
            tier="core",
        ),
    ]
    probe = Probe(
        id="probe-harm",
        probe_class="single_hop",
        query="what is on page a?",
        expected_uids=("page-a",),
        answer_tokens=("TokenOne",),
        forbidden_tokens=("TokenOne",),
    )
    problems = validate_core(pages, [probe])
    assert any(
        "probe-harm" in p and "TokenOne" in p and "collides" in p for p in problems
    ), problems


def test_validate_core_rejects_forbidden_token_shared_between_two_pages() -> None:
    """issue athenaeum#1772: a ``forbidden_tokens`` value must be plantable
    on exactly one decoy page -- one occurring on two pages makes it
    ambiguous which page's occurrence a correct "avoided the forbidden
    token" answer is actually about."""
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TokenOne sits here.\n\nInternal reference tag: TokenOne.",
            tier="core",
        ),
        Page(
            uid="page-decoy",
            type="note",
            name="Decoy Page",
            body="Ghostword appears on this decoy page too.",
            tier="core",
        ),
        Page(
            uid="page-decoy-2",
            type="note",
            name="Decoy Page 2",
            body="Ghostword also appears here.",
            tier="core",
        ),
    ]
    probe = Probe(
        id="probe-harm",
        probe_class="single_hop",
        query="what is on page a?",
        expected_uids=("page-a",),
        answer_tokens=("TokenOne",),
        forbidden_tokens=("Ghostword",),
    )
    problems = validate_core(pages, [probe])
    assert any(
        "probe-harm" in p and "Ghostword" in p and "multiple pages" in p for p in problems
    ), problems


def test_validate_core_accepts_valid_forbidden_tokens() -> None:
    """issue athenaeum#1772 positive control: a ``forbidden_tokens`` value
    that is plantable on exactly one page and collides with no probe's
    ``answer_tokens`` passes ``validate_core`` cleanly."""
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TokenOne sits here.\n\nInternal reference tag: TokenOne.",
            tier="core",
        ),
        Page(
            uid="page-decoy",
            type="note",
            name="Decoy Page",
            body="Ghostword appears on this decoy page.",
            tier="core",
        ),
    ]
    probe = Probe(
        id="probe-harm",
        probe_class="single_hop",
        query="what is on page a?",
        expected_uids=("page-a",),
        answer_tokens=("TokenOne",),
        forbidden_tokens=("Ghostword",),
    )
    problems = validate_core(pages, [probe])
    # Only forbidden_tokens-specific problems are pinned here -- the module
    # also runs an unrelated, module-scoped relatedness-cluster check
    # (`_validate_relatedness_ground_truth`) whose fixtures do not exist in
    # this synthetic corpus at all, so asserting `not problems` outright
    # would fail on noise this test has no way to satisfy.
    forbidden_problems = [p for p in problems if "probe-harm" in p and "Ghostword" in p]
    assert not forbidden_problems, forbidden_problems


def test_validate_core_rejects_forbidden_token_on_its_own_expected_page() -> None:
    """issue athenaeum#1772: a ``forbidden_tokens`` value must not occur on
    the probe's OWN ``expected_uids`` (correct-answer) page -- it belongs
    on a decoy page. An answer that legitimately cites the right page would
    otherwise get flagged as harmful for citing content that was never
    actually a decoy."""
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body=(
                "TokenOne sits here, and so does Ghostword.\n\n"
                "Internal reference tag: TokenOne."
            ),
            tier="core",
        ),
    ]
    probe = Probe(
        id="probe-harm",
        probe_class="single_hop",
        query="what is on page a?",
        expected_uids=("page-a",),
        answer_tokens=("TokenOne",),
        forbidden_tokens=("Ghostword",),
    )
    problems = validate_core(pages, [probe])
    assert any(
        "probe-harm" in p and "Ghostword" in p and "expected_uids page" in p for p in problems
    ), problems


def test_validate_core_rejects_forbidden_token_colliding_with_answer_token_via_substring() -> None:
    """issue athenaeum#1772: the collision guard is SUBSTRING-aware in both
    directions after normalization -- ``grade_harm``/``grade_correctness``
    match by normalized substring, not exact string equality, so a
    forbidden token that is merely a substring of an answer token (or vice
    versa) must still be flagged, in both directions ("Ash" vs
    "Ashfallow")."""
    # Direction 1: the forbidden token is a substring of the answer token.
    pages_dir1 = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="Ashfallow sits here.\n\nInternal reference tag: Ashfallow.",
            tier="core",
        ),
        Page(
            uid="page-decoy",
            type="note",
            name="Decoy Page",
            body="Ash appears on this decoy page.",
            tier="core",
        ),
    ]
    probe_dir1 = Probe(
        id="probe-substr-1",
        probe_class="single_hop",
        query="what is on page a?",
        expected_uids=("page-a",),
        answer_tokens=("Ashfallow",),
        forbidden_tokens=("Ash",),
    )
    problems_dir1 = validate_core(pages_dir1, [probe_dir1])
    assert any(
        "probe-substr-1" in p and "collides" in p and "Ashfallow" in p for p in problems_dir1
    ), problems_dir1

    # Direction 2: the answer token is a substring of the forbidden token.
    pages_dir2 = [
        Page(
            uid="page-b",
            type="note",
            name="Page B",
            body="Ash sits here.\n\nInternal reference tag: Ash.",
            tier="core",
        ),
        Page(
            uid="page-decoy-2",
            type="note",
            name="Decoy Page 2",
            body="Ashfallow appears on this decoy page.",
            tier="core",
        ),
    ]
    probe_dir2 = Probe(
        id="probe-substr-2",
        probe_class="single_hop",
        query="what is on page b?",
        expected_uids=("page-b",),
        answer_tokens=("Ash",),
        forbidden_tokens=("Ashfallow",),
    )
    problems_dir2 = validate_core(pages_dir2, [probe_dir2])
    assert any(
        "probe-substr-2" in p and "collides" in p and "Ash" in p for p in problems_dir2
    ), problems_dir2


def test_load_probes_defaults_report_only_from_class_enrolment(monkeypatch, tmp_path) -> None:
    """issue athenaeum#1776: when ``probes.yaml`` is silent on
    ``report_only``, :func:`load_probes` defaults it to
    ``probe_class not in CONDITION_2_ENROLLED`` -- an enrolled class stays
    ``False``, a brand-new class defaults ``True``.
    """
    import tests.evals.corpus as corpus_module

    probes_path = tmp_path / "probes.yaml"
    probes_path.write_text(
        """
- id: probe-enrolled
  probe_class: single_hop
  query: q1
  expected_uids: [page-a]
  answer_tokens: [TokenOne]
- id: probe-new-class
  probe_class: some_new_wave2_class
  query: q2
  expected_uids: [page-a]
  answer_tokens: [TokenTwo]
- id: probe-explicit-override
  probe_class: single_hop
  query: q3
  expected_uids: [page-a]
  answer_tokens: [TokenThree]
  report_only: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(corpus_module, "PROBES_PATH", probes_path)
    probes = {p.id: p for p in load_probes()}
    assert probes["probe-enrolled"].report_only is False
    assert probes["probe-new-class"].report_only is True
    assert probes["probe-explicit-override"].report_only is True


def test_load_probes_parses_forbidden_tokens(monkeypatch, tmp_path) -> None:
    """issue athenaeum#1772: ``load_probes`` parses ``forbidden_tokens``
    alongside ``answer_tokens``, defaulting to ``()`` when the yaml is
    silent -- exactly the shape ``answer_tokens`` already has."""
    import tests.evals.corpus as corpus_module

    probes_path = tmp_path / "probes.yaml"
    probes_path.write_text(
        """
- id: probe-with-forbidden
  probe_class: single_hop
  query: q1
  expected_uids: [page-a]
  answer_tokens: [TokenOne]
  forbidden_tokens: [Ghostword]
- id: probe-without-forbidden
  probe_class: single_hop
  query: q2
  expected_uids: [page-a]
  answer_tokens: [TokenTwo]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(corpus_module, "PROBES_PATH", probes_path)
    probes = {p.id: p for p in load_probes()}
    assert probes["probe-with-forbidden"].forbidden_tokens == ("Ghostword",)
    assert probes["probe-without-forbidden"].forbidden_tokens == ()


def test_every_non_abstention_probe_answer_token_is_in_its_page_body() -> None:
    """AC1 (issue athenaeum#1573): every non-abstention probe must carry at
    least one ``answer_tokens`` value, and it must actually occur in one of
    the probe's own ``expected_uids`` pages' bodies -- a planted token that
    does not occur anywhere would silently grade every rollout incorrect,
    which reads as a model regression rather than a corpus authoring bug.

    Also pins the inverse for abstention probes: they carry NO answer
    tokens, because nothing in the corpus answers them (see
    ``tests.evals.north_star_report``'s separate abstention grading rule).
    """
    pages_by_uid = {page.uid: page for page in load_core_pages()}
    probes = load_probes()
    non_abstention = [p for p in probes if p.probe_class != "abstention"]
    abstention = [p for p in probes if p.probe_class == "abstention"]
    assert non_abstention, "expected at least one non-abstention probe"
    assert abstention, "expected at least one abstention probe"

    for probe in non_abstention:
        assert probe.answer_tokens, f"probe {probe.id!r} has no answer_tokens"
        body = "\n".join(pages_by_uid[uid].body for uid in probe.expected_uids)
        assert any(token in body for token in probe.answer_tokens), (
            f"probe {probe.id!r}: none of {probe.answer_tokens} appear in its "
            f"answer page body ({probe.expected_uids})"
        )

    for probe in abstention:
        assert probe.answer_tokens == (), f"abstention probe {probe.id!r} must have no tokens"


def _two_page_follow_through(
    *,
    second_hop_shares_query_term: bool,
    edge_between_expected_pages: bool,
    source_shares_query_term: bool = True,
    body_wikilink: bool = True,
    frontmatter_edge: bool = True,
    second_hop_carries_token: bool = True,
) -> tuple[list[Page], Probe]:
    """Build the minimal two-page fixture ``validate_core``'s
    ``follow_through`` checks operate on, with each check independently
    toggleable so they can be tested in isolation.

    ``body_wikilink``/``frontmatter_edge`` are separate knobs (issue
    athenaeum#1737 Quine finding): the real corpus authors BOTH on every
    first-hop page (the frontmatter edge feeds the relatedness ground
    truth), but only the body ``[[wikilink]]`` is what a live ``recall`` hit
    actually renders, so ``validate_core`` must accept only that one as the
    qualifying edge.
    """
    edge_target = "page-b" if edge_between_expected_pages else "page-unrelated"
    second_hop_body = (
        "Alderquill covers galaxy formation in early cosmic history."
        if second_hop_shares_query_term
        else "Verdigrove is a quiet coastal harbour town."
    )
    token_two_clause = (
        "TokenTwo sits here.\n\nInternal reference tag: TokenTwo. "
        if second_hop_carries_token
        else ""
    )
    wikilink_clause = f" See [[{edge_target}]] for more." if body_wikilink else ""
    source_body = (
        f"TokenOne sits here, in a note about early galaxy history.{wikilink_clause}\n\n"
        "Internal reference tag: TokenOne."
        if source_shares_query_term
        else f"TokenOne sits here.{wikilink_clause}\n\nInternal reference tag: TokenOne."
    )
    related = (RelatedEdge(uid=edge_target, role="related"),) if frontmatter_edge else ()
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body=source_body,
            tier="core",
            related=related,
        ),
        Page(
            uid="page-b",
            type="note",
            name="Page B",
            body=f"{token_two_clause}{second_hop_body}",
            tier="core",
        ),
        Page(
            uid="page-unrelated",
            type="note",
            name="Unrelated",
            body="Nothing planted.",
            tier="core",
        ),
    ]
    probe = Probe(
        id="synthetic_follow_through",
        probe_class="follow_through",
        query="what is the history of galaxy formation?",
        expected_uids=("page-a", "page-b"),
        answer_tokens=("TokenOne", "TokenTwo"),
    )
    return pages, probe


def test_follow_through_rejects_tokens_concentrated_on_one_page() -> None:
    """AC (issue athenaeum#1737): a ``follow_through`` probe whose
    ``answer_tokens`` all sit on a single ``expected_uids`` page has nothing
    to follow through TO, and must be rejected at load."""
    pages, probe = _two_page_follow_through(
        second_hop_shares_query_term=False, edge_between_expected_pages=True
    )
    pages[1] = Page(
        uid="page-b",
        type="note",
        name="Page B",
        body="Verdigrove is a quiet coastal harbour town.",
        tier="core",
    )
    # Both tokens now sit on page-a only.
    pages[0] = Page(
        uid="page-a",
        type="note",
        name="Page A",
        body="TokenOne and TokenTwo both sit here.",
        tier="core",
        related=(RelatedEdge(uid="page-b", role="related"),),
    )
    problems = validate_core(pages, [probe])
    assert any("split across at least two" in p for p in problems), problems


def test_follow_through_rejects_second_hop_sharing_a_query_term() -> None:
    """AC (issue athenaeum#1737): the second-hop page must share NO content
    term with the query -- otherwise a plain lexical match on the query
    would reach it directly, without ever following the edge."""
    pages, probe = _two_page_follow_through(
        second_hop_shares_query_term=True, edge_between_expected_pages=True
    )
    problems = validate_core(pages, [probe])
    assert any("share no content term" in p for p in problems), problems


def test_follow_through_rejects_source_page_sharing_no_query_term() -> None:
    """AC (issue athenaeum#1737, Sentry finding): the SOURCE page of the
    qualifying edge must itself share a content term with the query --
    otherwise a probe could be authored where nothing in ``expected_uids``
    is lexically reachable from the query at all, and the no-overlap check
    on the second-hop page would pass on a technicality rather than because
    a real breadcrumb was followed."""
    pages, probe = _two_page_follow_through(
        second_hop_shares_query_term=False,
        edge_between_expected_pages=True,
        source_shares_query_term=False,
    )
    problems = validate_core(pages, [probe])
    assert any("shares a content term with the" in p for p in problems), problems


def test_follow_through_rejects_no_edge_between_expected_pages() -> None:
    """AC (issue athenaeum#1737): the second-hop page must be reachable from
    another ``expected_uids`` page by a body ``[[wikilink]]``; a wikilink to
    some other page in the corpus does not satisfy the assertion."""
    pages, probe = _two_page_follow_through(
        second_hop_shares_query_term=False, edge_between_expected_pages=False
    )
    problems = validate_core(pages, [probe])
    assert any("related/links edge" in p for p in problems), problems


def test_follow_through_rejects_frontmatter_only_edge() -> None:
    """AC (issue athenaeum#1737, MUST finding): the qualifying edge must be a
    body ``[[wikilink]]``, not merely a ``related``/``links`` frontmatter
    entry. A live ``recall`` hit renders its ``**Links:**`` line from the
    body only (``athenaeum.mcp_server._extract_outbound_links``) --
    frontmatter-only edges reach an agent solely through a native arm's
    raw-file grep, so a probe authored that way would be passable by grep
    and structurally unpassable by Athenaeum, the exact asymmetry this class
    exists to catch."""
    pages, probe = _two_page_follow_through(
        second_hop_shares_query_term=False,
        edge_between_expected_pages=True,
        body_wikilink=False,
        frontmatter_edge=True,
    )
    problems = validate_core(pages, [probe])
    assert any("body [[wikilink]]" in p for p in problems), problems


def test_follow_through_rejects_hop_target_with_no_planted_token() -> None:
    """AC (issue athenaeum#1737, SHOULD finding): the page reached by the
    qualifying body edge must itself carry a planted answer token. Three
    pages: A (token1, body-links to B), B (no token, clean of query terms),
    C (token2, unlinked, shares a query term). The token-split check passes
    trivially (token1 on A, token2 on C), and B is a clean, reachable
    target -- but B carries no token, so following the edge to it would
    never reach an answer."""
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TokenOne sits here, about early galaxy history. See [[page-b]] for more.",
            tier="core",
        ),
        Page(
            uid="page-b",
            type="note",
            name="Page B",
            body="Verdigrove is a quiet coastal harbour town.",
            tier="core",
        ),
        Page(
            uid="page-c",
            type="note",
            name="Page C",
            body="TokenTwo sits here, also about galaxy formation history.",
            tier="core",
        ),
    ]
    probe = Probe(
        id="synthetic_follow_through_no_token_hop",
        probe_class="follow_through",
        query="what is the history of galaxy formation?",
        expected_uids=("page-a", "page-b", "page-c"),
        answer_tokens=("TokenOne", "TokenTwo"),
    )
    problems = validate_core(pages, [probe])
    assert any("carries a planted answer token" in p for p in problems), problems


def test_follow_through_rejects_target_leaking_query_term_via_uid_or_name() -> None:
    """AC (issue athenaeum#1737, SHOULD finding): the second-hop page's
    ``uid``/``name``/``tags`` must also share no content term with the
    query -- including a >=5-character stemmed-prefix match -- not just its
    body. A native arm's topic file is named ``<uid>.md`` and a grep can
    match on name/tags too, so a page whose BODY is clean but whose uid
    leaks query vocabulary (here ``formative`` vs. the query's
    ``formation`` -- both prefix ``forma``) is still grep-reachable and
    must be rejected."""
    pages, probe = _two_page_follow_through(
        second_hop_shares_query_term=False, edge_between_expected_pages=True
    )
    pages[1] = Page(
        uid="page-formative-notes",
        type="note",
        name="Page B",
        body="TokenTwo sits here. Verdigrove is a quiet coastal harbour town.",
        tier="core",
    )
    probe = Probe(
        id=probe.id,
        probe_class=probe.probe_class,
        query=probe.query,
        expected_uids=("page-a", "page-formative-notes"),
        answer_tokens=probe.answer_tokens,
    )
    # page-a's body wikilink must point at the renamed uid to stay reachable.
    pages[0] = Page(
        uid="page-a",
        type="note",
        name="Page A",
        body=(
            "TokenOne sits here, in a note about early galaxy history. "
            "See [[page-formative-notes]] for more."
        ),
        tier="core",
    )
    problems = validate_core(pages, [probe])
    assert any("uid, name, and tags" in p for p in problems), problems


def test_content_terms_strips_stopwords_and_tokens_under_three_chars() -> None:
    """AC (issue athenaeum#1737, SHOULD finding): pins BOTH filters
    ``_content_terms`` applies, so a future edit that drops either one fails
    this test rather than silently loosening every ``follow_through``
    overlap check that depends on it. ``the``/``what``/``and`` are
    stopwords; ``ok``/``id``/``at`` are not stopwords but fall under the
    ``len(w) >= 3`` floor -- only ``cats`` survives both filters."""
    assert _content_terms("the what and to") == set()
    assert _content_terms("ok id at cats") == {"cats"}


def test_follow_through_passes_when_both_halves_hold() -> None:
    """The positive control: split tokens plus a qualifying edge produces no
    ``follow_through``-specific problem.

    ``validate_core`` also runs the (unrelated) relatedness ground-truth
    check over the full page set, which this minimal synthetic fixture does
    not attempt to satisfy -- so the assertion is scoped to problems
    mentioning THIS probe, not an empty list overall.
    """
    pages, probe = _two_page_follow_through(
        second_hop_shares_query_term=False, edge_between_expected_pages=True
    )
    problems = validate_core(pages, [probe])
    own_problems = [p for p in problems if probe.id in p]
    assert own_problems == [], own_problems


def test_validate_core_rejects_expected_uid_page_with_no_tag_line() -> None:
    """athenaeum#1766 defect 2: a page named in some probe's
    ``expected_uids`` but carrying no ``Internal reference tag:`` line has
    nothing a model can cite for it, so a correct answer that draws on the
    page falls back to citing its ``uid`` -- exactly the
    ``spend_approver_named`` failure mode the issue describes
    (``policy-budget-approval`` had no tag line before this issue). Verified
    against the real fixtures too: reverting the ``policy-budget-approval``
    tag line added by this PR reproduces this failure on
    ``test_core_corpus_is_internally_consistent``.
    """
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TokenOne sits here.\n\nInternal reference tag: TokenOne.",
            tier="core",
        ),
        Page(uid="page-b", type="note", name="Page B", body="No tag line here.", tier="core"),
    ]
    probe = Probe(
        id="probe-untagged",
        probe_class="multi_hop",
        query="what about page a and page b?",
        expected_uids=("page-a", "page-b"),
        answer_tokens=("TokenOne",),
    )
    problems = validate_core(pages, [probe])
    assert any(
        "page-b" in p and "carries no" in p and "Internal reference tag" in p for p in problems
    ), problems


def test_validate_core_rejects_must_not_rank_overlapping_expected_uids() -> None:
    """athenaeum#1777: a uid named in BOTH a probe's ``must_not_rank`` and
    its ``expected_uids`` would grade retrieving that page as simultaneously
    correct (a retrieval hit) and a contamination hit (a precision failure)
    -- not a coherent ground-truth assertion, and silently misleading for
    any precision/contamination report that reads ``must_not_rank`` (the
    first real consumer is issue athenaeum#1782). Verified against the real
    fixtures too: ``test_core_corpus_is_internally_consistent`` passes with
    zero problems, so this check is not merely reachable in principle.
    """
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TokenOne sits here.\n\nInternal reference tag: TokenOne.",
            tier="core",
        ),
        Page(uid="page-b", type="note", name="Page B", body="Unrelated body text.", tier="core"),
    ]
    probe = Probe(
        id="probe-contradictory",
        probe_class="single_hop",
        query="what about page a?",
        expected_uids=("page-a",),
        must_not_rank=("page-a", "page-b"),
        answer_tokens=("TokenOne",),
    )
    problems = validate_core(pages, [probe])
    assert any(
        "probe-contradictory" in p and "must_not_rank and expected_uids both name" in p
        for p in problems
    ), problems


def test_every_non_abstention_probe_has_must_not_rank_or_a_documented_na() -> None:
    """athenaeum#1777 acceptance criterion: every non-abstention probe either
    carries a ``must_not_rank`` set or is explicitly excluded with a
    documented reason, so a precision/contamination report (issue
    athenaeum#1782) never has to default a probe's precision to a silent 1.0
    off an empty negative set. The documented-reason convention is a
    ``note`` beginning with the literal marker ``precision: n/a`` -- see
    ``data/corpus/probes/probes.yaml``'s two current uses.
    """
    probes = load_probes()
    non_abstention = [p for p in probes if p.probe_class != "abstention"]
    assert non_abstention, "expected at least one non-abstention probe"
    unexplained = [
        p.id
        for p in non_abstention
        if not p.must_not_rank and "precision: n/a" not in p.note
    ]
    assert unexplained == [], (
        "probes with neither must_not_rank nor a 'precision: n/a' note: " + str(unexplained)
    )


def test_validate_core_rejects_two_pages_sharing_a_tag() -> None:
    """athenaeum#1766 AC2, the collision half: two pages carrying the same
    invented token would let ``grade_correctness``'s whole-corpus token scan
    credit an answer that drew on one page's fact against a different page's
    probe. Verified against the real fixtures too: giving
    ``project-keelbridge-rollout``'s tag (added by this PR) the same value
    as an existing token reproduces this failure on
    ``test_core_corpus_is_internally_consistent``.
    """
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TagOne sits here.\n\nInternal reference tag: TagOne.",
            tier="core",
        ),
        Page(
            uid="page-b",
            type="note",
            name="Page B",
            body="Something else entirely.\n\nInternal reference tag: TagOne.",
            tier="core",
        ),
    ]
    probe = Probe(
        id="probe-collision",
        probe_class="multi_hop",
        query="what about page a and page b?",
        expected_uids=("page-a", "page-b"),
        answer_tokens=("TagOne",),
    )
    problems = validate_core(pages, [probe])
    assert any("TagOne" in p and "multiple pages" in p for p in problems), problems


def test_validate_core_rejects_multi_hop_token_page_that_is_lexically_reachable() -> None:
    """athenaeum#1768 AC2: the multi_hop counterpart of the follow_through
    lexical-unreachability check. ``spend_approver_named`` and
    ``portal_design_reviewer`` were both ungradable before this issue
    because the page carrying the planted token restated enough of the
    query's own vocabulary (exactly, or via a stemmed uid/name/tags prefix)
    that a plain lexical match or grep could land on it directly, without
    ever needing the second hop.

    Synthetic case here checks the exact-body-overlap half; the sibling
    test ``test_multi_hop_token_pages_are_lexically_unreachable`` confirms
    the real fixtures (including their uid/name/tags stemmed-meta half)
    pass this same check, matching this file's existing convention of
    pairing a synthetic failure case with a live-fixture confirmation.
    """
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="Page A talks about widgets.",
            tier="core",
        ),
        Page(
            uid="page-b",
            type="note",
            name="Page B",
            body="TokenTwo sits here, and it is also about widgets.\n\n"
            "Internal reference tag: TokenTwo.",
            tier="core",
        ),
    ]
    probe = Probe(
        id="probe-reachable-token",
        probe_class="multi_hop",
        query="what widgets does page a mention?",
        expected_uids=("page-a", "page-b"),
        answer_tokens=("TokenTwo",),
    )
    problems = validate_core(pages, [probe])
    assert any(
        "probe-reachable-token" in p and "page-b" in p and "content term" in p for p in problems
    ), problems


def test_multi_hop_token_pages_are_lexically_unreachable() -> None:
    """athenaeum#1768 AC2, restated directly against the live fixtures
    (mirroring ``test_follow_through_second_pages_are_lexically_unreachable_and_tokened``'s
    convention): for every real ``multi_hop`` probe, the ``expected_uids``
    page carrying the planted token must share no content term -- exact, or
    a stemmed uid/name/tags prefix -- with the query.
    """
    pages_by_uid = {page.uid: page for page in load_core_pages()}
    multi_hop_probes = [p for p in load_probes() if p.probe_class == "multi_hop"]
    assert multi_hop_probes, "expected at least one multi_hop probe"
    for probe in multi_hop_probes:
        query_terms = _content_terms(probe.query)
        expected_pages = [pages_by_uid[uid] for uid in probe.expected_uids]
        token_pages = [
            page
            for page in expected_pages
            if any(token in page.body for token in probe.answer_tokens)
        ]
        assert token_pages, probe.id
        for page in token_pages:
            assert not (_content_terms(page.body) & query_terms), (probe.id, page.uid)
            page_meta_terms = _content_terms(
                f"{page.uid.replace('-', ' ')} {page.name} {' '.join(page.tags)}"
            )
            assert not _shares_stemmed_term(page_meta_terms, query_terms), (probe.id, page.uid)


def test_validate_core_rejects_multi_hop_token_page_reachable_via_stemmed_meta() -> None:
    """athenaeum#1768 AC2, the stemmed uid/name/tags branch -- the sibling
    of ``test_validate_core_rejects_multi_hop_token_page_that_is_lexically_reachable``,
    which only exercises the exact-body-overlap branch. Here the BODY shares
    no content term with the query at all, but the page's own uid/tags leak
    the query's vocabulary through a >=5-character stemmed prefix (mirroring
    the ``follow_through`` stemmed-meta check's own synthetic coverage
    style), so a native arm's grep over its topic file's own name would
    still reach it without the second hop.
    """
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="Page A talks about something else entirely.",
            tier="core",
        ),
        Page(
            uid="tooling-widgetworks",
            type="note",
            name="widgetworks",
            body="TokenThree sits here.\n\nInternal reference tag: TokenThree.",
            tier="core",
            tags=("tooling",),
        ),
    ]
    probe = Probe(
        id="probe-stemmed-reachable",
        probe_class="multi_hop",
        query="what page a's tooling does the widget work rely on?",
        expected_uids=("page-a", "tooling-widgetworks"),
        answer_tokens=("TokenThree",),
    )
    problems = validate_core(pages, [probe])
    assert any(
        "probe-stemmed-reachable" in p and "tooling-widgetworks" in p and "stemmed term" in p
        for p in problems
    ), problems


def test_ratecard_tooling_owner_answer_person_is_not_named_off_expected_uids() -> None:
    """athenaeum#1768 Quine review: ``validate_core``'s multi_hop check only
    scans ``expected_uids`` pages (see the ``Probe`` docstring's scope
    note), so it would not catch a DIFFERENT core page that shares
    ``ratecard_tooling_owner``'s query vocabulary while naming its answer
    person outright. Before this issue, ``person-tomas-briell`` did exactly
    that: it named Tomas Briell and said he is "one of four maintainers on
    the rate-card repository," sharing "rate", "card", and "repository"
    with the query and reaching the answer without the hop. Restated here
    directly, independent of ``validate_core``'s control flow, since the
    corpus check cannot derive "the answer identity" as a general string to
    search for and so cannot enforce this itself (an authoring discipline,
    not a checked contract).
    """
    pages_by_uid = {page.uid: page for page in load_core_pages()}
    probe = next(p for p in load_probes() if p.id == "ratecard_tooling_owner")
    query_terms = _content_terms(probe.query)
    # The answer to "who maintains buildpipe" is Tomas Briell (tool-buildpipe's
    # body says "Maintained by Tomas Briell"); neither of ratecard_tooling_owner's
    # own expected_uids pages is a person page, so his name is not derivable
    # from the probe's own fields the way it would be for a probe whose
    # expected_uids includes a person page directly.
    answer_person = "Tomas Briell"
    assert any(
        answer_person in pages_by_uid[uid].body for uid in probe.expected_uids
    ), "fixture drifted: the answer person is no longer named in an expected_uids page"
    for page in pages_by_uid.values():
        if page.uid in probe.expected_uids:
            continue
        if answer_person not in page.body:
            continue
        assert not (_content_terms(page.body) & query_terms), (
            probe.id,
            page.uid,
            "names the answer person while sharing a query content term",
        )


def test_follow_through_second_pages_are_lexically_unreachable_and_tokened() -> None:
    """athenaeum#1766 AC3: pins, for every real ``follow_through`` probe,
    that exactly one of its ``expected_uids`` pages is lexically unreachable
    from the query -- no content term, including a stemmed prefix, shared
    with the query via body OR uid/name/aliases/tags -- and that this
    second-hop page carries one of the probe's ``answer_tokens``.

    ``validate_core``'s own ``follow_through`` check already enforces this
    structurally (see the ``Probe`` docstring); this test restates it
    directly against the live fixtures, independent of that check's control
    flow, so a regression in either the fixture or the check is caught from
    both ends.
    """
    pages_by_uid = {page.uid: page for page in load_core_pages()}
    follow_through_probes = [p for p in load_probes() if p.probe_class == "follow_through"]
    assert follow_through_probes, "expected at least one follow_through probe"
    for probe in follow_through_probes:
        query_terms = _content_terms(probe.query)
        expected_pages = [pages_by_uid[uid] for uid in probe.expected_uids]
        second_pages = [
            page
            for page in expected_pages
            if not (_content_terms(page.body) & query_terms)
            and not _shares_stemmed_term(
                _content_terms(
                    f"{page.uid.replace('-', ' ')} {page.name} "
                    f"{' '.join(page.aliases)} {' '.join(page.tags)}"
                ),
                query_terms,
            )
        ]
        assert len(second_pages) == 1, (probe.id, [p.uid for p in expected_pages])
        second_page = second_pages[0]
        assert any(token in second_page.body for token in probe.answer_tokens), probe.id


def test_generation_is_deterministic_within_a_process() -> None:
    first = build_corpus(scale="small", seed=4242)
    second = build_corpus(scale="small", seed=4242)
    assert first.fingerprint() == second.fingerprint()


def test_generation_is_deterministic_ACROSS_processes() -> None:
    """The contract that actually matters, and the one a same-process test
    cannot see.

    Python salts string hashing per process, so any generation input derived
    from builtin ``hash()`` differs between runs while an in-process test
    passes -- which is precisely how this shipped broken once. A corpus that
    is not reproducible across processes makes every stored fingerprint name
    a corpus nobody can rebuild, so this must run in a SUBPROCESS.
    """
    script = (
        "from tests.evals.corpus import build_corpus; "
        "print(build_corpus(scale='small', seed=4242).fingerprint())"
    )
    env = {**os.environ, "PYTHONPATH": "src"}
    seen = {
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
        ).stdout.strip()
        for _ in range(3)
    }
    assert len(seen) == 1, f"fingerprint varies across processes: {seen}"


def test_different_seeds_yield_different_corpora() -> None:
    """Guards the inverse: a seed that is ignored would make every run identical
    and silently collapse replicates into one sample."""
    assert (
        build_corpus(scale="small", seed=1).fingerprint()
        != build_corpus(scale="small", seed=2).fingerprint()
    )


def test_xlarge_scale_is_pinned() -> None:
    """Issue athenaeum#1735: page count, ``distractors_per_probe``, and the
    default-seed fingerprint for ``xlarge`` are pinned so a change to
    ``SCALES["xlarge"]`` or to the generator that silently shifts its
    output is caught here rather than only in CI's timing/leakage checks.

    The fingerprint literal is DELIBERATELY brittle to a ``GENERATOR_VERSION``
    bump (:data:`tests.evals.corpus.GENERATOR_VERSION`) -- any version bump
    is expected to change every stored fingerprint across the whole corpus
    module, not just this one, and this test failing is the intended signal
    to re-derive and update the pinned value, not a bug in the pin itself.
    """
    assert SCALES["xlarge"].total_pages >= 25_000
    assert SCALES["xlarge"].distractors_per_probe == 2
    corpus = build_corpus(scale="xlarge")
    assert len(corpus.pages) >= 25_000
    # athenaeum#1766: six core pages gained a new `Internal reference tag:`
    # line, the follow_through probe queries were reworded, and
    # client-fenwick-systems's body was trimmed to remove a cadence leak
    # (Quine review) -- each shifts every stored fingerprint that hashes
    # rendered markdown, same class of expected change as a
    # GENERATOR_VERSION bump, per this test's own docstring.
    # athenaeum#1768: person-amir-osei, person-hana-lindqvist, and
    # tool-buildpipe body text changed (the multi_hop token pages, to stop
    # restating the query's own vocabulary), the three multi_hop probe
    # queries were reworded, and their distractor_terms were updated to
    # match (planted distractor pages are built from distractor_terms --
    # see `_generate_distractors` -- so stale terms would plant decoys that
    # no longer compete with the reworded query) -- same class of expected
    # fingerprint shift. Quine review then found person-tomas-briell (outside
    # ratecard_tooling_owner's expected_uids) independently named the answer
    # person while sharing the query's own "rate"/"card"/"repository" terms,
    # so its bio was reworded too, shifting the fingerprint once more.
    # athenaeum#1779: 12-long-pages.yaml added four new core pages (tier
    # `long`) and four new single_hop probes -- new pages in `core/*.yaml`
    # always shift `Corpus.fingerprint()` since it hashes every page's
    # rendered markdown, same class of expected change as the entries above.
    assert corpus.fingerprint() == "023d72e4ea0981b9"


def test_long_tier_tag_is_outside_the_recall_snippet() -> None:
    """Behavioral companion to `validate_core`'s offset check (athenaeum#1779).

    The offset check pins a number (`tag offset > _snippet's max_chars
    default`); it does not by itself prove `recall`'s snippet -- which is
    windowed around the QUERY'S FIRST MATCH, not from character 0 -- cannot
    reach the tag anyway if that match sits deep in the page. This test
    calls the real `_snippet` with each long-page probe's own tokenized
    query and asserts the tag word is absent from what it returns, which is
    the actual claim ("read_entity was required"), not merely a proxy for
    it. No model call.
    """
    from athenaeum.mcp_server import _snippet, tokenize_keyword_query

    pages_by_uid = {page.uid: page for page in load_core_pages()}
    long_probes = [probe for probe in load_probes() if probe.tier == "long"]
    assert long_probes, "no long-tier probes found -- fixture or loader regressed"
    for probe in long_probes:
        for uid in probe.expected_uids:
            page = pages_by_uid[uid]
            if page.tier != "long":
                continue
            snippet = _snippet(page.body, tokenize_keyword_query(probe.query))
            for token in probe.answer_tokens:
                assert token not in snippet, (
                    f"probe {probe.id!r}: answer token {token!r} appears in the "
                    "recall snippet for its own query -- the long-page tier claim "
                    "(that read_entity is required) does not hold for this page"
                )


def test_core_scale_generates_nothing() -> None:
    """``core`` scale must not add distractor/ballast padding.

    Compared against a `Counter` over `load_core_pages()`'s own tiers rather
    than a hardcoded ``{"core": N}`` (issue athenaeum#1779 added a second
    tier, ``long``, among the hand-authored core pages) -- a literal count
    here would need to move every time a core fixture's tier composition
    changes, which is not what this test exists to catch. What it exists to
    catch is any generated (distractor/ballast) tier appearing at all.
    """
    corpus = build_corpus(scale="core")
    assert corpus.tier_counts() == Counter(page.tier for page in load_core_pages())


@pytest.mark.parametrize("scale", ["small", "medium", "medium_verydense"])
def test_scales_reach_their_page_floor(scale: str) -> None:
    """``total_pages`` is a floor ballast fills to, never a cap.

    Core and distractor pages are the measurement; trimming either to hit a
    page count would discard ground truth or retrieval pressure to satisfy
    padding. The floor must still be MET, or the size axis stops separating
    its points.
    """
    corpus = build_corpus(scale=scale)
    floor = SCALES[scale].total_pages
    assert len(corpus.pages) >= floor


def test_page_floors_leave_room_for_ballast() -> None:
    """Each size-axis point must actually produce ballast.

    A floor set below core+distractors silently yields zero ballast, and that
    scale stops being a distinct point on the size axis while still appearing
    in the grid as though it were one.
    """
    for scale in ("small", "medium", "large"):
        counts = build_corpus(scale=scale).tier_counts()
        assert counts.get("ballast", 0) > 0, f"{scale}: floor too low for ballast"


def test_size_and_confusability_are_independent_axes() -> None:
    """The design's load-bearing property.

    ``medium`` and ``medium_dense`` hold page count constant while varying
    near-miss density. If these two axes moved together, a recall drop could
    not be attributed to either, and the two causes have different fixes (a
    better index vs better disambiguation).
    """
    sparse = build_corpus(scale="medium")
    dense = build_corpus(scale="medium_dense")

    assert len(sparse.pages) == len(dense.pages), "size axis must not move"
    assert dense.tier_counts()["distractor"] > sparse.tier_counts()["distractor"] * 3, (
        "confusability axis must move"
    )


def test_distractors_actually_reach_the_top_k() -> None:
    """The confusability axis must move something. This is the load-bearing one.

    Vocabulary overlap is NOT the property that matters -- the first version of
    this suite asserted only that some distractor contained some probe term,
    which passed while distractors occupied 0 of 95 top-5 slots. The axis was
    inert and the test could not see it.

    What matters is rank competition: if near-misses never surface, raising
    their density changes nothing, and Workstream G's scale-dependence result
    -- read directly off this axis -- would be a measurement of noise.
    """
    import tempfile

    from athenaeum.mcp_server import recall_search
    from tests.evals.metrics import uids_from_recall_output

    corpus = build_corpus(scale="medium_verydense")
    root = Path(tempfile.mkdtemp())
    corpus.materialize(root)
    tier_of = {page.uid: page.tier for page in corpus.pages}

    slots = distractor_slots = 0
    for probe in corpus.probes:
        output = recall_search(root / "wiki", probe.query, top_k=5)
        hits = uids_from_recall_output(output)[:5]
        slots += len(hits)
        distractor_slots += sum(1 for uid in hits if tier_of.get(uid) == "distractor")

    assert slots, "no results at all -- the probe harness is broken, not the corpus"
    share = distractor_slots / slots
    assert share >= 0.10, (
        f"distractors took {distractor_slots}/{slots} top-5 slots "
        f"({share:.0%}); below ~10% the confusability axis cannot move a "
        "result and density is a knob attached to nothing"
    )


def test_distractors_share_probe_vocabulary() -> None:
    """Necessary-but-insufficient companion to the rank test above.

    Kept because it localizes a failure: if rank competition disappears, this
    says whether the cause was vocabulary (a template regression) or ranking.
    """
    corpus = build_corpus(scale="small")
    by_probe: dict[str, list[str]] = {}
    for page in corpus.pages:
        if page.tier == "distractor":
            by_probe.setdefault(page.uid.rsplit("-", 1)[0], []).append(
                (page.name + " " + page.body).lower()
            )

    for probe in corpus.probes:
        texts = by_probe.get(f"dis-{probe.id}", [])
        assert texts, f"probe {probe.id!r} generated no distractors"
        terms = [t.lower() for t in probe.distractor_terms] or [probe.query.lower()]
        assert any(any(term in text for term in terms) for text in texts), (
            f"probe {probe.id!r}: distractors share none of its vocabulary"
        )


def test_ballast_does_not_compete_with_probes() -> None:
    """The inverse guard: ballast must measure SIZE only.

    If ballast shared probe vocabulary it would be distractor mass under
    another name, and the two axes would be confounded from the start.
    """
    corpus = build_corpus(scale="medium")
    ballast = [p for p in corpus.pages if p.tier == "ballast"]
    assert ballast, "medium scale should produce ballast"

    distinctive = {
        term.lower() for probe in corpus.probes for term in probe.distractor_terms if len(term) > 6
    }
    for page in ballast[:400]:
        text = (page.name + " " + page.body).lower()
        overlap = {term for term in distinctive if term in text}
        assert not overlap, (
            f"ballast page {page.uid} shares probe vocabulary {overlap} -- "
            "that makes it a distractor and confounds the two axes"
        )


def test_probe_taxonomy_is_complete() -> None:
    """Every class must be populated, including the two that are usually missed.

    Abstention and disambiguation are the classes a retrieval suite most often
    lacks, and they are where a push sidecar does its real damage: confidently
    surfacing a wrong page beats surfacing nothing only if the page is right.
    """
    classes = {p.probe_class for p in load_probes()}
    assert {
        "single_hop",
        "multi_hop",
        "temporal",
        "disambiguation",
        "abstention",
        "distractor_robustness",
        "follow_through",
    } <= classes


def test_relationship_probe_subset_has_all_four_classes_at_core() -> None:
    """Issue athenaeum#1744: the go/no-go rule's condition 1
    (``docs/design/native-memory-baseline.md`` §7) is evaluated over
    ``_relationship_probe_ids`` -- ``single_hop``/``multi_hop``/
    ``disambiguation``/``temporal`` probes whose ``expected_uids`` resolve
    to a ``person`` or ``company`` page. Quine's mutation review of PR
    athenaeum#1740 found that dropping ``temporal`` from that class list is
    an EQUIVALENT mutant at ``core``: no ``temporal`` probe's expected
    pages were person/company, so removing the class from the filter
    changed nothing. Asserting every one of the four classes is
    represented in the subset -- not merely that the subset is non-empty
    -- is what makes that mutant detectable again: dropping any single
    class from ``_relationship_probe_ids``'s class filter must fail this
    test.
    """
    ids = _relationship_probe_ids({"core"})
    classes_in_subset = {probe.probe_class for probe in load_probes() if probe.id in ids}
    assert {"single_hop", "multi_hop", "disambiguation", "temporal"} <= classes_in_subset


def test_frontmatter_scalars_survive_yaml_round_trip(tmp_path: Path) -> None:
    """Every rendered tag/alias/name must load back as a STRING.

    YAML 1.1 reads an unquoted ``9:00`` as the integer 540. A distractor
    carrying a time-shaped tag therefore loaded a non-string and crashed
    ``recall_search`` outright -- an eval that dies in the parser measures
    nothing, and the failure looks like a backend fault rather than a fixture
    one. Probe terms legitimately include times, so this must stay pinned.
    """
    import yaml

    corpus = build_corpus(scale="medium_verydense")
    wiki = corpus.materialize(tmp_path)

    checked = 0
    for path in wiki.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        front = yaml.safe_load(text[3 : text.find("\n---", 3)])
        for field in ("tags", "aliases"):
            for value in front.get(field) or ():
                assert isinstance(value, str), (
                    f"{path.name}: {field} entry {value!r} loaded as "
                    f"{type(value).__name__}, not str"
                )
                checked += 1
        assert isinstance(front["name"], str)
    assert checked, "no tags/aliases were actually checked"


def test_materialize_writes_a_readable_wiki_tree(tmp_path: Path) -> None:
    corpus = build_corpus(scale="core")
    wiki = corpus.materialize(tmp_path)
    written = list(wiki.glob("*.md"))
    assert len(written) == len(corpus.pages)

    sample = (wiki / "person-rowan-wrenfield.md").read_text(encoding="utf-8")
    assert sample.startswith("---\n")
    assert "uid: person-rowan-wrenfield" in sample
    assert "type: person" in sample
