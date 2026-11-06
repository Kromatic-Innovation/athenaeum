# SPDX-License-Identifier: Apache-2.0
"""Pure, operator-overridable source-vs-source comparison (issue athenaeum#2330, TG2).

Parent epic: athenaeum#2145 ("trust graph"). This is the second slice (TG2):
a pure comparison function plus the config surface for the edges it
consults. **No caller is wired in this slice** — `corrections.py` (TG3) and
`resolutions.py` (TG4) are out of scope, as is edge transitivity/closure
(deferred, v1 has none) and witness/attestation strength (TG5/TG6).

Design context: :mod:`athenaeum.precedence` already ranks a *single* source
type against a fixed 9-tier ladder (`SOURCE_PRECEDENCE_TIERS`). This module
adds an operator-declared OVERRIDE layer on top of that ladder, scoped to a
domain of (entity type, field, evidence kind): two sources that the ladder
would rank one way can be flipped, for a *specific* domain, by a pairwise
edge an operator declares in `librarian.source_trust_edges`. Where no edge
matches, :func:`compare_sources` falls back to
:func:`athenaeum.precedence.source_rank` unchanged, so behaviour with no
edges configured (or the feature flag off) is byte-identical to today's
precedence-only comparison (AC1). Edges are **not transitive** in v1: ``A
over B`` and ``B over C`` never implies anything about ``A`` vs ``C``
(explicit scope cut, no closure computation anywhere in this module).

**Contradictory edges never silently pick a winner.** This mirrors the
"undeclared is incomparable, never lesser" rule at
`src/athenaeum/asserter_authority.py:34-41` (`asserter_authority.
AUTHORITY_RELATIONS` has the same four-outcome shape this module's
:data:`COMPARISON_RELATIONS` mirrors): if the configured edges assert BOTH
``A over B`` and ``B over A`` for overlapping domains, the result is
``incomparable`` — never a coin-flip, never "last edge wins".

Evidence-kind vocabulary (pinned for this work order; see
`docs/design/trust-graph.md`, TG1):

- The closed vocabulary an OPERATOR EDGE may name is exactly
  :data:`EVIDENCE_KINDS` — ``document``, ``verbal``, ``authored-by-agent``.
  A `librarian.source_trust_edges` entry whose `domain.evidence_kind` names
  anything outside this set is rejected at config load (see
  :func:`athenaeum.config.resolve_source_trust_edges`, which reads this
  module's vocabulary rather than keeping its own copy).
- A CLAIM whose evidence kind cannot be determined carries
  :data:`UNKNOWN_EVIDENCE_KIND` (``"unknown"``). This is a claim-side value
  only — an edge may never name it, which falls out for free because
  ``"unknown"`` is not a member of :data:`EVIDENCE_KINDS`.
- **Unknown-kind matching rule.** An edge that names one or more evidence
  kinds does NOT match a claim whose evidence kind is unknown. Only an edge
  whose `evidence_kind` is ABSENT (meaning "any") matches an unknown-kind
  claim. Rationale: an operator who scoped an edge to, say, documentary
  evidence did not thereby authorize that edge to override a claim whose
  evidence we failed to classify — the safe direction on an unclassifiable
  claim is the 9-tier `source_rank` fallback, not a narrower override that
  was never written with that claim in mind. See
  :func:`_evidence_kind_matches`.

Layering: L1 primitive. Imports only :mod:`athenaeum.precedence` and
:mod:`athenaeum.provenance` (both L1). No config, no LLM client, no
``promptwitness`` import, and no I/O of any kind — every function here is a
pure function over already-parsed Python values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, NamedTuple, Sequence

from athenaeum import precedence
from athenaeum.provenance import parse_source

__all__ = [
    "EQUAL",
    "EVIDENCE_KINDS",
    "INCOMPARABLE",
    "OUTRANKED",
    "OUTRANKS",
    "COMPARISON_RELATIONS",
    "UNKNOWN_EVIDENCE_KIND",
    "ComparisonResult",
    "SourceTrustDomain",
    "SourceTrustEdge",
    "TrustGraphConfigError",
    "compare_sources",
    "find_contradictory_edges",
    "parse_edge",
]

#: Outcomes `compare_sources` can return -- mirrors the shape of
#: `asserter_authority.AUTHORITY_RELATIONS` (four outcomes, not three):
#: an operator-declared pairwise edge can make one source strictly outrank
#: another, the two can be declared equal only via a tie in the fallback
#: ladder (edges never assert equality directly), or a contradiction can
#: make them incomparable.
OUTRANKS = "outranks"
OUTRANKED = "outranked"
EQUAL = "equal"
INCOMPARABLE = "incomparable"

COMPARISON_RELATIONS: frozenset[str] = frozenset({OUTRANKS, OUTRANKED, EQUAL, INCOMPARABLE})

#: Closed vocabulary an OPERATOR EDGE may name in `domain.evidence_kind`
#: (pinned for athenaeum#2330/TG2 by the work-order orchestrator; cross-check
#: against `docs/design/trust-graph.md` once TG1 lands and report any
#: discrepancy rather than silently reconciling either side).
EVIDENCE_KINDS: frozenset[str] = frozenset({"document", "verbal", "authored-by-agent"})

#: Claim-side-only value: a claim whose evidence kind could not be
#: determined. Deliberately NOT a member of :data:`EVIDENCE_KINDS`, which is
#: what makes "an edge may never name `unknown`" fall out of ordinary
#: vocabulary validation rather than needing a separate check.
UNKNOWN_EVIDENCE_KIND = "unknown"

#: Selector `type` segment follows the same restrictive shape as
#: `provenance.SourceRef.type` (`[a-z][a-z0-9_-]*`).
_SELECTOR_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]*\Z")

#: Source selector type that MUST name a ref (issue AC): "any api source" is
#: too broad to be a meaningful trust edge -- `api` always names a specific
#: third-party system (`api:govdb`, `api:apollo`), never "any api".
_REF_REQUIRED_TYPES: frozenset[str] = frozenset({"api"})


class TrustGraphConfigError(ValueError):
    """A malformed `librarian.source_trust_edges` entry (issue athenaeum#2330).

    Raised rather than silently dropping the edge: a typo in a precedence
    override is a correctness/evidence-ranking knob, not a nuisance -- the
    same reasoning `config.ReceiptAnchorConfigError` states for anchoring
    adapters. Covers: a non-dict edge, a missing/non-string `over`/`under`,
    an `api` selector with no `:ref`, a malformed selector string, and a
    `domain.evidence_kind` outside :data:`EVIDENCE_KINDS`.
    """


@dataclass(frozen=True)
class _Selector:
    """A parsed `over`/`under` edge endpoint: a `SourceRef` type token,
    optionally narrowed to one specific ref (`type:ref`). A bare type
    (no colon) matches ANY ref of that type -- this is deliberately more
    permissive than `provenance.parse_source`, which requires `type:ref`
    for an actual claim's source value; a selector is a MATCHER, not a
    claim, so "any api source" would be a legal selector shape were `api`
    not specifically required to narrow (see `_REF_REQUIRED_TYPES`).
    """

    type: str
    ref: str | None
    raw: str

    def matches(self, source_type: str, source_ref: str) -> bool:
        if self.type != source_type:
            return False
        if self.ref is None:
            return True
        return self.ref == source_ref

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.raw


@dataclass(frozen=True)
class SourceTrustDomain:
    """An edge's domain matcher. Each field is ``None`` ("any"), meaning the
    key was absent from the edge's `domain` mapping, or a non-empty
    `frozenset` of acceptable values for that key. A scalar in yaml (e.g.
    ``field: payout_amount``) and a list (``field: [payout_amount, salary]``)
    both normalize to a `frozenset` here -- see :func:`_parse_domain_key`.
    """

    entity_type: frozenset[str] | None = None
    field: frozenset[str] | None = None
    evidence_kind: frozenset[str] | None = None

    def overlaps(self, other: "SourceTrustDomain") -> bool:
        """Whether some concrete (entity_type, field, evidence_kind) triple
        could match BOTH domains at once. Used only by the load-time
        contradictory-edge lint (:func:`find_contradictory_edges`); claim
        matching uses :func:`matches`, which has the unknown-kind rule this
        method deliberately does not need (overlap is about two EDGES, not
        a claim).
        """
        for mine, theirs in (
            (self.entity_type, other.entity_type),
            (self.field, other.field),
            (self.evidence_kind, other.evidence_kind),
        ):
            if mine is None or theirs is None:
                continue
            if mine.isdisjoint(theirs):
                return False
        return True

    def matches(self, entity_type: str, field: str, evidence_kind: str) -> bool:
        """Whether this domain matcher matches a claim's CONCRETE domain.

        Implements the unknown-kind matching rule from the module
        docstring: an edge that names one or more evidence kinds never
        matches a claim whose evidence kind is :data:`UNKNOWN_EVIDENCE_KIND`
        -- only an edge with `evidence_kind` absent (``None`` here) matches
        an unknown-kind claim.
        """
        if self.entity_type is not None and entity_type not in self.entity_type:
            return False
        if self.field is not None and field not in self.field:
            return False
        if self.evidence_kind is not None:
            if evidence_kind == UNKNOWN_EVIDENCE_KIND:
                return False
            if evidence_kind not in self.evidence_kind:
                return False
        return True


@dataclass(frozen=True)
class SourceTrustEdge:
    """One operator-declared pairwise override: *over* outranks *under*
    within *domain*. *note* is free-form operator commentary, carried
    through for reason strings / log messages but never interpreted.
    """

    domain: SourceTrustDomain
    over: _Selector
    under: _Selector
    note: str | None = None

    def describe(self) -> str:
        suffix = f" ({self.note})" if self.note else ""
        return f"{self.over.raw!r} over {self.under.raw!r}{suffix}"


class ComparisonResult(NamedTuple):
    """:func:`compare_sources`'s return value: the relation (one of
    :data:`COMPARISON_RELATIONS`) plus a human-readable reason naming
    whichever edge(s) (or the `source_rank` fallback) produced it.
    """

    relation: str
    reason: str


def _parse_selector(raw: Any, *, which: str) -> _Selector:
    if not isinstance(raw, str) or not raw.strip():
        raise TrustGraphConfigError(
            f"source_trust_edges: {which!r} must be a non-empty string, got {raw!r}"
        )
    if raw != raw.strip():
        raise TrustGraphConfigError(
            f"source_trust_edges: {which!r} must not have leading/trailing "
            f"whitespace, got {raw!r}"
        )
    if ":" in raw:
        type_part, _, ref_part = raw.partition(":")
        if not ref_part:
            raise TrustGraphConfigError(
                f"source_trust_edges: {which!r} selector {raw!r} has a trailing "
                f"colon with no ref"
            )
    else:
        type_part, ref_part = raw, ""
    if not _SELECTOR_TYPE_RE.match(type_part):
        raise TrustGraphConfigError(
            f"source_trust_edges: {which!r} selector {raw!r} has an invalid "
            f"type segment (expected [a-z][a-z0-9_-]*)"
        )
    ref: str | None = ref_part if ref_part else None
    if type_part in _REF_REQUIRED_TYPES and ref is None:
        raise TrustGraphConfigError(
            f"source_trust_edges: {which!r} selector {raw!r} is type "
            f"{type_part!r}, which must name a ref (e.g. {type_part}:govdb) "
            f"-- a bare {type_part!r} selector is too broad"
        )
    return _Selector(type=type_part, ref=ref, raw=raw)


def _parse_domain_key(
    raw: Any, *, key: str, vocabulary: frozenset[str] | None
) -> frozenset[str] | None:
    """Normalize one `domain` entry (scalar, list, or absent) to a
    `frozenset`, or ``None`` for "absent / any". Shared by all three domain
    keys; *vocabulary*, when given, additionally rejects any value outside
    it (used only for `evidence_kind` -- `entity_type`/`field` are free-form
    operator-chosen strings with no closed vocabulary).
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        raise TrustGraphConfigError(
            f"source_trust_edges: domain.{key} must be a string or list of "
            f"strings, got {raw!r}"
        )
    if not values:
        raise TrustGraphConfigError(
            f"source_trust_edges: domain.{key} must not be an empty list"
        )
    normalized: list[str] = []
    for v in values:
        if not isinstance(v, str) or not v.strip():
            raise TrustGraphConfigError(
                f"source_trust_edges: domain.{key} entries must be non-empty "
                f"strings, got {v!r}"
            )
        if vocabulary is not None and v not in vocabulary:
            raise TrustGraphConfigError(
                f"source_trust_edges: domain.{key} value {v!r} is outside "
                f"the closed vocabulary {sorted(vocabulary)!r}"
            )
        normalized.append(v)
    return frozenset(normalized)


def parse_edge(raw: Any) -> SourceTrustEdge:
    """Parse and validate one `librarian.source_trust_edges` list entry.

    Raises :class:`TrustGraphConfigError` on any malformed shape --
    :func:`athenaeum.config.resolve_source_trust_edges` lets this propagate
    (config validation rejects a bad edge at load time, per the issue's
    acceptance criteria) rather than dropping it silently.
    """
    if not isinstance(raw, dict):
        raise TrustGraphConfigError(
            f"source_trust_edges: each entry must be a mapping, got {raw!r}"
        )
    domain_raw = raw.get("domain", {})
    if not isinstance(domain_raw, dict):
        raise TrustGraphConfigError(
            f"source_trust_edges: domain must be a mapping, got {domain_raw!r}"
        )
    domain = SourceTrustDomain(
        entity_type=_parse_domain_key(
            domain_raw.get("entity_type"), key="entity_type", vocabulary=None
        ),
        field=_parse_domain_key(domain_raw.get("field"), key="field", vocabulary=None),
        evidence_kind=_parse_domain_key(
            domain_raw.get("evidence_kind"), key="evidence_kind", vocabulary=EVIDENCE_KINDS
        ),
    )
    over = _parse_selector(raw.get("over"), which="over")
    under = _parse_selector(raw.get("under"), which="under")
    note_raw = raw.get("note")
    note = note_raw if isinstance(note_raw, str) and note_raw.strip() else None
    return SourceTrustEdge(domain=domain, over=over, under=under, note=note)


def find_contradictory_edges(
    edges: Sequence[SourceTrustEdge],
) -> list[tuple[int, int]]:
    """Return ``(i, j)`` index pairs, ``i < j``, where edge *i* and edge *j*
    have overlapping domains and assert OPPOSITE directions for the same
    selector pair (*i* says A over B, *j* says B over A). Pure function --
    the actual load-time warning is logged by
    :func:`athenaeum.config.resolve_source_trust_edges`, which calls this.
    """
    pairs: list[tuple[int, int]] = []
    for i, edge_i in enumerate(edges):
        for j in range(i + 1, len(edges)):
            edge_j = edges[j]
            if not edge_i.domain.overlaps(edge_j.domain):
                continue
            if edge_i.over.raw == edge_j.under.raw and edge_i.under.raw == edge_j.over.raw:
                pairs.append((i, j))
    return pairs


def _safe_source_type_ref(source: Any) -> tuple[str, str] | None:
    """Parse *source* (a claim's scalar/dict source value) into
    ``(type, ref)``, or ``None`` if unparseable. ``None`` here means no
    edge selector can ever match this source -- the caller falls through to
    the `source_rank` fallback, which has its own (identical) treatment of
    an unparseable source as rank :data:`athenaeum.precedence.UNKNOWN_SOURCE_RANK`.
    """
    try:
        ref = parse_source(source)
    except ValueError:
        return None
    if ref is None:
        return None
    return ref.type, ref.ref


def compare_sources(
    a: Any,
    b: Any,
    domain: dict[str, str],
    edges: Sequence[SourceTrustEdge] = (),
) -> ComparisonResult:
    """Compare sources *a* and *b* for the claim domain *domain*.

    *domain* is the CLAIM's concrete domain -- ``{"entity_type": ...,
    "field": ..., "evidence_kind": ...}`` -- not an edge's domain matcher
    (which may hold scalars/lists/absent keys; see :class:`SourceTrustDomain`
    for that shape). *evidence_kind* must be one of :data:`EVIDENCE_KINDS`
    or :data:`UNKNOWN_EVIDENCE_KIND`.

    Algorithm: collect every edge in *edges* whose domain matches *domain*
    and that relates *a* and *b* in either direction (`a over b`, or
    `b over a`). Exactly one direction yields :data:`OUTRANKS` /
    :data:`OUTRANKED`. Both directions yield :data:`INCOMPARABLE` -- a
    contradiction never silently picks a winner. No matching edge at all
    falls back to :func:`athenaeum.precedence.source_rank`, which is exactly
    today's (pre-trust-graph) behaviour, so an empty *edges* list (or the
    `librarian.source_trust_enabled` flag off, enforced by the caller, not
    here) reproduces `source_rank`-only comparisons byte-for-byte (AC1).
    Edges are **not transitive**: this function only ever looks at *a* and
    *b* directly against each edge's `over`/`under`, never at a third
    source, so `A over B` + `B over C` implies nothing about `A` vs `C`.
    """
    for key in ("entity_type", "field", "evidence_kind"):
        if key not in domain:
            raise ValueError(f"compare_sources: domain missing required key {key!r}")
    evidence_kind = domain["evidence_kind"]
    if evidence_kind != UNKNOWN_EVIDENCE_KIND and evidence_kind not in EVIDENCE_KINDS:
        raise ValueError(
            f"compare_sources: domain['evidence_kind'] {evidence_kind!r} is "
            f"neither in {sorted(EVIDENCE_KINDS)!r} nor {UNKNOWN_EVIDENCE_KIND!r}"
        )

    type_ref_a = _safe_source_type_ref(a)
    type_ref_b = _safe_source_type_ref(b)

    forward: list[tuple[int, SourceTrustEdge]] = []
    backward: list[tuple[int, SourceTrustEdge]] = []
    if type_ref_a is not None and type_ref_b is not None:
        type_a, ref_a = type_ref_a
        type_b, ref_b = type_ref_b
        for i, edge in enumerate(edges):
            if not edge.domain.matches(
                domain["entity_type"], domain["field"], evidence_kind
            ):
                continue
            if edge.over.matches(type_a, ref_a) and edge.under.matches(type_b, ref_b):
                forward.append((i, edge))
            if edge.over.matches(type_b, ref_b) and edge.under.matches(type_a, ref_a):
                backward.append((i, edge))

    if forward and backward:
        fi, fe = forward[0]
        bi, be = backward[0]
        return ComparisonResult(
            INCOMPARABLE,
            f"edge[{fi}] ({fe.describe()}) contradicts edge[{bi}] ({be.describe()})",
        )
    if forward:
        i, e = forward[0]
        return ComparisonResult(OUTRANKS, f"edge[{i}]: {e.describe()}")
    if backward:
        i, e = backward[0]
        return ComparisonResult(OUTRANKED, f"edge[{i}]: {e.describe()}")

    rank_a = precedence.source_rank(a)
    rank_b = precedence.source_rank(b)
    if rank_a < rank_b:
        return ComparisonResult(
            OUTRANKS, f"source_rank fallback: rank {rank_a} outranks rank {rank_b}"
        )
    if rank_a > rank_b:
        return ComparisonResult(
            OUTRANKED, f"source_rank fallback: rank {rank_a} outranked by rank {rank_b}"
        )
    return ComparisonResult(EQUAL, f"source_rank fallback: both rank {rank_a}")
