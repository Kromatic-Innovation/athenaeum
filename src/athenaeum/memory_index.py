# SPDX-License-Identifier: Apache-2.0
"""Per-scope ``MEMORY.md`` index maintenance (issue athenaeum#388).

Each ``raw/auto-memory/<scope>/`` directory carries a curated ``MEMORY.md``
index — one ``- [Title](file.md) — hook`` pointer per raw member. Unlike the
compiled ``wiki/auto-*.md`` pages, ``MEMORY.md`` is loaded into **every** Claude
Code session's context for its scope, so a stale line keeps asserting a fact
long after the file it points at is gone.

Move-then-retire (:mod:`athenaeum.retire`) ``git rm``\\s a retired member but
historically never rewrote the sibling index, so every retirement left a
dangling pointer behind. This module supplies the piece that closes that
gap going forward:

- :func:`index_line_target` / :func:`rewrite_index` — the pure parsing
  primitives used *inline* by the retire pass to drop a just-retired member's
  pointer in the same commit as the deletion (prevents NEW dangling pointers).

The one-shot backfill that used to live here (``build_dangling_report`` /
``apply_prune_index``, exposed as ``athenaeum auto-memory prune-index``) was
removed once the inline fix above shipped and the pre-existing dangling
pointers it was written to sweep were cleared (athenaeum#2466): the inline
fix is sufficient to prevent new dangling pointers going forward.

This path only ever *drops* a sibling pointer whose target ``.md`` is gone;
non-pointer lines and pointers into other trees (``../wiki/x.md``, URLs) are
preserved verbatim.

**Factoring rule:** this module owns parsing/rewriting the ``MEMORY.md``
pointer-list TEXT. It does NOT decide WHEN a member is retired
(:mod:`athenaeum.retire` owns that decision and calls in here inline) and
does NOT touch any file other than a scope's ``MEMORY.md`` — a retired
member's own deletion is the caller's job.

**Layering:** L3 service. Module scope has no non-stdlib imports. Never
imports L4. Consumed inline by :mod:`athenaeum.retire` (L4, same commit as a
retirement).
"""

from __future__ import annotations

import logging
import re
from typing import Callable

log = logging.getLogger(__name__)

# The per-scope index file name (mirrors the ``MEMORY.md`` skip-name every
# intake scanner already excludes — see ``librarian._AUTO_MEMORY_SKIP_NAMES``
# and ``search._INTAKE_SKIP_NAMES``).
INDEX_FILENAME = "MEMORY.md"

# A ``MEMORY.md`` pointer line references a sibling raw member with a markdown
# link whose target is a bare ``<file>.md`` (no directory, no anchor). The
# writing convention is ``- [Title](file.md) — hook`` but we only require the
# ``](<target>.md)`` link so table rows and other shapes still parse.
_LINK_RE = re.compile(r"\]\(([^)]+?\.md)\)")


def index_line_target(line: str) -> str | None:
    """Return the bare *sibling* ``.md`` target a MEMORY.md line points at, else None.

    Only sibling pointers — a bare filename with no path separator, URL scheme,
    or anchor fragment — are returned, because those are the raw members that
    retirement removes from the same directory. A link that already points
    elsewhere (``../wiki/x.md``, ``https://…``, ``foo.md#frag``) is deliberately
    left alone: it is not a dangling sibling pointer and must never be swept.
    """
    m = _LINK_RE.search(line)
    if m is None:
        return None
    target = m.group(1).strip()
    # Sibling pointers only: reject anything with a path separator, a scheme /
    # anchor (``:`` covers ``http:`` and Windows drive refs; ``#`` an anchor).
    if "/" in target or ":" in target or "#" in target:
        return None
    return target


def rewrite_index(
    text: str, should_drop: Callable[[str], bool]
) -> tuple[str, list[str]]:
    """Drop index lines whose sibling target satisfies ``should_drop``.

    Returns ``(new_text, dropped_targets)``. A memory is one line per the
    ``MEMORY.md`` convention, so a matching line is removed whole. Non-pointer
    lines (headings, prose, blank lines) are preserved verbatim, and the file's
    line-ending shape is preserved because we split with ``keepends=True``.
    """
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    dropped: list[str] = []
    for line in lines:
        target = index_line_target(line)
        if target is not None and should_drop(target):
            dropped.append(target)
            continue
        kept.append(line)
    return "".join(kept), dropped
