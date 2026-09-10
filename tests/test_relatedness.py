# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the compile-time relatedness writer (issue athenaeum#1576).

The eval in ``tests/evals/test_relatedness_writer_eval.py`` grades the writer's
OUTPUT against issue athenaeum#1570's corpus. These tests pin the properties
that corpus cannot see: the mutuality rule itself, what happens with no
neighbours at all, the compile-time direction, the config knob, and the
wiki-facing helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from athenaeum.config import resolve_relatedness_writer_enabled
from athenaeum.relatedness import (
    DEFAULT_MAX_INDEX_PAGES,
    DEFAULT_MIN_INDEX_PAGES,
    ROLE_TERM_OVERLAP,
    RelatednessIndex,
    build_index_from_wiki,
    page_text,
    propose_related_edges,
    reset_run_index,
    run_index,
    stamp_related_edges,
)


@dataclass
class _Entity:
    uid: str
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    body: str = ""
    related: list[dict[str, str]] = field(default_factory=list)


def _index(pages: dict[str, str]) -> RelatednessIndex:
    index = RelatednessIndex()
    for uid, text in pages.items():
        index.add(uid, text)
    return index


# Every proposal test below passes ``min_pages=0``. The small-index floor is a
# separate property with its own tests further down; leaving it at its default
# here would make each of these assert nothing but "the index is small".


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


def test_neighbours_rank_by_shared_distinctive_terms():
    index = _index(
        {
            "a": "berth scheduling quayside harbour rota",
            "b": "berth scheduling quayside harbour manifest",
            "c": "invoice ledger reconciliation vat return",
        }
    )
    ranked = index.neighbours("a", k=2, floor=0.0)
    assert [n.uid for n in ranked][0] == "b"
    assert all(0.0 <= n.score <= 1.0 for n in ranked)


def test_a_page_with_no_overlap_has_no_neighbours():
    index = _index({"a": "berth scheduling quayside", "b": "invoice ledger vat"})
    assert index.neighbours("a", k=4, floor=0.05) == []


def test_floor_suppresses_a_weak_best_candidate():
    index = _index(
        {
            "a": "berth scheduling quayside harbour rota manifest",
            "b": "invoice ledger reconciliation harbour",
        }
    )
    assert index.neighbours("a", k=4, floor=0.0), "sanity: they do share a term"
    assert index.neighbours("a", k=4, floor=0.99) == []


def test_remove_restores_the_pre_add_ranking():
    """Document frequencies must be reversible, or a re-index skews the idf."""
    base = _index({"a": "berth quayside", "b": "berth manifest"})
    before = base.neighbours("a", k=4, floor=0.0)
    base.add("c", "berth ledger vat")
    base.remove("c")
    assert base.neighbours("a", k=4, floor=0.0) == before
    assert "c" not in base


def test_re_adding_a_uid_does_not_double_count_it():
    index = _index({"a": "berth quayside", "b": "berth manifest"})
    index.add("a", "berth quayside")
    assert len(index) == 2
    assert index.neighbours("b", k=4, floor=0.0)[0].uid == "a"


def test_neighbours_are_stable_across_index_insertion_order():
    forward = _index({"a": "berth quay", "b": "berth quay dock", "c": "berth dock"})
    backward = _index({"c": "berth dock", "b": "berth quay dock", "a": "berth quay"})
    assert [n.uid for n in forward.neighbours("b", k=3, floor=0.0)] == [
        n.uid for n in backward.neighbours("b", k=3, floor=0.0)
    ]


# ---------------------------------------------------------------------------
# The mutuality rule
# ---------------------------------------------------------------------------


def test_mutuality_refuses_a_one_sided_edge():
    """A page near a hub does not get to link to it.

    ``hub`` shares a term with every other page, so it is in everyone's top-1;
    but ``spoke``'s own strongest neighbours are the other spokes, so ``hub``
    is not mutual with any of them.
    """
    index = _index(
        {
            "hub": "berth invoice rota manifest ledger quayside dock harbour",
            "spoke-1": "berth berth berth quayside quayside dock dock harbour",
            "spoke-2": "berth berth berth quayside quayside dock dock harbour",
            "spoke-3": "berth berth berth quayside quayside dock dock harbour",
        }
    )
    edges = propose_related_edges("spoke-1", index, k=1, floor=0.0, min_pages=0)
    assert all(edge["uid"] != "hub" for edge in edges)


def test_max_edges_caps_a_page_that_is_mutual_with_many():
    text = "berth scheduling quayside harbour rota manifest"
    index = _index({f"p{i}": text for i in range(8)})
    edges = propose_related_edges("p0", index, k=8, floor=0.0, max_edges=2, min_pages=0)
    assert len(edges) == 2


def test_every_proposed_edge_names_the_signal():
    index = _index({"a": "berth quayside harbour", "b": "berth quayside harbour"})
    edges = propose_related_edges("a", index, k=2, floor=0.0, min_pages=0)
    assert edges == [{"uid": "b", "role": ROLE_TERM_OVERLAP}]


def test_a_uid_absent_from_the_index_proposes_nothing():
    index = _index({"a": "berth quayside"})
    assert propose_related_edges("ghost", index, k=4, floor=0.0, min_pages=0) == []


def test_a_tiny_index_proposes_nothing_even_though_every_cosine_is_large():
    """The degenerate-IDF regime :data:`DEFAULT_MIN_INDEX_PAGES` exists for.

    These three pages share only the word "facts", and on a three-document
    corpus that is enough to clear the floor in both directions -- the writer
    would emit a COMPLETE graph. Asserted both ways so the test proves the
    floor is what suppresses it, not an absence of signal.
    """
    pages = {
        "a": "Acme Corp\nFacts about Acme Corp.",
        "b": "WidgetAlpha\nFacts about WidgetAlpha.",
        "c": "WidgetBeta\nFacts about WidgetBeta.",
    }
    index = _index(pages)
    without_floor = propose_related_edges("a", index, min_pages=0)
    assert len(without_floor) == 2, "fixture no longer demonstrates the failure"
    assert propose_related_edges("a", index) == []


def test_the_index_floor_is_inclusive_at_its_own_value():
    text = "berth scheduling quayside harbour rota"
    index = _index({f"p{i}": f"{text} variant{i}" for i in range(4)})
    assert propose_related_edges("p0", index, min_pages=5) == []
    assert propose_related_edges("p0", index, min_pages=4) != []


# ---------------------------------------------------------------------------
# stamp_related_edges: the write-boundary glue
# ---------------------------------------------------------------------------


def test_edges_point_only_at_pages_that_already_exist():
    """AC5, structurally: nothing already on disk is rewritten.

    ``old`` is in the index when ``new`` is compiled, so ``new`` may link to
    it -- but ``old`` is never handed to the writer and so cannot gain an
    edge back. That asymmetry IS the no-backfill guarantee.
    """
    index = _index({"old": "berth scheduling quayside harbour rota"})
    new = _Entity(uid="new", name="Berth rota", body="berth scheduling quayside harbour rota")
    added = stamp_related_edges([new], index, k=2, floor=0.0, min_pages=0)
    assert added == 1
    assert new.related == [{"uid": "old", "role": ROLE_TERM_OVERLAP}]


def test_a_later_entity_in_the_same_run_can_link_to_an_earlier_one():
    index = RelatednessIndex()
    first = _Entity(uid="one", body="berth scheduling quayside harbour rota")
    second = _Entity(uid="two", body="berth scheduling quayside harbour rota")
    stamp_related_edges([first, second], index, k=2, floor=0.0, min_pages=0)
    assert first.related == []
    assert second.related == [{"uid": "one", "role": ROLE_TERM_OVERLAP}]


def test_existing_edges_are_preserved_and_never_duplicated():
    index = _index({"parent": "berth scheduling quayside harbour rota"})
    child = _Entity(
        uid="child",
        body="berth scheduling quayside harbour rota",
        related=[{"uid": "parent", "role": "split-from"}],
    )
    added = stamp_related_edges([child], index, k=2, floor=0.0, min_pages=0)
    assert added == 0
    assert child.related == [{"uid": "parent", "role": "split-from"}]


def test_a_none_index_is_a_no_op():
    entity = _Entity(uid="a", body="berth scheduling quayside")
    assert stamp_related_edges([entity], None) == 0
    assert entity.related == []


# ---------------------------------------------------------------------------
# Wiki-facing helpers
# ---------------------------------------------------------------------------


def _write_page(root, uid: str, name: str, body: str, tags: str = "") -> None:
    tag_block = f"tags:\n  - {tags}\n" if tags else ""
    (root / f"{uid}.md").write_text(
        f"---\nuid: {uid}\ntype: concept\nname: {name!r}\n{tag_block}"
        f"access: internal\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_build_index_from_wiki_reads_uid_name_tags_and_body(tmp_path):
    _write_page(tmp_path, "u-1", "Berth rota", "quayside harbour scheduling", "delivery")
    _write_page(tmp_path, "u-2", "Quay ledger", "invoice reconciliation vat")
    index = build_index_from_wiki(tmp_path)
    assert index is not None
    assert set(index.uids) == {"u-1", "u-2"}


def test_build_index_from_wiki_skips_sidecars_and_cluster_outputs(tmp_path):
    _write_page(tmp_path, "u-1", "Berth rota", "quayside harbour")
    _write_page(tmp_path, "_pending_merges", "Pending", "quayside harbour")
    _write_page(tmp_path, "auto-cluster-3", "Cluster", "quayside harbour")
    index = build_index_from_wiki(tmp_path)
    assert index is not None
    assert index.uids == ["u-1"]


def test_build_index_from_wiki_declines_above_the_page_ceiling(tmp_path):
    _write_page(tmp_path, "u-1", "Berth rota", "quayside harbour")
    _write_page(tmp_path, "u-2", "Quay ledger", "invoice vat")
    assert build_index_from_wiki(tmp_path, max_pages=1) is None


def test_build_index_from_wiki_survives_a_page_with_no_uid(tmp_path):
    _write_page(tmp_path, "u-1", "Berth rota", "quayside harbour")
    (tmp_path / "broken.md").write_text("---\ntype: concept\n---\n\nbody\n", encoding="utf-8")
    index = build_index_from_wiki(tmp_path)
    assert index is not None
    assert index.uids == ["u-1"]


def test_page_text_includes_name_aliases_and_tags():
    text = page_text("Quiet Handover", ["quiet handover"], ["delivery"], "body text")
    assert "Quiet Handover" in text
    assert "quiet handover" in text
    assert "delivery" in text
    assert "body text" in text


def test_page_text_truncates_a_long_body():
    text = page_text("N", [], [], "x" * 100, max_body_chars=10)
    assert text.count("x") == 10


# ---------------------------------------------------------------------------
# The knob and the run cache
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_run_index():
    reset_run_index()
    yield
    reset_run_index()


def test_run_index_is_built_once_per_wiki_root(tmp_path, monkeypatch):
    _write_page(tmp_path, "u-1", "Berth rota", "quayside harbour")
    builds = []
    import athenaeum.relatedness as relatedness

    real = relatedness.build_index_from_wiki

    def counting(root, **kwargs):
        builds.append(root)
        return real(root, **kwargs)

    monkeypatch.setattr(relatedness, "build_index_from_wiki", counting)
    assert run_index(tmp_path) is not None
    assert run_index(tmp_path) is not None
    assert len(builds) == 1


def test_run_index_returns_none_when_the_writer_is_disabled(tmp_path):
    _write_page(tmp_path, "u-1", "Berth rota", "quayside harbour")
    config = {"librarian": {"relatedness_writer": False}}
    assert run_index(tmp_path, config=config) is None


def test_writer_is_enabled_by_default():
    assert resolve_relatedness_writer_enabled(None) is True
    assert resolve_relatedness_writer_enabled({}) is True
    assert resolve_relatedness_writer_enabled({"librarian": {}}) is True


@pytest.mark.parametrize("value", [False, "false", "off", "no", "0"])
def test_writer_knob_accepts_the_usual_false_spellings(value):
    assert resolve_relatedness_writer_enabled({"librarian": {"relatedness_writer": value}}) is False


def test_writer_knob_ignores_an_unrecognized_value():
    """An unparseable value falls through to the default, never to False."""
    assert (
        resolve_relatedness_writer_enabled({"librarian": {"relatedness_writer": "maybe"}}) is True
    )


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("ATHENAEUM_RELATEDNESS_WRITER", "0")
    assert resolve_relatedness_writer_enabled({"librarian": {"relatedness_writer": True}}) is False


def test_page_ceiling_default_is_not_accidentally_small():
    assert DEFAULT_MAX_INDEX_PAGES >= 25_000


def test_page_floor_default_sits_under_the_measured_regime():
    """96 is the eval corpus's core size -- the smallest N with a measured
    precision for this signal. The floor must be under it (or a real corpus
    the size of the eval's would write nothing) and well above the N=3 regime
    where the signal demonstrably degenerates."""
    assert 3 < DEFAULT_MIN_INDEX_PAGES < 96
