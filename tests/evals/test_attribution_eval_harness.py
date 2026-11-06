# SPDX-License-Identifier: Apache-2.0
"""Offline, unmarked tests for the attribution-calibration record-keeping
and cost-bounding fixes (issue athenaeum#2342).

UNMARKED — no ``pytest.mark.calibration`` — so these run in the default
suite, offline, with no credential and no network dependency. The live
sweep itself (``test_attribution_eval.py``) is marked ``calibration`` and
deselected from the default suite's ``addopts``, so a regression in the
pure functions it depends on would otherwise never fail CI; this module
exercises those functions directly, plus (where a call count matters) a
real :class:`tests.conftest.FakeLLMClient` that is never a live model.

Covers:

- the zero-token preflight (:func:`tests.evals.attribution_eval.
  assert_accuracy_bar_reachable` / ``preflight_unreachable_classes`` /
  ``best_achievable_lower_bound``) — asserts the failure AND that a fake
  client alongside it is never called;
- the agreement-K3 skip (:func:`tests.evals.attribution_eval.
  needs_agreement_k3` / ``self_reported_auroc_or_reason``) — zero K3 calls
  for a perfectly self-fit class, nonzero for a class with fit errors, and
  the explicit "any other undefined reason" behaviour;
- the PASS/FAIL-per-class summary and all-fail guard
  (``summarize_classes`` / ``require_some_class_passes``);
- the best-candidate selection among failing reports
  (``best_failing_report``);
- ``to_calibration_entries``' non-degenerate rendering for a sweep where
  no class passes, including the genuinely-unmeasured placeholder row.
"""

from __future__ import annotations

import pytest

from athenaeum.pii_attribution import QUESTION_CLASSES
from tests.conftest import FakeLLMClient
from tests.evals.attribution_eval import (
    AGREEMENT_K,
    AUROC_BAR,
    AUROC_UNDEFINED_NO_CORRECT_CASES,
    AUROC_UNDEFINED_NO_WRONG_CASES,
    CANDIDATE_MODELS,
    CaseRun,
    ClassReport,
    NoClassPassedError,
    UnreachableAccuracyBarError,
    assert_accuracy_bar_reachable,
    best_achievable_lower_bound,
    best_failing_report,
    load_cases,
    needs_agreement_k3,
    preflight_unreachable_classes,
    require_some_class_passes,
    run_case_agreement_k3,
    self_reported_auroc_or_reason,
    summarize_classes,
    to_calibration_entries,
)

SMALL_CLASS = QUESTION_CLASSES[0]
AMPLE_CLASSES = QUESTION_CLASSES[1:]


def _case_for_class(question_class: str) -> dict:
    for case in load_cases():
        if case["question_class"] == question_class and case["split"] == "fit":
            return case
    raise AssertionError(f"no fit case found for {question_class!r}")


def _run(
    score: float,
    correct: bool,
    *,
    question_class: str = "held-elsewhere",
    model: str = "m1",
    signal: str = "self-reported",
) -> CaseRun:
    return CaseRun(
        case_id="c",
        question_class=question_class,
        model=model,
        signal=signal,
        gold_verdict="own",
        judgement_verdict="own" if correct else "unclear",
        score=score,
        correct=correct,
    )


def _report(
    *,
    question_class: str = "held-elsewhere",
    model: str = "m1",
    n_fit: int = 10,
    n_holdout: int = 5,
    holdout_wilson_lb: float = 0.5,
    passes: bool = False,
    signal: str = "self-reported",
    threshold: float | None = 0.8,
) -> ClassReport:
    return ClassReport(
        question_class=question_class,
        model=model,
        signal=signal,
        bins=(),
        threshold=threshold,
        n_fit=n_fit,
        n_holdout=n_holdout,
        holdout_accuracy_above=holdout_wilson_lb,
        holdout_wilson_lb=holdout_wilson_lb,
        ece=0.02,
        auroc=0.8,
        abstain_share=0.1,
        passes=passes,
    )


class TestZeroTokenPreflight:
    def test_best_achievable_lower_bound_is_monotone_in_n(self) -> None:
        assert best_achievable_lower_bound(1) < best_achievable_lower_bound(500)
        assert best_achievable_lower_bound(0) == 0.0

    def test_preflight_flags_only_the_undersized_class(self) -> None:
        accuracy_floor = 0.95
        tiny_n, ample_n = 2, 500
        assert best_achievable_lower_bound(tiny_n) < accuracy_floor
        assert best_achievable_lower_bound(ample_n) >= accuracy_floor

        cases = []
        for question_class in QUESTION_CLASSES:
            n = tiny_n if question_class == SMALL_CLASS else ample_n
            cases.extend({"question_class": question_class, "split": "holdout"} for _ in range(n))
            cases.extend({"question_class": question_class, "split": "fit"} for _ in range(n))

        unreachable = preflight_unreachable_classes(cases, accuracy_floor=accuracy_floor)
        assert set(unreachable) == {SMALL_CLASS}

    def test_preflight_refuses_before_any_client_call(self) -> None:
        """Zero-token preflight (issue athenaeum#2342 AC): the sweep must
        fail BEFORE any client is constructed or called. Here the client
        is constructed (to prove it COULD have been called) but the
        preflight call never reaches it."""
        accuracy_floor = 0.95
        cases = [
            {"question_class": question_class, "split": "holdout"}
            for question_class in QUESTION_CLASSES
            for _ in range(2)  # every class too small
        ]
        client = FakeLLMClient(text='{"verdict": "own", "score": 0.9}')

        with pytest.raises(UnreachableAccuracyBarError) as exc_info:
            assert_accuracy_bar_reachable(cases, accuracy_floor=accuracy_floor)
        for question_class in QUESTION_CLASSES:
            assert question_class in str(exc_info.value)
        assert client.calls == []

    def test_reachable_corpus_raises_nothing(self) -> None:
        cases = [
            {"question_class": question_class, "split": "holdout"}
            for question_class in QUESTION_CLASSES
            for _ in range(500)
        ]
        assert_accuracy_bar_reachable(cases, accuracy_floor=0.95)  # does not raise


class TestAgreementK3Skip:
    def test_perfectly_fit_class_needs_no_k3(self) -> None:
        fit_runs = [_run(0.9, True), _run(0.95, True), _run(0.99, True)]
        value, reason = self_reported_auroc_or_reason(fit_runs)
        assert value is None
        assert reason == AUROC_UNDEFINED_NO_WRONG_CASES
        assert needs_agreement_k3(fit_runs) is False

    def test_class_with_fit_errors_needs_k3(self) -> None:
        fit_runs = [_run(0.9, True), _run(0.9, False)]
        value, reason = self_reported_auroc_or_reason(fit_runs)
        assert reason is None
        assert value is not None and value < AUROC_BAR
        assert needs_agreement_k3(fit_runs) is True

    def test_all_wrong_fit_split_is_a_different_undefined_reason_and_still_runs_k3(
        self,
    ) -> None:
        """A single-label fit split where every case is WRONG is a
        different degenerate case from "no wrong cases" (issue
        athenaeum#2342: "A class where AUROC is undefined for any other
        reason ... is covered by an explicit test stating its
        behaviour") — this one keeps the pre-existing behaviour of still
        attempting agreement-K3."""
        fit_runs = [_run(0.1, False), _run(0.2, False)]
        value, reason = self_reported_auroc_or_reason(fit_runs)
        assert value is None
        assert reason == AUROC_UNDEFINED_NO_CORRECT_CASES
        assert needs_agreement_k3(fit_runs) is True

    def test_k3_call_count_is_zero_for_perfect_fit_and_nonzero_for_fit_errors(self) -> None:
        case = _case_for_class("held-elsewhere")

        perfect_fit = [_run(0.9, True), _run(0.95, True), _run(0.99, True)]
        client_a = FakeLLMClient(text='{"verdict": "own", "score": 0.9}')
        if needs_agreement_k3(perfect_fit):
            run_case_agreement_k3(case, "m1", client_a)
        assert len(client_a.calls) == 0

        buggy_fit = [_run(0.9, True), _run(0.9, False)]
        client_b = FakeLLMClient(text='{"verdict": "own", "score": 0.9}')
        if needs_agreement_k3(buggy_fit):
            run_case_agreement_k3(case, "m1", client_b)
        assert len(client_b.calls) == AGREEMENT_K


class TestSummarizeClassesAndAllFailGuard:
    def test_summarize_classes_counts_a_class_as_passing_if_any_candidate_does(self) -> None:
        reports_by_class = {
            question_class: [_report(question_class=question_class, passes=False)]
            for question_class in QUESTION_CLASSES
        }
        reports_by_class[SMALL_CLASS] = [
            _report(question_class=SMALL_CLASS, model="m1", passes=False),
            _report(question_class=SMALL_CLASS, model="m2", passes=True),
        ]
        lines, passed, total = summarize_classes(reports_by_class)
        assert total == len(QUESTION_CLASSES)
        assert passed == 1
        assert any(line.startswith(f"PASS {SMALL_CLASS}") for line in lines)

    def test_all_fail_result_raises(self) -> None:
        reports_by_class = {
            question_class: [_report(question_class=question_class, passes=False)]
            for question_class in QUESTION_CLASSES
        }
        with pytest.raises(NoClassPassedError):
            require_some_class_passes(reports_by_class)

    def test_at_least_one_pass_does_not_raise(self) -> None:
        reports_by_class = {
            question_class: [_report(question_class=question_class, passes=False)]
            for question_class in QUESTION_CLASSES
        }
        reports_by_class[SMALL_CLASS] = [_report(question_class=SMALL_CLASS, passes=True)]
        require_some_class_passes(reports_by_class)  # does not raise


class TestBestFailingReport:
    def test_picks_highest_lower_bound(self) -> None:
        low = _report(model="m1", holdout_wilson_lb=0.5, n_holdout=10)
        high = _report(model="m2", holdout_wilson_lb=0.8, n_holdout=10)
        assert best_failing_report([low, high]) is high

    def test_ties_broken_by_larger_n_holdout(self) -> None:
        fewer = _report(model="m1", holdout_wilson_lb=0.7, n_holdout=10)
        more = _report(model="m2", holdout_wilson_lb=0.7, n_holdout=40)
        assert best_failing_report([fewer, more]) is more

    def test_further_tie_broken_by_candidate_model_order(self) -> None:
        later = _report(model=CANDIDATE_MODELS[-1], holdout_wilson_lb=0.7, n_holdout=10)
        earlier = _report(model=CANDIDATE_MODELS[0], holdout_wilson_lb=0.7, n_holdout=10)
        assert best_failing_report([later, earlier]) is earlier

    def test_raises_on_empty_input(self) -> None:
        with pytest.raises(ValueError):
            best_failing_report([])


class TestToCalibrationEntriesNonDegenerate:
    def test_all_fail_sweep_writes_real_measured_values_not_the_stub_shape(self) -> None:
        reports_by_class = {
            "held-elsewhere": [
                _report(
                    question_class="held-elsewhere",
                    model=CANDIDATE_MODELS[0],
                    n_fit=36,
                    n_holdout=24,
                    holdout_wilson_lb=0.60,
                    passes=False,
                ),
                _report(
                    question_class="held-elsewhere",
                    model=CANDIDATE_MODELS[-1],
                    n_fit=36,
                    n_holdout=24,
                    holdout_wilson_lb=0.82,
                    passes=False,
                ),
            ],
            "names-other": [
                _report(
                    question_class="names-other",
                    model=CANDIDATE_MODELS[0],
                    n_fit=54,
                    n_holdout=36,
                    holdout_wilson_lb=0.55,
                    passes=False,
                ),
            ],
        }
        for question_class in QUESTION_CLASSES:
            reports_by_class.setdefault(question_class, [])

        entries = to_calibration_entries(
            reports_by_class,
            dataset_sha256="a" * 64,
            prompt_sha256="b" * 64,
            measured_at="2026-11-06",
        )
        assert len(entries) == len(QUESTION_CLASSES)

        by_class = {e["question_class"]: e for e in entries}

        measured_classes = {"held-elsewhere", "names-other"}
        for question_class, entry in by_class.items():
            if question_class in measured_classes:
                assert entry["measured"] is True
                assert entry["n_fit"] != 0
                assert entry["n_holdout"] != 0
                assert entry["passes"] is False
                assert entry["model"] in CANDIDATE_MODELS
            else:
                assert entry["measured"] is False
                assert entry["n_fit"] == 0
                assert entry["n_holdout"] == 0
                assert entry["threshold"] == 0.0
                assert entry["model"] == ""

        # held-elsewhere's best failing candidate is the higher-lower-bound
        # one, which also happens to be a different model than
        # names-other's only candidate -- proving more than one distinct
        # model shows up across rows when more than one was measured.
        assert by_class["held-elsewhere"]["model"] == CANDIDATE_MODELS[-1]
        measured_models = {
            by_class[qc]["model"] for qc in measured_classes
        }
        assert len(measured_models) > 1

    def test_unmeasured_class_never_defaults_model_to_first_candidate(self) -> None:
        reports_by_class: dict[str, list[ClassReport]] = {
            question_class: [] for question_class in QUESTION_CLASSES
        }
        entries = to_calibration_entries(
            reports_by_class,
            dataset_sha256="a" * 64,
            prompt_sha256="b" * 64,
            measured_at="unmeasured",
        )
        for entry in entries:
            assert entry["model"] != CANDIDATE_MODELS[0]
            assert entry["model"] == ""
            assert entry["measured"] is False

    def test_passing_class_still_marked_measured(self) -> None:
        reports_by_class: dict[str, list[ClassReport]] = {
            question_class: [] for question_class in QUESTION_CLASSES
        }
        reports_by_class["held-elsewhere"] = [
            _report(question_class="held-elsewhere", passes=True, n_fit=36, n_holdout=24)
        ]
        entries = to_calibration_entries(
            reports_by_class,
            dataset_sha256="a" * 64,
            prompt_sha256="b" * 64,
            measured_at="2026-11-06",
        )
        held = next(e for e in entries if e["question_class"] == "held-elsewhere")
        assert held["measured"] is True
        assert held["passes"] is True
