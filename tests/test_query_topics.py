"""Tests for athenaeum.query_topics — LLM-based topic extraction.

The module must collapse every failure mode (missing key, SDK missing,
timeout, bad JSON, non-string items) to an empty list so the hook can
cleanly fall back to its built-in extractor. These tests pin that
contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from athenaeum import query_topics


def _mock_response(text: str) -> Any:
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def test_returns_empty_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert query_topics.extract_topics("Tell me about Return Path") == []


def test_returns_empty_on_short_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert query_topics.extract_topics("hi") == []
    assert query_topics.extract_topics("") == []
    assert query_topics.extract_topics("   ") == []


def test_extracts_topics_from_instruction_heavy_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    captured: dict[str, Any] = {}

    class _FakeMessages:
        def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return _mock_response('["Return Path", "consulting engagement"]')

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["__client_kwargs__"] = kwargs
            self.messages = _FakeMessages()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)

    topics = query_topics.extract_topics(
        "Without calling any tools, quote the block about Return Path verbatim."
    )
    assert topics == ["Return Path", "consulting engagement"]
    assert captured["model"] == query_topics.DEFAULT_TOPIC_MODEL
    assert captured["__client_kwargs__"]["api_key"] == "sk-test"
    assert captured["__client_kwargs__"]["max_retries"] == 0


def test_returns_empty_when_api_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            self.messages = self

        def create(self, **_: Any) -> Any:
            raise RuntimeError("network fell over")

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)

    with caplog.at_level(logging.WARNING, logger="athenaeum.query_topics"):
        assert query_topics.extract_topics("Tell me about Return Path") == []

    # A silent degradation here (dropping to DEBUG or swallowing the log)
    # would hide misconfiguration in production. Pin that a WARNING is
    # emitted on every API failure and that it names the exception class
    # so ops can triage without re-running with debug logging.
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "expected WARNING log on API failure"
    assert any("RuntimeError" in r.getMessage() for r in warning_records)


def test_returns_empty_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            self.messages = self

        def create(self, **_: Any) -> Any:
            return _mock_response("sorry, I can't help with that")

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)

    assert query_topics.extract_topics("Tell me about Return Path") == []


def test_filters_non_string_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            self.messages = self

        def create(self, **_: Any) -> Any:
            return _mock_response('["Return Path", 42, null, "", "  ", "Kalshi"]')

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)

    assert query_topics.extract_topics("anything goes") == ["Return Path", "Kalshi"]


def test_caps_at_eight_topics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            self.messages = self

        def create(self, **_: Any) -> Any:
            many = [f"topic{i}" for i in range(20)]
            import json as _json
            return _mock_response(_json.dumps(many))

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)

    topics = query_topics.extract_topics("some prompt with topics")
    assert len(topics) == 8
    assert topics[0] == "topic0"


def test_respects_topic_model_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ATHENAEUM_TOPIC_MODEL", "claude-haiku-3-5")
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            self.messages = self

        def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return _mock_response("[]")

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)

    query_topics.extract_topics("some prompt")
    assert captured["model"] == "claude-haiku-3-5"
