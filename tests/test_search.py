"""Tests for the athenaeum search backends."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import search as search_module
from athenaeum.search import (
    FTS5Backend,
    SearchBackend,
    VectorBackend,
    build_fts5_index,
    get_backend,
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

    def test_build_index_creates_cache_dir(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
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

    def test_query_finds_match(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query("lean startup methodology", cache)
        assert len(results) > 0
        filenames = [r[0] for r in results]
        assert "lean-startup.md" in filenames

    def test_query_no_match(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query("xyznonexistent", cache)
        assert results == []

    def test_query_respects_limit(self, wiki_with_pages: Path, tmp_path: Path) -> None:
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
            "lean startup methodology",
            cache,
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

    def test_returns_tuples(self, wiki_with_pages: Path, tmp_path: Path) -> None:
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
    @pytest.fixture(autouse=True)
    def _require_chromadb(self) -> None:
        pytest.importorskip("chromadb")

    def test_satisfies_protocol(self) -> None:
        assert isinstance(VectorBackend(), SearchBackend)

    def test_build_index(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        count = backend.build_index(wiki_with_pages, cache)
        assert count == 3
        assert (cache / "wiki-vectors").is_dir()

    def test_build_index_skips_underscore(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        count = backend.build_index(wiki_with_pages, cache)
        assert count == 3  # _index.md excluded

    def test_build_index_replaces_existing(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki_with_pages, cache)
        (wiki_with_pages / "new-page.md").write_text(
            "---\nname: New Page\n---\nNew content.\n"
        )
        count = backend.build_index(wiki_with_pages, cache)
        assert count == 4

    def test_build_index_recovers_from_corrupt_vector_dir(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        """Regression: stale/corrupt on-disk state must not break rebuild.

        Reproduces the scenario from issue #32 where chromadb's SQLite and
        rust-binding state desynced, causing ``create_collection`` to succeed
        but ``collection.add`` to raise ``NotFoundError``. The fix wipes
        ``vector_dir`` wholesale on each rebuild.
        """
        cache = tmp_path / "cache"
        vector_dir = cache / "wiki-vectors"
        vector_dir.mkdir(parents=True)
        # Garbage that would confuse a freshly-opened PersistentClient
        (vector_dir / "chroma.sqlite3").write_bytes(b"not a sqlite db")
        (vector_dir / "stray-file.bin").write_bytes(b"\x00\x01\x02")

        count = VectorBackend().build_index(wiki_with_pages, cache)
        assert count == 3
        assert vector_dir.is_dir()
        # Garbage was replaced, not merged
        assert not (vector_dir / "stray-file.bin").exists()

    def test_query_finds_match(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query("lean startup methodology", cache)
        assert len(results) > 0
        filenames = [r[0] for r in results]
        assert "lean-startup.md" in filenames

    def test_query_semantic_match(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki_with_pages, cache)
        # "build measure learn" should match Lean Startup via embeddings
        results = backend.query("build measure learn", cache)
        assert len(results) > 0
        filenames = [r[0] for r in results]
        assert "lean-startup.md" in filenames

    def test_query_respects_limit(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query("methodology", cache, n=1)
        assert len(results) <= 1

    def test_query_excludes_filenames(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query(
            "lean startup methodology",
            cache,
            exclude={"lean-startup.md"},
        )
        filenames = [r[0] for r in results]
        assert "lean-startup.md" not in filenames

    def test_query_no_index(self, tmp_path: Path) -> None:
        cache = tmp_path / "empty-cache"
        cache.mkdir()
        backend = VectorBackend()
        assert backend.query("anything", cache) == []

    def test_returns_tuples(self, wiki_with_pages: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query("fintech company", cache)
        assert len(results) > 0
        fname, name, score = results[0]
        assert isinstance(fname, str)
        assert isinstance(name, str)
        assert isinstance(score, float)


@pytest.fixture
def wiki_and_auto_memory(tmp_path: Path) -> tuple[Path, Path]:
    """Create a wiki + auto-memory intake tree for extra-roots tests.

    Layout mirrors the real shape produced by the auto-memory Phase B
    migration so the tests catch regressions in the actual path pattern
    (``<knowledge>/raw/auto-memory/<scope>/feedback_*.md`` plus a
    ``_unscoped/`` bucket and per-scope ``MEMORY.md`` index files).
    """
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "lean-startup.md").write_text(
        "---\nname: Lean Startup\ntags: [methodology]\n"
        "description: Build-measure-learn methodology\n---\n\n"
        "The Lean Startup methodology emphasizes validated learning.\n"
    )

    auto_memory = knowledge / "raw" / "auto-memory"
    scope_a = auto_memory / "-Users-tristankromer-Code"
    scope_a.mkdir(parents=True)
    (scope_a / "feedback_develop_first_flow.md").write_text(
        "---\nname: develop-first flow\ntags: [workflow, git]\n"
        "description: Ship to develop first, promote to staging after CI.\n"
        "---\n\n"
        "When shipping changes, always merge to the develop branch first. "
        "Promotion to staging happens via the ref API after CI is green.\n"
    )
    # Per-scope MEMORY.md index — must be excluded (filename pattern)
    (scope_a / "MEMORY.md").write_text(
        "# MEMORY INDEX\n\n- [develop-first flow](feedback_develop_first_flow.md)\n"
    )

    unscoped = auto_memory / "_unscoped"
    unscoped.mkdir()
    (unscoped / "feedback_bayesian_is_a_prompt.md").write_text(
        "---\nname: bayesian prompt\ntags: [prompting]\n---\n\n"
        "Framing prompts as bayesian prior updates tightens outputs.\n"
    )

    # Non-markdown file (migration log) — must be skipped
    (auto_memory / "_migration-log.jsonl").write_text(
        '{"ts": "2026-04-21T00:00:00Z", "action": "migrated"}\n'
    )

    return wiki, auto_memory


class TestFTS5ExtraRoots:
    """Extra-root intake coverage for the FTS5 backend."""

    def test_indexes_auto_memory_files(
        self, wiki_and_auto_memory: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wiki, auto_memory = wiki_and_auto_memory
        cache = tmp_path / "cache"
        count = FTS5Backend().build_index(
            wiki,
            cache,
            extra_roots=[auto_memory],
        )
        # wiki: 1, scope_a feedback: 1, _unscoped: 1 = 3
        assert count == 3

    def test_recall_finds_auto_memory_topic(
        self, wiki_and_auto_memory: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Acceptance: a known auto-memory topic resolves via recall."""
        wiki, auto_memory = wiki_and_auto_memory
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki, cache, extra_roots=[auto_memory])
        results = backend.query("develop first flow", cache, n=5)
        filenames = [r[0] for r in results]
        # Indexed as <root_name>/<relpath_posix>
        assert (
            "auto-memory/-Users-tristankromer-Code/" "feedback_develop_first_flow.md"
        ) in filenames

    def test_memory_index_excluded(
        self, wiki_and_auto_memory: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Acceptance: per-scope MEMORY.md is not a recall hit."""
        wiki, auto_memory = wiki_and_auto_memory
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki, cache, extra_roots=[auto_memory])
        # Even a targeted MEMORY-flavored query must not surface the index
        results = backend.query("memory index develop", cache, n=10)
        filenames = [r[0] for r in results]
        assert not any(f.endswith("MEMORY.md") for f in filenames)

    def test_unscoped_included(
        self, wiki_and_auto_memory: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Acceptance: files under ``_unscoped/`` are recall-hittable."""
        wiki, auto_memory = wiki_and_auto_memory
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki, cache, extra_roots=[auto_memory])
        results = backend.query("bayesian prompt prompting", cache, n=5)
        filenames = [r[0] for r in results]
        assert any("_unscoped/feedback_bayesian_is_a_prompt.md" in f for f in filenames)

    def test_non_markdown_skipped(
        self, wiki_and_auto_memory: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """JSONL migration log must not enter the markdown index."""
        wiki, auto_memory = wiki_and_auto_memory
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        count = backend.build_index(wiki, cache, extra_roots=[auto_memory])
        # Count is wiki(1) + scope_a feedback(1) + _unscoped(1) = 3
        # If the JSONL slipped in, count would be 4
        assert count == 3

    def test_missing_extra_root_does_not_raise(self, tmp_path: Path) -> None:
        """A missing extra root is silently dropped, not fatal."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "p.md").write_text("---\nname: P\n---\ncontent\n")
        cache = tmp_path / "cache"
        missing = tmp_path / "does-not-exist"
        count = FTS5Backend().build_index(
            wiki,
            cache,
            extra_roots=[missing],
        )
        assert count == 1

    def test_wiki_entries_still_bare_filename(
        self, wiki_and_auto_memory: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Wiki entries must keep the bare-filename convention so existing
        recall consumers (and the kept-stable ``wiki/<name>.md`` path
        display) don't regress."""
        wiki, auto_memory = wiki_and_auto_memory
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki, cache, extra_roots=[auto_memory])
        results = backend.query("lean startup methodology", cache, n=5)
        filenames = [r[0] for r in results]
        assert "lean-startup.md" in filenames


class TestVectorExtraRoots:
    """Extra-root intake coverage for the vector backend.

    The vector backend must index auto-memory files under the same
    ``<root_name>/<relpath>`` key scheme as FTS5 so the hybrid recall
    merge in the MCP layer sees one id space. ``MEMORY.md`` is excluded
    and ``_unscoped/`` is included on identical terms.
    """

    @pytest.fixture(autouse=True)
    def _require_chromadb(self) -> None:
        pytest.importorskip("chromadb")

    def test_indexes_auto_memory_files(
        self, wiki_and_auto_memory: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wiki, auto_memory = wiki_and_auto_memory
        cache = tmp_path / "cache"
        count = VectorBackend().build_index(
            wiki,
            cache,
            extra_roots=[auto_memory],
        )
        assert count == 3

    def test_recall_finds_auto_memory_topic(
        self, wiki_and_auto_memory: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wiki, auto_memory = wiki_and_auto_memory
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki, cache, extra_roots=[auto_memory])
        results = backend.query("develop first flow", cache, n=5)
        filenames = [r[0] for r in results]
        assert any(f.endswith("feedback_develop_first_flow.md") for f in filenames)

    def test_memory_index_excluded(
        self, wiki_and_auto_memory: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wiki, auto_memory = wiki_and_auto_memory
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki, cache, extra_roots=[auto_memory])
        results = backend.query("memory index develop", cache, n=10)
        filenames = [r[0] for r in results]
        assert not any(f.endswith("MEMORY.md") for f in filenames)


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


class TestHybridRescueClasses:
    """Each backend rescues a failure class the other has.

    This is the evidence behind the README claim that the hybrid merge is
    load-bearing — not just "both backends work on the same wiki" but
    "neither backend alone surfaces the page on its rescue-class query."
    Without this pin, a future simplification that drops one backend will
    still pass ``test_query_finds_match`` on the obvious lexical queries
    and quietly regress the non-obvious ones.

    See ``docs/recall-architecture.md`` for the full walkthrough.
    """

    @pytest.fixture(autouse=True)
    def _require_chromadb(self) -> None:
        pytest.importorskip("chromadb")

    @pytest.fixture
    def rescue_wiki(self, tmp_path: Path) -> Path:
        """A two-entity wiki that exposes each backend's blind spot.

        - ``return-path.md``: short proper-noun entity (3 words of body). A
          vector query for "Return Path" embeds closer to pages containing
          the generic token "path" than to this sparse page. FTS5 phrase
          matching on the frontmatter finds it.
        - ``innovation-accounting.md``: a semantically-rich page that never
          uses the query tokens "iterative feedback loops." FTS5 can't
          match it; vector embeds the concepts together.
        """
        wiki = tmp_path / "wiki"
        wiki.mkdir()

        (wiki / "return-path.md").write_text(
            "---\n"
            "name: Return Path\n"
            "tags: [company, past-client]\n"
            "description: Email deliverability SaaS acquired by Validity\n"
            "---\n\n"
            "Brief stub.\n"
        )
        (wiki / "innovation-accounting.md").write_text(
            "---\n"
            "name: Innovation Accounting\n"
            "tags: [methodology, metrics]\n"
            "description: Ries's measurement framework for startups\n"
            "---\n\n"
            "A way to measure progress when traditional accounting fails. "
            "It emphasises learning milestones and validated experiments — "
            "the cycle of building a small change, measuring the result, "
            "and adjusting course based on what you learned. The core idea "
            "is that improvement compounds through short cycles of "
            "hypothesis, experiment, and adjustment rather than through "
            "one big plan.\n"
        )
        # A distractor page containing the token "path" so the vector query
        # for "Return Path" has a plausible-but-wrong nearest neighbour.
        (wiki / "migration-path.md").write_text(
            "---\n"
            "name: Migration Path\n"
            "tags: [infra]\n"
            "description: Generic upgrade path documentation\n"
            "---\n\n"
            "A migration path is the sequence of steps to move a system "
            "from one state to another.\n"
        )
        return wiki

    def test_fts5_rescues_short_proper_noun(
        self, rescue_wiki: Path, tmp_path: Path
    ) -> None:
        """FTS5 must find ``return-path.md`` on the query "Return Path"
        even though vector embedding places it below a "path"-heavy page.
        """
        cache = tmp_path / "cache"
        FTS5Backend().build_index(rescue_wiki, cache)
        results = FTS5Backend().query("Return Path", cache, n=3)
        filenames = [r[0] for r in results]
        assert "return-path.md" in filenames, (
            "FTS5 must surface the proper-noun entity on a short query — "
            "this is the failure class vector embedding alone misses."
        )
        # And it must rank ahead of the distractor.
        assert (
            filenames.index("return-path.md") < filenames.index("migration-path.md")
            if "migration-path.md" in filenames
            else True
        )

    def test_vector_rescues_semantic_no_overlap(
        self, rescue_wiki: Path, tmp_path: Path
    ) -> None:
        """Vector must find ``innovation-accounting.md`` on a query that
        shares no literal tokens with its body or frontmatter.
        """
        cache = tmp_path / "cache"
        VectorBackend().build_index(rescue_wiki, cache)
        results = VectorBackend().query("iterative feedback loops", cache, n=3)
        filenames = [r[0] for r in results]
        assert "innovation-accounting.md" in filenames, (
            "Vector must surface the semantic neighbour even when the "
            "query has zero lexical overlap — this is the failure class "
            "FTS5 alone misses."
        )

    def test_fts5_misses_semantic_query(
        self, rescue_wiki: Path, tmp_path: Path
    ) -> None:
        """FTS5 alone cannot find ``innovation-accounting.md`` on
        "iterative feedback loops." If this assertion ever flips (e.g.
        the page body gets rewritten to contain those words, or a
        porter-stemmer collision pulls it in), the rescue-class claim
        needs revisiting — not just the test.
        """
        cache = tmp_path / "cache"
        FTS5Backend().build_index(rescue_wiki, cache)
        results = FTS5Backend().query("iterative feedback loops", cache, n=3)
        filenames = [r[0] for r in results]
        assert "innovation-accounting.md" not in filenames


class TestHybridRescueClassesExtraRoot:
    """Hybrid rescue semantics also hold when the winning doc lives in an
    extra intake root (``raw/auto-memory/<scope>/``) rather than wiki.

    The MCP recall layer feeds both wiki/ and extra-root docs through the
    same hybrid merge. If the rescue-class claim only held for wiki-rooted
    docs, a scope-indexed memory would silently fall out of recall on the
    exact failure-class queries that motivate the hybrid design. These
    tests pin the invariant: same rescue outcome regardless of which root
    the doc lives under.
    """

    @pytest.fixture(autouse=True)
    def _require_chromadb(self) -> None:
        pytest.importorskip("chromadb")

    @pytest.fixture
    def rescue_wiki_and_auto_memory(self, tmp_path: Path) -> tuple[Path, Path]:
        """Mirror of ``TestHybridRescueClasses.rescue_wiki`` but with the
        rescue targets relocated to ``raw/auto-memory/<scope>/``.

        - wiki holds only a ``path``-heavy distractor so the vector query
          for "Return Path" has a plausible-but-wrong nearest neighbour.
        - scope dir holds ``feedback_return_path.md`` (short proper-noun,
          FTS5 rescue class) and ``feedback_innovation_accounting.md``
          (semantic no-overlap, vector rescue class).
        """
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)

        # Distractor in wiki — token "path" so vector for "Return Path"
        # has a plausible-but-wrong nearest neighbour.
        (wiki / "migration-path.md").write_text(
            "---\n"
            "name: Migration Path\n"
            "tags: [infra]\n"
            "description: Generic upgrade path documentation\n"
            "---\n\n"
            "A migration path is the sequence of steps to move a system "
            "from one state to another.\n"
        )

        auto_memory = knowledge / "raw" / "auto-memory"
        scope = auto_memory / "-Users-tristankromer-Code"
        scope.mkdir(parents=True)

        # FTS5 rescue class — short proper-noun entity in extra root.
        (scope / "feedback_return_path.md").write_text(
            "---\n"
            "name: Return Path\n"
            "tags: [company, past-client]\n"
            "description: Email deliverability SaaS acquired by Validity\n"
            "---\n\n"
            "Brief stub.\n"
        )

        # Vector rescue class — semantically-rich page that never uses the
        # query tokens "iterative feedback loops."
        (scope / "feedback_innovation_accounting.md").write_text(
            "---\n"
            "name: Innovation Accounting\n"
            "tags: [methodology, metrics]\n"
            "description: Ries's measurement framework for startups\n"
            "---\n\n"
            "A way to measure progress when traditional accounting fails. "
            "It emphasises learning milestones and validated experiments — "
            "the cycle of building a small change, measuring the result, "
            "and adjusting course based on what you learned. The core idea "
            "is that improvement compounds through short cycles of "
            "hypothesis, experiment, and adjustment rather than through "
            "one big plan.\n"
        )
        return wiki, auto_memory

    def test_fts5_rescues_short_proper_noun_in_extra_root(
        self,
        rescue_wiki_and_auto_memory: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        """FTS5 must surface ``feedback_return_path.md`` on "Return Path"
        even though the doc lives in ``raw/auto-memory/<scope>/`` rather
        than wiki/. Extra-root keys are ``<root_name>/<relpath>``.
        """
        wiki, auto_memory = rescue_wiki_and_auto_memory
        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki, cache, extra_roots=[auto_memory])
        results = FTS5Backend().query("Return Path", cache, n=3)
        filenames = [r[0] for r in results]
        expected = "auto-memory/-Users-tristankromer-Code/" "feedback_return_path.md"
        assert expected in filenames, (
            "FTS5 must surface the proper-noun entity on a short query "
            "when the doc lives in an extra intake root — same rescue "
            "class as wiki-rooted docs."
        )
        # And must rank ahead of the wiki distractor.
        if "migration-path.md" in filenames:
            assert filenames.index(expected) < filenames.index("migration-path.md")

    def test_vector_rescues_semantic_no_overlap_in_extra_root(
        self,
        rescue_wiki_and_auto_memory: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        """Vector must surface ``feedback_innovation_accounting.md`` on a
        query with zero lexical overlap, even when the doc lives in
        ``raw/auto-memory/<scope>/`` rather than wiki/.
        """
        wiki, auto_memory = rescue_wiki_and_auto_memory
        cache = tmp_path / "cache"
        VectorBackend().build_index(wiki, cache, extra_roots=[auto_memory])
        results = VectorBackend().query("iterative feedback loops", cache, n=3)
        filenames = [r[0] for r in results]
        expected = (
            "auto-memory/-Users-tristankromer-Code/" "feedback_innovation_accounting.md"
        )
        assert expected in filenames, (
            "Vector must surface the semantic neighbour on a zero-overlap "
            "query when the doc lives in an extra intake root — same "
            "rescue class as wiki-rooted docs."
        )


# ---------------------------------------------------------------------------
# Incremental indexing (issue #348) — whole-file hash diff for both backends
# ---------------------------------------------------------------------------


@pytest.fixture
def delta_spy(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture the ``(added, changed, removed)`` delta of the last rebuild.

    Both backends route the incremental delta through the module-level
    ``_compute_delta``; spying there is backend-agnostic. It is only invoked
    on the incremental path (a seeded manifest + live index), so the seed
    build leaves ``captured`` empty and the second build populates it.
    """
    captured: dict = {}
    orig = search_module._compute_delta

    def spy(current_hashes, stored_hashes):  # type: ignore[no-untyped-def]
        added, changed, removed = orig(current_hashes, stored_hashes)
        captured["added"] = added
        captured["changed"] = changed
        captured["removed"] = removed
        return added, changed, removed

    monkeypatch.setattr(search_module, "_compute_delta", spy)
    return captured


def _write_page(
    wiki: Path, fname: str, *, name: str, body: str, extra_fm: str = ""
) -> None:
    """Write a wiki page with the given frontmatter name/body."""
    fm = f"name: {name}\n"
    if extra_fm:
        fm += extra_fm if extra_fm.endswith("\n") else extra_fm + "\n"
    (wiki / fname).write_text(f"---\n{fm}---\n\n{body}\n")


class TestFTS5Incremental:
    """Hash-diff coverage for the FTS5 backend: add/update/delete/no-op."""

    @pytest.fixture
    def seeded(self, wiki_with_pages: Path, tmp_path: Path) -> tuple[Path, Path]:
        cache = tmp_path / "cache"
        # Seed build writes the manifest; subsequent builds go incremental.
        FTS5Backend().build_index(wiki_with_pages, cache)
        assert (cache / "fts5-manifest.json").is_file()
        return wiki_with_pages, cache

    def test_noop_touches_nothing(
        self,
        seeded: tuple[Path, Path],
        delta_spy: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wiki, cache = seeded
        # Also spy the row-builder: on a no-op it must never be called.
        rows_built = {"n": 0}
        orig_row = FTS5Backend._row_for

        def counting_row(name, path, text, meta):  # type: ignore[no-untyped-def]
            rows_built["n"] += 1
            return orig_row(name, path, text, meta)

        monkeypatch.setattr(FTS5Backend, "_row_for", staticmethod(counting_row))

        count = FTS5Backend().build_index(wiki, cache)
        assert delta_spy["added"] == []
        assert delta_spy["changed"] == []
        assert delta_spy["removed"] == []
        assert rows_built["n"] == 0  # zero inserts on a no-op
        assert count == 3

    def test_add_new_page(self, seeded: tuple[Path, Path], delta_spy: dict) -> None:
        wiki, cache = seeded
        _write_page(
            wiki,
            "growth-loops.md",
            name="Growth Loops",
            body="Compounding acquisition loops for startups.",
        )
        count = FTS5Backend().build_index(wiki, cache)
        assert delta_spy["added"] == ["growth-loops.md"]
        assert delta_spy["changed"] == []
        assert delta_spy["removed"] == []
        assert count == 4
        results = FTS5Backend().query("growth loops compounding", cache)
        assert "growth-loops.md" in [r[0] for r in results]

    def test_update_body(self, seeded: tuple[Path, Path], delta_spy: dict) -> None:
        wiki, cache = seeded
        # Same frontmatter, different body → whole-file hash changes.
        (wiki / "acme-corp.md").write_text(
            "---\n"
            "name: Acme Corp\n"
            "tags: [client, fintech]\n"
            "description: Enterprise client in financial services\n"
            "---\n\n"
            "Acme Corp pivoted to a quantum cryptography product line.\n"
        )
        count = FTS5Backend().build_index(wiki, cache)
        # A whole-file hash change re-indexes even when the change is in the
        # body (FTS5 indexes frontmatter only, so the query surface is
        # unchanged here — the point is the differ never MISSES the edit).
        assert delta_spy["changed"] == ["acme-corp.md"]
        assert delta_spy["added"] == []
        assert delta_spy["removed"] == []
        assert count == 3  # replace, not accrete
        # The page is still present and findable via its frontmatter.
        results = FTS5Backend().query("acme fintech", cache)
        assert "acme-corp.md" in [r[0] for r in results]

    def test_update_frontmatter_only(
        self, seeded: tuple[Path, Path], delta_spy: dict
    ) -> None:
        """A frontmatter-only edit (body byte-identical) must re-index.

        The whole-file hash covers frontmatter, so an audience/tag/name
        change is caught where a body-only hash would miss it (#312).
        """
        wiki, cache = seeded
        # Body identical to the fixture; only the tags line changes.
        (wiki / "customer-development.md").write_text(
            "---\n"
            "name: Customer Development\n"
            "tags: [methodology, customers, sales]\n"
            "description: Steve Blank's customer development process\n"
            "---\n\n"
            "Customer development is a four-step framework for startups.\n"
        )
        count = FTS5Backend().build_index(wiki, cache)
        assert delta_spy["changed"] == ["customer-development.md"]
        assert delta_spy["added"] == []
        assert delta_spy["removed"] == []
        assert count == 3

    def test_delete_page(self, seeded: tuple[Path, Path], delta_spy: dict) -> None:
        wiki, cache = seeded
        (wiki / "acme-corp.md").unlink()
        count = FTS5Backend().build_index(wiki, cache)
        assert delta_spy["removed"] == ["acme-corp.md"]
        assert delta_spy["added"] == []
        assert delta_spy["changed"] == []
        assert count == 2
        # Deleted page must disappear from recall.
        results = FTS5Backend().query("acme fintech", cache)
        assert "acme-corp.md" not in [r[0] for r in results]

    def test_flip_inactive_is_a_delete(
        self, seeded: tuple[Path, Path], delta_spy: dict
    ) -> None:
        """A page that flips to inactive (deprecated) drops from the index."""
        wiki, cache = seeded
        (wiki / "acme-corp.md").write_text(
            "---\n"
            "name: Acme Corp\n"
            "tags: [client, fintech]\n"
            "description: Enterprise client in financial services\n"
            "deprecated: true\n"
            "---\n\n"
            "Acme Corp is a fintech company.\n"
        )
        count = FTS5Backend().build_index(wiki, cache)
        assert delta_spy["removed"] == ["acme-corp.md"]
        assert count == 2
        results = FTS5Backend().query("acme fintech", cache)
        assert "acme-corp.md" not in [r[0] for r in results]

    def test_full_flag_rebuilds_from_scratch(self, seeded: tuple[Path, Path]) -> None:
        """``incremental=False`` wipes and rebuilds (seed / reindex --full)."""
        wiki, cache = seeded
        _write_page(
            wiki,
            "extra.md",
            name="Extra",
            body="An extra page body.",
        )
        count = FTS5Backend().build_index(wiki, cache, incremental=False)
        assert count == 4
        results = FTS5Backend().query("extra page body", cache)
        assert "extra.md" in [r[0] for r in results]


class TestVectorIncremental:
    """Hash-diff coverage for the vector backend: add/update/delete/no-op."""

    @pytest.fixture(autouse=True)
    def _require_chromadb(self) -> None:
        pytest.importorskip("chromadb")

    @pytest.fixture
    def seeded(self, wiki_with_pages: Path, tmp_path: Path) -> tuple[Path, Path]:
        cache = tmp_path / "cache"
        VectorBackend().build_index(wiki_with_pages, cache)
        assert (cache / "vector-manifest.json").is_file()
        return wiki_with_pages, cache

    def test_noop_reembeds_nothing(
        self,
        seeded: tuple[Path, Path],
        delta_spy: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wiki, cache = seeded
        # Spy the embed/add path: on a no-op it must never be called.
        added_records = {"n": 0}
        orig_add = VectorBackend._add_records

        def counting_add(self, collection, records):  # type: ignore[no-untyped-def]
            added_records["n"] += len(records)
            return orig_add(self, collection, records)

        monkeypatch.setattr(VectorBackend, "_add_records", counting_add)

        count = VectorBackend().build_index(wiki, cache)
        assert delta_spy["added"] == []
        assert delta_spy["changed"] == []
        assert delta_spy["removed"] == []
        assert added_records["n"] == 0  # zero re-embeds on a no-op
        assert count == 3

    def test_add_new_page(self, seeded: tuple[Path, Path], delta_spy: dict) -> None:
        wiki, cache = seeded
        _write_page(
            wiki,
            "growth-loops.md",
            name="Growth Loops",
            body="Compounding acquisition loops for startups.",
        )
        count = VectorBackend().build_index(wiki, cache)
        assert delta_spy["added"] == ["growth-loops.md"]
        assert delta_spy["removed"] == []
        assert count == 4
        results = VectorBackend().query("compounding acquisition loops", cache)
        assert "growth-loops.md" in [r[0] for r in results]

    def test_update_body(self, seeded: tuple[Path, Path], delta_spy: dict) -> None:
        wiki, cache = seeded
        (wiki / "acme-corp.md").write_text(
            "---\n"
            "name: Acme Corp\n"
            "tags: [client, fintech]\n"
            "description: Enterprise client in financial services\n"
            "---\n\n"
            "Acme Corp pivoted to a quantum cryptography product line.\n"
        )
        count = VectorBackend().build_index(wiki, cache)
        assert delta_spy["changed"] == ["acme-corp.md"]
        assert delta_spy["added"] == []
        assert delta_spy["removed"] == []
        assert count == 3  # replace, not accrete

    def test_update_frontmatter_only(
        self, seeded: tuple[Path, Path], delta_spy: dict
    ) -> None:
        wiki, cache = seeded
        # Body byte-identical; only frontmatter tags change.
        (wiki / "customer-development.md").write_text(
            "---\n"
            "name: Customer Development\n"
            "tags: [methodology, customers, sales]\n"
            "description: Steve Blank's customer development process\n"
            "---\n\n"
            "Customer development is a four-step framework for startups.\n"
        )
        count = VectorBackend().build_index(wiki, cache)
        assert delta_spy["changed"] == ["customer-development.md"]
        assert delta_spy["added"] == []
        assert delta_spy["removed"] == []
        assert count == 3

    def test_delete_page(self, seeded: tuple[Path, Path], delta_spy: dict) -> None:
        wiki, cache = seeded
        (wiki / "acme-corp.md").unlink()
        count = VectorBackend().build_index(wiki, cache)
        assert delta_spy["removed"] == ["acme-corp.md"]
        assert delta_spy["added"] == []
        assert delta_spy["changed"] == []
        assert count == 2
        results = VectorBackend().query("fintech financial services", cache, n=5)
        assert "acme-corp.md" not in [r[0] for r in results]

    def test_flip_inactive_is_a_delete(
        self, seeded: tuple[Path, Path], delta_spy: dict
    ) -> None:
        wiki, cache = seeded
        (wiki / "acme-corp.md").write_text(
            "---\n"
            "name: Acme Corp\n"
            "tags: [client, fintech]\n"
            "description: Enterprise client in financial services\n"
            "deprecated: true\n"
            "---\n\n"
            "Acme Corp is a fintech company.\n"
        )
        count = VectorBackend().build_index(wiki, cache)
        assert delta_spy["removed"] == ["acme-corp.md"]
        assert count == 2

    def test_full_flag_rebuilds_from_scratch(self, seeded: tuple[Path, Path]) -> None:
        wiki, cache = seeded
        _write_page(
            wiki,
            "extra.md",
            name="Extra",
            body="An extra page body.",
        )
        count = VectorBackend().build_index(wiki, cache, incremental=False)
        assert count == 4

    def test_embedding_model_swap_forces_full_rebuild(
        self,
        seeded: tuple[Path, Path],
        delta_spy: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A different configured model must re-embed the whole corpus.

        The alternate EF is stubbed (no real model download) so the seam is
        exercised without a heavy embed. The manifest records the model, so
        a mismatch bypasses the incremental path entirely (no delta call).
        """
        wiki, cache = seeded

        # Stub the alternate embedding function so no model is downloaded.
        monkeypatch.setattr(VectorBackend, "_embedding_function", lambda self: None)
        backend = VectorBackend(embedding_model="some-other-model")
        count = backend.build_index(wiki, cache)
        # Model changed → full rebuild, so the delta spy was never invoked.
        assert "added" not in delta_spy
        assert count == 3
        # Manifest now records the swapped model.
        import json

        manifest = json.loads((cache / "vector-manifest.json").read_text())
        assert manifest["embedding_model"] == "some-other-model"


class TestIndexGlobs:
    """Corpus-scoping include/exclude globs (issue #348 COULD)."""

    def test_exclude_glob_skips_matching_pages(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        count = FTS5Backend().build_index(
            wiki_with_pages, cache, exclude_globs=["acme-*.md"]
        )
        assert count == 2  # acme-corp.md excluded
        results = FTS5Backend().query("acme fintech", cache)
        assert "acme-corp.md" not in [r[0] for r in results]

    def test_include_glob_restricts_to_matching_pages(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        count = FTS5Backend().build_index(
            wiki_with_pages, cache, include_globs=["lean-*.md"]
        )
        assert count == 1  # only lean-startup.md
        results = FTS5Backend().query("lean startup methodology", cache)
        assert [r[0] for r in results] == ["lean-startup.md"]

    def test_default_indexes_everything(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        count = FTS5Backend().build_index(wiki_with_pages, cache)
        assert count == 3  # no globs → index-all


class TestIncrementalHelpers:
    """Unit coverage for the shared hash-diff helpers."""

    def test_compute_delta(self) -> None:
        current = {"a.md": "h1", "b.md": "h2new", "c.md": "h3"}
        stored = {"a.md": "h1", "b.md": "h2old", "d.md": "h4"}
        added, changed, removed = search_module._compute_delta(current, stored)
        assert added == ["c.md"]
        assert changed == ["b.md"]
        assert removed == ["d.md"]

    def test_passes_globs_default(self) -> None:
        assert search_module._passes_globs("x.md", None, None) is True

    def test_passes_globs_include(self) -> None:
        assert search_module._passes_globs("lean.md", ["lean*"], None) is True
        assert search_module._passes_globs("acme.md", ["lean*"], None) is False

    def test_passes_globs_exclude(self) -> None:
        assert search_module._passes_globs("acme.md", None, ["acme*"]) is False
        assert search_module._passes_globs("lean.md", None, ["acme*"]) is True


# ---------------------------------------------------------------------------
# Stat pre-filter (issue #370) — skip re-reading files whose (mtime,size) match
# ---------------------------------------------------------------------------


class TestStatPreFilter:
    """The manifest stores per-file (mtime_ns, size); a stat match reuses the
    stored hash without reading/hashing the body (rsync-style heuristic)."""

    @pytest.fixture
    def hash_spy(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        """Count sha256 invocations — one per file body actually read+hashed.

        The only ``hashlib.sha256`` caller on the FTS5 build path is the scan's
        read+hash of a file body, so the count is exactly the number of files
        NOT served by the stat fast-path.
        """
        calls = {"n": 0}
        orig = search_module.hashlib.sha256

        def spy(data: bytes = b"") -> object:
            calls["n"] += 1
            return orig(data)

        monkeypatch.setattr(search_module.hashlib, "sha256", spy)
        return calls

    def test_seed_writes_v2_manifest_with_stats(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        import json

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)
        m = json.loads((cache / "fts5-manifest.json").read_text())
        assert m["version"] == 2
        assert set(m["stats"]) == set(m["hashes"])
        # Each stat record is (mtime_ns, size, valid_until).
        rec = next(iter(m["stats"].values()))
        assert len(rec) == 3
        assert isinstance(rec[0], int) and isinstance(rec[1], int)

    def test_unchanged_rebuild_reads_no_bodies(
        self, wiki_with_pages: Path, tmp_path: Path, hash_spy: dict, delta_spy: dict
    ) -> None:
        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)  # seed reads+hashes
        hash_spy["n"] = 0  # reset AFTER the seed
        count = FTS5Backend().build_index(wiki_with_pages, cache)
        # No file touched → every page stat-matches → zero bodies re-hashed.
        assert hash_spy["n"] == 0
        assert delta_spy["added"] == []
        assert delta_spy["changed"] == []
        assert delta_spy["removed"] == []
        assert count == 3

    def test_changed_file_is_rehashed_and_reindexed(
        self, wiki_with_pages: Path, tmp_path: Path, hash_spy: dict, delta_spy: dict
    ) -> None:
        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)
        hash_spy["n"] = 0
        # Edit one body (size + mtime change) → only that file re-hashed.
        (wiki_with_pages / "acme-corp.md").write_text(
            "---\n"
            "name: Acme Corp\n"
            "tags: [client, fintech]\n"
            "description: Enterprise client in financial services\n"
            "---\n\n"
            "Acme Corp pivoted to a quantum cryptography product line entirely.\n"
        )
        FTS5Backend().build_index(wiki_with_pages, cache)
        assert hash_spy["n"] == 1  # ONLY the changed file was read+hashed
        assert delta_spy["changed"] == ["acme-corp.md"]
        assert delta_spy["added"] == []
        assert delta_spy["removed"] == []

    def test_mtime_bump_same_content_touches_no_index_rows(
        self,
        wiki_with_pages: Path,
        tmp_path: Path,
        delta_spy: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stat change with IDENTICAL content is re-hashed but never re-indexed.

        Bumping mtime breaks the (mtime,size) match, so the body is re-read to
        re-verify the hash — but the identical hash means the differ reports NO
        change and zero index rows are touched. This is the correctness backstop
        for the rsync heuristic: a stat change never causes a spurious re-index.
        """
        import os

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)
        p = wiki_with_pages / "acme-corp.md"
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))

        rows_built = {"n": 0}
        orig_row = FTS5Backend._row_for

        def counting_row(name, path, text, meta):  # type: ignore[no-untyped-def]
            rows_built["n"] += 1
            return orig_row(name, path, text, meta)

        monkeypatch.setattr(FTS5Backend, "_row_for", staticmethod(counting_row))
        count = FTS5Backend().build_index(wiki_with_pages, cache)
        assert delta_spy["changed"] == []  # identical content → not a change
        assert rows_built["n"] == 0  # zero index rows touched
        assert count == 3

    def test_v1_manifest_backcompat_upgrades_to_v2(
        self, wiki_with_pages: Path, tmp_path: Path, hash_spy: dict
    ) -> None:
        """A v1 manifest (hashes only, no stats) loads, forces one full re-hash,
        and upgrades to v2 with stats on write."""
        import json

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)  # writes v2
        mpath = cache / "fts5-manifest.json"
        m = json.loads(mpath.read_text())
        # Downgrade to a pre-#370 v1 manifest: drop the stat map entirely.
        mpath.write_text(json.dumps({"version": 1, "hashes": m["hashes"]}))

        hash_spy["n"] = 0
        count = FTS5Backend().build_index(wiki_with_pages, cache)
        # No stats → the fast-path can't fire → every file is read+hashed once.
        assert hash_spy["n"] == 3
        assert count == 3
        # The manifest is upgraded back to v2 with a full stat map.
        m2 = json.loads(mpath.read_text())
        assert m2["version"] == 2
        assert set(m2["stats"]) == set(m2["hashes"])

    def test_valid_until_expiry_drops_page_without_reading(
        self, tmp_path: Path
    ) -> None:
        """A content-unchanged page whose valid_until has passed is dropped on a
        later build (date-expiry re-checked from the stored bound, no read)."""
        from datetime import date, timedelta

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # valid_until yesterday, but written "as of" a past build is simulated by
        # building first while still valid is not possible retroactively; instead
        # assert the stored-bound path: a page already expired is absent, and a
        # far-future page stays. Use a future bound so the seed indexes it.
        future = (date.today() + timedelta(days=3650)).isoformat()
        (wiki / "temp.md").write_text(
            f"---\nname: Temp\nvalid_until: {future}\n---\n\nStill valid.\n"
        )
        cache = tmp_path / "cache"
        assert FTS5Backend().build_index(wiki, cache) == 1

        # Rewrite the manifest's stored valid_until to a PAST date, leaving stat
        # (mtime,size) untouched so the fast-path fires — the stored-bound expiry
        # re-check must then drop the page WITHOUT the file being read.
        import json

        mpath = cache / "fts5-manifest.json"
        m = json.loads(mpath.read_text())
        past = (date.today() - timedelta(days=1)).isoformat()
        name = next(iter(m["stats"]))
        mtime_ns, size, _vu = m["stats"][name]
        m["stats"][name] = [mtime_ns, size, past]
        mpath.write_text(json.dumps(m))

        # Guard: the body must NOT be read on this build (stat still matches).
        import athenaeum.search as sm

        orig = sm.hashlib.sha256

        def boom(_data: bytes = b"") -> object:  # pragma: no cover - guard
            raise AssertionError("stat-matched file must not be re-hashed")

        try:
            sm.hashlib.sha256 = boom  # type: ignore[assignment]
            count = FTS5Backend().build_index(wiki, cache)
        finally:
            sm.hashlib.sha256 = orig  # type: ignore[assignment]
        assert count == 0  # expired page dropped from the index


class TestFetchEmbeddingsNoModelLoad:
    """fetch_embeddings is a pure read; it must not attach the default (ONNX) EF."""

    @pytest.fixture(autouse=True)
    def _require_chromadb(self) -> None:
        pytest.importorskip("chromadb")

    def test_get_collection_called_with_no_embedding_function(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import chromadb
        from chromadb.api.client import Client, SharedSystemClient

        from athenaeum.search import _VECTOR_COLLECTION, _VECTOR_DIR

        cache = tmp_path / "cache"
        vector_dir = cache / _VECTOR_DIR
        vector_dir.mkdir(parents=True)

        # Build a collection with PRE-COMPUTED embeddings and no EF, exactly as
        # the read path expects — so no ONNX model is needed to seed it either.
        SharedSystemClient.clear_system_cache()
        client = chromadb.PersistentClient(path=str(vector_dir))
        col = client.create_collection(_VECTOR_COLLECTION, embedding_function=None)
        col.add(
            ids=["a.md", "b.md"],
            embeddings=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
            documents=["x", "y"],
        )

        # Spy: fetch_embeddings must open the collection with embedding_function
        # explicitly None, so chromadb never constructs its default (ONNX) EF.
        captured: dict = {}
        orig_get = Client.get_collection

        def spy_get(self, name, *a, **k):  # type: ignore[no-untyped-def]
            captured["ef"] = k.get("embedding_function", "MISSING")
            return orig_get(self, name, *a, **k)

        monkeypatch.setattr(Client, "get_collection", spy_get)

        out = VectorBackend().fetch_embeddings(["a.md", "b.md"], cache)
        assert captured["ef"] is None
        assert set(out) == {"a.md", "b.md"}
        assert out["a.md"] == pytest.approx([0.1, 0.2, 0.3], abs=1e-6)
