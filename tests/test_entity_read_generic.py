# SPDX-License-Identifier: Apache-2.0
"""Tests for the entity-class-generic excluded read (issue athenaeum#883).

The primitive layer only — no ``recall()``, MCP or CLI surface here (those
are issues athenaeum#885/#886). Structure mirrors the issue's acceptance criteria:

- ``TestSurfaceClassParameterization`` — ``excluded_surface_root`` takes the
  SURFACE class; ``contacts_surface_root`` still resolves identically.
- ``TestFieldPolicy`` — the three-step resolution order, the
  denylist-complement default for an unknown class, and the config override.
- ``TestReadEntitiesBatch`` — ``read_entities`` batch-read behaviour (laziness
  on an empty uid list). Formerly also pinned ``read_person``/``read_people``
  parity with ``read_entity``/``read_entities``, removed in athenaeum#888
  along with those symbols.
- ``TestNonPersonSurfaceClass`` — a non-person entity class reads back its
  fields through the denylist-complement default.
- ``TestAssemblyFunctionIsEntityIndexFree`` — the athenaeum#885 seam: the public
  assembly function builds no ``EntityIndex``.
- ``TestExcludedRecordIndex`` — first-wins, collision warning, missing root,
  the register-does-not-reorder invariant, and the athenaeum#850 duplicate-mint
  regression.
- ``TestLibrarianThreadsOneIndex`` — the index is built ONCE above
  ``process_one`` and threaded down, not rebuilt per raw file.
- ``TestReadEntityPreparedIndexes`` — issue athenaeum#1124's ``read_entity``
  half: optional prepared ``excluded_index``/``entity_index`` parameters,
  byte-identical default behaviour with neither supplied, and the
  counter-based regression guard (``EntityIndex`` built once,
  ``iter_contact_records`` scanned once, across N uid reads).

Fixtures follow the ``EXCLUDED_CONFIG`` + tmp-path corpus-builder idiom used
throughout this suite's excluded-read tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from athenaeum import pii
from athenaeum.models import EntityIndex, RawFile
from athenaeum.pii import (
    CONTACT_DATA_FIELDS,
    EXCLUDED_RECORD_BOOKKEEPING_FIELDS,
    ExcludedRecordIndex,
    RedactionMarker,
    assemble_excluded_read,
    contacts_surface_root,
    excluded_surface_root,
    mark_bounced,
    read_entities,
    read_entity,
    resolve_excluded_fields,
)

EXCLUDED_CONFIG = {"storage": {"mapping": {"pii": "excluded"}}}

#: A second excluded surface for a NON-person class — the shape the whole
#: issue exists to make readable. ``vendor`` is a plain class name; nothing in
#: the module special-cases it, which is the point. It gets its OWN adapter
#: (hence its own ``surface_root``) rather than sharing the built-in
#: ``excluded`` one, because the root is a property of the ADAPTER: two classes
#: mapped to the same adapter deliberately share a directory.
TWO_SURFACE_CONFIG = {
    "storage": {
        "mapping": {"pii": "excluded", "vendor": "vendor-excluded"},
        "adapters": {
            "vendor-excluded": {
                "backing_store": "markdown",
                "surface_root": "vendor-excluded",
                "corpus_policy": {
                    "embedded": False,
                    "recallable": False,
                    "merge_eligible": False,
                },
            }
        },
    }
}


def _write_wiki_entity(
    wiki_root: Path,
    uid: str,
    *,
    name: str = "Alex Example",
    entity_type: str = "person",
) -> Path:
    """Write a minimal wiki page of any ``type:``, indexable by ``EntityIndex``."""
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: {entity_type}\n---\n\nNotes about {name}.\n",
        encoding="utf-8",
    )
    return path


def _write_record(contacts_root: Path, filename: str, *, uid: str, fields: str = "") -> Path:
    """Write a synthetic record on an excluded surface."""
    contacts_root.mkdir(parents=True, exist_ok=True)
    path = contacts_root / filename
    path.write_text(
        f"---\nuid: {uid}\npii: true\n{fields}---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return path


def _raw_bounce_note(tmp_path: Path) -> "RawFile":
    """A conforming tier-0 hard-bounce note, in ``test_bounce_mark.py``'s idiom."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "note.md"
    path.write_text(
        "---\nobserved_at: 2026-08-14\nsource: script:voltaire-bounce-relay\n---\n\n"
        "Alex's address alex@example.org hard-bounced. "
        "Diagnostic: 550 5.1.1 user unknown.\n",
        encoding="utf-8",
    )
    return RawFile(path=path, source="voltaire-bounce-relay", timestamp="", uuid8="")


def _person_corpus(tmp_path: Path, *, with_record: bool = True) -> Path:
    """A knowledge root with one person page and (optionally) its pii record."""
    knowledge = tmp_path / "knowledge"
    _write_wiki_entity(knowledge / "wiki", "alex")
    if with_record:
        _write_record(
            contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\nphones:\n  - '+15550000001'\n",
        )
    return knowledge


class TestSurfaceClassParameterization:
    """The resolver takes the surface class; the pii wrapper is unchanged."""

    def test_resolves_any_class_through_the_adapter_layer(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"

        pii_root = excluded_surface_root("pii", knowledge, TWO_SURFACE_CONFIG)
        vendor_root = excluded_surface_root("vendor", knowledge, TWO_SURFACE_CONFIG)

        # Each class reaches its OWN adapter's root — the parameterization this
        # issue adds; before it, every caller got `pii`'s root and nothing else.
        assert pii_root == knowledge / "excluded"
        assert vendor_root == knowledge / "vendor-excluded"
        # Both land OUTSIDE the corpus — that is what `excluded` means.
        assert (knowledge / "wiki") not in pii_root.parents
        assert (knowledge / "wiki") not in vendor_root.parents

    def test_classes_sharing_one_adapter_share_its_root(self, tmp_path: Path) -> None:
        """The root belongs to the ADAPTER, not the class — pin it so the

        two-classes-one-directory case reads as deliberate rather than as a
        collision. The uid-keyed join is what keeps records distinct there.
        """
        knowledge = tmp_path / "knowledge"
        config = {"storage": {"mapping": {"pii": "excluded", "vendor": "excluded"}}}

        assert excluded_surface_root("pii", knowledge, config) == excluded_surface_root(
            "vendor", knowledge, config
        )

    def test_contacts_surface_root_is_the_pii_wrapper(self, tmp_path: Path) -> None:
        """The retained wrapper must resolve byte-identically to the generic call."""
        knowledge = tmp_path / "knowledge"

        assert contacts_surface_root(knowledge, EXCLUDED_CONFIG) == excluded_surface_root(
            "pii", knowledge, EXCLUDED_CONFIG
        )

    def test_unmapped_class_falls_back_to_the_default_surface(self, tmp_path: Path) -> None:
        """No mapping is a no-op convenience, not a silent leak."""
        knowledge = tmp_path / "knowledge"

        assert excluded_surface_root("nobody-mapped-me", knowledge, EXCLUDED_CONFIG) == (
            knowledge / "wiki"
        )


class TestFieldPolicy:
    """``resolve_excluded_fields``' three-step resolution order."""

    def test_pii_default_is_contact_data_fields_verbatim(self) -> None:
        """Rule 2 — a person read must be byte-identical to before athenaeum#883."""
        assert resolve_excluded_fields("pii", None, {"emails": ["a@example.org"]}) == (
            CONTACT_DATA_FIELDS
        )

    def test_unknown_class_defaults_to_denylist_complement(self) -> None:
        """Rule 3 — every field on the record MINUS the bookkeeping denylist."""
        record = {
            "uid": "acme",
            "type": "vendor",
            "pii": True,
            "source": "import",
            "account_numbers": ["A-1"],
            "billing_contact": ["ap@example.org"],
        }

        fields = resolve_excluded_fields("vendor", None, record)

        assert fields == ("account_numbers", "billing_contact")
        assert not set(fields) & EXCLUDED_RECORD_BOOKKEEPING_FIELDS

    def test_every_bookkeeping_field_is_denied(self) -> None:
        """The denylist is the complement's whole safety property — pin it."""
        record = {name: "x" for name in EXCLUDED_RECORD_BOOKKEEPING_FIELDS}
        record["real_field"] = ["kept"]

        assert resolve_excluded_fields("vendor", None, record) == ("real_field",)

    def test_contact_classification_is_never_returned_as_a_field(self) -> None:
        """It is metadata ABOUT a field — consulted to classify, never a value."""
        assert "contact_classification" in EXCLUDED_RECORD_BOOKKEEPING_FIELDS

    def test_explicit_config_override_wins_over_both_defaults(self) -> None:
        """Rule 1 — including for ``pii``, whose built-in default it replaces."""
        config = {
            "storage": {
                "mapping": {"pii": "excluded"},
                "excluded_fields": {"pii": ["emails"], "vendor": ["account_numbers"]},
            }
        }

        assert resolve_excluded_fields("pii", config, {"emails": [], "phones": []}) == ("emails",)
        assert resolve_excluded_fields("vendor", config, {"account_numbers": [], "other": []}) == (
            "account_numbers",
        )

    def test_explicit_empty_list_is_honoured_literally(self) -> None:
        """An operator saying "no data fields" differs from not configuring the class."""
        config = {"storage": {"excluded_fields": {"vendor": []}}}

        assert resolve_excluded_fields("vendor", config, {"anything": ["x"]}) == ()

    def test_unknown_class_with_no_record_yields_no_fields(self) -> None:
        assert resolve_excluded_fields("vendor", None, None) == ()


class TestReadEntitiesBatch:
    """``read_entities`` batch-read behaviour not already covered by the
    single-uid tests above.

    Formerly also pinned ``read_person``/``read_people`` parity with
    ``read_entity``/``read_entities`` and the ``PersonRead is EntityRead``
    alias — removed in athenaeum#888 along with those symbols, which no
    longer exist to compare against.
    """

    def test_batch_reads_nothing_for_an_empty_uid_list(self, tmp_path: Path) -> None:
        """Laziness is preserved: an empty batch costs no scan at all."""
        knowledge = _person_corpus(tmp_path)

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError("an empty batch must not scan the surface")

        original = pii.iter_contact_records
        pii.iter_contact_records = _explode  # type: ignore[assignment]
        try:
            assert list(read_entities(knowledge, EXCLUDED_CONFIG, [], surface_class="pii")) == []
        finally:
            pii.iter_contact_records = original  # type: ignore[assignment]


class TestNonPersonSurfaceClass:
    """The whole point: an excluded record for a class that is not a person."""

    def _vendor_corpus(self, tmp_path: Path) -> Path:
        knowledge = tmp_path / "knowledge"
        _write_wiki_entity(knowledge / "wiki", "acme", name="Acme Ltd", entity_type="vendor")
        _write_record(
            excluded_surface_root("vendor", knowledge, TWO_SURFACE_CONFIG),
            "acme-record.md",
            uid="acme",
            fields="account_numbers:\n  - A-1\n  - A-2\nbilling_contact:\n  - ap@example.org\n",
        )
        return knowledge

    def test_included_read_returns_denylist_complement_fields(self, tmp_path: Path) -> None:
        knowledge = self._vendor_corpus(tmp_path)

        read = read_entity(
            knowledge,
            TWO_SURFACE_CONFIG,
            "acme",
            surface_class="vendor",
            include_excluded=True,
        )

        assert read is not None
        assert read.contact == {
            "account_numbers": ["A-1", "A-2"],
            "billing_contact": ["ap@example.org"],
        }
        assert read.redactions == ()
        # Bookkeeping never leaks in as data.
        assert "uid" not in read.contact and "pii" not in read.contact

    def test_withheld_read_marks_every_field_it_withholds(self, tmp_path: Path) -> None:
        """Honest by construction — no field is silently neither value nor marker."""
        knowledge = self._vendor_corpus(tmp_path)

        read = read_entity(knowledge, TWO_SURFACE_CONFIG, "acme", surface_class="vendor")

        assert read is not None
        assert read.contact == {}
        assert set(read.redactions) == {
            RedactionMarker(field="account_numbers", value_count=2),
            RedactionMarker(field="billing_contact", value_count=1),
        }

    def test_entity_with_no_record_is_not_an_error(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_wiki_entity(knowledge / "wiki", "acme", entity_type="vendor")

        read = read_entity(knowledge, TWO_SURFACE_CONFIG, "acme", surface_class="vendor")

        assert read is not None
        assert read.contact == {} and read.redactions == ()
        assert read.contact_record_path is None


class TestAssemblyFunctionIsEntityIndexFree:
    """The athenaeum#885 seam — reachable without paying for an ``EntityIndex``."""

    def test_assembles_from_resolved_inputs_without_building_an_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _write_wiki_entity(tmp_path / "wiki", "alex")

        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("assemble_excluded_read must not build an EntityIndex")

        monkeypatch.setattr(pii, "EntityIndex", _explode)

        fields, redactions, classifications = assemble_excluded_read(
            page,
            {"uid": "alex", "type": "person"},
            {"uid": "alex", "emails": ["alex@example.org"]},
            surface_class="pii",
            include_excluded=True,
        )

        assert fields == {"emails": ["alex@example.org"]}
        assert redactions == ()
        assert list(classifications) == ["emails"]

    def test_no_record_yields_three_empty_containers(self, tmp_path: Path) -> None:
        page = _write_wiki_entity(tmp_path / "wiki", "alex")

        assert assemble_excluded_read(
            page, {"uid": "alex"}, None, surface_class="pii", include_excluded=True
        ) == ({}, (), {})

    def test_uid_mismatch_warns_but_still_assembles(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A cross-joined record is the worst failure here — never silent."""
        page = _write_wiki_entity(tmp_path / "wiki", "alex")

        with caplog.at_level(logging.WARNING, logger=pii.log.name):
            fields, _, _ = assemble_excluded_read(
                page,
                {"uid": "alex"},
                {"uid": "sam", "emails": ["sam@example.org"]},
                surface_class="pii",
                include_excluded=True,
            )

        assert fields == {"emails": ["sam@example.org"]}
        assert "does not match page uid" in caplog.text


class TestExcludedRecordIndex:
    """One scan, first-wins, and safe against mid-batch record minting."""

    def test_missing_root_is_an_empty_index_never_a_raise(self, tmp_path: Path) -> None:
        index = ExcludedRecordIndex(tmp_path / "does-not-exist")

        assert index.by_uid("alex") is None
        assert index.by_identifier("alex@example.org") is None

    def test_resolves_by_uid_and_by_identifier(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        record = _write_record(
            root, "alex.md", uid="alex", fields="emails:\n  - Alex@Example.org\n"
        )

        index = ExcludedRecordIndex(root)

        assert index.by_uid("alex") == record
        # Case-insensitive, exactly as `record_lists_identifier` compares.
        assert index.by_identifier("alex@example.org") == record

    def test_blank_keys_never_match(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        _write_record(root, "alex.md", uid="alex", fields="emails:\n  - a@example.org\n")

        index = ExcludedRecordIndex(root)

        assert index.by_uid("") is None
        assert index.by_uid("   ") is None
        assert index.by_identifier("") is None

    def test_first_match_in_sorted_order_wins_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "excluded"
        _write_record(root, "a-first.md", uid="a", fields="emails:\n  - shared@example.org\n")
        _write_record(root, "z-second.md", uid="z", fields="emails:\n  - shared@example.org\n")

        with caplog.at_level(logging.WARNING, logger=pii.log.name):
            index = ExcludedRecordIndex(root)
            resolved = index.by_identifier("shared@example.org")

        assert resolved is not None and resolved.name == "a-first.md"
        assert "2 contact records" in caplog.text

    def test_indexed_resolution_matches_the_unindexed_function(self, tmp_path: Path) -> None:
        """The load-bearing property: moving a caller onto the index changes nothing."""
        root = tmp_path / "excluded"
        _write_record(root, "a.md", uid="a", fields="emails:\n  - shared@example.org\n")
        _write_record(root, "z.md", uid="z", fields="emails:\n  - shared@example.org\n")
        index = ExcludedRecordIndex(root)

        for address in ("shared@example.org", "missing@example.org", ""):
            assert pii.resolve_contact_record(root, address, index=index) == (
                pii.resolve_contact_record(root, address)
            )

    def test_register_never_reorders_an_already_indexed_identifier(self, tmp_path: Path) -> None:
        """Batch resolution must be stable regardless of interleaved writes."""
        root = tmp_path / "excluded"
        first = _write_record(
            root, "a-first.md", uid="a", fields="emails:\n  - shared@example.org\n"
        )
        index = ExcludedRecordIndex(root)
        assert index.by_identifier("shared@example.org") == first

        # A record written mid-batch that ALSO lists the same address.
        late = _write_record(
            root, "b-later.md", uid="b", fields="emails:\n  - shared@example.org\n"
        )
        index.register(late)

        assert index.by_identifier("shared@example.org") == first

    def test_register_is_idempotent(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        record = _write_record(root, "a.md", uid="a", fields="emails:\n  - alex@example.org\n")
        index = ExcludedRecordIndex(root)

        index.register(record)
        index.register(record)

        assert index.by_identifier("alex@example.org") == record

    def test_register_picks_up_a_merged_identifier_wholesale(self, tmp_path: Path) -> None:
        """A single-key insert would miss the MERGE case — re-index the record."""
        root = tmp_path / "excluded"
        record = _write_record(root, "a.md", uid="a", fields="emails:\n  - first@example.org\n")
        index = ExcludedRecordIndex(root)
        assert index.by_identifier("second@example.org") is None

        record.write_text(
            "---\nuid: a\npii: true\nemails:\n  - first@example.org\n"
            "  - second@example.org\n---\n\nArchival data.\n",
            encoding="utf-8",
        )
        index.register(record)

        assert index.by_identifier("second@example.org") == record
        assert index.by_identifier("first@example.org") == record

    def test_shared_index_mints_exactly_one_record_for_one_address(self, tmp_path: Path) -> None:
        """athenaeum#850 regression: two marks, one batch, one index, ONE record."""
        root = tmp_path / "excluded"
        root.mkdir(parents=True)
        index = ExcludedRecordIndex(root)

        first, first_changed = mark_bounced(
            root,
            "alex@example.org",
            diagnostic="5.1.1",
            observed_at="2026-08-14",
            source="mailer",
            index=index,
        )
        second, second_changed = mark_bounced(
            root,
            "alex@example.org",
            diagnostic="5.1.1",
            observed_at="2026-08-14",
            source="mailer",
            index=index,
        )

        assert first == second
        assert first_changed is True and second_changed is False
        assert len(list(root.rglob("*.md"))) == 1

    def test_shared_index_resolves_the_second_mark_onto_a_minted_person_record(
        self, tmp_path: Path
    ) -> None:
        """The merge case: a record minted mid-batch is resolvable by address after."""
        root = tmp_path / "excluded"
        _write_record(root, "alex.md", uid="alex", fields="emails:\n  - alex@example.org\n")
        index = ExcludedRecordIndex(root)

        target, _ = mark_bounced(
            root,
            "alex@example.org",
            diagnostic="5.1.1",
            observed_at="2026-08-14",
            source="mailer",
            index=index,
        )

        assert target.name == "alex.md"
        assert len(list(root.rglob("*.md"))) == 1


class TestLibrarianThreadsOneIndex:
    """The index is built ONCE above ``process_one``, never per raw file."""

    def test_tier0_bounce_mark_passes_its_index_through_to_mark_bounced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        wiki_root.mkdir(parents=True)
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        contacts_root.mkdir(parents=True, exist_ok=True)
        index = ExcludedRecordIndex(contacts_root)

        seen: list[object] = []

        def _spy(*args: object, **kwargs: object) -> tuple[Path, bool]:
            seen.append(kwargs.get("index"))
            return contacts_root / "x.md", False

        monkeypatch.setattr(librarian, "mark_bounced", _spy)

        raw = _raw_bounce_note(tmp_path)
        fact = librarian.tier0_bounce_mark(
            raw, wiki_root, config=EXCLUDED_CONFIG, excluded_index=index
        )

        assert fact is not None
        assert seen == [index]

    def test_tier0_bounce_mark_without_an_index_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every existing caller keeps today's behaviour AND today's cost."""
        from athenaeum import librarian

        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        wiki_root.mkdir(parents=True)

        seen: list[object] = []

        def _spy(*args: object, **kwargs: object) -> tuple[Path, bool]:
            seen.append(kwargs.get("index"))
            return tmp_path / "x.md", False

        monkeypatch.setattr(librarian, "mark_bounced", _spy)

        raw = _raw_bounce_note(tmp_path)
        librarian.tier0_bounce_mark(raw, wiki_root, config=EXCLUDED_CONFIG)

        assert seen == [None]


class TestReadEntityPreparedIndexes:
    """athenaeum#1124 — optional prepared ``excluded_index``/``entity_index``
    on ``read_entity``. Additive: neither is built when both are supplied,
    and default behaviour with neither supplied is byte-identical to today.
    """

    def test_default_behaviour_unaffected_when_neither_supplied(self, tmp_path: Path) -> None:
        knowledge = _person_corpus(tmp_path)

        result = read_entity(
            knowledge,
            EXCLUDED_CONFIG,
            "alex",
            surface_class="pii",
            include_excluded=True,
        )

        assert result is not None
        assert result.uid == "alex"
        assert result.contact["emails"] == ["alex@example.org"]

    def test_prepared_indexes_return_the_same_read_as_unprepared(self, tmp_path: Path) -> None:
        knowledge = _person_corpus(tmp_path)
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)

        unprepared = read_entity(
            knowledge,
            EXCLUDED_CONFIG,
            "alex",
            surface_class="pii",
            include_excluded=True,
        )
        prepared = read_entity(
            knowledge,
            EXCLUDED_CONFIG,
            "alex",
            surface_class="pii",
            include_excluded=True,
            excluded_index=ExcludedRecordIndex(contacts_root),
            entity_index=EntityIndex(knowledge / "wiki"),
        )

        assert unprepared is not None
        assert prepared is not None
        assert prepared.contact == unprepared.contact
        assert prepared.redactions == unprepared.redactions
        assert prepared.classifications == unprepared.classifications
        assert prepared.do_not_email == unprepared.do_not_email

    def test_prepared_path_uses_excluded_index_by_uid_not_the_full_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC: ``resolve_contact_record_for_uid``'s full scan is replaced by
        ``excluded_index.by_uid(uid)`` on the prepared path."""
        knowledge = _person_corpus(tmp_path)
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)

        def _explode(*args: object, **kwargs: object) -> Path | None:
            raise AssertionError(
                "the prepared path must resolve via excluded_index.by_uid, "
                "not resolve_contact_record_for_uid's full scan"
            )

        monkeypatch.setattr(pii, "resolve_contact_record_for_uid", _explode)

        result = read_entity(
            knowledge,
            EXCLUDED_CONFIG,
            "alex",
            surface_class="pii",
            include_excluded=True,
            excluded_index=ExcludedRecordIndex(contacts_root),
            entity_index=EntityIndex(knowledge / "wiki"),
        )

        assert result is not None
        assert result.contact["emails"] == ["alex@example.org"]

    def test_n_uid_reads_build_entity_index_once_and_scan_contacts_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counter-based regression guard (not a timing assertion):
        resolving N uids in one process must construct ``EntityIndex``
        exactly once and call ``iter_contact_records`` exactly once,
        regardless of N."""
        knowledge = tmp_path / "knowledge"
        contacts_root = excluded_surface_root("pii", knowledge, EXCLUDED_CONFIG)
        n = 4
        uids = [f"p{i}" for i in range(n)]
        for uid in uids:
            _write_wiki_entity(knowledge / "wiki", uid, name=f"Person {uid}")
            _write_record(
                contacts_root,
                f"{uid}.md",
                uid=uid,
                fields=f"emails:\n  - {uid}@example.org\n",
            )

        entity_index_builds = 0
        original_entity_init = EntityIndex.__init__

        def _counting_entity_init(self: EntityIndex, wiki_root: Path) -> None:
            nonlocal entity_index_builds
            entity_index_builds += 1
            original_entity_init(self, wiki_root)

        monkeypatch.setattr(EntityIndex, "__init__", _counting_entity_init)

        iter_contact_records_calls = 0
        original_iter = pii.iter_contact_records

        def _counting_iter(root: Path) -> list[Path]:
            nonlocal iter_contact_records_calls
            iter_contact_records_calls += 1
            return original_iter(root)

        monkeypatch.setattr(pii, "iter_contact_records", _counting_iter)

        excluded_index = ExcludedRecordIndex(contacts_root)
        entity_index = EntityIndex(knowledge / "wiki")
        assert entity_index_builds == 1
        assert iter_contact_records_calls == 0  # lazy — no scan until first lookup

        for uid in uids:
            result = read_entity(
                knowledge,
                EXCLUDED_CONFIG,
                uid,
                surface_class="pii",
                include_excluded=True,
                excluded_index=excluded_index,
                entity_index=entity_index,
            )
            assert result is not None
            assert result.uid == uid

        assert entity_index_builds == 1, "EntityIndex must not be rebuilt per uid"
        assert iter_contact_records_calls == 1, (
            "iter_contact_records must be called exactly once for the whole batch, not once per uid"
        )
