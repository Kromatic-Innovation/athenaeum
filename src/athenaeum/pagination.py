# SPDX-License-Identifier: Apache-2.0
"""Canonical offset/limit pagination envelope (issue athenaeum#1431).

This is a leaf module by construction: it imports nothing from
``athenaeum`` (stdlib/``__future__``/typing only). :func:`paginate` is
needed by both :mod:`athenaeum.answers` (``list_unanswered``) and
:mod:`athenaeum.decisions` (``list_pending_decisions`` /
``list_pending_decisions_page``) — and ``decisions`` already imports from
``answers`` at module level, so putting the helper in either of those two
modules closes an import cycle the other direction. It was tried in
``decisions.py`` first and caught immediately by
``tests/test_import_graph_acyclic.py`` — that test walks BOTH top-level and
function-local (deferred) import edges, so even a function-local
``from athenaeum.decisions import paginate`` inside ``answers.py`` counted
as a real graph edge and closed the cycle. Moving the shared primitive DOWN
to this leaf, then dropping the back-edge, is the repo's established
pattern for exactly this situation (see that test's module docstring:
``intake.py``, ``vecmath.py``, ``merge_type_gate.py``, ``drain_advisor.py``).

Layering: L0 primitive (leaf). Imports only stdlib/``__future__`` — no
``athenaeum`` module at all — which is what makes it safe to import
top-level from both ``answers`` and ``decisions`` without re-opening the
cycle this module exists to dissolve.
"""

from __future__ import annotations


def paginate(items: list[dict], *, offset: int = 0, limit: int | None = None) -> dict:
    """Canonical offset/limit pagination envelope (issue athenaeum#1431).

    Returns::

        {
            "items": [...],       # items[offset:offset+limit] (or items[offset:])
            "total": <int>,       # len(items), the FULL unpaginated count
            "offset": <int>,      # the clamped offset actually applied
            "limit": <int|None>,  # the effective limit actually applied
            "next_offset": <int|None>,  # offset for the next page, or None
        }

    The single canonical rule, applied consistently everywhere this is
    called: ``offset`` is clamped to ``>= 0``; ``limit`` of ``None`` or
    ``<= 0`` means UNBOUNDED (the whole remainder from ``offset`` on);
    ``next_offset`` is ``offset + len(items)`` when more items remain past
    this page, otherwise ``None``.

    **This is the LIBRARY rule, not the MCP transport rule.** A non-positive
    ``limit`` resolving to unbounded is exactly what keeps direct callers —
    notably the ``athenaeum decisions`` CLI's ``_counts()``, which must see
    every item — working unchanged. The MCP tools (``list_pending_questions``
    / ``list_pending_decisions`` in :mod:`athenaeum.mcp_server`) are the
    transport-safety boundary this issue exists to protect, so THEY
    deliberately resolve ``limit`` to a strictly positive default (via
    :func:`athenaeum.config.resolve_decisions_page_limit`) BEFORE calling
    this helper — a caller passing ``limit=0`` at the MCP boundary must never
    get the unbounded list back, which is the exact transport failure
    (11,355,998 bytes / 8,632 items, ``Connection closed``) this issue fixes.
    This helper itself has no way to tell "an MCP caller passed 0" apart
    from "a library caller wants unbounded" -- that distinction is the
    caller's job, made once, at the one site (the MCP tools) that needs it.
    """
    total = len(items)
    offset = max(0, offset)
    effective_limit = limit if (limit is not None and limit > 0) else None
    if effective_limit is None:
        page = items[offset:]
    else:
        page = items[offset : offset + effective_limit]

    next_offset = offset + len(page) if (offset + len(page)) < total else None

    return {
        "items": page,
        "total": total,
        "offset": offset,
        "limit": effective_limit,
        "next_offset": next_offset,
    }
