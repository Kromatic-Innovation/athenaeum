# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1558: pin the underdetermined layer's sentence-split fix.

``tests/evals/test_underdetermined_eval.py`` is a Live-API eval (module-level
``pytestmark = pytest.mark.eval``, deselected from the default selection and
run only by the Evals workflow), so its SCORING logic is otherwise exercised
only against a real model on ``main``. This module is deliberately unmarked —
it imports the eval module and calls the pure predicates directly, so the
sentence splitter is covered by ordinary CI with zero network, same pattern
as ``tests/test_underdetermined_scoring.py``.

Why it exists: since athenaeum#1730 the librarian writes inline footnote
markers immediately after the sentence terminator (``...approved all three
layouts.[^1] Separately, ...``). ``_SENTENCE_SPLIT``'s lookbehind
(``(?<=[.!?])\\s+``) requires whitespace immediately after ``.``/``!``/``?``,
so ``.[^1] `` never matched and two sentences silently scored as one. The
case ``adjacent_action_not_folded_into_role``
(tests/evals/data/underdetermined/cases.yaml) then failed a CORRECT model
answer: "Priya Anand" (sentence 1) and "vendor"/"proofing" (sentence 2, a
different party's action) were scored as co-occurring in one sentence purely
because the footnote marker blocked the split. ``RECORDED_BODY`` below is
the verbatim body recorded for that case
(recorded-fixtures/underdetermined/adjacent_action_not_folded_into_role.json,
2026-09-15) — pinned here so a future edit that re-breaks the split, or
over-corrects it into merging genuinely-folded sentences, fails here rather
than on a live run.
"""

from __future__ import annotations

from tests.evals.test_underdetermined_eval import _score_case, _sentences

# Verbatim ``response_text`` recorded for
# ``adjacent_action_not_folded_into_role`` (Evals run of 2026-09-15). The
# footnote markers after "layouts." and at the very end are the ones that
# used to defeat the sentence split.
RECORDED_BODY = """Description: Windrow Dashboard Rollout is an active client project \
involving dashboard chart layout design and an upcoming user training session.

# Windrow Dashboard Rollout

A client project for Windrow involving the design and rollout of a dashboard. During a \
design review, Priya Anand presented revised chart layouts to client stakeholders, who \
approved all three layouts.[^1] Separately, the project team coordinated with a print \
vendor to proof an updated user guide ahead of a training session planned for the \
following week.[^1]

## Open Questions
- [ ] What is the full scope and timeline of the dashboard rollout?
- [ ] Who else, besides Priya Anand, is on the project team?
- [ ] What is the client organization's relationship/contract structure with the team \
(e.g., vendor, internal client)?

[^1]: sessions/2026-04-15.md
"""

CASE = {
    "id": "adjacent_action_not_folded_into_role",
    "expected": {
        "must_include_substrings": ["Priya Anand", "chart layouts"],
        # Action 2 (the vendor/proofing follow-up) belongs to the note's own
        # first-person author, not Priya Anand — it must not read as
        # something Priya did.
        "forbidden_co_occurrence": [
            ["Priya Anand", "proofing"],
            ["Priya Anand", "vendor"],
        ],
    },
}


class TestSentenceSplitAcrossFootnoteMarker:
    def test_footnote_marker_after_period_still_splits(self) -> None:
        sentences = _sentences(RECORDED_BODY)
        joined_layouts_sentence = next(s for s in sentences if "Priya Anand" in s)
        assert "vendor" not in joined_layouts_sentence
        assert "proofing" not in joined_layouts_sentence and "proof" not in joined_layouts_sentence

    def test_recorded_correct_answer_now_passes(self) -> None:
        passed, detail = _score_case(CASE, RECORDED_BODY)
        assert passed, detail

    def test_multiple_consecutive_footnote_markers_still_split(self) -> None:
        body = RECORDED_BODY.replace("layouts.[^1]", "layouts.[^1][^2]", 1)
        passed, detail = _score_case(CASE, body)
        assert passed, detail

    def test_genuinely_folded_sentence_still_fails(self) -> None:
        # A real single-sentence fold (no footnote marker involved) must
        # still be caught — the fix must not overcorrect into merging real
        # sentence boundaries away.
        folded = (
            "Priya Anand presented the revised chart layouts and then "
            "followed up with the print vendor about proofing the guide.[^1]"
        )
        passed, detail = _score_case(CASE, folded)
        assert not passed
        assert "co-occur" in detail

    def test_footnote_definition_line_does_not_create_false_co_occurrence(self) -> None:
        # The definition line's own marker must not bridge it into the
        # preceding paragraph's sentence.
        body = (
            "Priya Anand presented the chart layouts to stakeholders.[^1]\n\n"
            "[^1]: sessions/2026-04-15.md — filed by our print vendor.\n"
        )
        case = {
            "id": "definition-line-guard",
            "expected": {
                "forbidden_co_occurrence": [["Priya Anand", "vendor"]],
            },
        }
        passed, detail = _score_case(case, body)
        assert passed, detail
