# SPDX-License-Identifier: Apache-2.0
"""Deterministic source-precedence ranker (issue athenaeum#797).

Implements `docs/design/field-corrections.md` §6.1: the tier-0 field-correction
applier cannot call an LLM, so the source-precedence taxonomy that until now
existed only as prose inside `resolutions._RESOLVE_SYSTEM` (consulted by a
live model at tier 3-4) needs a pure, in-process equivalent for tier 0.

**A tier may hold more than one source type.** Tier 2 of the canonical
taxonomy is "`linkedin:<username>` / `twitter:<username>` — user-curated
public profile": two tokens, one rank. `SOURCE_PRECEDENCE_TIERS` is
therefore one entry PER TIER carrying that tier's type tokens, not a flat
9-tuple indexed by position — a flat tuple cannot express "two tokens, one
rank" and would silently drop `twitter:` to the unknown-type default of 9,
seven ranks below its documented position. That is not hypothetical: the
first draft of the design doc omitted `twitter:` exactly this way.

DRIFT GUARD (site 4 of 4 — see `resolutions.py`'s drift-guard comment above
`_RESOLVE_SYSTEM`, `docs/design/conflict-resolution.md` §11, and the `9-tier` count
in `resolutions.py`'s module docstring): the tier list here — its order AND
each tier's membership — must agree with the `SOURCE-PRECEDENCE TAXONOMY`
block of `resolutions._RESOLVE_SYSTEM`, the canonical prose list every other
copy derives from. `tests/test_precedence.py` binds this module to that
prose block by PARSING it (not transcribing it), so an edit to the taxonomy
that forgets this site fails loudly instead of silently ranking a token 9
(indistinguishable from a genuine `unsourced`).

Layering: L1 primitive. Imports only :mod:`athenaeum.provenance` (also L1)
for :func:`~athenaeum.provenance.parse_source`. No config, no LLM client,
no I/O — a pure function over a source string/dict.
"""

from __future__ import annotations

from athenaeum.provenance import parse_source

#: One entry per precedence tier, highest first; index + 1 is the rank.
#: A tier may carry several source-type tokens that rank equally.
SOURCE_PRECEDENCE_TIERS: tuple[tuple[str, ...], ...] = (
    ("user",),  # 1  user said it directly
    ("linkedin", "twitter"),  # 2  user-curated public profile
    ("api",),  # 3  third-party authoritative source
    ("wikipedia",),  # 4  consensus public source
    ("agent-observed",),  # 5  derived from an in-session artifact
    ("claude",),  # 6  LLM-generated
    ("script",),  # 7  pipeline-generated, no upstream evidence
    ("model-prior",),  # 8  training-data assertion, no session evidence
    ("unsourced",),  # 9  always loses to any sourced claim
)

#: Rank assigned to a source type absent from every tier above, and to
#: ``None`` / an unparseable value. Indistinguishable from a genuine
#: ``unsourced`` claim by design (§6.1) — which is exactly why the drift
#: guard above must bind on tier MEMBERSHIP, not just tier count.
UNKNOWN_SOURCE_RANK = 9

#: Flattened ``type token -> 1-based rank`` lookup, derived from
#: ``SOURCE_PRECEDENCE_TIERS`` so the two can never drift from each other.
_RANK_BY_TYPE: dict[str, int] = {
    token: rank
    for rank, tier in enumerate(SOURCE_PRECEDENCE_TIERS, start=1)
    for token in tier
}


def source_rank(source: str | dict | None) -> int:
    """Return the 1-based precedence rank for a `SourceRef` shorthand.

    A source type absent from every tier ranks 9, as does ``None`` or an
    unparseable value (:func:`athenaeum.provenance.parse_source` raising
    ``ValueError``, or returning ``None`` for a ``None`` input).

    ``source_rank("twitter:someone")`` returns 2 (tier 2, alongside
    ``linkedin:``) — NOT 9. That specific case is the regression this
    function exists to prevent; see the module docstring.
    """
    if source is None:
        return UNKNOWN_SOURCE_RANK
    try:
        ref = parse_source(source)
    except ValueError:
        return UNKNOWN_SOURCE_RANK
    if ref is None:
        return UNKNOWN_SOURCE_RANK
    return _RANK_BY_TYPE.get(ref.type, UNKNOWN_SOURCE_RANK)
