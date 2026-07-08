# SPDX-License-Identifier: Apache-2.0
"""Detector live-API eval (issue #331).

Runs :func:`detect_contradictions` against every case in
``tests/evals/data/detector/cases.yaml`` using a real Anthropic Haiku call.
Per-case outcomes are appended to the session accumulator; the aggregate
pass floor is asserted in :func:`test_detector_aggregate_floor` — a single
mis-classification does NOT flake main (single-case model determinism is
not achievable at Haiku's temperature), but a systemic degradation to
< 8/10 does.

Marker: ``pytest.mark.eval`` — deselected by default (see pyproject).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum.contradictions import DEFAULT_CONTRADICTION_MODEL, detect_contradictions
from athenaeum.models import AutoMemoryFile, TokenUsage
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_DETECTOR,
    RecordingClient,
    build_live_client,
    live_ready,
)

pytestmark = pytest.mark.eval


DETECTOR_FLOOR = 8  # ≥ 8/10 per acceptance criteria


def _load_cases() -> list[dict[str, Any]]:
    cases_path = EVAL_DATA_ROOT / "detector" / "cases.yaml"
    return list(yaml.safe_load(cases_path.read_text(encoding="utf-8")))


def _materialise_members(
    scope_dir: Path,
    case: dict[str, Any],
) -> list[AutoMemoryFile]:
    """Write each case's member spec to disk and return the AutoMemoryFile list.

    The detector's ``_build_user_message`` re-reads the on-disk body + the
    scope-header metadata (``valid_from``, ``valid_until``, ``source_type``,
    ``updated``), so the fixture must round-trip through the real intake
    frontmatter shape rather than the dataclass alone.
    """
    scope_dir.mkdir(parents=True, exist_ok=True)
    members: list[AutoMemoryFile] = []
    for spec in case["members"]:
        fm = spec.get("frontmatter") or {}
        # Render the frontmatter as YAML (single-line values only — the
        # golden set uses simple scalar frontmatter, which parse_frontmatter
        # loads reliably without a full YAML round-trip).
        fm_lines = ["---"]
        for key, value in fm.items():
            fm_lines.append(f"{key}: {value}")
        fm_lines.append("---")
        body = str(spec["body"]).rstrip()
        path = scope_dir / spec["filename"]
        path.write_text("\n".join(fm_lines) + "\n" + body + "\n", encoding="utf-8")
        members.append(
            AutoMemoryFile(
                path=path,
                origin_scope=scope_dir.name,
                memory_type=str(fm.get("type", "feedback")),
                name=str(fm.get("name", spec["filename"])),
                source_type=str(fm.get("source_type", "inferred")),
                source_ref=str(fm.get("source_ref", "")),
                valid_from=str(fm.get("valid_from", "")),
                valid_until=str(fm.get("valid_until", "")),
            )
        )
    return members


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_detector_case(
    case: dict[str, Any],
    tmp_path: Path,
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Run one detector case; record its outcome for the aggregate score.

    Individual case failure does NOT fail the test — the aggregate floor
    (see :func:`test_detector_aggregate_floor`) does. This keeps single-case
    model noise from flaking main while still surfacing per-case detail in
    the JSON summary + workflow log.
    """
    scope_dir = tmp_path / f"scope-{case['id']}"
    members = _materialise_members(scope_dir, case)

    inner = build_live_client()
    client = RecordingClient(inner, record=eval_record, layer=LAYER_DETECTOR)
    client.start_case(case["id"])

    # Wrap the recording client so every response's usage counters roll up
    # into the session budget guard. We intercept at messages.create rather
    # than replacing the client so the recording behaviour still fires.
    original_create = client.messages.create

    def _create(**params: Any) -> Any:
        response = original_create(**params)
        eval_session.observe_response(str(params.get("model", "")), response)
        return response

    client.messages.create = _create  # type: ignore[method-assign]

    usage = TokenUsage()
    result = detect_contradictions(members, client, usage=usage)
    client.end_case()

    expected = case["expected"]
    passed = bool(result.detected) == bool(expected.get("detected", False))
    if passed and expected.get("conflict_type") is not None:
        passed = result.conflict_type == expected["conflict_type"]

    observed = (
        f"detected={result.detected} conflict_type={result.conflict_type}"
    )
    expected_str = (
        f"detected={expected.get('detected')} "
        f"conflict_type={expected.get('conflict_type')}"
    )
    eval_session.record_case(
        LAYER_DETECTOR,
        case["id"],
        expected=expected_str,
        observed=observed,
        passed=passed,
        detail=f"outcome_class={case.get('outcome_class', '')} "
        f"rationale={result.rationale[:120]}",
    )


def test_detector_aggregate_floor(eval_session: Any, _live_ready: None) -> None:
    """Assert the detector layer meets the ≥ 8/10 aggregate floor."""
    passed, total = eval_session.layer_score(LAYER_DETECTOR)
    assert total > 0, "detector eval collected no cases"
    assert passed >= DETECTOR_FLOOR, (
        f"detector below aggregate floor: {passed}/{total} "
        f"(need ≥ {DETECTOR_FLOOR}). Model: {DEFAULT_CONTRADICTION_MODEL}. "
        "Check eval-summary.json for per-case failures."
    )
