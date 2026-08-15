# SPDX-License-Identifier: Apache-2.0
"""Tests for the default contact-sync ruleset and the `google_resource_name`
source handle (issue athenaeum#902).

Each test class is annotated with the AC it proves. The rule ENGINE is
athenaeum#901 (`tests/test_rules.py`) and the dispositions are athenaeum#903
(`tests/test_rules_dispositions.py`); this file covers only what athenaeum#902
adds — the handle registration and the packaged ruleset's behaviour against
recorded record fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from athenaeum.corrections import find_correction_batches
from athenaeum.init import _RULE_EXAMPLE_FILES, copy_example_rules
from athenaeum.registry import (
    SCALAR_HANDLE_KEYS,
    SOURCE_HANDLE_KEYS,
    build_registry,
    collect_handles,
)
from athenaeum.rules import load_rules, run_shape_rule_phase

_RULESET = ("contact-sync-skip.yaml", "contact-sync-email-removal.yaml")


def _copy_ruleset(knowledge_root: Path, *, live: bool = True) -> None:
    """Install the packaged contact-sync examples, optionally flipped live.

    Flipping to `mode: live` is what an operator does after reviewing the
    ledger (docs/shape-rules.md §5); the packaged files themselves always
    ship `observe`, which `TestPackagedRuleset` asserts separately.
    """
    rules_dir = knowledge_root / "rules"
    copy_example_rules(rules_dir)
    for fname in _RULESET:
        path = rules_dir / fname
        rule = yaml.safe_load(path.read_text(encoding="utf-8"))
        if live:
            rule["mode"] = "live"
        path.write_text(yaml.safe_dump(rule), encoding="utf-8")
    # Drop the non-contact-sync examples so a test's expectations concern
    # only the ruleset under test.
    for fname in _RULE_EXAMPLE_FILES:
        if fname not in _RULESET:
            (rules_dir / fname).unlink()


def _write_record(raw_root: Path, name: str, record: dict) -> Path:
    d = raw_root / "contact-sync"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _skip_record() -> dict:
    """A recorded contact-sync no-op — 91% of daily volume."""
    return {
        "kind": "skip_no_change",
        "resource_name": "people/c1234567890",
        "observed_at": "2026-08-14T03:00:00Z",
    }


def _email_removal_record() -> dict:
    """A recorded contact-sync update that removed one address."""
    return {
        "kind": "update_contact",
        "resource_name": "people/c1234567890",
        "emails_before": ["alex@example.org", "alex.old@example.org"],
        "emails_after": ["alex@example.org"],
        "observed_at": "2026-08-14T03:00:00Z",
    }


def _run(tmp_path: Path):
    return run_shape_rule_phase(
        raw_root=tmp_path / "raw",
        wiki_root=tmp_path / "wiki",
        knowledge_root=tmp_path,
        config=None,
    )


# ---------------------------------------------------------------------------
# AC1 + AC2: `google_resource_name` is registered as a source-handle key and
# resolves through the registry to a person uid; seeding follows the existing
# source-handle seeding pattern.
# ---------------------------------------------------------------------------


class TestGoogleResourceNameHandle:
    def test_registered_as_a_scalar_source_handle(self) -> None:
        assert "google_resource_name" in SCALAR_HANDLE_KEYS
        assert "google_resource_name" in SOURCE_HANDLE_KEYS

    def test_collect_handles_picks_it_up_from_frontmatter(self) -> None:
        # The EXISTING seeding pattern: `collect_handles` reads the key off
        # wiki frontmatter, exactly as it does for apollo_organization_id
        # (athenaeum#874). Registration is the whole of the wiring.
        handles = collect_handles({"google_resource_name": "people/c123"})
        assert handles["google_resource_name"] == "people/c123"

    def test_unset_handle_is_omitted_not_empty(self) -> None:
        assert collect_handles({"google_resource_name": ""}) == {}
        assert collect_handles({"google_resource_name": None}) == {}

    def test_resolves_through_the_registry_to_a_person_uid(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "alex.md").write_text(
            "---\n"
            "uid: person-alex\n"
            "type: person\n"
            "name: Alex\n"
            'google_resource_name: "people/c1234567890"\n'
            "---\n\nBody.\n",
            encoding="utf-8",
        )
        registry = build_registry(wiki)
        entry = registry["entities"]["person-alex"]
        assert entry["handles"]["google_resource_name"] == "people/c1234567890"

    def test_is_not_routed_to_the_pii_surface(self) -> None:
        # An opaque provider id is not a contact identifier. `email` is
        # deliberately NOT a SOURCE_HANDLE_KEYS member (corrections.py's
        # EMAIL_HANDLE_KEY docstring); a resource name deliberately IS.
        from athenaeum.corrections import EMAIL_HANDLE_KEY

        assert EMAIL_HANDLE_KEY not in SOURCE_HANDLE_KEYS
        assert "google_resource_name" in SOURCE_HANDLE_KEYS


# ---------------------------------------------------------------------------
# AC3: the ruleset ships as installer-copied packaged files, not as engine
# defaults.
# ---------------------------------------------------------------------------


class TestPackagedRuleset:
    def test_both_rules_are_in_the_installer_copy_set(self) -> None:
        for fname in _RULESET:
            assert fname in _RULE_EXAMPLE_FILES

    def test_installer_copies_them_into_a_knowledge_root(
        self, tmp_path: Path
    ) -> None:
        written, _skipped = copy_example_rules(tmp_path / "rules")
        for fname in _RULESET:
            assert fname in written
            assert (tmp_path / "rules" / fname).is_file()

    def test_packaged_files_ship_observe_mode(self, tmp_path: Path) -> None:
        # "The required first state for any new or edited rule" — a packaged
        # example must never arrive live.
        copy_example_rules(tmp_path / "rules")
        for fname in _RULESET:
            rule = yaml.safe_load(
                (tmp_path / "rules" / fname).read_text(encoding="utf-8")
            )
            assert rule["mode"] == "observe", fname

    def test_not_an_engine_default(self, tmp_path: Path) -> None:
        # Nothing loads from the package: a knowledge root with no rules/
        # directory has no rules at all.
        rules, errors = load_rules(tmp_path)
        assert rules == []
        assert errors == []

    def test_packaged_rules_are_schema_valid(self, tmp_path: Path) -> None:
        copy_example_rules(tmp_path / "rules")
        rules, errors = load_rules(tmp_path)
        assert errors == []
        names = {r.name for r in rules}
        assert "example-contact-sync-skip" in names
        assert "example-contact-sync-email-removal" in names


# ---------------------------------------------------------------------------
# AC4: the ruleset drops records whose payload is a no-op skip.
# ---------------------------------------------------------------------------


class TestSkipRecordsAreDropped:
    def test_skip_record_is_dropped(self, tmp_path: Path) -> None:
        _copy_ruleset(tmp_path)
        raw_path = _write_record(
            tmp_path / "raw", "20260814T030000Z-9f3ac1d0.jsonl", _skip_record()
        )
        summary = _run(tmp_path)

        assert summary["dispositions"] == {"drop": 1}
        assert not raw_path.exists()
        # A drop writes no correction — it is a discard, not a compile.
        assert find_correction_batches(tmp_path / "raw") == []

    def test_every_skip_variant_matches_the_glob(self, tmp_path: Path) -> None:
        _copy_ruleset(tmp_path)
        for i, kind in enumerate(
            ["skip_no_change", "skip_unchanged", "skip_filtered"]
        ):
            rec = _skip_record()
            rec["kind"] = kind
            _write_record(tmp_path / "raw", f"20260814T03000{i}Z-9f3ac1d{i}.jsonl", rec)
        summary = _run(tmp_path)
        assert summary["dispositions"] == {"drop": 3}

    def test_a_non_skip_record_is_not_dropped(self, tmp_path: Path) -> None:
        _copy_ruleset(tmp_path)
        _write_record(
            tmp_path / "raw",
            "20260814T030000Z-9f3ac1d0.jsonl",
            _email_removal_record(),
        )
        summary = _run(tmp_path)
        assert "drop" not in summary["dispositions"]


# ---------------------------------------------------------------------------
# AC5 + AC6 + AC7: an email removal compiles to a set-difference correction on
# `alt_emails`, routed by the EXISTING sensitivity routing (unchanged), and
# the ruleset produces no person-create operations.
# ---------------------------------------------------------------------------


class TestEmailRemovalCorrection:
    def _compile(self, tmp_path: Path) -> dict:
        _copy_ruleset(tmp_path)
        _write_record(
            tmp_path / "raw",
            "20260814T030000Z-9f3ac1d0.jsonl",
            _email_removal_record(),
        )
        summary = _run(tmp_path)
        assert summary["dispositions"] == {"emit": 1}
        batches = find_correction_batches(tmp_path / "raw")
        assert len(batches) == 1
        path, _source, _envelope = batches[0]
        return json.loads(path.read_text(encoding="utf-8").splitlines()[1])

    def test_removed_address_is_the_set_difference(self, tmp_path: Path) -> None:
        record = self._compile(tmp_path)
        # before - after == exactly the address that went away.
        assert record["value"] == ["alex.old@example.org"]

    def test_expressed_as_an_alt_emails_remove(self, tmp_path: Path) -> None:
        record = self._compile(tmp_path)
        assert record["op"] == "remove"
        assert record["field"] == "alt_emails"

    def test_targets_the_person_via_the_resource_name_handle(
        self, tmp_path: Path
    ) -> None:
        record = self._compile(tmp_path)
        assert record["target"] == {
            "type": "person",
            "handle": {"google_resource_name": "people/c1234567890"},
        }

    def test_correction_is_machine_tier_and_conformant(
        self, tmp_path: Path
    ) -> None:
        record = self._compile(tmp_path)
        assert record["record"] == "correction"
        assert record["source"] == "script:example-contact-sync"
        assert record["observed_at"] == "2026-08-14T03:00:00Z"
        assert "correction_id" in record

    def test_no_person_create_operations(self, tmp_path: Path) -> None:
        # AC7: contact-sync is update-only. `remove` is the only op the
        # ruleset emits — nothing here can bring a person into existence.
        _copy_ruleset(tmp_path)
        rules, _ = load_rules(tmp_path)
        ops = {r.correction.op for r in rules if r.correction is not None}
        assert ops <= {"remove"}
        assert "create" not in ops

    def test_routing_code_is_not_touched_by_this_ruleset(
        self, tmp_path: Path
    ) -> None:
        # AC6: the correction NAMES alt_emails; docs/field-corrections.md §7
        # makes target/field a proposal that the librarian's existing
        # sensitivity routing disposes. The rule carries no routing directive
        # of its own — there is no field by which it could.
        _copy_ruleset(tmp_path)
        rules, _ = load_rules(tmp_path)
        rule = next(r for r in rules if r.name == "example-contact-sync-email-removal")
        assert rule.correction is not None
        assert not hasattr(rule.correction, "route")
        assert rule.correction.field == "alt_emails"


# ---------------------------------------------------------------------------
# AC8: tests cover handle resolution and EACH rule's disposition against
# recorded record fixtures.
# ---------------------------------------------------------------------------


class TestBothDispositionsAgainstFixtures:
    def test_mixed_batch_dispositions_each_record_once(
        self, tmp_path: Path
    ) -> None:
        _copy_ruleset(tmp_path)
        _write_record(
            tmp_path / "raw", "20260814T030000Z-9f3ac1d0.jsonl", _skip_record()
        )
        _write_record(
            tmp_path / "raw",
            "20260814T030001Z-9f3ac1d1.jsonl",
            _email_removal_record(),
        )
        summary = _run(tmp_path)

        assert summary["dispositions"] == {"drop": 1, "emit": 1}
        assert summary["files_matched"] == 2
        # athenaeum#903's denominator invariant holds for this ruleset too.
        assert sum(summary["dispositions"].values()) == summary["files_matched"]

    def test_observe_mode_as_shipped_writes_nothing(self, tmp_path: Path) -> None:
        # The packaged files as they actually ship: everything is computed and
        # ledgered, nothing is written or removed.
        _copy_ruleset(tmp_path, live=False)
        skip_path = _write_record(
            tmp_path / "raw", "20260814T030000Z-9f3ac1d0.jsonl", _skip_record()
        )
        _write_record(
            tmp_path / "raw",
            "20260814T030001Z-9f3ac1d1.jsonl",
            _email_removal_record(),
        )
        summary = _run(tmp_path)

        assert summary["dispositions"] == {"observed-drop": 1, "observed-emit": 1}
        assert skip_path.exists()
        assert find_correction_batches(tmp_path / "raw") == []
