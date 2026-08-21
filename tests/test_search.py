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

    def test_build_index_skips_memory_md_in_wiki_root(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        """A ``MEMORY.md`` directly in the wiki root (not just under an extra
        intake root — see ``TestFTS5ExtraRoots::test_memory_index_excluded``)
        is also excluded, same skip-listed-name rule."""
        (wiki_with_pages / "MEMORY.md").write_text("# MEMORY INDEX\n")
        cache = tmp_path / "cache"
        count = FTS5Backend().build_index(wiki_with_pages, cache)
        assert count == 3  # unchanged: MEMORY.md never entered the index

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

        Reproduces the scenario from issue athenaeum#32 where chromadb's SQLite and
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


class TestHitsFromQueryResults:
    """athenaeum#489 AC3/AC4: hardened parsing of a chromadb query result — no crash on
    a None metadata, and a degenerate flat-score set surfaces explicitly.
    Pure/deterministic, so no chromadb is needed."""

    def test_none_metadata_entry_does_not_crash(self) -> None:
        # AC4: a stale/corrupt collection can return a None metadata entry;
        # `'NoneType' object has no attribute 'get'` must NOT reach the caller.
        from athenaeum.search import _hits_from_query_results

        results = {
            "ids": [["a.md", "b.md"]],
            "metadatas": [[None, {"name": "Bee"}]],
            "distances": [[0.1, 0.9]],
        }
        hits = _hits_from_query_results(results, n=5, caller_audience=None)
        assert hits == [("a.md", "a", 0.1), ("b.md", "Bee", 0.9)]

    def test_none_metadatas_list_does_not_crash(self) -> None:
        from athenaeum.search import _hits_from_query_results

        results = {
            "ids": [["a.md"]],
            "metadatas": None,
            "distances": [[0.2]],
        }
        hits = _hits_from_query_results(results, n=5, caller_audience=None)
        assert hits == [("a.md", "a", 0.2)]

    def test_flat_scores_raise_degraded(self) -> None:
        # AC3: six unrelated results all at an identical distance is the
        # degenerate fallback (the pre-reindex `score: 1.5` failure mode).
        from athenaeum.search import DegradedIndexError, _hits_from_query_results

        results = {
            "ids": [[f"c{i}.md" for i in range(6)]],
            "metadatas": [[{"name": f"C{i}"} for i in range(6)]],
            "distances": [[1.5] * 6],
        }
        with pytest.raises(DegradedIndexError, match="degraded"):
            _hits_from_query_results(results, n=5, caller_audience=None)

    def test_distinct_scores_are_ranked_normally(self) -> None:
        from athenaeum.search import _hits_from_query_results

        results = {
            "ids": [["a.md", "b.md"]],
            "metadatas": [[{"name": "A"}, {"name": "B"}]],
            "distances": [[0.3, 0.7]],
        }
        hits = _hits_from_query_results(results, n=5, caller_audience=None)
        assert [h[0] for h in hits] == ["a.md", "b.md"]

    def test_single_hit_is_not_treated_as_degraded(self) -> None:
        from athenaeum.search import _hits_from_query_results

        results = {
            "ids": [["only.md"]],
            "metadatas": [[{"name": "Only"}]],
            "distances": [[1.5]],
        }
        hits = _hits_from_query_results(results, n=5, caller_audience=None)
        assert hits == [("only.md", "Only", 1.5)]

    def test_empty_results(self) -> None:
        from athenaeum.search import _hits_from_query_results

        assert _hits_from_query_results({"ids": [[]]}, 5, None) == []
        assert _hits_from_query_results({}, 5, None) == []


class TestReindexUnderLiveServer:
    """athenaeum#489: a long-lived process must observe an out-of-process reindex and
    re-open its stale chromadb handle — no restart, no degraded/None results."""

    def test_build_index_writes_generation_stamp(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        from athenaeum.search import _VECTOR_DIR, _read_generation

        cache = tmp_path / "cache"
        VectorBackend().build_index(wiki_with_pages, cache, incremental=False)
        gen = _read_generation(cache / _VECTOR_DIR)
        assert gen  # a non-empty token was stamped for readers to observe

    def test_query_reopens_after_out_of_process_reindex(
        self, tmp_path: Path
    ) -> None:
        # Faithful reproduction of the live incident: the reindex runs in a
        # SEPARATE process, so this ("server") process's chromadb SharedSystem
        # cache stays pinned to the pre-reindex collection. Without the athenaeum#489
        # re-open, the second query serves the stale (deleted) page at the
        # degenerate ~1.5 distance; with it, recall reflects the new corpus.
        import subprocess
        import sys

        cache = tmp_path / "cache"
        wiki = tmp_path / "wiki"
        wiki.mkdir()

        def reindex_in_subprocess() -> None:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import pathlib,sys;"
                    "from athenaeum.search import VectorBackend;"
                    "VectorBackend().build_index("
                    "pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]),"
                    " incremental=False)",
                    str(wiki),
                    str(cache),
                ],
                check=True,
            )

        (wiki / "a.md").write_text(
            "---\nname: Alpha\ntype: concept\n---\napple orchard harvest cider\n"
        )
        reindex_in_subprocess()

        server = VectorBackend()  # long-lived, like the MCP server process
        first = server.query("apple orchard cider", cache, n=3)
        assert first and first[0][0] == "a.md"  # warms this process's cache

        # Out-of-process reindex replaces the corpus entirely.
        (wiki / "a.md").unlink()
        (wiki / "b.md").write_text(
            "---\nname: Beta\ntype: concept\n---\nbanana tropical fruit smoothie\n"
        )
        reindex_in_subprocess()

        # SAME long-lived backend — must reflect the new corpus, not the stale
        # deleted page, and must not crash.
        second = server.query("banana tropical smoothie", cache, n=3)
        assert second and second[0][0] == "b.md"
        assert all(fname != "a.md" for fname, _n, _s in second)


class TestRecallSurfacesDegradedIndex:
    """athenaeum#489 AC3: a DegradedIndexError from the backend surfaces to the recall
    caller as an explicit, actionable message — never as ranked hits."""

    def test_recall_reports_degraded_index(self, tmp_path: Path) -> None:
        import athenaeum.search as search_mod
        from athenaeum.mcp_server import _recall_via_backend
        from athenaeum.search import DegradedIndexError

        class _DegradedBackend:
            def query(self, *a, **k):  # type: ignore[no-untyped-def]
                raise DegradedIndexError("all 6 results at identical distance 1.5")

        original = search_mod.get_backend
        search_mod.get_backend = lambda name: _DegradedBackend()  # type: ignore[assignment]
        try:
            out = _recall_via_backend(
                tmp_path, "spartacus persona", 5, "vector", tmp_path, []
            )
        finally:
            search_mod.get_backend = original

        assert "unavailable" in out.lower()
        assert "reindex" in out.lower()


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
# Incremental indexing (issue athenaeum#348) — whole-file hash diff for both backends
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
        change is caught where a body-only hash would miss it (athenaeum#312).
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
    """Corpus-scoping include/exclude globs (issue athenaeum#348 COULD)."""

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


class TestManifestStatsSchema:
    """Unit coverage for ``_manifest_stats``' opaque-token schema stamp
    (issue athenaeum#977) — direct coverage of the schema-mismatch and
    malformed-row branches that a full ``build_index`` round trip only
    reaches indirectly."""

    def test_none_manifest_returns_empty(self) -> None:
        assert search_module._manifest_stats(None) == {}

    def test_schema_mismatch_returns_empty(self) -> None:
        """A manifest whose top-level ``version`` isn't the current
        opaque-token schema (whether absent, v1, or the old v2 shape) has no
        usable prior stats — this is the "stamp mismatch forces one full
        re-hash" mechanism."""
        assert search_module._manifest_stats({"version": 1, "hashes": {}}) == {}
        assert (
            search_module._manifest_stats(
                {"version": 2, "hashes": {}, "stats": {"a.md": [0, 0, ""]}}
            )
            == {}
        )

    def test_current_schema_missing_stats_key_returns_empty(self) -> None:
        assert (
            search_module._manifest_stats(
                {"version": search_module._STORE_STATS_SCHEMA_VERSION, "hashes": {}}
            )
            == {}
        )

    def test_current_schema_malformed_row_is_skipped(self) -> None:
        """A row that's missing an element never crashes the build — it is
        just treated as having no usable prior stat for that one file."""
        manifest = {
            "version": search_module._STORE_STATS_SCHEMA_VERSION,
            "hashes": {},
            "stats": {"good.md": ["mtimehash:1", "2099-01-01"], "bad.md": ["only-one"]},
        }
        assert search_module._manifest_stats(manifest) == {
            "good.md": ("mtimehash:1", "2099-01-01")
        }


# ---------------------------------------------------------------------------
# Stat pre-filter (issue athenaeum#370) — skip re-reading files whose (mtime,size) match
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
        """Issue athenaeum#977: the manifest's schema stamp is now 3 (the opaque
        store-adapter version token replaces the raw ``(mtime_ns, size)``
        pair — design note §6.2 D3)."""
        import json

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)
        m = json.loads((cache / "fts5-manifest.json").read_text())
        assert m["version"] == search_module._STORE_STATS_SCHEMA_VERSION
        assert set(m["stats"]) == set(m["hashes"])
        # Each stat record is (version, valid_until) — version is FilesystemStore's
        # opaque "mtime_ns:size" token, compared for equality only (never parsed).
        rec = next(iter(m["stats"].values()))
        assert len(rec) == 2
        assert isinstance(rec[0], str) and ":" in rec[0]

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
        and upgrades to the current opaque-token schema on write (athenaeum#977)."""
        import json

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)  # writes the current schema
        mpath = cache / "fts5-manifest.json"
        m = json.loads(mpath.read_text())
        # Downgrade to a pre-athenaeum#370 v1 manifest: drop the stat map entirely.
        mpath.write_text(json.dumps({"version": 1, "hashes": m["hashes"]}))

        hash_spy["n"] = 0
        count = FTS5Backend().build_index(wiki_with_pages, cache)
        # No stats → the fast-path can't fire → every file is read+hashed once.
        assert hash_spy["n"] == 3
        assert count == 3
        # The manifest is upgraded back to the current schema with a full stat map.
        m2 = json.loads(mpath.read_text())
        assert m2["version"] == search_module._STORE_STATS_SCHEMA_VERSION
        assert set(m2["stats"]) == set(m2["hashes"])

    def test_v2_manifest_backcompat_upgrades_to_current_schema(
        self, wiki_with_pages: Path, tmp_path: Path, hash_spy: dict
    ) -> None:
        """Issue athenaeum#977: the OLD v2 manifest shape (raw ``(mtime_ns, size,
        valid_until)`` stats, pre-opaque-token) is ALSO schema-incompatible —
        not just v1 — and forces exactly one full re-hash, same as v1."""
        import json

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)
        mpath = cache / "fts5-manifest.json"
        m = json.loads(mpath.read_text())
        # Downgrade to the pre-athenaeum#977 v2 shape: (mtime_ns, size, valid_until).
        old_stats = {name: [0, 0, ""] for name in m["hashes"]}
        mpath.write_text(json.dumps({"version": 2, "hashes": m["hashes"], "stats": old_stats}))

        hash_spy["n"] = 0
        count = FTS5Backend().build_index(wiki_with_pages, cache)
        assert hash_spy["n"] == 3  # forced full re-hash exactly once
        assert count == 3
        m2 = json.loads(mpath.read_text())
        assert m2["version"] == search_module._STORE_STATS_SCHEMA_VERSION
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
        version, _vu = m["stats"][name]
        m["stats"][name] = [version, past]
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


# ---------------------------------------------------------------------------
# Periodic full re-hash backstop (issue athenaeum#373) — heal the athenaeum#370 stat pre-filter's
# blind spot: a content edit that preserves BOTH (mtime_ns, size).
# ---------------------------------------------------------------------------


def _stat_preserving_edit(path: Path, old: str, new: str) -> None:
    """Replace ``old``->``new`` in ``path`` keeping (mtime_ns, size) identical.

    ``old`` and ``new`` MUST be equal length so the file size is unchanged; the
    original mtime is restored via ``os.utime``. This forges exactly the edit the
    athenaeum#370 stat fast-path cannot see — the manifest's stored ``(mtime_ns, size)``
    still match, so the stored hash would be wrongly reused without a re-hash.
    """
    import os

    assert len(old) == len(new), "edit must preserve byte length"
    st = path.stat()
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
    assert path.stat().st_size == st.st_size  # size preserved
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime
    assert path.stat().st_mtime_ns == st.st_mtime_ns


def _age_manifest_rehash(manifest_path: Path, days: float) -> None:
    """Rewind the manifest's ``last_full_rehash_at`` by ``days`` (issue athenaeum#373)."""
    import json
    import time

    m = json.loads(manifest_path.read_text())
    m["last_full_rehash_at"] = time.time() - days * 86400.0
    manifest_path.write_text(json.dumps(m))


@pytest.fixture
def hash_spy(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Count sha256 invocations — one per file body actually read+hashed."""
    calls = {"n": 0}
    orig = search_module.hashlib.sha256

    def spy(data: bytes = b"") -> object:
        calls["n"] += 1
        return orig(data)

    monkeypatch.setattr(search_module.hashlib, "sha256", spy)
    return calls


class TestFullReHashBackstopFTS5:
    """athenaeum#373: a periodic full re-hash on the incremental path catches a
    stat-preserved content edit that athenaeum#370's fast-path would otherwise miss,
    without falling back to a full FTS5 rebuild."""

    def test_seed_stamps_last_full_rehash_at(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        import json
        import time

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)
        m = json.loads((cache / "fts5-manifest.json").read_text())
        assert "last_full_rehash_at" in m
        assert abs(m["last_full_rehash_at"] - time.time()) < 60

    def test_fresh_uses_fast_path_and_preserves_timestamp(
        self, wiki_with_pages: Path, tmp_path: Path, hash_spy: dict, delta_spy: dict
    ) -> None:
        """A fresh manifest (recent stamp) + unchanged corpus keeps the stat
        fast-path (0 body reads) and does NOT bump last_full_rehash_at."""
        import json

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)
        seed_stamp = json.loads((cache / "fts5-manifest.json").read_text())[
            "last_full_rehash_at"
        ]
        hash_spy["n"] = 0
        FTS5Backend().build_index(wiki_with_pages, cache)
        assert hash_spy["n"] == 0  # fast-path: no body re-hashed
        assert delta_spy["changed"] == []
        # Timestamp preserved, not re-stamped on a non-stale build.
        after = json.loads((cache / "fts5-manifest.json").read_text())[
            "last_full_rehash_at"
        ]
        assert after == seed_stamp

    def test_stale_rehash_catches_stat_preserved_edit(
        self,
        wiki_with_pages: Path,
        tmp_path: Path,
        hash_spy: dict,
        delta_spy: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The key backstop test. An edit that preserves (mtime,size) is invisible
        to the fast-path; an aged manifest forces a re-hash that catches it, and
        only the ONE changed file is re-indexed (incremental delta, not a full
        rebuild)."""
        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)

        # Forge a stat-preserved frontmatter edit (fintech -> medtech, both 7
        # chars). FTS5 indexes the tags, so a caught edit is searchable.
        p = wiki_with_pages / "acme-corp.md"
        _stat_preserving_edit(p, "fintech", "medtech")

        # Sanity: with the fresh manifest the fast-path WOULD miss it.
        pre = FTS5Backend().query("medtech", cache)
        assert "acme-corp.md" not in [r[0] for r in pre]

        # Age the manifest past the 7-day window and rebuild.
        _age_manifest_rehash(cache / "fts5-manifest.json", days=8)
        hash_spy["n"] = 0
        rows_built = {"n": 0}
        orig_row = FTS5Backend._row_for

        def counting_row(name, path, text, meta):  # type: ignore[no-untyped-def]
            rows_built["n"] += 1
            return orig_row(name, path, text, meta)

        monkeypatch.setattr(FTS5Backend, "_row_for", staticmethod(counting_row))
        count = FTS5Backend().build_index(wiki_with_pages, cache)

        # Stale forced a re-hash of EVERY file (3 bodies read+hashed)...
        assert hash_spy["n"] == 3
        # ...but only the one changed file entered the delta and was re-indexed
        # (a full rebuild would have re-inserted all 3 rows).
        assert delta_spy["changed"] == ["acme-corp.md"]
        assert delta_spy["added"] == []
        assert delta_spy["removed"] == []
        assert rows_built["n"] == 1
        assert count == 3
        # The edit is now caught and searchable.
        post = FTS5Backend().query("medtech", cache)
        assert "acme-corp.md" in [r[0] for r in post]

    def test_max_age_zero_always_rehashes(
        self, wiki_with_pages: Path, tmp_path: Path, hash_spy: dict
    ) -> None:
        """full_rehash_max_age_days=0 => never trust the fast-path."""
        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)
        hash_spy["n"] = 0
        # Fresh manifest, unchanged corpus, but max_age 0 forces a full re-hash.
        FTS5Backend().build_index(wiki_with_pages, cache, full_rehash_max_age_days=0)
        assert hash_spy["n"] == 3

    def test_backcompat_no_timestamp_triggers_one_rehash_then_stamps(
        self, wiki_with_pages: Path, tmp_path: Path, hash_spy: dict
    ) -> None:
        """A manifest without last_full_rehash_at (pre-athenaeum#373) is infinitely stale:
        exactly one full re-hash, then it stamps and reverts to the fast-path."""
        import json

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki_with_pages, cache)
        mpath = cache / "fts5-manifest.json"
        m = json.loads(mpath.read_text())
        # Simulate a pre-athenaeum#373 manifest: keep stats, drop the timestamp.
        m.pop("last_full_rehash_at", None)
        mpath.write_text(json.dumps(m))

        hash_spy["n"] = 0
        FTS5Backend().build_index(wiki_with_pages, cache)
        assert hash_spy["n"] == 3  # forced full re-hash exactly once
        m2 = json.loads(mpath.read_text())
        assert "last_full_rehash_at" in m2  # stamped

        # Next build is fresh again → fast-path, zero bodies read.
        hash_spy["n"] = 0
        FTS5Backend().build_index(wiki_with_pages, cache)
        assert hash_spy["n"] == 0


class TestFullReHashBackstopVector:
    """athenaeum#373 for the vector backend: a stale re-hash catches a stat-preserved edit
    and re-embeds ONLY the changed file (no rmtree / full re-embed)."""

    @pytest.fixture(autouse=True)
    def _require_chromadb(self) -> None:
        pytest.importorskip("chromadb")

    def test_seed_stamps_last_full_rehash_at(
        self, wiki_with_pages: Path, tmp_path: Path
    ) -> None:
        import json
        import time

        cache = tmp_path / "cache"
        VectorBackend().build_index(wiki_with_pages, cache)
        m = json.loads((cache / "vector-manifest.json").read_text())
        assert "last_full_rehash_at" in m
        assert abs(m["last_full_rehash_at"] - time.time()) < 60

    def test_fresh_uses_fast_path_and_preserves_timestamp(
        self, wiki_with_pages: Path, tmp_path: Path, hash_spy: dict, delta_spy: dict
    ) -> None:
        import json

        cache = tmp_path / "cache"
        VectorBackend().build_index(wiki_with_pages, cache)
        seed_stamp = json.loads((cache / "vector-manifest.json").read_text())[
            "last_full_rehash_at"
        ]
        hash_spy["n"] = 0
        VectorBackend().build_index(wiki_with_pages, cache)
        assert hash_spy["n"] == 0
        assert delta_spy["changed"] == []
        after = json.loads((cache / "vector-manifest.json").read_text())[
            "last_full_rehash_at"
        ]
        assert after == seed_stamp

    def test_stale_rehash_catches_stat_preserved_edit(
        self,
        wiki_with_pages: Path,
        tmp_path: Path,
        hash_spy: dict,
        delta_spy: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = tmp_path / "cache"
        VectorBackend().build_index(wiki_with_pages, cache)

        # Same-length body edit, mtime restored → invisible to the fast-path.
        p = wiki_with_pages / "acme-corp.md"
        _stat_preserving_edit(p, "fintech", "medtech")

        _age_manifest_rehash(cache / "vector-manifest.json", days=8)
        hash_spy["n"] = 0

        # Spy the embed/add path: a full re-embed would add all 3; the delta adds 1.
        added_records = {"n": 0}
        orig_add = VectorBackend._add_records

        def counting_add(self, collection, records):  # type: ignore[no-untyped-def]
            added_records["n"] += len(records)
            return orig_add(self, collection, records)

        monkeypatch.setattr(VectorBackend, "_add_records", counting_add)

        count = VectorBackend().build_index(wiki_with_pages, cache)
        assert hash_spy["n"] == 3  # stale forced a full re-hash
        assert delta_spy["changed"] == ["acme-corp.md"]
        assert delta_spy["added"] == []
        assert delta_spy["removed"] == []
        assert added_records["n"] == 1  # ONLY the changed file re-embedded
        assert count == 3

    def test_backcompat_no_timestamp_triggers_one_rehash_then_stamps(
        self, wiki_with_pages: Path, tmp_path: Path, hash_spy: dict
    ) -> None:
        import json

        cache = tmp_path / "cache"
        VectorBackend().build_index(wiki_with_pages, cache)
        mpath = cache / "vector-manifest.json"
        m = json.loads(mpath.read_text())
        m.pop("last_full_rehash_at", None)
        mpath.write_text(json.dumps(m))

        hash_spy["n"] = 0
        VectorBackend().build_index(wiki_with_pages, cache)
        assert hash_spy["n"] == 3  # forced full re-hash exactly once
        assert "last_full_rehash_at" in json.loads(mpath.read_text())

        hash_spy["n"] = 0
        VectorBackend().build_index(wiki_with_pages, cache)
        assert hash_spy["n"] == 0  # fresh again → fast-path


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


class TestEmbedTextsFallbackObservability:
    """Issue athenaeum#1032: the coarse embedding fallback used to engage silently
    three layers deep (``_get_ef`` init failure, ``embed_texts`` returning
    ``None``, and — one layer up — ``wiki_dedupe._resolve_wiki_embeddings``
    engaging the hashing-trick fallback, covered in test_wiki_dedupe.py).
    These tests force a failure deterministically via ``sys.modules`` /
    a stub raising EF — never real chromadb — so they pass regardless of
    whether the optional ``[vector]`` extra happens to be installed.
    """

    @pytest.fixture(autouse=True)
    def _reset_ef_memo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ``_get_ef``/``embed_texts`` memo flags are process-global module
        state — reset them before every test in this class so one test's
        memoized failure can't mask another's warning assertion."""
        monkeypatch.setattr(search_module, "_EF", None)
        monkeypatch.setattr(search_module, "_EF_LOADED", False)
        monkeypatch.setattr(search_module, "_EMBED_TEXTS_NONE_WARNED", False)

    def test_get_ef_warns_once_naming_exception_class_and_message(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A forced import failure logs exactly one WARNING naming the
        exception class and message; the pre-existing memoized-failure shape
        (``_EF`` stays ``None``, no repeated stack spam on a second call) is
        unchanged."""
        import logging
        import sys

        monkeypatch.setitem(sys.modules, "chromadb.utils", None)
        caplog.set_level(logging.WARNING, logger="athenaeum.search")

        first = search_module._get_ef()
        assert first is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "ModuleNotFoundError" in msg

        caplog.clear()
        second = search_module._get_ef()
        assert second is None
        assert not caplog.records  # memoized — no repeat warning on the second call

    def test_embed_texts_warns_once_when_no_ef_available(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``embed_texts`` returning ``None`` because no EF is available emits
        its own one-time WARNING (separate flag from ``_get_ef``'s) naming
        the fallback-hashing embedder as what will produce vectors instead."""
        import logging
        import sys

        monkeypatch.setitem(sys.modules, "chromadb.utils", None)
        caplog.set_level(logging.WARNING, logger="athenaeum.search")

        first = search_module.embed_texts(["hello"])
        assert first is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        fallback_hashing = [r for r in warnings if "fallback-hashing" in r.getMessage()]
        assert fallback_hashing, [r.getMessage() for r in warnings]

        caplog.clear()
        second = search_module.embed_texts(["hello"])
        assert second is None
        assert not caplog.records  # both _get_ef's and embed_texts' flags are one-time

    def test_embed_texts_warns_once_when_embedding_call_fails(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A different failure mode: ``_get_ef`` succeeds (a stub EF is
        memoized), but calling it raises. ``embed_texts`` still emits its own
        one-time WARNING naming the exception and the fallback embedder."""
        import logging

        class _BoomEF:
            def __call__(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("boom: embedding call failed")

        monkeypatch.setattr(search_module, "_EF", _BoomEF())
        monkeypatch.setattr(search_module, "_EF_LOADED", True)
        caplog.set_level(logging.WARNING, logger="athenaeum.search")

        first = search_module.embed_texts(["hello"])
        assert first is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "RuntimeError" in msg
        assert "fallback-hashing" in msg

        caplog.clear()
        second = search_module.embed_texts(["hello"])
        assert second is None
        assert not caplog.records  # one-time — no repeat warning
