# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the re-floored audit_retirement gate (issue athenaeum#2231).

UNMARKED — no ``pytest.mark.eval`` — so this runs in the default suite,
offline, with no credential and no network dependency, against
:class:`tests.conftest.FakeLLMClient` (never the network), mirroring
``tests/evals/test_write_tier_compare_stub.py``'s own rationale: a harness
(here, a re-floored gate) that has never been exercised against a degraded
response is unproven. Reuses the real scoring helper
(:func:`tests.evals.test_audit_retirement_eval._score_case`) and the real
:func:`athenaeum.audit.audit_page`, not a re-implementation of either — so
this proves the SHIPPED gate logic, not a parallel copy of it.

AC4 (athenaeum#2231): "genuine regressions must still fail." The two tests
below replay the exact per-case structure the re-floor's docstring analysis
rests on (see the comment above
:data:`tests.evals.test_audit_retirement_eval.AUDIT_RETIREMENT_FLOOR`):

- a regression in TWO of the four always-passing cases reds the floor
  regardless of the stochastic case's outcome (the collapse the gate exists
  to catch), and
- a regression in exactly ONE always-passing case does NOT red the floor
  when the stochastic case happens to pass that run (the single-case
  regression the gate — honestly — cannot reliably catch at n=6).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from athenaeum.audit import audit_page
from athenaeum.config import DEFAULT_CLASSIFY_MODEL
from tests.conftest import FakeLLMClient
from tests.evals.test_audit_retirement_eval import (
    AUDIT_RETIREMENT_FLOOR,
    _load_cases,
    _score_case,
)

# The four cases observed to pass every single time across 23 October
# dispatches (see the floor-derivation comment in test_audit_retirement_eval.py).
_ALWAYS_PASSING_IDS = frozenset(
    {
        "pipeline_metadata_only",
        "dated_engagement_with_pipeline_metadata",
        "self_declared_filename_artifact",
        "light_source_summary",
    }
)
_STOCHASTIC_ID = "name_plus_one_affiliation_line"
_PERMANENT_MISS_ID = "name_only_person_stub"


def _verdict_json(*, candidate: bool) -> str:
    return json.dumps({"retirement_candidate": candidate, "retirement_reason": "synthetic"})


def _run_case(case: dict[str, Any], *, candidate_sent: bool) -> bool:
    """Run one case through the real ``audit_page`` with a scripted verdict.

    Returns whether :func:`_score_case` marks it passed.
    """
    client = FakeLLMClient(text=_verdict_json(candidate=candidate_sent))
    meta = dict(case["meta"])
    verdict = audit_page(
        client,
        uid=str(meta["uid"]),
        path=Path(f"/tmp/eval-audit-retirement-stub/{case['id']}.md"),
        meta=meta,
        body=str(case["body"]),
        model=DEFAULT_CLASSIFY_MODEL,
    )
    passed, _detail = _score_case(case, verdict)
    return passed


def test_score_case_catches_a_wrong_verdict_on_a_reliable_case() -> None:
    """A wrong answer on an always-passing case must score FAIL.

    Proves the per-case scorer — not just the floor comparison — actually
    distinguishes right from wrong, against the real ``audit_page`` parse
    path (not a hand-rolled verdict object).
    """
    cases = {c["id"]: c for c in _load_cases()}
    case = cases["pipeline_metadata_only"]
    assert case["expected"]["retirement_candidate"] is True

    correct = _run_case(case, candidate_sent=True)
    wrong = _run_case(case, candidate_sent=False)

    assert correct is True
    assert wrong is False


def test_two_case_collapse_reds_the_new_floor() -> None:
    """A regression in two of the four always-passing cases must red floor=4.

    Scripts: permanent miss wrong (as it always is live), two always-passing
    cases wrong (the injected regression), the remaining two always-passing
    cases AND the stochastic case correct. That is 3/6 passed — below
    ``AUDIT_RETIREMENT_FLOOR`` (4) regardless of the stochastic case's
    outcome, which is exactly the "two-or-more always-passing cases
    regress" collapse the re-floor's docstring says is always caught.
    """
    cases = {c["id"]: c for c in _load_cases()}
    regressed = {"pipeline_metadata_only", "dated_engagement_with_pipeline_metadata"}

    passed_count = 0
    for case_id, case in cases.items():
        expected = bool(case["expected"]["retirement_candidate"])
        if case_id == _PERMANENT_MISS_ID:
            send_correct = False  # permanent miss: always wrong live
        elif case_id in regressed:
            send_correct = False  # the injected regression
        else:
            send_correct = True  # the other two always-passing + stochastic
        candidate_sent = expected if send_correct else (not expected)
        if _run_case(case, candidate_sent=candidate_sent):
            passed_count += 1

    assert passed_count == 3
    assert passed_count < AUDIT_RETIREMENT_FLOOR, (
        "a two-case regression among the always-passing cases must red the "
        "re-floored gate"
    )


def test_single_case_regression_can_slip_through_when_stochastic_case_passes() -> None:
    """Honest limitation: ONE always-passing case regressing does not
    necessarily red floor=4, if the stochastic case happens to pass.

    Scripts: permanent miss wrong (as always), ONE always-passing case
    wrong (the injected single-case regression), the other three
    always-passing-or-stochastic cases correct. That is exactly 4/6 passed
    — AT the floor, not below it — demonstrating the "~22% power against a
    single-case regression" limitation the floor-derivation comment states
    plainly rather than hiding it.
    """
    cases = {c["id"]: c for c in _load_cases()}
    regressed_id = "pipeline_metadata_only"

    passed_count = 0
    for case_id, case in cases.items():
        expected = bool(case["expected"]["retirement_candidate"])
        if case_id in (_PERMANENT_MISS_ID, regressed_id):
            send_correct = False
        else:
            send_correct = True  # includes the stochastic case passing this run
        candidate_sent = expected if send_correct else (not expected)
        if _run_case(case, candidate_sent=candidate_sent):
            passed_count += 1

    assert passed_count == 4
    assert passed_count >= AUDIT_RETIREMENT_FLOOR, (
        "this is the documented false-green case: a single-case regression "
        "plus a lucky stochastic-case pass clears the floor"
    )
