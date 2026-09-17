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
from pathlib import Path

import pytest

from tests.evals.corpus import (
    SCALES,
    Page,
    Probe,
    RelatedEdge,
    build_corpus,
    load_core_pages,
    load_probes,
    validate_core,
)


def test_core_corpus_is_internally_consistent() -> None:
    """Every probe's ground truth and every page link must resolve.

    A dangling ``expected_uids`` reference does not fail loudly at use time --
    it silently scores as a retrieval MISS, which reads as a model regression.
    A corpus error must never be able to masquerade as an eval result.
    """
    problems = validate_core(load_core_pages(), load_probes())
    assert not problems, "corpus inconsistencies:\n  " + "\n  ".join(problems)


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
    *, second_hop_shares_query_term: bool, edge_between_expected_pages: bool
) -> tuple[list[Page], Probe]:
    """Build the minimal two-page fixture ``validate_core``'s
    ``follow_through`` checks operate on, with each half independently
    toggleable so the two checks can be tested in isolation."""
    edge_target = "page-b" if edge_between_expected_pages else "page-unrelated"
    second_hop_body = (
        "Alderquill covers galaxy formation in early cosmic history."
        if second_hop_shares_query_term
        else "Verdigrove is a quiet coastal harbour town."
    )
    pages = [
        Page(
            uid="page-a",
            type="note",
            name="Page A",
            body="TokenOne sits here.",
            tier="core",
            related=(RelatedEdge(uid=edge_target, role="related"),),
        ),
        Page(
            uid="page-b",
            type="note",
            name="Page B",
            body=f"TokenTwo sits here. {second_hop_body}",
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
    assert any("shares no content term" in p for p in problems), problems


def test_follow_through_rejects_no_edge_between_expected_pages() -> None:
    """AC (issue athenaeum#1737): the second-hop page must be reachable from
    another ``expected_uids`` page by a ``related``/``links`` edge; an edge
    to some other page in the corpus does not satisfy the assertion."""
    pages, probe = _two_page_follow_through(
        second_hop_shares_query_term=False, edge_between_expected_pages=False
    )
    problems = validate_core(pages, [probe])
    assert any("related/links edge" in p for p in problems), problems


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


def test_core_scale_generates_nothing() -> None:
    corpus = build_corpus(scale="core")
    assert corpus.tier_counts() == {"core": len(load_core_pages())}


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
