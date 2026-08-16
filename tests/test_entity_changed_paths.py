# SPDX-License-Identifier: Apache-2.0
"""Entity phase honours the caller's ``changed_paths`` (issue athenaeum#900).

A SessionEnd compile is meant to be scoped to what that session just wrote.
Before athenaeum#900 the entity phase always discovered the WHOLE raw tree
(``discover_raw_files``) and then truncated to ``max_files``, while
``changed_paths`` was threaded only into the auto-memory delta path — which
excludes entity raw BY CONSTRUCTION, so an entity-only ingest yielded an empty
delta set. A session's own new files therefore joined the back of a backlog that
routinely exceeds the window and could wait days to compile.

These pin the seam: the caller's files are compiled FIRST, remaining budget
still fills from the backlog in its existing order, and a run with no caller
scope (the nightly) behaves exactly as before.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.librarian import _prioritize_caller_scoped_raw, ingest, run, session_end
from athenaeum.models import RawFile


def _raw(path: Path, ts: str = "20240410T120000Z") -> RawFile:
    return RawFile(path=path, source="sessions", timestamp=ts, uuid8="aabbccdd")


class TestPrioritizeCallerScopedRaw:
    def test_no_changed_set_returns_input_unchanged(self, tmp_path: Path) -> None:
        # The nightly run passes no caller scope — discovery order must survive
        # byte-for-byte, and the SAME list object is handed back.
        files = [_raw(tmp_path / f"{i}.md") for i in range(3)]
        out, n = _prioritize_caller_scoped_raw(files, None)
        assert out is files
        assert n == 0

    def test_empty_changed_set_returns_input_unchanged(self, tmp_path: Path) -> None:
        files = [_raw(tmp_path / f"{i}.md") for i in range(3)]
        out, n = _prioritize_caller_scoped_raw(files, set())
        assert out is files
        assert n == 0

    def test_callers_files_move_to_the_front(self, tmp_path: Path) -> None:
        files = [_raw(tmp_path / f"{i}.md") for i in range(5)]
        changed = {tmp_path / "3.md", tmp_path / "4.md"}

        out, n = _prioritize_caller_scoped_raw(files, changed)

        assert n == 2
        assert [p.path.name for p in out] == ["3.md", "4.md", "0.md", "1.md", "2.md"]

    def test_is_a_stable_partition_not_a_sort(self, tmp_path: Path) -> None:
        # Both groups keep their discovery order among themselves, so the
        # backlog's own ordering (and its fair-share question) is untouched.
        files = [_raw(tmp_path / f"{i}.md") for i in range(6)]
        changed = {tmp_path / "4.md", tmp_path / "1.md"}

        out, _ = _prioritize_caller_scoped_raw(files, changed)

        assert [p.path.name for p in out] == [
            "1.md",
            "4.md",  # caller's, in discovery order
            "0.md",
            "2.md",
            "3.md",
            "5.md",  # backlog, in discovery order
        ]

    def test_changed_set_matching_nothing_leaves_order_alone(
        self, tmp_path: Path
    ) -> None:
        files = [_raw(tmp_path / f"{i}.md") for i in range(3)]
        out, n = _prioritize_caller_scoped_raw(files, {tmp_path / "elsewhere.md"})
        assert out is files
        assert n == 0

    def test_matches_an_unresolved_spelling_of_the_same_file(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "sub").mkdir()
        target = tmp_path / "sub" / "a.md"
        target.write_text("x", encoding="utf-8")
        files = [_raw(tmp_path / "b.md"), _raw(target)]

        out, n = _prioritize_caller_scoped_raw(
            files, {tmp_path / "sub" / ".." / "sub" / "a.md"}
        )

        assert n == 1
        assert out[0].path == target


# ---------------------------------------------------------------------------
# End-to-end: a session-scoped compile beats the backlog
# ---------------------------------------------------------------------------


def _seed(tmp_path: Path) -> Path:
    """Knowledge root on a non-protected branch, raw files written post-commit."""
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "wiki").mkdir()
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    return root


def _recording_process_one(seen: list[str], wiki_root: Path):
    """A ``process_one`` stand-in recording which raw file it was handed."""

    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        seen.append(raw.path.name)
        page = wiki_root / f"entity-{len(seen)}.md"
        page.write_text(f"# Entity\nfrom {raw.ref}\n", encoding="utf-8")
        return SimpleNamespace(created=[page.name], updated=[], escalated=[], skipped=[])

    return fake_process_one


class TestSessionScopedCompile:
    def test_sessions_own_files_compile_though_backlog_exceeds_max_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE acceptance criterion: a backlog larger than the window must not
        # push the session's own writes out of this compile. The session file
        # carries the LATEST timestamp, so discovery (which sorts by timestamp)
        # puts it dead last — beyond a 2-file window — without athenaeum#900.
        root = _seed(tmp_path)
        sessions = root / "raw" / "sessions"
        for i in range(5):
            (sessions / f"2024041{i}T120000Z-aabbccd{i}.md").write_text(
                f"Backlog note {i} about Acme Corp.\n", encoding="utf-8"
            )
        mine = sessions / "20260815T120000Z-deadbeef.md"
        mine.write_text("Met Alice Zhang about the new thing.\n", encoding="utf-8")

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _recording_process_one(seen, root / "wiki")
        )

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=2,
            max_api_calls=100,
            entity_changed_paths={mine},
        )

        assert rc == 0
        # Compiled FIRST, not merely compiled.
        assert seen[0] == mine.name
        # Remaining budget still filled from the backlog, oldest-first.
        assert seen == [mine.name, "20240410T120000Z-aabbccd0.md"]

    def test_without_caller_scope_discovery_order_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The nightly run's behaviour must be byte-identical to pre-athenaeum#900.
        root = _seed(tmp_path)
        sessions = root / "raw" / "sessions"
        for i in range(5):
            (sessions / f"2024041{i}T120000Z-aabbccd{i}.md").write_text(
                f"Backlog note {i} about Acme Corp.\n", encoding="utf-8"
            )
        (sessions / "20260815T120000Z-deadbeef.md").write_text(
            "Met Alice Zhang about the new thing.\n", encoding="utf-8"
        )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _recording_process_one(seen, root / "wiki")
        )

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=2,
            max_api_calls=100,
        )

        assert rc == 0
        assert seen == [
            "20240410T120000Z-aabbccd0.md",
            "20240411T120000Z-aabbccd1.md",
        ]


class TestEntryPointsThreadTheDelta:
    """``ingest`` and ``session_end`` must hand their delta to the entity phase."""

    def test_ingest_passes_entity_changed_paths_to_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path)
        mine = root / "raw" / "sessions" / "20260815T120000Z-deadbeef.md"
        mine.write_text("Met Alice Zhang.\n", encoding="utf-8")

        captured: dict = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr("athenaeum.librarian.run", fake_run)

        ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=tmp_path / "cache",
        )

        assert mine.resolve() in captured["entity_changed_paths"]
        # The auto-memory delta stays entity-free — athenaeum#900 does not widen it.
        assert captured["changed_paths"] == set()

    def test_session_end_passes_entity_changed_paths_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path)
        mine = root / "raw" / "sessions" / "20260815T120000Z-deadbeef.md"
        mine.write_text("Met Alice Zhang.\n", encoding="utf-8")

        captured: dict = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr("athenaeum.librarian.run", fake_run)
        monkeypatch.setattr("athenaeum.librarian.reindex", lambda *a, **k: ("kw", 0))

        session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=tmp_path / "cache",
        )

        assert mine.resolve() in captured["entity_changed_paths"]
