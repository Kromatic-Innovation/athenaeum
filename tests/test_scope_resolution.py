# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``scope`` dimension's read side (issue athenaeum#715).

:mod:`athenaeum.scope_resolution` is the read-side counterpart to
:func:`athenaeum.verdict_effects.write_refines_declaration`'s write side.
Organized as:

- ``TestInScopeFilter`` / ``TestMostSpecificSelection`` — the containment-only
  half (:func:`athenaeum.dimensions.hierarchy_contains` consumed via
  :func:`athenaeum.scope_resolution.resolve_most_specific`).
- ``TestRefinesEdges`` — the ``refines:`` half, including cycle safety.
- ``TestPartialOrderNoTieBreak`` — incomparable minima are ALL returned, and
  the resolver takes no scalar threshold anywhere.
- ``TestHierarchyContains`` — the new directional primitive in
  :mod:`athenaeum.dimensions`.
- ``TestRecallScopeAwareWiring`` — the dark, config-gated wiring into
  :func:`athenaeum.mcp_server.recall_search`.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import athenaeum.scope_resolution as sr_mod
from athenaeum.dimensions import hierarchy_contains
from athenaeum.mcp_server import recall_search
from athenaeum.models import slugify
from athenaeum.scope_resolution import ScopedClaim, resolve_most_specific
from athenaeum.verdict_effects import write_refines_declaration
from athenaeum.verdicts import page_id_for_path

# ---------------------------------------------------------------------------
# hierarchy_contains (athenaeum.dimensions)
# ---------------------------------------------------------------------------


class TestHierarchyContains:
    def test_ancestor_contains_descendant(self) -> None:
        assert hierarchy_contains("kromatic", "kromatic/platform") is True

    def test_descendant_does_not_contain_ancestor(self) -> None:
        assert hierarchy_contains("kromatic/platform", "kromatic") is False

    def test_equal_contains_itself(self) -> None:
        assert hierarchy_contains("kromatic/platform", "kromatic/platform") is True

    def test_siblings_do_not_contain(self) -> None:
        assert hierarchy_contains("kromatic/platform", "kromatic/marketing") is False

    def test_none_outer_contains_everything(self) -> None:
        assert hierarchy_contains(None, "kromatic/platform") is True
        assert hierarchy_contains(None, None) is True

    def test_non_none_outer_does_not_contain_none_inner(self) -> None:
        assert hierarchy_contains("kromatic", None) is False

    def test_universal_marker_matches_none_semantics(self) -> None:
        assert hierarchy_contains("*", "kromatic/platform") is True
        assert hierarchy_contains("kromatic/platform", "*") is False
        assert hierarchy_contains("*", "*") is True

    def test_case_and_whitespace_normalization_matches_compare_hierarchy(self) -> None:
        """Same normalization :class:`TestCompareHierarchy` in
        ``test_dimensions.py`` already asserts for ``compare_hierarchy`` —
        both functions must agree since ``hierarchy_contains`` factors its
        normalization out of the same helper."""
        assert hierarchy_contains(" Kromatic/Platform ", "kromatic/platform/team") is True
        assert hierarchy_contains("kromatic/platform", " Kromatic/Platform ") is True


# ---------------------------------------------------------------------------
# In-scope filter
# ---------------------------------------------------------------------------


class TestInScopeFilter:
    def test_ancestor_scoped_claim_applies_to_descendant_query(self) -> None:
        claim = ScopedClaim(id="c1", claimed_scope="kromatic")
        result = resolve_most_specific([claim], "kromatic/platform")
        assert result == [claim]

    def test_descendant_scoped_claim_does_not_apply_to_ancestor_query(self) -> None:
        claim = ScopedClaim(id="c1", claimed_scope="kromatic/platform")
        result = resolve_most_specific([claim], "kromatic")
        assert result == []

    def test_unscoped_claim_always_applies(self) -> None:
        claim = ScopedClaim(id="c1", claimed_scope=None)
        assert resolve_most_specific([claim], "kromatic/platform") == [claim]
        assert resolve_most_specific([claim], "acme") == [claim]

    def test_sibling_scoped_claim_out_of_scope(self) -> None:
        claim = ScopedClaim(id="c1", claimed_scope="acme")
        assert resolve_most_specific([claim], "kromatic") == []


# ---------------------------------------------------------------------------
# Most-specific selection
# ---------------------------------------------------------------------------


class TestMostSpecificSelection:
    def test_only_most_specific_of_two_ancestor_claims_survives(self) -> None:
        general = ScopedClaim(id="general", claimed_scope="kromatic")
        specific = ScopedClaim(id="specific", claimed_scope="kromatic/platform")
        result = resolve_most_specific([general, specific], "kromatic/platform")
        assert result == [specific]

    def test_unscoped_claim_suppressed_by_more_specific_in_scope_claim(self) -> None:
        unscoped = ScopedClaim(id="unscoped", claimed_scope=None)
        specific = ScopedClaim(id="specific", claimed_scope="kromatic/platform")
        result = resolve_most_specific([unscoped, specific], "kromatic/platform")
        assert result == [specific]

    def test_unscoped_claim_returned_when_sole_in_scope_claim(self) -> None:
        unscoped = ScopedClaim(id="unscoped", claimed_scope=None)
        result = resolve_most_specific([unscoped], "kromatic/platform")
        assert result == [unscoped]

    def test_two_incomparable_in_scope_claims_both_returned(self) -> None:
        """The containment order is PARTIAL: two claims neither of which
        contains the other must both survive -- proves no total order was
        invented for the tie. Both claims are unscoped (universal), so both
        are in scope for the query and neither contains the other more
        tightly than the other -- the genuinely incomparable case."""
        a = ScopedClaim(id="a", claimed_scope=None)
        b = ScopedClaim(id="b", claimed_scope=None)
        result = resolve_most_specific([a, b], "kromatic")
        assert {c.id for c in result} == {"a", "b"}
        # Preserves caller's input order among the returned minima.
        assert result == [a, b]

    def test_input_order_preserved_among_returned_minima(self) -> None:
        c1 = ScopedClaim(id="c1", claimed_scope=None)
        c2 = ScopedClaim(id="c2", claimed_scope=None)
        c3 = ScopedClaim(id="c3", claimed_scope=None)
        result = resolve_most_specific([c3, c1, c2], "kromatic")
        assert [c.id for c in result] == ["c3", "c1", "c2"]


# ---------------------------------------------------------------------------
# refines: edges
# ---------------------------------------------------------------------------


class TestRefinesEdges:
    def test_refines_suppresses_general_claim_with_equal_scope(self) -> None:
        """The whole point of the criterion: a `refines:` edge suppresses
        the general claim even when the two claimed_scope coordinates are
        EQUAL -- containment alone would never suppress either side."""
        general = ScopedClaim(id="general", claimed_scope="kromatic")
        specific = ScopedClaim(id="specific", claimed_scope="kromatic", refines=("general",))
        result = resolve_most_specific([general, specific], "kromatic")
        assert result == [specific]

    def test_refines_suppresses_with_universal_marker_scope(self) -> None:
        """Both claims declare the explicit universal marker (``*``) as their
        scope -- containment alone treats that pair as EQUAL, same as the
        equal-scope test above, just via the other spelling of "applies
        everywhere". The explicit ``refines:`` edge still suppresses the
        general side."""
        general = ScopedClaim(id="general", claimed_scope="*")
        specific = ScopedClaim(id="specific", claimed_scope="*", refines=("general",))
        result = resolve_most_specific([general, specific], "kromatic/anything")
        assert result == [specific]

    def test_refines_entry_naming_absent_claim_is_ignored(self) -> None:
        specific = ScopedClaim(id="specific", claimed_scope="kromatic", refines=("ghost",))
        result = resolve_most_specific([specific], "kromatic")
        assert result == [specific]

    def test_refines_entry_naming_out_of_scope_claim_is_ignored(self) -> None:
        out_of_scope_general = ScopedClaim(id="general", claimed_scope="acme")
        specific = ScopedClaim(
            id="specific", claimed_scope="kromatic", refines=("general",)
        )
        # "general" (acme) is not in scope for query "kromatic" -- so it is
        # simply absent from the result, and the edge naming it suppresses
        # nothing (there's nothing in-scope for it to suppress).
        result = resolve_most_specific([out_of_scope_general, specific], "kromatic")
        assert result == [specific]

    def test_refines_cycle_terminates_and_suppresses_nothing_among_members(self) -> None:
        a = ScopedClaim(id="a", claimed_scope="kromatic", refines=("b",))
        b = ScopedClaim(id="b", claimed_scope="kromatic", refines=("a",))
        result = resolve_most_specific([a, b], "kromatic")
        # Neither the direct cycle should crash, hang, nor suppress either
        # member via the cyclic edges.
        assert {c.id for c in result} == {"a", "b"}

    def test_refines_transitive_cycle_terminates(self) -> None:
        a = ScopedClaim(id="a", claimed_scope="kromatic", refines=("b",))
        b = ScopedClaim(id="b", claimed_scope="kromatic", refines=("c",))
        c = ScopedClaim(id="c", claimed_scope="kromatic", refines=("a",))
        result = resolve_most_specific([a, b, c], "kromatic")
        assert {claim.id for claim in result} == {"a", "b", "c"}

    def test_refines_override_mapping_takes_precedence_over_claim_attribute(self) -> None:
        general = ScopedClaim(id="general", claimed_scope="kromatic")
        specific = ScopedClaim(id="specific", claimed_scope="kromatic")  # no .refines set
        result = resolve_most_specific(
            [general, specific], "kromatic", refines={"specific": ("general",)}
        )
        assert result == [specific]

    def test_self_refines_entry_is_a_no_op(self) -> None:
        claim = ScopedClaim(id="c1", claimed_scope="kromatic", refines=("c1",))
        result = resolve_most_specific([claim], "kromatic")
        assert result == [claim]


# ---------------------------------------------------------------------------
# Partial order / no scalar gate
# ---------------------------------------------------------------------------


class TestPartialOrderNoTieBreak:
    def test_resolver_signature_has_no_threshold_or_confidence_parameter(self) -> None:
        sig = inspect.signature(resolve_most_specific)
        param_names = set(sig.parameters)
        assert not any(
            "threshold" in name.lower() or "confidence" in name.lower()
            for name in param_names
        )

    def test_module_exposes_no_threshold_or_confidence_constant(self) -> None:
        suspicious = [
            name
            for name in dir(sr_mod)
            if ("THRESHOLD" in name.upper() or "CONFIDENCE" in name.upper())
        ]
        assert suspicious == []

    def test_empty_claims_returns_empty(self) -> None:
        assert resolve_most_specific([], "kromatic") == []


# ---------------------------------------------------------------------------
# Recall wiring (issue athenaeum#715, dark by default)
# ---------------------------------------------------------------------------


class TestRecallScopeAwareWiring:
    def _wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        return wiki

    def _write_pair(self, wiki: Path) -> None:
        (wiki / "general.md").write_text(
            "---\nname: General widget policy\nclaimed_scope: kromatic\n---\n\n"
            "widget policy applies broadly across the org\n"
        )
        (wiki / "specific.md").write_text(
            "---\nname: Platform widget policy\nclaimed_scope: kromatic/platform\n---\n\n"
            "widget policy platform specific exception applies\n"
        )

    def test_flag_off_is_byte_identical_with_and_without_claimed_scope(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_SCOPE_AWARE_RECALL_ENABLED", raising=False)
        wiki = self._wiki(tmp_path)
        self._write_pair(wiki)
        without_scope = recall_search(wiki, "widget policy", top_k=5)
        with_scope = recall_search(
            wiki, "widget policy", top_k=5, claimed_scope="kromatic/platform"
        )
        assert without_scope == with_scope

    def test_flag_on_no_scope_supplied_is_unchanged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SCOPE_AWARE_RECALL_ENABLED", "true")
        wiki = self._wiki(tmp_path)
        self._write_pair(wiki)
        without_scope = recall_search(wiki, "widget policy", top_k=5)
        with_no_scope_kwarg = recall_search(wiki, "widget policy", top_k=5, claimed_scope=None)
        assert without_scope == with_no_scope_kwarg

    def test_flag_on_and_scope_supplied_drops_general_in_favor_of_specific(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SCOPE_AWARE_RECALL_ENABLED", "true")
        wiki = self._wiki(tmp_path)
        self._write_pair(wiki)
        result = recall_search(
            wiki, "widget policy", top_k=5, claimed_scope="kromatic/platform"
        )
        assert "Platform widget policy" in result
        assert "General widget policy" not in result

    def test_flag_on_scope_at_general_level_keeps_only_the_containing_claim(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SCOPE_AWARE_RECALL_ENABLED", "true")
        wiki = self._wiki(tmp_path)
        self._write_pair(wiki)
        # Query scope "kromatic" is NOT contained by "kromatic/platform"'s
        # region, so the platform-specific claim drops out of scope entirely
        # and only the general (org-wide) claim remains.
        result = recall_search(wiki, "widget policy", top_k=5, claimed_scope="kromatic")
        assert "General widget policy" in result
        assert "Platform widget policy" not in result


# ---------------------------------------------------------------------------
# refines: through recall, real writer -> real reader (regression: the two
# modules' id spaces -- filename vs. slug -- must be translated at the
# `_recall_via_backend` boundary, not compared directly).
# ---------------------------------------------------------------------------


class TestRefinesEdgeThroughRecall:
    """`ScopedClaim.id` (inside `_recall_via_backend`) is a recall hit
    `filename` (e.g. ``platform-widget-policy.md``); a `refines:`
    frontmatter entry is a bare SLUG, written by
    :func:`athenaeum.verdict_effects.write_refines_declaration` via
    :func:`athenaeum.verdicts.page_id_for_path` (=
    ``slugify(Path(path).stem)``). Comparing the two directly can never
    match -- every real edge falls into the (correct, separately tested)
    "names an absent claim, ignore it" branch. These tests exercise the
    REAL writer against the REAL reader through :func:`recall_search`
    itself, not the pure :func:`resolve_most_specific` function alone,
    which is id-space agnostic and would happily "pass" with mismatched
    but internally-consistent ids on both sides."""

    def _wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        return wiki

    def test_write_refines_declaration_slug_matches_recall_hit_id_translation(
        self, tmp_path: Path
    ) -> None:
        """Pins the two id spaces together directly: the slug
        ``write_refines_declaration`` writes for a page must equal what
        ``slugify(Path(filename).stem)`` computes for that SAME page's
        recall hit filename -- the exact translation ``_recall_via_backend``
        must perform for a ``refines:`` edge to ever resolve."""
        wiki = self._wiki(tmp_path)
        general_path = wiki / "general-widget-policy.md"
        general_path.write_text(
            "---\nname: General widget policy\n---\n\nwidget policy applies broadly\n"
        )
        general_id = page_id_for_path(general_path)
        recall_hit_filename = general_path.name  # what the search backend indexes it as
        assert slugify(Path(recall_hit_filename).stem) == general_id

    def test_refines_edge_suppresses_general_hit_through_recall(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        wiki = self._wiki(tmp_path)
        general_path = wiki / "general-widget-policy.md"
        specific_path = wiki / "platform-widget-policy.md"
        # Deliberately EQUAL claimed_scope on both pages: containment alone
        # (`hierarchy_contains`) leaves two equally-scoped claims as
        # incomparable minima -- neither strictly contains the other -- so
        # any suppression observed below can only come from the `refines:`
        # edge, isolating exactly the mechanism this regression covers.
        general_path.write_text(
            "---\nname: General widget policy\nclaimed_scope: kromatic\n---\n\n"
            "widget policy applies broadly across the org\n"
        )
        specific_path.write_text(
            "---\nname: Platform widget policy\nclaimed_scope: kromatic\n---\n\n"
            "widget policy platform specific exception applies\n"
        )
        # The REAL write side: exactly what the `specialization` verdict's
        # effect branch does.
        general_id = page_id_for_path(general_path)
        write_refines_declaration(specific_path, general_id)

        # Flag OFF: both hits present -- proves the two pages both genuinely
        # match the query and would both be returned absent the feature (so
        # the later "dropped" assertion isn't vacuous).
        monkeypatch.delenv("ATHENAEUM_SCOPE_AWARE_RECALL_ENABLED", raising=False)
        flag_off = recall_search(wiki, "widget policy", top_k=5, claimed_scope="kromatic")
        assert "General widget policy" in flag_off
        assert "Platform widget policy" in flag_off

        # Flag ON + a query scope: the refines: edge -- a bare slug, exactly
        # what write_refines_declaration produced -- must resolve against
        # the general hit's filename id and suppress it. With equal
        # claimed_scope on both sides, containment alone explains NOTHING
        # being dropped, so any suppression observed here can only be the
        # refines: edge actually firing (not the "absent target, ignore"
        # branch the id-space bug always fell into).
        monkeypatch.setenv("ATHENAEUM_SCOPE_AWARE_RECALL_ENABLED", "true")
        flag_on = recall_search(wiki, "widget policy", top_k=5, claimed_scope="kromatic")
        assert "Platform widget policy" in flag_on
        assert "General widget policy" not in flag_on
        # The whole point: flag-on must actually differ from flag-off. A
        # test passing identically either way would not have caught the
        # id-space bug.
        assert flag_on != flag_off
