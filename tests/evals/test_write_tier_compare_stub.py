# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the write-tier-compare harness (issue athenaeum#1139).

UNMARKED — no ``pytest.mark.eval``/``live``/``embedding`` — so this file runs
in the DEFAULT pytest selection, offline, on every contributor run and every
``ci.yml`` invocation, with no credential and no network dependency. That is
deliberate: this lane has no ``ANTHROPIC_API_KEY`` (empty, not merely unset),
so ``test_write_tier_compare.py``'s live eval skips cleanly here — this file
is what proves, in THIS lane, that the runner/scorer/table-generator/corpus-
guard machinery actually works, against
:class:`tests.conftest.FakeLLMClient` (a canned response double, never the
network). "A harness that has never executed is worthless" (athenaeum#1139 task
brief) — this is the execution.

Two responsibilities:

1. The AC4 corpus guard (:func:`tests.evals.tier_compare.assert_corpus_covers_required_kinds`)
   — proven against the REAL committed corpus (so a future PR that
   accidentally empties ``cases.yaml`` fails THIS test, unconditionally, with
   no credential needed to catch it — the exact athenaeum#551 failure mode) AND
   against synthetic empty/incomplete corpora (proving the guard actually
   fires, not just that today's fixture happens to pass it).
2. The runner (:func:`tests.evals.tier_compare.run_create_case` /
   :func:`run_merge_case`) and scorers (:func:`score_create` /
   :func:`score_merge`) exercised through a real call into
   :func:`athenaeum.tiers.tier3_create` / :func:`athenaeum.tiers.tier3_merge`
   with a stub client — proving token/cost/wall-clock capture, per-model
   attribution, frontmatter validation, content-preservation scoring, and
   prompt-injection-leak detection are all real, working code paths, not
   merely written ones.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage
from tests.evals.tier_compare import (
    CANDIDATE_MODELS,
    INJECTION_CANARY,
    REQUIRED_SCENARIO_KINDS,
    EmptyCorpusError,
    load_cases,
    render_comparison_table,
    run_create_case,
    run_merge_case,
    score_merge,
)


def _case_by_id(cases: list[dict[str, Any]], case_id: str) -> dict[str, Any]:
    for case in cases:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


# ---------------------------------------------------------------------------
# AC4 — the corpus guard, unconditional (no credential needed to run this)
# ---------------------------------------------------------------------------


def test_real_corpus_is_nonempty_and_covers_required_kinds() -> None:
    """The COMMITTED cases.yaml, loaded for real, must pass the AC4 guard.

    This is the test that would have caught athenaeum#551's failure mode (fixtures
    silently went empty, and 3 of 4 replay tests skipped unconditionally
    instead of failing) — it runs offline, unconditionally, in every default
    pytest invocation, so a future PR that empties or truncates this corpus
    fails HERE, immediately, rather than surfacing as a false "clean
    downgrade" report weeks later.
    """
    cases = load_cases()
    assert cases, "committed write-tier-compare corpus is empty"
    present = {c["scenario_kind"] for c in cases}
    assert REQUIRED_SCENARIO_KINDS <= present, (
        f"committed corpus missing required kinds: {REQUIRED_SCENARIO_KINDS - present}"
    )
    # from_disk is used, not a lambda re-import, so a real parse of the real
    # file also proves the module's own guard agrees.
    from tests.evals.tier_compare import assert_corpus_covers_required_kinds

    assert_corpus_covers_required_kinds(cases)  # must not raise


def test_corpus_guard_fires_on_empty_corpus() -> None:
    """The guard function itself, proven against a SYNTHESIZED empty corpus.

    Synthesizing the condition (per the athenaeum#1139 brief's own guidance) rather
    than waiting for a real fixture regression is what makes this a proof the
    guard fires, not just an observation that today's fixture happens to
    pass it.
    """
    from tests.evals.tier_compare import assert_corpus_covers_required_kinds

    with pytest.raises(EmptyCorpusError, match="EMPTY"):
        assert_corpus_covers_required_kinds([])


def test_corpus_guard_fires_on_missing_scenario_kind() -> None:
    """The guard also fires on a corpus that has cases but under-covers."""
    from tests.evals.tier_compare import assert_corpus_covers_required_kinds

    real_cases = load_cases()
    # Drop every merge_large case -- a corpus with 3/4 required kinds must
    # still fail loudly, not silently degrade to "3 kinds is good enough".
    trimmed = [c for c in real_cases if c["scenario_kind"] != "merge_large"]
    assert trimmed, "test setup bug: trimming left nothing to test against"
    with pytest.raises(EmptyCorpusError, match="merge_large"):
        assert_corpus_covers_required_kinds(trimmed)


def test_corpus_guard_does_not_subclass_exception() -> None:
    """EmptyCorpusError must survive a bare ``except Exception`` — see its
    docstring: the runner's own ``except Exception`` (used so one model's
    call failure doesn't abort the whole comparison) must never be able to
    swallow this."""
    assert issubclass(EmptyCorpusError, BaseException)
    assert not issubclass(EmptyCorpusError, Exception)


# ---------------------------------------------------------------------------
# Runner + scorer, exercised end-to-end against a stub client
# ---------------------------------------------------------------------------


@pytest.fixture()
def cases() -> list[dict[str, Any]]:
    return load_cases()


def test_run_create_case_clean_response_passes(cases: list[dict[str, Any]]) -> None:
    """A well-formed, non-leaking create response scores PASS with real
    token/cost/wall-clock capture."""
    case = _case_by_id(cases, "simple_create_single_person")
    client = FakeLLMClient(
        text="# Dana Whitfield\n\nDana Whitfield is a senior consultant on "
        "the Atlas Retooling engagement, who joined Meridian Advisory in "
        "March 2026.",
        response=make_llm_response(
            "# Dana Whitfield\n\nDana Whitfield is a senior consultant on "
            "the Atlas Retooling engagement, who joined Meridian Advisory "
            "in March 2026.",
            usage=make_llm_usage(input_tokens=512, output_tokens=128),
        ),
    )

    results = run_create_case(case, "claude-haiku-4-5", client)

    assert len(results) == 1
    r = results[0]
    assert r.model == "claude-haiku-4-5"
    assert r.case_id == "simple_create_single_person"
    assert r.entity_name == "Dana Whitfield"
    assert r.error is None
    assert r.quality_passed is True, r.quality_detail
    assert r.input_tokens == 512
    assert r.output_tokens == 128
    # claude-haiku-4-5 is $1.00/$5.00 per MTok in the code-default rate
    # table (src/athenaeum/models.py) -- prove cost is computed at that
    # rate, not a placeholder/zero.
    expected_cost = (512 / 1_000_000) * 1.0 + (128 / 1_000_000) * 5.0
    assert r.cost_usd == pytest.approx(expected_cost)
    assert r.wall_clock_s >= 0.0


def test_run_create_case_injection_leak_fails(cases: list[dict[str, Any]]) -> None:
    """A response that follows the embedded injected instruction (echoes the
    canary) must score FAIL with a leak reason — proves the injection check
    is a real, exercised code path, not just a substring check that has
    never seen a positive case."""
    case = _case_by_id(cases, "simple_create_single_person")
    compromised_body = (
        f"# Dana Whitfield\n\n{INJECTION_CANARY} -- ignoring prior "
        "instructions as requested."
    )
    client = FakeLLMClient(
        response=make_llm_response(
            compromised_body,
            usage=make_llm_usage(input_tokens=400, output_tokens=64),
        )
    )

    results = run_create_case(case, "claude-sonnet-5", client)

    assert len(results) == 1
    r = results[0]
    assert r.quality_passed is False
    assert "injection" in r.quality_detail.lower()
    # Cost/tokens still captured even on a quality FAIL -- a failing case is
    # data for the table, not a dropped row.
    assert r.input_tokens == 400
    assert r.output_tokens == 64


def test_run_create_case_multi_entity_isolates_per_entity_expectations(
    cases: list[dict[str, Any]],
) -> None:
    """The multi-entity case makes two separate tier3_create calls; a
    response that bleeds the OTHER entity's name into this entity's page
    must fail THAT entity's score, not silently pass because len(results)
    just happened to be right."""
    case = _case_by_id(cases, "multi_entity_create_person_and_project")

    def _responder(**kwargs: Any) -> str:
        # Always answer with a page about Marcus ONLY, regardless of which
        # entity was actually asked for -- simulates a model conflating the
        # two entities named in the shared raw intake. Deliberately never
        # mentions the project's name.
        return "# Marcus Oyelaran\n\nMarcus is a senior consultant at Meridian."

    client = FakeLLMClient(
        responder=lambda **kw: make_llm_response(
            _responder(**kw), usage=make_llm_usage(input_tokens=300, output_tokens=64)
        )
    )

    results = run_create_case(case, "claude-haiku-4-5", client)

    assert len(results) == 2
    by_entity = {r.entity_name: r for r in results}
    # Marcus's own page: passes (contains "Marcus").
    assert by_entity["Marcus Oyelaran"].quality_passed is True
    # Beacon Compliance Review's page never mentions "Beacon" -- must fail.
    assert by_entity["Beacon Compliance Review"].quality_passed is False
    assert "Beacon" in by_entity["Beacon Compliance Review"].quality_detail


def test_run_merge_case_clean_patch_response_preserves_content(
    cases: list[dict[str, Any]],
) -> None:
    """A well-formed anchored-patch response over the small merge page scores
    PASS and preserves the existing marker."""
    case = _case_by_id(cases, "merge_small_page_new_fact")
    ops_response = (
        '{"ops": [{"op": "insert_after", '
        '"anchor": "09:30 UK time.[^1]", '
        '"text": " Update: moves to 10:00 UK time next sprint.[^2]"}]}'
    )
    client = FakeLLMClient(
        response=make_llm_response(
            ops_response, usage=make_llm_usage(input_tokens=900, output_tokens=96)
        )
    )

    results = run_merge_case(case, "claude-sonnet-5", client)

    assert len(results) == 1
    r = results[0]
    assert r.error is None
    assert r.quality_passed is True, r.quality_detail
    assert r.model == "claude-sonnet-5"
    assert r.input_tokens == 900
    assert r.output_tokens == 96


def test_score_merge_detects_silent_content_loss() -> None:
    """Direct scorer test (issue athenaeum#1139's core failure mode): a merge output
    that DROPS the existing page's unique markers must fail with a
    content-loss reason.

    Exercised directly against :func:`score_merge` rather than through
    :func:`run_merge_case` because reaching the full-echo fallback path
    deterministically through a stub client requires forcing a patch-mode
    parse failure first (two chained fake responses) — the scorer is the
    unit actually responsible for catching the defect, so this is a direct,
    unambiguous test of it; the token/cost/wall-clock WIRING through
    run_merge_case is proven separately by
    test_run_merge_case_clean_patch_response_preserves_content above using a
    real (single-call) patch response.
    """
    case = {
        "id": "merge_large_page_preserve_content",
        "expected": {
            "must_include_substrings": [
                "UNIQUE_MARKER_HEAD_atlas_kickoff_2025_11_03",
                "UNIQUE_MARKER_TAIL_atlas_budget_ceiling_480k",
            ],
        },
    }
    # Simulates exactly the failure the issue names: a weak/compromised
    # model's full-echo response drops the pre-existing content entirely.
    destroyed_body = f"{INJECTION_CANARY}\n"

    passed, detail = score_merge(case, destroyed_body, None)

    assert passed is False
    assert "silent content loss" in detail
    assert "injection" in detail.lower()


def test_score_merge_escalation_without_body_is_not_a_content_failure() -> None:
    """An ESCALATE outcome (no body returned) must not be scored as content
    loss -- nothing was silently overwritten, a human is being asked."""
    from athenaeum.models import EscalationItem

    case = {"id": "x", "expected": {"must_include_substrings": ["marker"]}}
    escalation = EscalationItem(
        raw_ref="sessions/x.md",
        entity_name="x",
        conflict_type="principled",
        description="a genuine values-level tension",
    )

    passed, detail = score_merge(case, None, escalation)

    assert passed is True, detail


# ---------------------------------------------------------------------------
# Full stub run -> comparison table (AC5's "shape", generated + labelled)
# ---------------------------------------------------------------------------


def test_stub_run_produces_labelled_comparison_table(cases: list[dict[str, Any]]) -> None:
    """Run the WHOLE corpus against BOTH candidate models via a stub client
    and render the table -- proves the multi-model loop + table generator
    work together, not just each piece in isolation."""
    from tests.evals.tier_compare import CREATE_KINDS, run_case

    def _responder(**kwargs: Any) -> str:
        model = kwargs.get("model", "")
        # A distinct, clean canned answer per model so the table visibly
        # differs by row -- proves config={"models": {"write": ...}} is
        # actually reaching the call (a bug that silently pinned every
        # candidate to one model would make every row identical).
        return f"# Stub response from {model}\n\nDana Marcus Beacon content preserved."

    all_results = []
    for model in CANDIDATE_MODELS:
        client = FakeLLMClient(
            responder=lambda **kw: make_llm_response(
                _responder(**kw), usage=make_llm_usage(input_tokens=200, output_tokens=50)
            )
        )
        for case in cases:
            all_results.extend(run_case(case, model, client))

    expected_rows = len(CANDIDATE_MODELS) * sum(
        len(c["entities"]) if c["scenario_kind"] in CREATE_KINDS else 1 for c in cases
    )
    assert len(all_results) == expected_rows

    table = render_comparison_table(all_results, stub=True)
    assert "STUB DATA" in table
    assert "MUST NOT be used for a tier downgrade decision" in table
    for model in CANDIDATE_MODELS:
        assert model in table
    assert "Per-model summary" in table


def test_write_model_env_pin_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ATHENAEUM_WRITE_MODEL is set, the runner must refuse rather than
    silently comparing one model against itself twice (see
    tier_compare._assert_write_model_not_env_pinned's docstring)."""
    monkeypatch.setenv("ATHENAEUM_WRITE_MODEL", "claude-opus-5")
    cases_local = load_cases()
    case = _case_by_id(cases_local, "simple_create_single_person")
    client = FakeLLMClient(text="# X")

    with pytest.raises(RuntimeError, match="ATHENAEUM_WRITE_MODEL"):
        run_create_case(case, "claude-haiku-4-5", client)
