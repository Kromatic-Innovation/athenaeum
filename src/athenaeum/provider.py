# SPDX-License-Identifier: Apache-2.0
"""LLM provider seam + first-party backends (issue #330).

Centralizes LLM client construction behind a single factory so the four
``messages.create`` call sites (:mod:`athenaeum.tiers`,
:mod:`athenaeum.contradictions`, :mod:`athenaeum.resolutions`, and — on the
recall hot path — :mod:`athenaeum.query_topics`) never learn which backend is
serving them. Two backends ship:

``api`` (default)
    Wraps today's :class:`anthropic.Anthropic` client verbatim. Params pass
    through UNCHANGED, so prompt caching (issue #230), the Messages Batch API
    (issue #236), retries, and every other SDK behavior are byte-for-byte
    identical to the pre-#330 code. The returned object *is* a real
    ``anthropic.Anthropic``.

``claude-cli``
    Drives the operator's ambient Claude Code subscription login via
    ``claude -p --model <id> --system-prompt <sys> --output-format json``.
    No credential handling: exactly like the git-push path (#284), athenaeum
    relies on the operator's own ``claude`` login. The adapter mirrors the
    slice of the SDK surface the call sites use — ``client.messages.create(
    **params)`` returning an object with ``.content[0].text`` plus a ``.usage``
    carrying the four token counters :func:`athenaeum.models.cache_usage_counts`
    reads — so the call sites need no change.

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
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from athenaeum._retry import TransientAPIError

log = logging.getLogger("athenaeum")

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

    Issue #330. Mirrors :func:`athenaeum.config.resolve_model`'s precedence:
    the ``ATHENAEUM_LLM_PROVIDER`` env var wins over the yaml ``llm.provider``
    key so an operator can swap backends for a single run without editing
    config, and the yaml key is read only when actually set. Values are
    case-folded and whitespace-trimmed. An unrecognized value raises
    :class:`ProviderConfigError` (loud — a typo must never silently fall back
    to a different backend). No seed in ``_DEFAULTS`` (issue #231) so the code
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

    Issue #330. The ``claude-cli`` backend authenticates via an ambient
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
# claude-cli adapter — response shapes mirroring the anthropic SDK surface
# the four call sites consume (``.content[0].text`` + ``.usage`` counters).
# ---------------------------------------------------------------------------


@dataclass
class _CliUsage:
    """Token counters in the exact shape ``cache_usage_counts`` reads (#230)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _CliTextBlock:
    """One content block; mirrors ``response.content[0].text``."""

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
    path has no caching breakpoints, so only the prompt TEXT survives.
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
    # (issue #330); the loud startup guard in run_librarian rejects
    # ``claude-cli`` + batch before any batch call could reach here.


class ClaudeCliClient:
    """Adapter that serves ``messages.create`` via the ``claude`` CLI (#330).

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

    def _build_argv(self, model: str, system_text: str, user_text: str) -> list[str]:
        argv = [self.binary, "-p", user_text, "--output-format", "json"]
        if model:
            argv += ["--model", model]
        if system_text:
            # ``--system-prompt`` (not ``--append-system-prompt``): fully
            # REPLACE Claude Code's default agent persona so the tier prompt
            # is the entire instruction context (#330).
            argv += ["--system-prompt", system_text]
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

        argv = self._build_argv(model, system_text, user_text)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd,
                check=False,
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
        use (#219/#222); this adapter returns the text verbatim.
        """
        stdout = stdout.strip()
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"claude CLI returned unparseable envelope: {exc}; "
                f"first 200 chars: {stdout[:200]!r}"
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
    """Construct the LLM client for the resolved provider (issue #330).

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
    return anthropic.Anthropic(**kwargs)
