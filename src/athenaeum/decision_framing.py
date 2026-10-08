# SPDX-License-Identifier: Apache-2.0
"""Framed, effort-capped decision items (issue athenaeum#717).

:mod:`athenaeum.decisions` already joins every human-decision surface into
ONE outbound queue, and :mod:`athenaeum.decision_answers` makes the path back
in uniform. What neither of them did was make an item *answerable within a
bounded amount of human effort* — and that, not the item count, is what
actually stalls the queue.

The origin failure is precise and was re-measured against the live corpus on
2026-10-06: the merge surface alone held 817 unresolved proposals (724
fold-into-existing, 93 create-merged) and the question surface a further 322.
That backlog did not sit undrained because 1,139 is a large number. It sat
undrained because each item cost several full page bodies of reading to
decide. **Human decision load is bounded in EFFORT, not count.** An
items/day budget alone is gameable by construction, because batching lets a
system size its own items; so the budget this module enforces the first and
hardest part of is a *per-item context cap*.

This module adds two things to every item the unified queue emits.

**1. Framing.** An item is only answerable if it states what is being asked
and what answering it costs. Each framed item carries:

``summary``
    The plain-language question. This is the EXISTING field — every
    ``*_to_decision`` builder in :mod:`athenaeum.decisions` already phrases
    one ("Merge these N pages into one? — …"), and that rule
    (never show a raw cosine; phrase it as something a human can answer)
    predates this module. Framing does not add a second, redundant question
    field; it adds the five below *around* it.
``context_bundle``
    The minimal context needed to answer, as structured data — never a
    free-text blob. This is what the cap is measured against.
``proposed_default``
    ``{"action": ..., "consequences": ...}`` — what happens if the human
    never answers. Every default here is the CONSERVATIVE one (leave the
    corpus as it is), because an unanswered item must not be able to enact
    a change by timing out.
``reversibility``
    One of :data:`REVERSIBILITY_CLASSES`. Humans are the arbiters of
    irreversible decisions; knowing which bucket an item is in is what lets
    a triage agent (a later child of this epic) absorb the reversible ones.
``response_schema``
    A JSON Schema for a valid answer, so an answer can be machine-validated
    rather than interpreted. :func:`validate_answer` applies it;
    :mod:`athenaeum.decision_answers` calls that on the inbound path, so a
    malformed answer is refused at the boundary instead of half-applied.
``escalation_rationale``
    What, specifically, could not be determined without a human. This is the
    field that makes the queue auditable: an item with no statable rationale
    is an item that should not have been escalated.
``routing``
    :data:`ROUTING_AUTHORITY` (only the owner may decide — a question of
    permission, not of knowledge) or :data:`ROUTING_COMPETENCE` (the system
    could not work it out). The distinction matters for measurement, not
    just bookkeeping: authority items reaching a human are the queue working
    as designed, so they must be countable SEPARATELY from competence
    escalations, or a shrinking competence rate can be faked by relabelling.

**2. The per-item context cap, enforced in code.** An item whose context
bundle exceeds :func:`athenaeum.config.resolve_decisions_max_item_context_tokens`
may not enter the queue as an oversized item. :func:`frame_decision` applies
a two-step remedy, in this order:

1. **Decompose** — shrink the bundle deterministically (see
   :data:`_SHRINK_ORDER`): drop the parts that are bulk rather than signal,
   largest-payoff first, re-measuring after each step. A decomposed item is
   still a normal, answerable item; it is flagged ``context_decomposed:
   True`` and records what was dropped, so nothing is silently lost.
2. **Escalate as a scheduled review** — if even the irreducible pointer
   bundle is over cap, the item is admitted with
   ``routing = ``:data:`ROUTING_SCHEDULED_REVIEW`, a pointer-only bundle, and
   a ``context_overflow`` record. It is explicitly NOT presented as a
   one-screen decision, because it isn't one; it is work to be scheduled.

Either way the invariant holds: **no item enters the queue carrying an
over-cap context bundle.** :func:`frame_decision` is the single admission
gate, which is also deliberately the right seam for the cap to be checked
*after* batching rather than before — a batched item is one item and passes
through here exactly once, so batching can never be used to slip oversized
context past the cap by sizing its own members.

Layering: L4 domain/pipeline module. It is consumed by
:mod:`athenaeum.decisions` (the outbound view) and
:mod:`athenaeum.decision_answers` (the inbound answer path), and imports only
L3 services (:mod:`athenaeum.context` for the token estimator) plus stdlib
and ``jsonschema``. It owns no queue's storage format and mutates nothing —
framing is a pure function of an item dict.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from athenaeum.context import estimate_tokens

# ---------------------------------------------------------------------------
# Reversibility classes
# ---------------------------------------------------------------------------

#: Undoing the decision costs nothing but a second answer — no corpus content
#: is destroyed and no external side effect fires.
REVERSIBILITY_REVERSIBLE = "reversible"

#: Undoable, but only by doing real work: the decision enacts a change whose
#: inverse exists and is supported (e.g. a fold, which athenaeum#716 made
#: reversible by leaving the source as a tombstone rather than deleting it).
REVERSIBILITY_REVERSIBLE_WITH_WORK = "reversible-with-work"

#: No supported inverse. These are the decisions a human must arbitrate.
REVERSIBILITY_IRREVERSIBLE = "irreversible"

#: Every valid value of a framed item's ``reversibility``, cheapest first.
REVERSIBILITY_CLASSES = (
    REVERSIBILITY_REVERSIBLE,
    REVERSIBILITY_REVERSIBLE_WITH_WORK,
    REVERSIBILITY_IRREVERSIBLE,
)

# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

#: The system could not work the answer out. Reaching a human is a gap.
ROUTING_COMPETENCE = "competence"

#: Only the owner may decide — a question of permission, not of knowledge.
#: Reaching a human here is the queue working as designed, so these are
#: counted separately from competence escalations.
ROUTING_AUTHORITY = "authority"

#: The item's irreducible context is still over the per-item cap, so it is
#: not a one-screen decision at all. Admitted as scheduled work, never as a
#: queue item a human is expected to answer in a sitting.
ROUTING_SCHEDULED_REVIEW = "scheduled-review"

ROUTING_CLASSES = (ROUTING_COMPETENCE, ROUTING_AUTHORITY, ROUTING_SCHEDULED_REVIEW)

# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

_NOTE_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": "Optional free text recorded with the answer.",
}

#: A free-text answer (the question/contradiction surface: the answer IS the
#: resolution text, which is then ingested as a claim).
_SCHEMA_FREE_TEXT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "minLength": 1,
            "description": "The resolution, in the operator's own words.",
        },
        "note": _NOTE_PROPERTY,
    },
    "required": ["verdict"],
    "additionalProperties": False,
}


#: A ``coordinate`` answer (issue athenaeum#1993): the human supplies a value
#: for one or more separator dimensions, for one or both members of one or
#: more pairs in a batch (:func:`athenaeum.verdict_effects.queue_coordinate_batch`).
#:
#: ``verdict`` stays a plain, non-empty STRING here -- not a nested object --
#: because the existing inbound plumbing enforces that shape at BOTH ends
#: regardless of decision type: :func:`athenaeum._cmd_decisions._cmd_answer`
#: forwards only ``str(parsed["verdict"])`` into the on-disk answer file, and
#: :func:`athenaeum.decision_answers._load_decision_answer` rejects a
#: ``verdict`` that is not ``isinstance(..., str)``. So a coordinate answer's
#: REAL structure -- which pair(s), which dimension(s), which page id(s) --
#: travels as a JSON-encoded string inside that one field, e.g.::
#:
#:     {"verdict": "{\\"answers\\": [{\\"pair\\": \\"a+b\\", \\"dimensions\\": "
#:                  "{\\"subject\\": {\\"a\\": \\"alice\\"}}}]}"}
#:
#: This schema only gates the OUTER shape (a non-empty string), which is all
#: :func:`validate_answer` / :func:`shape_errors_only` can check without
#: parsing it. The decoded payload's REAL structural contract is
#: :data:`_SCHEMA_COORDINATE_PAYLOAD`, applied by
#: :func:`validate_coordinate_payload` -- called from
#: :mod:`athenaeum.decision_answers`'s coordinate applier, after
#: ``json.loads``, before anything is written.
_SCHEMA_COORDINATE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "minLength": 1,
            "description": (
                "A JSON-encoded object: {\"answers\": [{\"pair\": "
                "\"<pair_key, exactly as queued>\", \"dimensions\": "
                "{\"<dimension-name>\": {\"<page-id>\": <value>, ...}}}]}. "
                "Supply a value only for the page id(s)/dimension(s) you "
                "are answering; the rest are left unset."
            ),
        },
        "note": _NOTE_PROPERTY,
    },
    "required": ["verdict"],
    "additionalProperties": False,
}

#: The REAL structural contract for a decoded ``coordinate`` answer payload
#: (issue athenaeum#1993) -- see :data:`_SCHEMA_COORDINATE`'s docstring for
#: why this is a SEPARATE schema from the one :func:`response_schema_for`
#: publishes. Each ``answers[]`` entry names one queued pair (``pair``, the
#: exact ``pair_key`` string the batch's machine-parseable marker carries --
#: see :func:`athenaeum.verdict_effects.parse_coordinate_batch_members`) and
#: a non-empty map of dimension name -> {page id: value}. Which page ids are
#: VALID for a given ``pair`` (its own two members) and which dimensions the
#: batch actually named are both instance-specific facts a static schema
#: cannot express -- :mod:`athenaeum.decision_answers`'s applier checks
#: those against the batch's own recovered members before writing anything.
_SCHEMA_COORDINATE_PAYLOAD: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string", "minLength": 1},
                    "dimensions": {
                        "type": "object",
                        "minProperties": 1,
                        "additionalProperties": {
                            "type": "object",
                            "minProperties": 1,
                        },
                    },
                },
                "required": ["pair", "dimensions"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answers"],
    "additionalProperties": False,
}


def validate_coordinate_payload(payload: Any) -> list[str]:
    """Validate a decoded ``coordinate`` answer payload against
    :data:`_SCHEMA_COORDINATE_PAYLOAD`. Mirrors :func:`validate_answer`'s
    contract exactly: returns a list of human-readable errors, empty when
    valid. ``payload`` that is not even a ``dict`` (a malformed
    ``json.loads`` result -- a bare list, string, or number) is refused
    without handing a non-mapping to :mod:`jsonschema`.
    """
    if not isinstance(payload, dict):
        return ["<payload>: must be a JSON object"]
    import jsonschema

    validator = jsonschema.Draft202012Validator(_SCHEMA_COORDINATE_PAYLOAD)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    return [
        f"{'/'.join(str(p) for p in error.path) or '<payload>'}: {error.message}"
        for error in errors
    ]


def _approve_reject_schema(*, description: str) -> dict[str, Any]:
    """A two-token ``approve``/``reject`` answer schema."""
    return {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["approve", "reject"],
                "description": description,
            },
            "note": _NOTE_PROPERTY,
        },
        "required": ["verdict"],
        "additionalProperties": False,
    }


def _approve_rename_reject_schema(*, description: str) -> dict[str, Any]:
    """A three-token ``approve``/``rename``/``reject`` answer schema.

    Issue athenaeum#2015: a dimension proposal needs a verdict
    :func:`_approve_reject_schema` cannot express — a human may rename the
    drafted axis before ratifying it (the drafter's own name is a
    mechanical derivation, never a claimed-final one). ``name`` is
    REQUIRED when ``verdict == "rename"`` and forbidden otherwise, enforced
    via a conditional subschema rather than a bare optional property, so a
    ``rename`` answer with no new name is schema-invalid rather than
    silently falling back to the drafted name.

    Issue athenaeum#2016 (the ratification child): ``dimension-proposal`` now
    HAS an inbound applier
    (:func:`athenaeum.decision_answers._apply_dimension_proposal_answer`),
    registered in :data:`ANSWERABLE_AS` below — this schema is what that
    applier (and :func:`validate_answer`) checks a ``rename`` answer's
    ``name`` against.
    """
    return {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["approve", "rename", "reject"],
                "description": description,
            },
            "name": {
                "type": "string",
                "description": "Required when verdict is 'rename': the operator-chosen "
                "replacement for the drafter's proposed dimension name.",
            },
            "note": _NOTE_PROPERTY,
        },
        "required": ["verdict"],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"verdict": {"const": "rename"}}, "required": ["verdict"]},
                "then": {"required": ["name"]},
            },
            {
                "if": {
                    "properties": {"verdict": {"enum": ["approve", "reject"]}},
                    "required": ["verdict"],
                },
                "then": {"not": {"required": ["name"]}},
            },
        ],
    }


#: Per-decision-type framing metadata. Keyed by the ``type`` tag
#: :mod:`athenaeum.decisions` stamps on each item.
#:
#: ``reversibility`` / ``routing`` are stated per type rather than inferred,
#: because both are design judgements about what the decision DOES, which no
#: amount of payload inspection recovers. The reasoning for each is in the
#: ``rationale`` entry, which is also what seeds the item's
#: ``escalation_rationale``.
_TYPE_FRAMING: dict[str, dict[str, Any]] = {
    "question": {
        "reversibility": REVERSIBILITY_REVERSIBLE,
        "routing": ROUTING_COMPETENCE,
        "schema": _SCHEMA_FREE_TEXT,
        "default_action": "leave unresolved",
        "default_consequences": (
            "Both conflicting claims stay live; recall may surface either one, "
            "and the contradiction is re-detected on the next run."
        ),
        "rationale": (
            "The detector found claims it cannot rank: the sources carry no "
            "precedence order that settles which one holds."
        ),
    },
    "confirmation": {
        # The narrowed scope has already shipped, so undoing it means
        # building the alternative — real work, but a supported path.
        "reversibility": REVERSIBILITY_REVERSIBLE_WITH_WORK,
        "routing": ROUTING_AUTHORITY,
        "schema": _approve_reject_schema(
            description="approve = accept the narrowed scope; reject = the "
            "alternative is required.",
        ),
        "default_action": "accept the narrowed scope",
        "default_consequences": (
            "The alternative behaviour is not built, and no record is left "
            "that it was considered and declined."
        ),
        "rationale": (
            "An agent narrowed scope mid-build. Whether the narrower scope is "
            "acceptable is a question of what the operator wants, not of what "
            "the code can determine."
        ),
    },
    "merge": {
        # athenaeum#716 made a fold reversible: the folded source survives as
        # a tombstone, so the inverse (unfold) exists but costs work.
        "reversibility": REVERSIBILITY_REVERSIBLE_WITH_WORK,
        "routing": ROUTING_COMPETENCE,
        "schema": _approve_reject_schema(
            description="approve = fold these sources together; reject = keep "
            "them separate.",
        ),
        "default_action": "reject (keep the pages separate)",
        "default_consequences": (
            "The pages stay separate, so recall can return either of two "
            "partial answers instead of one whole one. Nothing is destroyed."
        ),
        "rationale": (
            "Topic similarity is not sameness — the comparator could not "
            "establish that these pages describe the same thing rather than "
            "two things that merely read alike."
        ),
    },
    "retraction": {
        "reversibility": REVERSIBILITY_IRREVERSIBLE,
        "routing": ROUTING_AUTHORITY,
        "schema": _SCHEMA_FREE_TEXT,
        "default_action": "leave the downstream merge standing, flagged",
        "default_consequences": (
            "Content derived from a retracted observation stays readable in "
            "the corpus, flagged but not removed."
        ),
        "rationale": (
            "An observation was retracted after content was already derived "
            "from it. Whether that content may stand is a question about a "
            "person's data, which only its subject's authority settles."
        ),
    },
    "audit": {
        "reversibility": REVERSIBILITY_REVERSIBLE,
        "routing": ROUTING_COMPETENCE,
        "schema": _SCHEMA_FREE_TEXT,
        "default_action": "no review recorded",
        "default_consequences": (
            "The sampled decision is never checked, so the calibration rate "
            "it was drawn to measure is computed from a smaller sample."
        ),
        "rationale": (
            "This item is a deliberate SAMPLE, not a failure: it exists to "
            "measure how often the automated verdict is wrong, which cannot "
            "be measured without a human reading one."
        ),
    },
    "quarantine": {
        "reversibility": REVERSIBILITY_REVERSIBLE_WITH_WORK,
        "routing": ROUTING_AUTHORITY,
        "schema": _approve_reject_schema(
            description="approve = release the file for ingestion; reject = "
            "leave it quarantined.",
        ),
        "default_action": "leave quarantined",
        "default_consequences": (
            "The file is never ingested, so whatever it contains stays "
            "outside the corpus — including anything the corpus needs."
        ),
        "rationale": (
            "The file tripped a sensitivity or provenance gate. Whether its "
            "contents may enter the corpus is a permission question."
        ),
    },
    "proposed-rule": {
        "reversibility": REVERSIBILITY_REVERSIBLE,
        "routing": ROUTING_AUTHORITY,
        "schema": _approve_reject_schema(
            description="approve = adopt the rule; reject = discard it.",
        ),
        "default_action": "reject (do not adopt the rule)",
        "default_consequences": (
            "The pattern the rule was drafted from keeps recurring and keeps "
            "producing items in this queue."
        ),
        "rationale": (
            "A drafted rule changes how future intake is handled. Adopting "
            "policy is the operator's call, not the drafter's."
        ),
    },
    "coordinate": {
        # Undoing a coordinate answer costs nothing but a second answer --
        # the same REVERSIBLE class "question" uses, for the same reason.
        "reversibility": REVERSIBILITY_REVERSIBLE,
        "routing": ROUTING_COMPETENCE,
        "schema": _SCHEMA_COORDINATE,
        "default_action": "leave the pair(s) underdetermined",
        "default_consequences": (
            "The pair(s)' verdict stays `underdetermined`; recall may "
            "surface either side, and nothing is auto-authorized to treat "
            "them as a settled merge or supersession until answered."
        ),
        "rationale": (
            "The comparator could not resolve a required separator "
            "dimension on one or both sides -- the missing coordinate, not "
            "a content judgement, is what blocked a verdict (issue "
            "athenaeum#715's `underdetermined` branch)."
        ),
    },
    "dimension-proposal": {
        # Issue athenaeum#2015 (athenaeum#719 Plan step 3): retiring a
        # mis-registered dimension re-nulls its coordinates (see
        # athenaeum.dimensions.retire_dimension_coordinate) -- it never
        # moves or deletes claim data -- so the class is REVERSIBLE, the
        # same as "question"/"coordinate", not REVERSIBLE_WITH_WORK.
        "reversibility": REVERSIBILITY_REVERSIBLE,
        # Registering a new axis is a policy decision about the registry
        # itself, like "proposed-rule" adopting a rule -- the operator's
        # call, not something the drafter's confidence settles.
        "routing": ROUTING_AUTHORITY,
        "schema": _approve_rename_reject_schema(
            description="approve = register the dimension as drafted; "
            "rename = register it under a different name (see 'name'); "
            "reject = discard the proposal.",
        ),
        "default_action": "reject (do not register the dimension)",
        "default_consequences": (
            "The recurring missing-dimension shape keeps recurring and "
            "keeps producing signal-mining candidates; no new axis is "
            "added to the registry."
        ),
        "rationale": (
            "The self-tuning loop detected a recurring missing-dimension "
            "shape and drafted a candidate axis from it. Whether to adopt "
            "a new dimension into the operator's registry is policy, not "
            "something the mining signal alone can decide."
        ),
    },
    "tier-movement-proposal": {
        # Issue athenaeum#2019 (athenaeum#719 Plan step 6): this is an ADVISORY
        # review flag, not a tier assignment -- athenaeum#1514 retired the
        # memory-tier vocabulary and its promote-on-use sweep, so there is
        # no tier left to move a claim into, and approving this today
        # changes nothing beyond the ledger record itself. Reversible is
        # therefore the honest class: there is no mutation to undo.
        "reversibility": REVERSIBILITY_REVERSIBLE,
        # Same reasoning as "proposed-rule"/"dimension-proposal": adopting
        # (or even just acknowledging) a usage-pattern finding as worth
        # acting on is the operator's call, not the drafter's confidence.
        "routing": ROUTING_AUTHORITY,
        "schema": _approve_reject_schema(
            description="approve = acknowledge the usage-pattern flag; "
            "reject = dismiss it.",
        ),
        "default_action": "leave unacknowledged",
        "default_consequences": (
            "The claim's low-reference usage pattern is never reviewed; it "
            "keeps being pushed at the same rate and keeps producing the "
            "same proposal on the next drafting pass."
        ),
        "rationale": (
            "A claim's push/reference counts crossed a configured "
            "low-value threshold. There is no automatic tier-movement "
            "executor (athenaeum#1514), so a human reviewing the pattern is the "
            "only available next step."
        ),
    },
    "policy-pack-edit": {
        # athenaeum#719's own AC states this directly: "Policy-pack edits are
        # proposal-only and never auto-applied -- policy changes are
        # irreversible-class decisions." A retention-pack rule governs
        # erasure-class classification for whole categories of future
        # content; unwinding a bad edit after content has already been
        # classified under it is not a simple undo. Same class as
        # "retraction" for the same reason: a consequential, not-cheaply-
        # reversible change to policy that governs other data.
        "reversibility": REVERSIBILITY_IRREVERSIBLE,
        "routing": ROUTING_AUTHORITY,
        "schema": _approve_reject_schema(
            description="approve = apply the proposed edit to the named "
            "policy pack; reject = discard the proposal.",
        ),
        "default_action": "reject (do not edit the policy pack)",
        "default_consequences": (
            "The named policy pack's rule table is unchanged; whatever "
            "classification gap or over/under-broad rule motivated the "
            "proposal persists."
        ),
        "rationale": (
            "A proposed edit to a retention-policy pack changes how future "
            "content is classified for erasure/retention purposes across "
            "the pack's whole ruleset. That is a policy decision, not "
            "something any detector's confidence settles on its own."
        ),
    },
}

#: Issue athenaeum#2019 (athenaeum#719 Plan step 6, the delegation-ratchet
#: guard): decision types whose RATIFICATION (an "approve" answer, once a
#: future applier wires one in) must be refused outright while the queue's
#: effort budget is in breach -- "no constitutional amendments during an
#: emergency" (athenaeum#717's own Ratchet guard 1 wording). See
#: :mod:`athenaeum.decision_budget`'s :data:`~athenaeum.decision_budget.
#: RATCHET_GUARDED_DECISION_TYPES` and :func:`~athenaeum.decision_budget.
#: ratification_refusal` for the actual guard -- this module only frames
#: items, it does not gate ratification, so the guard is NOT re-declared
#: here. This comment exists purely so a reader who finds "policy-pack-edit"
#: here also finds the guard's module.

#: Issue athenaeum#2015 (athenaeum#719 Plan step 3): "dimension-proposal" is
#: framed (visible in the unified queue, with a real schema a future triage
#: UI can render) but DELIBERATELY carries no entry in :data:`ANSWERABLE_AS`
#: below -- :func:`answerable_as("dimension-proposal")` returns ``None``
#: until the ratification child (the next one; see the module this issue's
#: own "Out of scope" section names) lands
#: ``decision_answers._apply_dimension_proposal_answer`` and registers it
#: there. This is the SAME declared-but-not-yet-answerable state
#: "quarantine" already lives in (see that type's own comment on
#: :data:`_DEFAULT_ACCEPTANCE_VERDICT` above) -- not a half-wired bug.
#: ``tests/test_dimension_proposals.py``'s misroute-guard tests assert both
#: halves hold: the item is listed, and answering it is refused cleanly by
#: every inbound caller (the CLI, the MCP triage path), never dispatched
#: into ``decision_answers._apply_proposed_rule_answer`` by accident.
#: Issue athenaeum#2015 (athenaeum#719 Plan step 3) framed "dimension-proposal"
#: (visible in the unified queue, with a real schema) but deliberately left
#: it out of :data:`ANSWERABLE_AS` until the ratification child landed.
#: Issue athenaeum#2016 (that child) now registers it below, with its OWN
#: applier (``decision_answers._apply_dimension_proposal_answer``) — NOT
#: routed through ``proposed-rule``, which writes a different store
#: (:mod:`athenaeum.rule_proposals`) entirely.
#: ``tests/test_dimension_proposals.py``'s former misroute-guard tests were
#: flipped by athenaeum#2016 to assert the OPPOSITE of what they asserted
#: under athenaeum#2015: the type IS in :data:`athenaeum.decision_answers.
#: VALID_DECISION_TYPES`, :func:`answerable_as` returns ``"dimension-proposal"``,
#: and a well-formed answer reaches ``_apply_dimension_proposal_answer``,
#: never ``_apply_proposed_rule_answer``.

#: Framing for an item whose ``type`` this module does not know. Deliberately
#: the most conservative cell of the table: irreversible, owner-only, free
#: text. A new decision type is thereby safe by default — it reaches a human
#: with a stated rationale rather than being quietly auto-classified as
#: cheap — and :func:`athenaeum.decisions`'s own tests assert every live type
#: has a real entry, so this is a safety net, not a shrug.
_UNKNOWN_FRAMING: dict[str, Any] = {
    "reversibility": REVERSIBILITY_IRREVERSIBLE,
    "routing": ROUTING_AUTHORITY,
    "schema": _SCHEMA_FREE_TEXT,
    "default_action": "leave unresolved",
    "default_consequences": "The item stays in the queue, undecided.",
    "rationale": (
        "This decision type carries no framing entry, so nothing is known "
        "about whether it can be undone. It is routed to the owner."
    ),
}

#: Payload keys dropped, in this order, when a context bundle is over cap.
#: Ordered by bulk-per-unit-of-signal: page/draft bodies first (they are the
#: single largest contributor and are always reachable from the item's own
#: id), then long prose fields, then the fan-out lists. Nothing in this list
#: is required to understand the question — the question itself lives in
#: ``summary``, which is never shrunk.
_SHRINK_ORDER: tuple[str, ...] = (
    "draft_merged_body",
    "body",
    "full_body",
    "passages",
    "proposal",
    "rationale",
    "description",
    # Issue athenaeum#1993: ``coordinate_to_decision``'s structured,
    # per-pair recovery of a batch's members (parsed off the SAME raw
    # ``description`` text this already drops) — same bulk-per-unit-of-
    # signal class as ``description`` for a many-pair batch, dropped right
    # after it for the same reason. Absent from every other type's
    # payload, so this is a no-op for them.
    "members",
    "context",
    "sources",
)

#: Keys that survive every shrink step — the irreducible pointer bundle. If
#: these alone are over cap, the item escalates to a scheduled review.
_POINTER_KEYS: frozenset[str] = frozenset(
    {"id", "type", "created_at", "confidence", "merge_target_name", "entity", "source"}
)


def response_schema_for(decision_type: str) -> dict[str, Any]:
    """JSON Schema a valid answer to a ``decision_type`` item must satisfy.

    Deep-copied on the way out. The schemas in :data:`_TYPE_FRAMING` are
    shared between every item of a type, and a schema travels out of this
    module into an item dict that callers (the CLI, the MCP boundary, a
    future triage agent) are free to serialize and edit — a shallow copy
    would let one of them reach ``properties`` and poison every later item.
    """
    schema = _TYPE_FRAMING.get(decision_type, _UNKNOWN_FRAMING)["schema"]
    return copy.deepcopy(schema)


def reversibility_for(decision_type: str) -> str:
    """Reversibility class of a ``decision_type`` item."""
    framing = _TYPE_FRAMING.get(decision_type, _UNKNOWN_FRAMING)
    return str(framing["reversibility"])


def routing_for(decision_type: str) -> str:
    """Whether a ``decision_type`` item reaches a human for authority or competence."""
    framing = _TYPE_FRAMING.get(decision_type, _UNKNOWN_FRAMING)
    return str(framing["routing"])


def proposed_default_for(decision_type: str) -> dict[str, str]:
    """The conservative no-answer outcome, and what it costs.

    Every default in :data:`_TYPE_FRAMING` leaves the corpus as it is, so an
    unanswered item can never enact a change by timing out.
    """
    framing = _TYPE_FRAMING.get(decision_type, _UNKNOWN_FRAMING)
    return {
        "action": str(framing["default_action"]),
        "consequences": str(framing["default_consequences"]),
    }


#: Issue athenaeum#1996 guard 2: the discrete answer-schema ``verdict`` token
#: that matches each approve/reject-shaped decision type's own
#: :func:`proposed_default_for` action, hand-mapped here once rather than
#: parsed from that prose at runtime -- ``default_action`` is written for a
#: human reader (e.g. ``"leave quarantined"``) and has already drifted in
#: wording from the machine token (``"reject"``) that denotes the very same
#: outcome. A free-text type (``question``/``retraction``/``audit``, or an
#: unknown type) has no discrete default an answer could match unmodified,
#: so it can never register as a "default acceptance" -- see
#: :func:`is_default_acceptance`.
#:
#: Deliberately does NOT include ``quarantine``, even though it uses
#: :func:`_approve_reject_schema` too: :func:`answerable_as` returns
#: ``None`` for it (:data:`ANSWERABLE_AS` has no entry), so it has no
#: applier and cannot be answered through
#: :mod:`athenaeum.decision_answers`'s ``apply_decision_answers`` at all
#: today -- it resolves only through :func:`athenaeum.quarantine.
#: release_quarantine`'s own dedicated path. A table entry this function
#: can never actually be asked about would be a declared-but-unreachable
#: type (the exact Seer finding on PR athenaeum#2005 for ``confirmation`` before the
#: ``origin_decision_type`` fix below) -- dropped rather than left looking
#: live. Add it back if/when ``quarantine`` grows an inbound applier.
#:
#: Issue athenaeum#2016: ``dimension-proposal`` now has an inbound applier
#: (:func:`athenaeum.decision_answers._apply_dimension_proposal_answer`), so
#: per the rule above it qualifies for an entry -- its ``default_action`` is
#: "reject (do not register the dimension)", the SAME discrete token
#: ``merge``/``proposed-rule`` already use for their own conservative default.
_DEFAULT_ACCEPTANCE_VERDICT: dict[str, str] = {
    "confirmation": "approve",  # default_action: "accept the narrowed scope"
    "merge": "reject",  # default_action: "reject (keep the pages separate)"
    "proposed-rule": "reject",  # default_action: "reject (do not adopt the rule)"
    "dimension-proposal": "reject",  # default_action: "reject (do not register the dimension)"
}


def default_acceptance_verdict_for(decision_type: str) -> str | None:
    """The answer-schema ``verdict`` token that matches *decision_type*'s
    ``proposed_default``, or ``None`` when this type has no discrete
    default a free-text answer could match (issue athenaeum#1996 guard 2).
    """
    return _DEFAULT_ACCEPTANCE_VERDICT.get(decision_type)


def is_default_acceptance(decision_type: str, verdict: str) -> bool:
    """Whether answering *decision_type* with *verdict* accepts that item's
    ``proposed_default`` UNMODIFIED, as opposed to overriding it (issue
    athenaeum#1996 guard 2).

    This measures the SHAPE of the answer against the default -- never
    whether the default (or the human's acceptance of it) was actually
    correct. Case/whitespace-insensitive on *verdict*, matching how every
    answer applier already normalizes it (see e.g.
    :mod:`athenaeum.decision_answers`'s ``_apply_merge_answer``).
    """
    default_verdict = default_acceptance_verdict_for(decision_type)
    if default_verdict is None:
        return False
    return verdict.strip().lower() == default_verdict


def escalation_rationale_for(item: dict[str, Any]) -> str:
    """What, specifically, could not be determined without a human.

    The type-level rationale from :data:`_TYPE_FRAMING`, extended with the
    one discriminating detail the item's own payload carries where there is
    one (the conflict type a question was raised under; the confidence the
    comparator reached on a merge). Keeping the detail OUT of the type table
    is what stops the rationale from being a constant string that satisfies
    the field without informing anyone.
    """
    decision_type = str(item.get("type") or "")
    framing = _TYPE_FRAMING.get(decision_type, _UNKNOWN_FRAMING)
    rationale = str(framing["rationale"])
    payload = item.get("payload")
    payload = payload if isinstance(payload, dict) else {}

    conflict_type = payload.get("conflict_type")
    if isinstance(conflict_type, str) and conflict_type.strip():
        rationale += f" Conflict type: {conflict_type.strip()}."
    confidence = item.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        rationale += f" Automated confidence: {confidence}."
    return rationale


def build_context_bundle(item: dict[str, Any]) -> dict[str, Any]:
    """The minimal structured context needed to answer ``item``.

    Built from the item's own ``payload`` — this module invents no context
    and reads no file. ``summary`` is deliberately excluded: it is the
    question, not context, and it is never shrunk or dropped.
    """
    payload = item.get("payload")
    bundle: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
    for key in ("id", "type", "created_at", "confidence"):
        value = item.get(key)
        if value is not None and key not in bundle:
            bundle[key] = value
    return bundle


def bundle_tokens(bundle: dict[str, Any]) -> int:
    """Estimated token cost of rendering ``bundle`` to a human.

    Measured over the bundle's JSON serialization, using the repo's existing
    estimator (:func:`athenaeum.context.estimate_tokens`) so this cap is on
    the same footing as every other token figure athenaeum reports. A bundle
    carrying a non-serializable value is measured over its ``repr`` rather
    than raising — an unmeasurable item must still be measured somehow, and
    over-reporting its size is the safe direction to err.
    """
    try:
        rendered = json.dumps(bundle, ensure_ascii=False, sort_keys=True, default=repr)
    except (TypeError, ValueError):  # pragma: no cover - default=repr covers this
        rendered = repr(bundle)
    return estimate_tokens(rendered)


def _shrink(bundle: dict[str, Any], *, cap: int) -> tuple[dict[str, Any], list[str]]:
    """Drop :data:`_SHRINK_ORDER` keys until ``bundle`` fits ``cap``.

    Returns the shrunk bundle and the keys dropped, in the order dropped.
    Stops as soon as the bundle fits, so the cheapest sufficient decompose
    is the one applied.
    """
    shrunk = dict(bundle)
    dropped: list[str] = []
    for key in _SHRINK_ORDER:
        if bundle_tokens(shrunk) <= cap:
            break
        if key in shrunk:
            del shrunk[key]
            dropped.append(key)
    return shrunk, dropped


def _pointer_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """The irreducible identifying subset of ``bundle``."""
    return {k: v for k, v in bundle.items() if k in _POINTER_KEYS}


def frame_decision(
    item: dict[str, Any],
    *,
    max_context_tokens: int,
) -> dict[str, Any]:
    """Return ``item`` with framing fields added and the context cap applied.

    This is the single admission gate for the unified queue. It never
    mutates ``item`` and never lets an over-cap bundle through: a bundle
    that does not fit ``max_context_tokens`` is decomposed, and one that
    still does not fit after decomposition is admitted as a
    :data:`ROUTING_SCHEDULED_REVIEW` item with a pointer-only bundle.

    Being the single gate is also what makes the cap check happen AFTER
    batching rather than before: a batched item is one item and passes
    through here exactly once, so no amount of member-sizing can smuggle
    oversized context past the cap.
    """
    decision_type = str(item.get("type") or "")
    framed = dict(item)
    bundle = build_context_bundle(item)
    cap = max_context_tokens if max_context_tokens > 0 else 0

    tokens = bundle_tokens(bundle)
    dropped: list[str] = []
    overflow: dict[str, int] | None = None
    routing = routing_for(decision_type)

    if cap > 0 and tokens > cap:
        bundle, dropped = _shrink(bundle, cap=cap)
        tokens = bundle_tokens(bundle)
        if tokens > cap:
            # Even the irreducible pointer bundle is over cap. This is not a
            # one-screen decision, so it is not presented as one.
            bundle = _pointer_bundle(bundle)
            tokens = bundle_tokens(bundle)
            overflow = {"tokens": tokens, "cap": cap}
            routing = ROUTING_SCHEDULED_REVIEW

    framed["context_bundle"] = bundle
    framed["context_tokens"] = tokens
    framed["context_cap"] = cap
    framed["context_decomposed"] = bool(dropped)
    if dropped:
        framed["context_dropped"] = dropped
    if overflow is not None:
        framed["context_overflow"] = overflow
    framed["reversibility"] = reversibility_for(decision_type)
    framed["routing"] = routing
    framed["proposed_default"] = proposed_default_for(decision_type)
    framed["response_schema"] = response_schema_for(decision_type)
    framed["escalation_rationale"] = escalation_rationale_for(item)
    return framed


def validate_answer(decision_type: str, answer: dict[str, Any]) -> list[str]:
    """Validate ``answer`` against ``decision_type``'s response schema.

    Returns a list of human-readable validation errors — empty when the
    answer is valid. Returning errors rather than raising keeps this usable
    from :mod:`athenaeum.decision_answers`'s fail-soft applier, which must
    record a refusal and move on rather than abort a batch.
    """
    import jsonschema

    schema = response_schema_for(decision_type)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(answer), key=lambda e: list(e.path))
    return [
        f"{'/'.join(str(p) for p in error.path) or '<answer>'}: {error.message}"
        for error in errors
    ]


#: Prefix every error :func:`validate_answer` locates on the ``verdict``
#: property carries. Used by :func:`shape_errors_only`.
_VERDICT_ERROR_PREFIX = "verdict:"


def shape_errors_only(errors: list[str]) -> list[str]:
    """Drop the errors that are about a verdict's VALUE rather than its SHAPE.

    The response schema and the per-type resolvers overlap on exactly one
    thing: whether a verdict string is in the type's vocabulary. Both can
    detect it, and if both report it the same condition acquires two error
    codes — ``schema_invalid`` here and the resolver's long-established
    ``invalid_decision``, which the MCP mutators document as part of their
    contract.

    So the line is drawn here rather than left to chance: the schema owns the
    answer's shape (unknown keys, wrong types, a missing verdict), and each
    resolver keeps owning its own verdict vocabulary. Errors located on the
    ``verdict`` property are therefore dropped — not ignored, but deferred to
    the resolver that is about to see the same answer and report it with a
    per-type message.
    """
    return [error for error in errors if not error.startswith(_VERDICT_ERROR_PREFIX)]


#: Queue item ``type`` -> the ``decision_type``
#: :func:`athenaeum.decision_answers.apply_decision_answers` dispatches on.
#:
#: The two id spaces are NOT the same vocabulary, which is easy to miss: the
#: outbound view tags seven types, while the inbound applier registers four
#: (:data:`athenaeum.decision_answers.VALID_DECISION_TYPES`). Handing the
#: applier an outbound tag it does not register raises, so any interface that
#: accepts a type from ``decisions list`` must translate through this table
#: and refuse what it cannot route.
#:
#: ``confirmation`` maps to ``question`` because that is literally how it is
#: stored and resolved — a confirmation IS a block in
#: ``_pending_questions.md`` with ``decision_kind: confirmation``, and
#: :func:`athenaeum.decisions.confirmation_to_decision` is explicit that
#: nothing about its resolution differs. Nothing is lost by recording the
#: answer against the question path: the confirmation-ness lives in the
#: block, not in the answer file.
#:
#: ``retraction`` and ``quarantine`` are deliberately ABSENT rather than
#: guessed at. Neither has an inbound applier at all, so there is nothing to
#: translate to, and inventing one here would be a cut-over smuggled in
#: through a lookup table.
ANSWERABLE_AS: dict[str, str] = {
    "question": "question",
    "confirmation": "question",
    "merge": "merge",
    "audit": "audit",
    "proposed-rule": "proposed-rule",
    # Issue athenaeum#1993: a dedicated applier, NOT routed through
    # "question" -- a coordinate answer writes coordinates and triggers a
    # mechanical re-compare, which the free-text question path knows
    # nothing about. See `athenaeum.decision_answers._apply_coordinate_answer`.
    "coordinate": "coordinate",
    # Issue athenaeum#2016: self-mapped -- the outbound queue tag and the
    # inbound applier's decision_type are the SAME string for this type
    # (no translation needed, matching merge/audit/proposed-rule/coordinate).
    "dimension-proposal": "dimension-proposal",
}


def answerable_as(decision_type: str) -> str | None:
    """The applier's ``decision_type`` for a queue item type, or ``None``.

    ``None`` means the item cannot currently be answered through the answer
    interface — see :data:`ANSWERABLE_AS`. Callers must refuse cleanly on
    ``None`` rather than passing the value through, which raises.
    """
    return ANSWERABLE_AS.get(decision_type)
