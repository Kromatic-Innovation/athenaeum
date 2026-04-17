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
