# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum session-end` — the change-gated SessionEnd path (#350).

`session_end` composes the incremental `ingest` engine (#349) with the
incremental `reindex` (#348) as ONE change-gated command: the cwc SessionEnd
hook and the nightly-after-librarian path both invoke it so a memory
`remember`ed by one agent becomes recallable by every other agent after that
session ends — closing the ~24h gap where a raw fact sat uncompiled until the
next nightly librarian run.

Two guarantees under test:

* Composition + cost bound: new raw → ingest + reindex; an idle SessionEnd
  (nothing new) is a fast no-op with zero LLM work AND no reindex; a failed
  compile never indexes a half-built wiki; `--full` forces both steps.
* End-to-end cross-agent recall (the issue's acceptance criterion): session A
  `remember`s a fact, session A's SessionEnd runs `session_end`, and session B
  `recall`s the fully-compiled wiki entry — no waiting for the nightly run.

All LLM/embedder work is stubbed. The `tier0_passthrough` structured path
exercises the "compiles with NO LLM cost" guarantee: the mocked Anthropic
client's `messages.create` is asserted never-called.
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


def _write_tier0_raw(
    root: Path,
    uid: str,
    name: str,
    ts: str,
    uuid8: str,
    *,
    session_dir: str = "sessions",
    origin_session: str | None = None,
) -> Path:
    """A pre-structured (tier0-eligible) raw intake file — uid/type/name set."""
    origin = f"originSessionId: {origin_session}\n" if origin_session else ""
    path = root / "raw" / session_dir / f"{ts}-{uuid8}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"uid: {uid}\n"
        "type: person\n"
        f"name: {name}\n"
        "tags: [active]\n"
        "access: internal\n"
        f"{origin}"
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
# session_end composition (librarian.session_end)
# ---------------------------------------------------------------------------


class TestSessionEndComposition:
    def test_new_raw_ingests_then_reindexes_no_llm(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
            backend="fts5",
        )

        assert result.exit_code == 0
        assert result.ingest.noop is False
        assert result.ingest.compiled == 1
        # Compile ran → reindex ran, and the new wiki page is in the index.
        assert result.reindexed is True
        assert result.reindex_pages >= 1
        assert result.backend == "fts5"
        # tier0 passthrough must never touch the model.
        mock_anthropic.messages.create.assert_not_called()
        # wiki page written; ingest stamp + index manifests created.
        assert list((root / "wiki").glob("p-0001-*.md"))
        assert (cache / "ingest-manifest.json").is_file()

    def test_idle_session_end_is_noop_and_skips_reindex(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert first.reindexed is True and first.ingest.compiled == 1

        # Nothing new since the last SessionEnd → ingest no-op → NO reindex.
        second = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert second.ingest.noop is True
        assert second.reindexed is False
        assert second.reindex_pages == 0
        assert second.exit_code == 0

    def test_failed_compile_skips_reindex_and_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        monkeypatch.setattr(lib, "run", lambda *a, **k: 1)  # simulate failure
        # Spy the reindex so we can assert it is never called on a bad compile.
        reindex_calls: list[bool] = []
        real_reindex = lib.reindex
        monkeypatch.setattr(
            lib,
            "reindex",
            lambda *a, **k: (reindex_calls.append(True), real_reindex(*a, **k))[1],
        )

        result = lib.session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert result.exit_code == 1
        assert result.reindexed is False
        assert reindex_calls == []
        # No stamp written on failure → next SessionEnd retries.
        assert not (cache / "ingest-manifest.json").exists()

    def test_dry_run_skips_reindex_and_stamp(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
            dry_run=True,
        )
        assert result.reindexed is False
        # Dry-run never stamps and never consumes the raw file.
        assert not (cache / "ingest-manifest.json").exists()
        assert list((root / "raw" / "sessions").glob("2024*.md"))

    def test_full_forces_recompile_and_full_reindex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"
        # Pre-seed an ingest stamp so an incremental run WOULD no-op.
        cache.mkdir()
        (cache / "ingest-manifest.json").write_text(
            json.dumps({"version": 1, "hashes": {}})
        )

        run_calls: list[bool] = []
        reindex_modes: list[bool] = []
        monkeypatch.setattr(lib, "run", lambda *a, **k: (run_calls.append(True), 0)[1])
        monkeypatch.setattr(
            lib,
            "reindex",
            lambda *a, **k: (
                reindex_modes.append(bool(k.get("incremental", True))),
                ("fts5", 0),
            )[1],
        )

        result = lib.session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=False,
            cache_dir=cache,
            backend="fts5",
        )
        assert result.ingest.mode == "full"
        assert run_calls == [True]  # --full ignored the stamp and compiled.
        assert result.reindexed is True
        assert reindex_modes == [False]  # full compile → full reindex.


# ---------------------------------------------------------------------------
# session-end CLI wrapper
# ---------------------------------------------------------------------------


class TestSessionEndCLI:
    def test_json_summary_shape_and_exit_zero(
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
                "session-end",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["command"] == "session-end"
        assert payload["mode"] == "incremental"
        assert payload["reindexed"] is True
        assert payload["reindex_pages"] >= 1
        assert payload["backend"] == "fts5"
        assert payload["exit_code"] == 0
        assert isinstance(payload["duration_ms"], int)
        # The nested ingest summary round-trips.
        assert payload["ingest"]["command"] == "ingest"
        assert payload["ingest"]["compiled"] == 1

    def test_single_flight_lock_held(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from athenaeum.runlock import RunLock

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")

        with RunLock(root):  # hold the lock so session-end can't acquire it
            rc = main(
                [
                    "session-end",
                    "--path",
                    str(root),
                    "--cache-dir",
                    str(tmp_path / "c"),
                    "--backend",
                    "fts5",
                ]
            )
        assert rc == EXIT_LOCK_HELD
        assert "error" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# End-to-end cross-agent recall — the issue #350 acceptance criterion
# ---------------------------------------------------------------------------


class TestCrossAgentRecall:
    """Session A remembers → A's SessionEnd → session B recalls it.

    Demonstrates the ~24h gap is closed WITHOUT waiting for the nightly
    librarian: the fact is invisible to `recall` while it sits in `raw/`, and
    becomes recallable the moment A's `session_end` compiles + indexes it.
    """

    def test_remember_in_session_a_is_recallable_in_session_b(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from athenaeum.librarian import session_end
        from athenaeum.mcp_server import remember_write

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        # --- Session A: an agent remembers a structured (tier0) fact. It lands
        #     in raw/<session>/ only — recall reads wiki/, so it is invisible.
        content = (
            "---\n"
            "uid: p-9350\n"
            "type: person\n"
            "name: Marie Curie\n"
            "tags: [active]\n"
            "access: internal\n"
            "originSessionId: sess-A\n"
            "---\n\n"
            "Marie Curie pioneered research on radioactivity.\n"
        )
        msg = remember_write(
            root / "raw", content, source="sess-A", sources="user-stated:e2e"
        )
        assert msg.startswith("Saved to")
        # Pre-condition (the gap): raw file exists, but NO compiled wiki page.
        assert list((root / "raw" / "sess-A").glob("*.md"))
        assert not list((root / "wiki").glob("p-9350-*.md"))

        # --- Session A's SessionEnd: change-gated ingest + reindex, scoped to
        #     the originating session id (the cwc hook use-case).
        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            session="sess-A",
            cache_dir=cache,
            backend="fts5",
        )
        assert result.exit_code == 0
        assert result.ingest.new_or_changed == 1
        assert result.ingest.compiled == 1
        assert result.reindexed is True
        assert result.session == "sess-A"
        # No LLM: the structured entry compiled via tier0 passthrough.
        mock_anthropic.messages.create.assert_not_called()
        # The compiled, fully-resolved wiki page now exists.
        wiki_pages = list((root / "wiki").glob("p-9350-*.md"))
        assert wiki_pages, "session_end must compile the raw fact into wiki/"

        # --- Session B: a DIFFERENT agent recalls the fact via the shell recall
        #     path (same index the MCP `recall` tool reads). It is now found.
        capsys.readouterr()  # clear
        rc = main(
            [
                "recall",
                "Marie Curie",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert out.strip(), "session B recall returned no hits — gap not closed"
        assert "curie" in out.lower()
