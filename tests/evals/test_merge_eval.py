# SPDX-License-Identifier: Apache-2.0
"""Merge (Tier-3 WRITE/MERGE) live-API eval (issue athenaeum#552).

Runs :func:`athenaeum.tiers.tier3_merge` against every case in
``tests/evals/data/merge/cases.yaml`` using a real Anthropic call (the same
WRITE model tier3_merge uses in production, ``DEFAULT_WRITE_MODEL``).

This covers the "WRITE / MERGE" gap named in issue athenaeum#552's inventory:
tier3_merge's primary contract is a JSON list of ANCHORED EDIT OPERATIONS
(``replace`` / ``insert_after`` / ``append_section``) applied deterministically
to the existing page body, with a text-prefix ``ESCALATE:`` protocol for
principled-tension conflicts, and a full-echo fallback for any unparseable /
truncated / inapplicable patch response (issue athenaeum#469/#496). Existing unit
tests (``tests/test_tiers.py``) prove the PARSER handles canned responses of
each shape; they cannot show whether the live model still tends to (a) emit
an appliable ops list for a genuinely new fact, (b) fold a re-confirming
observation into an existing bullet instead of duplicating it (athenaeum#297 dedup
policy), (c) avoid escalating a merely-factual contradiction, and (d) DOES
escalate a genuinely principled one. That's exactly the "output shape / a
scoring judgment a unit test can't pin" gap this eval fills.

Scoring is over RESULT SHAPE (keyword presence, escalation yes/no, duplicate
bullet count) — never exact prose — and is deliberately agnostic to whether
tier3_merge's patch path or its full-echo fallback produced the result: both
are the same public contract, and the eval should not fail merely because a
particular run's patch-mode ops didn't apply cleanly.

Per-case outcomes are appended to the session accumulator; the aggregate pass
floor is asserted in :func:`test_merge_aggregate_floor` — a single miss does
NOT flake main, but a systemic degradation does.

Marker: ``pytest.mark.eval`` — deselected by default (see pyproject).
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from athenaeum.models import EntityAction, TokenUsage
from athenaeum.tiers import DEFAULT_WRITE_MODEL, tier3_merge
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_MERGE,
    RecordingClient,
    build_live_client,
    live_ready,
)

pytestmark = pytest.mark.eval


# Floor derivation: N=4 cases, one per outcome class (clean_insert,
# reconfirmation, factual_contradiction, principled_escalate). A floor of 3
# leaves 1-case slack over an all-pass expectation — the smallest golden set
# in the suite (matching the resolver set's per-action-class minimum of two
# would double authoring cost for a stage already backstopped by tier3_merge's
# own deterministic full-echo fallback), so a single hard case does not sink
# the whole layer, but a systemic miss (e.g. escalation never fires, or fires
# on a non-principled case) fails loudly.
MERGE_FLOOR = 3  # >= 3/4


def _load_cases() -> list[dict[str, Any]]:
    cases_path = EVAL_DATA_ROOT / "merge" / "cases.yaml"
    return list(yaml.safe_load(cases_path.read_text(encoding="utf-8")))


def _make_action(case: dict[str, Any]) -> EntityAction:
    return EntityAction(
        kind="update",
        name=case["id"],
        entity_type="reference",
        tags=[],
        access="internal",
        existing_uid="eval0001",
        observations=str(case["observation"]),
    )


def _score_case(
    case: dict[str, Any], body: str | None, escalation: Any
) -> tuple[bool, str]:
    expected = case["expected"]
    reasons: list[str] = []

    expect_escalate = bool(expected.get("escalates", False))
    got_escalate = escalation is not None
    if expect_escalate != got_escalate:
        reasons.append(f"escalates={got_escalate} expected={expect_escalate}")

    haystack = (body or "").lower()

    for substr in expected.get("must_include_substrings", []):
        if substr.lower() not in haystack:
            reasons.append(f"missing expected substring {substr!r}")

    for substr, max_count in (expected.get("max_occurrences") or {}).items():
        count = haystack.count(substr.lower())
        if count > max_count:
            reasons.append(
                f"substring {substr!r} occurs {count}x (max {max_count}) "
                "— looks like a duplicate bullet"
            )

    passed = not reasons
    detail = "; ".join(reasons) if reasons else "ok"
    return passed, detail


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_merge_case(
    case: dict[str, Any],
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Run one merge case; record its outcome for the aggregate score.

    Individual case failure does NOT fail the test — the aggregate floor
    (see :func:`test_merge_aggregate_floor`) does.
    """
    action = _make_action(case)

    inner = build_live_client()
    client = RecordingClient(inner, record=eval_record, layer=LAYER_MERGE)
    client.start_case(case["id"])

    original_create = client.messages.create

    def _create(**params: Any) -> Any:
        response = original_create(**params)
        eval_session.observe_response(str(params.get("model", "")), response)
        return response

    client.messages.create = _create  # type: ignore[method-assign]

    usage = TokenUsage()
    body, escalation = tier3_merge(
        action,
        str(case["existing_body"]),
        str(case["source_ref"]),
        client,
        usage=usage,
    )
    client.end_case()

    passed, detail = _score_case(case, body, escalation)

    eval_session.record_case(
        LAYER_MERGE,
        case["id"],
        expected=str(case["expected"]),
        observed=f"escalated={escalation is not None} body_len={len(body or '')}",
        passed=passed,
        detail=f"outcome_class={case.get('outcome_class', '')} {detail}",
    )


def test_merge_aggregate_floor(eval_session: Any, _live_ready: None) -> None:
    """Assert the merge layer meets the aggregate floor."""
    passed, total = eval_session.layer_score(LAYER_MERGE)
    assert total > 0, "merge eval collected no cases"
    assert passed >= MERGE_FLOOR, (
        f"merge below aggregate floor: {passed}/{total} "
        f"(need >= {MERGE_FLOOR}). Model: {DEFAULT_WRITE_MODEL}. "
        "Check eval-summary.json for per-case failures."
    )
