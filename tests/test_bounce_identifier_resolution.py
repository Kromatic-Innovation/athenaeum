# SPDX-License-Identifier: Apache-2.0
"""Tests for identifier -> record resolution on the bounce mark (issue athenaeum#850).

Structure mirrors the issue's acceptance criteria:

- ``TestResolveContactRecord`` — the resolver itself: which record does an
  incoming address belong to, across ``emails:`` / ``former_emails:`` /
  ``alt_emails:``, case-insensitively and deterministically.
- ``TestMarkAnnotatesExistingRecord`` — the first criterion: marking an
  address that already appears in some record's ``emails:`` annotates THAT
  record rather than creating a sibling.
- ``TestSlugKeyedFallbackUnchanged`` — the athenaeum#765 shape is preserved verbatim
  when no record lists the address, so the fix adds a resolution step without
  changing what happens where there is nothing to resolve to.
- ``TestIsBouncedIdentifier`` — the address-level read predicate, which is
  what makes the annotation legible to a consumer (and what keeps a person
  record from reading as wholly expired).
- ``TestFoldOrphanedBounceMarks`` — the second criterion: the repairable path
  for pairs the previous behaviour already created, reporting a count.

All fixtures are synthetic — no client data lives in this public repo. Every
store below is built in ``tmp_path``; nothing reads a live store.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from athenaeum.models import parse_frontmatter
from athenaeum.pii import (
    FOLDED_INTO_FIELD,
    IDENTIFIER_VALIDITY_FIELD,
    find_orphaned_bounce_marks,
    fold_orphaned_bounce_marks,
    identifier_validity_entries,
    identifiers_on_record,
    is_bounced,
    is_bounced_identifier,
    mark_bounced,
    read_bounce_record,
    record_lists_identifier,
    resolve_contact_record,
)

PERSON_RECORD = (
    "---\n"
    "uid: '19052'\n"
    "name: Alex Example — contact record\n"
    "contact_of: Alex Example\n"
    "pii: true\n"
    "emails:\n"
    "  - alex@example.org\n"
    "  - alex.example@example.net\n"
    "---\n\n"
    "Archival contact data migrated off entity page 'Alex Example'.\n"
)


def _write_person_record(contacts_root: Path, text: str = PERSON_RECORD) -> Path:
    """Write a synthetic person record in the athenaeum#479/#502 migrator's shape."""
    contacts_root.mkdir(parents=True, exist_ok=True)
    path = contacts_root / "19052-alex-example.md"
    path.write_text(text, encoding="utf-8")
    return path


class TestResolveContactRecord:
    """Which existing record does an incoming address belong to?"""

    def test_resolves_by_emails_list(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        assert resolve_contact_record(contacts_root, "alex@example.org") == person

    def test_resolves_a_second_address_on_the_same_record(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        assert resolve_contact_record(contacts_root, "alex.example@example.net") == person

    def test_resolves_case_insensitively(self, tmp_path: Path) -> None:
        # Matching case-sensitively would mint a duplicate record for the same
        # address in different case — the very failure athenaeum#850 is about.
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        assert resolve_contact_record(contacts_root, "Alex@Example.org") == person

    def test_resolves_by_former_emails(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        contacts_root.mkdir(parents=True)
        path = contacts_root / "19053-sam-example.md"
        path.write_text(
            "---\nuid: '19053'\npii: true\nformer_emails:\n  - old@example.org\n---\n\nx\n",
            encoding="utf-8",
        )
        assert resolve_contact_record(contacts_root, "old@example.org") == path

    def test_unknown_address_resolves_to_nothing(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        _write_person_record(contacts_root)
        assert resolve_contact_record(contacts_root, "nobody@example.com") is None

    def test_missing_surface_resolves_to_nothing(self, tmp_path: Path) -> None:
        assert resolve_contact_record(tmp_path / "absent", "alex@example.org") is None

    def test_several_matches_resolve_deterministically(self, tmp_path: Path) -> None:
        # A genuinely shared address is legitimate; resolution must be stable
        # rather than a coin flip, so a re-report lands on the same record.
        contacts_root = tmp_path / "contacts"
        contacts_root.mkdir(parents=True)
        for stem in ("b-second", "a-first"):
            (contacts_root / f"{stem}.md").write_text(
                "---\npii: true\nemails:\n  - shared@example.org\n---\n\nx\n",
                encoding="utf-8",
            )
        first = resolve_contact_record(contacts_root, "shared@example.org")
        assert first is not None and first.name == "a-first.md"
        assert resolve_contact_record(contacts_root, "shared@example.org") == first

    def test_slug_keyed_record_is_not_matched_by_its_identifier_field(
        self, tmp_path: Path
    ) -> None:
        # ``identifier:`` is not an address LIST — a slug-keyed bounce record
        # must never be resolved to as though it were a person record, or the
        # fold in the other direction would never happen.
        contacts_root = tmp_path / "contacts"
        contacts_root.mkdir(parents=True)
        (contacts_root / "contact-alex-example-org.md").write_text(
            "---\nidentifier: alex@example.org\npii: true\nvalid_until: '2026-08-05'\n---\n\nx\n",
            encoding="utf-8",
        )
        assert resolve_contact_record(contacts_root, "alex@example.org") is None

    def test_identifiers_on_record_reads_all_three_fields(self) -> None:
        meta = {
            "emails": ["a@example.org"],
            "former_emails": ["b@example.org"],
            "alt_emails": ["c@example.org"],
        }
        assert identifiers_on_record(meta) == [
            "a@example.org",
            "b@example.org",
            "c@example.org",
        ]

    def test_malformed_list_values_are_skipped_not_fatal(self) -> None:
        meta = {"emails": [None, 42, "  ", "a@example.org"]}
        assert identifiers_on_record(meta) == ["a@example.org"]
        assert record_lists_identifier(meta, "a@example.org") is True


class TestMarkAnnotatesExistingRecord:
    """AC 1: annotate the record that already lists the address, not a sibling."""

    def test_annotates_person_record_and_creates_no_sibling(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)

        path, changed = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1 user unknown",
            observed_at="2026-08-05",
            source="script:voltaire-bounce-relay",
        )

        assert path == person
        assert changed is True
        # The whole point: exactly one record on the surface afterwards.
        assert sorted(p.name for p in contacts_root.glob("*.md")) == [person.name]

    def test_annotation_is_a_per_identifier_close(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1 user unknown",
            observed_at="2026-08-05",
            source="script:voltaire-bounce-relay",
        )

        meta = read_bounce_record(person)
        entries = identifier_validity_entries(meta)
        assert len(entries) == 1
        assert entries[0]["identifier"] == "alex@example.org"
        assert entries[0]["valid_until"] == "2026-08-05"
        assert entries[0]["bounce_diagnostic"] == "550 5.1.1 user unknown"
        assert entries[0]["source"] == "script:voltaire-bounce-relay"

    def test_person_record_is_not_closed_as_a_whole(self, tmp_path: Path) -> None:
        # A bare top-level valid_until would assert the PERSON expired — a
        # second silent-wrong-answer in place of the one athenaeum#850 fixes.
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )

        meta = read_bounce_record(person)
        assert "valid_until" not in meta
        assert is_bounced(meta, as_of=date(2026, 9, 1)) is False

    def test_pre_existing_fields_survive_the_annotation(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )

        meta, body = parse_frontmatter(person.read_text(encoding="utf-8"))
        assert meta["uid"] == "19052"
        assert meta["contact_of"] == "Alex Example"
        assert meta["emails"] == ["alex@example.org", "alex.example@example.net"]
        assert "Archival contact data migrated" in body

    def test_second_address_gets_its_own_entry(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        for identifier in ("alex@example.org", "alex.example@example.net"):
            mark_bounced(
                contacts_root,
                identifier,
                diagnostic="550 5.1.1",
                observed_at="2026-08-05",
                source="manual",
            )

        entries = identifier_validity_entries(read_bounce_record(person))
        assert [entry["identifier"] for entry in entries] == [
            "alex@example.org",
            "alex.example@example.net",
        ]

    def test_re_reporting_the_identical_fact_is_a_no_op(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        kwargs = {
            "diagnostic": "550 5.1.1",
            "observed_at": "2026-08-05",
            "source": "manual",
        }
        mark_bounced(contacts_root, "alex@example.org", **kwargs)  # type: ignore[arg-type]
        before = person.read_text(encoding="utf-8")

        _, changed = mark_bounced(contacts_root, "alex@example.org", **kwargs)  # type: ignore[arg-type]

        assert changed is False
        assert person.read_text(encoding="utf-8") == before

    def test_later_bounce_updates_the_entry_in_place(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        _, changed = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.2.1 mailbox disabled",
            observed_at="2026-09-01",
            source="manual",
        )

        entries = identifier_validity_entries(read_bounce_record(person))
        assert changed is True
        assert len(entries) == 1  # updated in place, never duplicated
        assert entries[0]["valid_until"] == "2026-09-01"
        assert entries[0]["bounce_diagnostic"] == "550 5.2.1 mailbox disabled"

    def test_explicit_record_path_still_wins(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        _write_person_record(contacts_root)
        chosen = contacts_root / "chosen.md"

        path, _ = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
            record_path=chosen,
        )
        assert path == chosen


class TestSlugKeyedFallbackUnchanged:
    """No record lists the address: athenaeum#765's shape is preserved verbatim."""

    def test_unknown_address_still_mints_the_slug_keyed_record(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        _write_person_record(contacts_root)

        path, changed = mark_bounced(
            contacts_root,
            "nobody@example.com",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )

        assert changed is True
        assert path.name == "contact-nobody-example-com.md"
        meta = read_bounce_record(path)
        assert meta["identifier"] == "nobody@example.com"
        assert meta["valid_until"] == "2026-08-05"
        assert is_bounced(meta, as_of=date(2026, 9, 1)) is True

    def test_empty_surface_mints_the_slug_keyed_record(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        path, _ = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        assert path.name == "contact-alex-example-org.md"
        assert IDENTIFIER_VALIDITY_FIELD not in read_bounce_record(path)


class TestIsBouncedIdentifier:
    """The address-level read predicate reads both record shapes."""

    def test_reads_the_per_identifier_close_on_a_person_record(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )

        meta = read_bounce_record(person)
        assert is_bounced_identifier(meta, "alex@example.org", as_of=date(2026, 9, 1)) is True
        # Inclusive last-valid date, exactly as is_bounced treats it.
        assert is_bounced_identifier(meta, "alex@example.org", as_of=date(2026, 8, 5)) is False

    def test_the_other_address_on_the_same_record_is_unaffected(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )

        meta = read_bounce_record(person)
        assert (
            is_bounced_identifier(meta, "alex.example@example.net", as_of=date(2026, 9, 1))
            is False
        )

    def test_reads_the_top_level_close_on_a_slug_keyed_record(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        path, _ = mark_bounced(
            contacts_root,
            "nobody@example.com",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        meta = read_bounce_record(path)
        assert is_bounced_identifier(meta, "nobody@example.com", as_of=date(2026, 9, 1)) is True

    def test_a_record_never_answers_for_a_neighbouring_address(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        path, _ = mark_bounced(
            contacts_root,
            "nobody@example.com",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        meta = read_bounce_record(path)
        assert is_bounced_identifier(meta, "someone@example.com", as_of=date(2026, 9, 1)) is False

    def test_absent_and_malformed_read_as_not_bounced(self) -> None:
        assert is_bounced_identifier({}, "alex@example.org") is False
        assert is_bounced_identifier(None, "alex@example.org") is False
        assert is_bounced_identifier({"identifier_validity": "nonsense"}, "a@b.org") is False
        assert is_bounced_identifier({"identifier_validity": [None, 7]}, "a@b.org") is False
        assert is_bounced_identifier({"emails": ["a@b.org"]}, "") is False

    def test_matches_case_insensitively(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        meta = read_bounce_record(person)
        assert is_bounced_identifier(meta, "ALEX@example.org", as_of=date(2026, 9, 1)) is True


class TestFoldOrphanedBounceMarks:
    """AC 2: the repairable path for pairs the previous behaviour already created."""

    def _store_with_orphaned_pair(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """A store in exactly the shape athenaeum#850 observed: two records, one person."""
        contacts_root = tmp_path / "contacts"
        person = _write_person_record(contacts_root)
        orphan = contacts_root / "contact-alex-example-org.md"
        orphan.write_text(
            "---\n"
            "identifier: alex@example.org\n"
            "pii: true\n"
            "bounce_diagnostic: 550 5.1.1 user unknown\n"
            "observed_at: '2026-08-05'\n"
            "valid_until: '2026-08-05'\n"
            "source: script:voltaire-bounce-relay\n"
            "---\n\nContact record.\n",
            encoding="utf-8",
        )
        return contacts_root, person, orphan

    def test_finds_the_pair(self, tmp_path: Path) -> None:
        contacts_root, person, orphan = self._store_with_orphaned_pair(tmp_path)

        found = find_orphaned_bounce_marks(contacts_root)

        assert len(found) == 1
        assert found[0].identifier == "alex@example.org"
        assert found[0].bounce_record == orphan
        assert found[0].person_record == person

    def test_folds_the_mark_onto_the_person_record_and_reports_a_count(
        self, tmp_path: Path
    ) -> None:
        contacts_root, person, _ = self._store_with_orphaned_pair(tmp_path)

        report = fold_orphaned_bounce_marks(contacts_root)

        assert report.count == 1
        assert report.dry_run is False
        meta = read_bounce_record(person)
        assert is_bounced_identifier(meta, "alex@example.org", as_of=date(2026, 9, 1)) is True
        entries = identifier_validity_entries(meta)
        assert entries[0]["bounce_diagnostic"] == "550 5.1.1 user unknown"
        assert entries[0]["source"] == "script:voltaire-bounce-relay"

    def test_dry_run_reports_without_writing(self, tmp_path: Path) -> None:
        contacts_root, person, orphan = self._store_with_orphaned_pair(tmp_path)
        before = (person.read_text(encoding="utf-8"), orphan.read_text(encoding="utf-8"))

        report = fold_orphaned_bounce_marks(contacts_root, dry_run=True)

        assert report.count == 1
        assert report.dry_run is True
        assert (person.read_text(encoding="utf-8"), orphan.read_text(encoding="utf-8")) == before

    def test_fold_deletes_nothing(self, tmp_path: Path) -> None:
        # Non-destructive by construction: the slug-keyed record stays, keeps
        # its own mark, and only GAINS the folded_into stamp.
        contacts_root, _, orphan = self._store_with_orphaned_pair(tmp_path)

        fold_orphaned_bounce_marks(contacts_root)

        assert orphan.exists()
        meta = read_bounce_record(orphan)
        assert meta["valid_until"] == "2026-08-05"
        assert meta[FOLDED_INTO_FIELD] == "19052"

    def test_fold_is_idempotent(self, tmp_path: Path) -> None:
        contacts_root, person, orphan = self._store_with_orphaned_pair(tmp_path)
        fold_orphaned_bounce_marks(contacts_root)
        after_first = (person.read_text(encoding="utf-8"), orphan.read_text(encoding="utf-8"))

        second = fold_orphaned_bounce_marks(contacts_root)

        assert second.count == 0
        assert (person.read_text(encoding="utf-8"), orphan.read_text(encoding="utf-8")) == (
            after_first
        )

    def test_slug_keyed_record_with_no_person_record_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        contacts_root = tmp_path / "contacts"
        contacts_root.mkdir(parents=True)
        mark_bounced(
            contacts_root,
            "nobody@example.com",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )

        report = fold_orphaned_bounce_marks(contacts_root)

        assert report.count == 0

    def test_unmarked_slug_keyed_record_is_not_a_pair(self, tmp_path: Path) -> None:
        # An identifier record with no bounce mark has nothing to fold.
        contacts_root = tmp_path / "contacts"
        _write_person_record(contacts_root)
        (contacts_root / "contact-alex-example-org.md").write_text(
            "---\nidentifier: alex@example.org\npii: true\n---\n\nx\n", encoding="utf-8"
        )

        assert find_orphaned_bounce_marks(contacts_root) == []

    def test_clean_store_reports_zero(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        _write_person_record(contacts_root)
        assert fold_orphaned_bounce_marks(contacts_root).count == 0

    def test_missing_surface_reports_zero(self, tmp_path: Path) -> None:
        assert fold_orphaned_bounce_marks(tmp_path / "absent").count == 0
