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

Issue athenaeum#3033 added a THIRD line, :func:`render_unmatched_names_line`,
for a case where nothing was withheld at all: the query names something
(a person, a client) that no page in the result mentions. On the 2026-11-20
native-memory grid an abstention probe ("what is <unknown person>
responsible for?") cost the shipped arm four ``recall`` calls on average --
the full name, then the surname, then the given name -- each returning five
unrelated pages, before the agent said it did not know. The line tells the
agent, once, that the name it searched for is not on any page it was shown.
It is detected by :func:`unmatched_name_terms`, a pure check over the query
and the exact text the caller is about to return.

Layering: L1. Deliberately stdlib-only (``importlib.resources``, ``re`` and
``collections.abc``) — :mod:`athenaeum.context` documents a hard
import-weight contract for the per-turn retrieval path, and a shared helper
that dragged in :mod:`athenaeum.config` or :mod:`athenaeum.models` could not
be imported at that module's scope at all.
"""

from __future__ import annotations

import importlib.resources
import re
from collections.abc import Mapping, Sequence

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

#: Issue athenaeum#3033: the unmatched-names breadcrumb's packaged template --
#: see :func:`render_unmatched_names_line`.
UNMATCHED_NAMES_TEMPLATE_NAME = "recall_unmatched_names_breadcrumb.md"

#: A name-like query word: a letter, then letters, digits, apostrophes or
#: hyphens (``O'Neill``, ``Mary-Jane``). Unicode letters count.
_QUERY_WORD_RE = re.compile(r"[^\W\d_][\w'\u2019-]*")

#: Capitalised words that open a question or a request rather than naming
#: anything. The capital-letter test in :func:`unmatched_name_terms` already
#: excludes ordinary lowercase words; this set covers a sentence-initial
#: capital on a word that is not a name.
_NON_NAME_WORDS = frozenset(
    "what who whom whose which when where why how does did do is are was were "
    "can could should would will has have had the and for any all tell find "
    "list show give about from with into".split()
)

#: Shortest query word treated as a name. Two-letter words (``HR``, ``IT``,
#: ``Al``) are too often an acronym that a page spells out to be a reliable
#: "this name is absent" signal.
_MIN_NAME_LEN = 3


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


def unmatched_name_terms(query: str, shown_text: str) -> list[str]:
    """The name-like words of *query* that appear nowhere in *shown_text*.

    Issue athenaeum#3033. A name-like word is one that starts with a capital
    letter, is at least :data:`_MIN_NAME_LEN` characters long, and is not a
    question or request word (:data:`_NON_NAME_WORDS`). A trailing
    possessive (``'s``) is dropped. Matching is case-insensitive substring
    matching against *shown_text*, which must be the text the agent will
    actually read -- the rendered result blocks -- so the check can only
    ever say something true about what was shown.

    Returns the words in query order, de-duplicated case-insensitively;
    ``[]`` when every name-like word appears, or when the query has none
    (an all-lowercase topic query never fires).

    Measured against the stored rows of the 2026-11-20 grid (run
    44783114473): of the pull arms' 528 ``recall`` calls, this fired on 49
    of the 73 abstention calls and on none of the 455 calls in every other
    class. It does not catch an abstention about an unnamed topic
    ("parental leave policy"); see
    ``docs/measurements/abstention-cost-2026-11-21.md``.
    """
    haystack = shown_text.casefold()
    out: list[str] = []
    seen: set[str] = set()
    for word in _QUERY_WORD_RE.findall(query):
        term = word.rstrip("'\u2019-")
        for suffix in ("'s", "\u2019s"):
            if term.casefold().endswith(suffix):
                term = term[: -len(suffix)]
        folded = term.casefold()
        if len(term) < _MIN_NAME_LEN or not term[0].isupper():
            continue
        if folded in _NON_NAME_WORDS or folded in seen:
            continue
        seen.add(folded)
        if folded not in haystack:
            out.append(term)
    return out


def load_unmatched_names_template() -> str:
    """Read the unmatched-names breadcrumb template (issue athenaeum#3033).

    Carries one placeholder, ``{names}``. Same "load fresh, tiny file" shape
    as :func:`load_overflow_template`.
    """
    resource = importlib.resources.files("athenaeum.prompts").joinpath(
        UNMATCHED_NAMES_TEMPLATE_NAME
    )
    return resource.read_text(encoding="utf-8")


def render_unmatched_names_line(names: Sequence[str]) -> str:
    """Render the unmatched-names breadcrumb, or ``""`` when *names* is empty.

    Issue athenaeum#3033: one line, appended after a non-empty result list
    whose pages mention none of the names in *names* (see
    :func:`unmatched_name_terms`). Each name is double-quoted and the names
    are joined by ``" or "``, so "Yusuf Adeyemi" with both words absent
    reads ``"Yusuf" or "Adeyemi"``. The line never starts with ``-``.
    """
    if not names:
        return ""
    rendered = " or ".join(f'"{name}"' for name in names)
    template = load_unmatched_names_template().strip("\n")
    return template.format(names=rendered)
