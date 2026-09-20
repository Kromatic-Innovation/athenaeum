# SPDX-License-Identifier: Apache-2.0
"""Audit retirement-candidacy live-API eval (issue athenaeum#1869).

Runs :func:`athenaeum.audit.audit_page` against every case in
``tests/evals/data/audit_retirement/cases.yaml`` using a real Anthropic
Haiku call (the same model ``athenaeum audit`` resolves for the ``classify``
knob, ``athenaeum.config.DEFAULT_CLASSIFY_MODEL``).

**Why this layer exists.** ``tests/test_audit_retirement_rules.py``
(issue athenaeum#1849) only pins SUBSTRINGS of ``AUDIT_SYSTEM``'s prompt
text and feeds :class:`~tests.conftest.FakeLLMClient` a canned verdict —
it proves the verdict a fake model RETURNS lands in the report correctly,
never that a real model actually FOLLOWS the rule. A live 50-page dry run
against the shipped ``audit-v3`` prompt (2026-09-19) flagged 7 of 7 bare
name-only person stubs as retirement candidates despite the prompt's own
placeholder exemption — exactly the gap a substring-pinning unit test
structurally cannot see. This layer closes it.

**Deliberately calls :func:`~athenaeum.audit.audit_page`, not
:func:`~athenaeum.audit.build_audit_report`.** athenaeum#1869 also added a
deterministic code-side override (``athenaeum.audit._is_bare_person_stub``,
wired into ``build_audit_report``'s ``_finalize`` closure) that forces a
bare ``type: person`` H1-only page to never be a retirement candidate,
regardless of the model's verdict. If this eval drove pages through
``build_audit_report`` instead, that override would silently launder a
still-broken prompt into a passing case 1 (the exact scenario the code
rule exists to backstop) — the eval would then measure the code, not the
prompt, and a prompt regression could ship unnoticed. Calling
``audit_page`` directly returns the model's RAW verdict, so this layer
measures ``AUDIT_SYSTEM`` alone; the code override has its own, separate
coverage in ``tests/test_audit_1869.py``.

Per-case outcomes are appended to the session accumulator; the aggregate
pass floor is asserted in :func:`test_audit_retirement_aggregate_floor` —
a single miss does not flake main, but a systemic regression does.

**Never seeded.** No live LLM backend was reachable from the lane that
built this layer (``ANTHROPIC_API_KEY`` unset) — see the PR body and
``tests/fixtures/recorded/audit_retirement/`` (absent by design; the layer
is unlisted in ``tests/fixtures/recorded/seeded-layers.yml``, so
``tests/test_recorded_fixtures.py``'s replay tests skip cleanly with an
explicit reason rather than erroring the suite, exactly like every other
never-seeded layer). Seeding it (an ``evals.yml`` ``record=true`` run
against the ``audit-v5`` prompt, which athenaeum#1877 restored to the
``audit-v3`` wording measured at 5/6) is a tracked follow-up —
athenaeum#1871.

Marker: ``pytest.mark.eval`` — deselected by default (see pyproject).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum.audit import audit_page
from athenaeum.config import DEFAULT_CLASSIFY_MODEL
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_AUDIT_RETIREMENT,
    RecordingClient,
    build_live_client,
    live_ready,
)

pytestmark = pytest.mark.eval

# Floor derivation: N=6 cases, one per athenaeum#1869 acceptance-criteria
# scenario (2 not-a-candidate exclusions, 1 pipeline-metadata trigger, 1
# dated-outcome exclusion, 1 spurious-entity trigger, 1 light-source-summary
# exclusion). Each case has an unambiguous, invented ground truth — there is
# no subjective shape judgment the way CLASSIFY_FLOOR's entity-extraction
# cases have — so a one-case slack over an all-pass expectation (matching
# MERGE_FLOOR / UNDERDETERMINED_FLOOR's ~75% ratio) absorbs ordinary Haiku
# nondeterminism without hiding a systemic miss. The floor has already
# earned its keep: athenaeum#1877 measured the ``audit-v4`` rewrite of
# section 2 at 2/6 and 3/6 against this set, versus 5/6 twice for the
# ``audit-v3`` wording it replaced, which is why ``audit-v5`` restores
# that wording.
AUDIT_RETIREMENT_FLOOR = 5  # >= 5/6


def _load_cases() -> list[dict[str, Any]]:
    cases_path = EVAL_DATA_ROOT / "audit_retirement" / "cases.yaml"
    return list(yaml.safe_load(cases_path.read_text(encoding="utf-8")))


def _score_case(case: dict[str, Any], verdict: Any) -> tuple[bool, str]:
    """Score one case's raw verdict against its expected candidacy.

    A single boolean compare — there is no shape/substring judgment to make
    here, unlike the prose-scoring layers, because the prompt's own output
    field IS the thing under test.
    """
    if verdict.error is not None:
        return False, f"audit_page returned an error: {verdict.error}"
    expected = bool(case["expected"]["retirement_candidate"])
    observed = bool(verdict.retirement_candidate)
    if observed != expected:
        return False, (
            f"expected retirement_candidate={expected}, got {observed} "
            f"(reason={verdict.retirement_reason!r})"
        )
    return True, f"retirement_candidate={observed} reason={verdict.retirement_reason!r}"


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_audit_retirement_case(
    case: dict[str, Any],
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Run one retirement-candidacy case; record its outcome for the
    aggregate score.

    Individual case failure does NOT fail the test — the aggregate floor
    (see :func:`test_audit_retirement_aggregate_floor`) does.
    """
    meta = dict(case["meta"])
    body = str(case["body"])

    inner = build_live_client()
    client = RecordingClient(inner, record=eval_record, layer=LAYER_AUDIT_RETIREMENT)
    client.start_case(case["id"])

    original_create = client.messages.create

    def _create(**params: Any) -> Any:
        response = original_create(**params)
        eval_session.observe_response(str(params.get("model", "")), response)
        return response

    client.messages.create = _create  # type: ignore[method-assign]

    # audit_page (unlike tier2_classify/tier3_create) has no usage= parameter
    # to thread through — token accounting happens on the returned verdict's
    # own input_tokens/output_tokens instead. The eval_session-level totals
    # are already captured by the client.messages.create wrapper above, so
    # nothing further needs threading here.
    verdict = audit_page(
        client,
        uid=str(meta["uid"]),
        path=Path(f"/tmp/eval-audit-retirement/{case['id']}.md"),
        meta=meta,
        body=body,
        model=DEFAULT_CLASSIFY_MODEL,
    )
    client.end_case()

    passed, detail = _score_case(case, verdict)

    eval_session.record_case(
        LAYER_AUDIT_RETIREMENT,
        case["id"],
        expected=str(case["expected"]),
        observed=(
            f"retirement_candidate={verdict.retirement_candidate} "
            f"error={verdict.error}"
        ),
        passed=passed,
        detail=f"outcome_class={case.get('outcome_class', '')} {detail}",
    )


def test_audit_retirement_aggregate_floor(eval_session: Any, _live_ready: None) -> None:
    """Assert the audit-retirement layer meets the aggregate floor."""
    passed, total = eval_session.layer_score(LAYER_AUDIT_RETIREMENT)
    assert total > 0, "audit_retirement eval collected no cases"
    assert passed >= AUDIT_RETIREMENT_FLOOR, (
        f"audit_retirement below aggregate floor: {passed}/{total} "
        f"(need >= {AUDIT_RETIREMENT_FLOOR}). Model: {DEFAULT_CLASSIFY_MODEL}. "
        "Check eval-summary.json for per-case failures."
    )
