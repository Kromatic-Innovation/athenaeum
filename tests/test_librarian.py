"""Integration tests for athenaeum.librarian — discover_raw_files, rebuild_index,
process_one, and the run() pipeline with mocked LLM."""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.models import EntityIndex, RawFile

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    """Create a raw directory with sample intake files."""
    raw = tmp_path / "raw"
    raw.mkdir()
    sessions = raw / "sessions"
    sessions.mkdir()
    imports = raw / "imports"
    imports.mkdir()

    (sessions / "20240406T120000Z-aabb0011.md").write_text(
        "Met with Alice Zhang from Acme Corp about lean coaching.\n"
    )
    (sessions / "20240406T120100Z-ccdd2233.md").write_text(
        "Explored innovation accounting as a concept.\n"
    )
    (imports / "20240406T130000Z-eeff4455.md").write_text(
        "User mentioned preferring dark mode in all tools.\n"
    )
    # Non-standard filename (should still be discovered)
    (sessions / "random-notes.md").write_text("Some freeform notes.\n")
    # .gitkeep should be skipped
    (sessions / ".gitkeep").write_text("")

    return raw


# ---------------------------------------------------------------------------
# discover_raw_files
# ---------------------------------------------------------------------------


class TestDiscoverRawFiles:
    def test_finds_all_files(self, raw_dir: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(raw_dir)
        # 3 standard + 1 non-standard = 4 (skips .gitkeep)
        assert len(files) == 4

    def test_extracts_metadata(self, raw_dir: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(raw_dir)
        standard = [f for f in files if f.timestamp]
        assert len(standard) == 3
        session_files = [f for f in standard if f.source == "sessions"]
        assert len(session_files) == 2
        import_files = [f for f in standard if f.source == "imports"]
        assert len(import_files) == 1

    def test_non_standard_filename(self, raw_dir: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(raw_dir)
        non_standard = [f for f in files if not f.timestamp]
        assert len(non_standard) == 1
        assert non_standard[0].path.name == "random-notes.md"

    def test_skips_gitkeep(self, raw_dir: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(raw_dir)
        names = [f.path.name for f in files]
        assert ".gitkeep" not in names

    def test_skips_answers_source(self, tmp_path: Path) -> None:
        """Issue athenaeum#414: raw/answers/*.md are resolution OUTPUT, not intake.

        Re-discovering them re-feeds already-settled rulings through tier1-2
        classification and tier4 contradiction escalation, so the same ruling
        re-surfaces as fresh pending questions on every run. discover_raw_files
        must exclude the answers/ source entirely, while still discovering
        genuine intake sources sitting alongside it.
        """
        from athenaeum.librarian import discover_raw_files

        raw = tmp_path / "raw"
        answers = raw / "answers"
        answers.mkdir(parents=True)
        sessions = raw / "sessions"
        sessions.mkdir()

        # A resolved-answer fragment (the terminal output of an earlier ruling).
        (answers / "20260711T062202Z-11223344.md").write_text(
            "Kromatic is the primary venture; Krobar.ai is subordinate.\n"
        )
        # A genuine new observation living beside it.
        (sessions / "20260712T090000Z-aabbccdd.md").write_text(
            "Met with a new prospect about lean coaching.\n"
        )

        files = discover_raw_files(raw)
        sources = {f.source for f in files}
        assert "answers" not in sources
        assert all(f.path.parent.name != "answers" for f in files)
        # The sibling intake source is still discovered — we did not over-skip.
        assert "sessions" in sources
        assert len(files) == 1

    def test_empty_dir(self, tmp_path: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        empty = tmp_path / "empty_raw"
        empty.mkdir()
        files = discover_raw_files(empty)
        assert files == []

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(tmp_path / "does_not_exist")
        assert files == []

    def test_characterization_md_corpus_unchanged_by_jsonl_widening(
        self, raw_dir: Path
    ) -> None:
        """Regression pin (issue athenaeum#797): widening the glob/regex to also
        accept `.jsonl` must not change discovery output over the existing
        `.md`-only fixture corpus one byte. This is the largest-blast-radius
        regression named in the issue — every existing caller of
        `discover_raw_files` depends on this being a no-op for pre-existing
        trees.
        """
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(raw_dir)
        assert len(files) == 4
        assert {f.path.name for f in files} == {
            "20240406T120000Z-aabb0011.md",
            "20240406T120100Z-ccdd2233.md",
            "20240406T130000Z-eeff4455.md",
            "random-notes.md",
        }
        standard = [f for f in files if f.timestamp]
        assert len(standard) == 3
        assert all(f.uuid8 for f in standard)


class TestDiscoverRawFilesCorrections:
    """Issue athenaeum#797, `docs/field-corrections.md` §3.1: a correction batch
    lives in the ordinary `raw/<source>/` tree, recognized by shape (its
    first line is a valid batch envelope), not by a reserved subtree. The
    test that matters is that a MALFORMED `.jsonl` still reaches ordinary
    intake — "falls through to ordinary intake" must not silently mean
    "seen by nothing," which is the bug this design removes.
    """

    _VALID_ENVELOPE = (
        '{"record":"batch","schema_version":1,"submitter":"graph-writer",'
        '"batch_id":"20260806T140211Z-9f3ac1d2",'
        '"created_at":"2026-08-06T14:02:11Z"}\n'
        '{"record":"correction","correction_id":"a1b2c3d4e5f60718",'
        '"target":{"uid":"person-alex-doe-a1b2c3d4"},"op":"add",'
        '"field":"backlinks","value":"company-northwind-77aa11bc",'
        '"source":"script:graph-writer","observed_at":"2026-08-06T03:00:00Z"}\n'
    )

    def _mkraw(self, tmp_path: Path) -> Path:
        raw = tmp_path / "raw"
        (raw / "graph-writer").mkdir(parents=True)
        return raw

    def test_valid_envelope_batch_is_skipped(self, tmp_path: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        raw = self._mkraw(tmp_path)
        batch = raw / "graph-writer" / "20260806T140211Z-9f3ac1d2.jsonl"
        batch.write_text(self._VALID_ENVELOPE)

        files = discover_raw_files(raw)
        assert files == []

    def test_not_json_reaches_ordinary_intake(self, tmp_path: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        raw = self._mkraw(tmp_path)
        batch = raw / "graph-writer" / "20260806T140211Z-11111111.jsonl"
        batch.write_text("this is not json at all\nsecond line\n")

        files = discover_raw_files(raw)
        assert [f.path.name for f in files] == ["20260806T140211Z-11111111.jsonl"]
        assert files[0].timestamp == "20260806T140211Z"
        assert files[0].uuid8 == "11111111"

    def test_valid_json_wrong_record_reaches_ordinary_intake(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.librarian import discover_raw_files

        raw = self._mkraw(tmp_path)
        batch = raw / "graph-writer" / "20260806T140211Z-22222222.jsonl"
        batch.write_text('{"record":"note","text":"just a note"}\n')

        files = discover_raw_files(raw)
        assert [f.path.name for f in files] == ["20260806T140211Z-22222222.jsonl"]

    def test_unknown_schema_version_reaches_ordinary_intake(
        self, tmp_path: Path
    ) -> None:
        """Adjudicated contradiction (design doc §3.1 vs §8): an unknown
        `schema_version` is NOT a valid envelope and MUST reach ordinary
        intake, not be skipped.
        """
        from athenaeum.librarian import discover_raw_files

        raw = self._mkraw(tmp_path)
        batch = raw / "graph-writer" / "20260806T140211Z-33333333.jsonl"
        batch.write_text(
            '{"record":"batch","schema_version":7,"submitter":"x",'
            '"batch_id":"b1","created_at":"2026-08-06T14:02:11Z"}\n'
        )

        files = discover_raw_files(raw)
        assert [f.path.name for f in files] == ["20260806T140211Z-33333333.jsonl"]

    def test_zero_byte_jsonl_reaches_ordinary_intake(self, tmp_path: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        raw = self._mkraw(tmp_path)
        batch = raw / "graph-writer" / "20260806T140211Z-44444444.jsonl"
        batch.write_text("")

        files = discover_raw_files(raw)
        assert [f.path.name for f in files] == ["20260806T140211Z-44444444.jsonl"]

    def test_missing_batch_id_reaches_ordinary_intake(self, tmp_path: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        raw = self._mkraw(tmp_path)
        batch = raw / "graph-writer" / "20260806T140211Z-55555555.jsonl"
        batch.write_text(
            '{"record":"batch","schema_version":1,"submitter":"x",'
            '"created_at":"2026-08-06T14:02:11Z"}\n'
        )

        files = discover_raw_files(raw)
        assert [f.path.name for f in files] == ["20260806T140211Z-55555555.jsonl"]

    def test_malformed_batch_alongside_ordinary_md_both_discovered(
        self, tmp_path: Path
    ) -> None:
        """A malformed batch must not eclipse ordinary sibling intake, and
        must itself still be discovered (not seen by nothing)."""
        from athenaeum.librarian import discover_raw_files

        raw = self._mkraw(tmp_path)
        malformed = raw / "graph-writer" / "20260806T140211Z-66666666.jsonl"
        malformed.write_text("not json\n")
        note = raw / "graph-writer" / "20260806T150000Z-77777777.md"
        note.write_text("An ordinary observation.\n")
        valid_batch = raw / "graph-writer" / "20260806T160000Z-88888888.jsonl"
        valid_batch.write_text(self._VALID_ENVELOPE)

        files = discover_raw_files(raw)
        names = {f.path.name for f in files}
        assert names == {
            "20260806T140211Z-66666666.jsonl",
            "20260806T150000Z-77777777.md",
        }


# ---------------------------------------------------------------------------
# RawFile content loading
# ---------------------------------------------------------------------------


class TestRawFileContent:
    def test_lazy_loading(self, raw_dir: Path) -> None:
        raw = RawFile(
            path=raw_dir / "sessions" / "20240406T120000Z-aabb0011.md",
            source="sessions",
            timestamp="20240406T120000Z",
            uuid8="aabb0011",
        )
        # _content is None before access
        assert raw._content is None
        content = raw.content
        assert "Alice Zhang" in content
        # Now cached
        assert raw._content is not None

    def test_ref_format(self) -> None:
        raw = RawFile(
            path=Path("/tmp/knowledge/raw/sessions/20240406T120000Z-aabb0011.md"),
            source="sessions",
            timestamp="20240406T120000Z",
            uuid8="aabb0011",
        )
        assert raw.ref == "sessions/20240406T120000Z-aabb0011.md"


# ---------------------------------------------------------------------------
# tier0_passthrough
# ---------------------------------------------------------------------------


PASSTHROUGH_RAW = """---
uid: 35297ed5
type: person
name: Nicole Segerer
access: personal
tags:
  - relationship:dormant
  - tier:warm-b
  - account:kromatic
  - apollo:enriched
google_contact: people/c971065947330806669
emails:
  - nsegerer@revenera.com
warm_score: 10.8
current_title: SVP and General Manager
current_company: Revenera
linkedin_url: "http://www.linkedin.com/in/nicole-segerer-5209921b"
apollo_employment_history:
  - title: SVP and General Manager
    organization_name: Revenera
    current: true
---

# Nicole Segerer

## Role / Background

_(role / background pending)_
"""


class TestTier0Passthrough:
    """tier0_passthrough must promote pre-structured raw-intake verbatim.

    Custom frontmatter namespaces (relationship:, apollo:, current_title,
    linkedin_url, apollo_employment_history) MUST round-trip unchanged —
    the regression these tests guard against is the LLM-driven Tier 2/3
    path silently dropping any field outside the WikiEntity allowlist.
    """

    def _make_wiki_root(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        return wiki

    def _make_raw(self, tmp_path: Path, content: str) -> RawFile:
        raw_dir = tmp_path / "raw" / "contact-wiki"
        raw_dir.mkdir(parents=True)
        path = raw_dir / "35297ed5-nicole.md"
        path.write_text(content, encoding="utf-8")
        return RawFile(
            path=path,
            source="contact-wiki",
            timestamp="",
            uuid8="",
        )

    def test_promotes_prestructured_verbatim(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_passthrough

        wiki = self._make_wiki_root(tmp_path)
        raw = self._make_raw(tmp_path, PASSTHROUGH_RAW)
        index = EntityIndex(wiki)

        entity = tier0_passthrough(raw, index, wiki, ["person"])

        assert entity is not None
        assert entity.uid == "35297ed5"
        assert entity.name == "Nicole Segerer"
        out_path = wiki / "35297ed5-nicole-segerer.md"
        assert out_path.exists()
        written = out_path.read_text(encoding="utf-8")
        # Custom frontmatter namespaces must survive
        for needle in (
            "relationship:dormant",
            "apollo:enriched",
            "current_title: SVP and General Manager",
            "current_company: Revenera",
            "linkedin_url: http://www.linkedin.com/in/nicole-segerer-5209921b",
            "apollo_employment_history:",
            "organization_name: Revenera",
        ):
            assert needle in written, f"missing custom field: {needle}"
        # Body preserved
        assert "# Nicole Segerer" in written

    def test_registers_in_index(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_passthrough

        wiki = self._make_wiki_root(tmp_path)
        raw = self._make_raw(tmp_path, PASSTHROUGH_RAW)
        index = EntityIndex(wiki)

        tier0_passthrough(raw, index, wiki, ["person"])

        # Subsequent lookups should find the new entity
        hit = index.lookup("Nicole Segerer")
        assert hit is not None
        assert hit[0] == "35297ed5"

    def test_idempotent_on_existing_uid(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_passthrough

        wiki = self._make_wiki_root(tmp_path)
        # Pre-seed wiki with the same uid
        existing = wiki / "35297ed5-nicole-segerer.md"
        existing.write_text(
            "---\nuid: 35297ed5\ntype: person\nname: Nicole Segerer\n"
            "access: personal\ntags: [active]\n---\n\n# Nicole\n",
            encoding="utf-8",
        )
        raw = self._make_raw(tmp_path, PASSTHROUGH_RAW)
        index = EntityIndex(wiki)

        result = tier0_passthrough(raw, index, wiki, ["person"])

        # Already in index — caller falls through to Tier 1/2/3.
        assert result is None
        # Existing wiki page untouched.
        assert "Nicole" in existing.read_text()

    def test_falls_through_when_unstructured(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_passthrough

        wiki = self._make_wiki_root(tmp_path)
        raw = self._make_raw(
            tmp_path,
            "Met with someone about something. No frontmatter at all.\n",
        )
        index = EntityIndex(wiki)

        assert tier0_passthrough(raw, index, wiki, ["person"]) is None
        # No wiki file written.
        assert list(wiki.glob("*.md")) == []

    def test_falls_through_when_type_not_in_schema(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_passthrough

        wiki = self._make_wiki_root(tmp_path)
        raw = self._make_raw(
            tmp_path,
            "---\nuid: deadbeef\ntype: alien\nname: E.T.\n---\n\nbody\n",
        )
        index = EntityIndex(wiki)

        assert tier0_passthrough(raw, index, wiki, ["person"]) is None

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_passthrough

        wiki = self._make_wiki_root(tmp_path)
        raw = self._make_raw(tmp_path, PASSTHROUGH_RAW)
        index = EntityIndex(wiki)

        entity = tier0_passthrough(
            raw,
            index,
            wiki,
            ["person"],
            dry_run=True,
        )

        assert entity is not None  # caller still gets the entity descriptor
        assert list(wiki.glob("*.md")) == []  # but nothing on disk

    def test_provenance_round_trips_byte_for_byte(self, tmp_path: Path) -> None:
        """source + field_sources must round-trip raw → wiki unchanged."""
        from athenaeum.librarian import tier0_passthrough

        wiki = self._make_wiki_root(tmp_path)
        sourced = (
            "---\n"
            "uid: 7a8b9c01\n"
            "type: person\n"
            "name: Sourced Sam\n"
            "access: personal\n"
            "tags:\n"
            "  - relationship:active\n"
            "current_title: VP Eng\n"
            "source: api:apollo:2026-05-07\n"
            "field_sources:\n"
            "  emails: api:apollo:2026-05-07\n"
            "  current_title: linkedin:sourced-sam-1234\n"
            "---\n"
            "\n"
            "# Sourced Sam\n"
        )
        raw = self._make_raw(tmp_path, sourced)
        index = EntityIndex(wiki)

        entity = tier0_passthrough(raw, index, wiki, ["person"])
        assert entity is not None

        out = (wiki / "7a8b9c01-sourced-sam.md").read_text(encoding="utf-8")
        # Exact lines preserved (yaml.dump default_flow_style=False emits
        # the same form on round-trip for these scalar values).
        assert "source: api:apollo:2026-05-07" in out
        assert "field_sources:" in out
        assert "  emails: api:apollo:2026-05-07" in out
        assert "  current_title: linkedin:sourced-sam-1234" in out

    def test_per_value_field_sources_round_trip(self, tmp_path: Path) -> None:
        """Per-value field_sources list shape (athenaeum#102) must round-trip
        byte-for-byte through tier0_passthrough — the shape contract in
        docs/provenance-shape.md §3."""
        from athenaeum.librarian import tier0_passthrough

        wiki = self._make_wiki_root(tmp_path)
        sourced = (
            "---\n"
            "uid: 9b8c7d61\n"
            "type: person\n"
            "name: Per Value Pat\n"
            "access: personal\n"
            "emails:\n"
            "  - pat@one.com\n"
            "  - pat@two.com\n"
            "field_sources:\n"
            "  emails:\n"
            "    - value: pat@one.com\n"
            "      source: api:apollo:2026-04-29\n"
            "    - value: pat@two.com\n"
            "      source: linkedin:patshandle\n"
            "---\n"
            "\n"
            "# Per Value Pat\n"
        )
        raw = self._make_raw(tmp_path, sourced)
        index = EntityIndex(wiki)

        entity = tier0_passthrough(raw, index, wiki, ["person"])
        assert entity is not None

        out_path = wiki / "9b8c7d61-per-value-pat.md"
        out = out_path.read_text(encoding="utf-8")

        # Byte-for-byte contract from docs/provenance-shape.md §3:
        # tier0 stamps ``created`` (when missing) + ``updated``, then
        # renders the meta verbatim. Reconstruct the expected bytes
        # rather than relying on substring checks.
        from athenaeum.models import parse_frontmatter, render_frontmatter

        expected_meta, expected_body = parse_frontmatter(sourced)
        today = date.today().isoformat()
        expected_meta["created"] = today
        expected_meta["updated"] = today
        expected = render_frontmatter(expected_meta) + "\n" + expected_body
        assert out == expected

        # And the per-value list shape parses back as a list of
        # ``{value, source}`` records.
        meta, _ = parse_frontmatter(out)
        emails_fs = meta["field_sources"]["emails"]
        assert isinstance(emails_fs, list)
        assert {e["value"] for e in emails_fs} == {"pat@one.com", "pat@two.com"}


class TestTier0HandleUpsert:
    """Deterministic source-handle seed onto an EXISTING entity (issue athenaeum#486).

    The end-to-end round-trip (raw intake → compile → frontmatter → registry)
    and idempotency are covered in ``tests/test_registry.py``; these lock the
    eligibility gate so a re-seed engages the upsert and everything else falls
    through to the LLM tiers unchanged.
    """

    def _make_raw(self, tmp_path: Path, content: str) -> RawFile:
        raw_dir = tmp_path / "raw" / "contact-wiki"
        raw_dir.mkdir(parents=True)
        path = raw_dir / "seed.md"
        path.write_text(content, encoding="utf-8")
        return RawFile(path=path, source="contact-wiki", timestamp="", uuid8="")

    def _existing(self, wiki: Path, extra_fm: str = "") -> Path:
        wiki.mkdir(parents=True, exist_ok=True)
        page = wiki / "company-x-x.md"
        page.write_text(
            "---\nuid: company-x\ntype: company\nname: X\naccess: internal\n"
            f"{extra_fm}---\n\n# X\n\nBody.\n",
            encoding="utf-8",
        )
        return page

    def test_merges_handles_onto_existing_page(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        page = self._existing(wiki)
        raw = self._make_raw(
            tmp_path,
            "---\nuid: company-x\ntype: company\nname: X\n"
            "domains:\n  - x.example\n---\n\n# X\n\nseed\n",
        )
        index = EntityIndex(wiki)

        out = tier0_handle_upsert(raw, index, wiki, ["company"])
        assert out is not None
        entity, changed = out
        assert changed is True
        assert entity.uid == "company-x"
        assert "domains:" in page.read_text()
        assert "x.example" in page.read_text()
        # Body untouched — not flattened into it.
        assert "Body." in page.read_text()

    def test_reseed_no_delta_is_noop(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        page = self._existing(wiki, extra_fm="domains:\n  - x.example\n")
        before = page.read_text(encoding="utf-8")
        raw = self._make_raw(
            tmp_path,
            "---\nuid: company-x\ntype: company\nname: X\n"
            "domains:\n  - x.example\n---\n\n# X\n\nseed\n",
        )
        out = tier0_handle_upsert(raw, EntityIndex(wiki), wiki, ["company"])
        assert out is not None
        _, changed = out
        assert changed is False
        assert page.read_text(encoding="utf-8") == before  # byte-for-byte stable

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        page = self._existing(wiki)
        before = page.read_text(encoding="utf-8")
        raw = self._make_raw(
            tmp_path,
            "---\nuid: company-x\ntype: company\nname: X\n"
            "domains:\n  - x.example\n---\n\n# X\n\nseed\n",
        )
        out = tier0_handle_upsert(raw, EntityIndex(wiki), wiki, ["company"], dry_run=True)
        assert out is not None and out[1] is True  # reports a delta...
        assert page.read_text(encoding="utf-8") == before  # ...but writes nothing

    def test_new_entity_falls_through(self, tmp_path: Path) -> None:
        """No existing page → tier0_passthrough owns it; upsert declines."""
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        raw = self._make_raw(
            tmp_path,
            "---\nuid: company-x\ntype: company\nname: X\n"
            "domains:\n  - x.example\n---\n\n# X\n\nseed\n",
        )
        assert tier0_handle_upsert(raw, EntityIndex(wiki), wiki, ["company"]) is None

    def test_prestructured_but_no_handles_falls_through(self, tmp_path: Path) -> None:
        """An ordinary note re-intake (no source handles) is left to the LLM tiers."""
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        self._existing(wiki)
        raw = self._make_raw(
            tmp_path,
            "---\nuid: company-x\ntype: company\nname: X\n---\n\n# X\n\nnew note\n",
        )
        assert tier0_handle_upsert(raw, EntityIndex(wiki), wiki, ["company"]) is None

    def test_unstructured_and_wrong_type_fall_through(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        self._existing(wiki)
        index = EntityIndex(wiki)
        # No frontmatter at all.
        raw_none = self._make_raw(tmp_path, "just prose, no frontmatter\n")
        assert tier0_handle_upsert(raw_none, index, wiki, ["company"]) is None
        # type not in the allowlist.
        raw_bad = RawFile(
            path=self._make_raw(
                tmp_path / "b",
                "---\nuid: company-x\ntype: alien\nname: X\n"
                "domains:\n  - x.example\n---\n\nseed\n",
            ).path,
            source="contact-wiki",
            timestamp="",
            uuid8="",
        )
        assert tier0_handle_upsert(raw_bad, index, wiki, ["company"]) is None

    # --- athenaeum#692: a seed with source handles + type/name but NO uid must resolve
    # the existing entity by name and land as frontmatter, not degrade to prose.

    def test_uid_less_seed_resolves_by_name_and_merges(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        page = self._existing(wiki)  # uid: company-x, name: X
        raw = self._make_raw(
            tmp_path,
            # No `uid:` — the realistic shape (the seed names the entity, not its
            # internal wiki uid).
            "---\ntype: company\nname: X\ndomains:\n  - x.example\n---\n\nseed\n",
        )
        out = tier0_handle_upsert(raw, EntityIndex(wiki), wiki, ["company"])
        assert out is not None
        entity, changed = out
        assert changed is True
        assert entity.uid == "company-x"  # resolved by name
        text = page.read_text()
        assert "domains:" in text and "x.example" in text  # landed as frontmatter
        assert "Body." in text  # body untouched, not flattened into it

    def test_uid_less_seed_resolves_by_alias(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        page = self._existing(wiki, extra_fm="aliases:\n  - Xco\n")
        raw = self._make_raw(
            tmp_path,
            "---\ntype: company\nname: Xco\ndomains:\n  - x.example\n---\n\nseed\n",
        )
        out = tier0_handle_upsert(raw, EntityIndex(wiki), wiki, ["company"])
        assert out is not None and out[1] is True
        assert "x.example" in page.read_text()

    def test_uid_less_reseed_is_idempotent_noop(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        page = self._existing(wiki, extra_fm="domains:\n  - x.example\n")
        before = page.read_text(encoding="utf-8")
        raw = self._make_raw(
            tmp_path,
            "---\ntype: company\nname: X\ndomains:\n  - x.example\n---\n\nseed\n",
        )
        out = tier0_handle_upsert(raw, EntityIndex(wiki), wiki, ["company"])
        assert out is not None and out[1] is False
        assert page.read_text(encoding="utf-8") == before  # byte-for-byte stable

    def test_uid_less_seed_without_handles_falls_through(self, tmp_path: Path) -> None:
        """A uid-less raw carrying no source handles is left to the LLM tiers."""
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        self._existing(wiki)
        raw = self._make_raw(
            tmp_path, "---\ntype: company\nname: X\n---\n\njust a note\n"
        )
        assert tier0_handle_upsert(raw, EntityIndex(wiki), wiki, ["company"]) is None

    def test_uid_less_seed_naming_no_entity_declines_loudly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A handle seed that resolves to no existing entity fails LOUDLY (WARNING)
        rather than silently degrading to prose — the athenaeum#692 defect."""
        import logging

        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        self._existing(wiki)  # only entity "X" exists
        raw = self._make_raw(
            tmp_path,
            "---\ntype: company\nname: Nonesuch\ndomains:\n  - n.example\n---\n\nseed\n",
        )
        with caplog.at_level(logging.WARNING):
            assert tier0_handle_upsert(raw, EntityIndex(wiki), wiki, ["company"]) is None
        assert any(
            "names no existing entity" in r.getMessage() for r in caplog.records
        )

    def test_uid_less_seed_cross_type_declines(self, tmp_path: Path) -> None:
        """A name that resolves to a same-named entity of a DIFFERENT type is not
        upserted cross-type."""
        from athenaeum.librarian import tier0_handle_upsert

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # Existing entity named "X" is a person, not a company.
        (wiki / "person-x.md").write_text(
            "---\nuid: person-x\ntype: person\nname: X\naccess: internal\n---\n\n# X\n\nBody.\n",
            encoding="utf-8",
        )
        raw = self._make_raw(
            tmp_path,
            "---\ntype: company\nname: X\ndomains:\n  - x.example\n---\n\nseed\n",
        )
        assert tier0_handle_upsert(raw, EntityIndex(wiki), wiki, ["company", "person"]) is None
        assert "x.example" not in (wiki / "person-x.md").read_text()


# ---------------------------------------------------------------------------
# rebuild_index
# ---------------------------------------------------------------------------


class TestRebuildIndex:
    def test_creates_index(self, wiki_dir: Path) -> None:
        from athenaeum.librarian import rebuild_index

        rebuild_index(wiki_dir)
        index_path = wiki_dir / "_index.md"
        assert index_path.exists()
        content = index_path.read_text()
        assert "# Knowledge Wiki Index" in content
        assert "Acme Corp" in content

    def test_groups_by_type(self, wiki_dir: Path) -> None:
        from athenaeum.librarian import rebuild_index

        rebuild_index(wiki_dir)
        content = (wiki_dir / "_index.md").read_text()
        assert "## Company" in content
        assert "## Project" in content

    def test_empty_wiki(self, tmp_path: Path) -> None:
        from athenaeum.librarian import rebuild_index

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        rebuild_index(wiki)
        content = (wiki / "_index.md").read_text()
        assert "Total entities: 0" in content

    def test_skips_underscore_files(self, wiki_dir: Path) -> None:
        from athenaeum.librarian import rebuild_index

        # Add an underscore file that should not appear in index
        (wiki_dir / "_config.md").write_text("---\nname: Config\ntype: tool\n---\n")
        rebuild_index(wiki_dir)
        content = (wiki_dir / "_index.md").read_text()
        assert "Config" not in content


# ---------------------------------------------------------------------------
# run() integration — mocked LLM, real filesystem + git
# ---------------------------------------------------------------------------


class TestRunIntegration:
    """End-to-end integration test for the run() pipeline.

    Uses a real tmp_path-based knowledge root with a real git repo,
    but mocks anthropic.Anthropic at the module level so no HTTP calls
    are made and no API key is needed.
    """

    def _seed_knowledge_root(self, tmp_path: Path) -> Path:
        """Create a minimal knowledge/ tree with .git, wiki/_schema, raw/sessions."""
        root = tmp_path / "knowledge"
        root.mkdir()

        wiki = root / "wiki"
        (wiki / "_schema").mkdir(parents=True)
        (wiki / "_schema" / "types.md").write_text(
            "# Types\n\n| Type |\n|------|\n| person |\n"
        )
        (wiki / "_schema" / "tags.md").write_text(
            "# Tags\n\n| Tag |\n|-----|\n| active |\n"
        )
        (wiki / "_schema" / "access-levels.md").write_text(
            "# Access\n\n| Level |\n|-------|\n| internal |\n"
        )

        sessions = root / "raw" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / ".gitkeep").write_text("")

        subprocess.run(
            ["git", "init", "-q", "-b", "test-branch"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test Runner"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"],
            cwd=root,
            check=True,
        )

        # Drop the raw intake file post-commit so it is an uncommitted
        # change when run() takes its pre-processing snapshot.
        (sessions / "20240410T120000Z-aabbccdd.md").write_text(
            "Met with Alice Zhang about product strategy. "
            "She leads product at Acme Corp.\n"
        )
        return root

    def test_max_api_calls_stops_processing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issue #6: run() must stop processing when max_api_calls budget is exhausted."""
        import json
        import logging

        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)
        sessions = root / "raw" / "sessions"

        # Add a second raw file so there are 2 to process
        (sessions / "20240410T130000Z-11223344.md").write_text(
            "Discussed innovation accounting methodology in detail.\n"
        )

        # Mock client that returns valid classification + creation responses
        classify_response = MagicMock()
        classify_response.content = [
            MagicMock(
                text=json.dumps(
                    [
                        {
                            "name": "Alice Zhang",
                            "entity_type": "person",
                            "tags": ["active"],
                            "access": "internal",
                            "observations": "Product leader.",
                        }
                    ]
                )
            )
        ]
        create_response = MagicMock()
        create_response.content = [MagicMock(text="# Alice Zhang\n\nProduct leader.")]

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            classify_response,
            create_response,
            # If budget is working, the second file should NOT be processed
            # and these would never be called
        ]
        monkeypatch.setattr(
            anthropic_mod,
            "Anthropic",
            lambda **kwargs: mock_client,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        caplog.set_level(logging.DEBUG, logger="athenaeum")

        # Set max_api_calls=2 — processing first file uses ~2 calls (1 classify + 1 create)
        # so the second file should be skipped
        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=2,
        )

        assert any(
            "budget exhausted" in rec.message.lower()
            or "API call budget" in rec.message
            for rec in caplog.records
        ), "Expected budget exhaustion log message"

    def test_self_resolving_claim_flagged_before_reaching_classify(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Issue athenaeum#300 follow-up (athenaeum#304): a raw file embedding its own
        self-confirmation claim must reach Tier 2 classify with the
        deterministic warning prepended -- not the bare unflagged claim.
        """
        import json

        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)
        sessions = root / "raw" / "sessions"
        (sessions / "20240410T120000Z-aabbccdd.md").write_text(
            "Kromatic is the primary venture. "
            "Human-confirmed (Tristan, 2026-07-02).\n"
        )

        classify_response = MagicMock()
        classify_response.content = [MagicMock(text=json.dumps([]))]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = classify_response
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        run(raw_root=root / "raw", wiki_root=root / "wiki", knowledge_root=root)

        call_args = mock_client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "UNVERIFIED SELF-CLAIM" in user_msg
        assert "Human-confirmed (Tristan, 2026-07-02)." in user_msg
        # Warning precedes the claim in the actual prompt sent to the LLM.
        assert user_msg.index("UNVERIFIED SELF-CLAIM") < user_msg.index(
            "Human-confirmed"
        )

    def test_max_retries_passed_to_client(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Issue #6: Anthropic client must be created with max_retries=3."""
        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)

        captured_kwargs: dict = {}

        def mock_anthropic(**kwargs):
            captured_kwargs.update(kwargs)
            client = MagicMock()
            client.messages.create.return_value = MagicMock(
                content=[MagicMock(text="[]")]
            )
            return client

        monkeypatch.setattr(anthropic_mod, "Anthropic", mock_anthropic)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key")

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        assert captured_kwargs.get("max_retries") == 3

    def test_keeps_raw_on_llm_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When the LLM fails, raw files must be preserved for retry."""
        import logging

        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)
        raw_file = root / "raw" / "sessions" / "20240410T120000Z-aabbccdd.md"
        assert raw_file.exists(), "test setup: raw file not seeded"

        # Patch anthropic.Anthropic to return a client that always raises
        failing_client = MagicMock()
        failing_client.messages.create.side_effect = anthropic_mod.APIError(
            message="Simulated server error",
            request=MagicMock(),
            body=None,
        )
        monkeypatch.setattr(
            anthropic_mod,
            "Anthropic",
            lambda **kwargs: failing_client,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        caplog.set_level(logging.DEBUG, logger="athenaeum")

        exit_code = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        # Contract 1: raw intake preserved for retry on next run
        assert raw_file.exists(), (
            "raw file was deleted despite LLM failure -- must keep raw files "
            "when an LLM call fails so the next run can retry."
        )

        # Contract 2: logged the failure through outer exception handler
        assert any(
            "Failed to process" in rec.message for rec in caplog.records
        ), "run() did not log the failure via its outer exception handler"

        # Contract 3: no wiki entity pages created
        wiki_entities = [
            p
            for p in (root / "wiki").rglob("*.md")
            if "_schema" not in p.parts and not p.name.startswith("_")
        ]
        assert (
            wiki_entities == []
        ), f"Wiki pages were created despite LLM failure: {wiki_entities}"

        # Contract 4: pre-processing git snapshot ran
        git_log = subprocess.run(
            ["git", "log", "--format=%s"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        assert (
            "librarian: pre-processing snapshot" in git_log.stdout
        ), "pre-processing snapshot was not taken"

        # run() returns 1 when any files failed (partial failure)
        assert exit_code == 1

    def _write_oversized_entity(
        self, root: Path, uid: str = "bigbig01", *, fill: int = 20000
    ) -> str:
        """Drop an oversized (>flag) wiki entity page; return its filename."""
        name = f"{uid}-big-page.md"
        header = (
            "---\n"
            f"uid: {uid}\n"
            "type: person\n"
            "name: Big Page\n"
            "access: internal\n"
            "---\n\n"
            "# Big Page\n\n"
        )
        (root / "wiki" / name).write_text(header + "x" * fill)
        return name

    def test_flag_page_warns_nonfatally(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issue athenaeum#310 / athenaeum#490 (slice A): run() surfaces a flagged page in the
        single aggregated oversized-pages WARNING (non-fatal). With one page
        over the flag, exactly one such WARNING names it."""
        import logging

        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)
        page_name = self._write_oversized_entity(root)

        # Empty classification => trivial processing; run reaches the end block.
        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="[]")]
        )
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key")
        caplog.set_level(logging.WARNING, logger="athenaeum")

        exit_code = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        assert exit_code == 0
        flag_warnings = [
            rec
            for rec in caplog.records
            if "oversized wiki page" in rec.message and page_name in rec.message
        ]
        assert len(flag_warnings) == 1, (
            "expected exactly one oversized-page WARNING for the flagged page, "
            f"got {[r.message for r in caplog.records]}"
        )

    def test_multiple_oversized_pages_aggregate_to_single_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issue athenaeum#490 (slice A) / athenaeum#310: with several pages over the flag,
        run() emits exactly ONE aggregated WARNING carrying the count and
        every page name — not one line per page (which buried a ~35-page
        corpus's log)."""
        import logging

        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)
        page_a = self._write_oversized_entity(root, "aaaaaa01")
        page_b = self._write_oversized_entity(root, "bbbbbb02")
        page_c = self._write_oversized_entity(root, "cccccc03")

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="[]")]
        )
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key")
        caplog.set_level(logging.WARNING, logger="athenaeum")

        exit_code = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        assert exit_code == 0
        oversized_lines = [
            rec.message
            for rec in caplog.records
            if "oversized wiki page" in rec.message
        ]
        assert len(oversized_lines) == 1, (
            "expected a SINGLE aggregated oversized-pages WARNING, got "
            f"{oversized_lines}"
        )
        line = oversized_lines[0]
        assert "3 over flag" in line
        for page in (page_a, page_b, page_c):
            assert page in line

    def test_pending_merge_revalidation_advisor_warns_dry_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issue athenaeum#481: a run surfaces stale pending-merge proposals the current
        gate would retire with a WARNING naming the remedy — WITHOUT mutating
        the queue (the nightly advisor runs dry-run)."""
        import logging

        import anthropic as anthropic_mod

        from athenaeum.librarian import run
        from athenaeum.pending_merges import render_block

        root = self._seed_knowledge_root(tmp_path)
        over_cap = render_block(
            merge_target_name="merge-workflow-pattern",
            sources=[f"/k/src-{i}.md" for i in range(9)],  # over the cap of 5
            rationale="chained",
            draft_merged_body="draft",
            confidence=0.3,
            created_at="2026-06-20",
        )
        merges_path = root / "wiki" / "_pending_merges.md"
        merges_path.write_text(
            "# Pending Merges\n\n" + over_cap + "\n", encoding="utf-8"
        )
        before = merges_path.read_text(encoding="utf-8")

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="[]")]
        )
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key")
        caplog.set_level(logging.WARNING, logger="athenaeum")

        exit_code = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        assert exit_code == 0
        advisor = [
            rec.message
            for rec in caplog.records
            if "pending-merge queue" in rec.message
        ]
        assert len(advisor) == 1
        assert "revalidate --apply" in advisor[0]
        # Advisor is dry-run: the sidecar is untouched.
        assert merges_path.read_text(encoding="utf-8") == before

    def test_page_size_scan_error_is_swallowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issue athenaeum#310: a scan failure must never break a run (non-fatal degrade)."""
        import logging

        import anthropic as anthropic_mod

        import athenaeum.status as status_mod
        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="[]")]
        )
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key")

        def _boom(*args, **kwargs):
            raise RuntimeError("scan blew up")

        # run() does a function-local import of scan_page_sizes, so patching
        # the attribute on the module is picked up at call time.
        monkeypatch.setattr(status_mod, "scan_page_sizes", _boom)
        caplog.set_level(logging.WARNING, logger="athenaeum")

        exit_code = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        # The run completes (does not raise, returns 0) and logs the degrade.
        assert exit_code == 0
        assert any(
            "page-size guardrail check failed" in rec.message for rec in caplog.records
        ), "expected non-fatal degrade warning when the scan raises"


# ---------------------------------------------------------------------------
# Module docstring model defaults reference the constants, not literals (athenaeum#686)
# ---------------------------------------------------------------------------


def test_module_docstring_references_model_constants_not_literals() -> None:
    """athenaeum#686: the librarian env-var docstring documents the Tier-2/Tier-3 model
    defaults by pointing at the DEFAULT_*_MODEL constants (the authority) rather
    than restating their values, so it cannot silently drift again — which is
    how it came to document `claude-sonnet-4-6` while `tiers.DEFAULT_WRITE_MODEL`
    shipped `claude-sonnet-5`.
    """
    import athenaeum.librarian as lib

    doc = lib.__doc__ or ""
    assert "config.DEFAULT_CLASSIFY_MODEL" in doc
    assert "tiers.DEFAULT_WRITE_MODEL" in doc
    # The drifted literal (and its classify sibling) are gone from the docstring.
    assert "claude-sonnet-4-6" not in doc
    assert "claude-haiku-4-5-20251001" not in doc
