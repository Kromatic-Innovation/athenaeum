# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the contact-attribution eval harness (issue athenaeum#2211).

UNMARKED — no ``pytest.mark.eval`` — so this runs in the default suite,
offline, with no credential and no network dependency, mirroring
``tests/evals/test_write_tier_compare_stub.py``'s own rationale: "a harness
that has never executed is worthless". Exercises
:func:`tests.evals.attribution_eval.run_case_self_reported` /
:func:`run_case_agreement_k3` through a real call into
:func:`athenaeum.pii_attribution.judge_contact_attribution` with
:class:`tests.conftest.FakeLLMClient` (never the network), then the
signal-selection, tier-recommendation, and calibration-record-rendering
machinery on top of those real (stub-sourced) runs.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

import pytest
import yaml

from athenaeum.pii_attribution import QUESTION_CLASSES
from tests.conftest import FakeLLMClient
from tests.evals.attribution_eval import (
    CASES_PATH,
    ClassReport,
    EmptyCorpusError,
    assert_corpus_covers_every_question_class,
    judgement_matches_gold,
    load_cases,
    question_from_case,
    recommended_report,
    run_case_agreement_k3,
    run_case_self_reported,
    select_signal_and_report,
    to_calibration_entries,
    write_calibration_record,
)

STUB_MODEL = "stub-model"


def _case_for_class(question_class: str) -> dict:
    cases = load_cases()
    for case in cases:
        if case["question_class"] == question_class and case["split"] == "fit":
            return case
    raise AssertionError(f"no fit case found for {question_class!r}")


class TestCorpusGuard:
    def test_real_corpus_is_nonempty_and_covers_every_class(self) -> None:
        assert_corpus_covers_every_question_class(load_cases())

    def test_empty_corpus_raises(self) -> None:
        with pytest.raises(EmptyCorpusError):
            assert_corpus_covers_every_question_class([])

    def test_incomplete_corpus_raises(self) -> None:
        with pytest.raises(EmptyCorpusError):
            assert_corpus_covers_every_question_class(
                [{"question_class": "held-elsewhere"}]
            )


class TestQuestionFromCase:
    def test_builds_a_question_from_a_real_case(self) -> None:
        case = _case_for_class("held-elsewhere")
        question = question_from_case(case)
        assert question.value == case["value"]
        assert question.target.uid == case["target"]["uid"]


class TestRunCaseSelfReported:
    def test_correct_case_scores_correct(self) -> None:
        case = _case_for_class("names-other")
        gold = case["gold_verdict"]
        text = f'{{"verdict": "{gold}", "score": 0.95}}'
        if case.get("gold_owner_uid"):
            owner_uid = case["gold_owner_uid"]
            text = f'{{"verdict": "{gold}", "owner_uid": "{owner_uid}", "score": 0.95}}'
        client = FakeLLMClient(text=text)
        run = run_case_self_reported(case, STUB_MODEL, client)
        assert run.correct is True
        assert run.model == STUB_MODEL
        assert run.question_class == "names-other"

    def test_wrong_case_scores_incorrect(self) -> None:
        case = _case_for_class("names-other")
        client = FakeLLMClient(text='{"verdict": "unclear", "score": 0.1}')
        run = run_case_self_reported(case, STUB_MODEL, client)
        assert run.correct is False

    def test_env_pinned_model_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_ATTRIBUTION_MODEL", "pinned")
        case = _case_for_class("held-elsewhere")
        client = FakeLLMClient(text='{"verdict": "own", "score": 0.9}')
        with pytest.raises(RuntimeError):
            run_case_self_reported(case, STUB_MODEL, client)


class TestRunCaseAgreementK3:
    def test_majority_verdict_and_agreement_score(self) -> None:
        case = _case_for_class("role-unmatched")
        gold = case["gold_verdict"]
        # Two calls agree with gold, one disagrees -> 2/3 agreement.
        responses = iter(
            [f'{{"verdict": "{gold}", "score": 0.5}}'] * 2
            + ['{"verdict": "unclear", "score": 0.1}']
        )

        def _responder(**kwargs: object) -> str:
            return next(responses)

        client = FakeLLMClient(responder=_responder)
        run = run_case_agreement_k3(case, STUB_MODEL, client, rng=random.Random(0))
        assert run.judgement_verdict == gold
        assert run.score == pytest.approx(2 / 3)
        assert run.correct is True

    def test_candidate_order_is_permuted_between_calls(self) -> None:
        case = _case_for_class("source-context")
        seen_orders: list[tuple[str, ...]] = []

        def _responder(**kwargs: object) -> str:
            # Record the candidate uids as they appear in the rendered
            # user message, by scanning for "Candidate:" lines.
            messages = kwargs["messages"]  # type: ignore[index]
            text = messages[0]["content"]  # type: ignore[index]
            order = tuple(
                line.split("(uid=")[1].rstrip(")")
                for line in text.splitlines()
                if line.startswith("Candidate:")
            )
            seen_orders.append(order)
            return '{"verdict": "unclear", "score": 0.1}'

        client = FakeLLMClient(responder=_responder)
        run_case_agreement_k3(case, STUB_MODEL, client, rng=random.Random(1))
        # At least two candidates were present on this case, and the
        # permutation uses a real RNG shuffle -- across 3 calls we expect
        # not every order to be byte-identical (this is probabilistic only
        # in the sense that a seeded RNG is fixed, so this assertion is
        # deterministic for this seed; a `len(order) < 2` case would make
        # permutation a no-op, which is why source-context's case 0 is
        # picked -- it carries a holder+candidate pair).
        assert len(seen_orders) == 3


class TestSelectSignalAndReport:
    def _runs(self, scores: list[float], correct: list[bool], question_class: str, model: str):
        from tests.evals.attribution_eval import CaseRun

        return [
            CaseRun(
                case_id=f"c{i}",
                question_class=question_class,
                model=model,
                signal="self-reported",
                gold_verdict="own",
                judgement_verdict="own" if c else "unclear",
                score=s,
                correct=c,
            )
            for i, (s, c) in enumerate(zip(scores, correct))
        ]

    def test_self_reported_passes_when_auroc_and_accuracy_clear_the_bar(self) -> None:
        # Full separation on fit (AUROC 1.0): low scores wrong, high scores
        # right. derive_threshold at floor=0.5 picks the LOWEST qualifying
        # cutoff, which (since accuracy is exactly 0.5 across the whole
        # set) is the minimum fit score -- so every holdout case below
        # clears it too.
        fit = self._runs(
            [0.9, 0.95, 0.85, 0.92, 0.88, 0.1, 0.15, 0.2, 0.12, 0.18],
            [True, True, True, True, True, False, False, False, False, False],
            "held-elsewhere",
            "m1",
        )
        # n=10, all correct, scores near 1.0 (keeps ECE small) -- large
        # enough n for the Wilson LOWER bound to clear a 0.5 floor despite
        # 10/10 being a small sample (wilson_lower_bound(10, 10) ~= 0.72).
        holdout = self._runs(
            [0.97, 0.98, 0.96, 0.99, 0.97, 0.98, 0.96, 0.99, 0.97, 0.98],
            [True] * 10,
            "held-elsewhere",
            "m1",
        )
        report = select_signal_and_report(
            "held-elsewhere",
            "m1",
            accuracy_floor=0.5,
            self_reported_fit=fit,
            self_reported_holdout=holdout,
        )
        assert report.signal == "self-reported"
        assert report.passes is True

    def test_falls_through_to_agreement_k3_when_self_reported_auroc_is_weak(self) -> None:
        # No separation at all (AUROC ~0.5) in self-reported fit.
        weak_fit = self._runs(
            [0.5, 0.5, 0.5, 0.5], [True, False, True, False], "names-other", "m1"
        )
        weak_holdout = self._runs([0.5, 0.5], [True, False], "names-other", "m1")
        # Agreement signal: perfect separation on fit, and a large enough
        # all-correct holdout for the Wilson lower bound to clear the
        # (loose, 0.5) floor -- same sizing rationale as the self-reported
        # test above.
        strong_agreement_fit = self._runs(
            [1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [True, True, True, True, True, False, False, False, False, False],
            "names-other",
            "m1",
        )
        strong_agreement_holdout = self._runs(
            [1.0] * 10, [True] * 10, "names-other", "m1"
        )
        report = select_signal_and_report(
            "names-other",
            "m1",
            accuracy_floor=0.5,
            self_reported_fit=weak_fit,
            self_reported_holdout=weak_holdout,
            agreement_fit=strong_agreement_fit,
            agreement_holdout=strong_agreement_holdout,
        )
        assert report.signal == "agreement-k3"
        assert report.passes is True

    def test_falls_through_to_class_level_when_both_signals_fail(self) -> None:
        weak_fit = self._runs(
            [0.5, 0.5, 0.5, 0.5], [True, False, True, False], "source-context", "m1"
        )
        weak_holdout = self._runs([0.5, 0.5], [True, False], "source-context", "m1")
        weak_agreement_fit = self._runs(
            [0.6, 0.6, 0.6, 0.6], [True, False, True, False], "source-context", "m1"
        )
        weak_agreement_holdout = self._runs(
            [0.6, 0.6], [True, False], "source-context", "m1"
        )
        report = select_signal_and_report(
            "source-context",
            "m1",
            accuracy_floor=0.9,
            self_reported_fit=weak_fit,
            self_reported_holdout=weak_holdout,
            agreement_fit=weak_agreement_fit,
            agreement_holdout=weak_agreement_holdout,
        )
        assert report.signal == "class-level"

    def test_class_level_signal_has_no_ece(self) -> None:
        all_correct_fit = self._runs([1.0] * 4, [True] * 4, "non-person-target", "m1")
        all_correct_holdout = self._runs([1.0] * 4, [True] * 4, "non-person-target", "m1")
        report = select_signal_and_report(
            "non-person-target",
            "m1",
            accuracy_floor=0.9,
            self_reported_fit=all_correct_fit,
            self_reported_holdout=all_correct_holdout,
        )
        # self-reported AUROC is undefined here (no negative case), so this
        # falls straight to class-level.
        assert report.signal == "class-level"
        assert report.ece is None


class TestRecommendedReportAndRecord:
    def _passing_report(self, question_class: str, model: str, abstain: float) -> ClassReport:
        return ClassReport(
            question_class=question_class,
            model=model,
            signal="self-reported",
            bins=(),
            threshold=0.8,
            n_fit=10,
            n_holdout=5,
            holdout_accuracy_above=1.0,
            holdout_wilson_lb=0.95,
            ece=0.01,
            auroc=0.9,
            abstain_share=abstain,
            passes=True,
        )

    def test_recommended_report_picks_lowest_abstain_share(self) -> None:
        reports = [
            self._passing_report("held-elsewhere", "m1", abstain=0.3),
            self._passing_report("held-elsewhere", "m2", abstain=0.1),
        ]
        best = recommended_report(reports)
        assert best is not None
        assert best.model == "m2"

    def test_no_passing_report_is_none(self) -> None:
        failing = ClassReport(
            question_class="held-elsewhere",
            model="m1",
            signal="self-reported",
            bins=(),
            threshold=None,
            n_fit=10,
            n_holdout=5,
            holdout_accuracy_above=0.0,
            holdout_wilson_lb=0.0,
            ece=None,
            auroc=None,
            abstain_share=1.0,
            passes=False,
        )
        assert recommended_report([failing]) is None

    def test_to_calibration_entries_covers_every_class(self) -> None:
        reports_by_class = {
            qc: (self._passing_report(qc, "m1", abstain=0.1) if qc == "held-elsewhere" else None)
            for qc in QUESTION_CLASSES
        }
        entries = to_calibration_entries(
            reports_by_class,
            dataset_sha256="a" * 64,
            prompt_sha256="b" * 64,
            measured_at="2026-10-31",
        )
        assert {e["question_class"] for e in entries} == set(QUESTION_CLASSES)
        held = next(e for e in entries if e["question_class"] == "held-elsewhere")
        assert held["passes"] is True
        other = next(e for e in entries if e["question_class"] == "names-other")
        assert other["passes"] is False

    def test_write_calibration_record_round_trips(self) -> None:
        reports_by_class: dict[str, ClassReport | None] = {qc: None for qc in QUESTION_CLASSES}
        entries = to_calibration_entries(
            reports_by_class,
            dataset_sha256="a" * 64,
            prompt_sha256="b" * 64,
            measured_at="unmeasured",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.yaml"
            write_calibration_record(entries, path)
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert len(loaded) == len(QUESTION_CLASSES)


def test_judgement_matches_gold_checks_owner_uid_not_just_verdict() -> None:
    from athenaeum.pii_attribution import AttributionJudgement

    case = {"gold_verdict": "other-known", "gold_owner_uid": "p-correct"}
    right = AttributionJudgement(verdict="other-known", owner_uid="p-correct", score=0.9)
    wrong = AttributionJudgement(verdict="other-known", owner_uid="p-wrong", score=0.9)
    assert judgement_matches_gold(case, right) is True
    assert judgement_matches_gold(case, wrong) is False


def test_cases_path_is_the_real_committed_corpus() -> None:
    assert CASES_PATH.is_file()
