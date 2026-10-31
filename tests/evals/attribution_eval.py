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
    """One (case, model, signal) run's outcome."""

    case_id: str
    question_class: str
    model: str
    signal: str
    gold_verdict: str
    judgement_verdict: str
    score: float
    correct: bool


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
    wilson_lb = wilson_lower_bound(n_correct_above, n_above)
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


# ---------------------------------------------------------------------------
# Shipped-record rendering (design §12.4)
# ---------------------------------------------------------------------------


def to_calibration_entries(
    reports_by_class: dict[str, ClassReport | None],
    *,
    dataset_sha256: str,
    prompt_sha256: str,
    measured_at: str,
) -> list[dict[str, Any]]:
    """Render selected :class:`ClassReport`\\ s into the shipped YAML shape
    (design §12.4). A ``None`` report (no tier passed) still yields a row,
    with ``passes: false`` and a ``class-level`` placeholder signal — the
    record always carries one entry per class."""
    entries: list[dict[str, Any]] = []
    for question_class in QUESTION_CLASSES:
        report = reports_by_class.get(question_class)
        if report is None:
            entries.append(
                {
                    "question_class": question_class,
                    "model": CANDIDATE_MODELS[0],
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
            }
        )
    return entries


def write_calibration_record(entries: list[dict[str, Any]], path: Path) -> None:
    """Write *entries* to *path* in the shipped record's YAML shape."""
    path.write_text(
        yaml.safe_dump(entries, sort_keys=False, default_flow_style=False), encoding="utf-8"
    )
