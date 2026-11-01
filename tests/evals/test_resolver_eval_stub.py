# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the slack-widened resolver gate (issue athenaeum#2231).

UNMARKED — no ``pytest.mark.eval`` — runs in the default suite, offline,
no credential, no network, against a hand-built fake client mirroring the
exact JSON contract :func:`tests.test_resolutions.TestWorkedExample.
test_tristan_is_german_keep_b` already proves for
:func:`athenaeum.resolutions.propose_resolution`. Exercises the real
``propose_resolution`` and the real ``_classify_proposal`` /
``_materialise_members`` / ``_detector_result`` helpers from
:mod:`tests.evals.test_resolver_eval`, not a re-implementation.

AC4 (athenaeum#2231): RESOLVER_FLOOR=6 over n=9 tolerates up to 2 failures
among the 8 non-permanent-miss cases (the pre-existing
``decision_conflict_hosting_migration`` permanent miss always consumes the
other allowed failure — see the ``RESOLVER_FLOOR`` derivation comment).
This proves that boundary is real, both directions:

- exactly 2 live regressions (on top of the permanent miss) still clears
  the floor — the INTENDED slack athenaeum#345/#2231 designed in, not a bug, and
- a 3rd live regression crosses it and reds the floor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from athenaeum.models import TokenUsage
from athenaeum.resolutions import propose_resolution
from tests.evals.test_resolver_eval import (
    RESOLVER_FLOOR,
    _classify_proposal,
    _detector_result,
    _load_cases,
    _materialise_members,
)

_CORRECT_KEEP_B = (
    '{"recommended_winner": "b", "action": "keep_b", "confidence": 0.9, '
    '"rationale": "dated, user-stated claim takes precedence over the '
    'unsourced one", "source_precedence_used": ["a:inferred > b:user-stated"]}'
)
# Mirrors a real failure shape _classify_proposal must catch: the resolver
# escalating a precedence-decidable pair instead of picking a winner — maps
# to "disambiguation", not the expected "keep_pick_winner".
_WRONG_ESCALATES = (
    '{"recommended_winner": "neither", "action": "retain_both_with_context", '
    '"confidence": 0.4, "rationale": "insufficient signal to pick a winner", '
    '"disambiguation_options": ["a", "b"]}'
)
# A disambiguation case answered as a precedence pick instead of an
# escalation — maps to "keep_pick_winner", not the expected "disambiguation".
_WRONG_PICKS_WINNER = (
    '{"recommended_winner": "a", "action": "keep_a", "confidence": 0.8, '
    '"rationale": "picked a winner instead of escalating", '
    '"source_precedence_used": []}'
)

_PRECEDENCE_CASE_IDS = (
    "precedence_user_over_unsourced_contact",
    "precedence_user_over_unsourced_deadline",
)
_PERMANENT_MISS_ID = "decision_conflict_hosting_migration"
_DISAMBIGUATION_CASE_ID = "undated_office_address"


def _fake_client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _run_case(case: dict[str, Any], tmp_path: Path, *, payload: str) -> bool:
    scope_dir = tmp_path / f"scope-{case['id']}"
    members = _materialise_members(scope_dir, case)
    detector = _detector_result(case, members)
    client = _fake_client(payload)

    proposal = propose_resolution(detector, members, client, usage=TokenUsage())
    observed_class = _classify_proposal(proposal)
    return observed_class == case["expected"]["action_class"]


def test_classify_proposal_catches_a_wrong_action_on_a_reliable_case(
    tmp_path: Path,
) -> None:
    """Sanity: the scripted correct/wrong payload pair for a
    keep_pick_winner precedence case actually scores PASS/FAIL through the
    real propose_resolution() + _classify_proposal() path."""
    cases = {c["id"]: c for c in _load_cases()}
    case = cases["precedence_user_over_unsourced_contact"]
    assert case["expected"]["action_class"] == "keep_pick_winner"

    assert _run_case(case, tmp_path, payload=_CORRECT_KEEP_B) is True
    assert _run_case(case, tmp_path, payload=_WRONG_ESCALATES) is False


def test_two_live_regressions_plus_the_permanent_miss_stay_at_the_floor(
    tmp_path: Path,
) -> None:
    """The INTENDED slack: 2 live failures + the pre-existing permanent
    miss lands exactly AT RESOLVER_FLOOR (6/9), not below it.

    Both precedence cases are run for real through propose_resolution()
    with the wrong payload; the other 6 cases (including the permanent
    miss, whose own real-world failure is established by the strict xfail
    in tests/test_recorded_fixtures.py, not re-derived here) are counted
    via the arithmetic the real test_resolver_aggregate_floor performs.
    """
    cases = {c["id"]: c for c in _load_cases()}
    assert len(cases) == 9, "expected athenaeum#2231's added case to bring n to 9"

    for case_id in _PRECEDENCE_CASE_IDS:
        assert _run_case(cases[case_id], tmp_path, payload=_WRONG_ESCALATES) is False

    failing_ids = {_PERMANENT_MISS_ID, *_PRECEDENCE_CASE_IDS}
    passed_count = len(cases) - len(failing_ids)

    assert passed_count == RESOLVER_FLOOR, (
        "2 live regressions on top of the permanent miss should land "
        "exactly at the floor (the designed-in slack), not below it"
    )


def test_a_third_live_regression_reds_resolver_floor(tmp_path: Path) -> None:
    """One more live regression beyond the two above crosses the floor's
    2-failure allowance and must red it."""
    cases = {c["id"]: c for c in _load_cases()}

    for case_id in _PRECEDENCE_CASE_IDS:
        assert _run_case(cases[case_id], tmp_path, payload=_WRONG_ESCALATES) is False
    disambiguation_case = cases[_DISAMBIGUATION_CASE_ID]
    assert disambiguation_case["expected"]["action_class"] == "disambiguation"
    assert (
        _run_case(disambiguation_case, tmp_path, payload=_WRONG_PICKS_WINNER) is False
    )

    failing_ids = {
        _PERMANENT_MISS_ID,
        _DISAMBIGUATION_CASE_ID,
        *_PRECEDENCE_CASE_IDS,
    }
    passed_count = len(cases) - len(failing_ids)

    assert passed_count < RESOLVER_FLOOR, (
        "a third live regression, on top of the permanent miss and the "
        "two precedence-case regressions, must red the floor"
    )
