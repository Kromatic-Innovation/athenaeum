# SPDX-License-Identifier: Apache-2.0
"""Recast-machinery test for :mod:`athenaeum.shadow_parity` (issue athenaeum#1333, AC4).

Runs in NORMAL CI — zero network, zero paid calls, NOT ``pytest.mark.eval``
— by replaying the real recorded detector fixtures
(``tests/fixtures/recorded/detector/``, prompt-hash enforced, the same
posture ``tests/test_recorded_fixtures.py`` already uses) for detector-suite
cases, using the resolver-suite cases' hand-authored ``detector:`` block for
resolver-suite cases (the two corpora together are the 18 committed cases),
and a SCRIPTED comparator stub for every pair.

**This test proves the recast MACHINERY is wired correctly — it is NOT a
parity measurement.** The comparator stub's verdict is a fixed, deterministic
function of each case's ``outcome_class`` (see
:data:`_CONTENT_RELATION_BY_OUTCOME_CLASS`), not a live model judgement; a
``comparator_correct``/``agreement`` value that happens to look "right" here
says nothing about real detector/comparator parity. The real measurement is
athenaeum#1258's live-corpus run — do not dress this scripted answer up as one.

**Why this drives the module's building blocks directly instead of calling
:func:`athenaeum.shadow_parity.run_shadow_parity` end-to-end:**
:func:`~athenaeum.shadow_parity.run_shadow_parity` materialises each case
under ``workdir/<source>-<case_id>/`` (issue athenaeum#1333's own spec), so a
detector-suite case's ``AutoMemoryFile.origin_scope`` there is
``"detector-<case_id>"``. The ORIGINAL fixtures under
``tests/fixtures/recorded/detector/`` were recorded by
``tests/evals/test_detector_eval.py``, whose own scope directory is
``"scope-<case_id>"`` — and ``origin_scope`` is embedded verbatim in the
detector prompt (``contradictions._member_ref``), so it is part of what the
prompt-hash staleness contract checks. Replaying through
``run_shadow_parity``'s own materialisation therefore raises
``FixtureStaleError`` for a reason that has NOTHING to do with the detector
prompt actually drifting — it is purely this test's choice of scope-directory
name diverging from the recording session's. Calling
:func:`~athenaeum.shadow_parity.materialise_members` directly with a
``"scope-<case_id>"`` destination (matching the recording convention exactly)
avoids that false positive while still exercising every real recast function
this issue built: :func:`~athenaeum.shadow_parity.detector_verdict_from_result`,
:func:`~athenaeum.shadow_parity.roll_up_comparator_verdict`,
:func:`~athenaeum.shadow_parity.classify_agreement`,
:func:`~athenaeum.shadow_parity.comparator_decided_correctly`, plus the real
:func:`athenaeum.contradictions.detect_contradictions` /
:func:`athenaeum.cluster_comparator.run_cluster_comparator` call sites.
A genuine prompt drift (an actual edit to ``contradictions._DETECT_SYSTEM``
or ``_build_user_message``) still raises ``FixtureStaleError`` here exactly
as it would through ``run_shadow_parity`` — this test does not special-case
that away, per athenaeum#1333's own instruction: a real staleness error must
stop the test, not be worked around with a stub.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from athenaeum.cluster_comparator import run_cluster_comparator
from athenaeum.contradictions import detect_contradictions
from athenaeum.models import ContradictionResult, TokenUsage
from athenaeum.shadow_parity import (
    AgreementMatrix,
    ParityCase,
    ParityItem,
    classify_agreement,
    comparator_decided_correctly,
    detector_verdict_from_result,
    load_parity_cases,
    materialise_members,
    roll_up_comparator_verdict,
)
from tests.evals.harness import EVAL_DATA_ROOT, LAYER_DETECTOR, replay_client

# Forces the comparator gate on, mirroring tests/test_cluster_comparator.py's
# own ``_AUTO_ON`` constant -- the comparator defaults OFF everywhere else in
# the codebase (athenaeum.config.resolve_comparator_enabled), so a harness
# run must force it explicitly or it silently measures nothing.
_COMPARATOR_ON: dict[str, object] = {"librarian": {"comparator_enabled": True}}

# Deterministic, SCRIPTED Gate-2 content-relation verdict per outcome_class
# -- not a live judgement (see module docstring). Picked to be a plausible
# rough match for each class without claiming to BE the real answer.
_CONTENT_RELATION_BY_OUTCOME_CLASS: dict[str, str] = {
    "pass": "compatible",
    "contradict": "conflicting",
    "escalate": "conflicting",
    "merge": "equivalent",
}


def _canned_response(payload: dict[str, Any]) -> MagicMock:
    response = MagicMock()
    response.content = [MagicMock(type="text", text=json.dumps(payload))]
    response.usage = MagicMock(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


def _scripted_comparator_client(outcome_class: str) -> MagicMock:
    relation = _CONTENT_RELATION_BY_OUTCOME_CLASS.get(outcome_class, "conflicting")
    payload = {
        "content_relation": relation,
        "conflicting_passages": (
            ["scripted passage a", "scripted passage b"] if relation == "conflicting" else []
        ),
        "predicate_a": "scripted-predicate-a",
        "predicate_b": "scripted-predicate-b",
        "rationale": "scripted stub -- proves recast machinery, not real parity",
    }
    client = MagicMock()
    client.messages.create.return_value = _canned_response(payload)
    return client


def _all_cases() -> list[ParityCase]:
    return load_parity_cases(EVAL_DATA_ROOT / "detector" / "cases.yaml") + load_parity_cases(
        EVAL_DATA_ROOT / "resolver" / "cases.yaml"
    )


def _recast_one_case(case: ParityCase, tmp_path: Path) -> ParityItem:
    # "scope-<case_id>" matches tests/evals/test_detector_eval.py's own
    # recording scope-directory convention exactly -- see module docstring.
    dest_dir = tmp_path / f"scope-{case.case_id}"
    members = materialise_members(case, dest_dir)

    if case.declared_detector is not None:
        det = case.declared_detector
        detector_result = ContradictionResult(
            detected=True,
            conflict_type=det.conflict_type,
            conflicting_passages=list(det.passages),
            rationale=det.rationale,
        )
        detector_calls = 0
    else:
        # Real recorded fixture, prompt-hash enforced. A genuine
        # FixtureStaleError propagates uncaught here -- a real finding,
        # never worked around with a stub (athenaeum#1333's own instruction).
        detector_client = replay_client(LAYER_DETECTOR, case.case_id)
        detector_result = detect_contradictions(members, detector_client, usage=TokenUsage())
        detector_calls = 1
    detector_verdict = detector_verdict_from_result(detector_result)

    comparator_client = _scripted_comparator_client(case.outcome_class)
    cluster_result = run_cluster_comparator(
        members,
        comparator_client,
        config=_COMPARATOR_ON,
        usage=TokenUsage(),
        cluster_id=f"{case.source}-{case.case_id}",
    )
    comparator_verdict = roll_up_comparator_verdict(
        [outcome for _a, _b, outcome in cluster_result.outcomes]
    )
    agreement = classify_agreement(detector_verdict, comparator_verdict)
    correct = comparator_decided_correctly(case.outcome_class, comparator_verdict)

    return ParityItem(
        case_id=case.case_id,
        source=case.source,
        outcome_class=case.outcome_class,
        detector_verdict=detector_verdict,
        comparator_verdict=comparator_verdict,
        pair_verdicts=[
            {"a": id_a, "b": id_b, "verdict": outcome.verdict}
            for id_a, id_b, outcome in cluster_result.outcomes
        ],
        agreement=agreement,
        comparator_correct=correct,
        detector_calls=detector_calls,
        comparator_calls=len(cluster_result.outcomes),
    )


class TestShadowParityRecast:
    def test_all_18_cases_recast_with_source_and_scored_correctness(
        self, tmp_path: Path
    ) -> None:
        cases = _all_cases()
        assert len(cases) == 18

        items = [_recast_one_case(case, tmp_path) for case in cases]

        assert len(items) == 18
        assert {item.case_id for item in items} == {c.case_id for c in cases}

        detector_items = [item for item in items if item.source == "detector"]
        resolver_items = [item for item in items if item.source == "resolver"]
        assert len(detector_items) == 10
        assert len(resolver_items) == 8

        for item in items:
            assert item.source in ("detector", "resolver")
            # Every item carries a comparator_correct verdict of
            # True/False/None -- present, not missing (``in`` over the
            # 3-tuple also rejects an accidental falsy-but-wrong sentinel).
            assert item.comparator_correct in (True, False, None)

        matrix = AgreementMatrix()
        for item in items:
            matrix.add(item.detector_verdict, item.comparator_verdict)
        assert matrix.total == 18

        # Resolver-suite items cost zero detector calls (declared verdict,
        # never dispatched); detector-suite items cost exactly one each.
        for item in resolver_items:
            assert item.detector_calls == 0
        for item in detector_items:
            assert item.detector_calls == 1
