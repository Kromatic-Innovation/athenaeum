# SPDX-License-Identifier: Apache-2.0
"""Unified "human decisions needed" view (issue athenaeum#401).

Athenaeum accumulates two separate queues that need a human:

- **questions** — contradiction-detector escalations in
  ``wiki/_pending_questions.md`` (surfaced by ``athenaeum questions``).
- **merges** — resolver merge proposals in ``wiki/_pending_merges.md``
  (previously reachable ONLY through the ``list_pending_merges`` MCP tool —
  no CLI, no briefing, so a real backlog could sit unseen for weeks).

This module builds ONE list that unifies both, each item tagged
``type: "question" | "merge"``, so any consumer (the ``decisions`` CLI, the
``merges`` CLI, a briefing sub-skill, or the ``list_pending_decisions`` MCP
tool) gets the whole queue in one call with a common shape. (Several more
item types have since joined the union — ``retraction``, ``audit``,
``quarantine``, ``proposed-rule``, and, as of issue athenaeum#1290,
``confirmation`` — see each ``*_to_decision`` function below; this
docstring's "two queues" framing predates them.)

``confirmation`` (issue athenaeum#1290) is an AGENT-raiseable item — an
agent that narrowed scope mid-build flags "implemented X without Y,
confirm?" through ``raise_decision`` (MCP) or
``athenaeum decisions raise-confirmation`` (CLI). Storage-wise it reuses the
``question`` queue's own file (``_pending_questions.md``) and block grammar
— see :func:`confirmation_to_decision` and
:func:`athenaeum.answers.raise_pending_question`'s ``kind="confirmation"``
path — so every existing question-queue mechanic (durability across a
session, ``resolve_question`` for closing it, the ``ingest-answers``
deferred-apply tick) already applies to it with zero special-casing; only
the unified VIEW's ``type`` tag and payload shape differ from a plain
question.

The hard requirement from the issue's live-triage comment: a merge item MUST
be expressed as **a question a human can actually answer**. A proposal shown
as ``merge_target_name=28e56467-…, cosine 0.84`` is undecidable; cosine
topic-similarity is not "should-merge" (0.92 wrongly fused *MCP Public Auth
Design* with *OAuth 2.1 Refresh-Token Rotation* — two different auth
systems). So every merge carries, per source page:

- the **human title** (frontmatter ``name:``, not the uuid-slug),
- a **one-line gist** (frontmatter ``description:`` or the first body line),

and a plainly-phrased ``summary`` question ("Merge these N pages into one? —
…") built from them, so a human can decide approve/reject without opening the
raw wiki files.

Layering: L4 domain/pipeline module. Aggregates OTHER L4 modules
(:mod:`athenaeum.answers`, :mod:`athenaeum.calibration`,
:mod:`athenaeum.pending_merges`, :mod:`athenaeum.quarantine`,
:mod:`athenaeum.retraction_cascade`, :mod:`athenaeum.rule_proposals`) into
one unified view and may import L3 services (``models``) freely. Factoring
rule:
this module only READS and re-shapes the three underlying queues into a
common item shape — it owns no queue's storage format or mutation path;
resolving/writing back to a given queue stays the owning module's job (e.g.
``answers.py`` for questions, ``merge.py``/``resolutions.py`` for merges).

Issue athenaeum#1992 narrows that factoring rule for exactly the three types
its own AC3 names. ``merge`` and ``question``/``confirmation`` are the two
legacy surfaces with a REAL CLI (``_cmd_merges.py``, ``_cmd_questions.py``,
now both flagged deprecated — :data:`athenaeum.config.
DEPRECATED_CLI_SURFACE_MESSAGES`); ``audit`` has no legacy CLI of its own
(it was always only reachable through this module's union — see
:func:`audit_to_decision`'s own docstring and the PII-hazard note in
:func:`migrate_legacy_queues`), but AC3 names it explicitly alongside the
other two, so it gets the same treatment. For these three, this module now
OWNS a persisted unified-schema store (:func:`migrate_legacy_queues`,
:data:`MIGRATED_QUEUE_FILENAME`) that :func:`list_pending_decisions` reads
back FROM (sync-then-read, on every call) rather than re-deriving
independently — this is the first time this module has written anything to
disk, and the store is the thing actually consumed, not an unread mirror.
Mutation of an individual item's disposition still belongs to the owning
module (:func:`athenaeum.pending_merges.resolve_merge`,
:func:`athenaeum.answers.resolve_by_id`,
:func:`athenaeum.calibration.record_audit_review`) unchanged — migrating/
re-syncing the store never calls any of them, by construction (see
:func:`migrate_legacy_queues`'s own docstring); an answer landing on a
legacy store is picked up on the VERY NEXT ``list_pending_decisions`` call,
since the sync is unconditional, not a cache. The remaining three types
(``retraction``, ``quarantine``, ``proposed-rule``) are ledger-derived,
ephemeral-by-design records outside AC3's named scope, so they stay pure
read-time projections, unchanged.

``_cmd_audit.py`` (the ``athenaeum audit`` CLI) is a DIFFERENT, unrelated
surface — page-freshness auditing (``last_audited``/``retirement_candidate``
written into each wiki page's own frontmatter), not a pending-decision
queue at all, with no disposition and no overlap with this module's own
``audit`` item type (calibration-sampled T1/T2 review). It is deliberately
NOT flagged deprecated by athenaeum#1992 — see the PR description for the
explicit reading of AC2 this rests on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from athenaeum.answers import PendingQuestion, parse_pending_questions
from athenaeum.atomic_io import atomic_write_text
from athenaeum.calibration import AUDIT_KIND, REVIEW_KIND, read_calibration_ledger
from athenaeum.decision_framing import frame_decision
from athenaeum.models import parse_frontmatter
from athenaeum.pagination import paginate
from athenaeum.pending_merges import PendingMerge, parse_pending_merges
from athenaeum.quarantine import list_pending_quarantine
from athenaeum.retraction_cascade import read_retraction_reviews
from athenaeum.rule_proposals import list_pending_rule_proposals

# Keys the resolver appends to a pending-question block tail (issue athenaeum#126),
# re-extracted verbatim when ``--with-proposal`` is requested. Kept in sync
# with :mod:`athenaeum._cmd_questions`.
_PROPOSAL_KEYS = (
    "**Proposed resolution**:",
    "**Confidence**:",
    "**Rationale**:",
    "**Source precedence**:",
)

# A leading ``<uid>-`` slug prefix on a wiki filename (hex uid, 6+ chars) —
# stripped when falling back to a filename-derived title.
_UID_PREFIX_RE = re.compile(r"^[0-9a-f]{6,}-(?P<rest>.+)$")

# Conventional auto-memory filename prefixes (see the frontmatter ``type``);
# stripped for a friendlier fallback title when there is no ``name:``.
_MEMORY_PREFIXES = ("feedback_", "project_", "reference_", "user_", "recall_")

# Cap for a one-line gist so a ``decisions list`` line stays readable.
_GIST_LIMIT = 160

# Fallback cap on rendered sources per merge item (issue athenaeum#431) used when a
# caller does not resolve its own value from config. Mirrors the code default
# in :func:`athenaeum.config.resolve_decisions_max_sources_per_merge` (kept as
# a plain literal here, not an import, to avoid a decisions->config->decisions
# import cycle risk; the two are covered by
# ``tests/test_bound_merge_read_path.py``'s config-parity check).
_DECISIONS_MAX_SOURCES_DEFAULT = 20

# Fallback per-item context cap (issue athenaeum#717) used when a caller does not
# resolve its own value from config. Mirrors the code default in
# :func:`athenaeum.config.resolve_decisions_max_item_context_tokens` (kept as a
# plain literal here for the same reason as the cap above — no
# decisions->config->decisions import risk; the two are covered by the
# config-parity check in ``tests/test_decision_framing.py``).
_DECISIONS_MAX_ITEM_CONTEXT_TOKENS_DEFAULT = 1500


def _one_line(text: str, *, limit: int = _GIST_LIMIT) -> str:
    """Collapse ``text`` to a single trimmed line, truncated to ``limit``."""
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def _extract_proposal_block(raw_block: str) -> str:
    """Pull the trailing 4-key proposal block out of a question ``raw_block``.

    Mirrors :func:`athenaeum._cmd_questions._extract_proposal_block` so the
    ``decisions`` view renders the same proposal text the ``questions`` view
    does. Returns ``""`` when the block carries no proposal.
    """
    proposal_lines: list[str] = []
    for line in raw_block.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(key) for key in _PROPOSAL_KEYS):
            proposal_lines.append(stripped)
    return "\n".join(proposal_lines)


def _fallback_title(source: str) -> str:
    """Derive a readable title from a source path when frontmatter is absent.

    Strips a leading ``<uid>-`` wiki prefix (e.g.
    ``34f82884-auth-authentication`` -> ``auth-authentication``) or a
    conventional auto-memory prefix (``user_alice_a`` -> ``alice_a``) so the
    fallback is never a bare uuid-slug.
    """
    stem = Path(source).stem
    m = _UID_PREFIX_RE.match(stem)
    if m:
        return m.group("rest")
    for prefix in _MEMORY_PREFIXES:
        if stem.startswith(prefix):
            return stem[len(prefix) :]
    return stem


def _first_body_line(body: str) -> str:
    """First non-blank, non-heading line of a memory body (the gist fallback)."""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return ""


def source_info(source: str) -> dict:
    """Resolve one merge source path to ``{path, title, gist}``.

    ``title`` prefers the frontmatter ``name:``; ``gist`` prefers the
    frontmatter ``description:`` and otherwise falls back to the first body
    line. When the file is missing or unreadable, ``title`` degrades to a
    filename-derived slug and ``gist`` is empty — the item is still an
    answerable question, just without the page's own words.
    """
    path = Path(source).expanduser()
    title = ""
    gist = ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    if text is not None:
        meta, body = parse_frontmatter(text)
        if isinstance(meta, dict):
            raw_name = meta.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                title = raw_name.strip()
            raw_desc = meta.get("description")
            if isinstance(raw_desc, str) and raw_desc.strip():
                gist = _one_line(raw_desc)
        if not gist:
            gist = _one_line(_first_body_line(body))
    if not title:
        title = _fallback_title(source)
    return {"path": source, "title": title, "gist": gist}


def _merge_question(target_name: str, source_infos: list[dict]) -> str:
    """Phrase a merge proposal as a plain, answerable question.

    e.g. ``Merge these 2 pages into "auth"? — "MCP Public Auth Design": <gist>;
    "OAuth 2.1 Refresh-Token Rotation": <gist>``. This is the field the
    live-triage comment on athenaeum#401 requires so cosine similarity alone can't
    mislead the human.
    """
    n = len(source_infos)
    parts = []
    for info in source_infos:
        gist = info["gist"]
        parts.append(f'"{info["title"]}": {gist}' if gist else f'"{info["title"]}"')
    detail = "; ".join(parts) if parts else "(no readable sources)"
    noun = "page" if n == 1 else "pages"
    return f'Merge these {n} {noun} into "{target_name}"? — {detail}'


def merge_to_rich(pm: PendingMerge) -> dict:
    """Convert a :class:`PendingMerge` to the ``merges`` CLI/MCP dict.

    Carries per-source ``title`` + ``gist`` and a phrased ``question`` so the
    output is decidable without opening the raw wiki files (issue athenaeum#401).

    Issue athenaeum#1170 code review: the phrased ``question`` uses
    ``pm.display_name or pm.merge_target_name`` — never bare
    ``merge_target_name`` — so a proposal whose ``merge_target_name`` is a
    machine-shaped value (e.g. :mod:`athenaeum.name_collisions` deliberately
    passes the canonical page's FILENAME STEM there, for correct
    fold-target derivation against an entity-template ``<uid>-<slug>.md``
    page) still asks a decidable, human-readable question — this module's
    own founding complaint (see module docstring) is exactly a proposal
    "shown as `merge_target_name=28e56467-…`" being undecidable.
    ``pm.display_name`` is ``""`` for every pre-athenaeum#1170 block, so this
    falls through to the unchanged ``merge_target_name`` behavior there.
    ``merge_target_name`` itself (the dict key below) is UNCHANGED — still
    the mechanical slug-derivation value, not the display one — so a
    caller resolving/approving by that field is unaffected.
    """
    source_infos = [source_info(s) for s in pm.sources]
    display = pm.display_name or pm.merge_target_name
    return {
        "id": pm.id,
        "merge_target_name": pm.merge_target_name,
        "created_at": pm.created_at,
        "confidence": pm.confidence,
        "rationale": pm.rationale,
        "question": _merge_question(display, source_infos),
        "sources": source_infos,
    }


def merge_to_decision(
    pm: PendingMerge, *, max_sources: int = _DECISIONS_MAX_SOURCES_DEFAULT
) -> dict:
    """Convert a :class:`PendingMerge` to a unified decision dict.

    Issue athenaeum#431 (read-path defense-in-depth): the decisions view previously
    rendered EVERY source of a merge with no cap, so a proposal with a very
    large source list could blow out a single decision item's payload. The
    rendered ``payload["sources"]`` list is capped to ``max_sources`` entries;
    when sources are omitted, ``payload["sources_omitted"]`` carries the
    accurate remainder count (``0`` when nothing was omitted, so a normal-
    sized merge's payload is unchanged from before this cap existed).
    ``max_sources <= 0`` disables the cap (all sources rendered).

    Args:
        pm: The pending merge to convert.
        max_sources: Cap on rendered sources — see
            :func:`athenaeum.config.resolve_decisions_max_sources_per_merge`
            for the config-resolved default (env > yaml > 20).
    """
    rich = merge_to_rich(pm)
    all_sources = rich["sources"]
    if max_sources > 0 and len(all_sources) > max_sources:
        shown_sources = all_sources[:max_sources]
        omitted = len(all_sources) - max_sources
    else:
        shown_sources = all_sources
        omitted = 0
    return {
        "type": "merge",
        "id": rich["id"],
        "created_at": rich["created_at"],
        "summary": rich["question"],
        "confidence": rich["confidence"],
        "payload": {
            "merge_target_name": rich["merge_target_name"],
            "rationale": rich["rationale"],
            "sources": shown_sources,
            "sources_omitted": omitted,
        },
    }


def confirmation_to_decision(pq: PendingQuestion) -> dict:
    """Convert a confirmation-kind :class:`PendingQuestion` to a unified decision dict.

    A ``type: "confirmation"`` item (issue athenaeum#1290): an agent narrowed
    scope mid-build and raised "implemented X without Y, confirm?" through
    ``raise_decision`` (MCP) or ``athenaeum decisions raise-confirmation``
    (CLI). Storage-wise this is STILL a block in ``_pending_questions.md`` —
    :func:`question_to_decision` dispatches here purely on
    ``pq.decision_kind == "confirmation"``; nothing about resolution differs
    (the existing ``resolve_question`` tool / ``athenaeum ingest-answers``
    tick closes it exactly like any other question block, since both
    operate on the raw block text, never on ``type``).

    ``summary`` is a plainly-phrased confirm question — mirroring
    :func:`_merge_question`'s "never show a raw score, phrase it as
    something a human can answer" rule — built from the structured fields
    rather than reusing ``pq.question`` verbatim, so a bare/generic question
    string (as a default-generated one can be) never has to carry the whole
    story alone.
    """
    summary = (
        f'{pq.raiser} narrowed scope on {pq.repo}#{pq.issue_ref}: implemented '
        f'"{pq.implemented_behavior}" instead of "{pq.alternative}" '
        f"(scope narrowed: {pq.narrowed_scope}) — confirm?"
    )
    return {
        "type": "confirmation",
        "id": pq.id,
        "created_at": pq.created_at,
        "summary": summary,
        "confidence": None,
        "payload": {
            "raiser": pq.raiser,
            "repo": pq.repo,
            "issue_ref": pq.issue_ref,
            "narrowed_scope": pq.narrowed_scope,
            "implemented_behavior": pq.implemented_behavior,
            "alternative": pq.alternative,
            "raised_at": pq.raised_at,
            "question": pq.question,
            "context": pq.description,
            "raised_by": pq.raised_by,
        },
    }


def question_to_decision(pq: PendingQuestion, *, with_proposal: bool = False) -> dict:
    """Convert a :class:`PendingQuestion` to a unified decision dict.

    ``payload["raised_by"]`` (issue athenaeum#912) is ``""`` for a detector-raised
    block (every ``tier4_escalate`` block, including every block that
    predates athenaeum#912) and ``"agent"`` for one filed via the
    ``raise_decision`` MCP tool (:func:`athenaeum.answers.raise_pending_question`)
    — the queue's provenance signal so an agent-raised item is never
    mistaken for a corpus-detected contradiction.

    Issue athenaeum#1290: a block whose ``decision_kind`` is
    ``"confirmation"`` is delegated to :func:`confirmation_to_decision`
    instead — a DIFFERENT ``type`` in the unified view (``"confirmation"``,
    not ``"question"``) with a richer, structured payload. This branch is the
    ONLY change athenaeum#1290 makes here; an ordinary question's returned
    dict (``decision_kind == "question"``, true for every pre-athenaeum#1290
    block and every plain ``raise_decision`` call) is byte-for-byte
    unchanged.
    """
    if pq.decision_kind == "confirmation":
        return confirmation_to_decision(pq)
    payload: dict = {
        "entity": pq.entity,
        "source": pq.source,
        "question": pq.question,
        "conflict_type": pq.conflict_type,
        "description": pq.description,
        "raised_by": pq.raised_by,
    }
    if with_proposal:
        payload["proposal"] = _extract_proposal_block(pq.raw_block)
    return {
        "type": "question",
        "id": pq.id,
        "created_at": pq.created_at,
        "summary": pq.question,
        "confidence": None,
        "payload": payload,
    }


def retraction_to_decision(rec: dict) -> dict:
    """Convert a retraction-cascade review record to a unified decision dict.

    A ``type: "retraction"`` item (issue athenaeum#435): a supporting source of a
    completed merge was retracted, so the merge is flagged for a human to
    decide whether it still holds. The merge is never auto-unmerged — this is
    purely a "please look" signal. ``confidence`` is ``None`` (there is no
    similarity score behind a retraction; it is a hard provenance fact).
    """
    slug = rec.get("canonical_slug") or "(unknown page)"
    ref = rec.get("retracted_ref", "")
    reason = rec.get("reason", "")
    reason_tail = f" (retraction reason: {_one_line(reason)})" if reason else ""
    summary = (
        f'A retracted source "{ref}" supported the merge into "{slug}" — '
        f"review whether that merge still holds.{reason_tail}"
    )
    return {
        "type": "retraction",
        "id": rec.get("id"),
        "created_at": rec.get("created_at"),
        "summary": summary,
        "confidence": None,
        "payload": {
            "merge_id": rec.get("merge_id"),
            "canonical_slug": rec.get("canonical_slug"),
            "retracted_ref": ref,
            "reason": reason,
        },
    }


def audit_to_decision(rec: dict) -> dict:
    """Convert a sampled tier-audit item to a unified decision dict (issue athenaeum#438).

    A ``type: "audit"`` item — distinguishable from an ordinary escalation —
    surfaces a randomly-sampled T1 reject or T2 approval for human
    calibration review. Confirming it leaves the tier's original decision
    untouched; overturning it records a calibration signal (it does not
    re-execute the merge). ``confidence`` is ``None``.
    """
    tier = rec.get("tier", "")
    verdict = rec.get("verdict", "")
    proposal_id = rec.get("proposal_id", "")
    reason = rec.get("reason", "")
    reason_tail = f" (tier reason: {_one_line(reason)})" if reason else ""
    summary = (
        f"Calibration audit: tier {tier} returned {verdict!r} on proposal "
        f"{proposal_id} — confirm the verdict or overturn it.{reason_tail}"
    )
    return {
        "type": "audit",
        "id": rec.get("id"),
        "created_at": rec.get("created_at"),
        "summary": summary,
        "confidence": None,
        "payload": {
            "tier": tier,
            "verdict": verdict,
            "proposal_id": proposal_id,
            "reason": reason,
            "sample_rate": rec.get("sample_rate"),
        },
    }


def quarantine_to_decision(rec: dict) -> dict:
    """Convert a quarantine ledger record to a unified decision dict (issue athenaeum#898).

    A ``type: "quarantine"`` item: a raw intake file was moved out of the
    discovery set after exceeding one of its per-file bounds (byte size, LLM
    call count, or wall-clock — see ``rec["bound"]``) on ``rec["violations"]``
    consecutive runs. ``confidence`` is ``None`` — there is no similarity
    score behind a resource-bound trip, it is a hard measured fact, mirroring
    :func:`retraction_to_decision` / :func:`audit_to_decision`. Reviewing
    this item (:func:`athenaeum.quarantine.release_quarantine`) is the ONLY
    way to return the file to the discovery set (AC 6) — there is no
    automatic un-quarantine path.
    """
    ref = rec.get("ref", "")
    bound = rec.get("bound", "")
    detail = rec.get("detail", "")
    violations = rec.get("violations", 0)
    detail_tail = f" ({_one_line(detail)})" if detail else ""
    summary = (
        f'Raw intake file "{ref}" was quarantined after exceeding its {bound} '
        f"bound on {violations} consecutive run(s){detail_tail} — release it "
        "to return it to the discovery set, or leave it quarantined."
    )
    return {
        "type": "quarantine",
        "id": rec.get("id"),
        "created_at": rec.get("created_at"),
        "summary": summary,
        "confidence": None,
        "payload": {
            "ref": ref,
            "source": rec.get("source"),
            "bound": bound,
            "detail": detail,
            "violations": violations,
            "quarantine_path": rec.get("quarantine_path"),
            "original_path": rec.get("original_path"),
        },
    }


def proposed_rule_to_decision(rec: dict) -> dict:
    """Convert a rule-proposal ledger record to a unified decision dict (issue athenaeum#905).

    A ``type: "proposed-rule"`` item: the librarian drafted a candidate
    shape rule from ``rec["count"]`` records of one ``(source,
    key_fingerprint)`` shape that the deterministic shape-rules pass
    deferred to the reasoning tiers (``tier is None`` in
    ``_shape_rule_dispositions.jsonl`` -- see
    :mod:`athenaeum.rule_proposals` for why this, not a literal tier 2/3,
    is AC1's faithful reading). ``confidence`` is ``None`` -- there is no
    similarity score behind a drafted rule, mirroring
    :func:`quarantine_to_decision` / :func:`audit_to_decision`. Resolving
    this item is :func:`athenaeum.rule_proposals.approve_rule_proposal`
    (writes the rule into the rules directory in OBSERVE mode) or
    :func:`athenaeum.rule_proposals.reject_rule_proposal` (permanently
    suppresses this shape).
    """
    source = rec.get("source", "")
    key_fingerprint = rec.get("key_fingerprint", "")
    count = rec.get("count", 0)
    window_days = rec.get("window_days")
    impact = rec.get("projected_impact", "")
    summary = (
        f'The librarian drafted a candidate shape rule from {count} record(s) '
        f'from "{source}" (shape {key_fingerprint}) deferred to the reasoning '
        f'tiers over the last {window_days} day(s) -- approve to write it into '
        f'the rules directory in OBSERVE mode, or reject to suppress this '
        f'shape. Projected impact: {impact}'
    )
    return {
        "type": "proposed-rule",
        "id": rec.get("id"),
        "created_at": rec.get("created_at"),
        "summary": summary,
        "confidence": None,
        "payload": {
            "source": source,
            "key_fingerprint": key_fingerprint,
            "count": count,
            "window_days": window_days,
            "threshold": rec.get("threshold"),
            "rule_name": rec.get("rule_name"),
            "rule_yaml": rec.get("rule_yaml", ""),
            "projected_impact": impact,
            "rationale": rec.get("rationale", ""),
            "exemplar_refs": rec.get("exemplar_refs", []),
            "tier3_linked": rec.get("tier3_linked", False),
            "tier3_note": rec.get("tier3_note", ""),
        },
    }


def list_pending_merges_rich(merges_path: Path) -> list[dict]:
    """Unresolved merges as decidable dicts (title + gist + question)."""
    return [
        merge_to_rich(pm)
        for pm in parse_pending_merges(merges_path)
        if not pm.resolved
    ]


def list_pending_decisions(
    wiki_root: Path,
    *,
    with_proposal: bool = False,
    max_sources_per_merge: int = _DECISIONS_MAX_SOURCES_DEFAULT,
    max_item_context_tokens: int = _DECISIONS_MAX_ITEM_CONTEXT_TOKENS_DEFAULT,
    caller_audience: set[str] | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> list[dict]:
    """Unified list of pending questions + merges, oldest first.

    ``wiki_root`` is the directory holding ``_pending_questions.md`` and
    ``_pending_merges.md`` (i.e. ``<knowledge>/wiki``). Items are sorted by
    ``created_at`` ascending so the oldest decision — the one most at risk of
    rotting unseen — leads the list.

    ``max_sources_per_merge`` (issue athenaeum#431) caps how many sources are rendered
    per merge item — see :func:`merge_to_decision` and
    :func:`athenaeum.config.resolve_decisions_max_sources_per_merge` for the
    config-resolved default (env > yaml > 20). Callers that already loaded
    config (the CLI, the MCP tool) should resolve it there and pass it
    through; this default keeps direct callers working unchanged.

    Issue athenaeum#538 (audience scoping): a restricted ``caller_audience`` (non-owner)
    sees only the decisions whose underlying pages it is authorized to read —
    the same fail-closed predicate ``recall`` applies. A question is withheld
    unless its ``source`` memory authorizes; a merge unless EVERY source page
    authorizes (checked over the FULL source set, before the athenaeum#431 render cap);
    and ``retraction`` / ``audit`` / ``quarantine`` / ``proposed-rule`` items —
    which reference pages, raw-intake files, or a shape's exemplar refs by
    slug/proposal-id/ref rather than a readable compiled-wiki source path —
    are withheld wholesale from a restricted caller (adjudicating them is
    owner-only, mirroring the write-side guard on ``review_audit_item``).
    Owner (``None``, the default) sees everything, preserving existing
    behavior.

    Issue athenaeum#1992: for the three types with a real persisted
    disposition (``merge``, ``question``/``confirmation``, ``audit``), this
    function is no longer a from-scratch re-derivation — it SYNCS the
    unified-schema store (:func:`migrate_legacy_queues`, using this call's
    own ``with_proposal``/``max_sources_per_merge``) and then reads the
    ``item`` shape straight back from that store (:func:`load_migrated_queue`)
    rather than re-building it inline from ``parse_pending_merges`` /
    ``parse_pending_questions`` / the calibration ledger a second time. The
    sync is unconditional on every call — this is a real, idempotent,
    read-only-w.r.t.-the-legacy-stores rebuild (same cost as the old inline
    re-derivation plus one small JSONL write), not a cached snapshot that
    could go stale between an answer landing and the next list call.
    ``retraction`` / ``quarantine`` / ``proposed-rule`` stay pure
    ledger-derived read-time projections, unchanged — AC3's named scope is
    exactly the three migrated types.

    Issue athenaeum#1431: ``offset``/``limit`` page over the FINAL, unified,
    oldest-first list — the slice is applied AFTER the ``decisions.sort(...)``
    call below, not threaded into any of the per-kind sub-lists (in
    particular, this function's own internal question filtering above is
    never limited). Paging any one sub-list independently would break the
    queue-wide oldest-first ordering contract: questions, merges, retractions,
    audits, quarantine items, and proposed rules would each restart their own
    page boundary instead of interleaving by age. Because the slice happens
    strictly after the sort, the oldest-first contract holds ACROSS pages —
    page N's last item is always older than page N+1's first. ``offset``
    defaults to ``0`` and is clamped to ``0`` if negative. ``limit`` defaults
    to ``None`` (unbounded, preserving existing behavior for every direct
    caller — notably the ``athenaeum decisions`` CLI); ``0`` or a negative
    ``limit`` is likewise treated as unbounded rather than yielding an empty
    page. For a bounded call that also reports a total count, next-offset,
    and the effective limit, see :func:`list_pending_decisions_page`.
    """
    from athenaeum.models import all_sources_authorized, is_page_authorized_at

    knowledge_root = wiki_root.parent

    # Issue athenaeum#1992: sync the unified store to the CURRENT legacy-file
    # / calibration-ledger state (this call's own with_proposal /
    # max_sources_per_merge shape it), then read every merge / question /
    # confirmation / audit item back FROM that store — it is the actual
    # queue for these three types, not a second, independently-derived
    # presentation of the same underlying data.
    migrate_legacy_queues(
        wiki_root,
        with_proposal=with_proposal,
        max_sources_per_merge=max_sources_per_merge,
    )
    decisions: list[dict] = []
    for rec in load_migrated_queue(wiki_root):
        if rec.get("disposition") != "pending":
            continue
        rtype = rec.get("type")
        if rtype == "merge":
            if not all_sources_authorized(
                rec.get("sources", []), caller_audience, base=knowledge_root
            ):
                continue
        elif rtype in ("question", "confirmation"):
            if not is_page_authorized_at(
                rec.get("source", ""), caller_audience, base=knowledge_root
            ):
                continue
        elif rtype == "audit":
            # Owner-only for a restricted caller, same as retraction/
            # quarantine/proposed-rule below (no readable source-page path
            # to authorize against, issue athenaeum#538).
            if caller_audience is not None:
                continue
        else:  # pragma: no cover - defensive; the store only holds the above
            continue
        decisions.append(rec["item"])

    if caller_audience is None:
        # Retraction/quarantine/proposed-rule items are owner-only for a
        # restricted caller (no readable source-page path to authorize
        # against, athenaeum#538). Ledger-derived, ephemeral by design, no
        # comparable legacy CLI to deprecate — outside athenaeum#1992's
        # named migration scope ("pending merges, pending questions, audit
        # items"), so they stay pure read-time projections, unchanged.
        decisions += [
            retraction_to_decision(rec) for rec in read_retraction_reviews(wiki_root)
        ]
        # Issue athenaeum#898: quarantined raw-intake files awaiting an operator's
        # release/leave-quarantined decision (AC 4/5).
        decisions += [
            quarantine_to_decision(rec) for rec in list_pending_quarantine(wiki_root)
        ]
        # Issue athenaeum#905: librarian-drafted rule proposals awaiting an
        # operator's approve/reject decision.
        decisions += [
            proposed_rule_to_decision(rec) for rec in list_pending_rule_proposals(wiki_root)
        ]
    # Issue athenaeum#717: framing is applied to the WHOLE union, at the one
    # point every item passes through, so no item can enter the queue unframed
    # or carrying an over-cap context bundle. Because this is downstream of
    # every per-type builder, it is also the correct seam for the cap to be
    # checked AFTER any future batching rather than before it.
    decisions = [
        frame_decision(d, max_context_tokens=max_item_context_tokens) for d in decisions
    ]
    decisions.sort(key=lambda d: d["created_at"] or "")

    return paginate(decisions, offset=offset, limit=limit)["items"]


def list_pending_decisions_page(
    wiki_root: Path,
    *,
    with_proposal: bool = False,
    max_sources_per_merge: int = _DECISIONS_MAX_SOURCES_DEFAULT,
    max_item_context_tokens: int = _DECISIONS_MAX_ITEM_CONTEXT_TOKENS_DEFAULT,
    caller_audience: set[str] | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> dict:
    """Bounded, self-describing page of :func:`list_pending_decisions`.

    Issue athenaeum#1431: the MCP boundary needs enough information for a
    caller to fetch the rest of an unbounded list without either transport
    truncating it — a plain JSON array can't carry a total count or a
    next-offset. This wraps :func:`list_pending_decisions` in an envelope::

        {
            "items": [...],       # this page's decisions, oldest first
            "total": <int>,       # the FULL unpaginated count
            "offset": <int>,      # the clamped offset actually applied
            "limit": <int|None>,  # the effective limit actually applied
            "next_offset": <int|None>,  # offset for the next page, or None
        }

    ``next_offset`` is ``offset + len(items)`` when more items remain past
    this page, otherwise ``None`` (the caller has reached the end).

    The full sorted list is built ONCE, with no limit, so ``total`` is exact
    and the slice below is taken from it — there is no double-fetch or
    separate counting pass. This is safe to do unconditionally: the issue's
    own measurement found that BUILDING the unified list is fast (~0.2s
    against the corpus that motivated this issue); the failure this issue
    fixes is in SERIALIZING an unbounded list over the MCP stdio transport,
    which is exactly what this envelope bounds.
    """
    all_decisions = list_pending_decisions(
        wiki_root,
        with_proposal=with_proposal,
        max_sources_per_merge=max_sources_per_merge,
        max_item_context_tokens=max_item_context_tokens,
        caller_audience=caller_audience,
    )
    return paginate(all_decisions, offset=offset, limit=limit)


def age_days(created_at: str, *, today: date | None = None) -> int | None:
    """Whole days between ``created_at`` (an ISO date/datetime) and ``today``.

    Returns ``None`` when ``created_at`` can't be parsed. Only the date
    portion is used, so a full ``YYYY-MM-DDThh:mm:ssZ`` timestamp works too.

    Delegates to :func:`athenaeum.metrics.days_since` (issue athenaeum#1990)
    — the shared L3 implementation :mod:`athenaeum.decision_budget` also
    uses, so the two never fork the same day-diff logic, and so this module
    does not need to import that one (which would close an import cycle:
    ``decisions`` already reaches ``quarantine``, and ``decision_budget``
    reaches both).
    """
    from athenaeum.metrics import days_since

    return days_since(created_at, today=today)


def decision_time_minutes(raised_at: str, answered_at: str) -> int | None:
    """Whole minutes between ``raised_at`` and ``answered_at``.

    Sibling to :func:`age_days`: the queue's decision-time metric (issue
    athenaeum#1990), consumed by :mod:`athenaeum.decision_budget`.
    ``raised_at`` is an item's own raise timestamp —
    ``PendingQuestion.raised_at`` when the item was agent-raised (issue
    athenaeum#912), otherwise its ``created_at`` (every detector-raised
    question, and every merge / audit / quarantine / proposed-rule record).
    ``answered_at`` is the matching resolution timestamp each per-type
    resolver stamps: ``PendingQuestion.answered_at``,
    ``PendingMerge.answered_at``, or the ``answered_at`` key on the
    audit-review, quarantine-release, and rule-proposal-disposition ledger
    records.

    Returns ``None`` — never a fabricated duration — when either timestamp
    is missing or unparseable, or when the pair resolves to a negative
    duration (clock skew or a malformed record, not a real decision time).
    Delegates to :func:`athenaeum.metrics.minutes_between`; see
    :func:`age_days` above for why the delegation (not a local
    implementation importing this module from ``decision_budget``) matters.
    """
    from athenaeum.metrics import minutes_between

    return minutes_between(raised_at, answered_at)


#: Issue athenaeum#1992: filename of the persisted unified-schema mirror of
#: every legacy merge + question record, written by
#: :func:`migrate_legacy_queues`. Lives directly under ``wiki_root`` (the
#: same directory as ``_pending_merges.md`` / ``_pending_questions.md``).
#: One JSON object per line, sorted by ``id`` so a re-run with no legacy
#: change produces a byte-identical file.
MIGRATED_QUEUE_FILENAME = "_decisions_queue.jsonl"


@dataclass(frozen=True)
class MigrationReport:
    """Outcome of one :func:`migrate_legacy_queues` run (issue athenaeum#1992).

    ``by_id`` maps every migrated record's PRESERVED legacy id to the
    disposition read from the legacy store/ledger at migration time
    (``"pending"``, ``"approved"``, ``"rejected"``, ``"answered"``,
    ``"confirmed"``, or ``"overturned"``) — the exact shape an id-set /
    disposition-drift comparison needs before and after a migration run
    (the issue's own test AC: "verified by id-set comparison, not by count
    alone").
    """

    path: Path
    by_id: dict[str, str]
    merge_count: int
    question_count: int
    audit_count: int

    @property
    def ids(self) -> set[str]:
        """The full set of migrated ids (merge + question + audit combined)."""
        return set(self.by_id)


def _merge_disposition(pm: PendingMerge) -> str:
    """The migration-stable disposition of one legacy merge proposal.

    A straight read of ``pm.resolved``/``pm.decision`` — the same two
    fields :func:`athenaeum.pending_merges.resolve_merge` writes and
    ``_cmd_merges.py`` renders. Never invents a value: an unresolved record
    is ``"pending"``; a resolved record whose ``decision`` is neither
    ``"approve"`` nor ``"reject"`` (should not happen, but never silently
    misreported as either) comes back as ``"resolved"``.
    """
    if not pm.resolved:
        return "pending"
    if pm.decision == "approve":
        return "approved"
    if pm.decision == "reject":
        return "rejected"
    return "resolved"


def _question_disposition(pq: PendingQuestion) -> str:
    """The migration-stable disposition of one legacy pending question."""
    return "answered" if pq.answered else "pending"


def _audit_disposition(review: dict | None) -> str:
    """The migration-stable disposition of one calibration-ledger audit item.

    A straight read of whether a ``REVIEW_KIND`` record exists for this
    audit id, and if so, its own ``overturned`` field — the same field
    :func:`athenaeum.calibration.record_audit_review` writes. Never
    invents a value: no review record is ``"pending"``; a review's
    ``overturned=True``/``False`` becomes ``"overturned"``/``"confirmed"``.
    """
    if review is None:
        return "pending"
    return "overturned" if review.get("overturned") else "confirmed"


def migrate_legacy_queues(
    wiki_root: Path,
    *,
    with_proposal: bool = False,
    max_sources_per_merge: int = _DECISIONS_MAX_SOURCES_DEFAULT,
) -> MigrationReport:
    """Migrate every legacy merge + question + audit record into the unified schema.

    Issue athenaeum#717's AC group 1 (slice athenaeum#1992) requires this
    module to stop being a read-only view and become the actual queue for
    the three legacy surfaces named in AC3 — pending merges, pending
    questions/confirmations, and calibration-sampled audit items. This is
    the function that makes that true, and :func:`list_pending_decisions`
    now calls it (with its own ``with_proposal``/``max_sources_per_merge``)
    on every listing rather than re-deriving the same shapes independently
    — the persisted file this writes is what gets read back, not a second,
    unread presentation of the same data.

    Read-only with respect to every LEGACY store: nothing here ever calls
    :func:`athenaeum.pending_merges.resolve_merge`,
    :func:`athenaeum.answers.resolve_by_id`,
    :func:`athenaeum.calibration.record_audit_review`, or any other
    disposition mutator. It only *reads* ``_pending_merges.md`` /
    ``_pending_questions.md`` / the calibration ledger — every record,
    resolved AND unresolved/reviewed AND unreviewed — and writes the full
    union into :data:`MIGRATED_QUEUE_FILENAME` under ``wiki_root``,
    atomically (:func:`athenaeum.atomic_io.atomic_write_text`).

    Identity preservation: every record's ``id`` is the SAME id its owning
    store already assigned it (:class:`PendingMerge`'s content-addressed
    id / :class:`PendingQuestion`'s block-derived id /
    :func:`athenaeum.calibration.audit_item_id`'s ``(tier, proposal_id)``
    hash) — nothing is re-minted, so an id-set comparison against the
    legacy stores' own ids is exact, and running this twice with no legacy
    change produces a byte-identical file (idempotent; cheap enough to run
    on every list call — see :func:`list_pending_decisions` — as well as
    the standalone ``athenaeum decisions migrate`` CLI mode). Disposition
    preservation: :func:`_merge_disposition` / :func:`_question_disposition`
    / :func:`_audit_disposition` read each record's own fields with zero
    transformation, so migrating (or re-migrating any number of times) can
    never flip a disposition — the two PII-hazard proposals the issue's AC
    names stay exactly as unresolved/resolved as the legacy store already
    has them, because nothing in this function ever writes to that store.

    Returns a :class:`MigrationReport` (rather than ``None``) so a caller —
    the ``athenaeum decisions migrate`` CLI, :func:`list_pending_decisions`,
    or a test building a fixture legacy store — can inspect exactly what
    migrated without re-reading the written file.
    """
    merges_path = wiki_root / "_pending_merges.md"
    questions_path = wiki_root / "_pending_questions.md"

    by_id: dict[str, str] = {}
    records: list[dict] = []

    for pm in parse_pending_merges(merges_path):
        disposition = _merge_disposition(pm)
        by_id[pm.id] = disposition
        records.append(
            {
                "id": pm.id,
                "type": "merge",
                "disposition": disposition,
                "resolved": pm.resolved,
                "decision": pm.decision,
                "created_at": pm.created_at,
                "answered_at": pm.answered_at,
                "sources": list(pm.sources),
                "item": merge_to_decision(pm, max_sources=max_sources_per_merge),
            }
        )
    merge_count = len(records)

    for pq in parse_pending_questions(questions_path):
        disposition = _question_disposition(pq)
        by_id[pq.id] = disposition
        is_confirmation = pq.decision_kind == "confirmation"
        item = (
            confirmation_to_decision(pq)
            if is_confirmation
            else question_to_decision(pq, with_proposal=with_proposal)
        )
        records.append(
            {
                "id": pq.id,
                "type": "confirmation" if is_confirmation else "question",
                "disposition": disposition,
                "resolved": pq.answered,
                "decision": "",
                "created_at": pq.created_at,
                "answered_at": pq.answered_at,
                "source": pq.source,
                "item": item,
            }
        )
    question_count = len(records) - merge_count

    ledger_records = read_calibration_ledger(wiki_root)
    reviews_by_id = {
        str(r.get("id")): r for r in ledger_records if r.get("kind") == REVIEW_KIND
    }
    audit_records = [r for r in ledger_records if r.get("kind") == AUDIT_KIND]
    for rec in audit_records:
        audit_id = str(rec.get("id"))
        review = reviews_by_id.get(audit_id)
        disposition = _audit_disposition(review)
        by_id[audit_id] = disposition
        records.append(
            {
                "id": audit_id,
                "type": "audit",
                "disposition": disposition,
                "resolved": review is not None,
                "decision": "",
                "created_at": rec.get("created_at"),
                "answered_at": (review or {}).get("answered_at", ""),
                "item": audit_to_decision(rec),
            }
        )
    audit_count = len(audit_records)

    records.sort(key=lambda r: r["id"])
    out_path = wiki_root / MIGRATED_QUEUE_FILENAME
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
    atomic_write_text(out_path, text)

    return MigrationReport(
        path=out_path,
        by_id=by_id,
        merge_count=merge_count,
        question_count=question_count,
        audit_count=audit_count,
    )


def load_migrated_queue(wiki_root: Path) -> list[dict]:
    """Read back :data:`MIGRATED_QUEUE_FILENAME`, or ``[]`` if never migrated."""
    path = wiki_root / MIGRATED_QUEUE_FILENAME
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            records.append(json.loads(stripped))
    return records
