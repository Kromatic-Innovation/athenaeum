# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum import-memory`` (issue athenaeum#2316).

No test in this module makes a real compile call (issue athenaeum#2316
ruling 5) — every call to :func:`run_import` substitutes a fake
``compile_fn``. Fixtures write memory files under ``tmp_path`` only; no
``/home``, ``/Users``, or ``~`` path literal appears anywhere here.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest

from athenaeum._cmd_import_memory import (
    EXIT_INCOMPLETE,
    _is_imported,
    _pending_files,
    load_state,
    run_import,
    save_state,
    select_projects,
)
from athenaeum.memory_discovery import MemoryFile, ProjectMemory, discover_project_memories


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _retire_everything(knowledge_root: Path, **_: object) -> int:
    """A ``compile_fn`` fake that simulates a FULLY successful real compile:
    the default-on move-then-retire pass unlinks every raw file it
    processes. Every ``run_import`` test whose fixture wants "the compile
    succeeded for everything staged" uses this rather than a bare
    ``lambda *a, **k: 0`` — a fake that leaves staged files in place is
    indistinguishable, post-fix, from one where intake silently dropped
    them (see ``TestRunImport.test_file_silently_skipped_by_intake_is_not_
    marked_imported``), so "succeeded" fakes must actually remove what they
    claim to have compiled.
    """
    for p in (knowledge_root / "raw" / "auto-memory").rglob("*.md"):
        p.unlink()
    return 0


def _discover(tmp_path: Path, projects: dict[str, list[str]]) -> list[ProjectMemory]:
    """Write *projects* (scope -> list of filenames) under a fresh projects
    root and return what :func:`discover_project_memories` finds."""
    root = tmp_path / "claude-projects"
    for scope, filenames in projects.items():
        for name in filenames:
            _write(root / scope / "memory" / name, "content of " + name)
    return discover_project_memories(root)


class TestSelectProjects:
    def test_cli_projects_skip_the_picker(self, tmp_path: Path) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"], "proj-b": ["MEMORY.md"]})
        picker_called = []
        selected, unknown = select_projects(
            discovered, ["proj-a"], picker=lambda d: picker_called.append(d) or []
        )
        assert [p.scope for p in selected] == ["proj-a"]
        assert unknown == []
        assert picker_called == []

    def test_unknown_cli_project_is_reported(self, tmp_path: Path) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"]})
        selected, unknown = select_projects(discovered, ["does-not-exist"])
        assert selected == []
        assert unknown == ["does-not-exist"]

    def test_no_cli_projects_uses_picker_with_nothing_preselected(
        self, tmp_path: Path
    ) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"], "proj-b": ["MEMORY.md"]})

        seen = []

        def fake_picker(projects: list[ProjectMemory]) -> list[str]:
            seen.append([p.scope for p in projects])
            return []  # AC4: blank input selects nothing.

        selected, unknown = select_projects(discovered, None, picker=fake_picker)
        assert selected == []
        assert seen == [["proj-a", "proj-b"]]

    def test_picker_selection_is_honoured(self, tmp_path: Path) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"], "proj-b": ["MEMORY.md"]})
        selected, _ = select_projects(discovered, None, picker=lambda d: ["proj-b"])
        assert [p.scope for p in selected] == ["proj-b"]


class TestResumableState:
    def test_round_trip(self, tmp_path: Path) -> None:
        state = load_state(tmp_path)
        f = MemoryFile(path=Path("/x/MEMORY.md"), size_bytes=10, mtime=123.0)
        assert not _is_imported(state, "proj-a", f)
        state["imported"]["proj-a/MEMORY.md"] = {
            "size_bytes": 10,
            "mtime": 123.0,
            "imported_at": "2026-11-11T00:00:00Z",
        }
        save_state(tmp_path, state)

        reloaded = load_state(tmp_path)
        assert _is_imported(reloaded, "proj-a", f)

    def test_changed_source_file_is_pending_again(self, tmp_path: Path) -> None:
        state = load_state(tmp_path)
        state["imported"]["proj-a/MEMORY.md"] = {
            "size_bytes": 10,
            "mtime": 123.0,
            "imported_at": "x",
        }
        changed = MemoryFile(path=Path("/x/MEMORY.md"), size_bytes=99, mtime=999.0)
        assert not _is_imported(state, "proj-a", changed)

    def test_corrupt_state_file_fails_open(self, tmp_path: Path) -> None:
        (tmp_path / "import_memory_state.json").write_text("{not json", encoding="utf-8")
        state = load_state(tmp_path)
        assert state == {"version": 1, "imported": {}}

    def test_pending_files_excludes_imported(self, tmp_path: Path) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md", "topic.md"]})
        project = discovered[0]
        state = load_state(tmp_path)
        imported_file = next(f for f in project.files if f.path.name == "MEMORY.md")
        state["imported"]["proj-a/MEMORY.md"] = {
            "size_bytes": imported_file.size_bytes,
            "mtime": imported_file.mtime,
            "imported_at": "x",
        }
        pending = _pending_files([project], state)
        assert [f.path.name for _, f in pending] == ["topic.md"]


class _FakeLock:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class TestRunImport:
    def _knowledge_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "knowledge"
        (root / "raw" / "auto-memory").mkdir(parents=True)
        return root

    def test_nothing_selected_reads_nothing(self, tmp_path: Path) -> None:
        """AC1/AC4: declining to select anything is a clean no-op."""
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"]})
        knowledge_root = self._knowledge_root(tmp_path)
        compile_calls = []
        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=tmp_path / "cache",
            provider="claude-cli",
            discovered=discovered,
            cli_projects=None,
            auto_yes=False,
            max_files=20,
            max_calls=100,
            picker=lambda d: [],
            compile_fn=lambda *a, **k: compile_calls.append((a, k)) or 0,
        )
        assert rc == 0
        assert compile_calls == []
        assert not (knowledge_root / "raw" / "auto-memory" / "proj-a").exists()

    def test_declining_confirmation_reads_nothing(self, tmp_path: Path) -> None:
        """AC1/AC7: a decline at the proceed prompt leaves everything untouched,
        and the command itself is otherwise complete (returns 0)."""
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"]})
        knowledge_root = self._knowledge_root(tmp_path)
        compile_calls = []
        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=tmp_path / "cache",
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=False,
            max_files=20,
            max_calls=100,
            confirm=lambda: False,
            compile_fn=lambda *a, **k: compile_calls.append((a, k)) or 0,
        )
        assert rc == 0
        assert compile_calls == []
        assert not (knowledge_root / "raw" / "auto-memory" / "proj-a").exists()

    def test_unchosen_project_is_never_staged_or_read(self, tmp_path: Path) -> None:
        """AC6 counter-example: a project NOT selected is never read, proven
        by making its file's content unreadable and selecting only the
        other project."""
        discovered = _discover(
            tmp_path, {"proj-a": ["MEMORY.md"], "proj-b": ["MEMORY.md"]}
        )
        unchosen_path = next(
            f.path for p in discovered if p.scope == "proj-b" for f in p.files
        )
        knowledge_root = self._knowledge_root(tmp_path)

        real_read_text = Path.read_text

        def _guarded_read_text(self: Path, *a: object, **k: object) -> str:
            if self == unchosen_path:
                raise AssertionError("unchosen project's file was read")
            return real_read_text(self, *a, **k)

        import pytest as _pytest  # local import keeps monkeypatch scoping simple

        mp = _pytest.MonkeyPatch()
        mp.setattr(Path, "read_text", _guarded_read_text)
        try:
            rc = run_import(
                knowledge_root=knowledge_root,
                cache_dir=tmp_path / "cache",
                provider="claude-cli",
                discovered=discovered,
                cli_projects=["proj-a"],
                auto_yes=True,
                max_files=20,
                max_calls=100,
                compile_fn=lambda *a, **k: 0,
            )
        finally:
            mp.undo()

        assert rc == 0
        assert (knowledge_root / "raw" / "auto-memory" / "proj-a" / "MEMORY.md").exists()
        assert not (knowledge_root / "raw" / "auto-memory" / "proj-b").exists()

    def test_confirmed_run_stages_and_marks_imported(self, tmp_path: Path) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md", "topic.md"]})
        knowledge_root = self._knowledge_root(tmp_path)
        cache_dir = tmp_path / "cache"
        staged_during_compile: dict[str, str] = {}

        def _compile(knowledge_root: Path, **_: object) -> int:
            # Capture staging's effect WHILE the staged copy still exists,
            # then retire it -- exactly what a fully successful real
            # compile does by default, and what `run_import`'s post-compile
            # reconciliation now requires to mark a file imported.
            staged = knowledge_root / "raw" / "auto-memory" / "proj-a"
            for p in staged.glob("*.md"):
                staged_during_compile[p.name] = p.read_text(encoding="utf-8")
                p.unlink()
            return 0

        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=cache_dir,
            provider="api",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            compile_fn=_compile,
        )

        assert rc == 0
        assert staged_during_compile == {
            "MEMORY.md": "content of MEMORY.md",
            "topic.md": "content of topic.md",
        }
        staged = knowledge_root / "raw" / "auto-memory" / "proj-a"
        assert not (staged / "MEMORY.md").exists()
        assert not (staged / "topic.md").exists()

        state = json.loads((cache_dir / "import_memory_state.json").read_text())
        assert set(state["imported"]) == {"proj-a/MEMORY.md", "proj-a/topic.md"}

    def test_second_run_skips_already_imported_files(self, tmp_path: Path) -> None:
        """AC8: resuming does not re-select already-imported files."""
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"]})
        knowledge_root = self._knowledge_root(tmp_path)
        cache_dir = tmp_path / "cache"

        rc1 = run_import(
            knowledge_root=knowledge_root,
            cache_dir=cache_dir,
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            compile_fn=_retire_everything,
        )
        assert rc1 == 0

        compile_calls = []
        rc2 = run_import(
            knowledge_root=knowledge_root,
            cache_dir=cache_dir,
            provider="claude-cli",
            discovered=discover_project_memories(discovered[0].memory_dir.parent.parent),
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            compile_fn=lambda *a, **k: compile_calls.append(k) or 0,
        )
        assert rc2 == 0
        assert compile_calls == []  # nothing pending -> compile never invoked again

    def test_run_cap_leaves_remainder_pending(self, tmp_path: Path) -> None:
        """AC3: the run cap leaves a valid store and reports what was left."""
        discovered = _discover(
            tmp_path, {"proj-a": ["a.md", "b.md", "c.md"]}
        )
        knowledge_root = self._knowledge_root(tmp_path)
        cache_dir = tmp_path / "cache"

        compile_calls = []
        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=cache_dir,
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=2,
            max_calls=100,
            compile_fn=lambda *a, **k: compile_calls.append(k) or _retire_everything(*a, **k),
        )
        assert rc == 0
        assert compile_calls[0]["max_files"] == 2
        state = load_state(cache_dir)
        assert len(state["imported"]) == 2

        # Store stays valid: a plain re-run resumes the remaining file.
        rc2 = run_import(
            knowledge_root=knowledge_root,
            cache_dir=cache_dir,
            provider="claude-cli",
            discovered=discover_project_memories(discovered[0].memory_dir.parent.parent),
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=2,
            max_calls=100,
            compile_fn=lambda *a, **k: compile_calls.append(k) or _retire_everything(*a, **k),
        )
        assert rc2 == 0
        state2 = load_state(cache_dir)
        assert len(state2["imported"]) == 3

    def test_file_silently_skipped_by_intake_is_not_marked_imported(
        self, tmp_path: Path
    ) -> None:
        """Seer finding on athenaeum PR 2407: intake can silently drop a
        staged file (named ``MEMORY.md``, unrecognised name+frontmatter, or
        ``classify_ephemeral``-flagged) while the overall compile still
        exits 0 for the rest of the batch. A dropped file must stay
        on disk and PENDING -- never marked imported -- so the next run
        retries exactly it, never losing it permanently (AC3/AC8)."""
        discovered = _discover(tmp_path, {"proj-a": ["kept.md", "dropped.md"]})
        knowledge_root = self._knowledge_root(tmp_path)
        cache_dir = tmp_path / "cache"
        out = io.StringIO()

        def _compile(knowledge_root: Path, **_: object) -> int:
            # "kept.md" compiles and is retired; "dropped.md" is silently
            # skipped by intake (e.g. ephemeral) and stays on disk. Exit
            # code is 0 either way -- intake surfaces no signal of its own.
            (knowledge_root / "raw" / "auto-memory" / "proj-a" / "kept.md").unlink()
            return 0

        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=cache_dir,
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            compile_fn=_compile,
            out=out,
        )

        assert rc == 0
        state = load_state(cache_dir)
        assert set(state["imported"]) == {"proj-a/kept.md"}
        assert "1 file(s) were staged but not compiled" in out.getvalue()
        # The dropped file is untouched on disk -- still there to retry.
        assert (knowledge_root / "raw" / "auto-memory" / "proj-a" / "dropped.md").exists()

        # Rerun: only the skipped file is still pending, and compile is
        # invoked again for exactly it.
        compile_calls = []
        rc2 = run_import(
            knowledge_root=knowledge_root,
            cache_dir=cache_dir,
            provider="claude-cli",
            discovered=discover_project_memories(discovered[0].memory_dir.parent.parent),
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            compile_fn=lambda *a, **k: compile_calls.append(k) or 0,
        )
        assert rc2 == 0
        assert compile_calls == [{"max_files": 1, "max_api_calls": 100}]

    def test_staged_copy_preserves_source_mtime(self, tmp_path: Path) -> None:
        """Seer finding on athenaeum PR 2407: ``_stage_file`` must not reset
        the staged copy's mtime to "now" -- intake's provenance-recovery
        fallback (``session_recovery.written_at_from_frontmatter``'s own
        docstring) relies on the staged file's mtime matching the memory's
        real write time, a property every pre-existing symlink-bridged
        scope got for free and a real file copy does not."""
        root = tmp_path / "claude-projects"
        source = root / "proj-a" / "memory" / "topic.md"
        _write(source, "content of topic.md")
        old_time = time.time() - 7 * 86400  # a week old
        os.utime(source, (old_time, old_time))

        discovered = discover_project_memories(root)
        knowledge_root = self._knowledge_root(tmp_path)

        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=tmp_path / "cache",
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            compile_fn=lambda *a, **k: 0,  # leaves the staged copy in place
        )
        assert rc == 0
        staged = knowledge_root / "raw" / "auto-memory" / "proj-a" / "topic.md"
        assert staged.stat().st_mtime == pytest.approx(old_time, abs=1.0)

    def test_interrupted_run_reports_and_does_not_mark_imported(
        self, tmp_path: Path
    ) -> None:
        """AC8: a raising compile (e.g. a missing/expired login) is reported
        and nothing in the batch is marked imported — safely retryable."""
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"]})
        knowledge_root = self._knowledge_root(tmp_path)
        cache_dir = tmp_path / "cache"

        def _boom(*a: object, **k: object) -> int:
            raise RuntimeError("not logged in")

        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=cache_dir,
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            compile_fn=_boom,
        )
        assert rc == EXIT_INCOMPLETE
        state = load_state(cache_dir)
        assert state["imported"] == {}

    def test_nonzero_exit_does_not_mark_imported(self, tmp_path: Path) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"]})
        knowledge_root = self._knowledge_root(tmp_path)
        cache_dir = tmp_path / "cache"

        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=cache_dir,
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            compile_fn=lambda *a, **k: 75,
        )
        assert rc == EXIT_INCOMPLETE
        state = load_state(cache_dir)
        assert state["imported"] == {}

    def test_lock_acquire_failure_short_circuits(self, tmp_path: Path) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"]})
        knowledge_root = self._knowledge_root(tmp_path)
        compile_calls = []

        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=tmp_path / "cache",
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            acquire_lock=lambda: 75,
            compile_fn=lambda *a, **k: compile_calls.append(k) or 0,
        )
        assert rc == 75
        assert compile_calls == []

    def test_lock_is_released_after_success(self, tmp_path: Path) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"]})
        knowledge_root = self._knowledge_root(tmp_path)
        lock = _FakeLock()

        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=tmp_path / "cache",
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            acquire_lock=lambda: lock,
            compile_fn=lambda *a, **k: 0,
        )
        assert rc == 0
        assert lock.released

    def test_unknown_project_name_errors_without_locking(self, tmp_path: Path) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["MEMORY.md"]})
        knowledge_root = self._knowledge_root(tmp_path)
        lock_calls = []

        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=tmp_path / "cache",
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["nope"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            acquire_lock=lambda: lock_calls.append(1) or _FakeLock(),
            compile_fn=lambda *a, **k: 0,
        )
        assert rc == EXIT_INCOMPLETE
        assert lock_calls == []
