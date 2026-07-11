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
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Backfill classification (issue #328)
#
# The #328 ``repair --backfill-sources`` pass re-examines memories whose source
# was DEFAULTED to ``claude:inferred`` and re-classifies each against its origin
# transcript. Unlike :func:`verify_user_stated` (which folds agent/tool text
# into an ``external`` URL check), the backfill needs to tell three channels
# apart:
#
#   ``user-stated``    — the CLAIM appears in a genuine human message.
#   ``agent-observed`` — the CLAIM appears in a tool-result / tool-output block
#                        the agent READ in-session (file contents, command
#                        output). Grounded in a real artifact, unlike a leap.
#   ``inferred``       — transcript present, but no support found. Confirm the
#                        existing ``inferred`` label (idempotency marker).
#
# A MISSING transcript is NOT "confirm inferred" — it is ``unavailable``: we
# could not verify, so the caller SKIPS the memory (never guesses).


@dataclass(frozen=True)
class BackfillClassification:
    """Outcome of classifying a defaulted-inferred claim against its transcript.

    Attributes:
        channel: one of ``"user-stated"``, ``"agent-observed"``,
            ``"inferred"`` (transcript present, no support — confirm), or
            ``"unavailable"`` (transcript missing/rolled off — SKIP, never
            guess).
        ref: session-anchored reference (``"<session>#turn<N>"`` or
            ``"<session>"``), or ``""``. NEVER a raw ``auto-memory`` filename.
        model: the assistant-turn model id for the ``agent-observed`` channel
            (empty when unavailable or not applicable).
    """

    channel: str
    ref: str = ""
    model: str = ""


def _content_list(record: object) -> list:
    """Return a transcript record's ``message.content`` list, or ``[]``."""
    if not isinstance(record, dict):
        return []
    message = record.get("message")
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = record.get("content")
    return content if isinstance(content, list) else []


def _user_authored_text(record: object) -> str:
    """Extract ONLY genuine human-authored text from a user record.

    A tool result is delivered as a ``type: "user"`` record whose content is a
    list of ``tool_result`` blocks — that is an artifact the agent read, NOT a
    human utterance, so those blocks are EXCLUDED here (they belong to
    :func:`_tool_result_text`). A bare-string content or ``{"type": "text"}``
    blocks are the real user message.
    """
    if not _is_user_record(record):
        return ""
    assert isinstance(record, dict)
    message = record.get("message")
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = record.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in _content_list(record):
        if isinstance(block, dict):
            if block.get("type") == "tool_result":
                continue
            txt = block.get("text")
            if isinstance(txt, str):
                parts.append(txt)
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _tool_result_text(record: object) -> str:
    """Extract text from tool-result / tool-output blocks the agent read.

    Handles the two shapes Claude Code emits for a tool result: an inline
    ``{"type": "tool_result", "content": <str|list>}`` block inside the message
    content, and the top-level ``toolUseResult`` mirror (a string, or a dict
    with ``stdout``/``text``/``content``). Unknown shapes contribute nothing
    rather than raising.
    """
    parts: list[str] = []
    for block in _content_list(record):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                for sub in inner:
                    if isinstance(sub, dict):
                        t = sub.get("text")
                        if isinstance(t, str):
                            parts.append(t)
                    elif isinstance(sub, str):
                        parts.append(sub)
    if isinstance(record, dict):
        tur = record.get("toolUseResult")
        if isinstance(tur, str):
            parts.append(tur)
        elif isinstance(tur, dict):
            for key in ("stdout", "text", "content"):
                v = tur.get(key)
                if isinstance(v, str):
                    parts.append(v)
    return "\n".join(parts)


def _assistant_model(records: list[object]) -> str:
    """Return the first assistant turn's ``model`` id, or ``""`` when absent."""
    for record in records:
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if isinstance(message, dict):
            model = message.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
        model = record.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return ""


def classify_backfill_claim(
    scope: str,
    session_id: str,
    turn: int | None = None,
    claim: str = "",
    projects_root: Path | None = None,
) -> BackfillClassification:
    """Classify a defaulted-inferred claim against its origin transcript (#328).

    Reads ONLY the originating session's transcript (one session = one file,
    per issue #260 Quine S1) and matches ``claim`` as a whitespace-normalized,
    case-insensitive substring. Precedence: a genuine user message wins over a
    tool-result block; if neither matches, the claim is confirmed ``inferred``.
    A missing transcript yields ``unavailable`` — the caller must SKIP, never
    guess.

    Args:
        scope: origin-scope directory name under ``projects_root``.
        session_id: the originating session id.
        turn: optional turn number, carried into the ref when known.
        claim: the fact text to match (title/name, or first body line).
        projects_root: transcript root; defaults to ``~/.claude/projects``.
    """
    root = projects_root if projects_root is not None else DEFAULT_PROJECTS_ROOT
    ref = _best_effort_ref(session_id, turn)

    records = _iter_session_records(root / scope, session_id)
    if not records:
        # Transcript missing or rolled off — we cannot verify. SKIP (never
        # guess): this is distinct from "confirm inferred" (transcript present,
        # no support), which the caller treats as an idempotency-marker write.
        return BackfillClassification("unavailable", ref, "")

    needle = _normalize(claim)
    if not needle:
        # Nothing to match against, but the transcript exists — no support can
        # be established, so confirm the existing inferred label.
        return BackfillClassification("inferred", ref, "")

    # User-stated wins over an artifact match (a human confirmation outranks a
    # tool output carrying the same text).
    for record in records:
        text = _user_authored_text(record)
        if text and needle in _normalize(text):
            return BackfillClassification("user-stated", ref, "")

    for record in records:
        text = _tool_result_text(record)
        if text and needle in _normalize(text):
            return BackfillClassification(
                "agent-observed", ref, _assistant_model(records)
            )

    return BackfillClassification("inferred", ref, "")
