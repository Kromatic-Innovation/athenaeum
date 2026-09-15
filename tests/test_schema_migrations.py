# SPDX-License-Identifier: Apache-2.0
"""``athenaeum.schema_migrations`` — the kernel-field migration registry
(issue athenaeum#1628).

Covers: the registry shape (at least one rule/eager and one model/on-audit
entry), `page_schema_version` defaulting a missing/malformed value to 0,
`pending_migrations` slicing the registry by a page's current version, and
`validate_migrations` rejecting each of the three declared invariant
violations (a version gap, a rule entry with no `apply`, a model entry
that DOES carry one) plus accepting a duplicate-id rejection. No test here
touches a live knowledge store — every check is over an in-memory dict or
the module's own registry.
"""

from __future__ import annotations

import pytest

from athenaeum.schema_migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    page_schema_version,
    pending_migrations,
    validate_migrations,
)


class TestRegistryShape:
    def test_at_least_one_rule_eager_and_one_model_on_audit(self) -> None:
        assert any(m.derivation == "rule" and m.timing == "eager" for m in MIGRATIONS)
        assert any(m.derivation == "model" and m.timing == "on-audit" for m in MIGRATIONS)

    def test_current_schema_version_is_the_highest_to_version(self) -> None:
        assert CURRENT_SCHEMA_VERSION == max(m.to_version for m in MIGRATIONS)

    def test_v0_to_v1_is_the_worked_rule_example(self) -> None:
        v1 = next(m for m in MIGRATIONS if m.to_version == 1)
        assert v1.from_version == 0
        assert v1.derivation == "rule"
        assert v1.timing == "eager"
        assert v1.apply is not None
        # Issue athenaeum#1628 Plan item 2: stamps nothing of its own.
        assert v1.apply({}) == {}

    def test_v1_to_v2_is_the_coordinate_fields_migration(self) -> None:
        v2 = next(m for m in MIGRATIONS if m.to_version == 2)
        assert v2.from_version == 1
        assert v2.derivation == "model"
        assert v2.timing == "on-audit"
        assert v2.apply is None
        assert set(v2.fields) == {"valid_from", "valid_until", "claimed_scope"}


class TestPageSchemaVersion:
    def test_missing_reads_as_zero(self) -> None:
        assert page_schema_version({}) == 0

    def test_present_int_is_read_through(self) -> None:
        assert page_schema_version({"schema_version": 2}) == 2

    def test_numeric_string_is_coerced(self) -> None:
        assert page_schema_version({"schema_version": "1"}) == 1

    def test_boolean_is_never_read_as_a_version(self) -> None:
        # bool is an int subclass in Python; a stray `schema_version: true`
        # must read as "absent" (0), never "version 1".
        assert page_schema_version({"schema_version": True}) == 0

    def test_garbage_string_reads_as_zero(self) -> None:
        assert page_schema_version({"schema_version": "not-a-number"}) == 0


class TestPendingMigrations:
    def test_version_zero_sees_every_migration(self) -> None:
        pending = pending_migrations({})
        assert pending == MIGRATIONS

    def test_version_one_sees_only_the_model_migration(self) -> None:
        pending = pending_migrations({"schema_version": 1})
        assert len(pending) == 1
        assert pending[0].derivation == "model"

    def test_fully_current_page_has_nothing_pending(self) -> None:
        assert pending_migrations({"schema_version": CURRENT_SCHEMA_VERSION}) == ()

    def test_a_version_past_current_also_has_nothing_pending(self) -> None:
        assert pending_migrations({"schema_version": CURRENT_SCHEMA_VERSION + 5}) == ()


def _migration(**overrides: object) -> Migration:
    base = dict(
        id="m",
        fields=(),
        from_version=0,
        to_version=1,
        derivation="rule",
        timing="eager",
        apply=lambda meta: {},
    )
    base.update(overrides)
    return Migration(**base)  # type: ignore[arg-type]


class TestValidateMigrations:
    def test_the_real_registry_is_valid(self) -> None:
        validate_migrations(MIGRATIONS)  # must not raise

    def test_rejects_a_version_gap(self) -> None:
        broken = (
            _migration(id="a", from_version=0, to_version=1),
            # Gap: next migration should start at 1, starts at 2 instead.
            _migration(id="b", from_version=2, to_version=3, derivation="model", apply=None),
        )
        with pytest.raises(ValueError, match="version gap"):
            validate_migrations(broken)

    def test_rejects_first_migration_not_starting_at_zero(self) -> None:
        broken = (_migration(id="a", from_version=1, to_version=2),)
        with pytest.raises(ValueError, match="version gap"):
            validate_migrations(broken)

    def test_rejects_a_rule_entry_without_apply(self) -> None:
        broken = (_migration(id="a", derivation="rule", apply=None),)
        with pytest.raises(ValueError, match="needs an apply callable"):
            validate_migrations(broken)

    def test_rejects_a_model_entry_that_carries_apply(self) -> None:
        broken = (
            _migration(id="a", derivation="model", timing="on-audit", apply=lambda meta: {}),
        )
        with pytest.raises(ValueError, match="must not carry"):
            validate_migrations(broken)

    def test_rejects_duplicate_ids(self) -> None:
        broken = (
            _migration(id="dup", from_version=0, to_version=1),
            _migration(id="dup", from_version=1, to_version=2, derivation="model", apply=None),
        )
        with pytest.raises(ValueError, match="duplicate migration id"):
            validate_migrations(broken)

    def test_rejects_a_migration_that_does_not_advance_the_version(self) -> None:
        broken = (_migration(id="a", from_version=0, to_version=0),)
        with pytest.raises(ValueError, match="does not advance"):
            validate_migrations(broken)
