# SPDX-License-Identifier: Apache-2.0
"""Canonical text normalization, digests, and canonical JSON (athenaeum#2147).

Layering: L0. Standard library only, no I/O, no imports of any other
``promptwitness`` module and (per the package boundary) never of
``athenaeum``.

:func:`normalize` is byte-identical to ``athenaeum.transcript_verify.
_normalize`` by construction (``" ".join(text.split()).lower()``) — see
``tests/test_promptwitness_canon.py``'s parity test, which calls both
functions over a shared fixture corpus and asserts equality. The athenaeum
function is deliberately NOT turned into a re-export of this one: an L0/L1
athenaeum leaf must not depend on a package that athenaeum#2162 may later
extract to a separate repository, so the two implementations are kept as
independent, pinned-identical copies rather than one depending on the other.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["normalize", "digest", "canonical_json"]


def normalize(text: str) -> str:
    """Lowercase + collapse whitespace for tolerant, hash-stable comparison.

    Identical in behavior (and, per its own test, in literal source shape)
    to ``athenaeum.transcript_verify._normalize``.
    """
    return " ".join(text.split()).lower()


def digest(text: str) -> str:
    """``"sha256:<hex>"`` over *text*'s exact UTF-8 bytes.

    Callers decide whether to normalize first — this function never
    normalizes on its own, so it is equally correct for ``prompt_digest``
    (``digest(normalize(prompt))``) and ``raw_digest``
    (``digest(prompt)``, the exact bytes).
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, ``(",", ":")`` separators,
    ``ensure_ascii=False``, UTF-8 bytes.

    This is the exact byte sequence every hash in the ledger is computed
    over (``ledger.WitnessLine.line_hash``), so determinism here is load
    bearing: two processes handed the same logical object must produce the
    same bytes, independent of dict insertion order, locale, or platform.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
