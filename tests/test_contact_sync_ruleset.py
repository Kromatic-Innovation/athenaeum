# SPDX-License-Identifier: Apache-2.0
"""Tests for the default contact-sync ruleset and the `google_resource_name`
source handle (issues athenaeum#902, athenaeum#1721).

Each test class is annotated with the AC it proves. The rule ENGINE is
athenaeum#901 (`tests/test_rules.py`) and the dispositions are athenaeum#903
(`tests/test_rules_dispositions.py`); this file covers only what athenaeum#902
adds — the handle registration and the packaged ruleset's behaviour against
recorded record fixtures.

athenaeum#1721 replaced this file's email-removal fixture. The original was
invented from the issue's prose (a `kind` discriminator, flat
`emails_before`/`emails_after` keys) rather than recorded from the producer,
and it is what let a rule that could never fire against a real record pass
this suite for months. `_email_removal_record` below is now the recorded
shape — `action`, `reason`, and a `before`/`after`-nested payload — taken
from a live `raw/contact-sync/*.semantic.jsonl` drop.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import yaml

from athenaeum.corrections import find_correction_batches, process_correction_record
from athenaeum.init import _RULE_EXAMPLE_FILES, copy_example_rules
from athenaeum.models import EntityIndex, parse_frontmatter
from athenaeum.pii import is_bounced_identifier
from athenaeum.registry import (
    SCALAR_HANDLE_KEYS,
    SOURCE_HANDLE_KEYS,
    build_registry,
    collect_handles,
)
from athenaeum.rules import load_rules, run_shape_rule_phase

_RULESET = ("contact-sync-skip.yaml", "contact-sync-email-removal.yaml")

#: The submitter a shape-rule-written batch carries
#: (`rules.write_correction_batch` call sites) — this, not the correction's
#: own `source`, is what §6.3's `writers` allowlist is checked against.
_SUBMITTER = "shape-rule:example-contact-sync-email-removal@1"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Contact Sync Test")


def _copy_ruleset(knowledge_root: Path, *, live: bool = True) -> None:
    """Install the packaged contact-sync examples, optionally flipped live.

    Flipping to `mode: live` is what an operator does after reviewing the
    ledger (docs/design/shape-rules.md §5); the packaged files themselves always
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


#: The uid the recorded fixture's producer carries, reused by the applier
#: tests below so target resolution is exercised against the same value the
#: rule actually reads.
_WIKI_UID = "90892f54"


def _email_removal_record(**overrides: object) -> dict:
    """A RECORDED contact-sync record that propagated an email removal.

    Shape taken from a live `raw/contact-sync/sync-<date>.semantic.jsonl`
    drop: `action` is the discriminator (never `kind`), `reason` separates a
    removal from the union path that shares the same action, and the address
    lists nest under `before`/`after`.
    """
    record = {
        "ts": "2026-08-14T03:00:00.123456+00:00",
        "action": "update_emails_applied",
        "account": "2",
        "resource_name": "people/c1234567890",
        "wiki_uid": _WIKI_UID,
        "reason": "email_removal_propagated",
        "before": {"emails": ["alex@example.org", "alex.old@example.org"]},
        "after": {"emails": ["alex@example.org"]},
    }
    record.update(overrides)
    return record


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
        # issue athenaeum#978 (S3): retirement now refuses against a store that
        # is not versioned rather than falling back to a silent unlink, so
        # this needs a real git repo to observe the dropped raw file.
        _git_init(tmp_path)
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
# athenaeum#902 AC5 + AC6 + AC7, restated by athenaeum#1721: an email removal
# compiles to an `identifier_validity` SOFT CLOSE (never a `remove`), routed
# by the EXISTING sensitivity routing (unchanged), and the ruleset produces no
# person-create operations.
# ---------------------------------------------------------------------------


class TestEmailRemovalCorrection:
    def _compile(self, tmp_path: Path, record: dict | None = None) -> dict:
        _copy_ruleset(tmp_path)
        _write_record(
            tmp_path / "raw",
            "20260814T030000Z-9f3ac1d0.jsonl",
            _email_removal_record() if record is None else record,
        )
        summary = _run(tmp_path)
        assert summary["dispositions"] == {"emit": 1}
        batches = find_correction_batches(tmp_path / "raw")
        assert len(batches) == 1
        path, _source, _envelope = batches[0]
        return json.loads(path.read_text(encoding="utf-8").splitlines()[1])

    def test_binds_against_the_recorded_producer_shape(self, tmp_path: Path) -> None:
        # athenaeum#1721 AC1/AC3. The rule reads `action`/`before`/`after` off
        # a record recorded from the producer -- the whole defect was a rule
        # whose match keys existed only in prose.
        record = self._compile(tmp_path)
        assert record["record"] == "correction"

    def test_removed_address_is_the_set_difference(self, tmp_path: Path) -> None:
        record = self._compile(tmp_path)
        # before - after == exactly the address that went away, reached
        # through the nested `before.emails` / `after.emails` path.
        assert record["value"]["identifier"] == "alex.old@example.org"

    def test_expressed_as_an_identifier_validity_close(self, tmp_path: Path) -> None:
        # athenaeum#1721 AC2: a soft close, never a hard `remove`. The entry
        # mirrors `pii.mark_bounced`'s shape so `pii.is_bounced_identifier`
        # -- the predicate outreach eligibility already consults -- reads it.
        record = self._compile(tmp_path)
        assert record["op"] == "add"
        assert record["field"] == "identifier_validity"
        assert record["field"] != "alt_emails"
        assert set(record["value"]) == {
            "identifier",
            "bounce_diagnostic",
            "observed_at",
            "valid_until",
            "source",
        }
        # Inclusive last-valid date, exactly as mark_bounced writes it.
        assert record["value"]["valid_until"] == "2026-08-14"
        assert record["value"]["observed_at"] == "2026-08-14"

    def test_close_names_contact_sync_not_the_mail_agent(
        self, tmp_path: Path
    ) -> None:
        record = self._compile(tmp_path)
        assert record["value"]["source"] == "script:contact-sync-email-removal"

    def test_the_entry_is_readable_by_is_bounced_identifier(
        self, tmp_path: Path
    ) -> None:
        # The whole point of the close: the existing read-side predicate
        # answers for the removed address and NOT for the retained one.
        record = self._compile(tmp_path)
        meta = {"identifier_validity": [record["value"]]}
        as_of = date(2026, 8, 15)
        assert is_bounced_identifier(meta, "alex.old@example.org", as_of) is True
        assert is_bounced_identifier(meta, "alex@example.org", as_of) is False

    def test_targets_the_person_by_the_producers_wiki_uid(
        self, tmp_path: Path
    ) -> None:
        # athenaeum#902's `google_resource_name` handle is registered but has
        # nothing seeding it onto person frontmatter, so a handle target
        # resolves to zero entities and raises forever. `wiki_uid` is carried
        # on every update record the producer emits.
        record = self._compile(tmp_path)
        assert record["target"] == {"uid": _WIKI_UID}

    def test_correction_is_machine_tier_and_conformant(
        self, tmp_path: Path
    ) -> None:
        record = self._compile(tmp_path)
        assert record["record"] == "correction"
        assert record["source"] == "script:example-contact-sync"
        assert record["observed_at"] == "2026-08-14T03:00:00.123456+00:00"
        assert "correction_id" in record

    def test_no_person_create_operations(self, tmp_path: Path) -> None:
        # AC7: contact-sync is update-only. Nothing the ruleset emits can
        # bring a person into existence -- and the `{uid: ...}` target shape
        # is one §3.3 explicitly does not create against.
        _copy_ruleset(tmp_path)
        rules, _ = load_rules(tmp_path)
        ops = {r.correction.op for r in rules if r.correction is not None}
        assert ops <= {"add"}
        assert "create" not in ops
        rule = next(r for r in rules if r.name == "example-contact-sync-email-removal")
        assert rule.correction is not None
        assert set(rule.correction.target) == {"uid"}

    def test_routing_code_is_not_touched_by_this_ruleset(
        self, tmp_path: Path
    ) -> None:
        # AC6: the correction NAMES a field; docs/design/field-corrections.md §7
        # makes target/field a proposal that the librarian's existing
        # sensitivity routing disposes. The rule carries no routing directive
        # of its own — there is no field by which it could.
        _copy_ruleset(tmp_path)
        rules, _ = load_rules(tmp_path)
        rule = next(r for r in rules if r.name == "example-contact-sync-email-removal")
        assert rule.correction is not None
        assert not hasattr(rule.correction, "route")
        assert rule.correction.field == "identifier_validity"


# ---------------------------------------------------------------------------
# athenaeum#1721 AC1: the rule must not fire on the record variants that share
# its action but do not mean "an address was removed".
# ---------------------------------------------------------------------------


class TestOnlyTheAppliedRemovalMatches:
    def _dispositions(self, tmp_path: Path, record: dict) -> dict:
        _copy_ruleset(tmp_path)
        _write_record(tmp_path / "raw", "20260814T030000Z-9f3ac1d0.jsonl", record)
        return _run(tmp_path)["dispositions"]

    def test_a_deferred_removal_does_not_compile(self, tmp_path: Path) -> None:
        # The producer emits the bare `update_emails` action for a removal it
        # DEFERRED (write budget, quota breaker, dry run, failure). Nothing
        # was removed upstream, so nothing may be closed on the wiki.
        record = _email_removal_record(
            action="update_emails",
            reason="deferred_write_budget_exhausted (limit=50)",
        )
        assert self._dispositions(tmp_path, record) == {}

    def test_a_union_does_not_compile(self, tmp_path: Path) -> None:
        # The union path shares the `update_emails_applied` action but adds
        # addresses; the set difference is empty and `first([])` is null, so
        # matching on the action alone would emit a null-identifier close.
        record = _email_removal_record(
            reason="patched",
            before={"emails": ["alex@example.org"]},
            after={"emails": ["alex@example.org", "alex.new@example.org"]},
        )
        assert self._dispositions(tmp_path, record) == {}

    def test_the_old_prose_shape_no_longer_matches_anything(
        self, tmp_path: Path
    ) -> None:
        # The fixture this file used to carry. Kept as a regression guard: if
        # a future edit reintroduces a `kind`/`emails_before` match, this goes
        # red rather than the suite quietly re-blessing an unfireable rule.
        record = {
            "kind": "update_contact",
            "resource_name": "people/c1234567890",
            "emails_before": ["alex@example.org", "alex.old@example.org"],
            "emails_after": ["alex@example.org"],
            "observed_at": "2026-08-14T03:00:00Z",
        }
        assert self._dispositions(tmp_path, record) == {}


# ---------------------------------------------------------------------------
# athenaeum#1721 AC4: a correction whose target does not resolve is a DEFINED
# no-op — a tier raise, never an exception and never a partial write.
# ---------------------------------------------------------------------------


class TestUnresolvedTargetIsADefinedNoop:
    def _config(self) -> dict:
        # The operator configuration the rule's header documents.
        return {
            "librarian": {
                "corrections": {
                    "fields": {
                        "identifier_validity": {
                            "shape": "list",
                            "writers": [_SUBMITTER],
                        }
                    },
                    "sensitive_fields": {"identifier_validity": "pii"},
                }
            },
            "storage": {"mapping": {"pii": "excluded"}},
        }

    def _apply(self, tmp_path: Path, record: dict) -> tuple:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "alex.md").write_text(
            f"---\nuid: {_WIKI_UID}\ntype: person\nname: Alex\n---\n\nBody.\n",
            encoding="utf-8",
        )
        index = EntityIndex(wiki)
        envelope = {
            "record": "batch",
            "schema_version": 1,
            "submitter": _SUBMITTER,
            "batch_id": "20260814T030000Z-9f3ac1d0",
            "created_at": "2026-08-14T03:00:00Z",
            "defaults": {},
        }
        result = process_correction_record(
            record,
            envelope,
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        return result, wiki

    def _record(self, uid: str) -> dict:
        return {
            "record": "correction",
            "target": {"uid": uid},
            "op": "add",
            "field": "identifier_validity",
            "value": {
                "identifier": "alex.old@example.org",
                "bounce_diagnostic": "removed upstream by contact-sync",
                "observed_at": "2026-08-14",
                "valid_until": "2026-08-14",
                "source": "script:contact-sync-email-removal",
            },
            "source": "script:example-contact-sync",
            "observed_at": "2026-08-14T03:00:00.123456+00:00",
        }

    def test_unmatched_target_raises_tier_without_raising_an_exception(
        self, tmp_path: Path
    ) -> None:
        result, _wiki = self._apply(tmp_path, self._record("no-such-uid"))
        assert result.disposition == "raised-tier"
        assert result.reason == "target resolves to zero or several entities"

    def test_unmatched_target_writes_nothing_anywhere(self, tmp_path: Path) -> None:
        before = {
            path: path.read_text(encoding="utf-8")
            for path in sorted((tmp_path / "wiki").rglob("*.md"))
        }
        _result, wiki = self._apply(tmp_path, self._record("no-such-uid"))
        after = {
            path: path.read_text(encoding="utf-8")
            for path in sorted(wiki.rglob("*.md"))
        }
        # No page minted, no page edited: a `{uid}` target that resolves to
        # zero entities never creates (field-corrections.md §3.3).
        assert {p.name for p in after} == {"alex.md"}
        assert before.get(wiki / "alex.md", after[wiki / "alex.md"]) == after[
            wiki / "alex.md"
        ]
        assert not (tmp_path / "excluded").exists()

    def test_the_resolvable_target_does_apply(self, tmp_path: Path) -> None:
        # The positive control: without it, the two tests above would pass
        # just as happily against a correction that could never apply at all.
        result, wiki = self._apply(tmp_path, self._record(_WIKI_UID))
        assert result.disposition == "routed-elsewhere"
        surface = tmp_path / "excluded" / f"{_WIKI_UID}.md"
        assert surface.is_file()
        meta, _body = parse_frontmatter(surface.read_text(encoding="utf-8"))
        assert is_bounced_identifier(
            meta, "alex.old@example.org", date(2026, 8, 15)
        ) is True
        # The close lands on the EXCLUDED surface, never on the public page.
        assert "alex.old@example.org" not in (wiki / "alex.md").read_text(
            encoding="utf-8"
        )


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
