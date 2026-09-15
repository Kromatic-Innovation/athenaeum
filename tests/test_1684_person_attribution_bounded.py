# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1684 — person attribution must never paste a whole raw file.

Regression coverage for the two remedies the issue's Plan calls for:

1. :func:`athenaeum.intake.attribute_person_observation` attaches a BOUNDED
   excerpt (:data:`athenaeum.intake.PERSON_OBSERVATION_MAX_CHARS`) plus a
   source reference — never the whole raw body.
2. :func:`athenaeum.intake.is_structured_jsonl_raw_file` gates a `.jsonl`
   file shaped as structured machine records out of person attribution
   entirely, ahead of `athenaeum.identity_resolution.match_person_mentions`'s
   call site in `athenaeum.librarian.process_one`.

Four scenarios, matching the issue's four acceptance criteria:

- ``TestLargeRawFileNeverPastesWhole`` (AC1)
- ``TestContactSyncShapedJsonlIsExcluded`` (AC2)
- ``TestProseFileMentioningTwoPeopleGetsBoundedExcerptsEach`` (AC3)
- ``TestNothingAttributableCreatesNoEmptyNotesHeading`` (AC4)

All fixtures are synthetic — no client data lives in this public repo.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.intake import (
    PERSON_OBSERVATION_MAX_CHARS,
    attribute_person_observation,
    is_structured_jsonl_raw_file,
)
from athenaeum.librarian import process_one
from athenaeum.models import EntityIndex, RawFile
from athenaeum.person_registry import PersonRegistry, PersonRegistryEntry


def _write_person(
    root: Path,
    *,
    uid: str,
    name: str,
    body: str = "Body.\n",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    filename = f"{uid}-{name.lower().replace(' ', '-')}.md"
    path = root / filename
    path.write_text(
        f"---\nuid: {uid}\ntype: person\nname: {name}\n---\n\n# {name}\n\n{body}",
        encoding="utf-8",
    )
    return path


def _make_raw(content: str, path: Path | None = None) -> RawFile:
    return RawFile(
        path=path or Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


class _EmptyClassifyClient:
    """A client whose tier2 classify call always reports zero entities —
    used to drive `process_one` past the gated tier-0 step without any
    OTHER tier mutating a fixture page, so any change observed on a person
    page can only have come from the step under test."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        response = MagicMock()
        response.content = [MagicMock(text=json.dumps([]))]
        response.stop_reason = "end_turn"
        return response


# ---------------------------------------------------------------------------
# AC1 — a large raw file never pastes its whole body
# ---------------------------------------------------------------------------


class TestLargeRawFileNeverPastesWhole:
    def test_large_raw_file_yields_only_a_bounded_excerpt(self, tmp_path: Path) -> None:
        registry_root = tmp_path / "registry"
        page = _write_person(registry_root, uid="person1a", name="Alice Zhang")
        entry = PersonRegistryEntry(uid="person1a", path=page, name="Alice Zhang")

        filler_lines = [
            f"Filler context line number {i:04d} about the project.\n" for i in range(250)
        ]
        filler_lines.insert(125, "Caught up with Alice Zhang about the roadmap today.\n")
        raw_body = "".join(filler_lines)
        assert len(raw_body.splitlines()) > 200  # fixture really is >200 lines
        raw = _make_raw(raw_body)

        changed = attribute_person_observation(raw, entry)
        assert changed is True

        text = page.read_text(encoding="utf-8")
        assert raw_body not in text, "the whole raw body must never be pasted verbatim"

        # A source reference is present in place of the pasted body.
        assert f"(source: {raw.ref})" in text

        notes_block = text.split("## Notes", 1)[1].strip()
        prefix = f"- {date.today().isoformat()}: "
        assert notes_block.startswith(prefix)
        observation = notes_block[len(prefix) :]
        assert len(observation) <= PERSON_OBSERVATION_MAX_CHARS, (
            f"attributed observation exceeds the tested size ceiling: {len(observation)} chars"
        )


# ---------------------------------------------------------------------------
# AC2 — a contact-sync-shaped .jsonl is excluded from attribution entirely
# ---------------------------------------------------------------------------


class TestContactSyncShapedJsonlIsExcluded:
    def _semantic_jsonl_fixture(self) -> str:
        # Shaped like the real contact-sync producer's semantic.jsonl:
        # one JSON record per line, several `update_name`-style records,
        # each naming a different person.
        records = [
            {"op": "update_name", "uid": "c1", "field": "name", "value": "Bob Lee"},
            {"op": "update_email", "uid": "c2", "field": "email", "value": "carol@example.com"},
            {"op": "update_name", "uid": "c2", "field": "name", "value": "Carol King"},
            {"op": "update_title", "uid": "c1", "field": "title", "value": "Bob Lee is now VP"},
        ]
        return "\n".join(json.dumps(r) for r in records) + "\n"

    def test_shape_gate_confirms_structured_records(self, tmp_path: Path) -> None:
        raw_path = tmp_path / "raw" / "contact-sync" / "semantic.jsonl"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text(self._semantic_jsonl_fixture(), encoding="utf-8")
        raw = RawFile(path=raw_path, source="contact-sync", timestamp="", uuid8="")

        assert is_structured_jsonl_raw_file(raw) is True

    def test_md_extension_is_never_excluded_by_this_gate(self, tmp_path: Path) -> None:
        # Same JSON-shaped content, but a `.md` extension -- the gate is
        # scoped to `.jsonl` only, regardless of content.
        raw_path = tmp_path / "raw" / "contact-sync" / "notes.md"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text(self._semantic_jsonl_fixture(), encoding="utf-8")
        raw = RawFile(path=raw_path, source="contact-sync", timestamp="", uuid8="")

        assert is_structured_jsonl_raw_file(raw) is False

    def test_prose_jsonl_is_not_excluded(self, tmp_path: Path) -> None:
        # `.jsonl` extension, but genuinely prose content -- the "not
        # prose" half of the test must not fire on this.
        raw_path = tmp_path / "raw" / "contact-sync" / "20240101T000000Z-aa112233.jsonl"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text("Just a note that happens to have a .jsonl name.\n", encoding="utf-8")
        raw = RawFile(path=raw_path, source="contact-sync", timestamp="", uuid8="")

        assert is_structured_jsonl_raw_file(raw) is False

    def test_process_one_never_attributes_the_jsonl_to_matched_people(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        registry_root = tmp_path / "registry"
        bob_page = _write_person(registry_root, uid="c1", name="Bob Lee")
        carol_page = _write_person(registry_root, uid="c2", name="Carol King")
        registry = PersonRegistry(registry_root)
        index = EntityIndex(wiki)  # empty -- no wiki-side match possible either

        raw_dir = tmp_path / "raw" / "contact-sync"
        raw_dir.mkdir(parents=True)
        raw_path = raw_dir / "semantic.jsonl"
        raw_path.write_text(self._semantic_jsonl_fixture(), encoding="utf-8")
        raw = RawFile(path=raw_path, source="contact-sync", timestamp="", uuid8="")

        bob_before = bob_page.read_text(encoding="utf-8")
        carol_before = carol_page.read_text(encoding="utf-8")

        client = _EmptyClassifyClient()
        process_one(
            raw, index, wiki, client, ["person"], [], ["internal"], person_registry=registry
        )

        assert bob_page.read_text(encoding="utf-8") == bob_before, (
            "a structured-record .jsonl must never reach person attribution"
        )
        assert carol_page.read_text(encoding="utf-8") == carol_before
        assert "## Notes" not in bob_page.read_text(encoding="utf-8")
        assert "## Notes" not in carol_page.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC3 — a prose memory file mentioning two people gets bounded excerpts each
# ---------------------------------------------------------------------------


class TestProseFileMentioningTwoPeopleGetsBoundedExcerptsEach:
    def test_each_person_gets_a_bounded_excerpt_not_the_whole_file(self, tmp_path: Path) -> None:
        registry_root = tmp_path / "registry"
        dana_page = _write_person(registry_root, uid="person2a", name="Dana Osei")
        erin_page = _write_person(registry_root, uid="person2b", name="Erin Fox")
        dana_entry = PersonRegistryEntry(uid="person2a", path=dana_page, name="Dana Osei")
        erin_entry = PersonRegistryEntry(uid="person2b", path=erin_page, name="Erin Fox")

        preamble = "Standup notes from today. " * 40  # padding well past any excerpt radius
        raw_body = (
            f"{preamble}\nDana Osei walked through the new onboarding metrics dashboard "
            "and flagged a data quality issue in the funnel step.\n"
            f"{preamble}\nErin Fox is leading the follow-up spike on the funnel data and "
            "will report back Thursday.\n"
            f"{preamble}\n"
        )
        raw = _make_raw(raw_body)

        assert attribute_person_observation(raw, dana_entry) is True
        assert attribute_person_observation(raw, erin_entry) is True

        dana_text = dana_page.read_text(encoding="utf-8")
        erin_text = erin_page.read_text(encoding="utf-8")

        assert raw_body not in dana_text
        assert raw_body not in erin_text
        assert "onboarding metrics dashboard" in dana_text
        assert "follow-up spike" in erin_text
        # Neither page's Notes swallowed the OTHER person's whole context
        # block wholesale -- each excerpt is scoped around its own mention.
        assert "follow-up spike" not in dana_text or len(dana_text) < len(raw_body)
        assert len(dana_text) < len(raw_body)
        assert len(erin_text) < len(raw_body)


# ---------------------------------------------------------------------------
# AC4 — nothing attributable under the new bound creates no empty heading
# ---------------------------------------------------------------------------


class TestNothingAttributableCreatesNoEmptyNotesHeading:
    def test_empty_raw_body_creates_no_notes_heading(self, tmp_path: Path) -> None:
        registry_root = tmp_path / "registry"
        page = _write_person(registry_root, uid="person1a", name="Alice Zhang")
        entry = PersonRegistryEntry(uid="person1a", path=page, name="Alice Zhang")
        raw = _make_raw("---\nsource: manual\n---\n\n   \n")

        before = page.read_text(encoding="utf-8")
        changed = attribute_person_observation(raw, entry)

        assert changed is False
        after = page.read_text(encoding="utf-8")
        assert after == before, "no write at all when nothing is attributable"
        assert "## Notes" not in after

    def test_gated_jsonl_creates_no_notes_heading_via_process_one(self, tmp_path: Path) -> None:
        """The excluded-by-shape path (AC2's mechanism) must ALSO leave no
        empty ``## Notes`` heading behind -- the two remedies share this
        guarantee even though they take different code paths."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        registry_root = tmp_path / "registry"
        page = _write_person(registry_root, uid="c1", name="Bob Lee")
        registry = PersonRegistry(registry_root)
        index = EntityIndex(wiki)

        raw_dir = tmp_path / "raw" / "contact-sync"
        raw_dir.mkdir(parents=True)
        raw_path = raw_dir / "semantic.jsonl"
        raw_path.write_text(
            json.dumps({"op": "update_name", "uid": "c1", "value": "Bob Lee"}) + "\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="contact-sync", timestamp="", uuid8="")

        client = _EmptyClassifyClient()
        process_one(
            raw, index, wiki, client, ["person"], [], ["internal"], person_registry=registry
        )

        assert "## Notes" not in page.read_text(encoding="utf-8")
