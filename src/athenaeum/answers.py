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
  (newest-first, append-only, never deleted).
- Leaves unanswered ``[ ]`` blocks in place.

Re-running with no new ``[x]`` blocks is a no-op. Malformed blocks are
skipped with a warning on stderr and a log entry; the rest of the file is
still processed.

Defensive recovery: a block missing its ``- [ ]`` checkbox line (e.g. from
a stray or legacy escalation writer that didn't route through
``tier4_escalate``) is NOT silently dropped if it still carries a
``**Description**:`` line. The parser synthesizes an unchecked checkbox
from the first line of the description so the block becomes answerable, and
the repair is persisted on the next file rewrite. Only blocks with neither
a checkbox nor a description — i.e. no recoverable question — are skipped.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic

from athenaeum.atomic_io import atomic_write_text
from athenaeum.fingerprint import (
    _member_key_str,
    _pair_text_from_passages,
    extract_passages,
    normalize_side,
    record_resolution,
)

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


# Checkbox grammar — ``- [ ]`` or ``- [x]`` (case-insensitive on ``x``).
_CHECKBOX_RE = re.compile(r"^- \[(?P<state>[ xX])\]\s*(?P<question>.*)$")
# Lines we strip when extracting the user's answer body.
_META_PREFIXES = ("**Conflict type**:", "**Description**:")


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
    # Issue #198: claim-pair fingerprint embedded by tier4_escalate. Recovered
    # off the ``**Fingerprint**:`` line so resolution can persist the
    # adjudication to ``raw/_resolved_contradictions.jsonl``. Empty when the
    # block predates #198 or carried no recoverable passage pair.
    fingerprint: str = ""
    # Issue #157: entities sharing the same source-memory pair that
    # got merged into this block instead of getting their own. The
    # primary entity (header) is NOT included here. Empty by default
    # — only populated when the dedup path in tier4_escalate fires.
    also_affects: list[str] = field(default_factory=list)


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

    Blocks are separated by ``## `` headers or ``---`` dividers; the file
    leader (``# Pending Questions``, blank lines, or a stray preamble)
    is discarded. Each returned block starts with its ``## `` header.
    """
    blocks: list[str] = []
    current: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if line.startswith("## "):
            if current:
                blocks.append("\n".join(current).rstrip())
            current = [line]
        elif stripped == "---":
            if current:
                blocks.append("\n".join(current).rstrip())
                current = []
        else:
            if current:
                current.append(line)
    if current:
        blocks.append("\n".join(current).rstrip())

    return [b for b in blocks if b.startswith("## ")]


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


def _parse_block(block_text: str) -> PendingQuestion | None:
    """Parse one block. Returns ``None`` on malformed input."""
    lines = block_text.splitlines()
    if not lines:
        return None

    header_match = _HEADER_RE.match(lines[0])
    if not header_match:
        log.warning("Skipping block with malformed header: %r", lines[0][:80])
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
            # Issue #157: dedup-merge tag. Comma-separated entity names
            # that share the source-memory pair with this block's primary
            # entity. Recognized as metadata so it does NOT leak into
            # answer_lines (which would forge a phantom user answer).
            in_description = False
            payload = stripped.removeprefix("**Also affects**:").strip()
            also_affects = [name.strip() for name in payload.split(",") if name.strip()]
            continue
        if stripped.startswith("**Fingerprint**:"):
            # Issue #198: claim-pair fingerprint metadata. Recognized so it
            # does NOT leak into answer_lines (which would forge a phantom
            # user answer).
            in_description = False
            fingerprint = stripped.removeprefix("**Fingerprint**:").strip()
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


def _render_archive_block(pq: PendingQuestion, archived_at: str) -> str:
    """Render an archive entry for an answered block.

    Includes the original raw block verbatim plus a trailer noting when the
    answer was ingested. Newest-first is handled by the caller.
    """
    return f"{pq.raw_block}\n\n" f"**Archived**: {archived_at}\n"


def _render_answer_raw_file(pq: PendingQuestion, resolved_at: str) -> str:
    """Render the raw intake markdown for a resolved question."""
    body = "\n".join(pq.answer_lines).strip()
    if not body:
        body = "(no answer body provided)"

    return (
        "---\n"
        "source: pending_question_answer\n"
        f"original_source: raw/{pq.source}\n"
        f"entity: {pq.entity}\n"
        f"resolved_at: {resolved_at}\n"
        f"question: {pq.question}\n"
        "---\n\n"
        f"{body}\n"
    )


# ---------------------------------------------------------------------------
# Source write-back (issue #197)
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

# Single-source historical verdicts (#197): a HUMAN answer can ask to archive
# / deprecate / supersede a named source outright. These are NOT in develop's
# resolver ``ENACTING_ACTIONS`` (a/b-indexed delete/mark); they take the
# whole-file ``deprecated: true`` marker path via ``_mark_member_frontmatter``.
_HISTORICAL_VERDICTS: frozenset[str] = frozenset(
    ("archive", "deprecate", "supersede", "supersedes")
)

# ``**Member paths**: a, b`` — explicit source paths carried on the block.
_MEMBER_PATHS_RE = re.compile(
    r"^\s*\*\*Member paths\*\*:\s*(?P<payload>.+)$", re.MULTILINE
)
# ``Members involved: a, b`` — the detector's source-attribution line on
# auto-memory contradiction blocks (issue #210 follow-up). The refs are
# relative to the configured intake roots (default ``raw/auto-memory``), and
# the block's ``source:`` header points at a compiled wiki page rather than
# the raw memory — so this line is the only handle on the true source files.
_MEMBERS_INVOLVED_RE = re.compile(
    r"^\s*Members involved:\s*(?P<payload>.+)$", re.MULTILINE
)
# ``Passage A: <text>`` / ``Passage 1: <text>`` inside the description.
_PASSAGE_RE = re.compile(r"^\s*Passage\s+\S+:\s*(?P<text>.+)$", re.MULTILINE)


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

    Issue #210 follow-up: auto-memory contradiction blocks carry their true
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


def _writeback_source(
    pq: PendingQuestion,
    roots: list[Path],
    *,
    client: "object | None" = None,
    config: "dict | None" = None,
    usage: "object | None" = None,
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
    call is metered into it (#248): this function bumps ``api_calls`` once per
    attempted proposer call (the caller counts attempts, mirroring the #239
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

        # ``**Member paths**:`` is block metadata that the block parser routes
        # into answer_lines (it is not a recognized key). Strip it (and any
        # stray ``Passage N:`` line) so it can't masquerade as the answer body.
        answer_body = "\n".join(
            line
            for line in pq.answer_lines
            if not _MEMBER_PATHS_RE.match(line)
            and not _MEMBERS_INVOLVED_RE.match(line)
            and not _PASSAGE_RE.match(line)
        ).strip()
        if not answer_body:
            return 0

        # Resolver a/b order: pq.source is side a; ``**Member paths**:`` refs
        # are the additional members the block involves (also-affects), side b
        # onward.
        refs = [
            pq.source,
            *_extract_member_path_refs(pq.raw_block),
            *_extract_members_involved_refs(pq.raw_block),
        ]
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

        # --- Historical / archive a single named source (#197). ---
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
                # #248: count one attempt per proposer call at the call site
                # (the callee accumulates tokens but never bumps api_calls,
                # mirroring the #239 convention). Bump BEFORE the call so an
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
                            path.write_text(
                                render_frontmatter(meta) + "\n" + new_body,
                                encoding="utf-8",
                            )
                        else:
                            path.write_text(new_body, encoding="utf-8")
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
                path.write_text(
                    render_frontmatter(meta) + "\n" + new_body, encoding="utf-8"
                )
            else:
                path.write_text(new_body, encoding="utf-8")
            edited += 1
        return edited
    except Exception:  # noqa: BLE001 -- write-back must not block provenance
        log.exception("answers: source write-back failed for entity=%s", pq.entity)
        return 0


def ingest_answers(
    pending_path: Path,
    raw_root: Path,
    *,
    client: "anthropic.Anthropic | None" = None,
    config: "dict | None" = None,
) -> int:
    """Parse resolved items from ``pending_path``, write raw intake, archive.

    Walks ``_pending_questions.md``; for each ``[x]`` block writes a file
    under ``raw/answers/`` with frontmatter linking back to the original
    source, then moves the block to ``_pending_questions_archive.md``
    (newest-first, append-only). ``[ ]`` blocks are left in place.

    Idempotent: calling again with no new ``[x]`` blocks is a no-op.
    Malformed blocks emit a warning and are skipped.

    Args:
        pending_path: Path to ``_pending_questions.md``.
        raw_root: Raw intake root (answers land in ``raw_root/answers/``).
        client: Optional live Anthropic client. When provided, free-text
            answers invoke the LLM-backed proposer to generate source-file
            edits instead of falling back to annotation-only. Keyword-only;
            defaults to ``None`` so every existing caller is unaffected.
        config: Optional athenaeum config dict. Forwarded to the resolver
            for model selection. Keyword-only; defaults to ``None``.

    Returns:
        Count of answers ingested on this run.
    """
    if not pending_path.exists():
        return 0

    text = pending_path.read_text(encoding="utf-8")
    blocks = _split_blocks(text)
    if not blocks:
        return 0

    answers_dir = raw_root / "answers"
    # Issue #197: roots under which a block's source ref(s) are resolved for
    # write-back. raw_root first (auto-memory sources live there), then the
    # wiki root (``pending_path.parent``) for wiki-side memories.
    #
    # Issue #210 follow-up: the detector attributes auto-memory contradictions
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

    # #248: meter the LLM calls this run makes (the free-text proposer is the
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

    now = datetime.now(timezone.utc)
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    filename_ts = now.strftime("%Y%m%dT%H%M%SZ")

    for block_text in blocks:
        pq = _parse_block(block_text)
        if pq is None:
            # Malformed — preserve as-is in the primary file so the human
            # can see + fix it. Do not archive.
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

        # Write raw intake file — retry with a counter if the slug collides
        # within the same second (two answers resolved in the same run).
        answers_dir.mkdir(parents=True, exist_ok=True)
        slug = _slugify(pq.entity)
        candidate = answers_dir / f"{filename_ts}-{slug}.md"
        counter = 1
        while candidate.exists():
            candidate = answers_dir / f"{filename_ts}-{slug}-{counter}.md"
            counter += 1
        candidate.write_text(_render_answer_raw_file(pq, iso_ts), encoding="utf-8")

        # Issue #197/#210: apply the ratified verdict to the source memory
        # file(s). The provenance doc above is the audit trail and is ALWAYS
        # written first; this write-back is what stops the contradiction from
        # regenerating on the next wiki build. Failures are swallowed inside
        # _writeback_source so the audit/archive path is never blocked.
        # Issue #210: thread client/config so free-text answers can use the
        # LLM-backed proposer to enact source edits instead of annotating only.
        edited = _writeback_source(
            pq, source_roots, client=client, config=config, usage=usage
        )
        if edited:
            log.info(
                "answers: wrote ratified verdict back to %d source file(s) "
                "for entity=%s",
                edited,
                pq.entity,
            )

        # Issue #198: persist the human resolution to the fingerprint cache so
        # the settled claim-pair stops re-escalating on future pages.
        # resolved_by="human" is load-bearing for sibling #199 (only human
        # verdicts auto-apply there). No-op when the block carried no
        # fingerprint (pre-#198 block or no recoverable passage pair).
        if pq.fingerprint:
            verdict, _ = _parse_verdict("\n".join(pq.answer_lines))
            # Issue #199: persist per-side anchors in the verdict's ORIGINAL
            # a/b orientation so the auto-apply lane can reconcile a swapped
            # re-surfacing of the same (order-independent-fingerprinted) pair.
            # side a = Passage 1 = pq.source = member_paths[0]; side b =
            # Passage 2. Normalized with the SAME helper the fingerprint uses.
            # Issue #216 (follow-up to #211): derive side passages from the
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
            # Issue #211 + #216: persist member_key and pair_text so the
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

        archived_new.append(_render_archive_block(pq, iso_ts))
        _archived_entities.append(pq.entity)
        _archived_raw_blocks.append(pq.raw_block)
        ingested += 1

    # #248: one cost summary per run that made >= 1 API call (the free-text
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

    if ingested == 0:
        return 0

    # Rewrite the primary file — keep the header, keep unanswered blocks.
    primary_parts = ["# Pending Questions"]
    for pq in unanswered:
        primary_parts.append(pq.raw_block)
    primary_body = "\n\n---\n\n".join(primary_parts) + "\n"
    atomic_write_text(pending_path, primary_body)

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


def list_unanswered(pending_path: Path) -> list[dict]:
    """Return unanswered pending questions as dicts suitable for MCP output.

    Each dict has: ``id``, ``entity``, ``source``, ``question``,
    ``conflict_type``, ``description``, ``created_at``.
    """
    return [
        {
            "id": pq.id,
            "entity": pq.entity,
            "source": pq.source,
            "question": pq.question,
            "conflict_type": pq.conflict_type,
            "description": pq.description,
            "created_at": pq.created_at,
        }
        for pq in parse_pending_questions(pending_path)
        if not pq.answered
    ]


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
