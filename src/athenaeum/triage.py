# SPDX-License-Identifier: Apache-2.0
"""Agent triage lane over the unified decision queue (issue athenaeum#1995).

Child of athenaeum#717 (unified decision queue), slice (e) of the
hestia-lanes-agent 2026-10-06 "what remains" survey on athenaeum#717 (AC
group 5, "Agent triage, honestly scoped"). :mod:`athenaeum.decisions` already joins every human-decision
surface into one queue; :mod:`athenaeum.decision_framing` already frames
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
   ``question``-type items are offered (see "Why only `question`" below).
   A :data:`TriageResearcher` callable either resolves the item (returns a
   :class:`TriageResolution`) or declines (returns ``None``). A resolved
   item is submitted through :func:`submit_answer` — the SAME validate-
   then-write sequence :mod:`athenaeum._cmd_decisions`'s ``answer``
   subcommand uses (:func:`athenaeum.decision_framing.answerable_as`,
   :func:`~athenaeum.decision_framing.validate_answer`,
   :func:`athenaeum.decision_answers.write_decision_answer`) — never a
   parallel write path. A declined item is left exactly where it already
   was; the next ``athenaeum ingest-answers`` tick and the budget ledger
   (:mod:`athenaeum.decision_budget`) do not even know triage looked at it.

**Why only `question`.** ``merge`` and ``audit`` are also competence-routed
and answerable, but both are deliberately EXCLUDED from absorption:

- ``merge`` — answering approve/reject on a merge proposal IS re-judging the
  comparator's verdict, which this issue's "Out of scope" section rules out
  explicitly ("triage only answers framed decisions, it never re-judges a
  verdict").
- ``audit`` — an audit item IS the calibration measurement surface itself
  (:mod:`athenaeum.calibration`'s sampled T1/T2 review). Auto-answering a
  sample would corrupt the very signal it exists to produce.

**The default researcher (:func:`coordinate_request_researcher`) is
deterministic — no LLM call.** It resolves exactly one concrete, safe case:
a ``question`` item born from the comparator's ``underdetermined`` verdict
(:func:`athenaeum.verdict_effects.build_coordinate_request` /
:func:`~athenaeum.verdict_effects.queue_coordinate_batch`), where the
MISSING coordinate(s) it named have since become determinate on BOTH
sides' CURRENT frontmatter. It answers by re-running Gate 1
(:func:`athenaeum.comparator.gate1_separator_relations`) — the SAME typed,
free, no-LLM comparator step that would itself have settled this pair had
the coordinate been known at compare time — never Gate 2's LLM content
judgement. This is "research" in the sense the issue's examples describe
("answerable from provenance/... session context"): reading what the corpus
itself now says, not forming a new editorial opinion about it. A batch with
any still-``unknown``/absent/ambiguous (``contains``/``overlaps``) named
dimension is left for the human, whole — never partially answered.

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
instructions:

- The authority/competence routing gate and every control-plane value this
  module itself produces (``decided_by``, which type gets attempted, which
  item is prepared vs. submitted) are derived ONLY from the item's ``type``/
  ``routing`` fields and from this module's own code — never from parsed
  page or question text. A page body (or a question's free-text
  description) containing a forged instruction therefore has no surface to
  act on, which ``tests/test_triage.py``'s
  ``TestInjectionHardening.test_adversarial_page_body_does_not_change_routing_or_attribution``
  proves directly: an adversarial payload is threaded through both the
  authority-prepare path and the default researcher, and the observed
  routing/``decided_by``/submitted-verdict are asserted byte-identical to
  the non-adversarial control.
- Any corpus snippet this module DOES choose to surface (the research
  digest a resolution's ``rationale`` carries, for audit readability) is
  built with :func:`render_research_digest`, which fences it through
  :func:`athenaeum.prompt_safety.fence_untrusted` exactly like every other
  untrusted-content call site in this repo — so a forged fence marker in a
  page body cannot break out of the digest and poison whatever surface
  later re-displays it (the pending-decisions queue, a future LLM prompt
  that embeds an item's rationale as :mod:`athenaeum.contradictions` already
  does for ordinary question text).

**decided_by stamping.** A ``question``-type answer's ``verdict`` text IS
what :func:`athenaeum.answers.resolve_by_id` writes into the resolved
block body — :mod:`athenaeum.decision_answers`'s question applier does not
thread ``note`` through to the store at all (see that module's
``_apply_question_answer``). So "records ``decided_by: agent:<ref>``"
(issue athenaeum#1995 AC2) is implemented by stamping it directly onto the
submitted ``verdict`` text via :func:`_stamp_decided_by` — the one place
the attribution is guaranteed to survive into the durable, human-readable
record, with zero changes to ``answers.py``/``decision_answers.py``.

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
primitives via that module's NEW, additive
:func:`athenaeum.calibration.sample_triage_decision` /
:func:`~athenaeum.calibration.triage_confirmed_wrong_count` /
:func:`~athenaeum.calibration.triage_confirmed_wrong_threshold_breached` —
see that module's "Agent-triage sampling" section for why this is additive
rather than a second mechanism.

Layering: L4 domain/pipeline module, a peer of :mod:`athenaeum.decisions` /
:mod:`athenaeum.decision_framing` / :mod:`athenaeum.decision_answers` /
:mod:`athenaeum.comparator` / :mod:`athenaeum.verdict_effects` (all L4) — may
import any of them plus L0-L3 (:mod:`athenaeum.calibration`,
:mod:`athenaeum.config`, :mod:`athenaeum.prompt_safety`, :mod:`athenaeum.
models`, :mod:`athenaeum.dimensions`) freely. Never imports
:mod:`athenaeum.cli` or any ``_cmd_*`` module (L5) — the CLI wiring
(``athenaeum triage``) lives in the sibling :mod:`athenaeum._cmd_triage`,
which imports THIS module, not the other way around.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from athenaeum.calibration import sample_triage_decision
from athenaeum.comparator import gate1_separator_relations
from athenaeum.decision_answers import write_decision_answer
from athenaeum.decision_framing import (
    ROUTING_AUTHORITY,
    ROUTING_COMPETENCE,
    answerable_as,
    routing_for,
    validate_answer,
)
from athenaeum.decisions import list_pending_decisions
from athenaeum.dimensions import DEFAULT_REGISTRY, Relation
from athenaeum.models import EntityIndex, parse_frontmatter
from athenaeum.prompt_safety import fence_untrusted
from athenaeum.verdict_effects import parse_coordinate_batch_members

log = logging.getLogger(__name__)

#: ``decided_by`` prefix this module ALWAYS applies — never derived from,
#: or overridable by, anything the research step reads (see module
#: docstring, "Prompt-injection hardening").
TRIAGE_DECIDED_BY_PREFIX = "agent:"

#: The only outbound decision ``type`` the default researcher is offered —
#: see module docstring, "Why only `question`". A FUTURE researcher plugged
#: in via :data:`TriageResearcher` is free to decline just as easily as the
#: default one; this constant only bounds what :func:`run_triage` ever
#: calls a researcher FOR, so a misconfigured/future researcher can never
#: be asked to adjudicate a merge or an audit sample by construction.
RESEARCHABLE_DECISION_TYPES: frozenset[str] = frozenset({"question"})

#: Actions one :class:`TriageOutcome` records.
ACTION_PREPARED = "prepared"  # authority (or unrecognized) routing — human only
ACTION_ABSORBED = "absorbed"  # competence item, researcher resolved, submitted
ACTION_ABSORBED_DRY_RUN = "absorbed-dry-run"  # as above, but dry_run=True
ACTION_ESCALATED = "escalated"  # competence item, no researcher resolved it
ACTION_REFUSED = "refused"  # researcher resolved it, but submission was refused


@dataclass(frozen=True)
class TriageResolution:
    """What a :data:`TriageResearcher` proposes for one competence item.

    ``verdict`` is the resolution text as a human would write it — it
    becomes (after :func:`_stamp_decided_by`) the literal ``verdict`` field
    submitted via :func:`submit_answer`, so it must already satisfy the
    item's ``response_schema`` (for ``question`` items, any non-empty
    string). ``ref`` is the free-form researcher identifier that becomes
    ``agent:<ref>`` in the stamped verdict (e.g. ``"coordinate-gate1"``);
    ``rationale`` is audit-readable context carried onto the calibration
    sample, never into the submitted verdict itself.
    """

    verdict: str
    ref: str
    rationale: str = ""


class TriageResearcher(Protocol):
    """Callable a researcher implements: resolve one item, or decline.

    ``item`` is one framed decision dict from
    :func:`athenaeum.decisions.list_pending_decisions` (already carries
    ``type``/``routing``/``payload``/``response_schema`` etc.); ``wiki_root``
    is where the corpus this item's payload may reference lives. Returning
    ``None`` means "this item needs a human" — the ONLY refusal signal;
    raising is a bug, not a decline (:func:`run_triage` does not catch
    researcher exceptions).
    """

    def __call__(self, item: dict[str, Any], wiki_root: Path) -> TriageResolution | None: ...


def render_research_digest(page_body: str, *, max_chars: int = 2000) -> str:
    """Fence a corpus-page-body snippet for inclusion in an audit rationale.

    The one place this module embeds raw corpus text anywhere outside its
    own internal comparison logic — truncate/defang/wrap via
    :func:`athenaeum.prompt_safety.fence_untrusted`, same as every other
    untrusted-content call site in this repo (module docstring,
    "Prompt-injection hardening"). Never fed back into a submitted
    ``verdict`` — only into a :class:`TriageResolution`'s ``rationale``,
    which :func:`run_triage` carries onto the calibration audit record
    (:func:`athenaeum.calibration.sample_triage_decision`'s ``reason``), a
    surface a human reads, not one that re-executes anything.
    """
    return fence_untrusted(page_body, tag="corpus_page", max_chars=max_chars)


def _resolvable_dimension(relation: str | None) -> bool:
    """Whether a Gate-1 relation is determinate enough to answer a coordinate request.

    Only ``EQUAL``/``DISJOINT`` are determinate ("same" / "different", which
    is exactly what a coordinate-request question asks). ``CONTAINS``/
    ``OVERLAPS`` are a partial relation that still needs a human to say
    which side is which; ``UNKNOWN`` or the dimension being absent entirely
    (not consulted — see :func:`athenaeum.comparator.gate1_separator_relations`)
    both mean the coordinate is still missing. A single non-resolvable named
    dimension defers the WHOLE item (see :func:`coordinate_request_researcher`).
    """
    return relation in (Relation.EQUAL, Relation.DISJOINT)


def coordinate_request_researcher(
    item: dict[str, Any], wiki_root: Path
) -> TriageResolution | None:
    """Default researcher: settle a comparator coordinate-request batch, if its
    named dimensions are now determinate on both sides' CURRENT frontmatter.

    See the module docstring's "The default researcher" section for the
    full rationale. Declines (returns ``None``) on anything that is not
    recognizably a coordinate-request ``question`` item, on a page id this
    corpus no longer has (renamed/retracted since the item was raised —
    answering against a page that may not even be the same one is exactly
    the kind of guess this module must not make), or on any named dimension
    that Gate 1 still cannot settle.
    """
    if item.get("type") != "question":
        return None
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None
    description = str(payload.get("description") or "")
    members = parse_coordinate_batch_members(description)
    if not members:
        return None

    index = EntityIndex(wiki_root)
    resolved_lines: list[str] = []
    for member in members:
        pair = str(member.get("pair") or "")
        dims = [str(d) for d in (member.get("dimensions") or [])]
        if not pair or "+" not in pair or not dims:
            return None
        id_a, id_b = pair.split("+", 1)
        path_a = index.get_by_uid(id_a)
        path_b = index.get_by_uid(id_b)
        if path_a is None or path_b is None:
            return None  # page vanished/renamed; do not guess

        try:
            meta_a, _ = parse_frontmatter(path_a.read_text(encoding="utf-8"))
            meta_b, _ = parse_frontmatter(path_b.read_text(encoding="utf-8"))
        except OSError:
            return None

        relations = gate1_separator_relations(DEFAULT_REGISTRY, meta_a, meta_b)
        for dim in dims:
            relation = relations.get(dim)
            if not _resolvable_dimension(relation):
                return None  # still underdetermined -> defer the WHOLE item
            name_a = str(meta_a.get("name") or id_a)
            name_b = str(meta_b.get("name") or id_b)
            if relation == Relation.EQUAL:
                resolved_lines.append(
                    f'{pair} ({dim}): "{name_a}" and "{name_b}" do NOT actually '
                    f"differ by {dim} — both carry the same coordinate."
                )
            else:  # Relation.DISJOINT
                resolved_lines.append(
                    f'{pair} ({dim}): "{name_a}" and "{name_b}" DO differ by '
                    f"{dim} — their current coordinates disagree."
                )

    verdict = " ".join(resolved_lines)
    return TriageResolution(
        verdict=verdict,
        ref="coordinate-gate1",
        rationale=(
            f"Resolved {len(members)} coordinate-request member(s) by re-running "
            "comparator Gate 1 against current frontmatter (no LLM judgement)."
        ),
    )


def _stamp_decided_by(verdict: str, decided_by: str) -> str:
    """Append a ``(decided_by: agent:<ref>)`` tag to a submitted verdict text.

    See module docstring, "decided_by stamping" — this is the durable
    record issue athenaeum#1995 AC2 asks for, since ``note`` is dropped on
    the question apply path.
    """
    return f"{verdict} (decided_by: {decided_by})"


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
    caller of the same three functions the CLI command itself calls.
    Never touches ``_cmd_decisions.py`` (a file sibling lanes athenaeum#1992/
    athenaeum#1993 are editing concurrently — see this issue's concurrency
    note).

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
    competence ``question`` items to *researcher*, submit what it resolves.

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
            # docstring, "Why only `question`".
            report.outcomes.append(
                TriageOutcome(decision_id, decision_type, routing, ACTION_ESCALATED)
            )
            continue

        resolution = active_researcher(item, wiki_root)
        if resolution is None:
            report.outcomes.append(
                TriageOutcome(decision_id, decision_type, routing, ACTION_ESCALATED)
            )
            continue

        decided_by = f"{TRIAGE_DECIDED_BY_PREFIX}{resolution.ref}"
        stamped_verdict = _stamp_decided_by(resolution.verdict, decided_by)

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
            answer={"verdict": stamped_verdict},
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
            verdict=stamped_verdict,
            reason=resolution.rationale,
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
    "render_research_digest",
    "coordinate_request_researcher",
    "submit_answer",
    "run_triage",
]
