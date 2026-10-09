# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2043: merge anchors survive a hard line wrap in the page body.

The ``person_hint`` live eval read ``write_merge:dropped`` on every run for
the two cases whose claim must REPLACE a sentence the fixture page wraps at
~80 columns (``role_change_note``, and Vantry in
``memo_names_four_asserts_two``). The model quotes the sentence as prose;
the body carries a ``\\n`` inside it; ``apply_merge_ops`` used a bare
``str.find`` and reported ``anchor not found``; and a hint-derived action
that needs the full-echo fallback is dropped by design (athenaeum#1866).

These tests pin the resolution order of :func:`athenaeum.tiers._locate_anchor`
through the public applier: exact match first, and a whitespace-tolerant
match ONLY when the exact form is absent — never widening an exact match's
uniqueness check, never matching an all-whitespace anchor anywhere.
"""

from __future__ import annotations

import pytest

from athenaeum.tiers import MergeOpsError, _coerce_merge_ops, apply_merge_ops

# The real Vantry fixture page shape (tests/evals/data/person_hint/wiki):
# one sentence broken across two lines by the author's editor.
_WRAPPED = (
    "# Corwin Vantry\n\n"
    "Corwin Vantry buys refractory for the practice and keeps the supplier list. He\n"
    "has no signing authority of his own and routes every order through the works\n"
    "manager.\n"
)


class TestWrappedAnchorReplace:
    def test_replace_across_one_line_wrap_consumes_the_wrapped_span(self) -> None:
        ops = [
            {
                "op": "replace",
                "anchor": (
                    "has no signing authority of his own and routes every order "
                    "through the works manager."
                ),
                "text": (
                    "holds signing authority for refractory orders up to the "
                    "programme's standing budget line.[^1]"
                ),
            }
        ]
        out = apply_merge_ops(_WRAPPED, ops)
        assert "no signing authority" not in out
        assert "routes every order" not in out
        assert out.endswith("standing budget line.[^1]\n")
        # The untouched prefix is byte-identical, including its own wrap.
        assert out.startswith(
            "# Corwin Vantry\n\n"
            "Corwin Vantry buys refractory for the practice and keeps the supplier list. He\n"
        )

    def test_insert_after_lands_after_the_wrapped_anchor(self) -> None:
        ops = [
            {
                "op": "insert_after",
                "anchor": "routes every order through the works manager.",
                "text": " Since the review he signs up to the budget line.[^1]",
            }
        ]
        out = apply_merge_ops(_WRAPPED, ops)
        assert (
            "routes every order through the works\nmanager. Since the review he signs "
            "up to the budget line.[^1]\n"
        ) in out

    def test_anchor_may_span_more_than_one_wrap(self) -> None:
        anchor = (
            "Corwin Vantry buys refractory for the practice and keeps the supplier "
            "list. He has no signing authority of his own and routes every order "
            "through the works manager."
        )
        out = apply_merge_ops(
            _WRAPPED, [{"op": "replace", "anchor": anchor, "text": "Rewritten.[^1]"}]
        )
        assert out == "# Corwin Vantry\n\nRewritten.[^1]\n"

    def test_runs_of_spaces_in_the_anchor_also_match_a_wrap(self) -> None:
        # A model that double-spaces after a full stop still finds the span.
        anchor = "keeps the supplier list.  He has no signing authority"
        out = apply_merge_ops(_WRAPPED, [{"op": "replace", "anchor": anchor, "text": "X"}])
        assert "X of his own" in out


class TestFallbackNeverWidensTheContract:
    def test_exact_match_is_preferred_over_a_wrapped_candidate(self) -> None:
        # The exact form occurs once; a tolerant scan would find a second
        # (wrapped) occurrence. Exact wins, and the result is unambiguous.
        body = "alpha beta\n\nalpha\nbeta\n"
        out = apply_merge_ops(body, [{"op": "replace", "anchor": "alpha beta", "text": "X"}])
        assert out == "X\n\nalpha\nbeta\n"

    def test_exact_duplicate_still_raises_even_if_wrapping_would_disambiguate(
        self,
    ) -> None:
        with pytest.raises(MergeOpsError, match="not unique"):
            apply_merge_ops("aa aa", [{"op": "replace", "anchor": "aa", "text": "x"}])

    def test_wrapped_duplicate_raises_not_unique(self) -> None:
        body = "alpha\nbeta\n\nalpha\nbeta\n"
        with pytest.raises(MergeOpsError, match="not unique"):
            apply_merge_ops(body, [{"op": "replace", "anchor": "alpha beta", "text": "X"}])

    def test_absent_anchor_still_raises_not_found(self) -> None:
        with pytest.raises(MergeOpsError, match="not found"):
            apply_merge_ops(_WRAPPED, [{"op": "replace", "anchor": "NOPE", "text": "x"}])

    def test_whitespace_only_anchor_is_not_found_not_everywhere(self) -> None:
        with pytest.raises(MergeOpsError, match="not found"):
            apply_merge_ops(_WRAPPED, [{"op": "replace", "anchor": " \n ", "text": "x"}])

    def test_tokens_must_appear_in_order_and_adjacent(self) -> None:
        # "manager works" is not a reordering-tolerant match of "works\nmanager".
        with pytest.raises(MergeOpsError, match="not found"):
            apply_merge_ops(_WRAPPED, [{"op": "replace", "anchor": "manager works", "text": "x"}])

    def test_regex_metacharacters_in_the_anchor_are_literal(self) -> None:
        body = "Stage: Series B (closed).\nNext: C+ round.\n"
        out = apply_merge_ops(
            body,
            [{"op": "replace", "anchor": "B (closed). Next: C+", "text": "B. Next: D"}],
        )
        assert out == "Stage: Series B. Next: D round.\n"


class TestTopLevelFootnotesKeyIsFoldedIntoAnAppend:
    """A recorded live response (athenaeum#2043, run 37930589001) carried its
    footnote definitions under ``"footnotes"`` instead of an op, so the page
    gained ``[^1]`` markers with no definition and lost its source pointer."""

    def test_footnote_definitions_become_a_trailing_append_section(self) -> None:
        obj = {
            "ops": [{"op": "insert_after", "anchor": "works.", "text": " New.[^1]"}],
            "adds_new_claim": True,
            "footnotes": ["[^1]: sessions/20260701T120000Z-same_nam.md"],
        }
        ops = _coerce_merge_ops(obj)
        assert ops is not None
        assert ops[-1] == {
            "op": "append_section",
            "text": "[^1]: sessions/20260701T120000Z-same_nam.md",
        }
        out = apply_merge_ops("Quoted from the works.\n", ops)
        assert "[^1]: sessions/20260701T120000Z-same_nam.md" in out

    def test_definition_already_in_an_op_is_not_duplicated(self) -> None:
        obj = {
            "ops": [
                {"op": "insert_after", "anchor": "a", "text": " b[^1]"},
                {"op": "append_section", "text": "[^1]: sessions/x.md"},
            ],
            "footnotes": ["[^1]: sessions/x.md"],
        }
        ops = _coerce_merge_ops(obj)
        assert ops is not None
        assert len(ops) == 2

    def test_non_definition_entries_are_ignored(self) -> None:
        obj = {"ops": [], "footnotes": ["not a footnote", 3, "[^2]:   "]}
        assert _coerce_merge_ops(obj) == []

    def test_footnotes_without_any_ops_field_is_still_a_shape_failure(self) -> None:
        assert _coerce_merge_ops({"footnotes": ["[^1]: s.md"]}) is None
