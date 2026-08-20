# SPDX-License-Identifier: Apache-2.0
"""Facts-through-the-existing-read-path for authorized readers (athenaeum#851).

The re-scoped shape of athenaeum#851: the originally-filed
``suppression_state()`` predicate was CANCELLED, and what ships instead is the
underlying FACTS, in parseable fields, on the read path that already exists.
Athenaeum returns what it knows and how it knows it; the consumer decides what
to do about it.

Structure mirrors the issue's replacement acceptance criteria:

- ``TestValidityIsStructuredAndProvenanced`` — every contact value carries its
  validity state (close date, reason, source), not just a boolean.
- ``TestRepresentationTrapIsIrrelevantToCallers`` — a caller learns "closed as
  of D, for reason R, per source S" WITHOUT knowing a bounce is encoded as a
  ``valid_until`` on the identifier rather than a ``bounced:`` enum. This is
  the trap that makes ``grep '^bounced:'`` return 0 after a successful mark.
- ``TestDoNotEmailIsFirstClass`` — the field exists in live frontmatter and was
  absent from the API surface entirely; it is now exposed WITH provenance, and
  read tolerantly across the shapes live records actually carry.
- ``TestEntityReadCarriesTheFacts`` — the two co-indexed maps and the
  per-record mark ride ``read_entity``; no new read seam.
- ``TestUnknownIsStatedNotInferred`` — "we have never heard of this address" is
  distinguishable from "we know it and hold nothing against it", positively.
- ``TestBulkReadPaysOneScan`` — N identifiers, ONE ``iter_contact_records``
  pass, built on ``ExcludedRecordIndex``.
- ``TestFailClosed`` — an unreachable surface RAISES; it never returns a
  permissive answer. A false skip is recoverable by a human; a false send is
  not.
- ``TestNoEligibilityPredicateShips`` — the cancelled predicate stays
  cancelled.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

from athenaeum import pii
from athenaeum.mcp_server import recall_search

#: `pii` routed off-corpus, as in the live `~/knowledge/athenaeum.yaml`.
EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}


def _write_page(wiki_root: Path, uid: str, *, name: str, extra_frontmatter: str = "") -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: person\n{extra_frontmatter}---\n\n"
        f"{name} does things.\n",
        encoding="utf-8",
    )
    return path


def _write_record(root: Path, filename: str, *, uid: str, fields: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(
        f"---\nuid: {uid}\npii: true\n{fields}---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return path


#: A person record carrying a per-identifier valid-time close — the shape
#: `mark_bounced` writes onto a record that LISTS addresses.
_BOUNCED_RECORD = (
    "emails:\n"
    "  - alex@example.org\n"
    "  - alex.old@example.org\n"
    "identifier_validity:\n"
    "  - identifier: alex.old@example.org\n"
    "    bounce_diagnostic: '5.1.1'\n"
    "    observed_at: '2026-01-05'\n"
    "    valid_until: '2026-01-05'\n"
    "    source: smtp-bounce\n"
)


class TestValidityIsStructuredAndProvenanced:
    def test_close_carries_date_reason_and_source(self) -> None:
        meta = {
            "identifier_validity": [
                {
                    "identifier": "alex.old@example.org",
                    "bounce_diagnostic": "5.1.1",
                    "observed_at": "2026-01-05",
                    "valid_until": "2026-01-05",
                    "source": "smtp-bounce",
                }
            ]
        }

        validity = pii.validity_for_value(meta, "alex.old@example.org")

        assert validity.closed is True
        assert validity.valid_until == "2026-01-05"
        assert validity.reason == "5.1.1"
        assert validity.source == "smtp-bounce"
        assert validity.observed_at == "2026-01-05"
        assert validity.recorded is True

    def test_matching_is_case_insensitive(self) -> None:
        """A close stored lowercase must answer for a mixed-case lookup.

        The same `normalize_identifier` comparison `is_bounced_identifier` and
        `classification_for_value` use — a miss here would silently report a
        recorded close as "nothing recorded", losing a suppression fact.
        """
        meta = {
            "identifier_validity": [
                {"identifier": "alex@example.org", "valid_until": "2020-01-01"}
            ]
        }

        assert pii.validity_for_value(meta, "Alex@Example.ORG").closed is True

    def test_no_entry_is_recorded_false_not_closed_false_alone(self) -> None:
        """"Nothing recorded" and "recorded and still open" are different facts.

        Both are `closed=False`. Collapsing them would hide the difference
        between a value someone has vouched for and one nobody has looked at.
        """
        recorded_open = pii.validity_for_value(
            {"identifier_validity": [{"identifier": "a@b.com", "valid_until": "2099-01-01"}]},
            "a@b.com",
        )
        never_seen = pii.validity_for_value({"emails": ["a@b.com"]}, "a@b.com")

        assert recorded_open.closed is False and recorded_open.recorded is True
        assert never_seen.closed is False and never_seen.recorded is False

    def test_as_of_evaluates_the_close_at_a_past_date(self) -> None:
        """A campaign asks "was this closed when the segment was cut"."""
        meta = {
            "identifier_validity": [
                {"identifier": "a@b.com", "valid_until": "2026-06-01"}
            ]
        }

        assert pii.validity_for_value(meta, "a@b.com", date(2026, 8, 1)).closed is True
        assert pii.validity_for_value(meta, "a@b.com", date(2026, 1, 1)).closed is False

    def test_slug_keyed_record_answers_only_for_its_own_address(self) -> None:
        """A one-address record must never answer for a neighbouring address.

        Mirrors `is_bounced_identifier`'s second shape exactly.
        """
        meta = {
            "identifier": "solo@example.org",
            "valid_until": "2020-01-01",
            "bounce_diagnostic": "5.0.0",
            "source": "smtp-bounce",
        }

        assert pii.validity_for_value(meta, "solo@example.org").closed is True
        other = pii.validity_for_value(meta, "neighbour@example.org")
        assert other.closed is False
        assert other.recorded is False

    def test_malformed_validity_field_degrades_rather_than_raising(self) -> None:
        """A hand-edited record must not crash a consumer's send loop."""
        for broken in ({"identifier_validity": "nonsense"}, {"identifier_validity": [7]}):
            result = pii.validity_for_value(broken, "a@b.com")
            assert result.closed is False
            assert result.recorded is False


class TestRepresentationTrapIsIrrelevantToCallers:
    def test_grep_for_bounced_finds_nothing_but_the_api_reports_the_close(
        self, tmp_path: Path
    ) -> None:
        """The athenaeum#851 "representation trap", asserted end to end.

        `mark_bounced` writes a valid-time close, NOT a `bounced:` enum. So a
        verifier grepping `^bounced:` sees zero after a fully successful mark —
        which has already misled one verification lane. A caller reading
        through this API never has to know that.
        """
        contacts = tmp_path / "contacts"
        record, changed = pii.mark_bounced(
            contacts,
            "alex@example.org",
            diagnostic="5.1.1",
            observed_at="2026-01-05",
            source="smtp-bounce",
        )

        assert changed is True
        raw = record.read_text(encoding="utf-8")
        # The trap itself: the naive verification returns nothing.
        assert "bounced:" not in raw

        # The API answers anyway, and in structured fields.
        validity = pii.validity_for_value(pii.read_bounce_record(record), "alex@example.org")
        assert validity.closed is True
        assert validity.valid_until == "2026-01-05"
        assert validity.reason == "5.1.1"
        assert validity.source == "smtp-bounce"

    def test_caller_never_reads_the_encoding_key_itself(self, tmp_path: Path) -> None:
        """Both storage shapes present ONE shape to the caller.

        A person record (per-identifier close) and a slug-keyed record
        (top-level close) are different on disk and identical through
        `validity_for_value` — which is what makes changing the encoding later
        a non-event for consumers.
        """
        contacts = tmp_path / "contacts"
        person = _write_record(contacts, "alex.md", uid="alex", fields=_BOUNCED_RECORD)
        slug, _ = pii.mark_bounced(
            contacts,
            "solo@example.org",
            diagnostic="5.0.0",
            observed_at="2026-02-01",
            source="smtp-bounce",
        )

        from_person = pii.validity_for_value(
            pii.read_bounce_record(person), "alex.old@example.org"
        )
        from_slug = pii.validity_for_value(pii.read_bounce_record(slug), "solo@example.org")

        for validity in (from_person, from_slug):
            assert validity.closed is True
            assert validity.valid_until is not None
            assert validity.reason is not None
            assert validity.source == "smtp-bounce"


class TestDoNotEmailIsFirstClass:
    def test_absent_field_is_marked_false_with_no_provenance(self) -> None:
        state = pii.do_not_email_state({"emails": ["a@b.com"]})
        assert state.marked is False
        assert state.source is None and state.reason is None

    def test_bare_true_is_marked(self) -> None:
        assert pii.do_not_email_state({"do_not_email": True}).marked is True

    def test_mapping_carries_provenance(self) -> None:
        """athenaeum#77 requires operator marks and platform unsubscribes to
        stay distinguishable — a bare boolean cannot say which it is."""
        state = pii.do_not_email_state(
            {
                "do_not_email": {
                    "value": True,
                    "source": "mailchimp",
                    "observed_at": "2026-03-04",
                    "reason": "unsubscribed",
                }
            }
        )

        assert state.marked is True
        assert state.source == "mailchimp"
        assert state.observed_at == "2026-03-04"
        assert state.reason == "unsubscribed"

    def test_mapping_without_an_explicit_value_key_is_marked(self) -> None:
        """A mapping was written AT ALL, which is an assertion."""
        state = pii.do_not_email_state({"do_not_email": {"source": "operator"}})
        assert state.marked is True
        assert state.source == "operator"

    def test_explicit_false_is_not_marked(self) -> None:
        assert pii.do_not_email_state({"do_not_email": False}).marked is False
        assert pii.do_not_email_state({"do_not_email": "false"}).marked is False
        assert pii.do_not_email_state({"do_not_email": "no"}).marked is False

    def test_bare_key_with_no_value_asserts_nothing(self) -> None:
        """`do_not_email:` with nothing after it parses to YAML None."""
        assert pii.do_not_email_state({"do_not_email": None}).marked is False

    def test_freeform_string_is_marked_and_keeps_the_operators_words(self) -> None:
        """Fail-closed on an unparseable scalar: MARKED, reason preserved.

        The failure direction of a typo must be a false skip (recoverable),
        never a false send (not).
        """
        state = pii.do_not_email_state({"do_not_email": "unsubscribed 2026-02-01"})
        assert state.marked is True
        assert state.reason == "unsubscribed 2026-02-01"

    def test_list_takes_the_last_entry(self) -> None:
        """Last-writer-wins, matching `mark_bounced`'s posture."""
        state = pii.do_not_email_state(
            {"do_not_email": [{"value": True, "source": "old"}, {"value": False}]}
        )
        assert state.marked is False

    def test_empty_list_is_no_mark(self) -> None:
        assert pii.do_not_email_state({"do_not_email": []}).marked is False

    def test_is_not_treated_as_a_redactable_contact_value(self) -> None:
        """The mark is a fact, not an address — it has no value to withhold.

        Were it routed through `resolve_excluded_fields` as a contact field it
        would produce a `RedactionMarker` on a redacted read, asserting that a
        withheld VALUE exists where there is only a boolean.
        """
        assert pii.DO_NOT_EMAIL_FIELD not in pii.CONTACT_DATA_FIELDS


class TestDoNotEmailReadsBothSurfaces:
    """athenaeum#960: `do_not_email_state()` reads the wiki page too.

    athenaeum#851 shipped reading only the excluded-record surface, which
    holds zero live `do_not_email` marks — every hand-authored mark lives on
    the wiki page's frontmatter instead, so the field was inert on live data.
    """

    def test_wiki_page_only_mark_is_read(self) -> None:
        """The defect: a mark on the wiki page ONLY. This is the test that
        failed before athenaeum#960 — `do_not_email_state` took only the
        excluded-record meta and never saw the page at all."""
        state = pii.do_not_email_state({}, {"do_not_email": True})

        assert state.marked is True
        assert state.surface == "wiki"

    def test_excluded_record_only_mark_is_still_read(self) -> None:
        """No regression for the shape athenaeum#851 shipped."""
        state = pii.do_not_email_state({"do_not_email": True}, {})

        assert state.marked is True
        assert state.surface == "excluded"

    def test_neither_surface_marked_is_false_with_no_provenance(self) -> None:
        state = pii.do_not_email_state({"emails": ["a@b.com"]}, {"name": "Alex"})

        assert state.marked is False
        assert state.source is None
        assert state.observed_at is None
        assert state.reason is None
        assert state.surface is None

    def test_wiki_page_takes_precedence_when_both_surfaces_carry_the_field(self) -> None:
        """2026-08-20 AC amendment: wiki wins — never a merge of both.

        A caller reading `.source`/`.reason` must get one surface's answer,
        never a value stitched together from both.
        """
        state = pii.do_not_email_state(
            {"do_not_email": {"value": True, "source": "mailchimp", "reason": "unsubscribed"}},
            {"do_not_email": True, "do_not_email_reason": "family request"},
        )

        assert state.marked is True
        assert state.surface == "wiki"
        assert state.source == "operator"
        assert state.reason == "family request"

    def test_wiki_provenance_reads_the_sibling_reason_and_date_keys(self) -> None:
        """The page's shape is flat — not the excluded surface's nested mapping."""
        state = pii.do_not_email_state(
            None,
            {
                "do_not_email": True,
                "do_not_email_reason": "confirmed deceased by operator",
                "do_not_email_date": "2026-07-01",
            },
        )

        assert state.marked is True
        assert state.surface == "wiki"
        assert state.source == "operator"
        assert state.reason == "confirmed deceased by operator"
        assert state.observed_at == "2026-07-01"

    def test_malformed_wiki_scalar_reads_marked_fail_closed(self) -> None:
        """The same fail-closed rule as the excluded surface, on the page too."""
        state = pii.do_not_email_state(None, {"do_not_email": "do not contact, ever"})

        assert state.marked is True
        assert state.surface == "wiki"
        assert state.reason == "do not contact, ever"

    def test_wiki_explicit_false_falls_back_to_the_excluded_record(self) -> None:
        state = pii.do_not_email_state(
            {"do_not_email": True}, {"do_not_email": False}
        )

        assert state.marked is True
        assert state.surface == "excluded"

    def test_page_frontmatter_defaults_to_none_for_pre_960_callers(self) -> None:
        """Existing single-argument callers keep working unchanged."""
        state = pii.do_not_email_state({"do_not_email": True})

        assert state.marked is True
        assert state.surface == "excluded"


class TestEntityReadCarriesTheFacts:
    """No new read seam: the facts ride `read_entity` (athenaeum#888)."""

    def _corpus(self, tmp_path: Path) -> Path:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Example")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields=_BOUNCED_RECORD + "do_not_email: true\n",
        )
        return knowledge

    def test_validity_is_co_indexed_with_contact_values(self, tmp_path: Path) -> None:
        knowledge = self._corpus(tmp_path)

        read = pii.read_entity(
            knowledge, EXCLUDED_CONFIG, "alex", surface_class="pii", include_excluded=True
        )

        assert read is not None
        emails = read.contact["emails"]
        validity = read.validity["emails"]
        assert len(validity) == len(emails)
        # `validity[i]` describes `contact[i]` — the co-indexing contract.
        by_value = dict(zip(emails, validity, strict=True))
        assert by_value["alex@example.org"].closed is False
        assert by_value["alex.old@example.org"].closed is True
        assert by_value["alex.old@example.org"].reason == "5.1.1"

    def test_do_not_email_rides_the_same_read(self, tmp_path: Path) -> None:
        knowledge = self._corpus(tmp_path)

        read = pii.read_entity(
            knowledge, EXCLUDED_CONFIG, "alex", surface_class="pii", include_excluded=True
        )

        assert read is not None
        assert read.do_not_email.marked is True

    def test_do_not_email_survives_a_redacted_read(self, tmp_path: Path) -> None:
        """The mark carries no value to withhold, so redaction does not hide it.

        Withholding it would leave a consumer unable to learn the one fact that
        most constrains what it may do.
        """
        knowledge = self._corpus(tmp_path)

        read = pii.read_entity(
            knowledge, EXCLUDED_CONFIG, "alex", surface_class="pii", include_excluded=False
        )

        assert read is not None
        assert read.contact == {}
        assert read.validity == {}  # no values exposed, so none described
        assert read.do_not_email.marked is True

    def test_do_not_email_wiki_only_mark_is_read(self, tmp_path: Path) -> None:
        """athenaeum#960's central defect, at the `read_entity` call site
        (`src/athenaeum/pii.py` `_entity_read_from_indexes`): a mark on the
        wiki page only, with an excluded record present but carrying nothing.
        This is the shape all 4 live marks are in — this test fails before
        athenaeum#960's fix."""
        knowledge = tmp_path / "knowledge"
        _write_page(
            knowledge / "wiki",
            "alex",
            name="Alex Example",
            extra_frontmatter="do_not_email: true\ndo_not_email_reason: family request\n",
        )
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        read = pii.read_entity(
            knowledge, EXCLUDED_CONFIG, "alex", surface_class="pii", include_excluded=True
        )

        assert read is not None
        assert read.do_not_email.marked is True
        assert read.do_not_email.surface == "wiki"
        assert read.do_not_email.reason == "family request"

    def test_entity_with_no_record_reports_no_mark(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "sam", name="Sam Example")

        read = pii.read_entity(
            knowledge, EXCLUDED_CONFIG, "sam", surface_class="pii", include_excluded=True
        )

        assert read is not None
        assert read.do_not_email.marked is False
        assert read.validity == {}

    def test_to_dict_is_json_serializable_and_additive(self, tmp_path: Path) -> None:
        import json

        knowledge = self._corpus(tmp_path)
        read = pii.read_entity(
            knowledge, EXCLUDED_CONFIG, "alex", surface_class="pii", include_excluded=True
        )
        assert read is not None

        payload = json.loads(json.dumps(read.to_dict(), default=str))

        assert payload["do_not_email"]["marked"] is True
        assert payload["validity"]["emails"][0]["identifier"]
        # Every pre-athenaeum#851 key is still there.
        for key in ("uid", "contact", "redactions", "classifications", "contact_included"):
            assert key in payload


class TestUnknownIsStatedNotInferred:
    def test_unknown_address_is_known_false_with_no_facts(self, tmp_path: Path) -> None:
        """`maecenas#97` joins on exactly this distinction.

        A consumer must never have to infer a stranger from an absence — that
        is how strangers get silently treated as safe to email.
        """
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_record(contacts, "alex.md", uid="alex", fields="emails:\n  - alex@example.org\n")

        facts = dict(
            pii.read_identifier_facts(
                knowledge, EXCLUDED_CONFIG, ["alex@example.org", "stranger@example.org"]
            )
        )

        assert facts["alex@example.org"].known is True
        assert facts["alex@example.org"].uid == "alex"

        stranger = facts["stranger@example.org"]
        assert stranger.known is False
        assert stranger.uid is None
        assert stranger.classification is None
        assert stranger.validity is None
        # Explicitly NOT a claim of "nothing recorded against them".
        assert stranger.do_not_email.marked is False

    def test_known_and_unsuppressed_is_a_positive_answer(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_record(contacts, "alex.md", uid="alex", fields="emails:\n  - alex@example.org\n")

        (_, facts), = pii.read_identifier_facts(
            knowledge, EXCLUDED_CONFIG, ["alex@example.org"]
        )

        assert facts.known is True
        assert facts.validity is not None and facts.validity.closed is False
        assert facts.do_not_email.marked is False

    def test_every_identifier_yields_a_pair_in_input_order(self, tmp_path: Path) -> None:
        """A silently missing row is exactly the absence a consumer misreads."""
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_record(contacts, "alex.md", uid="alex", fields="emails:\n  - alex@example.org\n")

        wanted = ["z@example.org", "alex@example.org", "z@example.org"]
        got = list(pii.read_identifier_facts(knowledge, EXCLUDED_CONFIG, wanted))

        assert [identifier for identifier, _ in got] == wanted

    def test_shared_address_is_flagged_ambiguous(self, tmp_path: Path) -> None:
        """Legitimate (a role or family address), but not resolvable to one person."""
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        for uid in ("alex", "sam"):
            _write_record(
                contacts, f"{uid}.md", uid=uid, fields="emails:\n  - shared@example.org\n"
            )

        (_, facts), = pii.read_identifier_facts(
            knowledge, EXCLUDED_CONFIG, ["shared@example.org"]
        )

        assert facts.known is True
        assert facts.ambiguous is True

    def test_facts_carry_validity_and_the_mark(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_record(
            contacts, "alex.md", uid="alex", fields=_BOUNCED_RECORD + "do_not_email: true\n"
        )

        facts = dict(
            pii.read_identifier_facts(
                knowledge, EXCLUDED_CONFIG, ["alex.old@example.org", "alex@example.org"]
            )
        )

        closed = facts["alex.old@example.org"]
        assert closed.validity is not None and closed.validity.closed is True
        assert closed.validity.reason == "5.1.1"
        # The mark is per-RECORD, so it answers for both of the record's values.
        assert facts["alex@example.org"].do_not_email.marked is True
        assert facts["alex@example.org"].validity is not None
        assert facts["alex@example.org"].validity.closed is False

    def test_do_not_email_wiki_only_mark_is_read(self, tmp_path: Path) -> None:
        """athenaeum#960 at the `IdentifierFacts` call site
        (`src/athenaeum/pii.py` `_facts_for_identifier`): the record's `uid`
        is resolved back to its wiki page, and the mark there is read even
        though the excluded record itself carries nothing."""
        knowledge = tmp_path / "knowledge"
        _write_page(
            knowledge / "wiki",
            "alex",
            name="Alex Example",
            extra_frontmatter="do_not_email: true\n",
        )
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_record(contacts, "alex.md", uid="alex", fields="emails:\n  - alex@example.org\n")

        (_, facts), = pii.read_identifier_facts(
            knowledge, EXCLUDED_CONFIG, ["alex@example.org"]
        )

        assert facts.known is True
        assert facts.do_not_email.marked is True
        assert facts.do_not_email.surface == "wiki"

    def test_to_dict_is_json_serializable(self, tmp_path: Path) -> None:
        import json

        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_record(contacts, "alex.md", uid="alex", fields=_BOUNCED_RECORD)

        facts = dict(
            pii.read_identifier_facts(
                knowledge, EXCLUDED_CONFIG, ["alex.old@example.org", "nobody@example.org"]
            )
        )

        payload = json.loads(json.dumps({k: v.to_dict() for k, v in facts.items()}, default=str))

        assert payload["alex.old@example.org"]["validity"]["closed"] is True
        assert payload["nobody@example.org"]["known"] is False
        assert payload["nobody@example.org"]["validity"] is None


class TestBulkReadPaysOneScan:
    def test_many_identifiers_cost_one_corpus_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ~16.9k-contact campaign case: ONE scan, not N.

        Built on `ExcludedRecordIndex` (athenaeum#883) rather than a second
        index, so there is one definition of how an address resolves.
        """
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        for index in range(25):
            _write_record(
                contacts,
                f"p{index}.md",
                uid=f"p{index}",
                fields=f"emails:\n  - p{index}@example.org\n",
            )

        scans = 0
        original = pii.iter_contact_records

        def _counting(root: Path) -> list[Path]:
            nonlocal scans
            scans += 1
            return original(root)

        monkeypatch.setattr(pii, "iter_contact_records", _counting)

        wanted = [f"p{index}@example.org" for index in range(25)] + ["nobody@example.org"]
        results = list(pii.read_identifier_facts(knowledge, EXCLUDED_CONFIG, wanted))

        assert scans == 1
        assert len(results) == 26
        assert sum(1 for _, facts in results if facts.known) == 25

    def test_empty_batch_costs_no_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A quiet week must not pay a full pass to read zero facts.

        Also the reason the fail-closed probe rides the first identifier: an
        empty batch asks the store nothing, so it has nothing to be wrong about.
        """

        def _explode(root: Path) -> list[Path]:
            raise AssertionError("the surface must not be scanned for an empty batch")

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        assert list(pii.read_identifier_facts(tmp_path, EXCLUDED_CONFIG, [])) == []

    def test_a_supplied_index_is_reused(self, tmp_path: Path) -> None:
        """A caller interleaving reads with `mark_bounced` writes shares one index."""
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_record(contacts, "alex.md", uid="alex", fields="emails:\n  - alex@example.org\n")
        index = pii.ExcludedRecordIndex(contacts)

        (_, facts), = pii.read_identifier_facts(
            knowledge, EXCLUDED_CONFIG, ["alex@example.org"], index=index
        )

        assert facts.known is True

    def test_as_of_is_honoured_by_the_bulk_path(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_record(contacts, "alex.md", uid="alex", fields=_BOUNCED_RECORD)

        def _closed(as_of: date | None) -> bool:
            (_, facts), = pii.read_identifier_facts(
                knowledge, EXCLUDED_CONFIG, ["alex.old@example.org"], as_of=as_of
            )
            assert facts.validity is not None
            return facts.validity.closed

        assert _closed(date(2026, 12, 1)) is True
        assert _closed(date(2026, 1, 1)) is False


class TestFailClosed:
    def test_missing_surface_raises_rather_than_reporting_nothing_suppressed(
        self, tmp_path: Path
    ) -> None:
        """The athenaeum#851 fail-closed AC, and the test the issue asks for:
        it proves the read does NOT return a permissive answer on failure.

        An unreachable store answering "known=False for everyone" is
        indistinguishable from a clean store in which nobody is suppressed —
        and would be acted on by sending.
        """
        knowledge = tmp_path / "nonexistent-knowledge"

        with pytest.raises(pii.ExcludedSurfaceUnavailable):
            list(
                pii.read_identifier_facts(
                    knowledge, EXCLUDED_CONFIG, ["alex@example.org"]
                )
            )

    @pytest.mark.skipif(
        os.geteuid() == 0,
        reason="root bypasses directory permissions, so the unreadable case cannot be staged",
    )
    def test_unreadable_surface_raises(self, tmp_path: Path) -> None:
        """Exists but cannot be listed — `is_dir()` alone would say True.

        An unmounted volume that still has a mount point, or a decryption layer
        that is not up, is precisely the "unreachable store" case.
        """
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        contacts.mkdir(parents=True)
        contacts.chmod(0o000)
        try:
            with pytest.raises(pii.ExcludedSurfaceUnavailable):
                list(
                    pii.read_identifier_facts(
                        knowledge, EXCLUDED_CONFIG, ["alex@example.org"]
                    )
                )
        finally:
            contacts.chmod(0o755)

    def test_the_error_is_not_a_generic_exception_consumers_would_swallow(self) -> None:
        """A consumer must be able to catch THIS and nothing else."""
        assert issubclass(pii.ExcludedSurfaceUnavailable, RuntimeError)

    def test_write_path_still_bootstraps_an_absent_surface(self, tmp_path: Path) -> None:
        """Reading and writing want OPPOSITE defaults, deliberately.

        `mark_bounced` mints the first record on a surface that does not exist
        yet, so `iter_contact_records` must keep returning [] rather than
        raising. The fail-closed contract lives on the READ entry point.
        """
        contacts = tmp_path / "never-created"

        record, changed = pii.mark_bounced(
            contacts,
            "alex@example.org",
            diagnostic="5.1.1",
            observed_at="2026-01-05",
            source="smtp-bounce",
        )

        assert changed is True and record.exists()
        assert pii.iter_contact_records(tmp_path / "still-missing") == []


class TestRecallRendersStructuredFacts:
    """"Prose is not an interface" — the athenaeum#851 AC on the recall path.

    The human-readable `**field:**` lines are kept (additive, so every existing
    reader keeps working) and a machine consumer gets a parseable block beside
    them rather than having to regex values back out of rendered markdown.
    """

    def _facts_block(self, result: str) -> dict[str, object]:
        marker = "```json athenaeum-excluded-facts\n"
        assert marker in result, result
        payload = result.split(marker, 1)[1].split("\n```", 1)[0]
        return json.loads(payload)

    def test_values_carry_classification_and_validity(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields=_BOUNCED_RECORD,
        )

        result = recall_search(
            knowledge / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )

        # The human line survives unchanged.
        assert "**emails:**" in result

        facts = self._facts_block(result)
        entries = {item["value"]: item for item in facts["contact"]["emails"]}
        assert entries["alex.old@example.org"]["validity"]["closed"] is True
        assert entries["alex.old@example.org"]["validity"]["reason"] == "5.1.1"
        assert entries["alex.old@example.org"]["validity"]["source"] == "smtp-bounce"
        assert entries["alex@example.org"]["validity"]["closed"] is False
        # Classification rides the same per-value entry (athenaeum#866).
        assert entries["alex@example.org"]["classification"]["usage_class"]

    def test_do_not_email_is_rendered_and_parseable(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields=(
                "emails:\n  - alex@example.org\n"
                "do_not_email:\n  value: true\n  source: mailchimp\n"
            ),
        )

        result = recall_search(
            knowledge / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )

        assert "**do_not_email:** marked" in result
        facts = self._facts_block(result)
        assert facts["do_not_email"]["marked"] is True
        assert facts["do_not_email"]["source"] == "mailchimp"

    def test_do_not_email_wiki_only_mark_is_rendered(self, tmp_path: Path) -> None:
        """athenaeum#960 at the MCP read path
        (`src/athenaeum/mcp_server.py` `_excluded_block_for_hit`, the
        `recall(with_pii=True)` join): a mark on the wiki page only still
        renders, even though the matched excluded record carries nothing."""
        knowledge = tmp_path / "knowledge"
        _write_page(
            knowledge / "wiki",
            "alex",
            name="Alex Widget",
            extra_frontmatter="do_not_email: true\ndo_not_email_reason: operator request\n",
        )
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        result = recall_search(
            knowledge / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )

        assert "**do_not_email:** marked" in result
        facts = self._facts_block(result)
        assert facts["do_not_email"]["marked"] is True
        assert facts["do_not_email"]["surface"] == "wiki"
        assert facts["do_not_email"]["reason"] == "operator request"

    def test_record_with_nothing_to_report_renders_no_block(self, tmp_path: Path) -> None:
        """A matched record holding no values and no mark says nothing at all.

        Covers the `if not lines` branch, whose condition athenaeum#851 widened:
        it now also requires the do-not-email mark to be absent, so an empty
        block is only emitted when there is genuinely nothing to report.
        """
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="note: nothing contactable here\n",
        )

        result = recall_search(
            knowledge / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )

        assert "Alex Widget" in result
        assert "athenaeum-excluded-facts" not in result

    def test_flag_unset_renders_no_facts_block(self, tmp_path: Path) -> None:
        """The default path is untouched — no excluded read, no block."""
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields=_BOUNCED_RECORD,
        )

        result = recall_search(knowledge / "wiki", "widget", config=EXCLUDED_CONFIG)

        assert "athenaeum-excluded-facts" not in result
        assert "alex@example.org" not in result


class TestNoEligibilityPredicateShips:
    def test_suppression_state_was_cancelled_and_stays_cancelled(self) -> None:
        """athenaeum#851's decision: `suppression_state()` is cancelled.

        Eligibility is the consumer's policy over athenaeum's facts. This test
        is the tripwire that stops a future lane re-deriving the predicate
        under the originally-filed name.
        """
        assert not hasattr(pii, "suppression_state")

    def test_facts_carry_no_verdict_field(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_record(contacts, "alex.md", uid="alex", fields=_BOUNCED_RECORD)

        (_, facts), = pii.read_identifier_facts(
            knowledge, EXCLUDED_CONFIG, ["alex.old@example.org"]
        )

        for verdict in ("suppressed", "may_email", "eligible", "should_send"):
            assert verdict not in facts.to_dict()

    def test_is_outreach_eligible_is_not_that_predicate(self) -> None:
        """It reports one value's usage class and does NOT consult bounce state.

        Kept explicit so the doc's non-goal section is not read as
        contradicting a function that already exists.
        """
        meta = {
            "emails": ["a@b.com"],
            "contact_classification": [
                {"identifier": "a@b.com", "usage_class": pii.USAGE_CLASS_OBSERVED}
            ],
            "identifier_validity": [{"identifier": "a@b.com", "valid_until": "2020-01-01"}],
        }

        # Bounced, yet still "outreach eligible" — because that function answers
        # a different question. A caller about to send needs BOTH facts.
        assert pii.is_bounced_identifier(meta, "a@b.com") is True
        assert pii.is_outreach_eligible(meta, "a@b.com") is True
