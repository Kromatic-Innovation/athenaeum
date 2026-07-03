# SPDX-License-Identifier: Apache-2.0
"""Tests for the wiki-page dedup pass (issue #290).

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
