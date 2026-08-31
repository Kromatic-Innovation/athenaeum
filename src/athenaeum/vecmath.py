# SPDX-License-Identifier: Apache-2.0
"""Shared pure-Python vector math (issue athenaeum#542).

:func:`cosine` was previously duplicated verbatim as a ``_cosine`` private
helper in both :mod:`athenaeum.clusters` and :mod:`athenaeum.cross_scope`,
with three OTHER modules (:mod:`athenaeum.delta`,
:mod:`athenaeum.recurring_claims`, :mod:`athenaeum.fingerprint`) reaching
across module boundaries to import one sibling's private copy. This module
hoists the single implementation to an L0 leaf so every layer can import it
directly instead of importing another module's ``_``-prefixed symbol.

Layering: L0 primitive (leaf). May import only stdlib — no chromadb, no
:mod:`athenaeum.search`, no models. This is what makes it safe to import
top-level anywhere, including from function-local deferred-import call sites
that exist solely to avoid paying the optional ``chromadb`` ``[vector]``
extra's import cost (see :mod:`athenaeum.fingerprint`,
:mod:`athenaeum.clusters`, :mod:`athenaeum.delta`,
:mod:`athenaeum.cross_scope` — those deferrals are unrelated to this module
and are NOT to be un-deferred as part of this change).
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors. Zero on 0-norm.

    Returns ``0.0`` (rather than raising) when the vectors have mismatched
    lengths or either has zero norm, matching the pre-athenaeum#542 ``_cosine``
    behavior in both :mod:`athenaeum.clusters` and
    :mod:`athenaeum.cross_scope` (the two copies were identical).
    """
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def mean_pool(vectors: Sequence[Sequence[float]]) -> list[float]:
    """Element-wise mean of one-or-more equal-length vectors, re-normalized.

    Issue athenaeum#1140: a chunk-and-mean-pool embedding strategy needs to combine
    several chunk vectors for one document into a single vector. chromadb's
    default embedding function L2-normalizes every individual vector it
    produces (``onnx_mini_lm_l6_v2.py``'s ``_normalize``), so averaging
    several already-unit vectors and re-normalizing the mean is the
    standard recipe for a cosine-similarity consumer — the mean of unit
    vectors is not itself unit length, so skipping the re-normalize step
    would silently shrink the magnitude of any document embedded from more
    than one chunk relative to a single-chunk document, which would bias
    every downstream cosine comparison.

    A single-vector input is returned re-normalized (a no-op when it is
    already unit length). Returns ``[]`` for zero vectors, and returns the
    (unnormalized) mean unchanged when its norm is exactly zero — mirrors
    :func:`cosine`'s "zero norm is not an error" convention rather than
    raising.
    """
    if not vectors:
        return []
    dim = len(vectors[0])
    summed = [0.0] * dim
    for vec in vectors:
        for i, x in enumerate(vec):
            summed[i] += x
    count = float(len(vectors))
    mean = [x / count for x in summed]
    norm = math.sqrt(sum(x * x for x in mean))
    if norm == 0.0:
        return mean
    return [x / norm for x in mean]
