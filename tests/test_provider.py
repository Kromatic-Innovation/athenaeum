# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#330 — LLM provider seam + claude-cli subscription backend.

All CLI interaction is STUBBED via monkeypatched ``subprocess.run``. No test
here shells out to a real ``claude``; there is no live API or network.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from athenaeum._retry import TransientAPIError, TransientError
from athenaeum.json_utils import extract_json_object
from athenaeum.models import TokenUsage, cache_usage_counts
from athenaeum.provider import (
    AnthropicBatchClientBackend,
    ClaudeCliClient,
    LLMBackend,
    LLMClientCache,
    LLMContentBlock,
    LLMMessages,
    LLMResponse,
    LLMTextBlock,
    LLMUsage,
    ProviderCapabilities,
    ProviderConfigError,
    _CliResponse,
    _CliTextBlock,
    _CliUsage,
    build_llm_client,
    capabilities_for,
    capabilities_for_knob,
    reported_stop_reason,
    resolve_max_tokens,
    resolve_provider,
    resolve_thinking,
    response_text,
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
# resolve_provider(config, knob=...) — per-knob routing (issue athenaeum#786)
# ---------------------------------------------------------------------------


class TestResolveProviderKnob:
    """AC1/AC3/AC6/Trap B."""

    def _clear_all_knob_envs(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        for knob in (
            "CLASSIFY",
            "WRITE",
            "RESOLVE",
            "TOPIC",
            "REASONING_T1",
            "REASONING_T2",
        ):
            monkeypatch.delenv(f"ATHENAEUM_{knob}_LLM_PROVIDER", raising=False)

    # -- AC6: no per-knob key anywhere -> identical to the pre-athenaeum#786 call --

    def test_no_knob_arg_is_byte_identical_to_pre_786(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        config = {"llm": {"provider": "claude-cli"}}
        assert resolve_provider(config) == "claude-cli"

    def test_knob_with_no_override_falls_through_to_global(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        config = {"llm": {"provider": "claude-cli"}}
        assert resolve_provider(config, knob="write") == "claude-cli"
        assert resolve_provider(config, knob="write") == resolve_provider(config)

    def test_empty_providers_section_is_byte_identical(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        config = {"llm": {"provider": "api", "providers": {}}}
        assert resolve_provider(config, knob="classify") == "api"

    # -- AC1: yaml llm.providers.<knob> overrides the global default --------

    def test_yaml_knob_override_wins_over_global_yaml(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        config = {
            "llm": {"provider": "api", "providers": {"write": "claude-cli"}}
        }
        assert resolve_provider(config, knob="write") == "claude-cli"
        # An unset knob still inherits the global default.
        assert resolve_provider(config, knob="classify") == "api"
        assert resolve_provider(config) == "api"

    # -- Trap B: ATHENAEUM_<KNOB>_LLM_PROVIDER env naming, all 6 knobs ------

    @pytest.mark.parametrize(
        "knob,env_var",
        [
            ("classify", "ATHENAEUM_CLASSIFY_LLM_PROVIDER"),
            ("write", "ATHENAEUM_WRITE_LLM_PROVIDER"),
            ("resolve", "ATHENAEUM_RESOLVE_LLM_PROVIDER"),
            ("topic", "ATHENAEUM_TOPIC_LLM_PROVIDER"),
            ("reasoning_t1", "ATHENAEUM_REASONING_T1_LLM_PROVIDER"),
            ("reasoning_t2", "ATHENAEUM_REASONING_T2_LLM_PROVIDER"),
        ],
    )
    def test_per_knob_env_var_name_for_all_six_knobs(
        self, monkeypatch, knob, env_var
    ):
        self._clear_all_knob_envs(monkeypatch)
        monkeypatch.setenv(env_var, "claude-cli")
        config = {"llm": {"provider": "api"}}
        assert resolve_provider(config, knob=knob) == "claude-cli"
        # No OTHER knob is affected by this one env var — a sibling knob
        # still falls through to the global default.
        other_knob = "resolve" if knob != "resolve" else "write"
        assert resolve_provider(config, knob=other_knob) == "api"

    def test_per_knob_env_wins_over_per_knob_yaml(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        monkeypatch.setenv("ATHENAEUM_WRITE_LLM_PROVIDER", "api")
        config = {
            "llm": {"provider": "api", "providers": {"write": "claude-cli"}}
        }
        assert resolve_provider(config, knob="write") == "api"

    def test_per_knob_env_wins_over_global_env(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
        monkeypatch.setenv("ATHENAEUM_WRITE_LLM_PROVIDER", "api")
        assert resolve_provider(None, knob="write") == "api"
        # The global chain (no knob) is untouched by the per-knob env var.
        assert resolve_provider(None) == "claude-cli"

    # -- AC3: unknown provider id in a per-knob key names the knob ----------

    def test_unknown_yaml_knob_value_raises_naming_the_knob(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        config = {"llm": {"providers": {"write": "bedrock"}}}
        with pytest.raises(ProviderConfigError) as exc_info:
            resolve_provider(config, knob="write")
        assert "write" in str(exc_info.value)
        assert "bedrock" in str(exc_info.value)

    def test_unknown_env_knob_value_raises_naming_the_knob(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        monkeypatch.setenv("ATHENAEUM_CLASSIFY_LLM_PROVIDER", "gpt-cli")
        with pytest.raises(ProviderConfigError) as exc_info:
            resolve_provider(None, knob="classify")
        assert "classify" in str(exc_info.value)

    def test_case_and_whitespace_normalized_for_knob(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        monkeypatch.setenv("ATHENAEUM_TOPIC_LLM_PROVIDER", "  Claude-CLI  ")
        assert resolve_provider(None, knob="topic") == "claude-cli"

    # -- default= : caller-supplied fallback instead of a fresh global resolve --

    def test_default_param_used_when_no_per_knob_override(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        # config's global provider is "api", but the caller passes an
        # independently-resolved default of "claude-cli" — default wins over
        # a fresh _resolve_global_provider(config) recompute.
        config = {"llm": {"provider": "api"}}
        assert (
            resolve_provider(config, knob="write", default="claude-cli")
            == "claude-cli"
        )

    def test_per_knob_override_still_wins_over_default(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        config = {"llm": {"providers": {"write": "claude-cli"}}}
        assert (
            resolve_provider(config, knob="write", default="api") == "claude-cli"
        )

    def test_default_ignored_when_knob_is_none(self, monkeypatch):
        self._clear_all_knob_envs(monkeypatch)
        # default= only applies to the per-knob chain; with no knob, the
        # global chain runs unchanged regardless of default=.
        assert resolve_provider(None, default="claude-cli") == "api"


# ---------------------------------------------------------------------------
# build_llm_client — factory dispatch
# ---------------------------------------------------------------------------


class TestPreflightProvider:
    def test_api_never_probes(self):
        from athenaeum.provider import preflight_provider

        assert preflight_provider("api") is None

    def test_claude_cli_missing_binary_returns_error(self, monkeypatch):
        import athenaeum.provider as prov

        monkeypatch.setattr(prov.shutil, "which", lambda _b: None)
        monkeypatch.setattr(prov.os.path, "exists", lambda _b: False)
        msg = prov.preflight_provider("claude-cli")
        assert msg is not None and "not found" in msg.lower()

    def test_claude_cli_present_binary_returns_none(self, monkeypatch):
        import athenaeum.provider as prov

        monkeypatch.setattr(prov.shutil, "which", lambda _b: "/usr/bin/claude")
        assert prov.preflight_provider("claude-cli") is None


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

    # -- knob= routing (issue athenaeum#786) ---------------------------------

    def test_knob_routes_to_its_own_per_knob_provider(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {
            "llm": {"provider": "api", "providers": {"write": "claude-cli"}}
        }
        client = build_llm_client(config, knob="write")
        assert isinstance(client, ClaudeCliClient)

    def test_knob_with_no_override_matches_pre_786_global_call(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {"llm": {"provider": "claude-cli"}}
        assert isinstance(build_llm_client(config, knob="write"), ClaudeCliClient)
        assert isinstance(build_llm_client(config), ClaudeCliClient)


# ---------------------------------------------------------------------------
# LLMClientCache — memoize per distinct (provider, api_key, max_retries,
# timeout), never per call (issue athenaeum#786 Trap C)
# ---------------------------------------------------------------------------


class TestLLMClientCache:
    def test_two_knobs_same_provider_share_one_client(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {"llm": {"provider": "claude-cli"}}
        cache = LLMClientCache()
        classify_client = build_llm_client(config, knob="classify", cache=cache)
        write_client = build_llm_client(config, knob="write", cache=cache)
        assert classify_client is write_client

    def test_two_knobs_different_providers_get_different_clients(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {
            "llm": {"provider": "api", "providers": {"write": "claude-cli"}}
        }
        cache = LLMClientCache()
        classify_client = build_llm_client(
            config, knob="classify", api_key="k-1", cache=cache
        )
        write_client = build_llm_client(config, knob="write", cache=cache)
        assert isinstance(write_client, ClaudeCliClient)
        assert not isinstance(classify_client, ClaudeCliClient)
        assert classify_client is not write_client

    def test_repeated_call_same_knob_returns_identical_object(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {"llm": {"provider": "claude-cli"}}
        cache = LLMClientCache()
        first = build_llm_client(config, knob="topic", cache=cache)
        second = build_llm_client(config, knob="topic", cache=cache)
        assert first is second

    def test_does_not_leak_across_differing_timeout(self, monkeypatch):
        # Trap C: two call sites resolving to the SAME provider but different
        # ``timeout`` must not collide on one memoized client.
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {"llm": {"provider": "claude-cli"}}
        cache = LLMClientCache()
        default_timeout = build_llm_client(config, knob="write", cache=cache)
        short_timeout = build_llm_client(
            config, knob="topic", timeout=3.0, cache=cache
        )
        assert default_timeout is not short_timeout
        assert short_timeout.timeout == 3.0

    def test_does_not_leak_across_differing_max_retries_or_api_key(
        self, monkeypatch
    ):
        # Mirrors query_topics's real shape: max_retries=0, vs. the
        # librarian's max_retries=3 — must not collide.
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        captured_kwargs: list[dict] = []

        class FakeAnthropic:
            def __init__(self, **kwargs):
                captured_kwargs.append(kwargs)

        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
        config = {"llm": {"provider": "api"}}
        cache = LLMClientCache()
        librarian_client = build_llm_client(
            config, knob="write", api_key="k", max_retries=3, cache=cache
        )
        topic_client = build_llm_client(
            config, knob="topic", api_key="k", max_retries=0, timeout=3.0, cache=cache
        )
        assert librarian_client is not topic_client
        assert len(captured_kwargs) == 2
        assert {"api_key": "k", "max_retries": 3} in captured_kwargs
        assert {"api_key": "k", "max_retries": 0, "timeout": 3.0} in captured_kwargs

    def test_no_cache_arg_never_memoizes_ac6(self, monkeypatch):
        # AC6: every pre-athenaeum#786 caller passes no cache= — behavior stays a
        # fresh client per call, exactly as before this issue.
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {"llm": {"provider": "claude-cli"}}
        first = build_llm_client(config)
        second = build_llm_client(config)
        assert first is not second

    def test_get_or_build_method_equivalent_to_module_function(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {"llm": {"provider": "claude-cli"}}
        cache = LLMClientCache()
        via_method = cache.get_or_build(config, knob="write")
        via_function = build_llm_client(config, knob="write", cache=cache)
        assert via_method is via_function


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
        # Issue athenaeum#543 (L4): the user prompt goes on STDIN, never argv/`ps`.
        assert "USER-TEXT" not in argv
        assert cap["kwargs"]["input"] == "USER-TEXT"
        # ``-p`` is present but carries no positional prompt (stdin does).
        assert "-p" in argv
        assert "--model" in argv and "m-1" in argv
        assert "--output-format" in argv and "json" in argv
        # ``--append-system-prompt`` must NOT be used (would inherit persona).
        assert "--append-system-prompt" not in argv

    def test_argv_includes_strict_mcp_config(self, monkeypatch):
        # athenaeum#775: every ``claude -p`` spawn must pass
        # ``--strict-mcp-config`` so it does not boot the nine user-scoped
        # MCP servers from ``~/.claude.json`` (including athenaeum's own).
        cap = {}
        _stub_run(monkeypatch, stdout=_envelope(), capture=cap)
        client = ClaudeCliClient()
        client.messages.create(
            model="m-1",
            system="s",
            messages=[{"role": "user", "content": "u"}],
        )
        assert "--strict-mcp-config" in cap["argv"]

    def test_user_prompt_passed_on_stdin_not_argv(self, monkeypatch):
        # Issue athenaeum#543 (L4): the user's own notes must never sit in the process
        # table. The prompt rides subprocess stdin (`input=`), and no element of
        # argv equals or contains it — pinning that a future refactor can't
        # quietly reintroduce ``-p <prompt>``.
        cap = {}
        _stub_run(monkeypatch, stdout=_envelope(), capture=cap)
        secret = "my private note about acme's Series B and bob@x.example"
        client = ClaudeCliClient()
        client.messages.create(
            model="m-1",
            system="s",
            messages=[{"role": "user", "content": secret}],
        )
        assert cap["kwargs"]["input"] == secret
        assert all(secret not in element for element in cap["argv"])

    def test_suppresses_host_desktop_notification(self, monkeypatch):
        # athenaeum#377: every programmatic ``claude -p`` call must set
        # CLAUDE_SUPPRESS_NOTIFY=1, merged on top of the inherited env so
        # PATH/HOME/ambient auth still reach the subprocess.
        monkeypatch.setenv("PATH", "/sentinel/bin")
        cap = {}
        _stub_run(monkeypatch, stdout=_envelope(), capture=cap)
        client = ClaudeCliClient()
        client.messages.create(
            model="m-1",
            system="SYSTEM-TEXT",
            messages=[{"role": "user", "content": "USER-TEXT"}],
        )
        env = cap["kwargs"]["env"]
        assert env["CLAUDE_SUPPRESS_NOTIFY"] == "1"
        # inherited environment is preserved, not replaced.
        assert env["PATH"] == "/sentinel/bin"

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
        # The system prompt TEXT survives on argv; cache_control / block
        # structure does not.
        assert "RESOLVER-SYSTEM" in flat
        assert "cache_control" not in flat
        assert "ephemeral" not in flat
        # Issue athenaeum#543 (L4): the user block's text rides stdin, not argv, and is
        # flattened to plain text (no cache_control / block structure).
        assert "USER-BLOCK" not in flat
        assert cap["kwargs"]["input"] == "USER-BLOCK"

    def test_malformed_result_json_still_leniently_extracted(self, monkeypatch):
        # The model fenced its JSON answer in prose — extract_json_object (the
        # same athenaeum#219/#222 path used for API responses) must still recover it.
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
        # Issue athenaeum#782: a rate-limit failure raises TransientError (the
        # "please retry me" request signal), not TransientAPIError (the
        # give-up type raised by with_retry once retries are exhausted) — so
        # a call wrapped in with_retry actually gets retried in-run. See
        # tests/test_retry_registry.py for the with_retry integration.
        _stub_run(monkeypatch, returncode=1, stderr="Error: rate limit exceeded (429)")
        client = ClaudeCliClient()
        with pytest.raises(TransientError):
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

    def test_unparseable_envelope_redacts_pii_in_error(self, monkeypatch):
        # Issue athenaeum#543 (L5): raw model output embedded in the unparseable-envelope
        # RuntimeError must be run through redact_outbound_text first, so a PII
        # email in the (non-JSON) stdout does not leak into logs/exceptions.
        _stub_run(monkeypatch, stdout="oops not json, contact bob@secret.example")
        client = ClaudeCliClient()
        with pytest.raises(RuntimeError) as excinfo:
            client.messages.create(
                model="m", system="s", messages=[{"role": "user", "content": "u"}]
            )
        msg = str(excinfo.value)
        assert "bob@secret.example" not in msg
        assert "[redacted-email]" in msg

    def test_envelope_is_error_retryable_maps_transient(self, monkeypatch):
        # Issue athenaeum#782: see test_nonzero_exit_rate_limit_maps_transient
        # above — same TransientError-not-TransientAPIError rationale.
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
        with pytest.raises(TransientError):
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
# $0 subscription-covered cost accounting (athenaeum#330)
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


# ---------------------------------------------------------------------------
# LLMBackend contract — the declared seam (athenaeum#572 / epic athenaeum#515)
# ---------------------------------------------------------------------------


class TestLLMBackendContract:
    """The backend contract is DECLARED (a Protocol), and the shipping
    ``claude-cli`` backend ACTUALLY satisfies it — not a `# type: ignore`
    duck-type (the anti-pattern athenaeum#572 calls out from search.py:1654)."""

    def test_claude_cli_client_is_an_llm_backend(self):
        # runtime_checkable: ClaudeCliClient exposes the ``messages`` facade.
        client = ClaudeCliClient()
        assert isinstance(client, LLMBackend)
        assert isinstance(client.messages, LLMMessages)

    def test_build_llm_client_claude_cli_satisfies_contract(self, monkeypatch):
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
        client = build_llm_client(None)
        assert isinstance(client, LLMBackend)

    def test_cli_response_shapes_satisfy_the_declared_protocols(self):
        resp = _CliResponse(
            content=[_CliTextBlock(text='{"ok": 1}')],
            usage=_CliUsage(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=32,
                cache_read_input_tokens=0,
            ),
            stop_reason="end_turn",
        )
        assert isinstance(resp, LLMResponse)
        assert isinstance(resp.content[0], LLMTextBlock)
        assert isinstance(resp.usage, LLMUsage)
        # The surface the four call sites read is reachable through the contract.
        assert resp.content[0].text == '{"ok": 1}'
        assert resp.stop_reason == "end_turn"
        assert cache_usage_counts(resp) == (10, 5, 32, 0)

    def test_create_returns_an_llm_response(self, monkeypatch):
        # A real ``create`` call (subprocess stubbed) returns an LLMResponse.
        _stub_run(monkeypatch, stdout=_envelope(result='{"ok": 1}'))
        client = ClaudeCliClient()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert isinstance(resp, LLMResponse)
        assert extract_json_object(resp.content[0].text) == {"ok": 1}


# ---------------------------------------------------------------------------
# Real-SDK-shape conformance — corrected declarations vs. reality (athenaeum#835)
#
# ``isinstance(client, LLMBackend)`` (used throughout ``TestLLMBackendContract``
# above) is NOT evidence the *shape* is correct: ``@runtime_checkable`` only
# checks attribute PRESENCE, not field types, so it passes identically before
# and after this fix. The STATIC proof — binding a realistically-shaped
# ``anthropic.types.Message`` to an ``LLMResponse``-annotated name so mypy
# does the real work — lives in ``src/athenaeum/provider.py``'s own
# ``TYPE_CHECKING`` block (alongside the pre-existing
# ``_cli_backend_contract: LLMBackend = ClaudeCliClient()`` assertion), NOT
# here: ``[tool.mypy] files = ["src/athenaeum"]`` in ``pyproject.toml`` means
# a ``TYPE_CHECKING`` block in ``tests/`` is never actually type-checked by
# the ``mypy`` gate (nor CI's ``run: mypy`` step), so putting the static
# proof here would be inert. ``TestRealSDKShapeConformance`` below is the
# runtime companion: the identically-shaped object also BEHAVES correctly,
# not just type-checks.
# ---------------------------------------------------------------------------


class TestRealSDKShapeConformance:
    """Runtime companion to the ``TYPE_CHECKING`` assertion in
    ``provider.py`` (issue athenaeum#835): the same realistically-shaped
    ``anthropic.types.Message`` — a ``ThinkingBlock`` preceding the
    ``TextBlock``, ``Usage`` with both cache_* fields ``None`` — not only
    satisfies the corrected declarations but behaves correctly when read
    through them."""

    @staticmethod
    def _build_message():
        import anthropic.types as t

        return t.Message(
            id="msg_01",
            content=[
                t.ThinkingBlock(
                    type="thinking", thinking="reasoning...", signature="sig"
                ),
                t.TextBlock(type="text", text="hello", citations=None),
            ],
            model="claude-sonnet-4-6",
            role="assistant",
            stop_reason="end_turn",
            stop_sequence=None,
            type="message",
            usage=t.Usage(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=None,
                cache_read_input_tokens=None,
            ),
        )

    def test_response_text_skips_leading_thinking_block(self):
        # Fails-closed at runtime: if content typing regressed to assume
        # every block is text, this would raise AttributeError instead of
        # returning the text block past the thinking block (issue athenaeum#578).
        msg = self._build_message()
        assert response_text(msg) == "hello"

    def test_cache_usage_counts_coerces_none_cache_fields_to_zero(self):
        # The real SDK's None cache fields (no cache breakpoints in this
        # request) already coerce to 0 at the read site (athenaeum#230) — the
        # retyping to ``int | None`` needed no downstream change.
        msg = self._build_message()
        assert cache_usage_counts(msg) == (10, 5, 0, 0)

    def test_real_message_satisfies_the_corrected_protocols_at_runtime(self):
        msg = self._build_message()
        assert isinstance(msg, LLMResponse)
        assert isinstance(msg.usage, LLMUsage)
        # Every block satisfies the narrow LLMContentBlock (just ``.type``)...
        assert isinstance(msg.content[0], LLMContentBlock)
        assert isinstance(msg.content[1], LLMContentBlock)
        # ...but only the TEXT block satisfies LLMTextBlock (``.text``) — the
        # ThinkingBlock genuinely has no ``.text`` attribute, which is exactly
        # the mismatch the pre-athenaeum#835 ``Sequence[LLMTextBlock]`` declaration
        # papered over.
        assert not isinstance(msg.content[0], LLMTextBlock)
        assert isinstance(msg.content[1], LLMTextBlock)


# ---------------------------------------------------------------------------
# Batch hand-off boundary adapter — ``AnthropicBatchClientBackend`` (athenaeum#778)
#
# ``batch.py``'s 3 hand-off sites pass a concrete ``anthropic.Anthropic`` into
# shared tier functions typed against ``LLMBackend``; the SDK's ``.messages``
# doesn't structurally satisfy ``LLMMessages`` (typed overloads vs ``**params:
# Any``), so this adapter bridges the gap. The STATIC proof that the adapter
# satisfies ``LLMBackend`` lives in ``src/athenaeum/provider.py``'s own
# ``TYPE_CHECKING`` block (``_batch_boundary_contract: LLMBackend =
# AnthropicBatchClientBackend(anthropic.Anthropic())``), NOT here — same
# reasoning as ``TestRealSDKShapeConformance`` above:
# ``isinstance(adapter, LLMBackend)`` would pass whether or not the adapter is
# actually correct (``@runtime_checkable`` checks attribute PRESENCE only, not
# signatures), and a ``TYPE_CHECKING`` block placed in ``tests/`` is never
# type-checked (``[tool.mypy] files = ["src/athenaeum"]`` in ``pyproject.toml``).
# These tests are the runtime companion: the adapter genuinely DELEGATES (same
# params in, same response out — no transform) and genuinely NARROWS (no
# ``.batches`` reachable through it, even though the wrapped client has one).
# ---------------------------------------------------------------------------


class TestBatchBoundaryAdapter:
    @staticmethod
    def _fake_sdk_client(response):
        """Duck-types the ``anthropic.Anthropic`` shape the adapter wraps —
        a ``.messages.create(**params)`` facade AND a sibling ``.batches``,
        so the narrowing assertion below proves something (a client with no
        ``.batches`` at all would prove nothing)."""
        received = {}

        def _create(**params):
            received.update(params)
            return response

        sdk_client = SimpleNamespace(
            messages=SimpleNamespace(create=_create),
            batches=SimpleNamespace(create=lambda **_: None),
        )
        return sdk_client, received

    def test_create_delegates_params_and_response_unchanged(self):
        sentinel_response = object()
        sdk_client, received = self._fake_sdk_client(sentinel_response)
        adapter = AnthropicBatchClientBackend(sdk_client)

        result = adapter.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": "hi"}],
        )

        # Same response object back — no transform.
        assert result is sentinel_response
        # Same params the caller passed — no transform.
        assert received == {
            "model": "claude-sonnet-4-6",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "hi"}],
        }

    def test_adapter_does_not_expose_batches(self):
        sdk_client, _ = self._fake_sdk_client(response=None)
        adapter = AnthropicBatchClientBackend(sdk_client)

        assert not hasattr(adapter, "batches")
        # The narrowing is the adapter's — the wrapped client still has it.
        assert hasattr(sdk_client, "batches")


# ---------------------------------------------------------------------------
# ProviderCapabilities — each backend DECLARES what it can honor (athenaeum#573)
# ---------------------------------------------------------------------------


class TestProviderCapabilities:
    def test_cli_declares_the_dropped_capabilities(self):
        caps = capabilities_for("claude-cli")
        # The CLI backend drops max_tokens, cannot report stop_reason, strips
        # cache_control, drops sampling params, and has no Batch API.
        assert caps.honors_max_tokens is False
        assert caps.reports_stop_reason is False
        assert caps.honors_cache_control is False
        assert caps.honors_sampling_params is False
        assert caps.supports_batches is False

    def test_api_honors_the_full_surface(self):
        caps = capabilities_for("api")
        # The api backend wraps the SDK verbatim: every param passes through.
        assert caps.honors_max_tokens is True
        assert caps.reports_stop_reason is True
        assert caps.honors_cache_control is True
        assert caps.honors_sampling_params is True
        assert caps.supports_batches is True

    def test_unknown_provider_falls_back_to_api_caps(self):
        # A caller reaching here with a bad id has passed resolve_provider's
        # validation; the api caps are the conservative (most-honoring) default.
        assert capabilities_for("something-else") == capabilities_for("api")

    def test_capabilities_are_frozen(self):
        caps = capabilities_for("claude-cli")
        with pytest.raises((AttributeError, TypeError)):
            caps.honors_max_tokens = True  # type: ignore[misc]

    def test_supports_batches_is_the_folded_batch_guard(self):
        # The batch-mode startup guard now reads this flag (issue athenaeum#573):
        # claude-cli cannot batch; api can.
        assert capabilities_for("claude-cli").supports_batches is False
        assert capabilities_for("api").supports_batches is True

    def test_is_a_provider_capabilities_instance(self):
        assert isinstance(capabilities_for("api"), ProviderCapabilities)


class TestCapabilitiesForKnob:
    """AC4: capability declarations resolve per knob."""

    def test_two_knobs_on_different_providers_report_different_capabilities(
        self, monkeypatch
    ):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {
            "llm": {"provider": "api", "providers": {"write": "claude-cli"}}
        }
        write_caps = capabilities_for_knob(config, "write")
        classify_caps = capabilities_for_knob(config, "classify")
        assert write_caps.supports_batches is False
        assert classify_caps.supports_batches is True
        assert write_caps == capabilities_for("claude-cli")
        assert classify_caps == capabilities_for("api")

    def test_no_override_matches_global_capabilities(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {"llm": {"provider": "claude-cli"}}
        assert capabilities_for_knob(config, "resolve") == capabilities_for(
            "claude-cli"
        )

    def test_unknown_knob_override_raises_naming_the_knob(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {"llm": {"providers": {"classify": "bedrock"}}}
        with pytest.raises(ProviderConfigError) as exc_info:
            capabilities_for_knob(config, "classify")
        assert "classify" in str(exc_info.value)

    def test_default_param_forwarded(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        config = {"llm": {"provider": "api"}}
        caps = capabilities_for_knob(config, "write", default="claude-cli")
        assert caps.supports_batches is False


# ---------------------------------------------------------------------------
# reported_stop_reason — trust stop_reason only when the backend reports it (athenaeum#574)
# ---------------------------------------------------------------------------


class TestReportedStopReason:
    def test_none_when_backend_cannot_report(self):
        # Even a stop_reason that LOOKS meaningful is suppressed for claude-cli:
        # the CLI envelope's stop_reason is not a faithful mirror (it can carry
        # a spurious value), so trusting it routes truncation the wrong way.
        resp = SimpleNamespace(stop_reason="max_tokens")
        assert reported_stop_reason(resp, capabilities_for("claude-cli")) is None

    def test_passthrough_when_backend_reports(self):
        resp = SimpleNamespace(stop_reason="max_tokens")
        assert reported_stop_reason(resp, capabilities_for("api")) == "max_tokens"

    def test_non_str_stop_reason_coerces_to_none(self):
        resp = SimpleNamespace(stop_reason=object())
        assert reported_stop_reason(resp, capabilities_for("api")) is None

    def test_missing_stop_reason_is_none(self):
        assert reported_stop_reason(SimpleNamespace(), capabilities_for("api")) is None


# ---------------------------------------------------------------------------
# resolve_max_tokens — per-stage budget, env > yaml > default (athenaeum#575)
# ---------------------------------------------------------------------------


class TestResolveMaxTokens:
    _ENV = "ATHENAEUM_CLASSIFY_MAX_TOKENS"

    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv(self._ENV, raising=False)
        assert resolve_max_tokens("classify", self._ENV, 4096, None) == 4096
        assert resolve_max_tokens("classify", self._ENV, 4096, {}) == 4096

    def test_env_wins_over_yaml_and_default(self, monkeypatch):
        monkeypatch.setenv(self._ENV, "1500")
        config = {"max_tokens": {"classify": 999}}
        assert resolve_max_tokens("classify", self._ENV, 4096, config) == 1500

    def test_yaml_over_default(self, monkeypatch):
        monkeypatch.delenv(self._ENV, raising=False)
        config = {"max_tokens": {"classify": 2222}}
        assert resolve_max_tokens("classify", self._ENV, 4096, config) == 2222

    def test_invalid_env_falls_through_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv(self._ENV, "not-a-number")
        with caplog.at_level("WARNING"):
            got = resolve_max_tokens("classify", self._ENV, 4096, None)
        assert got == 4096
        assert any("not a positive integer" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("bad", ["0", "-10"])
    def test_non_positive_env_falls_through(self, monkeypatch, bad):
        # A budget of 0 would truncate every response — never honor it.
        monkeypatch.setenv(self._ENV, bad)
        assert resolve_max_tokens("classify", self._ENV, 4096, None) == 4096

    @pytest.mark.parametrize("bad", [0, -5, True, "2048", 3.5])
    def test_invalid_yaml_falls_through_to_default(self, monkeypatch, bad):
        # Non-positive, bool (an int subclass), and non-int yaml values are all
        # rejected in favor of the code default.
        monkeypatch.delenv(self._ENV, raising=False)
        config = {"max_tokens": {"classify": bad}}
        assert resolve_max_tokens("classify", self._ENV, 4096, config) == 4096

    def test_only_the_named_knob_is_read(self, monkeypatch):
        monkeypatch.delenv(self._ENV, raising=False)
        config = {"max_tokens": {"merge_full": 8192}}
        # A knob absent from the section falls to the default, not another knob.
        assert resolve_max_tokens("classify", self._ENV, 4096, config) == 4096


# ---------------------------------------------------------------------------
# resolve_thinking — per-stage thinking posture, env > yaml > default (athenaeum#578)
# ---------------------------------------------------------------------------


class TestResolveThinking:
    _ENV = "ATHENAEUM_RESOLVE_THINKING"

    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv(self._ENV, raising=False)
        assert resolve_thinking("resolve", self._ENV, "adaptive", None) == {
            "type": "adaptive"
        }
        assert resolve_thinking("resolve", self._ENV, "adaptive", {}) == {
            "type": "adaptive"
        }

    def test_default_disabled(self, monkeypatch):
        monkeypatch.delenv(self._ENV, raising=False)
        assert resolve_thinking("classify", self._ENV, "disabled", None) == {
            "type": "disabled"
        }

    def test_env_wins_over_yaml_and_default(self, monkeypatch):
        monkeypatch.setenv(self._ENV, "disabled")
        config = {"thinking": {"resolve": "adaptive"}}
        assert resolve_thinking("resolve", self._ENV, "adaptive", config) == {
            "type": "disabled"
        }

    def test_env_is_case_insensitive_and_trims_whitespace(self, monkeypatch):
        monkeypatch.setenv(self._ENV, "  ADAPTIVE  ")
        assert resolve_thinking("resolve", self._ENV, "disabled", None) == {
            "type": "adaptive"
        }

    def test_yaml_over_default(self, monkeypatch):
        monkeypatch.delenv(self._ENV, raising=False)
        config = {"thinking": {"resolve": "disabled"}}
        assert resolve_thinking("resolve", self._ENV, "adaptive", config) == {
            "type": "disabled"
        }

    def test_invalid_env_falls_through_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv(self._ENV, "enabled")  # not a valid posture
        with caplog.at_level("WARNING"):
            got = resolve_thinking("resolve", self._ENV, "adaptive", None)
        assert got == {"type": "adaptive"}
        assert any(
            "not 'adaptive' or 'disabled'" in r.getMessage() for r in caplog.records
        )

    def test_invalid_yaml_falls_through_to_default(self, monkeypatch):
        monkeypatch.delenv(self._ENV, raising=False)
        config = {"thinking": {"resolve": "budget_tokens"}}
        assert resolve_thinking("resolve", self._ENV, "adaptive", config) == {
            "type": "adaptive"
        }

    @pytest.mark.parametrize("bad", [123, True, None, ["adaptive"]])
    def test_non_string_yaml_falls_through_to_default(self, monkeypatch, bad):
        monkeypatch.delenv(self._ENV, raising=False)
        config = {"thinking": {"resolve": bad}}
        assert resolve_thinking("resolve", self._ENV, "adaptive", config) == {
            "type": "adaptive"
        }

    def test_only_the_named_knob_is_read(self, monkeypatch):
        monkeypatch.delenv(self._ENV, raising=False)
        config = {"thinking": {"merge_patch": "disabled"}}
        # A knob absent from the section falls to the default, not another knob.
        assert resolve_thinking("resolve", self._ENV, "adaptive", config) == {
            "type": "adaptive"
        }

    def test_never_returns_none(self, monkeypatch):
        # Per issue athenaeum#578's acceptance criteria: prefer an explicit disabled
        # dict over None so no stage relies on the model default.
        monkeypatch.delenv(self._ENV, raising=False)
        result = resolve_thinking("resolve", self._ENV, "disabled", None)
        assert result is not None
        assert result == {"type": "disabled"}

    def test_returns_the_dict_shape_the_sdk_expects(self, monkeypatch):
        monkeypatch.delenv(self._ENV, raising=False)
        result = resolve_thinking("resolve", self._ENV, "adaptive", None)
        assert isinstance(result, dict)
        assert set(result) == {"type"}
        assert result["type"] in ("adaptive", "disabled")
