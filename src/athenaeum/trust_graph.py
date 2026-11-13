# SPDX-License-Identifier: Apache-2.0
"""Pure, operator-overridable source-vs-source comparison (issue athenaeum#2330, TG2).

Experimental: may change or be removed without a deprecation cycle; the
trust-graph design is under reconsideration. Do not build on it.

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

**TG6 (issue athenaeum#2334): attestation tie-break and demote-on-broken-
chain.** Per operator ruling OQ1 (`docs/design/trust-graph.md` §8),
witness attestation never overrides rank — it only breaks a tie between
two sources :func:`compare_sources` already found :data:`EQUAL`, or
demotes a claim whose witness chain no longer verifies. See
:func:`attestation_for` and :func:`attestation_tiebreak` near the end of
this module. The adapter is deliberately dumb: it takes already-joined
plain fields (never a receipt, a ledger, or a ``promptwitness`` type) so
this module's "no ``promptwitness`` import, no I/O" invariant below
extends to TG6 unchanged — the caller (`corrections._claim_attestation`)
owns the join and the ledger read.

Layering: L1 primitive. Imports only :mod:`athenaeum.precedence` and
:mod:`athenaeum.provenance` (both L1). No config, no LLM client, no
``promptwitness`` import, and no I/O of any kind — every function here is a
pure function over already-parsed Python values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
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
    "ATTESTATION_STRENGTHS",
    "NO_ATTESTATION",
    "AttestedClaim",
    "ComparisonResult",
    "SourceTrustDomain",
    "SourceTrustEdge",
    "TrustGraphConfigError",
    "attestation_for",
    "attestation_tiebreak",
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
        """EXACT ref equality, not a prefix check -- deliberately (Seer
        finding, PR athenaeum#2356). `provenance.parse_source` treats the
        ENTIRE remainder after the first colon as one opaque ref, so
        ``api:apollo:2026-05-07`` parses to ``ref="apollo:2026-05-07"``, not
        a vendor-plus-date pair -- that shape is real and common in the
        live corpus (date-suffixed `api:` refs). A selector naming just
        ``api:apollo`` is therefore a DIFFERENT, shorter ref, and
        `parse_source` itself has no notion under which it and
        ``api:apollo:2026-05-07`` are "the same source"; this method's
        exact-equality compare already matches on that same (type, ref)
        normalization on both sides (see `_safe_source_type_ref`), which is
        the narrowest change available. Widening it to a prefix/sub-segment
        match would make a selector reach claims the operator never named,
        the opposite of the safe direction, and the issue asks only for
        "optionally narrowed to `type:ref`" -- not prefix semantics. An
        operator who wants to scope an edge to one specific dated pull
        writes that exact ref as the selector (fully supported; see
        `tests/test_trust_graph.py::TestSelectorMatchesDatedRefs`).
        """
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


#: The only keys a `librarian.source_trust_edges` entry may carry.
_EDGE_KEYS: frozenset[str] = frozenset({"domain", "over", "under", "note"})

#: The only keys an edge's `domain` mapping may carry.
_DOMAIN_KEYS: frozenset[str] = frozenset({"entity_type", "field", "evidence_kind"})


def _reject_unknown_keys(raw: dict[str, Any], *, allowed: frozenset[str], where: str) -> None:
    """Raise on any key in *raw* outside *allowed*.

    An unrecognized key is NOT defensively ignored here, unlike most of
    this module's value-shape checks: a misspelled key (``feild`` for
    ``field``, ``evidence_kinds`` for ``evidence_kind``) would otherwise
    vanish silently, and "absent key means any" would then treat the
    dimension the operator meant to constrain as a wildcard -- silently
    WIDENING what the edge overrides, the exact direction this module's
    docstring says must never happen quietly (the same reasoning that
    already rejects an out-of-vocabulary `evidence_kind` *value* at load
    time; a misspelled *key* deserves the same strictness as a bad value).
    """
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise TrustGraphConfigError(
            f"source_trust_edges: {where} has unrecognized key(s) {unknown!r} "
            f"(allowed: {sorted(allowed)!r}) -- a misspelled key would "
            f"otherwise silently fall back to 'any', widening the edge's scope"
        )


def parse_edge(raw: Any) -> SourceTrustEdge:
    """Parse and validate one `librarian.source_trust_edges` list entry.

    Raises :class:`TrustGraphConfigError` on any malformed shape --
    :func:`athenaeum.config.resolve_source_trust_edges` lets this propagate
    (config validation rejects a bad edge at load time, per the issue's
    acceptance criteria) rather than dropping it silently. This includes
    an unrecognized key at either the edge level (anything other than
    ``domain``/``over``/``under``/``note``) or the ``domain`` level
    (anything other than ``entity_type``/``field``/``evidence_kind``) --
    see :func:`_reject_unknown_keys`.
    """
    if not isinstance(raw, dict):
        raise TrustGraphConfigError(
            f"source_trust_edges: each entry must be a mapping, got {raw!r}"
        )
    _reject_unknown_keys(raw, allowed=_EDGE_KEYS, where="an edge")
    domain_raw = raw.get("domain", {})
    if not isinstance(domain_raw, dict):
        raise TrustGraphConfigError(
            f"source_trust_edges: domain must be a mapping, got {domain_raw!r}"
        )
    _reject_unknown_keys(domain_raw, allowed=_DOMAIN_KEYS, where="an edge's domain")
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


# ---------------------------------------------------------------------------
# TG6: attestation tie-break and demote-on-broken-chain (issue athenaeum#2334)
# ---------------------------------------------------------------------------
#
# Operator ruling OQ1 (`docs/design/trust-graph.md` §8): attestation is a
# TIE-BREAK and a DEMOTION only — it never overrides rank and never
# promotes a claim across tiers. The only legal caller of this section is
# `corrections.decide_verdict`'s own `incoming_rank == existing_rank`
# branch (TG3, issue athenaeum#2331): that branch is unreachable once a
# rank difference has already decided the verdict, so "never overrides
# rank" is structural (a property of WHERE this is called), not a rule
# enforced by anything checked in this section.
#
# **No `promptwitness` import anywhere in this module (issue athenaeum#2334
# AC6).** `ATTESTATION_STRENGTHS` below is a deliberate, hand-maintained
# duplicate of `promptwitness.signing.STRENGTHS` — never imported, because
# this module's whole layering point (L1, no `promptwitness`) is that
# trust code must never need to know a signing key exists. The two are
# kept from silently drifting apart by a MEMBERSHIP test in athenaeum's
# own suite (`tests/test_trust_graph.py::test_attestation_strengths_match_
# promptwitness_vocabulary`), never by an import here.
#
# **Nothing here does I/O, and nothing here reads a receipt directly
# (issue athenaeum#2334 AC4).** `attestation_for` is a pure function over
# already-joined plain fields its caller assembles —
# `corrections._claim_attestation` is that caller: it re-reads the
# matching `transcript_receipts.ReceiptEntry` and re-derives chain-
# intactness via a FRESH `witness_attestation.AttestationIndex` on every
# call, never cached across decisions (failure class F6, `docs/design/
# trust-graph.md` §4) and never written back to memory frontmatter or
# anywhere else.

#: Decision-time attestation-strength vocabulary, weakest to strongest —
#: see the section note above for why this is a duplicate, never an
#: import, of `promptwitness.signing.STRENGTHS`.
ATTESTATION_STRENGTHS: tuple[str, ...] = (
    "none",
    "software-file",
    "os-keystore",
    "hardware-nonexportable",
    "hardware-presence",
)

#: Anchor sources that do NOT count as "anchored" for the tie-break
#: (`docs/design/trust-graph.md` G2/F7): the mandatory local-clock
#: fallback anchorer proves only that a machine claimed a time, never a
#: third-party source. `None` (nothing anchors this line at all) is
#: equally inert.
_UNANCHORED_SOURCES: frozenset[str | None] = frozenset({None, "local-clock"})


class AttestedClaim(NamedTuple):
    """One claim's decision-time attestation summary (issue athenaeum#2334).

    ``effective_strength`` is always a member of :data:`ATTESTATION_STRENGTHS`
    — ``"none"`` means "no effective attestation". An unwitnessed claim, an
    unverified/local-clock-only witnessed claim, and a demoted (broken-
    chain) witnessed claim all produce the identical value, which is what
    makes AC1 (inertness) and AC2 (demotion) the same code path rather
    than two separately-maintained ones. Built fresh by
    :func:`attestation_for` on every call — never cached, never persisted
    (AC4).
    """

    effective_strength: str


#: The "no effective attestation" sentinel every unwitnessed claim starts
#: from, and the value a demoted (broken-chain) or unverified/local-clock-
#: only witnessed claim collapses to (AC1, AC2) — both compare identically
#: via :func:`attestation_tiebreak`.
NO_ATTESTATION = AttestedClaim(effective_strength="none")


def attestation_for(meta: Mapping[str, Any] | None) -> AttestedClaim:
    """Decision-time attestation for one claim, from already-joined fields.

    *meta* is the CALLER's own join (issue athenaeum#2334's adapter
    contract) of:

    - ``witness_ref`` — the claim's own frontmatter pointer, written only
      by :func:`athenaeum.frontmatter_edit.witnessed_source_fields` (which
      writes no strength of its own);
    - ``witness_strength_verified`` / ``witness_anchor_source`` — the
      matching receipt's fields (`athenaeum.transcript_receipts.
      ReceiptEntry`, populated by TG5's signature verification);
    - ``chain_intact`` — a bool the CALLER re-derives fresh, at decision
      time, via `athenaeum.witness_attestation.AttestationIndex.
      chain_intact_through` — never reused from whatever it was when the
      receipt was originally written (failure class F6).

    This function is pure: no I/O, no ``promptwitness`` import, and
    nothing it reads is written anywhere. Every one of the demotion/
    inertness rules below collapses to the identical :data:`NO_ATTESTATION`
    value, which is what lets AC1 and AC2 share one code path:

    - no ``witness_ref`` at all — an ordinary, unwitnessed claim, the ONLY
      shape any claim has on a store with no ledger (AC5);
    - ``chain_intact`` is ``False`` — AC2: a broken chain demotes to
      ``"none"``, which compares identically to an ordinary
      ``user:``/unwitnessed claim ("falls back to user: rank handling",
      per the issue text);
    - ``witness_strength_verified`` is ``None``, ``"none"``, or not a
      recognized :data:`ATTESTATION_STRENGTHS` member — AC1: unverified
      attestation has no effect on any comparison;
    - ``witness_anchor_source`` is absent or ``"local-clock"`` — AC1: a
      local-clock-only anchor has no effect either (G2/F7 in
      `docs/design/trust-graph.md`).
    """
    if not isinstance(meta, Mapping):
        return NO_ATTESTATION
    if not meta.get("witness_ref"):
        return NO_ATTESTATION
    if meta.get("chain_intact", True) is False:
        return NO_ATTESTATION
    strength = meta.get("witness_strength_verified")
    if (
        not isinstance(strength, str)
        or strength not in ATTESTATION_STRENGTHS
        or strength == "none"
    ):
        return NO_ATTESTATION
    anchor_source = meta.get("witness_anchor_source")
    if not isinstance(anchor_source, str) or anchor_source in _UNANCHORED_SOURCES:
        return NO_ATTESTATION
    return AttestedClaim(effective_strength=strength)


def attestation_tiebreak(a: AttestedClaim, b: AttestedClaim) -> str:
    """Compare two :class:`AttestedClaim`\\ s for the TG6 tie-break (issue
    athenaeum#2334, operator ruling OQ1).

    Returns one of :data:`OUTRANKS` / :data:`OUTRANKED` / :data:`EQUAL` —
    never :data:`INCOMPARABLE` (:data:`ATTESTATION_STRENGTHS` is a total
    order, unlike the edge-based comparison, which can contradict itself).
    The only legal caller is `corrections.decide_verdict`'s own
    ``incoming_rank == existing_rank`` branch: this function has no notion
    of rank at all, so "attestation never overrides rank" is enforced by
    WHERE it is called, not by anything checked in here.
    """
    rank_a = ATTESTATION_STRENGTHS.index(a.effective_strength)
    rank_b = ATTESTATION_STRENGTHS.index(b.effective_strength)
    if rank_a == rank_b:
        return EQUAL
    return OUTRANKS if rank_a > rank_b else OUTRANKED
