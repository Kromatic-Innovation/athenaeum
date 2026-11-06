# SPDX-License-Identifier: Apache-2.0
"""Fixture tests for the noise-floor report (issue athenaeum#2309).

Every test here is built from SYNTHETIC rows -- either hand-constructed
:class:`~tests.evals.noise_floor.GradedRow` objects (for the pure
statistics: variance, MDE, required-n, oracle extraction) or small real-
corpus-grounded :class:`~tests.evals.rollout.RolloutRecord` rows (for the
few tests that must prove this module actually calls
:func:`tests.evals.north_star_report.grade_correctness` rather than a
second grading path). No live rollout, no model client, no paid store,
no network, no credential -- UNMARKED (no ``eval``/``embedding`` marker),
runs in the default pytest selection.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tests.evals.containment import GridCell, ResultStore
from tests.evals.corpus import build_corpus
from tests.evals import noise_floor
from tests.evals.noise_floor import (
    _Z_SUM,
    POSITIVE_CONTROL_PROBE_CLASS,
    REQUIRED_N_TARGET_DELTAS,
    GradedRow,
    MDEResult,
    ProvenanceMismatchError,
    _effective_model,
    _model_from_transcript,
    assert_provenance,
    build_noise_floor_report,
    compute_mde,
    compute_oracle_harness_failures,
    compute_oracle_incorrect,
    compute_pooled_correctness_by_arm,
    compute_positive_control,
    compute_replicate_variance,
    compute_required_n,
    grade_rows,
    main,
    render_report,
    report_to_json,
)
from tests.evals.north_star_report import RolloutRow, append_rollout_row
from tests.evals.rollout import Arm, RolloutRecord, TurnTokenUsage

CORPUS_SCALE = "core"
# Real "core" corpus, loaded once -- same discipline as
# tests/evals/test_north_star_report.py: probe ids and planted markers are
# read off it in the handful of tests that exercise the real grader, so a
# corpus edit cannot silently desync those tests from the ground truth.
_CORPUS = build_corpus(scale=CORPUS_SCALE)
# single_hop probe, marker "25 days per year" on uid "policy-pto".
PTO_PROBE_ID = "pto_allowance"
PTO_MARKER = "25 days per year"
# unprompted_push probe (one of the 12 athenaeum#2303 added), marker
# "a couple of seconds" on uid "decision-console-latency-requirement".
UNPROMPTED_PUSH_PROBE_ID = "relay_sync_fix"
UNPROMPTED_PUSH_MARKER = "a couple of seconds"


def _row(
    *,
    arm: Arm,
    probe_id: str,
    probe_class: str,
    corpus_scale: str = CORPUS_SCALE,
    replicate: int = 0,
    answer: str = "",
    transcript: list[dict] | None = None,
    harness_failure: str | None = None,
    search_backend: str | None = "vector",
    model: str = "",
) -> RolloutRow:
    record = RolloutRecord(
        arm=arm,
        probe_id=probe_id,
        probe_class=probe_class,
        corpus_scale=corpus_scale,
        answer=answer,
        transcript=transcript if transcript is not None else [],
        harness_failure=harness_failure,
        search_backend=search_backend,
        model=model,
    )
    cell = GridCell(probe=probe_id, arm=arm.value, corpus_scale=corpus_scale, replicate=replicate)
    return RolloutRow(cell=cell, record=record)


def _graded(
    *,
    arm: Arm,
    probe_class: str,
    probe_id: str,
    correct: bool | None,
    corpus_scale: str = CORPUS_SCALE,
    replicate: int = 0,
    harness_failure: bool = False,
) -> GradedRow:
    """A :class:`GradedRow` built directly, bypassing real grading -- the
    right tool for every test of the PURE statistics (variance, MDE,
    required-n, oracle extraction), which have no opinion on how
    correctness was decided."""
    row = _row(
        arm=arm,
        probe_id=probe_id,
        probe_class=probe_class,
        corpus_scale=corpus_scale,
        replicate=replicate,
    )
    return GradedRow(row=row, harness_failure=harness_failure, correct=correct)


# ---------------------------------------------------------------------------
# Model provenance extraction
# ---------------------------------------------------------------------------


def test_model_from_transcript_reads_cli_mode_system_event() -> None:
    record = RolloutRecord(
        arm=Arm.PULL,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer="x",
        transcript=[
            {
                "type": "system",
                "subtype": "init",
                "model": "claude-haiku-4-5-20251001",
            }
        ],
    )
    assert _model_from_transcript(record) == "claude-haiku-4-5-20251001"


def test_model_from_transcript_reads_assistant_message_fallback() -> None:
    record = RolloutRecord(
        arm=Arm.PULL,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer="x",
        transcript=[{"type": "assistant", "message": {"model": "claude-haiku-4-5-20251001"}}],
    )
    assert _model_from_transcript(record) == "claude-haiku-4-5-20251001"


def test_model_from_transcript_none_for_api_mode_push_shape() -> None:
    """The api-mode arms' own transcript shape (``run_push_pages_upper_bound``
    et al. in ``tests/evals/rollout.py``) carries no ``model`` key at all --
    this must return ``None``, never a guess."""
    record = RolloutRecord(
        arm=Arm.NONE,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer="x",
        transcript=[{"system": "...", "user": "...", "answer": "x"}],
    )
    assert _model_from_transcript(record) is None


def test_effective_model_prefers_authoritative_rollout_record_field() -> None:
    """Issue athenaeum#2309: a sibling lane added ``RolloutRecord.model``,
    stamped for every arm (api-mode included) by ``run_probe_all_arms``.
    When present, it must win over any transcript-derived guess -- even
    one that disagrees, which should never happen on real data but must
    not silently flip the answer if it does."""
    record = RolloutRecord(
        arm=Arm.PULL,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer="x",
        model="claude-haiku-4-5-20251001",
        transcript=[{"type": "system", "subtype": "init", "model": "claude-opus-4-6"}],
    )
    assert _effective_model(record) == "claude-haiku-4-5-20251001"


def test_effective_model_covers_api_mode_arm_via_rollout_record_field() -> None:
    """The exact gap this field closes: an api-mode arm (NONE here) has no
    transcript to fall back to, but now carries ``model`` directly."""
    record = RolloutRecord(
        arm=Arm.NONE,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer="x",
        model="claude-haiku-4-5-20251001",
        transcript=[{"system": "...", "user": "...", "answer": "x"}],
    )
    assert _effective_model(record) == "claude-haiku-4-5-20251001"


def test_effective_model_falls_back_to_transcript_on_unstamped_record() -> None:
    """``model`` defaults to ``""`` -- unstamped, for a pre-athenaeum#2309
    store -- which must fall back to the transcript, not read as a real
    empty-string model value."""
    record = RolloutRecord(
        arm=Arm.PULL,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer="x",
        transcript=[{"type": "system", "subtype": "init", "model": "claude-haiku-4-5-20251001"}],
    )
    assert record.model == ""
    assert _effective_model(record) == "claude-haiku-4-5-20251001"


def test_effective_model_none_when_unstamped_and_no_transcript() -> None:
    record = RolloutRecord(
        arm=Arm.NONE,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        corpus_scale=CORPUS_SCALE,
        answer="x",
    )
    assert _effective_model(record) is None


# ---------------------------------------------------------------------------
# Provenance refusal (athenaeum#2310 AC5)
# ---------------------------------------------------------------------------


def _cli_row(
    *, model: str, arm: Arm = Arm.PULL, search_backend: str | None = "vector"
) -> RolloutRow:
    return _row(
        arm=arm,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        search_backend=search_backend,
        transcript=[{"type": "system", "subtype": "init", "model": model}],
    )


def test_assert_provenance_happy_path_echoes_triple_and_flags_unverified_arms() -> None:
    cli = _cli_row(model="claude-haiku-4-5-20251001")
    api = _row(
        arm=Arm.NONE, probe_id=PTO_PROBE_ID, probe_class="single_hop", search_backend="vector"
    )
    provenance = assert_provenance([cli, api])
    assert provenance.model == "claude-haiku-4-5-20251001"
    assert provenance.model_unverified_arms == ("none",)
    assert provenance.search_backend == "vector"
    assert provenance.corpus_digests[CORPUS_SCALE] == _CORPUS.fingerprint()
    assert provenance.grader_revision
    assert provenance.git_sha


def test_assert_provenance_no_unverified_arms_when_rows_carry_rollout_record_model() -> None:
    """Issue athenaeum#2309's own closing of the gap: on a store where every
    row (api-mode included) carries the real ``RolloutRecord.model`` field,
    ``model_unverified_arms`` must be EMPTY -- the gap the earlier
    transcript-only fallback could not close for api-mode rows."""
    cli = _row(
        arm=Arm.PULL,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        search_backend="vector",
        model="claude-haiku-4-5-20251001",
    )
    api = _row(
        arm=Arm.NONE,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        search_backend="vector",
        model="claude-haiku-4-5-20251001",
    )
    provenance = assert_provenance([cli, api])
    assert provenance.model == "claude-haiku-4-5-20251001"
    assert provenance.model_unverified_arms == ()


def test_assert_provenance_refuses_model_mismatch() -> None:
    rows = [_cli_row(model="claude-haiku-4-5-20251001"), _cli_row(model="claude-opus-4-6")]
    with pytest.raises(ProvenanceMismatchError, match="model"):
        assert_provenance(rows)


def test_assert_provenance_refuses_search_backend_mismatch() -> None:
    rows = [
        _cli_row(model="claude-haiku-4-5-20251001", search_backend="vector"),
        _cli_row(model="claude-haiku-4-5-20251001", search_backend="fts5"),
    ]
    with pytest.raises(ProvenanceMismatchError, match="search_backend"):
        assert_provenance(rows)


def test_assert_provenance_does_not_raise_on_single_distinct_value_with_nones() -> None:
    """A row carrying no search_backend at all (pre-athenaeum#1764 store, or
    a cli-mode row this test does not bother stamping) must not be read as
    a MISMATCH against a row that does -- ``None`` means "not recorded",
    not "a third value"."""
    rows = [
        _cli_row(model="claude-haiku-4-5-20251001", search_backend="vector"),
        _cli_row(model="claude-haiku-4-5-20251001", search_backend=None),
    ]
    provenance = assert_provenance(rows)
    assert provenance.search_backend == "vector"


# ---------------------------------------------------------------------------
# Harness failure vs graded-incorrect (never conflated)
# ---------------------------------------------------------------------------


def test_grade_rows_harness_failure_short_circuits_before_any_probe_lookup() -> None:
    """A harness-failed row's probe_id/corpus_scale need not even resolve --
    ``grade_rows`` must never reach ``_probe_for_row``/``_corpus_for_scale``
    for it, which this garbage probe_id would otherwise raise KeyError on."""
    row = _row(
        arm=Arm.PULL,
        probe_id="does-not-exist-in-any-corpus",
        probe_class="single_hop",
        harness_failure="turn_cap",
    )
    graded = grade_rows([row])
    assert graded[0].harness_failure is True
    assert graded[0].correct is None


def test_grade_rows_reuses_real_grade_correctness_for_a_gradable_row() -> None:
    """Wiring check: a real ORACLE row over the real ``pto_allowance`` probe,
    answer containing the planted marker, must grade correct through THIS
    module's call into ``north_star_report.grade_correctness`` -- proving
    reuse, not a second grading path."""
    correct_row = _row(
        arm=Arm.ORACLE,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        answer=f"The PTO allowance is {PTO_MARKER}.",
    )
    wrong_row = _row(
        arm=Arm.ORACLE,
        probe_id=PTO_PROBE_ID,
        probe_class="single_hop",
        answer="I have no idea.",
    )
    graded = grade_rows([correct_row, wrong_row])
    assert graded[0].harness_failure is False
    assert graded[0].correct is True
    assert graded[1].correct is False


# ---------------------------------------------------------------------------
# Replicate-to-replicate variance (item 2)
# ---------------------------------------------------------------------------


def test_replicate_variance_discordant_pair_matches_hand_computed_formula() -> None:
    graded = [
        _graded(arm=Arm.PULL, probe_class="single_hop", probe_id="p1", replicate=0, correct=True),
        _graded(arm=Arm.PULL, probe_class="single_hop", probe_id="p1", replicate=1, correct=False),
        _graded(arm=Arm.PULL, probe_class="single_hop", probe_id="p2", replicate=0, correct=True),
        _graded(arm=Arm.PULL, probe_class="single_hop", probe_id="p2", replicate=1, correct=True),
    ]
    (rv,) = compute_replicate_variance(graded)
    assert rv.n_paired == 2
    assert rv.discordant == 1
    assert rv.concordant == 1
    assert rv.discordance_rate == pytest.approx(0.5)
    # p10=0.5 (p1), p01=0.0 -> Var(d_i) = 0.5 - (0.5-0.0)**2 = 0.25
    assert rv.variance_of_difference == pytest.approx(0.25)
    assert rv.sd_rate_difference is not None
    assert rv.sd_rate_difference == pytest.approx(math.sqrt(0.25 / 2))
    assert rv.sd_class_rate_implied == pytest.approx(rv.sd_rate_difference / math.sqrt(2))
    assert rv.df == 1
    assert rv.rate_replicate0 == pytest.approx(1.0)
    assert rv.rate_replicate1 == pytest.approx(0.5)


def test_replicate_variance_no_overlap_reports_zero_paired_not_an_error() -> None:
    graded = [
        _graded(arm=Arm.PULL, probe_class="single_hop", probe_id="p1", replicate=0, correct=True)
    ]
    (rv,) = compute_replicate_variance(graded)
    assert rv.n_paired == 0
    assert rv.discordance_rate is None
    assert rv.sd_class_rate_implied is None
    assert rv.df == -1


def test_replicate_variance_excludes_harness_failed_replicate_from_pairing() -> None:
    graded = [
        _graded(arm=Arm.PULL, probe_class="single_hop", probe_id="p1", replicate=0, correct=True),
        _graded(
            arm=Arm.PULL,
            probe_class="single_hop",
            probe_id="p1",
            replicate=1,
            correct=None,
            harness_failure=True,
        ),
    ]
    (rv,) = compute_replicate_variance(graded)
    assert rv.n_paired == 0
    assert rv.n_replicate0 == 1
    assert rv.n_replicate1 == 0


# ---------------------------------------------------------------------------
# MDE: paired/McNemar + unpaired normal approximation (item 3)
# ---------------------------------------------------------------------------


def test_mde_paired_formula_matches_hand_computation() -> None:
    # 10 paired probes: 2 discordant in arm's favour (p10), 1 against (p01).
    graded = []
    for i in range(7):
        graded.append(_graded(arm=Arm.PULL, probe_class="c", probe_id=f"p{i}", correct=True))
        graded.append(
            _graded(arm=Arm.PUSH_BREADCRUMB_PULL, probe_class="c", probe_id=f"p{i}", correct=True)
        )
    for i in range(7, 9):
        graded.append(_graded(arm=Arm.PULL, probe_class="c", probe_id=f"p{i}", correct=True))
        graded.append(
            _graded(arm=Arm.PUSH_BREADCRUMB_PULL, probe_class="c", probe_id=f"p{i}", correct=False)
        )
    graded.append(_graded(arm=Arm.PULL, probe_class="c", probe_id="p9", correct=False))
    graded.append(
        _graded(arm=Arm.PUSH_BREADCRUMB_PULL, probe_class="c", probe_id="p9", correct=True)
    )

    (m,) = compute_mde(graded)
    assert m.n == 10
    assert m.p10 == pytest.approx(0.2)
    assert m.p01 == pytest.approx(0.1)
    var_d = 0.3 - (0.2 - 0.1) ** 2
    expected_paired = _Z_SUM * math.sqrt(var_d) / math.sqrt(10)
    assert m.mde_paired == pytest.approx(expected_paired)
    assert m.rate_arm is not None and m.rate_verdict_arm is not None
    pbar = (m.rate_arm + m.rate_verdict_arm) / 2
    expected_unpaired = _Z_SUM * math.sqrt(2 * pbar * (1 - pbar)) / math.sqrt(10)
    assert m.mde_unpaired == pytest.approx(expected_unpaired)


def test_mde_normal_approx_reliability_flag() -> None:
    # n=4, p_disc=0.5 -> n*p_disc=2 < 5 -> unreliable.
    graded = [
        _graded(arm=Arm.PULL, probe_class="c", probe_id="p0", correct=True),
        _graded(arm=Arm.PUSH_BREADCRUMB_PULL, probe_class="c", probe_id="p0", correct=False),
        _graded(arm=Arm.PULL, probe_class="c", probe_id="p1", correct=False),
        _graded(arm=Arm.PUSH_BREADCRUMB_PULL, probe_class="c", probe_id="p1", correct=True),
        _graded(arm=Arm.PULL, probe_class="c", probe_id="p2", correct=True),
        _graded(arm=Arm.PUSH_BREADCRUMB_PULL, probe_class="c", probe_id="p2", correct=True),
        _graded(arm=Arm.PULL, probe_class="c", probe_id="p3", correct=False),
        _graded(arm=Arm.PUSH_BREADCRUMB_PULL, probe_class="c", probe_id="p3", correct=False),
    ]
    (m,) = compute_mde(graded)
    assert m.n == 4
    assert m.normal_approx_reliable is False
    # The number is still computed (never a missing field), per the module's
    # own instruction that render_report, not compute_mde, is where the
    # "insufficient n" substitution happens.
    assert m.mde_paired is not None


def test_mde_skips_group_with_no_verdict_arm_rows() -> None:
    graded = [_graded(arm=Arm.PULL, probe_class="c", probe_id="p0", correct=True)]
    assert compute_mde(graded) == ()


# ---------------------------------------------------------------------------
# Required n (item 4)
# ---------------------------------------------------------------------------


def _mde_result(*, n: int, p10: float, p01: float) -> MDEResult:
    return MDEResult(
        arm="pull",
        verdict_arm="push_breadcrumb_pull",
        probe_class="c",
        corpus_scale=CORPUS_SCALE,
        n=n,
        rate_arm=0.5,
        rate_verdict_arm=0.5,
        p10=p10,
        p01=p01,
        mde_paired=0.1,
        normal_approx_reliable=True,
        mde_unpaired=0.1,
    )


def test_required_n_inversion_matches_formula() -> None:
    (r,) = compute_required_n([_mde_result(n=10, p10=0.2, p01=0.1)])
    var_d = 0.3 - (0.2 - 0.1) ** 2
    for delta in REQUIRED_N_TARGET_DELTAS:
        expected = math.ceil((_Z_SUM * math.sqrt(var_d) / delta) ** 2)
        assert r.required_n[delta] == expected


def test_required_n_undefined_for_zero_discordance() -> None:
    (r,) = compute_required_n([_mde_result(n=10, p10=0.0, p01=0.0)])
    assert all(r.required_n[delta] is None for delta in REQUIRED_N_TARGET_DELTAS)


# ---------------------------------------------------------------------------
# The oracle counter-example (item 5) + positive control (item 6)
# ---------------------------------------------------------------------------


def test_oracle_incorrect_pairs_with_push_breadcrumb_pull_on_same_probe() -> None:
    graded = [
        _graded(arm=Arm.ORACLE, probe_class="single_hop", probe_id="p1", correct=False),
        _graded(
            arm=Arm.PUSH_BREADCRUMB_PULL, probe_class="single_hop", probe_id="p1", correct=True
        ),
        # A correctly-graded oracle row must NOT appear in the list.
        _graded(arm=Arm.ORACLE, probe_class="single_hop", probe_id="p2", correct=True),
        _graded(
            arm=Arm.PUSH_BREADCRUMB_PULL, probe_class="single_hop", probe_id="p2", correct=True
        ),
    ]
    incorrect = compute_oracle_incorrect(graded)
    assert [o.probe_id for o in incorrect] == ["p1"]
    assert incorrect[0].push_breadcrumb_pull_correct is True
    assert incorrect[0].push_breadcrumb_pull_harness_failure is False


def test_oracle_incorrect_reports_no_row_when_push_breadcrumb_pull_missing() -> None:
    graded = [_graded(arm=Arm.ORACLE, probe_class="single_hop", probe_id="p1", correct=False)]
    (o,) = compute_oracle_incorrect(graded)
    assert o.push_breadcrumb_pull_correct is None
    assert o.push_breadcrumb_pull_harness_failure is False


def test_oracle_harness_failure_never_pooled_as_wrong_answer() -> None:
    """A harness-failed ORACLE row must land in
    compute_oracle_harness_failures, and NEVER in compute_oracle_incorrect
    -- the issue's own instruction ("never pooled as if they were")."""
    graded = [
        _graded(
            arm=Arm.ORACLE,
            probe_class="single_hop",
            probe_id="p1",
            correct=None,
            harness_failure=True,
        ),
    ]
    assert compute_oracle_incorrect(graded) == ()
    (failure,) = compute_oracle_harness_failures(graded)
    assert failure.probe_id == "p1"


def test_oracle_harness_failure_push_breadcrumb_pull_flagged_distinctly() -> None:
    graded = [
        _graded(arm=Arm.ORACLE, probe_class="single_hop", probe_id="p1", correct=False),
        _graded(
            arm=Arm.PUSH_BREADCRUMB_PULL,
            probe_class="single_hop",
            probe_id="p1",
            correct=None,
            harness_failure=True,
        ),
    ]
    (o,) = compute_oracle_incorrect(graded)
    assert o.push_breadcrumb_pull_harness_failure is True
    assert o.push_breadcrumb_pull_correct is None


def test_positive_control_counts_unprompted_push_oracle_rows_per_scale_replicate() -> None:
    graded = [
        _graded(
            arm=Arm.ORACLE,
            probe_class=POSITIVE_CONTROL_PROBE_CLASS,
            probe_id="u1",
            replicate=0,
            correct=True,
        ),
        _graded(
            arm=Arm.ORACLE,
            probe_class=POSITIVE_CONTROL_PROBE_CLASS,
            probe_id="u2",
            replicate=0,
            correct=False,
        ),
        _graded(
            arm=Arm.ORACLE,
            probe_class=POSITIVE_CONTROL_PROBE_CLASS,
            probe_id="u3",
            replicate=1,
            correct=None,
            harness_failure=True,
        ),
        # A different probe_class/arm must not be counted here.
        _graded(arm=Arm.ORACLE, probe_class="single_hop", probe_id="p1", replicate=0, correct=True),
        _graded(
            arm=Arm.PULL,
            probe_class=POSITIVE_CONTROL_PROBE_CLASS,
            probe_id="u1",
            replicate=0,
            correct=True,
        ),
    ]
    cells = {(c.corpus_scale, c.replicate): c for c in compute_positive_control(graded)}
    rep0 = cells[(CORPUS_SCALE, 0)]
    assert rep0.n == 2
    assert rep0.correct == 1
    assert rep0.rate == pytest.approx(0.5)
    rep1 = cells[(CORPUS_SCALE, 1)]
    assert rep1.n == 0
    assert rep1.harness_failures == 1


def test_pooled_correctness_is_n_weighted_not_mean_of_group_means() -> None:
    """The exact shape of the issue's own oracle/push_breadcrumb_pull
    counter-example: a ceiling arm graded on a HARD-skewed mix (low n,
    low rate) pools below an arm graded on an EASY-skewed mix, and the
    n-weighted pooled rate must reflect that -- never an unweighted
    average of per-class rates, which would hide it."""
    graded = [
        # oracle: 1 easy class (n=8, all correct) + 1 hard class (n=2, 0 correct)
        *[
            _graded(arm=Arm.ORACLE, probe_class="easy", probe_id=f"e{i}", correct=True)
            for i in range(8)
        ],
        *[
            _graded(arm=Arm.ORACLE, probe_class="hard", probe_id=f"h{i}", correct=False)
            for i in range(2)
        ],
        # push_breadcrumb_pull: only graded on the easy class, n=8, all correct.
        *[
            _graded(
                arm=Arm.PUSH_BREADCRUMB_PULL, probe_class="easy", probe_id=f"e{i}", correct=True
            )
            for i in range(8)
        ],
    ]
    by_arm = {a.arm: a for a in compute_pooled_correctness_by_arm(graded)}
    # n-weighted: 8/10 = 0.8, strictly below push_breadcrumb_pull's 1.0 --
    # the oracle-scores-below-the-arm-it-caps shape, reproduced from a
    # known class/n skew.
    assert by_arm["oracle"].n == 10
    assert by_arm["oracle"].rate == pytest.approx(0.8)
    assert by_arm["push_breadcrumb_pull"].rate == pytest.approx(1.0)
    # An unweighted mean of the two oracle class rates would be (1.0+0.0)/2
    # = 0.5, a DIFFERENT number -- asserting the real one is 0.8 is what
    # pins "n-weighted" rather than merely computing a mean.
    assert by_arm["oracle"].rate != pytest.approx(0.5)


# ---------------------------------------------------------------------------
# CLI end-to-end (real store, real grader, tmp_path)
# ---------------------------------------------------------------------------


def _write_store(path: Path, records: list[tuple[RolloutRecord, int]]) -> None:
    store = ResultStore(path)
    for record, replicate in records:
        cell = GridCell(
            probe=record.probe_id,
            arm=record.arm.value,
            corpus_scale=record.corpus_scale,
            replicate=replicate,
        )
        append_rollout_row(store, cell, record)


def _cli_record(
    *,
    arm: Arm,
    probe_id: str,
    probe_class: str,
    answer: str,
    model: str = "claude-haiku-4-5-20251001",
) -> RolloutRecord:
    return RolloutRecord(
        arm=arm,
        probe_id=probe_id,
        probe_class=probe_class,
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        transcript=[{"type": "system", "subtype": "init", "model": model}],
        search_backend="vector",
    )


def test_cli_end_to_end_writes_markdown_and_json(tmp_path: Path) -> None:
    store_path = tmp_path / "store.jsonl"
    _write_store(
        store_path,
        [
            (
                _cli_record(
                    arm=Arm.PULL, probe_id=PTO_PROBE_ID, probe_class="single_hop", answer=PTO_MARKER
                ),
                0,
            ),
            (
                _cli_record(
                    arm=Arm.PULL, probe_id=PTO_PROBE_ID, probe_class="single_hop", answer="no idea"
                ),
                1,
            ),
            (
                _cli_record(
                    arm=Arm.PUSH_BREADCRUMB_PULL,
                    probe_id=PTO_PROBE_ID,
                    probe_class="single_hop",
                    answer=PTO_MARKER,
                ),
                0,
            ),
            (
                _cli_record(
                    arm=Arm.PUSH_BREADCRUMB_PULL,
                    probe_id=PTO_PROBE_ID,
                    probe_class="single_hop",
                    answer=PTO_MARKER,
                ),
                1,
            ),
            (
                _cli_record(
                    arm=Arm.ORACLE,
                    probe_id=UNPROMPTED_PUSH_PROBE_ID,
                    probe_class=POSITIVE_CONTROL_PROBE_CLASS,
                    answer=UNPROMPTED_PUSH_MARKER,
                ),
                0,
            ),
        ],
    )
    out_md = tmp_path / "report.md"
    out_json = tmp_path / "report.json"
    exit_code = main(
        [
            "--store",
            str(store_path),
            "--run-id",
            "42169666857",
            "--out",
            str(out_md),
            "--json-out",
            str(out_json),
        ]
    )
    assert exit_code == 0
    assert out_md.exists()
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["run_id"] == "42169666857"
    assert payload["provenance"]["model"] == "claude-haiku-4-5-20251001"
    assert payload["n_rows"] == 5
    rendered = out_md.read_text(encoding="utf-8")
    assert "North-star grid noise floor" in rendered
    assert "Positive control" in rendered


def test_cli_refuses_on_provenance_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store_path = tmp_path / "store.jsonl"
    _write_store(
        store_path,
        [
            (
                _cli_record(
                    arm=Arm.PULL, probe_id=PTO_PROBE_ID, probe_class="single_hop", answer="x"
                ),
                0,
            ),
            (
                _cli_record(
                    arm=Arm.PULL,
                    probe_id=PTO_PROBE_ID,
                    probe_class="single_hop",
                    answer="x",
                    model="claude-opus-4-6",
                ),
                1,
            ),
        ],
    )
    out_md = tmp_path / "report.md"
    out_json = tmp_path / "report.json"
    exit_code = main(
        ["--store", str(store_path), "--out", str(out_md), "--json-out", str(out_json)]
    )
    assert exit_code == 1
    assert not out_md.exists()
    assert "model" in capsys.readouterr().err


def test_cli_reports_empty_store_rather_than_crashing(tmp_path: Path) -> None:
    store_path = tmp_path / "store.jsonl"
    out_md = tmp_path / "report.md"
    out_json = tmp_path / "report.json"
    exit_code = main(
        ["--store", str(store_path), "--out", str(out_md), "--json-out", str(out_json)]
    )
    assert exit_code == 1


# ---------------------------------------------------------------------------
# report_to_json / render_report smoke (dict-keyed-by-float round-trips)
# ---------------------------------------------------------------------------


def test_report_to_json_roundtrips_float_keyed_required_n_dict() -> None:
    """``RequiredN.required_n`` is keyed by float (0.05/0.10/0.15/0.20) --
    the one field shape in this report that ``json.dumps`` would otherwise
    choke on, since JSON object keys must be strings. Built via the real
    pipeline (cli-mode rows, so :func:`assert_provenance` passes) rather
    than a hand-built :class:`~tests.evals.noise_floor.NoiseFloorReport`,
    so this also exercises ``build_noise_floor_report`` end to end."""
    rows = [
        _cli_row(model="claude-haiku-4-5-20251001", arm=Arm.PULL),
        _cli_row(model="claude-haiku-4-5-20251001", arm=Arm.PUSH_BREADCRUMB_PULL),
    ]
    report = build_noise_floor_report(rows, run_id="r1")
    payload = report_to_json(report)
    json.dumps(payload)  # must not raise
    rendered = render_report(report)
    assert "Required n" in rendered


# ---------------------------------------------------------------------------
# Actual spend (issue athenaeum#2309)
# ---------------------------------------------------------------------------


def _row_with_tokens(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
    harness_failure: str = "",
    model: str = "claude-haiku-4-5-20251001",
) -> RolloutRow:
    record = RolloutRecord(
        arm=Arm.NONE,
        probe_id="pto_allowance",
        probe_class="single_hop",
        corpus_scale="core",
        answer="an answer",
        model=model,
        harness_failure=harness_failure,
        turn_tokens=[
            TurnTokenUsage(
                turn=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
            )
        ],
    )
    cell = GridCell(probe="pto_allowance", arm="none", corpus_scale="core", replicate=0)
    return RolloutRow(cell=cell, record=record)


def test_spend_prices_input_and_output_at_the_models_own_rates() -> None:
    spend = noise_floor.compute_spend(
        [_row_with_tokens(input_tokens=1_000_000, output_tokens=100_000)],
        model_priced="claude-haiku-4-5-20251001",
    )
    # $1/MTok input, $5/MTok output.
    assert spend.usd == pytest.approx(1.0 + 0.5)
    assert spend.total_tokens == 1_100_000


def test_spend_applies_the_documented_cache_multipliers() -> None:
    spend = noise_floor.compute_spend(
        [_row_with_tokens(cache_creation=1_000_000, cache_read=1_000_000)],
        model_priced="claude-haiku-4-5-20251001",
    )
    # 1.25x on a cache write, 0.1x on a cache read, both at the INPUT rate.
    assert spend.billable_input_tokens == pytest.approx(1_350_000)
    assert spend.usd == pytest.approx(1.35)


def test_spend_counts_a_harness_failed_cell_it_was_paid_for() -> None:
    """A turn-cap kill is billed exactly like an answered cell. Dropping it
    would under-report a capped budget, which is the wrong direction."""
    spend = noise_floor.compute_spend(
        [_row_with_tokens(input_tokens=500_000, harness_failure="turn_cap")],
        model_priced="claude-haiku-4-5-20251001",
    )
    assert spend.rows_counted == 1
    assert spend.usd == pytest.approx(0.5)


def test_spend_prices_a_higher_tier_model_at_its_own_rate() -> None:
    haiku = noise_floor.compute_spend(
        [_row_with_tokens(input_tokens=1_000_000)],
        model_priced="claude-haiku-4-5-20251001",
    )
    sonnet = noise_floor.compute_spend(
        [_row_with_tokens(input_tokens=1_000_000, model="claude-sonnet-5")],
        model_priced="claude-sonnet-5",
    )
    assert sonnet.usd == pytest.approx(3 * haiku.usd)


def test_an_unstamped_store_is_priced_against_the_caller_supplied_label() -> None:
    rows = [_row_with_tokens(input_tokens=1_000_000, model="")]
    report = noise_floor.build_noise_floor_report(rows, model_label="claude-sonnet-5")
    assert report.spend.model_priced == "claude-sonnet-5"
    assert report.spend.usd == pytest.approx(3.0)


def test_a_stamped_store_ignores_the_label_and_prices_what_the_rows_say() -> None:
    """The rows are authoritative: a wrong --model-label must not be able to
    re-price a run whose model is recorded with the data."""
    rows = [_row_with_tokens(input_tokens=1_000_000, model="claude-haiku-4-5-20251001")]
    report = noise_floor.build_noise_floor_report(rows, model_label="claude-sonnet-5")
    assert report.spend.model_priced == "claude-haiku-4-5-20251001"
    assert report.spend.usd == pytest.approx(1.0)
