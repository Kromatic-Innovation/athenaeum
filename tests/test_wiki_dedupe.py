# SPDX-License-Identifier: Apache-2.0
"""Tests for the wiki-page dedup pass (issue athenaeum#290).

Mirrors the stub-embedder convention used by
``tests/test_recurring_claims.py`` / ``tests/test_resolved_semantic_match.py``
— a text->vector dict keyed on exact page body text, never real chromadb, so
the suite is deterministic and dependency-free.
"""

from __future__ import annotations

import json
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


class TestProposeWikiPageMerges:
    def test_duplicate_cluster_produces_one_proposal(
        self, duplicate_topic_wiki: Path
    ) -> None:
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        proposals = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )

        assert len(proposals) == 1
        assert len(proposals[0]["sources"]) == 3
        source_names = {Path(s).name for s in proposals[0]["sources"]}
        assert source_names == {"venture-a.md", "venture-b.md", "venture-c.md"}

        merges_path = duplicate_topic_wiki / "wiki" / "_pending_merges.md"
        assert merges_path.is_file()
        text = merges_path.read_text(encoding="utf-8")
        assert text.count("## [") == 1
        assert "venture-a.md" in text
        assert "venture-b.md" in text
        assert "venture-c.md" in text
        assert "hobby.md" not in text

    def test_second_run_is_idempotent(self, duplicate_topic_wiki: Path) -> None:
        """Load-bearing acceptance criterion: rerun produces zero NEW proposals."""
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        first = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        assert len(first) == 1

        merges_path = duplicate_topic_wiki / "wiki" / "_pending_merges.md"
        text_after_first = merges_path.read_text(encoding="utf-8")

        second = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        assert second == []  # no NEW proposals

        text_after_second = merges_path.read_text(encoding="utf-8")
        assert text_after_second == text_after_first
        assert text_after_second.count("## [") == 1

    def test_dry_run_previews_without_writing(self, duplicate_topic_wiki: Path) -> None:
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        proposals = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed,
            dry_run=True,
        )
        assert len(proposals) == 1
        merges_path = duplicate_topic_wiki / "wiki" / "_pending_merges.md"
        assert not merges_path.exists()

    def test_dry_run_reflects_already_proposed_state(
        self, duplicate_topic_wiki: Path
    ) -> None:
        """A dry-run preview after a real run must report 0, not re-propose
        what a real run would silently skip as already-present (Quine
        review of athenaeum#293) — otherwise the preview lies about what a real
        run would actually do.
        """
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        first = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        assert len(first) == 1

        preview = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed,
            dry_run=True,
        )
        assert preview == []

    def test_resolved_merge_round_trips_full_draft_body(
        self, duplicate_topic_wiki: Path
    ) -> None:
        """The reader/approval path must see the SAME draft a real reviewer
        would approve — not just that a block got appended (Quine review
        of athenaeum#293, which found the multi-source draft's ``## From ...``
        headers were silently truncating the block before the athenaeum#291 fence
        fix). Exercises ``parse_pending_merges``/``list_pending_merges``/
        ``resolve_merge`` end to end against a real wiki_dedupe proposal.
        """
        from athenaeum.models import slugify
        from athenaeum.pending_merges import (
            list_pending_merges,
            parse_pending_merges,
            resolve_merge,
        )
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        proposals = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        assert len(proposals) == 1

        merges_path = duplicate_topic_wiki / "wiki" / "_pending_merges.md"
        pms = parse_pending_merges(merges_path)
        assert len(pms) == 1
        draft = pms[0].draft_merged_body
        assert _BODY_A in draft
        assert _BODY_B in draft
        assert _BODY_C in draft

        listed = list_pending_merges(merges_path)
        assert len(listed) == 1
        assert listed[0]["draft_merged_body"] == draft

        result = resolve_merge(
            merges_path,
            pms[0].id,
            "approve",
            wiki_root=duplicate_topic_wiki / "wiki",
        )
        assert result["ok"] is True

        target_path = (
            duplicate_topic_wiki
            / "wiki"
            / f"{slugify(pms[0].merge_target_name)}.md"
        )
        assert target_path.is_file()
        written = target_path.read_text(encoding="utf-8")
        assert written  # not silently emptied
        assert _BODY_A in written
        assert _BODY_B in written
        assert _BODY_C in written

    def test_unrelated_page_not_included(self, duplicate_topic_wiki: Path) -> None:
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        proposals = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        all_sources = {Path(s).name for p in proposals for s in p["sources"]}
        assert "hobby.md" not in all_sources

    def test_no_wiki_root_returns_empty(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        proposals = propose_wiki_page_merges(tmp_path, config={}, threshold=0.8)
        assert proposals == []

    def test_fewer_than_two_candidates_short_circuits_before_embedding(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "solo.md", body="solo")

        def _boom(texts: list[str]):  # must not be called
            raise AssertionError("embedder should not be invoked for <2 candidates")

        proposals = propose_wiki_page_merges(
            tmp_path, config={}, threshold=0.8, embedding_provider=_boom
        )
        assert proposals == []


# --- Issue athenaeum#478: degenerate-over-cluster suppression on the wiki-dedupe path ---
#
# The athenaeum#400/#421 gates (``max_merge_sources`` default 5, ``min_merge_mean_similarity``
# default 0.6 — both active out of the box) were only wired into ``merge.py``'s
# resolver write path, NOT ``propose_wiki_page_merges``. Because this pass uses the
# SAME single-linkage clusterer, one weak bridging edge could chain hundreds/
# thousands of pages into a giant component (the live 1,711-/1,746-source
# ``merge-workflow-pattern`` proposals) that was written straight to
# ``_pending_merges.md``, bypassing the gates entirely. These tests exercise the
# suppression gate through the REAL wiki-dedupe call path — the gap
# ``test_merge_proposal_gates.py`` (which calls the gate function in isolation)
# could not catch.


def _identical_embed(texts: list[str]) -> list[list[float]]:
    """Every page embeds to the same unit vector → one cohesive cluster of all pages.

    Mean/min pairwise cosine are both 1.0, so the ONLY gate arm that can fire is
    the ``max_merge_sources`` size cap — isolating it from the cohesion arms.
    """
    return [[1.0, 0.0] for _ in texts]


class TestSuppressionGates:
    """Issue athenaeum#478: the athenaeum#400/#421 suppression gates apply on wiki-dedupe."""

    def _seed_cohesive_cluster(self, tmp_path: Path, n: int) -> Path:
        wiki_root = tmp_path / "knowledge" / "wiki"
        for i in range(n):
            _write_page(
                wiki_root,
                f"dup-{i}.md",
                body=f"Cohesive duplicate-topic wiki page number {i}.",
            )
        return wiki_root.parent  # knowledge_root

    def test_over_cluster_suppressed_not_written(self, tmp_path: Path) -> None:
        """n_sources (6) > max_merge_sources (5, default) → nothing written.

        The exact regression the issue asks for: a cluster over the default size
        cap, fed through the path that produced the live degenerate entries,
        must reach ``_pending_merges.md`` as ZERO blocks.
        """
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        knowledge_root = self._seed_cohesive_cluster(tmp_path, n=6)
        proposals = propose_wiki_page_merges(
            knowledge_root,
            config={},  # defaults: max_merge_sources=5, min_merge_mean_similarity=0.6
            threshold=0.8,
            embedding_provider=_identical_embed,
        )
        assert proposals == []
        merges_path = knowledge_root / "wiki" / "_pending_merges.md"
        # Either the sidecar was never created, or it exists with no merge block.
        if merges_path.is_file():
            assert "## [" not in merges_path.read_text(encoding="utf-8")

    def test_over_cluster_suppressed_states_chromadb_default_embedder(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Issue athenaeum#1032: the SUPPRESSED log line names which embedder
        produced the suppressed cluster's vectors — chromadb-default when
        real (stub) vectors were used."""
        import logging

        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        knowledge_root = self._seed_cohesive_cluster(tmp_path, n=6)
        caplog.set_level(logging.INFO, logger="athenaeum.wiki_dedupe")
        proposals = propose_wiki_page_merges(
            knowledge_root,
            config={},  # defaults: max_merge_sources=5
            threshold=0.8,
            embedding_provider=_identical_embed,
        )
        assert proposals == []
        suppressed = [r for r in caplog.records if "SUPPRESSED" in r.getMessage()]
        assert suppressed
        assert all("embedder=chromadb-default" in r.getMessage() for r in suppressed)

    def test_over_cluster_suppressed_states_fallback_hashing_embedder(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Issue athenaeum#1032: same gate, but the embedding provider returns
        ``None`` (chromadb absent / embedding call failed), so the pass
        falls back to the hashing-trick embedder. The SUPPRESSED line must
        name THAT embedder, not chromadb-default, since it produced the
        vectors that actually drove the suppression decision.

        Bodies are near-identical (differing only in the filename-derived
        ``name``/stem token — file paths must be distinct) so the
        hashing-trick vectors land at a uniform ~0.667 (2/3) pairwise
        cosine across all 15 pairs — a lower ``threshold`` (0.6, vs. 0.8
        for the real-vector variant of this test) reliably clusters all 6
        into one clique without depending on the real embedder this test
        deliberately disables. Issue athenaeum#1050: ``_fallback_embeddings`` now
        hashes tokens with ``hashlib.sha256`` instead of the
        PYTHONHASHSEED-salted builtin ``hash()``, so this 0.667 figure is a
        fixed, measured constant (verified stable across
        ``PYTHONHASHSEED`` in this repo's CI) rather than a per-process
        range — pre-athenaeum#1050 this same fixture landed anywhere in
        ~0.62-0.67 depending on the run's random hash seed, which is
        exactly what made this test flaky (CI run 32379062624).
        """
        import logging

        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "knowledge" / "wiki"
        for i in range(6):
            _write_page(
                wiki_root,
                f"dup-{i}.md",
                body="Identical cohesive duplicate content for the fallback-hashing test.",
            )
        knowledge_root = wiki_root.parent

        def _no_vectors(texts: list[str]) -> list[list[float]] | None:
            return None

        caplog.set_level(logging.INFO, logger="athenaeum.wiki_dedupe")
        proposals = propose_wiki_page_merges(
            knowledge_root,
            config={},  # defaults: max_merge_sources=5
            threshold=0.6,
            embedding_provider=_no_vectors,
        )
        assert proposals == []
        suppressed = [r for r in caplog.records if "SUPPRESSED" in r.getMessage()]
        assert suppressed
        assert all("embedder=fallback-hashing" in r.getMessage() for r in suppressed)

    def test_over_cluster_suppressed_in_dry_run(self, tmp_path: Path) -> None:
        """dry-run reflects the gated real run — the over-cluster is not previewed."""
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        knowledge_root = self._seed_cohesive_cluster(tmp_path, n=6)
        proposals = propose_wiki_page_merges(
            knowledge_root,
            config={},
            threshold=0.8,
            embedding_provider=_identical_embed,
            dry_run=True,
        )
        assert proposals == []

    def test_raising_max_merge_sources_admits_the_cluster(self, tmp_path: Path) -> None:
        """The gate is config-driven: a higher cap admits the same 6-page cluster,
        proving the suppression is the size cap firing (not clustering collapsing
        the group) and that the wiki-dedupe path honors ``librarian`` config."""
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        knowledge_root = self._seed_cohesive_cluster(tmp_path, n=6)
        proposals = propose_wiki_page_merges(
            knowledge_root,
            config={"librarian": {"max_merge_sources": 10}},
            threshold=0.8,
            embedding_provider=_identical_embed,
        )
        assert len(proposals) == 1
        assert len(proposals[0]["sources"]) == 6

    def test_low_mean_cohesion_suppressed_via_config(
        self, duplicate_topic_wiki: Path
    ) -> None:
        """The mean-cohesion arm is wired too: a floor above the cluster's mean
        pairwise cohesion (~0.97 for the 3 venture pages) suppresses an
        otherwise size-legal 3-page cluster on the wiki-dedupe path."""
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        proposals = propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={"librarian": {"min_merge_mean_similarity": 0.99}},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        assert proposals == []


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

    def test_no_legitimate_pair_lost_and_no_over_cluster_suppression(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 7-page chain that WOULD have been one 7-source single-linkage
        component (over the default ``max_merge_sources=5``, and thus
        wholesale suppressed with no record of the legitimate pairs inside
        it — the loss mode issue athenaeum#681's AC3 was written to detect on
        the raw-source clusterer) must instead reach ``_pending_merges.md``
        as legitimate small proposals. The over-cluster gate never fires,
        because formation now guarantees the property it used to enforce
        after the fact.
        """
        import logging

        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "knowledge" / "wiki"
        bodies = _chain_bodies(7)
        for i, body in enumerate(bodies):
            _write_page(wiki_root, f"chain-{i}.md", body=body)
        knowledge_root = wiki_root.parent

        caplog.set_level(logging.INFO, logger="athenaeum.wiki_dedupe")
        proposals = propose_wiki_page_merges(
            knowledge_root,
            config={},  # defaults: max_merge_sources=5, min_merge_mean_similarity=0.6
            threshold=0.9,
            embedding_provider=_circle_provider(bodies),
        )

        assert proposals, "legitimate near-duplicate pairs must not be dropped"
        for proposal in proposals:
            assert len(proposal["sources"]) <= 5

        suppressed = [r for r in caplog.records if "SUPPRESSED" in r.getMessage()]
        assert not suppressed, (
            "over-cluster suppression should not fire once formation is "
            f"complete-linkage: {[r.getMessage() for r in suppressed]}"
        )

        merges_path = wiki_root / "_pending_merges.md"
        assert merges_path.is_file()
        assert "## [" in merges_path.read_text(encoding="utf-8")

        # No member of a legitimate small cluster is dropped: every proposed
        # source is one of the original chain pages, and no proposal folds
        # in an out-of-chain source.
        all_sources = {Path(s).name for p in proposals for s in p["sources"]}
        assert all_sources <= {f"chain-{i}.md" for i in range(7)}


# --- Issue athenaeum#1142: durable embedder + suppression attribution ---
#
# athenaeum#1032 stamped the embedder onto suppression LOG LINES and the raw-intake
# clusters JSONL. It never reached ``wiki/_pending_merges.md`` (a written
# proposal carried no embedder field) or any durable record of a SUPPRESSED
# wiki-dedupe cluster (suppression was a log.info call only) -- so a
# suppressed cluster could never answer "which embedder, and why?" without
# a live host log read. These tests exercise the two surfaces this issue
# closes: the proposal block itself (AC1), and a new sidecar ledger for
# suppressions (AC2), plus the AC3 non-constant-field guard and the AC4
# bounded-retention behavior.


class TestEmbedderAttributionOnProposals:
    """AC1: a written proposal carries the embedder that produced its cluster."""

    def test_proposal_block_carries_chromadb_default_embedder(
        self, duplicate_topic_wiki: Path
    ) -> None:
        from athenaeum.pending_merges import parse_pending_merges
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        merges_path = duplicate_topic_wiki / "wiki" / "_pending_merges.md"
        pm = parse_pending_merges(merges_path)[0]
        assert pm.embedder == "chromadb-default"
        assert "**Embedder**: chromadb-default" in merges_path.read_text(
            encoding="utf-8"
        )

    def test_proposal_block_carries_fallback_hashing_embedder(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.pending_merges import parse_pending_merges
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        wiki_root = tmp_path / "knowledge" / "wiki"
        for i in range(3):
            _write_page(
                wiki_root,
                f"dup-{i}.md",
                body=(
                    "Identical cohesive duplicate content for the "
                    "fallback-hashing embedder-attribution test."
                ),
            )
        knowledge_root = wiki_root.parent

        def _no_vectors(texts: list[str]) -> list[list[float]] | None:
            return None

        proposals = propose_wiki_page_merges(
            knowledge_root,
            config={},  # 3 sources, well under max_merge_sources=5 -- not suppressed
            threshold=0.6,
            embedding_provider=_no_vectors,
        )
        assert len(proposals) == 1
        merges_path = wiki_root / "_pending_merges.md"
        pm = parse_pending_merges(merges_path)[0]
        assert pm.embedder == "fallback-hashing"
        assert "**Embedder**: fallback-hashing" in merges_path.read_text(
            encoding="utf-8"
        )

    def test_raw_intake_write_path_unaffected_no_embedder_line(
        self, tmp_path: Path
    ) -> None:
        """Blast-radius guard: ``write_pending_merge`` called without
        ``embedder=`` (both of ``merge.py``'s raw-intake call sites, neither
        modified by athenaeum#1142) renders byte-identical to before this
        field existed -- no ``**Embedder**:`` line, empty string on parse."""
        from athenaeum.pending_merges import parse_pending_merges, write_pending_merge

        merges_path = tmp_path / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="some-topic",
            sources=[str(tmp_path / "a.md"), str(tmp_path / "b.md")],
            rationale="test",
            draft_merged_body="body",
            confidence=0.9,
        )
        text = merges_path.read_text(encoding="utf-8")
        assert "**Embedder**" not in text
        pm = parse_pending_merges(merges_path)[0]
        assert pm.embedder == ""


class TestSuppressionLedger:
    """AC2/AC4: suppressed clusters are recorded durably, with reason and
    embedder, in a bounded (never unbounded-append) sidecar ledger."""

    def _seed_cohesive_cluster(self, tmp_path: Path, n: int) -> Path:
        wiki_root = tmp_path / "knowledge" / "wiki"
        for i in range(n):
            _write_page(
                wiki_root,
                f"dup-{i}.md",
                body=f"Cohesive duplicate-topic wiki page number {i}.",
            )
        return wiki_root.parent

    def test_suppressed_cluster_recorded_with_reason_and_embedder(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.wiki_dedupe import (
            DEFAULT_WIKI_SUPPRESSIONS_FILENAME,
            propose_wiki_page_merges,
        )

        knowledge_root = self._seed_cohesive_cluster(tmp_path, n=6)
        proposals = propose_wiki_page_merges(
            knowledge_root,
            config={},  # default max_merge_sources=5 < 6 sources -> suppressed
            threshold=0.8,
            embedding_provider=_identical_embed,
        )
        assert proposals == []

        ledger_path = knowledge_root / "wiki" / DEFAULT_WIKI_SUPPRESSIONS_FILENAME
        assert ledger_path.is_file()
        rows = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 1
        row = rows[0]
        # AC5: this one row answers both "which embedder" and "why
        # suppressed" with no live host read.
        assert row["n_sources"] == 6
        assert "over-cluster" in row["reason"]
        assert row["embedder"] == "chromadb-default"
        assert row["cluster_threshold"] == 0.8
        assert len(row["sources"]) == 6
        assert row["suppressed_at"]  # non-empty ISO timestamp

    def test_ledger_written_even_with_zero_suppressions(
        self, duplicate_topic_wiki: Path
    ) -> None:
        """Written every real run, even to empty -- so the canonical file
        always reflects THIS run's state, never a stale prior one."""
        from athenaeum.wiki_dedupe import (
            DEFAULT_WIKI_SUPPRESSIONS_FILENAME,
            propose_wiki_page_merges,
        )

        propose_wiki_page_merges(
            duplicate_topic_wiki,
            config={},
            threshold=0.8,
            embedding_provider=_fake_embed,
        )
        ledger_path = duplicate_topic_wiki / "wiki" / DEFAULT_WIKI_SUPPRESSIONS_FILENAME
        assert ledger_path.is_file()
        assert ledger_path.read_text(encoding="utf-8") == ""

    def test_dry_run_never_writes_the_ledger(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import (
            DEFAULT_WIKI_SUPPRESSIONS_FILENAME,
            propose_wiki_page_merges,
        )

        knowledge_root = self._seed_cohesive_cluster(tmp_path, n=6)
        propose_wiki_page_merges(
            knowledge_root,
            config={},
            threshold=0.8,
            embedding_provider=_identical_embed,
            dry_run=True,
        )
        ledger_path = knowledge_root / "wiki" / DEFAULT_WIKI_SUPPRESSIONS_FILENAME
        assert not ledger_path.exists()

    def test_canonical_file_is_replaced_not_appended_across_runs(
        self, tmp_path: Path
    ) -> None:
        """AC4: a current-run SNAPSHOT, not an accumulating append-only
        ledger -- the exact asymmetry a sibling item (athenaeum#1229) is
        separately fixing for a DIFFERENT ledger that grew unbounded."""
        from athenaeum.wiki_dedupe import (
            DEFAULT_WIKI_SUPPRESSIONS_FILENAME,
            propose_wiki_page_merges,
        )

        knowledge_root = self._seed_cohesive_cluster(tmp_path, n=6)
        propose_wiki_page_merges(
            knowledge_root,
            config={},
            threshold=0.8,
            embedding_provider=_identical_embed,
        )
        ledger_path = knowledge_root / "wiki" / DEFAULT_WIKI_SUPPRESSIONS_FILENAME
        first_rows = ledger_path.read_text(encoding="utf-8").splitlines()
        assert len(first_rows) == 1

        # Same corpus, cap raised so THIS run suppresses nothing.
        propose_wiki_page_merges(
            knowledge_root,
            config={"librarian": {"max_merge_sources": 10}},
            threshold=0.8,
            embedding_provider=_identical_embed,
        )
        second_text = ledger_path.read_text(encoding="utf-8")
        assert second_text == ""  # replaced, not appended to the prior row

    def test_rotation_pruned_to_configured_retention(self, tmp_path: Path) -> None:
        """AC4: rotations follow the SAME ``librarian.rotation_retention``
        policy ``_librarian-clusters.jsonl`` already uses -- reused, not a
        second retention knob. Mirrors
        ``tests/test_librarian_clusters.py::TestPruneClusterRotations``'s
        hand-seeded-timestamp technique rather than depending on real
        wall-clock separation between calls."""
        from athenaeum.wiki_dedupe import (
            DEFAULT_WIKI_SUPPRESSIONS_FILENAME,
            _write_wiki_suppressions_report,
        )

        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        canonical = wiki_root / DEFAULT_WIKI_SUPPRESSIONS_FILENAME
        stem = canonical.stem
        for stamp in ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"]:
            (wiki_root / f"{stem}-{stamp}.jsonl").write_text("{}\n", encoding="utf-8")

        _write_wiki_suppressions_report(
            [],
            wiki_root,
            knowledge_root=knowledge_root,
            config={"librarian": {"rotation_retention": 2}},
        )
        remaining = sorted(p.name for p in wiki_root.glob(f"{stem}-*.jsonl"))
        # The 3 hand-seeded rotations + the 1 just written = 4 candidates;
        # keep=2 prunes down to the 2 newest (the just-written one is
        # newest by construction -- today's real UTC timestamp).
        assert len(remaining) == 2
        assert canonical.is_file()  # canonical itself never matches the glob


class TestEmbedderAttributionDiffersByRun:
    """AC3: a run using a non-default embedder produces artifacts whose
    attribution DIFFERS from a default-embedder run, on BOTH surfaces this
    issue touches -- so the field cannot silently go constant."""

    @staticmethod
    def _make_identical_pages(root: Path, n: int = 3) -> Path:
        wiki_root = root / "knowledge" / "wiki"
        for i in range(n):
            _write_page(
                wiki_root,
                f"dup-{i}.md",
                body="Shared identical body text for the run-comparison fixture.",
            )
        return wiki_root.parent

    def test_proposal_embedder_field_differs_between_runs(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.pending_merges import parse_pending_merges
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        real_root = self._make_identical_pages(tmp_path / "real")
        propose_wiki_page_merges(
            real_root, config={}, threshold=0.8, embedding_provider=_identical_embed
        )
        real_pm = parse_pending_merges(real_root / "wiki" / "_pending_merges.md")[0]

        fallback_root = self._make_identical_pages(tmp_path / "fallback")
        propose_wiki_page_merges(
            fallback_root,
            config={},
            threshold=0.6,
            embedding_provider=lambda texts: None,
        )
        fallback_pm = parse_pending_merges(
            fallback_root / "wiki" / "_pending_merges.md"
        )[0]

        assert real_pm.embedder == "chromadb-default"
        assert fallback_pm.embedder == "fallback-hashing"
        assert real_pm.embedder != fallback_pm.embedder

    def test_suppression_ledger_embedder_field_differs_between_runs(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.wiki_dedupe import (
            DEFAULT_WIKI_SUPPRESSIONS_FILENAME,
            propose_wiki_page_merges,
        )

        real_root = self._make_identical_pages(tmp_path / "real", n=6)
        propose_wiki_page_merges(
            real_root, config={}, threshold=0.8, embedding_provider=_identical_embed
        )
        real_rows = [
            json.loads(line)
            for line in (real_root / "wiki" / DEFAULT_WIKI_SUPPRESSIONS_FILENAME)
            .read_text(encoding="utf-8")
            .splitlines()
        ]

        fallback_root = self._make_identical_pages(tmp_path / "fallback", n=6)
        propose_wiki_page_merges(
            fallback_root,
            config={},
            # 0.6, not 0.8: the fallback-hashing embedder folds in each
            # page's distinct filename token, so even byte-identical bodies
            # land at ~0.667 pairwise (see TestSuppressionGates's docstring
            # in this file) -- 0.8 would form no cluster at all here.
            threshold=0.6,
            embedding_provider=lambda texts: None,
        )
        fallback_rows = [
            json.loads(line)
            for line in (fallback_root / "wiki" / DEFAULT_WIKI_SUPPRESSIONS_FILENAME)
            .read_text(encoding="utf-8")
            .splitlines()
        ]

        assert real_rows[0]["embedder"] == "chromadb-default"
        assert fallback_rows[0]["embedder"] == "fallback-hashing"
        assert real_rows[0]["embedder"] != fallback_rows[0]["embedder"]
# --- Issue athenaeum#1140: chunk-and-mean-pool fixes the truncation collapse ---
#
# Root cause (full measurement trail in the issue): chromadb's default ONNX
# MiniLM embedding function hard-codes a 256-token truncation window. Content
# past that window is invisible to the embedder. This corpus's wiki pages
# open with a structurally uniform lede (``H1 | blank | para | H2``), so two
# pages sharing that lede but diverging completely in their body collapse to
# a near-identical vector once both are truncated to "lede only" — the
# defect this AC targets directly.
#
# The fixture below models that failure mode explicitly rather than
# hand-waving it: LEDE is long enough (930 raw chars) that it alone exceeds
# ``_CHUNK_CHARS`` (900) — i.e. long enough to fill an entire truncation
# window by itself, exactly like the corpus's real ledes with their H1 +
# intro paragraph. BODY_A and BODY_B are two substantively different,
# same-length continuations. A pre-athenaeum#1140 whole-page embed would see
# only the shared lede for both pages (truncated at the SAME point,
# independent of which body follows) and report them as identical; the
# chunk-and-mean-pool representation embeds every chunk — including the
# chunks made up entirely of body content the old representation never
# reached — so the differing bodies pull the pooled vectors apart.


_SHARED_LEDE = (
    "Merge Workflow Standard Operating Procedure\n\n"
    "This page documents the standard operating procedure every reviewer follows "
    "before approving a proposed wiki merge in this knowledge base, independent of "
    "which two pages are actually involved in any given proposal. The reviewer "
    "first confirms both source pages are still live and unresolved, then reads "
    "the synthesized draft body end to end checking for any accidental loss of a "
    "distinguishing fact, and only then flips the checkbox to approve or leaves a "
    "rejection note explaining what specifically should be preserved instead of "
    "folded together. This same checklist applies uniformly no matter which two "
    "workspaces happen to be the source of the cluster under review, since the "
    "review procedure itself is workspace-agnostic and was written once for the "
    "whole knowledge base rather than per team, and it has not changed since the "
    "process was first documented two reorganizations ago. "
)

_DIVERGENT_BODY_A = (
    "For the finance workspace specifically, every invoice above the departmental "
    "threshold routes through a three-stage approval chain: the cost-center controller "
    "signs off first, then a CFO delegate reviews the vendor contract terms, and "
    "finally the vendor-master data owner confirms the banking details before the "
    "payment batch is released to the clearing house. Exceptions require a written "
    "waiver from the controller and are logged in the quarterly audit trail alongside "
    "every other manual override issued that quarter. " * 3
)

_DIVERGENT_BODY_B = (
    "For the marketing workspace specifically, every campaign brief above the "
    "quarterly spend threshold routes through a two-stage creative review: the brand "
    "lead checks tone and asset compliance first, and then the paid-media buyer signs "
    "off on the channel mix and flight dates before any spend is committed to a "
    "vendor. Exceptions require a written waiver from the brand lead and are logged "
    "in the campaign register alongside every other late addition made that quarter. " * 3
)

assert len(_SHARED_LEDE) > 900, "fixture assumption: lede alone must exceed _CHUNK_CHARS"

_PAGE_A_CONTENT = _SHARED_LEDE + _DIVERGENT_BODY_A
_PAGE_B_CONTENT = _SHARED_LEDE + _DIVERGENT_BODY_B


def _build_chunk_vector_map(
    chunks_a: list[str], chunks_b: list[str]
) -> dict[str, list[float]]:
    """Map every chunk to one of three orthogonal vectors.

    A chunk shared verbatim between both pages' chunk lists (there is at
    least one — the lede, which is long enough to fill a chunk on its own)
    maps to ``LEDE_VEC``; a chunk unique to page A maps to ``BODY_A_VEC``;
    a chunk unique to page B maps to ``BODY_B_VEC``. Orthogonal vectors
    make the mean-pooled cosine fully deterministic and hand-checkable —
    no hashing noise, no dependence on a particular chromadb version's
    actual semantic geometry.
    """
    shared = set(chunks_a) & set(chunks_b)
    assert shared, (
        "fixture design assumption broken: expected the shared lede to "
        "produce at least one identical chunk between page A and page B"
    )
    vector_map: dict[str, list[float]] = {}
    for chunk in chunks_a:
        vector_map[chunk] = _LEDE_VEC if chunk in shared else _BODY_A_VEC
    for chunk in chunks_b:
        if chunk not in vector_map:
            vector_map[chunk] = _LEDE_VEC if chunk in shared else _BODY_B_VEC
    return vector_map


_LEDE_VEC = [1.0, 0.0, 0.0]
_BODY_A_VEC = [0.0, 1.0, 0.0]
_BODY_B_VEC = [0.0, 0.0, 1.0]


class TestChunkAndMeanPoolFixesTruncationCollapse:
    """Issue athenaeum#1140 AC2: shared-lede/divergent-body pages must NOT reach
    cosine >= 0.55 under the chunk-and-mean-pool representation, even though
    a pre-fix whole-page embed (truncated to the shared lede) would have."""

    def test_old_whole_page_truncation_would_have_collapsed_them(self) -> None:
        """Not a tautology: pin the REGRESSION this AC fixes. A hypothetical
        embedder that only ever sees the first ~900 chars of a page (chromadb's
        real truncation behavior) reports these two substantively different
        pages as byte-identical, because both pages' first 900 characters
        ARE the shared lede."""
        from athenaeum.vecmath import cosine

        assert _PAGE_A_CONTENT[:900] == _PAGE_B_CONTENT[:900]
        truncated_a = _PAGE_A_CONTENT[:900]
        truncated_b = _PAGE_B_CONTENT[:900]
        old_style_vectors = {truncated_a: _LEDE_VEC}  # same key for both
        cos_old = cosine(
            old_style_vectors[truncated_a], old_style_vectors[truncated_b]
        )
        assert cos_old == 1.0

    def test_chunk_and_mean_pool_representation_stays_below_threshold(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.vecmath import cosine as vec_cosine
        from athenaeum.wiki_dedupe import (
            _chunk_page_text,
            _resolve_wiki_embeddings,
            discover_wiki_dedupe_candidates,
        )

        wiki_root = tmp_path / "knowledge" / "wiki"
        path_a = _write_page(wiki_root, "shared-lede-a.md", body=_PAGE_A_CONTENT)
        path_b = _write_page(wiki_root, "shared-lede-b.md", body=_PAGE_B_CONTENT)

        chunks_a = _chunk_page_text(_PAGE_A_CONTENT)
        chunks_b = _chunk_page_text(_PAGE_B_CONTENT)
        assert len(chunks_a) > 1 and len(chunks_b) > 1, (
            "fixture design assumption broken: each page must span multiple "
            "chunks for mean-pooling to have anything to combine"
        )
        vector_map = _build_chunk_vector_map(chunks_a, chunks_b)

        def stub_provider(texts: list[str]) -> list[list[float]]:
            return [vector_map[t] for t in texts]

        files = discover_wiki_dedupe_candidates(wiki_root)
        by_name = {am.path.name: am for am in files}
        embeddings, sources = _resolve_wiki_embeddings(
            files, embedding_provider=stub_provider
        )
        vec_a = embeddings[str(by_name[path_a.name].path)]
        vec_b = embeddings[str(by_name[path_b.name].path)]
        cos_new = vec_cosine(vec_a, vec_b)

        assert cos_new < 0.55, (
            f"chunk-and-mean-pool cosine {cos_new!r} did not clear the "
            "shared-lede/divergent-body pair below the 0.55 formation "
            "threshold — the athenaeum#1140 defect is not fixed"
        )

    def test_pages_do_not_cluster_together(self, tmp_path: Path) -> None:
        """End-to-end confirmation through the real entry point: at the
        production 0.55 threshold, the two pages never land in the same
        cluster once the chunk-and-mean-pool representation is in effect."""
        from athenaeum.wiki_dedupe import _chunk_page_text, find_wiki_page_clusters

        wiki_root = tmp_path / "knowledge" / "wiki"
        _write_page(wiki_root, "shared-lede-a.md", body=_PAGE_A_CONTENT)
        _write_page(wiki_root, "shared-lede-b.md", body=_PAGE_B_CONTENT)

        chunks_a = _chunk_page_text(_PAGE_A_CONTENT)
        chunks_b = _chunk_page_text(_PAGE_B_CONTENT)
        vector_map = _build_chunk_vector_map(chunks_a, chunks_b)

        def stub_provider(texts: list[str]) -> list[list[float]]:
            return [vector_map[t] for t in texts]

        clusters = find_wiki_page_clusters(
            wiki_root, threshold=0.55, embedding_provider=stub_provider
        )
        assert clusters == []
