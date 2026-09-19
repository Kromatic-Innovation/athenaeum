# SPDX-License-Identifier: Apache-2.0
"""Offline unit tests for the person-hint grader (issue athenaeum#1867).

Deliberately UNMARKED, so it runs in the default CI selection
(``-m 'not eval and not embedding and not rollout'``) — the same split
``tests/evals/test_attachment_grading.py`` draws. Nothing here calls a model,
touches the network, or runs the librarian: it exercises
``tests/evals/person_hint.py`` against constructed before/after bodies.

That split is what makes the grader falsifiable. The metered layer's product
is a score, and a score cannot tell you whether the thing producing it can
distinguish the outcomes it claims to. These tests can, and the one that
matters most is :func:`test_a_constructed_shipped_path_bullet_scores_as_a_failure`:
the shipped tier-0 bullet is built here from
``intake.attribute_person_observation``'s own shape, so a grader that stopped
recognising it would fail HERE, in ordinary CI, rather than silently start
passing the layer it exists to fail.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from tests.evals.harness import EVAL_DATA_ROOT
from tests.evals.person_hint import (
    ALL_OUTCOMES,
    GRADED_OUTCOMES,
    OUTCOME_CITATION_ONLY,
    OUTCOME_FOOTNOTED_CLAIM,
    OUTCOME_NOTES_BULLET,
    OUTCOME_UNCHANGED,
    OUTCOME_UNCITED_CHANGE,
    classify_person_outcome,
    deterministic_matched,
    duplicated_sentences,
    non_person_pointer_uids,
    read_page_bodies,
    read_page_types,
    score_case,
    tier0_person_matches,
    validate_case_spec,
)

DATA_ROOT = EVAL_DATA_ROOT / "person_hint"
RAW_REF = "sessions/20260701T120000Z-a77ac401.md"

BASE_BODY = """# Thessa Oakmoor

Thessa Oakmoor supervises the kiln floor at the Draymouth works. She runs the
shutdown handover and signs the floor's daily firing log.
"""


def shipped_notes_bullet(excerpt: str, ref: str = RAW_REF, on: str = BASE_BODY) -> str:
    """Build the bullet ``intake.attribute_person_observation`` writes.

    Reproduces that function's shape exactly — ``- YYYY-MM-DD: <excerpt>
    (source: <ref>)`` prepended immediately under ``## Notes``, the heading
    appended at the end of the body when the page has none. Constructed here
    rather than imported so this test pins the SHAPE the grader must
    recognise; if the librarian's shape changes, the layer's expected-RED
    reading changes with it and that should be a visible edit, not a silent
    one.
    """
    bullet = f"- {date.today().isoformat()}: {excerpt} (source: {ref})"
    heading = "## Notes"
    if heading in on:
        return on.replace(heading, f"{heading}\n\n{bullet}", 1)
    return on.rstrip("\n") + f"\n\n{heading}\n\n{bullet}\n"


# ---------------------------------------------------------------------------
# AC1 — the four outcomes
# ---------------------------------------------------------------------------


def test_an_untouched_page_is_unchanged() -> None:
    assert classify_person_outcome(BASE_BODY, BASE_BODY, RAW_REF) == OUTCOME_UNCHANGED


def test_a_trailing_newline_alone_is_not_a_change() -> None:
    """A re-render that only moved trailing whitespace changed no claim."""
    assert classify_person_outcome(BASE_BODY, BASE_BODY + "\n\n", RAW_REF) == OUTCOME_UNCHANGED


def test_a_constructed_shipped_path_bullet_scores_as_a_failure() -> None:
    """AC1's named case: the shipped tier-0 bullet is a ``notes_bullet``.

    This is the anti-vacuity gate. On the shipped path the person page DOES
    change, so a grader that merely detected change would pass the layer.
    """
    after = shipped_notes_bullet("Thessa Oakmoor sat in for the floor handover section.")
    assert classify_person_outcome(BASE_BODY, after, RAW_REF) == OUTCOME_NOTES_BULLET


def test_a_multiline_shipped_bullet_is_still_recognised() -> None:
    """The excerpt is a bounded slice of the raw body and routinely wraps.

    Measured, not hypothetical: the shipped path pastes the raw's own line
    breaks, so the closing ``(source: ...)`` usually lands on a LATER line
    than the date. A single-line pattern would miss every real bullet and
    hand this layer a silent pass.
    """
    excerpt = (
        "The first-kiln retrospective ran ninety minutes.\n"
        "Thessa Oakmoor sat in\nand had nothing to add."
    )
    after = shipped_notes_bullet(excerpt)
    assert classify_person_outcome(BASE_BODY, after, RAW_REF) == OUTCOME_NOTES_BULLET


def test_a_bullet_onto_a_page_with_no_notes_heading_is_still_recognised() -> None:
    body = "# Corwin Vantry\n\nCorwin Vantry buys refractory for the practice.\n"
    after = shipped_notes_bullet("Corwin Vantry attended the review.", on=body)
    assert "## Notes" in after
    assert classify_person_outcome(body, after, RAW_REF) == OUTCOME_NOTES_BULLET


def test_a_single_appended_footnote_definition_is_citation_only() -> None:
    """The shape ``tiers._append_source_citation`` produces."""
    after = BASE_BODY.rstrip("\n") + f"\n\n[^1]: {RAW_REF}"
    assert classify_person_outcome(BASE_BODY, after, RAW_REF) == OUTCOME_CITATION_ONLY


def test_citation_only_numbers_after_an_existing_footnote() -> None:
    before = BASE_BODY.rstrip("\n") + "\n\n[^1]: sessions/2026-02-11.md"
    after = before + f"\n\n[^2]: {RAW_REF}"
    assert classify_person_outcome(before, after, RAW_REF) == OUTCOME_CITATION_ONLY


def test_a_new_claim_with_a_marker_and_a_citation_is_a_footnoted_claim() -> None:
    after = (
        BASE_BODY.rstrip("\n") + "\n\nShe is the works' first certified operator on the new firing"
        " controller.[^1]\n\n" + f"[^1]: {RAW_REF}"
    )
    assert classify_person_outcome(BASE_BODY, after, RAW_REF) == OUTCOME_FOOTNOTED_CLAIM


def test_a_claim_citing_some_other_source_is_not_a_footnoted_claim() -> None:
    """``footnoted_claim`` says where THIS file's claim came from.

    A page that grew a claim and cited a different source did not record the
    provenance of the file under grading, and crediting it would make the ref
    check decorative.
    """
    after = (
        BASE_BODY.rstrip("\n")
        + "\n\nShe now manages the programme.[^1]\n\n[^1]: sessions/2026-02-11.md"
    )
    assert classify_person_outcome(BASE_BODY, after, RAW_REF) == OUTCOME_UNCITED_CHANGE


def test_a_rewritten_body_with_no_citation_is_the_residual_not_a_claim() -> None:
    """The four shapes are not exhaustive, and the residual must stay visible.

    Folding an UNCITED body edit into ``footnoted_claim`` would score an
    unsourced claim as a correctly-sourced one — the vacuity the four shapes
    exist to prevent, reintroduced at the bottom of the classifier.
    """
    after = BASE_BODY.rstrip("\n") + "\n\nShe now manages the relining programme.\n"
    assert classify_person_outcome(BASE_BODY, after, RAW_REF) == OUTCOME_UNCITED_CHANGE


def test_every_outcome_is_one_of_the_documented_labels() -> None:
    """The classifier is total and single-valued over :data:`ALL_OUTCOMES`."""
    afters = [
        BASE_BODY,
        shipped_notes_bullet("x" * 40),
        BASE_BODY.rstrip("\n") + f"\n\n[^1]: {RAW_REF}",
        BASE_BODY.rstrip("\n") + f"\n\nA claim.[^1]\n\n[^1]: {RAW_REF}",
        BASE_BODY.rstrip("\n") + "\n\nAn uncited claim.\n",
        "",
    ]
    for after in afters:
        assert classify_person_outcome(BASE_BODY, after, RAW_REF) in ALL_OUTCOMES


def test_a_bullet_alongside_a_citation_still_reports_the_bullet() -> None:
    """Most-damning-wins, mirroring ``TierAttribution``'s most-expensive-wins.

    A run that pasted a raw excerpt onto a person page AND cited it has still
    pasted a raw excerpt onto a person page.
    """
    after = shipped_notes_bullet("An excerpt.").rstrip("\n") + f"\n\n[^1]: {RAW_REF}"
    assert classify_person_outcome(BASE_BODY, after, RAW_REF) == OUTCOME_NOTES_BULLET


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _case(**expected: object) -> dict[str, object]:
    return {"id": "t", "expected": expected}


def test_notes_bullet_fails_even_when_the_case_never_named_the_page() -> None:
    """AC1: ``notes_bullet`` is a failure in EVERY case, listed or not.

    An expectation set that merely omitted a page must not let the shipped
    shape through unremarked — silence is not permission.
    """
    passed, detail = score_case(
        _case(person_outcomes={"ph-person-selmire": OUTCOME_UNCHANGED}),
        _EMPTY_DELTA,
        {"ph-person-selmire": OUTCOME_UNCHANGED, "ph-person-vantry": OUTCOME_NOTES_BULLET},
    )
    assert not passed
    assert "ph-person-vantry" in detail


def test_a_case_passes_when_every_expectation_holds() -> None:
    passed, detail = score_case(
        _case(person_outcomes={"a": OUTCOME_UNCHANGED}, non_person_pointer_uids=["p"]),
        _EMPTY_DELTA,
        {"a": OUTCOME_UNCHANGED},
        pointer_uids=frozenset({"p"}),
    )
    assert passed, detail


def test_an_alternative_outcome_list_accepts_either_shape() -> None:
    """Case E's expectation: ``citation_only`` OR ``footnoted_claim``."""
    for got in (OUTCOME_CITATION_ONLY, OUTCOME_FOOTNOTED_CLAIM):
        passed, detail = score_case(
            _case(person_outcomes={"a": [OUTCOME_CITATION_ONLY, OUTCOME_FOOTNOTED_CLAIM]}),
            _EMPTY_DELTA,
            {"a": got},
        )
        assert passed, detail


def test_the_compile_requirement_fails_a_person_only_run() -> None:
    """AC3: a run in which the only change is on person pages fails."""
    passed, detail = score_case(
        _case(person_outcomes={"a": OUTCOME_UNCHANGED}, require_non_person_pointer=True),
        _EMPTY_DELTA,
        {"a": OUTCOME_UNCHANGED},
        pointer_uids=frozenset(),
    )
    assert not passed
    assert "only change is on person pages" in detail


def test_a_named_pointer_target_must_be_the_page_that_moved() -> None:
    """Naming the page is stronger than "something non-person moved"."""
    passed, detail = score_case(
        _case(person_outcomes={"a": OUTCOME_UNCHANGED}, non_person_pointer_uids=["wanted"]),
        _EMPTY_DELTA,
        {"a": OUTCOME_UNCHANGED},
        pointer_uids=frozenset({"some-other-page"}),
    )
    assert not passed
    assert "wanted" in detail


def test_the_residual_fails_wherever_it_appears() -> None:
    passed, detail = score_case(
        _case(person_outcomes={"a": OUTCOME_FOOTNOTED_CLAIM}),
        _EMPTY_DELTA,
        {"a": OUTCOME_UNCITED_CHANGE},
    )
    assert not passed
    assert "no citation" in detail


def test_a_missing_page_is_reported_rather_than_skipped() -> None:
    passed, detail = score_case(
        _case(person_outcomes={"absent": OUTCOME_UNCHANGED}), _EMPTY_DELTA, {}
    )
    assert not passed
    assert "no page observed" in detail


def test_a_duplicated_sentence_fails_the_restated_fact_case() -> None:
    passed, detail = score_case(
        _case(
            person_outcomes={"a": [OUTCOME_CITATION_ONLY, OUTCOME_FOOTNOTED_CLAIM]},
            forbid_duplicate_sentence=["a"],
        ),
        _EMPTY_DELTA,
        {"a": OUTCOME_CITATION_ONLY},
        duplicated={"a": ["Thessa Oakmoor supervises the kiln floor at the Draymouth works."]},
    )
    assert not passed
    assert "duplicated" in detail


# ---------------------------------------------------------------------------
# Duplicated prose
# ---------------------------------------------------------------------------


def test_duplicated_sentences_finds_a_restated_fact() -> None:
    """The failure case E exists to catch, as the shipped path produces it.

    Measured, not invented: the tier-0 bullet pastes the raw file's own
    sentence onto a page that already carries it verbatim, so the page ends
    up saying the same thing twice.
    """
    sentence = "Thessa Oakmoor supervises the kiln floor at the Draymouth works."
    assert duplicated_sentences(f"# T\n\n{sentence}\n\n## Notes\n\n{sentence}\n") == [sentence]


def test_duplicated_sentences_does_not_catch_a_REWORDED_restatement() -> None:
    """A stated limit, pinned so it is not mistaken for coverage.

    This check is literal: it finds the same sentence twice, which is the
    shape a paste produces. A model that restated the fact in ITS OWN words
    would pass here, and case E's outcome expectation
    (``citation_only``/``footnoted_claim``) is what carries that half — a
    genuine rewrite is not ``citation_only``, so the two checks together are
    what the case rests on, never this one alone.
    """
    assert (
        duplicated_sentences(
            "Thessa Oakmoor supervises the kiln floor at the Draymouth works.\n\n"
            "The kiln floor at the Draymouth works is supervised by Thessa Oakmoor.\n"
        )
        == []
    )


def test_duplicated_sentences_ignores_a_repeated_footnote_marker() -> None:
    """``X.`` and ``X.[^2]`` are the same sentence.

    Appending a marker to a restated sentence is precisely the near-miss the
    check must still catch.
    """
    sentence = "Thessa Oakmoor supervises the kiln floor at the Draymouth works."
    assert duplicated_sentences(f"{sentence}\n\n{sentence}[^2]\n") == [sentence]


def test_duplicated_sentences_ignores_short_repeats() -> None:
    """Headings and list markers repeat harmlessly; facts do not."""
    assert duplicated_sentences("## Notes\n\ntext\n\n## Notes\n\ntext\n") == []


# ---------------------------------------------------------------------------
# Tier attribution of the tier-0 consult
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, matched: int = 0, updated: list[str] | None = None) -> None:
        self.matched = matched
        self.updated = updated or []


def test_a_zero_call_update_is_attributed_to_the_deterministic_tier() -> None:
    """``ProcessingResult.matched`` is 0 on the tier-0 early return.

    ``attribute_tier`` would report ``decided_by="none"`` for a decision that
    was very much made — deterministically, at tier 0. AC4 asks which tier
    decided; "none" would be a wrong answer, not a missing one.
    """
    assert tier0_person_matches(_Result(updated=["a", "b"]), []) == 2
    assert deterministic_matched(_Result(updated=["a", "b"]), []) == 2


def test_any_model_call_hands_attribution_back_to_the_ordinary_rule() -> None:
    """OBSERVED, never declared: one call means some later tier ran."""
    assert tier0_person_matches(_Result(updated=["a"]), ["claude-haiku-4-5"]) == 0
    assert deterministic_matched(_Result(matched=3, updated=["a"]), ["claude-haiku-4-5"]) == 3


# ---------------------------------------------------------------------------
# AC5 — the golden set's own shape
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def spec() -> dict:
    return dict(yaml.safe_load((DATA_ROOT / "cases.yaml").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def case_wiki_uids() -> list[str]:
    return sorted(read_page_bodies(DATA_ROOT / "wiki"))


def test_the_golden_set_has_the_five_cases_the_issue_names(spec: dict) -> None:
    assert len(spec["cases"]) >= 5
    assert {str(c["outcome_class"]) for c in spec["cases"]} == {
        "passing_mention",
        "asserted_claim",
        "mixed_fanout",
        "name_collision",
        "restated_fact",
    }


def test_every_expected_uid_exists_and_every_case_names_an_outcome(
    spec: dict, case_wiki_uids: list[str]
) -> None:
    assert validate_case_spec(spec["cases"], case_wiki_uids) == []


def test_validate_case_spec_catches_a_uid_that_is_not_in_the_wiki() -> None:
    """A guard that never fires is not a guard."""
    problems = validate_case_spec(
        [{"id": "x", "expected": {"person_outcomes": {"ph-person-nobody": OUTCOME_UNCHANGED}}}],
        ["ph-person-oakmoor"],
    )
    assert any("ph-person-nobody" in p for p in problems)


def test_validate_case_spec_catches_a_case_with_no_expected_outcome() -> None:
    problems = validate_case_spec([{"id": "x", "expected": {}}], ["a"])
    assert any("names no expected person outcome" in p for p in problems)


def test_validate_case_spec_rejects_the_residual_as_an_expectation() -> None:
    """No case may EXPECT ``uncited_change``; it is a failure, not a target."""
    problems = validate_case_spec(
        [{"id": "x", "expected": {"person_outcomes": {"a": OUTCOME_UNCITED_CHANGE}}}], ["a"]
    )
    assert any(OUTCOME_UNCITED_CHANGE in p for p in problems)
    assert OUTCOME_UNCITED_CHANGE not in GRADED_OUTCOMES


def test_every_case_names_a_raw_file_that_exists(spec: dict) -> None:
    for case in spec["cases"]:
        assert (DATA_ROOT / "raw" / str(case["raw"])).is_file(), case["id"]


def test_the_case_wiki_carries_a_description_on_every_person_page() -> None:
    """Case D contradicts the known page's ``description:``, so it must exist."""
    types = read_page_types(DATA_ROOT / "wiki")
    from athenaeum.models import parse_frontmatter

    persons = [uid for uid, ptype in types.items() if ptype == "person"]
    assert persons
    for path in sorted((DATA_ROOT / "wiki").glob("*.md")):
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if meta.get("type") == "person":
            assert str(meta.get("description") or "").strip(), path.name


def test_the_case_wiki_is_below_the_relatedness_writer_index_floor() -> None:
    """Under 50 pages the athenaeum#1576 writer cannot fire.

    That is why this layer hand-authors a small tree instead of overlaying the
    generated ``core`` corpus: there is no ``term-overlap`` edge to partition
    out, so no incidental edge can be mistaken for a compile.
    """
    assert len(list((DATA_ROOT / "wiki").glob("*.md"))) < 50


# ---------------------------------------------------------------------------
# Non-person pointer detection
# ---------------------------------------------------------------------------


class _Delta:
    def __init__(self, minted=(), touched=(), gained=None) -> None:
        self.minted = frozenset(minted)
        self.touched_uids = frozenset(touched)
        self.gained_source_refs = gained or {}


_EMPTY_DELTA = _Delta()


def test_a_gained_source_ref_on_a_non_person_page_is_a_pointer() -> None:
    hits = non_person_pointer_uids(
        delta=_Delta(
            touched=["ph-project-relining"], gained={"ph-project-relining": frozenset({RAW_REF})}
        ),
        before_bodies={"ph-project-relining": "x"},
        after_bodies={"ph-project-relining": "x"},
        page_types={"ph-project-relining": "project"},
        raw_ref=RAW_REF,
    )
    assert hits == frozenset({"ph-project-relining"})


def test_a_body_footnote_naming_the_ref_is_also_a_pointer() -> None:
    """Frontmatter alone would miss a correctly-cited body claim."""
    hits = non_person_pointer_uids(
        delta=_Delta(touched=["p"]),
        before_bodies={"p": "x"},
        after_bodies={"p": f"x\n\n[^1]: {RAW_REF}"},
        page_types={"p": "project"},
        raw_ref=RAW_REF,
    )
    assert hits == frozenset({"p"})


def test_a_person_page_is_never_counted_as_the_compile_target() -> None:
    """AC3's whole point: person pages cannot satisfy the compile requirement."""
    hits = non_person_pointer_uids(
        delta=_Delta(
            touched=["ph-person-oakmoor"], gained={"ph-person-oakmoor": frozenset({RAW_REF})}
        ),
        before_bodies={"ph-person-oakmoor": "x"},
        after_bodies={"ph-person-oakmoor": "y"},
        page_types={"ph-person-oakmoor": "person"},
        raw_ref=RAW_REF,
    )
    assert hits == frozenset()


def test_reading_bodies_and_types_round_trips_the_case_wiki(
    case_wiki_uids: list[str],
) -> None:
    types = read_page_types(DATA_ROOT / "wiki")
    assert set(types) == set(case_wiki_uids)
    assert sorted(t for t in types.values()) == [
        "company",
        "person",
        "person",
        "person",
        "person",
        "project",
    ]


def test_reading_an_absent_tree_yields_nothing(tmp_path: Path) -> None:
    assert read_page_bodies(tmp_path / "nope") == {}
    assert read_page_types(tmp_path / "nope") == {}
