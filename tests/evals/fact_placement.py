# SPDX-License-Identifier: Apache-2.0
"""Offline fact-placement measure for the synthetic corpus (issue athenaeum#1658).

Sibling to :mod:`tests.evals.relatedness`, not an extension of it: relatedness
grades whether the right EDGES exist between pages; this module grades
whether a FACT sits on the right PAGE. The two are orthogonal -- a librarian
could write every expected edge in
``tests/evals/data/corpus/ground_truth/relatedness.yaml`` and still leave the
milestone-three payment on the person page, and that is exactly the gap this
module exists to make visible (see
:func:`tests.evals.test_person_company_project_eval` for the assertion that
the shipped athenaeum#1576 writer does precisely this).

No model call, no network, no index build: this reads page bodies off a
corpus a caller already holds and checks a distinctive substring's presence,
the same "offline, no model call" shape :mod:`tests.evals.relatedness` uses
for edges.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from tests.evals.corpus import FactPlacement, Page


@dataclass(frozen=True)
class FactPlacementScore:
    """One fact's placement result against a corpus.

    Both booleans are kept on the record, not collapsed into one pass/fail,
    for the same reason :class:`tests.evals.relatedness.RelatednessScore`
    keeps precision and recall apart: "the fact never moved" and "the fact
    was copied rather than moved" are different bugs with different fixes,
    and :attr:`satisfied` alone would blur them.
    """

    fact_id: str
    correct_on: str
    misplaced_on: str
    on_correct_page: bool
    on_misplaced_page: bool

    @property
    def satisfied(self) -> bool:
        """True only when the fact has been placed on the correct page AND
        removed from the wrong one. A writer that COPIES the fact onto the
        correct page without retracting it from the person would still leave
        the person's page making a claim the project page also makes --
        satisfied requires the retraction, not only the addition."""
        return self.on_correct_page and not self.on_misplaced_page

    def summary(self) -> str:
        return (
            f"{self.fact_id}: correct_on={self.correct_on} "
            f"(present={self.on_correct_page}) misplaced_on={self.misplaced_on} "
            f"(still present={self.on_misplaced_page}) satisfied={self.satisfied}"
        )


def score_fact_placement(pages: Iterable[Page], placement: FactPlacement) -> FactPlacementScore:
    """Score one :class:`~tests.evals.corpus.FactPlacement` against *pages*.

    *pages* is the corpus AS IT STANDS -- the committed fixture, or a corpus a
    librarian (or a replayed writer) has rewritten.
    """
    bodies = {page.uid: page.body for page in pages}
    correct_body = bodies.get(placement.correct_on, "")
    misplaced_body = bodies.get(placement.misplaced_on, "")
    return FactPlacementScore(
        fact_id=placement.fact_id,
        correct_on=placement.correct_on,
        misplaced_on=placement.misplaced_on,
        on_correct_page=placement.marker in correct_body,
        on_misplaced_page=placement.marker in misplaced_body,
    )


def score_fact_placements(
    pages: Iterable[Page], placements: Iterable[FactPlacement]
) -> list[FactPlacementScore]:
    """Score every placement in *placements* against *pages*."""
    materialized = list(pages)
    return [score_fact_placement(materialized, placement) for placement in placements]
