# SPDX-License-Identifier: Apache-2.0
"""Word-shingle overlap between two passages — one definition, two callers.

Layer 1. Pure text arithmetic: no I/O, no config, no model, no cost.

``distinctive_ngram_overlap`` was written for the north-star rollout report
(``tests/evals/north_star_report.py``) as the free, judge-free "did the answer
draw on what was delivered?" signal. Issue athenaeum#1585 gives
``push_metrics.determine_references`` the same question to answer, and a
*second* definition of "content signal" is the failure mode to avoid: the two
numbers would drift, and the measurement that scores the heuristic would stop
describing the heuristic. So the definition lives here and both callers import
it.

The signal is deliberately coarse. It says the answer reproduces distinctive
phrasing from the delivered text — it is NOT a claim-support check, which
needs a judge and is out of scope for both callers.
"""

from __future__ import annotations

import re

#: Lowercased alphanumeric word runs. Punctuation, markdown syntax and
#: whitespace are all separators, so ``fact.`` and ``fact`` shingle alike.
WORD_RE = re.compile(r"[a-z0-9]+")

#: Word-shingle size for :func:`distinctive_ngram_overlap`. 4 words is long
#: enough that a shared shingle is very unlikely by chance (unlike a
#: 1- or 2-gram, which overlaps between almost any two passages on the same
#: topic) while still short enough to survive light paraphrase of a
#: sentence fragment.
DISTINCTIVE_NGRAM_SIZE = 4


def ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    """The set of *n*-word shingles in *text*, lowercased. Empty when too short."""
    words = WORD_RE.findall(text.lower())
    if len(words) < n:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def distinctive_ngram_overlap(
    delivered: str, answer: str, n: int = DISTINCTIVE_NGRAM_SIZE
) -> float:
    """Fraction of *delivered*'s n-gram shingles that also appear in *answer*.

    ``overlap = |ngrams(delivered) & ngrams(answer)| / |ngrams(delivered)|``
    -- the denominator is the shingle count of the DELIVERED text (what
    there was to draw from), not the answer's, so the metric reads as
    "how much of what was delivered shows up verbatim in the answer",
    never the reverse. ``0.0`` when *delivered* is too short to have any
    n-grams at all (never a division by zero).

    This is a coarse, free, zero-judgment signal that the answer echoes
    delivered phrasing -- it is NOT a claim-support check (that requires a
    judge and is explicitly out of scope for both callers).
    """
    delivered_grams = ngrams(delivered, n)
    if not delivered_grams:
        return 0.0
    answer_grams = ngrams(answer, n)
    return len(delivered_grams & answer_grams) / len(delivered_grams)
