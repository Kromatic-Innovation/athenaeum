# SPDX-License-Identifier: Apache-2.0
"""The relevance-cap overflow breadcrumb, single-sourced (issue athenaeum#1905).

One line — "memory has N more matching results (...) that were withheld by
the relevance cap — call ``recall`` to see them." — rendered from the
packaged template at ``src/athenaeum/prompts/recall_overflow_breadcrumb.md``
and appended to a surface that has just shown a capped result list.

Issue athenaeum#1783 introduced this line on two surfaces at once: the MCP
``recall`` renderer (:mod:`athenaeum.mcp_server`, in Python) and the shell
``UserPromptSubmit`` hook (``examples/claude-code/user-prompt-recall.sh``, in
awk). When issue athenaeum#1361/athenaeum#1887 made the packaged adapter
(:mod:`athenaeum.claude_code_adapter`) the shipped hook, the awk half stopped
running and the notice silently vanished from the per-turn push path —
athenaeum#1894 measured the cost as a 9.5-point ``push_breadcrumb_pull``
drop against the 2026-09-19 shell-hook reading. This module exists so the
third caller (:mod:`athenaeum.context`, the sidecar core the adapter calls)
renders the SAME line from the SAME template as the MCP surface, rather than
becoming a second Python implementation that can drift the same way the awk
one did.

Layering: L1. Deliberately stdlib-only (``importlib.resources`` and
``collections.abc``) — :mod:`athenaeum.context` documents a hard
import-weight contract for the per-turn retrieval path, and a shared helper
that dragged in :mod:`athenaeum.config` or :mod:`athenaeum.models` could not
be imported at that module's scope at all.
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Mapping

#: The packaged prompt file's name, resolved via ``importlib.resources``
#: below -- same packaged-prompt convention
#: ``athenaeum.tiers._load_name_resolution_confirm_prompt`` established
#: (``policies/prompt-text-is-content.md``: wording is content, not code, so
#: it lives in a ``.md`` file next to the caller, not a string literal here).
OVERFLOW_TEMPLATE_NAME = "recall_overflow_breadcrumb.md"


def load_overflow_template() -> str:
    """Read the overflow-breadcrumb template (issue athenaeum#1783).

    Carries two placeholders, ``{count}`` and ``{types}`` -- see
    :func:`render_overflow_line` for what each receives. Loaded fresh on
    every call (the file is tiny and this is never a hot loop relative to
    the retrieval it decorates) rather than cached at import time, matching
    :func:`athenaeum.tiers._load_name_resolution_confirm_prompt`'s own shape.
    """
    resource = importlib.resources.files("athenaeum.prompts").joinpath(OVERFLOW_TEMPLATE_NAME)
    return resource.read_text(encoding="utf-8")


def render_overflow_line(withheld_by_type: Mapping[str, int], *, at_least: bool) -> str:
    """Render the overflow breadcrumb line, or ``""`` when nothing was withheld.

    Issue athenaeum#1783's "Overflow line shape" AC: exactly one line,
    emitted only when *withheld_by_type* is non-empty (at least one
    candidate inside the fetch window was withheld by the cap). *at_least*
    marks the count as a LOWER BOUND (``"at least N"``) when the fetch
    window itself was exhausted -- there may be more withheld candidates
    this call never even fetched, so ``N`` alone would understate.

    Types are rendered ``"<count> <type>"``, joined by ``", "``, in
    ASCII-sorted type order -- the same order the shell hook's awk
    insertion sort produces, so the two surfaces cannot disagree about how
    a multi-type breakdown reads.

    The rendered line never starts with ``-`` (the template's own wording
    guarantees this; see ``src/athenaeum/prompts/recall_overflow_breadcrumb.md``)
    so it can never be misread as a hook bullet by
    ``tests/evals/test_recall_covers_grep.py``'s ``_HOOK_BULLET_RE``.
    """
    total = sum(withheld_by_type.values())
    if total == 0:
        return ""
    count_str = f"at least {total}" if at_least else str(total)
    types_str = ", ".join(f"{n} {t}" for t, n in sorted(withheld_by_type.items()))
    template = load_overflow_template().strip("\n")
    return template.format(count=count_str, types=types_str)
