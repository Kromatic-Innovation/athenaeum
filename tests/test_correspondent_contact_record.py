# SPDX-License-Identifier: Apache-2.0
"""Correspondent address recorded on a page the prose compile creates (athenaeum#1948).

athenaeum#884 put the ``email -> contact record -> uid -> wiki page`` resolution
inside the librarian, so a correction keyed on an address resolves at tier 0.
What was never built is the other half: a correspondent met for the FIRST time
has no contact record for that chain to walk. The correction raises a tier, the
prose half creates the person page, and nothing writes the address down — so
every later correction for the same person resolves to zero again
(``email-handle-no-match``) and pays another escalation, indefinitely.

The producer key is voltaire's, not invented here: its shipped
conversation-intake writer emits ``correspondent_email`` on the prose half of
every triaged conversation, alongside the ``.jsonl`` correction batch targeting
the same address.

Cases:

- ``TestWritePrimitive`` — the :mod:`athenaeum.pii` writer in isolation: mint,
  classify-onto-existing, idempotency, and both decline branches.
- ``TestProseCompileRecordsTheAddress`` — AC1, end to end through
  ``process_one``: the address lands on the excluded record with provenance and
  usage class ``observed``, and NEVER on the page.
- ``TestLaterCorrectionResolves`` — AC2: a later email-keyed correction
  resolves at tier 0 to the page this compile created.
- ``TestAmbiguityIsPreserved`` — AC3: an address owned by another entity writes
  no second record and escalates.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum import pii
from athenaeum.corrections import resolve_target
from athenaeum.librarian import CORRESPONDENT_EMAIL_KEY, process_one
from athenaeum.models import ClassifiedEntity, EntityIndex, RawFile, TokenUsage

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}
VALID_TYPES = ["person", "company", "concept", "reference"]
VALID_ACCESS = ["public", "internal", "confidential"]

_ADDRESS = "dana.quill@example.org"
_DISPLAY = "Dana Quill"
_OBSERVED_AT = "2026-09-30T11:04:00Z"


def _prose_note(
    *,
    address: str = _ADDRESS,
    display_name: str | None = _DISPLAY,
    title: str = "Pricing follow-up",
) -> str:
    """A one-correspondent conversation summary, in the producer's own shape.

    Synthetic and PII-free: ``example.org`` is the reserved documentation
    domain and the display name is invented.
    """
    lines = [
        "---",
        "schema_version: 1",
        'source: "script:voltaire"',
        'submitter: "voltaire"',
        f'observed_at: "{_OBSERVED_AT}"',
        'thread_id: "thread-synthetic-0001"',
        f'{CORRESPONDENT_EMAIL_KEY}: "{address}"',
    ]
    if display_name is not None:
        lines.append(f'correspondent_name: "{display_name}"')
    lines += ["---", "", f"# {title}", "", "They asked about tiered pricing.", ""]
    return "\n".join(lines)


def _raw(raw_dir: Path, content: str, filename: str = "summary.md") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=raw_dir.name, timestamp="", uuid8="")


def _write_page(wiki_root: Path, uid: str, *, name: str) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: person\n---\n\nNotes about {name}.\n",
        encoding="utf-8",
    )
    return path


def _write_record(
    knowledge: Path, filename: str, *, uid: str | None, emails: list[str]
) -> Path:
    root = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
    root.mkdir(parents=True, exist_ok=True)
    listed = "".join(f"  - {address}\n" for address in emails)
    uid_line = f"uid: {uid}\n" if uid is not None else ""
    path = root / filename
    path.write_text(
        f"---\n{uid_line}pii: true\nemails:\n{listed}---\n\nContact record.\n",
        encoding="utf-8",
    )
    return path


def _classification_for(record: Path, address: str) -> dict[str, object] | None:
    meta = pii.read_bounce_record(record)
    for entry in pii.contact_classification_entries(meta):
        if pii.normalize_identifier(str(entry.get("identifier", ""))) == (
            pii.normalize_identifier(address)
        ):
            return entry
    return None


# ---------------------------------------------------------------------------
# The pii.py write primitive, in isolation
# ---------------------------------------------------------------------------


class TestWritePrimitive:
    def test_mints_a_record_carrying_the_uid_and_the_address(
        self, tmp_path: Path
    ) -> None:
        contacts = pii.contacts_surface_root(tmp_path / "knowledge", EXCLUDED_CONFIG)
        written = pii.record_observed_contact_value(
            contacts,
            _ADDRESS,
            uid="uid-dana",
            name=_DISPLAY,
            source="voltaire/summary.md",
            observed_at=_OBSERVED_AT,
        )
        assert written.outcome == pii.OBSERVED_CONTACT_MINTED
        assert written.path is not None
        # The whole point: the address->record->uid chain now resolves.
        assert pii.resolve_contact_records(contacts, _ADDRESS) == [written.path]
        assert pii.uid_on_record(written.path) == "uid-dana"
        entry = _classification_for(written.path, _ADDRESS)
        assert entry is not None
        assert entry["usage_class"] == pii.USAGE_CLASS_OBSERVED
        assert entry["source"] == "voltaire/summary.md"
        assert entry["observed_at"] == _OBSERVED_AT

    def test_re_observation_is_byte_identical(self, tmp_path: Path) -> None:
        contacts = pii.contacts_surface_root(tmp_path / "knowledge", EXCLUDED_CONFIG)
        kwargs: dict[str, object] = {
            "uid": "uid-dana",
            "name": _DISPLAY,
            "source": "voltaire/summary.md",
            "observed_at": _OBSERVED_AT,
        }
        first = pii.record_observed_contact_value(contacts, _ADDRESS, **kwargs)  # type: ignore[arg-type]
        assert first.path is not None
        before = first.path.read_text(encoding="utf-8")
        again = pii.record_observed_contact_value(contacts, _ADDRESS, **kwargs)  # type: ignore[arg-type]
        assert again.path == first.path
        assert again.path.read_text(encoding="utf-8") == before

    def test_classifies_onto_the_existing_record_of_the_same_uid(
        self, tmp_path: Path
    ) -> None:
        knowledge = tmp_path / "knowledge"
        existing = _write_record(
            knowledge, "dana-contact.md", uid="uid-dana", emails=[_ADDRESS]
        )
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        written = pii.record_observed_contact_value(
            contacts,
            _ADDRESS,
            uid="uid-dana",
            name=_DISPLAY,
            source="voltaire/summary.md",
            observed_at=_OBSERVED_AT,
        )
        assert written.outcome == pii.OBSERVED_CONTACT_CLASSIFIED
        assert written.path == existing
        # No SECOND record for the same address.
        assert pii.resolve_contact_records(contacts, _ADDRESS) == [existing]
        assert _classification_for(existing, _ADDRESS) is not None

    def test_declines_an_address_owned_by_another_entity(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        other = _write_record(
            knowledge, "other-contact.md", uid="uid-someone-else", emails=[_ADDRESS]
        )
        before = other.read_text(encoding="utf-8")
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        written = pii.record_observed_contact_value(
            contacts,
            _ADDRESS,
            uid="uid-dana",
            name=_DISPLAY,
            source="voltaire/summary.md",
            observed_at=_OBSERVED_AT,
        )
        assert written.outcome == pii.OBSERVED_CONTACT_OTHER_ENTITY
        assert written.uids == ("uid-someone-else",)
        assert written.path is None
        assert pii.resolve_contact_records(contacts, _ADDRESS) == [other]
        assert other.read_text(encoding="utf-8") == before

    def test_declines_a_record_that_names_no_entity(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        unowned = _write_record(
            knowledge, "unowned-contact.md", uid=None, emails=[_ADDRESS]
        )
        before = unowned.read_text(encoding="utf-8")
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        written = pii.record_observed_contact_value(
            contacts,
            _ADDRESS,
            uid="uid-dana",
            source="voltaire/summary.md",
            observed_at=_OBSERVED_AT,
        )
        assert written.outcome == pii.OBSERVED_CONTACT_RECORD_WITHOUT_UID
        assert written.path is None
        assert unowned.read_text(encoding="utf-8") == before
        assert pii.resolve_contact_records(contacts, _ADDRESS) == [unowned]

    @pytest.mark.parametrize(
        ("address", "uid"),
        [("", "uid-dana"), ("   ", "uid-dana"), (_ADDRESS, ""), (_ADDRESS, "  ")],
    )
    def test_blank_input_is_a_no_op(
        self, tmp_path: Path, address: str, uid: str
    ) -> None:
        contacts = pii.contacts_surface_root(tmp_path / "knowledge", EXCLUDED_CONFIG)
        written = pii.record_observed_contact_value(
            contacts,
            address,
            uid=uid,
            source="voltaire/summary.md",
            observed_at=_OBSERVED_AT,
        )
        assert written.outcome == pii.OBSERVED_CONTACT_SKIPPED
        assert written.path is None
        assert not contacts.exists() or list(contacts.rglob("*.md")) == []


# ---------------------------------------------------------------------------
# AC1 / AC3 -- end to end through athenaeum.librarian.process_one
# ---------------------------------------------------------------------------


def _compile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    note: str,
    classified: list[ClassifiedEntity],
    config: dict[str, object] | None = None,
) -> tuple[Path, object]:
    """Run one prose note through ``process_one`` with Tier 2/3 stubbed out.

    Returns ``(knowledge_root, ProcessingResult)``. Every path derives from
    ``tmp_path``; no network, no LLM call (the classify client is wired to
    raise if the monkeypatched ``tier2_classify`` is ever bypassed).
    """
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    raw = _raw(knowledge / "raw" / "voltaire", note)

    monkeypatch.setattr(
        "athenaeum.librarian.tier2_classify",
        lambda *_a, **_k: list(classified),
    )

    classify_client = MagicMock()
    classify_client.messages.create.side_effect = AssertionError(
        "tier2_classify is monkeypatched — the real classify client must never be called"
    )
    write_client = MagicMock()

    def _write_response(*_a: object, **_k: object) -> MagicMock:
        # One merged-body response per created entity, each citing the raw file.
        response = MagicMock()
        response.content = [
            MagicMock(
                text=(
                    f"# {_write_response.pending.pop(0)}\n\n"  # type: ignore[attr-defined]
                    "They asked about tiered pricing.[^1]\n\n"
                    f"[^1]: voltaire/{raw.path.name}"
                )
            )
        ]
        return response

    _write_response.pending = [c.name for c in classified]  # type: ignore[attr-defined]
    write_client.messages.create.side_effect = _write_response

    result = process_one(
        raw,
        EntityIndex(wiki),
        wiki,
        classify_client,
        valid_types=VALID_TYPES,
        valid_tags=[],
        valid_access=VALID_ACCESS,
        usage=TokenUsage(),
        config=config if config is not None else dict(EXCLUDED_CONFIG),
        write_client=write_client,
    )
    return knowledge, result


def _person(name: str) -> ClassifiedEntity:
    return ClassifiedEntity(
        name=name,
        entity_type="person",
        tags=[],
        access="internal",
        is_new=True,
        existing_uid=None,
        observations="They asked about tiered pricing.",
    )


class TestProseCompileRecordsTheAddress:
    def test_address_lands_on_the_excluded_record_with_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, result = _compile(
            tmp_path, monkeypatch, note=_prose_note(), classified=[_person(_DISPLAY)]
        )
        assert len(result.created) == 1  # type: ignore[attr-defined]
        created = result.created[0]  # type: ignore[attr-defined]
        assert result.correspondent_contacts_recorded == 1  # type: ignore[attr-defined]

        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        records = pii.resolve_contact_records(contacts, _ADDRESS)
        assert len(records) == 1, "exactly one contact record for the address"
        record = records[0]
        assert pii.uid_on_record(record) == created.uid

        entry = _classification_for(record, _ADDRESS)
        assert entry is not None
        assert entry["usage_class"] == pii.USAGE_CLASS_OBSERVED
        # Provenance points at the raw file this was observed in.
        assert "voltaire" in str(entry["source"])
        assert entry["observed_at"] == _OBSERVED_AT

    def test_the_address_never_reaches_the_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, result = _compile(
            tmp_path, monkeypatch, note=_prose_note(), classified=[_person(_DISPLAY)]
        )
        page = knowledge / "wiki" / result.created[0].filename  # type: ignore[attr-defined]
        page_text = page.read_text(encoding="utf-8")
        assert _ADDRESS not in page_text, "address must never land on the wiki page"
        assert CORRESPONDENT_EMAIL_KEY not in page_text
        # And nowhere else in the corpus either.
        for other in (knowledge / "wiki").rglob("*.md"):
            assert _ADDRESS not in other.read_text(encoding="utf-8")

    def test_a_note_naming_no_correspondent_records_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = "---\nobserved_at: 2026-09-30\n---\n\nA note about tiered pricing.\n"
        knowledge, result = _compile(
            tmp_path, monkeypatch, note=plain, classified=[_person(_DISPLAY)]
        )
        assert result.correspondent_contacts_recorded == 0  # type: ignore[attr-defined]
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        assert not contacts.exists() or list(contacts.rglob("*.md")) == []

    def test_a_created_company_page_never_gets_the_address(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An email address identifies a person; a company page must not carry one."""
        company = ClassifiedEntity(
            name="Quill Industries",
            entity_type="company",
            tags=[],
            access="internal",
            is_new=True,
            existing_uid=None,
            observations="They asked about tiered pricing.",
        )
        knowledge, result = _compile(
            tmp_path, monkeypatch, note=_prose_note(), classified=[company]
        )
        assert result.correspondent_contacts_recorded == 0  # type: ignore[attr-defined]
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        assert not contacts.exists() or list(contacts.rglob("*.md")) == []


class TestLaterCorrectionResolves:
    def test_email_keyed_correction_resolves_at_tier_0_to_the_created_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2 — the defect this issue exists to close, asserted end to end.

        Before this change the compile wrote no contact record, so this exact
        resolution returned ``None`` (``email-handle-no-match``) and the
        correction raised a tier again.
        """
        knowledge, result = _compile(
            tmp_path, monkeypatch, note=_prose_note(), classified=[_person(_DISPLAY)]
        )
        created = result.created[0]  # type: ignore[attr-defined]
        page_path = knowledge / "wiki" / created.filename

        resolved = resolve_target(
            {"type": "person", "handle": {"email": _ADDRESS}},
            index=EntityIndex(knowledge / "wiki"),
            registry_entities={},
            knowledge_root=knowledge,
            config=dict(EXCLUDED_CONFIG),
        )
        assert resolved == page_path

    def test_resolution_is_case_insensitive_on_the_address(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, result = _compile(
            tmp_path, monkeypatch, note=_prose_note(), classified=[_person(_DISPLAY)]
        )
        page_path = knowledge / "wiki" / result.created[0].filename  # type: ignore[attr-defined]
        resolved = resolve_target(
            {"type": "person", "handle": {"email": _ADDRESS.upper()}},
            index=EntityIndex(knowledge / "wiki"),
            registry_entities={},
            knowledge_root=knowledge,
            config=dict(EXCLUDED_CONFIG),
        )
        assert resolved == page_path


class TestAmbiguityIsPreserved:
    def test_address_owned_by_another_entity_writes_nothing_and_escalates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 — no second record, and the case escalates as it does today."""
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "uid-incumbent", name="Robin Vance")
        other = _write_record(
            knowledge, "incumbent-contact.md", uid="uid-incumbent", emails=[_ADDRESS]
        )
        before = other.read_text(encoding="utf-8")

        _, result = _compile(
            tmp_path, monkeypatch, note=_prose_note(), classified=[_person(_DISPLAY)]
        )
        assert result.correspondent_contacts_recorded == 0  # type: ignore[attr-defined]

        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        assert pii.resolve_contact_records(contacts, _ADDRESS) == [other]
        assert other.read_text(encoding="utf-8") == before

        escalated = result.escalated  # type: ignore[attr-defined]
        assert any(
            "uid-incumbent" in item.description for item in escalated
        ), f"expected an ambiguity escalation naming the incumbent, got {escalated}"

    def test_several_created_people_and_no_name_match_declines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, result = _compile(
            tmp_path,
            monkeypatch,
            note=_prose_note(display_name=None),
            classified=[_person(_DISPLAY), _person("Robin Vance")],
        )
        assert len(result.created) == 2  # type: ignore[attr-defined]
        assert result.correspondent_contacts_recorded == 0  # type: ignore[attr-defined]
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        assert not contacts.exists() or list(contacts.rglob("*.md")) == []
        assert any(
            "cannot tell which one" in item.description
            for item in result.escalated  # type: ignore[attr-defined]
        )

    def test_several_created_people_with_a_name_match_picks_the_correspondent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, result = _compile(
            tmp_path,
            monkeypatch,
            note=_prose_note(),
            classified=[_person("Robin Vance"), _person(_DISPLAY)],
        )
        assert len(result.created) == 2  # type: ignore[attr-defined]
        assert result.correspondent_contacts_recorded == 1  # type: ignore[attr-defined]
        dana = next(e for e in result.created if e.name == _DISPLAY)  # type: ignore[attr-defined]
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        records = pii.resolve_contact_records(contacts, _ADDRESS)
        assert len(records) == 1
        assert pii.uid_on_record(records[0]) == dana.uid
