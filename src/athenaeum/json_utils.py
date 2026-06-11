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

:func:`extract_json_object` instead scans for the first *balanced,
parseable* JSON object via :meth:`json.JSONDecoder.raw_decode`, which
stops at the object's true closing brace regardless of fences, prose,
stray braces, or trailing text. Retry-on-parse-failure is deliberately
out of scope for #219.
"""

from __future__ import annotations

import json
from typing import Any

_DECODER = json.JSONDecoder()


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the first JSON object found in ``text``, or ``None``.

    Tolerates markdown code fences (with or without a language tag),
    leading/trailing prose, stray ``{`` before the object, and nested
    braces inside string values. Only objects count — top-level arrays
    and scalars are ignored. Returns ``None`` when no parseable object
    exists so callers keep their existing warning + fallback behavior.
    """
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _end = _DECODER.raw_decode(text, idx)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        idx = text.find("{", idx + 1)
    return None
