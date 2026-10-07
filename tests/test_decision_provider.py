# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.decision_provider` (issue athenaeum#1997).

No network calls: :class:`~athenaeum.decision_provider.JevDecisionBackend`'s
transport is always injected.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from athenaeum._retry import TransientError
from athenaeum.decision_provider import (
    DEFAULT_DECISION_PROVIDER,
    VALID_DECISION_PROVIDERS,
    DecisionProviderConfig,
    DecisionProviderConfigError,
    DecisionResult,
    JevDecisionBackend,
    build_decision_client,
    preflight_decision_provider,
    resolve_decision_provider,
)

# ---------------------------------------------------------------------------
# resolve_decision_provider
# ---------------------------------------------------------------------------


def test_default_is_none_and_redact_off() -> None:
    cfg = resolve_decision_provider(None, "resolve")
    assert cfg.provider == DEFAULT_DECISION_PROVIDER == "none"
    assert cfg.redact_outbound is False


def test_yaml_provider_and_redact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATHENAEUM_RESOLVE_DECISION_PROVIDER", raising=False)
    monkeypatch.delenv("ATHENAEUM_RESOLVE_DECISION_PROVIDER_REDACT_OUTBOUND", raising=False)
    config = {
        "llm": {"decision_providers": {"resolve": {"provider": "jev", "redact_outbound": True}}}
    }
    cfg = resolve_decision_provider(config, "resolve")
    assert cfg.provider == "jev"
    assert cfg.redact_outbound is True


def test_env_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENAEUM_RESOLVE_DECISION_PROVIDER", "none")
    monkeypatch.setenv("ATHENAEUM_RESOLVE_DECISION_PROVIDER_REDACT_OUTBOUND", "true")
    config = {
        "llm": {"decision_providers": {"resolve": {"provider": "jev", "redact_outbound": False}}}
    }
    cfg = resolve_decision_provider(config, "resolve")
    assert cfg.provider == "none"
    assert cfg.redact_outbound is True


def test_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENAEUM_RESOLVE_DECISION_PROVIDER", "openai")
    with pytest.raises(DecisionProviderConfigError):
        resolve_decision_provider(None, "resolve")


def test_knobs_namespace_is_independent() -> None:
    assert "jev" not in ("api", "claude-cli")  # provider.VALID_PROVIDERS vocabulary
    assert set(VALID_DECISION_PROVIDERS) == {"none", "jev"}


# ---------------------------------------------------------------------------
# preflight_decision_provider
# ---------------------------------------------------------------------------


def test_preflight_none_is_always_ok() -> None:
    assert preflight_decision_provider("none") is None


def test_preflight_jev_missing_key_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    err = preflight_decision_provider("jev")
    assert err is not None
    assert "JEV_API_KEY" in err


def test_preflight_jev_with_key_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    assert preflight_decision_provider("jev") is None


# ---------------------------------------------------------------------------
# build_decision_client
# ---------------------------------------------------------------------------


def test_build_decision_client_none_provider_returns_none() -> None:
    assert build_decision_client(DecisionProviderConfig("none", False)) is None


def test_build_decision_client_jev_provider_returns_jev_backend() -> None:
    backend = build_decision_client(DecisionProviderConfig("jev", False))
    assert isinstance(backend, JevDecisionBackend)


# ---------------------------------------------------------------------------
# JevDecisionBackend.decide — injected transport, no network
# ---------------------------------------------------------------------------


def _ok_transport(payload: dict) -> "object":
    def _transport(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        assert "/decide" in url
        assert headers["Authorization"] == "Bearer test-key"
        sent = json.loads(body)
        assert sent["type"] == "choice"
        return json.dumps(payload).encode("utf-8")

    return _transport


def test_decide_success_choice() -> None:
    backend = JevDecisionBackend(
        api_key="test-key",
        transport=_ok_transport(
            {
                "choice": "keep_a",
                "probability": 0.87,
                "probabilities": {"keep_a": 0.87},
                "confidence": 0.87,
            }
        ),
    )
    result = backend.decide(
        question_id="q1",
        kind="choice",
        instructions="pick one",
        state="the state",
        criteria=["keep_a", "keep_b"],
    )
    assert isinstance(result, DecisionResult)
    assert result.choice == "keep_a"
    assert result.probability == 0.87


def test_decide_rate_limit_raises_transient_error() -> None:
    def _transport(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        raise urllib.error.HTTPError(url, 429, "rate limited", {}, None)

    backend = JevDecisionBackend(api_key="k", transport=_transport)
    with pytest.raises(TransientError):
        backend.decide(question_id="q", kind="noul", instructions="i", state="s")


def test_decide_server_error_raises_transient_error() -> None:
    def _transport(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        raise urllib.error.HTTPError(url, 503, "down", {}, None)

    backend = JevDecisionBackend(api_key="k", transport=_transport)
    with pytest.raises(TransientError):
        backend.decide(question_id="q", kind="noul", instructions="i", state="s")


def test_decide_client_error_is_not_transient() -> None:
    def _transport(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        raise urllib.error.HTTPError(url, 401, "unauthorized", {}, None)

    backend = JevDecisionBackend(api_key="k", transport=_transport)
    with pytest.raises(urllib.error.HTTPError):
        backend.decide(question_id="q", kind="noul", instructions="i", state="s")


def test_decide_malformed_json_raises_value_error() -> None:
    def _transport(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        return b"not json"

    backend = JevDecisionBackend(api_key="k", transport=_transport)
    with pytest.raises(ValueError):
        backend.decide(question_id="q", kind="noul", instructions="i", state="s")


def test_decide_connection_error_raises_transient_error() -> None:
    def _transport(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        raise urllib.error.URLError("no route")

    backend = JevDecisionBackend(api_key="k", transport=_transport)
    with pytest.raises(TransientError):
        backend.decide(question_id="q", kind="noul", instructions="i", state="s")
