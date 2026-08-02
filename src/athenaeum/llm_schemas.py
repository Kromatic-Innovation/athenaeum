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

Convention for adding a contract (so other sites can follow this one):

1. Model **the shape the site accepts today**, not a tightened ideal. A field
   the site tolerates as missing (because it defaults/coerces it) is
   ``Optional`` here; a field whose value the site *rejects* when out of
   vocabulary (``claim_kind``, ``action``) is a ``Literal`` so an
   out-of-vocabulary value logs as genuine drift.
2. Use ``extra="allow"`` — an unexpected key is signal, not an error, and
   :func:`observe` reports which keys appeared (top-level and per list item)
   so a newly-emitted field surfaces without failing validation.
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

import logging
from collections.abc import Mapping
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, RootModel, ValidationError

log = logging.getLogger(__name__)

#: Greppable marker prefixing every mismatch WARNING (one per response with a
#: validation error and/or an unexpected key). Aggregate by ``contract=``.
SCHEMA_MISMATCH_MARKER = "llm-schema-mismatch"

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
    """


class ClaimKindResponse(BaseModel):
    """``claim_kind`` — ``{"claim_kind": <one of CLAIM_KINDS>}`` (``claim_kind.py``).

    The site rejects (→ unclassified) any value outside CLAIM_KINDS, so an
    out-of-vocabulary label is genuine drift → ``Literal`` over the set.
    """

    model_config = ConfigDict(extra="allow")

    claim_kind: Literal[_CLAIM_KINDS]  # type: ignore[valid-type]


class ContradictionResponse(BaseModel):
    """``contradictions`` — detector output (``contradictions.py``).

    ``detected`` gates everything; when true the site requires a
    ``conflict_type`` in the allowed set and reads optional member/passage
    lists (each defaulted to ``[]``), so those are ``Optional`` here.
    ``conflict_type`` stays ``Optional[str]`` rather than a ``Literal`` because
    the site only enforces its vocabulary on the ``detected`` branch; the
    mismatch signal there is captured by the ``detected``/shape check.
    """

    model_config = ConfigDict(extra="allow")

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
    """

    model_config = ConfigDict(extra="allow")

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


def observe(
    model: type[BaseModel],
    payload: Any,
    *,
    contract: str,
    call_site: str,
) -> None:
    """Validate ``payload`` against ``model`` for OBSERVATION ONLY.

    Emits one structured WARNING (the :data:`SCHEMA_MISMATCH_MARKER` line) when
    validation fails and/or the payload carries unexpected keys, then returns
    ``None``. This function **never raises and never alters behavior** — the
    entire body is wrapped so that even a bug in validation cannot touch the
    pipeline. Callers invoke it *after* their own parse and ignore its result.

    Args:
        model: the contract's response model.
        payload: the already-parsed payload (dict, list, …) to validate.
        contract: the contract name (aggregate mismatch counts by this).
        call_site: a stable label identifying the parse site.
    """
    try:
        errors: list[str] = []
        extra_keys: list[str] = []
        try:
            validated = model.model_validate(payload)
        except ValidationError as exc:
            errors = [_fmt_error(e) for e in exc.errors()]
        else:
            extra_keys = _extra_keys(validated)
        if errors or extra_keys:
            log.warning(
                "%s contract=%s call_site=%s error_class=%s errors=%s extra_keys=%s",
                SCHEMA_MISMATCH_MARKER,
                contract,
                call_site,
                "ValidationError" if errors else "ExtraKeys",
                errors or "-",
                extra_keys or "-",
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


def observe_query_topics(payload: Any, *, call_site: str) -> None:
    observe(QueryTopicsResponse, payload, contract="query_topics", call_site=call_site)


def observe_claim_kind(payload: Any, *, call_site: str) -> None:
    observe(ClaimKindResponse, payload, contract="claim_kind", call_site=call_site)


def observe_contradictions(payload: Any, *, call_site: str) -> None:
    observe(ContradictionResponse, payload, contract="contradictions", call_site=call_site)


def observe_resolutions(payload: Any, *, call_site: str) -> None:
    observe(ResolutionResponse, payload, contract="resolutions", call_site=call_site)


def observe_tier2_classify(payload: Any, *, call_site: str) -> None:
    observe(Tier2ClassifyResponse, payload, contract="tiers.tier2", call_site=call_site)


def observe_tier3_merge_ops(payload: Any, *, call_site: str) -> None:
    observe(Tier3MergeOpsResponse, payload, contract="tiers.tier3-merge", call_site=call_site)
