# SPDX-License-Identifier: Apache-2.0
"""Model + session-ref stamping on AI-attributed auto-memory (issue athenaeum#2148).

The companion half of ``mcp_server.remember``'s default-inferred stamping
(covered by ``tests/test_mcp_server.py::TestDefaultInferredModelAndSession``)
lives in ``intake.py``'s ``coerce_source_type`` block: when a discovered
``AutoMemoryFile``'s coerced ``source_type`` is AI-attributed
(``models.AI_ATTRIBUTED_SOURCE_TYPES``) and it declares no ``model:``, one is
filled IN MEMORY (never written back to raw -- raw stays write-once on this
path), with a three-rung fallback:

1. the model on the assistant record that WROTE this file
   (``session_recovery.SessionRecoverer.model_for_write``);
2. failing that, the origin session's own first assistant turn
   (``SessionRecoverer.model_for_session``);
3. the honest ``"unknown"``.

An empty ``source_ref`` is filled the same way, from
``transcript_verify._best_effort_ref``.

These tests inject a synthetic ``projects_root`` under ``tmp_path``; the
real ``~/.claude`` is never touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from athenaeum import session_recovery
from athenaeum.intake import discover_auto_memory_files
from athenaeum.models import AI_ATTRIBUTED_SOURCE_TYPES

SCOPE = "-Users-alice-Code-projectx"
T0 = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)


def _ts(offset_minutes: int) -> str:
    return (T0 + timedelta(minutes=offset_minutes)).isoformat().replace("+00:00", "Z")


def _write_transcript(
    projects_root: Path, session_id: str, records: list[dict[str, object]], scope: str = SCOPE
) -> Path:
    scope_dir = projects_root / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _tool_use_record(
    tool: str, path: str, offset_minutes: int, model: str, session_id: str
) -> dict[str, object]:
    """An assistant turn invoking a WRITING tool against ``path``, carrying
    ``model`` on the SAME record (issue athenaeum#2148 rung 1)."""
    return {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": _ts(offset_minutes),
        "message": {
            "role": "assistant",
            "model": model,
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": tool,
                    "input": {"file_path": path, "content": "..."},
                }
            ],
        },
    }


def _text_record(text: str, offset_minutes: int) -> dict[str, object]:
    return {
        "type": "user",
        "timestamp": _ts(offset_minutes),
        "message": {"role": "user", "content": text},
    }


def _native_path(projects_root: Path, filename: str, scope: str = SCOPE) -> str:
    return str(projects_root / scope / "memory" / filename)


def _write_config(knowledge_root: Path) -> None:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n", encoding="utf-8"
    )
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)


def _write_memory(knowledge_root: Path, filename: str, text: str, scope: str = SCOPE) -> Path:
    scope_dir = knowledge_root / "raw" / "auto-memory" / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(text, encoding="utf-8")
    return path


class TestModelFillFromWriteCitedRecord:
    """AC4: model filled from the writing tool_use's assistant record, as
    shown by a fixture transcript where the model changes mid-session."""

    def test_model_changes_mid_session_fills_latest_writer(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        _write_config(knowledge_root)

        filename = "feedback_cache-strategy.md"
        text = (
            "---\n"
            "name: cache-strategy\n"
            "description: a guess about caching\n"
            "originSessionId: sess-a\n"
            "source_type: inferred\n"
            "---\n"
            "A guess about caching strategy.\n"
        )
        _write_memory(knowledge_root, filename, text)

        native_path = _native_path(projects_root, filename)
        _write_transcript(
            projects_root,
            "sess-a",
            [
                _text_record("let's cache this", 0),
                _tool_use_record("Write", native_path, 1, "claude-opus-4-0", "sess-a"),
                # The SAME session rewrites the file later under a DIFFERENT
                # model -- the LATEST writer's model must win, matching the
                # `writes[name]` latest-wins rule the recovery index already
                # enforces for the write TIME.
                _tool_use_record("Edit", native_path, 5, "claude-haiku-4-0", "sess-a"),
            ],
        )

        (entry,) = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert entry.model == "claude-haiku-4-0"
        assert entry.source_type in AI_ATTRIBUTED_SOURCE_TYPES

    def test_explicit_model_is_never_overwritten(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        _write_config(knowledge_root)
        filename = "feedback_explicit.md"
        text = (
            "---\n"
            "name: explicit\n"
            "originSessionId: sess-b\n"
            "source_type: model-prior\n"
            "model: caller-declared-model\n"
            "---\n"
            "body\n"
        )
        _write_memory(knowledge_root, filename, text)
        (entry,) = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert entry.model == "caller-declared-model"


class TestModelFallbackToSessionAssistantModel:
    """Rung 2: the session's own first assistant-turn model, mirroring
    ``transcript_verify._assistant_model`` without a second file read."""

    def test_falls_back_to_first_assistant_model_in_session(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        _write_config(knowledge_root)
        filename = "feedback_no-write-cite.md"
        text = (
            "---\n"
            "name: no-write-cite\n"
            "originSessionId: sess-c\n"
            "source_type: inferred\n"
            "---\n"
            "No matching tool_use names this file.\n"
        )
        _write_memory(knowledge_root, filename, text)
        _write_transcript(
            projects_root,
            "sess-c",
            [
                _tool_use_record(
                    "Write", "/some/other/file.md", 0, "claude-sonnet-4-5", "sess-c"
                ),
                _text_record("unrelated", 2),
            ],
        )
        (entry,) = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert entry.model == "claude-sonnet-4-5"

    def test_unknown_when_nothing_resolves(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        _write_config(knowledge_root)
        filename = "feedback_unknown.md"
        text = (
            "---\nname: unknown-model\noriginSessionId: sess-missing\n"
            "source_type: inferred\n---\nbody\n"
        )
        _write_memory(knowledge_root, filename, text)
        (entry,) = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert entry.model == "unknown"

    def test_non_ai_attributed_type_gets_no_model_fill(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        _write_config(knowledge_root)
        filename = "user_claim.md"
        text = (
            "---\nname: user-claim\noriginSessionId: sess-d\n"
            "source_type: user-stated\n---\nbody\n"
        )
        _write_memory(knowledge_root, filename, text)
        (entry,) = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert entry.model == ""


class TestSourceRefFillForAIAttributed:
    def test_empty_source_ref_filled_with_best_effort_ref(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        _write_config(knowledge_root)
        filename = "feedback_ref.md"
        text = (
            "---\nname: ref-test\noriginSessionId: sess-e\noriginTurn: 3\n"
            "source_type: inferred\n---\nbody\n"
        )
        _write_memory(knowledge_root, filename, text)
        (entry,) = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert entry.source_ref == "sess-e#turn3"

    def test_explicit_source_ref_is_never_overwritten(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        _write_config(knowledge_root)
        filename = "feedback_ref2.md"
        text = (
            "---\nname: ref-test-2\noriginSessionId: sess-f\noriginTurn: 1\n"
            "source_type: inferred\nsource_ref: already-set:ref\n---\nbody\n"
        )
        _write_memory(knowledge_root, filename, text)
        (entry,) = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert entry.source_ref == "already-set:ref"


class TestTranscriptReadBudget:
    """AC5 / athenaeum#2048: the intake fill adds no transcript reads beyond
    one per transcript per run."""

    def test_three_memories_sharing_a_scope_scan_each_transcript_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        _write_config(knowledge_root)

        for i in range(3):
            text = (
                f"---\nname: m{i}\noriginSessionId: sess-shared\n"
                "source_type: inferred\n---\nmemory {i}\n"
            )
            _write_memory(knowledge_root, f"feedback_m{i}.md", text)

        _write_transcript(
            projects_root,
            "sess-shared",
            [_tool_use_record("Write", "/irrelevant/path.md", 0, "claude-x", "sess-shared")],
        )

        calls = {"n": 0}
        real_scan = session_recovery._scan_transcript

        def counting_scan(jsonl: Path, scope: str = "") -> Any:
            calls["n"] += 1
            return real_scan(jsonl, scope)

        monkeypatch.setattr(session_recovery, "_scan_transcript", counting_scan)

        files = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert len(files) == 3
        assert all(f.model == "claude-x" for f in files)
        # One scan of the ONE transcript file in the scope, no matter how
        # many memories in that scope needed a model fill.
        assert calls["n"] == 1
