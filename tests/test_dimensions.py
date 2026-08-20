# SPDX-License-Identifier: Apache-2.0
"""Tests for the dimension registry + kernel dimensions (issue athenaeum#714)."""

from __future__ import annotations

import warnings
from datetime import date, timedelta
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.config import (
    resolve_dimension_registry_epoch,
    resolve_dimension_tree_epoch,
    resolve_dimensions,
)
from athenaeum.dimensions import (
    DEFAULT_REGISTRY,
    KERNEL_DIMENSIONS,
    MEMORY_CLASS,
    OBSERVED_TIME,
    RECORDED_TIME,
    SCOPE,
    SUBJECT,
    VALID_TIME,
    Dimension,
    DimensionKind,
    DimensionRegistry,
    DimensionRegistryError,
    LifecycleState,
    NullMeans,
    ObservedAfterRecordedError,
    Relation,
    build_registry,
    can_separate,
    compare_dimension,
    compare_enum,
    compare_hierarchy,
    compare_identity,
    compare_interval,
    cross_corpus_compare,
    dimension_applies,
    maybe_flip_to_enforced,
    retire_dimension_coordinate,
    stamp_recorded_time,
    validate_intake_temporal,
)
from athenaeum.models import WikiEntity
from athenaeum.runlock import RunLock
from athenaeum.schemas import validate_wiki_meta
from athenaeum.verdicts import append_verdict, build_verdict_entry, lookup_pair


def _entry(id_a: str, id_b: str, verdict: str = "distinct"):
    from athenaeum.verdicts import Basis

    return build_verdict_entry(
        id_a,
        id_b,
        verdict,
        basis=Basis(),
        at="2026-08-01",
        decided_by="comparator",
    )


# ---------------------------------------------------------------------------
# Registry parsing + validation
# ---------------------------------------------------------------------------


class TestParseDimensionEntry:
    def test_kernel_only_registry_by_default(self) -> None:
        registry = build_registry(None)
        assert {d.name for d in registry} == {d.name for d in KERNEL_DIMENSIONS}

    def test_operator_dimension_parses(self) -> None:
        registry = build_registry(
            [
                {
                    "name": "engagement",
                    "kind": "identity",
                    "null_means": "unknown",
                    "separates": True,
                }
            ]
        )
        dim = registry.get("engagement")
        assert dim is not None
        assert dim.kind == DimensionKind.IDENTITY
        assert dim.origin == "operator"

    def test_rejects_non_kebab_case_name(self) -> None:
        with pytest.raises(DimensionRegistryError, match="kebab-case"):
            build_registry([{"name": "Engagement_Level", "kind": "identity"}])

    def test_rejects_bad_kind(self) -> None:
        with pytest.raises(DimensionRegistryError, match="kind"):
            build_registry([{"name": "engagement", "kind": "nonsense"}])

    def test_rejects_bad_null_means(self) -> None:
        with pytest.raises(DimensionRegistryError, match="null_means"):
            build_registry(
                [{"name": "engagement", "kind": "identity", "null_means": "nonsense"}]
            )

    def test_enum_requires_values(self) -> None:
        with pytest.raises(DimensionRegistryError, match="enum requires"):
            build_registry([{"name": "tier", "kind": "enum"}])

    def test_enum_with_values_ok(self) -> None:
        registry = build_registry(
            [{"name": "tier", "kind": "enum", "values": ["gold", "silver"]}]
        )
        assert registry.get("tier").values == ("gold", "silver")

    def test_rejects_duplicate_names(self) -> None:
        with pytest.raises(DimensionRegistryError, match="duplicate"):
            build_registry(
                [
                    {"name": "engagement", "kind": "identity"},
                    {"name": "engagement", "kind": "enum", "values": ["a"]},
                ]
            )

    def test_rejects_kernel_name_collision(self) -> None:
        with pytest.raises(DimensionRegistryError, match="kernel"):
            build_registry([{"name": "scope", "kind": "hierarchy"}])

    def test_rejects_non_list_dimensions_config(self) -> None:
        with pytest.raises(DimensionRegistryError, match="list"):
            build_registry({"name": "engagement"})

    def test_rejects_non_mapping_entry(self) -> None:
        with pytest.raises(DimensionRegistryError, match="mapping"):
            build_registry(["engagement"])

    def test_rejects_missing_name(self) -> None:
        with pytest.raises(DimensionRegistryError, match="name"):
            build_registry([{"kind": "identity"}])

    def test_rejects_bad_state(self) -> None:
        with pytest.raises(DimensionRegistryError, match="state"):
            build_registry([{"name": "engagement", "kind": "identity", "state": "nope"}])

    def test_rejects_bad_origin(self) -> None:
        with pytest.raises(DimensionRegistryError, match="origin"):
            build_registry([{"name": "engagement", "kind": "identity", "origin": "nope"}])

    def test_accepts_proposed_origin(self) -> None:
        registry = build_registry(
            [{"name": "engagement", "kind": "identity", "origin": "proposed:p-1"}]
        )
        assert registry.get("engagement").origin == "proposed:p-1"

    def test_rejects_malformed_since(self) -> None:
        with pytest.raises(DimensionRegistryError, match="since"):
            build_registry([{"name": "engagement", "kind": "identity", "since": "not-a-date"}])

    def test_parses_valid_since(self) -> None:
        registry = build_registry(
            [{"name": "engagement", "kind": "identity", "since": "2026-08-01"}]
        )
        assert registry.get("engagement").since == date(2026, 8, 1)


class TestParseDimensionEntryEdges:
    def test_rejects_non_str_kind(self) -> None:
        with pytest.raises(DimensionRegistryError, match="'kind'"):
            build_registry([{"name": "x", "kind": 5}])

    def test_rejects_non_str_null_means(self) -> None:
        with pytest.raises(DimensionRegistryError, match="'null_means'"):
            build_registry([{"name": "x", "kind": "identity", "null_means": 5}])

    def test_rejects_non_str_values_entries(self) -> None:
        with pytest.raises(DimensionRegistryError, match="'values'"):
            build_registry([{"name": "x", "kind": "enum", "values": [1, 2]}])

    def test_rejects_non_bool_separates(self) -> None:
        with pytest.raises(DimensionRegistryError, match="'separates'"):
            build_registry([{"name": "x", "kind": "identity", "separates": "yes"}])

    def test_rejects_non_dict_applies_to(self) -> None:
        with pytest.raises(DimensionRegistryError, match="'applies_to'"):
            build_registry([{"name": "x", "kind": "identity", "applies_to": ["a"]}])

    def test_rejects_non_str_state(self) -> None:
        with pytest.raises(DimensionRegistryError, match="'state'"):
            build_registry([{"name": "x", "kind": "identity", "state": 1}])

    def test_rejects_non_str_origin(self) -> None:
        with pytest.raises(DimensionRegistryError, match="'origin'"):
            build_registry([{"name": "x", "kind": "identity", "origin": 1}])

    def test_rejects_bad_coverage_threshold(self) -> None:
        with pytest.raises(DimensionRegistryError, match="coverage_threshold"):
            build_registry(
                [{"name": "x", "kind": "identity", "coverage_threshold": "not-a-number"}]
            )

    def test_dimension_direct_construction_rejects_bad_applies_to(self) -> None:
        with pytest.raises(DimensionRegistryError, match="applies_to"):
            Dimension(name="x", kind=DimensionKind.IDENTITY, applies_to=["not-a-dict"])  # type: ignore[arg-type]

    def test_since_accepts_date_object(self) -> None:
        registry = build_registry([{"name": "x", "kind": "identity", "since": date(2026, 1, 1)}])
        assert registry.get("x").since == date(2026, 1, 1)


class TestDimensionRegistry:
    def test_duplicate_dimension_objects_raise(self) -> None:
        dim = Dimension(name="x", kind=DimensionKind.IDENTITY)
        with pytest.raises(DimensionRegistryError, match="duplicate"):
            DimensionRegistry(dimensions=(dim, dim))

    def test_get_missing_returns_none(self) -> None:
        assert DEFAULT_REGISTRY.get("does-not-exist") is None

    def test_len_and_iter(self) -> None:
        assert len(DEFAULT_REGISTRY) == 6
        assert len(list(DEFAULT_REGISTRY)) == 6


# ---------------------------------------------------------------------------
# Kernel dimensions
# ---------------------------------------------------------------------------


class TestKernelDimensions:
    def test_six_kernel_dimensions(self) -> None:
        names = {d.name for d in KERNEL_DIMENSIONS}
        assert names == {
            "recorded-time",
            "observed-time",
            "valid-time",
            "scope",
            "subject",
            "memory-class",
        }

    def test_recorded_time_never_separates(self) -> None:
        assert RECORDED_TIME.separates is False
        assert RECORDED_TIME.kind == DimensionKind.INTERVAL

    def test_observed_time_never_separates(self) -> None:
        assert OBSERVED_TIME.separates is False

    def test_valid_time_separates_and_universal_null(self) -> None:
        assert VALID_TIME.separates is True
        assert VALID_TIME.null_means == NullMeans.UNIVERSAL

    def test_scope_hierarchy_separates_universal_null(self) -> None:
        assert SCOPE.kind == DimensionKind.HIERARCHY
        assert SCOPE.separates is True
        assert SCOPE.null_means == NullMeans.UNIVERSAL

    def test_subject_identity_separates_unknown_null(self) -> None:
        assert SUBJECT.kind == DimensionKind.IDENTITY
        assert SUBJECT.separates is True
        assert SUBJECT.null_means == NullMeans.UNKNOWN

    def test_memory_class_enum_ships_at_backfill(self) -> None:
        # Issue athenaeum#972 disposition (PR review comment on athenaeum#714): never
        # `enforced` at ship — coverage is real but thin for decision/procedure.
        assert MEMORY_CLASS.kind == DimensionKind.ENUM
        assert MEMORY_CLASS.state == LifecycleState.BACKFILL
        assert "entity" in MEMORY_CLASS.values

    def test_kernel_dimensions_are_builtin_origin(self) -> None:
        assert all(d.origin == "builtin" for d in KERNEL_DIMENSIONS)


# ---------------------------------------------------------------------------
# Comparators — interval (half-open [from, until))
# ---------------------------------------------------------------------------


class TestCompareInterval:
    def test_equal(self) -> None:
        d1, d2 = date(2020, 1, 1), date(2021, 1, 1)
        assert compare_interval((d1, d2), (d1, d2)) == Relation.EQUAL

    def test_abutting_windows_are_disjoint_not_overlapping(self) -> None:
        # "2020-2022" then "2022-" must NOT collide as a zero-width overlap
        # (issue athenaeum#714 AC — the known collapsed-interval-algebra failure).
        window_a = (date(2020, 1, 1), date(2022, 1, 1))  # [2020, 2022)
        window_b = (date(2022, 1, 1), None)  # [2022, open)
        assert compare_interval(window_a, window_b) == Relation.DISJOINT
        assert compare_interval(window_b, window_a) == Relation.DISJOINT

    def test_overlapping_windows(self) -> None:
        a = (date(2020, 1, 1), date(2021, 6, 1))
        b = (date(2021, 1, 1), date(2022, 1, 1))
        assert compare_interval(a, b) == Relation.OVERLAPS

    def test_nested_window_contains(self) -> None:
        outer = (date(2020, 1, 1), date(2023, 1, 1))
        inner = (date(2021, 1, 1), date(2022, 1, 1))
        assert compare_interval(outer, inner) == Relation.CONTAINS
        assert compare_interval(inner, outer) == Relation.CONTAINS

    def test_disjoint_far_apart(self) -> None:
        a = (date(2020, 1, 1), date(2020, 6, 1))
        b = (date(2025, 1, 1), date(2025, 6, 1))
        assert compare_interval(a, b) == Relation.DISJOINT

    def test_fully_open_contains_everything(self) -> None:
        everything = (None, None)
        narrow = (date(2020, 1, 1), date(2020, 6, 1))
        assert compare_interval(everything, narrow) == Relation.CONTAINS

    def test_instant_containment(self) -> None:
        # "instants by containment" — a single day inside a wider window.
        window = (date(2020, 1, 1), date(2021, 1, 1))
        instant = (date(2020, 6, 1), date(2020, 6, 2))
        assert compare_interval(window, instant) == Relation.CONTAINS

    def test_both_null_is_unknown_not_separable(self) -> None:
        # A dimension separates only pairs where at least one side carries a
        # coordinate (issue athenaeum#714 AC).
        assert compare_interval(None, None, null_means=NullMeans.UNIVERSAL) == Relation.UNKNOWN
        assert compare_interval(None, None, null_means=NullMeans.UNKNOWN) == Relation.UNKNOWN

    def test_null_means_universal_contains(self) -> None:
        window = (date(2020, 1, 1), date(2020, 6, 1))
        assert compare_interval(None, window, null_means=NullMeans.UNIVERSAL) == Relation.CONTAINS
        assert compare_interval(window, None, null_means=NullMeans.UNIVERSAL) == Relation.CONTAINS

    def test_null_means_unknown_is_unknown(self) -> None:
        window = (date(2020, 1, 1), date(2020, 6, 1))
        assert compare_interval(None, window, null_means=NullMeans.UNKNOWN) == Relation.UNKNOWN


# ---------------------------------------------------------------------------
# Comparators — hierarchy
# ---------------------------------------------------------------------------


class TestCompareHierarchy:
    def test_equal(self) -> None:
        assert compare_hierarchy("kromatic/platform", "kromatic/platform") == Relation.EQUAL

    def test_prefix_subsumption_contains(self) -> None:
        assert compare_hierarchy("kromatic", "kromatic/platform") == Relation.CONTAINS
        assert compare_hierarchy("kromatic/platform", "kromatic") == Relation.CONTAINS

    def test_siblings_are_disjoint(self) -> None:
        assert compare_hierarchy("kromatic/platform", "kromatic/marketing") == Relation.DISJOINT

    def test_unrelated_trees_disjoint(self) -> None:
        assert compare_hierarchy("kromatic", "acme") == Relation.DISJOINT

    def test_case_and_whitespace_normalized(self) -> None:
        assert compare_hierarchy(" Kromatic/Platform ", "kromatic/platform") == Relation.EQUAL

    def test_both_null_unknown(self) -> None:
        assert compare_hierarchy(None, None, null_means=NullMeans.UNIVERSAL) == Relation.UNKNOWN

    def test_null_means_universal_contains(self) -> None:
        assert (
            compare_hierarchy(None, "kromatic/platform", null_means=NullMeans.UNIVERSAL)
            == Relation.CONTAINS
        )

    def test_explicit_universal_marker(self) -> None:
        assert compare_hierarchy("*", "kromatic/platform") == Relation.CONTAINS
        assert compare_hierarchy("kromatic/platform", "*") == Relation.CONTAINS
        assert compare_hierarchy("*", "*") == Relation.EQUAL


# ---------------------------------------------------------------------------
# Comparators — enum
# ---------------------------------------------------------------------------


class TestCompareEnum:
    def test_same_is_equal(self) -> None:
        assert compare_enum("entity", "entity") == Relation.EQUAL

    def test_different_is_disjoint(self) -> None:
        assert compare_enum("entity", "fact") == Relation.DISJOINT

    def test_both_null_unknown(self) -> None:
        assert compare_enum(None, None) == Relation.UNKNOWN

    def test_null_means_unknown_default(self) -> None:
        assert compare_enum(None, "entity") == Relation.UNKNOWN

    def test_null_means_universal_contains(self) -> None:
        assert compare_enum(None, "entity", null_means=NullMeans.UNIVERSAL) == Relation.CONTAINS

    def test_explicit_universal_marker(self) -> None:
        assert compare_enum("*", "entity") == Relation.CONTAINS


# ---------------------------------------------------------------------------
# Comparators — identity
# ---------------------------------------------------------------------------


class TestCompareIdentity:
    def test_same_is_equal(self) -> None:
        assert compare_identity("person-uid-1", "person-uid-1") == Relation.EQUAL

    def test_different_unratified_is_unknown_not_disjoint(self) -> None:
        # "Subjects never separate on a model-reported scalar — no confidence
        # threshold, ever" (issue athenaeum#714 AC).
        assert compare_identity("person-uid-1", "person-uid-2") == Relation.UNKNOWN
        assert (
            compare_identity("person-uid-1", "person-uid-2", ratified=False) == Relation.UNKNOWN
        )

    def test_different_ratified_is_disjoint(self) -> None:
        assert (
            compare_identity("person-uid-1", "person-uid-2", ratified=True) == Relation.DISJOINT
        )

    def test_no_confidence_kwarg_exists(self) -> None:
        # Structural guard: the function signature has no numeric/confidence
        # parameter at all — a caller cannot smuggle a threshold in.
        import inspect

        params = inspect.signature(compare_identity).parameters
        assert "confidence" not in params
        assert "threshold" not in params
        assert set(params) == {"a", "b", "null_means", "ratified"}

    def test_both_null_unknown(self) -> None:
        assert compare_identity(None, None) == Relation.UNKNOWN


class TestCompareDispatch:
    def test_compare_dispatches_per_kind(self) -> None:
        from athenaeum.dimensions import compare

        assert compare(VALID_TIME, None, None) == Relation.UNKNOWN
        hierarchy_dim = Dimension(name="x", kind=DimensionKind.HIERARCHY)
        assert compare(hierarchy_dim, "a", "a") == Relation.EQUAL
        enum_dim = Dimension(name="y", kind=DimensionKind.ENUM, values=("a", "b"))
        assert compare(enum_dim, "a", "b") == Relation.DISJOINT
        assert compare(SUBJECT, "u1", "u2", ratified=True) == Relation.DISJOINT

    def test_compare_rejects_unknown_kind(self) -> None:
        # Dimension.__post_init__ already rejects a bad kind at construction
        # time, so exercising compare()'s OWN defensive dispatch guard needs
        # an object that merely LOOKS like a Dimension (has .kind/.name) —
        # not a real one, which cannot be built with an invalid kind.
        import types

        from athenaeum.dimensions import compare

        fake = types.SimpleNamespace(kind="nonsense", name="fake", null_means="unknown")
        with pytest.raises(DimensionRegistryError, match="unknown kind"):
            compare(fake, None, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# separates / can_separate
# ---------------------------------------------------------------------------


class TestCanSeparate:
    def test_separator_disjoint_can_separate(self) -> None:
        assert can_separate(VALID_TIME, Relation.DISJOINT) is True

    def test_sequencer_disjoint_cannot_separate(self) -> None:
        # Without this gate every standing-state update would exit DISTINCT
        # (issue athenaeum#714 AC).
        assert can_separate(RECORDED_TIME, Relation.DISJOINT) is False
        assert can_separate(OBSERVED_TIME, Relation.DISJOINT) is False

    def test_non_disjoint_never_separates(self) -> None:
        assert can_separate(VALID_TIME, Relation.EQUAL) is False
        assert can_separate(VALID_TIME, Relation.OVERLAPS) is False
        assert can_separate(VALID_TIME, Relation.CONTAINS) is False
        assert can_separate(VALID_TIME, Relation.UNKNOWN) is False


# ---------------------------------------------------------------------------
# applies_to
# ---------------------------------------------------------------------------


class TestDimensionApplies:
    def test_empty_applies_to_matches_everything(self) -> None:
        dim = Dimension(name="x", kind=DimensionKind.IDENTITY)
        assert dimension_applies(dim, {"memory_class": "entity"}) is True
        assert dimension_applies(dim, None) is True

    def test_narrow_applies_to_excludes_other_pages(self) -> None:
        dim = Dimension(
            name="crm-only",
            kind=DimensionKind.IDENTITY,
            applies_to={"memory_class": "entity"},
        )
        assert dimension_applies(dim, {"memory_class": "entity"}) is True
        # A dev-rig page (memory_class=reference) must never be consulted.
        assert dimension_applies(dim, {"memory_class": "reference"}) is False
        assert dimension_applies(dim, {}) is False

    def test_applies_to_accepts_list_of_allowed_values(self) -> None:
        dim = Dimension(
            name="crm-only",
            kind=DimensionKind.IDENTITY,
            applies_to={"memory_class": ["entity", "fact"]},
        )
        assert dimension_applies(dim, {"memory_class": "fact"}) is True
        assert dimension_applies(dim, {"memory_class": "guideline"}) is False

    def test_non_mapping_meta_does_not_apply(self) -> None:
        dim = Dimension(name="x", kind=DimensionKind.IDENTITY, applies_to={"memory_class": "e"})
        assert dimension_applies(dim, None) is False
        assert dimension_applies(dim, "not-a-mapping") is False  # type: ignore[arg-type]

    def test_compare_dimension_not_consulted_outside_applies_to(self) -> None:
        dim = Dimension(
            name="crm-only",
            kind=DimensionKind.ENUM,
            values=("a", "b"),
            applies_to={"memory_class": "entity"},
        )
        meta_a = {"memory_class": "entity", "crm-only": "a"}
        meta_b = {"memory_class": "reference", "crm-only": "b"}
        # Outside applies_to on one side -> never reaches the enum comparator
        # (which would otherwise report DISJOINT for "a" vs "b").
        assert compare_dimension(dim, meta_a, meta_b) == Relation.UNKNOWN


# ---------------------------------------------------------------------------
# Lifecycle: backfill gating in compare_dimension
# ---------------------------------------------------------------------------


class TestBackfillGating:
    def test_backfill_only_consults_when_both_sides_carry_coordinate(self) -> None:
        assert MEMORY_CLASS.state == LifecycleState.BACKFILL
        meta_both = {"memory_class": "entity"}
        meta_none = {}
        assert (
            compare_dimension(MEMORY_CLASS, meta_both, {"memory_class": "fact"})
            == Relation.DISJOINT
        )
        assert compare_dimension(MEMORY_CLASS, meta_both, meta_none) == Relation.UNKNOWN
        assert compare_dimension(MEMORY_CLASS, meta_none, meta_none) == Relation.UNKNOWN

    def test_enforced_dimension_uses_null_means_normally(self) -> None:
        assert VALID_TIME.state == LifecycleState.ENFORCED
        meta_open = {}
        meta_bounded = {"valid_from": "2020-01-01", "valid_until": "2020-12-31"}
        # valid-time null_means=universal -> the open side CONTAINS.
        assert compare_dimension(VALID_TIME, meta_open, meta_bounded) == Relation.CONTAINS


# ---------------------------------------------------------------------------
# Write discipline: origin scope vs. claimed scope
# ---------------------------------------------------------------------------


class TestCoordinateValueAndParsing:
    def test_deployment_declared_dimension_reads_bare_key(self) -> None:
        from athenaeum.dimensions import coordinate_value

        dim = Dimension(name="engagement", kind=DimensionKind.IDENTITY)
        assert coordinate_value(dim, {"engagement": "acme"}) == "acme"
        assert coordinate_value(dim, {"other": "acme"}) is None
        assert coordinate_value(dim, None) is None

    def test_coerce_date_or_none_full_datetime_string(self) -> None:
        from athenaeum.dimensions import _coerce_date_or_none

        assert _coerce_date_or_none("2026-08-20T12:30:00+00:00") == date(2026, 8, 20)
        assert _coerce_date_or_none("not-a-date-at-all") is None
        assert _coerce_date_or_none(None) is None

    def test_parsed_coordinate_valid_time_none_when_both_bounds_absent(self) -> None:
        from athenaeum.dimensions import parsed_coordinate

        assert parsed_coordinate(VALID_TIME, {}) is None


class TestOriginScopeNeverPopulatesClaimedScope:
    def test_origin_scope_never_populates_claimed_scope(self) -> None:
        """Regression test (issue athenaeum#714 AC): constructing a WikiEntity with
        ``origin_scope`` set but ``claimed_scope`` unset must NEVER result in
        ``claimed_scope`` being populated from ``origin_scope``.

        Rationale: origin scope is PROVENANCE (where/what context wrote the
        claim — a writer gets this "for free"); claimed scope is an ASSERTED
        coordinate (where the claim APPLIES) that must be explicit. Silently
        auto-copying would let the one free typed gate in the pipeline with
        no LLM, no human, and no sampling consume provenance as semantics —
        the measured duplication case this issue's Motivation section names
        (one policy restated across four sibling-scoped pages) would exit as
        DISTINCT before any content comparison, permanently and silently,
        and genuine specialization could never be detected because every
        general rule would be born narrow.
        """
        entity = WikiEntity(
            uid="p-1",
            type="principle",
            name="Test Principle",
            origin_scope="kromatic/platform",
        )
        assert entity.origin_scope == "kromatic/platform"
        assert entity.claimed_scope is None

    def test_claimed_scope_survives_when_explicitly_asserted(self) -> None:
        entity = WikiEntity(
            uid="p-2",
            type="principle",
            name="Test Principle 2",
            origin_scope="kromatic/platform",
            claimed_scope="kromatic",
        )
        assert entity.origin_scope == "kromatic/platform"
        assert entity.claimed_scope == "kromatic"

    def test_render_never_derives_claimed_scope_from_origin(self) -> None:
        entity = WikiEntity(uid="p-3", type="principle", name="P3", origin_scope="acme/widgets")
        rendered = entity.render()
        assert "origin_scope: acme/widgets" in rendered
        assert "claimed_scope:" not in rendered


class TestSessionStampsRecordedTimeOnly:
    def test_recorded_at_stamped_on_construction(self) -> None:
        entity = WikiEntity(uid="p-4", type="principle", name="P4")
        assert entity.recorded_at is not None
        # Full ISO datetime, not just a date — parseable.
        from datetime import datetime as _dt

        _dt.fromisoformat(entity.recorded_at)

    def test_recorded_at_stamping_never_touches_observed_or_valid_time(self) -> None:
        # WikiEntity has no observed_at/valid_from/valid_until fields (those
        # live on the OTHER write-side dataclass / are read via the existing
        # meta-dict parsers) — constructing one and getting a fresh
        # recorded_at must never set anything else. This is the "session
        # time stamps recorded-time (bookkeeping) and never observed-time or
        # valid-time (claims about the territory)" AC, verified against the
        # concrete stamping code path.
        entity = WikiEntity(uid="p-5", type="principle", name="P5")
        assert not hasattr(entity, "observed_at")
        assert not hasattr(entity, "valid_from")
        assert not hasattr(entity, "valid_until")
        assert entity.claimed_scope is None
        assert entity.subject is None

    def test_recorded_at_preserved_on_reconstruction_not_bumped(self) -> None:
        first = WikiEntity(uid="p-6", type="principle", name="P6")
        original = first.recorded_at
        # Simulate an edit/merge round-trip that threads the existing
        # recorded_at back through the constructor.
        second = WikiEntity(uid="p-6", type="principle", name="P6 renamed", recorded_at=original)
        assert second.recorded_at == original

    def test_stamp_recorded_time_ignores_writer_input_shape(self) -> None:
        from datetime import datetime, timezone

        fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert stamp_recorded_time(fixed) == "2026-01-01T00:00:00+00:00"


class TestMissingCoordinateNotRejected:
    def test_wiki_entity_with_no_coordinates_constructs_fine(self) -> None:
        entity = WikiEntity(uid="p-7", type="principle", name="P7")
        assert entity.claimed_scope is None
        assert entity.origin_scope is None
        assert entity.subject is None

    def test_validate_wiki_meta_accepts_missing_coordinates(self) -> None:
        meta = {"uid": "p-8", "type": "principle", "name": "P8"}
        model = validate_wiki_meta(meta)
        assert model.claimed_scope is None
        assert model.subject is None


# ---------------------------------------------------------------------------
# Intake temporal validation
# ---------------------------------------------------------------------------


class TestValidateIntakeTemporal:
    def test_observed_after_recorded_rejected(self) -> None:
        with pytest.raises(ObservedAfterRecordedError, match="cannot have observed the future"):
            validate_intake_temporal(
                observed_at="2026-12-31", recorded_at="2026-08-20"
            )

    def test_observed_before_recorded_ok(self) -> None:
        validate_intake_temporal(observed_at="2026-08-01", recorded_at="2026-08-20")

    def test_observed_equal_recorded_ok(self) -> None:
        validate_intake_temporal(observed_at="2026-08-20", recorded_at="2026-08-20")

    def test_deep_backdate_flagged_not_rejected(self) -> None:
        with pytest.warns(UserWarning, match="deep back-date"):
            validate_intake_temporal(
                observed_at="2020-01-01",
                recorded_at="2026-08-20",
                deep_backdate_days=730,
            )

    def test_shallow_backdate_no_warning(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            validate_intake_temporal(
                observed_at="2026-01-01", recorded_at="2026-08-20", deep_backdate_days=730
            )

    def test_missing_observed_at_is_noop(self) -> None:
        validate_intake_temporal(observed_at=None, recorded_at="2026-08-20")

    def test_missing_recorded_at_falls_back_to_today(self) -> None:
        future = (date.today() + timedelta(days=5)).isoformat()
        with pytest.raises(ObservedAfterRecordedError):
            validate_intake_temporal(observed_at=future, recorded_at=None)

    def test_schema_boundary_rejects_future_observed_at(self) -> None:
        meta = {
            "uid": "p-9",
            "type": "principle",
            "name": "P9",
            "observed_at": (date.today() + timedelta(days=30)).isoformat(),
        }
        with pytest.raises(Exception, match="cannot have observed the future"):
            validate_wiki_meta(meta)


# ---------------------------------------------------------------------------
# Lifecycle flip, wired to athenaeum#712's targeted stale-marking
# ---------------------------------------------------------------------------


class TestMaybeFlipToEnforced:
    def test_below_threshold_is_noop(self, tmp_path: Path) -> None:
        dim = Dimension(
            name="engagement",
            kind=DimensionKind.IDENTITY,
            state=LifecycleState.BACKFILL,
            coverage_threshold=0.9,
        )
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        lock = RunLock(tmp_path)
        with lock:
            flipped, marked = maybe_flip_to_enforced(
                dim,
                coverage=0.5,
                entries=[],
                changed_ids=set(),
                wiki_root=wiki_root,
                lock=lock,
            )
        assert flipped.state == LifecycleState.BACKFILL
        assert marked == 0

    def test_already_enforced_is_noop(self, tmp_path: Path) -> None:
        dim = Dimension(name="engagement", kind=DimensionKind.IDENTITY)  # enforced default
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        lock = RunLock(tmp_path)
        with lock:
            flipped, marked = maybe_flip_to_enforced(
                dim,
                coverage=1.0,
                entries=[],
                changed_ids=set(),
                wiki_root=wiki_root,
                lock=lock,
            )
        assert flipped.state == LifecycleState.ENFORCED
        assert marked == 0

    def test_crossing_threshold_flips_and_stale_marks(self, tmp_path: Path) -> None:
        dim = Dimension(
            name="engagement",
            kind=DimensionKind.IDENTITY,
            state=LifecycleState.BACKFILL,
            coverage_threshold=0.8,
        )
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        lock = RunLock(tmp_path)
        entry = _entry("alpha", "beta")
        with lock:
            append_verdict(wiki_root, entry, lock=lock)
            flipped, marked = maybe_flip_to_enforced(
                dim,
                coverage=0.95,
                entries=[entry],
                changed_ids={"alpha"},
                wiki_root=wiki_root,
                lock=lock,
            )
        assert flipped.state == LifecycleState.ENFORCED
        assert marked == 1
        found = lookup_pair(wiki_root, entry.pair)
        assert found.stale is True
        assert "engagement" in found.stale_reason


class TestRetireDimensionCoordinate:
    def test_re_nulls_not_deletes(self) -> None:
        meta = {"claimed_scope": "kromatic/platform", "other": "unchanged"}
        out = retire_dimension_coordinate(meta, "claimed_scope")
        assert "claimed_scope" in out  # key stays present
        assert out["claimed_scope"] is None  # value re-nulled
        assert out["other"] == "unchanged"
        assert meta["claimed_scope"] == "kromatic/platform"  # original untouched

    def test_absent_key_is_noop(self) -> None:
        meta = {"other": "value"}
        out = retire_dimension_coordinate(meta, "claimed_scope")
        assert "claimed_scope" not in out


# ---------------------------------------------------------------------------
# Corpus namespacing
# ---------------------------------------------------------------------------


class TestCrossCorpusCompare:
    def test_same_name_same_kind_compares_normally(self) -> None:
        dim = Dimension(name="maturity", kind=DimensionKind.ENUM, values=("mvp", "ga"))
        assert cross_corpus_compare(dim, dim, "mvp", "mvp") == Relation.EQUAL

    def test_different_names_unmapped_is_unknown(self) -> None:
        org_dim = Dimension(name="org-maturity", kind=DimensionKind.ENUM, values=("mvp", "ga"))
        personal_dim = Dimension(
            name="personal-maturity", kind=DimensionKind.ENUM, values=("mvp", "ga")
        )
        # Same values, different axis names, no ratified mapping -> UNKNOWN,
        # never a false DISJOINT/EQUAL from colliding vocabularies.
        assert cross_corpus_compare(org_dim, personal_dim, "mvp", "ga") == Relation.UNKNOWN
        assert cross_corpus_compare(org_dim, personal_dim, "mvp", "mvp") == Relation.UNKNOWN

    def test_ratified_mapping_translates(self) -> None:
        org_dim = Dimension(name="org-maturity", kind=DimensionKind.ENUM, values=("mvp", "ga"))
        personal_dim = Dimension(
            name="personal-maturity", kind=DimensionKind.ENUM, values=("mvp", "ga")
        )
        result = cross_corpus_compare(
            org_dim,
            personal_dim,
            "mvp",
            "mvp",
            mapping={"org-maturity": "personal-maturity"},
        )
        assert result == Relation.EQUAL

    def test_missing_dimension_is_unknown(self) -> None:
        dim = Dimension(name="maturity", kind=DimensionKind.ENUM, values=("mvp", "ga"))
        assert cross_corpus_compare(dim, None, "mvp", "ga") == Relation.UNKNOWN

    def test_kind_mismatch_is_unknown(self) -> None:
        enum_dim = Dimension(name="x", kind=DimensionKind.ENUM, values=("a", "b"))
        hierarchy_dim = Dimension(name="x", kind=DimensionKind.HIERARCHY)
        assert cross_corpus_compare(enum_dim, hierarchy_dim, "a", "a") == Relation.UNKNOWN


# ---------------------------------------------------------------------------
# config.py wiring
# ---------------------------------------------------------------------------


class TestResolveDimensions:
    def test_no_config_is_kernel_only(self) -> None:
        registry = resolve_dimensions(None)
        assert {d.name for d in registry} == {d.name for d in KERNEL_DIMENSIONS}

    def test_reads_operator_dimensions_from_config(self) -> None:
        config = {
            "dimensions": [
                {"name": "engagement", "kind": "identity", "null_means": "unknown"}
            ]
        }
        registry = resolve_dimensions(config)
        assert registry.get("engagement") is not None
        assert registry.get("scope") is not None  # kernel still present

    def test_malformed_entry_raises(self) -> None:
        config = {"dimensions": [{"name": "Bad_Name", "kind": "identity"}]}
        with pytest.raises(DimensionRegistryError):
            resolve_dimensions(config)


class TestResolveDimensionEpochs:
    def test_registry_epoch_default(self) -> None:
        assert resolve_dimension_registry_epoch(None) == 1

    def test_registry_epoch_from_yaml(self) -> None:
        config = {"librarian": {"dimensions_registry_epoch": 3}}
        assert resolve_dimension_registry_epoch(config) == 3

    def test_registry_epoch_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_DIMENSION_REGISTRY_EPOCH", "7")
        config = {"librarian": {"dimensions_registry_epoch": 3}}
        assert resolve_dimension_registry_epoch(config) == 7

    def test_tree_epoch_default(self) -> None:
        assert resolve_dimension_tree_epoch(None) == 1

    def test_tree_epoch_from_yaml(self) -> None:
        config = {"librarian": {"dimensions_tree_epoch": 2}}
        assert resolve_dimension_tree_epoch(config) == 2

    def test_epochs_populate_verdict_basis(self) -> None:
        # Issue athenaeum#714 AC: "Both must appear in the ledger basis of any
        # verdict written after this issue" — proves the wiring, since no
        # live verdict-writing call site exists yet (the comparator that
        # would call this in production is a separate, future child of
        # epic athenaeum#709 — see this issue's Out-of-scope section).
        from athenaeum.verdicts import Basis

        basis = Basis(
            registry_epoch=resolve_dimension_registry_epoch(None),
            tree_epoch=resolve_dimension_tree_epoch(None),
        )
        assert basis.registry_epoch == 1
        assert basis.tree_epoch == 1


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestDimensionsCLI:
    def _write_page(self, path: Path, **frontmatter: str) -> None:
        lines = ["---"]
        lines.append("uid: p-1")
        lines.append("type: principle")
        lines.append("name: Test")
        for k, v in frontmatter.items():
            lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        lines.append("body text")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_show_prints_coordinates(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        page = tmp_path / "page.md"
        self._write_page(page, claimed_scope="kromatic/platform")
        rc = main(["dimensions", "show", str(page), "--path", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "scope: 'kromatic/platform'" in out

    def test_show_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        page = tmp_path / "page.md"
        self._write_page(page, subject="person-uid-1")
        rc = main(["dimensions", "show", str(page), "--path", str(tmp_path), "--json"])
        assert rc == 0
        import json

        payload = json.loads(capsys.readouterr().out)
        assert payload["subject"] == "person-uid-1"

    def test_compare_axis_by_axis(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        page_a = tmp_path / "a.md"
        page_b = tmp_path / "b.md"
        self._write_page(page_a, memory_class="entity")
        self._write_page(page_b, memory_class="fact")
        rc = main(
            [
                "dimensions",
                "compare",
                str(page_a),
                str(page_b),
                "--path",
                str(tmp_path),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "memory-class: disjoint" in out

    def test_show_missing_file_exits_nonzero(self, tmp_path: Path) -> None:
        rc = main(["dimensions", "show", str(tmp_path / "missing.md"), "--path", str(tmp_path)])
        assert rc == 1
