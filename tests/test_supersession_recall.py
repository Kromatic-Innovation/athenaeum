# SPDX-License-Identifier: Apache-2.0
"""Tests for recall's supersession handling (issue athenaeum#1493).

athenaeum already models supersession (``supersedes`` / ``superseded_by`` are
declared frontmatter relationships, parsed and validated at intake —
``athenaeum.intake``). Before this issue, retrieval never consulted the
declared field for ranking; the sole place that *did* consult it
(``athenaeum.search._is_recall_inactive``) treated it as a HARD EXCLUSION —
a page with the field set never entered the index/scan at all, so it could
never be marked and the "current outranks superseded" property held only
vacuously (the superseded page simply never appeared).

Organized by acceptance criterion:

- AC1: ranking consults the declared field, resolved at RENDER time (not
  carried into the FTS5/vector index) — see ``TestIsSuperseded`` and
  ``TestReorderHitsBySupersession``.
- AC2: rendered output distinguishes a superseded page from a current one —
  see ``TestRecallSupersessionIntegration::test_superseded_hit_is_marked_*``.
- AC3/AC4: the eval corpus fixtures use the declared field (see
  ``tests/evals/data/corpus/core/05-temporal.yaml``) and the temporal probes
  are re-run against them for FTS5 and keyword alike — see
  ``TestTemporalProbesAgainstDeclaredField``.
- AC5: regression coverage over the temporal probe class asserts RELATIVE
  order (current before superseded), never an absolute rank.
- AC6: the push (``unprompted=True``) path EXCLUDES a superseded hit rather
  than demoting it — a decision distinct from the explicit-recall path,
  covered by ``TestUnpromptedPushExcludesSuperseded``.

Multi-hop chains and cycles are covered by ``TestChainAndCycleSafety``: the
predicate never traverses the pointer, so chain length and cycles cannot
cause a hang — there is nothing to traverse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import search
from athenaeum.mcp_server import (
    _is_superseded,
    _recall_metadata_lines,
    _reorder_hits_by_supersession,
    recall_search,
)

# ---------------------------------------------------------------------------
# AC1 (boundary primitive): _is_superseded
# ---------------------------------------------------------------------------


class TestIsSuperseded:
    def test_no_superseded_by_is_false(self) -> None:
        assert _is_superseded({"name": "Current page"}) is False

    def test_empty_frontmatter_is_false(self) -> None:
        assert _is_superseded({}) is False

    def test_declared_superseded_by_is_true(self) -> None:
        assert _is_superseded({"superseded_by": "Current page"}) is True

    def test_blank_superseded_by_is_false(self) -> None:
        # parse_superseded_by strips whitespace; an all-whitespace value is
        # the same as absent.
        assert _is_superseded({"superseded_by": "   "}) is False

    def test_does_not_traverse_a_chain(self) -> None:
        """Each page's status depends ONLY on its own field — a chain of any
        length never requires following the pointer, which is what keeps a
        cycle (see TestChainAndCycleSafety) from being able to hang this."""
        a = {"name": "A (current)"}
        b = {"name": "B", "superseded_by": "A (current)"}
        c = {"name": "C", "superseded_by": "B"}
        assert _is_superseded(a) is False
        assert _is_superseded(b) is True
        assert _is_superseded(c) is True


# ---------------------------------------------------------------------------
# AC1/AC4: the render-time reorder
# ---------------------------------------------------------------------------


class TestReorderHitsBySupersession:
    def _write(self, wiki: Path, name: str, *, superseded_by: str | None) -> None:
        lines = ["---", f"name: {name}"]
        if superseded_by:
            lines.append(f"superseded_by: {superseded_by!r}")
        lines += ["---", "", f"Body for {name}."]
        (wiki / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")

    def test_reorders_superseded_to_the_end(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._write(wiki, "former", superseded_by="'current'")
        self._write(wiki, "current", superseded_by=None)
        hits = [("former.md", "former", 9.0), ("current.md", "current", 1.0)]
        reordered = _reorder_hits_by_supersession(hits, wiki_root=wiki, extra_roots=[])
        assert [h[0] for h in reordered] == ["current.md", "former.md"]

    def test_no_supersession_anywhere_is_a_no_op(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._write(wiki, "a", superseded_by=None)
        self._write(wiki, "b", superseded_by=None)
        hits = [("a.md", "a", 5.0), ("b.md", "b", 1.0)]
        reordered = _reorder_hits_by_supersession(hits, wiki_root=wiki, extra_roots=[])
        assert reordered == hits

    def test_never_drops_a_hit(self, tmp_path: Path) -> None:
        """Deprioritizes, does not filter — AC2 needs the page present to mark."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._write(wiki, "former", superseded_by="'current'")
        hits = [("former.md", "former", 9.0)]
        reordered = _reorder_hits_by_supersession(hits, wiki_root=wiki, extra_roots=[])
        assert reordered == hits


# ---------------------------------------------------------------------------
# AC2: rendered marking
# ---------------------------------------------------------------------------


class TestMetadataLinesMarking:
    def test_superseded_page_gets_a_status_line(self) -> None:
        lines = _recall_metadata_lines({"superseded_by": "Current page"})
        assert any("superseded" in line.lower() for line in lines)
        assert any("Current page" in line for line in lines)

    def test_current_page_gets_no_supersession_line(self) -> None:
        lines = _recall_metadata_lines({"name": "Current page"})
        assert not any("superseded" in line.lower() for line in lines)

    def test_contested_and_superseded_are_independent_lines(self) -> None:
        """A page can be both — the two Status lines are unrelated concerns
        (issue athenaeum#325's contradiction flag vs issue athenaeum#1493's
        supersession pointer) and neither suppresses the other."""
        lines = _recall_metadata_lines(
            {
                "status": "contradiction-flagged",
                "superseded_by": "Current page",
            }
        )
        assert any("contradiction-flagged" in line for line in lines)
        assert any("superseded" in line.lower() for line in lines)


# ---------------------------------------------------------------------------
# AC1/AC2/AC4: end-to-end via recall_search
# ---------------------------------------------------------------------------


class TestRecallSupersessionIntegration:
    def _wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        return wiki

    def _write_pair(self, wiki: Path) -> None:
        (wiki / "office_former.md").write_text(
            "---\nname: Office (former)\nsuperseded_by: 'Office (current)'\n---\n\n"
            "widget office location widget office widget office widget office widget\n"
        )
        (wiki / "office_current.md").write_text(
            "---\nname: Office (current)\n---\n\nwidget office location\n"
        )

    def test_superseded_page_stays_reachable(self, tmp_path: Path) -> None:
        """Demotes, does not exclude — the pre-fix behavior (a hard exclusion
        inside ``_is_recall_inactive``) made this impossible; AC2 requires the
        page be present so it can be marked."""
        wiki = self._wiki(tmp_path)
        self._write_pair(wiki)
        result = recall_search(wiki, "widget office location", top_k=5)
        assert "Office (former)" in result
        assert "Office (current)" in result

    def test_current_outranks_superseded_keyword(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        self._write_pair(wiki)
        # The former page has the higher raw keyword score (more "widget"
        # repeats) yet must still rank AFTER the current one.
        result = recall_search(wiki, "widget office location", top_k=5)
        current_pos = result.index("Office (current)")
        former_pos = result.index("Office (former)")
        assert current_pos < former_pos

    def test_current_outranks_superseded_fts5(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        self._write_pair(wiki)
        cache = tmp_path / "cache"
        search.build_fts5_index(wiki, cache)
        result = recall_search(
            wiki, "widget office location", top_k=5, search_backend="fts5", cache_dir=cache
        )
        current_pos = result.index("Office (current)")
        former_pos = result.index("Office (former)")
        assert current_pos < former_pos

    def test_superseded_hit_is_marked_in_rendered_text(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        self._write_pair(wiki)
        result = recall_search(wiki, "widget office location", top_k=5)
        # Only the FORMER page's block should carry the marker.
        former_block = result[result.index("Office (former)") :]
        assert "**Status:** superseded" in former_block
        current_block = result[result.index("Office (current)") : result.index("Office (former)")]
        assert "**Status:** superseded" not in current_block

    def test_history_flag_disables_supersession_reorder(self, tmp_path: Path) -> None:
        """Mirrors ``test_history_flag_disables_currency_reorder`` (issue
        athenaeum#904): an explicit historical query gets the backend's own
        relevance order, not the demotion applied by default."""
        wiki = self._wiki(tmp_path)
        self._write_pair(wiki)
        result = recall_search(wiki, "widget office location", top_k=5, history=True)
        former_pos = result.index("Office (former)")
        current_pos = result.index("Office (current)")
        # The former page has strictly higher keyword relevance in this
        # fixture (more repeats), so with the reorder disabled it ranks first.
        assert former_pos < current_pos


# ---------------------------------------------------------------------------
# AC3/AC4/AC5: the real eval-corpus temporal probes, re-run against the
# migrated (declared-field) fixtures.
# ---------------------------------------------------------------------------


class TestTemporalProbesAgainstDeclaredField:
    """Confirms AC3's finding for the RIGHT reason.

    The issue's own evidence used a body-text ``SUPERSEDED:`` convention that
    nothing in retrieval ever read. ``tests/evals/data/corpus/core/05-temporal.yaml``
    now uses the declared ``superseded_by`` field (issue athenaeum#1493 AC3); these
    tests assert the corpus's own ground truth (``expected_uids`` /
    ``must_not_rank``) against BOTH backends, by RELATIVE order — never an
    absolute rank, per AC5 (the corpus's own evidence shows an unrelated page
    can legitimately outrank the current answer on FTS5; asserting an
    absolute position would be both brittle and wrong).
    """

    @pytest.fixture(scope="class")
    @classmethod
    def corpus_wiki(cls, tmp_path_factory: pytest.TempPathFactory) -> Path:
        from tests.evals.corpus import build_corpus

        corpus = build_corpus(scale="core")
        root = tmp_path_factory.mktemp("supersession-corpus")
        wiki = corpus.materialize(root)
        return wiki

    @pytest.fixture(scope="class")
    @classmethod
    def fts5_cache(cls, corpus_wiki: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
        cache = tmp_path_factory.mktemp("supersession-fts5-cache")
        search.build_fts5_index(corpus_wiki, cache)
        return cache

    @staticmethod
    def _temporal_probes() -> list[tuple[str, str, str, str]]:
        """(probe_id, query, expected_current_uid, must_not_rank_uid)."""
        from tests.evals.corpus import load_probes

        out = []
        for probe in load_probes():
            if probe.probe_class != "temporal":
                continue
            assert len(probe.expected_uids) == 1
            assert len(probe.must_not_rank) == 1
            out.append((probe.id, probe.query, probe.expected_uids[0], probe.must_not_rank[0]))
        return out

    @pytest.mark.parametrize("case", _temporal_probes(), ids=lambda c: c[0])
    def test_keyword_current_outranks_or_excludes_superseded(
        self, corpus_wiki: Path, case: tuple[str, str, str, str]
    ) -> None:
        from tests.evals.metrics import uids_from_recall_output

        _probe_id, query, current_uid, superseded_uid = case
        output = recall_search(
            corpus_wiki, query, top_k=10, search_backend="keyword", cache_dir=None
        )
        ranked = uids_from_recall_output(output)
        # Relative order only (AC5) — whether the CURRENT page itself clears
        # the keyword top-10 against this probe's query is a single_hop
        # retrieval-quality question this issue does not own (the "core"
        # scale carries no distractor/ballast tier at all, so it is not the
        # scale the corpus's own design uses to calibrate that). What this
        # issue owns is: whenever the superseded page is a candidate, it must
        # never rank above its replacement.
        if current_uid in ranked and superseded_uid in ranked:
            assert ranked.index(current_uid) < ranked.index(superseded_uid), (
                f"superseded page {superseded_uid!r} outranked current {current_uid!r}: {ranked}"
            )

    @pytest.mark.parametrize("case", _temporal_probes(), ids=lambda c: c[0])
    def test_fts5_current_outranks_or_excludes_superseded(
        self, corpus_wiki: Path, fts5_cache: Path, case: tuple[str, str, str, str]
    ) -> None:
        from tests.evals.metrics import uids_from_recall_output

        _probe_id, query, current_uid, superseded_uid = case
        output = recall_search(
            corpus_wiki, query, top_k=10, search_backend="fts5", cache_dir=fts5_cache
        )
        ranked = uids_from_recall_output(output)
        # See the keyword variant's comment: relative order only.
        if current_uid in ranked and superseded_uid in ranked:
            assert ranked.index(current_uid) < ranked.index(superseded_uid), (
                f"superseded page {superseded_uid!r} outranked current {current_uid!r}: {ranked}"
            )

    def test_at_least_one_probe_actually_exercises_both_pages_per_backend(
        self, corpus_wiki: Path, fts5_cache: Path
    ) -> None:
        """A pure-absence pass ('superseded never shows up so it never loses')
        would satisfy the two tests above vacuously. This asserts at least one
        temporal probe puts BOTH pages of a pair into the SAME result set on
        EACH backend, so the ordering assertion is exercised for real."""
        from tests.evals.metrics import uids_from_recall_output

        for backend, cache in (("keyword", None), ("fts5", fts5_cache)):
            saw_both = False
            for _probe_id, query, current_uid, superseded_uid in self._temporal_probes():
                output = recall_search(
                    corpus_wiki, query, top_k=10, search_backend=backend, cache_dir=cache
                )
                ranked = uids_from_recall_output(output)
                if current_uid in ranked and superseded_uid in ranked:
                    saw_both = True
                    break
            assert saw_both, f"{backend}: no temporal probe surfaced both pages of a pair"


# ---------------------------------------------------------------------------
# AC6: the unprompted push path excludes rather than demotes
# ---------------------------------------------------------------------------


class TestUnpromptedPushExcludesSuperseded:
    def _wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "office_former.md").write_text(
            "---\nname: Office (former)\nmemory_tier: hot\n"
            "superseded_by: 'Office (current)'\n---\n\n"
            "widget office location\n"
        )
        (wiki / "office_current.md").write_text(
            "---\nname: Office (current)\nmemory_tier: hot\n---\n\nwidget office location\n"
        )
        return wiki

    def test_explicit_recall_keeps_superseded_reachable(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        result = recall_search(wiki, "widget office location", top_k=5, unprompted=False)
        assert "Office (former)" in result

    def test_unprompted_push_drops_superseded(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        result = recall_search(wiki, "widget office location", top_k=5, unprompted=True)
        assert "Office (current)" in result
        assert "Office (former)" not in result


# ---------------------------------------------------------------------------
# Multi-hop chains and cycles: deliberate, not accidental.
# ---------------------------------------------------------------------------


class TestChainAndCycleSafety:
    def test_three_hop_chain_completes_and_demotes_each_non_current_hop(
        self, tmp_path: Path
    ) -> None:
        """A (current) <- B (superseded_by A) <- C (superseded_by B).

        The chosen design (see ``_is_superseded``'s docstring) is a one-hop,
        local check: B and C are each demoted/marked off their OWN field,
        independently, without resolving the chain to its root. This test
        documents that choice and proves it terminates promptly for a chain
        longer than one hop -- there is no traversal to bound.
        """
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "a.md").write_text(
            "---\nname: A (current)\n---\n\nwidget chain topic\n"
        )
        (wiki / "b.md").write_text(
            "---\nname: B\nsuperseded_by: 'A (current)'\n---\n\nwidget chain topic\n"
        )
        (wiki / "c.md").write_text(
            "---\nname: C\nsuperseded_by: 'B'\n---\n\nwidget chain topic\n"
        )
        result = recall_search(wiki, "widget chain topic", top_k=5)
        assert "A (current)" in result and "B" in result and "C" in result
        a_pos = result.index("A (current)")
        # Both B and C must be marked superseded and demoted after A.
        for name in ("B", "C"):
            block_start = result.index(f"{name} (score:")
            assert block_start > a_pos
            block = result[block_start : block_start + 400]
            assert "**Status:** superseded" in block

    def test_mutual_cycle_does_not_hang(self, tmp_path: Path) -> None:
        """A superseded_by B, B superseded_by A -- a declared contradiction
        that intake/merge already rejects (``merge.py``'s MUST #3), included
        here only to prove recall itself never traverses the pointer and so
        cannot hang even if an inconsistent pair reached it."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "a.md").write_text(
            "---\nname: A\nsuperseded_by: 'B'\n---\n\nwidget cycle topic\n"
        )
        (wiki / "b.md").write_text(
            "---\nname: B\nsuperseded_by: 'A'\n---\n\nwidget cycle topic\n"
        )
        # No assertion on ORDER (both claim to supersede the other -- there is
        # no defined winner); the assertion is that this returns at all.
        result = recall_search(wiki, "widget cycle topic", top_k=5)
        assert "A" in result and "B" in result
