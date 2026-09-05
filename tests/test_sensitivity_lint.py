# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``storage.mapping`` completeness lint + the deferred
``(read_policy, adapter)`` pair check (issue athenaeum#993 — S5 of
``docs/design/sensitivity-class-vocabulary.md`` §9).

Mirrors ``tests/test_schemas.py``'s bundled-fixture convention
(``FIXTURE_ROOT = Path(__file__).parent / "fixtures" / ...``) for the
committed synthetic corpora this issue's AC requires, and
``tests/test_storage_migrate_pii.py``'s in-process ``cli.main([...])`` +
``capsys`` style for the CLI-level assertions.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from athenaeum._cmd_storage import EXIT_MAPPING_ISSUES
from athenaeum.cli import main
from athenaeum.sensitivity_lint import (
    FINDING_DANGLING_ADAPTER,
    FINDING_MISSING_MAPPING,
    FINDING_POLICY_MISMATCH,
    MappingFinding,
    SensitivityMappingLintResult,
    lint_read_policy_adapter_pairs,
    lint_sensitivity_storage_mapping,
    lint_storage_mapping_completeness,
    scan_sensitivity_class_names,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sensitivity_mapping"
CLEAN_ROOT = FIXTURE_ROOT / "clean"
MISSING_MAPPING_ROOT = FIXTURE_ROOT / "missing_mapping"
DANGLING_ADAPTER_ROOT = FIXTURE_ROOT / "dangling_adapter"

#: Pairs `hipaa`/`pii` with real adapters — the config the `clean/` fixture
#: tree is written against.
CLEAN_CONFIG = {
    "storage": {
        "mapping": {"hipaa": "hipaa-vault", "pii": "excluded"},
        "adapters": {
            "hipaa-vault": {
                "backing_store": "markdown",
                "surface_root": "hipaa",
                "corpus_policy": {
                    "embedded": False,
                    "recallable": False,
                    "merge_eligible": False,
                },
            }
        },
    }
}

#: No `secret` entry at all (the fixture's own gap) — `pii: excluded` is
#: included only so the built-in `pii` class's own D4 pair check stays quiet
#: in tests that combine both checks, keeping cross-test noise out of
#: assertions that are about `secret`, not `pii`. This is the config the
#: `missing_mapping/` fixture tree is written against.
MISSING_MAPPING_CONFIG: dict = {"storage": {"mapping": {"pii": "excluded"}}}

#: `classified` maps to an adapter name that is neither built in nor
#: registered under `storage.adapters` — the config the `dangling_adapter/`
#: fixture tree is written against. `pii: excluded` again keeps the built-in
#: `pii` class's D4 pair check quiet so it doesn't leak into `classified`-
#: focused assertions.
DANGLING_ADAPTER_CONFIG = {
    "storage": {"mapping": {"classified": "gov-classified-store", "pii": "excluded"}},
}


def _hash_tree(root: Path) -> dict[str, str]:
    """sha256 per file, keyed by path relative to *root* — for a byte-
    identical-before-and-after comparison that doesn't depend on mtimes."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# scan_sensitivity_class_names
# ---------------------------------------------------------------------------


class TestScanSensitivityClassNames:
    def test_clean_tree_finds_tagged_classes_only(self) -> None:
        found = scan_sensitivity_class_names(CLEAN_ROOT)
        assert set(found) == {"hipaa", "pii"}

    def test_untagged_page_is_not_a_finding(self) -> None:
        # ordinary-person.md has type: person but no sensitivity_class: —
        # must never appear as a class name (the false-positive this design
        # decision exists to avoid — see the module docstring).
        found = scan_sensitivity_class_names(CLEAN_ROOT)
        assert "person" not in found

    def test_paths_are_recorded_and_sorted(self) -> None:
        found = scan_sensitivity_class_names(CLEAN_ROOT)
        assert found["hipaa"] == [CLEAN_ROOT / "hipaa-record.md"]
        assert found["pii"] == [CLEAN_ROOT / "pii-record.md"]

    def test_corpus_root_scoping_scans_only_the_given_tree(self) -> None:
        # AC: "A test asserts that pointing it at a fixture tree scans only
        # that tree." Pointing at missing_mapping/ must not see secret's
        # sibling classes (hipaa/pii/classified) from the other fixture dirs.
        found = scan_sensitivity_class_names(MISSING_MAPPING_ROOT)
        assert set(found) == {"secret"}

    def test_missing_corpus_root_yields_empty(self, tmp_path: Path) -> None:
        found = scan_sensitivity_class_names(tmp_path / "does-not-exist")
        assert found == {}


# ---------------------------------------------------------------------------
# lint_storage_mapping_completeness — the three committed fixture cases
# ---------------------------------------------------------------------------


class TestCompletenessCheck:
    def test_clean_tree_passes(self) -> None:
        findings = lint_storage_mapping_completeness(CLEAN_CONFIG, CLEAN_ROOT)
        assert findings == []

    def test_missing_mapping_is_reported(self) -> None:
        findings = lint_storage_mapping_completeness(
            MISSING_MAPPING_CONFIG, MISSING_MAPPING_ROOT
        )
        assert len(findings) == 1
        finding = findings[0]
        assert finding.kind == FINDING_MISSING_MAPPING
        assert finding.class_name == "secret"
        assert finding.paths == (MISSING_MAPPING_ROOT / "secret-record.md",)
        assert not finding.advisory

    def test_dangling_adapter_is_reported(self) -> None:
        findings = lint_storage_mapping_completeness(
            DANGLING_ADAPTER_CONFIG, DANGLING_ADAPTER_ROOT
        )
        assert len(findings) == 1
        finding = findings[0]
        assert finding.kind == FINDING_DANGLING_ADAPTER
        assert finding.class_name == "classified"
        assert "gov-classified-store" in finding.detail
        assert not finding.advisory

    def test_missing_mapping_and_dangling_adapter_are_distinct_kinds(self) -> None:
        # AC: "reports it, distinctly from the first case."
        missing = lint_storage_mapping_completeness(
            MISSING_MAPPING_CONFIG, MISSING_MAPPING_ROOT
        )
        dangling = lint_storage_mapping_completeness(
            DANGLING_ADAPTER_CONFIG, DANGLING_ADAPTER_ROOT
        )
        assert missing[0].kind != dangling[0].kind

    def test_none_config_is_treated_as_no_mapping(self) -> None:
        findings = lint_storage_mapping_completeness(None, MISSING_MAPPING_ROOT)
        assert findings[0].kind == FINDING_MISSING_MAPPING


# ---------------------------------------------------------------------------
# lint_read_policy_adapter_pairs — the deferred Decision D4 pair check
# ---------------------------------------------------------------------------


class TestReadPolicyAdapterPairs:
    def _config(self, *, embedded: bool, access: str = "confidential") -> dict:
        return {
            "sensitivity": {
                "classes": {
                    "secret": {
                        "recognizers": [],
                        "read_policy": {"access": access},
                    }
                }
            },
            "storage": {
                # pii: excluded keeps the built-in pii class's own D4 pair
                # check quiet, so these assertions are only about `secret`.
                "mapping": {"secret": "secret-surface", "pii": "excluded"},
                "adapters": {
                    "secret-surface": {
                        "backing_store": "markdown",
                        "surface_root": "secret",
                        "corpus_policy": {
                            "embedded": embedded,
                            "recallable": False,
                            "merge_eligible": False,
                        },
                    }
                },
            },
        }

    def test_restricted_access_mapped_to_embedded_adapter_fires(self) -> None:
        # Direction 1: read_policy says restricted, storage says embedded —
        # the exact mismatch Decision D4 defers a lint for.
        findings = lint_read_policy_adapter_pairs(self._config(embedded=True))
        assert len(findings) == 1
        finding = findings[0]
        assert finding.kind == FINDING_POLICY_MISMATCH
        assert finding.class_name == "secret"
        assert finding.advisory
        assert finding.paths == ()

    def test_restricted_access_mapped_to_non_embedded_adapter_does_not_fire(
        self,
    ) -> None:
        # Direction 2: read_policy says restricted, storage says NOT
        # embedded — correctly paired, no finding.
        findings = lint_read_policy_adapter_pairs(self._config(embedded=False))
        assert findings == []

    def test_open_access_mapped_to_embedded_adapter_does_not_fire(self) -> None:
        # access outside {confidential, personal} is not "restricted" for
        # this check even when embedded — no false positive on ordinary,
        # unrestricted classes.
        findings = lint_read_policy_adapter_pairs(
            self._config(embedded=True, access="open")
        )
        assert findings == []

    def test_personal_access_also_counts_as_restricted(self) -> None:
        findings = lint_read_policy_adapter_pairs(
            self._config(embedded=True, access="personal")
        )
        assert len(findings) == 1
        assert findings[0].kind == FINDING_POLICY_MISMATCH

    def test_builtin_pii_class_fires_when_left_unmapped(self) -> None:
        # Fresh install, no sensitivity/storage config at all: pii's
        # read_policy.access is "personal" (restricted) but with no
        # storage.mapping.pii entry it resolves to the DEFAULT adapter,
        # which is embedded=True. This is a real, honest finding — an
        # unconfigured install has NOT actually excluded pii from the
        # corpus (see docs/design/sensitivity-class-vocabulary.md §5: exclusion
        # requires the operator to set storage.mapping.pii: excluded) — not
        # a bug in the check. Asserted explicitly so a future change to
        # either default is caught rather than silently changing this
        # lint's default-install behavior.
        findings = lint_read_policy_adapter_pairs(None)
        assert len(findings) == 1
        assert findings[0].class_name == "pii"

    def test_builtin_pii_class_quiet_once_mapped_to_excluded(self) -> None:
        findings = lint_read_policy_adapter_pairs(
            {"storage": {"mapping": {"pii": "excluded"}}}
        )
        assert findings == []


# ---------------------------------------------------------------------------
# Advisory / severity separability
# ---------------------------------------------------------------------------


class TestAdvisorySeparability:
    def test_is_clean_ignores_policy_only_findings(self) -> None:
        result = SensitivityMappingLintResult(
            completeness=(),
            policy=(
                MappingFinding(
                    kind=FINDING_POLICY_MISMATCH, class_name="secret", detail="x"
                ),
            ),
        )
        assert result.is_clean is True

    def test_is_clean_false_when_any_completeness_finding(self) -> None:
        result = SensitivityMappingLintResult(
            completeness=(
                MappingFinding(
                    kind=FINDING_MISSING_MAPPING, class_name="secret", detail="x"
                ),
            ),
            policy=(),
        )
        assert result.is_clean is False

    def test_findings_property_orders_completeness_first(self) -> None:
        completeness_finding = MappingFinding(
            kind=FINDING_MISSING_MAPPING, class_name="a", detail="x"
        )
        policy_finding = MappingFinding(
            kind=FINDING_POLICY_MISMATCH, class_name="b", detail="y"
        )
        result = SensitivityMappingLintResult(
            completeness=(completeness_finding,), policy=(policy_finding,)
        )
        assert result.findings == (completeness_finding, policy_finding)

    def test_combined_entry_point_runs_both_checks(self) -> None:
        result = lint_sensitivity_storage_mapping(
            MISSING_MAPPING_CONFIG, MISSING_MAPPING_ROOT
        )
        assert len(result.completeness) == 1
        assert result.is_clean is False


# ---------------------------------------------------------------------------
# Read-only — this lint never widens access or mutates anything (AC)
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_corpus_tree_and_config_unchanged_after_a_findings_run(self) -> None:
        before_hashes = _hash_tree(MISSING_MAPPING_ROOT)
        before_dangling_hashes = _hash_tree(DANGLING_ADAPTER_ROOT)
        config_before = copy.deepcopy(MISSING_MAPPING_CONFIG)
        dangling_config_before = copy.deepcopy(DANGLING_ADAPTER_CONFIG)

        result = lint_sensitivity_storage_mapping(
            MISSING_MAPPING_CONFIG, MISSING_MAPPING_ROOT
        )
        dangling_result = lint_storage_mapping_completeness(
            DANGLING_ADAPTER_CONFIG, DANGLING_ADAPTER_ROOT
        )

        assert result.completeness  # sanity: this run actually produced findings
        assert dangling_result  # sanity: this run actually produced findings
        assert _hash_tree(MISSING_MAPPING_ROOT) == before_hashes
        assert _hash_tree(DANGLING_ADAPTER_ROOT) == before_dangling_hashes
        assert MISSING_MAPPING_CONFIG == config_before
        assert DANGLING_ADAPTER_CONFIG == dangling_config_before


# ---------------------------------------------------------------------------
# Module docstring — "what it does not guarantee" (AC)
# ---------------------------------------------------------------------------


def test_module_docstring_states_what_it_does_not_guarantee() -> None:
    import athenaeum.sensitivity_lint as mod

    doc = mod.__doc__ or ""
    assert "does NOT guarantee" in doc
    assert "not a proof" in doc
    assert "mapping is complete for content it has not seen" in doc


def test_module_does_not_modify_storage_or_sensitivity_resolvers() -> None:
    # AC: "No enforcement is added to available_classes() or
    # resolve_adapter_for_class by this slice." This module only ever
    # calls into athenaeum.storage / athenaeum.sensitivity, never assigns
    # to or monkeypatches either.
    import inspect

    import athenaeum.sensitivity_lint as mod

    src = inspect.getsource(mod)
    assert "sensitivity.available_classes =" not in src
    assert "storage.resolve_adapter_for_class =" not in src


# ---------------------------------------------------------------------------
# CLI — `athenaeum storage lint-mapping`
# ---------------------------------------------------------------------------


class TestCli:
    def _write_config(self, root: Path, config: dict) -> None:
        import yaml

        root.mkdir(parents=True, exist_ok=True)
        (root / "athenaeum.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    def test_clean_exits_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        root = tmp_path / "knowledge"
        self._write_config(root, CLEAN_CONFIG)
        rc = main(
            [
                "storage", "lint-mapping",
                "--path", str(root),
                "--corpus", str(CLEAN_ROOT),
            ]
        )
        assert rc == 0
        assert "0 storage.mapping completeness finding" in capsys.readouterr().out

    def test_missing_mapping_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        root = tmp_path / "knowledge"
        self._write_config(root, MISSING_MAPPING_CONFIG)
        rc = main(
            [
                "storage", "lint-mapping",
                "--path", str(root),
                "--corpus", str(MISSING_MAPPING_ROOT),
            ]
        )
        assert rc == EXIT_MAPPING_ISSUES
        out = capsys.readouterr().out
        assert "missing_mapping" in out
        assert "secret" in out

    def test_dangling_adapter_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        root = tmp_path / "knowledge"
        self._write_config(root, DANGLING_ADAPTER_CONFIG)
        rc = main(
            [
                "storage", "lint-mapping",
                "--path", str(root),
                "--corpus", str(DANGLING_ADAPTER_ROOT),
            ]
        )
        assert rc == EXIT_MAPPING_ISSUES
        out = capsys.readouterr().out
        assert "dangling_adapter" in out

    def test_json_output_shape(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        import json

        root = tmp_path / "knowledge"
        self._write_config(root, MISSING_MAPPING_CONFIG)
        rc = main(
            [
                "storage", "lint-mapping",
                "--path", str(root),
                "--corpus", str(MISSING_MAPPING_ROOT),
                "--json",
            ]
        )
        assert rc == EXIT_MAPPING_ISSUES
        payload = json.loads(capsys.readouterr().out)
        assert payload["completeness"][0]["kind"] == FINDING_MISSING_MAPPING
        assert payload["completeness"][0]["class_name"] == "secret"
        assert payload["policy"] == []

    def test_default_corpus_root_is_the_knowledge_path_not_hardcoded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        # AC: never a hardcoded or environment-derived path. Omitting
        # --corpus falls back to --path (still caller-supplied), not to
        # some fixed location — an empty knowledge root scans as empty
        # and passes trivially.
        root = tmp_path / "knowledge"
        self._write_config(root, {})
        rc = main(["storage", "lint-mapping", "--path", str(root)])
        assert rc == 0

    def test_bare_storage_command_advertises_lint_mapping(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        rc = main(["storage"])
        assert rc == 2
        assert "lint-mapping" in capsys.readouterr().err
