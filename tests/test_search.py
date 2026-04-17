"""Tests for the athenaeum search backends."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.search import (
    FTS5Backend,
    SearchBackend,
    VectorBackend,
    get_backend,
    build_fts5_index,
    query_fts5_index,
)


@pytest.fixture
def wiki_with_pages(tmp_path: Path) -> Path:
    """Create a wiki directory with sample pages for search testing."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    (wiki / "lean-startup.md").write_text(
        "---\n"
        "name: Lean Startup\n"
        "tags: [methodology, startup]\n"
        "aliases: [lean, LSM]\n"
        "description: Build-measure-learn methodology\n"
        "---\n\n"
        "The Lean Startup methodology emphasizes validated learning.\n"
    )

    (wiki / "customer-development.md").write_text(
        "---\n"
        "name: Customer Development\n"
        "tags: [methodology, customers]\n"
        "description: Steve Blank's customer development process\n"
        "---\n\n"
        "Customer development is a four-step framework for startups.\n"
    )

    (wiki / "acme-corp.md").write_text(
        "---\n"
        "name: Acme Corp\n"
        "tags: [client, fintech]\n"
        "description: Enterprise client in financial services\n"
        "---\n\n"
        "Acme Corp is a fintech company.\n"
    )

    # Should be skipped (underscore prefix)
    (wiki / "_index.md").write_text("# Index\n")

    return wiki


class TestFTS5Backend:
    def test_satisfies_protocol(self) -> None:
        assert isinstance(FTS5Backend(), SearchBackend)

    def test_build_index(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        count = backend.build_index(wiki_with_pages, cache)
        assert count == 3
        assert (cache / "wiki-index.db").is_file()

    def test_build_index_skips_underscore(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        count = backend.build_index(wiki_with_pages, cache)
        # _index.md should be excluded
        assert count == 3

    def test_build_index_creates_cache_dir(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "nonexistent" / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        assert cache.is_dir()

    def test_build_index_replaces_existing(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        # Add a page and rebuild
        (wiki_with_pages / "new-page.md").write_text(
            "---\nname: New Page\n---\nNew content.\n"
        )
        count = backend.build_index(wiki_with_pages, cache)
        assert count == 4

    def test_query_finds_match(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query("lean startup methodology", cache)
        assert len(results) > 0
        filenames = [r[0] for r in results]
        assert "lean-startup.md" in filenames

    def test_query_no_match(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query("xyznonexistent", cache)
        assert results == []

    def test_query_respects_limit(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query("methodology", cache, n=1)
        assert len(results) <= 1

    def test_query_excludes_filenames(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query(
            "lean startup methodology", cache,
            exclude={"lean-startup.md"},
        )
        filenames = [r[0] for r in results]
        assert "lean-startup.md" not in filenames

    def test_query_no_index(self, tmp_path: Path) -> None:
        cache = tmp_path / "empty-cache"
        cache.mkdir()
        backend = FTS5Backend()
        assert backend.query("anything", cache) == []

    def test_query_short_terms_filtered(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        # All terms under 3 chars
        results = backend.query("is an of", cache)
        assert results == []

    def test_query_stopwords_filtered(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        # All terms are stopwords
        results = backend.query("the and they have been", cache)
        assert results == []

    def test_returns_tuples(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query("acme fintech", cache)
        assert len(results) > 0
        fname, name, score = results[0]
        assert isinstance(fname, str)
        assert isinstance(name, str)
        assert isinstance(score, float)


class TestVectorBackend:
    def test_satisfies_protocol(self) -> None:
        assert isinstance(VectorBackend(), SearchBackend)

    def test_build_raises(self, tmp_path: Path) -> None:
        with pytest.raises(NotImplementedError, match="issue #32"):
            VectorBackend().build_index(tmp_path, tmp_path)

    def test_query_raises(self, tmp_path: Path) -> None:
        with pytest.raises(NotImplementedError, match="issue #32"):
            VectorBackend().query("test", tmp_path)


class TestGetBackend:
    def test_fts5(self) -> None:
        assert isinstance(get_backend("fts5"), FTS5Backend)

    def test_vector(self) -> None:
        assert isinstance(get_backend("vector"), VectorBackend)

    def test_unknown(self) -> None:
        with pytest.raises(KeyError, match="unknown"):
            get_backend("unknown")


class TestConvenienceFunctions:
    def test_build_fts5(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        count = build_fts5_index(wiki_with_pages, cache)
        assert count == 3

    def test_query_fts5(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        build_fts5_index(wiki_with_pages, cache)
        results = query_fts5_index("acme", cache)
        assert len(results) > 0

    def test_accepts_str_paths(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        count = build_fts5_index(str(wiki_with_pages), str(cache))
        assert count == 3
        results = query_fts5_index("acme", str(cache))
        assert len(results) > 0
