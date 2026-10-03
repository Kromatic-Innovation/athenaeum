# SPDX-License-Identifier: Apache-2.0
"""List-item and footnote-label hygiene in the patch-mode merge write path.

Issue athenaeum#1942. The 2026-10-02 read-only dry run on a real aggregate
entity page found its body in a corrupted shape: ~271 footnote-cited subject
clauses packed into two run-on markdown list items, text fragments interleaved
mid-sentence, and 16 footnote labels reused across unrelated subjects.

The write path that produces that shape is
:func:`athenaeum.tiers.apply_merge_ops` — the deterministic applier for the
anchored patch-mode merge contract (``replace`` / ``insert_after`` /
``append_section``), reached from both the synchronous tier-3 merge
(:func:`athenaeum.tiers.tier3_merge`) and the batch transport's finalize step.
Two properties of the pre-athenaeum#1942 applier produce exactly that
signature, and this module pins both:

1. ``insert_after`` was a raw character splice at ``anchor_end``. A model that
   anchors on a list item's own text — which the merge prompt actively asks it
   to do, copying "the smallest verbatim snippet" — and returns a new clause
   with no leading newline has that clause *concatenated onto the existing
   list item* rather than added as a sibling. Repeat nightly and one bullet
   accumulates hundreds of clauses.

2. The applier never allocated footnote labels. The model writes ``[^N]`` in
   its own op text while seeing only a SELECTED WINDOW of the page
   (``_select_merge_section``, athenaeum#1181), so it cannot see which labels
   the rest of the page already spends and numbers from ``[^1]`` every time.
   Nothing downstream renumbered them, so every merge's citation collided with
   the last one's — provenance for every fact on the page becomes ambiguous.

:func:`athenaeum.tiers._append_source_citation` already had the right
allocation discipline for the citation-only path (lowest unused numeric
label); athenaeum#1942 gives the op applier the same one.

The three-merge fixture below is the reproduction the issue asks for: a page
already carrying a list item, three successive merges, each landing one new
cited fact — the minimal shape in which both defects are visible at once.
"""

from __future__ import annotations

import re

from athenaeum.tiers import apply_merge_ops

#: An inline footnote REFERENCE (``[^2]``), never a definition (``[^2]:``).
#: The negative lookahead on ``:`` is the only thing that tells them apart.
_REF_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")

#: A footnote DEFINITION line (``[^2]: ...``).
_DEF_RE = re.compile(r"^\[\^([^\]\s]+)\]:", re.MULTILINE)

#: A markdown list-item line, capturing indent and marker.
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s")


def _list_items(body: str) -> list[str]:
    """Every list item in *body*, each folded to one string.

    An item runs from its marker line through any following indented
    continuation lines, so a clause glued onto an item by a mid-item splice
    is counted as part of that item rather than as a line of its own.
    """
    items: list[str] = []
    current: list[str] | None = None
    for line in body.splitlines():
        if _LIST_ITEM_RE.match(line):
            if current is not None:
                items.append("\n".join(current))
            current = [line]
            continue
        if current is not None and line.strip() and line[:1].isspace():
            current.append(line)
            continue
        if current is not None:
            items.append("\n".join(current))
            current = None
    if current is not None:
        items.append("\n".join(current))
    return items


def _multi_cited_items(body: str) -> list[str]:
    """List items carrying more than one footnote reference."""
    return [item for item in _list_items(body) if len(_REF_RE.findall(item)) > 1]


def _reused_labels(body: str) -> list[str]:
    """Labels with more than one definition on the page."""
    labels = _DEF_RE.findall(body)
    return sorted({label for label in labels if labels.count(label) > 1})


def _dangling_refs(body: str) -> set[str]:
    return set(_REF_RE.findall(body)) - set(_DEF_RE.findall(body))


def _orphan_defs(body: str) -> set[str]:
    return set(_DEF_RE.findall(body)) - set(_REF_RE.findall(body))


#: The page as it stands before the first merge: one cited fact, one list item.
_PAGE = (
    "# Sales pipeline tool\n"
    "\n"
    "## Observations\n"
    "\n"
    "- Piloted with two design partners.[^1]\n"
    "\n"
    "[^1]: raw/sessions/2026-09-01-a.md\n"
)

#: Three successive merges, each the op set a windowed patch-mode call
#: actually returns: one ``insert_after`` anchored on text inside the list
#: item it can see, carrying a new clause with NO leading newline, plus the
#: ``[^1]`` definition it numbered from scratch because the footnote block was
#: outside its window.
_MERGES: list[tuple[str, list[dict[str, object]]]] = [
    (
        "Priced per seat.",
        [
            {
                "op": "insert_after",
                "anchor": "two design partners.[^1]",
                "text": " Priced per seat.[^1]",
            },
            {"op": "append_section", "text": "[^1]: raw/sessions/2026-09-14-b.md"},
        ],
    ),
    (
        "Onboarding takes two weeks.",
        [
            {
                "op": "insert_after",
                "anchor": "Piloted with two design partners.",
                "text": " Onboarding takes two weeks.[^1]",
            },
            {"op": "append_section", "text": "[^1]: raw/sessions/2026-09-21-c.md"},
        ],
    ),
    (
        "Renewal lands in March.",
        [
            {
                "op": "insert_after",
                "anchor": "- Piloted",
                "text": " Renewal lands in March.[^1]",
            },
            {"op": "append_section", "text": "[^1]: raw/sessions/2026-09-28-d.md"},
        ],
    ),
]


def _merge_three_facts() -> str:
    body = _PAGE
    for _, ops in _MERGES:
        body = apply_merge_ops(body, ops)
    return body


class TestThreeMergesIntoAPageWithAListItem:
    """The athenaeum#1942 reproduction, and the invariants that close it."""

    def test_every_fact_survives_the_three_merges(self) -> None:
        """Control. The fix must not buy hygiene by dropping a claim."""
        body = _merge_three_facts()
        assert "Piloted with two design partners." in body
        for fact, _ in _MERGES:
            assert fact in body, fact

    def test_each_fact_lands_in_its_own_list_item(self) -> None:
        """AC2: a merge into an existing page creates a NEW list item per fact.

        Pre-fix this fails with one run-on item carrying four references —
        the two-items-holding-271-clauses shape at fixture scale.
        """
        body = _merge_three_facts()
        assert _multi_cited_items(body) == []
        assert len(_list_items(body)) == 1 + len(_MERGES)

    def test_no_clause_is_spliced_inside_an_existing_item(self) -> None:
        """AC2: no mid-item insertion.

        The pre-existing item must still end exactly where it ended, rather
        than having later clauses interleaved into the middle of its sentence.
        """
        body = _merge_three_facts()
        original = [
            item
            for item in _list_items(body)
            if "Piloted with two design partners." in item
        ]
        assert original == ["- Piloted with two design partners.[^1]"]

    def test_each_merge_allocates_a_fresh_footnote_label(self) -> None:
        """AC2: no label reuse.

        Pre-fix all four citations are ``[^1]``, so three of the four
        definitions are unreachable and every fact's provenance is ambiguous.
        """
        body = _merge_three_facts()
        assert _reused_labels(body) == []
        assert len(_DEF_RE.findall(body)) == 1 + len(_MERGES)
        assert len(set(_DEF_RE.findall(body))) == 1 + len(_MERGES)

    def test_every_reference_resolves_and_no_definition_is_orphaned(self) -> None:
        body = _merge_three_facts()
        assert _dangling_refs(body) == set()
        assert _orphan_defs(body) == set()

    def test_each_fact_keeps_its_own_source(self) -> None:
        """Provenance is not merely unique — it is CORRECT.

        A renumbering that detached a fact from the source that reported it
        would satisfy every count above and still be wrong, so pin the pairing
        itself: each fact's reference must resolve to its own merge's source.
        """
        body = _merge_three_facts()
        definitions = {
            m.group(1): m.group(2)
            for m in re.finditer(r"^\[\^([^\]\s]+)\]:\s*(.+)$", body, re.MULTILINE)
        }
        expected = {
            "Piloted with two design partners.": "raw/sessions/2026-09-01-a.md",
            "Priced per seat.": "raw/sessions/2026-09-14-b.md",
            "Onboarding takes two weeks.": "raw/sessions/2026-09-21-c.md",
            "Renewal lands in March.": "raw/sessions/2026-09-28-d.md",
        }
        for item in _list_items(body):
            for fact, source in expected.items():
                if fact in item:
                    refs = _REF_RE.findall(item)
                    assert len(refs) == 1, item
                    assert definitions[refs[0]] == source, item
                    break
            else:  # pragma: no cover - a list item matching no known fact
                raise AssertionError(f"unexpected list item: {item!r}")


class TestInsertAfterListItemDiscipline:
    """``insert_after`` on a list-item line, op by op."""

    def test_clause_with_no_leading_newline_becomes_a_sibling_item(self) -> None:
        body = apply_merge_ops(
            "# T\n\n- Alpha.[^1]\n",
            [{"op": "insert_after", "anchor": "- Alpha.[^1]", "text": " Beta.[^2]"}],
        )
        assert body == "# T\n\n- Alpha.[^1]\n- Beta.[^2]\n"

    def test_anchor_ending_mid_item_does_not_splice_mid_sentence(self) -> None:
        body = apply_merge_ops(
            "# T\n\n- Alpha ships monthly.[^1]\n",
            [{"op": "insert_after", "anchor": "- Alpha", "text": " Beta.[^2]"}],
        )
        assert body == "# T\n\n- Alpha ships monthly.[^1]\n- Beta.[^2]\n"

    def test_a_text_that_is_already_a_sibling_item_is_left_alone(self) -> None:
        body = apply_merge_ops(
            "# T\n\n- Alpha.[^1]\n",
            [
                {
                    "op": "insert_after",
                    "anchor": "- Alpha.[^1]",
                    "text": "\n- Beta.[^2]",
                }
            ],
        )
        assert body == "# T\n\n- Alpha.[^1]\n- Beta.[^2]\n"

    def test_marker_style_and_indentation_are_inherited(self) -> None:
        body = apply_merge_ops(
            "# T\n\n  * Alpha.[^1]\n",
            [{"op": "insert_after", "anchor": "* Alpha.[^1]", "text": "Beta.[^2]"}],
        )
        assert body == "# T\n\n  * Alpha.[^1]\n  * Beta.[^2]\n"

    def test_an_items_continuation_lines_stay_with_their_item(self) -> None:
        body = apply_merge_ops(
            "# T\n\n- Alpha.[^1]\n  Still alpha.\n",
            [{"op": "insert_after", "anchor": "- Alpha.[^1]", "text": "Beta.[^2]"}],
        )
        assert body == "# T\n\n- Alpha.[^1]\n  Still alpha.\n- Beta.[^2]\n"

    def test_an_ordered_list_keeps_a_numeric_marker(self) -> None:
        body = apply_merge_ops(
            "# T\n\n1. Alpha.[^1]\n",
            [{"op": "insert_after", "anchor": "1. Alpha.[^1]", "text": "Beta.[^2]"}],
        )
        assert body == "# T\n\n1. Alpha.[^1]\n1. Beta.[^2]\n"

    def test_a_prose_line_still_takes_an_inline_amendment(self) -> None:
        """The discipline is scoped to list items, deliberately.

        An inline amendment to a PROSE line is a legitimate, intended use of
        ``insert_after`` (``tests/test_tiers.py`` pins it), and the shape the
        corrupted page exhibits is specifically list-item accumulation. This
        pins that the narrower rule did not widen.
        """
        body = apply_merge_ops(
            "# Acme\n\nHQ: SF.\n\nStage: Series B.",
            [{"op": "insert_after", "anchor": "HQ: SF.", "text": " (moved 2024)"}],
        )
        assert body == "# Acme\n\nHQ: SF. (moved 2024)\n\nStage: Series B."


class TestFootnoteLabelAllocation:
    """Labels an op batch DEFINES are allocated against the whole page."""

    def test_a_batch_defined_label_colliding_with_the_page_is_reallocated(
        self,
    ) -> None:
        body = apply_merge_ops(
            "# T\n\nAlpha.[^1]\n\n[^1]: src/a.md\n",
            [{"op": "append_section", "text": "Beta.[^1]\n\n[^1]: src/b.md"}],
        )
        assert body == "# T\n\nAlpha.[^1]\n\n[^1]: src/a.md\n\nBeta.[^2]\n\n[^2]: src/b.md"

    def test_two_ops_in_one_batch_defining_the_same_label_are_separated(self) -> None:
        body = apply_merge_ops(
            "# T\n\nAlpha.[^1]\n\n[^1]: src/a.md\n",
            [
                {"op": "append_section", "text": "Beta.[^1]\n\n[^1]: src/b.md"},
                {"op": "append_section", "text": "Gamma.[^1]\n\n[^1]: src/c.md"},
            ],
        )
        assert "[^2]: src/b.md" in body
        assert "[^3]: src/c.md" in body
        assert _reused_labels(body) == []

    def test_a_non_colliding_batch_label_is_left_as_written(self) -> None:
        """Minimal diff: nothing is renumbered that does not have to be."""
        body = apply_merge_ops(
            "# T\n\nAlpha.[^1]\n\n[^1]: src/a.md\n",
            [{"op": "append_section", "text": "Beta.[^2]\n\n[^2]: src/b.md"}],
        )
        assert body == "# T\n\nAlpha.[^1]\n\n[^1]: src/a.md\n\nBeta.[^2]\n\n[^2]: src/b.md"

    def test_a_label_the_batch_only_references_is_never_touched(self) -> None:
        """A reference with no definition in the batch points at the page's
        own existing definition — renumbering it would dangle it."""
        body = apply_merge_ops(
            "# T\n\nAlpha.[^1]\n\n[^1]: src/a.md\n",
            [{"op": "append_section", "text": "Also alpha.[^1]"}],
        )
        assert body == "# T\n\nAlpha.[^1]\n\n[^1]: src/a.md\n\nAlso alpha.[^1]"

    def test_a_replace_that_rewrites_a_definition_keeps_its_label(self) -> None:
        """The label is only 'in use' if it SURVIVES the batch.

        A ``replace`` consuming the page's own ``[^1]:`` definition line is
        re-stating that definition, not colliding with it — renumbering the
        replacement would leave the page's inline ``[^1]`` dangling.
        """
        body = apply_merge_ops(
            "# T\n\nAlpha.[^1]\n\n[^1]: src/a.md\n",
            [
                {
                    "op": "replace",
                    "anchor": "[^1]: src/a.md",
                    "text": "[^1]: src/a-corrected.md",
                }
            ],
        )
        assert body == "# T\n\nAlpha.[^1]\n\n[^1]: src/a-corrected.md\n"

    def test_a_non_numeric_page_label_is_still_counted_as_spent(self) -> None:
        """``attach_markers`` writes ``[^src-N]`` labels (athenaeum#1730).

        They are not numeric, so they never take a number away — but a batch
        that happens to define one of them must still be reallocated.
        """
        body = apply_merge_ops(
            "# T\n\nAlpha.[^src-1]\n\n[^src-1]: src/a.md\n",
            [{"op": "append_section", "text": "Beta.[^src-1]\n\n[^src-1]: src/b.md"}],
        )
        assert "[^1]: src/b.md" in body
        assert "Beta.[^1]" in body
        assert _reused_labels(body) == []

    def test_allocation_skips_numbers_already_spent_anywhere_on_the_page(
        self,
    ) -> None:
        body = apply_merge_ops(
            "# T\n\nAlpha.[^1] Gamma.[^7]\n\n[^1]: src/a.md\n\n[^7]: src/g.md\n",
            [{"op": "append_section", "text": "Beta.[^1]\n\n[^1]: src/b.md"}],
        )
        assert "[^8]: src/b.md" in body
        assert "Beta.[^8]" in body

    def test_an_inline_only_reference_on_the_page_still_blocks_its_number(
        self,
    ) -> None:
        """A label referenced but never defined is still spent: handing a new
        fact that number would silently adopt the page's dangling reference."""
        body = apply_merge_ops(
            "# T\n\nAlpha.[^1]\n",
            [{"op": "append_section", "text": "Beta.[^1]\n\n[^1]: src/b.md"}],
        )
        assert "[^2]: src/b.md" in body
        assert "Beta.[^2]" in body
