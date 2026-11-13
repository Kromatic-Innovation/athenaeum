"""Tests for per-scope MEMORY.md index maintenance (issue athenaeum#388).

Covers the pure parsing primitives (:func:`index_line_target`,
:func:`rewrite_index`) that the retire pass uses inline to drop a
just-retired member's pointer in the same commit as its deletion.

The one-shot backfill over EXISTING dangling pointers
(:func:`build_dangling_report` / :func:`apply_prune_index`, exposed as
``auto-memory prune-index``) that used to be tested here was removed along
with the command once the backlog it existed to clear was gone (athenaeum#2466);
see :mod:`athenaeum.memory_index`'s own module docstring.

Acceptance:
  - only *dangling sibling* pointers are dropped; cross-tree links, URLs,
    anchors, prose and headings survive verbatim.
"""

from __future__ import annotations

from athenaeum.memory_index import index_line_target, rewrite_index


class TestIndexLineTarget:
    def test_bare_sibling_pointer(self) -> None:
        assert (
            index_line_target("- [Berlin](user_x_berlin.md) — hook")
            == "user_x_berlin.md"
        )

    def test_no_link_returns_none(self) -> None:
        assert index_line_target("# Memory Index") is None
        assert index_line_target("") is None
        assert index_line_target("- just prose, no link") is None

    def test_cross_tree_link_is_not_a_sibling_pointer(self) -> None:
        # A link into the wiki tree (or any path with a separator) is left
        # alone — retirement only removes SIBLING members.
        assert index_line_target("- [Wiki](../../wiki/auto-x.md) — hook") is None
        assert index_line_target("- [Sub](sub/dir/x.md)") is None

    def test_url_and_anchor_rejected(self) -> None:
        assert index_line_target("- [Ext](https://example.com/x.md)") is None
        assert index_line_target("- [Frag](user_x.md#section)") is None

    def test_first_link_on_line_wins(self) -> None:
        assert index_line_target("- [A](a.md) and [B](b.md)") == "a.md"


# ---------------------------------------------------------------------------
# rewrite_index — line-level drop preserving everything else
# ---------------------------------------------------------------------------


class TestRewriteIndex:
    def test_drops_only_matching_lines(self) -> None:
        text = (
            "# Memory Index\n"
            "- [Gone](gone.md) — dangling\n"
            "- [Keep](keep.md) — live\n"
        )
        new_text, dropped = rewrite_index(text, {"gone.md"}.__contains__)
        assert dropped == ["gone.md"]
        assert new_text == "# Memory Index\n- [Keep](keep.md) — live\n"

    def test_non_pointer_lines_preserved(self) -> None:
        text = "# Heading\n\nprose paragraph\n- [Keep](keep.md)\n"
        new_text, dropped = rewrite_index(text, lambda _t: True)
        assert dropped == ["keep.md"]
        # Heading, blank line and prose all survive; only the pointer went.
        assert new_text == "# Heading\n\nprose paragraph\n"

    def test_preserves_missing_trailing_newline(self) -> None:
        text = "- [Keep](keep.md)\n- [Gone](gone.md)"  # no trailing newline
        new_text, dropped = rewrite_index(text, {"gone.md"}.__contains__)
        assert dropped == ["gone.md"]
        assert new_text == "- [Keep](keep.md)\n"

    def test_nothing_dropped_returns_input_unchanged(self) -> None:
        text = "- [A](a.md)\n- [B](b.md)\n"
        new_text, dropped = rewrite_index(text, {"z.md"}.__contains__)
        assert dropped == []
        assert new_text == text


