# SPDX-License-Identifier: Apache-2.0
"""Live-LLM regression for the resolver prompt (issue #169, Lane 3).

Opt-in. Skipped in CI by default. Set ``ATHENAEUM_LIVE_TESTS=1`` and have a
working ``ANTHROPIC_API_KEY`` in the environment to run:

::

    ATHENAEUM_LIVE_TESTS=1 pytest -m live tests/regression/

Parametrizes the twelve canonical not_a_conflict cases plus the one
propose_merge case borrowed from
:mod:`tests.test_parse_response_routing`. Each case is sent through
:func:`propose_resolution` with a REAL Anthropic client (no mock). The
resolver must return the expected action with confidence >= 0.85.

These tests guard against prompt drift on the classification side:
``test_resolve_system_snapshot`` flags ANY edit to the prompt string, and
this test confirms that an intentional edit hasn't broken classification
quality on the canonical training set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.resolutions import (
    MergeProposal,
    ResolutionProposal,
    propose_resolution,
)
from tests.test_parse_response_routing import (  # type: ignore[import-not-found]
    MERGE_CASE,
    NOT_A_CONFLICT_CASES,
    ResolvedCase,
    _write_case_files,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("ATHENAEUM_LIVE_TESTS") != "1",
        reason="set ATHENAEUM_LIVE_TESTS=1 to run live-LLM prompt regression",
    ),
]


def _live_client():
    import anthropic  # type: ignore[import-not-found]

    return anthropic.Anthropic()


def _detector_result(case: ResolvedCase, ams_paths: list[Path]) -> ContradictionResult:
    return ContradictionResult(
        detected=True,
        conflict_type="prescriptive",
        members_involved=[f"scope/{p.name}" for p in ams_paths[:2]],
        conflicting_passages=list(case.passages),
        rationale=case.rationale,
    )


@pytest.mark.parametrize(
    "case",
    NOT_A_CONFLICT_CASES,
    ids=[c.label for c in NOT_A_CONFLICT_CASES],
)
def test_live_not_a_conflict(case: ResolvedCase, tmp_path: Path) -> None:
    ams = _write_case_files(case, tmp_path)
    detector = _detector_result(case, [m.path for m in ams])
    client = _live_client()

    proposal = propose_resolution(detector, ams, client)

    assert isinstance(proposal, ResolutionProposal), (
        f"{case.label}: expected ResolutionProposal, got " f"{type(proposal).__name__}"
    )
    assert proposal.action == "not_a_conflict", (
        f"{case.label}: expected action='not_a_conflict', " f"got {proposal.action!r}"
    )
    assert proposal.confidence >= 0.85, (
        f"{case.label}: confidence below contract floor " f"({proposal.confidence})"
    )


def test_live_propose_merge(tmp_path: Path) -> None:
    ams = _write_case_files(MERGE_CASE, tmp_path)
    detector = _detector_result(MERGE_CASE, [m.path for m in ams])
    client = _live_client()

    proposal = propose_resolution(detector, ams, client)

    assert isinstance(
        proposal, MergeProposal
    ), f"expected MergeProposal, got {type(proposal).__name__}"
    assert proposal.action == "propose_merge"
    assert proposal.confidence >= 0.85
