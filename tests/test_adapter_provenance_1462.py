# SPDX-License-Identifier: Apache-2.0
"""Adapter frontmatter provenance ledger (issue athenaeum#1462).

Covers the issue's four acceptance criteria:

- **AC1** (mechanism decision + rejected alternative): recorded in
  :mod:`athenaeum.adapter_provenance`'s module docstring and
  ``docs/design/provenance-shape.md`` §13 — not re-litigated here, but
  ``TestMechanismChoiceIsDocumented`` pins that both live where the issue
  requires.
- **AC2** (explicit, bounded key set): ``TestExtractAdapterProvenance`` —
  an unrecognized source, and a recognized source's own UNLISTED
  frontmatter key (``participants``, straight out of the live corpus
  shape), are both proven excluded.
- **AC3** (counter-example: join works, unlisted key does not survive):
  ``TestExtractAdapterProvenance::test_unlisted_key_never_captured`` plus
  ``TestEndToEndCompile::test_compiled_page_carries_no_adapter_fields``.
- **AC4** (end-to-end join without reading the raw file):
  ``TestEndToEndCompile::test_resolve_source_for_page_after_raw_unlinked``
  compiles a fixture record through the real :func:`athenaeum.librarian.process_one`
  pipeline, deletes the raw file, and resolves page -> source purely from
  the ledger.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.adapter_provenance import (
    ADAPTER_PROVENANCE_VALUE_KEYS,
    SOURCE_OBJECT_ID_KEYS,
    extract_adapter_provenance,
    read_adapter_provenance,
    record_adapter_provenance_for_pages,
    resolve_source_for_page,
)
from athenaeum.librarian import process_one
from athenaeum.models import ClassifiedEntity, EntityIndex, RawFile, TokenUsage

VALID_TYPES = ["person", "company", "concept", "reference"]
VALID_ACCESS = ["open", "internal", "confidential", "personal"]

# Verbatim shape measured against a live mural-board-summary record
# (/knowledge/raw/mural-board-summary/, 2026-09-08) -- see the module
# docstring's decision note. `participants` is deliberately present and
# deliberately NOT in ADAPTER_PROVENANCE_VALUE_KEYS: it is this fixture's
# "unlisted key" for the AC3 counter-example.
_MURAL_BOARD_ID = "kromatic5164.1730753845696"
_MURAL_FIXTURE_CONTENT = (
    "---\n"
    "name: Nebiyou - Innovation Accounting Program (IAP)\n"
    "description: Mural board summary\n"
    "source: script:athenaeum_adapters\n"
    f"mural_board_id: {_MURAL_BOARD_ID}\n"
    "room: KIT Open Enrollment Workspaces\n"
    "workspace: Kromatic\n"
    "created_on: '1730753845696'\n"
    "updated_on: '1732116400407'\n"
    "text_fragment_count: 137\n"
    "archive_path: /Users/tristankromer/knowledge/raw/mural/kromatic5164.json\n"
    "template_only: false\n"
    "participants:\n"
    "- Nebiyou\n"
    "---\n\n"
    "This board documents a workshop on Innovation Accounting.\n"
)


def _raw(raw_dir: Path, content: str, filename: str = "board.md") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=raw_dir.name, timestamp="", uuid8="")


def _classified(name: str, *, observations: str = "") -> ClassifiedEntity:
    return ClassifiedEntity(
        name=name,
        entity_type="concept",
        tags=[],
        access="internal",
        is_new=True,
        existing_uid=None,
        observations=observations,
    )


# ---------------------------------------------------------------------------
# AC1 -- the decision is recorded somewhere durable
# ---------------------------------------------------------------------------


class TestMechanismChoiceIsDocumented:
    def test_module_docstring_records_decision_and_rejected_alternative(self) -> None:
        import athenaeum.adapter_provenance as mod

        doc = mod.__doc__ or ""
        assert "Decision (AC1): mechanism (b)" in doc
        assert "Rejected: (a)" in doc

    def test_design_doc_records_the_same_decision(self) -> None:
        design_doc = Path("docs/design/provenance-shape.md").read_text(encoding="utf-8")
        assert "athenaeum#1462" in design_doc
        assert "adapter_provenance" in design_doc


# ---------------------------------------------------------------------------
# AC2 / AC3 -- extract_adapter_provenance: bounded, explicit set
# ---------------------------------------------------------------------------


class TestExtractAdapterProvenance:
    def test_full_fixture_extracts_id_and_bounded_fields(self) -> None:
        result = extract_adapter_provenance("mural-board-summary", _MURAL_FIXTURE_CONTENT)
        assert result is not None
        source_object_id, fields = result
        assert source_object_id == _MURAL_BOARD_ID
        assert set(fields) == ADAPTER_PROVENANCE_VALUE_KEYS
        assert fields["room"] == "KIT Open Enrollment Workspaces"
        assert fields["workspace"] == "Kromatic"
        assert fields["template_only"] is False
        assert fields["text_fragment_count"] == 137

    def test_unlisted_key_never_captured(self) -> None:
        """AC3 counter-example, extraction level: `participants` is present
        in the raw frontmatter but is not in ADAPTER_PROVENANCE_VALUE_KEYS,
        so it must never appear in the extracted fields."""
        result = extract_adapter_provenance("mural-board-summary", _MURAL_FIXTURE_CONTENT)
        assert result is not None
        _source_object_id, fields = result
        assert "participants" not in fields

    def test_unrecognized_source_returns_none(self) -> None:
        """A source with no entry in SOURCE_OBJECT_ID_KEYS is never
        ledgered, no matter what its frontmatter carries -- an adapter
        cannot opt itself in by shape alone."""
        assert "some-other-source" not in SOURCE_OBJECT_ID_KEYS
        result = extract_adapter_provenance("some-other-source", _MURAL_FIXTURE_CONTENT)
        assert result is None

    def test_missing_id_key_returns_none(self) -> None:
        content = (
            "---\nname: Board with no id\nroom: Somewhere\n---\n\nBody.\n"
        )
        assert extract_adapter_provenance("mural-board-summary", content) is None

    def test_blank_id_value_returns_none(self) -> None:
        content = "---\nname: X\nmural_board_id: ''\n---\n\nBody.\n"
        assert extract_adapter_provenance("mural-board-summary", content) is None

    def test_no_frontmatter_fails_open(self) -> None:
        assert extract_adapter_provenance("mural-board-summary", "just text\n") is None

    def test_partial_fixture_only_captures_present_keys(self) -> None:
        """Fields absent from the raw frontmatter are absent from the
        result too -- never backfilled/defaulted."""
        content = (
            "---\nname: Sparse board\nmural_board_id: board-1\nroom: R1\n---\n\nBody.\n"
        )
        result = extract_adapter_provenance("mural-board-summary", content)
        assert result is not None
        _source_object_id, fields = result
        assert fields == {"room": "R1"}


# ---------------------------------------------------------------------------
# Ledger round-trip in isolation
# ---------------------------------------------------------------------------


class TestLedgerRoundTrip:
    def test_write_then_resolve_source_for_page(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        records = record_adapter_provenance_for_pages(
            "mural-board-summary",
            "mural-board-summary/board.md",
            _MURAL_FIXTURE_CONTENT,
            ["uid-abc123"],
            cache_dir=cache_dir,
        )
        assert len(records) == 1
        assert records[0].source_object_id == _MURAL_BOARD_ID

        rows = resolve_source_for_page("uid-abc123", cache_dir=cache_dir)
        assert len(rows) == 1
        assert rows[0]["source_object_id"] == _MURAL_BOARD_ID
        assert rows[0]["source"] == "mural-board-summary"
        assert rows[0]["raw_ref"] == "mural-board-summary/board.md"
        assert "participants" not in rows[0]["fields"]
        assert set(rows[0]["fields"]) == ADAPTER_PROVENANCE_VALUE_KEYS

    def test_resolve_source_for_unknown_page_is_empty(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        record_adapter_provenance_for_pages(
            "mural-board-summary",
            "mural-board-summary/board.md",
            _MURAL_FIXTURE_CONTENT,
            ["uid-abc123"],
            cache_dir=cache_dir,
        )
        assert resolve_source_for_page("uid-does-not-exist", cache_dir=cache_dir) == []

    def test_empty_page_uids_writes_nothing(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        records = record_adapter_provenance_for_pages(
            "mural-board-summary", "mural-board-summary/board.md", _MURAL_FIXTURE_CONTENT, [],
            cache_dir=cache_dir,
        )
        assert records == []
        assert read_adapter_provenance(cache_dir=cache_dir) == []

    def test_unrecognized_source_writes_nothing(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        records = record_adapter_provenance_for_pages(
            "some-other-source",
            "some-other-source/board.md",
            _MURAL_FIXTURE_CONTENT,
            ["uid-xyz"],
            cache_dir=cache_dir,
        )
        assert records == []
        assert read_adapter_provenance(cache_dir=cache_dir) == []

    def test_one_raw_can_join_multiple_pages(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        records = record_adapter_provenance_for_pages(
            "mural-board-summary",
            "mural-board-summary/board.md",
            _MURAL_FIXTURE_CONTENT,
            ["uid-1", "uid-2"],
            cache_dir=cache_dir,
        )
        assert len(records) == 2
        assert resolve_source_for_page("uid-1", cache_dir=cache_dir)[0]["source_object_id"] == (
            _MURAL_BOARD_ID
        )
        assert resolve_source_for_page("uid-2", cache_dir=cache_dir)[0]["source_object_id"] == (
            _MURAL_BOARD_ID
        )


# ---------------------------------------------------------------------------
# AC3 / AC4 -- end to end through athenaeum.librarian.process_one
# ---------------------------------------------------------------------------


class TestEndToEndCompile:
    def _compile_fixture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, str, RawFile]:
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))

        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(knowledge / "raw" / "mural-board-summary", _MURAL_FIXTURE_CONTENT)

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [
                _classified(
                    "Nebiyou - Innovation Accounting Program (IAP)",
                    observations="Workshop notes.",
                )
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — the real classify client must never be called"
        )
        write_response = MagicMock()
        write_response.content = [
            MagicMock(
                text="# Nebiyou - Innovation Accounting Program (IAP)\n\n"
                "Workshop notes on Innovation Accounting.[^1]\n\n"
                "[^1]: mural-board-summary/board.md"
            )
        ]
        write_client = MagicMock()
        write_client.messages.create.return_value = write_response

        usage = TokenUsage()
        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=usage,
            write_client=write_client,
        )
        assert len(result.created) == 1
        uid = result.created[0].uid
        page_path = wiki / result.created[0].filename
        assert page_path.is_file()
        return page_path, uid, raw

    def test_compiled_page_carries_no_adapter_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3: the unlisted key (and every OTHER adapter key) must not
        reach the compiled page under mechanism (b) -- the page carries
        nothing from the adapter's frontmatter beyond what Tier 2/3 already
        wrote through its own fixed field set."""
        page_path, _uid, _raw_file = self._compile_fixture(tmp_path, monkeypatch)
        page_text = page_path.read_text(encoding="utf-8")
        assert "mural_board_id" not in page_text
        assert "archive_path" not in page_text
        assert "participants" not in page_text
        assert "template_only" not in page_text

    def test_resolve_source_for_page_after_raw_unlinked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4: compile a fixture record, then resolve page -> source
        object WITHOUT reading the raw file -- proven here by actually
        deleting the raw file before resolving."""
        page_path, uid, raw = self._compile_fixture(tmp_path, monkeypatch)

        # Simulate raw-record unlinking -- the librarian's own post-compile
        # retire pass deletes the raw file once it has been folded into a
        # wiki page. If resolve_source_for_page had to read raw.path this
        # would raise FileNotFoundError from here on.
        raw.path.unlink()
        assert not raw.path.exists()

        rows = resolve_source_for_page(uid)
        assert len(rows) == 1
        assert rows[0]["source_object_id"] == _MURAL_BOARD_ID
        assert rows[0]["source"] == "mural-board-summary"
        assert "participants" not in rows[0]["fields"]
        assert set(rows[0]["fields"]) == ADAPTER_PROVENANCE_VALUE_KEYS
        # The page itself still exists and is untouched by this join.
        assert page_path.is_file()
