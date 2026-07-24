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
from typing import Any

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


# --- Memory-taxonomy lint (issue #424) ---
#
# ``memory_class:`` validity (unknown non-empty value -> flagged) is
# enforced at the pydantic boundary: schemas.py's ``WikiBase`` field
# validator emits a ``UserWarning`` via ``validate_wiki_meta``, mirroring
# the #93 ``KNOWN_TYPES`` precedent — that stays the single source of truth
# for "is this value one of the 7 recognized classes" so this module does
# not need to import/duplicate ``MEMORY_CLASSES``.
#
# This module's complementary job is the "untyped" lint surface: reporting,
# across a batch of frontmatter dicts (e.g. a wiki-tree scan), which pages
# carry NO ``memory_class`` at all. Absence is tolerated by validation
# (legacy/untyped pages must not break) but should not silently disappear
# from an operator's lint output either.


def lint_untyped_memory_class(
    meta: dict[str, Any], fpath: Path | None = None
) -> str | None:
    """Return an "untyped" lint message when ``meta`` has no ``memory_class``.

    Returns ``None`` when ``memory_class`` is present (regardless of
    validity — an invalid value is flagged separately by
    :func:`athenaeum.schemas.validate_wiki_meta`'s ``UserWarning``; this
    helper only reports the ABSENT case). ``fpath`` is optional (mirrors
    :func:`_strip_self_reference`) so the helper is trivially callable from
    tests; production call sites should pass it so the report names the
    file.
    """
    value = meta.get("memory_class")
    if value is None or value == "":
        return f"{fpath}: untyped (no memory_class)" if fpath else "untyped (no memory_class)"
    return None
