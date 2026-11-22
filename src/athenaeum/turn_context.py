# SPDX-License-Identifier: Apache-2.0
"""Read the previous conversation turn for per-turn topic extraction.

Issue athenaeum#3105. The per-turn recall hook used to extract search topics
from the new user prompt alone, so a follow-up such as "close it" or "what
about the second one?" was searched on its own words. The athenaeum#3104
measurement found that giving the topic extractor the previous user message
plus the end of the assistant's last reply (arm A2, each cut to 1,500
characters) raises push coverage, ungated, in one extractor call per turn.
This module reads those two pieces from the session transcript whose path
the Claude Code ``UserPromptSubmit`` payload carries as ``transcript_path``.

**Contract:** :func:`read_previous_turn` returns a :class:`PreviousTurn`, or
``None`` when there is nothing usable (no path, a missing or unreadable file,
no earlier turn in the file, malformed lines only). It never raises: a
missing or unreadable transcript means prompt-only extraction, never a
blocked prompt.

**Bounded cost.** Only the last :data:`TAIL_BYTES` of the transcript are
read, so the time spent here does not grow with the length of the
conversation. A previous user message that lies before that window is
simply not found; the assistant reply that follows it still is.

**Layering:** L2 service on the recall hot path. Module scope imports only
the standard library and :mod:`athenaeum.config` (for the switch); the
transcript record predicates come from :mod:`athenaeum.transcript_verify`
(L1), imported inside :func:`read_previous_turn` so a plain import of this
module stays light.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_push_context_carryover

#: Characters kept from each piece of the previous turn (the athenaeum#3104
#: A2 arm: "each cut to 1,500 characters").
PREVIOUS_TURN_CHARS = 1500

#: Bytes read from the end of the transcript. A turn whose tool results are
#: larger than this may lose its user message to the window; the cost of the
#: read stays constant however long the session is.
TAIL_BYTES = 512 * 1024


@dataclass(frozen=True)
class PreviousTurn:
    """The previous turn, as given to the topic extractor.

    ``user_message`` is the start of the previous human-typed message (empty
    when it lies outside the read window). ``assistant_reply`` is the end of
    the assistant's text reply after it (empty when there was none). At least
    one of the two is non-empty.
    """

    user_message: str
    assistant_reply: str


def _read_tail_lines(path: Path) -> list[str]:
    """The complete lines in the last :data:`TAIL_BYTES` of *path*."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - TAIL_BYTES)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        # The first line is cut by the window; drop it rather than misparse it.
        lines = lines[1:]
    return lines


def _assistant_text(record: dict[str, Any]) -> str:
    """The ``text`` blocks of an assistant record (tool calls and thinking
    blocks are not part of the reply the user read)."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts)


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


def read_previous_turn(transcript_path: str | Path | None, prompt: str = "") -> PreviousTurn | None:
    """Return the turn before *prompt* in the transcript at *transcript_path*.

    Walks the transcript backwards: assistant text is collected until the
    previous human-typed message is reached. Sub-agent (sidechain) records,
    tool results and harness-injected records are skipped (the human-record
    test is :func:`athenaeum.transcript_verify._is_human_record`). If the
    newest human message is *prompt* itself, with no assistant reply after it
    yet, it is the current turn and is skipped.

    Never raises; see the module docstring.
    """
    if not transcript_path:
        return None
    try:
        from athenaeum.transcript_verify import _is_human_record, _non_tool_result_text

        lines = _read_tail_lines(Path(transcript_path).expanduser())
        reply_parts: list[str] = []
        user_message = ""
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("isSidechain") is True:
                continue
            if record.get("type") == "assistant":
                text = _assistant_text(record).strip()
                if text:
                    reply_parts.append(text)
                continue
            if _is_human_record(record):
                text = _non_tool_result_text(record).strip()
                if not reply_parts and prompt and _same_text(text, prompt):
                    continue
                user_message = text
                break
        assistant_reply = "\n".join(reversed(reply_parts))
    except Exception:  # noqa: BLE001 - an unreadable transcript means prompt-only, never a blocked prompt
        return None
    user_message = user_message[:PREVIOUS_TURN_CHARS]
    assistant_reply = assistant_reply[-PREVIOUS_TURN_CHARS:]
    if not user_message and not assistant_reply:
        return None
    return PreviousTurn(user_message=user_message, assistant_reply=assistant_reply)


def previous_turn_for_hook(
    transcript_path: str | Path | None,
    prompt: str,
    config: dict[str, Any] | None = None,
) -> PreviousTurn | None:
    """:func:`read_previous_turn`, behind the
    :func:`athenaeum.config.resolve_push_context_carryover` switch: ``None``
    when the switch is off. Never raises."""
    try:
        if not resolve_push_context_carryover(config):
            return None
    except Exception:  # noqa: BLE001 - a bad switch value must not block a prompt
        return None
    return read_previous_turn(transcript_path, prompt)
