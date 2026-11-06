# SPDX-License-Identifier: Apache-2.0
"""Noise-floor report for the north-star grid (issue athenaeum#2309).

WHY THIS EXISTS. The north-star grid (``tests.evals.north_star_cli``) runs
every probe through every arm at R=2 replicates and reports per-cell
correctness, but nothing in this repo yet asks the prior question a reader
needs before trusting any arm-to-arm difference in that report: at the
achieved sample size, is a measured difference even DISTINGUISHABLE from
sampling noise? This module answers that, and nothing else -- it is not a
second grader and not a second report. Every correctness verdict it reports
comes from :func:`tests.evals.north_star_report.grade_correctness`, read
over rows decoded by :func:`tests.evals.north_star_report.load_rollout_rows_and_diagnostics`
-- the SAME functions ``north_star_report.py`` and ``paired_regrade.py``
already use. A second, divergent grading path in this repo would be a
defect, not a feature (this issue's own instruction).

**What "noise floor" means here.** With only R=2 replicates per cell, the
only direct evidence of measurement noise is how often the SAME probe,
graded twice (once per replicate) under the SAME arm, flips between
correct and incorrect. :func:`compute_replicate_variance` turns that
paired-difference signal into (a) a discordance rate and (b) an implied
standard deviation of the class-level correctness RATE, under the
assumption that the two replicates are independent, identically
noisy measurements of one true rate (stated explicitly at each call site,
never silently assumed). :func:`compute_mde` then asks the practical
question the epic's decision rule needs: at this noise level and this n,
how big would a real difference between one arm and the verdict arm
(``push_breadcrumb_pull``, ``tests.evals.north_star_report.DEFAULT_VERDICT_ARM``)
have to be before this grid could reliably detect it?

**Two formulas, by design, never collapsed into one number.**

1. **Paired / McNemar discordance-based MDE** (the honest one for THIS
   design, since the comparison is always the SAME probe graded under two
   arms). For *n* paired probe/replicate observations with discordant
   cells split ``p10`` (arm correct, verdict-arm wrong) and ``p01`` (arm
   wrong, verdict-arm correct), the exact variance of one paired
   difference ``d_i in {-1, 0, +1}`` is::

       Var(d_i) = p_disc - (p10 - p01)^2        where p_disc = p10 + p01

   (``E[d_i] = p10 - p01``, ``E[d_i^2] = p_disc`` since ``d_i^2 in
   {0, 1}``.) The minimum detectable difference at two-sided alpha and
   power ``1 - beta`` is the standard paired-samples formula::

       MDE_paired = (z_{alpha/2} + z_{beta}) * sqrt(Var(d_i)) / sqrt(n)

   No independence-between-arms assumption; this is what the paired
   design actually buys.

2. **Unpaired two-proportion normal approximation**, reported ONLY for
   comparison -- it throws away the pairing (treats the two arms' rates as
   two independent samples of size *n*), which overstates the combined
   variance whenever the two arms tend to agree on the same probes (the
   common case: an easy probe is easy for both arms, a hard one is hard
   for both). Pooled-variance-at-the-mean-rate form::

       MDE_unpaired = (z_{alpha/2} + z_{beta}) * sqrt(2 * pbar * (1 - pbar)) / sqrt(n)
       pbar = (rate_arm + rate_verdict_arm) / 2

Both use ``z_{alpha/2} = 1.9599639845400545`` (two-sided alpha=0.05) and
``z_{beta} = 0.8416212335729143`` (power=0.80), per this issue's fixed
alpha/power.

**Normal approximation is not honest at tiny n.** Several probe classes on
this grid have n=2 or n=3 probes (see athenaeum#2310's own framing). The
standard rule of thumb for a normal approximation to a binomial-flavoured
statistic -- here applied to the discordant count out of *n* -- is
``n * p_disc >= 5`` and ``n * (1 - p_disc) >= 5``. Below that,
:func:`compute_mde` still computes the number (so a JSON consumer is never
handed a missing field) but sets ``normal_approx_reliable = False``, and
:func:`render_report` prints "insufficient n for a normal approximation --
use an exact McNemar/binomial test" in its place rather than a number the
sample size cannot support.

**Required n** (:func:`compute_required_n`) inverts formula 1 above to
answer "how many probes per class would this grid need, at its OWN
measured noise level, to detect a difference of 0.05 / 0.10 / 0.15 /
0.20?" -- the issue's own acceptance criterion ("the report says how many
more are needed"). When the measured discordance is exactly zero (no
flips observed yet), the inversion is undefined (dividing by a
variance of zero) and this module says so rather than printing a
confident-looking number computed from no evidence.

**Provenance is asserted, never assumed** (:func:`assert_provenance`,
athenaeum#2310 AC5: "a noise floor is only readable against ONE
model+corpus+grader triple"). Two honesty notes this module's own callers
must not paper over:

* ``RolloutRecord.model`` (issue athenaeum#2309, added to
  ``tests/evals/rollout.py`` by a sibling lane working the same issue,
  stamped once per probe in ``run_probe_all_arms`` for every arm) is the
  authoritative per-row model field this module reads via
  :func:`_effective_model`. A store written BEFORE that field existed
  decodes every row's ``model`` as ``""`` ("unstamped") -- for exactly
  that case, :func:`_effective_model` falls back to
  :func:`_model_from_transcript`, which reads the ``claude -p
  --output-format stream-json`` wire format's own ``model`` key (the
  ``system``/``init`` event, mirrored on every ``assistant`` event's
  ``message.model`` -- verified against
  ``tests/evals/data/rollout/pull_stream_spike.jsonl``). That fallback
  only ever reaches the four cli-mode arms (PULL, PUSH_BREADCRUMB_PULL,
  NATIVE_GREP, NATIVE_INDEX); the four api-mode arms (NONE,
  PUSH_PAGES_UPPER_BOUND, PUSH_BREADCRUMB, ORACLE) carry no transcript
  shape to fall back to (see ``run_none`` et al.), so a pre-field store's
  api-mode rows have no recoverable model at all. This module surfaces
  that residual gap explicitly (:attr:`Provenance.model_unverified_arms`)
  rather than silently trusting "one CLI dispatch passes one ``--model``
  to every arm it runs" -- the gap :func:`_effective_model`'s fallback
  cannot close for those rows.
* ``grader_revision`` and ``git_sha`` are properties of the CODE computing
  THIS report, not of the code that produced the stored rows (the same
  convention ``north_star_report.build_report`` already uses for its own
  ``git_sha`` header field) -- they cannot "disagree" across rows and this
  module does not pretend to check that; it echoes them so a reader always
  knows which grader version read the store. ``search_backend`` and
  (where derivable) ``model`` genuinely are per-row data and genuinely can
  disagree, so those two are the ones :func:`assert_provenance` refuses to
  pool a mismatch on.

**Harness failure vs graded-incorrect, everywhere.** A row with
``RolloutRecord.harness_failure`` set (a turn-cap kill, a permission
request, an empty breadcrumb -- see ``tests/evals/rollout.py``) is never
treated as a wrong answer. It is excluded from every correctness rate,
every discordance pair, and every oracle counter-example entry computed
here, and counted separately (``n_harness_failures`` on the top-level
report, plus the named ``oracle_harness_failures`` list) -- the same
``graded_rows`` discipline ``north_star_report.build_report`` already
applies, reused rather than re-derived.

Run it (from the repository root)::

    python -m tests.evals.noise_floor --store STORE.jsonl --run-id RUN_ID \\
        --out noise-floor.md --json-out noise-floor.json

``--run-id`` is a caller-supplied LABEL only (for example a GitHub Actions
run id) -- the result store carries no run-id field of its own, so this
is never used to filter or select rows, only to stamp the output header
with what produced the store under measurement.

No real result-store path is hard-coded here, matching ``paired_regrade.py``'s
own public-repo discipline: the operator-host eval-store directory is
handed to a caller via the issue's own Plan section, never via a literal
path in this module.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from athenaeum.models import _rates_for_model
from athenaeum.shadow_linkage import _get_git_sha, _get_version
from athenaeum.store import now_iso
from tests.evals.containment import ResultStore
from tests.evals.north_star_report import (
    DEFAULT_VERDICT_ARM,
    GRADER_REVISION,
    RolloutRow,
    _corpus_for_scale,
    _probe_for_row,
    grade_correctness,
    load_rollout_rows_and_diagnostics,
)
from tests.evals.rollout import Arm, RolloutRecord

#: Two-sided alpha=0.05 critical value -- fixed by this issue's own ask,
#: never a CLI knob (a caller who wants a different alpha/power is asking
#: a different question than "the noise floor at the epic's fixed rule").
_Z_ALPHA_2: float = 1.9599639845400545
#: One-sided z for power=0.80.
_Z_BETA: float = 0.8416212335729143
_Z_SUM: float = _Z_ALPHA_2 + _Z_BETA

#: Target differences the required-n table answers (issue's own AC: "If
#: the probe count is too low for the epic's question, the report says how
#: many more are needed").
REQUIRED_N_TARGET_DELTAS: tuple[float, ...] = (0.05, 0.10, 0.15, 0.20)

#: Normal-approximation reliability rule of thumb applied to the discordant
#: count out of n (the standard "np >= 5 and n(1-p) >= 5" guideline, read
#: here against the discordant proportion rather than a raw success
#: proportion, since the discordant count is the Bernoulli quantity the
#: paired MDE formula's variance term is built from).
_NORMAL_APPROX_MIN_EXPECTED: float = 5.0

#: The probe class athenaeum#2303 added specifically as a positive control
#: for this issue's measurement (12 probes): the ceiling arm (ORACLE) is
#: expected to answer these, since they exist to prove the harness can
#: detect a real signal at all.
POSITIVE_CONTROL_PROBE_CLASS: str = "unprompted_push"


# ---------------------------------------------------------------------------
# Provenance: asserted, not assumed (athenaeum#2310 AC5)
# ---------------------------------------------------------------------------


class ProvenanceMismatchError(ValueError):
    """Raised by :func:`assert_provenance` when *rows* do not read back as
    ONE model+corpus+grader triple. A ``ValueError`` subclass (matching
    ``tests.evals.north_star_report.MixedFloorError``'s own precedent) so a
    caller that already catches ``ValueError`` around corpus-scale lookups
    is not forced to special-case this failure mode."""


@dataclasses.dataclass(frozen=True)
class Provenance:
    """The triple athenaeum#2310 AC5 says a noise floor is only readable
    against: model, corpus (per scale), grader revision -- plus
    ``search_backend`` and ``git_sha``, which the same reasoning applies
    to. See the module docstring's "Provenance is asserted, never assumed"
    section for what each field actually verifies versus merely echoes."""

    #: The one model every row's :func:`_effective_model` agrees on
    #: (preferring the authoritative ``RolloutRecord.model`` field, see
    #: that function's docstring), or ``None`` when no row in the store
    #: carries one at all (every row pre-dates ``RolloutRecord.model`` AND
    #: carries no cli-mode transcript to fall back to).
    model: str | None
    #: Arms present in *rows* whose rows carry NO per-row model evidence
    #: at all -- sorted, deduplicated. Empty on any store written after
    #: issue athenaeum#2309's ``RolloutRecord.model`` field landed (every
    #: arm is stamped by ``run_probe_all_arms``); non-empty only for a
    #: pre-field store whose api-mode rows have no transcript to fall back
    #: to either. Kept rather than deleted now that the common case is
    #: empty: a non-empty value here is exactly the gap this module
    #: refuses to paper over, on whichever store still has it.
    model_unverified_arms: tuple[str, ...]
    #: The one ``search_backend`` value across every row that carries one.
    #: ``None`` (never an error) when every row predates that field or
    #: carries no backend -- same "absent means not yet known" reading
    #: ``RolloutRecord.search_backend`` itself uses.
    search_backend: str | None
    #: The grading rule THIS report was computed under (a property of this
    #: module's import of ``north_star_report``, not of the stored rows --
    #: see the module docstring).
    grader_revision: str
    #: The git SHA of the checkout THIS report was computed from (same
    #: caveat as ``grader_revision`` above).
    git_sha: str
    #: athenaeum package version string, echoed for the same reason
    #: ``north_star_report.NorthStarReport.athenaeum_version`` is.
    athenaeum_version: str
    #: ``{corpus_scale: fingerprint}`` for every distinct ``corpus_scale``
    #: present in *rows*, computed fresh from the CURRENT checkout's
    #: ``tests.evals.corpus.build_corpus`` (the same call
    #: ``north_star_report.build_report`` already makes) -- a property of
    #: today's corpus-authoring code at that scale, not of the row.
    corpus_digests: dict[str, str]


def _model_from_transcript(record: RolloutRecord) -> str | None:
    """Best-effort model identity read off *record*'s own transcript --
    the FALLBACK path, used only when ``record.model`` is unset (see
    :func:`_effective_model`). Checks the ``system``/``init`` event's
    top-level ``model`` key first (present once, at ``transcript[0]``, for
    every cli-mode arm), then falls back to any ``assistant`` event's
    ``message.model`` -- both are the same value on every real transcript,
    but a hand-built test transcript need only supply one. Returns
    ``None`` for any transcript that carries neither (every api-mode arm
    on a pre-``RolloutRecord.model`` store, and an empty transcript)."""
    for event in record.transcript:
        if not isinstance(event, dict):
            continue
        model = event.get("model")
        if isinstance(model, str) and model:
            return model
        message = event.get("message")
        if isinstance(message, dict):
            model = message.get("model")
            if isinstance(model, str) and model:
                return model
    return None


def _effective_model(record: RolloutRecord) -> str | None:
    """The model *record* actually ran under, preferring the authoritative
    ``RolloutRecord.model`` field (issue athenaeum#2309: a sibling lane
    working the same issue added this field directly to
    ``tests/evals/rollout.py``, stamped once per probe in
    ``run_probe_all_arms`` beside ``llm_provider`` -- so every arm's row,
    api-mode included, now carries it). Falls back to
    :func:`_model_from_transcript` ONLY when ``record.model`` is the empty
    string -- ``RolloutRecord.model``'s own default/back-compat value for
    "unstamped": every row persisted before that field existed, and every
    record built directly without going through ``run_probe_all_arms``.
    The fallback still reaches only cli-mode rows; an api-mode row from a
    pre-field store has no recoverable model at all and this returns
    ``None`` for it."""
    return record.model or _model_from_transcript(record)


def assert_provenance(rows: Sequence[RolloutRow]) -> Provenance:
    """Read provenance off *rows*, refusing (:class:`ProvenanceMismatchError`)
    when the per-row fields that genuinely can disagree -- ``model`` and
    ``search_backend`` -- do. Never raises for
    ``grader_revision``/``git_sha``/``corpus_digests``: those are current-
    code properties this function echoes, not row data it compares (see
    the module docstring).

    Deliberately a direct, local mismatch check (mirroring
    ``north_star_report._pooled_floor_value``'s RAISING discipline) rather
    than a reuse of that module's own ``_pooled_model`` -- this issue's own
    acceptance criterion (AC5) is "exit non-zero naming the disagreement",
    where ``_pooled_model`` instead renders a soft ``"MIXED: a, b"`` string
    for its own report (a legitimate but DIFFERENT policy choice for a
    dimensional report that can still say something useful about mixed
    data; a noise floor's MDE/required-n numbers cannot)."""
    models: set[str] = set()
    unverified_arms: set[str] = set()
    backends: set[str] = set()
    scales: set[str] = set()
    for row in rows:
        scales.add(row.record.corpus_scale)
        model = _effective_model(row.record)
        if model is not None:
            models.add(model)
        else:
            unverified_arms.add(row.record.arm.value)
        if row.record.search_backend is not None:
            backends.add(row.record.search_backend)

    if len(models) > 1:
        raise ProvenanceMismatchError(
            f"noise_floor.assert_provenance: rows carry differing model values "
            f"{sorted(models)!r} -- a noise floor is only readable against ONE "
            "model+corpus+grader triple (athenaeum#2310 AC5). Split the store or "
            "dispatch one model at a time."
        )
    if len(backends) > 1:
        raise ProvenanceMismatchError(
            f"noise_floor.assert_provenance: rows carry differing search_backend "
            f"values {sorted(backends)!r} -- refusing to pool a vector-backend run "
            "together with an fts5 (or mixed) run into one noise-floor measurement."
        )

    digests = {scale: _corpus_for_scale(scale).fingerprint() for scale in sorted(scales)}
    return Provenance(
        model=next(iter(models), None),
        model_unverified_arms=tuple(sorted(unverified_arms)),
        search_backend=next(iter(backends), None),
        grader_revision=GRADER_REVISION,
        git_sha=_get_git_sha(),
        athenaeum_version=_get_version(),
        corpus_digests=digests,
    )


# ---------------------------------------------------------------------------
# Harness failure vs graded-incorrect -- one grading entry point
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GradedRow:
    """One row's grading outcome, with harness failure split out BEFORE
    correctness is even consulted -- no caller in this module reads
    ``.correct`` without checking ``.harness_failure`` first, which is
    exactly the defect class this dataclass exists to make structurally
    hard to reintroduce."""

    row: RolloutRow
    #: ``RolloutRecord.harness_failure`` is a reason string (``"turn_cap"``,
    #: etc.) or ``None``; this field collapses it to the bool every caller
    #: here actually branches on, keeping the reason string available on
    #: ``.row.record.harness_failure`` for anyone who wants it.
    harness_failure: bool
    #: ``None`` only for an ungradable probe (no answer_tokens/markers --
    #: a corpus authoring gap ``grade_correctness`` itself refuses to grade
    #: around, not a harness failure and not a graded miss). ``None``
    #: unconditionally when ``harness_failure`` is ``True``: a harness
    #: failure's answer text is never fed to the grader at all, so there is
    #: no correctness verdict to report for it -- see this dataclass's own
    #: invariant above.
    correct: bool | None


def grade_rows(rows: Sequence[RolloutRow]) -> tuple[GradedRow, ...]:
    """Grade every row in *rows*, reusing
    :func:`tests.evals.north_star_report.grade_correctness` exactly as
    ``north_star_report.build_report`` does -- never a second grading
    path. A harness-failed row's ``.correct`` is ``None`` by construction
    (the invariant :class:`GradedRow` documents), independent of what
    ``grade_correctness`` would have returned for its (likely truncated or
    permission-denied) answer text."""
    out: list[GradedRow] = []
    for row in rows:
        if row.record.harness_failure:
            out.append(GradedRow(row=row, harness_failure=True, correct=None))
            continue
        corpus = _corpus_for_scale(row.record.corpus_scale)
        probe = _probe_for_row(row)
        out.append(
            GradedRow(
                row=row, harness_failure=False, correct=grade_correctness(row.record, probe, corpus)
            )
        )
    return tuple(out)


def _gradable(graded: Sequence[GradedRow]) -> list[GradedRow]:
    """Rows with a defined correctness verdict: not a harness failure, and
    not an ungradable probe. The population every rate in this module is
    computed over."""
    return [g for g in graded if not g.harness_failure and g.correct is not None]


# ---------------------------------------------------------------------------
# Replicate-to-replicate variance (issue's item 2)
# ---------------------------------------------------------------------------

GroupKey = tuple[str, str, str]  # (probe_class, corpus_scale, arm)


def _group_key(g: GradedRow) -> GroupKey:
    record = g.row.record
    return (record.probe_class, record.corpus_scale, record.arm.value)


@dataclasses.dataclass(frozen=True)
class ReplicateVariance:
    """Replicate-to-replicate variance for one (probe_class, corpus_scale,
    arm) group, estimated from the PAIRED per-probe correctness
    difference between replicate 0 and replicate 1 -- the only noise
    evidence available at R=2 (see the module docstring)."""

    probe_class: str
    corpus_scale: str
    arm: str
    n_replicate0: int
    n_replicate1: int
    rate_replicate0: float | None
    rate_replicate1: float | None
    #: Probes gradable (not harness-failed, not ungradable) at BOTH
    #: replicates -- the paired-difference sample size.
    n_paired: int
    discordant: int
    concordant: int
    discordance_rate: float | None
    #: Exact ``Var(d_i)`` over the n_paired probes (see module docstring
    #: formula 1) -- ``None`` when ``n_paired == 0``.
    variance_of_difference: float | None
    #: SD of the MEAN difference (rate_replicate1 - rate_replicate0) over
    #: n_paired probes: ``sqrt(variance_of_difference / n_paired)``.
    sd_rate_difference: float | None
    #: Implied SD of a SINGLE replicate's class-level rate, under the
    #: stated assumption that the two replicates are iid noisy
    #: measurements of one true rate: ``sd_rate_difference / sqrt(2)``.
    sd_class_rate_implied: float | None
    #: Degrees of freedom for the paired-difference estimator: n_paired - 1
    #: (undefined, reported as -1, when n_paired == 0).
    df: int
    #: Fixed, human-readable name of the estimator used -- stated per the
    #: issue's own instruction ("state the estimator and its degrees of
    #: freedom in the output, not just the number").
    estimator: str = (
        "paired per-probe replicate-0-vs-replicate-1 discordance "
        "(exact Var(d_i) = p_disc - (p10-p01)^2, no normal-approximation "
        "assumption); class-rate SD assumes the two replicates are iid "
        "noisy measurements of one true rate"
    )


def compute_replicate_variance(graded: Sequence[GradedRow]) -> tuple[ReplicateVariance, ...]:
    """:class:`ReplicateVariance` for every (probe_class, corpus_scale,
    arm) group present in *graded*, sorted for a stable report ordering."""
    groups: dict[GroupKey, list[GradedRow]] = defaultdict(list)
    for g in graded:
        groups[_group_key(g)].append(g)

    out: list[ReplicateVariance] = []
    for (probe_class, corpus_scale, arm), group in sorted(groups.items()):
        by_probe_rep: dict[str, dict[int, GradedRow]] = defaultdict(dict)
        for g in group:
            by_probe_rep[g.row.record.probe_id][g.row.cell.replicate] = g

        rep0_gradable = [
            g
            for g in group
            if g.row.cell.replicate == 0 and not g.harness_failure and g.correct is not None
        ]
        rep1_gradable = [
            g
            for g in group
            if g.row.cell.replicate == 1 and not g.harness_failure and g.correct is not None
        ]
        rate0 = (
            sum(1 for g in rep0_gradable if g.correct) / len(rep0_gradable)
            if rep0_gradable
            else None
        )
        rate1 = (
            sum(1 for g in rep1_gradable if g.correct) / len(rep1_gradable)
            if rep1_gradable
            else None
        )

        p10 = p01 = 0
        n_paired = 0
        for reps in by_probe_rep.values():
            g0, g1 = reps.get(0), reps.get(1)
            if g0 is None or g1 is None:
                continue
            if g0.harness_failure or g1.harness_failure or g0.correct is None or g1.correct is None:
                continue
            n_paired += 1
            if g0.correct and not g1.correct:
                p10 += 1
            elif not g0.correct and g1.correct:
                p01 += 1

        var_d: float | None
        sd_rate_diff: float | None
        sd_class_rate: float | None
        discordance_rate: float | None
        if n_paired > 0:
            p10_rate, p01_rate = p10 / n_paired, p01 / n_paired
            p_disc = p10_rate + p01_rate
            var_d = max(p_disc - (p10_rate - p01_rate) ** 2, 0.0)
            sd_rate_diff = math.sqrt(var_d / n_paired)
            sd_class_rate = sd_rate_diff / math.sqrt(2)
            discordance_rate = p_disc
            df = n_paired - 1
        else:
            var_d = sd_rate_diff = sd_class_rate = discordance_rate = None
            df = -1

        out.append(
            ReplicateVariance(
                probe_class=probe_class,
                corpus_scale=corpus_scale,
                arm=arm,
                n_replicate0=len(rep0_gradable),
                n_replicate1=len(rep1_gradable),
                rate_replicate0=rate0,
                rate_replicate1=rate1,
                n_paired=n_paired,
                discordant=p10 + p01,
                concordant=n_paired - (p10 + p01),
                discordance_rate=discordance_rate,
                variance_of_difference=var_d,
                sd_rate_difference=sd_rate_diff,
                sd_class_rate_implied=sd_class_rate,
                df=df,
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# MDE: paired/McNemar + unpaired normal approximation (issue's item 3)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MDEResult:
    """Minimum detectable difference for one (arm, probe_class,
    corpus_scale) comparison against ``push_breadcrumb_pull`` -- the
    design-doc verdict arm (``DEFAULT_VERDICT_ARM`` imported from
    ``north_star_report``) -- at alpha=0.05 two-sided, power=0.80."""

    arm: str
    verdict_arm: str
    probe_class: str
    corpus_scale: str
    #: probe x replicate observations gradable under BOTH arms for this
    #: (probe_class, corpus_scale) -- the paired sample size.
    n: int
    rate_arm: float | None
    rate_verdict_arm: float | None
    p10: float  # arm correct, verdict_arm wrong (fraction of n)
    p01: float  # arm wrong, verdict_arm correct (fraction of n)
    mde_paired: float | None
    #: "np>=5 and n(1-p)>=5" applied to the discordant proportion -- False
    #: means the issue's own instruction applies: report that an exact
    #: test is needed, not this number, in rendered output (JSON keeps the
    #: number so a programmatic consumer is never handed a missing field).
    normal_approx_reliable: bool
    mde_unpaired: float | None
    estimator_paired: str = (
        "paired/McNemar: MDE = (z_a/2 + z_b) * sqrt(p_disc - (p10-p01)^2) / sqrt(n), "
        "alpha=0.05 two-sided, power=0.80"
    )
    estimator_unpaired: str = (
        "unpaired two-proportion normal approximation (pooled variance at the mean "
        "rate, SAME n per side, pairing structure discarded): "
        "MDE = (z_a/2 + z_b) * sqrt(2 * pbar * (1-pbar)) / sqrt(n)"
    )


def _paired_counts(
    arm_rows: dict[tuple[str, str, int], GradedRow],
    verdict_rows: dict[tuple[str, str, int], GradedRow],
) -> tuple[int, int, int, int, int]:
    """Over keys common to both maps (probe_id, corpus_scale, replicate),
    gradable in both: returns (n, correct_arm, correct_verdict, p10_count,
    p01_count)."""
    n = correct_arm = correct_verdict = p10 = p01 = 0
    for key in set(arm_rows) & set(verdict_rows):
        a, v = arm_rows[key], verdict_rows[key]
        if a.harness_failure or v.harness_failure or a.correct is None or v.correct is None:
            continue
        n += 1
        correct_arm += int(bool(a.correct))
        correct_verdict += int(bool(v.correct))
        if a.correct and not v.correct:
            p10 += 1
        elif not a.correct and v.correct:
            p01 += 1
    return n, correct_arm, correct_verdict, p10, p01


def compute_mde(
    graded: Sequence[GradedRow], *, verdict_arm: str = DEFAULT_VERDICT_ARM
) -> tuple[MDEResult, ...]:
    """:class:`MDEResult` for every (arm, probe_class, corpus_scale) where
    *arm* != *verdict_arm* has at least one probe/replicate paired against
    *verdict_arm* under the same probe_class/corpus_scale. Sorted for a
    stable report ordering."""
    by_group: dict[tuple[str, str, str], dict[tuple[str, str, int], GradedRow]] = defaultdict(dict)
    for g in graded:
        record = g.row.record
        key = (record.arm.value, record.probe_class, record.corpus_scale)
        by_group[key][(record.probe_id, record.corpus_scale, g.row.cell.replicate)] = g

    results: list[MDEResult] = []
    class_scales = sorted({(pc, cs) for (_, pc, cs) in by_group})
    for probe_class, corpus_scale in class_scales:
        verdict_key = (verdict_arm, probe_class, corpus_scale)
        verdict_rows = by_group.get(verdict_key, {})
        if not verdict_rows:
            continue
        for arm, pc, cs in sorted(by_group):
            if pc != probe_class or cs != corpus_scale or arm == verdict_arm:
                continue
            arm_rows = by_group[(arm, pc, cs)]
            n, correct_arm, correct_verdict, p10, p01 = _paired_counts(arm_rows, verdict_rows)
            if n == 0:
                continue
            rate_arm = correct_arm / n
            rate_verdict = correct_verdict / n
            p10_rate, p01_rate = p10 / n, p01 / n
            p_disc = p10_rate + p01_rate
            var_d = max(p_disc - (p10_rate - p01_rate) ** 2, 0.0)
            mde_paired = _Z_SUM * math.sqrt(var_d) / math.sqrt(n) if n > 0 else None
            reliable = (n * p_disc) >= _NORMAL_APPROX_MIN_EXPECTED and (
                n * (1 - p_disc)
            ) >= _NORMAL_APPROX_MIN_EXPECTED
            pbar = (rate_arm + rate_verdict) / 2
            mde_unpaired = _Z_SUM * math.sqrt(2 * pbar * (1 - pbar)) / math.sqrt(n)
            results.append(
                MDEResult(
                    arm=arm,
                    verdict_arm=verdict_arm,
                    probe_class=probe_class,
                    corpus_scale=corpus_scale,
                    n=n,
                    rate_arm=rate_arm,
                    rate_verdict_arm=rate_verdict,
                    p10=p10_rate,
                    p01=p01_rate,
                    mde_paired=mde_paired,
                    normal_approx_reliable=reliable,
                    mde_unpaired=mde_unpaired,
                )
            )
    return tuple(results)


# ---------------------------------------------------------------------------
# Required n (issue's item 4)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RequiredN:
    """How many paired probe observations the grid would need, AT THIS
    GROUP'S OWN MEASURED discordance variance, to detect each target
    difference in :data:`REQUIRED_N_TARGET_DELTAS` -- inverting
    :data:`MDEResult`'s paired formula for n."""

    arm: str
    verdict_arm: str
    probe_class: str
    corpus_scale: str
    n_achieved: int
    #: ``{delta: required_n}``, required_n is ``None`` when the measured
    #: variance of the paired difference is exactly zero (no discordance
    #: observed yet) -- the inversion is undefined there (see
    #: :func:`compute_required_n`'s docstring), not merely large.
    required_n: dict[float, int | None]


def compute_required_n(mde_results: Sequence[MDEResult]) -> tuple[RequiredN, ...]:
    """Invert the paired MDE formula (``n = ((z_a/2+z_b) * sd_d / delta)^2``)
    for each target delta, per :class:`MDEResult` row. A group whose
    measured ``p10 == p01 == 0`` (zero discordance at the achieved n) gets
    ``None`` for every delta: zero variance is not "this grid needs zero
    more probes", it is "this grid has not yet observed ANY disagreement
    to extrapolate noise from" -- reported honestly rather than as a
    confident-looking number computed from no evidence."""
    out: list[RequiredN] = []
    for m in mde_results:
        p_disc = m.p10 + m.p01
        var_d = max(p_disc - (m.p10 - m.p01) ** 2, 0.0)
        required: dict[float, int | None] = {}
        for delta in REQUIRED_N_TARGET_DELTAS:
            if var_d <= 0.0:
                required[delta] = None
            else:
                required[delta] = math.ceil((_Z_SUM * math.sqrt(var_d) / delta) ** 2)
        out.append(
            RequiredN(
                arm=m.arm,
                verdict_arm=m.verdict_arm,
                probe_class=m.probe_class,
                corpus_scale=m.corpus_scale,
                n_achieved=m.n,
                required_n=required,
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# The oracle counter-example (issue's item 5) + positive control (item 6)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ArmCorrectness:
    """Pooled, n-weighted correctness for one arm over every gradable row
    in scope (every probe_class/corpus_scale/replicate, harness failures
    excluded) -- explicitly n-weighted (a plain mean over gradable rows,
    never a mean-of-per-group-means) because an unweighted average of
    group means is exactly the shape that produces the oracle/
    push_breadcrumb_pull counter-example the issue names: a ceiling arm
    graded on a class/scale mix skewed toward harder cells can pool BELOW
    an arm graded on an easier mix, and averaging group means first would
    hide which."""

    arm: str
    n: int
    correct: int
    rate: float | None
    harness_failures: int


def compute_pooled_correctness_by_arm(graded: Sequence[GradedRow]) -> tuple[ArmCorrectness, ...]:
    by_arm: dict[str, list[GradedRow]] = defaultdict(list)
    failures_by_arm: dict[str, int] = defaultdict(int)
    for g in graded:
        arm = g.row.record.arm.value
        if g.harness_failure:
            failures_by_arm[arm] += 1
            continue
        by_arm[arm].append(g)
    arms = sorted(set(by_arm) | set(failures_by_arm))
    out = []
    for arm in arms:
        rows = _gradable(by_arm.get(arm, []))
        n = len(rows)
        correct = sum(1 for g in rows if g.correct)
        out.append(
            ArmCorrectness(
                arm=arm,
                n=n,
                correct=correct,
                rate=(correct / n if n else None),
                harness_failures=failures_by_arm.get(arm, 0),
            )
        )
    return tuple(out)


@dataclasses.dataclass(frozen=True)
class ArmClassScaleCorrectness:
    arm: str
    probe_class: str
    corpus_scale: str
    n: int
    correct: int
    rate: float | None
    harness_failures: int


def compute_correctness_by_arm_class_scale(
    graded: Sequence[GradedRow],
) -> tuple[ArmClassScaleCorrectness, ...]:
    by_key: dict[tuple[str, str, str], list[GradedRow]] = defaultdict(list)
    failures_by_key: dict[tuple[str, str, str], int] = defaultdict(int)
    for g in graded:
        record = g.row.record
        key = (record.arm.value, record.probe_class, record.corpus_scale)
        if g.harness_failure:
            failures_by_key[key] += 1
            continue
        by_key[key].append(g)
    keys = sorted(set(by_key) | set(failures_by_key))
    out = []
    for arm, probe_class, corpus_scale in keys:
        rows = _gradable(by_key.get((arm, probe_class, corpus_scale), []))
        n = len(rows)
        correct = sum(1 for g in rows if g.correct)
        out.append(
            ArmClassScaleCorrectness(
                arm=arm,
                probe_class=probe_class,
                corpus_scale=corpus_scale,
                n=n,
                correct=correct,
                rate=(correct / n if n else None),
                harness_failures=failures_by_key.get((arm, probe_class, corpus_scale), 0),
            )
        )
    return tuple(out)


@dataclasses.dataclass(frozen=True)
class OracleIncorrectProbe:
    """One probe where ORACLE (the ceiling arm) graded incorrect -- the
    evidence the issue's oracle-counter-example acceptance criterion needs
    to name the cause, with the SAME probe's push_breadcrumb_pull verdict
    alongside it (``None`` when push_breadcrumb_pull has no row for this
    exact probe/scale/replicate, which happens on a partial grid)."""

    probe_id: str
    probe_class: str
    corpus_scale: str
    replicate: int
    #: ``"harness_failure"`` is impossible here by construction (this list
    #: is built only from gradable ORACLE rows -- a harness-failed ORACLE
    #: row goes to :data:`oracle_harness_failures` instead, never here).
    push_breadcrumb_pull_correct: bool | None
    push_breadcrumb_pull_harness_failure: bool


def compute_oracle_incorrect(graded: Sequence[GradedRow]) -> tuple[OracleIncorrectProbe, ...]:
    """Every gradable ORACLE row graded ``False``, paired with
    push_breadcrumb_pull's verdict on the identical (probe_id,
    corpus_scale, replicate). Harness-failed ORACLE rows are never
    considered here -- see :data:`compute_oracle_harness_failures`, the
    sibling function this one deliberately does not fold into."""
    pbp_by_key: dict[tuple[str, str, int], GradedRow] = {}
    for g in graded:
        if g.row.record.arm is Arm.PUSH_BREADCRUMB_PULL:
            pbp_by_key[(g.row.record.probe_id, g.row.record.corpus_scale, g.row.cell.replicate)] = g

    out: list[OracleIncorrectProbe] = []
    for g in graded:
        if g.row.record.arm is not Arm.ORACLE or g.harness_failure or g.correct is not False:
            continue
        key = (g.row.record.probe_id, g.row.record.corpus_scale, g.row.cell.replicate)
        pbp = pbp_by_key.get(key)
        out.append(
            OracleIncorrectProbe(
                probe_id=g.row.record.probe_id,
                probe_class=g.row.record.probe_class,
                corpus_scale=g.row.record.corpus_scale,
                replicate=g.row.cell.replicate,
                push_breadcrumb_pull_correct=(
                    None if pbp is None or pbp.harness_failure else pbp.correct
                ),
                push_breadcrumb_pull_harness_failure=bool(pbp is not None and pbp.harness_failure),
            )
        )
    return tuple(
        sorted(out, key=lambda o: (o.probe_class, o.corpus_scale, o.probe_id, o.replicate))
    )


@dataclasses.dataclass(frozen=True)
class OracleHarnessFailure:
    """One ORACLE row excluded from the oracle counter-example because it
    is a HARNESS failure, not a wrong answer -- flagged separately per the
    issue's own instruction ("never pooled as if they were")."""

    probe_id: str
    probe_class: str
    corpus_scale: str
    replicate: int
    reason: str


def compute_oracle_harness_failures(
    graded: Sequence[GradedRow],
) -> tuple[OracleHarnessFailure, ...]:
    out = [
        OracleHarnessFailure(
            probe_id=g.row.record.probe_id,
            probe_class=g.row.record.probe_class,
            corpus_scale=g.row.record.corpus_scale,
            replicate=g.row.cell.replicate,
            reason=g.row.record.harness_failure or "unknown",
        )
        for g in graded
        if g.row.record.arm is Arm.ORACLE and g.harness_failure
    ]
    return tuple(
        sorted(out, key=lambda o: (o.probe_class, o.corpus_scale, o.probe_id, o.replicate))
    )


@dataclasses.dataclass(frozen=True)
class PositiveControlCell:
    """ORACLE's correctness on the :data:`POSITIVE_CONTROL_PROBE_CLASS`
    probes specifically, per (corpus_scale, replicate) -- the issue's own
    positive control: the ceiling arm should answer the new probes."""

    corpus_scale: str
    replicate: int
    n: int
    correct: int
    rate: float | None
    harness_failures: int


def compute_positive_control(graded: Sequence[GradedRow]) -> tuple[PositiveControlCell, ...]:
    by_key: dict[tuple[str, int], list[GradedRow]] = defaultdict(list)
    failures_by_key: dict[tuple[str, int], int] = defaultdict(int)
    for g in graded:
        record = g.row.record
        if record.arm is not Arm.ORACLE or record.probe_class != POSITIVE_CONTROL_PROBE_CLASS:
            continue
        key = (record.corpus_scale, g.row.cell.replicate)
        if g.harness_failure:
            failures_by_key[key] += 1
            continue
        by_key[key].append(g)
    keys = sorted(set(by_key) | set(failures_by_key))
    out = []
    for corpus_scale, replicate in keys:
        rows = _gradable(by_key.get((corpus_scale, replicate), []))
        n = len(rows)
        correct = sum(1 for g in rows if g.correct)
        out.append(
            PositiveControlCell(
                corpus_scale=corpus_scale,
                replicate=replicate,
                n=n,
                correct=correct,
                rate=(correct / n if n else None),
                harness_failures=failures_by_key.get((corpus_scale, replicate), 0),
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Actual spend (issue athenaeum#2309)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SpendTotals:
    """What a run ACTUALLY cost, summed from the rows it persisted.

    Issue athenaeum#2309 caps its own dispatches in dollars and requires the
    actual spend of every dispatch -- including a failed or partial one -- to
    be reported. A pre-flight estimate is not that number: run 42169666857
    tripped its own token ceiling at 1360 of 1824 cells, which means the
    estimate was wrong in the expensive direction and only the persisted
    rows can say by how much.

    Priced per :data:`athenaeum.models._MODEL_RATES_USD_PER_MTOK` with the
    cache multipliers that module documents (1.25x on cache writes, 0.1x on
    cache reads), the same method ``docs/design/eval-trigger-surface.md`` §2
    uses to price an eval dispatch. ``model_priced`` names the rate row
    actually applied -- for an unstamped store that is the caller's
    ``--model-label``, and the figure is only as good as that label.
    """

    model_priced: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    billable_input_tokens: float
    usd: float
    rows_counted: int

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )


def compute_spend(
    rows: Sequence[RolloutRow],
    *,
    model_priced: str,
) -> SpendTotals:
    """Price every persisted row's ``turn_tokens`` at *model_priced*'s rates.

    Counts EVERY row, graded or harness-failed: a cell that tripped the turn
    cap was paid for exactly like one that answered. ``cache_creation`` and
    ``cache_read`` are billed against the INPUT rate with their documented
    multipliers rather than ignored -- ignoring them under-reports spend,
    which is the wrong direction for a capped budget.
    """
    input_rate, output_rate = _rates_for_model(model_priced)
    plain = cache_create = cache_read = output = 0
    for row in rows:
        for turn in row.record.turn_tokens:
            plain += turn.input_tokens
            output += turn.output_tokens
            cache_create += turn.cache_creation_input_tokens
            cache_read += turn.cache_read_input_tokens
    billable_input = plain + 1.25 * cache_create + 0.1 * cache_read
    usd = (billable_input * input_rate + output * output_rate) / 1_000_000
    return SpendTotals(
        model_priced=model_priced,
        input_tokens=plain,
        output_tokens=output,
        cache_creation_tokens=cache_create,
        cache_read_tokens=cache_read,
        billable_input_tokens=billable_input,
        usd=usd,
        rows_counted=len(rows),
    )


# ---------------------------------------------------------------------------
# Top-level report assembly + rendering
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class NoiseFloorReport:
    run_id: str | None
    generated: str
    provenance: Provenance
    n_rows: int
    n_graded_rows: int
    n_harness_failures: int
    torn_rows: int
    duplicate_rows: int
    replicate_variance: tuple[ReplicateVariance, ...]
    mde: tuple[MDEResult, ...]
    required_n: tuple[RequiredN, ...]
    pooled_correctness_by_arm: tuple[ArmCorrectness, ...]
    correctness_by_arm_class_scale: tuple[ArmClassScaleCorrectness, ...]
    oracle_incorrect: tuple[OracleIncorrectProbe, ...]
    oracle_harness_failures: tuple[OracleHarnessFailure, ...]
    positive_control_unprompted_push: tuple[PositiveControlCell, ...]
    spend: SpendTotals


def build_noise_floor_report(
    rows: Sequence[RolloutRow],
    *,
    run_id: str | None = None,
    torn_rows: int = 0,
    duplicate_rows: int = 0,
    model_label: str | None = None,
) -> NoiseFloorReport:
    """Assemble a :class:`NoiseFloorReport` from decoded result-store rows.

    Pure computation -- makes no model call and no network call, exactly
    like ``north_star_report.build_report``. Raises
    :class:`ProvenanceMismatchError` via :func:`assert_provenance` before
    computing anything else: a mismatched model/search_backend makes every
    number below unreadable, so this refuses rather than computing a report
    that would misattribute one configuration's noise to another's.
    """
    provenance = assert_provenance(rows)
    graded = grade_rows(rows)
    gradable = _gradable(graded)
    n_harness_failures = sum(1 for g in graded if g.harness_failure)
    mde_results = compute_mde(graded)
    return NoiseFloorReport(
        run_id=run_id,
        generated=now_iso(),
        provenance=provenance,
        n_rows=len(rows),
        n_graded_rows=len(gradable),
        n_harness_failures=n_harness_failures,
        torn_rows=torn_rows,
        duplicate_rows=duplicate_rows,
        replicate_variance=compute_replicate_variance(graded),
        mde=mde_results,
        required_n=compute_required_n(mde_results),
        pooled_correctness_by_arm=compute_pooled_correctness_by_arm(graded),
        correctness_by_arm_class_scale=compute_correctness_by_arm_class_scale(graded),
        oracle_incorrect=compute_oracle_incorrect(graded),
        oracle_harness_failures=compute_oracle_harness_failures(graded),
        positive_control_unprompted_push=compute_positive_control(graded),
        # An unstamped store (every row written before athenaeum#2309's
        # RolloutRecord.model) has no model to price against, so the caller's
        # --model-label is used and named in the output as the thing the
        # figure rests on. Never a silent fallback to the blended rate.
        spend=compute_spend(
            rows, model_priced=(provenance.model or model_label or "unknown")
        ),
    )


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_report(report: NoiseFloorReport) -> str:
    """Compact markdown summary of *report* -- mirrors
    ``north_star_report.render_report``'s header convention (provenance
    first, then the numbered sections this issue asks for) rather than
    inventing a third report shape."""
    p = report.provenance
    lines: list[str] = []
    lines.append("# North-star grid noise floor (athenaeum#2309)")
    lines.append("")
    lines.append(f"- run_id: {report.run_id or 'n/a'}")
    lines.append(f"- generated: {report.generated}")
    lines.append(f"- model: {p.model or 'n/a (unstamped store, no recoverable model)'}")
    if p.model_unverified_arms:
        lines.append(
            "  - model NOT independently verified for arms: "
            + ", ".join(p.model_unverified_arms)
            + " (pre-athenaeum#2309 store rows with no RolloutRecord.model and no "
            "cli-mode transcript to fall back to)"
        )
    lines.append(f"- search_backend: {p.search_backend or 'n/a'}")
    lines.append(
        f"- grader_revision: {p.grader_revision} (code computing THIS report, not the rows)"
    )
    lines.append(f"- git_sha: {p.git_sha} (code computing THIS report, not the rows)")
    lines.append(f"- athenaeum_version: {p.athenaeum_version}")
    for scale in sorted(p.corpus_digests):
        lines.append(f"- corpus_digest[{scale}]: {p.corpus_digests[scale]}")
    lines.append(
        f"- rows: {report.n_rows} total, {report.n_graded_rows} gradable, "
        f"{report.n_harness_failures} harness failures, {report.torn_rows} torn, "
        f"{report.duplicate_rows} duplicate"
    )
    lines.append("")

    lines.append("## 1. Replicate-to-replicate variance")
    lines.append("")
    lines.append(
        "| probe_class | scale | arm | n (rep0/rep1) | rate rep0 | rate rep1 | "
        "n paired | discordance | SD(class rate) | df | estimator |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for rv in report.replicate_variance:
        lines.append(
            f"| {rv.probe_class} | {rv.corpus_scale} | {rv.arm} | "
            f"{rv.n_replicate0}/{rv.n_replicate1} | {_fmt(rv.rate_replicate0)} | "
            f"{_fmt(rv.rate_replicate1)} | {rv.n_paired} | {_fmt(rv.discordance_rate)} | "
            f"{_fmt(rv.sd_class_rate_implied)} | {rv.df} | paired discordance |"
        )
    lines.append("")

    lines.append(
        f"## 2. Minimum detectable difference vs `{DEFAULT_VERDICT_ARM}` (alpha=0.05, power=0.80)"
    )
    lines.append("")
    lines.append(
        "| arm | probe_class | scale | n | rate(arm) | rate(verdict) | "
        "MDE paired | MDE unpaired | normal approx reliable |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for m in report.mde:
        paired_cell = (
            _fmt(m.mde_paired)
            if m.normal_approx_reliable
            else f"{_fmt(m.mde_paired)} (insufficient n for normal approx -- use exact test)"
        )
        lines.append(
            f"| {m.arm} | {m.probe_class} | {m.corpus_scale} | {m.n} | "
            f"{_fmt(m.rate_arm)} | {_fmt(m.rate_verdict_arm)} | {paired_cell} | "
            f"{_fmt(m.mde_unpaired)} | {m.normal_approx_reliable} |"
        )
    lines.append("")
    lines.append(f"Formulas: {MDEResult.estimator_paired}")
    lines.append("")
    lines.append(f"Formulas: {MDEResult.estimator_unpaired}")
    lines.append("")

    lines.append("## 3. Required n to detect 0.05 / 0.10 / 0.15 / 0.20")
    lines.append("")
    lines.append("| arm | probe_class | scale | n achieved | n@0.05 | n@0.10 | n@0.15 | n@0.20 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in report.required_n:
        cells = [
            (
                "n/a (zero discordance observed -- cannot extrapolate)"
                if r.required_n[d] is None
                else str(r.required_n[d])
            )
            for d in REQUIRED_N_TARGET_DELTAS
        ]
        lines.append(
            f"| {r.arm} | {r.probe_class} | {r.corpus_scale} | {r.n_achieved} | "
            + " | ".join(cells)
            + " |"
        )
    lines.append("")
    unprompted = [r for r in report.required_n if r.probe_class == POSITIVE_CONTROL_PROBE_CLASS]
    if unprompted:
        lines.append(f"### `{POSITIVE_CONTROL_PROBE_CLASS}` specifically")
        lines.append("")
        for r in unprompted:
            delta_cells = {
                d: ("n/a" if r.required_n[d] is None else str(r.required_n[d]))
                for d in REQUIRED_N_TARGET_DELTAS
            }
            lines.append(
                f"- {r.arm} vs {r.verdict_arm} @ {r.corpus_scale}: n_achieved={r.n_achieved}, "
                f"required n = {delta_cells}"
            )
        lines.append("")

    lines.append("## 4. Oracle counter-example")
    lines.append("")
    lines.append("Pooled correctness per arm (n-weighted over every gradable row):")
    lines.append("")
    lines.append("| arm | n | correct | rate | harness failures |")
    lines.append("|---|---|---|---|---|")
    for a in report.pooled_correctness_by_arm:
        lines.append(f"| {a.arm} | {a.n} | {a.correct} | {_fmt(a.rate)} | {a.harness_failures} |")
    lines.append("")
    lines.append("Correctness per (arm, probe_class, corpus_scale):")
    lines.append("")
    lines.append("| arm | probe_class | scale | n | correct | rate | harness failures |")
    lines.append("|---|---|---|---|---|---|---|")
    for c in report.correctness_by_arm_class_scale:
        lines.append(
            f"| {c.arm} | {c.probe_class} | {c.corpus_scale} | {c.n} | {c.correct} | "
            f"{_fmt(c.rate)} | {c.harness_failures} |"
        )
    lines.append("")
    lines.append(f"Every `oracle` probe graded incorrect ({len(report.oracle_incorrect)} probes):")
    lines.append("")
    lines.append("| probe_class | scale | replicate | probe_id | push_breadcrumb_pull correct |")
    lines.append("|---|---|---|---|---|")
    for o in report.oracle_incorrect:
        pbp = (
            "harness_failure"
            if o.push_breadcrumb_pull_harness_failure
            else (
                "n/a (no row)"
                if o.push_breadcrumb_pull_correct is None
                else str(o.push_breadcrumb_pull_correct)
            )
        )
        lines.append(
            f"| {o.probe_class} | {o.corpus_scale} | {o.replicate} | {o.probe_id} | {pbp} |"
        )
    lines.append("")
    if report.oracle_harness_failures:
        lines.append(
            f"`oracle` rows excluded above as HARNESS FAILURES, not wrong answers "
            f"({len(report.oracle_harness_failures)}):"
        )
        lines.append("")
        lines.append("| probe_class | scale | replicate | probe_id | reason |")
        lines.append("|---|---|---|---|---|")
        for h in report.oracle_harness_failures:
            lines.append(
                f"| {h.probe_class} | {h.corpus_scale} | {h.replicate} | "
                f"{h.probe_id} | {h.reason} |"
            )
        lines.append("")

    lines.append(f"## 5. Positive control: `oracle` on `{POSITIVE_CONTROL_PROBE_CLASS}`")
    lines.append("")
    lines.append("| scale | replicate | n | correct | rate | harness failures |")
    lines.append("|---|---|---|---|---|---|")
    for pc in report.positive_control_unprompted_push:
        lines.append(
            f"| {pc.corpus_scale} | {pc.replicate} | {pc.n} | {pc.correct} | {_fmt(pc.rate)} | "
            f"{pc.harness_failures} |"
        )
    lines.append("")
    lines.append("## 6. Actual spend for this run")
    lines.append("")
    spend = report.spend
    lines.append(
        f"Priced at `{spend.model_priced}` rates with the cache multipliers "
        "`athenaeum.models` documents (1.25x on a cache write, 0.1x on a cache "
        "read), over EVERY persisted row -- a cell that tripped the turn cap "
        "was paid for exactly like one that answered."
    )
    lines.append("")
    lines.append(
        "| rows counted | input | cache write | cache read | output | billable input | USD |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(
        f"| {spend.rows_counted} | {spend.input_tokens:,} | "
        f"{spend.cache_creation_tokens:,} | {spend.cache_read_tokens:,} | "
        f"{spend.output_tokens:,} | {spend.billable_input_tokens:,.0f} | "
        f"${spend.usd:.2f} |"
    )
    lines.append("")
    lines.append(f"Total tokens: {spend.total_tokens:,}.")
    lines.append("")
    return "\n".join(lines)


def _to_jsonable(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def report_to_json(report: NoiseFloorReport) -> dict[str, object]:
    """JSON-safe dict for *report* -- every dataclass field recursively
    converted, dict keys stringified (``required_n``'s float-keyed dict
    needs this to round-trip through ``json.dumps``)."""
    payload = _to_jsonable(report)
    assert isinstance(payload, dict)
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--store", required=True, type=Path, help="append-only JSONL result store path"
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "caller-supplied label for what produced --store (e.g. a GitHub Actions "
            "run id) -- stamped into the output header only, never used to filter rows"
        ),
    )
    parser.add_argument(
        "--model-label",
        default=None,
        help=(
            "which model to PRICE an unstamped store against (issue athenaeum#2309). "
            "Ignored when the rows carry their own model, which is the authoritative "
            "source; used only for a store written before RolloutRecord.model existed, "
            "and named in the output so the spend figure is only ever as good as the "
            "label it rests on."
        ),
    )
    parser.add_argument("--out", required=True, type=Path, help="markdown report output path")
    parser.add_argument(
        "--json-out", required=True, type=Path, help="machine-readable JSON output path"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    diagnostics = load_rollout_rows_and_diagnostics(ResultStore(args.store))
    rows = diagnostics.rows
    if not rows:
        print(f"noise_floor: no rows in {args.store} -- nothing to measure", file=sys.stderr)
        return 1
    try:
        report = build_noise_floor_report(
            rows,
            run_id=args.run_id,
            torn_rows=diagnostics.torn,
            duplicate_rows=diagnostics.duplicates,
            model_label=args.model_label,
        )
    except ProvenanceMismatchError as exc:
        print(f"noise_floor: refusing to measure -- {exc}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_report(report), encoding="utf-8")
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(report_to_json(report), indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"noise_floor: wrote {args.out} and {args.json_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover -- CLI entry
    sys.exit(main())
