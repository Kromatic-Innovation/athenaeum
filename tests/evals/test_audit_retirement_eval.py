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

**Seeded 2026-09-25 at audit-v5**, by athenaeum#1871, from ``evals.yml``
run 36099358949
(https://github.com/Kromatic-Innovation/athenaeum/actions/runs/36099358949)
— a ``record=true`` dispatch at ``develop``, whose ``AUDIT_SYSTEM``
athenaeum#1877 had restored to the ``audit-v3`` wording measured at 5/6.
That run scored this layer 5/6 against a floor of 5.
``tests/fixtures/recorded/audit_retirement/`` now holds one fixture per
case and the layer is listed in
``tests/fixtures/recorded/seeded-layers.yml``, so
``tests/test_recorded_fixtures.py``'s replay test replays real recorded
verdicts with zero network instead of skipping. Its one miss,
``name_only_person_stub``, is pinned there as a **strict** xfail rather
than dropped — see that module's ``_DISPUTED`` entry. Do NOT re-seed from
the ``audit-v4`` ``record=true`` artifact (run 35466590979): it recorded a
2/6 run.

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

# Floor derivation (issue athenaeum#2231 — see tests/evals/README.md's "How a
# floor is derived" section for the general rule this applies):
# floor = the number of cases that pass DETERMINISTICALLY, never n-1.
#
# Per-case live history across the 23 October 2026 workflow_dispatch runs
# (read from each run's eval-summary.json artifact):
#   name_only_person_stub            —  0/23 (0%)   — permanent miss, athenaeum#1871/#1877
#   name_plus_one_affiliation_line   — 18/23 (78.3%) — the one stochastic case
#   pipeline_metadata_only           — 23/23 (100%)
#   dated_engagement_with_pipeline_metadata — 23/23 (100%)
#   self_declared_filename_artifact  — 23/23 (100%)
#   light_source_summary             — 23/23 (100%)
#
# The previous floor of 5 was NOT a 1-case slack over 6 cases: the permanent
# miss above consumes the one nominal slot a "floor = n-1" scheme grants,
# leaving the aggregate score mathematically equal to the stochastic case's
# own outcome every run — a 6-case floor acting as a 1-trial gate. That
# produced 5 reds in 23 runs (21.7%) on code the stochastic case alone
# decided, not a systemic regression (full reproduction in athenaeum#2231's
# research-findings comment).
#
# Re-floored to 4 = the count of cases observed to pass every single time.
# The permanent miss and the stochastic case now sit OUTSIDE the required
# count rather than silently eating its slack: the floor goes red only when
# at least one of the four always-passing cases ALSO fails in the same run
# as the stochastic case (or when two+ of the four regress together,
# regardless of the other two). Replayed against the exact per-case history
# above, floor=4 scores 0 reds across the same 23 runs — every historical
# red was solely the stochastic case missing while the permanent miss
# consumed the other slot, and floor=4 has a free slot for exactly that.
#
# False-red rate. A simple (and deliberately pessimistic) exact-binomial
# bound, treating all 6 cases as iid at the suite's pooled historical
# accuracy (110/138 = 79.7%): P(X < 4) for X ~ Binomial(6, 0.797) ≈ 10.2%,
# versus ≈35.2% at the old floor of 5 under the same pooled model. The
# TRUE rate is lower still, because the pooled accuracy understates the four
# always-passing cases' real reliability (0 misses in 92 trials) and
# overstates the permanent miss's (0 passes in 23) — see the per-case
# replay above for the structurally honest number (0/23).
#
# False-green / what this floor cannot catch. Floor=4 tolerates exactly one
# failure among {4 always-passing cases, the stochastic case} per run (the
# permanent miss already accounts for the other). A genuine regression that
# turns ONE always-passing case into an always-failing one is caught only
# when the stochastic case ALSO misses that run — at the stochastic case's
# historical 21.7% miss rate, that is only ~22% power against a single-case
# collapse. A regression spanning TWO or more of the always-passing cases
# is always caught (both counted failures exceed the one free slot,
# independent of the stochastic case). In short: this floor is a reliable
# collapse detector for a multi-case regression and a weak one for a
# single-case regression — it cannot, at n=6, distinguish a healthy model
# from one that has silently broken exactly one of the four easy cases.
# See test_audit_retirement_degraded_model_reds_floor (offline, unmarked)
# for a synthetic case proving the floor actually catches a 2+ case
# collapse.
AUDIT_RETIREMENT_FLOOR = 4  # >= 4/6


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
    """Assert the audit-retirement layer meets the aggregate floor.

    See the derivation + false-red/false-green analysis in the comment
    above :data:`AUDIT_RETIREMENT_FLOOR` (issue athenaeum#2231).
    """
    passed, total = eval_session.layer_score(LAYER_AUDIT_RETIREMENT)
    assert total > 0, "audit_retirement eval collected no cases"
    assert passed >= AUDIT_RETIREMENT_FLOOR, (
        f"audit_retirement below aggregate floor: {passed}/{total} "
        f"(need >= {AUDIT_RETIREMENT_FLOOR}). Model: {DEFAULT_CLASSIFY_MODEL}. "
        "Check eval-summary.json for per-case failures."
    )
