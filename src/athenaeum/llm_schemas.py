# SPDX-License-Identifier: Apache-2.0
"""Observe-only schema validation of LLM response payloads (issue athenaeum#570, M17 phase 1).

Every JSON-shaped prompt contract in this codebase is parsed today by a
hand-rolled, individually-tuned check at its call site (coerce ``entity_type``
to a default, clamp ``confidence`` to ``[0, 1]``, basename-fallback a member
path, …). Those checks are correct but silent: when a model starts emitting a
new field, drops a required one, or returns a value outside the accepted
vocabulary, nothing records it — so there is no data on *how often* real
responses drift from the shape each site assumes.

This module is **phase 1** of closing that gap. It defines one Pydantic response
model per in-scope contract and a single :func:`observe` entry point that call
sites invoke *after* their existing parsing/coercion. The convention, and its
load-bearing constraint:

    **Observe only. A schema mismatch NEVER changes what the pipeline does with
    a response.** :func:`observe` validates, emits one structured WARNING per
    mismatch, and returns ``None``. It never raises, never mutates the payload,
    never signals the caller to reject/drop/coerce/re-request. The reject-vs-
    degrade decision is deliberately deferred to athenaeum#608, which consumes the
    per-contract mismatch rate this module produces; the T1/T2 authority
    contracts in ``reasoning_tiers.py`` are deferred to athenaeum#609 and are NOT modeled
    here (touching them is out of scope).

    **M17 phase 2a (athenaeum#1035) records a per-contract strictness decision for
    two of the six contracts** — see :data:`STRICT_CONTRACTS` for the decided
    posture, the measured window it is drawn from, and the reasoning.

    **M17 phase 2 (athenaeum#608) closes the remaining decision** over a
    28-day window, and its answer to the reject-vs-degrade question is:
    **degrade, everywhere, at this boundary.** :func:`observe` keeps its
    never-raise contract for every contract, decided rather than deferred —
    the "reject" teeth live in each site's own hand-rolled guard, which
    already degrades to a documented safe fallback, and adding a second gate
    here would duplicate them without adding protection. What a per-contract
    strictness setting therefore decides is the **schema SHAPE** — which
    keys are expected and which fields are required — so that a *future*
    drift is classified honestly in the observation log. Three more
    contracts (``query_topics``, ``claim_kind``, ``contradictions``) join
    :data:`STRICT_CONTRACTS` on that basis; ``resolutions`` alone stays
    deferred, for an under-sampled denominator rather than an unclear
    answer (see :data:`STRICT_CONTRACTS` for its stated release bar).

Convention for adding a contract (so other sites can follow this one):

1. Model **the shape the site accepts today**, not a tightened ideal. A field
   the site tolerates as missing (because it defaults/coerces it) is
   ``Optional`` here; a field whose value the site *rejects* when out of
   vocabulary (``claim_kind``, ``action``) is a ``Literal`` so an
   out-of-vocabulary value logs as genuine drift.
2. Use ``extra="allow"`` — an unexpected key is signal, not an error, and
   :func:`observe` reports which keys appeared (top-level and per list item)
   so a newly-emitted field surfaces without failing validation. A contract
   may earn a per-contract exception to this default once a measured window
   justifies one (see :data:`STRICT_CONTRACTS`) — that is a decision made
   FROM the data this convention produces, never assumed up front.
3. Validate the payload **after** the site's own parse/coercion, so the log
   reflects model drift rather than re-deriving what the hand-rolled checks
   already normalize.
4. Add an ``observe_<contract>`` convenience wrapper and call it from the parse
   site with a stable ``call_site`` label. Import it *lazily* at the call site
   (``from athenaeum.llm_schemas import observe_...``) so ``pydantic`` never
   loads on the import graph of the recall hot path (``query_topics`` →
   ``tiers``), which runs on every prompt under a 3s budget. This module is
   deliberately import-light — it pulls only ``pydantic`` and the lightweight
   ``models.CLAIM_KINDS`` set, never a heavy module like ``resolutions``.

The two vocabulary Literals (:data:`_CLAIM_KINDS` / :data:`_RESOLVER_ACTIONS`)
are stated locally rather than imported so this module stays import-light; a
test (``test_llm_schemas``) asserts each equals its live source set, so an
upstream vocabulary change fails CI here instead of silently desyncing.

Logging routes through the standard module logger, so the per-run correlation
id established by athenaeum#540 (``[run:<id>]`` in the shared format) is stamped onto
every mismatch line automatically. The WARNING marker is greppable:

    llm-schema-mismatch contract=<name> call_site=<label> error_class=<cls> \
        errors=[<field.path: msg>, …] extra_keys=[<key>, …]

Aggregating those by ``contract=`` yields the per-contract mismatch count that
is athenaeum#608's input.

**Contract:** :func:`observe` (and its per-contract ``observe_<name>``
wrappers) validates an already-parsed LLM response payload against a Pydantic
model and logs a WARNING on mismatch — it NEVER raises, NEVER mutates the
payload, and NEVER changes what the caller does with the response. Read the
load-bearing constraint above again: this is a logging side-channel, not a
gate.

**Factoring rule:** this module owns the response SHAPE models and the
observe-and-log entry point only. It does not own parsing/coercion (each call
site's existing hand-rolled logic runs first, unchanged) and does not own the
reject-vs-degrade decision (deferred to athenaeum#608).

**Layering:** L3 service. Module scope imports only ``pydantic`` — no
athenaeum imports at all, including :mod:`athenaeum.models` (the two
vocabulary tuples are stated LOCALLY rather than imported, kept in sync by a
test rather than a dependency) — so this module stays off the recall hot
path's (:mod:`athenaeum.query_topics`) import graph until a call site
explicitly, lazily imports an ``observe_*`` wrapper.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, RootModel, ValidationError

log = logging.getLogger(__name__)

#: Greppable marker prefixing every mismatch WARNING (one per response with a
#: validation error and/or an unexpected key). Aggregate by ``contract=``.
SCHEMA_MISMATCH_MARKER = "llm-schema-mismatch"

#: Durable, append-only observation ledger (issue athenaeum#724). Under the cache
#: dir — same discipline as ``push_metrics``' ``_push_records.jsonl`` — because
#: the ``query_topics`` contract runs inside the MCP server, whose Python logging
#: is retained NOWHERE (athenaeum#724 defect 3): a log-only marker there is
#: emitted and discarded, so the mismatch rate is unmeasurable. A ledger the
#: aggregation reads fixes that. Each line records ONE observation — outcome
#: ``ok``/``mismatch``, the mismatch class(es), and the contract/call-site — so
#: every reported rate has a real denominator (athenaeum#724 defect 2), and a
#: total parse failure (which returns BEFORE the payload ever reaches
#: :func:`observe`, athenaeum#724 defect 1) is counted via
#: :func:`observe_parse_failure`.
#:
#: **This ledger is production-only** (issue athenaeum#750). ``tests/conftest.py``
#: carries an autouse fixture that isolates ``ATHENAEUM_CACHE_DIR`` to a
#: per-test tmp dir and defaults ``ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED=0``,
#: so the test suite no longer appends to the real ``~/.cache/athenaeum``
#: ledger by default — trustworthy (free of test-run pollution) for records
#: written from 2026-08-05 onward. A test that specifically needs to exercise
#: this module's ledger (``tests/test_llm_schemas.py``) opts back in
#: explicitly: pass ``cache_dir=`` (wins over the env var per
#: :func:`athenaeum.config.resolve_cache_dir`'s precedence) and/or
#: ``monkeypatch.setenv("ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED", "1")``. See
#: ``docs/configuration.md`` § "LLM schema-observation ledger".
OBSERVATIONS_FILENAME = "_llm_schema_observations.jsonl"

#: Schema version stamped on every observation record.
OBSERVATION_SCHEMA_VERSION = 1

#: The four mismatch classes athenaeum#724 requires so athenaeum#608's
#: reject-vs-degrade question can be answered per class without re-reading raw
#: logs. ``extra-keys`` (validated OK, unexpected keys present) is a very
#: different signal from ``missing-required``; ``parse-fail`` (no JSON at all) is
#: the most extreme missing-required case and the one the pre-athenaeum#724
#: instrument could not see; ``wrong-type`` is a present-but-mistyped field.
MISMATCH_EXTRA_KEYS = "extra-keys"
MISMATCH_MISSING_REQUIRED = "missing-required"
MISMATCH_PARSE_FAIL = "parse-fail"
MISMATCH_WRONG_TYPE = "wrong-type"
MISMATCH_OTHER = "other"

#: Every contract :func:`observe` instruments, so :func:`aggregate_observations`
#: can report a contract with ZERO observations as an EXPLICIT *no-data* row
#: rather than a silent absence (athenaeum#724). ``claim_kind`` was a structural
#: no-data row before athenaeum#742 wired ``stamp_claim_kind`` into the nightly
#: auto-memory intake phase (``librarian._stamp_unclassified_claim_kinds``,
#: called from ``_run_auto_memory_phase``); now that real traffic flows, its
#: row reports actual observations like every other contract here. Surfacing
#: a genuinely-unreached contract explicitly (as this tuple still does for any
#: future no-caller addition) is what keeps a permanent no-data contract from
#: reading as "0 mismatches" (a false clean signal).
INSTRUMENTED_CONTRACTS: tuple[str, ...] = (
    "query_topics",
    "claim_kind",
    "contradictions",
    "resolutions",
    "tiers.tier2",
    "tiers.tier3-merge",
)

#: **M17 phase 2a strictness decision (athenaeum#1035, split from athenaeum#608).**
#: Contracts whose response model has graduated from the phase-1 uniform
#: ``extra="allow"``/decision-deferred posture to a per-contract posture decided
#: from a measured window, rather than the module-wide default every other
#: contract still carries. Membership here means "the schema below is the
#: DECIDED shape", not merely "the shape the site happens to tolerate today"
#: (the phase-1 convention in the module docstring above).
#:
#: Decided from ``~/.cache/athenaeum/_llm_schema_observations.jsonl``, window
#: 2026-08-05T13:12Z -> 2026-08-20T10:58Z (3,634 records, 0 unparseable),
#: recorded on athenaeum#608's 2026-08-20 comment and carried into athenaeum#1035's
#: issue body:
#:
#: | contract             | records | mismatches | rate   | classes                     |
#: |-----------------------|--------:|-----------:|-------:|-------------------------------|
#: | ``tiers.tier3-merge`` |    2961 |          4 | 0.135% | extra-keys 3, missing-required 1 |
#: | ``tiers.tier2``       |     150 |          0 |     0% | -                            |
#:
#: (``tiers.tier3-merge``'s extra-keys: ``[].text2``, ``[].append_section`` x2;
#: its missing-required: ``0.op: Field required``.)
#:
#: These two sit UPSTREAM of the C4 entity-phase bottleneck and have accrued a
#: representative sample; the three C4-downstream contracts (``contradictions``,
#: ``claim_kind``, ``resolutions``) remain starved (athenaeum#608, 2026-08-20 comment)
#: and are deliberately NOT in this set — they stay observe-only, decision
#: deferred to athenaeum#608, until a representative window exists for them too.
#:
#: **Decision, applying athenaeum#608's own framework verbatim** ("only missing-required
#: mismatches justify rejection; extra keys are a different signal"):
#:
#: - ``tiers.tier2`` — measured 0% mismatch of ANY class at n=150 (no extra key
#:   has ever appeared). "Teeth where mismatch is ~0%": :class:`Tier2Entity`
#:   tightens from ``extra="allow"`` to ``extra="forbid"``. Forbidding costs
#:   nothing today (nothing observed would newly fail) and gives real
#:   protection against a future silently-added field. ``name`` stays the only
#:   required field — unchanged, since it was already the site's own tolerance
#:   boundary and no missing-required mismatch was ever observed on any field.
#: - ``tiers.tier3-merge`` — the two observed extra-key shapes
#:   (``[].text2``, ``[].append_section``) are real, repeated production
#:   traffic, not a fluke of a thin sample (3 hits spread across the whole
#:   13-day-active window) — exactly the "different signal" the framework says
#:   is NOT grounds for rejection. Forbidding here WOULD newly reject
#:   responses that pass safely today, so :class:`MergeOp` stays
#:   ``extra="allow"``. The single missing-required hit (``0.op: Field
#:   required``) is the one class the framework says justifies rejection:
#:   ``op`` stays the required field it already was — confirmed, not relaxed.
#:   Enforcement for this class already lives downstream in ``tiers.py``:
#:   :func:`apply_merge_ops` raises :class:`~athenaeum.tiers.MergeOpsError` on
#:   an op with a missing/unrecognized ``op`` kind, which
#:   :func:`~athenaeum.tiers.parse_merge_ops_response` catches and turns into
#:   a full-echo fallback (reject-this-response-and-degrade-to-the-guaranteed-
#:   no-worse-than-status-quo path) — this issue confirms that existing
#:   behavior IS the decided posture rather than adding a second gate.
#:
#: No field became required, and no contract's required set shrank — the only
#: code change this decision makes is :class:`Tier2Entity`'s ``extra=`` value.
#: This is a **schema-shape decision only**: :func:`observe` for these two
#: contracts still never raises to its caller and never changes what the
#: pipeline does with a response — the tightened schema changes how a future
#: mismatch is *classified* in the observation log, not whether one is acted
#: on by the pipeline today.
#:
#: **M17 phase 2 (athenaeum#608) — the remaining four contracts.**
#: Decided 2026-09-02 from the same ledger over a 28-day window,
#: 2026-08-05T13:12Z -> 2026-09-02T12:42Z (16,411 records, 0 malformed) —
#: the post-quarantine clean window athenaeum#608's entry criteria require,
#: with the C4-downstream contracts no longer starved of a denominator:
#:
#: | contract          | records | mismatches | rate    | classes        |
#: |--------------------|--------:|-----------:|--------:|-----------------|
#: | ``contradictions`` |   1,464 |          0 |      0% | -              |
#: | ``query_topics``   |   1,405 |          1 | 0.0712% | wrong-type 1   |
#: | ``claim_kind``     |   1,071 |          0 |      0% | -              |
#: | ``resolutions``    |      89 |          0 |      0% | -              |
#:
#: **The uniform-vs-per-contract question, answered explicitly: per-contract,
#: and the measurement — not the intuition — is what decides it.** A uniform
#: ``extra="forbid"`` would have been wrong for ``tiers.tier3-merge``, whose
#: repeated extra-key traffic is real; a uniform ``extra="allow"`` leaves four
#: contracts tolerating a silently-added field that has demonstrably never
#: appeared across four figures of production traffic. Neither uniform posture
#: is defensible against this data, which is the answer.
#:
#: Applying athenaeum#608's framework verbatim ("only missing-required
#: mismatches justify rejection; extra keys are a different signal"):
#:
#: - ``claim_kind`` — 0 mismatches of any class at n=1,071, and (unlike the
#:   athenaeum#1035 window) this is now real production traffic: athenaeum#742
#:   wired ``stamp_claim_kind`` into the nightly intake, which is what reversed
#:   the earlier no-caller row. No extra key has ever appeared, so
#:   :class:`ClaimKindResponse` tightens to ``extra="forbid"``. ``claim_kind``
#:   stays the only required field — the site's own tolerance boundary,
#:   confirmed rather than moved.
#: - ``contradictions`` — 0 mismatches of any class at n=1,464, the largest
#:   clean sample of the four. :class:`ContradictionResponse` tightens to
#:   ``extra="forbid"``. ``detected`` stays the only required field: the site
#:   gates everything on it and defaults every other read, so the four
#:   ``Optional`` fields are its real tolerance boundary and none of them
#:   becomes required.
#: - ``query_topics`` — the decided shape is confirmed, not tightened.
#:   :class:`QueryTopicsResponse` is a ``RootModel[list[str]]``, which has no
#:   ``extra=`` knob to turn: there are no named fields, so "an unexpected
#:   key" is not a representable outcome for this contract. Its one mismatch
#:   in the window is ``wrong-type`` (a non-string element), which is neither
#:   of the framework's two classes — and it is already degraded safely at the
#:   site, which keeps each non-empty ``str`` element and drops the rest. That
#:   existing degrade IS the decided posture; membership below records the
#:   shape as DECIDED, which is what this set means.
#: - ``resolutions`` — **deferred, and the reason is the denominator, not the
#:   answer.** 0 mismatches at n=89 is 0%, but n=89 is one order of magnitude
#:   below every other contract here and this is the one contract downstream
#:   of the C4 entity-phase bottleneck that has still not cleared it
#:   (athenaeum#1102 shipped ``librarian.intake_runtime_floor`` defaulting
#:   OFF). Its model spans a discriminated union of 14 ``action`` branches;
#:   89 observations cannot have exercised them representatively, so an
#:   ``extra="forbid"`` taken now would be a guess wearing a measurement's
#:   clothes. :class:`ResolutionResponse` stays ``extra="allow"``.
#:   **Release bar** (stated so the next pass does not re-litigate it):
#:   ``resolutions`` reaches three figures of observations spread across
#:   several runs — i.e. the same order of magnitude as its siblings here —
#:   drawn from runs in which the C4 phase was not truncated.
#:
#: Again no field became required and no contract's required set shrank; the
#: only code changes are the two ``extra=`` values above. :func:`observe`
#: still never raises for ANY contract — see the module docstring for why
#: that is now a decision rather than a deferral.
STRICT_CONTRACTS: frozenset[str] = frozenset(
    {
        "claim_kind",
        "contradictions",
        "query_topics",
        "tiers.tier2",
        "tiers.tier3-merge",
    }
)

#: Stated-local copies of the two vocabularies these models range over. Kept in
#: sync with their live sources (``models.CLAIM_KINDS`` /
#: ``resolutions._VALID_ACTIONS``) by an equivalence test rather than an import,
#: so this module never pulls a heavy module onto the recall hot path.
_CLAIM_KINDS = ("decision", "definition", "fact", "observation", "opinion", "policy")
_RESOLVER_ACTIONS = (
    "attribute_both",
    "correct_a",
    "correct_b",
    "deprecate_both",
    "forget_a",
    "forget_b",
    "keep_a",
    "keep_b",
    "merge",
    "not_a_conflict",
    "propose_merge",
    "retain_both_with_context",
    "scope_a",
    "scope_b",
)


# ---------------------------------------------------------------------------
# Response models — one per in-scope JSON-shaped contract. ``extra="allow"``
# throughout so a newly-emitted field is reported (via observe) rather than
# rejected. Vocabulary-checked fields use ``Literal`` so an out-of-vocab value
# is genuine drift; tolerated-missing fields are ``Optional``.
# ---------------------------------------------------------------------------


class QueryTopicsResponse(RootModel[list[str]]):
    """``query_topics`` — a JSON array of topic strings (``query_topics.py``).

    The site keeps each non-empty ``str`` element and drops the rest, so the
    accepted shape is ``list[str]``; a non-list payload or a non-string element
    is drift worth logging.

    Shape CONFIRMED as decided (M17 phase 2, athenaeum#608 — see
    :data:`STRICT_CONTRACTS`): a ``RootModel[list[str]]`` has no named fields,
    so it has no ``extra=`` knob and no representable "unexpected key". The
    window's single mismatch was a ``wrong-type`` element, already degraded
    safely by the site's own per-element filter. Nothing tightened; the
    posture is recorded rather than deferred.
    """


class ClaimKindResponse(BaseModel):
    """``claim_kind`` — ``{"claim_kind": <one of CLAIM_KINDS>}`` (``claim_kind.py``).

    The site rejects (→ unclassified) any value outside CLAIM_KINDS, so an
    out-of-vocabulary label is genuine drift → ``Literal`` over the set.

    ``extra="forbid"`` (M17 phase 2, athenaeum#608 — see
    :data:`STRICT_CONTRACTS` for the measured window and full reasoning).
    Measured 0% mismatch of any class at n=1,071 over real production traffic
    (athenaeum#742 wired the nightly caller), so no extra key has ever
    appeared: forbidding costs nothing observed and catches a future
    silently-added field. ``claim_kind`` stays the only required field.
    """

    model_config = ConfigDict(extra="forbid")

    claim_kind: Literal[_CLAIM_KINDS]  # type: ignore[valid-type]


class ContradictionResponse(BaseModel):
    """``contradictions`` — detector output (``contradictions.py``).

    ``detected`` gates everything; when true the site requires a
    ``conflict_type`` in the allowed set and reads optional member/passage
    lists (each defaulted to ``[]``), so those are ``Optional`` here.
    ``conflict_type`` stays ``Optional[str]`` rather than a ``Literal`` because
    the site only enforces its vocabulary on the ``detected`` branch; the
    mismatch signal there is captured by the ``detected``/shape check.

    ``extra="forbid"`` (M17 phase 2, athenaeum#608 — see
    :data:`STRICT_CONTRACTS` for the measured window and full reasoning).
    Measured 0% mismatch of any class at n=1,464, the largest clean sample of
    the phase-2 four. ``detected`` stays the only required field: every other
    read is defaulted at the site, so the ``Optional`` fields below are its
    real tolerance boundary and none of them was tightened.
    """

    model_config = ConfigDict(extra="forbid")

    detected: bool
    conflict_type: Optional[str] = None
    members_involved: Optional[list[Any]] = None
    conflicting_passages: Optional[list[Any]] = None
    rationale: Optional[str] = None


class ResolutionResponse(BaseModel):
    """``resolutions`` — resolver output (``resolutions.py``).

    A single object discriminated by ``action``. The site rejects an
    out-of-vocabulary ``action`` (→ fallback), so ``action`` is a ``Literal``.
    ``recommended_winner`` is likewise vocabulary-checked but only on the
    non-``propose_merge`` branch, so it stays ``Optional[str]``; the merge-branch
    fields (``merge_target_name`` / ``draft_merged_body``) and the shared
    optional fields mirror today's tolerant reads.

    ``extra="allow"`` stays, DEFERRED rather than decided (M17 phase 2,
    athenaeum#608 — see :data:`STRICT_CONTRACTS`). The window measured 0
    mismatches, but at n=89 against four figures for every sibling contract:
    this is the one contract still downstream of the C4 entity-phase
    bottleneck, and 89 observations cannot have exercised 14 ``action``
    branches representatively. The release bar is stated in
    :data:`STRICT_CONTRACTS`.
    """

    model_config = ConfigDict(extra="allow")

    action: Literal[_RESOLVER_ACTIONS]  # type: ignore[valid-type]
    recommended_winner: Optional[str] = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None
    source_precedence_used: Optional[list[Any]] = None
    disambiguation_options: Optional[list[Any]] = None
    merge_target_name: Optional[str] = None
    draft_merged_body: Optional[str] = None


class Tier2Entity(BaseModel):
    """One classified entity in a Tier-2 response array.

    The site skips any item without a truthy ``name`` and defaults/coerces
    ``entity_type`` / ``access`` / ``tags`` / ``observations`` when absent, so
    only ``name`` is required here.

    ``extra="forbid"`` (M17 phase 2a, athenaeum#1035) — a deliberate exception to
    this module's phase-1 module-wide default (see :data:`STRICT_CONTRACTS` for
    the measured window and full reasoning). Measured 0% mismatch of any class
    at n=150: no extra key has ever appeared on this contract, so forbidding
    costs nothing observed today and gives real protection against a future
    silently-added field going unnoticed.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    entity_type: Optional[str] = None
    access: Optional[str] = None
    tags: Optional[list[Any]] = None
    observations: Optional[str] = None


class Tier2ClassifyResponse(RootModel[list[Tier2Entity]]):
    """``tiers`` tier2 — a JSON array of classified entities (``tiers.py``)."""


class MergeOp(BaseModel):
    """One patch-mode merge operation.

    ``op`` (the kind) is required; ``anchor`` / ``text`` are the other fields of
    the documented op vocabulary (``append_section`` uses ``text``; ``replace``
    / ``insert_after`` use ``anchor`` + ``text``), modeled ``Optional`` so a
    valid op is not mis-reported as carrying "unexpected" keys. Which fields a
    given kind actually requires is validated at apply time by
    ``apply_merge_ops``; a genuinely new field the model starts emitting is
    surfaced as an extra key.

    ``extra="allow"`` stays (M17 phase 2a, athenaeum#1035 — see
    :data:`STRICT_CONTRACTS`): the measured window shows real, repeated
    extra-key traffic (``[].text2``, ``[].append_section``), which athenaeum#608's
    framework treats as a different signal from a missing-required field, not
    grounds for rejection. ``op`` stays required — the one missing-required
    hit in the window (``0.op: Field required``) confirms it belongs there;
    enforcement for a missing ``op`` already lives in ``apply_merge_ops``
    (raises :class:`MergeOpsError`, which the caller turns into a full-echo
    fallback).
    """

    model_config = ConfigDict(extra="allow")

    op: str
    anchor: Optional[str] = None
    text: Optional[str] = None


class Tier3MergeOpsResponse(RootModel[list[MergeOp]]):
    """``tiers`` tier3-merge — the patch-mode ``{"ops": [...]}`` list, post-coercion.

    The JSON-shaped tier3 merge contract is the patch-mode ops response
    (``parse_merge_ops_response``); its ``ops`` list is normalized by
    ``_coerce_merge_ops`` (tolerating the alternate ``operations`` key and a
    bare-dict single op) *before* this model observes it, so we log op-shape
    drift rather than re-deriving the container normalization. The full-echo
    fallback (``parse_tier3_merge``) is a whole-body / ``ESCALATE:`` text
    protocol, not a JSON contract, so it is intentionally not modeled here.
    """


# ---------------------------------------------------------------------------
# The observe entry point + per-contract convenience wrappers.
# ---------------------------------------------------------------------------


def _fmt_error(err: Mapping[str, Any]) -> str:
    """Render one pydantic error as ``field.path: message`` (empty loc → ``<root>``).

    Takes a ``Mapping`` (not ``dict``) so pydantic's ``ErrorDetails`` TypedDict
    — returned by ``ValidationError.errors()`` — is accepted directly; only
    ``.get()`` is used here, which both support identically.
    """
    loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
    return f"{loc}: {err.get('msg', 'invalid')}"


def _extra_keys(obj: Any) -> list[str]:
    """Collect unexpected keys from a validated model (top-level + per list item).

    For a :class:`BaseModel` these are ``model_extra`` keys. For a
    :class:`RootModel` wrapping a list, aggregate the extra keys of any
    ``BaseModel`` items (e.g. a Tier-2 entity emitting a new field), prefixing
    with ``[].`` so the source is legible. Deduplicated, order-stable.
    """
    keys: list[str] = []
    seen: set[str] = set()

    def _add(k: str) -> None:
        if k not in seen:
            seen.add(k)
            keys.append(k)

    if isinstance(obj, BaseModel):
        extra = getattr(obj, "model_extra", None)
        if extra:
            for k in extra:
                _add(str(k))
        root = getattr(obj, "root", None)
        if isinstance(root, list):
            for item in root:
                if isinstance(item, BaseModel):
                    item_extra = getattr(item, "model_extra", None)
                    if item_extra:
                        for k in item_extra:
                            _add(f"[].{k}")
    return keys


def _classify_validation_errors(raw_errors: list[Mapping[str, Any]]) -> list[str]:
    """Map pydantic error dicts to athenaeum#724 mismatch classes, order-stable.

    ``missing`` → ``missing-required``; any ``*_type`` / ``*_parsing`` /
    ``model_type`` error, plus an out-of-vocabulary ``literal_error`` / ``enum``
    (a value that does not match the field's expected type/domain) →
    ``wrong-type``; ``extra_forbidden`` → ``extra-keys`` (raised by the
    contracts in :data:`STRICT_CONTRACTS` that carry ``extra="forbid"``;
    under ``extra="allow"`` the same signal arrives via the post-hoc
    :func:`_extra_keys` path instead); anything else → ``other``.
    """
    classes: list[str] = []
    seen: set[str] = set()

    def _add(cls: str) -> None:
        if cls not in seen:
            seen.add(cls)
            classes.append(cls)

    for err in raw_errors:
        etype = str(err.get("type", ""))
        if etype == "missing":
            _add(MISMATCH_MISSING_REQUIRED)
        elif etype == "extra_forbidden":
            _add(MISMATCH_EXTRA_KEYS)
        elif (
            etype.endswith("_type")
            or etype.endswith("_parsing")
            or "type" in etype
            or etype in ("literal_error", "enum")
        ):
            _add(MISMATCH_WRONG_TYPE)
        else:
            _add(MISMATCH_OTHER)
    return classes


def observations_path(cache_dir: Path | None = None) -> Path:
    """Resolve the observation ledger path under the (resolved) cache dir."""
    from athenaeum.config import resolve_cache_dir

    return resolve_cache_dir(cache_dir) / OBSERVATIONS_FILENAME


def durable_observations_path(wiki_root: Path, *, cache_dir: Path | None = None) -> Path:
    """The R3 ``operational``/``store-durable`` location (design note §5.2
    table row 8 'observations.jsonl'; issue athenaeum#980 AC4):
    ``<wiki_root>/_llm_schema_observations.jsonl``.

    Same legacy-fallback contract as :func:`athenaeum.spend.durable_ledger_path`:
    an existing installation's populated cache-dir ledger keeps resolving
    there until migrated; a fresh or already-migrated store resolves here.
    """
    new_path = Path(wiki_root) / OBSERVATIONS_FILENAME
    legacy_path = observations_path(cache_dir)
    if new_path.exists() or not legacy_path.exists():
        return new_path
    return legacy_path


def record_observation(
    *,
    contract: str,
    call_site: str,
    outcome: str,
    classes: list[str] | None = None,
    errors: list[str] | None = None,
    extra_keys: list[str] | None = None,
    cache_dir: Path | None = None,
    wiki_root: Path | None = None,
) -> None:
    """Append ONE observation record to the durable ledger. Best-effort.

    ``outcome`` is ``"ok"`` (a clean parse — the denominator athenaeum#724 defect
    2 was missing) or ``"mismatch"`` (carrying its ``classes``). Never raises: a
    telemetry write must not degrade the pipeline. Enabled by default; set
    ``ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED=0`` to disable.

    *wiki_root*, when supplied, resolves the ledger behind the seam (issue
    athenaeum#980 AC4) via :func:`durable_observations_path`; omitted,
    resolution is unchanged from before that issue.
    """
    try:
        if os.environ.get("ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED", "1").strip().lower() in (
            "0",
            "false",
            "no",
        ):
            return
        record = {
            "v": OBSERVATION_SCHEMA_VERSION,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "contract": contract,
            "call_site": call_site,
            "outcome": outcome,
            "classes": classes or [],
            # Redaction: only the schema-shape delta is recorded — field paths,
            # error messages, and unexpected KEY names — never any field VALUE,
            # so no claim content or personal data reaches the ledger.
            "errors": errors or [],
            "extra_keys": extra_keys or [],
        }
        path = (
            durable_observations_path(wiki_root, cache_dir=cache_dir)
            if wiki_root is not None
            else observations_path(cache_dir)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # pragma: no cover - defensive: telemetry must never throw
        log.debug(
            "llm-schema: could not record observation for contract=%s call_site=%s",
            contract,
            call_site,
            exc_info=True,
        )


def read_observations(
    cache_dir: Path | None = None, *, wiki_root: Path | None = None
) -> list[dict[str, Any]]:
    """Read every observation record from the ledger (``[]`` when absent).

    *wiki_root*, when supplied, resolves via :func:`durable_observations_path`
    — the SAME resolution :func:`record_observation` uses, so a read against
    a given store always finds exactly what the matching write produced.
    """
    path = (
        durable_observations_path(wiki_root, cache_dir=cache_dir)
        if wiki_root is not None
        else observations_path(cache_dir)
    )
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def aggregate_observations(cache_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Per-contract observation summary over the ledger (athenaeum#724 AC7).

    Returns one entry per :data:`INSTRUMENTED_CONTRACTS` (so a contract with no
    records is an EXPLICIT *no-data* row, not a silent absence that reads as a
    clean 0). Each entry carries ``observations`` (the denominator),
    ``mismatches``, a ``by_class`` count, and ``mismatch_rate`` — or
    ``no_data=True`` and ``mismatch_rate=None`` when ``observations == 0``, so
    "0 over 0" is never confused with "0 over 400".
    """
    rows = read_observations(cache_dir)
    summary: dict[str, dict[str, Any]] = {
        contract: {
            "observations": 0,
            "mismatches": 0,
            "by_class": {},
            "mismatch_rate": None,
            "no_data": True,
        }
        for contract in INSTRUMENTED_CONTRACTS
    }
    for row in rows:
        contract = str(row.get("contract", ""))
        entry = summary.setdefault(
            contract,
            {
                "observations": 0,
                "mismatches": 0,
                "by_class": {},
                "mismatch_rate": None,
                "no_data": True,
            },
        )
        entry["observations"] += 1
        if row.get("outcome") == "mismatch":
            entry["mismatches"] += 1
            for cls in row.get("classes") or []:
                entry["by_class"][cls] = entry["by_class"].get(cls, 0) + 1
    for entry in summary.values():
        obs = entry["observations"]
        entry["no_data"] = obs == 0
        entry["mismatch_rate"] = None if obs == 0 else entry["mismatches"] / obs
    return summary


def observe(
    model: type[BaseModel],
    payload: Any,
    *,
    contract: str,
    call_site: str,
    cache_dir: Path | None = None,
    wiki_root: Path | None = None,
) -> None:
    """Validate ``payload`` against ``model`` for OBSERVATION ONLY.

    Records ONE observation to the durable ledger on EVERY call (the denominator
    athenaeum#724 needs), emits the structured WARNING marker on a mismatch
    (validation error and/or unexpected key), then returns ``None``. This
    function **never raises and never alters behavior** — the entire body is
    wrapped so even a bug in validation cannot touch the pipeline. Callers invoke
    it *after* their own parse and ignore its result. A TOTAL parse failure never
    reaches here (the caller returns first); count that via
    :func:`observe_parse_failure`.

    Args:
        model: the contract's response model.
        payload: the already-parsed payload (dict, list, …) to validate.
        contract: the contract name (aggregate mismatch counts by this).
        call_site: a stable label identifying the parse site.
        cache_dir: override the ledger location (tests / non-default deploys).
        wiki_root: resolve the ledger behind the seam (issue athenaeum#980 AC4);
            omitted, resolution is unchanged from before that issue.
    """
    try:
        errors: list[str] = []
        classes: list[str] = []
        extra_keys: list[str] = []
        try:
            validated = model.model_validate(payload)
        except ValidationError as exc:
            raw = exc.errors()
            errors = [_fmt_error(e) for e in raw]
            classes = _classify_validation_errors(list(raw))
        else:
            extra_keys = _extra_keys(validated)
            if extra_keys:
                classes = [MISMATCH_EXTRA_KEYS]
        is_mismatch = bool(errors or extra_keys)
        if is_mismatch:
            log.warning(
                "%s contract=%s call_site=%s error_class=%s errors=%s extra_keys=%s classes=%s",
                SCHEMA_MISMATCH_MARKER,
                contract,
                call_site,
                "ValidationError" if errors else "ExtraKeys",
                errors or "-",
                extra_keys or "-",
                ",".join(classes) or "-",
            )
        record_observation(
            contract=contract,
            call_site=call_site,
            outcome="mismatch" if is_mismatch else "ok",
            classes=classes,
            errors=errors,
            extra_keys=extra_keys,
            cache_dir=cache_dir,
            wiki_root=wiki_root,
        )
    except Exception:  # pragma: no cover - defensive: observation must never throw
        # A failure to *observe* must not degrade the pipeline in any way — the
        # whole point of phase 1 is behavior neutrality. Record at DEBUG so a
        # bug here is diagnosable without adding a failure mode.
        log.debug(
            "llm-schema: observation failed for contract=%s call_site=%s",
            contract,
            call_site,
            exc_info=True,
        )


def observe_parse_failure(
    *,
    contract: str,
    call_site: str,
    detail: str | None = None,
    cache_dir: Path | None = None,
    wiki_root: Path | None = None,
) -> None:
    """Count a TOTAL parse failure as a ``parse-fail`` mismatch (athenaeum#724 defect 1).

    Called from a parse guard's early-return path — where the response never
    yielded a JSON object at all, so it never reaches :func:`observe`. A total
    parse failure is the most extreme missing-required case (athenaeum#608), and
    exactly the class the pre-athenaeum#724 instrument could not see. Emits the
    same WARNING marker and records a mismatch to the ledger. Never raises.

    *wiki_root* (issue athenaeum#980 AC4): forwarded to :func:`record_observation`.
    """
    try:
        log.warning(
            "%s contract=%s call_site=%s error_class=ParseFail errors=%s extra_keys=- classes=%s",
            SCHEMA_MISMATCH_MARKER,
            contract,
            call_site,
            [detail] if detail else "-",
            MISMATCH_PARSE_FAIL,
        )
        record_observation(
            contract=contract,
            call_site=call_site,
            outcome="mismatch",
            classes=[MISMATCH_PARSE_FAIL],
            errors=[detail] if detail else [],
            cache_dir=cache_dir,
            wiki_root=wiki_root,
        )
    except Exception:  # pragma: no cover - defensive: observation must never throw
        log.debug(
            "llm-schema: parse-failure observation failed for contract=%s call_site=%s",
            contract,
            call_site,
            exc_info=True,
        )


def observe_query_topics(
    payload: Any, *, call_site: str, wiki_root: Path | None = None
) -> None:
    observe(
        QueryTopicsResponse,
        payload,
        contract="query_topics",
        call_site=call_site,
        wiki_root=wiki_root,
    )


def observe_claim_kind(payload: Any, *, call_site: str, wiki_root: Path | None = None) -> None:
    observe(
        ClaimKindResponse, payload, contract="claim_kind", call_site=call_site, wiki_root=wiki_root
    )


def observe_contradictions(
    payload: Any, *, call_site: str, wiki_root: Path | None = None
) -> None:
    observe(
        ContradictionResponse,
        payload,
        contract="contradictions",
        call_site=call_site,
        wiki_root=wiki_root,
    )


def observe_resolutions(payload: Any, *, call_site: str, wiki_root: Path | None = None) -> None:
    observe(
        ResolutionResponse,
        payload,
        contract="resolutions",
        call_site=call_site,
        wiki_root=wiki_root,
    )


def observe_tier2_classify(
    payload: Any, *, call_site: str, wiki_root: Path | None = None
) -> None:
    observe(
        Tier2ClassifyResponse,
        payload,
        contract="tiers.tier2",
        call_site=call_site,
        wiki_root=wiki_root,
    )


def observe_tier3_merge_ops(
    payload: Any, *, call_site: str, wiki_root: Path | None = None
) -> None:
    observe(
        Tier3MergeOpsResponse,
        payload,
        contract="tiers.tier3-merge",
        call_site=call_site,
        wiki_root=wiki_root,
    )
