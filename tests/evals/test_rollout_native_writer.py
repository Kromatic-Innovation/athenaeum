# SPDX-License-Identifier: Apache-2.0
"""Offline coverage for the native writer runner (issue athenaeum#1726 AC3).

All ``claude`` subprocess calls are stubbed via a monkeypatched
``subprocess.run`` -- no live model, no credential, mirroring how the read
arms (NATIVE_INDEX/NATIVE_GREP) are exercised for real only in
``tests/evals/test_rollout_native_spike.py`` (``pytest.mark.rollout``,
credential/binary-gated) while their WIRING (argv shape, stdin discipline,
transcript parsing) is otherwise covered offline. These tests check the
runner's plumbing only: argv/env/stdin shape, one session per observation,
transcript capture, and the "wrote nothing is not an error" contract --
never a real model's write judgment.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.evals import rollout
from tests.evals.corpus import Observation


def _obs(i: int, body: str = "") -> Observation:
    return Observation(
        uid=f"obs-{i:03d}",
        page_uid="page-x",
        source="sessions",
        timestamp=f"2026010{i}T000000Z",
        uuid8=f"aaaaaa{i:02d}",
        body=body or f"note number {i}",
    )


def _fake_stdout(*, wrote: bool) -> str:
    events: list[dict[str, Any]] = [
        {"type": "system", "subtype": "init", "mcp_servers": [], "tools": []}
    ]
    message: dict[str, Any] = {"usage": {"input_tokens": 12, "output_tokens": 4}, "content": []}
    if wrote:
        message["content"].append(
            {"type": "tool_use", "name": "Write", "input": {"file_path": "note.md"}}
        )
    message["content"].append({"type": "text", "text": "noted."})
    events.append({"type": "assistant", "message": message})
    events.append({"type": "result", "result": "noted."})
    return "\n".join(json.dumps(e) for e in events)


def test_raises_runtime_error_when_claude_binary_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rollout.shutil, "which", lambda _binary: None)
    with pytest.raises(RuntimeError, match="not found on PATH"):
        rollout.run_native_writer([_obs(1)], tmp_path)


def test_one_session_per_observation_prompt_on_stdin_never_argv(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(rollout.shutil, "which", lambda _binary: "/usr/bin/claude")
    calls: list[dict[str, Any]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append({"argv": argv, **kwargs})
        return SimpleNamespace(stdout=_fake_stdout(wrote=True), stderr="")

    monkeypatch.setattr(rollout.subprocess, "run", fake_run)

    observations = [_obs(1, "first fact"), _obs(2, "second fact")]
    result = rollout.run_native_writer(observations, tmp_path, claude_binary="claude")

    assert len(calls) == 2
    for call, observation in zip(calls, observations, strict=True):
        assert call["input"] == f"{rollout._WRITER_PROMPT_PREFIX}{observation.body}"
        assert observation.body not in call["argv"]
        assert call["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-config")
        assert "--strict-mcp-config" in call["argv"]
        assert str(tmp_path / "memory") in call["argv"]

    assert [s.observation_uid for s in result.sessions] == ["obs-001", "obs-002"]
    assert result.memory_dir == tmp_path / "memory"
    assert len(result.sessions[0].tool_calls) == 1
    assert result.total_tool_calls == 2


def test_session_that_writes_nothing_is_a_recorded_outcome_not_an_error(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(rollout.shutil, "which", lambda _binary: "/usr/bin/claude")
    monkeypatch.setattr(
        rollout.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(stdout=_fake_stdout(wrote=False), stderr=""),
    )

    result = rollout.run_native_writer([_obs(1)], tmp_path)

    assert result.sessions[0].tool_calls == []
    assert result.total_tool_calls == 0


def test_memory_files_reads_back_whatever_the_model_actually_wrote(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(rollout.shutil, "which", lambda _binary: "/usr/bin/claude")

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        # Stand in for the model's own Write tool call landing a file on
        # disk under the auto-memory directory this runner prepared.
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "policy-notes.md").write_text("kept fact", encoding="utf-8")
        return SimpleNamespace(stdout=_fake_stdout(wrote=True), stderr="")

    monkeypatch.setattr(rollout.subprocess, "run", fake_run)

    result = rollout.run_native_writer([_obs(1)], tmp_path)

    assert result.memory_files == {"policy-notes.md": "kept fact"}


def test_spawn_timeout_propagates_uncaught(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rollout.shutil, "which", lambda _binary: "/usr/bin/claude")

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(rollout.subprocess, "run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        rollout.run_native_writer([_obs(1)], tmp_path)


def test_sessions_share_one_fresh_memory_directory(tmp_path: Path, monkeypatch) -> None:
    """All sessions in the sequence point at the SAME memory dir, so a later
    session can see an earlier one's writes -- unlike NATIVE_INDEX/NATIVE_GREP,
    which each get a fresh materialized store per probe."""
    monkeypatch.setattr(rollout.shutil, "which", lambda _binary: "/usr/bin/claude")
    memory_dirs: list[str] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        memory_dirs.append(argv[argv.index("--add-dir") + 1])
        return SimpleNamespace(stdout=_fake_stdout(wrote=False), stderr="")

    monkeypatch.setattr(rollout.subprocess, "run", fake_run)

    rollout.run_native_writer([_obs(1), _obs(2), _obs(3)], tmp_path)

    assert len(set(memory_dirs)) == 1
