# SPDX-License-Identifier: Apache-2.0
"""Underdetermined-source (Tier-3 CREATE) live-API eval (issue athenaeum#1518).

Runs :func:`athenaeum.tiers.tier3_create` against every case in
``tests/evals/data/underdetermined/cases.yaml`` using a real Anthropic call
(the same WRITE model tier3_create uses in production, ``DEFAULT_WRITE_MODEL``).

Every existing golden set that drives tier3_create/tier3_merge is
conflict-shaped: ``merge/cases.yaml``'s four cases are all two-or-more
claims competing, and ``tests/evals/tier_compare.py`` scores each case for
an embedded probe rather than dedicating a case to underdetermination. None
of them asks what happens when a source simply says NOTHING about a field
the entity template invites — the gap this layer fills. ``tests/test_tiers.py``
proves the PARSER handles canned responses of each shape; it cannot show
whether the live model, given a source that underdetermines a field, still
tends to (a) leave that field absent rather than infer it from context such
as the authoring document's own provenance, (b) avoid folding an adjacent
first-person-author fact into a different named person's attributed role,
(c) raise a materially-gapped field via the existing `## Open Questions`
mechanism instead of guessing OR staying silent, and (d) still populate a
field the source DOES state plainly — the negative control proving the fix
is not blanket suppression. That's exactly the "output shape / a scoring
judgment a unit test can't pin" gap this eval fills.

Scoring is over RESULT SHAPE (substring presence/absence, same-sentence
co-occurrence as a proxy for "is this fact attributed to this person") —
never exact prose.

Per-case outcomes are appended to the session accumulator; the aggregate
pass floor is asserted in :func:`test_underdetermined_aggregate_floor` — a
single miss does NOT flake main, but a systemic degradation does.

Marker: ``pytest.mark.eval`` — deselected by default (see pyproject).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from athenaeum.models import EntityAction, TokenUsage
from athenaeum.tiers import DEFAULT_WRITE_MODEL, tier3_create
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_UNDERDETERMINED,
    RecordingClient,
    build_live_client,
    live_ready,
)

pytestmark = pytest.mark.eval


# Floor derivation: N=4 cases, one per outcome class (silent_field_absent,
# adjacent_fact_not_folded, material_gap_escalates, stated_field_populates).
# A floor of 3 leaves 1-case slack over an all-pass expectation, matching
# MERGE_FLOOR's reasoning exactly (tests/evals/test_merge_eval.py) — the
# smallest golden set in the suite, so a single hard case does not sink the
# whole layer, but a systemic miss (e.g. the negative control D starts
# failing, meaning the fix regressed to blanket suppression) fails loudly.
UNDERDETERMINED_FLOOR = 3  # >= 3/4


def _load_cases() -> list[dict[str, Any]]:
    cases_path = EVAL_DATA_ROOT / "underdetermined" / "cases.yaml"
    return list(yaml.safe_load(cases_path.read_text(encoding="utf-8")))


def _make_action(case: dict[str, Any]) -> EntityAction:
    entity = case["entity"]
    return EntityAction(
        kind="create",
        name=str(entity["name"]),
        entity_type=str(entity["entity_type"]),
        tags=list(entity.get("tags") or []),
        access=str(entity.get("access", "internal")),
        existing_uid=None,
        observations=str(case["observation"]),
    )


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _co_occurs_same_sentence(body: str, a: str, b: str) -> bool:
    """Whether *a* and *b* both appear (case-insensitively) in one sentence.

    A proxy for "is fact b attributed to a" that scores shape, not exact
    prose: it does not care HOW the sentence says it, only whether the two
    substrings ever land in the same sentence.
    """
    a_l, b_l = a.lower(), b.lower()
    return any(a_l in s.lower() and b_l in s.lower() for s in _sentences(body))


def _score_case(case: dict[str, Any], body: str | None) -> tuple[bool, str]:
    expected = case["expected"]
    reasons: list[str] = []
    haystack = (body or "").lower()

    for substr in expected.get("must_include_substrings", []):
        if substr.lower() not in haystack:
            reasons.append(f"missing expected substring {substr!r}")

    for substr in expected.get("must_not_include_substrings", []):
        if substr.lower() in haystack:
            reasons.append(f"unexpected substring {substr!r} present (invented fact?)")

    for pair in expected.get("forbidden_co_occurrence", []):
        a, b = pair[0], pair[1]
        if _co_occurs_same_sentence(body or "", a, b):
            reasons.append(
                f"{a!r} and {b!r} co-occur in one sentence — looks like a "
                "fact folded into the wrong person's role"
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
def test_underdetermined_case(
    case: dict[str, Any],
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Run one underdetermined-source case; record its outcome for the
    aggregate score.

    Individual case failure does NOT fail the test — the aggregate floor
    (see :func:`test_underdetermined_aggregate_floor`) does.
    """
    action = _make_action(case)

    inner = build_live_client()
    client = RecordingClient(inner, record=eval_record, layer=LAYER_UNDERDETERMINED)
    client.start_case(case["id"])

    original_create = client.messages.create

    def _create(**params: Any) -> Any:
        response = original_create(**params)
        eval_session.observe_response(str(params.get("model", "")), response)
        return response

    client.messages.create = _create  # type: ignore[method-assign]

    usage = TokenUsage()
    entity = tier3_create(
        action,
        str(case["source_ref"]),
        client,
        usage=usage,
    )
    client.end_case()

    passed, detail = _score_case(case, entity.body)

    eval_session.record_case(
        LAYER_UNDERDETERMINED,
        case["id"],
        expected=str(case["expected"]),
        observed=f"body_len={len(entity.body or '')}",
        passed=passed,
        detail=f"outcome_class={case.get('outcome_class', '')} {detail}",
    )


def test_underdetermined_aggregate_floor(eval_session: Any, _live_ready: None) -> None:
    """Assert the underdetermined-source layer meets the aggregate floor."""
    passed, total = eval_session.layer_score(LAYER_UNDERDETERMINED)
    assert total > 0, "underdetermined eval collected no cases"
    assert passed >= UNDERDETERMINED_FLOOR, (
        f"underdetermined below aggregate floor: {passed}/{total} "
        f"(need >= {UNDERDETERMINED_FLOOR}). Model: {DEFAULT_WRITE_MODEL}. "
        "Check eval-summary.json for per-case failures."
    )
