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
gating (:func:`tests.evals.harness.live_ready`) and record/replay
(:class:`tests.evals.harness.RecordingClient`) are inherited rather than
reimplemented.

**Marked ``calibration``, not ``eval`` (issue athenaeum#2287).** A full sweep
spends ~3.7M tokens (eval run 41325591218: 3,686,060), over fourteen times
the default suite's ``EVAL_TOKEN_CEILING``. While it carried ``eval``,
every default ``evals.yml`` dispatch selected it and failed on that
ceiling. It now runs only via ``evals.yml``'s ``suite=calibration``
dispatch input (or a local ``pytest -m calibration tests/evals/``), and
its usage accumulates into the separate ``calibration_session`` fixture,
guarded by :data:`tests.evals.harness.CALIBRATION_TOKEN_CEILING`.

**No build lane may run this test.** Token spend from a live model call is
an irreversible external action outside a build lane's authority — this
eval runs only via an operator's own local invocation, or CI's
``evals.yml`` ``workflow_dispatch`` with ``suite=calibration``. Skips cleanly (:func:`live_ready`)
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
    CANDIDATE_MODELS,
    CASES_PATH,
    ClassReport,
    assert_accuracy_bar_reachable,
    assert_corpus_covers_every_question_class,
    load_cases,
    needs_agreement_k3,
    render_class_report_line,
    require_some_class_passes,
    run_case_agreement_k3,
    run_case_self_reported,
    select_signal_and_report,
    summarize_classes,
    to_calibration_entries,
    write_calibration_record,
)
from tests.evals.harness import LAYER_ATTRIBUTION, RecordingClient, build_live_client, live_ready

pytestmark = pytest.mark.calibration

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
    calibration_session: Any,
    _live_ready: None,
) -> None:
    """Run the full corpus against every candidate model; write the record."""
    accuracy_floor = config_module.resolve_attribution_accuracy_floor(None)

    # Zero-token preflight (issue athenaeum#2342): refuse before
    # constructing any client when the corpus is too small to reach the
    # bar for one or more classes, no matter how the live run turns out.
    assert_accuracy_bar_reachable(_cases, accuracy_floor=accuracy_floor)

    inner = build_live_client()
    dataset_sha256 = hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()
    prompt_sha256 = hashlib.sha256(
        CONTACT_ATTRIBUTION_JUDGE_SYSTEM.encode("utf-8")
    ).hexdigest()

    reports_by_class: dict[str, list[ClassReport]] = {}

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
                calibration_session.observe_response(str(params.get("model", "")), response)
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
                calibration_session.record_case(
                    LAYER_ATTRIBUTION,
                    f"{model}::{run.case_id}",
                    expected=run.gold_verdict,
                    observed=run.judgement_verdict,
                    passed=run.correct,
                    detail=(
                        f"question_class={run.question_class} split={run.split} "
                        f"signal={run.signal} score={run.score} "
                        f"owner_uid={run.owner_uid} alias_relation={run.alias_relation}"
                    ),
                )

            agreement_fit = agreement_holdout = None
            # issue athenaeum#2342: skip agreement-K3 entirely for a class
            # whose self-reported fit split is already perfect (no wrong
            # case at all) -- it is the class K3 helps least.
            if needs_agreement_k3(self_fit):
                agreement_fit = []
                for case in fit_cases:
                    # issue athenaeum#2342: a distinct recording stamp per
                    # K3 call (not one `start_case` for all three) --
                    # otherwise all three raw responses land on the same
                    # `recorded_path` and only the last survives on disk.
                    agreement_fit.append(
                        run_case_agreement_k3(
                            case,
                            model,
                            client,
                            on_call=lambda i, case=case: client.start_case(
                                f"agree__{model}__{case['id']}__k{i}"
                            ),
                        )
                    )
                    client.end_case()
                agreement_holdout = []
                for case in holdout_cases:
                    agreement_holdout.append(
                        run_case_agreement_k3(
                            case,
                            model,
                            client,
                            on_call=lambda i, case=case: client.start_case(
                                f"agree__{model}__{case['id']}__k{i}"
                            ),
                        )
                    )
                    client.end_case()

                for run in agreement_fit + agreement_holdout:
                    calibration_session.record_case(
                        LAYER_ATTRIBUTION,
                        f"{model}::{run.case_id}::agreement",
                        expected=run.gold_verdict,
                        observed=run.judgement_verdict,
                        passed=run.correct,
                        detail=(
                            f"question_class={run.question_class} split={run.split} "
                            f"signal={run.signal} majority_verdict={run.judgement_verdict} "
                            f"agreement_score={run.score} "
                            f"raw_verdicts={list(run.raw_verdicts)} "
                            f"owner_uid={run.owner_uid} alias_relation={run.alias_relation}"
                        ),
                    )
                    for index, verdict in enumerate(run.raw_verdicts):
                        calibration_session.record_case(
                            LAYER_ATTRIBUTION,
                            f"{model}::{run.case_id}::agreement::run{index}",
                            expected=run.gold_verdict,
                            observed=verdict,
                            passed=(verdict == run.gold_verdict),
                            detail=(
                                f"question_class={run.question_class} split={run.split} "
                                f"raw_run_index={index}"
                            ),
                        )

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

            # issue athenaeum#2342: every candidate (class x model x
            # signal) report, not just the recommended one, is written to
            # the calibration summary artifact and printed to test output.
            print(render_class_report_line(report))
            calibration_session.record_case(
                LAYER_ATTRIBUTION,
                f"report::{model}::{question_class}::{report.signal}",
                expected=f"wilson_lb>={accuracy_floor}",
                observed=f"wilson_lb={report.holdout_wilson_lb:.4f}",
                passed=report.passes,
                detail=render_class_report_line(report),
            )

        reports_by_class[question_class] = candidate_reports

    # Write the record BEFORE the pass-count assertion below, mirroring
    # calibration_session's own "emit the artifact before asserting"
    # convention -- an all-fail sweep still leaves its real measured
    # values on disk for triage, exactly the data this issue's motivating
    # run failed to produce.
    entries = to_calibration_entries(
        reports_by_class,
        dataset_sha256=dataset_sha256,
        prompt_sha256=prompt_sha256,
        measured_at=datetime.now(timezone.utc).date().isoformat(),
    )
    write_calibration_record(entries, CALIBRATION_RECORD_PATH)

    lines, passed, total = summarize_classes(reports_by_class)
    for line in lines:
        print(line)
    print(f"{passed}/{total} classes passed")
    require_some_class_passes(reports_by_class)
