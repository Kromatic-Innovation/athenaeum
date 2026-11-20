# SPDX-License-Identifier: Apache-2.0
"""Relevance policy, classify-time enforcement (athenaeum#2647, slice 4 of athenaeum#2598).

:mod:`athenaeum.relevance_policy` enforces ``never`` rules on what a raw
DECLARES (slice 2). Most facts declare no category, so a ``never`` rule is only
enforceable once Tier-2 classify has said what a fact is about. This module is
that second, post-classify gate. Like slice 2's gate it is ONE decision both
pipeline transports call (``librarian.process_one`` and
``batch.process_batch_run``), so they cannot drift.

Two gates, both free of I/O except the escalation call:

* :func:`gate_classified_entities` -- after classify. Each extracted entity is
  matched against the policy on its classified type, tags and the
  ``category`` / ``confidence`` pair the classify prompt returns when the
  policy names a tag (see ``prompts/relevance_category.md``). A confident
  ``never`` match is dropped. A ``never`` rule the classifier is *unsure*
  about escalates to the reasoning tier (:func:`escalate_category`, which
  resolves its model through the existing ``reasoning_t2`` knob -- no new model
  knob). If the reasoning tier is also unsure, the fact is **dropped, not
  held** (operator ruling of 2026-11-14), recorded with reason
  ``relevance-unsure``.
* :func:`gate_update_actions` -- before Tier-3. An ``update`` action lands a
  fact on an EXISTING page; the page's own declared type / tags / uid are
  matched, so a fact for a ``never`` category is caught even when it never
  passed through classify (a Tier-1 name match). Deterministic, no LLM.

The explicit-remember marker (``explicit_remember: true`` on the raw,
athenaeum#2646) exempts every fact of the raw from both gates; the exemption
is logged. A failing escalation call (a transient API error) is NOT
"unsure": it propagates, the raw stays on disk and is retried, so an outage
can never turn into a deletion.

Layering: L4. Imports :mod:`athenaeum.relevance_policy` (L2), models and the
provider seam and the ``reasoning_t2`` model default in :mod:`athenaeum.config`
(not :mod:`athenaeum.reasoning_tiers`, whose import graph reaches ``batch``).
Never affects ranking.
"""

from __future__ import annotations

import importlib.resources
import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

from athenaeum._retry import with_retry
from athenaeum.config import DEFAULT_REASONING_T2_MODEL, resolve_model
from athenaeum.intake_decision import (
    CAPTURE_DROP,
    CAPTURE_KEEP,
    CAPTURE_UNSURE,
    Description,
    RuleLike,
    decide,
)
from athenaeum.models import ClassifiedEntity, EntityAction, parse_frontmatter
from athenaeum.prompt_safety import fence_untrusted
from athenaeum.provider import resolve_max_tokens, resolve_thinking, response_text
from athenaeum.relevance_policy import (
    CONFIDENCE_HIGH,
    DROP_REASON_PREFIX,
    DROP_REASON_UNSURE,
    RelevancePolicy,
    declared_facts,
    policy_categories,
    raw_is_explicit_remember,
)

log = logging.getLogger(__name__)

_EXCERPT_CHARS = 500
_ESCALATE_MAX_TOKENS = 4096


def _load_escalate_system() -> str:
    resource = importlib.resources.files("athenaeum.prompts").joinpath(
        "relevance_escalate_system.md"
    )
    return resource.read_text(encoding="utf-8")


#: System prompt of the reasoning-tier category call. Registered in
#: ``athenaeum.prompt_registry`` as ``relevance_classify.relevance_escalate_system``.
RELEVANCE_ESCALATE_SYSTEM = _load_escalate_system()


@dataclass(frozen=True)
class CategoryDrop:
    """One fact this module dropped. Duck-compatible with ``RelevanceDrop``.

    The entity loop and the batch transport read only ``reason`` and
    ``excerpt``, so a drop here becomes a drop-ledger row exactly like a slice-2
    drop. ``existing_uid`` lets the caller settle a person-hint decision.
    """

    reason: str
    excerpt: str
    name: str = ""
    existing_uid: str | None = None


@dataclass
class CategoryGateOutcome:
    """Result of :func:`gate_classified_entities`."""

    kept: list[ClassifiedEntity] = field(default_factory=list)
    drops: list[CategoryDrop] = field(default_factory=list)
    #: Entities sent to the reasoning tier (whatever it answered).
    escalated: int = 0
    #: Entities a ``never`` rule covered but the explicit-remember marker kept.
    overrides: int = 0


def _label(rule: RuleLike) -> str:
    return f"{rule.describe()} (rule {rule.number}, line {rule.line})"


def _excerpt(name: str, text: str) -> str:
    return f"{name}: {text}".strip()[:_EXCERPT_CHARS]


def build_escalation_request(
    entity: ClassifiedEntity,
    categories: list[str],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Messages API kwargs for the reasoning-tier category call.

    The model resolves through the existing ``reasoning_t2`` knob (env
    ``ATHENAEUM_REASONING_T2_MODEL`` / yaml ``models.reasoning_t2``); no new
    knob exists. :func:`athenaeum.reasoning_tiers.get_t2_model` is not used
    because it warns that the knob is inert while T2 merge screening is off,
    which is not true of this caller.
    """
    listing = "\n".join(f"- {c}" for c in categories)
    user_msg = (
        "## Fact\n"
        + fence_untrusted(
            f"{entity.name}\n{entity.observations}", tag="user_document", max_chars=4000
        )
        + f"\n\n## Categories\n{listing}\n\n## Instructions\nReturn ONLY the JSON object."
    )
    return {
        "model": resolve_model(
            "reasoning_t2",
            "ATHENAEUM_REASONING_T2_MODEL",
            DEFAULT_REASONING_T2_MODEL,
            config,
        ),
        "max_tokens": resolve_max_tokens(
            "reasoning_t2",
            "ATHENAEUM_REASONING_T2_MAX_TOKENS",
            _ESCALATE_MAX_TOKENS,
            config,
        ),
        "thinking": resolve_thinking(
            "reasoning_t2", "ATHENAEUM_REASONING_T2_THINKING", "adaptive", config
        ),
        "system": RELEVANCE_ESCALATE_SYSTEM,
        "messages": [{"role": "user", "content": user_msg}],
    }


def parse_escalation_response(text: str, categories: list[str]) -> tuple[str | None, bool]:
    """``(category, sure)`` from the reasoning tier's answer.

    Anything unparseable, a category outside *categories*, or a confidence
    other than ``high`` is *unsure* (``sure`` False) -- the caller then fails
    closed. ``(None, True)`` is a confident "none of the categories applies".
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None, False
    try:
        obj = json.loads(match.group())
    except ValueError:
        return None, False
    if not isinstance(obj, dict):
        return None, False
    conf = obj.get("confidence")
    sure = isinstance(conf, str) and conf.strip().lower() == CONFIDENCE_HIGH
    cat = obj.get("category")
    if cat is None:
        return None, sure
    if isinstance(cat, str) and cat.strip() in categories:
        return cat.strip(), sure
    return None, False


def escalate_category(
    entity: ClassifiedEntity,
    categories: list[str],
    *,
    client: Any,
    usage: Any | None = None,
    config: dict[str, Any] | None = None,
    raw_ref: str = "",
) -> tuple[str | None, bool]:
    """Ask the reasoning tier which category *entity* belongs to.

    Returns ``(category, sure)``; see :func:`parse_escalation_response`. A
    call that raises propagates (the raw is retried), it is never "unsure".
    """
    params = build_escalation_request(entity, categories, config)
    response = with_retry(
        lambda: client.messages.create(**params),
        description=f"relevance_escalate {raw_ref}",
    )
    if usage is not None and hasattr(response, "usage"):
        from athenaeum.models import cache_usage_counts

        i, o, cc, cr = cache_usage_counts(response)
        usage.add(i, o, cc, cr, model=params["model"], knob="reasoning_t2")
    try:
        text = response_text(response)
    except ValueError:  # e.g. truncated before any text block
        return None, False
    return parse_escalation_response(text, categories)


def gate_classified_entities(
    classified: list[ClassifiedEntity],
    policy: RelevancePolicy | None,
    *,
    raw_ref: str,
    raw_content: str,
    jsonl: bool = False,
    client: Any,
    usage: Any | None = None,
    config: dict[str, Any] | None = None,
) -> CategoryGateOutcome:
    """Enforce ``never`` rules on classified entities (see the module docstring).

    ``None`` / empty policy returns every entity untouched (a deployment with
    no policy is byte-identical to before this gate existed).
    """
    if policy is None or not policy.rules:
        return CategoryGateOutcome(kept=list(classified))
    explicit = raw_is_explicit_remember(raw_content, jsonl=jsonl)
    categories = policy_categories(policy)
    out = CategoryGateOutcome()
    for c in classified:
        # A person-hint item (athenaeum#2654) carries category + confidence like
        # any other, so it can be unsure and escalate; its subject is the
        # hinted page's uid (the owner's uid for an owner fact).
        decision = decide(
            Description(
                type=c.entity_type or None,
                tags=tuple(c.tags),
                subject=c.existing_uid,
                category=c.category,
                confidence=c.category_confidence,
                explicit=explicit,
                classified=True,
            ),
            policy,
        )
        if decision.overridden is not None:
            log.info(
                "relevance-explicit-override ref=%s name=%r rule=%s",
                raw_ref,
                c.name,
                _label(decision.overridden),
            )
            out.overrides += 1
            out.kept.append(c)
            continue
        rule = decision.rule
        if decision.capture == CAPTURE_KEEP or rule is None:
            out.kept.append(c)
            continue
        prefix = DROP_REASON_PREFIX
        if decision.capture == CAPTURE_UNSURE:
            out.escalated += 1
            category, sure = escalate_category(
                c, categories, client=client, usage=usage, config=config, raw_ref=raw_ref
            )
            if not sure:
                # Fail closed: the reasoning tier is unsure too. Dropped, not held.
                prefix = DROP_REASON_UNSURE
            else:
                resolved = replace(c, category=category, category_confidence=CONFIDENCE_HIGH)
                second = decide(
                    Description(
                        type=resolved.entity_type or None,
                        tags=tuple(resolved.tags),
                        subject=resolved.existing_uid,
                        category=category,
                        confidence=CONFIDENCE_HIGH,
                        classified=True,
                    ),
                    policy,
                )
                if second.capture != CAPTURE_DROP or second.rule is None:
                    out.kept.append(c)
                    continue
                rule = second.rule
        out.drops.append(
            CategoryDrop(
                reason=f"{prefix}: {_label(rule)}",
                excerpt=_excerpt(c.name, c.observations),
                name=c.name,
                existing_uid=c.existing_uid,
            )
        )
    return out


def gate_update_actions(
    actions: list[EntityAction],
    policy: RelevancePolicy | None,
    *,
    index: Any,
    raw_content: str,
    jsonl: bool = False,
    raw_ref: str = "",
) -> tuple[list[EntityAction], list[CategoryDrop]]:
    """Drop ``update`` actions whose TARGET page a ``never`` rule covers.

    Deterministic: the page's own frontmatter (type, tags, uid) is matched, so
    a covered fact is caught even when it reached the page by a Tier-1 name
    match and never carried a category. ``create`` actions pass through (the
    classify gate already judged them). Called by both transports ahead of
    Tier-3.
    """
    if policy is None or not policy.rules:
        return actions, []
    explicit = raw_is_explicit_remember(raw_content, jsonl=jsonl)
    kept: list[EntityAction] = []
    drops: list[CategoryDrop] = []
    for action in actions:
        decision = None
        if action.kind == "update" and action.existing_uid:
            path = index.get_by_uid(action.existing_uid)
            if path is not None and path.exists():
                meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
                type_, tags, uid = declared_facts({**meta, "uid": action.existing_uid})
                decision = decide(
                    Description(type_, tuple(tags), uid, explicit=explicit), policy
                )
        if decision is None or decision.capture != CAPTURE_DROP:
            if decision is not None and decision.overridden is not None:
                log.info(
                    "relevance-explicit-override ref=%s update=%s rule=%s",
                    raw_ref,
                    action.existing_uid,
                    _label(decision.overridden),
                )
            kept.append(action)
            continue
        rule = decision.rule
        assert rule is not None
        drops.append(
            CategoryDrop(
                reason=f"{DROP_REASON_PREFIX}: {_label(rule)}",
                excerpt=_excerpt(action.name, action.observations),
                name=action.name,
                existing_uid=action.existing_uid,
            )
        )
    return kept, drops
