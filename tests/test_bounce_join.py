# SPDX-License-Identifier: Apache-2.0
"""Tests for the pii-mark <-> wiki ``bounced:`` join (issue athenaeum#852).

Structure mirrors the issue's acceptance criteria:

- ``TestJoinChain`` — the three join cases the issue names: an address with a
  matching person record AND a wiki page; an address with a person record but
  no wiki page; an address with neither. Plus the two other ways a real store
  breaks the chain (a person record with no ``uid``, a ``uid`` with no page).
- ``TestDeliverabilityForPage`` — the direction P6 dictates: a consumer
  holding a wiki page determines deliverability, and the pii mark reaches that
  determination by the documented path.
- ``TestEvidenceClassesStaySeparate`` — the load-bearing property: neither the
  join nor the read promotes a transient (``4.x``) or a list-verification
  verdict to a hard bounce, and no third recorded state is invented.
- ``TestReReportContract`` — the contract the design section states: an
  identical re-report is a byte-identical no-op; a different ``observed_at``
  updates in place, last-writer-wins.

All fixtures are synthetic — no client data lives in this public repo, and
nothing here reads a live store. No count from athenaeum#849 or athenaeum#852 is asserted
as an expected value; the figures in those issues are provenanced observations
of a private store, not invariants.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from athenaeum.bounce_join import (
    BounceJoin,
    deliverability_for_page,
    join_identifier,
    wiki_bounced_value,
    wiki_page_for_uid,
)
from athenaeum.pii import mark_bounced


def _person_record(contacts_root: Path, *, uid: str | None = "19052") -> Path:
    """A synthetic person record in the athenaeum#427/#437 migrator's shape."""
    contacts_root.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if uid is not None:
        lines.append(f"uid: '{uid}'")
    lines += [
        "name: Alex Example — contact record",
        "contact_of: Alex Example",
        "pii: true",
        "emails:",
        "  - alex@example.org",
        "  - alex.example@example.net",
        "---",
        "",
        "Archival contact data.",
        "",
    ]
    path = contacts_root / "19052-alex-example.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _wiki_page(wiki_root: Path, *, uid: str = "19052", bounced: str | None = None) -> Path:
    """A synthetic entity page carrying a ``uid`` and, optionally, ``bounced:``."""
    wiki_root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"uid: '{uid}'", "type: person", "name: Alex Example"]
    if bounced is not None:
        lines.append(f"bounced: {bounced}")
    lines += ["---", "", "An entity page.", ""]
    path = wiki_root / "alex-example.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _mark(contacts_root: Path, identifier: str = "alex@example.org") -> None:
    mark_bounced(
        contacts_root,
        identifier,
        diagnostic="550 5.1.1 user unknown",
        observed_at="2026-08-05",
        source="script:voltaire-bounce-relay",
    )


class TestJoinChain:
    """identifier -> person record -> uid -> wiki page, however far it reaches."""

    def test_address_with_person_record_and_wiki_page(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        person = _person_record(contacts_root)
        page = _wiki_page(wiki_root)
        _mark(contacts_root)

        join = join_identifier(contacts_root, wiki_root, "alex@example.org")

        assert join.joined is True
        assert join.reached == "wiki-page"
        assert join.person_record == person
        assert join.uid == "19052"
        assert join.wiki_page == page
        assert join.pii_marked is True

    def test_address_with_person_record_but_no_wiki_page(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        person = _person_record(contacts_root)
        wiki_root.mkdir(parents=True)
        _mark(contacts_root)

        join = join_identifier(contacts_root, wiki_root, "alex@example.org")

        assert join.joined is False
        assert join.reached == "uid"
        assert join.person_record == person
        assert join.uid == "19052"
        assert join.wiki_page is None
        assert join.pii_marked is True  # the mark is still readable

    def test_address_with_neither(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        contacts_root.mkdir(parents=True)
        wiki_root.mkdir(parents=True)

        join = join_identifier(contacts_root, wiki_root, "nobody@example.com")

        assert join == BounceJoin(identifier="nobody@example.com")
        assert join.joined is False
        assert join.reached == "identifier"
        assert join.pii_marked is False

    def test_person_record_without_a_uid_breaks_at_the_record(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root, uid=None)
        _wiki_page(wiki_root)
        _mark(contacts_root)

        join = join_identifier(contacts_root, wiki_root, "alex@example.org")

        assert join.reached == "person-record"
        assert join.uid is None
        assert join.pii_marked is True

    def test_second_address_on_the_record_joins_to_the_same_page(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root)

        join = join_identifier(contacts_root, wiki_root, "alex.example@example.net")

        assert join.wiki_page == page
        assert join.pii_marked is False  # unmarked address, joined all the same

    def test_unmarked_address_still_joins(self, tmp_path: Path) -> None:
        # The join is about reachability, not about a mark existing — a report
        # needs the "on the wiki surface but unmarked" direction too (athenaeum#853).
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        _wiki_page(wiki_root, bounced="MailboxDoesNotExist")

        join = join_identifier(contacts_root, wiki_root, "alex@example.org")

        assert join.joined is True
        assert join.pii_marked is False
        assert join.wiki_bounced == "MailboxDoesNotExist"

    def test_missing_roots_never_raise(self, tmp_path: Path) -> None:
        join = join_identifier(tmp_path / "absent", tmp_path / "gone", "a@example.org")
        assert join.reached == "identifier"

    def test_wiki_page_for_uid_skips_underscore_files_and_misses_cleanly(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        _wiki_page(wiki_root)
        (wiki_root / "_index.md").write_text("---\nuid: '99999'\n---\n\nx\n", encoding="utf-8")

        assert wiki_page_for_uid(wiki_root, "99999") is None
        assert wiki_page_for_uid(wiki_root, "nosuchuid") is None
        assert wiki_page_for_uid(wiki_root, "") is None

    def test_as_of_rewinds_the_mark(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        _wiki_page(wiki_root)
        _mark(contacts_root)

        # Inclusive last-valid date: still deliverable ON the observed date.
        assert (
            join_identifier(
                contacts_root, wiki_root, "alex@example.org", as_of=date(2026, 8, 5)
            ).pii_marked
            is False
        )
        assert (
            join_identifier(
                contacts_root, wiki_root, "alex@example.org", as_of=date(2026, 8, 6)
            ).pii_marked
            is True
        )


class TestDeliverabilityForPage:
    """A consumer holding a wiki page determines deliverability (P6)."""

    def test_pii_mark_reaches_the_page_holder(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root)
        _mark(contacts_root)

        results = deliverability_for_page(page, contacts_root)

        assert [r.identifier for r in results] == [
            "alex@example.org",
            "alex.example@example.net",
        ]
        assert results[0].hard_bounced is True
        assert results[1].hard_bounced is False  # per-address, not per-person
        assert results[0].any_evidence is True

    def test_page_with_no_uid_reports_nothing(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        wiki_root.mkdir(parents=True)
        page = wiki_root / "no-uid.md"
        page.write_text("---\ntype: person\n---\n\nx\n", encoding="utf-8")

        assert deliverability_for_page(page, contacts_root) == []

    def test_uid_with_no_person_record_reports_nothing(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        contacts_root.mkdir(parents=True)
        page = _wiki_page(wiki_root)

        assert deliverability_for_page(page, contacts_root) == []

    def test_wiki_verdict_is_carried_to_the_consumer(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root, bounced="MailboxDoesNotExist")

        results = deliverability_for_page(page, contacts_root)

        assert results[0].wiki_verdict == "MailboxDoesNotExist"
        assert results[0].hard_bounced is False
        assert results[0].any_evidence is True

    def test_no_evidence_on_either_surface(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root)

        results = deliverability_for_page(page, contacts_root)

        assert results[0].hard_bounced is False
        assert results[0].wiki_verdict is None
        assert results[0].any_evidence is False


class TestEvidenceClassesStaySeparate:
    """No 4.x transient or list-verification verdict is promoted to a hard bounce."""

    def test_transient_wiki_verdict_is_not_a_hard_bounce(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root, bounced="'421 4.4.62 routing issue'")

        results = deliverability_for_page(page, contacts_root)

        assert results[0].wiki_verdict == "421 4.4.62 routing issue"
        assert results[0].hard_bounced is False  # the whole point

    def test_list_verification_verdict_is_not_a_hard_bounce(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root, bounced="DomainHasNullMx")

        results = deliverability_for_page(page, contacts_root)

        assert results[0].wiki_verdict == "DomainHasNullMx"
        assert results[0].hard_bounced is False

    def test_bare_smtp_reply_without_an_enhanced_code_is_not_a_hard_bounce(
        self, tmp_path: Path
    ) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root, bounced="'550 mailbox unavailable'")

        results = deliverability_for_page(page, contacts_root)

        assert results[0].hard_bounced is False

    def test_a_5xx_wiki_verdict_is_still_not_a_hard_bounce_by_itself(
        self, tmp_path: Path
    ) -> None:
        # Even a verdict that WOULD match the detector is not promoted: the
        # hard-bounce half is the pii mark, and nothing re-recognizes the wiki
        # field. Convergence is a join on a shared key, never a re-recognition
        # pass (athenaeum#852's motivation, point 3).
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root, bounced="'550 5.1.1 user unknown'")

        results = deliverability_for_page(page, contacts_root)

        assert results[0].wiki_verdict == "550 5.1.1 user unknown"
        assert results[0].hard_bounced is False

    def test_the_join_never_writes_to_the_wiki(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root)
        _mark(contacts_root)
        before = page.read_text(encoding="utf-8")

        join_identifier(contacts_root, wiki_root, "alex@example.org")
        deliverability_for_page(page, contacts_root)

        assert page.read_text(encoding="utf-8") == before

    def test_no_third_state_is_recorded(self, tmp_path: Path) -> None:
        # The join reads; it introduces no new field on either surface.
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        person = _person_record(contacts_root)
        page = _wiki_page(wiki_root)
        _mark(contacts_root)
        snapshot = (person.read_text(encoding="utf-8"), page.read_text(encoding="utf-8"))

        join_identifier(contacts_root, wiki_root, "alex@example.org")

        assert (person.read_text(encoding="utf-8"), page.read_text(encoding="utf-8")) == snapshot

    def test_empty_and_flag_shaped_wiki_values(self) -> None:
        assert wiki_bounced_value({"bounced": "   "}) is None
        assert wiki_bounced_value({"bounced": None}) is None
        assert wiki_bounced_value({}) is None
        assert wiki_bounced_value(None) is None
        assert wiki_bounced_value({"bounced": False}) is None
        assert wiki_bounced_value({"bounced": True}) == "true"


class TestReReportContract:
    """Identical re-report is a no-op; a different observed_at wins in place."""

    def test_identical_re_report_is_a_byte_identical_no_op(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        person = _person_record(contacts_root)
        _mark(contacts_root)
        before = person.read_text(encoding="utf-8")

        _, changed = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1 user unknown",
            observed_at="2026-08-05",
            source="script:voltaire-bounce-relay",
        )

        assert changed is False
        assert person.read_text(encoding="utf-8") == before

    def test_a_later_observed_at_wins_in_place(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        _wiki_page(wiki_root)
        _mark(contacts_root)

        mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.2.1 mailbox disabled",
            observed_at="2026-09-01",
            source="script:voltaire-bounce-relay",
        )

        join = join_identifier(
            contacts_root, wiki_root, "alex@example.org", as_of=date(2026, 8, 15)
        )
        # Last writer wins: the mark now closes at 2026-09-01, so an as-of read
        # between the two dates reads as still deliverable.
        assert join.pii_marked is False
        assert (
            join_identifier(
                contacts_root, wiki_root, "alex@example.org", as_of=date(2026, 9, 2)
            ).pii_marked
            is True
        )
