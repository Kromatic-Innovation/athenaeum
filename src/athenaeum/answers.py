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
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
        log.warning(
            "Skipping block without checkbox line: %r", lines[0][:80]
        )
        print(
            f"[warn] skipping pending-question block without `- [ ]` line: "
            f"{lines[0][:80]!r}",
            file=sys.stderr,
        )
        return None

    cb_match = _CHECKBOX_RE.match(lines[checkbox_idx])
    assert cb_match is not None  # guarded above
    answered = cb_match.group("state").lower() == "x"
    question = cb_match.group("question").strip()

    conflict_type = ""
    description = ""
    answer_lines: list[str] = []

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
                    conflict_type = stripped.removeprefix(
                        "**Conflict type**:"
                    ).strip()
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
    return (
        f"{pq.raw_block}\n\n"
        f"**Archived**: {archived_at}\n"
    )


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


def ingest_answers(pending_path: Path, raw_root: Path) -> int:
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
        archived_new.append(_render_archive_block(pq, iso_ts))
        _archived_entities.append(pq.entity)
        _archived_raw_blocks.append(pq.raw_block)
        ingested += 1

    if ingested == 0:
        return 0

    # Rewrite the primary file — keep the header, keep unanswered blocks.
    primary_parts = ["# Pending Questions"]
    for pq in unanswered:
        primary_parts.append(pq.raw_block)
    primary_body = "\n\n---\n\n".join(primary_parts) + "\n"
    pending_path.write_text(primary_body, encoding="utf-8")

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
            log.info(
                "Skipping duplicate archive entry for entity=%s", entity
            )
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
                "# Answered Questions\n\n"
                + new_section
                + "\n\n---\n\n"
                + rest.lstrip()
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

    archive_path.write_text(combined, encoding="utf-8")

    log.info(
        "Ingested %d pending-question answer(s) from %s", ingested, pending_path
    )
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
        if pq.id != question_id:
            rewritten_blocks.append(block_text)
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

        updated = _rewrite_block_as_answered(block_text, answer)
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
    pending_path.write_text(primary_body, encoding="utf-8")

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
