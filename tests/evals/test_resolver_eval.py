# SPDX-License-Identifier: Apache-2.0
"""Resolver live-API eval (issue athenaeum#331).

Runs :func:`propose_resolution` against every case in
``tests/evals/data/resolver/cases.yaml`` using a real Opus call.

Action-class taxonomy (per the issue):
    - ``not_a_conflict`` — pass (sequential dated snapshot / restatement)
    - ``keep_pick_winner`` — contradict (a DECISION superseded by a newer one,
      or a precedence-decidable fact — the winner is picked; matches
      ``keep_a`` / ``keep_b`` / ``correct_a`` / ``correct_b``)
    - ``disambiguation`` — escalate (undated mutually-exclusive fact NOT
      resolvable by precedence — the resolver returns
      ``retain_both_with_context`` with a non-empty ``disambiguation_options``
      list, per the resolver system prompt)
    - ``propose_merge`` — merge (a general PREFERENCE rule + narrower exception
      that reads more cleanly as one consolidated memory — the resolver system
      prompt directs this pattern to ``propose_merge``)

Aggregate floor: ≥ 6/9 (see ``RESOLVER_FLOOR`` derivation below; athenaeum#2231
added the 9th case for real slack — the floor value itself is unchanged).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum.contradictions import ContradictionResult
from athenaeum.models import AutoMemoryFile, TokenUsage
from athenaeum.resolutions import (
    DEFAULT_RESOLVE_MODEL,
    MergeProposal,
    ResolutionProposal,
    propose_resolution,
)
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_RESOLVER,
    RecordingClient,
    build_live_client,
    live_ready,
)

pytestmark = pytest.mark.eval


# Floor derivation (issue athenaeum#345, WS3, re-examined by athenaeum#2231): the
# golden set originally held N=8 cases, two per action class (not_a_conflict,
# keep_pick_winner, disambiguation, propose_merge), with a floor of 6 pitched
# as ">= 2-case slack over an all-pass expectation." That framing undercounted
# the real margin: ``decision_conflict_hosting_migration`` (one of the two
# keep_pick_winner cases) is a PERMANENT miss (0/23 across the 23 October
# workflow_dispatch runs, pinned per athenaeum#737/#760 — see the strict xfail
# in tests/test_recorded_fixtures.py), so it always consumes one of the two
# nominal slack slots. The real margin over the 7 non-permanent-miss cases
# was 1 failure in 7 (85.7%), not 2 in 8 — and the research-findings comment
# on athenaeum#2231 found the layer ran at EXACTLY 6/8 (i.e. used that one
# allowance in full) on 2026-10-29.
#
# ``precedence_user_over_unsourced_deadline`` (added by athenaeum#2231) is a
# second case of the SAME proven-reliable keep_pick_winner-via-precedence
# shape as ``precedence_user_over_unsourced_contact``, giving real slack:
# n=9, floor unchanged at 6, so the margin over the 8 non-permanent-miss
# cases becomes 2 failures in 8 — not a floor bump, a genuine widening.
#
# False-red rate. Pessimistic iid exact-binomial bound at the pooled
# historical accuracy (160/184 = 87.0%, issue athenaeum#2231's inventory):
# P(X < 6) for X ~ Binomial(9, 0.870) ≈ 2.1%, down from ≈7.5% at the
# pre-athenaeum#2231 n=8 floor under the same pooled model. As with the other
# two suites re-floored by athenaeum#2231, this pooled figure is a conservative
# bound, not the structural truth: it averages in the permanent miss's 0%,
# so it understates the live cases' real reliability.
#
# False-green / what this floor cannot catch: with the permanent miss always
# failing, floor=6 over n=9 tolerates up to 2 failures among the remaining 8
# live cases (previously 1 of 7). A regression confined to a single live
# case is very likely absorbed by that 2-failure allowance; a regression
# spanning 3+ live cases always reds. This is, like the other two suites,
# a collapse detector at this sample size rather than a fine-grained
# regression detector. See test_resolver_eval_stub.py (offline, unmarked)
# for a synthetic proof exercising the real propose_resolution() call.
RESOLVER_FLOOR = 6  # >= 6/9


def _load_cases() -> list[dict[str, Any]]:
    cases_path = EVAL_DATA_ROOT / "resolver" / "cases.yaml"
    return list(yaml.safe_load(cases_path.read_text(encoding="utf-8")))


def _materialise_members(
    scope_dir: Path,
    case: dict[str, Any],
) -> list[AutoMemoryFile]:
    scope_dir.mkdir(parents=True, exist_ok=True)
    members: list[AutoMemoryFile] = []
    for spec in case["members"]:
        fm = spec.get("frontmatter") or {}
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
            )
        )
    return members


def _detector_result(
    case: dict[str, Any], members: list[AutoMemoryFile]
) -> ContradictionResult:
    det = case["detector"]
    return ContradictionResult(
        detected=True,
        conflict_type=det.get("conflict_type"),
        members_involved=[f"{m.origin_scope}/{m.path.name}" for m in members[:2]],
        conflicting_passages=list(det.get("passages") or []),
        rationale=str(det.get("rationale", "")),
    )


def _classify_proposal(proposal: Any) -> str:
    """Bucket a resolver proposal into the golden-set action classes."""
    if isinstance(proposal, MergeProposal):
        return "propose_merge"
    if not isinstance(proposal, ResolutionProposal):
        return "unknown"
    if proposal.action == "not_a_conflict":
        return "not_a_conflict"
    if (
        proposal.action == "retain_both_with_context"
        and proposal.disambiguation_options
    ):
        return "disambiguation"
    if proposal.action in ("keep_a", "keep_b", "correct_a", "correct_b"):
        return "keep_pick_winner"
    return proposal.action


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_resolver_case(
    case: dict[str, Any],
    tmp_path: Path,
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    scope_dir = tmp_path / f"scope-{case['id']}"
    members = _materialise_members(scope_dir, case)
    detector = _detector_result(case, members)

    inner = build_live_client()
    client = RecordingClient(inner, record=eval_record, layer=LAYER_RESOLVER)
    client.start_case(case["id"])

    original_create = client.messages.create

    def _create(**params: Any) -> Any:
        response = original_create(**params)
        eval_session.observe_response(str(params.get("model", "")), response)
        return response

    client.messages.create = _create  # type: ignore[method-assign]

    usage = TokenUsage()
    proposal = propose_resolution(detector, members, client, usage=usage)
    client.end_case()

    observed_class = _classify_proposal(proposal)
    expected_class = case["expected"]["action_class"]
    passed = observed_class == expected_class

    rationale = getattr(proposal, "rationale", "")[:120]
    eval_session.record_case(
        LAYER_RESOLVER,
        case["id"],
        expected=expected_class,
        observed=observed_class,
        passed=passed,
        detail=f"outcome_class={case.get('outcome_class', '')} rationale={rationale}",
    )


def test_resolver_aggregate_floor(eval_session: Any, _live_ready: None) -> None:
    """Assert the resolver layer meets the aggregate floor.

    Stale pre-athenaeum#345 docstring ("≥ 4/5") corrected by athenaeum#2231 — the
    golden set has held N=8 (now N=9) cases with ``RESOLVER_FLOOR = 6``
    since athenaeum#345; see that constant's derivation comment above for the
    false-red/false-green analysis.
    """
    passed, total = eval_session.layer_score(LAYER_RESOLVER)
    assert total > 0, "resolver eval collected no cases"
    assert passed >= RESOLVER_FLOOR, (
        f"resolver below aggregate floor: {passed}/{total} "
        f"(need ≥ {RESOLVER_FLOOR}). Model: {DEFAULT_RESOLVE_MODEL}. "
        "Check eval-summary.json for per-case failures."
    )
