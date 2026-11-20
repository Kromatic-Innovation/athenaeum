# SPDX-License-Identifier: Apache-2.0
"""The pure accept check for a store's ``intake`` block.

Layering: L2 (imports :mod:`athenaeum.store_access`, L2, and
:mod:`athenaeum.relevance_policy`, L2, whose tag matching it reuses).

Design: ``docs/design/federation-intake-routing.md`` §7 (a store's own policy)
and §13 slice S5. Format reference: ``docs/reference/store-declaration.md``.

The block itself is parsed and validated by :mod:`athenaeum.store_access`, which
is where the declaration's ``version`` rule lives. This module only answers one
question over the result: would the store accept a fact of this type and these
tags?

**Pure, and never shown to a model.** :func:`intake_accepts` is evaluated by
code over a type and tags the caller already has. It adds no category to any
prompt. Nothing yet calls it (applying the rules in ``decide`` and at
acceptance are later slices), so no install behaves differently.

**Fails closed.** A declaration that is present but invalid (an unparseable
``intake`` block, a ``subject`` key, a ``route`` or ``always`` verdict, an
``intake`` block in a version that predates it, an unsupported version) refuses
everything and carries its problems so the caller can report them. A store with
no declaration, or none with an ``intake`` block, accepts whatever is sent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from athenaeum.relevance_policy import RelevanceRule
from athenaeum.store_access import (
    Problem,
    StoreDeclaration,
    StoreDeclarationInvalid,
)

__all__ = ["IntakeDecision", "intake_accepts"]


@dataclass(frozen=True)
class IntakeDecision:
    """The answer. ``reason`` is one of ``no-declaration``, ``no-intake-block``,
    ``rule`` (``rule`` is the 1-based index of the matching rule), ``default``
    or ``declaration-invalid`` (``problems`` says why; the answer is a refusal).
    """

    accepted: bool
    reason: str
    rule: int | None = None
    problems: tuple[Problem, ...] = ()


def intake_accepts(
    declaration: StoreDeclaration | StoreDeclarationInvalid | None,
    type_: str | None,
    tags: Sequence[str] = (),
) -> IntakeDecision:
    """Would the store take a fact of *type_* carrying *tags*?

    The last matching rule wins; with none, the block's ``default``. A rule's
    tag covers its ``/``-separated children, matched by the relevance policy's
    own rule matcher.
    """
    if declaration is None:
        return IntakeDecision(True, "no-declaration")
    if isinstance(declaration, StoreDeclarationInvalid):
        return IntakeDecision(False, "declaration-invalid", problems=declaration.problems)
    policy = declaration.intake
    if policy is None:
        return IntakeDecision(True, "no-intake-block")
    for number in range(len(policy.rules), 0, -1):
        rule = policy.rules[number - 1]
        matcher = RelevanceRule(
            number=number, line=0, verdict=rule.verdict, type=rule.type, tag=rule.tag
        )
        if matcher.matches(type_, tuple(tags), None):
            return IntakeDecision(rule.verdict == "accept", "rule", rule=number)
    return IntakeDecision(policy.default == "accept", "default")
