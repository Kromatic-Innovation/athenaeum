# SPDX-License-Identifier: Apache-2.0
"""Retrieval metrics and the push-outcome ladder.

No dependency, ~no cleverness: these are the standard ranked-retrieval
measures plus one domain-specific grading of what a recall push actually
achieved on a turn.

Why a ladder and not a score
----------------------------
A single number ('recall@5 = 0.8') blurs three failures that have entirely
different fixes:

* the right page was never suggested       -> an INDEX or ranking problem
* it was suggested but the agent ignored it -> a PRESENTATION or salience problem
* it was suggested along with four wrong pages -> a PRECISION problem, which
  costs tokens on every turn and is what makes a push expensive

The ladder keeps them separable while still ordering them, so a result can say
which of the three moved.

The ladder
----------

=====================  ==========================================
:attr:`MISS`           correct page not suggested (worst case)
:attr:`SUGGESTED`      correct page suggested (minimum acceptable)
:attr:`USED`           suggested AND the agent used it (good)
:attr:`CLEAN`          used, and ONLY correct pages pushed (ideal)
=====================  ==========================================

Two interpretive choices, stated because they are choices rather than
deductions:

1. :attr:`CLEAN` **requires** :attr:`USED`. A precise-but-ignored push is not
   better than an imprecise-but-load-bearing one, so precision alone does not
   promote past use. If you want precision reported independently of use --
   and you often will -- read :attr:`PushOutcome.precision`, which is always
   populated regardless of grade.
2. "Useful" is only genuinely established at :attr:`USED`. Ground truth can
   say a page is *correct*; only evidence that the agent drew on it shows it
   was *useful*. The ladder therefore treats correctness and usefulness as
   different rungs rather than a single judgment.

Abstention probes invert the ladder: there is no correct page, and the correct
behaviour is to push nothing. They are graded by :func:`grade_abstention`,
never by :func:`grade_push` -- scoring them on the same ladder would record a
permanent :attr:`MISS` for behaving correctly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from statistics import NormalDist
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from athenaeum.models import TokenUsage


class RecallOutcome(IntEnum):
    """Ordered outcome of one recall push. Higher is better.

    ``IntEnum`` so outcomes compare and sort directly (``outcome >=
    RecallOutcome.USED``) and aggregate without a lookup table.
    """

    MISS = 0
    SUGGESTED = 1
    USED = 2
    CLEAN = 3


@dataclass(frozen=True)
class PushOutcome:
    """A graded push, with the components that produced the grade.

    The components are always populated, including when they did not affect
    the grade -- a ladder that discarded them would make a regression visible
    but not diagnosable.
    """

    outcome: RecallOutcome
    expected_found: tuple[str, ...]
    expected_missing: tuple[str, ...]
    used: tuple[str, ...]
    noise: tuple[str, ...]
    precision: float
    recall: float

    @property
    def wasted_pages(self) -> int:
        """Pages pushed that were neither correct nor used.

        The per-turn cost of an imprecise push, and the quantity that makes
        PUSH more expensive than PULL whether or not it is more accurate.
        """
        return len(self.noise)


def grade_push(
    suggested: Sequence[str],
    expected: Iterable[str],
    used: Iterable[str] = (),
) -> PushOutcome:
    """Grade one push against ground truth and evidence of use.

    Args:
        suggested: page uids the sidecar actually pushed, in rank order.
        expected: uids ground truth says answer the query.
        used: uids with evidence the agent drew on them (see
            :func:`tests.evals.utilization` callers). Empty means "no evidence
            of use", which is NOT the same as evidence of non-use -- absence
            caps the grade at :attr:`SUGGESTED` rather than proving neglect.

    Returns:
        A :class:`PushOutcome` carrying the grade and its components.
    """
    expected_set = set(expected)
    if not expected_set:
        raise ValueError(
            "grade_push needs at least one expected uid; "
            "abstention probes belong in grade_abstention"
        )

    suggested_set = set(suggested)
    found = tuple(u for u in suggested if u in expected_set)
    missing = tuple(sorted(expected_set - suggested_set))
    used_correct = tuple(u for u in used if u in expected_set)
    noise = tuple(u for u in suggested if u not in expected_set)

    precision = len(found) / len(suggested) if suggested else 0.0
    recall = len(set(found)) / len(expected_set)

    if not found:
        outcome = RecallOutcome.MISS
    elif not used_correct:
        outcome = RecallOutcome.SUGGESTED
    elif noise:
        outcome = RecallOutcome.USED
    else:
        outcome = RecallOutcome.CLEAN

    return PushOutcome(
        outcome=outcome,
        expected_found=found,
        expected_missing=missing,
        used=used_correct,
        noise=noise,
        precision=precision,
        recall=recall,
    )


def grade_abstention(suggested: Sequence[str]) -> PushOutcome:
    """Grade a probe nothing in the corpus answers.

    Correct behaviour is an empty push. Anything pushed is a confabulation
    risk: the agent is handed plausible-looking context for a question the
    corpus cannot answer, which is strictly worse than being handed nothing.

    Graded :attr:`CLEAN` when nothing was pushed and :attr:`MISS` otherwise --
    the intermediate rungs have no meaning when there is no correct page.
    """
    noise = tuple(suggested)
    return PushOutcome(
        outcome=RecallOutcome.CLEAN if not noise else RecallOutcome.MISS,
        expected_found=(),
        expected_missing=(),
        used=(),
        noise=noise,
        precision=1.0 if not noise else 0.0,
        recall=1.0 if not noise else 0.0,
    )


# --------------------------------------------------------------------------
# Standard ranked-retrieval measures
# --------------------------------------------------------------------------


def recall_at_k(ranked: Sequence[str], expected: Iterable[str], k: int) -> float:
    """Fraction of expected uids appearing in the top ``k``."""
    expected_set = set(expected)
    if not expected_set:
        return 0.0
    return len(expected_set & set(ranked[:k])) / len(expected_set)


def precision_at_k(ranked: Sequence[str], expected: Iterable[str], k: int) -> float:
    """Fraction of the top ``k`` that are expected.

    Denominator is the number of results actually returned, not ``k``: a
    system returning two results, both correct, has precision 1.0 and should
    not be penalised for returning fewer than asked.
    """
    top = ranked[:k]
    if not top:
        return 0.0
    return len(set(top) & set(expected)) / len(top)


def mrr(ranked: Sequence[str], expected: Iterable[str]) -> float:
    """Reciprocal rank of the first expected uid; 0.0 if none appear.

    Rank-sensitive where :func:`recall_at_k` is not -- the difference between
    the right page at position 1 and at position 5 is invisible to recall@5
    but is most of the experienced quality of a push.
    """
    expected_set = set(expected)
    for index, uid in enumerate(ranked, start=1):
        if uid in expected_set:
            return 1.0 / index
    return 0.0


_UID_LINE = re.compile(r"^\*\*Uid:\*\*\s*(\S+)\s*$", re.MULTILINE)


def uids_from_recall_output(output: str) -> list[str]:
    """Extract ranked page uids from ``recall_search`` rendered output.

    Parses the explicit ``**Uid:**`` field rather than pattern-matching page
    filenames. An earlier version of this suite scraped ``([a-z0-9-]+)\\.md``
    and silently dropped every uid containing an underscore -- which happened
    to be the entire generated distractor tier. It reported "0/95 distractor
    slots" while distractors were in fact taking every top-5 slot, and nothing
    failed: a broken extractor returns a plausible number, not an error.

    Scoring code is the one place a bug is indistinguishable from a finding,
    so read the field the renderer actually emits.
    """
    return _UID_LINE.findall(output)


def outcome_histogram(outcomes: Iterable[PushOutcome]) -> dict[str, int]:
    """Count outcomes by ladder rung, for reporting.

    Every rung is present even at zero: a report that omits empty rungs makes
    'no CLEAN results at all' look like a missing row rather than a finding.
    """
    histogram = {rung.name: 0 for rung in RecallOutcome}
    for item in outcomes:
        histogram[item.outcome.name] += 1
    return histogram


# ---------------------------------------------------------------------------
# Calibration metrics (issue athenaeum#2211)
# ---------------------------------------------------------------------------
#
# Design docs/design/attribution-verification.md §12.3: per (tier, question
# class, signal), the calibration eval needs Wilson-interval accuracy,
# AUROC of the confidence score against correctness, a five-equal-count-bin
# reliability table, ECE, a threshold derived on the fit split, the
# abstain share, and cost per case. All of this is pure arithmetic over
# (score, correct) pairs -- no model call; canned-data unit tests cover it
# in tests/test_attribution_metrics.py.


def wilson_interval(successes: int, n: int, *, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the naive normal approximation for small ``n`` or a
    proportion near 0/1 -- exactly the regime a ~15-40-case held-out split
    per question class sits in. ``n <= 0`` returns ``(0.0, 1.0)`` (no
    evidence, maximal uncertainty) rather than raising, since a caller may
    be scanning several (tier, class, signal) combinations where some
    legitimately have zero cases.
    """
    if n <= 0:
        return (0.0, 1.0)
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    phat = successes / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * ((phat * (1 - phat) + z * z / (4 * n)) / n) ** 0.5
    lower = (center - margin) / denom
    upper = (center + margin) / denom
    # athenaeum#2211: at phat == 0 (or 1), `margin` and `center`'s second term
    # are EQUAL in real-number math (`phat * (1 - phat)` vanishes exactly),
    # so `lower`/`upper` should cancel to exactly 0.0/1.0 -- but `margin` is
    # reached through a sqrt that `center` never takes, so the two sides of
    # the cancellation are two independently-rounded floating-point paths,
    # not literally the same bit pattern. The residual left over (order
    # 1e-17, either sign) is a genuine rounding artifact of the CPU/libm
    # computing that sqrt, not a property of the statistics -- it was
    # observed to differ between aarch64 (exactly 0.0) and x86-64 CI
    # (~2e-17), which is what makes a bare `== 0.0` test architecture-
    # dependent without this clamp. A Wilson interval can never legitimately
    # sit outside `[0, 1]` NOR outside `[0, phat]` / `[phat, 1]` for its
    # lower/upper half respectively (the interval must contain its own point
    # estimate) -- clamping to `phat` on top of the `[0, 1]` floor/ceiling
    # removes the artifact at the one exact value (the point estimate) where
    # both the unclamped result AND the architecture-dependent noise live,
    # regardless of which direction the rounding happened to go.
    lower = min(max(0.0, lower), phat)
    upper = max(min(1.0, upper), phat)
    return lower, upper


def wilson_lower_bound(successes: int, n: int, *, confidence: float = 0.95) -> float:
    """The lower bound of :func:`wilson_interval` -- the pass-bar statistic
    (design §12.3: "the Wilson 95% lower bound of accuracy... is at least
    the target")."""
    return wilson_interval(successes, n, confidence=confidence)[0]


def auroc(scores: Sequence[float], labels: Sequence[bool]) -> float:
    """AUROC of ``scores`` ranking ``labels`` (``True`` = correct).

    Computed as the Mann-Whitney U statistic (ties count as half a point),
    which is numerically equivalent to the area under the ROC curve without
    needing a threshold sweep. Raises :class:`ValueError` when ``scores``
    carries no positive case or no negative case -- AUROC is undefined
    there, and a caller silently getting ``0.5`` back for a degenerate
    input would be worse than an explicit error.
    """
    positives = [s for s, label in zip(scores, labels) if label]
    negatives = [s for s, label in zip(scores, labels) if not label]
    if not positives or not negatives:
        raise ValueError(
            "auroc needs at least one positive and one negative case; "
            f"got {len(positives)} positive, {len(negatives)} negative"
        )
    total = 0.0
    for p in positives:
        for n in negatives:
            if p > n:
                total += 1.0
            elif p == n:
                total += 0.5
    return total / (len(positives) * len(negatives))


def _equal_count_bins(
    scores: Sequence[float], correct: Sequence[bool], *, n_bins: int
) -> list[list[tuple[float, bool]]]:
    """Partition ``(score, correct)`` pairs, sorted by score, into ``n_bins``
    equal-count chunks (the first ``n % n_bins`` chunks get one extra item).
    Empty when ``scores`` is empty; a chunk is never empty otherwise."""
    pairs = sorted(zip(scores, correct), key=lambda pair: pair[0])
    n = len(pairs)
    if n == 0:
        return []
    base, remainder = divmod(n, n_bins)
    chunks: list[list[tuple[float, bool]]] = []
    idx = 0
    for b in range(n_bins):
        size = base + (1 if b < remainder else 0)
        if size == 0:
            continue
        chunks.append(pairs[idx : idx + size])
        idx += size
    return chunks


def reliability_table(
    scores: Sequence[float], correct: Sequence[bool], *, n_bins: int = 5
) -> list[tuple[float, float, float]]:
    """Five (by default) equal-count reliability bins: ``[lo, hi, observed_accuracy]``.

    Design §12.3's "a reliability table over five equal-count bins
    (predicted probability against observed accuracy)" and §12.4's shipped
    ``bins:`` field shape. Equal-COUNT, not equal-width: a score
    distribution clustered near 1.0 (as a confident judge's usually is)
    would leave most equal-WIDTH bins empty.
    """
    bins: list[tuple[float, float, float]] = []
    for chunk in _equal_count_bins(scores, correct, n_bins=n_bins):
        lo = chunk[0][0]
        hi = chunk[-1][0]
        acc = sum(1 for _, c in chunk if c) / len(chunk)
        bins.append((lo, hi, acc))
    return bins


def expected_calibration_error(
    scores: Sequence[float], correct: Sequence[bool], *, n_bins: int = 5
) -> float:
    """ECE over the same equal-count bins :func:`reliability_table` uses.

    The size-weighted mean absolute gap between each bin's MEAN predicted
    score and its observed accuracy -- design §12.3's "ECE over those
    bins". ``0.0`` for an empty input (vacuously well-calibrated -- a
    caller checking ``ece <= 0.05`` on zero cases should not read a false
    pass from this; see the bin count in the same report).
    """
    chunks = _equal_count_bins(scores, correct, n_bins=n_bins)
    n = sum(len(c) for c in chunks)
    if n == 0:
        return 0.0
    total = 0.0
    for chunk in chunks:
        mean_predicted = sum(s for s, _ in chunk) / len(chunk)
        observed_accuracy = sum(1 for _, c in chunk if c) / len(chunk)
        total += (len(chunk) / n) * abs(mean_predicted - observed_accuracy)
    return total


def derive_threshold(
    scores: Sequence[float], correct: Sequence[bool], target_accuracy: float
) -> float | None:
    """The lowest score threshold on the FIT split clearing ``target_accuracy``.

    Design §12.3/§12.4: "the threshold derived on the fit split". Scans
    candidate thresholds (every distinct score, ascending) and returns the
    first whose at-or-above-threshold accuracy meets ``target_accuracy`` --
    the lowest such threshold, which is also the one with the lowest
    abstain share (:func:`abstain_share`) among thresholds that qualify.
    Returns ``None`` when no threshold (including the maximum score)
    clears the target -- design §12.4: "If no tier passes, the class has
    no recommended tier."
    """
    if not scores:
        return None
    for candidate in sorted(set(scores)):
        at_or_above = [c for s, c in zip(scores, correct) if s >= candidate]
        if not at_or_above:
            continue
        accuracy = sum(1 for c in at_or_above if c) / len(at_or_above)
        if accuracy >= target_accuracy:
            return candidate
    return None


def abstain_share(scores: Sequence[float], threshold: float) -> float:
    """Share of ``scores`` strictly below ``threshold`` (design §12.3's
    "abstain share... the expected human-queue share"). ``0.0`` for an
    empty ``scores``."""
    if not scores:
        return 0.0
    return sum(1 for s in scores if s < threshold) / len(scores)


def cost_per_case(usage: "TokenUsage", n_cases: int) -> float:
    """Mean cost per case, from an accumulated
    :class:`athenaeum.models.TokenUsage` (design §12.3's "cost per case,
    from the existing TokenUsage accounting"). ``0.0`` for ``n_cases <= 0``
    rather than raising a division error."""
    if n_cases <= 0:
        return 0.0
    return usage.estimated_cost_usd / n_cases
