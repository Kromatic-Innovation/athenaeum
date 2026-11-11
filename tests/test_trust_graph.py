# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum.trust_graph` and its config surface (issue
athenaeum#2330, TG2).

One class per acceptance criterion the issue names, mirroring the
`test_asserter_authority.py` convention. All offline, all pure functions
over already-parsed Python values -- `trust_graph.py` does no I/O.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any

import pytest

from athenaeum import config as config_mod
from athenaeum.precedence import SOURCE_PRECEDENCE_TIERS, source_rank
from athenaeum.provenance import parse_source
from athenaeum.trust_graph import (
    COMPARISON_RELATIONS,
    EQUAL,
    EVIDENCE_KINDS,
    INCOMPARABLE,
    OUTRANKED,
    OUTRANKS,
    UNKNOWN_EVIDENCE_KIND,
    ComparisonResult,
    SourceTrustEdge,
    TrustGraphConfigError,
    _Selector,
    compare_sources,
    find_contradictory_edges,
    parse_edge,
)

_ANY_DOMAIN: dict[str, str] = {
    "entity_type": "person",
    "field": "payout_amount",
    "evidence_kind": UNKNOWN_EVIDENCE_KIND,
}


def _domain(
    entity_type: str = "person",
    field: str = "payout_amount",
    evidence_kind: str = "document",
) -> dict[str, str]:
    return {"entity_type": entity_type, "field": field, "evidence_kind": evidence_kind}


# ---------------------------------------------------------------------------
# AC1: with edges=[] (or the flag off, which this slice's compare_sources has
# no parameter for -- the flag only gates a FUTURE caller's decision to call
# this function at all), compare_sources agrees with source_rank for every
# token pair in SOURCE_PRECEDENCE_TIERS. The test PARSES the tuple -- it
# does not transcribe the ladder by hand (failure class F3).
# ---------------------------------------------------------------------------


class TestAC1FallbackAgreesWithSourceRank:
    def _all_tokens(self) -> list[str]:
        """Parsed from `SOURCE_PRECEDENCE_TIERS` itself -- never hand-listed."""
        return [token for tier in SOURCE_PRECEDENCE_TIERS for token in tier]

    def test_parses_tiers_not_transcribed(self) -> None:
        """Guard against the F3 shape: prove this test reads the real
        tuple (>= 9 tiers, >= 11 tokens, matching test_precedence.py's own
        denominator) rather than a hand-copied list that would pass even
        if the ladder changed under it."""
        tokens = self._all_tokens()
        assert len(SOURCE_PRECEDENCE_TIERS) == 9
        assert len(tokens) >= 11

    @pytest.mark.parametrize("evidence_kind", [UNKNOWN_EVIDENCE_KIND, "document"])
    def test_every_pair_agrees_with_source_rank(self, evidence_kind: str) -> None:
        tokens = self._all_tokens()
        domain = _domain(evidence_kind=evidence_kind)
        mismatches = []
        for ta in tokens:
            for tb in tokens:
                a, b = f"{ta}:some-ref", f"{tb}:some-ref"
                result = compare_sources(a, b, domain, edges=())
                rank_a, rank_b = source_rank(a), source_rank(b)
                expected = OUTRANKS if rank_a < rank_b else OUTRANKED if rank_a > rank_b else EQUAL
                if result.relation != expected:
                    mismatches.append((ta, tb, result.relation, expected))
        assert not mismatches, mismatches

    def test_positive_control_a_broken_fallback_would_be_caught(self) -> None:
        """Prove the comparison above does real work: a deliberately wrong
        "always equal" stand-in disagrees with source_rank for a token pair
        the ladder actually separates, so the parametrized test above is not
        comparing a tautology."""
        a, b = "user:ref", "model-prior:ref"
        assert source_rank(a) != source_rank(b)
        broken_relation = EQUAL  # a broken fallback that always says "equal"
        real = compare_sources(a, b, _domain(evidence_kind=UNKNOWN_EVIDENCE_KIND), edges=())
        assert real.relation != broken_relation


# ---------------------------------------------------------------------------
# AC: contradictory edges in an overlapping domain yield `incomparable`,
# with a reason naming both edges.
# ---------------------------------------------------------------------------


class TestContradictoryEdgesIncomparable:
    def test_opposite_direction_edges_yield_incomparable_naming_both(self) -> None:
        edge_forward = parse_edge(
            {
                "domain": {"entity_type": "person", "field": "payout_amount"},
                "over": "api:govdb",
                "under": "user",
                "note": "forward",
            }
        )
        edge_backward = parse_edge(
            {
                "domain": {"entity_type": "person", "field": "payout_amount"},
                "over": "user",
                "under": "api:govdb",
                "note": "backward",
            }
        )
        result = compare_sources(
            "api:govdb", "user:tristan", _domain(evidence_kind="document"),
            edges=(edge_forward, edge_backward),
        )
        assert result.relation == INCOMPARABLE
        assert "edge[0]" in result.reason
        assert "edge[1]" in result.reason
        assert "forward" in result.reason
        assert "backward" in result.reason

    def test_find_contradictory_edges_detects_the_pair(self) -> None:
        edge_forward = parse_edge({"over": "api:govdb", "under": "user"})
        edge_backward = parse_edge({"over": "user", "under": "api:govdb"})
        assert find_contradictory_edges((edge_forward, edge_backward)) == [(0, 1)]

    def test_find_contradictory_edges_empty_when_domains_dont_overlap(self) -> None:
        edge_forward = parse_edge(
            {"domain": {"entity_type": "person"}, "over": "api:govdb", "under": "user"}
        )
        edge_backward = parse_edge(
            {"domain": {"entity_type": "company"}, "over": "user", "under": "api:govdb"}
        )
        assert find_contradictory_edges((edge_forward, edge_backward)) == []

    def test_non_opposite_edges_are_not_contradictory(self) -> None:
        edge_a = parse_edge({"over": "api:govdb", "under": "user"})
        edge_b = parse_edge({"over": "api:govdb", "under": "wikipedia"})
        assert find_contradictory_edges((edge_a, edge_b)) == []


# ---------------------------------------------------------------------------
# AC: no transitivity -- A over B and B over C do not make A over C.
# ---------------------------------------------------------------------------


class TestNoTransitivity:
    def test_a_over_b_and_b_over_c_implies_nothing_about_a_vs_c(self) -> None:
        # model-prior (tier 8) over claude (tier 6); claude (tier 6) over
        # user (tier 1). If (and only if) transitivity were wrongly applied,
        # model-prior would outrank user. Plain source_rank disagrees
        # (user, tier 1, outranks model-prior, tier 8) -- so this is a real
        # behavioural fork, not a vacuous check.
        edge_ab = parse_edge({"over": "model-prior:x", "under": "claude:x"})
        edge_bc = parse_edge({"over": "claude:x", "under": "user"})
        result = compare_sources(
            "model-prior:x", "user:tristan", _domain(evidence_kind=UNKNOWN_EVIDENCE_KIND),
            edges=(edge_ab, edge_bc),
        )
        # No direct edge relates model-prior and user, so the fallback
        # fires and agrees with plain source_rank -- the transitive chain
        # through "claude" is never consulted.
        assert result.relation == OUTRANKED
        assert "source_rank fallback" in result.reason
        assert source_rank("model-prior:x") > source_rank("user:tristan")


# ---------------------------------------------------------------------------
# AC: an `api` selector without a ref raises at config load.
# ---------------------------------------------------------------------------


class TestApiSelectorRequiresRef:
    def test_bare_api_selector_raises(self) -> None:
        with pytest.raises(TrustGraphConfigError, match="must name a ref"):
            parse_edge({"over": "api", "under": "user"})

    def test_bare_api_selector_as_under_raises(self) -> None:
        with pytest.raises(TrustGraphConfigError, match="must name a ref"):
            parse_edge({"over": "user", "under": "api"})

    def test_api_with_ref_is_fine(self) -> None:
        edge = parse_edge({"over": "api:govdb", "under": "user"})
        assert edge.over.type == "api"
        assert edge.over.ref == "govdb"

    def test_non_api_bare_type_selector_is_fine(self) -> None:
        edge = parse_edge({"over": "user", "under": "claude"})
        assert edge.over.ref is None
        assert edge.under.ref is None

    @pytest.mark.parametrize("bad", ["", "  ", "API:x", "api:", "has space", "trailing: "])
    def test_malformed_selector_strings_raise(self, bad: str) -> None:
        with pytest.raises(TrustGraphConfigError):
            parse_edge({"over": bad, "under": "user"})

    def test_config_load_propagates_the_raise(self) -> None:
        cfg = {"librarian": {"source_trust_edges": [{"over": "api", "under": "user"}]}}
        with pytest.raises(TrustGraphConfigError, match="must name a ref"):
            config_mod.resolve_source_trust_edges(cfg)


# ---------------------------------------------------------------------------
# AC: domain matching covers scalar, list and absent keys for each of
# `entity_type`, `field`, `evidence_kind`.
# ---------------------------------------------------------------------------


class TestDomainMatchingShapes:
    @pytest.mark.parametrize(
        "domain_raw,claim_value,should_match",
        [
            ({"entity_type": "person"}, "person", True),
            ({"entity_type": "person"}, "company", False),
            ({"entity_type": ["person", "company"]}, "company", True),
            ({"entity_type": ["person", "company"]}, "animal", False),
            ({}, "anything-at-all", True),  # absent -> any
        ],
    )
    def test_entity_type_scalar_list_absent(
        self, domain_raw: dict[str, Any], claim_value: str, should_match: bool
    ) -> None:
        edge = parse_edge({"domain": domain_raw, "over": "api:x", "under": "user"})
        assert (
            edge.domain.matches(claim_value, "any-field", "document") == should_match
        )

    @pytest.mark.parametrize(
        "domain_raw,claim_value,should_match",
        [
            ({"field": "payout_amount"}, "payout_amount", True),
            ({"field": "payout_amount"}, "salary", False),
            ({"field": ["payout_amount", "salary"]}, "salary", True),
            ({"field": ["payout_amount", "salary"]}, "title", False),
            ({}, "anything-at-all", True),
        ],
    )
    def test_field_scalar_list_absent(
        self, domain_raw: dict[str, Any], claim_value: str, should_match: bool
    ) -> None:
        edge = parse_edge({"domain": domain_raw, "over": "api:x", "under": "user"})
        assert edge.domain.matches("any-entity", claim_value, "document") == should_match

    @pytest.mark.parametrize(
        "domain_raw,claim_value,should_match",
        [
            ({"evidence_kind": "document"}, "document", True),
            ({"evidence_kind": "document"}, "verbal", False),
            ({"evidence_kind": ["document", "verbal"]}, "verbal", True),
            ({"evidence_kind": ["document", "verbal"]}, "authored-by-agent", False),
            ({}, "document", True),
            ({}, UNKNOWN_EVIDENCE_KIND, True),  # absent evidence_kind matches unknown too
        ],
    )
    def test_evidence_kind_scalar_list_absent(
        self, domain_raw: dict[str, Any], claim_value: str, should_match: bool
    ) -> None:
        edge = parse_edge({"domain": domain_raw, "over": "api:x", "under": "user"})
        assert edge.domain.matches("any-entity", "any-field", claim_value) == should_match

    def test_empty_list_domain_value_rejected(self) -> None:
        with pytest.raises(TrustGraphConfigError):
            parse_edge({"domain": {"entity_type": []}, "over": "api:x", "under": "user"})

    def test_non_string_non_list_domain_value_rejected(self) -> None:
        with pytest.raises(TrustGraphConfigError):
            parse_edge({"domain": {"entity_type": 42}, "over": "api:x", "under": "user"})


# ---------------------------------------------------------------------------
# AC: evidence-kind matching follows the TG1 doc, including the
# unknown-kind rule: known kind, different kind, unknown kind. An
# evidence_kind outside the vocabulary is rejected at config load.
# ---------------------------------------------------------------------------


class TestEvidenceKindMatchingRule:
    @pytest.fixture
    def doc_scoped_edge(self) -> SourceTrustEdge:
        return parse_edge(
            {
                "domain": {"evidence_kind": "document"},
                "over": "api:govdb",
                "under": "user",
            }
        )

    def test_known_matching_kind_lets_edge_apply(self, doc_scoped_edge: SourceTrustEdge) -> None:
        result = compare_sources(
            "api:govdb", "user:t", _domain(evidence_kind="document"), edges=(doc_scoped_edge,)
        )
        assert result.relation == OUTRANKS
        assert "edge[0]" in result.reason

    def test_different_known_kind_does_not_match(self, doc_scoped_edge: SourceTrustEdge) -> None:
        result = compare_sources(
            "api:govdb", "user:t", _domain(evidence_kind="verbal"), edges=(doc_scoped_edge,)
        )
        # No matching edge for "verbal" -> falls back to source_rank, which
        # ranks "user" (tier 1) above "api" (tier 3) -- the OPPOSITE of what
        # the document-scoped edge would have said, proving the edge did
        # not leak into the wrong evidence kind.
        assert result.relation == OUTRANKED
        assert "source_rank fallback" in result.reason

    def test_unknown_kind_does_not_match_a_kind_scoped_edge(
        self, doc_scoped_edge: SourceTrustEdge
    ) -> None:
        """The unknown-kind matching rule: an edge naming `document` must
        NOT match a claim whose evidence kind is `unknown`, even though the
        edge is otherwise applicable."""
        result = compare_sources(
            "api:govdb", "user:t", _domain(evidence_kind=UNKNOWN_EVIDENCE_KIND),
            edges=(doc_scoped_edge,),
        )
        assert result.relation == OUTRANKED
        assert "source_rank fallback" in result.reason

    def test_unknown_kind_claim_DOES_match_an_edge_with_no_evidence_kind(self) -> None:
        any_kind_edge = parse_edge(
            {"domain": {"entity_type": "person"}, "over": "api:govdb", "under": "user"}
        )
        result = compare_sources(
            "api:govdb", "user:t", _domain(evidence_kind=UNKNOWN_EVIDENCE_KIND),
            edges=(any_kind_edge,),
        )
        assert result.relation == OUTRANKS
        assert "edge[0]" in result.reason

    @pytest.mark.parametrize("bad_kind", ["unknown", "UNKNOWN", "doc", "notarealkind", ""])
    def test_out_of_vocabulary_evidence_kind_rejected_at_load(self, bad_kind: str) -> None:
        with pytest.raises(TrustGraphConfigError):
            parse_edge(
                {"domain": {"evidence_kind": bad_kind}, "over": "api:x", "under": "user"}
            )

    @pytest.mark.parametrize("good_kind", sorted(EVIDENCE_KINDS))
    def test_in_vocabulary_evidence_kind_accepted(self, good_kind: str) -> None:
        edge = parse_edge(
            {"domain": {"evidence_kind": good_kind}, "over": "api:x", "under": "user"}
        )
        assert edge.domain.evidence_kind == frozenset({good_kind})

    def test_vocabulary_is_exactly_three_members(self) -> None:
        assert EVIDENCE_KINDS == frozenset({"document", "verbal", "authored-by-agent"})


# ---------------------------------------------------------------------------
# AC: an edge with no `evidence_kind` behaves as an (entity type, field)
# edge and matches every evidence kind.
# ---------------------------------------------------------------------------


class TestEdgeWithNoEvidenceKindMatchesEveryKind:
    @pytest.mark.parametrize(
        "evidence_kind", [*sorted(EVIDENCE_KINDS), UNKNOWN_EVIDENCE_KIND]
    )
    def test_matches_every_kind_including_unknown(self, evidence_kind: str) -> None:
        edge = parse_edge(
            {
                "domain": {"entity_type": "person", "field": "payout_amount"},
                "over": "api:govdb",
                "under": "user",
            }
        )
        assert edge.domain.matches("person", "payout_amount", evidence_kind) is True


# ---------------------------------------------------------------------------
# AC: the load-time lint warns on overlapping contradictory edges.
# ---------------------------------------------------------------------------


class TestLoadTimeContradictionLint:
    def test_resolve_source_trust_edges_warns_on_contradiction(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = {
            "librarian": {
                "source_trust_edges": [
                    {"over": "api:govdb", "under": "user", "note": "fwd"},
                    {"over": "user", "under": "api:govdb", "note": "back"},
                ]
            }
        }
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            edges = config_mod.resolve_source_trust_edges(cfg)
        assert len(edges) == 2
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "edge[0]" in message and "edge[1]" in message
        assert "incomparable" in message

    def test_resolve_source_trust_edges_no_warning_without_contradiction(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = {
            "librarian": {
                "source_trust_edges": [
                    {"over": "api:govdb", "under": "user"},
                    {"over": "api:govdb", "under": "wikipedia"},
                ]
            }
        }
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            config_mod.resolve_source_trust_edges(cfg)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings


# ---------------------------------------------------------------------------
# docs/reference/configuration.md documents both keys -- covered by
# tests/test_generated_docs_parity.py and scripts/check_env_docs.py at the
# repo level (both run separately, per the work order); this module adds a
# narrow sanity check that the doc mentions both dotted keys by name.
# ---------------------------------------------------------------------------


class TestConfigResolvers:
    def test_source_trust_enabled_default_false(self) -> None:
        assert config_mod.resolve_source_trust_enabled(None) is False
        assert config_mod.resolve_source_trust_enabled({}) is False
        assert config_mod.resolve_source_trust_enabled({"librarian": {}}) is False

    def test_source_trust_enabled_reads_yaml(self) -> None:
        assert config_mod.resolve_source_trust_enabled(
            {"librarian": {"source_trust_enabled": True}}
        ) is True
        assert config_mod.resolve_source_trust_enabled(
            {"librarian": {"source_trust_enabled": False}}
        ) is False

    def test_source_trust_enabled_non_bool_falls_back(self) -> None:
        assert config_mod.resolve_source_trust_enabled(
            {"librarian": {"source_trust_enabled": "yes"}}
        ) is False

    def test_source_trust_edges_default_empty(self) -> None:
        assert config_mod.resolve_source_trust_edges(None) == ()
        assert config_mod.resolve_source_trust_edges({}) == ()
        assert config_mod.resolve_source_trust_edges({"librarian": {}}) == ()

    def test_source_trust_edges_non_list_defaults_empty(self) -> None:
        assert config_mod.resolve_source_trust_edges(
            {"librarian": {"source_trust_edges": "not-a-list"}}
        ) == ()

    def test_source_trust_edges_parses_real_entries(self) -> None:
        cfg = {
            "librarian": {
                "source_trust_edges": [
                    {
                        "domain": {
                            "entity_type": "person",
                            "field": "payout_amount",
                            "evidence_kind": "document",
                        },
                        "over": "api:govdb",
                        "under": "user",
                        "note": "official ledger beats self-report",
                    }
                ]
            }
        }
        edges = config_mod.resolve_source_trust_edges(cfg)
        assert len(edges) == 1
        edge = edges[0]
        assert isinstance(edge, SourceTrustEdge)
        assert edge.over.raw == "api:govdb"
        assert edge.under.raw == "user"
        assert edge.note == "official ledger beats self-report"

    def test_malformed_entry_raises_not_silently_dropped(self) -> None:
        with pytest.raises(TrustGraphConfigError):
            config_mod.resolve_source_trust_edges(
                {"librarian": {"source_trust_edges": ["not-a-dict"]}}
            )

    def test_documented_in_generated_configuration_reference(self) -> None:
        doc_path = (
            Path(__file__).resolve().parents[1] / "docs" / "reference" / "configuration.md"
        )
        text = doc_path.read_text()
        assert "librarian.source_trust_edges" in text
        assert "librarian.source_trust_enabled" in text


# ---------------------------------------------------------------------------
# AC: `trust_graph.py` imports nothing from `promptwitness` and performs no
# I/O. AC: no existing caller changes behaviour (no callers are added).
# ---------------------------------------------------------------------------


class TestLayeringAndNoCallers:
    def test_module_imports_nothing_from_promptwitness(self) -> None:
        """Checks the actual `import`/`from ... import` AST nodes -- not a
        raw substring scan, which would also match this module's own
        prose explaining that it imports nothing from promptwitness."""
        import ast

        from athenaeum import trust_graph

        tree = ast.parse(inspect.getsource(trust_graph))
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
        assert not any("promptwitness" in name for name in imported_modules)

    def test_module_imports_only_l1_siblings(self) -> None:
        from athenaeum import trust_graph

        source = inspect.getsource(trust_graph)
        athenaeum_imports = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import athenaeum", "from athenaeum"))
        ]
        assert athenaeum_imports == [
            "from athenaeum import precedence",
            "from athenaeum.provenance import parse_source",
        ]

    def test_no_other_src_module_imports_trust_graph_yet(self) -> None:
        """TG4/resolutions.py (the LLM-resolver rendering seam) remains
        later work and must not import this module yet. TG3
        (`corrections.py`, issue athenaeum#2331) wires the first real
        caller -- seams 1 and 2, tier-0 field corrections and tier-0 list
        removal -- so it is now expected alongside `config.py`'s existing
        lazy, function-local import. This test's job is unchanged: catch
        an UNEXPECTED new importer (in particular `resolutions.py`, which
        stays out of scope until TG4), not merely count mentions."""
        src_dir = Path(__file__).resolve().parents[1] / "src" / "athenaeum"
        importers = sorted(
            path.name
            for path in src_dir.glob("*.py")
            if path.name != "trust_graph.py" and "trust_graph" in path.read_text()
        )
        assert importers == ["config.py", "corrections.py"]


# ---------------------------------------------------------------------------
# Structural sanity on the comparison relations / result type themselves.
# ---------------------------------------------------------------------------


class TestComparisonRelationsShape:
    def test_four_outcomes(self) -> None:
        assert COMPARISON_RELATIONS == frozenset({OUTRANKS, OUTRANKED, EQUAL, INCOMPARABLE})

    def test_compare_sources_returns_named_tuple_with_reason(self) -> None:
        result = compare_sources("user:a", "user:a", _domain(evidence_kind=UNKNOWN_EVIDENCE_KIND))
        assert isinstance(result, ComparisonResult)
        assert result.relation in COMPARISON_RELATIONS
        assert isinstance(result.reason, str) and result.reason

    def test_missing_domain_key_raises(self) -> None:
        with pytest.raises(ValueError, match="entity_type"):
            compare_sources("user:a", "user:b", {"field": "x", "evidence_kind": "document"})

    def test_invalid_evidence_kind_value_raises(self) -> None:
        with pytest.raises(ValueError, match="evidence_kind"):
            compare_sources("user:a", "user:b", _domain(evidence_kind="not-a-real-kind"))

    def test_pure_backward_edge_match_outranked_without_fallback(self) -> None:
        """A backward-only match (an edge saying `b over a`, with no
        forward edge) must resolve via the EDGE, not the source_rank
        fallback -- pick a pair the ladder would otherwise call OUTRANKS
        for `a` so the edge is clearly doing the work."""
        edge = parse_edge({"over": "user", "under": "api:govdb", "note": "flip"})
        # Plain source_rank would say api (tier 3) is OUTRANKED by user
        # (tier 1) too, so also check the edge-specific reason fired.
        result = compare_sources(
            "api:govdb", "user:t", _domain(evidence_kind=UNKNOWN_EVIDENCE_KIND), edges=(edge,)
        )
        assert result.relation == OUTRANKED
        assert "edge[0]" in result.reason
        assert "source_rank fallback" not in result.reason

    def test_none_source_is_treated_as_unparseable(self) -> None:
        result = compare_sources(None, "user:a", _domain(evidence_kind=UNKNOWN_EVIDENCE_KIND))
        assert result.relation == OUTRANKED
        assert "source_rank fallback" in result.reason

    def test_unparseable_source_falls_back_to_unknown_rank(self) -> None:
        # "not a valid source" has no colon and isn't a legacy-retired slug
        # match either -- provenance.parse_source raises, and both this
        # module and precedence.source_rank treat that identically.
        result = compare_sources(
            "not a valid source", "user:a", _domain(evidence_kind=UNKNOWN_EVIDENCE_KIND)
        )
        assert result.relation == OUTRANKED
        assert "source_rank fallback" in result.reason


class TestSelectorParsing:
    def test_selector_without_colon_has_none_ref(self) -> None:
        edge = parse_edge({"over": "claude", "under": "user"})
        assert edge.over == _Selector(type="claude", ref=None, raw="claude")

    def test_selector_with_colon_splits_type_and_ref(self) -> None:
        edge = parse_edge({"over": "linkedin:nadia", "under": "user"})
        assert edge.over == _Selector(type="linkedin", ref="nadia", raw="linkedin:nadia")

    def test_selector_describe_includes_note(self) -> None:
        edge = parse_edge({"over": "api:x", "under": "user", "note": "why"})
        assert "why" in edge.describe()

    def test_non_dict_edge_entry_raises(self) -> None:
        with pytest.raises(TrustGraphConfigError):
            parse_edge("not-a-dict")

    def test_non_dict_domain_raises(self) -> None:
        with pytest.raises(TrustGraphConfigError):
            parse_edge({"domain": "not-a-dict", "over": "api:x", "under": "user"})

    def test_missing_under_raises(self) -> None:
        with pytest.raises(TrustGraphConfigError):
            parse_edge({"over": "api:x"})


class TestUnrecognizedKeysRejected:
    """Seer finding (PR athenaeum#2356): a misspelled domain key (`feild`,
    `evidence_kinds`) must not vanish silently and fall back to "any" --
    that would silently widen the scope of an edge the operator meant to
    constrain narrowly. Same treatment at the edge level."""

    @pytest.mark.parametrize(
        "bad_key", ["feild", "evidence_kinds", "entity", "Field", "evidence_kind "]
    )
    def test_misspelled_domain_key_raises(self, bad_key: str) -> None:
        with pytest.raises(TrustGraphConfigError, match="unrecognized key"):
            parse_edge(
                {"domain": {bad_key: "person"}, "over": "api:x", "under": "user"}
            )

    def test_misspelled_domain_key_names_the_offending_key(self) -> None:
        with pytest.raises(TrustGraphConfigError, match="feild"):
            parse_edge({"domain": {"feild": "payout_amount"}, "over": "api:x", "under": "user"})

    def test_valid_domain_keys_accepted_together(self) -> None:
        edge = parse_edge(
            {
                "domain": {
                    "entity_type": "person",
                    "field": "payout_amount",
                    "evidence_kind": "document",
                },
                "over": "api:x",
                "under": "user",
            }
        )
        assert edge.domain.entity_type == frozenset({"person"})

    @pytest.mark.parametrize("bad_key", ["source", "notes", "priority", "Over", "under_"])
    def test_misspelled_edge_level_key_raises(self, bad_key: str) -> None:
        with pytest.raises(TrustGraphConfigError, match="unrecognized key"):
            parse_edge({"over": "api:x", "under": "user", bad_key: "oops"})

    def test_misspelled_edge_level_key_names_the_offending_key(self) -> None:
        with pytest.raises(TrustGraphConfigError, match="notes"):
            parse_edge({"over": "api:x", "under": "user", "notes": "typo for note"})

    def test_all_four_valid_edge_keys_accepted_together(self) -> None:
        edge = parse_edge(
            {"domain": {}, "over": "api:x", "under": "user", "note": "fine"}
        )
        assert edge.note == "fine"

    def test_config_load_propagates_the_domain_key_raise(self) -> None:
        cfg = {
            "librarian": {
                "source_trust_edges": [
                    {"domain": {"evidence_kinds": "document"}, "over": "api:x", "under": "user"}
                ]
            }
        }
        with pytest.raises(TrustGraphConfigError, match="evidence_kinds"):
            config_mod.resolve_source_trust_edges(cfg)


class TestSelectorMatchesDatedRefs:
    """Seer finding (PR athenaeum#2356), adjudicated INVALID as a request
    for a behaviour change (see the PR thread reply for the full writeup):
    date-suffixed `api:` refs (`api:apollo:2026-05-07`) are real and common
    in the live corpus, but `provenance.parse_source` treats the ENTIRE
    remainder after the first colon as one opaque ref -- it has no notion
    under which `api:apollo` and `api:apollo:2026-05-07` are "the same
    source". `_Selector.matches` already compares the parsed (type, ref)
    the same way on both sides; these tests build both the selector and
    the claim from `parse_source`'s OWN output (never hand-typed strings)
    to prove exactly that boundary, both where it holds and where it
    (correctly) does not.
    """

    def test_selector_naming_the_full_dated_ref_matches_the_same_claim(self) -> None:
        claim_ref = parse_source("api:apollo:2026-05-07")
        assert claim_ref is not None
        selector_raw = f"{claim_ref.type}:{claim_ref.ref}"
        edge = parse_edge({"over": selector_raw, "under": "user"})
        # The selector string was built from parse_source's own (type, ref)
        # -- exactly "the same source" by parse_source's own normalization
        # -- and it DOES match.
        result = compare_sources(
            "api:apollo:2026-05-07",
            "user:t",
            _domain(evidence_kind=UNKNOWN_EVIDENCE_KIND),
            edges=(edge,),
        )
        assert result.relation == OUTRANKS
        assert "edge[0]" in result.reason

    def test_vendor_only_selector_does_not_match_a_dated_claim(self) -> None:
        dated_ref = parse_source("api:apollo:2026-05-07")
        bare_ref = parse_source("api:apollo")
        assert dated_ref is not None and bare_ref is not None
        # Two genuinely DIFFERENT refs by parse_source's own parsing --
        # "apollo" vs "apollo:2026-05-07" -- so a selector built from the
        # bare form is not "the same source" as the dated claim, and
        # correctly falls through to the source_rank ladder rather than
        # silently widening to a prefix match.
        assert bare_ref.ref != dated_ref.ref
        selector_raw = f"{bare_ref.type}:{bare_ref.ref}"
        edge = parse_edge({"over": selector_raw, "under": "user"})
        result = compare_sources(
            "api:apollo:2026-05-07",
            "user:t",
            _domain(evidence_kind=UNKNOWN_EVIDENCE_KIND),
            edges=(edge,),
        )
        assert "source_rank fallback" in result.reason

    def test_parse_source_treats_the_whole_remainder_after_first_colon_as_ref(
        self,
    ) -> None:
        """Documents the exact parsing fact the adjudication rests on: no
        third segment exists for `_Selector` to reason about separately --
        the date suffix is already inside `ref` as one opaque string."""
        ref = parse_source("api:apollo:2026-05-07")
        assert ref is not None
        assert ref.type == "api"
        assert ref.ref == "apollo:2026-05-07"
