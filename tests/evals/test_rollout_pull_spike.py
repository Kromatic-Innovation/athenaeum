# SPDX-License-Identifier: Apache-2.0
"""The PULL spike, encoded as tests (issue athenaeum#1522).

The spike proved two things, and this module keeps them structurally
separate because they have different gates:

1. **The MCP leg is proven and reproducible WITHOUT credentials.**
   ``test_pull_spike_mcp_connects_and_recall_tool_is_available`` spawns the
   real ``claude`` binary with a scoped ``--mcp-config`` +
   ``--strict-mcp-config`` + ``--output-format stream-json --verbose`` and
   asserts the ``system``/``init`` event names the athenaeum MCP server
   ``connected`` and lists ``mcp__athenaeum__recall`` in ``tools`` — exactly
   what the recorded spike evidence
   (``/box-claude-context/evidence/pull-spike-stream.jsonl`` in the
   container the spike ran in) showed, before the run failed at the MODEL
   TURN with "Not logged in · Please run /login". This test does not touch
   the model turn, so it needs no credential. Skips cleanly
   (``pytest.skip``) when the ``claude`` binary is absent from ``PATH``.

2. **The model-turn leg (an actual tool-use decision) is credential-gated.**
   ``test_pull_tool_use_loop_calls_recall_when_prompted`` runs the full
   loop and asserts a real ``tool_use`` block naming
   ``mcp__athenaeum__recall`` appears with its query captured. Gated the
   same way ``tests/regression/test_live_prompt_regression.py`` gates its
   live-LLM test — ``ATHENAEUM_LIVE_TESTS=1`` — plus a check that ``claude``
   is actually on ``PATH`` (the credential PULL needs is a logged-in
   subscription, not ``ANTHROPIC_API_KEY``; there is no way to probe
   "logged in" without running the CLI, which is what this test itself
   does, so the env var is the gate, exactly mirroring the existing idiom's
   shape rather than inventing a new one).

Both tests carry ``pytest.mark.rollout`` (deselected by default alongside
``eval``/``embedding``, see ``pyproject.toml``), so neither runs in the
default selection or in the ``eval`` selection either way — the env-var
skip is belt-and-suspenders, matching how the ``live`` marker's own test
combines a marker with a skipif rather than relying on either alone.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path

import pytest

from tests.evals.corpus import build_corpus
from tests.evals.rollout import RECALL_TOOL_NAME, run_pull

pytestmark = pytest.mark.rollout


def _pto_probe():
    corpus = build_corpus("core")
    probe = next(p for p in corpus.probes if p.id == "pto_allowance")
    return probe, corpus


def test_pull_spike_mcp_connects_and_recall_tool_is_available(tmp_path: Path) -> None:
    if shutil.which("claude") is None:
        pytest.skip("claude binary not on PATH")

    probe, corpus = _pto_probe()
    corpus.materialize(tmp_path)
    cache_dir = tmp_path / "cache"

    record = run_pull(probe, tmp_path, cache_dir, "core", timeout=60.0)

    init_events = [
        event
        for event in record.transcript
        if event.get("type") == "system" and event.get("subtype") == "init"
    ]
    assert init_events, f"no system/init event in the stream; transcript={record.transcript!r}"
    init = init_events[0]

    servers = init.get("mcp_servers") or []
    assert any(
        isinstance(s, dict) and s.get("name") == "athenaeum" and s.get("status") == "connected"
        for s in servers
    ), f"athenaeum MCP server not connected: {servers!r}"
    assert RECALL_TOOL_NAME in (init.get("tools") or []), (
        f"{RECALL_TOOL_NAME} missing from init tools list: {init.get('tools')!r}"
    )


_LIVE_GATE_REASON = (
    "set ATHENAEUM_LIVE_TESTS=1 with a logged-in `claude` CLI on PATH to run "
    "the PULL tool-use-loop rollout test"
)


@pytest.mark.skipif(
    os.environ.get("ATHENAEUM_LIVE_TESTS") != "1" or shutil.which("claude") is None,
    reason=_LIVE_GATE_REASON,
)
def test_pull_tool_use_loop_calls_recall_when_prompted(tmp_path: Path) -> None:
    probe, corpus = _pto_probe()
    # Nudge the model to use recall explicitly, matching the proven spike
    # prompt shape ("Use recall to find ...").
    probe = dataclasses.replace(probe, query=f"Use recall to find: {probe.query}")
    corpus.materialize(tmp_path)
    cache_dir = tmp_path / "cache"

    record = run_pull(probe, tmp_path, cache_dir, "core", timeout=120.0)

    assert record.recall_called, f"expected a recall tool call; transcript={record.transcript!r}"
    assert len(record.tool_calls) >= 1
    call = record.tool_calls[0]
    assert call.name == RECALL_TOOL_NAME
    assert call.query
