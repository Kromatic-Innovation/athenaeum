# SPDX-License-Identifier: Apache-2.0
"""Tests for the provider-seam fix in ``measure_contradiction_baseline.py``
(issue athenaeum#780, L7).

The script used to construct ``anthropic.Anthropic(...)`` directly — the
only such construction outside ``provider.py::build_llm_client`` in the
repo — which meant it could never use the ``claude-cli`` subscription
backend. ``_build_client`` now routes through
:func:`athenaeum.provider.build_llm_client` like every other call site.
These tests prove: the ``api`` path is byte-for-byte unchanged
(``max_retries=3``, ``None`` without a key), and the ``claude-cli`` path —
previously unreachable from this script — now works.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "measure_contradiction_baseline.py"
)

_spec = importlib.util.spec_from_file_location(
    "measure_contradiction_baseline", _SCRIPT
)
assert _spec and _spec.loader
measure_contradiction_baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure_contradiction_baseline)


class TestBuildClient:
    def test_api_path_with_key_matches_prior_kwargs(self, monkeypatch):
        """Byte-for-byte AC: ``max_retries=3`` still reaches the SDK client."""
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k-123")
        captured = {}

        class FakeAnthropic:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
        client = measure_contradiction_baseline._build_client(None)
        assert isinstance(client, FakeAnthropic)
        assert captured == {"api_key": "k-123", "max_retries": 3}

    def test_api_path_without_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert measure_contradiction_baseline._build_client(None) is None

    def test_claude_cli_provider_now_reachable(self, monkeypatch):
        """Previously impossible: the script could only ever build a raw SDK
        client. Routing through the seam means a ``claude-cli`` provider
        config now returns the subscription-backed adapter instead.
        """
        from athenaeum.provider import ClaudeCliClient

        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
        client = measure_contradiction_baseline._build_client(None)
        assert isinstance(client, ClaudeCliClient)

    def test_no_direct_anthropic_construction_outside_provider_seam(self):
        """Regression guard for the AC: grep -rn 'anthropic\\.Anthropic(' over
        src/ and scripts/ must return zero hits outside provider.py.
        """
        import re

        repo_root = Path(__file__).resolve().parent.parent
        pattern = re.compile(r"anthropic\.Anthropic\(")
        hits = []
        for base in ("src", "scripts"):
            for path in (repo_root / base).rglob("*.py"):
                if path.name == "provider.py":
                    continue
                if pattern.search(path.read_text(encoding="utf-8")):
                    hits.append(str(path.relative_to(repo_root)))
        assert hits == []
