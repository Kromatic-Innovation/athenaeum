# SPDX-License-Identifier: Apache-2.0
"""Agent triage lane over the unified decision queue (issue athenaeum#1995).

Child of athenaeum#717 (unified decision queue), slice (e) of the
hestia-lanes-agent 2026-10-06 "what remains" survey on athenaeum#717 (AC
group 5, "Agent triage, honestly scoped"). :mod:`athenaeum.decisions`
already joins every human-decision surface into one queue;
:mod:`athenaeum.decision_framing` already frames
each item with a ``routing`` tag (:data:`athenaeum.decision_framing.
ROUTING_AUTHORITY` vs :data:`~athenaeum.decision_framing.ROUTING_COMPETENCE`)
and a published ``response_schema``. This module is the first thing that
actually ACTS on that queue on an operator's behalf: :func:`run_triage`
walks every pending item and, for each one, does exactly one of two things —

1. **Authority items are PREPARED, never answered.** Routing is permission,
   not knowledge (:mod:`athenaeum.decision_framing`'s own framing) — a
   ``confirmation``/``retraction``/``quarantine``/``proposed-rule`` item (or
   anything this module does not recognize) always passes to the human with
   its already-framed ``proposed_default``/``escalation_rationale`` intact.
   Nothing here ever submits an answer for one. This is a safety property,
   not a nicety — see ``tests/test_triage.py``'s
   ``TestAuthorityNeverAutoAnswered`` for the explicit boundary test,
   including under prompt-injection pressure (an adversarial payload in the
   item's own context must not move this gate at all, because the gate
   never reads untrusted content in the first place — see "Prompt-injection
   hardening" below).
2. **Competence items are offered to a pluggable researcher.** Only
   ``question``/``coordinate``-type items are offered (see "Why only these
   two types" below). A :data:`TriageResearcher` callable either resolves
   the item (returns a :class:`TriageResolution`) or declines (returns
   ``None``). A resolved item is submitted through :func:`submit_answer` —
   the SAME validate-then-write sequence :mod:`athenaeum._cmd_decisions`'s
   ``answer`` subcommand uses (:func:`athenaeum.decision_framing.answerable_as`,
   :func:`~athenaeum.decision_framing.validate_answer`,
   :func:`athenaeum.decision_answers.write_decision_answer`) — never a
   parallel write path. A declined item is left exactly where it already
   was; the next ``athenaeum ingest-answers`` tick and the budget ledger
   (:mod:`athenaeum.decision_budget`) do not even know triage looked at it.

**Why only `question`/`coordinate`, and why the shipped researcher only
ever resolves the latter.** ``merge`` and ``audit`` are also
competence-routed and answerable, but both are deliberately EXCLUDED from
absorption:

- ``merge`` — answering approve/reject on a merge proposal IS re-judging
  the comparator's verdict, which this issue's "Out of scope" section
  rules out explicitly ("triage only answers framed decisions, it never
  re-judges a verdict").
- ``audit`` — an audit item IS the calibration measurement surface itself
  (:mod:`athenaeum.calibration`'s sampled T1/T2 review). Auto-answering a
  sample would corrupt the very signal it exists to produce.

``question`` (a detector- or agent-raised free-text escalation) and
``coordinate`` (issue athenaeum#1993's structured "supply a separator-
dimension coordinate" item, split out of the generic ``question`` type
after this module first shipped — see "Catch-up note" below) are both
left in :data:`RESEARCHABLE_DECISION_TYPES` because both are, in
principle, things a research step could resolve. The SHIPPED default
researcher (:func:`coordinate_request_researcher`) only ever resolves
``coordinate`` items: a ``question``'s payload is free text with no
structural contract a deterministic reader can safely act on, so the
default researcher declines every ``question`` item unconditionally. The
type stays in the researchable set anyway — a future, more capable
researcher plugged in via :data:`TriageResearcher` is free to attempt one;
nothing about this module's routing/sampling mechanics needs to change for
that to happen.

**The default researcher (:func:`coordinate_request_researcher`) is
deterministic — no LLM call, and it never reads a page BODY at all.** A
``coordinate`` item names one or more pairs and, per pair, one or more
separator dimensions the comparator could not resolve
(:func:`athenaeum.verdict_effects.build_coordinate_request` /
:func:`~athenaeum.verdict_effects.queue_coordinate_batch`) — structurally
recovered via ``item["payload"]["members"]``
(:func:`athenaeum.decisions.coordinate_to_decision`), never re-parsed from
free text. For each named pair/dimension, this researcher reads BOTH
sides' CURRENT frontmatter coordinate value
(:func:`athenaeum.dimensions.coordinate_value` — the exact raw-value
reader :func:`athenaeum.pending_merges._write_coordinate` round-trips
against). If every named dimension already has a value on BOTH named
pages, it resupplies those same, already-asserted values back through
:func:`submit_answer`'s ``coordinate`` applier
(:mod:`athenaeum.decision_answers`'s ``_apply_coordinate_answer``, issue
athenaeum#1993) — which writes them (a no-op when they already match) and
mechanically re-compares the pair via the REAL comparator entry point
(:func:`athenaeum.comparator.record_comparison`), never a second,
ad hoc judgement this module invents. This is "research" in the sense the
issue's examples describe ("answerable from provenance/... session
context"): reading what the corpus itself already asserts and routing it
through the designated channel — not forming a new editorial opinion, and
not fabricating a value nothing on disk supports. If ANY named dimension
is missing a value on EITHER side of ANY named pair, the WHOLE item is
left for the human — never a partial answer (a partial coordinate answer
still flips the item "answered" per the applier's own contract, so
submitting one half of what's needed would silently drop the other half
of the question; see ``tests/test_coordinate_answer_loop.py``'s
``test_partial_answer_defers_to_the_next_llm_backed_pass`` for that
applier-level behavior this researcher deliberately never triggers).

Because this researcher never touches ``body`` at all — only a page's
parsed frontmatter mapping — there is no untrusted-prose surface inside
it for a prompt-injection payload to land on in the first place; see
"Prompt-injection hardening" below for how this is actually proven, not
just asserted.

One known, accepted characteristic of the UNDERLYING #1993 applier this
researcher submits through, noted here rather than worked around (fixing
it would mean editing ``_apply_coordinate_answer``, which this issue does
not own and which issue athenaeum#1994 is actively extending): the
applier's own mechanical re-compare always stamps the verdict ledger's
``authority_basis`` as ``f"human-batch:{decision_id}"`` regardless of
whether a human or this module answered — so the comparator's OWN verdict
ledger does not distinguish an agent-triage coordinate resolution from a
human one. This module's own records (the run report, and the calibration
ledger when sampled) are therefore the only place "answered by triage, not
a human" is actually visible for a ``coordinate`` resolution — see
"decided_by stamping" below.

**No new model-backend call site.** A genuinely live-source research agent
(as the issue's phrasing gestures at) would need one, but this lane's own
brief makes that gate unsatisfiable here: ``.github/llm-surface.txt``'s rot
check (``tests/test_llm_surface.py``) would require this module the moment
it imported the provider module's backend protocol, and once that is true
"``Evals: not needed``" is false by the gate's own criterion while a real
``Evals: <run-url>`` receipt needs a live ``evals.yml`` dispatch this lane
is explicitly forbidden from making. :data:`TriageResearcher` is a plugin
seam precisely so a future, model-backed researcher can be swapped in by a
later issue without touching this module's queue-walking/routing/
sample-audit mechanics at all.

**Prompt-injection hardening (issue athenaeum#1995 AC5).** Per the standing
rule in ``docs/design/conflict-resolution.md`` ("corpus page bodies are
untrusted data"), any corpus text this module reads is DATA, never
instructions — and, for the shipped researcher, no corpus BODY text is
read at all:

- The authority/competence routing gate and every control-plane value this
  module itself produces (``decided_by``, which type gets attempted, which
  item is prepared vs. submitted) are derived ONLY from the item's ``type``/
  ``routing`` fields and from this module's own code — never from parsed
  page or question text.
- :func:`coordinate_request_researcher` reads ONLY each referenced page's
  parsed frontmatter MAPPING (via :func:`athenaeum.models.parse_frontmatter`,
  then :func:`athenaeum.dimensions.coordinate_value` on specific, named
  keys) — never the page ``body``. A forged instruction sitting in a page's
  prose body is therefore not merely fenced-and-ignored, it is never
  parsed into anything this researcher looks at in the first place.
  ``tests/test_triage.py``'s ``TestInjectionHardening`` proves this
  directly: a page carrying an adversarial body is still resolved
  correctly (frontmatter is clean), and the run's outcome
  (``decided_by``, the submitted verdict's actual coordinate values) is
  byte-identical to the same fixture with an innocuous body — the
  injected text has zero observable effect, not merely a fenced one.

**decided_by stamping.** For a ``question`` item, the submitted ``verdict``
text IS what :func:`athenaeum.answers.resolve_by_id` writes into the
resolved block body — :mod:`athenaeum.decision_answers`'s question
applier does not thread ``note`` through to the store at all (see that
module's ``_apply_question_answer``). So :func:`_stamp_decided_by` appends
``(decided_by: agent:<ref>)`` directly onto a ``question`` answer's
``verdict`` text before submission — the one place the attribution is
guaranteed to survive into that type's durable, human-readable record.

A ``coordinate`` answer's ``verdict`` is instead a JSON-encoded payload
the applier parses and discards after writing (see
:data:`athenaeum.decision_framing._SCHEMA_COORDINATE`'s docstring) — the
applier's own resolved-block text is a FIXED
``"Coordinate(s) recorded. Re-compare: ..."`` summary it constructs
itself, never influenced by this module's ``verdict``/``note``.
Appending text to a coordinate verdict would corrupt its JSON and be
refused outright (``malformed_verdict_json``), so :func:`run_triage`
submits a coordinate resolution's JSON verbatim and records
``decided_by`` ONLY in its own run report
(:attr:`TriageOutcome.decided_by`) and, when sampled, in the calibration
ledger's ``reason`` field (:func:`athenaeum.calibration.sample_triage_decision`)
— the one durable surface this module itself controls for that type. See
"The default researcher" above for the related, accepted
``authority_basis`` characteristic at the comparator-ledger layer.

**Budget instrumentation (issue athenaeum#1995 AC8) needs no new wiring.**
:func:`athenaeum.decision_answers.apply_decision_answers` already calls
:func:`athenaeum.decision_budget.record_decision_answered` for every
applied decision answer, regardless of submitter — so a triage-submitted
answer feeds the items/day and decision-time figures for free, the moment
it is applied by the next ``ingest-answers`` tick. ``tests/test_triage.py``
proves this by running that real tick after a triage submission and
reading the resulting ``_decision_budget_events.jsonl`` entry, rather than
writing to it directly.

**Sample-auditing / confirmed_wrong** wire into :mod:`athenaeum.calibration`'s
EXISTING ``should_sample``/``record_audit_review``/``calibration_summary``
primitives via that module's additive
:func:`athenaeum.calibration.sample_triage_decision` /
:func:`~athenaeum.calibration.triage_confirmed_wrong_count` /
:func:`~athenaeum.calibration.triage_confirmed_wrong_threshold_breached` —
see that module's "Agent-triage sampling" section for why this is additive
rather than a second mechanism.

**Catch-up note (issue athenaeum#1993).** This module originally shipped
with a ``question``-only researcher that re-parsed a coordinate-request's
free-text description and re-ran Gate 1 itself
(:func:`athenaeum.comparator.gate1_separator_relations`) to narrate an
answer. Issue athenaeum#1993 landed on ``develop`` afterward and gave
coordinate-request items their OWN decision type (``coordinate``) with a
dedicated, structured applier (the ``_apply_coordinate_answer`` resupply
path this module now submits through) — so the researcher was rewritten
against the real mechanism rather than patched to route around it. A
pre-existing coordinate-request item still sitting on disk tagged
``decision_kind: question`` from before issue athenaeum#1993's writer-side
change landed is NOT retroactively handled by this researcher — that would
need re-detecting the legacy free-text marker as a SEPARATE code path
alongside the structured one, which this module deliberately does not
carry; such an item is simply escalated to the human like any other
``question``, same as it always was before this module existed.

Layering: L4 domain/pipeline module, a peer of :mod:`athenaeum.decisions` /
:mod:`athenaeum.decision_framing` / :mod:`athenaeum.decision_answers` /
:mod:`athenaeum.comparator` / :mod:`athenaeum.verdict_effects` (all L4) — may
import any of them plus L0-L3 (:mod:`athenaeum.calibration`,
:mod:`athenaeum.config`, :mod:`athenaeum.models`, :mod:`athenaeum.
dimensions`) freely. Never imports :mod:`athenaeum.cli` or any ``_cmd_*``
module (L5) — the CLI wiring (``athenaeum triage``) lives in the sibling
:mod:`athenaeum._cmd_triage`, which imports THIS module, not the other way
around.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from athenaeum.calibration import sample_triage_decision
from athenaeum.config import resolve_dimensions
from athenaeum.decision_answers import write_decision_answer
from athenaeum.decision_framing import (
    ROUTING_AUTHORITY,
    ROUTING_COMPETENCE,
    answerable_as,
    routing_for,
    validate_answer,
)
from athenaeum.decisions import list_pending_decisions
from athenaeum.dimensions import coordinate_value
from athenaeum.models import EntityIndex, parse_frontmatter

log = logging.getLogger(__name__)

#: ``decided_by`` prefix this module ALWAYS applies — never derived from,
#: or overridable by, anything the research step reads (see module
#: docstring, "Prompt-injection hardening").
TRIAGE_DECIDED_BY_PREFIX = "agent:"

#: Decision ``type``s the default researcher is OFFERED — see module
#: docstring, "Why only `question`/`coordinate`". A FUTURE researcher
#: plugged in via :data:`TriageResearcher` is free to decline just as
#: easily as the default one; this constant only bounds what
#: :func:`run_triage` ever calls a researcher FOR, so a misconfigured/
#: future researcher can never be asked to adjudicate a merge or an audit
#: sample by construction.
RESEARCHABLE_DECISION_TYPES: frozenset[str] = frozenset({"question", "coordinate"})

#: Decision types whose submitted ``verdict`` is a machine-readable
#: payload the applier parses (and therefore must NOT have text appended
#: to it — see :func:`_verdict_text_for_submission`).
_STRUCTURED_VERDICT_TYPES: frozenset[str] = frozenset({"coordinate"})

#: Actions one :class:`TriageOutcome` records.
ACTION_PREPARED = "prepared"  # authority (or unrecognized) routing — human only
ACTION_ABSORBED = "absorbed"  # competence item, researcher resolved, submitted
ACTION_ABSORBED_DRY_RUN = "absorbed-dry-run"  # as above, but dry_run=True
ACTION_ESCALATED = "escalated"  # competence item, no researcher resolved it
ACTION_REFUSED = "refused"  # researcher resolved it, but submission was refused


@dataclass(frozen=True)
class TriageResolution:
    """What a :data:`TriageResearcher` proposes for one competence item.

    ``verdict`` must already satisfy the item's ``response_schema`` outer
    shape (a non-empty string either way): for a ``question`` item it is
    free text a human would write, and :func:`run_triage` appends the
    ``decided_by`` tag onto it before submission; for a ``coordinate`` item
    it is a JSON-encoded ``{"answers": [...]}`` string
    (:data:`athenaeum.decision_framing._SCHEMA_COORDINATE_PAYLOAD`) and is
    submitted VERBATIM — see module docstring, "decided_by stamping".
    ``ref`` is the free-form researcher identifier that becomes
    ``agent:<ref>``; ``rationale`` is audit-readable context carried onto
    the calibration sample, never into the submitted verdict itself.
    """

    verdict: str
    ref: str
    rationale: str = ""


class TriageResearcher(Protocol):
    """Callable a researcher implements: resolve one item, or decline.

    ``item`` is one framed decision dict from
    :func:`athenaeum.decisions.list_pending_decisions` (already carries
    ``type``/``routing``/``payload``/``response_schema`` etc.); ``wiki_root``
    is where the corpus this item's payload may reference lives; ``config``
    is the resolved athenaeum config dict (or ``None``), forwarded so a
    researcher can resolve its own config knobs (e.g. the separator
    dimension registry) the same way the rest of the pipeline does.
    Returning ``None`` means "this item needs a human" — the ONLY refusal
    signal; raising is a bug, not a decline (:func:`run_triage` does not
    catch researcher exceptions).
    """

    def __call__(
        self, item: dict[str, Any], wiki_root: Path, *, config: dict[str, Any] | None = None
    ) -> TriageResolution | None: ...


def coordinate_request_researcher(
    item: dict[str, Any], wiki_root: Path, *, config: dict[str, Any] | None = None
) -> TriageResolution | None:
    """Default researcher: resupply a ``coordinate`` item's named dimensions
    from both sides' CURRENT frontmatter, if every one is already present.

    See the module docstring's "The default researcher" section for the
    full rationale. Declines (returns ``None``) on anything that is not a
    ``coordinate`` item (including every ``question`` — see "Why only
    `question`/`coordinate`"), on a malformed/unrecoverable member list,
    on a page id this corpus no longer has (renamed/retracted since the
    item was raised — answering against a page that may not even be the
    same one is exactly the kind of guess this module must not make), on
    an unknown dimension name, or on any named dimension missing a value
    on either side. Never partially answers a batch — see the module
    docstring's "never a partial answer" note.
    """
    if item.get("type") != "coordinate":
        return None
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None
    members = payload.get("members")
    if not isinstance(members, list) or not members:
        return None

    registry = resolve_dimensions(config)
    index = EntityIndex(wiki_root)
    answers: list[dict[str, Any]] = []

    for member in members:
        if not isinstance(member, dict):
            return None
        pair = str(member.get("pair") or "")
        dims = [str(d) for d in (member.get("dimensions") or [])]
        if not pair or "+" not in pair or not dims:
            return None
        id_a, id_b = pair.split("+", 1)
        if not id_a or not id_b:
            return None

        path_a = index.get_by_uid(id_a)
        path_b = index.get_by_uid(id_b)
        if path_a is None or path_b is None:
            return None  # page vanished/renamed; do not guess

        try:
            meta_a, _ = parse_frontmatter(path_a.read_text(encoding="utf-8"))
            meta_b, _ = parse_frontmatter(path_b.read_text(encoding="utf-8"))
        except OSError:
            return None

        dims_for_pair: dict[str, dict[str, Any]] = {}
        for dim_name in dims:
            dimension = registry.get(dim_name)
            if dimension is None:
                return None  # unknown dimension; do not guess its shape
            value_a = coordinate_value(dimension, meta_a)
            value_b = coordinate_value(dimension, meta_b)
            if value_a is None or value_b is None:
                return None  # still genuinely missing -> defer the WHOLE item
            dims_for_pair[dim_name] = {id_a: value_a, id_b: value_b}

        answers.append({"pair": pair, "dimensions": dims_for_pair})

    verdict = json.dumps({"answers": answers}, sort_keys=True)
    return TriageResolution(
        verdict=verdict,
        ref="coordinate-resupply",
        rationale=(
            f"Resupplied {len(answers)} coordinate-request member(s) from "
            "both sides' current frontmatter (no LLM judgement; the "
            "mechanical re-compare is the applier's own, not this module's)."
        ),
    )


def _stamp_decided_by(verdict: str, decided_by: str) -> str:
    """Append a ``(decided_by: agent:<ref>)`` tag to a free-text verdict.

    See module docstring, "decided_by stamping" — this is the durable
    record issue athenaeum#1995 AC2 asks for a ``question`` answer, since
    ``note`` is dropped on that apply path. NEVER called for a structured
    (``coordinate``) verdict — see :func:`_verdict_text_for_submission`.
    """
    return f"{verdict} (decided_by: {decided_by})"


def _verdict_text_for_submission(
    decision_type: str, resolution: TriageResolution, decided_by: str
) -> str:
    """The exact text submitted as ``verdict`` for *decision_type*.

    A structured type's verdict (currently only ``coordinate``) is a
    machine-parsed payload — submitted VERBATIM, since appending anything
    would corrupt it and be refused as ``malformed_verdict_json`` by the
    applier. Every other (free-text) type gets the ``decided_by`` stamp
    appended, since that is the only durable channel for it. See module
    docstring, "decided_by stamping".
    """
    if decision_type in _STRUCTURED_VERDICT_TYPES:
        return resolution.verdict
    return _stamp_decided_by(resolution.verdict, decided_by)


@dataclass(frozen=True)
class TriageSubmission:
    """Result of :func:`submit_answer` — one call to the real answer interface."""

    ok: bool
    path: Path | None = None
    error_code: str | None = None
    message: str = ""


def submit_answer(
    knowledge_root: Path,
    *,
    decision_id: str,
    decision_type: str,
    answer: dict[str, Any],
) -> TriageSubmission:
    """Validate + write one decision answer — the SAME sequence
    ``athenaeum decisions answer`` runs (:func:`athenaeum._cmd_decisions._cmd_answer`),
    reusing its exact underlying primitives
    (:func:`athenaeum.decision_framing.answerable_as`,
    :func:`~athenaeum.decision_framing.validate_answer`,
    :func:`athenaeum.decision_answers.write_decision_answer`) so there is
    no parallel write path (issue athenaeum#1995 AC1) — only a second thin
    caller of the same three functions the CLI command itself calls. This
    is type-agnostic: it works unchanged for ``coordinate`` (issue
    athenaeum#1993) exactly as it already did for ``question``, because
    ``answerable_as``/``validate_answer`` are themselves generic over
    decision type. Never touches ``_cmd_decisions.py`` (a file sibling
    lanes athenaeum#1992/athenaeum#1993/athenaeum#1994 are editing — see
    this issue's concurrency note).

    Refuses (returns ``ok=False``) exactly where the CLI would: an
    unanswerable type, a schema-invalid answer. Writing is deferred to
    the SAME ``athenaeum ingest-answers`` tick that applies every other
    answer file — this function never mutates ``_pending_questions.md``/
    ``_pending_merges.md``/the calibration ledger itself.
    """
    applier_type = answerable_as(decision_type)
    if applier_type is None:
        return TriageSubmission(
            ok=False,
            error_code="type_not_answerable",
            message=f"decision type {decision_type!r} has no inbound applier",
        )

    errors = validate_answer(decision_type, answer)
    if errors:
        return TriageSubmission(
            ok=False,
            error_code="schema_invalid",
            message="; ".join(errors),
        )

    path = write_decision_answer(
        knowledge_root / "raw",
        decision_id=decision_id,
        decision_type=applier_type,
        verdict=str(answer["verdict"]),
        note=str(answer.get("note", "")),
    )
    return TriageSubmission(ok=True, path=path)


@dataclass
class TriageOutcome:
    """What happened to one queue item during a triage pass."""

    decision_id: str
    decision_type: str
    routing: str
    action: str
    decided_by: str | None = None
    answer_path: Path | None = None
    error_code: str | None = None
    sampled: bool = False
    audit_id: str | None = None


@dataclass
class TriageReport:
    """Summary of one :func:`run_triage` pass.

    ``authority_prepared`` and ``competence_escalated`` are reported
    SEPARATELY (issue athenaeum#1995 AC6) — both are "reached the human",
    but for different reasons (permission vs. unresolved knowledge), and
    folding them into one count would let a shrinking competence-escalation
    rate be faked by routing more items through authority instead.
    """

    outcomes: list[TriageOutcome] = field(default_factory=list)

    @property
    def authority_prepared(self) -> int:
        return sum(1 for o in self.outcomes if o.routing == ROUTING_AUTHORITY)

    @property
    def competence_absorbed(self) -> int:
        return sum(
            1 for o in self.outcomes if o.action in (ACTION_ABSORBED, ACTION_ABSORBED_DRY_RUN)
        )

    @property
    def competence_escalated(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ACTION_ESCALATED)

    @property
    def refused(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ACTION_REFUSED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority_prepared": self.authority_prepared,
            "competence_absorbed": self.competence_absorbed,
            "competence_escalated": self.competence_escalated,
            "refused": self.refused,
            "total": len(self.outcomes),
            "outcomes": [
                {
                    "decision_id": o.decision_id,
                    "decision_type": o.decision_type,
                    "routing": o.routing,
                    "action": o.action,
                    "decided_by": o.decided_by,
                    "answer_path": str(o.answer_path) if o.answer_path else None,
                    "error_code": o.error_code,
                    "sampled": o.sampled,
                    "audit_id": o.audit_id,
                }
                for o in self.outcomes
            ],
        }


def run_triage(
    knowledge_root: Path,
    *,
    config: dict[str, Any] | None = None,
    researcher: TriageResearcher | None = None,
    dry_run: bool = False,
) -> TriageReport:
    """Walk the unified decision queue once; prepare authority items, offer
    competence ``question``/``coordinate`` items to *researcher*, submit
    what it resolves.

    ``researcher`` defaults to :func:`coordinate_request_researcher`.
    ``dry_run=True`` runs the researcher and records what WOULD be
    submitted (:data:`ACTION_ABSORBED_DRY_RUN`) without calling
    :func:`submit_answer` or :func:`athenaeum.calibration.sample_triage_decision` —
    nothing is written to disk.

    Routing is read from the item's own already-framed ``routing`` field
    (every item from :func:`athenaeum.decisions.list_pending_decisions` has
    one), falling back to :func:`athenaeum.decision_framing.routing_for`
    for a hand-built item dict in a test that omits it.
    """
    wiki_root = knowledge_root / "wiki"
    active_researcher = researcher or coordinate_request_researcher
    items = list_pending_decisions(wiki_root)
    report = TriageReport()

    for item in items:
        decision_id = str(item.get("id") or "")
        decision_type = str(item.get("type") or "")
        routing = str(item.get("routing") or routing_for(decision_type))

        if routing != ROUTING_COMPETENCE:
            report.outcomes.append(
                TriageOutcome(decision_id, decision_type, routing, ACTION_PREPARED)
            )
            continue

        if decision_type not in RESEARCHABLE_DECISION_TYPES:
            # Competence-routed and (for merge/audit) even answerable, but
            # deliberately never offered to a researcher — see module
            # docstring, "Why only `question`/`coordinate`".
            report.outcomes.append(
                TriageOutcome(decision_id, decision_type, routing, ACTION_ESCALATED)
            )
            continue

        resolution = active_researcher(item, wiki_root, config=config)
        if resolution is None:
            report.outcomes.append(
                TriageOutcome(decision_id, decision_type, routing, ACTION_ESCALATED)
            )
            continue

        decided_by = f"{TRIAGE_DECIDED_BY_PREFIX}{resolution.ref}"
        verdict_to_submit = _verdict_text_for_submission(decision_type, resolution, decided_by)

        if dry_run:
            report.outcomes.append(
                TriageOutcome(
                    decision_id,
                    decision_type,
                    routing,
                    ACTION_ABSORBED_DRY_RUN,
                    decided_by=decided_by,
                )
            )
            continue

        submission = submit_answer(
            knowledge_root,
            decision_id=decision_id,
            decision_type=decision_type,
            answer={"verdict": verdict_to_submit},
        )
        if not submission.ok:
            log.warning(
                "triage: submission refused for %s (%s): %s",
                decision_id,
                submission.error_code,
                submission.message,
            )
            report.outcomes.append(
                TriageOutcome(
                    decision_id,
                    decision_type,
                    routing,
                    ACTION_REFUSED,
                    error_code=submission.error_code,
                )
            )
            continue

        outcome = TriageOutcome(
            decision_id,
            decision_type,
            routing,
            ACTION_ABSORBED,
            decided_by=decided_by,
            answer_path=submission.path,
        )
        sampled_record = sample_triage_decision(
            wiki_root,
            proposal_id=decision_id,
            verdict=verdict_to_submit,
            reason=f"decided_by: {decided_by}; {resolution.rationale}",
            config=config,
        )
        if sampled_record is not None:
            outcome.sampled = True
            outcome.audit_id = str(sampled_record["id"])
        report.outcomes.append(outcome)

    return report


__all__ = [
    "TRIAGE_DECIDED_BY_PREFIX",
    "RESEARCHABLE_DECISION_TYPES",
    "ACTION_PREPARED",
    "ACTION_ABSORBED",
    "ACTION_ABSORBED_DRY_RUN",
    "ACTION_ESCALATED",
    "ACTION_REFUSED",
    "TriageResolution",
    "TriageResearcher",
    "TriageSubmission",
    "TriageOutcome",
    "TriageReport",
    "coordinate_request_researcher",
    "submit_answer",
    "run_triage",
]
