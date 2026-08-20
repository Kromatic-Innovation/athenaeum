# SPDX-License-Identifier: Apache-2.0
"""The ``memory_class:`` vocabulary and its ``type:`` rule map (athenaeum#424/#996).

A deliberate LEAF module: it imports nothing from athenaeum. Both the read
model (:mod:`athenaeum.schemas`, which re-exports :data:`MEMORY_CLASSES` so
every existing ``from athenaeum.schemas import MEMORY_CLASSES`` keeps working)
and the write model (:mod:`athenaeum.models`) need this vocabulary, and those
two sit on opposite sides of the config/pii/storage import cluster — housing
the constants in either one would close an import cycle across it (caught by
``tests/test_import_graph_acyclic.py``). A leaf both can depend on is the
factoring that keeps the graph acyclic.

Layering: L0 (pure data + one total function, no I/O).
"""

from __future__ import annotations

#: The 7 recognized ``memory_class:`` values (issue athenaeum#424). Deliberately does
#: NOT include ``open-question`` / ``hypothesis`` — the settled taxonomy
#: defers those rather than over-minting classes up front.
MEMORY_CLASSES: frozenset[str] = frozenset(
    {
        "fact",
        "guideline",
        "axiom",
        "reference",
        "entity",
        "decision",
        "procedure",
    }
)

#: The subset of :data:`MEMORY_CLASSES` a MACHINE may mint (issue athenaeum#996).
#: ``axiom`` is deliberately excluded: athenaeum#434 makes axiom-hood an explicit
#: human-approved promotion (``axiom_governance.warn_if_unbacked_axiom``), so
#: neither the deterministic rule map below nor the LLM classifier that fills
#: the residual may ever produce one. Enforcement, not just prompt wording —
#: :mod:`athenaeum.memory_class_backfill` filters classifier output against
#: this set.
MACHINE_ASSIGNABLE_MEMORY_CLASSES: frozenset[str] = MEMORY_CLASSES - {"axiom"}

#: The adopted deterministic ``type:`` -> ``memory_class:`` rule map (issue
#: athenaeum#972's priced plan, executed by athenaeum#996). Covers ~97% of the corpus
#: at zero LLM calls. A ``type:`` that is an intake/lifecycle marker rather
#: than an entity kind (``auto-memory``/``preference``/``feedback``/
#: ``incident``/``issue``) is deliberately ABSENT — those have no 1:1 function
#: to a class and go to the classifier residual instead of being guessed here.
TYPE_TO_MEMORY_CLASS: dict[str, str] = {
    "person": "entity",
    "company": "entity",
    "concept": "entity",
    "tool": "entity",
    "project": "entity",
    "source": "entity",
    "user": "entity",
    "reference": "reference",
    "principle": "guideline",
}


def memory_class_for_type(entity_type: object) -> str | None:
    """Return the rule-derived ``memory_class`` for *entity_type*, or ``None``.

    ``None`` means "this rule does not decide" — an unmapped/blank/non-string
    ``type:``. Callers must treat that as *leave the field absent*, never as a
    default class: a wrong epistemic class is worse than an absent one, and
    the residual is exactly what the classifier pass in
    :mod:`athenaeum.memory_class_backfill` exists to handle.
    """
    if not isinstance(entity_type, str):
        return None
    return TYPE_TO_MEMORY_CLASS.get(entity_type.strip())
