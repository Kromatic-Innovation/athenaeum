# SPDX-License-Identifier: Apache-2.0
"""Unified "human decisions needed" view (issue #401).

Athenaeum accumulates two separate queues that need a human:

- **questions** — contradiction-detector escalations in
  ``wiki/_pending_questions.md`` (surfaced by ``athenaeum questions``).
- **merges** — resolver merge proposals in ``wiki/_pending_merges.md``
  (previously reachable ONLY through the ``list_pending_merges`` MCP tool —
  no CLI, no briefing, so a real backlog could sit unseen for weeks).

This module builds ONE list that unifies both, each item tagged
``type: "question" | "merge"``, so any consumer (the ``decisions`` CLI, the
``merges`` CLI, a briefing sub-skill, or the ``list_pending_decisions`` MCP
tool) gets the whole queue in one call with a common shape.

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
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from athenaeum.answers import PendingQuestion, parse_pending_questions
from athenaeum.calibration import list_pending_audit
from athenaeum.models import parse_frontmatter
from athenaeum.pending_merges import PendingMerge, parse_pending_merges
from athenaeum.retraction_cascade import read_retraction_reviews

# Keys the resolver appends to a pending-question block tail (issue #126),
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

# Fallback cap on rendered sources per merge item (issue #431) used when a
# caller does not resolve its own value from config. Mirrors the code default
# in :func:`athenaeum.config.resolve_decisions_max_sources_per_merge` (kept as
# a plain literal here, not an import, to avoid a decisions->config->decisions
# import cycle risk; the two are covered by
# ``tests/test_bound_merge_read_path.py``'s config-parity check).
_DECISIONS_MAX_SOURCES_DEFAULT = 20


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
    live-triage comment on #401 requires so cosine similarity alone can't
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
    output is decidable without opening the raw wiki files (issue #401).
    """
    source_infos = [source_info(s) for s in pm.sources]
    return {
        "id": pm.id,
        "merge_target_name": pm.merge_target_name,
        "created_at": pm.created_at,
        "confidence": pm.confidence,
        "rationale": pm.rationale,
        "question": _merge_question(pm.merge_target_name, source_infos),
        "sources": source_infos,
    }


def merge_to_decision(
    pm: PendingMerge, *, max_sources: int = _DECISIONS_MAX_SOURCES_DEFAULT
) -> dict:
    """Convert a :class:`PendingMerge` to a unified decision dict.

    Issue #431 (read-path defense-in-depth): the decisions view previously
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


def question_to_decision(pq: PendingQuestion, *, with_proposal: bool = False) -> dict:
    """Convert a :class:`PendingQuestion` to a unified decision dict."""
    payload: dict = {
        "entity": pq.entity,
        "source": pq.source,
        "question": pq.question,
        "conflict_type": pq.conflict_type,
        "description": pq.description,
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

    A ``type: "retraction"`` item (issue #435): a supporting source of a
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
    """Convert a sampled tier-audit item to a unified decision dict (issue #438).

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
) -> list[dict]:
    """Unified list of pending questions + merges, oldest first.

    ``wiki_root`` is the directory holding ``_pending_questions.md`` and
    ``_pending_merges.md`` (i.e. ``<knowledge>/wiki``). Items are sorted by
    ``created_at`` ascending so the oldest decision — the one most at risk of
    rotting unseen — leads the list.

    ``max_sources_per_merge`` (issue #431) caps how many sources are rendered
    per merge item — see :func:`merge_to_decision` and
    :func:`athenaeum.config.resolve_decisions_max_sources_per_merge` for the
    config-resolved default (env > yaml > 20). Callers that already loaded
    config (the CLI, the MCP tool) should resolve it there and pass it
    through; this default keeps direct callers working unchanged.
    """
    questions = [
        pq
        for pq in parse_pending_questions(wiki_root / "_pending_questions.md")
        if not pq.answered
    ]
    decisions = [question_to_decision(pq, with_proposal=with_proposal) for pq in questions]
    decisions += [
        merge_to_decision(pm, max_sources=max_sources_per_merge)
        for pm in parse_pending_merges(wiki_root / "_pending_merges.md")
        if not pm.resolved
    ]
    decisions += [
        retraction_to_decision(rec) for rec in read_retraction_reviews(wiki_root)
    ]
    decisions += [audit_to_decision(rec) for rec in list_pending_audit(wiki_root)]
    decisions.sort(key=lambda d: d["created_at"] or "")
    return decisions


def age_days(created_at: str, *, today: date | None = None) -> int | None:
    """Whole days between ``created_at`` (an ISO date/datetime) and ``today``.

    Returns ``None`` when ``created_at`` can't be parsed. Only the date
    portion is used, so a full ``YYYY-MM-DDThh:mm:ssZ`` timestamp works too.
    """
    if not created_at:
        return None
    day_part = created_at.strip()[:10]
    try:
        created = date.fromisoformat(day_part)
    except ValueError:
        return None
    ref = today or date.today()
    return (ref - created).days
