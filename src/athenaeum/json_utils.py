# SPDX-License-Identifier: Apache-2.0
"""Lenient JSON extraction from LLM response text (issue #219).

Both the contradiction detector (:mod:`athenaeum.contradictions`) and the
resolver (:mod:`athenaeum.resolutions`) instruct the model to return
STRICT JSON with no markdown fence — but models periodically wrap the
object in ``` fences or surround it with prose anyway. The previous
greedy first-``{``-to-last-``}`` regex swallowed everything between the
outermost braces, so a fenced object followed by any later brace produced
``json.JSONDecodeError: Extra data`` and silently dropped that cluster's
contradiction work (38 drops in the 2026-06-11 nightly run).

:func:`extract_json_object` scans for *balanced, parseable* JSON objects
via :meth:`json.JSONDecoder.raw_decode`, which stops at each object's
true closing brace regardless of fences, prose, stray braces, or
trailing text. Fenced content is preferred over unfenced text, with a
whole-text fallback when fences yield no object, and an ambiguous
multi-object scan yields ``None`` rather than a guess (see the function
docstring for the precise contract).
Retry-on-parse-failure is deliberately out of scope for #219.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_DECODER = json.JSONDecoder()

#: A markdown code fence: line-leading ``` plus an optional language tag
#: (any case, e.g. ``json``/``JSON``), then everything up to a closing
#: line-leading ```. Both delimiters must start a line (issue #222) — an
#: inline backtick run in prose (e.g. "wrap it in ``` fences") must not
#: pair with the real fence opener and shift every subsequent block
#: boundary. Per CommonMark, fence delimiters may be indented by up to
#: three spaces (four spaces is an indented code block, not a fence).
#: ``[^\n]*`` tolerates a ``\r`` before the newline (CRLF).
_FENCE_RE = re.compile(
    r"^[ ]{0,3}```[^\n]*\n(.*?)^[ ]{0,3}```", re.DOTALL | re.MULTILINE
)


def _scan_objects(
    text: str,
    limit: int,
) -> tuple[list[dict[str, Any]], json.JSONDecodeError | None, bool]:
    """Collect up to ``limit`` balanced top-level JSON objects in ``text``.

    Anchors on each ``{`` and attempts :meth:`json.JSONDecoder.raw_decode`.
    After a successful parse the scan resumes *past* the object's closing
    brace, so nested objects are not counted as separate top-level objects.
    Returns the collected objects, the last :class:`json.JSONDecodeError`
    seen (for debug logging), and whether any parse attempt raised
    ``RecursionError`` — pathologically deep nesting is contained and
    treated as a failed parse, but flagged so callers can log it
    distinctly from "no JSON object present".
    """
    found: list[dict[str, Any]] = []
    last_err: json.JSONDecodeError | None = None
    hit_recursion = False
    idx = text.find("{")
    while idx != -1 and len(found) < limit:
        try:
            obj, end = _DECODER.raw_decode(text, idx)
        except json.JSONDecodeError as err:
            last_err = err
            idx = text.find("{", idx + 1)
            continue
        except RecursionError:
            hit_recursion = True
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            found.append(obj)
            idx = text.find("{", end)
        else:
            idx = text.find("{", idx + 1)
    return found, last_err, hit_recursion


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the JSON object deliberately embedded in ``text``, or ``None``.

    Contract, in priority order:

    1. If ``text`` contains one or more fenced code blocks (line-leading
       ``` indented at most 3 spaces per CommonMark, with or without a
       language tag, any case; inline backtick runs in prose are not
       fence delimiters), extraction is attempted from fenced content
       first — the fence marks the deliberate answer. The first fenced
       block that yields a balanced object wins outright; unfenced
       braces (e.g. example objects in surrounding prose) are ignored.
    2. If fenced blocks are present but NONE yields a balanced object
       (e.g. the model fenced a plan/diff/quote and left the answer
       object unfenced), the whole text is scanned under the same
       exactly-one rule as clauses 3-4 — the fallback never guesses
       between multiple candidates.
    3. With no fenced blocks (or via the clause-2 fallback), the whole
       text is scanned: if EXACTLY ONE balanced top-level object is
       found, it is returned. Objects nested inside a matched object are
       not counted separately. An object inside a top-level array (e.g.
       ``[{...}]``) still counts — the scan anchors on ``{`` — but bare
       scalars and brace-free arrays yield ``None``.
    4. If MULTIPLE balanced top-level objects are found by the whole-text
       scan (e.g. a prose example object preceding the real answer), the
       response is ambiguous and ``None`` is returned rather than
       guessing — callers keep their existing warning + safe-fallback
       behavior.

    Tolerates leading/trailing prose, stray ``{`` before the object,
    nested braces inside string values, and CRLF line endings. Returns
    ``None`` when no parseable object exists (including truncated output
    and pathologically deep nesting that would exhaust recursion).
    """
    fenced_blocks = _FENCE_RE.findall(text)
    if fenced_blocks:
        fence_err: json.JSONDecodeError | None = None
        fence_recursion = False
        for block in fenced_blocks:
            found, err, rec = _scan_objects(block, limit=1)
            if found:
                return found[0]
            if err is not None:
                fence_err = err
            fence_recursion = fence_recursion or rec
        if fence_err is not None:
            log.debug(
                "json_utils: no balanced object in fenced content (last "
                "decode error: %s (pos %d)); falling back to whole-text scan",
                fence_err.msg,
                fence_err.pos,
            )
        elif fence_recursion:
            log.debug(
                "json_utils: fenced content parse attempts exhausted "
                "recursion depth; falling back to whole-text scan"
            )
        else:
            log.debug(
                "json_utils: fenced content contains no JSON object; "
                "falling back to whole-text scan"
            )

    found, last_err, hit_recursion = _scan_objects(text, limit=2)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        log.debug(
            "json_utils: multiple top-level JSON objects in whole-text "
            "scan; ambiguous, returning None"
        )
        return None
    if last_err is not None:
        log.debug(
            "json_utils: no JSON object extracted; last decode error: %s (pos %d)",
            last_err.msg,
            last_err.pos,
        )
    elif hit_recursion:
        log.debug(
            "json_utils: no JSON object extracted; parse attempts "
            "exhausted recursion depth"
        )
    return None
