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

    def test_match_near_start_trims_tail(self) -> None:
        # Match is early in the body; snippet should keep the match and
        # append an ellipsis for the trimmed tail, not drop the match.
        body = "The KEYWORD appears here. " + "tail " * 200
        snip = _snippet(body, ["keyword"], max_chars=60)
        assert "keyword" in snip.lower()
        assert snip.endswith("…") or len(snip) <= 63

    def test_match_near_end_prefixes_ellipsis(self) -> None:
        # Match is at the end; snippet should prefix an ellipsis so the
        # reader sees the match, not the irrelevant prefix. Uses a
        # realistic max_chars (>=80) so the window can reach the match —
        # the snippet algorithm centers ~80 chars before best_pos, so a
        # smaller max_chars would clip the window before the match.
        body = "lead " * 200 + " KEYWORD tail."
        snip = _snippet(body, ["keyword"], max_chars=200)
        assert "keyword" in snip.lower()
        assert snip.startswith("…")


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
        # The fixture contains _index.md which must be skipped by the
        # backend's `startswith("_")` guard. Direct assertion — previous
        # form was `"score:" not in result or "_index" not in result`,
        # which passes trivially when "No wiki pages matched" is returned
        # (the `a or b` shape masked the actual behavior being tested).
        # MEMORY.md is intentionally not skipped by the search backend —
        # only underscore-prefixed files are filtered. EntityIndex (a
        # different consumer) skips MEMORY.md separately.
        result = recall_search(wiki_dir, "Index")
        assert "_index.md" not in result

    def test_recall_top_k(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "knowledge architecture", top_k=1)
        # After the v0.2.1 backend unification the keyword path no longer
        # reports total-matched; top_k is enforced via a single result block.
        assert result.count("### ") == 1
        assert "Found 1 matching pages" in result

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
# Pending-questions tools (issue #61)
# ---------------------------------------------------------------------------


def _seed_pending_wiki(tmp_path: Path, *, answered: bool = False) -> Path:
    """Build a tmp knowledge dir with a seeded `_pending_questions.md`."""
    raw = tmp_path / "raw"
    wiki = tmp_path / "wiki"
    raw.mkdir()
    wiki.mkdir()
    checkbox = "[x]" if answered else "[ ]"
    pending = wiki / "_pending_questions.md"
    pending.write_text(
        "# Pending Questions\n\n"
        "## [2026-04-20] Entity: \"Acme Corp\" (from sessions/test.md)\n"
        f"- {checkbox} Is Acme Series A or Series B after 2026?\n"
        "**Conflict type**: principled\n"
        "**Description**: Prior wiki says Series A; new raw implies Series B.\n"
    )
    return tmp_path


class TestPendingQuestionMCPTools:
    """The two tools registered in `create_server` for issue #61.

    We exercise the underlying module helpers (same semantics as the tools)
    and verify the tools themselves are registered on the FastMCP server.
    """

    def test_list_and_resolve_happy_path(self, tmp_path: Path) -> None:
        from athenaeum.answers import list_unanswered, resolve_by_id

        root = _seed_pending_wiki(tmp_path)
        pending_path = root / "wiki" / "_pending_questions.md"

        items = list_unanswered(pending_path)
        assert len(items) == 1
        item = items[0]
        assert set(item.keys()) >= {
            "id", "entity", "source", "question",
            "conflict_type", "description", "created_at",
        }
        assert item["entity"] == "Acme Corp"

        result = resolve_by_id(pending_path, item["id"], "Series B, closed March 2026.")
        assert result["ok"] is True

        # After resolve, list_unanswered no longer returns this item.
        assert list_unanswered(pending_path) == []

    def test_resolve_not_found(self, tmp_path: Path) -> None:
        from athenaeum.answers import resolve_by_id

        root = _seed_pending_wiki(tmp_path)
        pending_path = root / "wiki" / "_pending_questions.md"
        result = resolve_by_id(pending_path, "nope", "answer")
        assert result["ok"] is False
        assert result["error_code"] == "id_not_found"
        assert "not found" in result["message"]

    def test_list_empty_when_file_missing(self, tmp_path: Path) -> None:
        from athenaeum.answers import list_unanswered

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        assert list_unanswered(wiki / "_pending_questions.md") == []

    def test_tools_registered_on_server(self, tmp_path: Path) -> None:
        """Both `list_pending_questions` + `resolve_question` must be exposed."""
        pytest.importorskip("fastmcp")
        import asyncio

        from athenaeum.mcp_server import create_server

        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        server = create_server(raw_root=raw, wiki_root=wiki)

        # FastMCP exposes tools via `get_tool(name)` (async). We only care
        # that both names resolve without raising.
        async def _lookup() -> tuple[object, object]:
            lpq = await server.get_tool("list_pending_questions")
            rq = await server.get_tool("resolve_question")
            return lpq, rq

        lpq, rq = asyncio.run(_lookup())
        assert lpq is not None
        assert rq is not None


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCLIServe:
    def test_serve_missing_dir(self, tmp_path: Path) -> None:
        from athenaeum.cli import main

        code = main(["serve", "--path", str(tmp_path / "nonexistent")])
        assert code == 1
