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

Extended by athenaeum#2209 (the correspondent shortcut records an introducer's
address on the introducee; see ``docs/design/attribution-verification.md``
§10.1 and §8.1):

- ``TestMaskContactAddress`` — the ``pii.mask_contact_address`` helper itself.
- ``TestResolvedCorrespondentRecordsNothing`` — AC1: the address already
  resolves to an existing page, so no created page is the correspondent;
  nothing is recorded and nothing escalates.
- ``TestNameRequiredWhenPresent`` — AC2: a present ``correspondent_name``
  must match a created page's name or alias, even with only one page.
- ``TestNoNameLocalPartMatchEndToEnd`` / ``TestLocalPartMatchesNameTable`` —
  AC3: no name, one created page — the address local part must name-match it.
- ``TestMaskedEscalationsNeverLeakTheAddress`` — AC4: every escalation masks
  the address, through the real ``_pending_questions.md`` renderer.
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
# pii.mask_contact_address (athenaeum#2209) — the masking helper in isolation
# ---------------------------------------------------------------------------


class TestMaskContactAddress:
    """``<first char of local part>***@<domain>`` — never the bare
    ``<first char>@<domain>`` form, which is itself a syntactically valid
    address (see the function's docstring for why that distinction is
    load-bearing for ``_pending_questions.md``)."""

    def test_masks_to_first_char_plus_domain(self) -> None:
        assert pii.mask_contact_address("alex@example.org") == "a***@example.org"

    def test_the_mask_is_not_itself_an_inline_email_token(self) -> None:
        """The whole point: a bare ``a@example.org`` mask would be
        re-detected by the very scanner this issue protects the wiki from."""
        masked = pii.mask_contact_address("alex@example.org")
        assert pii.find_inline_emails(masked) == []

    def test_single_character_local_part(self) -> None:
        assert pii.mask_contact_address("a@example.org") == "a***@example.org"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_or_whitespace_is_empty(self, value: str) -> None:
        assert pii.mask_contact_address(value) == ""

    def test_no_at_sign_masks_to_first_char_only(self) -> None:
        assert pii.mask_contact_address("not-an-address") == "n***"

    def test_more_than_one_at_sign_splits_on_the_last(self) -> None:
        assert (
            pii.mask_contact_address("a@b@example.org") == "a***@example.org"
        )


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


class TestResolvedCorrespondentRecordsNothing:
    """AC1 (athenaeum#2209) — design §10.1 step 1.

    This is the intro-email shape the issue exists for: the correspondent
    (the ``From`` address) already has an existing page and a contact record
    holding the address, so the ONE page this compile creates is someone
    else's (the introducee) — not the correspondent's. Before athenaeum#2209 the
    old shortcut handed the correspondent's address to that sole created page
    unconditionally (``len(created_people) == 1`` short-circuit), which is
    exactly the misattribution this issue closes. The fix: nothing is
    recorded on the created page, and — per the design — nothing is
    escalated either, because a resolved correspondent is not a question for
    a human.
    """

    def test_writes_nothing_and_does_not_escalate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

        # Nothing new was recorded, and the incumbent's own record is
        # byte-identical to before the compile ran.
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        assert pii.resolve_contact_records(contacts, _ADDRESS) == [other]
        assert other.read_text(encoding="utf-8") == before

        # And — unlike the OTHER_ENTITY/RECORD_WITHOUT_UID declines below —
        # a resolved correspondent raises no escalation at all: there is no
        # question for a human, per design §10.1 step 1.
        assert result.escalated == []  # type: ignore[attr-defined]


class TestAmbiguityIsPreserved:
    def test_address_tied_to_an_orphan_uid_writes_nothing_and_escalates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 — the OBSERVED_CONTACT_OTHER_ENTITY decline still escalates.

        Unlike ``TestResolvedCorrespondentRecordsNothing`` above, the
        contact record here names a uid with NO existing page
        (``uid-ghost``) — an orphan uid, which
        ``compile_routing.resolve_correspondent_page`` treats as a
        non-unique, unresolvable outcome (``email-handle-orphan-uid``) and
        returns ``None`` for, same as a genuinely unknown address. So the
        step-1 guard stays inactive, this compile picks the one page it
        created as the correspondent (its name matches), and
        ``record_observed_contact_value`` is the one that declines — because
        the address is already on the surface, tied to a DIFFERENT uid. That
        decline still escalates (there IS a real question here: which uid is
        right), and the escalation must mask the address.
        """
        knowledge = tmp_path / "knowledge"
        other = _write_record(
            knowledge, "orphan-contact.md", uid="uid-ghost", emails=[_ADDRESS]
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
            "uid-ghost" in item.description for item in escalated
        ), f"expected an ambiguity escalation naming the orphan uid, got {escalated}"
        for item in escalated:
            assert _ADDRESS not in item.description
            assert _ADDRESS not in item.entity_name

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

    def test_address_on_a_record_naming_no_entity_declines_and_escalates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The address is known, but nothing on the surface says whose it is.

        Declining leaves this resolving exactly as it does today
        (``email-handle-record-without-uid`` raises a tier) rather than
        adopting an address for a page created this run.
        """
        knowledge = tmp_path / "knowledge"
        unowned = _write_record(
            knowledge, "unowned-contact.md", uid=None, emails=[_ADDRESS]
        )
        before = unowned.read_text(encoding="utf-8")

        _, result = _compile(
            tmp_path, monkeypatch, note=_prose_note(), classified=[_person(_DISPLAY)]
        )
        assert result.correspondent_contacts_recorded == 0  # type: ignore[attr-defined]
        assert unowned.read_text(encoding="utf-8") == before
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        assert pii.resolve_contact_records(contacts, _ADDRESS) == [unowned]
        assert any(
            "no record says whose it is" in item.description
            for item in result.escalated  # type: ignore[attr-defined]
        )

    def test_a_surface_write_that_raises_never_fails_the_compile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-open: the wiki writes have already landed, so this cannot raise."""

        def _boom(*_a: object, **_k: object) -> None:
            raise OSError("contacts surface is read-only")

        monkeypatch.setattr(
            "athenaeum.librarian.record_observed_contact_value", _boom
        )
        knowledge, result = _compile(
            tmp_path, monkeypatch, note=_prose_note(), classified=[_person(_DISPLAY)]
        )
        # The page still landed, and the compile reported it as created.
        assert len(result.created) == 1  # type: ignore[attr-defined]
        assert (knowledge / "wiki" / result.created[0].filename).is_file()  # type: ignore[attr-defined]
        assert result.correspondent_contacts_recorded == 0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC2 (athenaeum#2209) — a present `correspondent_name` must match a created
# page's name or alias, even when only one page was created.
# ---------------------------------------------------------------------------


class TestNameRequiredWhenPresent:
    """Before athenaeum#2209 the one-page shortcut returned the sole created page
    unconditionally, so a name mismatch — or no name check at all — was never
    possible on the single-page path. This is exactly the intro-email defect:
    the correspondent's declared name need not belong to the page the compile
    happened to create.
    """

    def test_one_created_page_that_matches_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, result = _compile(
            tmp_path, monkeypatch, note=_prose_note(), classified=[_person(_DISPLAY)]
        )
        assert len(result.created) == 1  # type: ignore[attr-defined]
        assert result.correspondent_contacts_recorded == 1  # type: ignore[attr-defined]
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        records = pii.resolve_contact_records(contacts, _ADDRESS)
        assert len(records) == 1
        assert pii.uid_on_record(records[0]) == result.created[0].uid  # type: ignore[attr-defined]

    def test_one_created_page_that_does_not_match_declines_and_escalates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The note names ``Dana Quill``; the only created page is someone else."""
        knowledge, result = _compile(
            tmp_path,
            monkeypatch,
            note=_prose_note(),  # correspondent_name: "Dana Quill"
            classified=[_person("Robin Vance")],
        )
        assert len(result.created) == 1  # type: ignore[attr-defined]
        assert result.correspondent_contacts_recorded == 0  # type: ignore[attr-defined]
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        assert not contacts.exists() or list(contacts.rglob("*.md")) == []
        escalated = result.escalated  # type: ignore[attr-defined]
        assert any(
            "no created person page's name or alias matches" in item.description
            for item in escalated
        ), f"expected a name-mismatch escalation, got {escalated}"


# ---------------------------------------------------------------------------
# AC3 (athenaeum#2209) — no name, exactly one created page: the address's
# local part must name-match that page. A first name alone does not count.
# ---------------------------------------------------------------------------


class TestNoNameLocalPartMatchEndToEnd:
    def test_matching_local_part_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No correspondent_name; the address's local part is ``dana.quill``
        (``first.last``), which name-matches the one created page."""
        knowledge, result = _compile(
            tmp_path,
            monkeypatch,
            note=_prose_note(display_name=None),
            classified=[_person(_DISPLAY)],
        )
        assert len(result.created) == 1  # type: ignore[attr-defined]
        assert result.correspondent_contacts_recorded == 1  # type: ignore[attr-defined]

    def test_non_matching_local_part_declines_and_escalates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No correspondent_name; the one created page is someone else
        entirely, so the local part cannot name-match it."""
        knowledge, result = _compile(
            tmp_path,
            monkeypatch,
            note=_prose_note(display_name=None),
            classified=[_person("Robin Vance")],
        )
        assert len(result.created) == 1  # type: ignore[attr-defined]
        assert result.correspondent_contacts_recorded == 0  # type: ignore[attr-defined]
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        assert not contacts.exists() or list(contacts.rglob("*.md")) == []
        escalated = result.escalated  # type: ignore[attr-defined]
        assert any(
            "does not name-match" in item.description for item in escalated
        ), f"expected a local-part-mismatch escalation, got {escalated}"


class TestLocalPartMatchesNameTable:
    """Table-driven coverage of ``_local_part_matches_name`` (AC3).

    Covers full name, ``flast``, ``first.last``/``firstlast`` (one check,
    since normalizing strips the only difference between them), a
    three-token name's first+last ``flast`` form, an alias match, and the
    first-name-alone / last-name-alone declines the issue calls out
    explicitly.
    """

    @pytest.mark.parametrize(
        ("local_part", "name", "aliases", "expected"),
        [
            # firstlast / first.last / firstname_lastname — one normalized
            # check, so all three collapse to the same assertion.
            ("janedoe", "Jane Doe", [], True),
            ("jane.doe", "Jane Doe", [], True),
            ("jane_doe", "Jane Doe", [], True),
            ("Jane-Doe", "Jane Doe", [], True),
            # flast
            ("jdoe", "Jane Doe", [], True),
            # a +tag suffix is stripped before matching.
            ("jane.doe+work", "Jane Doe", [], True),
            # three-or-more tokens: flast uses FIRST and LAST only.
            ("jmdoe", "Jane Middle Doe", [], False),
            ("jdoe", "Jane Middle Doe", [], True),
            ("janemiddledoe", "Jane Middle Doe", [], True),
            # alias match, same rules as the primary name.
            ("ddoe", "Jane Doe", ["Dana Doe"], True),
            # first name alone, or last name alone: neither is an accepted
            # form, even though both are substrings of the accepted ones.
            ("jane", "Jane Doe", [], False),
            ("doe", "Jane Doe", [], False),
            # an unrelated local part.
            ("robin.vance", "Jane Doe", [], False),
        ],
    )
    def test_accepted_and_declined_forms(
        self, local_part: str, name: str, aliases: list[str], expected: bool
    ) -> None:
        from athenaeum.librarian import _local_part_matches_name

        assert _local_part_matches_name(local_part, name, aliases) is expected


# ---------------------------------------------------------------------------
# AC4 (athenaeum#2209) — every escalation masks the address, through the REAL
# `_pending_questions.md` renderer (`tiers.tier4_escalate`), not just on the
# `EscalationItem` fields. `tiers.py` renders `entity_name` into both the
# heading and the `**Also affects**:` line, so the no-display-name case —
# where `entity_name` used to fall back to the raw address — is the one
# that actually made the address the block's title.
# ---------------------------------------------------------------------------


class TestMaskedEscalationsNeverLeakTheAddress:
    def _rendered(self, tmp_path: Path, escalated: list[object]) -> str:
        from athenaeum.tiers import tier4_escalate

        pending = tmp_path / "pending" / "_pending_questions.md"
        tier4_escalate(escalated, pending)  # type: ignore[arg-type]
        return pending.read_text(encoding="utf-8")

    def test_no_display_name_decline_masks_the_title(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No display name: `entity_name` used to fall back to `ref.address`,
        making the raw address the rendered block's own title."""
        _, result = _compile(
            tmp_path,
            monkeypatch,
            note=_prose_note(display_name=None),
            classified=[_person("Robin Vance")],  # local part does not match
        )
        escalated = result.escalated  # type: ignore[attr-defined]
        assert escalated
        rendered = self._rendered(tmp_path, escalated)
        assert _ADDRESS not in rendered
        assert pii.mask_contact_address(_ADDRESS) in rendered

    def test_name_mismatch_decline_masks_the_address(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, result = _compile(
            tmp_path,
            monkeypatch,
            note=_prose_note(),  # correspondent_name: "Dana Quill"
            classified=[_person("Robin Vance")],
        )
        escalated = result.escalated  # type: ignore[attr-defined]
        assert escalated
        rendered = self._rendered(tmp_path, escalated)
        assert _ADDRESS not in rendered

    def test_other_entity_decline_masks_the_address(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_record(knowledge, "orphan-contact.md", uid="uid-ghost", emails=[_ADDRESS])

        _, result = _compile(
            tmp_path, monkeypatch, note=_prose_note(), classified=[_person(_DISPLAY)]
        )
        escalated = result.escalated  # type: ignore[attr-defined]
        assert escalated
        rendered = self._rendered(tmp_path, escalated)
        assert _ADDRESS not in rendered

    def test_several_pages_no_match_decline_masks_the_address(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, result = _compile(
            tmp_path,
            monkeypatch,
            note=_prose_note(display_name=None),
            classified=[_person(_DISPLAY), _person("Robin Vance")],
        )
        escalated = result.escalated  # type: ignore[attr-defined]
        assert escalated
        rendered = self._rendered(tmp_path, escalated)
        assert _ADDRESS not in rendered
