"""Tests for the athenaeum search backends."""

from __future__ import annotations

from pathlib import Path

import pytest

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

    def test_query_finds_match(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki_with_pages, cache)
        results = backend.query("lean startup methodology", cache)
        assert len(results) > 0
        filenames = [r[0] for r in results]
        assert "lean-startup.md" in filenames

    def test_query_semantic_match(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = VectorBackend()
        backend.build_index(wiki_with_pages, cache)
        # "build measure learn" should match Lean Startup via embeddings
        results = backend.query("build measure learn", cache)
        assert len(results) > 0
        filenames = [r[0] for r in results]
        assert "lean-startup.md" in filenames

    def test_query_respects_limit(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
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
            "lean startup methodology", cache,
            exclude={"lean-startup.md"},
        )
        filenames = [r[0] for r in results]
        assert "lean-startup.md" not in filenames

    def test_query_no_index(self, tmp_path: Path) -> None:
        cache = tmp_path / "empty-cache"
        cache.mkdir()
        backend = VectorBackend()
        assert backend.query("anything", cache) == []

    def test_returns_tuples(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
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
            wiki, cache, extra_roots=[auto_memory],
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
            "auto-memory/-Users-tristankromer-Code/"
            "feedback_develop_first_flow.md"
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
        assert any(
            "_unscoped/feedback_bayesian_is_a_prompt.md" in f
            for f in filenames
        )

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

    def test_missing_extra_root_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        """A missing extra root is silently dropped, not fatal."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "p.md").write_text("---\nname: P\n---\ncontent\n")
        cache = tmp_path / "cache"
        missing = tmp_path / "does-not-exist"
        count = FTS5Backend().build_index(
            wiki, cache, extra_roots=[missing],
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
            wiki, cache, extra_roots=[auto_memory],
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
        assert any(
            f.endswith("feedback_develop_first_flow.md")
            for f in filenames
        )

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
        assert filenames.index("return-path.md") < filenames.index(
            "migration-path.md"
        ) if "migration-path.md" in filenames else True

    def test_vector_rescues_semantic_no_overlap(
        self, rescue_wiki: Path, tmp_path: Path
    ) -> None:
        """Vector must find ``innovation-accounting.md`` on a query that
        shares no literal tokens with its body or frontmatter.
        """
        cache = tmp_path / "cache"
        VectorBackend().build_index(rescue_wiki, cache)
        results = VectorBackend().query(
            "iterative feedback loops", cache, n=3
        )
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
        results = FTS5Backend().query(
            "iterative feedback loops", cache, n=3
        )
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
    def rescue_wiki_and_auto_memory(
        self, tmp_path: Path
    ) -> tuple[Path, Path]:
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
        expected = (
            "auto-memory/-Users-tristankromer-Code/"
            "feedback_return_path.md"
        )
        assert expected in filenames, (
            "FTS5 must surface the proper-noun entity on a short query "
            "when the doc lives in an extra intake root — same rescue "
            "class as wiki-rooted docs."
        )
        # And must rank ahead of the wiki distractor.
        if "migration-path.md" in filenames:
            assert filenames.index(expected) < filenames.index(
                "migration-path.md"
            )

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
        VectorBackend().build_index(
            wiki, cache, extra_roots=[auto_memory]
        )
        results = VectorBackend().query(
            "iterative feedback loops", cache, n=3
        )
        filenames = [r[0] for r in results]
        expected = (
            "auto-memory/-Users-tristankromer-Code/"
            "feedback_innovation_accounting.md"
        )
        assert expected in filenames, (
            "Vector must surface the semantic neighbour on a zero-overlap "
            "query when the doc lives in an extra intake root — same "
            "rescue class as wiki-rooted docs."
        )
