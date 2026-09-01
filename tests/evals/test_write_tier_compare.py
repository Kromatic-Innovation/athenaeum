# SPDX-License-Identifier: Apache-2.0
"""Live-API write-knob (Tier-3 CREATE/MERGE) model-tier comparison (athenaeum#1139).

Replays ``tests/evals/data/write_tier_compare/cases.yaml`` once per model in
:data:`tests.evals.tier_compare.CANDIDATE_MODELS` — currently
``claude-sonnet-5`` (the current ``write`` default) and ``claude-haiku-4-5``
(already used for the ``classify``/``topic`` knobs) — via real Anthropic
calls, using the SAME live call sites production uses
(:func:`athenaeum.tiers.tier3_create` / :func:`athenaeum.tiers.tier3_merge`),
with the model chosen per candidate via ``config={"models": {"write": ...}}``
rather than the env/yaml-resolved production default.

Deliberately extends the existing ``tests/evals/`` harness rather than
standing alone (issue athenaeum#1139's "Not verified" section left this decision to
implementation): ``evals.yml`` already picks up every ``pytest.mark.eval``
test under ``tests/evals/`` by marker (not an enumerated file list — see
``docs/evals-inventory.md``'s closing paragraph), so this file needed ZERO
workflow changes to be wired in, and it inherits credential gating
(:func:`tests.evals.harness.live_ready`), the record/replay contract, and
the session-level token-budget guard for free. A standalone harness would
have to reimplement all three.

Deliberately does NOT assert a pass-rate floor across models the way
``test_merge_eval.py`` / ``test_classify_eval.py`` do for their single,
production-bound model: THIS eval's purpose is to MEASURE whether a cheaper
tier is viable, so a weak model failing cases is the informative outcome,
not a bug to gate on. What IS asserted is structural completeness — every
model x case/entity produced exactly one recorded result — so a harness
regression that silently drops a candidate is still caught (see
``test_write_tier_compare`` below).

Skips cleanly (via ``live_ready()``) when no LLM credential is configured —
this lane has none (``ANTHROPIC_API_KEY`` is present but empty); see the
athenaeum#1139 PR body for the follow-up issue that runs this for real.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.evals.harness import (
    LAYER_WRITE_TIER_COMPARE,
    RecordingClient,
    build_live_client,
    live_ready,
)
from tests.evals.tier_compare import (
    CANDIDATE_MODELS,
    CASES_PATH,
    CREATE_KINDS,
    TierRunResult,
    assert_corpus_covers_required_kinds,
    load_cases,
    render_comparison_table,
    run_case,
)

pytestmark = pytest.mark.eval

COMPARISON_TABLE_PATH = CASES_PATH.parent / "comparison_table.md"


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.fixture(scope="module")
def _cases() -> list[dict[str, Any]]:
    cases = load_cases()
    # AC4: never let an empty/under-covered corpus reach a live model call.
    assert_corpus_covers_required_kinds(cases)
    return cases


def _expected_result_count(cases: list[dict[str, Any]]) -> int:
    per_model = 0
    for case in cases:
        if case["scenario_kind"] in CREATE_KINDS:
            per_model += len(case["entities"])
        else:
            per_model += 1
    return per_model * len(CANDIDATE_MODELS)


def test_write_tier_compare(
    _cases: list[dict[str, Any]],
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Run the full corpus against every candidate model; commit the table."""
    inner = build_live_client()
    all_results: list[TierRunResult] = []

    for model in CANDIDATE_MODELS:
        client = RecordingClient(
            inner, record=eval_record, layer=LAYER_WRITE_TIER_COMPARE
        )
        # Wrap ONCE per client/model, before the case loop below — wrapping
        # inside the loop would re-wrap the previous case's wrapper instead
        # of the true underlying create() on the second and later cases.
        original_create = client.messages.create

        def _create(_orig: Any = original_create, **params: Any) -> Any:
            response = _orig(**params)
            eval_session.observe_response(str(params.get("model", "")), response)
            return response

        client.messages.create = _create  # type: ignore[method-assign]

        for case in _cases:
            client.start_case(f"{model}__{case['id']}")
            results = run_case(case, model, client)
            client.end_case()
            all_results.extend(results)
            for r in results:
                eval_session.record_case(
                    LAYER_WRITE_TIER_COMPARE,
                    f"{model}::{case['id']}::{r.entity_name}",
                    expected=str(case.get("expected", {})),
                    observed=(
                        f"in={r.input_tokens} out={r.output_tokens} "
                        f"cost=${r.cost_usd:.4f} t={r.wall_clock_s:.2f}s"
                    ),
                    passed=r.quality_passed,
                    detail=r.quality_detail,
                )

    # Structural completeness — every model x case/entity produced exactly
    # one result, whether it passed, failed on quality, or errored. This is
    # NOT a quality floor (see module docstring): it only proves the harness
    # itself didn't silently drop a candidate or a case.
    expected_count = _expected_result_count(_cases)
    assert len(all_results) == expected_count, (
        f"expected {expected_count} results ({len(CANDIDATE_MODELS)} models x "
        f"corpus entities), got {len(all_results)} — a model or case was "
        "silently dropped"
    )
    for model in CANDIDATE_MODELS:
        assert any(r.model == model for r in all_results), (
            f"no results recorded for candidate model {model!r}"
        )

    # AC5: commit the REAL comparison table (overwrites the stub committed
    # alongside this eval — see comparison_table.md's own header once a real
    # run has produced it).
    table = render_comparison_table(all_results, stub=False)
    COMPARISON_TABLE_PATH.write_text(table, encoding="utf-8")
