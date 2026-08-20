# SPDX-License-Identifier: Apache-2.0
"""Tests for the shadow-mode complete-linkage measurement (issue athenaeum#713, artifact 1).

Mirrors the stub-embedder convention :mod:`tests.test_wiki_dedupe` already
uses (a text->vector dict, never real chromadb) so the suite stays
deterministic and dependency-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BODY_A = "Kromatic is Tristan's primary venture and main business focus."
_BODY_B = "Tristan's primary venture is Kromatic, his main company."
_BODY_C = "The main venture Tristan runs day to day is Kromatic."
_BODY_UNRELATED = "Rock climbing is a fun weekend hobby unrelated to work."

# A, B, C mutually clear a 0.55 threshold (complete-linkage clique); UNRELATED
# is orthogonal and stays a singleton either way.
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
    wiki_root: Path, filename: str, *, page_type: str = "concept", body: str = ""
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {filename[:-3]}\ntype: {page_type}\n---\n{body}\n"
    path = wiki_root / filename
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def clique_wiki(tmp_path: Path) -> Path:
    """3 mutually-similar 'concept' pages + 1 unrelated singleton."""
    wiki_root = tmp_path / "knowledge" / "wiki"
    _write_page(wiki_root, "venture-a.md", body=_BODY_A)
    _write_page(wiki_root, "venture-b.md", body=_BODY_B)
    _write_page(wiki_root, "venture-c.md", body=_BODY_C)
    _write_page(wiki_root, "hobby.md", body=_BODY_UNRELATED)
    return wiki_root.parent  # knowledge_root


class TestRunShadowLinkage:
    def test_forms_expected_clique_and_singleton(self, clique_wiki: Path) -> None:
        from athenaeum.shadow_linkage import run_shadow_linkage

        result = run_shadow_linkage(clique_wiki, embedding_provider=_fake_embed)
        assert result.candidate_file_count == 4
        # One 3-member clique + one singleton, under complete linkage.
        assert result.complete_linkage.cluster_count == 2
        assert result.complete_linkage.multi_member_cluster_count == 1
        assert result.complete_linkage.size_distribution == {3: 1, 1: 1}
        # C(3,2) = 3 pairs would reach content-comparison.
        assert result.complete_linkage.comparator_pair_count == 3

    def test_empty_wiki_is_a_trivial_zero_result(self, tmp_path: Path) -> None:
        from athenaeum.shadow_linkage import run_shadow_linkage

        result = run_shadow_linkage(tmp_path / "empty-knowledge", embedding_provider=_fake_embed)
        assert result.candidate_file_count == 0
        assert result.complete_linkage.cluster_count == 0
        assert result.single_linkage.cluster_count == 0

    def test_result_carries_snapshot_identity(self, clique_wiki: Path) -> None:
        from athenaeum.shadow_linkage import run_shadow_linkage

        result = run_shadow_linkage(clique_wiki, embedding_provider=_fake_embed)
        assert result.corpus_digest  # non-empty
        assert result.athenaeum_version
        assert result.git_sha
        assert result.generated.endswith("Z")

    def test_read_only_never_touches_pending_merges_or_wiki_pages(
        self, clique_wiki: Path
    ) -> None:
        from athenaeum.shadow_linkage import run_shadow_linkage

        wiki_root = clique_wiki / "wiki"
        before = {p.name: p.read_text() for p in sorted(wiki_root.glob("*.md"))}
        pending_path = wiki_root / "_pending_merges.md"
        assert not pending_path.exists()

        run_shadow_linkage(clique_wiki, embedding_provider=_fake_embed)

        after = {p.name: p.read_text() for p in sorted(wiki_root.glob("*.md"))}
        assert before == after
        assert not pending_path.exists()


class TestNoLLMCalls:
    """AC: "Add a test asserting the shadow path makes no LLM provider call."

    Mirrors :mod:`athenaeum.decay_sweep`'s ``TestNoLLMCalls`` convention: this
    module's call graph never constructs an ``anthropic`` client, so
    patching ``anthropic.Anthropic.__init__`` to explode must not affect a
    shadow-linkage run at all — including when NO embedding_provider stub is
    supplied and the real chromadb-or-hashing-fallback path is exercised.
    """

    def test_shadow_linkage_never_constructs_an_anthropic_client(
        self, clique_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anthropic

        from athenaeum.shadow_linkage import run_shadow_linkage

        def _explode(self, *args, **kwargs):
            raise AssertionError("shadow-linkage path must never construct an LLM client")

        monkeypatch.setattr(anthropic.Anthropic, "__init__", _explode)

        # Uses the real embedding path (chromadb-or-hashing fallback, never
        # an LLM) — proves the zero-LLM property end to end, not just for
        # the stub-embedder tests above.
        result = run_shadow_linkage(clique_wiki)
        assert result.candidate_file_count == 4


class TestWriteSnapshot:
    def test_refuses_when_nothing_measured(self, tmp_path: Path) -> None:
        from athenaeum.shadow_linkage import LinkagePathSummary, ShadowLinkageResult, write_snapshot

        empty_summary = LinkagePathSummary(label="x", cluster_count=0, multi_member_cluster_count=0)
        result = ShadowLinkageResult(
            candidate_file_count=0,
            threshold=0.55,
            complete_linkage=empty_summary,
            single_linkage=empty_summary,
            corpus_digest="none",
            athenaeum_version="0.0.0",
            git_sha="unknown",
            generated="2026-01-01T00:00:00Z",
        )
        with pytest.raises(ValueError, match="candidate_file_count=0"):
            write_snapshot(result, docs_path=tmp_path / "measurements.md")

    def test_writes_idempotent_snapshot(self, clique_wiki: Path, tmp_path: Path) -> None:
        from athenaeum.shadow_linkage import (
            SECTION_HEADING,
            run_shadow_linkage,
            write_snapshot,
        )

        docs_path = tmp_path / "docs" / "memory-model-measurements.md"
        result = run_shadow_linkage(clique_wiki, embedding_provider=_fake_embed)
        write_snapshot(result, docs_path=docs_path)
        text = docs_path.read_text(encoding="utf-8")
        assert SECTION_HEADING in text
        assert "docs/memory-model.md" in text  # shared header's own promise
        assert "candidate_file_count: 4" in text
        assert "comparator_pair_count" not in text  # rendered as prose, not the dataclass repr

        # Second run appends a NEW dated entry without touching the heading count.
        write_snapshot(result, docs_path=docs_path)
        text2 = docs_path.read_text(encoding="utf-8")
        assert text2.count(SECTION_HEADING) == 1
        assert text2.count("candidate_file_count: 4") == 2
