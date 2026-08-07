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
