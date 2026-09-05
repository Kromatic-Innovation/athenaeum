# SPDX-License-Identifier: Apache-2.0
"""Off-corpus indexable storage tests (issue athenaeum#984).

Covers the four acceptance criteria:

* AC1 — indexable mode + federated recall (``TestIndexableModeAndFederatedRecall``).
* AC2 — single-store erasure delete, same-operation (``TestErasureDelete``).
* AC3 — off-corpus ledger shard, never the in-git ledger, including a
  cross-boundary pair (``tests/test_verdicts.py::TestOffCorpusLedgerRouting``,
  a sibling of this file so it can reuse ``tests/test_verdicts.py``'s
  existing erasure-class fixtures).
* AC4 — the purgeable surface + its ``derived`` artifacts are declared
  through the store contract, not a second abstraction
  (``TestArtifactRegistryDeclaresOffCorpus``, plus the outside-git-tree
  refusal test proving the ``versioned``/``purgeable`` distinction is real,
  not just declared).

Also covers two of the three defensive branches named in issue athenaeum#1280
(consolidating an athenaeum#984 follow-up finding) that shipped with zero test
assertions — ``TestBuildOffCorpusIndexVectorFallback`` (the vector-backend
``ImportError`` skip) and ``TestQueryOffCorpusUnknownBackend`` (the
unrecognized-``backend_name`` skip). The third (the misconfigured-off_corpus
fallback on the erasure-class verdict routing path) lives in
``tests/test_verdicts.py::TestOffCorpusLedgerRouting`` alongside its sibling
routing tests. ``TestMergeRankedHits`` below also covers athenaeum#1280's tie-break
invariant test.

Every fixture is a scratch ``tmp_path`` tree — never the operator's live
``~/knowledge`` store (see the issue's "Live-store boundary" section).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import off_corpus
from athenaeum.search import build_fts5_index
from athenaeum.store import ARTIFACT_REGISTRY
from tests.conftest import init_git_repo


def _write_page(path: Path, *, name: str, page_type: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ntype: {page_type}\n---\n{body}\n",
        encoding="utf-8",
    )


def _make_config(off_corpus_dir: Path, *, enabled: bool = True) -> dict:
    return {
        "off_corpus": {
            "enabled": enabled,
            "adapter": "off-corpus-test",
        },
        "storage": {
            "adapters": {
                "off-corpus-test": {
                    "backing_store": "markdown",
                    "surface_root": str(off_corpus_dir),
                    "corpus_policy": {
                        "embedded": False,
                        "recallable": True,
                        "merge_eligible": False,
                    },
                },
            },
            "mapping": {
                "erasure-claim": "off-corpus-test",
            },
        },
    }


@pytest.fixture()
def scratch_knowledge_root(tmp_path: Path) -> Path:
    """A scratch, git-tracked knowledge root with a wiki/ tree — never the
    operator's live store."""
    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "wiki").mkdir(parents=True)
    # A placeholder so the seed commit has something to track — an empty
    # directory alone leaves nothing for `git add -A` to stage.
    (knowledge_root / "wiki" / ".gitkeep").write_text("", encoding="utf-8")
    init_git_repo(knowledge_root)
    return knowledge_root


class TestAdapterAndRootResolution:
    def test_disabled_by_default_is_a_strict_noop(self) -> None:
        assert off_corpus.off_corpus_adapter(None) is None
        assert off_corpus.off_corpus_root(None, Path("/tmp/whatever")) is None
        assert off_corpus.off_corpus_store(None, Path("/tmp/whatever")) is None

    def test_enabled_without_adapter_name_raises(self) -> None:
        with pytest.raises(off_corpus.OffCorpusConfigError, match="adapter"):
            off_corpus.off_corpus_adapter({"off_corpus": {"enabled": True}})

    def test_enabled_with_unknown_adapter_name_raises(self) -> None:
        config = {"off_corpus": {"enabled": True, "adapter": "does-not-exist"}}
        with pytest.raises(off_corpus.OffCorpusConfigError, match="unknown"):
            off_corpus.off_corpus_adapter(config)

    def test_adapter_root_inside_knowledge_root_refuses(
        self, scratch_knowledge_root: Path
    ) -> None:
        """The core §4.4/§8 guarantee: an off-corpus surface INSIDE the git
        working tree cannot be a genuine erasure surface (a wiki-store
        ``snapshot()``'s ``git add -A`` could sweep it into history), so
        this must refuse loudly rather than silently declare ``purgeable``
        for a surface that is not really purgeable."""
        inside = scratch_knowledge_root / "off-corpus-inside"
        config = _make_config(inside)
        with pytest.raises(off_corpus.OffCorpusConfigError, match="OUTSIDE"):
            off_corpus.off_corpus_root(config, scratch_knowledge_root)

    def test_adapter_root_outside_knowledge_root_resolves(
        self, tmp_path: Path, scratch_knowledge_root: Path
    ) -> None:
        outside = tmp_path / "off-corpus-outside"
        config = _make_config(outside)
        root = off_corpus.off_corpus_root(config, scratch_knowledge_root)
        assert root == outside.resolve()

    def test_store_capabilities_are_genuinely_purgeable_and_unversioned(
        self, tmp_path: Path, scratch_knowledge_root: Path
    ) -> None:
        """AC4 / §8.5: the purgeable/versioned distinction comes from the
        SAME Store contract S1/S3 shipped (capabilities.versioned/purgeable),
        not a new abstraction — and it is REAL here (the off-corpus root has
        no ``.git`` of its own), not merely declared."""
        outside = tmp_path / "off-corpus-outside"
        config = _make_config(outside)
        store = off_corpus.off_corpus_store(config, scratch_knowledge_root)
        assert store is not None
        assert store.capabilities.purgeable is True
        assert store.capabilities.versioned is False


class TestArtifactRegistryDeclaresOffCorpus:
    def test_off_corpus_artifacts_are_declared_in_the_r3_catalogue(self) -> None:
        """AC4: the purgeable surface's derived/operational artifacts are
        declared through athenaeum.store.ARTIFACT_REGISTRY — the SAME R3
        catalogue every other store artifact declares through, not a
        second abstraction."""
        names = {entry.name for entry in ARTIFACT_REGISTRY}
        assert "off-corpus-fts5-index-db" in names
        assert "off-corpus-vector-collection" in names
        assert "off-corpus-ledger-shard" in names
        by_name = {entry.name: entry for entry in ARTIFACT_REGISTRY}
        assert by_name["off-corpus-fts5-index-db"].persistence_class == "derived"
        assert by_name["off-corpus-vector-collection"].persistence_class == "derived"
        assert by_name["off-corpus-ledger-shard"].persistence_class == "operational"
        assert by_name["off-corpus-ledger-shard"].operational_scope == "store-durable"


class TestIndexableModeAndFederatedRecall:
    def test_off_corpus_content_is_indexed_and_federated_into_recall(
        self, tmp_path: Path, scratch_knowledge_root: Path
    ) -> None:
        from athenaeum.mcp_server import recall_search

        wiki_root = scratch_knowledge_root / "wiki"
        _write_page(
            wiki_root / "ordinary-topic.md",
            name="Ordinary Topic",
            page_type="feedback",
            body="an ordinary corpus claim",
        )

        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        _write_page(
            off_corpus_dir / "erasure-claim-one.md",
            name="Zephyrwidgets Erasure Claim",
            page_type="erasure-claim",
            body="a very specific off-corpus fact",
        )
        config = _make_config(off_corpus_dir)
        cache_dir = tmp_path / "cache"

        build_fts5_index(wiki_root, cache_dir, config=config)
        counts = off_corpus.build_off_corpus_index(config, scratch_knowledge_root, cache_dir)
        assert counts is not None
        assert counts["fts5"] == 1

        # FTS5Backend indexes name/tags/aliases/description/type, not body
        # text (docs/design/recall-architecture.md) — the distinguishing token
        # lives in ``name:``, matching how every other FTS5-backed test in
        # this suite queries.
        result = recall_search(
            wiki_root,
            "Zephyrwidgets",
            search_backend="fts5",
            cache_dir=cache_dir,
            config=config,
        )
        assert "Zephyrwidgets Erasure Claim" in result
        assert "off-corpus/erasure-claim-one.md" in result

    def test_disabled_off_corpus_never_appears_in_recall(
        self, tmp_path: Path, scratch_knowledge_root: Path
    ) -> None:
        """The default (off_corpus.enabled unset) is a strict no-op —
        federated recall behaves byte-identically to before athenaeum#984."""
        from athenaeum.mcp_server import recall_search

        wiki_root = scratch_knowledge_root / "wiki"
        _write_page(
            wiki_root / "ordinary-topic.md",
            name="Ordinary Topic",
            page_type="feedback",
            body="an ordinary corpus claim",
        )
        cache_dir = tmp_path / "cache"
        build_fts5_index(wiki_root, cache_dir, config=None)

        result = recall_search(
            wiki_root,
            "Ordinary",
            search_backend="fts5",
            cache_dir=cache_dir,
            config=None,
        )
        assert "Ordinary Topic" in result
        assert "off-corpus" not in result


class TestErasureDelete:
    def test_erasure_removes_content_and_index_shard_in_one_operation(
        self, tmp_path: Path, scratch_knowledge_root: Path
    ) -> None:
        """AC2: deleting an off-corpus record removes it from the federated
        recall path in the SAME operation — no separate reindex step for
        the caller to remember."""
        from athenaeum.mcp_server import recall_search

        wiki_root = scratch_knowledge_root / "wiki"
        wiki_root.mkdir(exist_ok=True)

        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        _write_page(
            off_corpus_dir / "erasure-claim-two.md",
            name="Quixoticgadgets Erasure Claim",
            page_type="erasure-claim",
            body="a fact that must be erasable",
        )
        config = _make_config(off_corpus_dir)
        cache_dir = tmp_path / "cache"

        build_fts5_index(wiki_root, cache_dir, config=config)
        off_corpus.build_off_corpus_index(config, scratch_knowledge_root, cache_dir)

        before = recall_search(
            wiki_root,
            "Quixoticgadgets",
            search_backend="fts5",
            cache_dir=cache_dir,
            config=config,
        )
        assert "Quixoticgadgets Erasure Claim" in before

        deleted = off_corpus.erase_off_corpus_record(
            config, scratch_knowledge_root, cache_dir, "erasure-claim-two.md"
        )
        assert deleted is True

        # Content is genuinely gone.
        assert not (off_corpus_dir / "erasure-claim-two.md").exists()

        # Same query as `before` — a real disappearance, not a query that
        # was never going to match. Proves the FTS5 row (not just the file)
        # was pruned by the SAME erase_off_corpus_record call.
        after = recall_search(
            wiki_root,
            "Quixoticgadgets",
            search_backend="fts5",
            cache_dir=cache_dir,
            config=config,
        )
        assert "Quixoticgadgets Erasure Claim" not in after

    def test_erasing_absent_record_returns_false(
        self, tmp_path: Path, scratch_knowledge_root: Path
    ) -> None:
        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        config = _make_config(off_corpus_dir)
        cache_dir = tmp_path / "cache"
        deleted = off_corpus.erase_off_corpus_record(
            config, scratch_knowledge_root, cache_dir, "never-existed.md"
        )
        assert deleted is False

    def test_erasure_without_configuration_raises(self, tmp_path: Path) -> None:
        with pytest.raises(off_corpus.OffCorpusConfigError):
            off_corpus.erase_off_corpus_record(None, tmp_path, tmp_path / "cache", "x.md")


class TestBuildOffCorpusIndexVectorFallback:
    """issue athenaeum#1280 finding A: ``build_off_corpus_index``'s ``except
    ImportError:`` around the vector-index build (the ``athenaeum[vector]``
    extra not installed) shipped with zero assertions. Simulates the
    condition with a monkeypatch (the extra genuinely IS installed in this
    dev venv, so this cannot be reached by omitting it) rather than treating
    it as untestable."""

    def test_vector_backend_import_error_skips_vector_shard_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        scratch_knowledge_root: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        import athenaeum.search as search_mod

        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        _write_page(
            off_corpus_dir / "erasure-claim-one.md",
            name="Zephyrwidgets Erasure Claim",
            page_type="erasure-claim",
            body="a fact",
        )
        config = _make_config(off_corpus_dir)
        cache_dir = tmp_path / "cache"

        def _raise_import_error(*_args: object, **_kwargs: object) -> int:
            raise ImportError("simulated: athenaeum[vector] not installed")

        # build_off_corpus_index does ``from athenaeum.search import
        # build_fts5_index, build_vector_index`` at CALL time (function-local
        # import) -- patching the source attribute is what a deferred import
        # picks up.
        monkeypatch.setattr(search_mod, "build_vector_index", _raise_import_error)

        with caplog.at_level(logging.INFO, logger="athenaeum.off_corpus"):
            counts = off_corpus.build_off_corpus_index(
                config, scratch_knowledge_root, cache_dir
            )

        # Never raises, never returns None (that would mean "disabled",
        # which this is not) -- fts5 shard still built for real.
        assert counts is not None
        assert counts["fts5"] == 1
        assert "vector" not in counts
        assert any(
            "vector backend unavailable" in rec.message for rec in caplog.records
        ), [rec.message for rec in caplog.records]

        # The fts5 shard is a REAL artifact on disk, not just a count --
        # proves the ImportError only skipped the vector half.
        oc_cache = off_corpus.off_corpus_cache_dir(cache_dir)
        assert oc_cache.exists()


class TestQueryOffCorpusUnknownBackend:
    """issue athenaeum#1280 finding A: ``query_off_corpus``'s ``except
    KeyError:`` around ``get_backend(backend_name)`` (an unrecognized
    search-backend name) shipped with zero assertions."""

    def test_unknown_backend_name_degrades_to_empty_hits_not_a_crash(
        self, tmp_path: Path, scratch_knowledge_root: Path
    ) -> None:
        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        _write_page(
            off_corpus_dir / "erasure-claim-one.md",
            name="Zephyrwidgets Erasure Claim",
            page_type="erasure-claim",
            body="a fact",
        )
        config = _make_config(off_corpus_dir)
        cache_dir = tmp_path / "cache"
        off_corpus.build_off_corpus_index(config, scratch_knowledge_root, cache_dir)

        result = off_corpus.query_off_corpus(
            config,
            scratch_knowledge_root,
            cache_dir,
            "Zephyrwidgets",
            backend_name="not-a-real-backend",
            top_k=5,
        )

        # None means "off-corpus disabled" -- this is NOT that; the
        # subsystem was reached (root resolved) and degraded to empty hits.
        assert result is not None
        hits, root = result
        assert hits == []
        assert root == off_corpus.off_corpus_root(config, scratch_knowledge_root)

    def test_recognized_backend_still_returns_real_hits(
        self, tmp_path: Path, scratch_knowledge_root: Path
    ) -> None:
        """Control: proves the unknown-backend test above is exercising a
        real degrade path, not a fixture that would return empty hits
        regardless of ``backend_name``."""
        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        _write_page(
            off_corpus_dir / "erasure-claim-one.md",
            name="Zephyrwidgets Erasure Claim",
            page_type="erasure-claim",
            body="a fact",
        )
        config = _make_config(off_corpus_dir)
        cache_dir = tmp_path / "cache"
        off_corpus.build_off_corpus_index(config, scratch_knowledge_root, cache_dir)

        result = off_corpus.query_off_corpus(
            config,
            scratch_knowledge_root,
            cache_dir,
            "Zephyrwidgets",
            backend_name="fts5",
            top_k=5,
        )
        assert result is not None
        hits, _root = result
        assert len(hits) == 1


class TestMergeRankedHits:
    def test_merge_sorts_by_score_and_caps_top_k(self) -> None:
        primary = [("a.md", "A", 5.0), ("b.md", "B", 1.0)]
        off = [("c.md", "C", 9.0), ("d.md", "D", 0.5)]
        merged = off_corpus.merge_ranked_hits(primary, off, top_k=3)
        assert [h[0] for h in merged] == ["c.md", "a.md", "b.md"]

    def test_neither_side_is_dropped_wholesale(self) -> None:
        """Judgement call: 'neither index silently dominating' — a
        higher-scored off-corpus hit outranks a lower-scored corpus hit,
        proving the merge is score-based, not corpus-first-always."""
        primary = [("a.md", "A", 1.0)]
        off = [("b.md", "B", 2.0)]
        merged = off_corpus.merge_ranked_hits(primary, off, top_k=2)
        assert merged[0][0] == "b.md"

    def test_exact_score_tie_keeps_primary_before_off_corpus(self) -> None:
        """issue athenaeum#1280 finding A: the docstring's tie-break invariant —
        'A stable sort keeps primary before off_corpus_hits on an exact
        score tie' — had no test. Every pair here ties exactly, so the only
        thing that can determine order is the tie-break rule itself."""
        primary = [("a.md", "A", 5.0), ("b.md", "B", 5.0)]
        off = [("c.md", "C", 5.0), ("d.md", "D", 5.0)]
        merged = off_corpus.merge_ranked_hits(primary, off, top_k=10)
        assert [h[0] for h in merged] == ["a.md", "b.md", "c.md", "d.md"]

    def test_tie_break_only_breaks_ties_not_general_order(self) -> None:
        """The tie-break must not override real score ordering — it only
        decides among EQUAL scores. A higher-scored off-corpus hit still
        outranks a tied-lower primary/off-corpus pair, and a lower-scored
        primary hit still sorts after the tie."""
        primary = [("a.md", "A", 5.0), ("b.md", "B", 1.0)]
        off = [("c.md", "C", 5.0), ("d.md", "D", 9.0)]
        merged = off_corpus.merge_ranked_hits(primary, off, top_k=10)
        assert [h[0] for h in merged] == ["d.md", "a.md", "c.md", "b.md"]

    def test_tie_break_is_deterministic_across_repeated_calls(self) -> None:
        """A tie-break invariant only holds anything up if it is
        deterministic: the SAME inputs must produce the SAME order every
        time, not merely 'a stable-looking order this run'."""
        primary = [("a.md", "A", 3.0), ("b.md", "B", 3.0), ("e.md", "E", 3.0)]
        off = [("c.md", "C", 3.0), ("d.md", "D", 3.0)]
        results = [
            off_corpus.merge_ranked_hits(primary, off, top_k=10) for _ in range(25)
        ]
        assert all(r == results[0] for r in results)
        assert [h[0] for h in results[0]] == ["a.md", "b.md", "e.md", "c.md", "d.md"]
