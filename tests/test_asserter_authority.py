# SPDX-License-Identifier: Apache-2.0
"""Tests for the asserter-authority partial order (issue athenaeum#715).

One class per property the issue names as a hard constraint. All offline,
all pure functions over frontmatter dicts.
"""

from __future__ import annotations

from typing import Any

import pytest

from athenaeum.asserter_authority import (
    AUTHORITY_EQUAL,
    AUTHORITY_GREATER,
    AUTHORITY_INCOMPARABLE,
    AUTHORITY_LESS,
    AUTHORITY_RELATIONS,
    compare_authority,
    declared_grants,
    grant_closure,
    strictly_greater_authority,
    treated_as_equal_authority,
)
from athenaeum.config import resolve_authority_grant_implications

_LADDER: dict[str, Any] = {
    "librarian": {"authority_grant_implications": {"admin": ["editor"], "editor": ["reader"]}}
}


def _meta(*grants: str) -> dict[str, Any]:
    return {"asserter": {"type": "person", "iss": "i", "sub": "s", "grants": list(grants)}}


class TestDeclaredGrants:
    def test_reads_the_grants_list(self) -> None:
        assert declared_grants(_meta("editor", "reader")) == frozenset({"editor", "reader"})

    def test_a_bare_scalar_is_accepted_as_a_one_item_list(self) -> None:
        assert declared_grants({"asserter": {"grants": "admin"}}) == frozenset({"admin"})

    def test_absent_asserter_block_is_empty(self) -> None:
        assert declared_grants({}) == frozenset()
        assert declared_grants(None) == frozenset()

    def test_non_list_grants_is_empty_not_an_exception(self) -> None:
        assert declared_grants({"asserter": {"grants": 7}}) == frozenset()

    def test_blank_and_non_string_members_are_dropped(self) -> None:
        assert declared_grants({"asserter": {"grants": ["  ", "editor", 3, None]}}) == frozenset(
            {"editor"}
        )

    def test_whitespace_is_stripped(self) -> None:
        assert declared_grants({"asserter": {"grants": [" editor "]}}) == frozenset({"editor"})


class TestGrantClosure:
    def test_a_grant_with_no_implications_expands_to_itself(self) -> None:
        assert grant_closure(["reader"]) == frozenset({"reader"})

    def test_implications_are_transitive(self) -> None:
        implications = resolve_authority_grant_implications(_LADDER)
        assert grant_closure(["admin"], implications) == frozenset({"admin", "editor", "reader"})

    def test_a_cycle_terminates_instead_of_hanging(self) -> None:
        # A malformed config must not hang a nightly run.
        cyclic = {"a": frozenset({"b"}), "b": frozenset({"a"})}
        assert grant_closure(["a"], cyclic) == frozenset({"a", "b"})

    def test_empty_input_is_empty(self) -> None:
        assert grant_closure([]) == frozenset()


class TestPartialOrderNotAChain:
    """athenaeum#715: "authority is a partial order, not a chain ... No branch
    may assume a total order.\""""

    def test_there_are_four_outcomes_not_three(self) -> None:
        assert AUTHORITY_RELATIONS == frozenset(
            {AUTHORITY_GREATER, AUTHORITY_LESS, AUTHORITY_EQUAL, AUTHORITY_INCOMPARABLE}
        )

    def test_disjoint_grants_are_incomparable_not_ranked(self) -> None:
        assert compare_authority(_meta("billing"), _meta("deploy")) == AUTHORITY_INCOMPARABLE

    def test_strict_superset_is_greater(self) -> None:
        assert compare_authority(_meta("editor", "reader"), _meta("reader")) == AUTHORITY_GREATER

    def test_strict_subset_is_less(self) -> None:
        assert compare_authority(_meta("reader"), _meta("editor", "reader")) == AUTHORITY_LESS

    def test_identical_grants_are_equal(self) -> None:
        assert compare_authority(_meta("reader"), _meta("reader")) == AUTHORITY_EQUAL

    def test_the_implication_graph_makes_the_order_non_trivial(self) -> None:
        # Without the ladder these are disjoint singletons -> incomparable.
        assert compare_authority(_meta("admin"), _meta("reader")) == AUTHORITY_INCOMPARABLE
        # With it, admin's closure strictly contains reader's.
        assert (
            compare_authority(_meta("admin"), _meta("reader"), config=_LADDER) == AUTHORITY_GREATER
        )

    def test_the_relation_is_antisymmetric(self) -> None:
        assert compare_authority(_meta("reader"), _meta("admin"), config=_LADDER) == AUTHORITY_LESS

    def test_a_configured_cycle_collapses_to_equal_rather_than_ranking(self) -> None:
        cyclic: dict[str, Any] = {
            "librarian": {"authority_grant_implications": {"a": ["b"], "b": ["a"]}}
        }
        assert compare_authority(_meta("a"), _meta("b"), config=cyclic) == AUTHORITY_EQUAL


class TestUndeclaredAuthorityIsIncomparableNeverLesser:
    """The rule that keeps plain set inclusion from ranking every granted
    asserter above every ungranted one -- which would be exactly backwards
    for a destructive decision."""

    def test_no_grants_on_either_side_is_equal(self) -> None:
        assert compare_authority({}, {}) == AUTHORITY_EQUAL

    def test_ungranted_versus_granted_is_incomparable(self) -> None:
        assert compare_authority({}, _meta("admin"), config=_LADDER) == AUTHORITY_INCOMPARABLE

    def test_granted_versus_ungranted_is_incomparable(self) -> None:
        assert compare_authority(_meta("admin"), {}, config=_LADDER) == AUTHORITY_INCOMPARABLE

    def test_an_ungranted_asserter_is_never_strictly_defeated(self) -> None:
        assert strictly_greater_authority(_meta("admin"), {}, config=_LADDER) is False

    def test_an_ungranted_side_takes_the_corroboration_path(self) -> None:
        relation = compare_authority({}, _meta("admin"), config=_LADDER)
        assert treated_as_equal_authority(relation) is True


class TestStrictlyGreaterAuthority:
    def test_true_only_for_greater(self) -> None:
        assert strictly_greater_authority(_meta("editor", "reader"), _meta("reader")) is True

    def test_false_for_equal(self) -> None:
        assert strictly_greater_authority(_meta("reader"), _meta("reader")) is False

    def test_false_for_incomparable(self) -> None:
        # An incomparable peer is not a superior.
        assert strictly_greater_authority(_meta("billing"), _meta("deploy")) is False

    def test_false_for_less(self) -> None:
        assert strictly_greater_authority(_meta("reader"), _meta("editor", "reader")) is False


class TestTreatedAsEqualAuthority:
    """athenaeum#715: "Incomparable grants are treated as equal authority ...
    so incomparable-peer conflicts take the corroboration-or-queue path.\""""

    @pytest.mark.parametrize(
        ("relation", "expected"),
        [
            (AUTHORITY_EQUAL, True),
            (AUTHORITY_INCOMPARABLE, True),
            (AUTHORITY_GREATER, False),
            (AUTHORITY_LESS, False),
        ],
    )
    def test_equal_and_incomparable_both_take_the_condition_c_path(
        self, relation: str, expected: bool
    ) -> None:
        assert treated_as_equal_authority(relation) is expected


class TestConfigResolution:
    def test_yaml_implication_map_is_read(self) -> None:
        assert resolve_authority_grant_implications(_LADDER) == {
            "admin": frozenset({"editor"}),
            "editor": frozenset({"reader"}),
        }

    def test_absent_config_is_an_empty_map(self) -> None:
        assert resolve_authority_grant_implications(None) == {}
        assert resolve_authority_grant_implications({}) == {}

    def test_malformed_entries_are_dropped_defensively(self) -> None:
        config: dict[str, Any] = {
            "librarian": {
                "authority_grant_implications": {
                    "admin": ["editor"],
                    "": ["x"],
                    "bad": 1,
                    "empty": [],
                    7: ["y"],
                }
            }
        }
        assert resolve_authority_grant_implications(config) == {"admin": frozenset({"editor"})}


class TestNoScalarAnywhere:
    """athenaeum#715 bans confidence/similarity as a verdict input. Authority
    is set-shaped end to end: there is no numeric rank anywhere in the
    module's signatures, so no caller can thread one in or read one out."""

    def test_no_exported_function_takes_or_returns_a_number(self) -> None:
        import inspect

        from athenaeum import asserter_authority

        numeric = {"float", "int", "float | None", "int | None"}
        offenders: list[str] = []
        for name in asserter_authority.__all__:
            value = getattr(asserter_authority, name)
            if not callable(value) or not inspect.isfunction(value):
                continue
            signature = inspect.signature(value)
            for param in signature.parameters.values():
                if str(param.annotation) in numeric:
                    offenders.append(f"{name}({param.name})")
            if str(signature.return_annotation) in numeric:
                offenders.append(f"{name}() -> number")
        assert offenders == [], (
            f"authority acquired a numeric rank at {offenders} -- athenaeum#715 "
            "bans a scalar from ranking correctness"
        )

    def test_the_probe_would_catch_a_numeric_signature(self) -> None:
        # Positive control: the check above must actually be able to fail.
        import inspect

        def _ranked(a: dict, b: dict) -> float:  # pragma: no cover - probe only
            return 0.0

        assert str(inspect.signature(_ranked).return_annotation) == "float"

    def test_comparison_never_returns_a_number(self) -> None:
        result = compare_authority(_meta("admin"), _meta("reader"), config=_LADDER)
        assert isinstance(result, str)
        assert result in AUTHORITY_RELATIONS
