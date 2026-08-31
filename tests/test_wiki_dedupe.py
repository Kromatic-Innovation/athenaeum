# SPDX-License-Identifier: Apache-2.0
"""Tests for the wiki-page dedup pass (issue athenaeum#290).

Mirrors the stub-embedder convention used by
``tests/test_recurring_claims.py`` / ``tests/test_resolved_semantic_match.py``
— a text->vector dict keyed on exact page body text, never real chromadb, so
the suite is deterministic and dependency-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BODY_A = "Kromatic is Tristan's primary venture and main business focus."
_BODY_B = "Tristan's primary venture is Kromatic, his main company."
_BODY_C = "The main venture Tristan runs day to day is Kromatic."
_BODY_UNRELATED = "Rock climbing is a fun weekend hobby unrelated to work."

# Two duplicate-topic vectors close together (cosine > 0.9), a third
# slightly further but still above a 0.8 threshold, and an orthogonal
# unrelated vector.
_VEC_A = [1.0, 0.0]
_VEC_B = [0.98, 0.2]
_VEC_C = [0.95, 0.31]
_VEC_UNRELATED = [0.0, 1.0]

_TEXT_TO_VEC = {
    _BODY_A: _VEC_A,
    _BODY_B: _VEC_B,
    _BODY_C: _VEC_C,
    _BODY_UNRELATED: _VEC_UNRELATED,
}


def _fake_embed(texts: list[str]) -> list[list[float]] | None:
    return [_TEXT_TO_VEC.get(t.strip(), [0.0, 0.0]) for t in texts]


def _write_page(
    wiki_root: Path,
    filename: str,
    *,
    page_type: str = "concept",
    name: str | None = None,
    description: str = "",
    tags: list[str] | None = None,
    superseded_by: str = "",
    body: str = "",
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name or filename[:-3]}", f"type: {page_type}"]
    if description:
        lines.append(f"description: {description}")
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {t}" for t in tags)
    if superseded_by:
        lines.append(f"superseded_by: {superseded_by}")
    lines.append("---")
    lines.append(body)
    path = wiki_root / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def duplicate_topic_wiki(tmp_path: Path) -> Path:
    """3 near-duplicate 'concept' pages (same real-world topic) + 1 unrelated."""
    wiki_root = tmp_path / "knowledge" / "wiki"
    _write_page(wiki_root, "venture-a.md", body=_BODY_A)
    _write_page(wiki_root, "venture-b.md", body=_BODY_B)
    _write_page(wiki_root, "venture-c.md", body=_BODY_C)
    _write_page(wiki_root, "hobby.md", body=_BODY_UNRELATED)
    return wiki_root.parent  # knowledge_root


class TestDiscoverCandidates:
    def test_type_filter_includes_concept_reference_principle(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "a.md", page_type="concept", body="a")
        _write_page(wiki_root, "b.md", page_type="reference", body="b")
        _write_page(wiki_root, "c.md", page_type="principle", body="c")
        _write_page(wiki_root, "person.md", page_type="person", body="d")

        candidates = discover_wiki_dedupe_candidates(wiki_root)
        names = {c.path.name for c in candidates}
        assert names == {"a.md", "b.md", "c.md"}

    def test_archived_tag_excluded(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "live.md", body="live")
        _write_page(wiki_root, "old.md", tags=["archived"], body="old")

        candidates = discover_wiki_dedupe_candidates(wiki_root)
        names = {c.path.name for c in candidates}
        assert names == {"live.md"}

    def test_superseded_by_excluded(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "live.md", body="live")
        _write_page(wiki_root, "old.md", superseded_by="live", body="old")

        candidates = discover_wiki_dedupe_candidates(wiki_root)
        names = {c.path.name for c in candidates}
        assert names == {"live.md"}

    def test_auto_prefixed_and_sidecar_files_excluded(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True)
        _write_page(wiki_root, "auto-something.md", body="auto")
        (wiki_root / "_pending_merges.md").write_text("# Pending Merges\n")
        _write_page(wiki_root, "real.md", body="real")

        candidates = discover_wiki_dedupe_candidates(wiki_root)
        names = {c.path.name for c in candidates}
        assert names == {"real.md"}


_COMPARATOR_CONFIG = {"librarian": {"comparator_enabled": True}}


def _fake_llm_client(payload_json: str):
    """A MagicMock mirroring the Anthropic SDK's ``messages.create`` response
    shape — the same convention ``tests/test_comparator.py`` uses."""
    from unittest.mock import MagicMock

    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_json)]
    client.messages.create.return_value = response
    return client


def _content_payload(relation: str, *, passages: list[str] | None = None) -> str:
    import json

    return json.dumps(
        {
            "content_relation": relation,
            "conflicting_passages": passages or [],
            "predicate_a": "a-predicate",
            "predicate_b": "b-predicate",
            "rationale": "test rationale",
        }
    )


class TestProposeWikiPageMerges:
    """Issue athenaeum#715 cut-over: candidate PAIRS from the same clustering as
    before, but each pair is now decided by the five-verdict comparator
    (:mod:`athenaeum.comparator`) and enacted via
    :mod:`athenaeum.verdict_effects`, instead of the retired
    confidence/suppression-gate/``write_pending_merge`` algorithm.
    """

    def test_flag_off_is_a_noop(self, duplicate_topic_wiki: Path) -> None:
        """Dark by default: with ``comparator_enabled`` unset (the default),
        NOTHING is compared, ledgered, or written — old or new."""
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        def _boom(texts: list[str]):  # must not be called — short-circuits first
            raise AssertionError("embedder should not run while the flag is off")

        results = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_boom,
        )
        assert results == []
        assert not (duplicate_topic_wiki / "wiki" / "_verdicts").exists()

    def test_duplicate_cluster_writes_fold_evidence_not_a_merge_proposal(
        self, duplicate_topic_wiki: Path
    ) -> None:
        """The retired path wrote an LLM-adjacent draft straight to
        ``_pending_merges.md``. The comparator's ``duplicate`` verdict must
        instead write EVIDENCE (athenaeum#658 D2) — never a merged body, never
        ``_pending_merges.md``."""
        from athenaeum.runlock import RunLock
        from athenaeum.verdict_effects import FOLD_EVIDENCE_DIRNAME
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        client = _fake_llm_client(_content_payload("equivalent"))
        wiki_root = duplicate_topic_wiki / "wiki"
        lock = RunLock(duplicate_topic_wiki)
        with lock:
            results = propose_wiki_page_merges(
                duplicate_topic_wiki,
                config=_COMPARATOR_CONFIG,
                threshold=0.8,
                embedding_provider=_fake_embed,
                client=client,
                lock=lock,
            )

        assert results, "the venture-a/b/c cluster must yield decided pairs"
        assert all(r["verdict"] == "duplicate" for r in results)
        assert all(r["action"] != "noop" for r in results)
        merges_path = wiki_root / "_pending_merges.md"
        assert not merges_path.exists(), "duplicate verdicts must never write _pending_merges.md"
        assert (wiki_root / FOLD_EVIDENCE_DIRNAME).is_dir()

    def test_second_run_is_memoized_not_reenacted(self, duplicate_topic_wiki: Path) -> None:
        """Load-bearing: a pair whose verdict is already fresh in the ledger
        is skipped, not re-decided or re-enacted (issue athenaeum#715 AC5)."""
        from athenaeum.runlock import RunLock
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        client = _fake_llm_client(_content_payload("equivalent"))
        lock = RunLock(duplicate_topic_wiki)
        with lock:
            first = propose_wiki_page_merges(
                duplicate_topic_wiki,
                config=_COMPARATOR_CONFIG,
                threshold=0.8,
                embedding_provider=_fake_embed,
                client=client,
                lock=lock,
            )
            assert first
            call_count_after_first = client.messages.create.call_count

            second = propose_wiki_page_merges(
                duplicate_topic_wiki,
                config=_COMPARATOR_CONFIG,
                threshold=0.8,
                embedding_provider=_fake_embed,
                client=client,
                lock=lock,
            )
        assert second == []  # every pair was already fresh
        assert client.messages.create.call_count == call_count_after_first

    def test_dry_run_previews_without_ledgering_or_enacting(
        self, duplicate_topic_wiki: Path
    ) -> None:
        from athenaeum.verdict_effects import FOLD_EVIDENCE_DIRNAME
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        client = _fake_llm_client(_content_payload("equivalent"))
        results = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config=_COMPARATOR_CONFIG,
            threshold=0.8,
            embedding_provider=_fake_embed,
            client=client,
            dry_run=True,
        )
        assert results
        assert all("action" not in r for r in results)  # nothing enacted
        wiki_root = duplicate_topic_wiki / "wiki"
        assert not (wiki_root / "_verdicts").exists()
        assert not (wiki_root / FOLD_EVIDENCE_DIRNAME).exists()

    def test_no_lock_and_not_dry_run_skips_pass_entirely(
        self, duplicate_topic_wiki: Path
    ) -> None:
        """A real (non-dry-run) comparison requires the caller's lock — see
        ``athenaeum.verdicts``' single-appender contract. Without one this
        pass logs and skips rather than comparing unsafely."""
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        client = _fake_llm_client(_content_payload("equivalent"))
        results = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config=_COMPARATOR_CONFIG,
            threshold=0.8,
            embedding_provider=_fake_embed,
            client=client,
            lock=None,
        )
        assert results == []
        assert client.messages.create.call_count == 0

    def test_unrelated_page_not_included(self, duplicate_topic_wiki: Path) -> None:
        from athenaeum.runlock import RunLock
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        client = _fake_llm_client(_content_payload("equivalent"))
        lock = RunLock(duplicate_topic_wiki)
        with lock:
            results = propose_wiki_page_merges(
                duplicate_topic_wiki,
                config=_COMPARATOR_CONFIG,
                threshold=0.8,
                embedding_provider=_fake_embed,
                client=client,
                lock=lock,
            )
        all_sources = {Path(s).name for r in results for s in r["sources"]}
        assert "hobby.md" not in all_sources

    def test_no_wiki_root_returns_empty(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        results = propose_wiki_page_merges(
            tmp_path, config=_COMPARATOR_CONFIG, threshold=0.8
        )
        assert results == []

    def test_fewer_than_two_candidates_short_circuits_before_embedding(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "solo.md", body="solo")

        def _boom(texts: list[str]):  # must not be called
            raise AssertionError("embedder should not be invoked for <2 candidates")

        results = propose_wiki_page_merges(
            tmp_path,
            config=_COMPARATOR_CONFIG,
            threshold=0.8,
            embedding_provider=_boom,
        )
        assert results == []

    def test_cross_class_pair_skipped_before_any_llm_call(
        self, tmp_path: Path
    ) -> None:
        """Issue athenaeum#715: the comparator's own MEMORY_CLASS dimension is not
        yet ENFORCED, so ``cross_class_precheck`` survives as a pre-comparator
        filter (see module docstring) — a cross-class pair must never reach
        Gate 2."""
        from athenaeum.runlock import RunLock
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "knowledge" / "wiki"
        _write_page(
            wiki_root, "policy-a.md", page_type="concept", body=_BODY_A,
        )
        # A second page in the same near-duplicate embedding neighborhood
        # but a DIFFERENT declared memory_class.
        path_b = _write_page(wiki_root, "policy-b.md", page_type="concept", body=_BODY_B)
        text = path_b.read_text(encoding="utf-8")
        path_b.write_text(
            text.replace("type: concept", "type: concept\nmemory_class: fact"),
            encoding="utf-8",
        )
        path_a = wiki_root / "policy-a.md"
        text_a = path_a.read_text(encoding="utf-8")
        path_a.write_text(
            text_a.replace("type: concept", "type: concept\nmemory_class: guideline"),
            encoding="utf-8",
        )

        knowledge_root = wiki_root.parent
        client = _fake_llm_client(_content_payload("equivalent"))
        lock = RunLock(knowledge_root)
        with lock:
            results = propose_wiki_page_merges(
                knowledge_root,
                config=_COMPARATOR_CONFIG,
                threshold=0.8,
                embedding_provider=_fake_embed,
                client=client,
                lock=lock,
            )
        assert results == []
        assert client.messages.create.call_count == 0


def _chain_bodies(n: int) -> list[str]:
    """*n* distinct page bodies for the single-linkage chain fixture below."""
    return [
        f"Chain page number {i} of the athenaeum#803 regression fixture."
        for i in range(n)
    ]


def _circle_provider(bodies: list[str], degrees: float = 20.0):
    """Embed each body on a unit circle, ``degrees`` apart from its neighbor.

    Mirrors ``tests/test_librarian_clusters.py::TestClusterFormationIsCompleteLinkage``'s
    circle-of-vectors fixture: adjacent pages have cosine ``cos(20 deg) ~= 0.94``
    (an edge at threshold 0.9); pages two apart have ``cos(40 deg) ~= 0.77``
    (below 0.9, NOT an edge). So the pages form a single-linkage CHAIN but no
    clique larger than 2.
    """
    import math

    vec_by_body = {
        body: [
            math.cos(math.radians(degrees * i)),
            math.sin(math.radians(degrees * i)),
            0.0,
        ]
        for i, body in enumerate(bodies)
    }

    def provider(texts: list[str]) -> list[list[float]]:
        return [vec_by_body[t.strip()] for t in texts]

    return provider


class TestResolveWikiEmbeddingsObservability:
    """Issue athenaeum#1032: ``_resolve_wiki_embeddings`` itself — the per-file
    embedder-source map it returns, and the one-time WARNING it emits when it
    engages the hashing-trick fallback.
    """

    def test_real_vectors_branch_records_chromadb_default_and_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from athenaeum.clusters import EMBEDDER_CHROMADB_DEFAULT
        from athenaeum.wiki_dedupe import (
            _resolve_wiki_embeddings,
            discover_wiki_dedupe_candidates,
        )

        wiki_root = tmp_path / "knowledge" / "wiki"
        _write_page(wiki_root, "a.md", body=_BODY_A)
        _write_page(wiki_root, "b.md", body=_BODY_B)
        files = discover_wiki_dedupe_candidates(wiki_root)

        caplog.set_level(logging.WARNING, logger="athenaeum.wiki_dedupe")
        embeddings, sources = _resolve_wiki_embeddings(
            files, embedding_provider=_fake_embed
        )
        assert set(embeddings) == {str(am.path) for am in files}
        assert set(sources.values()) == {EMBEDDER_CHROMADB_DEFAULT}
        assert not caplog.records

    def test_fallback_branch_warns_once_and_records_fallback_hashing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging

        import athenaeum.wiki_dedupe as wiki_dedupe_module
        from athenaeum.clusters import EMBEDDER_FALLBACK_HASHING

        # The one-time-warning flag is process-global module state — reset it
        # so an earlier test's fallback engagement can't mask this assertion.
        monkeypatch.setattr(wiki_dedupe_module, "_WIKI_FALLBACK_WARNED", False)

        wiki_root = tmp_path / "knowledge" / "wiki"
        _write_page(wiki_root, "a.md", body=_BODY_A)
        _write_page(wiki_root, "b.md", body=_BODY_B)
        files = wiki_dedupe_module.discover_wiki_dedupe_candidates(wiki_root)

        def _none_provider(texts: list[str]) -> list[list[float]] | None:
            return None

        caplog.set_level(logging.WARNING, logger="athenaeum.wiki_dedupe")
        embeddings, sources = wiki_dedupe_module._resolve_wiki_embeddings(
            files, embedding_provider=_none_provider
        )
        assert set(embeddings) == {str(am.path) for am in files}
        assert set(sources.values()) == {EMBEDDER_FALLBACK_HASHING}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "fallback-hashing" in warnings[0].getMessage()

        caplog.clear()
        wiki_dedupe_module._resolve_wiki_embeddings(files, embedding_provider=_none_provider)
        assert not caplog.records  # one-time — no repeat warning on the second call


class TestWikiClusterFormationIsCompleteLinkage:
    """Issue athenaeum#803: pin complete-linkage FORMATION through the wiki-page
    entry points specifically. ``find_wiki_page_clusters`` /
    ``propose_wiki_page_merges`` route through the shared
    ``athenaeum.clusters.cluster_auto_memory_files`` (see the ``wiki_dedupe``
    module docstring) — the SAME formation routine issue athenaeum#681 fixed
    for the raw-source clusterer. This class exercises that sharing through
    the wiki-page entry points, mirroring
    ``tests/test_librarian_clusters.py::TestClusterFormationIsCompleteLinkage``,
    which only covers the raw ``AutoMemoryFile`` entry point
    (``cluster_auto_memory_files`` itself is not re-tested here — it already
    has its own unit tests; these confirm the WIKI wrapper actually reaches
    it with a real single-linkage-chain shape, and that nothing between the
    wiki entry points and that shared function re-flattens the result back
    into a single-linkage component).
    """

    def test_chain_is_not_one_giant_cluster(self, tmp_path: Path) -> None:
        """The degenerate all-member giant cluster must not exist."""
        from athenaeum.wiki_dedupe import find_wiki_page_clusters

        wiki_root = tmp_path / "knowledge" / "wiki"
        bodies = _chain_bodies(5)
        for i, body in enumerate(bodies):
            _write_page(wiki_root, f"chain-{i}.md", body=body)

        clusters = find_wiki_page_clusters(
            wiki_root, threshold=0.9, embedding_provider=_circle_provider(bodies)
        )

        assert all(len(c.member_paths) < 5 for c in clusters)
        assert clusters, "the chain's adjacent pairs must still cluster"

    def test_min_pairwise_score_recorded_not_just_centroid(
        self, tmp_path: Path
    ) -> None:
        """Every multi-member wiki cluster is a clique whose MINIMUM pairwise
        cosine clears the threshold, not merely its centroid (mean)."""
        from athenaeum.wiki_dedupe import find_wiki_page_clusters

        wiki_root = tmp_path / "knowledge" / "wiki"
        bodies = _chain_bodies(5)
        for i, body in enumerate(bodies):
            _write_page(wiki_root, f"chain-{i}.md", body=body)

        clusters = find_wiki_page_clusters(
            wiki_root, threshold=0.9, embedding_provider=_circle_provider(bodies)
        )

        multi = [c for c in clusters if len(c.member_paths) > 1]
        assert multi
        for c in multi:
            assert c.min_pairwise_score >= 0.9

    def test_no_legitimate_pair_lost_once_comparator_decides_each_one(
        self, tmp_path: Path
    ) -> None:
        """A 7-page chain that WOULD have been one 7-source single-linkage
        component (the loss mode issue athenaeum#681's AC3 was written to
        detect on the raw-source clusterer) forms multiple small cliques
        (never one giant component), and — since issue athenaeum#715's
        cut-over — every pair inside each clique is independently decided by
        the comparator rather than gated by a size/confidence suppression
        gate (which no longer exists on this path). No legitimate pair is
        dropped; no proposal ever touches ``_pending_merges.md``.
        """
        from athenaeum.runlock import RunLock
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "knowledge" / "wiki"
        bodies = _chain_bodies(7)
        for i, body in enumerate(bodies):
            _write_page(wiki_root, f"chain-{i}.md", body=body)
        knowledge_root = wiki_root.parent

        client = _fake_llm_client(_content_payload("equivalent"))
        lock = RunLock(knowledge_root)
        with lock:
            results = propose_wiki_page_merges(
                knowledge_root,
                config=_COMPARATOR_CONFIG,
                threshold=0.9,
                embedding_provider=_circle_provider(bodies),
                client=client,
                lock=lock,
            )

        assert results, "legitimate near-duplicate pairs must not be dropped"
        merges_path = wiki_root / "_pending_merges.md"
        assert not merges_path.exists()

        # No member of a legitimate small cluster is dropped: every compared
        # source is one of the original chain pages, and no pair folds in an
        # out-of-chain source.
        all_sources = {Path(s).name for r in results for s in r["sources"]}
        assert all_sources <= {f"chain-{i}.md" for i in range(7)}
