# SPDX-License-Identifier: Apache-2.0
"""Classify (Tier-2) live-API eval (issue athenaeum#552).

Runs :func:`athenaeum.tiers.tier2_classify` against every case in
``tests/evals/data/classify/cases.yaml`` using a real Anthropic Haiku call
(the same model tier2_classify uses in production, ``DEFAULT_CLASSIFY_MODEL``).

Unlike the detector/resolver/recall evals (issue athenaeum#331), which each classify a
RELATIONSHIP between two already-structured snippets, this eval covers
tier2_classify's job of extracting STRUCTURE (which entities are worth a wiki
page, and their name/type/tags/access) from ONE piece of unstructured raw
text — the "CLASSIFY" gap named in issue athenaeum#552's inventory. A stubbed-response
unit test (``tests/test_tiers.py``) proves the parser handles a canned
response; only a live call can show whether the classifier still extracts a
sensible entity SET from novel prose.

Per-case outcomes are appended to the session accumulator; the aggregate pass
floor is asserted in :func:`test_classify_aggregate_floor` — a single
mis-extraction does NOT flake main (model nondeterminism at Haiku's
temperature is expected), but a systemic degradation does.

Marker: ``pytest.mark.eval`` — deselected by default (see pyproject).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum.models import RawFile, TokenUsage
from athenaeum.tiers import DEFAULT_CLASSIFY_MODEL, tier2_classify
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_CLASSIFY,
    RecordingClient,
    build_live_client,
    live_ready,
)

pytestmark = pytest.mark.eval

# Floor derivation: N=6 cases spanning extract_single / extract_multiple /
# skip_matched / skip_placeholder / empty_procedural outcome classes. A
# generous 2-case slack over an all-pass expectation, matching the detector
# set's ~80% floor ratio — single-case Haiku noise on entity-count/name
# boundaries should not sink the layer, but a systemic drift (e.g. the
# classifier starts extracting placeholder mentions, or stops extracting
# real entities) must fail loudly.
CLASSIFY_FLOOR = 4  # >= 4/6


def _load_cases() -> list[dict[str, Any]]:
    cases_path = EVAL_DATA_ROOT / "classify" / "cases.yaml"
    return list(yaml.safe_load(cases_path.read_text(encoding="utf-8")))


def _make_raw(case: dict[str, Any]) -> RawFile:
    return RawFile(
        path=Path(f"/tmp/eval-classify/sessions/{case['id']}.md"),
        source="sessions",
        timestamp="20260301T120000Z",
        uuid8="ee110001",
        _content=str(case["content"]),
    )


def _score_case(case: dict[str, Any], entities: list[Any]) -> tuple[bool, str]:
    """Score one classify case's extracted entities against its expectation.

    Scoring is over SHAPE (entity count bounds, expected/unwanted name
    substrings) — never exact ``observations`` text, matching the other
    evals' "aggregate floor, not exact-output" contract.
    """
    expected = case["expected"]
    names = [e.name for e in entities]
    names_lower = " | ".join(n.lower() for n in names)

    reasons: list[str] = []

    min_entities = expected.get("min_entities")
    if min_entities is not None and len(entities) < min_entities:
        reasons.append(f"entity_count={len(entities)} < min {min_entities}")

    max_entities = expected.get("max_entities")
    if max_entities is not None and len(entities) > max_entities:
        reasons.append(f"entity_count={len(entities)} > max {max_entities}")

    for substr in expected.get("must_include_name_substrings", []):
        if substr.lower() not in names_lower:
            reasons.append(f"missing expected name substring {substr!r}")

    for substr in expected.get("must_not_include_name_substrings", []):
        if substr.lower() in names_lower:
            reasons.append(f"unwanted name substring {substr!r} present")

    passed = not reasons
    detail = "; ".join(reasons) if reasons else f"names={names}"
    return passed, detail


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_classify_case(
    case: dict[str, Any],
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Run one classify case; record its outcome for the aggregate score.

    Individual case failure does NOT fail the test — the aggregate floor
    (see :func:`test_classify_aggregate_floor`) does.
    """
    raw = _make_raw(case)

    inner = build_live_client()
    client = RecordingClient(inner, record=eval_record, layer=LAYER_CLASSIFY)
    client.start_case(case["id"])

    original_create = client.messages.create

    def _create(**params: Any) -> Any:
        response = original_create(**params)
        eval_session.observe_response(str(params.get("model", "")), response)
        return response

    client.messages.create = _create  # type: ignore[method-assign]

    usage = TokenUsage()
    entities = tier2_classify(
        raw,
        list(case.get("matched_names") or []),
        list(case["valid_types"]),
        list(case["valid_tags"]),
        list(case["valid_access"]),
        client,
        usage=usage,
    )
    client.end_case()

    passed, detail = _score_case(case, entities)

    eval_session.record_case(
        LAYER_CLASSIFY,
        case["id"],
        expected=str(case["expected"]),
        observed=f"entities={[e.name for e in entities]}",
        passed=passed,
        detail=f"outcome_class={case.get('outcome_class', '')} {detail}",
    )


def test_classify_aggregate_floor(eval_session: Any, _live_ready: None) -> None:
    """Assert the classify layer meets the aggregate floor."""
    passed, total = eval_session.layer_score(LAYER_CLASSIFY)
    assert total > 0, "classify eval collected no cases"
    assert passed >= CLASSIFY_FLOOR, (
        f"classify below aggregate floor: {passed}/{total} "
        f"(need >= {CLASSIFY_FLOOR}). Model: {DEFAULT_CLASSIFY_MODEL}. "
        "Check eval-summary.json for per-case failures."
    )
