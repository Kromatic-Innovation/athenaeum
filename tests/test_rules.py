# SPDX-License-Identifier: Apache-2.0
"""Tests for the shape-rule engine (issue athenaeum#901,
`docs/design/shape-rules.md` / `docs/design/field-corrections.md`).

Organized to map onto the issue's 13 acceptance criteria — each test class
below is annotated with the AC(s) it proves. Wiring-level phase-ordering
coverage (AC13) lives in ``TestPhaseWiring`` at the bottom, mirroring
``tests/test_librarian_corrections.py``'s own wiring-vs-unit split.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from athenaeum.corrections import find_correction_batches
from athenaeum.init import _RULE_EXAMPLE_FILES, copy_example_rules
from athenaeum.intake import discover_raw_files
from athenaeum.models import RawFile
from athenaeum.rules import (
    KNOWN_FUNCTIONS,
    MACHINE_TIER_SOURCE_TYPES,
    CorrectionSpec,
    FieldPredicate,
    MatchSpec,
    RuleLoadError,
    ShapeRule,
    ShapeRuleTransformError,
    _record_and_format,
    build_correction_record,
    load_rules,
    record_key_fingerprint,
    resolve_field_path,
    resolve_value_expr,
    run_shape_rule_phase,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Shape Rule Test")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CORRECTION = {
    "target": {"type": "person", "handle": {"email": "$email"}},
    "op": "set",
    "field": "bounced",
    "value": "$status_date",
    "source": "script:test-rule",
    "observed_at": "$observed_at",
}


def _rule_dict(**overrides) -> dict:
    d = {
        "version": 1,
        "name": "test-rule",
        "mode": "live",
        "match": {"source": "delivery-monitor", "format": "jsonl"},
        "disposition": "emit",
        "correction": dict(_BASE_CORRECTION),
    }
    d.update(overrides)
    return d


def _write_rule(rules_dir: Path, filename: str, rule: dict) -> Path:
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / filename
    path.write_text(yaml.safe_dump(rule), encoding="utf-8")
    return path


def _write_raw_jsonl(raw_root: Path, source: str, name: str, record: dict) -> Path:
    d = raw_root / source
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _record() -> dict:
    return {
        "status": "bounced",
        "email": "alex@example.org",
        "status_date": "2026-08-06",
        "observed_at": "2026-08-06T14:01:55Z",
    }


# ---------------------------------------------------------------------------
# AC1: rules load from <knowledge-root>/rules/*.yaml, schema-validated at
# run start.
# ---------------------------------------------------------------------------


class TestLoadRules:
    def test_loads_valid_rule(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _rule_dict())
        rules, errors = load_rules(tmp_path)
        assert len(rules) == 1
        assert errors == []
        assert rules[0].qualified_name == "test-rule@1"

    def test_no_rules_dir_is_empty_not_an_error(self, tmp_path: Path) -> None:
        rules, errors = load_rules(tmp_path)
        assert rules == []
        assert errors == []

    def test_only_yaml_glob_is_read(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "notes.txt").write_text("not a rule", encoding="utf-8")
        rules, errors = load_rules(tmp_path)
        assert rules == []
        assert errors == []

    def test_sorted_by_filename(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "b.yaml", _rule_dict(name="rule-b"))
        _write_rule(tmp_path / "rules", "a.yaml", _rule_dict(name="rule-a"))
        rules, _ = load_rules(tmp_path)
        assert [r.name for r in rules] == ["rule-a", "rule-b"]


# ---------------------------------------------------------------------------
# AC2: a malformed rule is skipped with a LOUD log line; its files take the
# ordinary tiered ladder.
# ---------------------------------------------------------------------------


class TestMalformedRuleSkip:
    def test_bad_yaml_skipped_with_loud_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "broken.yaml").write_text("{not: valid: yaml: [", encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger="athenaeum.rules"):
            rules, errors = load_rules(tmp_path)
        assert rules == []
        assert len(errors) == 1
        assert isinstance(errors[0], RuleLoadError)
        assert any(
            r.levelno >= logging.ERROR and "MALFORMED" in r.message for r in caplog.records
        )

    def test_schema_violation_skipped_with_loud_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad_correction = {
            **_BASE_CORRECTION,
            "source": "script:x",
            "value": {"fn": "eval", "args": []},
        }
        bad = _rule_dict(correction=bad_correction)
        _write_rule(tmp_path / "rules", "bad.yaml", bad)
        with caplog.at_level(logging.ERROR, logger="athenaeum.rules"):
            rules, errors = load_rules(tmp_path)
        assert rules == []
        assert len(errors) == 1
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_top_level_not_a_mapping_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "list.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger="athenaeum.rules"):
            rules, errors = load_rules(tmp_path)
        assert rules == []
        assert len(errors) == 1

    def test_malformed_rules_matching_files_take_ordinary_ladder(
        self, tmp_path: Path
    ) -> None:
        """A raw file that WOULD have matched a malformed rule is simply
        never evaluated against it -- it stays ordinary intake, discoverable
        by `discover_raw_files` exactly as if the rule never existed."""
        bad = _rule_dict(
            correction={**_BASE_CORRECTION, "value": {"fn": "not-a-real-fn", "args": []}}
        )
        _write_rule(tmp_path / "rules", "bad.yaml", bad)
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )

        summary = run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        assert summary["rules_loaded"] == 0
        assert summary["rules_skipped_malformed"] == 1
        assert raw_path.exists()
        # Ordinary tiered ladder still sees it (untouched, unclaimed).
        discovered = discover_raw_files(tmp_path / "raw")
        assert len(discovered) == 1
        assert discovered[0].path == raw_path


# ---------------------------------------------------------------------------
# AC3: match supports source directory, format, filename glob, record
# key-fingerprint, and field predicates (exact / glob / list membership).
# ---------------------------------------------------------------------------


class TestFieldPredicate:
    def test_exact(self) -> None:
        p = FieldPredicate.model_validate({"exact": "bounced"})
        assert p.matches("bounced")
        assert not p.matches("delivered")

    def test_glob(self) -> None:
        p = FieldPredicate.model_validate({"glob": "*@example.org"})
        assert p.matches("alex@example.org")
        assert not p.matches("alex@other.org")
        assert not p.matches(123)  # non-string never glob-matches

    def test_in_membership(self) -> None:
        p = FieldPredicate.model_validate({"in": ["cold", "warm"]})
        assert p.matches("cold")
        assert not p.matches("hot")

    def test_exactly_one_required_zero(self) -> None:
        with pytest.raises(PydanticValidationError):
            FieldPredicate.model_validate({})

    def test_exactly_one_required_multiple(self) -> None:
        with pytest.raises(PydanticValidationError):
            FieldPredicate.model_validate({"exact": "a", "glob": "b"})


class TestMatchSpec:
    def _raw(self, tmp_path: Path, source: str, name: str) -> RawFile:
        d = tmp_path / "raw" / source
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text("{}\n", encoding="utf-8")
        return RawFile(path=p, source=source, timestamp="", uuid8="")

    def test_source_match(self, tmp_path: Path) -> None:
        spec = MatchSpec.model_validate({"source": "delivery-monitor"})
        raw = self._raw(tmp_path, "delivery-monitor", "a.jsonl")
        other = self._raw(tmp_path, "other-source", "a.jsonl")
        assert spec.matches(raw=raw, record={}, fmt="jsonl")
        assert not spec.matches(raw=other, record={}, fmt="jsonl")

    def test_format_match(self, tmp_path: Path) -> None:
        spec = MatchSpec.model_validate({"format": "jsonl"})
        raw = self._raw(tmp_path, "s", "a.jsonl")
        assert spec.matches(raw=raw, record={}, fmt="jsonl")
        assert not spec.matches(raw=raw, record={}, fmt="md")

    def test_filename_glob_match(self, tmp_path: Path) -> None:
        spec = MatchSpec.model_validate({"filename_glob": "*-export.csv.md"})
        raw = self._raw(tmp_path, "s", "20260806-export.csv.md")
        other = self._raw(tmp_path, "s", "20260806-other.md")
        assert spec.matches(raw=raw, record={}, fmt="md")
        assert not spec.matches(raw=other, record={}, fmt="md")

    def test_key_fingerprint_match(self, tmp_path: Path) -> None:
        record = {"status": "bounced", "email": "a@b.com"}
        fp = record_key_fingerprint(record)
        spec = MatchSpec.model_validate({"key_fingerprint": fp})
        raw = self._raw(tmp_path, "s", "a.jsonl")
        assert spec.matches(raw=raw, record=record, fmt="jsonl")
        assert not spec.matches(raw=raw, record={"other": 1}, fmt="jsonl")

    def test_key_fingerprint_order_independent(self) -> None:
        assert record_key_fingerprint({"a": 1, "b": 2}) == record_key_fingerprint(
            {"b": 2, "a": 1}
        )

    def test_key_fingerprint_must_be_16_hex(self) -> None:
        with pytest.raises(PydanticValidationError):
            MatchSpec.model_validate({"key_fingerprint": "not-hex!"})

    def test_field_predicate_match_and_absent_field(self, tmp_path: Path) -> None:
        spec = MatchSpec.model_validate({"fields": {"status": {"exact": "bounced"}}})
        raw = self._raw(tmp_path, "s", "a.jsonl")
        assert spec.matches(raw=raw, record={"status": "bounced"}, fmt="jsonl")
        assert not spec.matches(raw=raw, record={"status": "delivered"}, fmt="jsonl")
        assert not spec.matches(raw=raw, record={}, fmt="jsonl")

    def test_all_present_keys_are_anded(self, tmp_path: Path) -> None:
        spec = MatchSpec.model_validate(
            {"source": "delivery-monitor", "fields": {"status": {"exact": "bounced"}}}
        )
        raw_match = self._raw(tmp_path, "delivery-monitor", "a.jsonl")
        raw_wrong_source = self._raw(tmp_path, "other", "a.jsonl")
        assert spec.matches(raw=raw_match, record={"status": "bounced"}, fmt="jsonl")
        assert not spec.matches(
            raw=raw_wrong_source, record={"status": "bounced"}, fmt="jsonl"
        )

    # -- issue athenaeum#974 AC1: nested frontmatter key resolution --------------

    def test_nested_field_one_level_below_record_root_matches(
        self, tmp_path: Path
    ) -> None:
        """The AC1 case named literally in the issue: a ``log_group`` value
        one level below the record root."""
        spec = MatchSpec.model_validate(
            {"fields": {"session.log_group": {"glob": "hestia-lanes-*"}}}
        )
        raw = self._raw(tmp_path, "hestia", "a.md")
        record = {"uid": "x", "session": {"log_group": "hestia-lanes-974"}}
        assert spec.matches(raw=raw, record=record, fmt="md")
        other = {"uid": "x", "session": {"log_group": "other-group"}}
        assert not spec.matches(raw=raw, record=other, fmt="md")

    def test_nested_field_missing_intermediate_key_no_match(
        self, tmp_path: Path
    ) -> None:
        spec = MatchSpec.model_validate(
            {"fields": {"session.log_group": {"exact": "hestia-lanes-974"}}}
        )
        raw = self._raw(tmp_path, "hestia", "a.md")
        assert not spec.matches(raw=raw, record={"uid": "x"}, fmt="md")

    def test_nested_field_non_dict_intermediate_no_match(
        self, tmp_path: Path
    ) -> None:
        spec = MatchSpec.model_validate(
            {"fields": {"session.log_group": {"exact": "hestia-lanes-974"}}}
        )
        raw = self._raw(tmp_path, "hestia", "a.md")
        # "session" resolves to a scalar, not a dict -- the path cannot walk
        # further, so this is "absent", never a crash.
        assert not spec.matches(raw=raw, record={"session": "not-a-dict"}, fmt="md")

    def test_exact_top_level_key_wins_over_dotted_interpretation(
        self, tmp_path: Path
    ) -> None:
        """Backward compatibility (issue athenaeum#974, non-negotiable): a
        literal top-level key that happens to contain a dot resolves as
        THAT key first -- this change must never reinterpret an existing
        rule's exact-key lookup as a nested path."""
        spec = MatchSpec.model_validate({"fields": {"a.b": {"exact": "literal"}}})
        raw = self._raw(tmp_path, "s", "a.md")
        record = {"a.b": "literal", "a": {"b": "nested-would-be-wrong"}}
        assert spec.matches(raw=raw, record=record, fmt="md")

    def test_every_existing_top_level_fields_rule_keeps_matching(
        self, tmp_path: Path
    ) -> None:
        """Every pre-athenaeum#974 top-level `fields` predicate keeps matching
        exactly as before -- same record, same predicate, same result."""
        spec = MatchSpec.model_validate({"fields": {"status": {"exact": "bounced"}}})
        raw = self._raw(tmp_path, "s", "a.jsonl")
        assert spec.matches(raw=raw, record={"status": "bounced"}, fmt="jsonl")
        assert not spec.matches(raw=raw, record={"status": "delivered"}, fmt="jsonl")
        assert not spec.matches(raw=raw, record={}, fmt="jsonl")


class TestMatchSpecUnclaimed:
    """`match.unclaimed` (issue athenaeum#1133): the opt-in that lets a rule
    reach audit-unclaimed candidates, its load-time illegal-key guard, and
    the hard partition `matches()` enforces between the two candidate
    kinds."""

    def _raw(self, tmp_path: Path, source: str, name: str) -> RawFile:
        d = tmp_path / "raw" / source
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text("hello\n", encoding="utf-8")
        return RawFile(path=p, source=source, timestamp="", uuid8="")

    def test_default_is_false_and_only_matches_ordinary_candidates(
        self, tmp_path: Path
    ) -> None:
        spec = MatchSpec.model_validate({"source": "s"})
        raw = self._raw(tmp_path, "s", "a.txt")
        assert spec.matches(raw=raw, record={}, fmt="txt")
        assert not spec.matches(raw=raw, record={}, fmt="txt", is_unclaimed=True)

    def test_unclaimed_true_matches_only_unclaimed_candidates(
        self, tmp_path: Path
    ) -> None:
        spec = MatchSpec.model_validate({"unclaimed": True, "source": "s"})
        raw = self._raw(tmp_path, "s", "a.txt")
        assert spec.matches(raw=raw, record={}, fmt="txt", is_unclaimed=True)
        assert not spec.matches(raw=raw, record={}, fmt="txt", is_unclaimed=False)

    def test_partition_holds_even_with_an_otherwise_empty_match_block(
        self, tmp_path: Path
    ) -> None:
        # Every OTHER key in `match:` is optional -- without the hard
        # partition, a bare `{unclaimed: true}` (or its false-default
        # sibling) would match every candidate of BOTH kinds.
        unclaimed_spec = MatchSpec.model_validate({"unclaimed": True})
        ordinary_spec = MatchSpec.model_validate({})
        raw = self._raw(tmp_path, "s", "a.txt")
        assert unclaimed_spec.matches(raw=raw, record={}, fmt="txt", is_unclaimed=True)
        assert not unclaimed_spec.matches(
            raw=raw, record={}, fmt="txt", is_unclaimed=False
        )
        assert ordinary_spec.matches(raw=raw, record={}, fmt="txt", is_unclaimed=False)
        assert not ordinary_spec.matches(
            raw=raw, record={}, fmt="txt", is_unclaimed=True
        )

    def test_unclaimed_with_fields_rejected_at_load_time(self) -> None:
        with pytest.raises(PydanticValidationError, match="match.fields"):
            MatchSpec.model_validate(
                {"unclaimed": True, "fields": {"status": {"exact": "x"}}}
            )

    def test_unclaimed_with_key_fingerprint_rejected_at_load_time(self) -> None:
        fp = record_key_fingerprint({"a": 1})
        with pytest.raises(PydanticValidationError, match="match.key_fingerprint"):
            MatchSpec.model_validate({"unclaimed": True, "key_fingerprint": fp})

    def test_unclaimed_with_format_rejected_at_load_time(self) -> None:
        with pytest.raises(PydanticValidationError, match="match.format"):
            MatchSpec.model_validate({"unclaimed": True, "format": "md"})

    def test_unclaimed_source_and_filename_glob_are_legal(
        self, tmp_path: Path
    ) -> None:
        spec = MatchSpec.model_validate(
            {"unclaimed": True, "source": "s", "filename_glob": "*.txt"}
        )
        raw = self._raw(tmp_path, "s", "a.txt")
        assert spec.matches(raw=raw, record={}, fmt="txt", is_unclaimed=True)

    def test_unclaimed_rule_rejects_emit_disposition_at_load_time(self) -> None:
        with pytest.raises(PydanticValidationError, match="cannot use disposition 'emit'"):
            ShapeRule.model_validate(
                {
                    "version": 1,
                    "name": "r",
                    "match": {"unclaimed": True, "source": "s"},
                    "disposition": "emit",
                    "correction": {
                        "target": {"uid": "u"},
                        "op": "set",
                        "field": "f",
                        "value": 1,
                        "source": "script:x",
                    },
                }
            )

    def test_unclaimed_rule_rejects_rollup_disposition_at_load_time(self) -> None:
        with pytest.raises(
            PydanticValidationError, match="cannot use disposition 'rollup'"
        ):
            ShapeRule.model_validate(
                {
                    "version": 1,
                    "name": "r",
                    "match": {"unclaimed": True, "source": "s"},
                    "disposition": "rollup",
                    "rollup": {"group_by": "$x", "aggregate": "count"},
                    "correction": {
                        "target": {"uid": "u"},
                        "op": "set",
                        "field": "f",
                        "value": 1,
                        "source": "script:x",
                    },
                }
            )

    def test_unclaimed_rule_allows_drop_retain_preserve_fallthrough(self) -> None:
        for disposition in ("drop", "retain", "fallthrough"):
            rule = ShapeRule.model_validate(
                {
                    "version": 1,
                    "name": "r",
                    "match": {"unclaimed": True, "source": "s"},
                    "disposition": disposition,
                }
            )
            assert rule.disposition == disposition
        preserve_rule = ShapeRule.model_validate(
            {
                "version": 1,
                "name": "r",
                "match": {"unclaimed": True, "source": "s"},
                "disposition": "preserve",
            }
        )
        assert preserve_rule.disposition == "preserve"

    def test_load_rules_records_a_rule_load_error_naming_fields(
        self, tmp_path: Path
    ) -> None:
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "bad.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "name": "r",
                    "match": {"unclaimed": True, "fields": {"a": {"exact": 1}}},
                    "disposition": "drop",
                }
            ),
            encoding="utf-8",
        )
        rules, errors = load_rules(tmp_path)
        assert rules == []
        assert len(errors) == 1
        assert isinstance(errors[0], RuleLoadError)
        assert "match.fields" in errors[0].reason


class TestResolveFieldPath:
    """Unit coverage for :func:`resolve_field_path` directly (issue
    athenaeum#974 AC1), independent of the ``MatchSpec.matches`` wiring above."""

    def test_top_level_key_found(self) -> None:
        assert resolve_field_path({"a": 1}, "a") == (True, 1)

    def test_top_level_key_absent(self) -> None:
        assert resolve_field_path({}, "a") == (False, None)

    def test_dotted_path_two_levels(self) -> None:
        record = {"session": {"log_group": "hestia-lanes-974"}}
        assert resolve_field_path(record, "session.log_group") == (
            True,
            "hestia-lanes-974",
        )

    def test_dotted_path_three_levels(self) -> None:
        record = {"a": {"b": {"c": "deep"}}}
        assert resolve_field_path(record, "a.b.c") == (True, "deep")

    def test_dotted_path_missing_leaf(self) -> None:
        record = {"session": {"other": "x"}}
        assert resolve_field_path(record, "session.log_group") == (False, None)

    def test_dotted_path_non_dict_intermediate(self) -> None:
        record = {"session": "scalar"}
        assert resolve_field_path(record, "session.log_group") == (False, None)

    def test_exact_key_with_literal_dot_takes_priority(self) -> None:
        record = {"a.b": "literal", "a": {"b": "nested"}}
        assert resolve_field_path(record, "a.b") == (True, "literal")


# ---------------------------------------------------------------------------
# Issue athenaeum#974 AC3: the intended `log_group: hestia-lanes-*` rule,
# demonstrated end-to-end against a fixture record shaped like a real
# hestia-lane raw file living in a nested source subdirectory -- proving
# BOTH gaps (AC1 nested-field resolution, AC2 nested-subdir discovery)
# together make the rule athenaeum#940 wants expressible.
# ---------------------------------------------------------------------------


class TestIntendedHestiaLanesRuleExpressible:
    def test_log_group_glob_rule_matches_nested_lane_fixture(
        self, tmp_path: Path
    ) -> None:
        # A fixture record shaped like a real hestia-lane raw file: the
        # `log_group` frontmatter key lives one level below the record root,
        # and the file itself lives one level below its source directory
        # (`raw/hestia/hestia-lanes-974/...`) -- exactly the two gaps the
        # issue names.
        raw_root = tmp_path / "raw"
        lane_file = raw_root / "hestia" / "hestia-lanes-974" / (
            "20260821T000000Z-aaaaaaaa.md"
        )
        lane_file.parent.mkdir(parents=True, exist_ok=True)
        lane_file.write_text(
            "---\nuid: lane-974\nsession:\n  log_group: hestia-lanes-974\n---\n"
            "lane body\n",
            encoding="utf-8",
        )
        # A non-matching sibling under a DIFFERENT log group -- proves the
        # glob is discriminating, not a rubber stamp.
        other_file = raw_root / "hestia" / "other-group" / (
            "20260821T000100Z-bbbbbbbb.md"
        )
        other_file.parent.mkdir(parents=True, exist_ok=True)
        other_file.write_text(
            "---\nuid: other\nsession:\n  log_group: other-group\n---\nbody\n",
            encoding="utf-8",
        )

        candidates = discover_raw_files(raw_root)
        assert len(candidates) == 2  # AC2: both nested files are discovered

        rule_spec = MatchSpec.model_validate(
            {
                "source": "hestia",
                "fields": {"session.log_group": {"glob": "hestia-lanes-*"}},
            }
        )

        matched_refs = []
        for raw in candidates:
            record, fmt = _record_and_format(raw)
            if rule_spec.matches(raw=raw, record=record, fmt=fmt):
                matched_refs.append(raw.ref)

        assert matched_refs == ["hestia/20260821T000000Z-aaaaaaaa.md"]


# ---------------------------------------------------------------------------
# AC4/AC5: transform = field interpolation + exactly the closed function
# vocabulary; unknown function fails validation; nothing eval'd / templated.
# ---------------------------------------------------------------------------


class TestTransformInterpolation:
    def test_field_reference_substitutes_whole_value(self) -> None:
        assert resolve_value_expr("$email", {"email": "a@b.com"}) == "a@b.com"

    def test_field_reference_preserves_type(self) -> None:
        assert resolve_value_expr("$count", {"count": [1, 2, 3]}) == [1, 2, 3]

    def test_missing_field_raises_transform_error(self) -> None:
        with pytest.raises(ShapeRuleTransformError):
            resolve_value_expr("$missing", {})

    def test_literal_string_passes_through_unchanged(self) -> None:
        assert resolve_value_expr("just a literal", {}) == "just a literal"

    def test_literal_containing_dollar_but_not_a_reference(self) -> None:
        # Not the EXACT "$name" form -- must not be treated as a reference.
        assert resolve_value_expr("cost is $5", {}) == "cost is $5"

    def test_no_partial_string_interpolation(self) -> None:
        # "field interpolation" is whole-value only -- embedding a field
        # inside a larger literal string is NOT a substitution.
        assert resolve_value_expr("prefix $email suffix", {"email": "x"}) == (
            "prefix $email suffix"
        )

    def test_nested_dict_and_list_resolved_recursively(self) -> None:
        expr = {"type": "person", "handle": {"email": "$email"}}
        assert resolve_value_expr(expr, {"email": "a@b.com"}) == {
            "type": "person",
            "handle": {"email": "a@b.com"},
        }

    def test_no_eval_no_template_language(self) -> None:
        """A value containing template-engine-looking or eval-looking
        syntax is treated as an ordinary opaque literal string -- nothing
        here is ever passed to `eval`/`exec`/Jinja/etc."""
        dangerous = "{{ 7 * 7 }}"
        assert resolve_value_expr(dangerous, {}) == dangerous
        dangerous2 = "__import__('os').system('echo pwned')"
        assert resolve_value_expr(dangerous2, {}) == dangerous2


class TestClosedFunctionVocabulary:
    def test_known_functions_are_exactly_three(self) -> None:
        assert KNOWN_FUNCTIONS == frozenset({"set_diff", "first", "date_of"})

    def test_first(self) -> None:
        assert resolve_value_expr({"fn": "first", "args": [["a", "b"]]}, {}) == "a"

    def test_first_empty_list(self) -> None:
        assert resolve_value_expr({"fn": "first", "args": [[]]}, {}) is None

    def test_first_wrong_arg_count(self) -> None:
        with pytest.raises(ShapeRuleTransformError):
            resolve_value_expr({"fn": "first", "args": [[1], [2]]}, {})

    def test_set_diff(self) -> None:
        result = resolve_value_expr(
            {"fn": "set_diff", "args": [["a", "b", "c"], ["b"]]}, {}
        )
        assert result == ["a", "c"]

    def test_set_diff_requires_lists(self) -> None:
        with pytest.raises(ShapeRuleTransformError):
            resolve_value_expr({"fn": "set_diff", "args": ["not-a-list", []]}, {})

    def test_date_of_iso_date(self) -> None:
        assert resolve_value_expr({"fn": "date_of", "args": ["2026-08-06"]}, {}) == (
            "2026-08-06"
        )

    def test_date_of_rfc3339(self) -> None:
        assert resolve_value_expr(
            {"fn": "date_of", "args": ["2026-08-06T14:01:55Z"]}, {}
        ) == "2026-08-06"

    def test_date_of_unparseable_raises(self) -> None:
        with pytest.raises(ShapeRuleTransformError):
            resolve_value_expr({"fn": "date_of", "args": ["not-a-date"]}, {})

    def test_functions_can_nest_and_reference_fields(self) -> None:
        record = {"backlinks": ["a", "b", "c"], "removed": ["b"]}
        expr = {"fn": "first", "args": [{"fn": "set_diff", "args": ["$backlinks", "$removed"]}]}
        assert resolve_value_expr(expr, record) == "a"

    def test_unknown_function_name_fails_schema_validation(self) -> None:
        rule = _rule_dict(
            correction={**_BASE_CORRECTION, "value": {"fn": "eval", "args": ["$status_date"]}}
        )
        with pytest.raises(PydanticValidationError, match="unknown transform function"):
            ShapeRule.model_validate(rule)

    def test_unknown_function_nested_inside_target_also_fails(self) -> None:
        rule = _rule_dict(
            correction={
                **_BASE_CORRECTION,
                "target": {
                    "type": "person",
                    "handle": {"email": {"fn": "exec", "args": ["$email"]}},
                },
            }
        )
        with pytest.raises(PydanticValidationError):
            ShapeRule.model_validate(rule)

    def test_fn_args_must_be_a_list(self) -> None:
        rule = _rule_dict(correction={**_BASE_CORRECTION, "value": {"fn": "first", "args": "nope"}})
        with pytest.raises(PydanticValidationError):
            ShapeRule.model_validate(rule)


# ---------------------------------------------------------------------------
# CorrectionSpec / ShapeRule schema shape
# ---------------------------------------------------------------------------


class TestCorrectionSpecSchema:
    def test_valid_correction_spec(self) -> None:
        spec = CorrectionSpec.model_validate(_BASE_CORRECTION)
        assert spec.op == "set"

    @pytest.mark.parametrize(
        "target",
        [
            {},
            {"foo": "bar"},
            {"uid": "x", "type": "y"},  # not one of the three exact shapes
            {"type": "person"},  # missing name/handle
        ],
    )
    def test_bad_target_shape_rejected(self, target: dict) -> None:
        with pytest.raises(PydanticValidationError):
            CorrectionSpec.model_validate({**_BASE_CORRECTION, "target": target})

    @pytest.mark.parametrize(
        "target",
        [
            {"uid": "person-alex-a1b2c3d4"},
            {"type": "person", "name": "Alex Doe"},
            {"type": "person", "handle": {"email": "a@b.com"}},
        ],
    )
    def test_good_target_shapes_accepted(self, target: dict) -> None:
        spec = CorrectionSpec.model_validate({**_BASE_CORRECTION, "target": target})
        assert spec.target == target

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            CorrectionSpec.model_validate({**_BASE_CORRECTION, "typo_key": 1})

    def test_bad_op_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            CorrectionSpec.model_validate({**_BASE_CORRECTION, "op": "clear"})


class TestShapeRuleSchema:
    def test_emit_requires_correction_block(self) -> None:
        rule = _rule_dict()
        del rule["correction"]
        with pytest.raises(PydanticValidationError, match="requires a 'correction' block"):
            ShapeRule.model_validate(rule)

    def test_fallthrough_forbids_correction_block(self) -> None:
        rule = _rule_dict(disposition="fallthrough")
        with pytest.raises(PydanticValidationError, match="must not carry"):
            ShapeRule.model_validate(rule)

    def test_fallthrough_without_correction_is_valid(self) -> None:
        rule = _rule_dict(disposition="fallthrough")
        del rule["correction"]
        parsed = ShapeRule.model_validate(rule)
        assert parsed.disposition == "fallthrough"
        assert parsed.correction is None

    def test_default_mode_is_observe(self) -> None:
        rule = _rule_dict()
        del rule["mode"]
        parsed = ShapeRule.model_validate(rule)
        assert parsed.mode == "observe"

    def test_bad_name_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ShapeRule.model_validate(_rule_dict(name="Not Valid!"))

    def test_bad_version_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ShapeRule.model_validate(_rule_dict(version=0))

    def test_qualified_name(self) -> None:
        parsed = ShapeRule.model_validate(_rule_dict(name="foo", version=3))
        assert parsed.qualified_name == "foo@3"

    def test_unknown_top_level_key_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ShapeRule.model_validate(_rule_dict(unexpected_key=True))


# ---------------------------------------------------------------------------
# AC8: a rule asserting a source above machine tier fails validation.
# ---------------------------------------------------------------------------


class TestMachineTierGuard:
    def test_machine_tier_types_are_script_and_api(self) -> None:
        assert MACHINE_TIER_SOURCE_TYPES == frozenset({"script", "api"})

    @pytest.mark.parametrize("source", ["script:my-rule", "api:my-vendor:2026"])
    def test_machine_tier_sources_accepted(self, source: str) -> None:
        spec = CorrectionSpec.model_validate({**_BASE_CORRECTION, "source": source})
        assert spec.source == source

    @pytest.mark.parametrize(
        "source",
        [
            "user:me",
            "linkedin:someone",
            "twitter:someone",
            "wikipedia:page",
            "claude:session-1",
            "model-prior:x",
            "unsourced:x",
        ],
    )
    def test_above_or_below_machine_tier_rejected(self, source: str) -> None:
        with pytest.raises(PydanticValidationError, match="machine"):
            CorrectionSpec.model_validate({**_BASE_CORRECTION, "source": source})

    def test_unparseable_source_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            CorrectionSpec.model_validate({**_BASE_CORRECTION, "source": "not-a-valid-source"})

    def test_dynamic_field_reference_source_rejected(self) -> None:
        """`source` must be a LITERAL -- a `$field` reference cannot assert
        precedence dynamically (see module docstring "Decisions")."""
        with pytest.raises(PydanticValidationError):
            CorrectionSpec.model_validate({**_BASE_CORRECTION, "source": "$declared_source"})

    def test_function_call_source_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            CorrectionSpec.model_validate(
                {**_BASE_CORRECTION, "source": {"fn": "first", "args": [["script:x"]]}}
            )


# ---------------------------------------------------------------------------
# Record extraction
# ---------------------------------------------------------------------------


class TestRecordExtraction:
    def test_jsonl_first_line_parsed(self, tmp_path: Path) -> None:
        raw = _write_raw_jsonl(tmp_path, "s", "a.jsonl", {"status": "bounced"})
        rf = RawFile(path=raw, source="s", timestamp="", uuid8="")
        record, fmt = _record_and_format(rf)
        assert fmt == "jsonl"
        assert record == {"status": "bounced"}

    def test_jsonl_malformed_first_line_is_empty_record(self, tmp_path: Path) -> None:
        d = tmp_path / "s"
        d.mkdir()
        p = d / "a.jsonl"
        p.write_text("not json\n", encoding="utf-8")
        rf = RawFile(path=p, source="s", timestamp="", uuid8="")
        record, fmt = _record_and_format(rf)
        assert record == {}
        assert fmt == "jsonl"

    def test_md_frontmatter_is_the_record(self, tmp_path: Path) -> None:
        d = tmp_path / "s"
        d.mkdir()
        p = d / "a.md"
        p.write_text("---\nstatus: bounced\nemail: a@b.com\n---\nBody.\n", encoding="utf-8")
        rf = RawFile(path=p, source="s", timestamp="", uuid8="")
        record, fmt = _record_and_format(rf)
        assert fmt == "md"
        assert record["status"] == "bounced"
        assert record["email"] == "a@b.com"

    def test_md_without_frontmatter_is_empty_record(self, tmp_path: Path) -> None:
        d = tmp_path / "s"
        d.mkdir()
        p = d / "a.md"
        p.write_text("Just prose, no frontmatter.\n", encoding="utf-8")
        rf = RawFile(path=p, source="s", timestamp="", uuid8="")
        record, fmt = _record_and_format(rf)
        assert record == {}
        assert fmt == "md"


# ---------------------------------------------------------------------------
# AC6: `emit` writes the §3.2 conformance format, consumed by the existing
# correction machinery with no changes to it.
# ---------------------------------------------------------------------------


class TestEmitDisposition:
    def test_emit_writes_conformant_batch(self, tmp_path: Path) -> None:
        # issue athenaeum#978 (S3): retirement now refuses against a store that
        # is not versioned rather than falling back to a silent unlink, so
        # this needs a real git repo to observe the compiled-away raw file.
        _git_init(tmp_path)
        _write_rule(tmp_path / "rules", "r1.yaml", _rule_dict())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        summary = run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        assert summary["dispositions"] == {"emit": 1}
        assert not raw_path.exists()  # compiled away

        batches = find_correction_batches(tmp_path / "raw")
        assert len(batches) == 1
        path, source, envelope = batches[0]
        assert source == "delivery-monitor"
        assert envelope["record"] == "batch"
        assert envelope["schema_version"] == 1

        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[1])
        assert record["record"] == "correction"
        assert record["target"] == {"type": "person", "handle": {"email": "alex@example.org"}}
        assert record["op"] == "set"
        assert record["field"] == "bounced"
        assert record["value"] == "2026-08-06"
        assert record["source"] == "script:test-rule"
        assert record["observed_at"] == "2026-08-06T14:01:55Z"
        assert "correction_id" in record

        # And the file is now INVISIBLE to ordinary discovery -- claimed by
        # the correction phase, exactly as any other conformant batch is.
        assert discover_raw_files(tmp_path / "raw") == []

    def test_emit_default_note_carries_rule_tag(self, tmp_path: Path) -> None:
        rule = _rule_dict(correction={k: v for k, v in _BASE_CORRECTION.items() if k != "note"})
        _write_rule(tmp_path / "rules", "r1.yaml", rule)
        _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        path, _source, _env = find_correction_batches(tmp_path / "raw")[0]
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
        assert "test-rule@1" in record["note"]

    def test_build_correction_record_default_observed_at(self) -> None:
        spec = CorrectionSpec.model_validate({**_BASE_CORRECTION, "observed_at": None})
        record = build_correction_record(spec, _record(), rule_tag="test-rule@1")
        # No explicit observed_at on the spec -- defaults to "now" (a valid
        # RFC-3339 UTC string), never absent.
        assert record["observed_at"].endswith("Z")

    def test_build_correction_record_transform_error_on_bad_target(self) -> None:
        spec = CorrectionSpec.model_validate(_BASE_CORRECTION)
        with pytest.raises(ShapeRuleTransformError):
            build_correction_record(spec, {}, rule_tag="test-rule@1")  # $email absent

    def test_emit_retires_via_git_recoverable_from_history(self, tmp_path: Path) -> None:
        """In a git-backed knowledge root, the compiled raw file is `git
        rm`'d after a provenance-snapshot commit -- recoverable from
        history, never hard-deleted (mirrors `corrections.retire_batch`'s
        two-commit pattern, own commit wording)."""
        _git_init(tmp_path)
        _write_rule(tmp_path / "rules", "r1.yaml", _rule_dict())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        # Deliberately left UNTRACKED (not pre-committed) so the retirement
        # path's provenance-snapshot commit has something to snapshot.

        run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        assert not raw_path.exists()

        log = _git(tmp_path, "log", "--oneline")
        assert "compiled into a correction batch" in log.stdout
        assert "provenance snapshot before compile" in log.stdout

        # Recoverable: the file's content is still reachable from history.
        rel = str(raw_path.relative_to(tmp_path))
        show = _git(tmp_path, "show", f"HEAD~1:{rel}")
        assert "bounced" in show.stdout

    def test_retire_refuses_against_fake_declaring_no_recovery_capability(
        self, tmp_path: Path
    ) -> None:
        """issue athenaeum#978 (S3, Tier B AC5): even with a REAL git repo
        present, an injected store fake declaring neither ``versioned`` nor
        ``purgeable`` (design note §4.4 R1) makes ``retire_compiled_raw_file``
        refuse — proving the gate is driven by the declared capability, not
        by probing ``knowledge_root / ".git"`` directly. The old silent
        ``unlink`` fallback is also gone: refusal leaves the file in place
        rather than discarding it unrecoverably."""
        from athenaeum.rules import retire_compiled_raw_file
        from tests.store_fakes import NoRecoveryStore

        _git_init(tmp_path)
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", "seed")

        ok = retire_compiled_raw_file(
            tmp_path, raw_path, rule_tag="test-rule@1", store=NoRecoveryStore()
        )
        assert ok is False
        assert raw_path.exists()


# ---------------------------------------------------------------------------
# AC7: `fallthrough` routes the record to the reasoning tiers.
# ---------------------------------------------------------------------------


class TestFallthroughDisposition:
    def test_fallthrough_leaves_file_untouched(self, tmp_path: Path) -> None:
        rule = _rule_dict(disposition="fallthrough", mode="live")
        del rule["correction"]
        _write_rule(tmp_path / "rules", "r1.yaml", rule)
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        summary = run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        assert summary["dispositions"] == {"fallthrough": 1}
        assert raw_path.exists()
        discovered = discover_raw_files(tmp_path / "raw")
        assert len(discovered) == 1
        assert discovered[0].path == raw_path
        # No correction batch was ever written.
        assert find_correction_batches(tmp_path / "raw") == []

    def test_transform_error_degrades_to_fallthrough(self, tmp_path: Path) -> None:
        rule = _rule_dict(
            mode="live",
            correction={**_BASE_CORRECTION, "value": {"fn": "date_of", "args": ["$status_date"]}},
        )
        _write_rule(tmp_path / "rules", "r1.yaml", rule)
        raw_path = _write_raw_jsonl(
            tmp_path / "raw",
            "delivery-monitor",
            "20260806T140211Z-9f3ac1d2.jsonl",
            {**_record(), "status_date": "not-a-date"},
        )
        summary = run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        assert summary["dispositions"] == {"transform-error": 1}
        assert raw_path.exists()
        discovered = discover_raw_files(tmp_path / "raw")
        assert len(discovered) == 1
        assert discovered[0].path == raw_path


# ---------------------------------------------------------------------------
# AC9: per-run record bounds are enforced, mirroring the existing
# librarian.corrections.* bounds.
# ---------------------------------------------------------------------------


class TestVolumeBound:
    def test_max_records_per_run_bounds_files_evaluated(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _rule_dict())
        for i in range(5):
            _write_raw_jsonl(
                tmp_path / "raw",
                "delivery-monitor",
                f"2026080{i}T140211Z-9f3ac1d{i}.jsonl",
                _record(),
            )
        config = {"librarian": {"shape_rules": {"max_records_per_run": 2}}}
        summary = run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=config,
        )
        assert summary["files_evaluated"] == 2
        assert summary["dispositions"]["emit"] == 2

    def test_env_override_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from athenaeum.config import resolve_shape_rules_max_records_per_run

        monkeypatch.setenv("ATHENAEUM_SHAPE_RULES_MAX_RECORDS_PER_RUN", "7")
        config = {"librarian": {"shape_rules": {"max_records_per_run": 2}}}
        assert resolve_shape_rules_max_records_per_run(config) == 7

    def test_runtime_share_default_and_yaml(self) -> None:
        from athenaeum.config import resolve_shape_rules_runtime_share

        assert resolve_shape_rules_runtime_share(None) == 0.05
        config = {"librarian": {"shape_rules": {"runtime_share": 0.2}}}
        assert resolve_shape_rules_runtime_share(config) == 0.2


# ---------------------------------------------------------------------------
# AC10/AC11: observe mode computes + ledgers dispositions while writing
# nothing else; ledger lines carry denominators and a rule@version tag.
# ---------------------------------------------------------------------------


class TestObserveModeAndLedger:
    def test_observe_mode_writes_no_batch_and_no_retirement(self, tmp_path: Path) -> None:
        rule = _rule_dict(mode="observe")
        _write_rule(tmp_path / "rules", "r1.yaml", rule)
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        summary = run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        assert summary["dispositions"] == {"observed-emit": 1}
        assert raw_path.exists()  # not retired
        assert find_correction_batches(tmp_path / "raw") == []  # no batch written

    def test_observe_mode_still_ledgers(self, tmp_path: Path) -> None:
        rule = _rule_dict(mode="observe")
        _write_rule(tmp_path / "rules", "r1.yaml", rule)
        _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        ledger_path = tmp_path / "wiki" / "_shape_rules_applied.jsonl"
        assert ledger_path.exists()
        lines = [json.loads(ln) for ln in ledger_path.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 1
        line = lines[0]
        assert line["rule"] == "test-rule@1"
        assert line["mode"] == "observe"
        assert line["dispositions"] == {"observed-emit": 1}
        # Denominator invariant (field-corrections.md §5.3 pattern).
        assert line["records_total"] == sum(line["dispositions"].values())

    def test_ledger_tag_is_rule_at_version(self, tmp_path: Path) -> None:
        rule = _rule_dict(name="my-rule", version=7, mode="live")
        _write_rule(tmp_path / "rules", "r1.yaml", rule)
        _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        ledger_path = tmp_path / "wiki" / "_shape_rules_applied.jsonl"
        line = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
        assert line["rule"] == "my-rule@7"

    def test_dry_run_writes_no_ledger(self, tmp_path: Path) -> None:
        rule = _rule_dict(mode="live")
        _write_rule(tmp_path / "rules", "r1.yaml", rule)
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        summary = run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
            dry_run=True,
        )
        assert summary["dispositions"] == {"observed-emit": 1}
        assert raw_path.exists()
        assert not (tmp_path / "wiki" / "_shape_rules_applied.jsonl").exists()
        assert find_correction_batches(tmp_path / "raw") == []

    def test_denominator_sums_across_multiple_matches(self, tmp_path: Path) -> None:
        rule = _rule_dict(mode="observe")
        _write_rule(tmp_path / "rules", "r1.yaml", rule)
        for i in range(3):
            _write_raw_jsonl(
                tmp_path / "raw",
                "delivery-monitor",
                f"2026080{i}T140211Z-9f3ac1d{i}.jsonl",
                _record(),
            )
        run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        ledger_path = tmp_path / "wiki" / "_shape_rules_applied.jsonl"
        line = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
        assert line["records_total"] == 3
        assert line["dispositions"]["observed-emit"] == 3


# ---------------------------------------------------------------------------
# AC12: example rules ship as PACKAGED files an installer copies in, never
# hardcoded engine defaults.
# ---------------------------------------------------------------------------


class TestPackagedExamples:
    def test_engine_ships_zero_rules_by_default(self, tmp_path: Path) -> None:
        # A fresh knowledge root with no rules/ dir at all -- the engine's
        # own defaults never seed one.
        rules, errors = load_rules(tmp_path)
        assert rules == []
        assert errors == []

    def test_copy_example_rules_writes_files(self, tmp_path: Path) -> None:
        dest = tmp_path / "rules"
        written, skipped = copy_example_rules(dest)
        assert skipped == []
        # The whole DECLARED packaged set is copied — tracked against the
        # constant rather than a frozen literal so a new example (athenaeum#902's
        # contact-sync ruleset, and later slices) does not silently fail this.
        assert set(written) == set(_RULE_EXAMPLE_FILES)
        # ...and the athenaeum#901 originals are still among them.
        assert {"contact-bounce.yaml", "unrecognized-export-fallthrough.yaml"} <= set(
            written
        )
        for fname in written:
            assert (dest / fname).exists()

    def test_copy_example_rules_skips_existing_without_force(self, tmp_path: Path) -> None:
        dest = tmp_path / "rules"
        copy_example_rules(dest)
        written, skipped = copy_example_rules(dest)
        assert written == []
        assert len(skipped) == len(_RULE_EXAMPLE_FILES)

    def test_copy_example_rules_force_overwrites(self, tmp_path: Path) -> None:
        dest = tmp_path / "rules"
        copy_example_rules(dest)
        written, skipped = copy_example_rules(dest, force=True)
        assert len(written) == len(_RULE_EXAMPLE_FILES)
        assert skipped == []

    def test_copied_examples_are_schema_valid_and_load(self, tmp_path: Path) -> None:
        copy_example_rules(tmp_path / "rules")
        rules, errors = load_rules(tmp_path)
        assert errors == []
        assert len(rules) == len(_RULE_EXAMPLE_FILES)
        for rule in rules:
            # Every packaged example ships in observe mode (docs/design/shape-rules.md §5/§7).
            assert rule.mode == "observe"

    def test_copying_examples_alone_activates_nothing(self, tmp_path: Path) -> None:
        """Copying the examples in does not, by itself, emit or retire
        anything -- they are observe-mode until an operator edits a copy."""
        copy_example_rules(tmp_path / "rules")
        _write_raw_jsonl(
            tmp_path / "raw",
            "delivery-monitor",
            "20260806T140211Z-9f3ac1d2.jsonl",
            {
                "status": "bounced",
                "email": "alex@example.org",
                "status_date": "2026-08-06",
                "observed_at": "2026-08-06T14:01:55Z",
            },
        )
        summary = run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        assert find_correction_batches(tmp_path / "raw") == []
        assert summary["dispositions"].get("emit", 0) == 0


# ---------------------------------------------------------------------------
# First-match-wins evaluation order
# ---------------------------------------------------------------------------


class TestFirstMatchWins:
    def test_first_matching_rule_by_filename_order_wins(self, tmp_path: Path) -> None:
        rule_a = _rule_dict(name="rule-a", mode="live", disposition="fallthrough")
        del rule_a["correction"]
        rule_b = _rule_dict(name="rule-b", mode="live")
        _write_rule(tmp_path / "rules", "a.yaml", rule_a)
        _write_rule(tmp_path / "rules", "b.yaml", rule_b)
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        summary = run_shape_rule_phase(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
            config=None,
        )
        # rule-a (alphabetically first, and it matches too) wins over rule-b.
        assert summary["dispositions"] == {"fallthrough": 1}
        assert raw_path.exists()


# ---------------------------------------------------------------------------
# AC13: the engine runs in the deterministic phase slot alongside
# `run_correction_phase` -- wiring-level test mirroring
# tests/test_librarian_corrections.py's own phase-ordering coverage.
# ---------------------------------------------------------------------------


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from athenaeum.librarian import RunContext, TokenUsage

    ctx = RunContext(
        raw_root=tmp_path / "raw",
        wiki_root=tmp_path / "wiki",
        knowledge_root=tmp_path,
        dry_run=False,
        max_files=None,
        max_api_calls=None,
        max_runtime=3600,
        cluster_only=False,
        merge_only=False,
        strict_budget=False,
        batch_mode=None,
        retire=None,
        push_after_run=None,
        pull_before_run=None,
        projects_root=None,
        install_signal_handlers=False,
        changed_paths=None,
        full_compile=False,
        now=datetime.now(timezone.utc),
        heartbeat=None,
        out_run_stats=None,
    )
    ctx.config = config
    ctx.usage = TokenUsage()
    ctx.run_deadline = None
    return ctx


class TestPhaseWiring:
    def test_shape_rule_phase_precedes_correction_phase_in_source(self) -> None:
        import inspect

        import athenaeum.librarian as librarian_mod

        source = inspect.getsource(librarian_mod.run)
        shape_idx = source.index("_run_shape_rule_phase(ctx)")
        corrections_idx = source.index("_run_correction_phase(ctx)")
        assert shape_idx < corrections_idx

    def test_shape_rule_phase_makes_zero_llm_calls(self, tmp_path: Path) -> None:
        from athenaeum.librarian import _run_shape_rule_phase

        rule = _rule_dict(mode="live")
        _write_rule(tmp_path / "rules", "r1.yaml", rule)
        _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        ctx = _make_ctx(tmp_path)
        _run_shape_rule_phase(ctx)
        assert ctx.usage.api_calls == 0
        assert ctx.shape_rules_summary is not None
        assert ctx.shape_rules_summary["dispositions"] == {"emit": 1}

    def test_same_run_visibility_batch_consumed_by_correction_phase(
        self, tmp_path: Path
    ) -> None:
        """A batch the shape-rule phase emits must be visible to the
        correction phase's OWN fresh scan LATER IN THE SAME RUN -- proving
        the "deterministic phase slot alongside run_correction_phase"
        claim, not merely "eventually, next run"."""
        from athenaeum.librarian import _run_correction_phase, _run_shape_rule_phase

        # issue athenaeum#978 (S3): retirement now refuses against a store
        # that is not versioned rather than falling back to a silent
        # unlink, so both phases' retirement need a real git repo.
        _git_init(tmp_path)
        rule = _rule_dict(mode="live")
        _write_rule(tmp_path / "rules", "r1.yaml", rule)
        _write_raw_jsonl(
            tmp_path / "raw", "delivery-monitor", "20260806T140211Z-9f3ac1d2.jsonl", _record()
        )
        ctx = _make_ctx(tmp_path)

        _run_shape_rule_phase(ctx)
        assert len(find_correction_batches(tmp_path / "raw")) == 1

        # Same ctx, same run -- the correction phase's own scan of raw_root
        # must pick up the batch the shape-rule phase just wrote.
        _run_correction_phase(ctx)
        assert ctx.corrections_summary is not None
        assert ctx.corrections_summary["batches_processed"] == 1
        # The batch was consumed (terminal on this pass -- unresolvable
        # target with no allowlisted field raises a tier, which is still
        # "consumed", not "still sitting there unprocessed").
        assert find_correction_batches(tmp_path / "raw") == []
