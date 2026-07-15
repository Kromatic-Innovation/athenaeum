# SPDX-License-Identifier: Apache-2.0
"""LLM-based topic extraction for query rewriting.

The UserPromptSubmit hook's recall quality degrades on instruction-heavy
prompts where the actual topic is buried in meta-instructions (e.g.
"quote the block about Return Path verbatim"). A keyword-by-alphabetical-
sort extractor drops proper nouns; embedding the whole prompt drifts the
semantic center away from the buried topic.

This module runs a cheap LLM over the prompt with a system message that
asks it to extract substantive topics while ignoring meta-commentary.
The extracted topics are then used to drive FTS5 or vector search in
the hook.

If the API is unavailable, the function returns an empty list and the
caller is expected to fall back to its existing extractor. No exception
escapes — every failure mode collapses to the empty-list fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re

from athenaeum.config import resolve_model

log = logging.getLogger("athenaeum")

DEFAULT_TOPIC_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = (
    "You extract substantive search topics from a user's message for a "
    "librarian to use against a wiki. Return ONLY a JSON array of short "
    "topic strings — proper nouns, entity names, company names, project "
    "names, concrete concepts. Ignore meta-instructions (\"don't call "
    'tools", "quote verbatim", "say so"), generic verbs, and filler. '
    "Prefer the exact casing the user used. Return at most 8 topics. "
    "If the message has no substantive topic, return []."
)

_USER_TEMPLATE = (
    "User message:\n---\n{prompt}\n---\n\n"
    'Respond with JSON only, no prose. Example: ["Return Path", "lean startup"]'
)


def _get_topic_model(config: dict[str, object] | None = None) -> str:
    # env ATHENAEUM_TOPIC_MODEL > yaml models.topic > code default (#232).
    return resolve_model("topic", "ATHENAEUM_TOPIC_MODEL", DEFAULT_TOPIC_MODEL, config)


def extract_topics(
    prompt: str,
    timeout: float = 3.0,
    config: dict[str, object] | None = None,
) -> list[str]:
    """Extract substantive search topics from a user prompt.

    Returns an empty list (never raises) on any failure: missing API key,
    missing anthropic SDK, network/timeout, malformed response. The hook
    treats an empty result as "fall back to the built-in extractor".

    Args:
        prompt: The raw user message.
        timeout: Seconds to wait for the API call before giving up.
        config: Optional resolved athenaeum.yaml dict (issue #232) — routes
            ``models.topic`` to the call. ``None`` keeps env > code-default
            resolution.
    """
    if not prompt or len(prompt.strip()) < 4:
        return []

    # Route through the provider seam (issue #380) instead of constructing an
    # anthropic.Anthropic client directly. With ``llm.provider: claude-cli`` this
    # runs the per-turn topic extraction on the subscription and makes ZERO
    # metered API calls; with ``provider: api`` and no key the factory returns
    # None and we fall back to the regex extractor exactly as before. The CLI
    # client mirrors ``client.messages.create(**params)`` so the call below is
    # unchanged. ``timeout`` / ``max_retries=0`` are preserved on both backends.
    # ``provider`` is resolved here too so the spend ledger (below, #378) records
    # the backend actually used — never a hardcoded ``api`` that would misreport
    # a subscription call as metered dollars.
    try:
        from athenaeum.provider import build_llm_client, resolve_provider

        provider = resolve_provider(config)
        client = build_llm_client(config, timeout=timeout, max_retries=0)
    except Exception as exc:  # noqa: BLE001 — never raise out of the recall hook
        # Missing SDK, a bad ``llm.provider`` value, etc. — collapse to the
        # regex-extractor fallback, same guarantee the whole module makes.
        log.warning(
            "query_topics: client construction failed (%s); falling back to "
            "regex extractor: %s",
            exc.__class__.__name__,
            exc,
        )
        return []
    if client is None:
        return []

    try:
        response = client.messages.create(
            model=_get_topic_model(config),
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": _USER_TEMPLATE.format(prompt=prompt)}
            ],
        )
    except Exception as exc:
        # WARNING (not debug): silent fall-through to the regex extractor
        # hides a degraded state — topics lose proper-noun rescue even
        # though the feature looks "working". The class name in the log
        # tells you at a glance whether it's auth (401), network, or an
        # SDK-level bug without needing to reproduce.
        log.warning(
            "query_topics: API call failed (%s); falling back to regex extractor: %s",
            exc.__class__.__name__,
            exc,
        )
        return []

    # Usage logging (issue #230). Inline getattr (not the shared
    # models.cache_usage_counts helper) — this module stays import-light
    # because it runs on the recall-hook path with a 3s budget.
    _usage = getattr(response, "usage", None)
    log.debug(
        "query_topics: usage input=%s output=%s cache_creation=%s cache_read=%s",
        getattr(_usage, "input_tokens", 0),
        getattr(_usage, "output_tokens", 0),
        getattr(_usage, "cache_creation_input_tokens", 0),
        getattr(_usage, "cache_read_input_tokens", 0),
    )
    # Issue #378: this is the highest-frequency LLM call in the system — the
    # per-turn recall extractor, fired on EVERY prompt. Post-#380 it routes
    # through the provider seam, so it runs on the metered Anthropic SDK only
    # under ``provider: api``; under ``claude-cli`` it is subscription-covered.
    # Record the backend ACTUALLY used (``provider`` resolved above) so the
    # ledger reports api usage as real dollars and claude-cli usage as $0 —
    # never a hardcoded ``api`` that would misreport a subscription call.
    # Best-effort and import-light: a ledger failure never touches the 3s
    # recall budget. Only recorded when the response carried usage counters
    # (a real SDK response always does) — never a phantom zero-token row.
    if _usage is not None:
        try:
            from athenaeum import spend
            from athenaeum.models import TokenUsage

            _u = TokenUsage()
            _u.add(
                int(getattr(_usage, "input_tokens", 0) or 0),
                int(getattr(_usage, "output_tokens", 0) or 0),
                int(getattr(_usage, "cache_creation_input_tokens", 0) or 0),
                int(getattr(_usage, "cache_read_input_tokens", 0) or 0),
                model=_get_topic_model(config),
            )
            spend.record_spend(
                _u,
                run_type="query-topics",
                provider=provider,
                session_id=os.environ.get("CLAUDE_SESSION_ID"),
                config=config,
            )
        except Exception:  # noqa: BLE001 — ledger must never break recall
            pass

    try:
        text = response.content[0].text.strip()
    except (AttributeError, IndexError, TypeError):
        return []

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []

    try:
        items = json.loads(match.group())
    except json.JSONDecodeError:
        return []

    if not isinstance(items, list):
        return []

    return [
        str(item).strip() for item in items if isinstance(item, str) and item.strip()
    ][:8]
