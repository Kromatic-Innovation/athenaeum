# SPDX-License-Identifier: Apache-2.0
"""Contact-attribution calibration eval harness (issue athenaeum#2211).

Design ``docs/design/attribution-verification.md`` §12. Built as an
EXTENSION of the existing ``tests/evals/`` harness
(:mod:`tests.evals.harness`), the same "extend, do not stand alone" shape
``tests/evals/tier_compare.py`` already established for the write-knob
tier comparison — this module reuses that harness's record/replay
contract, run-level token guard, and CI wiring (``evals.yml`` selects
``pytest tests/evals/ -m eval`` by marker across the whole directory, not
an enumerated file list) rather than duplicating any of it.

What's here:

- :func:`load_cases` / :func:`question_from_case` — read the committed
  synthetic corpus (``tests/evals/data/attribution/cases.yaml``) into
  :class:`athenaeum.pii_attribution.AttributionQuestion` objects.
- :func:`run_case_self_reported` / :func:`run_case_agreement_k3` — call
  :func:`athenaeum.pii_attribution.judge_contact_attribution` for one case,
  the way :func:`tests.evals.tier_compare.run_create_case` calls
  ``tier3_create`` — per-candidate-model, via ``config={"models":
  {"attribution": <model>}}``, with the same env-pin guard
  (:func:`_assert_attribution_model_not_env_pinned`,
  mirroring :func:`tests.evals.tier_compare._assert_write_model_not_env_pinned`).
- :func:`select_signal_and_report` — design §6.5/§12.3's signal-selection
  and pass-bar logic, pure over already-collected :class:`CaseRun` lists.
  This is the part the issue calls out as fully offline-testable, and
  ``tests/test_attribution_eval_selection.py`` covers every branch on
  canned data.
- :func:`to_calibration_entries` / :func:`write_calibration_record` — the
  shape the shipped record (design §12.4) is written in.

Two consumers, mirroring ``tier_compare``'s own split:

- ``test_attribution_eval.py`` (``pytest.mark.eval``, live Anthropic
  calls, skips cleanly without credentials) — the REAL calibration run.
  **No build lane has authority to dispatch this** (token spend is an
  irreversible external action outside that authority) — it runs only via
  an operator's local invocation or CI's ``evals.yml`` ``workflow_dispatch``.
- ``test_attribution_eval_stub.py`` (unmarked, offline, always runs) —
  proves the runner/selection/report-rendering machinery actually works,
  against :class:`tests.conftest.FakeLLMClient`. Never produces real
  calibration numbers.
"""

from __future__ import annotations

import dataclasses
import os
import random
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from athenaeum.config import DEFAULT_CLASSIFY_MODEL, DEFAULT_VERIFY_MODEL
from athenaeum.models import TokenUsage
from athenaeum.pii_attribution import (
    QUESTION_CLASSES,
    SIGNAL_AGREEMENT_K3,
    SIGNAL_CLASS_LEVEL,
    SIGNAL_SELF_REPORTED,
    AttributionJudgement,
    AttributionQuestion,
    CheckEvidence,
    EntityBrief,
    judge_contact_attribution,
)
from athenaeum.tiers import DEFAULT_WRITE_MODEL
from tests.evals.harness import EVAL_DATA_ROOT
from tests.evals.metrics import (
    abstain_share,
    auroc,
    derive_threshold,
    expected_calibration_error,
    reliability_table,
    wilson_lower_bound,
)

CASES_PATH = EVAL_DATA_ROOT / "attribution" / "cases.yaml"

#: AC: "Candidates come from the classify, write and verify knob defaults,
#: not hard-coded ids." Read live from each knob's own module constant, so
#: a future default change is picked up automatically rather than going
#: stale here. Deduplicated (order-preserving) because today's write and
#: verify defaults happen to coincide (both claude-sonnet-5) -- that is a
#: property of current config, not something this eval should hide by
#: silently running the same model twice.
CANDIDATE_MODELS: tuple[str, ...] = tuple(
    dict.fromkeys((DEFAULT_CLASSIFY_MODEL, DEFAULT_WRITE_MODEL, DEFAULT_VERIFY_MODEL))
)

#: Design §6.5: self-reported confidence is used only if its AUROC clears
#: this bar on the fit split.
AUROC_BAR = 0.75

#: Design §6.5/§12.2: agreement scoring over K permuted-candidate-order calls.
AGREEMENT_K = 3

#: Design §12.3's pass bar (the two fixed thresholds; the accuracy floor
#: itself is the operator-configurable
#: ``athenaeum.config.resolve_attribution_accuracy_floor``, not a constant
#: here).
MAX_ECE = 0.05
MAX_ABSTAIN_SHARE = 0.30

#: issue athenaeum#2342 / athenaeum#2344 seam: the pass bar
#: (:func:`_report_for_signal`) and the zero-token preflight below
#: (:func:`assert_accuracy_bar_reachable`) must read the IDENTICAL bound
#: function, so both reach it through this one named reference rather than
#: each calling :func:`tests.evals.metrics.wilson_lower_bound` directly.
#: athenaeum#2344 swaps this to a one-sided bound in exactly this one place;
#: nothing here depends on which bound it points to.
PASS_BAR_LOWER_BOUND = wilson_lower_bound


class EmptyCorpusError(BaseException):
    """The attribution corpus is empty or under-covers the question classes.

    Deliberately a :class:`BaseException` subclass — mirrors
    :class:`tests.evals.tier_compare.EmptyCorpusError` — so a broad
    ``except Exception`` elsewhere in a runner can never accidentally
    swallow it and report a comparison off zero evidence.
    """


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the corpus from *path* (default: the committed cases.yaml)."""
    p = path or CASES_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    return list(raw) if raw else []


def assert_corpus_covers_every_question_class(cases: list[dict[str, Any]]) -> None:
    """Raise :class:`EmptyCorpusError` unless *cases* is non-empty and covers
    every entry in :data:`athenaeum.pii_attribution.QUESTION_CLASSES`."""
    if not cases:
        raise EmptyCorpusError(
            "attribution eval corpus is EMPTY (0 cases) — refusing to run or "
            f"report a calibration off zero fixtures. Re-seed {CASES_PATH}."
        )
    present = {c.get("question_class") for c in cases}
    missing = set(QUESTION_CLASSES) - present
    if missing:
        raise EmptyCorpusError(
            f"attribution eval corpus is missing question_class value(s): "
            f"{sorted(missing)} (has {sorted(present)})."
        )


class UnreachableAccuracyBarError(BaseException):
    """At least one question class's held-out split is too small to reach
    the accuracy floor even if every held-out case scored correctly
    (issue athenaeum#2342).

    Deliberately a :class:`BaseException` subclass, mirroring
    :class:`EmptyCorpusError` above, so a broad ``except Exception``
    elsewhere can never swallow it and let a sweep spend tokens it was
    already certain to waste.
    """


def best_achievable_lower_bound(n_holdout: int) -> float:
    """The best :data:`PASS_BAR_LOWER_BOUND` a class can reach with
    *n_holdout* held-out cases, scoring every single one of them correct —
    the zero-token preflight's "can this even pass" arithmetic (issue
    athenaeum#2342). Uses the SAME bound function the pass bar itself uses
    (:func:`_report_for_signal`), via the module-level
    :data:`PASS_BAR_LOWER_BOUND` reference, so this preflight can never
    disagree with the real measurement about what "reachable" means.
    """
    return PASS_BAR_LOWER_BOUND(n_holdout, n_holdout) if n_holdout else 0.0


def preflight_unreachable_classes(
    cases: list[dict[str, Any]], *, accuracy_floor: float
) -> dict[str, float]:
    """For every question class in *cases*, the best achievable lower
    bound given that class's held-out split size
    (:func:`best_achievable_lower_bound`), filtered down to the classes
    that CANNOT reach *accuracy_floor* even in the best case.

    Pure and zero-cost — no client is constructed and no case is run — so
    a caller can refuse an entire sweep before spending a single token
    (issue athenaeum#2342: the run that spent ~3.5M tokens was certain to
    fail this check — held-out splits of 24/36/48 cases against a 0.95
    floor cannot reach a Wilson lower bound of 0.95 even at 100% holdout
    accuracy).
    """
    holdout_counts: Counter[str] = Counter(
        c["question_class"] for c in cases if c.get("split") == "holdout"
    )
    unreachable: dict[str, float] = {}
    for question_class in QUESTION_CLASSES:
        best = best_achievable_lower_bound(holdout_counts.get(question_class, 0))
        if best < accuracy_floor:
            unreachable[question_class] = best
    return unreachable


def assert_accuracy_bar_reachable(
    cases: list[dict[str, Any]], *, accuracy_floor: float
) -> None:
    """Raise :class:`UnreachableAccuracyBarError` naming every question
    class whose held-out split cannot reach *accuracy_floor*
    (:func:`preflight_unreachable_classes`).

    Call this before constructing any client or running any case — the
    whole point (issue athenaeum#2342) is refusing the run at zero token
    cost when the corpus is already too small to pass.
    """
    unreachable = preflight_unreachable_classes(cases, accuracy_floor=accuracy_floor)
    if unreachable:
        detail = ", ".join(
            f"{qc} (best achievable lower bound {bound:.4f})"
            for qc, bound in sorted(unreachable.items())
        )
        raise UnreachableAccuracyBarError(
            f"accuracy floor {accuracy_floor} is unreachable at today's "
            f"corpus size for: {detail}. Refusing to spend tokens on a "
            "sweep that cannot pass — grow the corpus (athenaeum#2341) or "
            "lower the floor before re-running."
        )


def _brief_from_dict(raw: dict[str, Any]) -> EntityBrief:
    return EntityBrief(
        uid=str(raw["uid"]),
        name=str(raw["name"]),
        type=raw.get("type"),
        aliases=tuple(raw.get("aliases") or ()),
        employer_domain_hints=tuple(raw.get("employer_domain_hints") or ()),
    )


def _check_from_dict(raw: dict[str, Any]) -> CheckEvidence:
    return CheckEvidence(
        check_id=str(raw["check_id"]),
        uids=tuple(raw.get("uids") or ()),
        label=raw.get("label"),
    )


def question_from_case(case: dict[str, Any]) -> AttributionQuestion:
    """Build the :class:`AttributionQuestion` one corpus case describes."""
    return AttributionQuestion(
        value=str(case["value"]),
        target=_brief_from_dict(case["target"]),
        holders=tuple(_brief_from_dict(h) for h in case.get("holders") or ()),
        candidates=tuple(_brief_from_dict(c) for c in case.get("candidates") or ()),
        checks=tuple(_check_from_dict(c) for c in case.get("checks") or ()),
        source_excerpt=case.get("source_excerpt"),
    )


def judgement_matches_gold(case: dict[str, Any], judgement: AttributionJudgement) -> bool:
    """Score *judgement* against *case*'s gold label.

    Exact verdict match, PLUS ``owner_uid`` for ``other-known``/
    ``same-entity`` and ``alias_relation`` for ``alias`` — a verdict that
    is right in KIND but names the wrong owner is scored wrong, not half
    credit (design §12.3's accuracy is of the whole answer, not just the
    verdict label).
    """
    if judgement.verdict != case["gold_verdict"]:
        return False
    if case["gold_verdict"] in ("other-known", "same-entity"):
        return judgement.owner_uid == case.get("gold_owner_uid")
    if case["gold_verdict"] == "alias":
        return judgement.alias_relation == case.get("gold_alias_relation")
    return True


def _assert_attribution_model_not_env_pinned() -> None:
    """Refuse to run if ``ATHENAEUM_ATTRIBUTION_MODEL`` is set.

    Mirrors :func:`tests.evals.tier_compare._assert_write_model_not_env_pinned`:
    this eval selects each candidate's model via ``config={"models":
    {"attribution": <model>}}``, which env overrides
    (:func:`athenaeum.pii_attribution._get_attribution_model`'s
    precedence) — a set env var would silently make every candidate
    resolve to the SAME model, corrupting the comparison.
    """
    pinned = os.environ.get("ATHENAEUM_ATTRIBUTION_MODEL")
    if pinned:
        raise RuntimeError(
            f"ATHENAEUM_ATTRIBUTION_MODEL={pinned!r} is set — this eval "
            "selects the model per candidate via config, which env "
            "overrides. Unset it before running the attribution eval."
        )


@dataclasses.dataclass(frozen=True)
class CaseRun:
    """One (case, model, signal) run's outcome.

    ``split``, ``owner_uid`` and ``alias_relation`` (issue athenaeum#2342)
    carry the values the calibration record-keeper AC asks for, as the
    model returned them (``owner_uid``/``alias_relation`` are ``None``
    when the judgement didn't set them). ``raw_verdicts`` is empty for a
    self-reported run; for an agreement-K3 run it holds the ``k``
    individual verdicts that ``judgement_verdict``/``score`` were
    summarized FROM (majority verdict / agreement share), so a caller can
    record each individual run as its own row without re-deriving them.
    """

    case_id: str
    question_class: str
    model: str
    signal: str
    gold_verdict: str
    judgement_verdict: str
    score: float
    correct: bool
    split: str = ""
    owner_uid: str | None = None
    alias_relation: str | None = None
    raw_verdicts: tuple[str, ...] = ()


def run_case_self_reported(
    case: dict[str, Any],
    model: str,
    client: Any,
    *,
    usage: TokenUsage | None = None,
) -> CaseRun:
    """One self-reported-confidence run of *case* against *model*."""
    _assert_attribution_model_not_env_pinned()
    question = question_from_case(case)
    config = {"models": {"attribution": model}}
    judgement = judge_contact_attribution(question, client, config=config, usage=usage)
    return CaseRun(
        case_id=str(case["id"]),
        question_class=str(case["question_class"]),
        model=model,
        signal=SIGNAL_SELF_REPORTED,
        gold_verdict=str(case["gold_verdict"]),
        judgement_verdict=judgement.verdict,
        score=judgement.score,
        correct=judgement_matches_gold(case, judgement),
        split=str(case.get("split", "")),
        owner_uid=judgement.owner_uid,
        alias_relation=judgement.alias_relation,
    )


def run_case_agreement_k3(
    case: dict[str, Any],
    model: str,
    client: Any,
    *,
    k: int = AGREEMENT_K,
    usage: TokenUsage | None = None,
    rng: random.Random | None = None,
) -> CaseRun:
    """*k* permuted-candidate-order runs of *case*; score = share agreeing
    with the majority verdict (design §6.5's signal 2).

    The newer models reject sampling parameters, so a temperature cannot
    be set — permuting ``question.candidates`` between calls is what makes
    the ``k`` calls independent (design §6.5).
    """
    _assert_attribution_model_not_env_pinned()
    rng = rng or random.Random(f"{case['id']}:{model}")
    base_question = question_from_case(case)
    config = {"models": {"attribution": model}}

    verdicts: list[str] = []
    first_judgement: AttributionJudgement | None = None
    for _ in range(k):
        shuffled = list(base_question.candidates)
        rng.shuffle(shuffled)
        permuted = dataclasses.replace(base_question, candidates=tuple(shuffled))
        judgement = judge_contact_attribution(permuted, client, config=config, usage=usage)
        verdicts.append(judgement.verdict)
        if first_judgement is None:
            first_judgement = judgement

    assert first_judgement is not None  # k >= 1 by construction
    majority_verdict, majority_count = Counter(verdicts).most_common(1)[0]
    agreement_score = majority_count / len(verdicts)
    representative = dataclasses.replace(
        first_judgement, verdict=majority_verdict, score=agreement_score
    )
    return CaseRun(
        case_id=str(case["id"]),
        question_class=str(case["question_class"]),
        model=model,
        signal=SIGNAL_AGREEMENT_K3,
        gold_verdict=str(case["gold_verdict"]),
        judgement_verdict=majority_verdict,
        score=agreement_score,
        correct=judgement_matches_gold(case, representative),
        split=str(case.get("split", "")),
        owner_uid=representative.owner_uid,
        alias_relation=representative.alias_relation,
        raw_verdicts=tuple(verdicts),
    )


# ---------------------------------------------------------------------------
# Signal selection + pass bar (design §6.5, §12.3, §12.4) — pure, offline
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ClassReport:
    """One (question_class, model)'s calibration measurement — the shape
    :func:`to_calibration_entries` turns into the shipped YAML record."""

    question_class: str
    model: str
    signal: str
    bins: tuple[tuple[float, float, float], ...]
    threshold: float | None
    n_fit: int
    n_holdout: int
    holdout_accuracy_above: float
    holdout_wilson_lb: float
    ece: float | None
    auroc: float | None
    abstain_share: float
    passes: bool


def _report_for_signal(
    question_class: str,
    model: str,
    signal: str,
    fit_runs: list[CaseRun],
    holdout_runs: list[CaseRun],
    *,
    accuracy_floor: float,
    auroc_value: float | None,
    has_ece: bool,
) -> ClassReport:
    fit_scores = [r.score for r in fit_runs]
    fit_correct = [r.correct for r in fit_runs]
    threshold = derive_threshold(fit_scores, fit_correct, accuracy_floor)

    holdout_scores = [r.score for r in holdout_runs]
    holdout_correct = [r.correct for r in holdout_runs]

    if threshold is None:
        return ClassReport(
            question_class=question_class,
            model=model,
            signal=signal,
            bins=(),
            threshold=None,
            n_fit=len(fit_runs),
            n_holdout=len(holdout_runs),
            holdout_accuracy_above=0.0,
            holdout_wilson_lb=0.0,
            ece=None,
            auroc=auroc_value,
            abstain_share=1.0,
            passes=False,
        )

    at_or_above = [c for s, c in zip(holdout_scores, holdout_correct) if s >= threshold]
    n_above = len(at_or_above)
    n_correct_above = sum(1 for c in at_or_above if c)
    accuracy_above = n_correct_above / n_above if n_above else 0.0
    wilson_lb = PASS_BAR_LOWER_BOUND(n_correct_above, n_above)
    ece = expected_calibration_error(holdout_scores, holdout_correct) if has_ece else None
    abstain = abstain_share(holdout_scores, threshold)
    bins = tuple(reliability_table(holdout_scores, holdout_correct)) if has_ece else ()

    passes = (
        wilson_lb >= accuracy_floor
        and (ece is None or ece <= MAX_ECE)
        and abstain <= MAX_ABSTAIN_SHARE
    )

    return ClassReport(
        question_class=question_class,
        model=model,
        signal=signal,
        bins=bins,
        threshold=threshold,
        n_fit=len(fit_runs),
        n_holdout=len(holdout_runs),
        holdout_accuracy_above=accuracy_above,
        holdout_wilson_lb=wilson_lb,
        ece=ece,
        auroc=auroc_value,
        abstain_share=abstain,
        passes=passes,
    )


#: issue athenaeum#2342: the reason :func:`tests.evals.metrics.auroc` raised
#: on the fit split, distinguishing "no wrong case at all" (the one case
#: design §12.2 says needs agreement-K3 LEAST — a perfectly self-fit class)
#: from any other single-label degenerate split.
AUROC_UNDEFINED_NO_WRONG_CASES = "no-wrong-cases"
AUROC_UNDEFINED_NO_CORRECT_CASES = "no-correct-cases"


def self_reported_auroc_or_reason(fit_runs: list[CaseRun]) -> tuple[float | None, str | None]:
    """AUROC of *fit_runs*' (score, correct) pairs on the fit split, or
    ``(None, reason)`` when :func:`tests.evals.metrics.auroc` raises
    because the split is single-label.

    *reason* is :data:`AUROC_UNDEFINED_NO_WRONG_CASES` when every run in
    *fit_runs* is correct (no negative/incorrect case), or
    :data:`AUROC_UNDEFINED_NO_CORRECT_CASES` for the opposite single-label
    split (every run wrong). Both are "a single-label fit split"; this
    function only tells them apart — it does not decide what a caller
    should do about either.
    """
    correct = [r.correct for r in fit_runs]
    try:
        return auroc([r.score for r in fit_runs], correct), None
    except ValueError:
        if correct and all(correct):
            return None, AUROC_UNDEFINED_NO_WRONG_CASES
        return None, AUROC_UNDEFINED_NO_CORRECT_CASES


def needs_agreement_k3(fit_runs: list[CaseRun]) -> bool:
    """Design §12.2's cost-bounding rule, reason-aware (issue
    athenaeum#2342): agreement-K3 runs only when self-reported confidence's
    AUROC on the fit split is EITHER defined and below :data:`AUROC_BAR`,
    OR undefined for a reason other than "no wrong cases".

    A fit split with zero incorrect cases is already perfectly self-fit —
    it is the class K3 helps least, so that one specific undefined-AUROC
    reason (:data:`AUROC_UNDEFINED_NO_WRONG_CASES`) routes straight past
    K3 (the caller falls through to the class-level signal, since
    :func:`select_signal_and_report` already does that whenever no
    agreement runs are supplied). Any other undefined reason (for example
    :data:`AUROC_UNDEFINED_NO_CORRECT_CASES`) keeps the pre-existing
    behaviour of still attempting agreement-K3.
    """
    value, reason = self_reported_auroc_or_reason(fit_runs)
    if reason == AUROC_UNDEFINED_NO_WRONG_CASES:
        return False
    return value is None or value < AUROC_BAR


def select_signal_and_report(
    question_class: str,
    model: str,
    *,
    accuracy_floor: float,
    self_reported_fit: list[CaseRun],
    self_reported_holdout: list[CaseRun],
    agreement_fit: list[CaseRun] | None = None,
    agreement_holdout: list[CaseRun] | None = None,
) -> ClassReport:
    """Design §6.5's signal choice, in order: self-reported if its AUROC is
    at least :data:`AUROC_BAR`, else agreement-K3 if THAT passes, else
    class-level.

    *agreement_fit*/*agreement_holdout* are optional — a caller that never
    ran agreement scoring for this (class, model) (because self-reported
    already cleared the bar, design §12.2's cost-bounding rule) simply
    omits them, and this function falls straight from self-reported to
    class-level.
    """
    try:
        self_auroc: float | None = auroc(
            [r.score for r in self_reported_fit], [r.correct for r in self_reported_fit]
        )
    except ValueError:
        self_auroc = None

    if self_auroc is not None and self_auroc >= AUROC_BAR:
        return _report_for_signal(
            question_class,
            model,
            SIGNAL_SELF_REPORTED,
            self_reported_fit,
            self_reported_holdout,
            accuracy_floor=accuracy_floor,
            auroc_value=self_auroc,
            has_ece=True,
        )

    if agreement_fit and agreement_holdout:
        try:
            agreement_auroc: float | None = auroc(
                [r.score for r in agreement_fit], [r.correct for r in agreement_fit]
            )
        except ValueError:
            agreement_auroc = None
        agreement_report = _report_for_signal(
            question_class,
            model,
            SIGNAL_AGREEMENT_K3,
            agreement_fit,
            agreement_holdout,
            accuracy_floor=accuracy_floor,
            auroc_value=agreement_auroc,
            has_ece=True,
        )
        if agreement_report.passes:
            return agreement_report

    # Class-level fallback (design §6.5): the score degenerates to a
    # constant, so "threshold" is really just "does this class auto-file
    # at all" — derive_threshold([1.0, 1.0, ...], correct, floor) returns
    # 1.0 when accuracy already clears the floor and None otherwise, which
    # is exactly that binary choice.
    class_level_fit = [dataclasses.replace(r, score=1.0) for r in self_reported_fit]
    class_level_holdout = [dataclasses.replace(r, score=1.0) for r in self_reported_holdout]
    return _report_for_signal(
        question_class,
        model,
        SIGNAL_CLASS_LEVEL,
        class_level_fit,
        class_level_holdout,
        accuracy_floor=accuracy_floor,
        auroc_value=None,
        has_ece=False,
    )


def recommended_report(reports: list[ClassReport]) -> ClassReport | None:
    """Design §12.4: among *reports* that pass, the one with the lowest
    abstain share; ties go to whichever sorts first in
    :data:`CANDIDATE_MODELS` order (the cheaper tier, since that tuple is
    ordered classify/write/verify — cheapest to costliest on today's
    pricing). ``None`` when nothing passes."""
    passing = [r for r in reports if r.passes]
    if not passing:
        return None
    model_rank = {model: i for i, model in enumerate(CANDIDATE_MODELS)}
    return min(
        passing,
        key=lambda r: (r.abstain_share, model_rank.get(r.model, len(CANDIDATE_MODELS))),
    )


def best_failing_report(reports: list[ClassReport]) -> ClassReport:
    """Among *reports* (none of which need have passed — a caller that
    already has a passing report should use :func:`recommended_report`
    instead), the measured report CLOSEST to reaching the bar (issue
    athenaeum#2342's "best candidate" selection among failing reports).

    Choice rule, deterministic: highest Wilson holdout lower bound first;
    ties broken by the larger held-out sample (more evidence backing the
    same bound); a further tie falls back to :data:`CANDIDATE_MODELS`
    order (the cheaper tier), the same tie-break :func:`recommended_report`
    uses, so repeated runs over the same inputs always pick the same row.

    Raises :class:`ValueError` on an empty *reports* — callers only reach
    this function when at least one candidate was measured.
    """
    if not reports:
        raise ValueError("best_failing_report requires at least one ClassReport")
    model_rank = {model: i for i, model in enumerate(CANDIDATE_MODELS)}
    return max(
        reports,
        key=lambda r: (
            r.holdout_wilson_lb,
            r.n_holdout,
            -model_rank.get(r.model, len(CANDIDATE_MODELS)),
        ),
    )


def summarize_classes(
    reports_by_class: dict[str, list[ClassReport]],
) -> tuple[list[str], int, int]:
    """One ``PASS``/``FAIL`` line per question class — a class passes if
    ANY of its candidate reports does — plus the overall ``(passed,
    total)`` count (issue athenaeum#2342: the sweep must emit this and
    fail when ``passed == 0`` rather than exiting quietly). Pure and
    offline-testable: takes already-collected :class:`ClassReport` lists,
    makes no client call.
    """
    lines: list[str] = []
    passed = 0
    for question_class in QUESTION_CLASSES:
        candidates = reports_by_class.get(question_class) or []
        class_passes = any(r.passes for r in candidates)
        if class_passes:
            passed += 1
        lines.append(f"{'PASS' if class_passes else 'FAIL'} {question_class}")
    return lines, passed, len(QUESTION_CLASSES)


class NoClassPassedError(BaseException):
    """Every question class failed the calibration bar (issue
    athenaeum#2342): an all-false sweep must fail loudly, not pass
    unnoticed the way the original (unasserted) test did. Deliberately a
    :class:`BaseException` subclass, mirroring :class:`EmptyCorpusError`
    above.
    """


def require_some_class_passes(reports_by_class: dict[str, list[ClassReport]]) -> None:
    """Raise :class:`NoClassPassedError` when :func:`summarize_classes`
    reports zero classes passing."""
    lines, passed, total = summarize_classes(reports_by_class)
    if passed == 0:
        raise NoClassPassedError(
            f"every question class failed the calibration bar (0/{total}); "
            "refusing to report a silent pass. Per-class results:\n"
            + "\n".join(lines)
        )


def render_class_report_line(report: ClassReport) -> str:
    """One text line summarizing *report* (issue athenaeum#2342: "every
    candidate ClassReport ... printed to the test output as one line per
    report")."""
    return (
        f"{report.question_class} model={report.model} signal={report.signal} "
        f"n_fit={report.n_fit} n_holdout={report.n_holdout} "
        f"threshold={report.threshold} "
        f"lower_bound={report.holdout_wilson_lb:.4f} passes={report.passes}"
    )


# ---------------------------------------------------------------------------
# Shipped-record rendering (design §12.4)
# ---------------------------------------------------------------------------


def to_calibration_entries(
    reports_by_class: dict[str, list[ClassReport]],
    *,
    dataset_sha256: str,
    prompt_sha256: str,
    measured_at: str,
) -> list[dict[str, Any]]:
    """Render every question class's candidate :class:`ClassReport`\\ s
    into the shipped YAML shape (design §12.4), one entry per class.

    A class with at least one measured candidate ALWAYS writes a real
    measurement: the recommended (passing) report when one exists
    (:func:`recommended_report`), otherwise the best-performing failing
    report (:func:`best_failing_report`) — real ``n_fit``, ``n_holdout``,
    ``threshold``, Wilson lower bound, AUROC, ECE, ``bins``, ``signal`` and
    the real ``model`` that produced it, with ``passes: false`` (issue
    athenaeum#2342: a measured failure must not render identically to the
    "not yet measured" stub).

    Only a class with ZERO candidate reports (the sweep never ran it at
    all) gets the legacy zero-placeholder row — ``n_fit: 0``,
    ``n_holdout: 0``, ``threshold: 0.0`` — marked ``measured: false``.
    That row's ``model`` is ``""``, never :data:`CANDIDATE_MODELS`\\ ``[0]``
    — defaulting to a real model id would misrepresent an unmeasured class
    as one actually scored by that model.

    Every entry carries ``measured`` (``True``/``False``) so a reader can
    tell a genuine failure from "never ran" without inspecting the other
    fields.
    """
    entries: list[dict[str, Any]] = []
    for question_class in QUESTION_CLASSES:
        candidates = reports_by_class.get(question_class) or []
        report = recommended_report(candidates) if candidates else None
        if report is None and candidates:
            report = best_failing_report(candidates)
        if report is None:
            entries.append(
                {
                    "question_class": question_class,
                    "model": "",
                    "prompt_sha256": prompt_sha256,
                    "signal": SIGNAL_CLASS_LEVEL,
                    "bins": [],
                    "threshold": 0.0,
                    "n_fit": 0,
                    "n_holdout": 0,
                    "holdout_accuracy_above": 0.0,
                    "holdout_wilson_lb": 0.0,
                    "ece": None,
                    "auroc": None,
                    "abstain_share": 0.0,
                    "dataset_sha256": dataset_sha256,
                    "measured_at": measured_at,
                    "passes": False,
                    "measured": False,
                }
            )
            continue
        entries.append(
            {
                "question_class": report.question_class,
                "model": report.model,
                "prompt_sha256": prompt_sha256,
                "signal": report.signal,
                "bins": [list(b) for b in report.bins],
                "threshold": report.threshold if report.threshold is not None else 0.0,
                "n_fit": report.n_fit,
                "n_holdout": report.n_holdout,
                "holdout_accuracy_above": report.holdout_accuracy_above,
                "holdout_wilson_lb": report.holdout_wilson_lb,
                "ece": report.ece,
                "auroc": report.auroc,
                "abstain_share": report.abstain_share,
                "dataset_sha256": dataset_sha256,
                "measured_at": measured_at,
                "passes": report.passes,
                "measured": True,
            }
        )
    return entries


def write_calibration_record(entries: list[dict[str, Any]], path: Path) -> None:
    """Write *entries* to *path* in the shipped record's YAML shape."""
    path.write_text(
        yaml.safe_dump(entries, sort_keys=False, default_flow_style=False), encoding="utf-8"
    )
