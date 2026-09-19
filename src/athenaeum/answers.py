# SPDX-License-Identifier: Apache-2.0
"""Pending-question answer ingestion.

``_pending_questions.md`` is populated by ``tier4_escalate`` whenever Tier 3
surfaces an ambiguity or a principled contradiction. Each block starts with
a header like::

    ## [2026-04-20] Entity: "Acme Corp" (from sessions/20240406T120000Z-aabb.md)
    - [ ] Is Acme still Series A after the 2026 recapitalisation?
    **Conflict type**: principled
    **Description**: Prior wiki says Series A; new raw file implies Series B.

The user resolves a question by either:

1. Editing the file and flipping ``- [ ]`` to ``- [x]`` (typing answer text
   below the checkbox on subsequent lines).
2. Calling the MCP tool :func:`resolve_question` which does the same edit.

Running ``athenaeum ingest-answers`` then:

- Writes each ``[x]`` block as a raw intake file under
  ``raw/answers/{ISO-TS}-{entity-slug}.md`` with frontmatter naming the
  original source.
- Appends the processed block to ``_pending_questions_archive.md``
  (newest-first, append-only, never deleted) — UNLESS the block's class
  expects a write-back that could not happen (see below), in which case it
  is held instead.
- Leaves unanswered ``[ ]`` blocks in place.

Re-running with no new ``[x]`` blocks is a no-op. Malformed blocks are
skipped with a warning on stderr and a log entry; the rest of the file is
still processed.

Issue athenaeum#1804 — held write-backs: archiving an answered block whose
authorized write-back could not happen (no source ref resolved) destroys
the signal that something needs fixing. A detector-raised block (not
agent-raised, not a field-correction/schema-amendment — the latter's source
ref is unresolvable BY DESIGN, its batch retired by ``git rm`` once the
question is recorded) with a non-empty answer and zero resolved source refs
is instead left in ``_pending_questions.md`` with a
``**Write-back**: held — ...`` line naming the unresolved refs; the
provenance file under ``raw/answers/`` is still written once. A later run
either completes the write-back once the source resolves, or archives
without ever calling it once a human edits ``held`` to ``waived`` on that
line.

Defensive recovery: a block missing its ``- [ ]`` checkbox line (e.g. from
a stray or legacy escalation writer that didn't route through
``tier4_escalate``) is NOT silently dropped if it still carries a
``**Description**:`` line. The parser synthesizes an unchecked checkbox
from the first line of the description so the block becomes answerable, and
the repair is persisted on the next file rewrite. Only blocks with neither
a checkbox nor a description — i.e. no recoverable question — are skipped.

Layering: L4 domain/pipeline module, part of the merge/contradiction SCC
(:mod:`athenaeum.merge`, :mod:`athenaeum.resolutions`, :mod:`athenaeum.tiers`).
May import L3 services freely (``atomic_io``, ``fingerprint``, etc.). Calls
into :mod:`athenaeum.resolutions` (e.g. for freetext-edit proposals) are
DEFERRED (function-local) imports to break the SCC's import cycle — this is
intentional cross-module wiring, not something to "fix" by hoisting to the
top of the file.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from athenaeum.models import TokenUsage
    from athenaeum.provider import LLMBackend

from athenaeum.atomic_io import atomic_write_text
from athenaeum.fingerprint import (
    _member_key_str,
    _pair_text_from_passages,
    extract_passages,
    normalize_side,
    record_resolution,
)
from athenaeum.pagination import paginate
from athenaeum.sidecar_blocks import GENERIC_FENCE_OPEN_RE, split_blocks
from athenaeum.store import now_iso

log = logging.getLogger(__name__)

# Header grammar — matches `## [ISO-DATE] Entity: "{name}" (from {ref})`.
# ISO-DATE is intentionally a loose match (``[^\]]+``) so a future shift to
# datetime-with-time doesn't break the parser.
#
# Entity: between straight quotes, but tolerates backslash-escaped quotes
# (``\\"``) written by the renderer so names containing `"` round-trip.
# Unescape with :func:`_unescape_entity` after capture.
#
# Ref: greedy to the final ``)`` anchored at end-of-line. This lets raw paths
# that happen to contain parens (e.g. ``sessions/foo (v2).md``) round-trip
# without special-casing the renderer.
#
# The trailing ``$`` anchor + the outer grammar prevents greedy-runaway if a
# line somehow contains multiple ``)`` sequences.
_HEADER_RE = re.compile(
    r"^## \[(?P<date>[^\]]+)\] Entity: \""
    r"(?P<entity>(?:[^\"\\]|\\.)*)"
    r"\" \(from (?P<ref>.+)\)$"
)


def _unescape_entity(raw: str) -> str:
    """Unescape ``\\"`` and ``\\\\`` in a captured entity name.

    Paired with the renderer in :mod:`athenaeum.tiers` which escapes
    backslashes and then double quotes.
    """
    # Walk the string so we don't over-unescape adjacent sequences.
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            out.append(raw[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# Any level-2 heading starts a new pending-question block. Unlike pending
# merges (which only breaks on its canonical ``Merge:`` header and absorbs other
# ``## `` lines as Draft body), a pending-question block with a MALFORMED header
# is preserved verbatim as its own block so a human can fix it — so the block
# boundary here is any ``## `` line, while only a real ``Entity:`` header
# (:data:`_HEADER_RE`) recovers a boundary swallowed by an unclosed fence.
_ANY_H2_RE = re.compile(r"^## ")

# Checkbox grammar — ``- [ ]`` or ``- [x]`` (case-insensitive on ``x``).
_CHECKBOX_RE = re.compile(r"^- \[(?P<state>[ xX])\]\s*(?P<question>.*)$")
# Lines we strip when extracting the user's answer body.
_META_PREFIXES = ("**Conflict type**:", "**Description**:")

# Issue athenaeum#912: single-line provenance-metadata tag recognized on a
# pending-question block, mirroring the existing ``**Fingerprint**:`` /
# ``**Also affects**:`` convention. Its presence (value ``"agent"``) is what
# :func:`athenaeum.decisions.question_to_decision` uses to distinguish an
# agent-raised item from a detector-raised one in ``list_pending_decisions``
# output — see :func:`raise_pending_question` below, the sole writer of this
# line.
_RAISED_BY_PREFIX = "**Raised by**:"
_RAISED_BY_AGENT = "agent"

# Issue athenaeum#1290: a "confirmation" decision — an agent narrowed scope
# mid-build and needs a durable, non-blocking place to flag "implemented X
# without Y, confirm?" that survives the session. Reuses the SAME
# `_pending_questions.md` block grammar `raise_pending_question` already
# writes (issue athenaeum#912) — a confirmation is a question block carrying
# extra structured metadata lines, never a second parallel queue. The kind
# tag distinguishes it from a plain agent-raised question in
# ``athenaeum.decisions.question_to_decision``; the six structured fields
# capture exactly what the pending-decisions consumer contract requires
# (see ``docs/reference/configuration.md``'s "confirmation-type consumer contract").
_DECISION_KIND_PREFIX = "**Decision kind**:"
_DECISION_KIND_CONFIRMATION = "confirmation"
_RAISER_PREFIX = "**Raiser**:"
_REPO_PREFIX = "**Repo**:"
_ISSUE_REF_PREFIX = "**Issue/PR**:"
_NARROWED_SCOPE_PREFIX = "**Narrowed scope**:"
_IMPLEMENTED_BEHAVIOR_PREFIX = "**Implemented behaviour**:"
_ALTERNATIVE_PREFIX = "**Alternative**:"
_RAISED_AT_PREFIX = "**Raised at**:"

# Maps a PendingQuestion attribute name to the block-line prefix that
# carries it. Used by both `_parse_block` (read) and `raise_pending_question`
# (write) so the two can never drift out of sync on which keys exist.
_CONFIRMATION_FIELD_PREFIXES: dict[str, str] = {
    "decision_kind": _DECISION_KIND_PREFIX,
    "raiser": _RAISER_PREFIX,
    "repo": _REPO_PREFIX,
    "issue_ref": _ISSUE_REF_PREFIX,
    "narrowed_scope": _NARROWED_SCOPE_PREFIX,
    "implemented_behavior": _IMPLEMENTED_BEHAVIOR_PREFIX,
    "alternative": _ALTERNATIVE_PREFIX,
    "raised_at": _RAISED_AT_PREFIX,
}


@dataclass
class PendingQuestion:
    """Parsed view of one block in ``_pending_questions.md``.

    Returned by :func:`parse_pending_questions` and consumed by the MCP
    ``list_pending_questions`` / ``resolve_question`` tools. ``raw_block``
    preserves the exact source text of the block so callers can rewrite the
    file without losing formatting.
    """

    id: str
    entity: str
    source: str
    question: str
    conflict_type: str
    description: str
    created_at: str
    answered: bool
    answer_lines: list[str]
    raw_block: str
    # Issue athenaeum#198: claim-pair fingerprint embedded by tier4_escalate. Recovered
    # off the ``**Fingerprint**:`` line so resolution can persist the
    # adjudication to ``raw/_resolved_contradictions.jsonl``. Empty when the
    # block predates athenaeum#198 or carried no recoverable passage pair.
    fingerprint: str = ""
    # Issue athenaeum#157: entities sharing the same source-memory pair that
    # got merged into this block instead of getting their own. The
    # primary entity (header) is NOT included here. Empty by default
    # — only populated when the dedup path in tier4_escalate fires.
    also_affects: list[str] = field(default_factory=list)
    # Issue athenaeum#912: provenance marker recovered off an optional
    # ``**Raised by**:`` line. Empty ("") means the block came from a
    # detector (``tier4_escalate`) — every pre-athenaeum#912 block on disk lacks
    # this line, so "" must mean detector-raised for those blocks to keep
    # parsing identically. ``"agent"`` means the block was inserted via the
    # ``raise_decision`` MCP tool (:func:`raise_pending_question` below).
    raised_by: str = ""
    # Issue athenaeum#1290: ``"question"`` (the default, including every
    # pre-athenaeum#1290 block, which lacks a ``**Decision kind**:`` line) or
    # ``"confirmation"`` for an agent-raised "implemented X without Y,
    # confirm?" flag. Drives ``athenaeum.decisions.question_to_decision``'s
    # choice of ``type: "question"`` vs ``type: "confirmation"`` — a purely
    # ADDITIVE distinction; a plain question's parsed shape and payload are
    # completely unchanged by this field's existence.
    decision_kind: str = "question"
    # The remaining fields are populated ONLY on a ``decision_kind ==
    # "confirmation"`` block (empty string otherwise, including every
    # ordinary question). See ``_CONFIRMATION_FIELD_PREFIXES`` above for the
    # on-disk line each one is recovered from.
    raiser: str = ""
    repo: str = ""
    issue_ref: str = ""
    narrowed_scope: str = ""
    implemented_behavior: str = ""
    alternative: str = ""
    # Full ISO-8601 UTC timestamp (``**Raised at**:``) — finer-grained than
    # ``created_at`` (date-only, from the header), which the confirmation
    # AC's "a timestamp" field maps onto.
    raised_at: str = ""


def _make_id(header_line: str, question_text: str) -> str:
    """Stable id derived from the header line + question text.

    Idempotent across runs as long as the block text hasn't been edited —
    which is also when the id should change, because the block's identity
    has changed.

    Stability contract (locked by ``test_id_stable_across_checkbox_flip``
    and ``test_id_changes_when_question_edited``):

    - The id is a 12-hex-char SHA-1 prefix over header + question text.
    - It is **stable** across description edits and across checkbox state
      flips (``[ ]`` ↔ ``[x]``). A handle obtained from
      :func:`list_unanswered` therefore remains valid for
      :func:`resolve_by_id` even after a description clarification edit.
    - It **changes** when the question text itself is edited. Changing
      the question is considered a new question — a new id is correct.

    This means MCP consumers can cache ``id`` values across a session
    without worrying about description-text churn invalidating them.
    """
    payload = f"{header_line.strip()}\n{question_text.strip()}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


def _split_blocks(text: str) -> list[str]:
    """Split ``_pending_questions.md`` text into per-question blocks.

    Only a canonical question header (``## [DATE] Entity: "..." (from ...)`` —
    the :data:`_HEADER_RE` shape) starts a new block. A block's tail is the
    human's free-form answer, which may legitimately contain a fenced code block
    that itself holds bare ``---`` dividers or ``## `` headings. While inside
    that fence those lines are body content, never block delimiters — the file
    leader (``# Pending Questions``, blank lines, stray preamble) is still
    discarded, and each returned block starts with its ``## `` header.

    Historically this split naively on ``startswith("## ")`` and bare ``---``
    with NO fence tracking, so a ``---`` or ``## `` inside a human's fenced
    answer split the block and silently dropped the tail — taking the answer
    with it (the athenaeum#394 failure class, fixed in ``pending_merges`` only; audit M11
    / athenaeum#527). It now delegates to the shared
    :func:`athenaeum.sidecar_blocks.split_blocks` — the single fence-aware
    splitter — parameterized with the Entity header and a generic backtick
    fence-opener (a human answer may fence with any language, not just
    ```markdown), so the two sidecar splitters cannot diverge again.
    """
    return split_blocks(
        text,
        block_header_re=_ANY_H2_RE,
        fence_open_re=GENERIC_FENCE_OPEN_RE,
        recovery_header_re=_HEADER_RE,
        context="pending_questions",
    )


def _synthesize_checkbox_block(lines: list[str]) -> list[str] | None:
    """Recover a checkbox-less block by inserting a synthesized ``- [ ]`` line.

    The well-formed escalation path (:func:`athenaeum.tiers.tier4_escalate`)
    always emits a ``- [ ]`` checkbox directly under the header. A stray or
    legacy writer that omits it produces a block the parser would otherwise
    skip forever. When such a block still carries a recoverable question —
    i.e. a ``**Description**:`` line — we synthesize an unchecked checkbox
    from the first non-empty line of that description and insert it right
    after the header, leaving every other line untouched.

    Returns the rewritten line list (header, synthesized checkbox, original
    remainder) or ``None`` when no description is present — a block with
    neither a checkbox nor a description carries no recoverable question and
    must still be skipped.
    """
    question: str | None = None
    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        if stripped.startswith("**Description**:"):
            desc = stripped.removeprefix("**Description**:").strip()
            # First non-empty line of the description, trimmed to a single
            # checkbox row (no bullets). Mirrors tiers._question_from_description.
            for desc_line in desc.splitlines():
                cleaned = desc_line.strip().lstrip("-*").strip()
                if cleaned:
                    question = cleaned
                    break
            break

    if question is None:
        return None

    # Insert the synthesized checkbox immediately after the header. The
    # remaining lines (Conflict type, Description, etc.) are preserved.
    return [lines[0], f"- [ ] {question}", *lines[1:]]


def _parse_block(block_text: str, *, quiet: bool = False) -> PendingQuestion | None:
    """Parse one block. Returns ``None`` on malformed input.

    Args:
        block_text: The raw block text to parse.
        quiet: Issue athenaeum#1446 — when true, suppress the per-block
            ``[warn] skipping ... malformed header`` line printed to stderr.
            The paired ``log.warning`` call is left unconditional here; the
            caller (``ingest_answers``, via ``athenaeum ingest-answers
            --quiet``) suppresses it by raising the ``athenaeum.answers``
            logger's level instead, so the warning stays visible to
            ``caplog``-style capture even under ``--quiet``. Keyword-only;
            defaults to ``False`` so every existing caller (including
            ``resolve_by_id`` and ``parse_pending_questions``) is unaffected.
    """
    lines = block_text.splitlines()
    if not lines:
        return None

    header_match = _HEADER_RE.match(lines[0])
    if not header_match:
        log.warning("Skipping block with malformed header: %r", lines[0][:80])
        if not quiet:
            print(
                f"[warn] skipping pending-question block with malformed header: "
                f"{lines[0][:80]!r}",
                file=sys.stderr,
            )
        return None

    # Find the checkbox line — first non-blank line after the header.
    checkbox_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "":
            continue
        if _CHECKBOX_RE.match(lines[idx]):
            checkbox_idx = idx
        break

    if checkbox_idx is None:
        # Defensive recovery: a block can lack the `- [ ]` line if it was
        # written by a stray/legacy escalation path that omits it (the
        # well-formed path is ``tier4_escalate``, which always emits the
        # checkbox). Rather than silently dropping a block that still
        # carries a recoverable question — i.e. has a ``**Description**:``
        # line — synthesize an unchecked checkbox from that description so
        # the block becomes answerable instead of being skipped forever.
        # Blocks with neither a checkbox nor a description are genuinely
        # unrecoverable and are still skipped.
        synthesized = _synthesize_checkbox_block(lines)
        if synthesized is None:
            log.warning(
                "Skipping block without checkbox line and no recoverable "
                "question: %r",
                lines[0][:80],
            )
            print(
                f"[warn] skipping pending-question block without `- [ ]` line: "
                f"{lines[0][:80]!r}",
                file=sys.stderr,
            )
            return None
        log.warning(
            "Recovering checkbox-less block by synthesizing `- [ ]` from "
            "description: %r",
            lines[0][:80],
        )
        print(
            f"[warn] recovering pending-question block without `- [ ]` line "
            f"(synthesized checkbox from description): {lines[0][:80]!r}",
            file=sys.stderr,
        )
        lines = synthesized
        # Persist the repair: ``raw_block`` is what the file rewriters
        # (``ingest_answers`` / ``resolve_by_id``) write back, so rebuild
        # it from the now-well-formed lines. Otherwise the synthesized
        # checkbox would be discarded on the next rewrite and the block
        # would relapse to checkbox-less / unparseable.
        block_text = "\n".join(lines)
        checkbox_idx = 1

    cb_match = _CHECKBOX_RE.match(lines[checkbox_idx])
    if cb_match is None:
        return None
    answered = cb_match.group("state").lower() == "x"
    question = cb_match.group("question").strip()

    conflict_type = ""
    description = ""
    answer_lines: list[str] = []
    also_affects: list[str] = []
    fingerprint = ""
    raised_by = ""
    # Issue athenaeum#1290: confirmation-only metadata, keyed by
    # PendingQuestion attribute name — see `_CONFIRMATION_FIELD_PREFIXES`.
    # Absent (empty dict) on every ordinary question block.
    confirmation_fields: dict[str, str] = {}

    def _match_confirmation_key(stripped_line: str) -> bool:
        """Recognize a `**<Confirmation key>**:` line; record it if found.

        Mirrors the `**Fingerprint**:` / `**Raised by**:` recognition
        pattern above (metadata, never leaked into `answer_lines`), but
        table-driven over `_CONFIRMATION_FIELD_PREFIXES` instead of one
        `if` per key — these eight keys only ever appear together, all on a
        `decision_kind == "confirmation"` block.
        """
        for field_name, prefix in _CONFIRMATION_FIELD_PREFIXES.items():
            if stripped_line.startswith(prefix):
                confirmation_fields[field_name] = stripped_line.removeprefix(
                    prefix
                ).strip()
                return True
        return False

    # Tracks whether we're still accumulating continuation lines into the
    # description field. A **Description**: line opens the window; the next
    # blank line or the next ``**Key**:`` style line closes it. This lets
    # multi-line descriptions (3+ lines) survive ingest instead of losing
    # everything after the first line.
    in_description = False

    remaining = lines[checkbox_idx + 1 :]
    for raw_line in remaining:
        stripped = raw_line.strip()
        if stripped.startswith("**Conflict type**:"):
            in_description = False
            conflict_type = stripped.removeprefix("**Conflict type**:").strip()
            continue
        if stripped.startswith("**Description**:"):
            in_description = True
            description = stripped.removeprefix("**Description**:").strip()
            continue
        if stripped.startswith("**Also affects**:"):
            # Issue athenaeum#157: dedup-merge tag. Comma-separated entity names
            # that share the source-memory pair with this block's primary
            # entity. Recognized as metadata so it does NOT leak into
            # answer_lines (which would forge a phantom user answer).
            in_description = False
            payload = stripped.removeprefix("**Also affects**:").strip()
            also_affects = [name.strip() for name in payload.split(",") if name.strip()]
            continue
        if stripped.startswith("**Fingerprint**:"):
            # Issue athenaeum#198: claim-pair fingerprint metadata. Recognized so it
            # does NOT leak into answer_lines (which would forge a phantom
            # user answer).
            in_description = False
            fingerprint = stripped.removeprefix("**Fingerprint**:").strip()
            continue
        if stripped.startswith(_RAISED_BY_PREFIX):
            # Issue athenaeum#912: provenance marker distinguishing an agent-raised
            # block (`raise_decision` MCP tool) from a detector-raised one.
            # Recognized as metadata for the same reason as **Fingerprint**:
            # above — it must not leak into answer_lines as a phantom answer.
            in_description = False
            raised_by = stripped.removeprefix(_RAISED_BY_PREFIX).strip()
            continue
        if _match_confirmation_key(stripped):
            in_description = False
            continue
        if in_description:
            # Continuation: consume into description until we hit a terminator.
            # Blank line or another ``**Key**:`` tag closes the window.
            if stripped == "" or stripped.startswith("**"):
                in_description = False
                # A blank line is a pure terminator — drop it and move on.
                if stripped == "":
                    continue
                # A new **Key**: line — fall through to the key dispatchers above
                # by handling it here (the only other recognized key is
                # **Conflict type**, already handled). For unknown keys, treat
                # the line as answer body.
                if stripped.startswith("**Conflict type**:"):
                    conflict_type = stripped.removeprefix("**Conflict type**:").strip()
                    continue
                if stripped.startswith("**Also affects**:"):
                    payload = stripped.removeprefix("**Also affects**:").strip()
                    also_affects = [
                        name.strip() for name in payload.split(",") if name.strip()
                    ]
                    continue
                if stripped.startswith("**Fingerprint**:"):
                    fingerprint = stripped.removeprefix("**Fingerprint**:").strip()
                    continue
                if stripped.startswith(_RAISED_BY_PREFIX):
                    raised_by = stripped.removeprefix(_RAISED_BY_PREFIX).strip()
                    continue
                if _match_confirmation_key(stripped):
                    continue
                # Unknown **Key**: — treat as answer body.
                answer_lines.append(raw_line)
                continue
            # Plain continuation line: append, preserving raw formatting.
            description = (description + "\n" + raw_line).lstrip("\n")
            continue
        # Otherwise: treat as part of the user's answer body.
        answer_lines.append(raw_line)

    # Trim leading/trailing blank lines from the answer body.
    while answer_lines and not answer_lines[0].strip():
        answer_lines.pop(0)
    while answer_lines and not answer_lines[-1].strip():
        answer_lines.pop()

    return PendingQuestion(
        id=_make_id(lines[0], question),
        entity=_unescape_entity(header_match.group("entity")),
        source=header_match.group("ref"),
        question=question,
        conflict_type=conflict_type,
        description=description,
        created_at=header_match.group("date"),
        answered=answered,
        answer_lines=answer_lines,
        raw_block=block_text,
        fingerprint=fingerprint,
        also_affects=also_affects,
        raised_by=raised_by,
        decision_kind=confirmation_fields.get("decision_kind", "question"),
        raiser=confirmation_fields.get("raiser", ""),
        repo=confirmation_fields.get("repo", ""),
        issue_ref=confirmation_fields.get("issue_ref", ""),
        narrowed_scope=confirmation_fields.get("narrowed_scope", ""),
        implemented_behavior=confirmation_fields.get("implemented_behavior", ""),
        alternative=confirmation_fields.get("alternative", ""),
        raised_at=confirmation_fields.get("raised_at", ""),
    )


def parse_pending_questions(pending_path: Path) -> list[PendingQuestion]:
    """Parse ``_pending_questions.md`` into :class:`PendingQuestion` objects.

    Malformed blocks are logged and skipped — a corrupt single block cannot
    poison the rest of the file.
    """
    if not pending_path.exists():
        return []
    text = pending_path.read_text(encoding="utf-8")
    return [pq for b in _split_blocks(text) if (pq := _parse_block(b)) is not None]


# ---------------------------------------------------------------------------
# Ingest pipeline
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Turn an entity name into a filesystem-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "entity"


def _render_archive_block(
    pq: PendingQuestion, archived_at: str, *, status_line: str | None = None
) -> str:
    """Render an archive entry for an answered block.

    Includes the original raw block verbatim plus a trailer noting when the
    answer was ingested. Newest-first is handled by the caller.

    Issue athenaeum#1804: *status_line*, when given, is inserted between the raw
    block and the ``**Archived**:`` trailer — used to stamp a no-write-back
    class (field-correction / schema-amendment) so the archive entry itself
    records that no source edit was attempted. Keyword-only; defaults to
    ``None`` so every existing caller renders byte-identically.
    """
    stamp = f"{status_line}\n\n" if status_line else ""
    return f"{pq.raw_block}\n\n" f"{stamp}" f"**Archived**: {archived_at}\n"


def _render_answer_raw_file(
    pq: PendingQuestion, resolved_at: str, *, source_field: str = "pending_question_answer"
) -> str:
    """Render the raw intake markdown for a resolved question.

    *source_field* defaults to the literal ``pending_question_answer`` this
    function always used before issue athenaeum#1116 AC2 — callers pass
    :func:`athenaeum.erasure.off_corpus_recall_source`'s output instead when
    the question's own source traces to an off-corpus recall
    (:func:`athenaeum.erasure.classify_by_provenance`), so the re-ingested
    fact's OWN provenance says where it came from rather than the next
    reader having to re-guess it from content.
    """
    body = "\n".join(pq.answer_lines).strip()
    if not body:
        body = "(no answer body provided)"

    return (
        "---\n"
        f"source: {source_field}\n"
        f"original_source: raw/{pq.source}\n"
        f"entity: {pq.entity}\n"
        f"resolved_at: {resolved_at}\n"
        f"question: {pq.question}\n"
        "---\n\n"
        f"{body}\n"
    )


# ---------------------------------------------------------------------------
# Source write-back (issue athenaeum#197)
# ---------------------------------------------------------------------------
#
# Answering a pending question must APPLY the ratified verdict to the
# source-of-truth memory file(s), not merely emit a sibling provenance doc.
# Without this the same contradiction regenerates on every wiki build because
# the source memory was never edited. The edit reuses the canonical enact
# machinery in :mod:`athenaeum.resolutions` — no parallel editor here.

# Leading verdict token recognized at the head of a free-text answer, e.g.
# an answer body that starts with ``correct_a`` followed by the ratified text.
_VERDICT_TOKENS: frozenset[str] = frozenset(
    (
        "correct_a",
        "correct_b",
        "keep_a",
        "keep_b",
        "supersede",
        "supersedes",
        "deprecate",
        "deprecate_both",
        "archive",
        "forget_a",
        "forget_b",
        "retain_both_with_context",
        "not_a_conflict",
    )
)

# Single-source historical verdicts (athenaeum#197): a HUMAN answer can ask to archive
# / deprecate / supersede a named source outright. These are NOT in develop's
# resolver ``ENACTING_ACTIONS`` (a/b-indexed delete/mark); they take the
# whole-file ``deprecated: true`` marker path via ``_mark_member_frontmatter``.
_HISTORICAL_VERDICTS: frozenset[str] = frozenset(
    ("archive", "deprecate", "supersede", "supersedes")
)

# Issue athenaeum#1850: the closed ratify/reject answer grammar for a
# field-correction question — deliberately NOT added to _VERDICT_TOKENS,
# which drives _writeback_source's enact/annotate machinery for the
# unrelated "source" write-back class. See _parse_correction_answer.
_RATIFY_TOKENS: frozenset[str] = frozenset(
    ("ratify", "ratified", "approve", "approved", "accept", "apply", "yes")
)
_REJECT_TOKENS: frozenset[str] = frozenset(
    ("reject", "rejected", "decline", "deny", "no")
)

# ``**Member paths**: a, b`` — explicit source paths carried on the block.
_MEMBER_PATHS_RE = re.compile(
    r"^\s*\*\*Member paths\*\*:\s*(?P<payload>.+)$", re.MULTILINE
)
# ``Members involved: a, b`` — the detector's source-attribution line on
# auto-memory contradiction blocks (issue athenaeum#210 follow-up). The refs are
# relative to the configured intake roots (default ``raw/auto-memory``), and
# the block's ``source:`` header points at a compiled wiki page rather than
# the raw memory — so this line is the only handle on the true source files.
_MEMBERS_INVOLVED_RE = re.compile(
    r"^\s*Members involved:\s*(?P<payload>.+)$", re.MULTILINE
)
# ``Passage A: <text>`` / ``Passage 1: <text>`` inside the description.
_PASSAGE_RE = re.compile(r"^\s*Passage\s+\S+:\s*(?P<text>.+)$", re.MULTILINE)

# ``**Write-back**: held`` / ``**Write-back**: waived`` / ``**Write-back**:
# none`` — the stamp this module (issue athenaeum#1804) appends to a block whose
# write-back was held or skipped. Only the leading status token is captured;
# the rest of the line is free text (refs, a timestamp, a class name).
_WRITEBACK_STATUS_RE = re.compile(
    r"^\s*\*\*Write-back\*\*:\s*(?P<state>\S+)", re.MULTILINE
)

# Issue athenaeum#1804: conflict-type classes whose source ref can NEVER resolve at
# answer time by design. `librarian._run_correction_phase` escalates a
# field-correction/schema-amendment question with `raw_ref` pointing at the
# originating `.jsonl` correction batch (librarian.py:6262-6266), and
# `docs/design/field-corrections.md` Section 5.4 makes the question terminal
# once recorded — the batch is then retired by `git rm`
# (`corrections.retire_batch`). Write-back must never be attempted for these
# classes: if a batch ever DID resolve, `_writeback_source` would annotate or
# LLM-rewrite a raw `.jsonl` batch as if it were a memory file.
_NO_SOURCE_WRITEBACK_CONFLICT_TYPES: frozenset[str] = frozenset(
    ("field-correction", "schema-amendment")
)


def _parse_verdict(answer_body: str) -> tuple[str | None, str]:
    """Split a leading verdict token off an answer body.

    Returns ``(verdict, remainder)``. When the first non-blank line is a
    recognized verdict token (optionally followed by ``:`` or whitespace),
    that token is returned and the remainder is the rest of the body. When
    no token is present, returns ``(None, original_body)`` — the caller must
    NOT drop the free text; it is recorded as an authoritative annotation.
    """
    lines = answer_body.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return None, answer_body
    first = lines[idx].strip()
    token = first.split(":", 1)[0].split()[0].strip().lower() if first else ""
    if token in _VERDICT_TOKENS:
        # Anything after the token on the same line is the start of the value.
        same_line_rest = first[len(token) :].lstrip(": ").strip()
        rest_lines = lines[idx + 1 :]
        remainder_parts = []
        if same_line_rest:
            remainder_parts.append(same_line_rest)
        remainder_parts.extend(rest_lines)
        return token, "\n".join(remainder_parts).strip()
    return None, answer_body


def _extract_member_path_refs(raw_block: str) -> list[str]:
    """Return explicit ``**Member paths**:`` refs from a pending block."""
    refs: list[str] = []
    for m in _MEMBER_PATHS_RE.finditer(raw_block):
        for part in m.group("payload").split(","):
            part = part.strip()
            if part:
                refs.append(part)
    return refs


def _extract_members_involved_refs(raw_block: str) -> list[str]:
    """Return ``Members involved:`` source refs from a pending block.

    Issue athenaeum#210 follow-up: auto-memory contradiction blocks carry their true
    source files on a ``Members involved:`` line (comma-separated, relative to
    the intake roots), while the block ``source:`` header names a compiled
    wiki page. Without recovering these refs the write-back resolves nothing
    and the source contradiction is never edited.
    """
    refs: list[str] = []
    for m in _MEMBERS_INVOLVED_RE.finditer(raw_block):
        for part in m.group("payload").split(","):
            part = part.strip()
            if part:
                refs.append(part)
    return refs


def _resolve_source_files(refs: list[str], roots: list[Path]) -> list[Path]:
    """Resolve raw-relative source refs to existing files under ``roots``.

    Each ref is tried (in order) under each root; the first existing path
    wins. Refs that resolve nowhere are skipped — the caller logs and the
    provenance doc remains the durable audit trail. De-duplicated, order
    preserved.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for ref in refs:
        ref = ref.strip()
        if not ref:
            continue
        candidate = Path(ref)
        resolved: Path | None = None
        if candidate.is_absolute() and candidate.exists():
            resolved = candidate
        else:
            for root in roots:
                trial = root / ref
                if trial.exists():
                    resolved = trial
                    break
        if resolved is None:
            log.warning("answers: source ref did not resolve: %r", ref)
            continue
        key = resolved.resolve()
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    return out


def _answer_body(pq: PendingQuestion) -> str:
    """Return ``pq``'s answer text with block-metadata lines stripped.

    Lifted out of :func:`_writeback_source` (issue athenaeum#1804) so
    :func:`ingest_answers` can classify a block (empty vs. non-empty answer)
    with the exact same filtering the write-back path uses. ``**Member
    paths**:``, ``Members involved:``, and ``Passage N:`` lines are block
    metadata the parser routes into ``answer_lines`` (none of them are
    recognized keys); the ``**Write-back**:`` stamp line this module appends
    is metadata too, added athenaeum#1804 — none of the four may masquerade as
    part of the user's answer.
    """
    return "\n".join(
        line
        for line in pq.answer_lines
        if not _MEMBER_PATHS_RE.match(line)
        and not _MEMBERS_INVOLVED_RE.match(line)
        and not _PASSAGE_RE.match(line)
        and not _WRITEBACK_STATUS_RE.match(line)
    ).strip()


def _block_source_refs(pq: PendingQuestion) -> list[str]:
    """Return the source refs ``pq``'s write-back would resolve against.

    Lifted out of :func:`_writeback_source` (issue athenaeum#1804): resolver
    a/b order — ``pq.source`` is side a; ``**Member paths**:`` refs are the
    additional members the block involves (also-affects), side b onward;
    ``Members involved:`` (issue athenaeum#210 follow-up) covers the
    auto-memory-contradiction detector's own attribution line.
    """
    return [
        pq.source,
        *_extract_member_path_refs(pq.raw_block),
        *_extract_members_involved_refs(pq.raw_block),
    ]


def _writeback_class(pq: PendingQuestion) -> str:
    """Classify ``pq`` by whether this path ever expects a write-back.

    Returns ``"agent"`` for an agent-raised block (:func:`raise_pending_question`
    — the answer is consumed by whoever reads the archive, not written back
    to a source), ``"field-correction"`` / ``"schema-amendment"`` for a block
    whose source ref is unresolvable by design (see
    :data:`_NO_SOURCE_WRITEBACK_CONFLICT_TYPES`), or ``"source"`` for every
    ordinary detector-raised block, where a write-back IS expected.
    """
    if pq.raised_by == "agent":
        return "agent"
    if pq.conflict_type in _NO_SOURCE_WRITEBACK_CONFLICT_TYPES:
        return pq.conflict_type
    return "source"


def _parse_correction_answer(pq: PendingQuestion) -> str | None:
    """Classify ``pq``'s answer as the closed ratify/reject token (issue
    athenaeum#1850), or ``None`` when it is neither.

    Takes the first non-blank line of :func:`_answer_body` (already strips
    the ``**Write-back**:`` stamp line, ``**Member paths**:``, ``Members
    involved:``, ``Passage N:``), splits off the first whitespace/colon
    -delimited token, lowercases it, and strips a single trailing ``.``/``!``.
    ``None`` covers an empty answer body and any unrecognized token — both
    mean "hold", never "guess"; the caller
    (:func:`_apply_field_correction`) never falls back to treating an
    unrecognized token as either verdict.
    """
    body = _answer_body(pq)
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        token = stripped.split(":", 1)[0].split()[0].strip().lower().rstrip(".!")
        if token in _RATIFY_TOKENS:
            return "ratify"
        if token in _REJECT_TOKENS:
            return "reject"
        return None
    return None


def _writeback_source(
    pq: PendingQuestion,
    roots: list[Path],
    *,
    client: "LLMBackend | None" = None,
    config: "dict | None" = None,
    usage: "TokenUsage | None" = None,
) -> int:
    """Apply ``pq``'s ratified answer to its source memory file(s).

    Resolves the primary ``pq.source`` plus any ``**Member paths**:`` refs,
    parses a leading verdict token (falling back to a non-destructive
    annotation for free text), and delegates the actual edit to
    :func:`athenaeum.resolutions.enact_resolution`. Returns the number of
    source files edited. Never raises — a write-back failure must not block
    the provenance/archive path.

    When ``verdict`` is ``None`` (pure free-text answer) and a live Anthropic
    ``client`` is provided, the LLM-backed proposer
    (:func:`athenaeum.resolutions.propose_freetext_source_edits`) is invoked
    to interpret the ruling as a concrete source-file edit. The annotation
    path is used as a fallback when the proposer returns no edits or when
    ``client is None``. ``retain_both_with_context`` / ``not_a_conflict``
    verdicts always annotate (do not call the proposer).

    When ``usage`` is a :class:`athenaeum.models.TokenUsage`, the proposer
    call is metered into it (athenaeum#248): this function bumps ``api_calls`` once per
    attempted proposer call (the caller counts attempts, mirroring the athenaeum#239
    convention) and the proposer accumulates the response's token + cache
    counts. Verdict paths that make no API call leave ``usage`` untouched.
    """
    try:
        from athenaeum.resolutions import (
            ENACTING_ACTIONS,
            ResolutionProposal,
            _annotate_body,
            _mark_member_frontmatter,
            enact_resolution,
        )

        # Issue athenaeum#1804: filtering + ref-list construction moved to
        # module-level helpers so `ingest_answers` can classify a block with
        # the exact same rules before deciding whether to call this
        # function at all.
        answer_body = _answer_body(pq)
        if not answer_body:
            return 0

        refs = _block_source_refs(pq)
        member_paths = _resolve_source_files(refs, roots)
        if not member_paths:
            return 0

        verdict, remainder = _parse_verdict(answer_body)

        # --- Enacting verdicts: reuse develop's canonical enact machinery. ---
        # correct_*/forget_* DELETE the wrong/transient member file;
        # keep_*/deprecate_both MARK frontmatter (superseded_by/deprecated).
        if verdict in ENACTING_ACTIONS:
            proposal = ResolutionProposal(
                recommended_winner="neither",
                action=verdict,  # type: ignore[arg-type]
                rationale="human-ratified via pending-question answer",
                confidence=1.0,
            )
            result = enact_resolution(proposal, member_paths)
            return 1 if result is not None else 0

        # --- Historical / archive a single named source (athenaeum#197). ---
        # ``archive`` / ``deprecate`` / ``supersede`` mark the source(s)
        # ``deprecated: true`` (whole-file inactive) — reuse the existing
        # frontmatter marker rather than a new editor.
        if verdict in _HISTORICAL_VERDICTS:
            edited = 0
            for path in member_paths:
                if _mark_member_frontmatter(path, "deprecated", True):
                    edited += 1
            return edited

        # --- Non-destructive: retain_both_with_context / not_a_conflict, or
        # free-text with no verdict token.
        #
        # For explicit non-destructive verdicts (retain_both_with_context /
        # not_a_conflict) always annotate — "keep both" means no mutation.
        #
        # For pure free-text (verdict is None): FIRST try the LLM-backed
        # proposer to interpret the ruling as a concrete source-file edit.
        # If the proposer returns edits, apply them (re-attaching frontmatter).
        # If the proposer returns nothing (no client, API failure, unchanged
        # body), fall back to the annotation path below.
        # ---
        from athenaeum.models import parse_frontmatter, render_frontmatter

        if verdict is None and client is not None:
            # Free-text path: try LLM-backed source edit proposer.
            from athenaeum.resolutions import propose_freetext_source_edits

            passages = extract_passages(pq.description)
            # Build (path, body) pairs for the proposer.
            source_pairs: list[tuple[Path, str]] = []
            path_to_meta: dict[Path, dict] = {}
            for path in member_paths:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    log.warning("answers: source missing/unreadable: %s", path)
                    continue
                meta, body = parse_frontmatter(text)
                source_pairs.append((path, body))
                path_to_meta[path] = meta or {}

            if source_pairs:
                # athenaeum#248: count one attempt per proposer call at the call site
                # (the callee accumulates tokens but never bumps api_calls,
                # mirroring the athenaeum#239 convention). Bump BEFORE the call so an
                # API failure still counts as an attempt.
                if usage is not None:
                    usage.api_calls += 1
                proposed = propose_freetext_source_edits(
                    answer_body, source_pairs, passages, client, config, usage=usage
                )
                if proposed:
                    edited = 0
                    for path, new_body in proposed.items():
                        meta = path_to_meta.get(path, {})
                        if meta:
                            atomic_write_text(
                                path,
                                render_frontmatter(meta) + "\n" + new_body,
                            )
                        else:
                            atomic_write_text(path, new_body)
                        edited += 1
                    if edited:
                        log.info(
                            "answers: freetext proposer edited %d source file(s) "
                            "for entity=%s",
                            edited,
                            pq.entity,
                        )
                        return edited
                # Proposer returned no edits — fall through to annotation.

        note = remainder if verdict is not None else answer_body
        note = (note or answer_body).strip()
        if not note:
            return 0
        edited = 0
        for path in member_paths:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                log.warning("answers: source missing/unreadable: %s", path)
                continue
            meta, body = parse_frontmatter(text)
            new_body = _annotate_body(body, note)
            if new_body == body:
                continue
            if meta:
                atomic_write_text(
                    path, render_frontmatter(meta) + "\n" + new_body
                )
            else:
                atomic_write_text(path, new_body)
            edited += 1
        return edited
    except Exception:
        log.exception("answers: source write-back failed for entity=%s", pq.entity)
        return 0


# ---------------------------------------------------------------------------
# Field-correction ratified apply (issue athenaeum#1850)
# ---------------------------------------------------------------------------
#
# An answered field-correction question is NOT a "source" write-back — its
# `raw_ref` points at a `.jsonl` correction batch already retired by
# `corrections.retire_batch` (docs/design/field-corrections.md §5.4), so there is no
# source file left to edit. The block itself carries everything a
# deterministic apply needs, on its `**Description**:` continuation lines
# (`corrections.parse_escalated_correction`). Ratifying re-drives
# `corrections.process_correction_record` in its ratified mode rather than
# adding a second writer.


def _apply_field_correction(
    pq: PendingQuestion,
    *,
    wiki_root: Path,
    knowledge_root: Path,
    config: "dict | None",
) -> tuple[str, str]:
    """Ratify or reject one answered field-correction block.

    Returns ``(outcome, detail)`` where ``outcome`` is ``"applied"``,
    ``"rejected"`` or ``"held"``. Never calls :func:`_writeback_source` or
    :mod:`athenaeum.resolutions`'s free-text proposer — this branch re-drives
    :func:`athenaeum.corrections.process_correction_record` instead of the
    source-annotation/enact machinery those serve, and never reaches the LLM.

    An unrecognized/empty answer, or a block that does not parse as a
    :class:`~athenaeum.corrections.EscalatedCorrection`
    (:func:`athenaeum.corrections.parse_escalated_correction`), always
    returns ``"held"`` — including for a ``reject`` answer, because a block
    that cannot be trusted to BE the correction it claims to be cannot be
    trusted to be safely dismissed either. A well-formed ``reject`` needs no
    further validation: nothing is written either way.
    """
    from athenaeum import corrections
    from athenaeum.models import EntityIndex

    answer_token = _parse_correction_answer(pq)
    if answer_token is None:
        return "held", "answer is not a recognized ratify/reject token"

    parsed = corrections.parse_escalated_correction(pq.description)
    if isinstance(parsed, str):
        return "held", parsed

    if answer_token == "reject":
        return "rejected", f"operator rejected correction {parsed.correction_id}; no write"

    # answer_token == "ratify"
    index = EntityIndex(wiki_root)
    registry_entities = corrections.load_registry(knowledge_root)
    # Issue athenaeum#1850: the ratified write is attributed to the OPERATOR,
    # never to the original proposer's `Source:` text (kept only as
    # provenance prose in the archive stamp below) — this is the exact
    # in-repo minting pattern `repair.py:611` uses for a user-stated source.
    # It parses to type=user (rank 1, precedence.py), and is what lets
    # `process_correction_record`'s ratified mode skip §6.2 entirely: the
    # operator IS the highest-precedence source, so there is nothing left
    # to arbitrate.
    ratified_source = f"user:pending-question:{pq.id}"
    raw_record = {
        "record": "correction",
        "target": parsed.target,
        "op": parsed.op,
        "field": parsed.field,
        "value": parsed.value,
        "source": ratified_source,
        "observed_at": date.today().isoformat(),
    }
    envelope: dict[str, Any] = {
        "schema_version": parsed.schema_version,
        "submitter": None,
        "defaults": {},
    }
    result = corrections.process_correction_record(
        raw_record,
        envelope,
        index=index,
        knowledge_root=knowledge_root,
        registry_entities=registry_entities,
        config=config,
        ratified_source=ratified_source,
    )
    if result.disposition in ("applied", "routed-elsewhere", "recorded-as-prose", "noop"):
        page_name = result.entity_path.name if result.entity_path is not None else "?"
        return "applied", f"{result.disposition} to {page_name} ({result.reason})"
    return "held", result.reason


@dataclass
class IngestAnswersReport:
    """Optional accumulator threaded through :func:`ingest_answers` (issue athenaeum#1804).

    Populated in place when a caller passes ``report=IngestAnswersReport()``;
    ``ingest_answers`` itself keeps returning a plain ``int`` (answers moved
    to the archive this run) for full back-compat with every existing call
    site. Every field defaults to 0/empty so an accumulator is usable
    standalone before a run.

    Attributes:
        files_written: Source memory files actually edited by
            :func:`_writeback_source` this run (summed across every call —
            the CLI's "source files written" line).
        held: Blocks that ended this run in ``held`` status — newly held
            plus still-held-and-still-unresolved. Not archived.
        waived: Blocks archived this run whose status line already said
            ``waived`` (write-back was skipped by human waiver, not
            attempted).
        archived_no_writeback: Blocks archived this run whose class never
            calls :func:`_writeback_source` by design (see
            :data:`_NO_SOURCE_WRITEBACK_CONFLICT_TYPES`), keyed by class
            name. Issue athenaeum#1850: this is ``"schema-amendment"`` ONLY
            now — a field-correction's ratified apply counts into
            ``corrections_applied``/``corrections_rejected``/``held``
            instead, since it now writes back (to its target page, not a
            "source" file).
        corrections_applied: Issue athenaeum#1850 — field-correction blocks
            archived this run whose ratified apply reached the target page
            (dispositions ``applied``/``routed-elsewhere``/
            ``recorded-as-prose``/``noop``).
        corrections_rejected: Issue athenaeum#1850 — field-correction blocks
            archived this run whose answer was ``reject`` (no write).
    """

    files_written: int = 0
    held: int = 0
    waived: int = 0
    archived_no_writeback: dict[str, int] = field(default_factory=dict)
    corrections_applied: int = 0
    corrections_rejected: int = 0


def ingest_answers(
    pending_path: Path,
    raw_root: Path,
    *,
    client: "LLMBackend | None" = None,
    config: "dict | None" = None,
    quiet: bool = False,
    report: IngestAnswersReport | None = None,
) -> int:
    """Parse resolved items from ``pending_path``, write raw intake, archive.

    Walks ``_pending_questions.md``; for each ``[x]`` block writes a file
    under ``raw/answers/`` with frontmatter linking back to the original
    source, then moves the block to ``_pending_questions_archive.md``
    (newest-first, append-only). ``[ ]`` blocks are left in place.

    Idempotent: calling again with no new ``[x]`` blocks is a no-op.
    Malformed blocks emit a warning and are skipped.

    Issue athenaeum#1804 — held write-backs: a detector-raised (not agent-raised,
    not field-correction/schema-amendment) block with a non-empty answer
    whose source refs ALL fail to resolve is NOT archived. It stays in
    ``pending_path`` and gains a ``**Write-back**: held — ...`` line naming
    the unresolved refs; the provenance file under ``raw/answers/`` is still
    written (once). A later run either completes the write-back (the source
    came back) or, if a human edits ``held`` to ``waived`` on that line,
    archives without ever calling the write-back. Field-correction /
    schema-amendment blocks — whose source ref is unresolvable by design,
    see :data:`_NO_SOURCE_WRITEBACK_CONFLICT_TYPES` — and agent-raised /
    empty-answer blocks keep archiving unconditionally (never held); the
    former two are stamped ``**Write-back**: none`` in their archive entry.

    Args:
        pending_path: Path to ``_pending_questions.md``.
        raw_root: Raw intake root (answers land in ``raw_root/answers/``).
        client: Optional live Anthropic client. When provided, free-text
            answers invoke the LLM-backed proposer to generate source-file
            edits instead of falling back to annotation-only. Keyword-only;
            defaults to ``None`` so every existing caller is unaffected.
        config: Optional athenaeum config dict. Forwarded to the resolver
            for model selection. Keyword-only; defaults to ``None``.
        quiet: Issue athenaeum#1446 — when true, suppress the per-block
            ``[warn] skipping ... malformed header`` print (forwarded to
            :func:`_parse_block`) and, in combination with the caller
            raising the ``athenaeum.answers`` logger's level, the per-block
            ``log.warning`` noise this function and ``_parse_block`` both
            emit. This function does not itself touch logger levels — see
            ``athenaeum._cmd_pending.cmd_ingest_answers`` for that half.
            Keyword-only; defaults to ``False`` so every existing caller is
            unaffected and the unflagged CLI path is byte-for-byte
            unchanged.
        report: Issue athenaeum#1804 — optional :class:`IngestAnswersReport`
            accumulator, updated in place. Keyword-only; defaults to
            ``None`` so every existing caller is unaffected. The function's
            return value stays a plain ``int`` regardless.

    Returns:
        Count of answers moved to the archive on this run (a held block is
        NOT counted — it stays in ``pending_path``).
    """
    if not pending_path.exists():
        return 0

    text = pending_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)
    if not blocks:
        return 0

    answers_dir = raw_root / "answers"
    # Issue athenaeum#1116: bound unconditionally (not only inside the
    # best-effort try block below) so the AC2 off-corpus routing check
    # further down always has a knowledge_root even if config loading fails.
    knowledge_root = raw_root.parent
    # Issue athenaeum#197: roots under which a block's source ref(s) are resolved for
    # write-back. raw_root first (auto-memory sources live there), then the
    # wiki root (``pending_path.parent``) for wiki-side memories.
    #
    # Issue athenaeum#210 follow-up: the detector attributes auto-memory contradictions
    # via ``Members involved:`` refs that are relative to the configured intake
    # roots (default ``raw/auto-memory``), not to ``raw/`` directly. Add the
    # auto-memory root and any configured extra intake roots so those refs
    # resolve to the real source files instead of nothing.
    source_roots = [raw_root, raw_root / "auto-memory", pending_path.parent]
    try:
        from athenaeum.config import load_config, resolve_extra_intake_roots

        knowledge_root = raw_root.parent
        cfg = config if config is not None else load_config(knowledge_root)
        for extra in resolve_extra_intake_roots(knowledge_root, cfg):
            if extra not in source_roots:
                source_roots.append(extra)
    except Exception:  # noqa: BLE001 -- config is best-effort; defaults suffice
        pass

    # athenaeum#248: meter the LLM calls this run makes (the free-text proposer is the
    # only API call on the ingest-answers path). The accumulator is threaded
    # into _writeback_source; a one-line cost summary is emitted at the end of
    # a run that made >= 1 API call. No budget enforcement on this path.
    from athenaeum.models import TokenUsage

    usage = TokenUsage()

    unanswered: list[PendingQuestion] = []
    archived_new: list[str] = []
    # Parallel arrays used only for dedup-skip logging and for matching each
    # rendered archive block back to the raw block text that originated it.
    _archived_entities: list[str] = []
    _archived_raw_blocks: list[str] = []
    ingested = 0
    # Issue athenaeum#1804: a run whose only change is a newly-held block still
    # rewrites `pending_path` even though nothing was archived (`ingested`
    # stays 0) — see the early-return check below.
    primary_dirty = False

    now = datetime.now(timezone.utc)
    iso_ts = now_iso(now)
    filename_ts = now.strftime("%Y%m%dT%H%M%SZ")

    for block_text in blocks:
        pq = _parse_block(block_text, quiet=quiet)
        if pq is None:
            # Malformed — preserve as-is in the primary file so the human
            # can see + fix it. Do not archive. Left as an unconditional
            # log.warning (issue athenaeum#1446): under `quiet`, the CLI
            # caller raises the `athenaeum.answers` logger's level rather
            # than this function structurally deleting the call, so the
            # record stays visible to caplog-style capture.
            log.warning("Preserving malformed block verbatim in primary file.")
            unanswered.append(
                PendingQuestion(
                    id="malformed",
                    entity="",
                    source="",
                    question="",
                    conflict_type="",
                    description="",
                    created_at="",
                    answered=False,
                    answer_lines=[],
                    raw_block=block_text,
                )
            )
            continue

        if not pq.answered:
            unanswered.append(pq)
            continue

        # --- Issue athenaeum#1804: classify before archiving -----------------
        # A detector-raised ("source" class) block whose answer authorizes a
        # write-back that cannot happen right now (non-empty answer, zero
        # resolved refs) is HELD rather than archived — see
        # `_writeback_class`/`_NO_SOURCE_WRITEBACK_CONFLICT_TYPES` above and
        # the function docstring. `existing_state` recovers a status this
        # function stamped on a PRIOR run, off the block's own raw text (the
        # only place that survives a checkbox-preserving rewrite).
        writeback_class = _writeback_class(pq)
        status_match = _WRITEBACK_STATUS_RE.search(pq.raw_block)
        existing_state = status_match.group("state").lower() if status_match else None

        will_hold = False
        hold_refs: list[str] = []
        # Issue athenaeum#1850: (outcome, detail) from the ratified-apply
        # attempt, computed here (before the provenance write below) so a
        # field-correction block's hold/apply/reject status is known before
        # deciding whether this run needs to write anything at all — the
        # same early-evaluation shape the "source" class already uses for
        # its own hold_refs resolution check just above.
        field_correction_outcome: tuple[str, str] | None = None
        if writeback_class == "source" and existing_state != "waived":
            hold_body = _answer_body(pq)
            hold_refs = _block_source_refs(pq)
            if hold_body and not _resolve_source_files(hold_refs, source_roots):
                will_hold = True
        elif writeback_class == "field-correction" and existing_state != "waived":
            field_correction_outcome = _apply_field_correction(
                pq,
                wiki_root=pending_path.parent,
                knowledge_root=knowledge_root,
                config=config,
            )
            if field_correction_outcome[0] == "held":
                will_hold = True

        if will_hold and existing_state == "held":
            # Still unresolved/unactionable, nothing changed this run: no
            # provenance rewrite, no fingerprint, no archive — leave the
            # block byte-identical (AC2's "second run ... primary file is
            # byte-identical"; athenaeum#1850 reuses this exact machinery for a
            # field-correction block that still cannot act, including
            # keeping the FIRST held line even when this run's reason text
            # differs from what produced it).
            unanswered.append(pq)
            if report is not None:
                report.held += 1
            continue

        # Write raw intake file — retry with a counter if the slug collides
        # within the same second (two answers resolved in the same run).
        # Skipped when provenance was already written on an earlier run
        # (existing_state is "held" [refs now resolve] or "waived").
        if existing_state is None:
            answers_dir.mkdir(parents=True, exist_ok=True)
            slug = _slugify(pq.entity)
            answer_filename = f"{filename_ts}-{slug}.md"
            candidate = answers_dir / answer_filename
            counter = 1
            while candidate.exists():
                answer_filename = f"{filename_ts}-{slug}-{counter}.md"
                candidate = answers_dir / answer_filename
                counter += 1

            # Issue athenaeum#1116 AC2: classify by PROVENANCE, never re-guess from
            # content — a question raised against off-corpus-recalled content
            # (``pq.source`` shaped ``recall-offcorpus:<ref>``, issue athenaeum#985 AC5)
            # means the ratified answer re-ingests that same off-corpus lineage.
            from athenaeum.erasure import classify_by_provenance, off_corpus_recall_source
            from athenaeum.provenance import parse_source

            recall_ref: str | None = None
            try:
                # ``pq.source`` is usually a bare file path (the pending-question
                # header's "(from <ref>)" — see ``_HEADER_RE``), not a
                # ``"<type>:<ref>"`` provenance scalar; ``parse_source`` raises
                # ValueError on that legacy/non-scalar shape rather than
                # returning None (issue athenaeum#97's retired bare-slug form).
                # That is exactly the common case here, so it is caught and
                # treated as "not provenance-taintable" — never re-guessed from
                # content, just not classifiable by provenance at all.
                if classify_by_provenance(pq.source):
                    parsed_source = parse_source(pq.source)
                    if parsed_source is not None:
                        recall_ref = parsed_source.ref
            except ValueError:
                recall_ref = None

            if recall_ref is not None:
                raw_text = _render_answer_raw_file(
                    pq, iso_ts, source_field=off_corpus_recall_source(recall_ref)
                )
                # Reversible default (issue athenaeum#1116, matching AC1's posture):
                # when an off-corpus surface IS configured, the re-ingested answer
                # is routed there instead of the ordinary raw intake tree. When it
                # is NOT configured, there is nothing to route to — the answer
                # still lands in the ordinary raw intake tree exactly as before
                # this wiring (breaking every deployment that has not configured
                # off-corpus would be worse than the gap this issue closes), but a
                # structured, greppable WARNING names the taint and the file.
                from athenaeum.off_corpus import off_corpus_adapter, off_corpus_store
                from athenaeum.store import StoreKey

                store = off_corpus_store(config, knowledge_root)
                if store is not None:
                    adapter = off_corpus_adapter(config)
                    assert adapter is not None  # off_corpus_store already returned non-None
                    relpath = f"answers/{answer_filename}"
                    store.put(StoreKey(surface=adapter.name, key=relpath), raw_text.encode("utf-8"))
                    log.info(
                        "answers: routed re-ingested off-corpus recall %s off-corpus "
                        "(athenaeum#1116 AC2, source=%s)",
                        relpath,
                        pq.source,
                    )
                else:
                    log.warning(
                        "erasure-taint-not-routed: answer for entity=%s re-ingests an "
                        "off-corpus recall (source=%s) but no off-corpus surface is "
                        "configured (off_corpus.enabled=false) - writing to the "
                        "ordinary raw intake corpus (athenaeum#1116)",
                        pq.entity,
                        pq.source,
                    )
                    atomic_write_text(candidate, raw_text)
            else:
                atomic_write_text(candidate, _render_answer_raw_file(pq, iso_ts))

        # Issue athenaeum#1804/#1850: schema-amendment blocks never call
        # _writeback_source — the batch is retired by git rm once the
        # question is recorded (see _NO_SOURCE_WRITEBACK_CONFLICT_TYPES). A
        # field-correction block ALSO never calls _writeback_source (same
        # retired-batch reason) but, unlike schema-amendment, it now has its
        # own ratified-apply write path (field_correction_outcome, computed
        # above) instead of a permanent "none" stamp. A waived block already
        # had its (non-)write-back decided by the human editing held ->
        # waived, so it is not re-attempted either.
        skip_writeback = False
        archive_status_line: str | None = None
        if writeback_class == "field-correction":
            skip_writeback = True
            if existing_state == "waived":
                if report is not None:
                    report.waived += 1
            elif not will_hold:
                assert field_correction_outcome is not None
                outcome, detail = field_correction_outcome
                archive_status_line = f"**Write-back**: {outcome} — {detail}"
                if report is not None:
                    if outcome == "applied":
                        report.corrections_applied += 1
                    else:
                        report.corrections_rejected += 1
        elif writeback_class == "schema-amendment":
            skip_writeback = True
            archive_status_line = (
                f"**Write-back**: none — {writeback_class} blocks never write "
                "back to source; the batch is retired once recorded (see "
                "athenaeum#1804) — re-submit the ratified change through its own applier"
            )
            if report is not None:
                report.archived_no_writeback[writeback_class] = (
                    report.archived_no_writeback.get(writeback_class, 0) + 1
                )
        elif existing_state == "waived":
            skip_writeback = True
            if report is not None:
                report.waived += 1

        if will_hold:
            # First transition into hold this run (existing_state is None —
            # the still-held/still-unresolved case returned above).
            if writeback_class == "field-correction":
                assert field_correction_outcome is not None
                _, hold_detail = field_correction_outcome
                held_line = (
                    f"**Write-back**: held — {hold_detail} as of {iso_ts}; answer "
                    '"ratify" or "reject", or change "held" to "waived" to '
                    "archive without a write"
                )
            else:
                held_line = (
                    "**Write-back**: held — no source ref resolved ("
                    + ", ".join(r for r in hold_refs if r.strip())
                    + f") as of {iso_ts}; restore the source or change \"held\" to "
                    '"waived" to archive without a write'
            )
            new_raw_block = pq.raw_block.rstrip("\n") + "\n\n" + held_line + "\n"
            unanswered.append(replace(pq, raw_block=new_raw_block))
            primary_dirty = True
            if report is not None:
                report.held += 1
            continue

        # Issue athenaeum#197/#210: apply the ratified verdict to the source memory
        # file(s). The provenance doc above is the audit trail and is ALWAYS
        # written first; this write-back is what stops the contradiction from
        # regenerating on the next wiki build. Failures are swallowed inside
        # _writeback_source so the audit/archive path is never blocked.
        # Issue athenaeum#210: thread client/config so free-text answers can use the
        # LLM-backed proposer to enact source edits instead of annotating only.
        if skip_writeback:
            edited = 0
        else:
            edited = _writeback_source(
                pq, source_roots, client=client, config=config, usage=usage
            )
            if report is not None:
                report.files_written += edited
        if edited:
            log.info(
                "answers: wrote ratified verdict back to %d source file(s) "
                "for entity=%s",
                edited,
                pq.entity,
            )

        # Issue athenaeum#198: persist the human resolution to the fingerprint cache so
        # the settled claim-pair stops re-escalating on future pages.
        # resolved_by="human" is load-bearing for sibling athenaeum#199 (only human
        # verdicts auto-apply there). No-op when the block carried no
        # fingerprint (pre-athenaeum#198 block or no recoverable passage pair).
        if pq.fingerprint:
            verdict, _ = _parse_verdict("\n".join(pq.answer_lines))
            # Issue athenaeum#199: persist per-side anchors in the verdict's ORIGINAL
            # a/b orientation so the auto-apply lane can reconcile a swapped
            # re-surfacing of the same (order-independent-fingerprinted) pair.
            # side a = Passage 1 = pq.source = member_paths[0]; side b =
            # Passage 2. Normalized with the SAME helper the fingerprint uses.
            # Issue athenaeum#216 (follow-up to athenaeum#211): derive side passages from the
            # FULL raw block, not pq.description. ``_parse_block`` truncates the
            # description at the first line starting with ``**`` (an intervening
            # bold passage line drops Passage 2), which silently emptied the
            # pair_text / side-norm anchors.
            side_passages = extract_passages(pq.raw_block)
            side_a_norm = (
                normalize_side(side_passages[0]) if len(side_passages) >= 1 else None
            )
            side_b_norm = (
                normalize_side(side_passages[1]) if len(side_passages) >= 2 else None
            )
            # Issue athenaeum#211 + athenaeum#216: persist member_key and pair_text so the
            # decision-log matcher can suppress re-detections that share the
            # same member pair even when the passage text drifted. The real
            # source attribution is the ``Members involved:`` line (the block
            # ``source:`` header is a compiled wiki page, not a memory pair).
            # Derive the key from it the SAME way the matcher does:
            # ``_member_key_str`` sorts+dedups, so feeding it the same
            # ``Members involved:`` refs yields a key identical to the one
            # ``tiers`` computes via ``_pair_key_from_description``.
            _answer_refs = [
                *_extract_members_involved_refs(pq.raw_block),
                *_extract_member_path_refs(pq.raw_block),
            ]
            _answer_member_key = _member_key_str(_answer_refs)
            _answer_pair_text: str | None = (
                _pair_text_from_passages(side_passages[0], side_passages[1])
                if len(side_passages) >= 2
                else None
            )
            record_resolution(
                raw_root.parent,
                fingerprint=pq.fingerprint,
                verdict=verdict or "human-answered",
                resolved_by="human",
                source_verdict_id=pq.id,
                resolved_at=iso_ts,
                side_a_norm=side_a_norm,
                side_b_norm=side_b_norm,
                member_key=_answer_member_key,
                pair_text=_answer_pair_text,
            )

        archived_new.append(
            _render_archive_block(pq, iso_ts, status_line=archive_status_line)
        )
        _archived_entities.append(pq.entity)
        _archived_raw_blocks.append(pq.raw_block)
        ingested += 1

    # athenaeum#248: one cost summary per run that made >= 1 API call (the free-text
    # proposer). Mirrors the librarian's run-summary format string in
    # ``librarian.run`` (tokens in/out, cache written/read, estimated cost).
    # No line is emitted when zero API calls were made. The per-call cache
    # DEBUG log in ``propose_freetext_source_edits`` is unchanged; this is
    # additive. Placed before the early ``ingested == 0`` return so a run that
    # attempted the proposer but ingested nothing still reports its spend.
    if usage.api_calls > 0:
        log.info(
            "Token usage: %d API calls, %d input + %d output = %d total"
            " (cache: %d written, %d read) (~$%.4f estimated)",
            usage.api_calls,
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
            usage.cache_creation_input_tokens,
            usage.cache_read_input_tokens,
            usage.estimated_cost_usd,
        )
    # Issue athenaeum#378: persist the answers-ingest spend to the durable ledger,
    # tagged with the resolved provider so the free-text proposer's metered
    # (or subscription) usage is answerable from data. Best-effort.
    # Issue athenaeum#786: resolved via the ``resolve`` knob — this ingest path's only
    # LLM call is ``resolutions.propose_freetext_source_edits`` (knob="resolve",
    # threaded through ``_answer_freetext`` above), so tagging the row with the
    # SAME knob's resolved provider keeps this ledger row accurate when
    # ``llm.providers.resolve`` differs from the global ``llm.provider``. No
    # ``llm.providers.resolve`` key resolves identically to the pre-athenaeum#786
    # global-only call (AC6).
    from athenaeum import spend
    from athenaeum.provider import resolve_provider

    spend.record_spend(
        usage,
        run_type=spend.RUN_TYPE_ANSWERS,
        provider=resolve_provider(config, knob="resolve"),
        config=config,
        # pending_path is <wiki_root>/_pending_questions.md (module docstring),
        # so its parent IS wiki_root — issue athenaeum#980 AC4.
        wiki_root=pending_path.parent,
    )

    # Issue athenaeum#1804: previously this returned whenever `ingested == 0`,
    # which also skipped the rewrite for a run whose ONLY change was a block
    # newly transitioning to held (never archived, so `ingested` stays 0
    # for it) — that block's `**Write-back**: held` stamp would be silently
    # dropped on the floor. `primary_dirty` covers exactly that case.
    if ingested == 0 and not primary_dirty:
        return 0

    # Rewrite the primary file — keep the header, keep unanswered blocks.
    primary_parts = ["# Pending Questions"]
    for pq in unanswered:
        primary_parts.append(pq.raw_block)
    primary_body = "\n\n---\n\n".join(primary_parts) + "\n"
    atomic_write_text(pending_path, primary_body)

    if ingested == 0:
        # Only a newly-held block changed this run — the primary file was
        # rewritten above to persist its stamp, but there is nothing new to
        # archive.
        return 0

    # Append to archive, newest-first.
    archive_path = pending_path.parent / "_pending_questions_archive.md"
    existing_archive = ""
    if archive_path.exists():
        existing_archive = archive_path.read_text(encoding="utf-8")

    # Dedup guard — if the raw block text of an answered item already appears
    # in the existing archive, skip re-appending it. Protects against the
    # "user re-pasted an already-answered block into the primary file"
    # failure mode noted in Quine Q5.
    filtered_archived: list[str] = []
    for rendered, raw_block, entity in zip(
        archived_new, _archived_raw_blocks, _archived_entities
    ):
        if raw_block.strip() and raw_block in existing_archive:
            print(
                f"[warn] skipping duplicate archive entry for entity={entity}",
                file=sys.stderr,
            )
            log.info("Skipping duplicate archive entry for entity=%s", entity)
            continue
        filtered_archived.append(rendered)

    if not filtered_archived:
        # Nothing new to archive — we still ingested raw intake files above.
        log.info(
            "Ingested %d answer(s) but archive already contained all of them; "
            "no archive update.",
            ingested,
        )
        return ingested

    new_section = "\n\n---\n\n".join(filtered_archived)
    if existing_archive.strip():
        # newest-first: new answers go at the top, under the header.
        if existing_archive.startswith("# Answered Questions"):
            # Split off the header so we can prepend under it.
            _, _, rest = existing_archive.partition("\n")
            combined = (
                "# Answered Questions\n\n" + new_section + "\n\n---\n\n" + rest.lstrip()
            )
        else:
            combined = (
                "# Answered Questions\n\n"
                + new_section
                + "\n\n---\n\n"
                + existing_archive.lstrip()
            )
    else:
        combined = "# Answered Questions\n\n" + new_section + "\n"

    atomic_write_text(archive_path, combined)

    log.info("Ingested %d pending-question answer(s) from %s", ingested, pending_path)
    return ingested


# ---------------------------------------------------------------------------
# MCP-facing helpers
# ---------------------------------------------------------------------------


def list_unanswered(
    pending_path: Path,
    *,
    caller_audience: set[str] | None = None,
    knowledge_root: Path | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> list[dict]:
    """Return unanswered pending questions as dicts suitable for MCP output.

    Each dict has: ``id``, ``entity``, ``source``, ``question``,
    ``conflict_type``, ``description``, ``created_at``.

    Issue athenaeum#538: a restricted ``caller_audience`` (non-owner) sees only questions
    whose originating ``source`` memory it is authorized to read — the same
    fail-closed predicate ``recall`` applies. Owner (``None``, the default)
    sees everything, preserving existing behavior. ``knowledge_root`` is the
    base a relative source path is resolved against.

    Issue athenaeum#1431: ``offset``/``limit`` page over the result. The
    slice is applied AFTER the ``answered``/authorization filtering above, so
    a page contains exactly the items a given caller is entitled to see —
    never a raw slice of the unfiltered file that then gets filtered down to
    fewer items than the caller asked for. ``offset`` defaults to ``0`` and
    is clamped to ``0`` if negative. ``limit`` defaults to ``None``, which
    means UNBOUNDED — this is deliberate and load-bearing: it preserves the
    existing behavior byte-for-byte for every direct caller (notably the
    ``athenaeum decisions`` CLI, whose ``_counts()`` helper must see every
    item — see ``src/athenaeum/_cmd_decisions.py``). A ``limit`` of ``0`` or
    a negative number is likewise treated as unbounded rather than yielding
    an empty page. Ordering is the file's block order, which is stable for a
    static backing file — consecutive pages neither duplicate nor skip items.
    """
    from athenaeum.models import is_page_authorized_at

    items = [
        {
            "id": pq.id,
            "entity": pq.entity,
            "source": pq.source,
            "question": pq.question,
            "conflict_type": pq.conflict_type,
            "description": pq.description,
            "created_at": pq.created_at,
            # Issue athenaeum#912: "" for a detector-raised (tier4_escalate) block,
            # "agent" for one filed via `raise_pending_question` /
            # ``raise_decision``. Additive field — existing callers that
            # don't look at it are unaffected.
            "raised_by": pq.raised_by,
        }
        for pq in parse_pending_questions(pending_path)
        if not pq.answered
        and is_page_authorized_at(pq.source, caller_audience, base=knowledge_root)
    ]

    return paginate(items, offset=offset, limit=limit)["items"]


# ---------------------------------------------------------------------------
# Raise path (issue athenaeum#912): agent-side INSERT into the pending-decisions
# queue. Every prior write to `_pending_questions.md` originated from
# athenaeum's own detectors (`tier4_escalate`, above); there was no way for an
# agent that discovers something needing a human decision to file it here.
# The real harm this closes: during a 2026-08-06 contact-sync fix, a
# delegated agent flagged "I narrowed this to scalar fields — flag it if you
# meant the stricter reading" in free-form prose to its orchestrator. The
# orchestrator folded that flag into a summary next to an unrelated
# question; the human answered the other one, and the flag — never given a
# forcing function or persistent state — silently evaporated when the
# session ended.
#
# Design: the file-backed `_pending_questions.md` sidecar is exactly why a
# detector-raised item survives the session that created it — so an
# agent-raised item is appended through the SAME sidecar, in the SAME block
# grammar `tier4_escalate` writes, rather than inventing a second, parallel
# queue (which the issue itself notes "reproduces the original problem one
# level up"). The only new thing on disk is the optional ``**Raised by**:``
# provenance line (see `_RAISED_BY_PREFIX` above) — everything else
# (`list_pending_questions`, `list_pending_decisions`, `resolve_question`,
# `ingest_answers`) already knows how to read and resolve this exact block
# shape with zero special-casing.
# ---------------------------------------------------------------------------


#: Fields required on ``kind="confirmation"``, in the order they are
#: validated (issue athenaeum#1290) — the AC's own enumeration order
#: ("raiser, repo, issue/PR, the narrowed scope, the implemented behaviour,
#: the alternative"). Maps the ``raise_pending_question`` / ``raise_decision``
#: keyword name to the human-readable label used in a validation message.
_REQUIRED_CONFIRMATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("raiser", "raiser"),
    ("repo", "repo"),
    ("issue_ref", "issue/PR"),
    ("narrowed_scope", "narrowed scope"),
    ("implemented_behavior", "implemented behaviour"),
    ("alternative", "alternative"),
)


def default_confirmation_question(
    *,
    repo: str,
    issue_ref: str,
    implemented_behavior: str,
    alternative: str,
) -> str:
    """Auto-phrase a confirmation's checkbox question from its structured fields.

    Issue athenaeum#1290: ``question``/``context`` are not among the AC's
    required confirmation fields (only raiser/repo/issue_ref/narrowed_scope/
    implemented_behavior/alternative/timestamp are) — but the block grammar
    (shared with plain questions) needs SOME checkbox text and description.
    Used by :func:`raise_pending_question` whenever a confirmation raise
    omits ``question`` — so a caller across the MCP tool AND the CLI can
    supply just the structured fields and get a sensibly-phrased block for
    free, mirroring how :func:`athenaeum.decisions._merge_question` phrases
    a merge proposal from its own structured fields.
    """
    return (
        f'Confirm: implemented "{implemented_behavior}" instead of '
        f'"{alternative}" on {repo}#{issue_ref}?'
    )


def default_confirmation_context(
    *,
    raiser: str,
    repo: str,
    issue_ref: str,
    narrowed_scope: str,
    implemented_behavior: str,
    alternative: str,
) -> str:
    """Auto-phrase a confirmation's standalone context from its structured fields.

    See :func:`default_confirmation_question` — same rationale, for the
    ``**Description**:`` field instead of the checkbox line.
    """
    return (
        f"Raised by {raiser} on {repo}#{issue_ref}. Narrowed scope: "
        f"{narrowed_scope}. Implemented instead: {implemented_behavior}. "
        f"Alternative not taken: {alternative}."
    )


def raise_pending_question(
    pending_path: Path,
    question: str,
    context: str,
    *,
    entity: str = "",
    source: str = "",
    now: datetime | None = None,
    kind: str = "question",
    raiser: str = "",
    repo: str = "",
    issue_ref: str = "",
    narrowed_scope: str = "",
    implemented_behavior: str = "",
    alternative: str = "",
) -> dict:
    """Append a NEW agent-raised block to ``_pending_questions.md`` (athenaeum#912).

    Unlike every other writer of this file (:func:`tier4_escalate`, the sole
    detector-side writer), this is called directly from the ``raise_decision``
    MCP tool at agent request — so, unlike a detector item, there is no
    upstream claim-pair or contradiction to describe. ``context`` fills that
    role: it becomes the block's ``**Description**:`` field, which is exactly
    what a human reading ``list_pending_decisions`` on a LATER, DIFFERENT
    session sees — the whole point is that the item must be answerable
    without the originating session ever having existed. Validation refuses
    to accept a raise with no context for that reason: a question with no
    standalone context reproduces the "flag with no forcing function" failure
    this issue exists to close, just moved one layer down.

    Args:
        pending_path: Path to ``_pending_questions.md``.
        question: The question a human should answer. Rejected if empty or
            all-whitespace — UNLESS ``kind="confirmation"``, where an empty
            value is auto-phrased from ``implemented_behavior``/
            ``alternative``/``repo``/``issue_ref`` (issue athenaeum#1290:
            ``question`` is not one of a confirmation's required fields).
        context: Standalone context — what a human needs to answer this
            WITHOUT the originating session. Rejected if empty or
            all-whitespace; there is no default, deliberately (see above) —
            UNLESS ``kind="confirmation"``, same auto-phrasing exception as
            ``question``.
        entity: Optional short human-readable label for the header's
            ``Entity: "..."`` field. Defaults to a generic
            ``"(agent-raised decision)"`` when omitted (``kind="question"``)
            or ``"(confirmation: <repo>#<issue_ref>)"`` (``kind=
            "confirmation"``) — this is cosmetic only; the machine-readable
            provenance signal is the ``**Raised by**:`` line, not this
            label.
        source: Optional free-text provenance ref for the header's
            ``(from ...)`` field (mirrors a detector item's originating raw
            file). Defaults to the literal ``"agent-raised"`` when omitted.
            Because this is not generally a readable wiki-page path, a
            RESTRICTED ``caller_audience`` (issue athenaeum#538) will not see this
            item via ``list_pending_decisions`` unless the supplied
            ``source`` happens to resolve to a page it is authorized to
            read — fail-closed, same as every other decision-queue item.
        now: Injectable clock for tests. Defaults to the real UTC time.
        kind: ``"question"`` (default, unchanged behaviour) or
            ``"confirmation"`` (issue athenaeum#1290) — an agent-raised
            "implemented X without Y, confirm?" flag. On ``"confirmation"``
            every one of ``raiser``/``repo``/``issue_ref``/``narrowed_scope``/
            ``implemented_behavior``/``alternative`` below is REQUIRED (each
            rejected if empty/all-whitespace, same fail-closed posture as
            ``question``/``context`` above); an unrecognized ``kind`` is
            rejected too. Ignored (no validation, nothing written) when
            ``kind="question"`` — passing them is simply a no-op, so an
            existing ``kind="question"`` caller is entirely unaffected by
            this parameter's existence.
        raiser: Who/what narrowed scope (an agent name, a lane id, ...).
            Confirmation-only.
        repo: The ``owner/repo`` the narrowing happened in. Confirmation-only.
        issue_ref: The issue or PR number/reference the narrowing relates to.
            Confirmation-only.
        narrowed_scope: What was narrowed — the scope the agent DIDN'T cover.
            Confirmation-only.
        implemented_behavior: What the agent actually built instead.
            Confirmation-only.
        alternative: The road not taken — what a human might have wanted
            instead. Confirmation-only.

    Returns:
        A dict with ``ok`` (bool), ``error_code`` (``"invalid_question"`` |
        ``"missing_context"`` | ``"invalid_kind"`` |
        ``"missing_confirmation_field"`` | ``None``), ``message`` (str),
        ``decision_id`` (the id ``list_pending_questions`` /
        ``resolve_question`` will use for this item, computed the same way
        :func:`_make_id` computes it for any other block — ``None`` on
        failure), and ``raw_block`` (the rendered block text, ``None`` on
        failure). ``block`` / ``error`` are legacy-shaped aliases (mirroring
        :func:`resolve_by_id`) for ``raw_block`` / ``message``-on-failure
        respectively. Never raises — every failure mode is a structured
        refusal, matching every other mutating MCP-facing helper in this
        module.
    """
    if kind not in ("question", "confirmation"):
        msg = f'kind must be "question" or "confirmation", got {kind!r}'
        return {
            "ok": False,
            "error_code": "invalid_kind",
            "message": msg,
            "decision_id": None,
            "raw_block": None,
            "block": None,
            "error": msg,
        }

    confirmation_values = {
        "raiser": raiser,
        "repo": repo,
        "issue_ref": issue_ref,
        "narrowed_scope": narrowed_scope,
        "implemented_behavior": implemented_behavior,
        "alternative": alternative,
    }
    if kind == "confirmation":
        for field_name, label in _REQUIRED_CONFIRMATION_FIELDS:
            if not confirmation_values[field_name].strip():
                msg = (
                    f"{label} must be non-empty for a confirmation raise "
                    "(issue athenaeum#1290) — every one of raiser/repo/"
                    "issue_ref/narrowed_scope/implemented_behavior/"
                    "alternative is required so a human reading this on a "
                    "LATER, DIFFERENT session has everything needed to "
                    "confirm without the originating session"
                )
                return {
                    "ok": False,
                    "error_code": "missing_confirmation_field",
                    "message": msg,
                    "decision_id": None,
                    "raw_block": None,
                    "block": None,
                    "error": msg,
                }

    q = question.strip()
    ctx_arg = context.strip()
    if kind == "confirmation":
        # Issue athenaeum#1290: question/context are NOT among the AC's
        # required confirmation fields — auto-phrase them from the
        # structured fields (already validated non-empty above) rather than
        # forcing a caller to restate the same story twice.
        if not q:
            q = default_confirmation_question(
                repo=repo,
                issue_ref=issue_ref,
                implemented_behavior=implemented_behavior,
                alternative=alternative,
            )
        if not ctx_arg:
            ctx_arg = default_confirmation_context(
                raiser=raiser,
                repo=repo,
                issue_ref=issue_ref,
                narrowed_scope=narrowed_scope,
                implemented_behavior=implemented_behavior,
                alternative=alternative,
            )

    if not q:
        msg = "question must be non-empty"
        return {
            "ok": False,
            "error_code": "invalid_question",
            "message": msg,
            "decision_id": None,
            "raw_block": None,
            "block": None,
            "error": msg,
        }
    ctx = ctx_arg
    if not ctx:
        msg = (
            "context must be non-empty — a human answering this on a later, "
            "different session needs standalone context; a contextless raise "
            "is never accepted (issue athenaeum#912)"
        )
        return {
            "ok": False,
            "error_code": "missing_context",
            "message": msg,
            "decision_id": None,
            "raw_block": None,
            "block": None,
            "error": msg,
        }

    when = now or datetime.now(timezone.utc)
    created_at = when.strftime("%Y-%m-%d")
    if kind == "confirmation":
        default_entity = f"(confirmation: {repo.strip()}#{issue_ref.strip()})"
    else:
        default_entity = "(agent-raised decision)"
    ent = entity.strip() or default_entity
    ref = source.strip() or "agent-raised"
    # Same escaping tier4_escalate applies to a header entity name, so an
    # entity containing a literal `"` or `\` round-trips through the header
    # grammar (`_HEADER_RE`) instead of corrupting it.
    escaped_entity = ent.replace("\\", "\\\\").replace('"', '\\"')
    header = f'## [{created_at}] Entity: "{escaped_entity}" (from {ref})'
    lines = [header, f"- [ ] {q}", "", f"**Description**: {ctx}"]
    if kind == "confirmation":
        raised_at_iso = now_iso(when)
        lines += [
            f"{_DECISION_KIND_PREFIX} {_DECISION_KIND_CONFIRMATION}",
            f"{_RAISER_PREFIX} {raiser.strip()}",
            f"{_REPO_PREFIX} {repo.strip()}",
            f"{_ISSUE_REF_PREFIX} {issue_ref.strip()}",
            f"{_NARROWED_SCOPE_PREFIX} {narrowed_scope.strip()}",
            f"{_IMPLEMENTED_BEHAVIOR_PREFIX} {implemented_behavior.strip()}",
            f"{_ALTERNATIVE_PREFIX} {alternative.strip()}",
            f"{_RAISED_AT_PREFIX} {raised_at_iso}",
        ]
    lines.append(f"{_RAISED_BY_PREFIX} {_RAISED_BY_AGENT}")
    block = ("\n".join(lines)).rstrip() + "\n"

    # Computed the exact same way `_make_id` computes an id when the block
    # is later re-parsed off disk, from the same (header, question) pair —
    # so the id returned here is valid immediately for `resolve_question`
    # without a round-trip read.
    decision_id = _make_id(header, q)

    existing_text = (
        pending_path.read_text(encoding="utf-8") if pending_path.exists() else ""
    )
    if existing_text.strip():
        new_content = existing_text.rstrip() + "\n\n---\n\n" + block
    else:
        new_content = "# Pending Questions\n\n" + block
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(pending_path, new_content)

    log.info(
        "raise_pending_question: appended agent-raised block id=%s to %s",
        decision_id,
        pending_path,
    )
    return {
        "ok": True,
        "error_code": None,
        "message": "ok",
        "decision_id": decision_id,
        "raw_block": block,
        # legacy-shaped aliases:
        "block": block,
        "error": None,
    }


def resolve_by_id(pending_path: Path, question_id: str, answer: str) -> dict:
    """Locate a block by id, flip ``[ ]`` -> ``[x]``, append the answer body.

    Does NOT archive — archival happens on the next ``ingest_answers`` run
    so the write path stays small.

    Returns a dict shaped for machine inspection:

    - ``ok`` (bool): true on success, false on any error.
    - ``error_code`` (str | None): one of ``id_not_found``,
      ``already_answered``, ``file_missing``, ``invalid_answer`` when
      ``ok`` is false; ``None`` on success.
    - ``message`` (str): human-readable status / failure text.
    - ``resolved_block`` (str | None): the rewritten block on success;
      ``None`` otherwise.
    - ``block`` (str | None): legacy alias for ``resolved_block`` kept
      for backward compatibility with early callers.
    - ``error`` (str | None): legacy alias for ``message`` on failure,
      kept for backward compatibility.
    """
    if not pending_path.exists():
        msg = f"pending questions file not found: {pending_path}"
        return {
            "ok": False,
            "error_code": "file_missing",
            "message": msg,
            "resolved_block": None,
            "block": None,
            "error": msg,
        }

    text = pending_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)
    if not blocks:
        msg = "no pending question blocks in file"
        return {
            "ok": False,
            "error_code": "id_not_found",
            "message": msg,
            "resolved_block": None,
            "block": None,
            "error": msg,
        }

    new_block_text: str | None = None
    rewritten_blocks: list[str] = []

    for block_text in blocks:
        pq = _parse_block(block_text)
        if pq is None:
            rewritten_blocks.append(block_text)
            continue
        # Use ``pq.raw_block`` rather than the raw split text: for a block
        # recovered from a checkbox-less source, ``raw_block`` already
        # carries the synthesized ``- [ ]`` line, so the repair persists in
        # the rewritten file. For normal blocks the two are identical.
        if pq.id != question_id:
            rewritten_blocks.append(pq.raw_block)
            continue
        if pq.answered:
            msg = f"question {question_id} already answered"
            return {
                "ok": False,
                "error_code": "already_answered",
                "message": msg,
                "resolved_block": None,
                "block": None,
                "error": msg,
            }

        updated = _rewrite_block_as_answered(pq.raw_block, answer)
        new_block_text = updated
        rewritten_blocks.append(updated)

    if new_block_text is None:
        msg = f"question id not found: {question_id}"
        return {
            "ok": False,
            "error_code": "id_not_found",
            "message": msg,
            "resolved_block": None,
            "block": None,
            "error": msg,
        }

    primary_parts = ["# Pending Questions", *rewritten_blocks]
    primary_body = "\n\n---\n\n".join(primary_parts) + "\n"
    atomic_write_text(pending_path, primary_body)

    return {
        "ok": True,
        "error_code": None,
        "message": "ok",
        "resolved_block": new_block_text,
        "block": new_block_text,
        "error": None,
    }


def _rewrite_block_as_answered(block_text: str, answer: str) -> str:
    """Flip the checkbox on ``block_text`` and insert ``answer`` beneath it.

    Preserves all other lines (header, conflict type, description) so the
    archive trail keeps full context.
    """
    lines = block_text.splitlines()
    new_lines: list[str] = []
    answer_inserted = False

    for line in lines:
        match = _CHECKBOX_RE.match(line)
        if match and not answer_inserted:
            new_lines.append(f"- [x] {match.group('question').strip()}")
            new_lines.append("")
            for answer_line in answer.rstrip().splitlines():
                new_lines.append(answer_line)
            new_lines.append("")
            answer_inserted = True
            continue
        new_lines.append(line)

    return "\n".join(new_lines).rstrip() + "\n"
