"""Tests for the athenaeum CLI command dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.cli import main


@pytest.fixture
def knowledge_with_wiki(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)

    (wiki / "lean-startup.md").write_text(
        "---\n"
        "name: Lean Startup\n"
        "tags: [methodology]\n"
        "description: Build-measure-learn methodology\n"
        "---\n\n"
        "The Lean Startup methodology.\n"
    )
    (wiki / "customer-development.md").write_text(
        "---\n"
        "name: Customer Development\n"
        "tags: [methodology]\n"
        "description: Steve Blank's framework\n"
        "---\n\n"
        "Customer development is a four-step framework.\n"
    )
    return knowledge


class TestRebuildIndex:
    def test_builds_fts5_index(
        self, knowledge_with_wiki: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache = tmp_path / "cache"
        rc = main([
            "rebuild-index",
            "--path", str(knowledge_with_wiki),
            "--cache-dir", str(cache),
            "--backend", "fts5",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "FTS5 index rebuilt: 2 wiki pages" in out
        assert (cache / "wiki-index.db").exists()

    def test_reads_backend_from_config(
        self, knowledge_with_wiki: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (knowledge_with_wiki / "athenaeum.yaml").write_text(
            "auto_recall: true\nsearch_backend: fts5\n"
        )
        cache = tmp_path / "cache"
        rc = main([
            "rebuild-index",
            "--path", str(knowledge_with_wiki),
            "--cache-dir", str(cache),
        ])
        assert rc == 0
        assert "FTS5 index rebuilt" in capsys.readouterr().out

    def test_defaults_to_fts5_when_no_config(
        self, knowledge_with_wiki: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache = tmp_path / "cache"
        rc = main([
            "rebuild-index",
            "--path", str(knowledge_with_wiki),
            "--cache-dir", str(cache),
        ])
        assert rc == 0
        assert "FTS5 index rebuilt" in capsys.readouterr().out

    def test_missing_wiki_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        nonexistent = tmp_path / "does-not-exist"
        rc = main([
            "rebuild-index",
            "--path", str(nonexistent),
            "--cache-dir", str(tmp_path / "cache"),
            "--backend", "fts5",
        ])
        assert rc == 1
        assert "Wiki directory not found" in capsys.readouterr().err

    def test_unknown_backend_returns_error(
        self, knowledge_with_wiki: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (knowledge_with_wiki / "athenaeum.yaml").write_text(
            "auto_recall: true\nsearch_backend: nonsense\n"
        )
        rc = main([
            "rebuild-index",
            "--path", str(knowledge_with_wiki),
            "--cache-dir", str(tmp_path / "cache"),
        ])
        assert rc == 1
        assert "Unknown search backend" in capsys.readouterr().err


class TestServe:
    """`athenaeum serve` must forward the configured search_backend + cache_dir
    to create_server so the MCP `recall` tool uses the vector index when the
    user has configured `search_backend: vector`. Regression guard against the
    bug where serve hard-coded defaults (keyword) and silently ignored
    athenaeum.yaml."""

    def test_serve_reads_vector_backend_from_config(
        self, knowledge_with_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (knowledge_with_wiki / "athenaeum.yaml").write_text(
            "auto_recall: true\nsearch_backend: vector\n"
        )
        captured: dict[str, object] = {}

        class _FakeServer:
            def run(self) -> None:
                raise KeyboardInterrupt

        def _fake_create_server(**kwargs: object) -> _FakeServer:
            captured.update(kwargs)
            return _FakeServer()

        import athenaeum.mcp_server as mcp_mod
        monkeypatch.setattr(mcp_mod, "create_server", _fake_create_server)

        rc = main(["serve", "--path", str(knowledge_with_wiki)])
        assert rc == 0
        assert captured["search_backend"] == "vector"
        assert captured["wiki_root"] == knowledge_with_wiki / "wiki"
        assert captured["raw_root"] == knowledge_with_wiki / "raw"
        assert captured["cache_dir"] is not None

    def test_serve_defaults_to_fts5_from_config_defaults(
        self, knowledge_with_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        class _FakeServer:
            def run(self) -> None:
                raise KeyboardInterrupt

        def _fake_create_server(**kwargs: object) -> _FakeServer:
            captured.update(kwargs)
            return _FakeServer()

        import athenaeum.mcp_server as mcp_mod
        monkeypatch.setattr(mcp_mod, "create_server", _fake_create_server)

        rc = main(["serve", "--path", str(knowledge_with_wiki)])
        assert rc == 0
        assert captured["search_backend"] == "fts5"

    def test_serve_missing_path_renders_path_in_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression guard for v0.2.0 bug where the 'run athenaeum init'
        hint printed the literal placeholder `{args.path}` because the
        f-string prefix was missing. First-adopter-facing error message —
        if this line drifts back to a non-f-string, pre-init users see
        the unrendered template and lose trust."""
        missing = tmp_path / "no-such-knowledge"
        rc = main(["serve", "--path", str(missing)])
        out = capsys.readouterr().out
        assert rc == 1
        assert str(missing) in out
        assert "{args.path}" not in out


class TestWarnIfBackendCacheMissing:
    """All four branches of ``_warn_if_backend_cache_missing``.

    First-adopter pain point from the v0.2.0 review: ``search_backend:
    vector`` in ``athenaeum.yaml`` but only an fts5 cache on disk (common
    when a user flips backends but forgets to rebuild). The MCP recall
    tool silently returns zero hits. The warning on ``athenaeum serve``
    startup is the only early signal — if any of these branches stops
    emitting, the silent-zero-hits UX regresses. Capture via ``capsys``
    because the warning goes to stderr.
    """

    def test_keyword_backend_no_op(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        _warn_if_backend_cache_missing("keyword", tmp_path)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "", (
            "keyword backend has no on-disk cache — warning would be noise"
        )

    def test_fts5_missing_cache_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        _warn_if_backend_cache_missing("fts5", tmp_path)
        err = capsys.readouterr().err
        assert "search_backend=fts5" in err
        assert "rebuild-index" in err

    def test_fts5_present_cache_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        (tmp_path / "wiki-index.db").write_bytes(b"")
        _warn_if_backend_cache_missing("fts5", tmp_path)
        assert capsys.readouterr().err == ""

    def test_vector_missing_cache_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        _warn_if_backend_cache_missing("vector", tmp_path)
        err = capsys.readouterr().err
        assert "search_backend=vector" in err
        assert "rebuild-index" in err

    def test_vector_present_cache_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        (tmp_path / "wiki-vectors").mkdir()
        _warn_if_backend_cache_missing("vector", tmp_path)
        assert capsys.readouterr().err == ""

    def test_unknown_backend_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        _warn_if_backend_cache_missing("sphinx", tmp_path)
        err = capsys.readouterr().err
        assert "unknown search_backend" in err
        assert "'sphinx'" in err


class TestStopwords:
    """`athenaeum stopwords` is the canonical source of the stopword list —
    shell hooks read it instead of hard-coding their own copy. Regression
    guard for issue #46: the two copies must stay in sync."""

    def test_prints_one_word_per_line_sorted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["stopwords"])
        assert rc == 0
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert lines == sorted(lines), "Output must be sorted for determinism"
        assert len(lines) > 50, "Stopword list should be non-trivial"
        assert "the" in lines
        assert "thanks" in lines

    def test_matches_search_module_constant(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.search import STOPWORDS

        rc = main(["stopwords"])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.strip().splitlines() == list(STOPWORDS), (
            "CLI output must match search.STOPWORDS exactly — divergence "
            "silently degrades the shell-hook fallback extractor."
        )


class TestTestMcp:
    def test_all_steps_pass_with_fastmcp_available(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pytest.importorskip("fastmcp")
        rc = main(["test-mcp"])
        captured = capsys.readouterr()
        assert rc == 0, f"stdout: {captured.out}\nstderr: {captured.err}"
        assert "PASS  remember_write" in captured.out
        assert "PASS  recall_search (keyword)" in captured.out
        assert "PASS  create_server (FastMCP)" in captured.out
        assert "3 passed, 0 failed" in captured.out

    def test_keep_flag_preserves_temp_dir(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pytest.importorskip("fastmcp")
        rc = main(["test-mcp", "--keep"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Temp dir preserved at:" in captured.out

        marker = "Temp dir preserved at: "
        line = next(
            line for line in captured.out.splitlines() if line.startswith(marker)
        )
        kept_dir = Path(line[len(marker):].strip())
        try:
            assert kept_dir.is_dir()
            assert (kept_dir / "wiki" / "test-page.md").is_file()
            assert list((kept_dir / "raw" / "test-mcp").glob("*.md"))
        finally:
            import shutil
            shutil.rmtree(kept_dir, ignore_errors=True)

    def test_reports_fastmcp_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import athenaeum.mcp_server as mcp_mod

        def _raise(*_a: object, **_k: object) -> None:
            raise ImportError("no module named 'fastmcp'")

        monkeypatch.setattr(mcp_mod, "create_server", _raise)
        rc = main(["test-mcp"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "FAIL  create_server" in captured.err
        assert "pip install athenaeum[mcp]" in captured.err
        assert "2 passed, 1 failed" in captured.out


class TestIngestAnswers:
    """`athenaeum ingest-answers` subcommand (issue #61)."""

    def test_missing_knowledge_dir_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["ingest-answers", "--path", str(tmp_path / "nope")])
        assert rc == 1
        assert "Knowledge directory not found" in capsys.readouterr().err

    def test_noop_on_empty_pending_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "wiki").mkdir()
        (tmp_path / "raw").mkdir()
        rc = main(["ingest-answers", "--path", str(tmp_path)])
        assert rc == 0
        assert "Ingested 0" in capsys.readouterr().out

    def test_ingests_answered_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki = tmp_path / "wiki"
        raw = tmp_path / "raw"
        wiki.mkdir()
        raw.mkdir()
        (wiki / "_pending_questions.md").write_text(
            "# Pending Questions\n\n"
            "## [2026-04-20] Entity: \"Acme Corp\" (from sessions/test.md)\n"
            "- [x] Question about Acme?\n"
            "**Conflict type**: principled\n"
            "**Description**: Conflicting Series info.\n"
            "\n"
            "Series B, closed March 2026.\n"
        )
        rc = main(["ingest-answers", "--path", str(tmp_path)])
        assert rc == 0
        assert "Ingested 1" in capsys.readouterr().out
        assert list((raw / "answers").glob("*.md"))
        assert (wiki / "_pending_questions_archive.md").exists()
