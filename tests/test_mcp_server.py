"""Tests for the MCP memory server module."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.mcp_server import (
    _score_page,
    _snippet,
    _tokenize_query,
    recall_search,
    remember_write,
)

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class TestTokenizeQuery:
    def test_basic_split(self) -> None:
        assert _tokenize_query("hello world") == ["hello", "world"]

    def test_filters_short_tokens(self) -> None:
        assert _tokenize_query("a is the go") == ["is", "the", "go"]

    def test_lowercases(self) -> None:
        assert _tokenize_query("Acme Corp") == ["acme", "corp"]

    def test_splits_on_punctuation(self) -> None:
        assert _tokenize_query("foo-bar/baz") == ["foo", "bar", "baz"]

    def test_empty_string(self) -> None:
        assert _tokenize_query("") == []


# ---------------------------------------------------------------------------
# Score page
# ---------------------------------------------------------------------------


class TestScorePage:
    def test_frontmatter_match_weighted(self) -> None:
        score = _score_page(
            ["acme"], {"name": "Acme Corp", "tags": ["fintech"]}, "Some body text"
        )
        assert score >= 3.0  # frontmatter hit

    def test_body_only_match(self) -> None:
        score = _score_page(["pipeline"], {}, "The pipeline processes raw files")
        assert score == 1.0

    def test_both_match(self) -> None:
        score = _score_page(
            ["acme"], {"name": "Acme Corp"}, "Acme is a company"
        )
        assert score == 4.0  # 3 (frontmatter) + 1 (body)

    def test_no_match(self) -> None:
        score = _score_page(["xyz"], {"name": "Acme"}, "No match here")
        assert score == 0.0

    def test_empty_tokens(self) -> None:
        assert _score_page([], {"name": "Acme"}, "body") == 0.0

    def test_list_tags_scored(self) -> None:
        score = _score_page(
            ["fintech"], {"tags": ["fintech", "client"]}, "body"
        )
        assert score >= 3.0


# ---------------------------------------------------------------------------
# Snippet
# ---------------------------------------------------------------------------


class TestSnippet:
    def test_returns_context_around_match(self) -> None:
        body = "x" * 200 + " KEYWORD " + "y" * 200
        snip = _snippet(body, ["keyword"], max_chars=100)
        assert "keyword" in snip.lower()

    def test_returns_start_when_no_match(self) -> None:
        body = "abcdef" * 50
        snip = _snippet(body, ["zzz"], max_chars=20)
        assert snip.startswith("abcdef")

    def test_short_body(self) -> None:
        snip = _snippet("short", ["short"])
        assert snip == "short"


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


class TestRecall:
    def test_recall_finds_matching_page(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "Acme")
        assert "Acme Corp" in result
        assert "score:" in result

    def test_recall_no_match(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "xyznonexistent")
        assert "No wiki pages matched" in result

    def test_recall_missing_dir(self, tmp_path: Path) -> None:
        result = recall_search(tmp_path / "nonexistent", "test")
        assert "not found" in result

    def test_recall_short_query(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "a")
        assert "too short" in result.lower()

    def test_recall_skips_underscore_files(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "Index")
        # _index.md should be skipped, so no match from that file
        assert "score:" not in result or "_index" not in result

    def test_recall_top_k(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "knowledge architecture", top_k=1)
        assert "showing top 1" in result

    def test_recall_shows_tags(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "Acme fintech")
        assert "fintech" in result

    def test_recall_top_k_capped(self, wiki_dir: Path) -> None:
        # top_k > _MAX_TOP_K should be silently capped
        result = recall_search(wiki_dir, "Acme", top_k=1_000_000)
        assert "Acme Corp" in result


# ---------------------------------------------------------------------------
# Remember
# ---------------------------------------------------------------------------


class TestRemember:
    def test_writes_raw_file(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(raw, "Test observation about Acme")
        assert result.startswith("Saved to")
        # Verify file exists and has content
        files = list((raw / "claude-session").glob("*.md"))
        assert len(files) == 1
        assert files[0].read_text() == "Test observation about Acme"

    def test_custom_source(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(raw, "content", source="manual")
        assert "Saved to" in result
        assert (raw / "manual").is_dir()

    def test_filename_format(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        remember_write(raw, "content")
        files = list((raw / "claude-session").glob("*.md"))
        # filename: 20260416T123456Z-abcd1234.md
        import re
        assert re.match(r"\d{8}T\d{6}Z-[0-9a-f]{8}\.md", files[0].name)

    def test_rejects_empty_source(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(raw, "content", source="!!!")
        assert "Error" in result

    def test_sanitizes_path_traversal(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        # "../../../etc" is sanitized to "etc" (dots and slashes stripped),
        # so it writes safely to raw/etc/ — the sanitization IS the defense
        result = remember_write(raw, "content", source="../../../etc")
        assert "Saved" in result
        assert (raw / "etc").is_dir()

    def test_sanitizes_wiki_traversal(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        # "../wiki" is sanitized to "wiki", writing to raw/wiki/ (not the
        # actual wiki root) — safely contained inside raw/
        result = remember_write(
            raw, "content", source="../wiki", wiki_root=wiki
        )
        assert "Saved" in result
        assert (raw / "wiki").is_dir()

    def test_creates_source_dir(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        remember_write(raw, "content", source="new-source")
        assert (raw / "new-source").is_dir()

    def test_append_only(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        r1 = remember_write(raw, "first")
        r2 = remember_write(raw, "second")
        assert "Saved" in r1
        assert "Saved" in r2
        files = list((raw / "claude-session").glob("*.md"))
        assert len(files) == 2

    def test_rejects_oversized_content(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        huge = "x" * (11 * 1024 * 1024)  # 11 MB
        result = remember_write(raw, huge)
        assert "Error" in result
        assert "limit" in result.lower()


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


class TestCreateServer:
    def test_creates_server_instance(self, tmp_path: Path) -> None:
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server

        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        server = create_server(raw_root=raw, wiki_root=wiki)
        assert server is not None

    def test_import_error_without_fastmcp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib
        import sys

        # Temporarily hide fastmcp
        saved = sys.modules.get("fastmcp")
        monkeypatch.setitem(sys.modules, "fastmcp", None)
        try:
            # Re-import to trigger the ImportError path
            import athenaeum.mcp_server as mod
            importlib.reload(mod)  # force fresh import of the function

            with pytest.raises(ImportError, match="FastMCP is required"):
                mod.create_server(
                    raw_root=tmp_path / "raw", wiki_root=tmp_path / "wiki"
                )
        finally:
            if saved is not None:
                monkeypatch.setitem(sys.modules, "fastmcp", saved)
            else:
                monkeypatch.delitem(sys.modules, "fastmcp", raising=False)


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCLIServe:
    def test_serve_missing_dir(self, tmp_path: Path) -> None:
        from athenaeum.cli import main

        code = main(["serve", "--path", str(tmp_path / "nonexistent")])
        assert code == 1
