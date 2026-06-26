# SPDX-License-Identifier: Apache-2.0
"""Origin-traced transcript verification (issue #260, slice A of #259).

The librarian must cite the ULTIMATE source of a wiki fact — the user, an
external URL, a permanent document, or (when nothing can be established) an
honest ``inferred``. It must NEVER cite the raw ``auto-memory/...`` filename.

This module gives the librarian *read-only* access to session transcripts so
it can verify a ``user-stated`` claim against what the user actually wrote.
Transcripts live under ``<projects_root>/<scope>/*.jsonl`` (Claude Code's
session log layout). ``projects_root`` is injectable so tests never touch the
real ``~/.claude``.

Resolution rules (see ``policies/auto-memory-citation.md``):

- Claim found in the transcript as a *user-authored* message
  → ``("user-stated", "<session>#turn<N>")``.
- Claim found only in a non-user message that quotes a URL
  → ``("external", "<url>")``.
- Transcript missing / rolled off, or claim unverifiable
  → ``("inferred", <best-effort session ref>)`` — never the raw filename.

This module is strictly read-only; it never writes transcripts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from athenaeum.models import DEFAULT_SOURCE_TYPE

# Default transcript home. Overridable via the ``projects_root`` parameter so
# tests inject a synthetic tree and never read the operator's real sessions.
DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

_URL_RE = re.compile(r"https?://[^\s)>\]\"']+")


def _best_effort_ref(session_id: str, turn: int | None) -> str:
    """Session-anchored fallback ref — NEVER the raw ``auto-memory`` filename.

    Used for the ``inferred`` resolution. Cites session + turn when a turn is
    known, else the bare session id. Returns ``""`` only when there is no
    session to cite.
    """
    if not session_id:
        return ""
    if turn is not None:
        return f"{session_id}#turn{turn}"
    return str(session_id)


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for tolerant substring matching."""
    return " ".join(text.split()).lower()


def _record_text(record: object) -> str:
    """Extract the plain text of a transcript record's message content.

    Tolerant of the two shapes Claude Code emits: ``message.content`` as a
    bare string, or as a list of typed blocks (``{"type": "text", "text": ...}``
    and tool blocks). Unknown shapes contribute the empty string rather than
    raising — a malformed line must not abort verification.
    """
    if not isinstance(record, dict):
        return ""
    message = record.get("message")
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = record.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                txt = block.get("text") or block.get("content")
                if isinstance(txt, str):
                    parts.append(txt)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _is_user_record(record: object) -> bool:
    """True when a transcript record is a user-authored message."""
    if not isinstance(record, dict):
        return False
    if record.get("type") == "user":
        return True
    message = record.get("message")
    if isinstance(message, dict) and message.get("role") == "user":
        return True
    return False


def _iter_session_records(scope_dir: Path, session_id: str) -> list[object]:
    """Read the ORIGINATING session's ``{session_id}.jsonl`` into record dicts.

    Issue #260 (Quine S1): the scan is restricted to the originating session's
    transcript, NOT every ``*.jsonl`` in the scope. Globbing the whole scope
    and then returning the caller-supplied ``session_id`` would let a claim
    that only appears in session B be falsely attributed to session A. One
    session = one file, so we read exactly that file.

    Malformed lines are skipped. Returns ``[]`` when the file is absent
    (transcript rolled off / missing).
    """
    jsonl = scope_dir / f"{session_id}.jsonl"
    if not jsonl.is_file():
        return []
    try:
        text = jsonl.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    records: list[object] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def verify_user_stated(
    scope: str,
    session_id: str,
    turn: int | None = None,
    claim: str = "",
    projects_root: Path | None = None,
) -> tuple[str, str]:
    """Resolve the origin-traced ``(source_type, source_ref)`` for a claim.

    Args:
        scope: The origin-scope directory name (path-hash identifier or
            ``_unscoped``) under ``projects_root``.
        session_id: The originating session id; anchors the fallback ref.
        turn: Optional turn number; included in a ``user-stated`` ref when a
            match is found, and in the ``inferred`` fallback ref when known.
        claim: The fact text to verify against the transcript. Matched as a
            whitespace-normalized, case-insensitive substring.
        projects_root: Transcript root. Defaults to ``~/.claude/projects``;
            inject a temp dir in tests.

    Returns:
        ``(source_type, source_ref)`` where ``source_type`` is one of
        :data:`athenaeum.models.SOURCE_TYPES` and ``source_ref`` is the
        ultimate reference — session+turn, URL, or (fallback) the session id.
        The ref is NEVER the raw ``auto-memory/...`` filename.
    """
    root = projects_root if projects_root is not None else DEFAULT_PROJECTS_ROOT
    fallback = _best_effort_ref(session_id, turn)

    needle = _normalize(claim)
    if not needle:
        # Nothing to verify against — honest inferred.
        return DEFAULT_SOURCE_TYPE, fallback

    records = _iter_session_records(root / scope, session_id)
    if not records:
        # Transcript missing or rolled off — honest inferred (NOT user-stated).
        return DEFAULT_SOURCE_TYPE, fallback

    external_ref: str | None = None
    for record in records:
        text = _record_text(record)
        if not text or needle not in _normalize(text):
            continue
        if _is_user_record(record):
            # The user said it → the user is the source.
            ref = f"{session_id}#turn{turn}" if turn is not None else str(session_id)
            return "user-stated", ref
        # A non-user (agent / tool) message carrying the claim. If it quotes a
        # link, remember it as an external candidate — but keep scanning in
        # case a later user message confirms the same claim (user wins).
        if external_ref is None:
            url = _URL_RE.search(text)
            if url:
                # Strip trailing sentence punctuation the regex may have
                # swept in (e.g. "...startup." → "...startup").
                external_ref = url.group(0).rstrip(".,;:!?")

    if external_ref is not None:
        return "external", external_ref

    # Claim present nowhere we can attribute, or only in agent text without a
    # link — an unverifiable leap. Honest inferred.
    return DEFAULT_SOURCE_TYPE, fallback
