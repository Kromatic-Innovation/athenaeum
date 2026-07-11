"""Tests for `athenaeum ingest` and `athenaeum reindex` (issue #349).

Covers the reusable incremental-ingest engine in ``librarian.ingest`` (which
the SessionEnd path #350 reuses) and the thin CLI wrappers: incremental
compiles only new/changed raw files and is a fast no-op when none, ``--full``
recompiles, ``reindex --incremental`` is a no-op when nothing changed, the
one-line JSON summary shape, exit codes, and single-flight via the runlock.

All LLM/embedder work is stubbed — no real API calls or 21k embeds. The
tier0-passthrough path deliberately exercises the "compiles with NO LLM cost"
guarantee: the mocked Anthropic client's ``messages.create`` is asserted
never-called.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.cli import EXIT_LOCK_HELD, main

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _seed_knowledge_root(tmp_path: Path) -> Path:
    """A minimal knowledge/ tree with .git, wiki/_schema, raw/sessions."""
    root = tmp_path / "knowledge"
    (root / "wiki" / "_schema").mkdir(parents=True)
    (root / "wiki" / "_schema" / "types.md").write_text(
        "# Types\n\n| Type |\n|------|\n| person |\n"
    )
    (root / "wiki" / "_schema" / "tags.md").write_text(
        "# Tags\n\n| Tag |\n|-----|\n| active |\n"
    )
    (root / "wiki" / "_schema" / "access-levels.md").write_text(
        "# Access\n\n| Level |\n|-------|\n| internal |\n"
    )
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _write_tier0_raw(root: Path, uid: str, name: str, ts: str, uuid8: str) -> Path:
    """A pre-structured (tier0-eligible) raw intake file — uid/type/name set."""
    path = root / "raw" / "sessions" / f"{ts}-{uuid8}.md"
    path.write_text(
        "---\n"
        f"uid: {uid}\n"
        "type: person\n"
        f"name: {name}\n"
        "tags: [active]\n"
        "access: internal\n"
        "---\n\n"
        f"Notes about {name}.\n"
    )
    return path


@pytest.fixture
def mock_anthropic(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch anthropic.Anthropic + a fake key so run()'s startup gate passes.

    Returns the mock client so tests can assert ``messages.create`` was never
    called (the tier0 "no LLM cost" guarantee).
    """
    import anthropic as anthropic_mod

    client = MagicMock()
    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key-not-real")
    return client


# ---------------------------------------------------------------------------
# ingest engine (librarian.ingest) — real tier0 compile, no LLM
# ---------------------------------------------------------------------------


class TestIngestEngineTier0:
    def test_incremental_compiles_new_tier0_with_no_llm_cost(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import ingest

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        result = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )

        assert result.exit_code == 0
        assert result.noop is False
        assert result.new_or_changed == 1
        assert result.compiled == 1
        # tier0 passthrough must never touch the model.
        mock_anthropic.messages.create.assert_not_called()
        # wiki page written; raw consumed; stamp manifest created.
        assert list((root / "wiki").glob("p-0001-*.md"))
        assert not list((root / "raw" / "sessions").glob("2024*.md"))
        assert (cache / "ingest-manifest.json").is_file()

    def test_incremental_is_noop_when_nothing_new(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import ingest

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert first.noop is False and first.compiled == 1

        # Nothing new since the last ingest → fast no-op, no compile.
        second = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert second.noop is True
        assert second.new_or_changed == 0
        assert second.compiled == 0
        assert second.exit_code == 0

    def test_full_recompiles_ignoring_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--full always invokes the compile even when the stamp is current."""
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"
        # Pre-seed a stamp so the incremental gate WOULD no-op.
        (cache).mkdir()
        (cache / "ingest-manifest.json").write_text(
            json.dumps({"version": 1, "hashes": {}})
        )

        calls: list[bool] = []

        def _spy_run(*_a: object, **_k: object) -> int:
            calls.append(True)
            return 0

        monkeypatch.setattr(lib, "run", _spy_run)

        # Incremental with an empty-but-present stamp and no raw files → no-op.
        inc = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert inc.noop is True
        assert calls == []

        # --full ignores the stamp and runs the compile.
        full = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=False,
            cache_dir=cache,
        )
        assert full.noop is False
        assert full.mode == "full"
        assert calls == [True]

    def test_failed_compile_leaves_stamp_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        monkeypatch.setattr(lib, "run", lambda *a, **k: 1)  # simulate failure

        result = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert result.exit_code == 1
        # No stamp written on failure → next run retries.
        assert not (cache / "ingest-manifest.json").exists()

    def test_session_scopes_new_change_detection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"
        (cache).mkdir()
        (cache / "ingest-manifest.json").write_text(
            json.dumps({"version": 1, "hashes": {}})
        )
        # An auto-memory raw file tagged with a session id.
        am = root / "raw" / "auto-memory" / "_unscoped"
        am.mkdir(parents=True)
        (am / "project_foo.md").write_text(
            "---\nname: Foo\noriginSessionId: sess-XYZ\n---\n\nbody\n"
        )

        monkeypatch.setattr(lib, "run", lambda *a, **k: 0)

        # A different session sees nothing new → no-op.
        other = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            session="sess-OTHER",
            cache_dir=cache,
        )
        assert other.noop is True

        # The owning session sees the new file → compiles.
        owner = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            session="sess-XYZ",
            cache_dir=cache,
        )
        assert owner.noop is False
        assert owner.new_or_changed == 1
        assert owner.session == "sess-XYZ"


# ---------------------------------------------------------------------------
# ingest CLI wrapper
# ---------------------------------------------------------------------------


class TestIngestCLI:
    def test_json_summary_shape_and_exit_zero(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        rc = main(["ingest", "--path", str(root), "--cache-dir", str(cache)])
        assert rc == 0
        line = capsys.readouterr().out.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["command"] == "ingest"
        assert payload["mode"] == "incremental"
        assert payload["compiled"] == 1
        assert payload["noop"] is False
        assert payload["exit_code"] == 0
        assert isinstance(payload["duration_ms"], int)

    def test_exit_nonzero_on_compile_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        monkeypatch.setattr(lib, "run", lambda *a, **k: 1)

        rc = main(["ingest", "--path", str(root), "--cache-dir", str(tmp_path / "c")])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["exit_code"] == 1

    def test_single_flight_lock_held(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from athenaeum.runlock import RunLock

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")

        with RunLock(root):  # hold the lock so ingest can't acquire it
            rc = main(
                ["ingest", "--path", str(root), "--cache-dir", str(tmp_path / "c")]
            )
        assert rc == EXIT_LOCK_HELD
        assert "error" in capsys.readouterr().err.lower()

    def test_dry_run_does_not_stamp(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        rc = main(
            [
                "ingest",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
                "--dry-run",
            ]
        )
        assert rc == 0
        # Dry-run never writes the stamp and never consumes the raw file.
        assert not (cache / "ingest-manifest.json").exists()
        assert list((root / "raw" / "sessions").glob("2024*.md"))


# ---------------------------------------------------------------------------
# reindex CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge_with_wiki(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "a.md").write_text(
        "---\nname: A\ntags: [x]\ndescription: d\n---\n\nAlpha body.\n"
    )
    (wiki / "b.md").write_text(
        "---\nname: B\ntags: [x]\ndescription: d\n---\n\nBeta body.\n"
    )
    return knowledge


class TestReindexCLI:
    def test_incremental_json_summary(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cache = tmp_path / "cache"
        rc = main(
            [
                "reindex",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["command"] == "reindex"
        assert payload["backend"] == "fts5"
        assert payload["mode"] == "incremental"
        assert payload["pages"] == 2
        assert payload["exit_code"] == 0
        assert isinstance(payload["duration_ms"], int)

    def test_incremental_noop_when_nothing_changed(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cache = tmp_path / "cache"
        args = [
            "reindex",
            "--path",
            str(knowledge_with_wiki),
            "--cache-dir",
            str(cache),
            "--backend",
            "fts5",
        ]
        assert main(args) == 0
        capsys.readouterr()
        # Second incremental pass with no file changes: still succeeds and is
        # sub-second (the #348 hash-diff finds an empty delta).
        assert main(args) == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["mode"] == "incremental"
        assert payload["exit_code"] == 0
        assert payload["duration_ms"] < 1000

    def test_full_mode_reported(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(
            [
                "reindex",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--backend",
                "fts5",
                "--full",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["mode"] == "full"

    def test_rebuild_index_alias_still_works(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(
            [
                "rebuild-index",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "FTS5 index rebuilt" in out  # legacy human line preserved
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["command"] == "rebuild-index"
