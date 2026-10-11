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
    DEFAULT_JEV_BASE_URL,
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
# JevDecisionBackend._build_payload — request bodies pinned byte-for-byte
# (issue athenaeum#2054)
# ---------------------------------------------------------------------------


def test_build_payload_noul_carries_no_criteria() -> None:
    backend = JevDecisionBackend(api_key="k")
    payload = backend._build_payload(
        question_id="q1",
        kind="noul",
        instructions="is it true",
        state="the state",
        criteria=None,
    )
    body = json.dumps(payload).encode("utf-8")
    assert body == (
        b'{"model": "jev-latest", "state": "the state", '
        b'"questions": {"q1": {"type": "noul", "instructions": "is it true"}}}'
    )


def test_build_payload_noul_drops_criteria_even_if_given() -> None:
    backend = JevDecisionBackend(api_key="k")
    payload = backend._build_payload(
        question_id="q1",
        kind="noul",
        instructions="is it true",
        state="the state",
        criteria=["should", "be", "dropped"],
    )
    assert "criteria" not in payload["questions"]["q1"]


def test_build_payload_choice_pinned() -> None:
    backend = JevDecisionBackend(api_key="k")
    payload = backend._build_payload(
        question_id="q1",
        kind="choice",
        instructions="pick one",
        state="the state",
        criteria=["keep_a", "keep_b"],
    )
    body = json.dumps(payload).encode("utf-8")
    assert body == (
        b'{"model": "jev-latest", "state": "the state", '
        b'"questions": {"q1": {"type": "choice", "instructions": "pick one", '
        b'"criteria": ["keep_a", "keep_b"]}}}'
    )


def test_build_payload_score_criteria_is_a_list_of_level_labels() -> None:
    backend = JevDecisionBackend(api_key="k")
    payload = backend._build_payload(
        question_id="q1",
        kind="score",
        instructions="rate it",
        state="the state",
        criteria=["low", "medium", "high"],
    )
    body = json.dumps(payload).encode("utf-8")
    assert body == (
        b'{"model": "jev-latest", "state": "the state", '
        b'"questions": {"q1": {"type": "score", "instructions": "rate it", '
        b'"criteria": ["low", "medium", "high"]}}}'
    )


# ---------------------------------------------------------------------------
# JevDecisionBackend._parse_payload — the three response shapes
# ---------------------------------------------------------------------------


def test_parse_payload_noul() -> None:
    backend = JevDecisionBackend(api_key="k")
    result = backend._parse_payload(
        {
            "answers": {"q1": {"noul": 0.13}},
            "usage": {"input_tokens": 347, "output_tokens": 12},
        },
        question_id="q1",
    )
    assert result.probability == 0.13
    assert result.choice is None
    assert result.score is None
    assert result.input_tokens == 347
    assert result.output_tokens == 12


def test_parse_payload_choice() -> None:
    backend = JevDecisionBackend(api_key="k")
    result = backend._parse_payload(
        {
            "answers": {
                "q1": {
                    "choice": "keep_a",
                    "probabilities": {"keep_a": 0.87, "keep_b": 0.13},
                    "confidence": 0.87,
                }
            },
            "usage": {"input_tokens": 200, "output_tokens": 5},
        },
        question_id="q1",
    )
    assert result.choice == "keep_a"
    assert result.probabilities == {"keep_a": 0.87, "keep_b": 0.13}
    assert result.confidence == 0.87
    assert result.probability is None
    assert result.input_tokens == 200
    assert result.output_tokens == 5


def test_parse_payload_score() -> None:
    backend = JevDecisionBackend(api_key="k")
    result = backend._parse_payload(
        {
            "answers": {
                "q1": {
                    "score": 0.62,
                    "confidence": 0.9,
                    "legend": ["low", "medium", "high"],
                    "probabilities": {"low": 0.1, "medium": 0.3, "high": 0.6},
                }
            },
            "usage": {"input_tokens": 150, "output_tokens": 8},
        },
        question_id="q1",
    )
    assert result.score == 0.62
    assert result.confidence == 0.9
    assert result.legend == ["low", "medium", "high"]
    assert result.probabilities == {"low": 0.1, "medium": 0.3, "high": 0.6}
    assert result.input_tokens == 150
    assert result.output_tokens == 8


def test_parse_payload_missing_answers_raises_value_error() -> None:
    backend = JevDecisionBackend(api_key="k")
    with pytest.raises(ValueError):
        backend._parse_payload({"usage": {}}, question_id="q1")


def test_parse_payload_missing_question_answer_raises_value_error() -> None:
    backend = JevDecisionBackend(api_key="k")
    with pytest.raises(ValueError):
        backend._parse_payload({"answers": {"other": {}}}, question_id="q1")


# ---------------------------------------------------------------------------
# JevDecisionBackend.decide — injected transport, no network
# ---------------------------------------------------------------------------


def _ok_transport(payload: dict) -> "object":
    def _transport(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        assert url == DEFAULT_JEV_BASE_URL
        assert headers["Authorization"] == "Bearer test-key"
        sent = json.loads(body)
        assert sent["model"] == "jev-latest"
        assert sent["questions"]["q1"]["type"] == "choice"
        return json.dumps(payload).encode("utf-8")

    return _transport


def test_decide_success_choice() -> None:
    backend = JevDecisionBackend(
        api_key="test-key",
        transport=_ok_transport(
            {
                "answers": {
                    "q1": {
                        "choice": "keep_a",
                        "probabilities": {"keep_a": 0.87},
                        "confidence": 0.87,
                    }
                },
                "usage": {"input_tokens": 100, "output_tokens": 4},
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
    assert result.confidence == 0.87
    assert result.input_tokens == 100
    assert result.output_tokens == 4


def test_decide_url_is_default_base_url_no_subpath() -> None:
    seen: dict = {}

    def _transport(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        seen["url"] = url
        return json.dumps({"answers": {"q1": {"noul": 0.5}}}).encode("utf-8")

    backend = JevDecisionBackend(api_key="k", transport=_transport)
    backend.decide(question_id="q1", kind="noul", instructions="i", state="s")
    assert seen["url"] == DEFAULT_JEV_BASE_URL == "https://api.typesafe.ai/v1/systemone"


def test_jev_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_BASE_URL", "https://override.example/v1/systemone")
    cfg = DecisionProviderConfig("jev", False)
    backend = build_decision_client(cfg)
    assert isinstance(backend, JevDecisionBackend)
    assert backend._base_url == "https://override.example/v1/systemone"


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


def test_decide_no_answer_for_question_id_raises_value_error() -> None:
    def _transport(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        return json.dumps({"answers": {"other-question": {"noul": 0.5}}}).encode("utf-8")

    backend = JevDecisionBackend(api_key="k", transport=_transport)
    with pytest.raises(ValueError):
        backend.decide(question_id="q", kind="noul", instructions="i", state="s")
