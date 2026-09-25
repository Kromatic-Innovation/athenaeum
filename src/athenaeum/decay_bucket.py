# SPDX-License-Identifier: Apache-2.0
"""Cheap intake-time decay-bucket classifier (issue athenaeum#1837).

Structural mirror of :mod:`athenaeum.claim_kind`: classifies how a raw memory
DECAYS — one of :data:`athenaeum.models.MEMORY_BUCKETS` — in a single cheap LLM
call on the shared ``models.classify`` knob (Haiku by default), and stamps the
result ONCE into the raw file's frontmatter (:func:`stamp_decay_bucket`) so it
round-trips byte-for-byte through tier0 passthrough and is read back by
:func:`athenaeum.models.parse_bucket`.

Why it matters: auto-memory intake already READS ``bucket``
(``intake.py``'s ``bucket=parse_bucket(meta_for_markers)``) but, before this
module, nothing ever WROTE it — so every transient observation Claude writes
compiled to a durable page. The vocabulary (:data:`MEMORY_BUCKETS`) and the
classify-and-stamp pattern both already existed; this module is the missing
producer.

Why this is NOT folded into the ``claim_kind`` prompt (issue athenaeum#1837's
stated rationale): :func:`athenaeum.claim_kind.stamp_claim_kind` short-circuits
on a file that already carries ``claim_kind:``, and memory files written per the
operator's conventions already carry it — so a fused call would skip exactly the
files that need buckets. Two prompts, two independent idempotence gates.

Fail-open throughout, with one deliberate asymmetry against
:mod:`athenaeum.claim_kind`: a failure NEVER falls back to ``durable``. The
stamp is idempotent, so a blip-written ``durable`` would be permanent, and
``durable`` is already indistinguishable from unset for the deterministic sweep
(:mod:`athenaeum.decay_sweep` selects only expired ``daily`` pages). Leaving the
file unstamped costs nothing and is retried on the next run.

Layering note: despite living alongside the L0 primitives, this module is NOT a
leaf — exactly like :mod:`athenaeum.claim_kind`, which it mirrors. It calls the
live LLM client and imports L2 config (:mod:`athenaeum.config`) plus
service-level helpers (:mod:`athenaeum.provider`, :mod:`athenaeum.prompt_safety`)
for model resolution and prompt hygiene, on top of the L0/L1 primitives
(:mod:`athenaeum._retry`, :mod:`athenaeum.atomic_io`,
:mod:`athenaeum.json_utils`, :mod:`athenaeum.models`). Factoring rule: this
module owns ONLY the decay-bucket classify+stamp round-trip; it must not grow
``valid_until`` derivation or sweep policy that CONSUMES ``bucket`` (those live
in :mod:`athenaeum.decay_sweep` and its siblings).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from athenaeum._retry import with_retry
from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import DEFAULT_CLASSIFY_MODEL, resolve_model
from athenaeum.json_utils import extract_json_object
from athenaeum.models import (
    MEMORY_BUCKETS,
    TokenUsage,
    cache_usage_counts,
    parse_bucket,
    parse_frontmatter,
    render_frontmatter,
)
from athenaeum.prompt_safety import data_only_clause, defang_tag, fence_untrusted
from athenaeum.provider import (
    LLMBackend,
    resolve_max_tokens,
    resolve_thinking,
    response_text,
)

if TYPE_CHECKING:
    from anthropic.types import MessageParam, ThinkingConfigParam

log = logging.getLogger(__name__)

# Char budget for the snippet shown to the classifier. A memory's decay shape is
# legible from the opening; we do not need the whole body. Same budget as the
# claim_kind sibling, for the same reason.
_CLASSIFY_BODY_CHARS = 800


# Decay-bucket classify output budget: a single one-word label — a tiny
# response. Same 64-token budget as the claim_kind sibling, resolved through the
# same ``max_tokens`` seam rather than sent as a bare literal.
_DECAY_BUCKET_MAX_TOKENS = 64

# The prompt text lives HERE, inline, next to the parser that consumes it —
# deliberately, per ``policies/prompt-text-is-content.md``: prompt text is
# content whose load-bearing contract is with its adjacent parser (the
# ``{"bucket": ...}`` JSON shape read a few dozen lines below), so moving it to
# a data file or into ``prompt_registry`` would break that adjacency. The
# registry (``src/athenaeum/prompt_registry.py``) only IMPORTS this constant to
# index it, which is why the row there names this module as the owner and why
# review discipline over its bytes is handled by the golden snapshot rather than
# by relocating the string.
#
# The data-only clause comes from the shared prompt_safety helper rather than a
# hand-rolled sentence, so a future hardening of the canonical clause reaches
# this system prompt too instead of silently missing it.
DECAY_BUCKET_SYSTEM = (
    """You classify a single memory snippet by HOW IT DECAYS over time.

Return exactly ONE label describing how long the snippet stays useful — NOT its
topic, NOT whether it is true, NOT how important it is. The buckets:

- daily — rapidly-overwritten status. Only the LATEST value matters; the
  history of prior values is noise. "The staging deploy is waiting on CI."
  "The develop tip is abc123." "Three lanes are running tonight."
- weekly — short-horizon state that turns over in days, not hours, and is
  stale within a week or two. "This sprint is focused on the intake path."
  "Alice is out until Friday." "The staging soak is in its second week."
- durable — long-lived. A decision, a policy, a definition, a person's role, an
  architecture fact, a preference. Still true months from now unless something
  explicitly supersedes it. "We pivoted from Heroku to Fly.io." "Never commit
  directly to main." "Bob leads the platform team."

Choose the SINGLE best-fitting bucket. When genuinely torn between two, prefer
the LONGER-LIVED one: a durable memory that could have been daily merely costs
storage, while a daily memory that was actually durable can be swept away.

"""
    + data_only_clause("memory")
    + """

Return STRICT JSON, no prose, no markdown fence:
{"bucket": "daily" | "weekly" | "durable"}"""
)


def _get_classify_model(config: dict[str, Any] | None = None) -> str:
    # Same knob as claim_kind / tier2_classify / the detector: env
    # ATHENAEUM_CLASSIFY_MODEL > yaml models.classify > code default.
    return resolve_model(
        "classify", "ATHENAEUM_CLASSIFY_MODEL", DEFAULT_CLASSIFY_MODEL, config
    )


def _snippet(text: str) -> str:
    """Return the body (frontmatter stripped), trimmed and memory-tag-defanged."""
    _, body = parse_frontmatter(text)
    body = (body or text).strip()
    # Defang any literal memory tags so an untrusted body cannot forge the
    # <memory> boundary in the prompt.
    body = defang_tag(body, "memory")
    return body[:_CLASSIFY_BODY_CHARS].strip()


def classify_decay_bucket(
    text: str,
    client: "LLMBackend | None",
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
    *,
    wiki_root: Path | None = None,
) -> str:
    """Classify a memory snippet into one of :data:`MEMORY_BUCKETS`, or ``""``.

    Args:
        text: The raw memory content (with or without frontmatter — the body
            is extracted for classification).
        client: A live Anthropic client, or ``None``. ``None`` short-circuits
            to ``""`` (unbucketed) with no network call.
        config: Optional resolved athenaeum.yaml dict — routes
            ``models.classify`` to the call.
        usage: Optional run-level :class:`TokenUsage`; token + cache counts
            accumulate via :meth:`TokenUsage.add`.

    Returns:
        A member of :data:`MEMORY_BUCKETS`, or ``""`` on any failure (no
        client, API error, malformed JSON, out-of-vocabulary label). Never
        raises, and never guesses ``durable`` as a fallback — see the module
        docstring for why an unstamped file is the correct failure state.
    """
    if client is None:
        return ""
    snippet = _snippet(text)
    if not snippet:
        return ""

    model = _get_classify_model(config)
    # Fence the untrusted snippet via the shared prompt_safety helper. `_snippet`
    # already truncated + defanged; the two defences are complementary and
    # fence_untrusted's defang is idempotent here.
    user_msg = "Classify this memory snippet.\n\n" + fence_untrusted(
        snippet, tag="memory", max_chars=_CLASSIFY_BODY_CHARS
    )
    try:
        # Retry transient 429/529/connection blips with backoff rather than
        # failing straight to unbucketed on the first one.
        response = with_retry(
            lambda: client.messages.create(
                model=model,
                max_tokens=resolve_max_tokens(
                    "decay_bucket",
                    "ATHENAEUM_DECAY_BUCKET_MAX_TOKENS",
                    _DECAY_BUCKET_MAX_TOKENS,
                    config,
                ),
                # Same ``classify``-knob / Haiku posture as claim_kind: a
                # single-label classification does not benefit from thinking.
                thinking=cast(
                    "ThinkingConfigParam",
                    resolve_thinking(
                        "decay_bucket",
                        "ATHENAEUM_DECAY_BUCKET_THINKING",
                        "disabled",
                        config,
                    ),
                ),
                system=DECAY_BUCKET_SYSTEM,
                messages=cast(
                    "list[MessageParam]", [{"role": "user", "content": user_msg}]
                ),
            ),
            description="decay_bucket_classify",
        )
    except Exception as exc:  # noqa: BLE001 -- transient give-up or hard failure: fail open
        log.warning("decay_bucket: classify call failed (%s); unbucketed", exc)
        return ""

    if usage is not None and hasattr(response, "usage"):
        input_toks, output_toks, cache_creation, cache_read = cache_usage_counts(
            response
        )
        usage.add(
            input_toks,
            output_toks,
            cache_creation,
            cache_read,
            model=model,
            knob="classify",
        )

    try:
        # response_text skips any leading thinking block (this stage runs
        # disabled today; the helper is text-block-equivalent for a text-only
        # response and keeps the site robust if the posture changes).
        raw_text = response_text(response)
    except (AttributeError, IndexError, ValueError):
        log.warning("decay_bucket: malformed classify response; unbucketed")
        # Count the parse failure: this early return is ABOVE
        # observe_decay_bucket, so a malformed response would otherwise be
        # uncounted (the athenaeum#724 defect, avoided here by construction).
        from athenaeum.llm_schemas import observe_parse_failure

        observe_parse_failure(
            contract="decay_bucket",
            call_site="decay_bucket.classify_decay_bucket",
            detail="malformed-classify-response",
            wiki_root=wiki_root,
        )
        return ""

    payload = extract_json_object(raw_text)
    if not isinstance(payload, dict):
        log.warning("decay_bucket: no JSON object in classify response; unbucketed")
        # No JSON object at all — the most extreme missing-required case.
        from athenaeum.llm_schemas import observe_parse_failure

        observe_parse_failure(
            contract="decay_bucket",
            call_site="decay_bucket.classify_decay_bucket",
            detail="no-json-object",
            wiki_root=wiki_root,
        )
        return ""
    # Observe-only schema validation: log any delta from the accepted
    # ``{"bucket": <MEMORY_BUCKETS>}`` shape without changing the
    # unbucketed-fallback behavior below. Lazy import keeps pydantic off the
    # import graph until first use.
    from athenaeum.llm_schemas import observe_decay_bucket

    observe_decay_bucket(
        payload, call_site="decay_bucket.classify_decay_bucket", wiki_root=wiki_root
    )
    value = payload.get("bucket")
    if isinstance(value, str) and value in MEMORY_BUCKETS:
        return value
    log.warning("decay_bucket: classifier returned %r (not a valid bucket)", value)
    return ""


def stamp_decay_bucket(
    path: Path,
    client: "LLMBackend | None",
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
    *,
    wiki_root: Path | None = None,
) -> str:
    """Classify + stamp ``bucket:`` into a raw file's frontmatter, once.

    Idempotent and fail-open (issue athenaeum#1837):

    - If the file already carries a valid ``bucket`` → returns it, no call.
    - No client / classification failure / unreadable file → returns ``""``
      and writes nothing (the member stays unbucketed, which behaves exactly
      as it did before this module existed).
    - On a successful classification the label is written into the existing
      frontmatter (or a fresh block) and the value is returned.

    Never raises — an intake-time classification error must not crash the
    write path.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("decay_bucket: cannot read %s (%s); unbucketed", path, exc)
        return ""
    meta, body = parse_frontmatter(content)
    existing = parse_bucket(meta if meta else None)
    if existing:
        return existing

    bucket = classify_decay_bucket(
        content, client, config=config, usage=usage, wiki_root=wiki_root
    )
    if not bucket:
        return ""

    meta = dict(meta) if meta else {}
    meta["bucket"] = bucket
    rendered = render_frontmatter(meta) + body
    try:
        atomic_write_text(path, rendered)
    except OSError as exc:
        log.warning("decay_bucket: cannot write %s (%s); leaving unbucketed", path, exc)
        return ""
    log.info("decay_bucket: stamped %s on %s", bucket, path.name)
    return bucket
