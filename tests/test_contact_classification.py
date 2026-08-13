# SPDX-License-Identifier: Apache-2.0
"""Tests for contact-value provenance + usage classification (issue athenaeum#866).

Structure mirrors the issue's acceptance criteria: a contact value carries
which system asserted it and when, at the level of the individual VALUE
rather than the record; a usage classification distinguishing "observed in
prior communication" from "supplied by a data provider"; a read interface
that exposes the classification alongside the value and can return only
values of a requested class; a no-downgrade rule; and legacy values reported
as unclassified rather than silently usable.

- ``TestStoreAndReadBackBothClasses`` — both classes stored and read back
  (AC: both classes, per-value provenance).
- ``TestClassFilteredRead`` — the read interface returns only the requested
  class.
- ``TestNoDowngrade`` — evidence of use outranks purchase.
- ``TestUnclassifiedLegacyValue`` — a value written before the marker existed
  reads as ``unclassified`` and is NOT outreach-eligible.
- ``TestOutreachEligibility`` — the permission rule itself: address-book
  population and outreach eligibility are different permissions.
- ``TestClassifyContactValueWriter`` — writer behaviour: idempotence, no
  record minting, unknown-class rejection.
- ``TestMcpAndCliSurfaces`` — the filter is reachable from the MCP tool
  helper and the ``athenaeum person`` CLI, not just the library call.

Fixtures follow ``tests/test_person_read.py``'s ``EXCLUDED_CONFIG`` +
tmp-path idiom rather than inventing new ones. Ordinary
``alex@example.org``-style addresses only — no ``+alias`` shape, which is not
suppressed for this file in ``.public-safe-lintignore``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from athenaeum.pii import (
    CONTACT_CLASSIFICATION_FIELD,
    OUTREACH_ELIGIBLE_CLASSES,
    USAGE_CLASS_OBSERVED,
    USAGE_CLASS_PROVIDER,
    USAGE_CLASS_UNCLASSIFIED,
    ContactClassification,
    classification_for_value,
    classify_contact_value,
    contact_classification_entries,
    contacts_surface_root,
    is_outreach_eligible,
    read_bounce_record,
    read_person,
)

EXCLUDED_CONFIG = {"storage": {"mapping": {"pii": "excluded"}}}

OBSERVED_ADDRESS = "alex@example.org"
PROVIDER_ADDRESS = "alex.example@corp.example"


def _write_wiki_person(wiki_root: Path, uid: str, *, name: str = "Alex Example") -> Path:
    """Minimal ``type: person`` wiki page indexable by ``EntityIndex``."""
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: person\n---\n\nNotes about {name}.\n",
        encoding="utf-8",
    )
    return path


def _write_contact_record(
    contacts_root: Path, filename: str, *, uid: str, fields: str = ""
) -> Path:
    """Synthetic contact record on the (excluded) contacts surface."""
    contacts_root.mkdir(parents=True, exist_ok=True)
    path = contacts_root / filename
    path.write_text(
        f"---\nuid: {uid}\npii: true\n{fields}---\n\nArchival contact data.\n",
        encoding="utf-8",
    )
    return path


def _person_with_two_addresses(tmp_path: Path) -> tuple[Path, Path]:
    """A person whose record lists one observed and one provider address.

    Returns ``(knowledge_root, contacts_root)``. The addresses are classified
    through the public writer, not hand-written frontmatter, so these tests
    exercise the same path a real writer takes.
    """
    knowledge = tmp_path / "knowledge"
    _write_wiki_person(knowledge / "wiki", "alex")
    contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
    _write_contact_record(
        contacts_root,
        "alex-contact.md",
        uid="alex",
        fields=f"emails:\n  - {OBSERVED_ADDRESS}\n  - {PROVIDER_ADDRESS}\n",
    )
    classify_contact_value(
        contacts_root,
        OBSERVED_ADDRESS,
        usage_class=USAGE_CLASS_OBSERVED,
        source="agent-observed:inbox-sync",
        observed_at="2026-08-01T00:00:00Z",
    )
    classify_contact_value(
        contacts_root,
        PROVIDER_ADDRESS,
        usage_class=USAGE_CLASS_PROVIDER,
        source="api:apollo",
        observed_at="2026-08-05T00:00:00Z",
    )
    return knowledge, contacts_root


# ---------------------------------------------------------------------------
# Both classes stored and read back
# ---------------------------------------------------------------------------


class TestStoreAndReadBackBothClasses:
    def test_both_classes_round_trip_with_per_value_provenance(
        self, tmp_path: Path
    ) -> None:
        """AC: provenance is per VALUE, not per record — the two addresses on
        one record carry different sources, different times, different
        classes."""
        _, contacts_root = _person_with_two_addresses(tmp_path)
        meta = read_bounce_record(contacts_root / "alex-contact.md")

        observed = classification_for_value(meta, OBSERVED_ADDRESS)
        provider = classification_for_value(meta, PROVIDER_ADDRESS)

        assert observed.usage_class == USAGE_CLASS_OBSERVED
        assert observed.source == "agent-observed:inbox-sync"
        assert observed.observed_at == "2026-08-01T00:00:00Z"

        assert provider.usage_class == USAGE_CLASS_PROVIDER
        assert provider.source == "api:apollo"
        assert provider.observed_at == "2026-08-05T00:00:00Z"

        # One record, two independently-classified values.
        assert len(contact_classification_entries(meta)) == 2

    def test_read_interface_exposes_classification_alongside_value(
        self, tmp_path: Path
    ) -> None:
        """AC: a caller receiving an address always knows which kind it is."""
        knowledge, _ = _person_with_two_addresses(tmp_path)

        result = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=True)

        assert result is not None
        assert result.contact["emails"] == [OBSERVED_ADDRESS, PROVIDER_ADDRESS]
        classes = [item.usage_class for item in result.classifications["emails"]]
        assert classes == [USAGE_CLASS_OBSERVED, USAGE_CLASS_PROVIDER]
        # Co-indexed with the values, so the pairing needs no second lookup.
        for value, item in zip(
            result.contact["emails"], result.classifications["emails"], strict=True
        ):
            assert item.identifier == value

    def test_matching_is_case_insensitive(self, tmp_path: Path) -> None:
        """A classification stored lowercase still answers for a mixed-case
        read — otherwise a recorded permission silently reads as
        unclassified."""
        _, contacts_root = _person_with_two_addresses(tmp_path)
        meta = read_bounce_record(contacts_root / "alex-contact.md")

        assert (
            classification_for_value(meta, OBSERVED_ADDRESS.upper()).usage_class
            == USAGE_CLASS_OBSERVED
        )

    def test_to_dict_is_json_serializable(self, tmp_path: Path) -> None:
        knowledge, _ = _person_with_two_addresses(tmp_path)
        result = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=True)
        assert result is not None

        payload = json.loads(json.dumps(result.to_dict()))

        emails = payload["classifications"]["emails"]
        assert [item["usage_class"] for item in emails] == [
            USAGE_CLASS_OBSERVED,
            USAGE_CLASS_PROVIDER,
        ]
        assert [item["outreach_eligible"] for item in emails] == [True, False]


# ---------------------------------------------------------------------------
# Class-filtered read
# ---------------------------------------------------------------------------


class TestClassFilteredRead:
    def test_filter_returns_only_requested_class(self, tmp_path: Path) -> None:
        """AC: a caller that must not see provider-sourced addresses cannot
        receive one by accident."""
        knowledge, _ = _person_with_two_addresses(tmp_path)

        result = read_person(
            knowledge,
            EXCLUDED_CONFIG,
            "alex",
            include_contact=True,
            usage_classes=[USAGE_CLASS_OBSERVED],
        )

        assert result is not None
        assert result.contact["emails"] == [OBSERVED_ADDRESS]
        assert PROVIDER_ADDRESS not in result.contact["emails"]
        assert [
            item.usage_class for item in result.classifications["emails"]
        ] == [USAGE_CLASS_OBSERVED]

    def test_outreach_eligible_filter_excludes_provider_and_unclassified(
        self, tmp_path: Path
    ) -> None:
        """The canonical filter a sending consumer uses."""
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields=(
                f"emails:\n  - {OBSERVED_ADDRESS}\n  - {PROVIDER_ADDRESS}\n"
                "  - legacy@example.org\n"
            ),
        )
        classify_contact_value(
            contacts_root,
            OBSERVED_ADDRESS,
            usage_class=USAGE_CLASS_OBSERVED,
            source="agent-observed:inbox-sync",
            observed_at="2026-08-01T00:00:00Z",
        )
        classify_contact_value(
            contacts_root,
            PROVIDER_ADDRESS,
            usage_class=USAGE_CLASS_PROVIDER,
            source="api:apollo",
            observed_at="2026-08-05T00:00:00Z",
        )

        result = read_person(
            knowledge,
            EXCLUDED_CONFIG,
            "alex",
            include_contact=True,
            usage_classes=OUTREACH_ELIGIBLE_CLASSES,
        )

        assert result is not None
        assert result.contact["emails"] == [OBSERVED_ADDRESS]

    def test_field_with_no_matching_value_is_dropped_entirely(
        self, tmp_path: Path
    ) -> None:
        """"No value of the class you asked for" must present identically to
        "no value at all" — an empty list would leak that a withheld other-class
        value exists."""
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields=f"emails:\n  - {PROVIDER_ADDRESS}\n",
        )
        classify_contact_value(
            contacts_root,
            PROVIDER_ADDRESS,
            usage_class=USAGE_CLASS_PROVIDER,
            source="api:apollo",
            observed_at="2026-08-05T00:00:00Z",
        )

        result = read_person(
            knowledge,
            EXCLUDED_CONFIG,
            "alex",
            include_contact=True,
            usage_classes=[USAGE_CLASS_OBSERVED],
        )

        assert result is not None
        assert "emails" not in result.contact
        assert "emails" not in result.classifications

    def test_no_filter_returns_every_value(self, tmp_path: Path) -> None:
        """Default is unchanged for an existing caller (issue athenaeum#864's
        behaviour is preserved)."""
        knowledge, _ = _person_with_two_addresses(tmp_path)

        result = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=True)

        assert result is not None
        assert result.contact["emails"] == [OBSERVED_ADDRESS, PROVIDER_ADDRESS]

    def test_empty_filter_is_honoured_literally(self, tmp_path: Path) -> None:
        """An explicitly empty collection is a caller asking for nothing, NOT
        a request for everything — the fail-safe direction."""
        knowledge, _ = _person_with_two_addresses(tmp_path)

        result = read_person(
            knowledge,
            EXCLUDED_CONFIG,
            "alex",
            include_contact=True,
            usage_classes=[],
        )

        assert result is not None
        assert result.contact == {}

    def test_filter_does_not_disturb_redacted_read(self, tmp_path: Path) -> None:
        """With contact data excluded there are no values to filter, and the
        redaction marker still counts what the record holds."""
        knowledge, _ = _person_with_two_addresses(tmp_path)

        result = read_person(
            knowledge,
            EXCLUDED_CONFIG,
            "alex",
            include_contact=False,
            usage_classes=[USAGE_CLASS_OBSERVED],
        )

        assert result is not None
        assert result.contact == {}
        assert result.classifications == {}
        assert [marker.value_count for marker in result.redactions] == [2]


# ---------------------------------------------------------------------------
# No-downgrade
# ---------------------------------------------------------------------------


class TestNoDowngrade:
    def test_provider_does_not_downgrade_observed(self, tmp_path: Path) -> None:
        """AC: a value observed in real communication is not downgraded by a
        later provider assertion of the same address."""
        _, contacts_root = _person_with_two_addresses(tmp_path)

        classify_contact_value(
            contacts_root,
            OBSERVED_ADDRESS,
            usage_class=USAGE_CLASS_PROVIDER,
            source="api:apollo",
            observed_at="2026-08-09T00:00:00Z",
        )

        meta = read_bounce_record(contacts_root / "alex-contact.md")
        still = classification_for_value(meta, OBSERVED_ADDRESS)
        assert still.usage_class == USAGE_CLASS_OBSERVED
        # The provenance that JUSTIFIES the surviving permission survives too —
        # keeping the class but taking the vendor's provenance would destroy
        # its basis.
        assert still.source == "agent-observed:inbox-sync"
        assert still.observed_at == "2026-08-01T00:00:00Z"

    def test_downgrade_attempt_leaves_file_byte_identical(
        self, tmp_path: Path
    ) -> None:
        _, contacts_root = _person_with_two_addresses(tmp_path)
        record = contacts_root / "alex-contact.md"
        before = record.read_text(encoding="utf-8")

        classify_contact_value(
            contacts_root,
            OBSERVED_ADDRESS,
            usage_class=USAGE_CLASS_PROVIDER,
            source="api:apollo",
            observed_at="2026-08-09T00:00:00Z",
        )

        assert record.read_text(encoding="utf-8") == before

    def test_observed_upgrades_a_provider_value(self, tmp_path: Path) -> None:
        """The permitted direction: real communication with a purchased
        address is exactly the evidence that promotes it."""
        _, contacts_root = _person_with_two_addresses(tmp_path)

        classify_contact_value(
            contacts_root,
            PROVIDER_ADDRESS,
            usage_class=USAGE_CLASS_OBSERVED,
            source="agent-observed:inbox-sync",
            observed_at="2026-08-11T00:00:00Z",
        )

        meta = read_bounce_record(contacts_root / "alex-contact.md")
        upgraded = classification_for_value(meta, PROVIDER_ADDRESS)
        assert upgraded.usage_class == USAGE_CLASS_OBSERVED
        assert upgraded.source == "agent-observed:inbox-sync"
        assert upgraded.observed_at == "2026-08-11T00:00:00Z"

    def test_same_class_reassertion_refreshes_provenance(
        self, tmp_path: Path
    ) -> None:
        """A fresher statement of the same fact is not a downgrade."""
        _, contacts_root = _person_with_two_addresses(tmp_path)

        classify_contact_value(
            contacts_root,
            PROVIDER_ADDRESS,
            usage_class=USAGE_CLASS_PROVIDER,
            source="api:other-vendor",
            observed_at="2026-08-12T00:00:00Z",
        )

        meta = read_bounce_record(contacts_root / "alex-contact.md")
        refreshed = classification_for_value(meta, PROVIDER_ADDRESS)
        assert refreshed.usage_class == USAGE_CLASS_PROVIDER
        assert refreshed.source == "api:other-vendor"

    def test_unknown_stored_class_never_outranks_a_real_one(
        self, tmp_path: Path
    ) -> None:
        """A typo on a hand-edited record must not win a no-downgrade
        comparison, and must not confer eligibility."""
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields=(
                f"emails:\n  - {OBSERVED_ADDRESS}\n"
                f"{CONTACT_CLASSIFICATION_FIELD}:\n"
                f"  - identifier: {OBSERVED_ADDRESS}\n"
                "    usage_class: obsrved\n"
            ),
        )
        meta = read_bounce_record(contacts_root / "alex-contact.md")
        assert is_outreach_eligible(meta, OBSERVED_ADDRESS) is False

        classify_contact_value(
            contacts_root,
            OBSERVED_ADDRESS,
            usage_class=USAGE_CLASS_PROVIDER,
            source="api:apollo",
            observed_at="2026-08-09T00:00:00Z",
        )

        meta = read_bounce_record(contacts_root / "alex-contact.md")
        assert (
            classification_for_value(meta, OBSERVED_ADDRESS).usage_class
            == USAGE_CLASS_PROVIDER
        )


# ---------------------------------------------------------------------------
# Unclassified legacy value
# ---------------------------------------------------------------------------


class TestUnclassifiedLegacyValue:
    def test_legacy_value_reads_as_unclassified_not_usable(
        self, tmp_path: Path
    ) -> None:
        """AC: existing records without the marker are treated as unclassified
        and reported as such, never silently defaulted to usable."""
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        record = _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields=f"emails:\n  - {OBSERVED_ADDRESS}\n",  # no classification at all
        )
        meta = read_bounce_record(record)

        item = classification_for_value(meta, OBSERVED_ADDRESS)

        assert item.usage_class == USAGE_CLASS_UNCLASSIFIED
        assert item.source is None
        assert item.observed_at is None
        assert item.outreach_eligible is False
        assert is_outreach_eligible(meta, OBSERVED_ADDRESS) is False

    def test_legacy_value_is_reported_through_the_read_interface(
        self, tmp_path: Path
    ) -> None:
        """Reported, not omitted: the caller is told the provenance is
        unknown rather than receiving a bare address."""
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields=f"emails:\n  - {OBSERVED_ADDRESS}\n",
        )

        result = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=True)

        assert result is not None
        assert result.contact["emails"] == [OBSERVED_ADDRESS]
        assert result.classifications["emails"][0].usage_class == (
            USAGE_CLASS_UNCLASSIFIED
        )

    def test_malformed_classification_block_degrades_to_unclassified(
        self, tmp_path: Path
    ) -> None:
        """A hand-edited record degrades to "no classification recorded",
        never to a crash in a consumer."""
        meta = {"emails": [OBSERVED_ADDRESS], CONTACT_CLASSIFICATION_FIELD: "nonsense"}

        assert contact_classification_entries(meta) == []
        assert (
            classification_for_value(meta, OBSERVED_ADDRESS).usage_class
            == USAGE_CLASS_UNCLASSIFIED
        )
        assert is_outreach_eligible(meta, OBSERVED_ADDRESS) is False

    def test_address_absent_from_record_is_unclassified(self) -> None:
        assert (
            classification_for_value({}, "stranger@example.org").usage_class
            == USAGE_CLASS_UNCLASSIFIED
        )
        assert is_outreach_eligible({}, "stranger@example.org") is False


# ---------------------------------------------------------------------------
# The permission rule itself
# ---------------------------------------------------------------------------


class TestOutreachEligibility:
    def test_only_observed_is_outreach_eligible(self) -> None:
        assert OUTREACH_ELIGIBLE_CLASSES == (USAGE_CLASS_OBSERVED,)
        assert ContactClassification(
            identifier=OBSERVED_ADDRESS, usage_class=USAGE_CLASS_OBSERVED
        ).outreach_eligible
        assert not ContactClassification(
            identifier=PROVIDER_ADDRESS, usage_class=USAGE_CLASS_PROVIDER
        ).outreach_eligible
        assert not ContactClassification(
            identifier=OBSERVED_ADDRESS, usage_class=USAGE_CLASS_UNCLASSIFIED
        ).outreach_eligible

    def test_provider_value_is_stored_and_readable_though_not_eligible(
        self, tmp_path: Path
    ) -> None:
        """The whole point: storable and syncable is not the same as usable
        for outreach. A provider address is NOT dropped from the store."""
        knowledge, contacts_root = _person_with_two_addresses(tmp_path)
        meta = read_bounce_record(contacts_root / "alex-contact.md")

        assert is_outreach_eligible(meta, PROVIDER_ADDRESS) is False

        result = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=True)
        assert result is not None
        assert PROVIDER_ADDRESS in result.contact["emails"]

    def test_eligibility_does_not_answer_the_bounce_question(
        self, tmp_path: Path
    ) -> None:
        """Two separate questions with two separate predicates — this one must
        not silently answer the other."""
        from athenaeum.pii import is_bounced_identifier

        _, contacts_root = _person_with_two_addresses(tmp_path)
        meta = read_bounce_record(contacts_root / "alex-contact.md")

        assert is_outreach_eligible(meta, OBSERVED_ADDRESS) is True
        assert is_bounced_identifier(meta, OBSERVED_ADDRESS) is False


# ---------------------------------------------------------------------------
# Writer behaviour
# ---------------------------------------------------------------------------


class TestClassifyContactValueWriter:
    def test_reassertion_is_byte_identical(self, tmp_path: Path) -> None:
        _, contacts_root = _person_with_two_addresses(tmp_path)
        record = contacts_root / "alex-contact.md"
        before = record.read_text(encoding="utf-8")

        classify_contact_value(
            contacts_root,
            OBSERVED_ADDRESS,
            usage_class=USAGE_CLASS_OBSERVED,
            source="agent-observed:inbox-sync",
            observed_at="2026-08-01T00:00:00Z",
        )

        assert record.read_text(encoding="utf-8") == before

    def test_never_mints_a_record_for_an_unknown_address(
        self, tmp_path: Path
    ) -> None:
        """A provider assertion must not conjure a contact record for an
        address the store was deliberately never given."""
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        contacts_root.mkdir(parents=True, exist_ok=True)

        written = classify_contact_value(
            contacts_root,
            "stranger@example.org",
            usage_class=USAGE_CLASS_PROVIDER,
            source="api:apollo",
            observed_at="2026-08-05T00:00:00Z",
        )

        assert written is None
        assert list(contacts_root.rglob("*.md")) == []

    def test_unknown_usage_class_raises(self, tmp_path: Path) -> None:
        """A misspelled class fails loudly at the write, where the caller can
        see it — storing it silently would read back as unclassified and
        quietly strip a permission."""
        _, contacts_root = _person_with_two_addresses(tmp_path)

        with pytest.raises(ValueError, match="unknown usage_class"):
            classify_contact_value(
                contacts_root,
                OBSERVED_ADDRESS,
                usage_class="totally-made-up",
                source="api:apollo",
                observed_at="2026-08-05T00:00:00Z",
            )

    def test_classifying_sets_the_pii_flag(self, tmp_path: Path) -> None:
        _, contacts_root = _person_with_two_addresses(tmp_path)
        meta = read_bounce_record(contacts_root / "alex-contact.md")
        assert meta.get("pii") is True

    def test_entry_position_is_stable_across_updates(self, tmp_path: Path) -> None:
        """Position stability keeps a record's history readable in file order
        and is what makes a re-assertion compare byte-identical."""
        _, contacts_root = _person_with_two_addresses(tmp_path)

        classify_contact_value(
            contacts_root,
            OBSERVED_ADDRESS,
            usage_class=USAGE_CLASS_OBSERVED,
            source="agent-observed:second-pass",
            observed_at="2026-08-10T00:00:00Z",
        )

        meta = read_bounce_record(contacts_root / "alex-contact.md")
        entries = contact_classification_entries(meta)
        assert [entry["identifier"] for entry in entries] == [
            OBSERVED_ADDRESS,
            PROVIDER_ADDRESS,
        ]


# ---------------------------------------------------------------------------
# MCP + CLI surfaces
# ---------------------------------------------------------------------------


class TestMcpAndCliSurfaces:
    def test_mcp_person_read_filters_by_class(self, tmp_path: Path) -> None:
        from athenaeum.mcp_server import person_read

        knowledge, _ = _person_with_two_addresses(tmp_path)

        payload = json.loads(
            person_read(
                knowledge,
                "alex",
                include_contact_data=True,
                usage_classes=[USAGE_CLASS_OBSERVED],
                config=EXCLUDED_CONFIG,
            )
        )

        assert payload["contact"]["emails"] == [OBSERVED_ADDRESS]

    def test_mcp_person_read_exposes_classification_by_default(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.mcp_server import person_read

        knowledge, _ = _person_with_two_addresses(tmp_path)

        payload = json.loads(
            person_read(
                knowledge, "alex", include_contact_data=True, config=EXCLUDED_CONFIG
            )
        )

        assert [
            item["usage_class"] for item in payload["classifications"]["emails"]
        ] == [USAGE_CLASS_OBSERVED, USAGE_CLASS_PROVIDER]

    def test_cli_person_usage_class_filter(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        from athenaeum._cmd_query import cmd_person

        knowledge, _ = _person_with_two_addresses(tmp_path)
        monkeypatch.setattr(
            "athenaeum.config.load_config", lambda _root: EXCLUDED_CONFIG
        )

        code = cmd_person(
            argparse.Namespace(
                uid="alex",
                include_contact=True,
                usage_class=[USAGE_CLASS_OBSERVED],
                path=knowledge,
            )
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["contact"]["emails"] == [OBSERVED_ADDRESS]

    def test_cli_person_without_filter_returns_both(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        from athenaeum._cmd_query import cmd_person

        knowledge, _ = _person_with_two_addresses(tmp_path)
        monkeypatch.setattr(
            "athenaeum.config.load_config", lambda _root: EXCLUDED_CONFIG
        )

        code = cmd_person(
            argparse.Namespace(
                uid="alex",
                include_contact=True,
                usage_class=[],
                path=knowledge,
            )
        )

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["contact"]["emails"] == [OBSERVED_ADDRESS, PROVIDER_ADDRESS]
