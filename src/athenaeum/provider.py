# SPDX-License-Identifier: Apache-2.0
"""LLM provider seam + first-party backends (issue athenaeum#330).

Centralizes LLM client construction behind a single factory so the four
``messages.create`` call sites (:mod:`athenaeum.tiers`,
:mod:`athenaeum.contradictions`, :mod:`athenaeum.resolutions`, and — on the
recall hot path — :mod:`athenaeum.query_topics`) never learn which backend is
serving them. Two backends ship:

``api`` (default)
    Wraps today's :class:`anthropic.Anthropic` client verbatim. Params pass
    through UNCHANGED, so prompt caching (issue athenaeum#230), the Messages Batch API
    (issue athenaeum#236), retries, and every other SDK behavior are byte-for-byte
    identical to the pre-athenaeum#330 code. The returned object *is* a real
    ``anthropic.Anthropic``.

``claude-cli``
    Drives the operator's ambient Claude Code subscription login via
    ``claude -p --model <id> --system-prompt <sys> --output-format json``.
    No credential handling: exactly like the git-push path (athenaeum#284), athenaeum
    relies on the operator's own ``claude`` login. The adapter mirrors the
    slice of the SDK surface the call sites use — ``client.messages.create(
    **params)`` returning an object whose text answer is read via
    :func:`response_text` (the first ``type == "text"`` content block, skipping
    any leading thinking blocks — issue athenaeum#578) plus a ``.usage`` carrying the
    four token counters :func:`athenaeum.models.cache_usage_counts` reads — so
    the call sites need no change.

Known constraints (implemented here / at the call sites, documented in
``docs/configuration.md``):

* **Batch mode is API-only.** ``ATHENAEUM_BATCH_MODE`` + ``claude-cli`` is a
  loud startup error (see :func:`athenaeum.librarian.run_librarian`); the Batch
  API is an Anthropic-endpoint feature with no CLI equivalent.
* **``cache_control`` is stripped** on the CLI path (caching breakpoints do not
  apply); it is preserved untouched on the ``api`` path.
* **Cost is subscription-covered.** Token COUNTS from the CLI JSON envelope are
  still recorded in :class:`~athenaeum.models.TokenUsage` (tagged by model), but
  ``estimated_cost_usd`` reports ``$0`` for a subscription run (the caller sets
  :attr:`TokenUsage.subscription_covered`).
* **Rate-limit / transient CLI failures map to
  :class:`athenaeum._retry.TransientAPIError`.** ``with_retry`` catches only the
  Anthropic SDK transient types, NOT this one — so a CLI transient is not
  retried in-run: it propagates and is caught downstream as a give-up (the
  affected file is deferred), and the single-machine run-lock + resume make the
  next run pick it up safely.

**Contract:** one factory (:func:`build_llm_client`) hides which backend is
serving a call site behind the shared ``messages.create(**params) ->
LLMResponse`` surface (see the :class:`LLMBackend` Protocol family below), and
one capability table (:func:`capabilities_for` / :class:`ProviderCapabilities`)
DECLARES what each backend can honor (``max_tokens``, ``stop_reason``,
``cache_control``, sampling params, batching) instead of a backend silently
dropping a param it cannot serve. A call site branches on the declared
capability, never on the provider id string.

**Factoring rule:** this module owns the LLM TRANSPORT seam — client
construction, backend capability declarations, and param/response translation
between the four call sites' expectations and each concrete backend
(``api`` wraps the Anthropic SDK unchanged; ``claude-cli`` drives the
``claude`` subscription CLI). It does NOT own prompt content, response
parsing/coercion (each call site's own job), or spend accounting
(:mod:`athenaeum.spend` reads the token counts this module's responses carry,
but this module never writes the ledger itself).

**Layering:** L3 service. Module scope imports :mod:`athenaeum._retry` and
:mod:`athenaeum.outbound_pii` (sibling L3) — never L4. ``anthropic`` (the
``api`` backend's SDK) is imported lazily inside :func:`build_llm_client` so a
``claude-cli``-only deployment need not have it installed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from athenaeum._retry import TransientAPIError
from athenaeum.outbound_pii import redact_outbound_text

log = logging.getLogger(__name__)

#: Recognized provider ids. ``api`` is the default and wraps the Anthropic SDK
#: unchanged; ``claude-cli`` drives the ambient Claude Code subscription login.
VALID_PROVIDERS = ("api", "claude-cli")

#: Default per-call timeout (seconds) for the ``claude`` subprocess. Generous
#: because a tier-3 merge over a large page can take a while; overridable via
#: ``ATHENAEUM_CLAUDE_CLI_TIMEOUT``.
DEFAULT_CLI_TIMEOUT = 300.0


class ProviderConfigError(ValueError):
    """Raised when the LLM provider is misconfigured (unknown id, etc.)."""


def resolve_provider(config: dict[str, Any] | None) -> str:
    """Resolve the active LLM provider from env > yaml ``llm.provider`` > api.

    Issue athenaeum#330. Mirrors :func:`athenaeum.config.resolve_model`'s precedence:
    the ``ATHENAEUM_LLM_PROVIDER`` env var wins over the yaml ``llm.provider``
    key so an operator can swap backends for a single run without editing
    config, and the yaml key is read only when actually set. Values are
    case-folded and whitespace-trimmed. An unrecognized value raises
    :class:`ProviderConfigError` (loud — a typo must never silently fall back
    to a different backend). No seed in ``_DEFAULTS`` (issue athenaeum#231) so the code
    default stays reachable.
    """
    raw = os.environ.get("ATHENAEUM_LLM_PROVIDER")
    source = "env ATHENAEUM_LLM_PROVIDER"
    if raw is None or not raw.strip():
        raw = None
        if isinstance(config, dict):
            llm_cfg = config.get("llm")
            if isinstance(llm_cfg, dict):
                candidate = llm_cfg.get("provider")
                if isinstance(candidate, str) and candidate.strip():
                    raw = candidate
                    source = "yaml llm.provider"
    if raw is None or not raw.strip():
        return "api"
    value = raw.strip().lower()
    if value not in VALID_PROVIDERS:
        raise ProviderConfigError(
            f"unknown LLM provider {value!r} (from {source}); "
            f"valid values are: {', '.join(VALID_PROVIDERS)}"
        )
    return value


def preflight_provider(provider: str) -> str | None:
    """Return a startup error message if PROVIDER cannot run, else ``None``.

    Issue athenaeum#330. The ``claude-cli`` backend authenticates via an ambient
    ``claude`` login and has no API-key check, so a missing / mistyped binary
    would otherwise fail per-file at call time — the run would exit rc 0 having
    silently deferred every file and printed no token summary. This probe makes
    that misconfiguration fail LOUDLY at startup (rc 1), matching the ``api``
    backend's missing-key behavior. Only the binary's PRESENCE is checked (a
    real auth check would spend a subscription call); a logged-OUT CLI still
    surfaces per-file at call time.
    """
    if provider == "claude-cli":
        binary = os.environ.get("ATHENAEUM_CLAUDE_CLI_BIN") or "claude"
        if shutil.which(binary) is None and not os.path.exists(binary):
            return (
                f"claude-cli provider selected but the {binary!r} binary was not "
                "found on PATH. Install Claude Code and log in (or set "
                "ATHENAEUM_CLAUDE_CLI_BIN). The provider is explicit — there is "
                "no silent fallback to the api backend."
            )
    return None


# ---------------------------------------------------------------------------
# The LLM backend contract (issue athenaeum#572 / epic athenaeum#515)
#
# The seam is not greenfield: ``build_llm_client`` already hides the backend
# from the four ``messages.create`` call sites, and ``ClaudeCliClient`` already
# proves a non-SDK backend can serve the same surface. What was missing is a
# *declared* interface — so adding a backend is a rewrite rather than a
# registration. These ``Protocol`` classes name exactly the slice of the
# anthropic SDK surface the call sites consume:
#
# * ``messages.create(**params)`` — the one method every backend must serve;
# * the response the callers read — its text answer (via :func:`response_text`,
#   the first ``type == "text"`` block — issue athenaeum#578) and ``.stop_reason``;
# * the four normalized ``.usage`` counters
#   :func:`athenaeum.models.cache_usage_counts` reads (issue athenaeum#230).
#
# The concrete backends must ACTUALLY satisfy this contract — the ``# type:
# ignore[dict-item]`` leaky-registry pattern the audit flagged at
# ``search.py:1654-1657`` (concrete classes that do not satisfy their declared
# Protocol) must not be repeated here. The ``TYPE_CHECKING`` assertion below
# is what enforces that for :class:`ClaudeCliClient`: it type-checks the
# adapter against :class:`LLMBackend` with no ``# type: ignore`` escape.
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMUsage(Protocol):
    """The four token counters :func:`~athenaeum.models.cache_usage_counts`
    reads off ``response.usage`` (issue athenaeum#230)."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


@runtime_checkable
class LLMTextBlock(Protocol):
    """One content block. Callers read the text answer via
    :func:`response_text` (the first ``type == "text"`` block, skipping any
    leading thinking blocks — issue athenaeum#578)."""

    text: str


@runtime_checkable
class LLMResponse(Protocol):
    """The response surface the four ``messages.create`` call sites consume.

    Declared as read-only ``@property`` members on purpose: a Protocol's plain
    data attributes are matched INVARIANTLY, which would reject a backend whose
    concrete field type merely *satisfies* the declared type (e.g.
    ``ClaudeCliClient``'s ``content: list[_CliTextBlock]`` against a
    ``Sequence[LLMTextBlock]`` field). Read-only properties are covariant, so a
    concrete backend satisfies the contract by exposing compatible attributes —
    which is exactly the "must ACTUALLY satisfy" guarantee issue athenaeum#572 requires.

    ``content`` is a sequence of blocks (callers read the text answer via
    :func:`response_text`, which skips any leading thinking blocks — issue
    athenaeum#578); ``stop_reason`` is the terminal reason (``"max_tokens"``,
    ``"end_turn"``, ...) or ``None`` when a backend cannot report it; ``usage``
    carries the four normalized token counters.
    """

    @property
    def content(self) -> Sequence[LLMTextBlock]: ...

    @property
    def stop_reason(self) -> str | None: ...

    @property
    def usage(self) -> LLMUsage: ...


@runtime_checkable
class LLMMessages(Protocol):
    """The ``client.messages`` facade — a single ``create(**params)`` method.

    Every athenaeum call site invokes ``client.messages.create(**params)`` and
    reads an :class:`LLMResponse` off the result; the parameter dict itself
    stays backend-neutral (a backend that cannot honor a param drops or
    normalizes it — see the ``ProviderCapabilities`` child, issue athenaeum#573).
    """

    def create(self, **params: Any) -> LLMResponse: ...


@runtime_checkable
class LLMBackend(Protocol):
    """The declared LLM backend contract (issue athenaeum#572 / epic athenaeum#515).

    A backend is anything exposing a ``messages`` facade whose ``create``
    returns an :class:`LLMResponse`. Both shipping backends satisfy it: the
    ``api`` backend *is* a real :class:`anthropic.Anthropic`, and
    :class:`ClaudeCliClient` is the first EXPLICIT implementor — it mirrors the
    same surface over the ``claude`` subscription CLI. Declaring the contract
    turns "add a backend" from a rewrite into a registration; a future backend
    registers by satisfying this Protocol, not by being wired into every call
    site.

    ``messages`` is a read-only ``@property`` for the same covariance reason as
    :class:`LLMResponse` — so a backend whose ``messages`` is a concrete facade
    (``_CliMessages``) still satisfies the contract.
    """

    @property
    def messages(self) -> LLMMessages: ...


# ---------------------------------------------------------------------------
# Provider capabilities (issue athenaeum#573 / epic athenaeum#515)
#
# The load-bearing piece of the epic: each backend DECLARES what it can honor
# instead of silently no-op'ing a param it drops. The audit found the exact
# bug this prevents (M15, issue athenaeum#574): the CLI backend drops ``max_tokens``
# with no CLI equivalent (``provider.py`` ``_create``), so the athenaeum#476 truncation
# retry — whose only change is raising ``max_tokens`` — re-sends a byte-
# identical request; and it cannot populate ``stop_reason``, so the
# truncation-detection branches never fire. Two defects, one root cause: a
# dropped capability with no declaration.
#
# This folds in the two ad-hoc precedents that already encoded a capability
# informally:
#   1. the batch-mode startup guard (``librarian.run_librarian``) — now
#      ``supports_batches`` (the guard reads this flag instead of testing the
#      provider id inline);
#   2. the documented-but-unenforced ``cache_control`` stripping on the CLI
#      path (``_text_from_system`` / ``_text_from_messages``) — now declared
#      as ``honors_cache_control=False``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a backend can honor. Frozen — a backend's capabilities are fixed.

    Callers branch or warn on a ``False`` flag instead of silently sending a
    request the backend will drop (issue athenaeum#573). The four flags here are
    TRANSPORT-level (a property of the backend), resolved by
    :func:`capabilities_for`.

    ``honors_sampling_params`` is the one MODEL-level flag: ``temperature`` /
    ``top_p`` / ``top_k`` return HTTP 400 on Opus 4.7+, Opus 5, Sonnet 5, and
    Fable 5, and are accepted on Haiku 4.5 / Sonnet 4.6 — a property of the
    *model*, not the transport. The model-level prefix set that records this
    lands with the pricing table in :mod:`athenaeum.models` (issue athenaeum#577 / epic
    athenaeum#516's B1), which is *"the single update site for model pricing"*; this
    module reads that set rather than re-declaring it (epic athenaeum#515: the sampling-
    capability set *"should land once, not twice"*). The value here is the
    transport-level default (the CLI drops sampling params like ``max_tokens``;
    the ``api`` transport passes them through and the model 400s or does not);
    per-model refinement is wired in :func:`capabilities_for` once athenaeum#577's set
    exists. The flag DESCRIBES reality — it is not a step toward sending the
    parameter (issues athenaeum#573/#577 both put that out of scope).
    """

    honors_max_tokens: bool
    reports_stop_reason: bool
    honors_cache_control: bool
    honors_sampling_params: bool
    supports_batches: bool


#: The ``api`` backend wraps the real Anthropic SDK: every param passes through
#: unchanged (issue athenaeum#330), so it honors ``max_tokens``, reports ``stop_reason``,
#: preserves ``cache_control`` breakpoints, and supports the Messages Batch API.
#: ``honors_sampling_params`` is the transport-level default (the SDK forwards
#: the params); whether a given MODEL 400s is athenaeum#577's model-level set.
_API_CAPABILITIES = ProviderCapabilities(
    honors_max_tokens=True,
    reports_stop_reason=True,
    honors_cache_control=True,
    honors_sampling_params=True,
    supports_batches=True,
)

#: The ``claude-cli`` subscription backend drives ``claude -p``: it has no
#: ``max_tokens`` equivalent (dropped in ``_create``), its ``--output-format
#: json`` envelope does not populate ``stop_reason``, it strips ``cache_control``
#: (``_text_from_*`` keep only prompt text), it drops sampling params the same
#: way, and it has no Batch API (the startup guard, now this flag).
_CLI_CAPABILITIES = ProviderCapabilities(
    honors_max_tokens=False,
    reports_stop_reason=False,
    honors_cache_control=False,
    honors_sampling_params=False,
    supports_batches=False,
)


def reported_stop_reason(
    response: Any, capabilities: ProviderCapabilities
) -> str | None:
    """Return *response*'s ``stop_reason``, or ``None`` if the backend cannot
    reliably report it (issue athenaeum#574).

    A backend with ``reports_stop_reason=False`` (``claude-cli``) does not
    reliably populate a message-level ``stop_reason``: the ``--output-format
    json`` envelope carries a top-level ``stop_reason`` field, but empirically
    (CLI 2.1.197) it can hold a spurious value (e.g. ``"stop_sequence"`` on an
    error envelope) and is not a faithful mirror of the model's terminal
    reason. Trusting it routes truncation detection down the wrong path.

    Returning ``None`` for such a backend makes every downstream branch that
    tests ``stop_reason == "max_tokens"`` fall through to its safe
    UNKNOWN-stop-reason path (a drop is classed as a generic degrade, not a
    truncation; the tier-3 truncation-refusal/fallback branches do not fire) —
    exactly the behavior the existing code already implements for a ``None``
    stop_reason. A backend that CAN report it (``api``) passes through
    unchanged.
    """
    if not capabilities.reports_stop_reason:
        return None
    value = getattr(response, "stop_reason", None)
    return value if isinstance(value, str) else None


def response_text(response: Any) -> str:
    """Return the model's TEXT answer from a Messages API response (issue athenaeum#578).

    The call sites parse the model's answer out of ``response.content`` — but
    ``response.content[0]`` is NOT always the text block. When a stage enables
    adaptive thinking (issue athenaeum#578 wired the resolver / tier-3 / merge stages to
    ``thinking: {"type": "adaptive"}``, which is supported on the CURRENT
    Opus 4.7 / Sonnet 4.6 defaults, not only on a future Opus 5 / Sonnet 5),
    the response begins with one or more ``type == "thinking"`` (or
    ``"redacted_thinking"``) blocks that PRECEDE the text block. With
    ``display`` omitted those thinking blocks carry empty/opaque text and, on
    the anthropic SDK, are ``ThinkingBlock`` objects with no ``.text``
    attribute at all — so a bare ``response.content[0].text`` either raises
    ``AttributeError`` or reads the wrong block.

    This helper walks ``response.content`` and returns the ``.text`` of the
    FIRST block whose ``type == "text"``, skipping thinking blocks. It works on
    both provider paths:

    * the live ``anthropic`` response, whose blocks expose ``.type`` (``"text"``
      / ``"thinking"`` / ``"redacted_thinking"``);
    * the ``claude-cli`` backend's constructed :class:`_CliResponse`, whose
      single :class:`_CliTextBlock` already carries ``type == "text"``.

    If no ``type == "text"`` block is found (a block with no ``.type``, or a
    genuinely text-less response), it falls back to ``response.content[0].text``
    — preserving today's exact behavior for single-block / text-only responses
    and letting the same ``AttributeError`` / ``IndexError`` the call sites
    already catch surface unchanged on a truly malformed response.
    """
    content = getattr(response, "content", None)
    if content:
        for block in content:
            if getattr(block, "type", None) == "text":
                return block.text
    # No text-typed block found (or empty/absent content): fall back to the
    # historical extraction so single-block and text-only responses — and the
    # malformed-response error paths the call sites already handle — are
    # byte-for-byte unchanged.
    return response.content[0].text


def capabilities_for(provider: str) -> ProviderCapabilities:
    """Return the :class:`ProviderCapabilities` for backend *provider* (athenaeum#573).

    Keyed by backend id (``"api"`` | ``"claude-cli"``). An unrecognized id maps
    to the ``api`` capabilities — the conservative choice, since ``api`` honors
    the most (a caller that reaches here with a bad id has already passed
    :func:`resolve_provider`'s validation, so this is only a fallback).

    The MODEL-level refinement of ``honors_sampling_params`` (Opus 4.7+/5 etc.
    return HTTP 400) is deferred to :mod:`athenaeum.models`' sampling-capability
    prefix set (issue athenaeum#577); when that set lands, this function grows a ``model``
    argument that reads it, rather than re-declaring the prefixes here.
    """
    if provider == "claude-cli":
        return _CLI_CAPABILITIES
    return _API_CAPABILITIES


# ---------------------------------------------------------------------------
# Per-stage params contract (issue athenaeum#575 / epic athenaeum#515)
#
# Each LLM stage's ``max_tokens`` budget used to be a literal baked into its
# call-site params dict (the audit counted them "scattered across nine call-site
# dicts", none config-overridable). Resolving them through the seam makes a
# stage's budget a KNOB a backend can normalize — and, mirroring the model-knob
# convention (:func:`athenaeum.config.resolve_model`, env > yaml > default),
# makes each one config-overridable. Today's values are unchanged: this moves
# WHERE the value lives, not what it is. A backend that cannot honor
# ``max_tokens`` (``claude-cli``) still drops it downstream — see
# ``ProviderCapabilities.honors_max_tokens`` (athenaeum#573/#574).
# ---------------------------------------------------------------------------


def _coerce_positive_int(raw: str) -> int | None:
    """Parse *raw* as a positive int, or ``None`` if it is not one."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_max_tokens(
    knob: str,
    env_var: str,
    default: int,
    config: dict[str, Any] | None = None,
) -> int:
    """Resolve a stage's ``max_tokens`` from env > yaml ``max_tokens.<knob>`` >
    code default (issue athenaeum#575).

    Mirrors :func:`athenaeum.config.resolve_model`'s precedence, but for a
    stage's OUTPUT-TOKEN budget: the ``env_var`` wins over the yaml
    ``max_tokens.<knob>`` key (read only when the operator set it), and
    *default* — today's baked-in literal, unchanged — is the code default. No
    seed in config ``_DEFAULTS`` so the code default stays reachable (issue
    athenaeum#231). A non-integer or non-positive override is IGNORED with a warning:
    the code default is far safer than a budget of ``0``, which would truncate
    every response.
    """
    env = os.environ.get(env_var)
    if env is not None and env.strip():
        parsed = _coerce_positive_int(env.strip())
        if parsed is not None:
            return parsed
        log.warning(
            "%s=%r is not a positive integer; using default max_tokens=%d",
            env_var,
            env,
            default,
        )
    if isinstance(config, dict):
        section = config.get("max_tokens")
        if isinstance(section, dict):
            raw = section.get(knob)
            # Reject bool (a subtype of int) and non-positive values.
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
    return default


# ---------------------------------------------------------------------------
# Per-stage ``thinking`` knob (issue athenaeum#578 / epic athenaeum#516's B2)
#
# No call site sets ``thinking`` today (harmless while every stage defaults to
# a model that runs without thinking when the param is omitted). But the
# moment a stage's default model moves to Opus 5 / Sonnet 5 (issue athenaeum#580,
# blocked_by this issue), OMITTING ``thinking`` on those tiers runs ADAPTIVE
# thinking silently — and ``max_tokens`` caps thinking + response TOGETHER, so
# a budget sized for a no-thinking response becomes a truncation risk. This
# resolver makes ``thinking`` an explicit, per-stage, config-overridable knob
# — mirroring :func:`resolve_max_tokens`'s env > yaml > default precedence —
# so no call site ever relies on a model-dependent default again.
# ---------------------------------------------------------------------------

#: The two postures a stage may declare. ``"adaptive"`` lets the model decide
#: when and how much to think (recommended for resolver/merge/reasoning
#: stages); ``"disabled"`` turns thinking off explicitly (recommended for
#: cheap/fast classification stages where thinking would only add latency).
_VALID_THINKING_TYPES = ("adaptive", "disabled")


def _coerce_thinking_type(raw: str) -> str | None:
    """Return *raw*, lowercased/stripped, iff it names a valid posture."""
    value = raw.strip().lower()
    return value if value in _VALID_THINKING_TYPES else None


def resolve_thinking(
    knob: str,
    env_var: str,
    default: str,
    config: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Resolve a stage's ``thinking`` posture from env > yaml
    ``thinking.<knob>`` > code default (issue athenaeum#578).

    Mirrors :func:`resolve_max_tokens`'s precedence exactly: the *env_var*
    wins over the yaml ``thinking.<knob>`` key (read only when the operator
    set it), and *default* — the stage's chosen posture — is the code
    default. No seed in config ``_DEFAULTS`` so the code default stays
    reachable (issue athenaeum#231, same rationale as :func:`resolve_max_tokens`).

    Args:
        knob: the stage name, e.g. ``"resolve"``, ``"merge_patch"``,
            ``"classify"`` — same knob namespace convention as
            :func:`resolve_max_tokens` and :func:`athenaeum.config.resolve_model`.
        env_var: the env var name, e.g. ``"ATHENAEUM_RESOLVE_THINKING"``.
        default: ``"adaptive"`` or ``"disabled"`` — the stage's code default.
        config: optional resolved athenaeum.yaml dict.

    Returns:
        The dict the SDK expects for the ``thinking`` request param —
        ``{"type": "adaptive"}`` or ``{"type": "disabled"}``. Deliberately
        never ``None``: per issue athenaeum#578's acceptance criteria, every call site
        should send an EXPLICIT disabled dict rather than omit the parameter,
        so no stage silently rides a model-dependent default (adaptive on
        Opus 5 / Sonnet 5 by omission, no-thinking on Opus 4.7/4.8 by
        omission) once the serving model changes under it.

    An invalid env or yaml value (anything other than ``"adaptive"`` /
    ``"disabled"``, case-insensitive) is IGNORED with a warning and falls
    through to *default* — a mistyped override should not silently disable
    thinking on a stage that needs it, or vice versa.
    """
    env = os.environ.get(env_var)
    if env is not None and env.strip():
        parsed = _coerce_thinking_type(env)
        if parsed is not None:
            return {"type": parsed}
        log.warning(
            "%s=%r is not 'adaptive' or 'disabled'; using default thinking=%r",
            env_var,
            env,
            default,
        )
    if isinstance(config, dict):
        section = config.get("thinking")
        if isinstance(section, dict):
            raw = section.get(knob)
            if isinstance(raw, str):
                parsed = _coerce_thinking_type(raw)
                if parsed is not None:
                    return {"type": parsed}
    return {"type": default}


# ---------------------------------------------------------------------------
# claude-cli adapter — response shapes mirroring the anthropic SDK surface the
# call sites consume (the text answer via :func:`response_text` + ``.usage``
# counters). ``_CliTextBlock`` carries ``type == "text"`` so :func:`response_text`
# treats it as the answer block (issue athenaeum#578) — the CLI never emits a thinking
# block, so its single block is always the text.
# ---------------------------------------------------------------------------


@dataclass
class _CliUsage:
    """Token counters in the exact shape ``cache_usage_counts`` reads (athenaeum#230)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _CliTextBlock:
    """One content block; its ``type == "text"`` default makes it the answer
    block :func:`response_text` returns (issue athenaeum#578)."""

    text: str
    type: str = "text"


@dataclass
class _CliResponse:
    """Drop-in for ``anthropic.types.Message`` over the consumed surface."""

    content: list[_CliTextBlock]
    usage: _CliUsage
    stop_reason: str | None = None
    model: str = ""


def _text_from_system(system: Any) -> str:
    """Flatten a ``system`` param (str OR list of text blocks) to plain text.

    Strips ``cache_control`` (and every other block key) by design — the CLI
    path has no caching breakpoints, so only the prompt TEXT survives. This is
    the behavior ``capabilities_for("claude-cli").honors_cache_control is
    False`` now DECLARES (issue athenaeum#573, folding the documented-but-unenforced
    stripping into the capability set).
    """
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n\n".join(parts)
    return str(system)


def _text_from_messages(messages: Any) -> str:
    """Flatten ``messages`` (each ``content`` a str OR list of blocks) to text.

    ``cache_control`` and non-text blocks are dropped; only user text reaches
    the ``-p`` prompt. All four athenaeum call sites send a single user turn
    whose content is a plain string, so this is loss-free for them.
    """
    parts: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(block, str):
                    parts.append(block)
    return "\n\n".join(parts)


def _coerce_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# Substrings in CLI stderr / error status that mark a RETRYABLE failure
# (subscription rate limit or transient overload). Matched case-insensitively.
_RETRYABLE_MARKERS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "429",
    "overloaded",
    "529",
    "too many requests",
    "quota",
    "usage limit",
    "temporarily unavailable",
    "service unavailable",
)


def _looks_retryable(*blobs: str) -> bool:
    haystack = " ".join(b for b in blobs if b).lower()
    return any(marker in haystack for marker in _RETRYABLE_MARKERS)


class _CliMessages:
    """The ``client.messages`` facade for the CLI backend."""

    def __init__(self, client: "ClaudeCliClient") -> None:
        self._client = client

    def create(self, **params: Any) -> _CliResponse:
        return self._client._create(**params)

    # NOTE: ``.batches`` is intentionally absent. Batch mode is API-only
    # (issue athenaeum#330); the loud startup guard in run_librarian rejects
    # ``claude-cli`` + batch before any batch call could reach here.


class ClaudeCliClient:
    """Adapter that serves ``messages.create`` via the ``claude`` CLI (athenaeum#330).

    Mirrors the ``anthropic.Anthropic`` surface the call sites use. Ambient
    subscription login only — no API key, no credential handling.
    """

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> None:
        self.binary = binary or os.environ.get("ATHENAEUM_CLAUDE_CLI_BIN") or "claude"
        if timeout is None:
            env_timeout = os.environ.get("ATHENAEUM_CLAUDE_CLI_TIMEOUT")
            if env_timeout:
                try:
                    timeout = float(env_timeout)
                except ValueError:
                    timeout = None
        self.timeout = timeout if (timeout and timeout > 0) else DEFAULT_CLI_TIMEOUT
        # Run from a neutral cwd so the subprocess does not inherit a project
        # CLAUDE.md / .mcp.json that would perturb the tier prompt. ``--system-
        # prompt`` already replaces the default agent persona.
        self.cwd = cwd or os.environ.get("TMPDIR") or "/tmp"
        self.messages = _CliMessages(self)

    def _build_argv(self, model: str, system_text: str) -> list[str]:
        # Issue athenaeum#543 (L4): the USER prompt is passed on STDIN (see ``_create``),
        # NOT as a ``-p`` argv element — so the user's own notes never sit in the
        # process table (visible to any local user via ``ps`` for the up-to-300s
        # life of the call). ``claude -p`` with no positional prompt reads the
        # prompt from stdin. ``--system-prompt`` still carries the tier
        # instruction text (not user content), so it stays on argv.
        argv = [self.binary, "-p", "--output-format", "json"]
        if model:
            argv += ["--model", model]
        if system_text:
            # ``--system-prompt`` (not ``--append-system-prompt``): fully
            # REPLACE Claude Code's default agent persona so the tier prompt
            # is the entire instruction context (athenaeum#330).
            argv += ["--system-prompt", system_text]
        # ``--strict-mcp-config`` (athenaeum#775): without it, every ``claude
        # -p`` spawn boots all nine user-scoped MCP servers from
        # ``~/.claude.json`` regardless of ``cwd`` — including athenaeum's own
        # MCP server, so the compile process boots the server that reads the
        # corpus it is compiling. Measured on the operator's host 2026-08-06:
        # ~8.7s CPU and ~12k tokens per call. No athenaeum call path relies on
        # an MCP tool being present (``--system-prompt`` already replaces the
        # agent persona; tier prompts are pure text-in/JSON-out), and output
        # was verified byte-identical with the flag on vs. off.
        argv.append("--strict-mcp-config")
        return argv

    def _create(self, **params: Any) -> _CliResponse:
        model = params.get("model", "") or ""
        system_text = _text_from_system(params.get("system"))
        user_text = _text_from_messages(params.get("messages"))
        # ``max_tokens`` has no CLI equivalent; the model/CLI applies its own
        # output cap. Intentionally dropped (documented).

        if shutil.which(self.binary) is None and not os.path.exists(self.binary):
            raise RuntimeError(
                f"claude CLI not found on PATH as {self.binary!r}; the "
                "claude-cli provider requires an installed, logged-in Claude "
                "Code (set ATHENAEUM_CLAUDE_CLI_BIN to override the binary)"
            )

        argv = self._build_argv(model, system_text)
        try:
            proc = subprocess.run(
                argv,
                # Issue athenaeum#543 (L4): user prompt on stdin, never in argv/`ps`.
                input=user_text,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd,
                check=False,
                # Suppress the host Claude Code Stop-hook desktop notification
                # for these programmatic ``claude -p`` calls (athenaeum#377). Merged on
                # top of the inherited environment so PATH/HOME/ambient auth
                # still reach the subprocess.
                env={**os.environ, "CLAUDE_SUPPRESS_NOTIFY": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            # A timeout is transient — surface it as TransientAPIError so it is
            # caught downstream as a give-up and the affected file is deferred to
            # the next run (run-lock + resume make that safe); the
            # resolver/detector fall-back paths degrade gracefully meanwhile.
            raise TransientAPIError(1, exc) from exc
        except OSError as exc:  # spawn failure (permissions, ENOENT race)
            raise RuntimeError(f"failed to invoke claude CLI: {exc}") from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            if _looks_retryable(stderr, proc.stdout or ""):
                raise TransientAPIError(
                    1, RuntimeError(f"claude CLI rate-limited/transient: {stderr}")
                )
            raise RuntimeError(
                f"claude CLI exited {proc.returncode}: {stderr or '(no stderr)'}"
            )

        return self._parse_envelope(proc.stdout or "", model)

    def _parse_envelope(self, stdout: str, model: str) -> _CliResponse:
        """Parse the ``--output-format json`` result envelope into a response.

        The envelope itself is well-formed JSON emitted by the CLI. The
        ASSISTANT TEXT it carries (``result``) may still be messy (fenced /
        prose-wrapped JSON) — that is handled downstream by the SAME lenient
        :func:`athenaeum.json_utils.extract_json_object` path the API responses
        use (athenaeum#219/#222); this adapter returns the text verbatim.
        """
        stdout = stdout.strip()
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            # Issue athenaeum#543 (L5): redact the raw model output before it lands in an
            # error message — the sibling response-log site (tiers.py) already
            # does this; this was the one that didn't.
            redacted_prefix, _findings = redact_outbound_text(stdout[:200])
            raise RuntimeError(
                f"claude CLI returned unparseable envelope: {exc}; "
                f"first 200 chars: {redacted_prefix!r}"
            ) from exc

        if not isinstance(envelope, dict):
            raise RuntimeError(
                f"claude CLI envelope was not a JSON object: {type(envelope).__name__}"
            )

        subtype = str(envelope.get("subtype") or "")
        api_error_status = str(envelope.get("api_error_status") or "")
        if envelope.get("is_error") or (subtype and subtype != "success"):
            detail = envelope.get("result") or subtype or "unknown error"
            if _looks_retryable(str(detail), subtype, api_error_status):
                raise TransientAPIError(
                    1, RuntimeError(f"claude CLI reported transient error: {detail}")
                )
            raise RuntimeError(f"claude CLI reported error ({subtype}): {detail}")

        result_text = envelope.get("result")
        if not isinstance(result_text, str):
            result_text = "" if result_text is None else str(result_text)

        usage_raw = envelope.get("usage")
        usage = _CliUsage()
        if isinstance(usage_raw, dict):
            usage = _CliUsage(
                input_tokens=_coerce_int(usage_raw.get("input_tokens")),
                output_tokens=_coerce_int(usage_raw.get("output_tokens")),
                cache_creation_input_tokens=_coerce_int(
                    usage_raw.get("cache_creation_input_tokens")
                ),
                cache_read_input_tokens=_coerce_int(
                    usage_raw.get("cache_read_input_tokens")
                ),
            )

        stop_reason = envelope.get("stop_reason")
        if stop_reason is not None and not isinstance(stop_reason, str):
            stop_reason = str(stop_reason)

        return _CliResponse(
            content=[_CliTextBlock(text=result_text)],
            usage=usage,
            stop_reason=stop_reason,
            model=model or str(envelope.get("model") or ""),
        )


if TYPE_CHECKING:
    # Issue athenaeum#572: ``ClaudeCliClient`` must ACTUALLY satisfy the declared
    # backend contract — no ``# type: ignore`` escape (the leaky-registry
    # anti-pattern the audit flagged at ``search.py:1654-1657``). If the
    # adapter ever drifts from :class:`LLMBackend` (a renamed ``messages``
    # facade, a ``create`` that stops returning an :class:`LLMResponse`), the
    # type checker flags it right here. Never executed — this is a
    # type-checker assertion only.
    _cli_backend_contract: LLMBackend = ClaudeCliClient()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_llm_client(
    config: dict[str, Any] | None,
    *,
    api_key: str | None = None,
    max_retries: int | None = None,
    timeout: float | None = None,
) -> Any | None:
    """Construct the LLM client for the resolved provider (issue athenaeum#330).

    The returned client satisfies the :class:`LLMBackend` contract (issue athenaeum#572):
    ``claude-cli`` returns a :class:`ClaudeCliClient` (the first explicit
    implementor, type-checked against the Protocol above), and ``api`` returns a
    real :class:`anthropic.Anthropic`, which serves the same
    ``messages.create`` / ``.content`` / ``.usage`` surface. The annotation
    stays ``Any`` rather than ``LLMBackend`` so the external SDK client is not
    forced through a fragile structural-subtype proof of the ``**params``
    surface; the contract is declared and enforced for our own backend.

    Returns ``None`` when nothing is configured for the ``api`` backend (no
    ``ANTHROPIC_API_KEY``) so every deterministic offline fallback keeps
    working unchanged — the ``client is None`` short-circuits in the tiers /
    contradictions / resolutions / reresolve paths are preserved.

    Args:
        config: resolved athenaeum.yaml dict (or ``None``).
        api_key: explicit key for the ``api`` backend; falls back to
            ``ANTHROPIC_API_KEY``. Ignored by ``claude-cli`` (subscription).
        max_retries: passed through to ``anthropic.Anthropic`` for the ``api``
            backend when set (byte-for-byte preserves each call site's value);
            omitted otherwise so the SDK default applies.
        timeout: per-call timeout override for the ``claude-cli`` subprocess.

    Returns the backend client, or ``None`` (api backend, no key).
    """
    provider = resolve_provider(config)

    if provider == "claude-cli":
        return ClaudeCliClient(timeout=timeout)

    # provider == "api": wrap the real SDK client verbatim (byte-for-byte).
    key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic

    kwargs: dict[str, Any] = {"api_key": key}
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    # Forward a client-level timeout when the caller set one (issue athenaeum#380). Only
    # the per-turn query_topics call site passes ``timeout`` today, so this is
    # additive for every other caller (they leave it None -> SDK default) while
    # preserving query_topics' 3s hook budget byte-for-byte on the api backend.
    if timeout is not None:
        kwargs["timeout"] = timeout
    return anthropic.Anthropic(**kwargs)
