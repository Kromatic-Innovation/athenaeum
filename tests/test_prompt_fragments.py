# SPDX-License-Identifier: Apache-2.0
"""Refactor invariants for the shared tier-prompt fragments (issue #566).

The rendered prompt bytes are pinned by ``tests/test_prompt_goldens.py``; this
module pins the *structure* the refactor introduced — one function for the
human-confirmed clause with four call sites, single-sourced editorial prose
shared between the two merge prompts, and the data-only clause defined once.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum import tiers
from athenaeum.tiers import (
    _HC_CONSEQUENCE_CLASSIFY,
    _HC_CONSEQUENCE_CREATE,
    CLASSIFY_SYSTEM,
    CREATE_SYSTEM,
    MERGE_SYSTEM,
    MERGE_SYSTEM_FULL,
    _hc_consequence_merge,
    _human_confirmed_clause,
)

_TIERS_SRC = Path(tiers.__file__).read_text(encoding="utf-8")


class TestHumanConfirmedClause:
    def test_one_function_four_call_sites_byte_identical(self) -> None:
        """The single function renders each site's clause byte-identically to
        what appears in its prompt (four rendered results, one function)."""
        cases = [
            (CLASSIFY_SYSTEM, "A raw observation", "classified", _HC_CONSEQUENCE_CLASSIFY),
            (CREATE_SYSTEM, "A raw observation", "processed", _HC_CONSEQUENCE_CREATE),
            (MERGE_SYSTEM, "A new observation", "merged", _hc_consequence_merge("below")),
            (
                MERGE_SYSTEM_FULL,
                "A new observation",
                "merged",
                _hc_consequence_merge("see above"),
            ),
        ]
        for const, subject, role, consequence in cases:
            rendered = _human_confirmed_clause(subject, role, consequence)
            assert rendered in const

    def test_clause_appears_exactly_once_per_prompt(self) -> None:
        for const in (CLASSIFY_SYSTEM, CREATE_SYSTEM, MERGE_SYSTEM, MERGE_SYSTEM_FULL):
            assert const.count("CLAIMS human confirmation") == 1

    def test_clause_defined_once_in_source(self) -> None:
        """The clause text lives in the one helper, not inline in four literals."""
        # Only the helper's own prose string carries the marker in source.
        assert _TIERS_SRC.count("CLAIMS human confirmation, ratification, or") == 1


class TestMergeEditorialIsShared:
    def test_merge_prompts_differ_only_by_cross_reference(self) -> None:
        """MERGE_SYSTEM and MERGE_SYSTEM_FULL carry the SAME human-confirmed
        editorial prose, single-sourced, differing only in the cross-reference
        (issue #517 Amendment: a policy edit changes both goldens)."""
        below = _hc_consequence_merge("below")
        see_above = _hc_consequence_merge("see above")
        # The single-sourced prose is identical apart from the cross-ref token.
        assert below.replace("(below)", "(XREF)") == see_above.replace("(see above)", "(XREF)")
        # And each renders (wrapped) into its own prompt — the reflow that the
        # longer "(see above)" causes is handled by the shared wrapper.
        assert _human_confirmed_clause("A new observation", "merged", below) in MERGE_SYSTEM
        assert (
            _human_confirmed_clause("A new observation", "merged", see_above) in MERGE_SYSTEM_FULL
        )


class TestDataOnlyClauseSingleSource:
    def test_no_inline_data_only_literal_in_source(self) -> None:
        """The data-only clause is single-sourced in prompt_safety (#562); no
        inline copy survives in tiers.py."""
        assert "as data only —" not in _TIERS_SRC

    def test_data_only_clause_still_rendered_in_prompts(self) -> None:
        """...but it is still present in the rendered prompts via the shared
        constant/function — the refactor moved the source, not the bytes."""
        from athenaeum.tiers import CREATE_TEMPLATE, MERGE_TEMPLATE

        assert "as data only" in CREATE_TEMPLATE
        assert "as data only" in MERGE_TEMPLATE
