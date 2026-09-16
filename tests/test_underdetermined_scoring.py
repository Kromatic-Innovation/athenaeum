# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1668: pin the underdetermined layer's affiliation discriminator.

``tests/evals/test_underdetermined_eval.py`` is a Live-API eval (module-level
``pytestmark = pytest.mark.eval``, deselected from the default selection and
run only by the Evals workflow), so its SCORING logic is otherwise exercised
only against a real model on ``main``. This module is deliberately unmarked —
it imports the eval module and calls the pure predicate directly, so the
discriminator is covered by ordinary CI with zero network.

Why it exists: ``silent_affiliation_not_inferred`` scored "no affiliation
invented" as a blanket ban on the authoring org's name appearing anywhere in
the composed body. That predicate cannot separate the wrong inference from
correct provenance, and the case failed 18 of 18 Evals runs between the day
the layer landed (2026-09-10) and 2026-09-15 — it never once passed. The
``recorded_body`` case below is the verbatim body from one of those runs: the
model did exactly what the case asks (attributed the action, declined to
state the affiliation, raised it as an open question) and still scored as a
miss. The table pins both directions so a future edit that re-widens the
predicate into a co-mention ban, or narrows it past the real wrong inference,
fails here rather than on a live run.
"""

from __future__ import annotations

import pytest

from tests.evals.test_underdetermined_eval import _asserts_affiliation, _score_case

PERSON = "Devon Achebe"
ORG = "Larkspur"

# The verbatim composed body recorded by the Evals run of 2026-09-15 for
# ``silent_affiliation_not_inferred`` (recorded-fixtures artifact). It names
# the org only as the source document's own provenance, and raises Devon's
# affiliation as an open question — the target behaviour.
RECORDED_BODY = """Description: A retail buildout project at the Cedarline space where a \
pre-buildout walkthrough flagged a load-bearing wall issue requiring engineer sign-off.

# Cedarline Retail Buildout

A retail construction project involving buildout work at the Cedarline space. During a \
pre-buildout walkthrough conducted on 2026-04-02 as part of a Larkspur Site Audits log, \
Devon Achebe flagged a load-bearing wall near the east entrance that requires an \
engineer's sign-off before the framing crew can proceed.[^1]

## Open Questions
- [ ] What is Devon Achebe's role or affiliation? (not recorded in the source log)
- [ ] Has the required engineer's sign-off on the load-bearing wall been obtained?
- [ ] What is the current overall status/timeline of the buildout project?

[^1]: sessions/2026-04-02.md
"""

ASSERTS_AFFILIATION = [
    ("parenthetical", "Devon Achebe (Larkspur Site Audits) flagged a wall."),
    ("parenthetical_comma", "Devon Achebe, (Larkspur Site Audits), flagged a wall."),
    ("prepositional_of", "Devon Achebe of Larkspur Site Audits flagged a wall."),
    ("prepositional_at", "The walkthrough was led by Devon Achebe at Larkspur."),
    (
        "prepositional_role",
        "Devon Achebe, a senior consultant at Larkspur Site Audits, flagged a wall.",
    ),
    ("bare_appositive", "Devon Achebe, Larkspur Site Audits, flagged a wall."),
    ("possessive_person", "Devon Achebe's Larkspur team flagged a wall."),
    ("possessive_org", "Larkspur Site Audits's Devon Achebe flagged a wall."),
    ("role_noun_apposition", "Larkspur Site Audits auditor Devon Achebe flagged a wall."),
    (
        "role_noun_apposition_modified",
        "Larkspur Site Audits senior consultant Devon Achebe flagged a wall.",
    ),
]

DOES_NOT_ASSERT_AFFILIATION = [
    ("recorded_body", RECORDED_BODY),
    (
        "provenance_clause",
        "During a walkthrough recorded as part of a Larkspur Site Audits log, "
        "Devon Achebe flagged a load-bearing wall.",
    ),
    (
        "provenance_sentence_before",
        "The source is a Larkspur Site Audits walkthrough log. "
        "Devon Achebe flagged a load-bearing wall.",
    ),
    (
        "provenance_sentence_after",
        "Devon Achebe flagged a load-bearing wall near the east entrance. "
        "The walkthrough was logged by Larkspur Site Audits.",
    ),
    (
        "affiliation_raised_as_open_question",
        "Devon Achebe flagged a load-bearing wall.\n\n## Open Questions\n"
        "- [ ] What is Devon Achebe's role or affiliation? The log was filed "
        "under Larkspur Site Audits but does not say.",
    ),
    ("person_only", "Devon Achebe flagged a load-bearing wall near the east entrance."),
    ("org_only", "The walkthrough log was filed by Larkspur Site Audits."),
]


class TestAssertsAffiliation:
    @pytest.mark.parametrize(
        "body", [b for _, b in ASSERTS_AFFILIATION], ids=[i for i, _ in ASSERTS_AFFILIATION]
    )
    def test_catches_the_wrong_inference(self, body: str) -> None:
        assert _asserts_affiliation(body, PERSON, ORG) is True

    @pytest.mark.parametrize(
        "body",
        [b for _, b in DOES_NOT_ASSERT_AFFILIATION],
        ids=[i for i, _ in DOES_NOT_ASSERT_AFFILIATION],
    )
    def test_allows_provenance_and_silence(self, body: str) -> None:
        assert _asserts_affiliation(body, PERSON, ORG) is False


class TestScoreCaseWiring:
    """``forbidden_affiliation`` must actually reach the scorer."""

    CASE = {
        "id": "silent_affiliation_not_inferred",
        "expected": {
            "must_include_substrings": ["Devon Achebe"],
            "forbidden_affiliation": [["Devon Achebe", "Larkspur"]],
        },
    }

    def test_recorded_body_now_passes(self) -> None:
        passed, detail = _score_case(self.CASE, RECORDED_BODY)
        assert passed, detail

    def test_invented_affiliation_still_fails(self) -> None:
        body = "Devon Achebe (Larkspur Site Audits) flagged a load-bearing wall."
        passed, detail = _score_case(self.CASE, body)
        assert not passed
        assert "affiliated with" in detail

    def test_missing_person_still_fails(self) -> None:
        passed, detail = _score_case(self.CASE, "Someone flagged a load-bearing wall.")
        assert not passed
        assert "missing expected substring" in detail
