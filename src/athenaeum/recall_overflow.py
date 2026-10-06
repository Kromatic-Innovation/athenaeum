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

Issue athenaeum#1967 (AC2) added a SECOND, distinct breadcrumb —
:func:`render_access_withheld_line` — for a different removal reason: a
restricted caller's Layer-C audience/``recallable`` drop (``athenaeum.mcp_server``'s
and ``athenaeum._cmd_query``'s own fail-closed re-check against fresh on-disk
frontmatter), never the relevance cap. The two are kept as separate
functions/templates rather than folded into one: the cap's "why" is a type
breakdown (safe to disclose — entity types are not sensitive), while an
access-withheld "why" is deliberately a single opaque reason with no type or
page-name breakdown, so the count itself cannot be used to infer which
access level or how many pages of a given type exist behind the caller's
scope. Both share this module's "single-sourced across every caller" shape so
the CLI (``athenaeum recall``) and the MCP ``recall`` tool render identical
wording for each from the exact same template.

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

#: Issue athenaeum#1967 (AC2): the access-withheld breadcrumb's packaged
#: template name, sibling to :data:`OVERFLOW_TEMPLATE_NAME` above but for a
#: different removal reason — see :func:`render_access_withheld_line`.
ACCESS_WITHHELD_TEMPLATE_NAME = "recall_access_withheld_breadcrumb.md"


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


def load_access_withheld_template() -> str:
    """Read the access-withheld breadcrumb template (issue athenaeum#1967, AC2).

    Carries one placeholder, ``{count}`` — see :func:`render_access_withheld_line`.
    Sibling to :func:`load_overflow_template`; same "load fresh, tiny file,
    never a hot loop" rationale.
    """
    resource = importlib.resources.files("athenaeum.prompts").joinpath(
        ACCESS_WITHHELD_TEMPLATE_NAME
    )
    return resource.read_text(encoding="utf-8")


def render_access_withheld_line(count: int) -> str:
    """Render the access-withheld breadcrumb line, or ``""`` when *count* is 0.

    Issue athenaeum#1967 (AC2) — the operator ruling: "When any filter removes
    results after ranking, the output states how many were withheld and
    why." This is the "why" for a RESTRICTED caller's Layer-C audience/
    ``recallable`` drop (:func:`athenaeum.models.is_page_authorized` /
    :func:`athenaeum.storage.is_recallable`, re-checked against fresh
    on-disk frontmatter by both the MCP ``recall`` tool and the CLI
    ``athenaeum recall`` command) — never the owner/default caller, who is
    authorized for everything and never reaches that branch, and never the
    relevance-cap breadcrumb :func:`render_overflow_line` already covers.

    Deliberately reports ONLY a count, never a type/page/access-level
    breakdown (contrast :func:`render_overflow_line`'s ``types`` field,
    which IS safe to disclose) — see this module's docstring for why a
    restricted caller must not be able to infer anything about what, or how
    much, sits behind their own scope beyond "something was here".

    *count* <= 0 returns ``""`` (AC2's honesty requirement: a count of 0
    must never be printed as noise) — the caller is responsible for passing
    an ACTUAL pre/post-filter delta (the number of candidate hits this same
    call's Layer-C check actually dropped), never a second guess at the
    filter.
    """
    if count <= 0:
        return ""
    template = load_access_withheld_template().strip("\n")
    return template.format(count=count)
