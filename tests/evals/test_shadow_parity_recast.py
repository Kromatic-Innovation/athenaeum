# SPDX-License-Identifier: Apache-2.0
"""Recast-machinery test for :mod:`athenaeum.shadow_parity` (issue athenaeum#1333, AC4).

Runs in NORMAL CI — zero network, zero paid calls, NOT ``pytest.mark.eval``
— by calling :func:`athenaeum.shadow_parity.run_shadow_parity` END-TO-END
over BOTH committed corpora, with:

- a detector client that dispatches, per detector-suite case, to
  ``tests.evals.harness.replay_client(LAYER_DETECTOR, case_id)`` — the real
  recorded fixture, prompt-hash enforced, the same posture
  ``tests/test_recorded_fixtures.py`` already uses. Resolver-suite cases
  carry a hand-authored ``detector:`` block and take
  :func:`~athenaeum.shadow_parity.run_shadow_parity`'s declared-verdict
  branch, which never reaches this client at all — see
  :func:`_ordered_detector_client`.
- a SCRIPTED comparator client, deterministic per case (see
  :func:`_case_scoped_comparator_client`).

**This test proves the recast MACHINERY is wired correctly — it is NOT a
parity measurement.** The comparator stub's verdict is a fixed, deterministic
function of each case's ``outcome_class`` (see
:data:`_CONTENT_RELATION_BY_OUTCOME_CLASS`), not a live model judgement; a
``comparator_correct``/``agreement`` value that happens to look "right" here
says nothing about real detector/comparator parity. The real measurement is
athenaeum#1258's live-corpus run — do not dress this scripted answer up as one.

A genuine prompt drift (an actual edit to ``contradictions._DETECT_SYSTEM``
or ``_build_user_message``) still raises ``FixtureStaleError`` here exactly
as it would through any other replay -- this test does not special-case
that away, per athenaeum#1333's own instruction: a real staleness error must
stop the test, not be worked around with a stub.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from athenaeum import comparator as comparator_mod
from athenaeum.cluster_comparator import candidate_pairs, page_from_auto_memory_file
from athenaeum.shadow_parity import (
    ParityCase,
    _case_scope_dir,
    load_parity_cases,
    materialise_members,
    run_shadow_parity,
)
from tests.evals.harness import EVAL_DATA_ROOT, LAYER_DETECTOR, replay_client

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


def _content_relation_payload(outcome_class: str) -> dict[str, Any]:
    relation = _CONTENT_RELATION_BY_OUTCOME_CLASS.get(outcome_class, "conflicting")
    return {
        "content_relation": relation,
        "conflicting_passages": (
            ["scripted passage a", "scripted passage b"] if relation == "conflicting" else []
        ),
        "predicate_a": "scripted-predicate-a",
        "predicate_b": "scripted-predicate-b",
        "rationale": "scripted stub -- proves recast machinery, not real parity",
    }


def _all_cases() -> list[ParityCase]:
    return load_parity_cases(EVAL_DATA_ROOT / "detector" / "cases.yaml") + load_parity_cases(
        EVAL_DATA_ROOT / "resolver" / "cases.yaml"
    )


def _ordered_detector_client(cases: list[ParityCase]) -> MagicMock:
    """One shared client for the whole run, dispatching to a REAL replay
    client per detector-suite case, IN THE ORDER :func:`run_shadow_parity`
    will call :func:`~athenaeum.contradictions.detect_contradictions`.

    Resolver-suite cases (a ``declared_detector``) take the declared-verdict
    branch and never dispatch to this client at all — the state counter
    below only ever advances past a detector-suite case's real fixture, and
    raises if called more times than there are detector-suite cases, so an
    accidental detector call for a declared-detector case fails loudly
    rather than silently consuming the wrong fixture.
    """
    ordered_case_ids = [c.case_id for c in cases if c.declared_detector is None]
    sub_creates = [replay_client(LAYER_DETECTOR, cid).messages.create for cid in ordered_case_ids]

    client = MagicMock()
    state = {"i": 0}

    def _create(**params: Any) -> Any:
        i = state["i"]
        if i >= len(sub_creates):
            raise AssertionError(
                f"detector client called {i + 1} times but only "
                f"{len(sub_creates)} detector-suite cases exist -- a "
                "declared-detector case must never dispatch a detector call"
            )
        state["i"] += 1
        return sub_creates[i](**params)

    client.messages.create.side_effect = _create
    return client


def _case_scoped_comparator_client(cases: list[ParityCase], workdir: Path) -> MagicMock:
    """One shared client for the whole run, dispatching by EXACT match of
    the outgoing Gate-2 user message against a precomputed per-case message.

    Built the SAME way :func:`athenaeum.cluster_comparator.run_cluster_comparator`
    builds it in production (:func:`~athenaeum.cluster_comparator.candidate_pairs`
    + :func:`~athenaeum.cluster_comparator.page_from_auto_memory_file` +
    :func:`athenaeum.comparator._build_content_relation_messages`), so this
    is robust to two real risks a simpler dispatch would not survive:

    - **Not every pair reaches Gate 2.** One committed case
      (``deploy_target_sequential_snapshot``, disjoint ``valid_from``/
      ``valid_until`` windows) resolves at Gate 1 without ever calling this
      client (verified directly against :mod:`athenaeum.cluster_comparator`
      before writing this dispatcher) — a naive "one call per case, in
      order" positional list would silently misalign every case after it.
    - **Body-text collisions.** Several resolver-suite cases reuse a
      detector-suite case's own member body verbatim or as a substring (the
      same underlying scenario, recast for the resolver eval) — a
      substring-matching dispatcher can genuinely pick the WRONG case
      (``tool_choice_editor`` vs ``refinement_editor_general_and_csv``,
      different ``outcome_class``, one body a substring of the other).
      Exact full-message equality does not have this problem: the two
      pages' full fenced content together is unique per case even when a
      fragment is shared.

    ``page.body`` (what the Gate-2 message embeds) depends only on each
    case's own content, not on which directory it is materialised under —
    so precomputing into a throwaway ``workdir/precompute/`` directory
    yields byte-identical messages to whatever
    :func:`~athenaeum.shadow_parity.run_shadow_parity`'s own
    materialisation (a DIFFERENT directory, per case) will send.
    """
    lookup: dict[str, str] = {}
    for case in cases:
        dest_dir = _case_scope_dir(workdir / "precompute", case)
        members = materialise_members(case, dest_dir)
        for member_a, member_b in candidate_pairs(members):
            page_a = page_from_auto_memory_file(member_a)
            page_b = page_from_auto_memory_file(member_b)
            msg = comparator_mod._build_content_relation_messages(page_a, page_b)
            lookup[msg] = case.outcome_class

    client = MagicMock()

    def _create(**params: Any) -> Any:
        messages = params.get("messages")
        content = ""
        if isinstance(messages, list) and messages:
            content = str(messages[0].get("content", ""))
        outcome_class = lookup.get(content)
        if outcome_class is None:
            raise AssertionError(
                f"no precomputed case matches this Gate-2 prompt: {content[:200]!r}"
            )
        return _canned_response(_content_relation_payload(outcome_class))

    client.messages.create.side_effect = _create
    return client


class TestShadowParityRecast:
    def test_all_18_cases_recast_end_to_end_via_run_shadow_parity(
        self, tmp_path: Path
    ) -> None:
        cases = _all_cases()
        assert len(cases) == 18
        detector_suite_cases = [c for c in cases if c.declared_detector is None]
        resolver_suite_cases = [c for c in cases if c.declared_detector is not None]
        assert len(detector_suite_cases) == 10
        assert len(resolver_suite_cases) == 8

        detector_client = _ordered_detector_client(cases)
        comparator_client = _case_scoped_comparator_client(cases, tmp_path)

        report = run_shadow_parity(
            cases,
            detector_client=detector_client,
            comparator_client=comparator_client,
            workdir=tmp_path,
        )

        assert len(report.items) == 18
        assert {item.case_id for item in report.items} == {c.case_id for c in cases}

        detector_items = [item for item in report.items if item.source == "detector"]
        resolver_items = [item for item in report.items if item.source == "resolver"]
        assert len(detector_items) == 10
        assert len(resolver_items) == 8

        for item in report.items:
            assert item.source in ("detector", "resolver")
            # Every item carries a comparator_correct verdict of
            # True/False/None -- present, not missing (``in`` over the
            # 3-tuple also rejects an accidental falsy-but-wrong sentinel).
            assert item.comparator_correct in (True, False, None)

        assert report.matrix.total == 18

        # Resolver-suite items took the declared-detector branch: zero
        # detector calls. Detector-suite items (all >= 2 members) cost
        # exactly one detector call each -- matching the number of
        # detector-suite cases, per the AC's own wording.
        for item in resolver_items:
            assert item.detector_calls == 0
        for item in detector_items:
            assert item.detector_calls == 1
        assert report.detector_calls == len(detector_suite_cases) == 10
        assert detector_client.messages.create.call_count == 10
