"""Lint helpers for auto-memory file construction.

Issue #183: extracted from ``librarian.py`` so the three
:class:`AutoMemoryFile` construction sites (discovery, cross-scope
similarity sweep, merge-time cluster shim) can share the helper without
:mod:`athenaeum.cross_scope` and :mod:`athenaeum.merge` having to do
function-local imports back into ``librarian.py`` (which would otherwise
create an import cycle: merge -> librarian -> merge).

This module intentionally has no athenaeum-internal imports.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("athenaeum")


def _strip_self_reference(
    name: str,
    refines: list[str],
    supersedes: list[dict[str, str]],
    fpath: Path | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Drop self-references from ``refines`` / ``supersedes`` with a WARN.

    Issue #173: a memory must never claim to refine or supersede itself —
    that is a YAML authoring mistake (most often the file's own ``name``
    accidentally pasted into its own ``refines:`` block). Silently dropping
    them keeps the resolver / merge planner from ever seeing the loop, and
    the WARNING line gives the operator a single grep target.

    Issue #181: the original lint lived inline in
    :func:`discover_auto_memory_files`. Extracted so the two other
    ``AutoMemoryFile`` construction sites (cross-scope similarity sweep,
    merge-time cluster shim) get the same lint+strip behavior.

    ``fpath`` is optional only to keep the helper trivially callable from
    tests; production call sites pass it so the WARNING names the file.
    """
    if not name:
        return refines, supersedes
    if any(r == name for r in refines):
        log.warning(
            "auto-memory %s: refines self (%r); dropping self-reference",
            fpath,
            name,
        )
        refines = [r for r in refines if r != name]
    if any(s.get("name") == name for s in supersedes):
        log.warning(
            "auto-memory %s: supersedes self (%r); dropping self-reference",
            fpath,
            name,
        )
        supersedes = [s for s in supersedes if s.get("name") != name]
    return refines, supersedes
