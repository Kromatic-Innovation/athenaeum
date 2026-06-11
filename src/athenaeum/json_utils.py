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
trailing text. Fenced content is preferred over unfenced text, and an
ambiguous unfenced multi-object response yields ``None`` rather than a
guess (see the function docstring for the precise contract).
Retry-on-parse-failure is deliberately out of scope for #219.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_DECODER = json.JSONDecoder()

#: A markdown code fence: ``` plus an optional language tag (any case,
#: e.g. ``json``/``JSON``), then everything up to the closing ```.
#: ``[^\n]*`` tolerates a ``\r`` before the newline (CRLF output).
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def _scan_objects(
    text: str,
    limit: int,
) -> tuple[list[dict[str, Any]], json.JSONDecodeError | None]:
    """Collect up to ``limit`` balanced top-level JSON objects in ``text``.

    Anchors on each ``{`` and attempts :meth:`json.JSONDecoder.raw_decode`.
    After a successful parse the scan resumes *past* the object's closing
    brace, so nested objects are not counted as separate top-level objects.
    Returns the collected objects plus the last :class:`json.JSONDecodeError`
    seen (for debug logging); ``RecursionError`` from pathologically deep
    nesting is contained and treated as a failed parse.
    """
    found: list[dict[str, Any]] = []
    last_err: json.JSONDecodeError | None = None
    idx = text.find("{")
    while idx != -1 and len(found) < limit:
        try:
            obj, end = _DECODER.raw_decode(text, idx)
        except json.JSONDecodeError as err:
            last_err = err
            idx = text.find("{", idx + 1)
            continue
        except RecursionError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            found.append(obj)
            idx = text.find("{", end)
        else:
            idx = text.find("{", idx + 1)
    return found, last_err


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the JSON object deliberately embedded in ``text``, or ``None``.

    Contract, in priority order:

    1. If ``text`` contains one or more fenced code blocks (``` with or
       without a language tag, any case), extraction is attempted ONLY
       from fenced content — the fence marks the deliberate answer. The
       first fenced block that yields a balanced object wins; unfenced
       braces (e.g. example objects in surrounding prose) are ignored.
    2. Otherwise the whole text is scanned: if EXACTLY ONE balanced
       top-level object is found, it is returned. Objects nested inside a
       matched object are not counted separately. An object inside a
       top-level array (e.g. ``[{...}]``) still counts — the scan anchors
       on ``{`` — but bare scalars and brace-free arrays yield ``None``.
    3. If MULTIPLE unfenced balanced top-level objects are found (e.g. a
       prose example object preceding the real answer), the response is
       ambiguous and ``None`` is returned rather than guessing — callers
       keep their existing warning + safe-fallback behavior.

    Tolerates leading/trailing prose, stray ``{`` before the object,
    nested braces inside string values, and CRLF line endings. Returns
    ``None`` when no parseable object exists (including truncated output
    and pathologically deep nesting that would exhaust recursion).
    """
    fenced_blocks = _FENCE_RE.findall(text)
    if fenced_blocks:
        last_err: json.JSONDecodeError | None = None
        for block in fenced_blocks:
            found, err = _scan_objects(block, limit=1)
            if found:
                return found[0]
            if err is not None:
                last_err = err
        if last_err is not None:
            log.debug(
                "json_utils: no JSON object in fenced content; "
                "last decode error: %s (pos %d)",
                last_err.msg,
                last_err.pos,
            )
        return None

    found, last_err = _scan_objects(text, limit=2)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        log.debug(
            "json_utils: multiple top-level JSON objects in unfenced text; "
            "ambiguous, returning None"
        )
        return None
    if last_err is not None:
        log.debug(
            "json_utils: no JSON object extracted; last decode error: %s (pos %d)",
            last_err.msg,
            last_err.pos,
        )
    return None
