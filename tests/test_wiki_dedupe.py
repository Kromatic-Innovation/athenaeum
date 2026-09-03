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


# --- Issue athenaeum#1142: durable embedder + suppression attribution ---
#
# athenaeum#715's comparator cut-over removed both surfaces athenaeum#1142's
# coverage asserted on: the wiki-dedupe pass no longer writes
# ``wiki/_pending_merges.md`` proposal blocks, and the suppression sidecar
# ledger has no producer under the comparator path. Those tests
# (``TestSuppressionLedger``, and the two proposal-attribution / run-
# comparison cases) are recovered verbatim from ref ``b79efc0`` in issue
# athenaeum#1243, which re-sites athenaeum#1142's requirement onto the
# comparator's verdict ledger. Only the blast-radius guard below survives
# here: it exercises ``pending_merges.write_pending_merge`` directly and
# guards ``merge.py``'s raw-intake path, which athenaeum#715 does not touch.


class TestEmbedderAttributionOnProposals:
    """Blast-radius guard for the raw-intake proposal write path."""

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


# --- Issue athenaeum#1252: residual over-clustering after athenaeum#1140 ---
#
# athenaeum#1140's chunk-and-mean-pool fix reduced but did not resolve
# over-clustering (99-page largest cluster, 88% of the corpus, at the live
# 0.55 threshold — full trail in the issue). This lane characterizes ONE of
# the issue's three candidate causes purely from code + synthetic fixtures
# (no real embedder, no corpus content — counts/classes only, per the
# issue's own privacy requirement): eligibility
# (``discover_wiki_dedupe_candidates``) admitting pages whose body is too
# short for athenaeum#1140's fix to help at all.
#
# The pipeline fact this rests on is ``mean_pool``'s own documented
# behavior (athenaeum.vecmath): "A single-vector input is returned
# re-normalized (a no-op...)". A page that fits in one
# ``_CHUNK_CHARS``-sized chunk therefore gets EXACTLY the same vector
# athenaeum#1140 would have produced pre-fix — chunking a single-chunk page
# changes nothing, mathematically, regardless of embedder quality. The new
# ``librarian.wiki_dedupe_min_body_chars`` knob (default 0 = off) lets an
# operator exclude that unprotected short end of the eligible population.


class TestSingleChunkPagesAreStructurallyUnprotectedByMeanPool:
    """Eligibility characterization (athenaeum#1252 AC2): a page short enough
    to fit in one chunk gets zero benefit from athenaeum#1140's fix, by
    construction of ``mean_pool`` — provable without any embedder."""

    def test_short_page_produces_exactly_one_chunk(self) -> None:
        from athenaeum.wiki_dedupe import _CHUNK_CHARS, _chunk_page_text

        short_body = "word " * 50  # well under _CHUNK_CHARS
        assert len(short_body) < _CHUNK_CHARS
        assert len(_chunk_page_text(short_body)) == 1

    def test_mean_pool_of_one_chunk_is_a_no_op(self) -> None:
        """Pins the exact claim the knob's rationale rests on: mean-pooling
        a single chunk vector reproduces the pre-athenaeum#1140 whole-page
        embedding (a re-normalize of itself), never diluting it."""
        from athenaeum.vecmath import cosine, mean_pool

        whole_page_vector = [3.0, 4.0, 0.0]  # not unit length on purpose
        pooled = mean_pool([whole_page_vector])
        assert cosine(pooled, whole_page_vector) == pytest.approx(1.0)

    def test_two_chunk_page_dilutes_lede_by_only_half(self) -> None:
        """Contrast case: the modal real-corpus shape (one shared/boilerplate
        lede chunk + exactly one divergent body chunk) gets the WEAKEST
        non-zero dilution the athenaeum#1140 fix can provide — the lede
        chunk still contributes half the pooled vector's weight, unlike a
        page with many divergent chunks (see the existing
        ``TestChunkAndMeanPoolFixesTruncationCollapse`` fixture above, whose
        multi-chunk divergent body dilutes far more)."""
        from athenaeum.vecmath import cosine, mean_pool

        lede_vec = [1.0, 0.0, 0.0]
        divergent_a = [0.0, 1.0, 0.0]
        divergent_b = [0.0, 0.0, 1.0]

        pooled_a = mean_pool([lede_vec, divergent_a])
        pooled_b = mean_pool([lede_vec, divergent_b])

        # Two fully orthogonal bodies still leave the pooled pair at exactly
        # 0.5 cosine once diluted by only one shared lede chunk — a real
        # (non-orthogonal) embedder's baseline prose-to-prose similarity
        # only pushes this UP, never down. This is the structural ceiling,
        # not a live measurement.
        assert cosine(pooled_a, pooled_b) == pytest.approx(0.5)


class TestWikiDedupeMinBodyCharsKnob:
    """The athenaeum#1252 fix behind its config knob. DEFAULT 0 (off) means
    every call site that does not opt in sees byte-identical eligibility to
    before this issue — see ``resolve_wiki_dedupe_min_body_chars``."""

    _SHORT_BODY = "A short stub page with almost no distinguishing content."
    _LONG_BODY = _SHARED_LEDE + _DIVERGENT_BODY_A  # well over any small floor

    def test_default_off_short_page_still_eligible(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "short.md", body=self._SHORT_BODY)
        _write_page(wiki_root, "long.md", body=self._LONG_BODY)

        # No config at all (existing call sites) ...
        names = {c.path.name for c in discover_wiki_dedupe_candidates(wiki_root)}
        assert names == {"short.md", "long.md"}

        # ... and an explicit config with the knob simply absent.
        names = {
            c.path.name
            for c in discover_wiki_dedupe_candidates(wiki_root, config={"librarian": {}})
        }
        assert names == {"short.md", "long.md"}

    def test_configured_floor_excludes_short_page_only(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "short.md", body=self._SHORT_BODY)
        _write_page(wiki_root, "long.md", body=self._LONG_BODY)

        config = {"librarian": {"wiki_dedupe_min_body_chars": 200}}
        candidates = discover_wiki_dedupe_candidates(wiki_root, config=config)
        names = {c.path.name for c in candidates}
        assert names == {"long.md"}

    def test_configured_floor_removes_short_page_from_clustering_end_to_end(
        self, tmp_path: Path
    ) -> None:
        """With the knob on, a short page never reaches the embedder at all
        — proven with a deterministic stub embedder that raises if handed
        the short page's body."""
        from athenaeum.wiki_dedupe import _chunk_page_text, find_wiki_page_clusters

        wiki_root = tmp_path / "knowledge" / "wiki"
        _write_page(wiki_root, "short-a.md", body=self._SHORT_BODY)
        _write_page(wiki_root, "short-b.md", body=self._SHORT_BODY + " ")
        _write_page(wiki_root, "long-a.md", body=_PAGE_A_CONTENT)
        _write_page(wiki_root, "long-b.md", body=_PAGE_B_CONTENT)

        def _stub_provider(texts: list[str]) -> list[list[float]]:
            for t in texts:
                assert "short stub page" not in t, (
                    "the short-body candidate must be excluded before "
                    "embedding when the floor is configured"
                )
            # Deterministic vectors keyed only on which long page a chunk
            # came from — reuses the athenaeum#1140 fixture's vector map.
            chunks_a = _chunk_page_text(_PAGE_A_CONTENT)
            chunks_b = _chunk_page_text(_PAGE_B_CONTENT)
            vector_map = _build_chunk_vector_map(chunks_a, chunks_b)
            return [vector_map[t] for t in texts]

        config = {"librarian": {"wiki_dedupe_min_body_chars": 200}}
        clusters = find_wiki_page_clusters(
            wiki_root, threshold=0.55, embedding_provider=_stub_provider, config=config
        )
        all_members = {name for c in clusters for name in c.member_paths}
        assert "short-a.md" not in all_members
        assert "short-b.md" not in all_members

    def test_configured_floor_is_a_yaml_only_knob_no_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors ``resolve_min_cluster_cohesion``'s shape: no env var —
        this is a corpus-tuning knob, not an ops-emergency dial."""
        from athenaeum.config import resolve_wiki_dedupe_min_body_chars

        monkeypatch.setenv("ATHENAEUM_WIKI_DEDUPE_MIN_BODY_CHARS", "500")
        assert resolve_wiki_dedupe_min_body_chars({"librarian": {}}) == 0


# --- Issue athenaeum#1243: athenaeum#1142's requirement, re-sited ---------
#
# athenaeum#1227's cut-over stranded athenaeum#1142's suppression ledger with
# no producer and left two holes the comparator's own verdict ledger does not
# cover: an embedder identity computed on every run and read by nothing, and a
# pair that produces no verdict leaving no row at all. The tests below are
# athenaeum#1142's executable spec (recovered from ref ``b79efc0``, per
# athenaeum#1243's "recover it from git, do not rewrite it") adapted to the
# comparator path: field names follow the re-sited artifact
# (:mod:`athenaeum.wiki_dedupe_attribution`), the assertions' INTENT carries
# over verbatim.
#
# Measured on the live corpus (athenaeum#1243's measurement comment): the
# comparator reaches a Gate 1 verdict for ZERO of 22,040 pairs today, so with
# ``client=None`` — the production wiring — the no-verdict branch is not an
# edge case, it is every pair. ``client=None`` is therefore the DEFAULT
# posture of these tests, not a degraded variant of them.


def _identical_embed(texts: list[str]) -> list[list[float]]:
    """Every chunk to the same unit vector — one cohesive cluster at any
    threshold, independent of page bodies."""
    return [[1.0, 0.0] for _ in texts]


def _seed_identical_pages(root: Path, n: int = 3) -> Path:
    """*n* byte-identical-body ``concept`` pages; returns the knowledge_root."""
    wiki_root = root / "knowledge" / "wiki"
    for i in range(n):
        _write_page(
            wiki_root,
            f"dup-{i}.md",
            body="Shared identical body text for the run-comparison fixture.",
        )
    return wiki_root.parent


def _run_pass(knowledge_root: Path, **kw):
    """Run the pass with a held lock and the comparator enabled."""
    from athenaeum.runlock import RunLock
    from athenaeum.wiki_dedupe import propose_wiki_page_merges

    kw.setdefault("config", _COMPARATOR_CONFIG)
    kw.setdefault("threshold", 0.8)
    kw.setdefault("embedding_provider", _identical_embed)
    kw.setdefault("client", None)
    lock = RunLock(knowledge_root)
    with lock:
        return propose_wiki_page_merges(knowledge_root, lock=lock, **kw)


def _rows(knowledge_root: Path):
    from athenaeum.wiki_dedupe_attribution import read_attribution_report

    return read_attribution_report(knowledge_root / "wiki")


class TestEveryExaminedPairLeavesADurableRow:
    """AC1: every candidate pair the pass examines leaves a durable,
    machine-readable row — including the pairs that produce no verdict, which
    were both a bare ``continue`` before this issue."""

    def test_no_verdict_pairs_each_leave_a_row_with_the_discarded_reason(
        self, tmp_path: Path
    ) -> None:
        """``record_comparison`` -> ``ok=False``: its own docstring says
        "nothing ledgered", and this caller used to drop ``reason`` on the
        floor. 53.6% of live-corpus pairs land here."""
        from athenaeum.wiki_dedupe_attribution import OUTCOME_NO_VERDICT

        knowledge_root = _seed_identical_pages(tmp_path, n=3)
        results = _run_pass(knowledge_root)

        assert results == [], "client=None settles nothing, so nothing is decided"
        rows = _rows(knowledge_root)
        # itertools.combinations over a 3-member cluster = 3 pairs.
        assert len(rows) == 3
        assert {r.pair for r in rows} == {"dup-0+dup-1", "dup-0+dup-2", "dup-1+dup-2"}
        for row in rows:
            assert row.outcome == OUTCOME_NO_VERDICT
            assert row.reason, "the reason record_comparison returned must survive"
            assert row.became_proposal is False
            assert len(row.sources) == 2
            assert row.at  # non-empty ISO timestamp
            assert row.cluster_threshold == 0.8
            assert row.n_cluster_members == 3

    def test_no_verdict_pair_is_not_written_into_the_verdict_ledger(
        self, tmp_path: Path
    ) -> None:
        """The row is re-sited to a SIBLING artifact, deliberately. A pair the
        comparator did not settle has no honest home in ``wiki/_verdicts/``
        (``build_verdict_entry`` validates against the five verdict values),
        and ``verdicts.compact()`` has no production caller, so that ledger is
        unbounded-append in production today (AC4)."""
        from athenaeum.verdicts import lookup_pair

        knowledge_root = _seed_identical_pages(tmp_path, n=3)
        _run_pass(knowledge_root)

        wiki_root = knowledge_root / "wiki"
        assert lookup_pair(wiki_root, "dup-0+dup-1") is None
        assert _rows(knowledge_root), "but the attribution snapshot HAS the row"

    def test_cross_class_rejected_pair_leaves_a_row(self, tmp_path: Path) -> None:
        """``cross_class_precheck`` rejects before any comparison — 46.4% of
        live-corpus pairs. The only prior evidence was a ``log.info``, i.e. a
        rotating log, which is exactly what athenaeum#1142 was filed to end."""
        from athenaeum.wiki_dedupe_attribution import OUTCOME_CROSS_CLASS_REJECTED

        wiki_root = tmp_path / "knowledge" / "wiki"
        body = "Shared identical body text for the cross-class fixture."
        for name, mem_class in (("policy-a.md", "guideline"), ("policy-b.md", "fact")):
            path = _write_page(wiki_root, name, body=body)
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace("type: concept", f"type: concept\nmemory_class: {mem_class}"),
                encoding="utf-8",
            )

        knowledge_root = wiki_root.parent
        client = _fake_llm_client(_content_payload("equivalent"))
        results = _run_pass(knowledge_root, client=client)

        assert results == []
        assert client.messages.create.call_count == 0, "must not reach Gate 2"
        rows = _rows(knowledge_root)
        assert len(rows) == 1
        assert rows[0].outcome == OUTCOME_CROSS_CLASS_REJECTED
        assert rows[0].reason == "cross_class_incompatible"
        assert rows[0].detail, "the human-readable rejection detail must survive too"
        assert rows[0].became_proposal is False

    def test_decided_pairs_also_leave_a_row_so_one_read_covers_the_pass(
        self, duplicate_topic_wiki: Path
    ) -> None:
        """A decided pair is in ``wiki/_verdicts/`` already, but it is recorded
        here too so AC3's diagnostic needs exactly ONE artifact read rather
        than a join across two."""
        from athenaeum.wiki_dedupe_attribution import OUTCOME_DECIDED

        client = _fake_llm_client(_content_payload("equivalent"))
        results = _run_pass(
            duplicate_topic_wiki, embedding_provider=_fake_embed, client=client
        )

        assert results, "the venture-a/b/c cluster must yield decided pairs"
        rows = _rows(duplicate_topic_wiki)
        assert len(rows) == len(results)
        for row in rows:
            assert row.outcome == OUTCOME_DECIDED
            assert row.verdict == "duplicate"
            assert row.action and row.action != "noop"
            assert row.became_proposal is True

    def test_memoized_fresh_pair_leaves_a_row_distinguishing_it_from_unexamined(
        self, duplicate_topic_wiki: Path
    ) -> None:
        """A pair decided on a PRIOR run is skipped, not re-decided. A bare
        ``continue`` made "examined, memoized" indistinguishable from "never
        examined" — a distinction AC1 needs."""
        from athenaeum.wiki_dedupe_attribution import OUTCOME_FRESH

        client = _fake_llm_client(_content_payload("equivalent"))
        first = _run_pass(
            duplicate_topic_wiki, embedding_provider=_fake_embed, client=client
        )
        assert first
        second = _run_pass(
            duplicate_topic_wiki, embedding_provider=_fake_embed, client=client
        )
        assert second == [], "second run is memoized, nothing newly decided"

        rows = _rows(duplicate_topic_wiki)
        assert rows, "the memoized run still accounts for every pair it examined"
        assert all(r.outcome == OUTCOME_FRESH for r in rows)
        assert all(r.verdict == "duplicate" for r in rows)
        assert all(r.became_proposal is True for r in rows)


class TestAttributionSnapshotIsBoundedAndCurrent:
    """AC4, recovered from athenaeum#1142's ``TestSuppressionLedger``."""

    def test_written_even_when_the_run_examines_zero_pairs(
        self, tmp_path: Path
    ) -> None:
        """athenaeum#1142's ``test_ledger_written_even_with_zero_suppressions``:
        written every real run, even to empty, so the canonical file always
        reflects THIS run's state, never a stale prior one."""
        from athenaeum.wiki_dedupe_attribution import attribution_path

        # Two ORTHOGONAL pages (cosine 0.0 under ``_fake_embed``): no cluster
        # of size >= 2 forms, so the pass runs and examines zero pairs.
        wiki_root = tmp_path / "knowledge" / "wiki"
        _write_page(wiki_root, "venture.md", body=_BODY_A)
        _write_page(wiki_root, "hobby.md", body=_BODY_UNRELATED)
        knowledge_root = wiki_root.parent

        results = _run_pass(knowledge_root, embedding_provider=_fake_embed)
        assert results == []
        canonical = attribution_path(wiki_root)
        assert canonical.is_file(), "the snapshot is written even to empty"
        assert canonical.read_text(encoding="utf-8") == ""

    def test_dry_run_never_writes_the_snapshot(self, tmp_path: Path) -> None:
        """athenaeum#1142's ``test_dry_run_never_writes_the_ledger``: a dry run
        decides and enacts nothing, so it has no run state to snapshot."""
        from athenaeum.wiki_dedupe import propose_wiki_page_merges
        from athenaeum.wiki_dedupe_attribution import attribution_path

        knowledge_root = _seed_identical_pages(tmp_path, n=3)
        propose_wiki_page_merges(
            knowledge_root,
            config=_COMPARATOR_CONFIG,
            threshold=0.8,
            embedding_provider=_identical_embed,
            dry_run=True,
        )
        assert not attribution_path(knowledge_root / "wiki").exists()

    def test_skipped_for_want_of_a_lock_never_writes_the_snapshot(
        self, tmp_path: Path
    ) -> None:
        """A pass skipped for want of a lock examined nothing, so it must not
        clobber the last REAL run's rows — the canonical file is a replace,
        not an append."""
        from athenaeum.wiki_dedupe import propose_wiki_page_merges

        knowledge_root = _seed_identical_pages(tmp_path, n=3)
        _run_pass(knowledge_root)
        before = _rows(knowledge_root)
        assert len(before) == 3

        propose_wiki_page_merges(
            knowledge_root,
            config=_COMPARATOR_CONFIG,
            threshold=0.8,
            embedding_provider=_identical_embed,
            lock=None,
        )
        assert _rows(knowledge_root) == before

    def test_canonical_file_is_replaced_not_appended_across_runs(
        self, tmp_path: Path
    ) -> None:
        """athenaeum#1142's
        ``test_canonical_file_is_replaced_not_appended_across_runs`` (AC4): a
        current-run SNAPSHOT, not an accumulating append-only artifact — the
        exact asymmetry athenaeum#1229's 1.4M-row unbounded ledger shows the
        cost of."""
        knowledge_root = _seed_identical_pages(tmp_path, n=3)
        _run_pass(knowledge_root)
        assert len(_rows(knowledge_root)) == 3

        # Same corpus, same posture: an identical second run must REPLACE the
        # three rows, never append a second set of three.
        _run_pass(knowledge_root)
        assert len(_rows(knowledge_root)) == 3

    def test_rotations_are_pruned_to_the_shared_retention_knob(
        self, tmp_path: Path
    ) -> None:
        """AC4: rotation retention reuses ``librarian.rotation_retention`` —
        the SAME knob and the SAME pruning helper
        ``raw/_librarian-clusters.jsonl`` uses, not a second policy."""
        from athenaeum.wiki_dedupe_attribution import attribution_path

        knowledge_root = _seed_identical_pages(tmp_path, n=3)
        wiki_root = knowledge_root / "wiki"
        stem = attribution_path(wiki_root).stem
        for stamp in ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"]:
            (wiki_root / f"{stem}-{stamp}.jsonl").write_text("{}\n", encoding="utf-8")

        _run_pass(
            knowledge_root,
            config={
                "librarian": {"comparator_enabled": True, "rotation_retention": 2}
            },
        )
        assert len(list(wiki_root.glob(f"{stem}-*.jsonl"))) == 2


class TestEmbedderAttributionDiffersByRun:
    """AC2's non-constant-field guard, recovered from athenaeum#1142's
    ``test_suppression_ledger_embedder_field_differs_between_runs``: the
    embedder field must DIFFER between a real-provider run and a
    ``provider -> None`` fallback run, so it cannot silently go constant."""

    def test_attribution_embedder_field_differs_between_runs(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.clusters import (
            EMBEDDER_CHROMADB_DEFAULT,
            EMBEDDER_FALLBACK_HASHING,
        )

        real_root = _seed_identical_pages(tmp_path / "real", n=3)
        _run_pass(real_root)
        real_rows = _rows(real_root)

        fallback_root = _seed_identical_pages(tmp_path / "fallback", n=3)
        # 0.6, not 0.8: the fallback-hashing embedder folds in each page's
        # distinct filename token, so even byte-identical bodies land at
        # ~0.667 pairwise — 0.8 would form no cluster at all here.
        _run_pass(
            fallback_root, threshold=0.6, embedding_provider=lambda texts: None
        )
        fallback_rows = _rows(fallback_root)

        assert real_rows and fallback_rows
        assert {r.embedder for r in real_rows} == {EMBEDDER_CHROMADB_DEFAULT}
        assert {r.embedder for r in fallback_rows} == {EMBEDDER_FALLBACK_HASHING}
        assert real_rows[0].embedder != fallback_rows[0].embedder

    def test_embedder_is_persisted_not_merely_computed(self, tmp_path: Path) -> None:
        """AC2's headline: before this issue ``Cluster.embedder`` was stamped
        on every run and read by NOTHING. It must now survive to disk and be
        readable back without re-running the pass."""
        from athenaeum.clusters import EMBEDDER_CHROMADB_DEFAULT
        from athenaeum.wiki_dedupe_attribution import read_attribution_report

        knowledge_root = _seed_identical_pages(tmp_path, n=3)
        _run_pass(knowledge_root)
        # A fresh read off disk, in a different call, with no pass re-run.
        reread = read_attribution_report(knowledge_root / "wiki")
        assert reread
        assert all(r.embedder == EMBEDDER_CHROMADB_DEFAULT for r in reread)


class TestOneArtifactReadAnswersTheDiagnosticQuestion:
    """AC3: the reproduction target is athenaeum#1005's diagnostic question —
    "which embedder produced this pair's candidacy, and why did it not become
    a proposal?" — answered by a SINGLE artifact read with no live host log
    access. That is the exact question two independent diagnostic passes
    failed to answer, each reaching a wrong conclusion."""

    def test_a_single_read_answers_both_halves_for_a_non_proposal_pair(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.clusters import EMBEDDER_CHROMADB_DEFAULT
        from athenaeum.wiki_dedupe_attribution import explain_pair

        knowledge_root = _seed_identical_pages(tmp_path, n=3)
        _run_pass(knowledge_root)

        answer = explain_pair(knowledge_root / "wiki", "dup-0+dup-1")
        assert answer is not None
        # "which embedder produced this pair's candidacy"
        assert answer["embedder"] == EMBEDDER_CHROMADB_DEFAULT
        # "...and why did it not become a proposal?"
        assert answer["became_proposal"] is False
        assert answer["outcome"] == "no-verdict"
        assert answer["reason"]
        assert answer["cluster_threshold"] == 0.8
        assert len(answer["sources"]) == 2
        assert answer["at"]


class TestNamedAsAGate:
    """AC5: ``comparator_enabled`` is not flipped on by this change, and
    ``wiki_dedupe`` cross-references the re-sited ledger at the site where
    ``DEFAULT_WIKI_SUPPRESSIONS_FILENAME`` was removed."""

    def test_comparator_enabled_default_is_still_off(self) -> None:
        from athenaeum.config import resolve_comparator_enabled

        assert resolve_comparator_enabled({}) is False
        assert resolve_comparator_enabled(None) is False

    def test_wiki_dedupe_cross_references_the_re_sited_ledger(self) -> None:
        """Structural, not textual: the pass must actually depend on the
        re-sited module, so the pointer cannot rot into a stale comment."""
        import athenaeum.wiki_dedupe as wd

        assert wd.write_attribution_report.__module__ == (
            "athenaeum.wiki_dedupe_attribution"
        )

    def test_the_removal_site_still_names_this_issue(self) -> None:
        from pathlib import Path as _Path

        import athenaeum.wiki_dedupe as wd

        source = _Path(wd.__file__).read_text(encoding="utf-8")
        assert "athenaeum#1243" in source
        assert "athenaeum#1244" in source, (
            "the gate comment must name the coordinate-backfill precondition "
            "sitting under this issue"
        )


class TestEveryRemainingBranchIsAccountedFor:
    """QA review of athenaeum#1243: the three ``propose_wiki_page_merges``
    branches that a green suite left unproven. Each mutates the WIRING
    (``monkeypatch`` on the collaborator this pass calls) rather than the
    logic under test, so the branch is exercised through the real pass."""

    def test_page_read_error_leaves_a_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pair whose page cannot be read off disk is still accounted for.
        Unreachable through candidate discovery (which reads every page first),
        so the read must be made to fail at the comparator's own read site."""
        import athenaeum.wiki_dedupe as wd
        from athenaeum.wiki_dedupe_attribution import OUTCOME_READ_ERROR

        knowledge_root = _seed_identical_pages(tmp_path, n=3)

        def _boom(path: Path):
            raise OSError("simulated unreadable page")

        monkeypatch.setattr(wd, "page_from_path", _boom)
        results = _run_pass(knowledge_root)

        assert results == []
        rows = _rows(knowledge_root)
        assert len(rows) == 3
        for row in rows:
            assert row.outcome == OUTCOME_READ_ERROR
            assert row.reason == "page-read-failed"
            assert "simulated unreadable page" in row.detail
            assert row.became_proposal is False

    def test_erasure_class_pair_is_deliberately_NOT_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ONE deliberate carve-out from AC1's "every examined pair leaves
        a row": an erasure-class (pii-flagged) pair must not reach this in-git
        artifact, because
        :func:`athenaeum.verdicts.refuse_if_erasure_class`'s posture outranks
        an observability record.

        Unreachable through the real pass today —
        ``discover_wiki_dedupe_candidates`` filters pii-flagged pages upstream
        — which is exactly why it needs a test: nothing would otherwise catch
        either an accidental leak (if that upstream filter is loosened) or an
        accidental REMOVAL of the carve-out."""
        import athenaeum.wiki_dedupe as wd
        from athenaeum.wiki_dedupe_attribution import ERASURE_CLASS_REFUSED_REASON

        knowledge_root = _seed_identical_pages(tmp_path, n=3)

        def _refused(wiki_root, page_a, page_b, **kw):
            return {
                "ok": False,
                "pair": f"{page_a.id}+{page_b.id}",
                "verdict": None,
                "skipped": None,
                "reason": ERASURE_CLASS_REFUSED_REASON,
                "outcome": None,
            }

        monkeypatch.setattr(wd, "record_comparison", _refused)
        results = _run_pass(knowledge_root)

        assert results == []
        # The snapshot is still WRITTEN (the pass ran) -- it is simply empty,
        # which is the honest artifact: no row, and no pii-derived value.
        # Asserted on the FILE, not just on ``_rows``: an empty read is
        # ambiguous between "written empty" and "never written", and only the
        # former is correct here.
        from athenaeum.wiki_dedupe_attribution import attribution_path

        canonical = attribution_path(knowledge_root / "wiki")
        assert canonical.is_file(), "the pass ran, so the snapshot is written"
        assert canonical.read_text(encoding="utf-8") == ""
        assert _rows(knowledge_root) == []

    def test_a_missing_outcome_still_accounts_for_the_pair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive branch: ``ok=True`` with no ``skipped`` should always
        carry an outcome under ``record_comparison``'s current contract. If
        that contract ever drifts, the pair must still be recorded rather than
        silently vanishing — which is what the pre-athenaeum#1243 bare
        ``continue`` did to every branch."""
        import athenaeum.wiki_dedupe as wd
        from athenaeum.wiki_dedupe_attribution import OUTCOME_NO_VERDICT

        knowledge_root = _seed_identical_pages(tmp_path, n=3)

        def _contract_drift(wiki_root, page_a, page_b, **kw):
            return {
                "ok": True,
                "pair": f"{page_a.id}+{page_b.id}",
                "verdict": "distinct",
                "skipped": None,
                "reason": None,
                "outcome": None,
            }

        monkeypatch.setattr(wd, "record_comparison", _contract_drift)
        results = _run_pass(knowledge_root)

        assert results == [], "no outcome means no effect can be enacted"
        rows = _rows(knowledge_root)
        assert len(rows) == 3
        assert all(r.outcome == OUTCOME_NO_VERDICT for r in rows)
        assert all(r.reason == "no-outcome-returned" for r in rows)
