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
        # text (docs/recall-architecture.md) — the distinguishing token
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
