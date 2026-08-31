# SPDX-License-Identifier: Apache-2.0
"""Tests for the bounce-mark divergence report (issue athenaeum#853).

Structure mirrors the issue's acceptance criteria:

- ``TestDivergenceCases`` — the four cases the issue names: both surfaces
  populated and agreeing; both populated and diverging; one populated and one
  empty; an unreadable surface.
- ``TestEmptyIsNotUnreadable`` — the property both prior false negatives came
  from: an empty result and a failed scan must never render identically, in
  the data, in the rendered text, and in the exit code.
- ``TestOutputIsPublicSafe`` — no address, no name, no record path appears in
  any output form; identifiers are opaque handles only.
- ``TestNumbersAreReDerived`` — the counts come from the store passed in, and
  no figure from athenaeum#849/#853 is hard-coded anywhere.

The ``athenaeum bounce-divergence`` CLI subcommand these module functions
once backed was removed by issue athenaeum#1111 (superseded by ``athenaeum
surface-divergence --field bounced`` — see ``tests/test_surface_divergence.py``
for its CLI-level coverage); the former ``TestCli`` class and the
``--verbose``-flag test that imported the deleted ``_cmd_bounce_divergence``
module were removed with it. The module functions exercised below
(``compute_divergence``, ``render_report``, ``report_as_dict``, etc.) remain
in place — ``athenaeum.surface_divergence`` still wraps them unchanged.

All fixtures are synthetic and built in ``tmp_path``; nothing reads a live
store. No count quoted in athenaeum#849 or athenaeum#853 is asserted here as an
expected value — those are as-of-2026-08-12 observations of a private store,
not invariants.
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.bounce_divergence import (
    SurfaceStatus,
    compute_divergence,
    marked_identifiers,
    opaque_handle,
    record_has_bounce_mark,
    render_report,
    report_as_dict,
)
from athenaeum.pii import mark_bounced

ADDRESS = "alex@example.org"
SECOND_ADDRESS = "alex.example@example.net"
PERSON_NAME = "Alex Example"


def _person_record(contacts_root: Path, *, uid: str = "19052") -> Path:
    contacts_root.mkdir(parents=True, exist_ok=True)
    path = contacts_root / f"{uid}-alex-example.md"
    path.write_text(
        "---\n"
        f"uid: '{uid}'\n"
        f"name: {PERSON_NAME} — contact record\n"
        f"contact_of: {PERSON_NAME}\n"
        "pii: true\n"
        "emails:\n"
        f"  - {ADDRESS}\n"
        f"  - {SECOND_ADDRESS}\n"
        "---\n\nArchival contact data.\n",
        encoding="utf-8",
    )
    return path


def _wiki_page(wiki_root: Path, *, uid: str = "19052", bounced: str | None = None) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"uid: '{uid}'", "type: person", f"name: {PERSON_NAME}"]
    if bounced is not None:
        lines.append(f"bounced: {bounced}")
    lines += ["---", "", "An entity page.", ""]
    path = wiki_root / f"alex-example-{uid}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _mark(contacts_root: Path, identifier: str = ADDRESS) -> None:
    mark_bounced(
        contacts_root,
        identifier,
        diagnostic="550 5.1.1 user unknown",
        observed_at="2026-08-05",
        source="script:voltaire-bounce-relay",
    )


class TestDivergenceCases:
    """The four cases the issue's test criterion names."""

    def test_both_populated_and_agreeing(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        _wiki_page(wiki_root, bounced="MailboxDoesNotExist")
        _mark(contacts_root)

        report = compute_divergence(wiki_root, contacts_root)

        assert report.complete is True
        assert report.wiki.count == 1
        assert report.contacts.count == 1
        assert report.diverged is False
        assert report.marked_not_on_wiki == []
        assert report.on_wiki_not_marked == []

    def test_both_populated_and_diverging_in_both_directions(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        # One person is marked but has no wiki bounce; another carries a wiki
        # bounce but no mark — the report must see BOTH directions.
        _person_record(contacts_root, uid="19052")
        _wiki_page(wiki_root, uid="19052")  # no bounced: field
        _person_record(contacts_root, uid="19053")
        _wiki_page(wiki_root, uid="19053", bounced="DomainHasNullMx")
        _mark(contacts_root)  # lands on whichever record lists the address

        report = compute_divergence(wiki_root, contacts_root)

        assert report.complete is True
        assert report.diverged is True
        assert len(report.marked_not_on_wiki) == 1
        assert len(report.on_wiki_not_marked) == 1
        assert report.marked_not_on_wiki[0].kind == "digest"
        assert report.on_wiki_not_marked[0].kind == "uid"

    def test_wiki_populated_contacts_empty(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        contacts_root.mkdir(parents=True)
        _wiki_page(wiki_root, bounced="MailboxDoesNotExist")

        report = compute_divergence(wiki_root, contacts_root)

        assert report.complete is True
        assert report.wiki.count == 1
        assert report.contacts.count == 0
        assert report.contacts.status is SurfaceStatus.READ  # empty, and READ
        assert len(report.on_wiki_not_marked) == 1
        assert report.marked_not_on_wiki == []

    def test_contacts_populated_wiki_empty(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        wiki_root.mkdir(parents=True)
        _mark(contacts_root)

        report = compute_divergence(wiki_root, contacts_root)

        assert report.complete is True
        assert report.wiki.count == 0
        assert report.contacts.count == 1
        assert len(report.marked_not_on_wiki) == 1
        assert report.on_wiki_not_marked == []

    def test_unreadable_surface_is_reported_as_such(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        _mark(contacts_root)
        # wiki_root never created — a missing surface is NOT an empty one.

        report = compute_divergence(wiki_root, contacts_root)

        assert report.complete is False
        assert report.wiki.status is SurfaceStatus.MISSING
        assert report.wiki.detail is not None
        assert report.contacts.status is SurfaceStatus.READ

    def test_unreadable_page_makes_the_count_a_lower_bound(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        contacts_root.mkdir(parents=True)
        _wiki_page(wiki_root, bounced="MailboxDoesNotExist")
        (wiki_root / "binary.md").write_bytes(b"\xff\xfe\x00broken")

        report = compute_divergence(wiki_root, contacts_root)

        assert report.wiki.unreadable_paths == 1
        assert report.wiki.reliable is False
        assert report.complete is False
        assert "lower bound" in (report.wiki.detail or "")

    def test_clean_zero_report_is_not_an_error(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)  # a person record, but no bounce mark
        _wiki_page(wiki_root)  # a page, but no bounced: field

        report = compute_divergence(wiki_root, contacts_root)

        assert report.complete is True
        assert report.clean_zero is True
        assert report.diverged is False
        assert report.wiki.count == 0 and report.contacts.count == 0

    def test_wholly_empty_store_is_a_clean_zero(self, tmp_path: Path) -> None:
        (tmp_path / "contacts").mkdir()
        (tmp_path / "wiki").mkdir()
        report = compute_divergence(tmp_path / "wiki", tmp_path / "contacts")
        assert report.clean_zero is True

    def test_unjoinable_items_are_reported_not_hidden(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        contacts_root.mkdir(parents=True)
        wiki_root.mkdir(parents=True)
        (wiki_root / "no-uid.md").write_text(
            "---\ntype: person\nbounced: MailboxDoesNotExist\n---\n\nx\n", encoding="utf-8"
        )
        _mark(contacts_root)  # slug-keyed record: no uid

        report = compute_divergence(wiki_root, contacts_root)

        assert report.unjoinable_wiki_pages == 1
        assert report.unjoinable_marks == 1
        # Counted on each surface, but not miscounted as agreement or divergence.
        assert report.wiki.count == 1
        assert report.contacts.count == 1
        assert report.marked_not_on_wiki == []
        assert report.on_wiki_not_marked == []


class TestEmptyIsNotUnreadable:
    """An empty result and a failed scan must never render identically."""

    def test_data_distinguishes_them(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        empty = compute_divergence(tmp_path / "empty", tmp_path / "empty")
        missing = compute_divergence(tmp_path / "gone", tmp_path / "absent")

        assert empty.wiki.status is SurfaceStatus.READ
        assert missing.wiki.status is SurfaceStatus.MISSING
        assert empty.complete is True and missing.complete is False

    def test_rendered_text_distinguishes_them(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        empty_text = render_report(compute_divergence(tmp_path / "empty", tmp_path / "empty"))
        missing_text = render_report(compute_divergence(tmp_path / "gone", tmp_path / "absent"))

        assert empty_text != missing_text
        assert "NOT READ" in missing_text
        assert "NOT READ" not in empty_text
        assert "INCOMPLETE" in missing_text

    def test_the_mark_is_found_by_the_field_it_actually_writes(self, tmp_path: Path) -> None:
        # The false negative athenaeum#850 records: a `bounced:` key is NEVER
        # written on the contacts surface, so a report keying on it would
        # count 0 after a fully successful mark.
        contacts_root = tmp_path / "contacts"
        person = _person_record(contacts_root)
        _mark(contacts_root)

        assert "bounced:" not in person.read_text(encoding="utf-8")
        report = compute_divergence(tmp_path / "wiki", contacts_root)
        assert report.contacts.count == 1

    def test_record_has_bounce_mark_reads_both_shapes(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        _person_record(contacts_root)
        _mark(contacts_root)
        _mark(contacts_root, "nobody@example.com")  # slug-keyed fallback

        from athenaeum.pii import read_bounce_record

        person = read_bounce_record(contacts_root / "19052-alex-example.md")
        slug = read_bounce_record(contacts_root / "contact-nobody-example-com.md")
        assert record_has_bounce_mark(person) is True
        assert record_has_bounce_mark(slug) is True
        assert record_has_bounce_mark({}) is False
        assert record_has_bounce_mark(None) is False

    def test_marked_identifiers_is_per_address(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        _person_record(contacts_root)
        _mark(contacts_root)

        from athenaeum.pii import read_bounce_record

        meta = read_bounce_record(contacts_root / "19052-alex-example.md")
        assert marked_identifiers(meta) == [ADDRESS]  # not the second address


class TestOutputIsPublicSafe:
    """Aggregate counts and opaque handles only — never an address or a name."""

    def _populated(self, tmp_path: Path) -> tuple[Path, Path]:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        _wiki_page(wiki_root, uid="19053", bounced="MailboxDoesNotExist")
        _person_record(contacts_root, uid="19053")
        _mark(contacts_root)
        return wiki_root, contacts_root

    def test_rendered_text_leaks_nothing(self, tmp_path: Path) -> None:
        wiki_root, contacts_root = self._populated(tmp_path)
        text = render_report(compute_divergence(wiki_root, contacts_root))

        assert ADDRESS not in text
        assert SECOND_ADDRESS not in text
        assert PERSON_NAME not in text
        assert "alex" not in text.lower()  # catches slugified filenames too
        assert str(tmp_path) not in text

    def test_json_form_leaks_nothing(self, tmp_path: Path) -> None:
        wiki_root, contacts_root = self._populated(tmp_path)
        payload = json.dumps(report_as_dict(compute_divergence(wiki_root, contacts_root)))

        assert ADDRESS not in payload
        assert PERSON_NAME not in payload
        assert "alex" not in payload.lower()
        assert str(tmp_path) not in payload

    def test_handles_are_opaque_and_stable(self) -> None:
        handle = opaque_handle(ADDRESS)
        assert ADDRESS not in handle
        assert handle == opaque_handle(ADDRESS)
        assert handle == opaque_handle("ALEX@Example.org")  # case-normalized
        assert handle != opaque_handle(SECOND_ADDRESS)

    # The former test_no_verbose_mode_exists_to_leak_detail asserted this at
    # the CLI-flag level via the now-deleted `_cmd_bounce_divergence` module
    # (issue athenaeum#1111 removed the `bounce-divergence` subcommand); the
    # module itself never grew a verbose/detail mode, so nothing regresses.


class TestNumbersAreReDerived:
    """Counts come from the store passed in — nothing is hard-coded."""

    def test_counts_track_the_store(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        contacts_root.mkdir(parents=True)
        wiki_root.mkdir(parents=True)

        assert compute_divergence(wiki_root, contacts_root).wiki.count == 0

        for uid in ("1", "2", "3"):
            _wiki_page(wiki_root, uid=uid, bounced="MailboxDoesNotExist")
        assert compute_divergence(wiki_root, contacts_root).wiki.count == 3

        _wiki_page(wiki_root, uid="4", bounced="DomainHasNullMx")
        assert compute_divergence(wiki_root, contacts_root).wiki.count == 4

    def test_a_different_store_gives_a_different_report(self, tmp_path: Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        for root in (first, second):
            (root / "wiki").mkdir(parents=True)
            (root / "contacts").mkdir(parents=True)
        _wiki_page(first / "wiki", bounced="MailboxDoesNotExist")

        assert compute_divergence(first / "wiki", first / "contacts").wiki.count == 1
        assert compute_divergence(second / "wiki", second / "contacts").wiki.count == 0


