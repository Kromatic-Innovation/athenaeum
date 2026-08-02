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
