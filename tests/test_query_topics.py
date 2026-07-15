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


# ---------------------------------------------------------------------------
# Provider seam routing (issue #380) — query_topics must go through the
# factory so ``llm.provider: claude-cli`` moves it to the subscription and no
# call site can bypass ``build_llm_client`` again.
# ---------------------------------------------------------------------------


def test_claude_cli_provider_makes_zero_sdk_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With provider claude-cli the extractor uses the subscription client and
    NEVER constructs an anthropic.Anthropic SDK client (zero metered calls)."""
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    # A key is present in the env, yet the metered SDK must still not be touched.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")

    sdk_constructed = {"count": 0}

    class _ForbiddenSDK:
        def __init__(self, **_: Any) -> None:
            sdk_constructed["count"] += 1
            raise AssertionError("anthropic.Anthropic must not be constructed")

    captured: dict[str, Any] = {}

    class _FakeCliClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["__cli_kwargs__"] = kwargs
            self.messages = self

        def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return _mock_response('["Return Path", "Kalshi"]')

    import anthropic

    from athenaeum import provider

    monkeypatch.setattr(anthropic, "Anthropic", _ForbiddenSDK)
    monkeypatch.setattr(provider, "ClaudeCliClient", _FakeCliClient)

    topics = query_topics.extract_topics(
        "quote the block about Return Path verbatim",
        config={"llm": {"provider": "claude-cli"}},
    )

    assert topics == ["Return Path", "Kalshi"]
    assert sdk_constructed["count"] == 0
    # timeout still flows to the subscription client (per-turn hook budget).
    assert captured["__cli_kwargs__"]["timeout"] == 3.0


def test_api_backend_preserves_timeout_and_no_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the api backend the factory must preserve the 3s hook timeout and
    max_retries=0 — a per-turn hook must never gain retries (issue #380)."""
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["__client_kwargs__"] = kwargs
            self.messages = self

        def create(self, **_: Any) -> Any:
            return _mock_response("[]")

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)

    query_topics.extract_topics("Tell me about Return Path", timeout=3.0)

    kwargs = captured["__client_kwargs__"]
    assert kwargs["api_key"] == "sk-test"
    assert kwargs["max_retries"] == 0
    assert kwargs["timeout"] == 3.0


def test_returns_empty_when_factory_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """api backend with no key -> build_llm_client returns None -> [] (the
    regex-extractor fallback), byte-for-byte the pre-#380 behaviour."""
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert query_topics.extract_topics("Tell me about Return Path") == []


def test_no_direct_anthropic_construction_outside_provider() -> None:
    """Durable guard (issue #380): the ONLY place an ``anthropic.Anthropic``
    SDK client may be constructed is ``provider.build_llm_client``. Every other
    call site must route through the factory, so a metered-API bypass can never
    be reintroduced invisibly. This is the real fix — not the one call site."""
    import re
    from pathlib import Path

    src = Path(__file__).parent.parent / "src" / "athenaeum"
    # Direct SDK construction, or a bare `from anthropic import Anthropic`
    # (which would let a call site drop the `anthropic.` prefix and dodge the
    # substring check). Both are only legitimate inside provider.py.
    construct = re.compile(r"anthropic\.Anthropic\s*\(")
    bare_import = re.compile(r"from\s+anthropic\s+import\b")

    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "provider.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if construct.search(line) or bare_import.search(line):
                offenders.append(f"{path.relative_to(src)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "anthropic.Anthropic must only be constructed in provider.py "
        "(route through build_llm_client instead):\n" + "\n".join(offenders)
    )
