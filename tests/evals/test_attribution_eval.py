# SPDX-License-Identifier: Apache-2.0
"""Live contact-attribution calibration eval (issue athenaeum#2211).

Design ``docs/design/attribution-verification.md`` §12. For every question
class, runs the synthetic corpus (``tests/evals/data/attribution/
cases.yaml``) against every candidate in
:data:`tests.evals.attribution_eval.CANDIDATE_MODELS` (the classify/write/
verify knob defaults), self-reported-confidence first; agreement-K3 only
for a (class, model) whose self-reported AUROC fails
:data:`tests.evals.attribution_eval.AUROC_BAR` on the fit split — design
§12.2's cost-bounding rule. Writes the recommended tier per class to
``src/athenaeum/calibration_data/contact_attribution.yaml``.

Extends the existing harness (:mod:`tests.evals.harness`) exactly the way
``test_write_tier_compare.py`` does for the write-knob comparison: credential
gating (:func:`tests.evals.harness.live_ready`), record/replay
(:class:`tests.evals.harness.RecordingClient`), and the session token
guard are inherited rather than reimplemented.

**No build lane may run this test.** Token spend from a live model call is
an irreversible external action outside a build lane's authority — this
eval runs only via an operator's own local invocation, or CI's
``evals.yml`` ``workflow_dispatch`` job. Skips cleanly (:func:`live_ready`)
when no LLM credential is configured, which is the only path this file has
ever actually taken in a build lane.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from athenaeum import config as config_module
from athenaeum.pii_attribution import (
    CALIBRATION_RECORD_FILENAME,
    CONTACT_ATTRIBUTION_JUDGE_SYSTEM,
    QUESTION_CLASSES,
)
from tests.evals.attribution_eval import (
    AUROC_BAR,
    CANDIDATE_MODELS,
    CASES_PATH,
    ClassReport,
    assert_corpus_covers_every_question_class,
    load_cases,
    recommended_report,
    run_case_agreement_k3,
    run_case_self_reported,
    select_signal_and_report,
    to_calibration_entries,
    write_calibration_record,
)
from tests.evals.harness import LAYER_ATTRIBUTION, RecordingClient, build_live_client, live_ready
from tests.evals.metrics import auroc as _auroc

pytestmark = pytest.mark.eval

REPO_ROOT = Path(__file__).resolve().parents[2]
CALIBRATION_RECORD_PATH = (
    REPO_ROOT / "src" / "athenaeum" / "calibration_data" / CALIBRATION_RECORD_FILENAME
)


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.fixture(scope="module")
def _cases() -> list[dict[str, Any]]:
    cases = load_cases()
    assert_corpus_covers_every_question_class(cases)
    return cases


def test_attribution_calibration_eval(
    _cases: list[dict[str, Any]],
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Run the full corpus against every candidate model; write the record."""
    inner = build_live_client()
    accuracy_floor = config_module.resolve_attribution_accuracy_floor(None)
    dataset_sha256 = hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()
    prompt_sha256 = hashlib.sha256(
        CONTACT_ATTRIBUTION_JUDGE_SYSTEM.encode("utf-8")
    ).hexdigest()

    reports_by_class: dict[str, ClassReport | None] = {}

    for question_class in QUESTION_CLASSES:
        class_cases = [c for c in _cases if c["question_class"] == question_class]
        fit_cases = [c for c in class_cases if c["split"] == "fit"]
        holdout_cases = [c for c in class_cases if c["split"] == "holdout"]

        candidate_reports: list[ClassReport] = []
        for model in CANDIDATE_MODELS:
            client = RecordingClient(inner, record=eval_record, layer=LAYER_ATTRIBUTION)
            original_create = client.messages.create

            def _create(_orig: Any = original_create, **params: Any) -> Any:
                response = _orig(**params)
                eval_session.observe_response(str(params.get("model", "")), response)
                return response

            client.messages.create = _create  # type: ignore[method-assign]

            self_fit = []
            for case in fit_cases:
                client.start_case(f"self__{model}__{case['id']}")
                self_fit.append(run_case_self_reported(case, model, client))
                client.end_case()
            self_holdout = []
            for case in holdout_cases:
                client.start_case(f"self__{model}__{case['id']}")
                self_holdout.append(run_case_self_reported(case, model, client))
                client.end_case()

            for run in self_fit + self_holdout:
                eval_session.record_case(
                    LAYER_ATTRIBUTION,
                    f"{model}::{run.case_id}",
                    expected=run.gold_verdict,
                    observed=run.judgement_verdict,
                    passed=run.correct,
                )

            agreement_fit = agreement_holdout = None
            try:
                preliminary_auroc: float | None = _auroc(
                    [r.score for r in self_fit], [r.correct for r in self_fit]
                )
            except ValueError:
                preliminary_auroc = None
            if preliminary_auroc is None or preliminary_auroc < AUROC_BAR:
                agreement_fit = []
                for case in fit_cases:
                    client.start_case(f"agree__{model}__{case['id']}")
                    agreement_fit.append(run_case_agreement_k3(case, model, client))
                    client.end_case()
                agreement_holdout = []
                for case in holdout_cases:
                    client.start_case(f"agree__{model}__{case['id']}")
                    agreement_holdout.append(run_case_agreement_k3(case, model, client))
                    client.end_case()

            report = select_signal_and_report(
                question_class,
                model,
                accuracy_floor=accuracy_floor,
                self_reported_fit=self_fit,
                self_reported_holdout=self_holdout,
                agreement_fit=agreement_fit,
                agreement_holdout=agreement_holdout,
            )
            candidate_reports.append(report)

        reports_by_class[question_class] = recommended_report(candidate_reports)

    entries = to_calibration_entries(
        reports_by_class,
        dataset_sha256=dataset_sha256,
        prompt_sha256=prompt_sha256,
        measured_at=datetime.now(timezone.utc).date().isoformat(),
    )
    write_calibration_record(entries, CALIBRATION_RECORD_PATH)
