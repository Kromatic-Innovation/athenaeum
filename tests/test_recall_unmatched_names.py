# SPDX-License-Identifier: Apache-2.0
"""The unmatched-names signal on ``recall`` results (issue athenaeum#3033).

When a query names something that no returned page mentions, ``recall``
appends one line saying so, so an agent asked about an unknown person stops
re-searching the surname and the given name. This module unit-tests the pure
check (:func:`athenaeum.recall_overflow.unmatched_name_terms`), the renderer
(:func:`athenaeum.recall_overflow.render_unmatched_names_line`), and the
wiring through :func:`athenaeum.mcp_server.recall_search`.

The query/result pairs in ``_GRID_*`` below are modelled on the stored rows
of the 2026-11-20 native-memory grid (run 44783114473), with each result
shortened to a line or two. The replay over every stored ``recall`` call is
recorded in ``docs/measurements/abstention-cost-2026-11-21.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.mcp_server import recall_search
from athenaeum.recall_overflow import (
    UNMATCHED_NAMES_TEMPLATE_NAME,
    load_unmatched_names_template,
    render_unmatched_names_line,
    unmatched_name_terms,
)

# ---------------------------------------------------------------------------
# unmatched_name_terms
# ---------------------------------------------------------------------------

_SHOWN = (
    "Found 3 matching pages:\n\n"
    "### 1. Amir Osei (score: 0.4)\n# Amir Osei\n\nAmir Osei is the finance partner.\n"
    "### 2. Gabriel Orsini (score: 0.3)\n# Gabriel Orsini\n\nGabriel Orsini builds "
    "the analytics models.\n"
    "### 3. Bramfield Retainer (score: 0.2)\n# Bramfield\n\nThe Bramfield retainer "
    "renews each spring; payment terms are net 30.\n"
)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # The abstention shapes from the grid: a name on no page.
        ("Yusuf Adeyemi responsible", ["Yusuf", "Adeyemi"]),
        ("Adeyemi", ["Adeyemi"]),
        ("Harrowgate Industrial payment terms", ["Harrowgate", "Industrial"]),
        # A name that IS on a shown page never fires.
        ("Amir Osei", []),
        ("Bramfield retainer renewal", []),
        # Only the absent half of a partly matched name is reported.
        ("Amir Adeyemi", ["Adeyemi"]),
        # Matching is case-insensitive against the shown text.
        ("BRAMFIELD terms", []),
    ],
)
def test_unmatched_name_terms(query: str, expected: list[str]) -> None:
    assert unmatched_name_terms(query, _SHOWN) == expected


def test_lowercase_topic_query_never_fires() -> None:
    """Only capitalised words are treated as names: an all-lowercase topic
    query says nothing, even when its words are absent. This is the known gap
    (the grid's ``abstain_unknown_policy`` probe is a topic, not a name)."""
    assert unmatched_name_terms("parental leave policy", _SHOWN) == []


def test_question_words_and_short_words_are_not_names() -> None:
    assert unmatched_name_terms("What does Who Why", _SHOWN) == []
    # Two-letter words (acronyms a page may spell out) are not names.
    assert unmatched_name_terms("HR IT", _SHOWN) == []


def test_possessive_is_dropped_before_matching() -> None:
    assert unmatched_name_terms("What is Amir's role?", _SHOWN) == []
    assert unmatched_name_terms("Adeyemi's manager", _SHOWN) == ["Adeyemi"]
    assert unmatched_name_terms("Adeyemi’s manager", _SHOWN) == ["Adeyemi"]


def test_names_are_deduplicated_in_query_order() -> None:
    assert unmatched_name_terms("Yusuf Adeyemi yusuf ADEYEMI Zora", _SHOWN) == [
        "Yusuf",
        "Adeyemi",
        "Zora",
    ]


def test_hyphenated_and_non_ascii_names() -> None:
    assert unmatched_name_terms("Mary-Jane O'Neill", _SHOWN) == ["Mary-Jane", "O'Neill"]
    assert unmatched_name_terms("Émilie Brontë", _SHOWN) == ["Émilie", "Brontë"]
    assert unmatched_name_terms("Émilie", "# émilie\n") == []


def test_empty_inputs() -> None:
    assert unmatched_name_terms("", _SHOWN) == []
    assert unmatched_name_terms("Adeyemi", "") == ["Adeyemi"]


# (query, shown text) pairs in the shape of the stored grid rows. The
# answerable ones are the classes the issue requires the mechanism not to
# hurt: none of them may fire.
_GRID_ANSWERABLE = [
    ("Amir Osei finance partner approval", "Amir Osei. Amir Osei is the finance partner."),
    ("Rowan pricing", "Rowan Wrenfield owns pricing. Rowan Deverell is a contract designer."),
    ("Bramfield retainer renewal", "Bramfield Retainer. The Bramfield retainer renews."),
]
_GRID_ABSTENTION = [
    ("Yusuf Adeyemi", "Amir Osei. Gabriel Orsini. Tovah Wrenfield. Rowan Deverell."),
    ("Harrowgate", "Bramfield Retainer. Corvale Holdings. Fenwick Partners."),
]


@pytest.mark.parametrize(("query", "shown"), _GRID_ANSWERABLE)
def test_answerable_grid_queries_do_not_fire(query: str, shown: str) -> None:
    assert unmatched_name_terms(query, shown) == []


@pytest.mark.parametrize(("query", "shown"), _GRID_ABSTENTION)
def test_abstention_grid_queries_fire(query: str, shown: str) -> None:
    assert unmatched_name_terms(query, shown)


# ---------------------------------------------------------------------------
# render_unmatched_names_line
# ---------------------------------------------------------------------------


def test_render_is_empty_when_nothing_is_unmatched() -> None:
    assert render_unmatched_names_line([]) == ""


def test_render_quotes_and_joins_the_names() -> None:
    line = render_unmatched_names_line(["Yusuf", "Adeyemi"])
    assert '"Yusuf" or "Adeyemi"' in line
    assert "\n" not in line
    assert not line.startswith("-")
    assert "{names}" not in line


def test_template_is_packaged_with_one_placeholder() -> None:
    template = load_unmatched_names_template()
    assert UNMATCHED_NAMES_TEMPLATE_NAME.endswith(".md")
    assert template.count("{names}") == 1


# ---------------------------------------------------------------------------
# recall_search wiring
# ---------------------------------------------------------------------------


def _write_page(wiki: Path, filename: str, *, name: str, body: str) -> None:
    (wiki / filename).write_text(
        f"---\nname: {name}\ntype: person\ntags: [person]\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture()
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    _write_page(
        root,
        "person-amir-osei.md",
        name="Amir Osei",
        body="Amir Osei is the finance partner, responsible for spend approval.",
    )
    _write_page(
        root,
        "person-gabriel-orsini.md",
        name="Gabriel Orsini",
        body="Gabriel Orsini is responsible for the analytics models.",
    )
    return root


def test_recall_appends_the_line_when_the_named_person_is_on_no_page(wiki: Path) -> None:
    text = recall_search(wiki, "Yusuf Adeyemi responsible", search_backend="keyword")
    assert text.startswith("Found ")
    assert text.rstrip().endswith(render_unmatched_names_line(["Yusuf", "Adeyemi"]))


def test_recall_adds_nothing_when_the_named_person_is_shown(wiki: Path) -> None:
    text = recall_search(wiki, "Amir Osei responsible", search_backend="keyword")
    assert "Amir Osei" in text
    assert "No page shown above mentions" not in text


def test_recall_adds_nothing_for_a_lowercase_topic_query(wiki: Path) -> None:
    text = recall_search(wiki, "who is responsible for analytics", search_backend="keyword")
    assert text.startswith("Found ")
    assert "No page shown above mentions" not in text


def test_unprompted_push_path_never_carries_the_line(wiki: Path) -> None:
    text = recall_search(
        wiki, "Yusuf Adeyemi responsible", search_backend="keyword", unprompted=True
    )
    assert "No page shown above mentions" not in text


def test_no_hit_result_is_unchanged(wiki: Path) -> None:
    """With no hits the existing "no pages matched" message already says
    the store has nothing; it gets no second line."""
    text = recall_search(wiki, "Zanzibar", search_backend="keyword")
    assert text.startswith("No wiki pages matched")
    assert "No page shown above mentions" not in text
