# SPDX-License-Identifier: Apache-2.0
"""Typed-decision backend seam for Jev (TypeSafe AI) — issue athenaeum#1997.

**Why a separate seam, not a widened `athenaeum.provider` backend.** Jev's
published contract is three question types (Noul/Choice/Score), each
returning a *structured* answer with no free-text slot at all — the
opposite shape of `athenaeum.provider`'s text-backend Protocol, whose four call sites all
read a text answer via `response_text()` and JSON-extract it themselves.
Folding Jev behind that seam would mean either widening `LLMResponse` to
sometimes carry a non-text shape (defeating the point of a declared
contract — athenaeum#572) or templating Jev's structured answer back into
fake prose (reintroducing the unparseable-JSON failure class Jev is sold as
eliminating). See the athenaeum#1997 issue body's "Seam decision" section
for the full rejected-alternatives list, including why `"jev"` must stay out
of `athenaeum.provider.VALID_PROVIDERS` entirely (a text-call knob
misrouted to `"jev"` must be unconfigurable, not silently broken).

**Contract:** :class:`DecisionBackend` is a Protocol with one method,
`decide()`, returning a :class:`DecisionResult`. A call site that has a
genuinely typed question (its answer is a probability, a choice from a
fixed set, or a weighted score — never free text) resolves a provider via
:func:`resolve_decision_provider` and, when it resolves to `"jev"`, routes
through a `DecisionBackend` built by :func:`build_decision_client` instead
of (or alongside) the text-provider seam.

**Knob family:** `llm.decision_providers.<knob>` (yaml) — a mapping
`{provider: "none" | "jev", redact_outbound: bool}` — mirrored by
`ATHENAEUM_<KNOB>_DECISION_PROVIDER` (scalar `"none"`/`"jev"`, env > yaml >
default = `"none"`) and `ATHENAEUM_<KNOB>_DECISION_PROVIDER_REDACT_OUTBOUND`
(bool, same precedence, default `False`). For the `resolve` knob these are
`ATHENAEUM_RESOLVE_DECISION_PROVIDER` and
`ATHENAEUM_RESOLVE_DECISION_PROVIDER_REDACT_OUTBOUND`. This deliberately
mirrors `athenaeum.provider.resolve_provider`'s env > yaml > default
precedence rather than inventing a new convention, but lives in its own
vocabulary (:data:`VALID_DECISION_PROVIDERS`) — never merged with
`athenaeum.provider.VALID_PROVIDERS`.

**Failure classes** (per the athenaeum#1997 issue body):

1. Missing credential at startup → :func:`preflight_decision_provider`
   returns a loud startup error naming the knob, mirroring
   `athenaeum.provider.preflight_provider`. No silent fallback to the text
   provider.
2. Transient Jev error (429/5xx/connection) → :class:`athenaeum._retry.TransientError`,
   retried by the caller via `with_retry`, exactly like every other backend
   (athenaeum#782).
3. Retries exhausted, or a response that cannot be coerced to the call
   site's required shape → the call site's own give-up path (for
   `resolutions.py`'s `resolve` knob, `_fallback()`) — never a crash, never
   a silently-wrong action.

**Layering:** L3 service, sibling to `athenaeum.provider` and
`athenaeum.outbound_pii`. Module scope imports only `athenaeum._retry` (L0).
The wire transport uses stdlib `urllib` behind an injectable `transport`
callable rather than adding a new HTTP dependency (`anthropic` is already
optional; `httpx` is not guaranteed) — tests monkeypatch the transport, no
network call is ever made in a test.

**Wire shape caveat:** Jev's request/response JSON shape below is
constructed from TypeSafe's published description (flaviocopes.com/jev/)
as read during this issue's Specify pass; it was not independently
re-verified against a live Jev endpoint while implementing this PR (no
account/key was available — see the issue's "Operator host step"). The
translation is isolated in :meth:`JevDecisionBackend._build_payload` /
:meth:`JevDecisionBackend._parse_payload` so it can be corrected in one
place once a live credential is available; flag this explicitly in the PR
description.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from athenaeum._retry import TransientError

log = logging.getLogger(__name__)

DecisionKind = Literal["noul", "choice", "score"]


@dataclass(frozen=True)
class DecisionResult:
    """A typed answer from a :class:`DecisionBackend`.

    Mirrors Jev's three response shapes (Noul/Choice/Score) in one
    dataclass rather than three, since every field is optional and a call
    site reads only the fields relevant to the `kind` it asked for:

    - ``noul`` (yes/no) populates ``probability`` only.
    - ``choice`` populates ``choice``, ``probabilities``, ``confidence``.
    - ``score`` populates ``score``, ``legend``-equivalent ``probabilities``,
      ``confidence``.
    """

    choice: str | None = None
    probability: float | None = None
    probabilities: dict[str, float] | None = None
    confidence: float | None = None
    score: float | None = None


@runtime_checkable
class DecisionBackend(Protocol):
    """The declared typed-decision backend contract (issue athenaeum#1997).

    One method: a call site asks a genuinely typed question (never one that
    could require free text — see `resolutions.py`'s exclusion of
    `propose_merge`/`freetext_edit`) and gets back a :class:`DecisionResult`.
    """

    def decide(
        self,
        *,
        question_id: str,
        kind: DecisionKind,
        instructions: str,
        state: str | dict[str, Any] | list[Any],
        criteria: dict[str, str] | list[str] | None = None,
    ) -> DecisionResult: ...


#: Decision-provider ids recognized by :func:`resolve_decision_provider`.
#: Deliberately a SEPARATE vocabulary from `athenaeum.provider.VALID_PROVIDERS`
#: — see this module's docstring, "Rejected alternative 3" in the athenaeum#1997
#: issue body.
VALID_DECISION_PROVIDERS: tuple[str, ...] = ("none", "jev")

#: Default decision-provider value when neither env nor yaml set one.
DEFAULT_DECISION_PROVIDER = "none"

_TRUTHY = frozenset(("true", "1", "yes"))


class DecisionProviderConfigError(ValueError):
    """Raised when a `llm.decision_providers.<knob>` knob is misconfigured."""


@dataclass(frozen=True)
class DecisionProviderConfig:
    """Resolved `llm.decision_providers.<knob>` mapping for one knob."""

    provider: str
    redact_outbound: bool


def resolve_decision_provider(config: dict[str, Any] | None, knob: str) -> DecisionProviderConfig:
    """Resolve the decision-provider mapping for *knob* (issue athenaeum#1997).

    Precedence (env > yaml > default), resolved independently per field:

    - ``provider``: ``ATHENAEUM_<KNOB>_DECISION_PROVIDER`` env (scalar
      ``"none"``/``"jev"``) > yaml ``llm.decision_providers.<knob>.provider``
      > ``"none"``.
    - ``redact_outbound``: ``ATHENAEUM_<KNOB>_DECISION_PROVIDER_REDACT_OUTBOUND``
      env (bool) > yaml ``llm.decision_providers.<knob>.redact_outbound`` >
      ``False``.

    An unrecognized ``provider`` value raises :class:`DecisionProviderConfigError`
    naming the knob — loud, mirroring
    :func:`athenaeum.provider.resolve_provider`'s existing behavior for its
    own (separate) provider vocabulary.
    """
    env_provider_var = f"ATHENAEUM_{knob.upper()}_DECISION_PROVIDER"
    env_redact_var = f"ATHENAEUM_{knob.upper()}_DECISION_PROVIDER_REDACT_OUTBOUND"

    yaml_provider: str | None = None
    yaml_redact: bool | None = None
    if isinstance(config, dict):
        llm_cfg = config.get("llm")
        if isinstance(llm_cfg, dict):
            dp_cfg = llm_cfg.get("decision_providers")
            if isinstance(dp_cfg, dict):
                knob_cfg = dp_cfg.get(knob)
                if isinstance(knob_cfg, dict):
                    raw_provider = knob_cfg.get("provider")
                    if isinstance(raw_provider, str) and raw_provider.strip():
                        yaml_provider = raw_provider.strip()
                    raw_redact = knob_cfg.get("redact_outbound")
                    if isinstance(raw_redact, bool):
                        yaml_redact = raw_redact

    raw_env_provider = os.environ.get(env_provider_var)
    if raw_env_provider is not None and raw_env_provider.strip():
        provider = raw_env_provider.strip().lower()
        provider_source = f"env {env_provider_var}"
    elif yaml_provider is not None:
        provider = yaml_provider.lower()
        provider_source = f"yaml llm.decision_providers.{knob}.provider"
    else:
        provider = DEFAULT_DECISION_PROVIDER
        provider_source = "default"

    if provider not in VALID_DECISION_PROVIDERS:
        raise DecisionProviderConfigError(
            f"unknown decision provider {provider!r} for knob {knob!r} "
            f"(from {provider_source}); valid values are: "
            f"{', '.join(VALID_DECISION_PROVIDERS)}"
        )

    raw_env_redact = os.environ.get(env_redact_var)
    if raw_env_redact is not None and raw_env_redact.strip():
        redact_outbound = raw_env_redact.strip().lower() in _TRUTHY
    elif yaml_redact is not None:
        redact_outbound = yaml_redact
    else:
        redact_outbound = False

    return DecisionProviderConfig(provider=provider, redact_outbound=redact_outbound)


def preflight_decision_provider(provider: str) -> str | None:
    """Return a startup error message if *provider* cannot run, else ``None``.

    Issue athenaeum#1997, mirrors :func:`athenaeum.provider.preflight_provider`'s
    "fail loudly at startup, never silently fall back" contract. Only
    ``"jev"`` has a precondition today: a reachable credential
    (``JEV_API_KEY``). Checks presence only — never logs the value.
    """
    if provider == "jev" and not os.environ.get("JEV_API_KEY"):
        return (
            "jev decision provider selected but JEV_API_KEY is not set. "
            "The decision provider is explicit -- there is no silent "
            "fallback to the text provider."
        )
    return None


#: Injectable HTTP transport: (url, body, headers, timeout) -> raw response
#: bytes. Tests monkeypatch this so no real network call is ever made.
Transport = Callable[[str, bytes, dict[str, str], float], bytes]


def _default_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


#: Unverified at build time (no live credential available — see this
#: module's docstring). Re-verify against TypeSafe's current docs before
#: turning a decision knob on for live traffic.
DEFAULT_JEV_BASE_URL = "https://api.jev.ai/v1"
DEFAULT_JEV_TIMEOUT = 30.0


class JevDecisionBackend:
    """:class:`DecisionBackend` over Jev's HTTP API (issue athenaeum#1997).

    See this module's docstring "Wire shape caveat" — the request/response
    JSON shape here is read from TypeSafe's published description, not
    re-verified against a live endpoint.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_JEV_BASE_URL,
        timeout: float = DEFAULT_JEV_TIMEOUT,
        transport: Transport = _default_transport,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("JEV_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    def _build_payload(
        self,
        *,
        question_id: str,
        kind: DecisionKind,
        instructions: str,
        state: str | dict[str, Any] | list[Any],
        criteria: dict[str, str] | list[str] | None,
    ) -> dict[str, Any]:
        return {
            "question_id": question_id,
            "type": kind,
            "instructions": instructions,
            "state": state,
            "criteria": criteria,
        }

    def _parse_payload(self, data: dict[str, Any]) -> DecisionResult:
        return DecisionResult(
            choice=data.get("choice"),
            probability=data.get("probability"),
            probabilities=data.get("probabilities"),
            confidence=data.get("confidence"),
            score=data.get("score"),
        )

    def decide(
        self,
        *,
        question_id: str,
        kind: DecisionKind,
        instructions: str,
        state: str | dict[str, Any] | list[Any],
        criteria: dict[str, str] | list[str] | None = None,
    ) -> DecisionResult:
        payload = self._build_payload(
            question_id=question_id,
            kind=kind,
            instructions=instructions,
            state=state,
            criteria=criteria,
        )
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        try:
            raw = self._transport(f"{self._base_url}/decide", body, headers, self._timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise TransientError(f"jev http {exc.code}") from exc
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientError(f"jev transport error: {exc}") from exc

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError(f"jev: unparseable response: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"jev: response is not a JSON object: {data!r}")
        return self._parse_payload(data)


def build_decision_client(
    provider_config: DecisionProviderConfig,
) -> DecisionBackend | None:
    """Construct the :class:`DecisionBackend` named by *provider_config*, or
    ``None`` for ``"none"``.

    Call :func:`preflight_decision_provider` BEFORE this — this function
    does not itself check credential presence; it only constructs.
    """
    if provider_config.provider == "jev":
        return JevDecisionBackend()
    return None


__all__ = [
    "DecisionKind",
    "DecisionResult",
    "DecisionBackend",
    "VALID_DECISION_PROVIDERS",
    "DEFAULT_DECISION_PROVIDER",
    "DecisionProviderConfigError",
    "DecisionProviderConfig",
    "resolve_decision_provider",
    "preflight_decision_provider",
    "Transport",
    "DEFAULT_JEV_BASE_URL",
    "DEFAULT_JEV_TIMEOUT",
    "JevDecisionBackend",
    "build_decision_client",
]
