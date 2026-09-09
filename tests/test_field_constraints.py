# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.field_constraints (issue athenaeum#1416).

Athenaeum ships NO policy — no entity type, field name, or address shape is
hardcoded anywhere in :mod:`athenaeum.field_constraints` or in these tests'
assertions about the EMPTY-schema case. The company/e-mail rule used below
is a WORKED EXAMPLE, declared only inside individual test fixtures, never
loaded by default and never asserted as this package's own opinion.

Two families of coverage, per the issue's own anti-vacuity requirement
(AC3): every ``TestEmptySchema*`` class proves a deployment with no
declared constraints is untouched; every other class declares a fixture
constraint and proves a violating page is ACTUALLY rejected or reported —
never a suite where every assertion is about the empty case.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.field_constraints import (
    BODY_PSEUDO_FIELD,
    FIELD_CONSTRAINT_LEDGER_NAME,
    FIELD_CONSTRAINT_REJECTED_DIR_NAME,
    FieldConstraint,
    check_entity_fields,
    guard_entity_field_constraints,
    list_field_constraint_violations,
    load_field_constraints,
    scan_field_constraint_violations,
)
from athenaeum.librarian import _apply_tier3_results
from athenaeum.models import EntityIndex, ProcessingResult, RawFile, WikiEntity

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _wiki_root(tmp_path: Path) -> Path:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    return wiki_root


def _write_field_constraints(wiki_root: Path, table_body: str) -> None:
    (wiki_root / "_schema").mkdir(parents=True, exist_ok=True)
    (wiki_root / "_schema" / "field-constraints.md").write_text(
        "# Field Constraints\n\n"
        "| Type | Field | Rule | Pattern |\n"
        "|------|-------|------|---------|\n" + table_body,
        encoding="utf-8",
    )


def _write_page(wiki: Path, filename: str, *, uid: str, page_type: str, name: str) -> Path:
    path = wiki / filename
    path.write_text(
        f"---\nuid: {uid}\ntype: {page_type}\nname: {name}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return path


def _raw(content: str = "irrelevant") -> RawFile:
    return RawFile(
        path=Path("/tmp/fake/raw/claude-session/20260807T090000Z-aabb0011.md"),
        source="claude-session",
        timestamp="20260807T090000Z",
        uuid8="aabb0011",
        _content=content,
    )


# ---------------------------------------------------------------------------
# load_field_constraints
# ---------------------------------------------------------------------------


class TestLoadFieldConstraintsEmptySchema:
    """AC2 counter-example that must fail: an absent/empty file must not
    manufacture a constraint out of nothing."""

    def test_absent_file_returns_empty(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        assert load_field_constraints(wiki_root) == ()

    def test_absent_schema_dir_returns_empty(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki-not-yet-created"
        assert load_field_constraints(wiki_root) == ()

    def test_present_but_header_only_file_returns_empty(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(wiki_root, "")
        assert load_field_constraints(wiki_root) == ()


class TestLoadFieldConstraintsPopulated:
    """AC1: the schema can express an arbitrary <type, field> pair — not
    just the contact-field worked example. AC6: allow-pattern/deny-pattern
    let the schema distinguish value CLASSES, not merely field presence."""

    def test_forbidden_row_parses(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(wiki_root, "| company | emails | forbidden | |\n")
        constraints = load_field_constraints(wiki_root)
        assert constraints == (
            FieldConstraint(entity_type="company", field="emails", rule="forbidden"),
        )

    def test_arbitrary_type_and_field_not_the_contact_case(self, tmp_path: Path) -> None:
        """Counter-example this AC names explicitly: a mechanism that only
        supports the contact-field case must fail. Prove an unrelated
        <type, field> pair (project/budget_usd) is equally expressible."""
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(
            wiki_root, "| project | budget_usd | forbidden | |\n"
        )
        constraints = load_field_constraints(wiki_root)
        assert constraints == (
            FieldConstraint(entity_type="project", field="budget_usd", rule="forbidden"),
        )

    def test_allow_pattern_and_deny_pattern_rows_parse_with_pattern(
        self, tmp_path: Path
    ) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(
            wiki_root,
            "| company | emails | allow-pattern | ^(info\\|sales)@ |\n"
            "| person | ssn | deny-pattern | ^\\d{3}-\\d{2}-\\d{4}$ |\n",
        )
        constraints = load_field_constraints(wiki_root)
        by_field = {c.field: c for c in constraints}
        assert by_field["emails"].rule == "allow-pattern"
        assert by_field["emails"].pattern == "^(info|sales)@"
        assert by_field["ssn"].rule == "deny-pattern"

    def test_unknown_rule_row_skipped_not_enforced(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(wiki_root, "| company | emails | delete-silently | |\n")
        assert load_field_constraints(wiki_root) == ()

    def test_pattern_rule_missing_pattern_column_skipped(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(wiki_root, "| company | emails | allow-pattern | |\n")
        assert load_field_constraints(wiki_root) == ()

    def test_invalid_regex_skipped(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(wiki_root, "| company | emails | allow-pattern | ( |\n")
        assert load_field_constraints(wiki_root) == ()


# ---------------------------------------------------------------------------
# check_entity_fields
# ---------------------------------------------------------------------------


class TestCheckEntityFieldsEmptySchema:
    def test_no_constraints_declared_is_a_true_noop(self) -> None:
        """AC2 counter-example that must fail: a fresh install (no
        declared constraints at all) must warn/filter/refuse NOTHING —
        not even for the company/email worked example's exact shape."""
        meta = {"uid": "u1", "type": "company", "name": "Acme", "emails": ["bob@example.com"]}
        assert check_entity_fields(meta, ()) == []

    def test_type_with_no_declared_row_is_untouched(self) -> None:
        constraints = (
            FieldConstraint(entity_type="company", field="emails", rule="forbidden"),
        )
        meta = {"uid": "u1", "type": "person", "name": "Alice", "emails": ["a@example.net"]}
        assert check_entity_fields(meta, constraints) == []


class TestCheckEntityFieldsPopulatedSchema:
    """AC3's anti-vacuity pairing: a DECLARED constraint against a
    violating page must actually report it."""

    def test_forbidden_field_present_is_a_violation(self) -> None:
        constraints = (
            FieldConstraint(entity_type="company", field="emails", rule="forbidden"),
        )
        meta = {
            "uid": "u1",
            "type": "company",
            "name": "Acme",
            "emails": ["bob@example.com"],
        }
        violations = check_entity_fields(meta, constraints)
        assert len(violations) == 1
        assert violations[0].field == "emails"
        assert violations[0].value == "bob@example.com"
        assert violations[0].entity_type == "company"

    def test_forbidden_field_absent_is_clean(self) -> None:
        constraints = (
            FieldConstraint(entity_type="company", field="emails", rule="forbidden"),
        )
        meta = {"uid": "u1", "type": "company", "name": "Acme"}
        assert check_entity_fields(meta, constraints) == []

    def test_allow_pattern_permits_role_rejects_personal(self) -> None:
        """AC6 worked example: permit a role address, forbid a personal one
        at the same domain — a class distinction the schema CAN express
        via a regex the operator supplies (not hardcoded here)."""
        constraints = (
            FieldConstraint(
                entity_type="company",
                field="emails",
                rule="allow-pattern",
                pattern=r"^(info|sales|support)@",
            ),
        )
        role_meta = {
            "uid": "u1",
            "type": "company",
            "name": "Acme",
            "emails": ["info@example.com"],
        }
        personal_meta = {
            "uid": "u2",
            "type": "company",
            "name": "Acme",
            "emails": ["bob.smith@example.com"],
        }
        assert check_entity_fields(role_meta, constraints) == []
        violations = check_entity_fields(personal_meta, constraints)
        assert len(violations) == 1
        assert violations[0].value == "bob.smith@example.com"

    def test_deny_pattern_flags_matching_value(self) -> None:
        constraints = (
            FieldConstraint(
                entity_type="person",
                field="ssn",
                rule="deny-pattern",
                pattern=r"^\d{3}-\d{2}-\d{4}$",
            ),
        )
        meta = {"uid": "u1", "type": "person", "name": "Alice", "ssn": "123-45-6789"}
        violations = check_entity_fields(meta, constraints)
        assert len(violations) == 1

    def test_never_mutates_meta(self) -> None:
        constraints = (
            FieldConstraint(entity_type="company", field="emails", rule="forbidden"),
        )
        meta = {
            "uid": "u1",
            "type": "company",
            "name": "Acme",
            "emails": ["bob@example.com"],
        }
        before = dict(meta)
        check_entity_fields(meta, constraints)
        assert meta == before  # AC7: report-only, never auto-repaired

    def test_body_pseudo_field_ac6_short_local_part_limit_is_honest(self) -> None:
        """AC6's real-data counter-example: a short-local-part shape rule
        cannot tell a genuine personal initials address from a role
        address. This mechanism does not pretend otherwise — it applies
        whatever regex the operator supplies, literally, with no semantic
        owner-detection. Demonstrate the documented limit directly: a
        pattern permissive enough to admit `jd@example.com` (initials) also
        admits a same-shaped role-looking string with no way to tell them
        apart from the string alone."""
        constraints = (
            FieldConstraint(
                entity_type="company",
                field="emails",
                rule="allow-pattern",
                pattern=r"^[a-z]{2,4}@example\.com$",
            ),
        )
        # A real personal address (initials) and a role address are
        # SHAPE-IDENTICAL under this pattern -- both pass. The mechanism
        # cannot distinguish them; it only checks the string shape.
        initials_meta = {
            "uid": "u1",
            "type": "company",
            "name": "Acme",
            "emails": ["jd@example.com"],
        }
        role_meta = {
            "uid": "u2",
            "type": "company",
            "name": "Acme",
            "emails": ["it@example.com"],
        }
        assert check_entity_fields(initials_meta, constraints) == []
        assert check_entity_fields(role_meta, constraints) == []

    def test_body_field_checks_body_text_not_frontmatter(self) -> None:
        """Tier-3 create/merge writers only ever emit BODY text (see
        athenaeum.tiers.tier3_entity_from_text / tier3_merge) -- a
        constraint that can only see frontmatter could never catch AC5's
        counter-example. `field: body` closes that gap."""
        constraints = (
            FieldConstraint(
                entity_type="company",
                field=BODY_PSEUDO_FIELD,
                rule="deny-pattern",
                pattern=r"[\w.+-]+@[\w-]+\.[\w.-]+",
            ),
        )
        meta = {"uid": "u1", "type": "company", "name": "Acme"}
        clean_violations = check_entity_fields(meta, constraints, body="No contact info here.")
        assert clean_violations == []
        dirty_violations = check_entity_fields(
            meta, constraints, body="Contact Bob at bob@example.com for details."
        )
        assert len(dirty_violations) == 1
        assert dirty_violations[0].field == BODY_PSEUDO_FIELD
        assert "bob@example.com" in (dirty_violations[0].value or "")


# ---------------------------------------------------------------------------
# scan_field_constraint_violations (AC4 detector)
# ---------------------------------------------------------------------------


class TestScanFieldConstraintViolationsEmptySchema:
    def test_no_constraints_scans_nothing(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_page(wiki_root, "a.md", uid="u1", page_type="company", name="Acme")
        assert scan_field_constraint_violations(wiki_root) == []

    def test_missing_wiki_root_returns_empty_when_unset(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "does-not-exist"
        assert scan_field_constraint_violations(wiki_root) == []


class TestScanFieldConstraintViolationsPopulatedSchema:
    def test_detects_violating_page_across_corpus(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(wiki_root, "| company | emails | forbidden | |\n")
        clean = wiki_root / "clean.md"
        clean.write_text(
            "---\nuid: u1\ntype: company\nname: Initech\n---\n\nNo contact fields.\n",
            encoding="utf-8",
        )
        dirty = wiki_root / "dirty.md"
        dirty.write_text(
            "---\nuid: u2\ntype: company\nname: Acme\nemails: [bob@example.com]\n---\n\nBody.\n",
            encoding="utf-8",
        )
        violations = scan_field_constraint_violations(wiki_root)
        assert len(violations) == 1
        assert violations[0].path == "dirty.md"
        assert violations[0].uid == "u2"
        assert violations[0].value == "bob@example.com"

    def test_underscore_prefixed_pages_excluded_from_scan(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(wiki_root, "| company | emails | forbidden | |\n")
        hidden = wiki_root / "_type_rejected.md"
        hidden.write_text(
            "---\nuid: u9\ntype: company\nname: Hidden\nemails: [x@example.net]\n---\n\nBody.\n",
            encoding="utf-8",
        )
        assert scan_field_constraint_violations(wiki_root) == []


# ---------------------------------------------------------------------------
# guard_entity_field_constraints (write-boundary guard)
# ---------------------------------------------------------------------------


class TestGuardEntityFieldConstraintsEmptySchema:
    def test_every_write_admitted_with_no_declared_constraints(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        meta = {"uid": "u1", "type": "company", "name": "Acme", "emails": ["bob@example.com"]}
        rendered = (
            "---\nuid: u1\ntype: company\nname: Acme\nemails: [bob@example.com]\n---\n\nBody.\n"
        )
        admitted, violations = guard_entity_field_constraints(
            wiki_root, "acme.md", rendered, meta
        )
        assert admitted is True
        assert violations == ()
        # No side effects at all -- no ledger, no parked file.
        assert not (wiki_root / FIELD_CONSTRAINT_REJECTED_DIR_NAME).exists()
        assert not (wiki_root / FIELD_CONSTRAINT_LEDGER_NAME).exists()


class TestGuardEntityFieldConstraintsPopulatedSchema:
    def test_violating_write_refused_parked_and_ledgered(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(wiki_root, "| company | emails | forbidden | |\n")
        meta = {"uid": "u1", "type": "company", "name": "Acme", "emails": ["bob@example.com"]}
        rendered = (
            "---\nuid: u1\ntype: company\nname: Acme\nemails: [bob@example.com]\n---\n\nBody.\n"
        )
        admitted, violations = guard_entity_field_constraints(
            wiki_root, "acme.md", rendered, meta, source="test"
        )
        assert admitted is False
        assert len(violations) == 1
        # AC7: never silently repaired -- the FULL rendered content is
        # parked byte-for-byte, not a stripped/edited version.
        parked = wiki_root / FIELD_CONSTRAINT_REJECTED_DIR_NAME / "acme.md"
        assert parked.read_text(encoding="utf-8") == rendered
        # And the original target path was never written.
        assert not (wiki_root / "acme.md").exists()
        records = list_field_constraint_violations(wiki_root)
        assert len(records) == 1
        assert records[0]["field"] == "emails"
        assert records[0]["value"] == "bob@example.com"
        assert records[0]["source"] == "test"

    def test_clean_write_admitted(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(wiki_root, "| company | emails | forbidden | |\n")
        meta = {"uid": "u1", "type": "company", "name": "Initech"}
        rendered = "---\nuid: u1\ntype: company\nname: Initech\n---\n\nBody.\n"
        admitted, violations = guard_entity_field_constraints(
            wiki_root, "initech.md", rendered, meta
        )
        assert admitted is True
        assert violations == ()

    def test_body_field_violation_refuses_a_llm_authored_page(self, tmp_path: Path) -> None:
        """AC5's exact counter-example, at the guard level: a rendered page
        whose BODY (not frontmatter) carries the forbidden address."""
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(
            wiki_root,
            "| company | body | deny-pattern | [\\w.+-]+@[\\w-]+\\.[\\w.-]+ |\n",
        )
        meta = {"uid": "u1", "type": "company", "name": "Acme"}
        rendered = (
            "---\nuid: u1\ntype: company\nname: Acme\n---\n\n"
            "Contact Bob at bob@example.com for details.\n"
        )
        admitted, violations = guard_entity_field_constraints(
            wiki_root, "acme.md", rendered, meta, source="tier3-create"
        )
        assert admitted is False
        assert len(violations) == 1
        assert violations[0].field == BODY_PSEUDO_FIELD


# ---------------------------------------------------------------------------
# Integration: the real Tier-3 write boundary (_apply_tier3_results)
# ---------------------------------------------------------------------------


class TestWritePathIntegrationEmptySchema:
    """AC2, at the real write boundary: no declared constraints -> a
    Tier-3 create AND a Tier-3 merge both behave exactly as before
    athenaeum#1416, including for a page that WOULD trip the worked
    example if it were declared."""

    def test_create_with_would_be_violating_body_still_writes_normally(
        self, tmp_path: Path
    ) -> None:
        wiki_root = _wiki_root(tmp_path)
        index = EntityIndex(wiki_root)
        result = ProcessingResult(raw_file=_raw())
        entity = WikiEntity(
            uid="abc12345",
            type="company",
            name="Acme Corp",
            body="Contact Bob at bob@example.com for details.",
        )

        _apply_tier3_results(
            result,
            new_entities=[entity],
            pending_updates=[],
            updated_uids=[],
            escalations=[],
            wiki_root=wiki_root,
            index=index,
            config=None,
        )

        assert result.field_constraint_rejected == 0
        assert len(result.created) == 1
        assert (wiki_root / entity.filename).exists()

    def test_merge_with_would_be_violating_body_still_writes_normally(
        self, tmp_path: Path
    ) -> None:
        wiki_root = _wiki_root(tmp_path)
        index = EntityIndex(wiki_root)
        existing = _write_page(
            wiki_root,
            "abc12345-acme-corp.md",
            uid="abc12345",
            page_type="company",
            name="Acme Corp",
        )
        result = ProcessingResult(raw_file=_raw())
        new_content = (
            "---\nuid: abc12345\ntype: company\nname: Acme Corp\n---\n\n"
            "Contact Bob at bob@example.com for details.\n"
        )

        _apply_tier3_results(
            result,
            new_entities=[],
            pending_updates=[(existing, new_content)],
            updated_uids=["abc12345"],
            escalations=[],
            wiki_root=wiki_root,
            index=index,
            config=None,
        )

        assert result.field_constraint_rejected == 0
        assert result.updated == ["abc12345"]
        assert existing.read_text(encoding="utf-8") == new_content


class TestWritePathIntegrationPopulatedSchema:
    """AC5's exact counter-example, driven through the real write-boundary
    function both Tier-3 paths call. A declared constraint forbidding an
    address in a company page's body REFUSES the write; it is not merely
    a prompt bias."""

    def test_create_writing_forbidden_address_into_body_is_refused(
        self, tmp_path: Path
    ) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(
            wiki_root,
            "| company | body | deny-pattern | [\\w.+-]+@[\\w-]+\\.[\\w.-]+ |\n",
        )
        index = EntityIndex(wiki_root)
        result = ProcessingResult(raw_file=_raw())
        entity = WikiEntity(
            uid="abc12345",
            type="company",
            name="Acme Corp",
            body="Contact Bob at bob@example.com for details.",
        )

        _apply_tier3_results(
            result,
            new_entities=[entity],
            pending_updates=[],
            updated_uids=[],
            escalations=[],
            wiki_root=wiki_root,
            index=index,
            config=None,
        )

        assert result.field_constraint_rejected == 1
        assert result.created == []
        assert not (wiki_root / entity.filename).exists()
        parked = wiki_root / FIELD_CONSTRAINT_REJECTED_DIR_NAME / entity.filename
        assert "bob@example.com" in parked.read_text(encoding="utf-8")
        records = list_field_constraint_violations(wiki_root)
        assert len(records) == 1
        assert records[0]["source"] == "tier3-create"

    def test_merge_writing_forbidden_address_into_body_is_refused_and_page_untouched(
        self, tmp_path: Path
    ) -> None:
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(
            wiki_root,
            "| company | body | deny-pattern | [\\w.+-]+@[\\w-]+\\.[\\w.-]+ |\n",
        )
        index = EntityIndex(wiki_root)
        existing = _write_page(
            wiki_root,
            "abc12345-acme-corp.md",
            uid="abc12345",
            page_type="company",
            name="Acme Corp",
        )
        original_content = existing.read_text(encoding="utf-8")
        result = ProcessingResult(raw_file=_raw())
        new_content = (
            "---\nuid: abc12345\ntype: company\nname: Acme Corp\n---\n\n"
            "Contact Bob at bob@example.com for details.\n"
        )

        _apply_tier3_results(
            result,
            new_entities=[],
            pending_updates=[(existing, new_content)],
            updated_uids=["abc12345"],
            escalations=[],
            wiki_root=wiki_root,
            index=index,
            config=None,
        )

        assert result.field_constraint_rejected == 1
        # AC7: nothing deleted, nothing silently repaired -- the
        # PRE-EXISTING page is left byte-for-byte untouched, and the
        # uid is NOT counted as updated (the merge never landed).
        assert existing.read_text(encoding="utf-8") == original_content
        assert result.updated == []
        records = list_field_constraint_violations(wiki_root)
        assert len(records) == 1
        assert records[0]["source"] == "tier3-merge"

    def test_frontmatter_forbidden_field_is_also_refused_at_the_guard(
        self, tmp_path: Path
    ) -> None:
        """A second, independent declared rule (frontmatter-based, an
        entity type unrelated to the contact-field worked example) at the
        same write-boundary guard the create/merge loops call, proving
        AC1's generality is not limited to the ``body`` pseudo-field."""
        wiki_root = _wiki_root(tmp_path)
        _write_field_constraints(wiki_root, "| project | budget_usd | forbidden | |\n")
        rendered = (
            "---\nuid: def45678\ntype: project\nname: Q4 Launch\n"
            "budget_usd: 500000\n---\n\nBody.\n"
        )
        meta = {
            "uid": "def45678",
            "type": "project",
            "name": "Q4 Launch",
            "budget_usd": 500000,
        }
        admitted, violations = guard_entity_field_constraints(
            wiki_root, "def45678-q4-launch.md", rendered, meta, source="tier3-create"
        )
        assert admitted is False
        assert len(violations) == 1
        assert violations[0].field == "budget_usd"
