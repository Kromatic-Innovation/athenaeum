# SPDX-License-Identifier: Apache-2.0
"""Tests for the librarian-side hard-bounce mark (issue athenaeum#765).

Covers the acceptance criteria that span more than one module:

- ``TestTier0BounceMarkEligibility`` — the deterministic gate in
  ``librarian.tier0_bounce_mark`` declines (falls through) unless every
  required signal is present, mirroring ``tier0_handle_upsert``'s shape.
- ``TestNormalIntakePath`` — a hard-bounce fact submitted through the SAME
  ``remember()`` MCP call every other fact uses is recognized, with no new
  intake schema, ``type:`` field, or dedicated code path.
- ``TestProcessOneShortCircuits`` — ``process_one`` returns before ever
  touching the LLM client when the deterministic mark fires (mirrors
  ``tests/test_registry.py``'s ``_run_seed`` pattern for ``tier0_handle_upsert``).

Unit-level coverage of the detector/mark/read-back primitives themselves
(``detect_hard_bounce_fact`` / ``mark_bounced`` / ``is_bounced``) lives in
``tests/test_pii_off_corpus.py``.

All fixtures are synthetic — no client data lives in this public repo.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.librarian import process_one, tier0_bounce_mark
from athenaeum.mcp_server import remember_write
from athenaeum.models import EntityIndex, RawFile, parse_frontmatter
from athenaeum.pii import contacts_surface_root, default_bounce_record_path, read_bounce_record

EXCLUDED_CONFIG = {"storage": {"mapping": {"pii": "excluded"}}}


def _raw_from_content(raw_dir: Path, content: str) -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "note.md"
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source="voltaire-bounce-relay", timestamp="", uuid8="")


_HARD_BOUNCE_NOTE = (
    "---\nobserved_at: 2026-08-05\nsource: script:voltaire-bounce-relay\n---\n\n"
    "Alex's address alex@example.org hard-bounced. "
    "Diagnostic: 550 5.1.1 user unknown.\n"
)


class TestTier0BounceMarkEligibility:
    """Every required signal must be present, else ``None`` — falls through."""

    def test_recognizes_and_marks_conformant_note(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_from_content(tmp_path / "raw" / "voltaire-bounce-relay", _HARD_BOUNCE_NOTE)

        fact = tier0_bounce_mark(raw, wiki, config=EXCLUDED_CONFIG)

        assert fact is not None
        assert fact.identifier == "alex@example.org"
        contacts_root = contacts_surface_root(wiki.parent, EXCLUDED_CONFIG)
        record_path = default_bounce_record_path(contacts_root, "alex@example.org")
        assert record_path.exists()

    def test_missing_observed_at_falls_through(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        content = (
            "---\nsource: script:voltaire-bounce-relay\n---\n\n"
            "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
        )
        raw = _raw_from_content(tmp_path / "raw" / "voltaire-bounce-relay", content)
        assert tier0_bounce_mark(raw, wiki, config=EXCLUDED_CONFIG) is None

    def test_missing_source_falls_through(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        content = (
            "---\nobserved_at: 2026-08-05\n---\n\n"
            "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
        )
        raw = _raw_from_content(tmp_path / "raw" / "voltaire-bounce-relay", content)
        assert tier0_bounce_mark(raw, wiki, config=EXCLUDED_CONFIG) is None

    def test_transient_4xx_falls_through(self, tmp_path: Path) -> None:
        # voltaire#81's "potentially stale" case is out of scope — a 4.x
        # diagnostic must never be marked bounced.
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        content = (
            "---\nobserved_at: 2026-08-05\nsource: script:voltaire-bounce-relay\n---\n\n"
            "alex@example.org soft-bounced. Diagnostic: 421 4.4.62 routing issue.\n"
        )
        raw = _raw_from_content(tmp_path / "raw" / "voltaire-bounce-relay", content)
        assert tier0_bounce_mark(raw, wiki, config=EXCLUDED_CONFIG) is None
        contacts_root = contacts_surface_root(wiki.parent, EXCLUDED_CONFIG)
        assert not contacts_root.exists() or list(contacts_root.glob("*.md")) == []

    def test_ordinary_prose_note_falls_through(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        content = (
            "---\nobserved_at: 2026-08-05\nsource: manual\n---\n\n"
            "Acme just raised a Series B.\n"
        )
        raw = _raw_from_content(tmp_path / "raw" / "manual", content)
        assert tier0_bounce_mark(raw, wiki, config=EXCLUDED_CONFIG) is None

    def test_dry_run_detects_but_does_not_write(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_from_content(tmp_path / "raw" / "voltaire-bounce-relay", _HARD_BOUNCE_NOTE)

        fact = tier0_bounce_mark(raw, wiki, config=EXCLUDED_CONFIG, dry_run=True)

        assert fact is not None
        contacts_root = contacts_surface_root(wiki.parent, EXCLUDED_CONFIG)
        assert not contacts_root.exists()


class TestNormalIntakePath:
    """A bounce fact rides the SAME ``remember()`` call every other fact uses.

    Issue athenaeum#765's acceptance criterion: "Assert in a test that no new
    intake schema, ``type:`` field, or dedicated code path is required to
    trigger it." These tests build the raw file via the real
    ``remember_write()`` MCP entry point — not a hand-crafted fixture — and
    confirm recognition fires with no ``type:`` key anywhere in the raw
    frontmatter.
    """

    def test_remember_call_carries_no_type_field(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        raw_root.mkdir()
        content = (
            "---\nobserved_at: 2026-08-05\n---\n\n"
            "Alex's address alex@example.org hard-bounced. "
            "Diagnostic: 550 5.1.1 user unknown."
        )
        remember_write(
            raw_root,
            content,
            source="voltaire-bounce-relay",
            sources="script:voltaire-bounce-relay",
        )
        files = list((raw_root / "voltaire-bounce-relay").glob("*.md"))
        assert len(files) == 1
        meta, _ = parse_frontmatter(files[0].read_text(encoding="utf-8"))
        assert "type" not in meta  # no dedicated schema/type field was needed

    def test_remember_written_note_is_recognized_and_marked(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        raw_root.mkdir()
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        content = (
            "---\nobserved_at: 2026-08-05\n---\n\n"
            "Alex's address alex@example.org hard-bounced. "
            "Diagnostic: 550 5.1.1 user unknown."
        )
        remember_write(
            raw_root,
            content,
            source="voltaire-bounce-relay",
            sources="script:voltaire-bounce-relay",
        )
        files = list((raw_root / "voltaire-bounce-relay").glob("*.md"))
        raw = RawFile(path=files[0], source="voltaire-bounce-relay", timestamp="", uuid8="")

        fact = tier0_bounce_mark(raw, wiki, config=EXCLUDED_CONFIG)

        assert fact is not None
        assert fact.identifier == "alex@example.org"
        contacts_root = contacts_surface_root(wiki.parent, EXCLUDED_CONFIG)
        record_path = default_bounce_record_path(contacts_root, "alex@example.org")
        meta = read_bounce_record(record_path)
        assert meta["bounce_diagnostic"]
        assert meta["observed_at"] == "2026-08-05"
        assert meta["source"] == "script:voltaire-bounce-relay"


class TestProcessOneShortCircuits:
    """``process_one`` applies the mark and returns before the LLM tiers run."""

    def test_llm_client_never_called(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_from_content(tmp_path / "raw" / "voltaire-bounce-relay", _HARD_BOUNCE_NOTE)

        client = MagicMock()
        client.messages.create.side_effect = AssertionError(
            "LLM tiers must not run for a deterministically-recognized hard bounce (athenaeum#765)"
        )
        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            client,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            config=EXCLUDED_CONFIG,
        )
        client.messages.create.assert_not_called()
        assert not result.created
        assert not result.updated
        assert not result.escalated

        contacts_root = contacts_surface_root(wiki.parent, EXCLUDED_CONFIG)
        record_path = default_bounce_record_path(contacts_root, "alex@example.org")
        assert record_path.exists()

    def test_wiki_untouched_by_the_mark(self, tmp_path: Path) -> None:
        # The mark lands on the excluded contacts surface, never on wiki/.
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw_from_content(tmp_path / "raw" / "voltaire-bounce-relay", _HARD_BOUNCE_NOTE)

        client = MagicMock()
        client.messages.create.side_effect = AssertionError("must not reach the LLM tiers")
        process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            client,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            config=EXCLUDED_CONFIG,
        )
        assert list(wiki.glob("*.md")) == []
