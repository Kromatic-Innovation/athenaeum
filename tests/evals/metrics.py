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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum


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


def outcome_histogram(outcomes: Iterable[PushOutcome]) -> dict[str, int]:
    """Count outcomes by ladder rung, for reporting.

    Every rung is present even at zero: a report that omits empty rungs makes
    'no CLEAN results at all' look like a missing row rather than a finding.
    """
    histogram = {rung.name: 0 for rung in RecallOutcome}
    for item in outcomes:
        histogram[item.outcome.name] += 1
    return histogram
