# SPDX-License-Identifier: Apache-2.0
"""Issue #330 — LLM provider seam + claude-cli subscription backend.

All CLI interaction is STUBBED via monkeypatched ``subprocess.run``. No test
here shells out to a real ``claude``; there is no live API or network.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from athenaeum._retry import TransientAPIError
from athenaeum.json_utils import extract_json_object
from athenaeum.models import TokenUsage, cache_usage_counts
from athenaeum.provider import (
    ClaudeCliClient,
    ProviderConfigError,
    build_llm_client,
    resolve_provider,
)
from athenaeum.tiers import _record_usage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(
    result: str = '{"detected": false}',
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_creation: int = 32,
    cache_read: int = 0,
    stop_reason: str = "end_turn",
    is_error: bool = False,
    subtype: str = "success",
    api_error_status: object = None,
) -> str:
    import json

    return json.dumps(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "api_error_status": api_error_status,
            "result": result,
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
            "total_cost_usd": 0.0649,
        }
    )


def _stub_run(
    monkeypatch, *, stdout="", returncode=0, stderr="", capture=None, raises=None
):
    """Patch subprocess.run in the provider module. Records argv into *capture*."""

    def fake_run(argv, **kwargs):
        if capture is not None:
            capture["argv"] = argv
            capture["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("athenaeum.provider.subprocess.run", fake_run)
    # ``shutil.which`` must find the binary so _create proceeds to subprocess.run.
    monkeypatch.setattr("athenaeum.provider.shutil.which", lambda _b: "/usr/bin/claude")


# ---------------------------------------------------------------------------
# resolve_provider — env > yaml > default; unknown raises
# ---------------------------------------------------------------------------


class TestResolveProvider:
    def test_default_is_api(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        assert resolve_provider(None) == "api"
        assert resolve_provider({}) == "api"

    def test_yaml_over_default(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        assert resolve_provider({"llm": {"provider": "claude-cli"}}) == "claude-cli"

    def test_env_over_yaml(self, monkeypatch):
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "api")
        assert resolve_provider({"llm": {"provider": "claude-cli"}}) == "api"

    def test_case_and_whitespace_normalized(self, monkeypatch):
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "  Claude-CLI  ")
        assert resolve_provider(None) == "claude-cli"

    def test_blank_env_falls_through_to_yaml(self, monkeypatch):
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "   ")
        assert resolve_provider({"llm": {"provider": "claude-cli"}}) == "claude-cli"

    def test_unknown_raises(self, monkeypatch):
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "gpt-cli")
        with pytest.raises(ProviderConfigError):
            resolve_provider(None)

    def test_unknown_yaml_raises(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        with pytest.raises(ProviderConfigError):
            resolve_provider({"llm": {"provider": "bedrock"}})


# ---------------------------------------------------------------------------
# build_llm_client — factory dispatch
# ---------------------------------------------------------------------------


class TestBuildLLMClient:
    def test_api_without_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert build_llm_client(None) is None

    def test_api_with_key_builds_sdk_client(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        captured = {}

        class FakeAnthropic:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
        client = build_llm_client(None, api_key="k-123", max_retries=3)
        assert isinstance(client, FakeAnthropic)
        assert captured == {"api_key": "k-123", "max_retries": 3}

    def test_api_omits_max_retries_when_none(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        captured = {}

        class FakeAnthropic:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
        build_llm_client(None, api_key="k")
        assert "max_retries" not in captured

    def test_claude_cli_returns_adapter(self, monkeypatch):
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
        client = build_llm_client(None)
        assert isinstance(client, ClaudeCliClient)
        # No SDK client, no ``.messages.batches`` (batch mode is API-only).
        assert not hasattr(client.messages, "batches")


# ---------------------------------------------------------------------------
# ClaudeCliClient.create — success path, usage shape, cache_control stripping
# ---------------------------------------------------------------------------


class TestClaudeCliCreate:
    def test_success_returns_text_and_usage(self, monkeypatch):
        _stub_run(monkeypatch, stdout=_envelope(result="hello world"))
        client = ClaudeCliClient()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system="be terse",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert resp.content[0].text == "hello world"
        # Consumed exactly the way tiers/_record_usage reads it.
        ins, outs, cc, cr = cache_usage_counts(resp)
        assert (ins, outs, cc, cr) == (10, 5, 32, 0)
        assert resp.stop_reason == "end_turn"

    def test_record_usage_consumes_response(self, monkeypatch):
        _stub_run(monkeypatch, stdout=_envelope())
        client = ClaudeCliClient()
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            system="s",
            messages=[{"role": "user", "content": "u"}],
        )
        usage = TokenUsage()
        _record_usage(resp, usage, model="claude-sonnet-4-6")
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5
        assert usage.cache_creation_input_tokens == 32

    def test_argv_uses_system_prompt_flag(self, monkeypatch):
        cap = {}
        _stub_run(monkeypatch, stdout=_envelope(), capture=cap)
        client = ClaudeCliClient()
        client.messages.create(
            model="m-1",
            system="SYSTEM-TEXT",
            messages=[{"role": "user", "content": "USER-TEXT"}],
        )
        argv = cap["argv"]
        assert "--system-prompt" in argv
        assert "SYSTEM-TEXT" in argv
        # user text is the -p prompt; model + json format present.
        assert "USER-TEXT" in argv
        assert "--model" in argv and "m-1" in argv
        assert "--output-format" in argv and "json" in argv
        # ``--append-system-prompt`` must NOT be used (would inherit persona).
        assert "--append-system-prompt" not in argv

    def test_cache_control_stripped_from_cli_path(self, monkeypatch):
        cap = {}
        _stub_run(monkeypatch, stdout=_envelope(), capture=cap)
        client = ClaudeCliClient()
        # Mirror resolutions.py: system is a list of blocks carrying cache_control.
        client.messages.create(
            model="m",
            system=[
                {
                    "type": "text",
                    "text": "RESOLVER-SYSTEM",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "USER-BLOCK",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        )
        argv = cap["argv"]
        flat = " ".join(argv)
        # The prompt TEXT survives; cache_control / block structure does not.
        assert "RESOLVER-SYSTEM" in flat
        assert "USER-BLOCK" in flat
        assert "cache_control" not in flat
        assert "ephemeral" not in flat

    def test_malformed_result_json_still_leniently_extracted(self, monkeypatch):
        # The model fenced its JSON answer in prose — extract_json_object (the
        # same #219/#222 path used for API responses) must still recover it.
        fenced = 'Here you go:\n```json\n{"detected": true}\n```\nHope that helps.'
        _stub_run(monkeypatch, stdout=_envelope(result=fenced))
        client = ClaudeCliClient()
        resp = client.messages.create(
            model="m", system="s", messages=[{"role": "user", "content": "u"}]
        )
        obj = extract_json_object(resp.content[0].text)
        assert obj == {"detected": True}


# ---------------------------------------------------------------------------
# ClaudeCliClient.create — error mapping
# ---------------------------------------------------------------------------


class TestClaudeCliErrors:
    def test_nonzero_exit_generic_raises_runtime(self, monkeypatch):
        _stub_run(monkeypatch, returncode=1, stderr="bad request: invalid model")
        client = ClaudeCliClient()
        with pytest.raises(RuntimeError):
            client.messages.create(
                model="m", system="s", messages=[{"role": "user", "content": "u"}]
            )

    def test_nonzero_exit_rate_limit_maps_transient(self, monkeypatch):
        _stub_run(monkeypatch, returncode=1, stderr="Error: rate limit exceeded (429)")
        client = ClaudeCliClient()
        with pytest.raises(TransientAPIError):
            client.messages.create(
                model="m", system="s", messages=[{"role": "user", "content": "u"}]
            )

    def test_timeout_maps_transient(self, monkeypatch):
        _stub_run(
            monkeypatch,
            raises=subprocess.TimeoutExpired(cmd="claude", timeout=1.0),
        )
        client = ClaudeCliClient()
        with pytest.raises(TransientAPIError):
            client.messages.create(
                model="m", system="s", messages=[{"role": "user", "content": "u"}]
            )

    def test_unparseable_envelope_raises_runtime(self, monkeypatch):
        _stub_run(monkeypatch, stdout="not json at all")
        client = ClaudeCliClient()
        with pytest.raises(RuntimeError):
            client.messages.create(
                model="m", system="s", messages=[{"role": "user", "content": "u"}]
            )

    def test_envelope_is_error_retryable_maps_transient(self, monkeypatch):
        _stub_run(
            monkeypatch,
            stdout=_envelope(
                result="overloaded, try again",
                is_error=True,
                subtype="error_during_execution",
                api_error_status=529,
            ),
        )
        client = ClaudeCliClient()
        with pytest.raises(TransientAPIError):
            client.messages.create(
                model="m", system="s", messages=[{"role": "user", "content": "u"}]
            )

    def test_envelope_is_error_nonretryable_raises_runtime(self, monkeypatch):
        _stub_run(
            monkeypatch,
            stdout=_envelope(
                result="prompt too long",
                is_error=True,
                subtype="error_max_turns",
            ),
        )
        client = ClaudeCliClient()
        with pytest.raises(RuntimeError):
            client.messages.create(
                model="m", system="s", messages=[{"role": "user", "content": "u"}]
            )

    def test_missing_binary_raises_runtime(self, monkeypatch):
        monkeypatch.setattr("athenaeum.provider.shutil.which", lambda _b: None)
        monkeypatch.setattr("athenaeum.provider.os.path.exists", lambda _b: False)
        client = ClaudeCliClient()
        with pytest.raises(RuntimeError, match="claude CLI not found"):
            client.messages.create(
                model="m", system="s", messages=[{"role": "user", "content": "u"}]
            )


# ---------------------------------------------------------------------------
# Parity — both backends produce a response the consumers accept
# ---------------------------------------------------------------------------


class TestBackendParity:
    def test_both_shapes_consumed_identically(self, monkeypatch):
        # CLI backend response.
        _stub_run(monkeypatch, stdout=_envelope(result='{"ok": 1}'))
        cli_resp = ClaudeCliClient().messages.create(
            model="claude-haiku-4-5-20251001",
            system="s",
            messages=[{"role": "user", "content": "u"}],
        )

        # API backend response double (the anthropic SDK Message shape).
        api_resp = SimpleNamespace(
            content=[SimpleNamespace(text='{"ok": 1}')],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=32,
                cache_read_input_tokens=0,
            ),
            stop_reason="end_turn",
        )

        for resp in (cli_resp, api_resp):
            # cache_usage_counts
            assert cache_usage_counts(resp) == (10, 5, 32, 0)
            # _record_usage
            usage = TokenUsage()
            _record_usage(resp, usage, model="claude-haiku-4-5-20251001")
            assert usage.input_tokens == 10
            # extract_json_object over the text
            assert extract_json_object(resp.content[0].text) == {"ok": 1}


# ---------------------------------------------------------------------------
# $0 subscription-covered cost accounting (#330)
# ---------------------------------------------------------------------------


class TestSubscriptionCost:
    def test_counts_preserved_cost_zero(self):
        usage = TokenUsage()
        usage.subscription_covered = True
        usage.add(1000, 500, model="claude-sonnet-4-6")
        # Counts still accumulate for the run summary.
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        assert usage.total_tokens == 1500
        # But cost is subscription-covered.
        assert usage.estimated_cost_usd == 0.0

    def test_api_backend_still_prices(self):
        usage = TokenUsage()
        usage.add(1000, 500, model="claude-sonnet-4-6")
        assert usage.estimated_cost_usd > 0.0
