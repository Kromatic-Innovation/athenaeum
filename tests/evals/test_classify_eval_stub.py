# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the slack-widened classify gate (issue athenaeum#2231).

UNMARKED — no ``pytest.mark.eval`` — runs in the default suite, offline,
no credential, no network, against :class:`tests.conftest.FakeLLMClient`.
Exercises the real :func:`athenaeum.tiers.tier2_classify` and the real
scorer (:func:`tests.evals.test_classify_eval._score_case`), not a
re-implementation of either.

AC4 (athenaeum#2231): proves, by exhaustive enumeration matching the
floor-derivation comment above
:data:`tests.evals.test_classify_eval.CLASSIFY_FLOOR`, that:

- a regression confined to ONE always-passing case does NOT red
  ``CLASSIFY_FLOOR`` (0% power — the floor sits two notches below n=7), and
- a regression spanning TWO always-passing cases DOES red it in this
  script (where the permanent-miss and weak cases both also miss, their
  historical norm ~82.6% of the time).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from athenaeum.models import RawFile, TokenUsage
from tests.conftest import FakeLLMClient
from tests.evals.test_classify_eval import CLASSIFY_FLOOR, _load_cases, _score_case

# The five cases observed to pass every single time (four original +
# single_project_entity, added by athenaeum#2231 for real slack).
_ALWAYS_PASSING_IDS = frozenset(
    {
        "single_person_entity",
        "multiple_entities_project_and_person",
        "skip_already_matched_entity",
        "purely_procedural_no_entities",
        "single_project_entity",
    }
)
_PERMANENT_MISS_ID = "passing_mention_not_substantive"
_WEAK_ID = "reference_entity_office_policy"

# One (correct, wrong) JSON response pair per case id. "Wrong" mirrors each
# case's real observed failure mode (empty extraction when an entity is
# expected, or a spurious extraction when none is expected) rather than an
# arbitrary malformed payload.
_RESPONSES: dict[str, tuple[str, str]] = {
    "single_person_entity": (
        json.dumps(
            [
                {
                    "name": "Priya Shah",
                    "entity_type": "person",
                    "tags": ["active", "client"],
                    "access": "internal",
                    "observations": "Client lead on the Thornhollow retainer.",
                }
            ]
        ),
        "[]",
    ),
    "multiple_entities_project_and_person": (
        json.dumps(
            [
                {
                    "name": "Amir Khan",
                    "entity_type": "person",
                    "tags": ["active"],
                    "access": "internal",
                    "observations": "Leads the Atlas Retooling engagement.",
                },
                {
                    "name": "Atlas Retooling",
                    "entity_type": "project",
                    "tags": ["active", "client"],
                    "access": "internal",
                    "observations": "Full audit of the client's tooling stack.",
                },
            ]
        ),
        "[]",
    ),
    "skip_already_matched_entity": (
        "[]",
        json.dumps(
            [
                {
                    "name": "Priya Shah",
                    "entity_type": "person",
                    "tags": ["active"],
                    "access": "internal",
                    "observations": "re-extracted despite being already matched",
                }
            ]
        ),
    ),
    "purely_procedural_no_entities": (
        "[]",
        json.dumps(
            [
                {
                    "name": "CI Run Tracker",
                    "entity_type": "concept",
                    "tags": ["internal"],
                    "access": "internal",
                    "observations": "spuriously extracted from a procedural log",
                }
            ]
        ),
    ),
    "single_project_entity": (
        json.dumps(
            [
                {
                    "name": "Harrowdene Audit",
                    "entity_type": "project",
                    "tags": ["active", "client"],
                    "access": "internal",
                    "observations": "Workstream for Thornhollow Advisory.",
                }
            ]
        ),
        "[]",
    ),
    _PERMANENT_MISS_ID: (
        "[]",
        json.dumps(
            [
                {
                    "name": "Bob",
                    "entity_type": "person",
                    "tags": ["internal"],
                    "access": "internal",
                    "observations": "IT support, a passing placeholder mention",
                }
            ]
        ),
    ),
    _WEAK_ID: (
        json.dumps(
            [
                {
                    "name": "Expense Policy Update",
                    "entity_type": "reference",
                    "tags": ["internal"],
                    "access": "internal",
                    "observations": "Receipts now required for expenses over 25 GBP.",
                }
            ]
        ),
        "[]",
    ),
}


def _make_raw(case: dict[str, Any]) -> RawFile:
    return RawFile(
        path=Path(f"/tmp/eval-classify-stub/sessions/{case['id']}.md"),
        source="sessions",
        timestamp="20260301T120000Z",
        uuid8="ee110002",
        _content=str(case["content"]),
    )


def _run_case(case: dict[str, Any], *, use_correct: bool) -> bool:
    correct_text, wrong_text = _RESPONSES[case["id"]]
    client = FakeLLMClient(text=correct_text if use_correct else wrong_text)
    raw = _make_raw(case)
    from athenaeum.tiers import tier2_classify

    entities = tier2_classify(
        raw,
        list(case.get("matched_names") or []),
        list(case["valid_types"]),
        list(case["valid_tags"]),
        list(case["valid_access"]),
        client,
        usage=TokenUsage(),
    )
    passed, _detail = _score_case(case, entities)
    return passed


def test_score_case_catches_a_wrong_extraction_on_a_reliable_case() -> None:
    """Sanity: the scripted "correct"/"wrong" pair for a reliable case
    actually scores PASS/FAIL through the real tier2_classify parse path."""
    cases = {c["id"]: c for c in _load_cases()}
    case = cases["single_person_entity"]

    assert _run_case(case, use_correct=True) is True
    assert _run_case(case, use_correct=False) is False


def test_one_case_regression_never_reds_the_slack_widened_floor() -> None:
    """Exhaustive-enumeration claim from the floor-derivation comment: a
    single always-passing case regressing does NOT red CLASSIFY_FLOOR,
    independent of the weak case's outcome that run.

    Scripts: permanent miss wrong (its norm), ONE always-passing case
    wrong (the injected regression), everything else — including the weak
    case — wrong too (the weak case's historical norm, ~82.6% of runs).
    That is the LEAST favorable weak-case outcome for passing, and the
    floor still clears: 4/7 passed, at the floor exactly.
    """
    cases = {c["id"]: c for c in _load_cases()}
    regressed_id = "single_person_entity"

    passed_count = 0
    for case_id, case in cases.items():
        use_correct = case_id not in (_PERMANENT_MISS_ID, _WEAK_ID, regressed_id)
        if _run_case(case, use_correct=use_correct):
            passed_count += 1

    assert passed_count == 4
    assert passed_count >= CLASSIFY_FLOOR, (
        "documented false-green: a single-case regression clears the floor "
        "even when the weak case also misses"
    )


def test_two_case_regression_reds_the_slack_widened_floor() -> None:
    """A regression spanning TWO always-passing cases, with the weak case
    also missing (its historical norm), must red CLASSIFY_FLOOR.
    """
    cases = {c["id"]: c for c in _load_cases()}
    regressed = {"single_person_entity", "multiple_entities_project_and_person"}

    passed_count = 0
    for case_id, case in cases.items():
        use_correct = case_id not in (_PERMANENT_MISS_ID, _WEAK_ID) and case_id not in regressed
        if _run_case(case, use_correct=use_correct):
            passed_count += 1

    assert passed_count == 3
    assert passed_count < CLASSIFY_FLOOR, (
        "a two-case regression among the always-passing cases must red the "
        "floor when the weak case also misses that run"
    )
