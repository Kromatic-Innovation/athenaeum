# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum import-memory`` (issue athenaeum#2316).

No test in this module makes a real compile call (issue athenaeum#2316
ruling 5) — every call to :func:`run_import` substitutes a fake
``compile_fn``. Fixtures write memory files under ``tmp_path`` only; no
``/home``, ``/Users``, or ``~`` path literal appears anywhere here.

``TestDefaultCompileBatch`` (issue athenaeum#2582) is the one exception to
"every call substitutes a fake ``compile_fn``": it calls the REAL
:func:`_default_compile_batch` directly, with only its own deepest
dependency — :func:`athenaeum.librarian.run` itself — patched to a
recording/sentinel-detecting stand-in, never a real compile call.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import time
from pathlib import Path

import pytest

from athenaeum._cmd_import_memory import (
    EXIT_INCOMPLETE,
    _cmd_import_memory,
    _default_compile_batch,
    _is_imported,
    _pending_files,
    check_provider_usable,
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


class TestRelevanceRefusalHandled:
    """athenaeum#2645: a relevance-gate refusal is recorded as handled."""

    def test_never_rule_file_is_staged_once_and_cap_not_consumed(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "claude-projects"
        _write(
            root / "proj-a" / "memory" / "refused.md",
            "---\nname: r\ntype: preference\ntags: [active]\n---\nbody\n",
        )
        _write(root / "proj-a" / "memory" / "zlater.md", "content of zlater")
        knowledge_root = tmp_path / "kb"
        schema = knowledge_root / "wiki" / "_schema"
        _write(
            schema / "relevance-policy.md",
            "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n"
            "| never | | active | |\n",
        )
        cache_dir = tmp_path / "cache"
        compiles: list[dict[str, object]] = []

        def _compile(kr: Path, **kw: object) -> int:
            compiles.append(kw)
            # real pipeline: compiles+retires everything except the refused file
            for p in (kr / "raw" / "auto-memory").rglob("*.md"):
                if p.name != "refused.md":
                    p.unlink()
            return 0

        def _run(max_files: int, out: io.StringIO) -> int:
            return run_import(
                knowledge_root=knowledge_root,
                cache_dir=cache_dir,
                provider="claude-cli",
                discovered=discover_project_memories(root),
                cli_projects=["proj-a"],
                auto_yes=True,
                max_files=max_files,
                max_calls=100,
                compile_fn=_compile,
                out=out,
            )

        out1 = io.StringIO()
        assert _run(1, out1) == 0  # cap of 1: the refused file takes it
        rec = load_state(cache_dir)["imported"]["proj-a/refused.md"]
        assert rec["refused"].startswith("relevance-policy:")
        assert "refused by the relevance policy" in out1.getvalue()
        staged = knowledge_root / "raw" / "auto-memory" / "proj-a" / "refused.md"
        assert staged.exists()  # never deleted

        # Second run: the refused file is not restaged and does not use the cap.
        staged.unlink()
        assert _run(1, io.StringIO()) == 0
        assert not staged.exists()  # not restaged
        assert compiles[1]["max_files"] == 1  # the cap went to the other file
        state = load_state(cache_dir)["imported"]
        assert set(state) == {"proj-a/refused.md", "proj-a/zlater.md"}
        assert "refused" not in state["proj-a/zlater.md"]


class TestDefaultCompileBatch:
    """Real calls to :func:`_default_compile_batch` (issue athenaeum#2582).

    ``_default_compile_batch`` used to call ``librarian.run(knowledge_root=...)``
    without ``raw_root``/``wiki_root``, so a real call fell back to
    ``librarian.run``'s own ``DEFAULT_RAW_ROOT``/``DEFAULT_WIKI_ROOT`` — the
    operator's home knowledge base — instead of the root named by
    ``knowledge_root``. Both tests below patch only
    :func:`athenaeum.librarian.run` itself (the deepest external dependency
    of the function under test); neither makes a real compile call.
    """

    def test_passes_raw_and_wiki_roots_derived_from_knowledge_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: a real call records ``raw_root``/``wiki_root`` under the
        NAMED root, never missing and never pointing outside it."""
        recorded: dict[str, object] = {}

        def _fake_run(**kwargs: object) -> int:
            recorded.update(kwargs)
            return 0

        monkeypatch.setattr("athenaeum.librarian.run", _fake_run)

        knowledge_root = tmp_path / "named-root"
        rc = _default_compile_batch(knowledge_root, max_files=7, max_api_calls=42)

        assert rc == 0
        # Fails loudly (KeyError) if either kwarg is missing, as the
        # pre-fix call left them. athenaeum#2582.
        raw_root = recorded["raw_root"]
        wiki_root = recorded["wiki_root"]
        assert raw_root == knowledge_root / "raw"
        assert wiki_root == knowledge_root / "wiki"
        assert knowledge_root in Path(raw_root).parents
        assert knowledge_root in Path(wiki_root).parents
        assert recorded["knowledge_root"] == knowledge_root
        assert recorded["max_files"] == 7
        assert recorded["max_api_calls"] == 42

    def test_default_roots_are_never_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 counter-example: with ``librarian.run``'s OWN defaults
        pointed at a sentinel directory (mirroring its real
        ``DEFAULT_RAW_ROOT``/``DEFAULT_WIKI_ROOT`` contract — a param used
        only when a caller omits the keyword), an import into a temp root
        leaves the sentinel directory untouched. Regressing to the pre-fix
        call (``knowledge_root=`` only) would make this stand-in fall back
        to its own sentinel defaults and write there — failing this test.
        """
        sentinel_raw = tmp_path / "sentinel-home" / "raw"
        sentinel_wiki = tmp_path / "sentinel-home" / "wiki"

        def _fake_run(
            *,
            raw_root: Path = sentinel_raw,
            wiki_root: Path = sentinel_wiki,
            **_: object,
        ) -> int:
            # Simulate a real compile touching both roots it was given —
            # exactly what the home-corpus pass athenaeum#2582 reported did.
            raw_root.mkdir(parents=True, exist_ok=True)
            wiki_root.mkdir(parents=True, exist_ok=True)
            (raw_root / "touched.marker").write_text("x", encoding="utf-8")
            return 0

        monkeypatch.setattr("athenaeum.librarian.run", _fake_run)

        knowledge_root = tmp_path / "named-root"
        rc = _default_compile_batch(knowledge_root, max_files=5, max_api_calls=10)

        assert rc == 0
        assert (knowledge_root / "raw" / "touched.marker").exists()
        assert not sentinel_raw.exists()
        assert not sentinel_wiki.exists()


def _stub_claude(bin_dir: Path, body: str = "exit 1") -> Path:
    """A stand-in ``claude`` on PATH (via *bin_dir*) running shell *body*."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


class TestProviderUsable:
    """A missing login stops the run before any file is read (athenaeum#2735)."""

    @pytest.fixture(autouse=True)
    def _path_only_the_stub(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_CLAUDE_CLI_BIN", raising=False)
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}/usr/bin:/bin")

    def test_logged_out_stub_gives_a_one_line_fix(self, tmp_path: Path) -> None:
        _stub_claude(tmp_path / "bin", "exit 1")
        message = check_provider_usable("claude-cli")
        assert message is not None
        assert "\n" not in message
        assert "claude auth login" in message

    def test_logged_out_json_status_is_caught(self, tmp_path: Path) -> None:
        _stub_claude(tmp_path / "bin", "echo '{\"loggedIn\": false}'")
        assert check_provider_usable("claude-cli") is not None

    def test_logged_in_stub_is_usable(self, tmp_path: Path) -> None:
        _stub_claude(tmp_path / "bin", "echo '{\"loggedIn\": true}'")
        assert check_provider_usable("claude-cli") is None

    def test_missing_binary_is_reported(self, tmp_path: Path) -> None:
        assert check_provider_usable("claude-cli") is not None

    def test_cli_entrypoint_stops_before_staging_or_state(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Wired through ``_cmd_import_memory``, not only the helper."""
        _stub_claude(tmp_path / "bin", "exit 1")
        projects_root = tmp_path / "claude-projects"
        _write(projects_root / "proj-a" / "memory" / "note.md", "a fact")
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw" / "auto-memory").mkdir(parents=True)
        cache_dir = tmp_path / "cache"
        args = argparse.Namespace(
            path=knowledge_root,
            projects=["proj-a"],
            yes=True,
            max_files=20,
            max_calls=100,
            projects_root=projects_root,
            cache_dir=cache_dir,
        )
        rc = _cmd_import_memory(args)
        assert rc != 0
        err = capsys.readouterr().err
        assert "claude auth login" in err
        assert len(err.strip().splitlines()) == 1
        assert not (knowledge_root / "raw" / "auto-memory" / "proj-a").exists()
        assert load_state(cache_dir)["imported"] == {}

    def test_run_import_never_stages_when_check_fails(self, tmp_path: Path) -> None:
        discovered = _discover(tmp_path, {"proj-a": ["note.md"]})
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw" / "auto-memory").mkdir(parents=True)
        compile_calls: list[object] = []
        rc = run_import(
            knowledge_root=knowledge_root,
            cache_dir=tmp_path / "cache",
            provider="claude-cli",
            discovered=discovered,
            cli_projects=["proj-a"],
            auto_yes=True,
            max_files=20,
            max_calls=100,
            compile_fn=lambda *a, **k: compile_calls.append(a) or 0,
            provider_check=lambda _p: "not logged in.",
        )
        assert rc == EXIT_INCOMPLETE
        assert compile_calls == []
        assert not (knowledge_root / "raw" / "auto-memory" / "proj-a").exists()


class TestAllClassifyCallsFailed:
    """Fail-open classify steps must not read as a clean import (athenaeum#2735)."""

    def _run(self, tmp_path: Path, compile_fn: object) -> tuple[int, Path, Path]:
        discovered = _discover(tmp_path, {"proj-a": ["one.md", "two.md"]})
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw" / "auto-memory").mkdir(parents=True)
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
            compile_fn=compile_fn,  # type: ignore[arg-type]
        )
        return rc, knowledge_root, cache_dir

    def test_every_call_failing_exits_nonzero_and_marks_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _compile(knowledge_root: Path, **_: object) -> int:
            for name in ("claim_kind", "decay_bucket"):
                logging.getLogger(f"athenaeum.{name}").warning(
                    "%s: classify call failed (%s); unclassified",
                    name,
                    "claude CLI exited 1: (no stderr)",
                )
            return _retire_everything(knowledge_root)

        rc, _root, cache_dir = self._run(tmp_path, _compile)
        assert rc == EXIT_INCOMPLETE
        assert load_state(cache_dir)["imported"] == {}
        err = capsys.readouterr().err
        assert "every classify call failed" in err

    def test_some_calls_succeeding_is_not_all_failed(self, tmp_path: Path) -> None:
        def _compile(knowledge_root: Path, **_: object) -> int:
            logging.getLogger("athenaeum.claim_kind").warning(
                "claim_kind: classify call failed (x); unclassified"
            )
            logging.getLogger("athenaeum.decay_bucket").info(
                "decay_bucket: stamped durable on one.md"
            )
            return _retire_everything(knowledge_root)

        rc, _root, cache_dir = self._run(tmp_path, _compile)
        assert rc == 0
        assert len(load_state(cache_dir)["imported"]) == 2

    def test_logger_levels_are_restored(self, tmp_path: Path) -> None:
        before = logging.getLogger("athenaeum.claim_kind").level
        self._run(tmp_path, _retire_everything)
        assert logging.getLogger("athenaeum.claim_kind").level == before
