# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.memory_discovery` (issue athenaeum#2316).

Covers AC2 (file/project counts + estimate before anything is read) and
AC5 (discovery finds Claude Code memory across projects using file metadata
only, until the user agrees) — proven here by making the discovered files'
content UNREADABLE and asserting discovery still succeeds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.memory_discovery import (
    MemoryFile,
    discover_project_memories,
    estimate_cost,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestDiscoverProjectMemories:
    def test_finds_memory_files_across_projects(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        _write(root / "proj-a" / "memory" / "MEMORY.md", "a" * 10)
        _write(root / "proj-a" / "memory" / "topic-1.md", "b" * 20)
        _write(root / "proj-b" / "memory" / "MEMORY.md", "c" * 30)

        discovered = discover_project_memories(root)

        assert [p.scope for p in discovered] == ["proj-a", "proj-b"]
        by_scope = {p.scope: p for p in discovered}
        assert by_scope["proj-a"].file_count == 2
        assert by_scope["proj-a"].total_bytes == 30
        assert by_scope["proj-b"].file_count == 1

    def test_scope_with_no_memory_dir_is_omitted(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        (root / "proj-no-memory").mkdir(parents=True)
        _write(root / "proj-has-memory" / "memory" / "MEMORY.md", "x")

        discovered = discover_project_memories(root)

        assert [p.scope for p in discovered] == ["proj-has-memory"]

    def test_empty_memory_dir_is_omitted(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        (root / "proj-a" / "memory").mkdir(parents=True)

        assert discover_project_memories(root) == []

    def test_missing_projects_root_returns_empty(self, tmp_path: Path) -> None:
        assert discover_project_memories(tmp_path / "does-not-exist") == []

    def test_non_md_files_are_ignored(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        _write(root / "proj-a" / "memory" / "MEMORY.md", "x")
        _write(root / "proj-a" / "memory" / "notes.txt", "y" * 100)

        discovered = discover_project_memories(root)

        assert discovered[0].file_count == 1

    def test_metadata_only_never_reads_file_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5: discovery must work from metadata alone — prove it by making
        every discovered file's content UNREADABLE and asserting discovery
        still succeeds with correct counts/sizes."""
        root = tmp_path / "projects"
        _write(root / "proj-a" / "memory" / "MEMORY.md", "hello world")

        def _boom(self: Path, *a: object, **k: object) -> str:
            raise AssertionError("discovery must not read file content")

        monkeypatch.setattr(Path, "read_text", _boom)
        monkeypatch.setattr(Path, "read_bytes", _boom)

        discovered = discover_project_memories(root)

        assert discovered[0].file_count == 1
        assert discovered[0].files[0].size_bytes == len("hello world")


class TestEstimateCost:
    def _file(self, size: int) -> MemoryFile:
        return MemoryFile(path=Path("/x/f.md"), size_bytes=size, mtime=0.0)

    def test_subscription_path_reports_tokens_only(self) -> None:
        cost = estimate_cost([self._file(400)], project_count=1, provider="claude-cli")
        assert cost.estimated_tokens == 100
        assert cost.estimated_cost_usd is None

    def test_api_path_also_reports_dollars(self) -> None:
        cost = estimate_cost([self._file(4_000_000)], project_count=1, provider="api")
        assert cost.estimated_tokens == 1_000_000
        assert cost.estimated_cost_usd is not None
        assert cost.estimated_cost_usd > 0

    def test_file_and_project_counts_carried_through(self) -> None:
        cost = estimate_cost(
            [self._file(4), self._file(8)], project_count=2, provider="claude-cli"
        )
        assert cost.file_count == 2
        assert cost.project_count == 2

    def test_empty_files_is_zero_cost(self) -> None:
        cost = estimate_cost([], project_count=0, provider="api")
        assert cost.estimated_tokens == 0
        assert cost.estimated_cost_usd == 0.0
