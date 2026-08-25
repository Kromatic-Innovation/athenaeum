# SPDX-License-Identifier: Apache-2.0
"""Tests for the librarian-side do-not-email mark (issue athenaeum#1121).

The librarian compiled a do-not-email intake statement ("Do not email
`<address>`") into prose in the page BODY rather than the structured
``do_not_email: true`` frontmatter field that
:func:`athenaeum.pii.do_not_email_state` (the sole structured consumer)
reads. Frontmatter is schema-driven and never LLM-authored, so the fix has
to be a deterministic Tier-0 step, mirroring ``tier0_handle_upsert`` /
``tier0_bounce_mark``'s shape — never a prompt change (a prompt is not
deterministically testable).

- ``TestDetectDoNotEmailFact`` — the recognizer in ``athenaeum.pii``.
- ``TestTier0DoNotEmailMarkEligibility`` — the deterministic gate declines
  (falls through) unless every required signal is present.
- ``TestUidPinning`` — a raw statement that pins an explicit ``uid`` targets
  that EXACT page, never the address-named page a bare name/alias lookup
  would find; an unresolvable pin fails loudly rather than silently
  degrading to body prose.
- ``TestUpdatePathFixture`` — the AC2 fixture: reproduces the real shape (an
  EXISTING page with no ``do_not_email`` key, plus a fresh statement of the
  maecenas opt-out migration's exact form) and asserts the field appears in
  the resulting frontmatter. This is the demonstrated route for the pages
  already sitting in ``raw/maecenas-opt-out-migration/`` — NOT recompilation
  of already-consumed intake, which does not exist (raw files are unlinked
  after processing).
- ``TestProcessOneShortCircuits`` — ``process_one`` applies the mark and
  returns before the LLM tiers ever run.

All fixtures are synthetic — no client data, no real addresses or names,
lives in this public repo.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.librarian import process_one, tier0_do_not_email_mark
from athenaeum.models import EntityIndex, RawFile, parse_frontmatter
from athenaeum.pii import detect_do_not_email_fact


def _raw(raw_dir: Path, content: str, filename: str = "note.md") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=raw_dir.name, timestamp="", uuid8="")


def _existing_page(
    wiki: Path,
    filename: str,
    *,
    uid: str,
    name: str,
    entity_type: str = "person",
    extra_fm: str = "",
) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    page = wiki / filename
    page.write_text(
        f"---\nuid: {uid}\ntype: {entity_type}\nname: {name}\n"
        f"memory_class: entity\naccess: personal\n"
        f"created: '2026-08-20'\nupdated: '2026-08-20'\n{extra_fm}---\n\n"
        f"# {name}\n\nBody.\n",
        encoding="utf-8",
    )
    return page


class TestDetectDoNotEmailFact:
    """Unit-level coverage of the recognizer itself."""

    def test_direct_instruction_recognized(self) -> None:
        fact = detect_do_not_email_fact(
            "Do not email someone@example.com. Reported by an operator."
        )
        assert fact is not None
        assert fact.identifier == "someone@example.com"
        assert "Do not email someone@example.com." == fact.reason

    def test_reported_optout_recognized(self) -> None:
        fact = detect_do_not_email_fact(
            "Sam asked to stop receiving email. Their address is sam@example.com."
        )
        assert fact is not None
        assert fact.identifier == "sam@example.com"

    def test_bounce_report_declines(self) -> None:
        # A 5.x.x diagnostic is deliverability, never consent (maecenas#95's
        # operator ruling) — tier0_bounce_mark owns this shape exclusively.
        assert (
            detect_do_not_email_fact(
                "someone@example.com hard-bounced. Diagnostic: 550 5.1.1 user unknown."
            )
            is None
        )

    def test_optout_list_mention_without_assertion_declines(self) -> None:
        # Merely mentioning an opt-out list, with no email in the text at
        # all, must not fire.
        assert (
            detect_do_not_email_fact(
                "The opt-out list at rsb_campaign/exclude.json tracks several addresses."
            )
            is None
        )

    def test_multiple_addresses_declines(self) -> None:
        assert (
            detect_do_not_email_fact("Do not email a@example.com or b@example.com.")
            is None
        )

    def test_ordinary_prose_declines(self) -> None:
        assert detect_do_not_email_fact("Acme just raised a Series B.") is None

    def test_reason_does_not_truncate_at_domain_period(self) -> None:
        # A domain period ("example.com") must not be mistaken for a
        # sentence boundary when extracting the reason text.
        fact = detect_do_not_email_fact(
            "Do not email hello@example.com. This person opted out."
        )
        assert fact is not None
        assert fact.reason == "Do not email hello@example.com."


class TestTier0DoNotEmailMarkEligibility:
    """Every required signal must be present, else ``None`` — falls through."""

    def test_stamps_field_onto_existing_page(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        raw = _raw(
            tmp_path / "raw" / "producer",
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "Do not email someone@example.com. Operator-directed opt-out.\n",
        )
        out = tier0_do_not_email_mark(raw, EntityIndex(wiki), wiki)
        assert out is not None
        entity, changed = out
        assert changed is True
        assert entity.uid == "aaaa1111"

    def test_no_matching_email_declines(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(
            tmp_path / "raw" / "producer",
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\nAcme raised a Series B.\n",
        )
        assert tier0_do_not_email_mark(raw, EntityIndex(wiki), wiki) is None

    def test_unmatched_address_declines_and_falls_through(self, tmp_path: Path) -> None:
        # No existing page for this address — this deterministic path only
        # upserts onto an EXISTING page; a brand-new address is left to the
        # LLM tiers (it does not create pages).
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(
            tmp_path / "raw" / "producer",
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "Do not email nobody@example.com.\n",
        )
        assert tier0_do_not_email_mark(raw, EntityIndex(wiki), wiki) is None

    def test_bounce_report_falls_through_untouched(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        before = page.read_text(encoding="utf-8")
        raw = _raw(
            tmp_path / "raw" / "producer",
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "someone@example.com hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n",
        )
        assert tier0_do_not_email_mark(raw, EntityIndex(wiki), wiki) is None
        assert page.read_text(encoding="utf-8") == before

    def test_reseed_already_marked_is_noop(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
            extra_fm=(
                "do_not_email: true\ndo_not_email_reason: prior reason\n"
                "do_not_email_date: '2026-08-01'\n"
            ),
        )
        before = page.read_text(encoding="utf-8")
        raw = _raw(
            tmp_path / "raw" / "producer",
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "Do not email someone@example.com. New re-report.\n",
        )
        out = tier0_do_not_email_mark(raw, EntityIndex(wiki), wiki)
        assert out is not None
        _, changed = out
        assert changed is False
        # Byte-for-byte stable — first-write provenance is never clobbered.
        assert page.read_text(encoding="utf-8") == before

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        before = page.read_text(encoding="utf-8")
        raw = _raw(
            tmp_path / "raw" / "producer",
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "Do not email someone@example.com.\n",
        )
        out = tier0_do_not_email_mark(raw, EntityIndex(wiki), wiki, dry_run=True)
        assert out is not None
        assert out[1] is True
        assert page.read_text(encoding="utf-8") == before

    def test_never_writes_to_a_non_wiki_path(self, tmp_path: Path) -> None:
        # Hard constraint: the wiki page is the sole authoring surface. This
        # function takes no contacts-root/excluded-surface argument at all,
        # so there is no path by which it could write there.
        wiki = tmp_path / "wiki"
        _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        raw = _raw(
            tmp_path / "raw" / "producer",
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "Do not email someone@example.com.\n",
        )
        tier0_do_not_email_mark(raw, EntityIndex(wiki), wiki)
        assert list(wiki.glob("*.md")) == [wiki / "aaaa1111-someone-example-com.md"]
        # No sibling contacts/excluded directory was created by this call.
        assert not (wiki.parent / "excluded").exists()


class TestUidPinning:
    """A raw statement that pins ``uid:`` targets that EXACT page (issue
    athenaeum#1121 follow-up) — never the address-named page a bare
    name/alias lookup on the statement's email would find instead.
    """

    def test_pinned_uid_targets_the_pinned_page_not_the_address_page(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        # The entity the address actually resolves to (a named-person page).
        target = _existing_page(
            wiki, "bbbb2222-fixture-person.md", uid="bbbb2222", name="Fixture Person"
        )
        # An unrelated address-named page a name/alias lookup on the
        # statement's own email would otherwise find.
        addr_page = _existing_page(
            wiki,
            "cccc3333-fixture-example-com.md",
            uid="cccc3333",
            name="fixture@example.com",
        )

        raw = _raw(
            tmp_path / "raw" / "producer",
            "---\nuid: bbbb2222\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "Do not email fixture@example.com; Fixture Person asked to stop receiving email.\n",
        )
        out = tier0_do_not_email_mark(raw, EntityIndex(wiki), wiki)
        assert out is not None
        entity, changed = out
        assert changed is True
        assert entity.uid == "bbbb2222"

        target_meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert target_meta.get("do_not_email") is True
        addr_meta, _ = parse_frontmatter(addr_page.read_text(encoding="utf-8"))
        assert "do_not_email" not in addr_meta

    def test_pinned_uid_that_does_not_resolve_declines_loudly(
        self, tmp_path: Path, caplog
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        # Even though the address WOULD resolve by name, a bad uid pin
        # must decline rather than silently falling back to name lookup —
        # that fallback is exactly the silent-degrade-to-prose defect this
        # issue exists to close.
        addr_page = _existing_page(
            wiki,
            "cccc3333-fixture-example-com.md",
            uid="cccc3333",
            name="fixture@example.com",
        )
        before = addr_page.read_text(encoding="utf-8")

        raw = _raw(
            tmp_path / "raw" / "producer",
            "---\nuid: does-not-exist\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "Do not email fixture@example.com.\n",
        )
        import logging

        with caplog.at_level(logging.WARNING):
            out = tier0_do_not_email_mark(raw, EntityIndex(wiki), wiki)
        assert out is None
        assert addr_page.read_text(encoding="utf-8") == before
        assert any("does-not-exist" in record.message for record in caplog.records)


class TestUpdatePathFixture:
    """The AC2 fixture: reproduces the real shape and demonstrates the
    working route.

    Recompilation of already-consumed raw intake does NOT exist — raw files
    are unlinked after successful processing, and ``compile_as_of`` re-derives
    from the current cluster JSONL, not raw intake, so it cannot reprocess a
    statement already applied. The route this fixture demonstrates instead is
    the one that actually applies to the pages sitting in
    ``raw/maecenas-opt-out-migration/`` right now: those addresses' wiki
    pages already exist, and a FRESH statement of the migration's exact
    shape (same ``source:`` provenance, same "Do not email `<address>`."
    wording) compiling through this tier-0 step takes the UPDATE path and
    stamps the field — no recompile needed, no hand-edit needed.
    """

    def test_fresh_migration_shaped_statement_stamps_existing_page(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        # An existing page with NO do_not_email key — the exact shape of the
        # pages the maecenas opt-out migration already created.
        page = _existing_page(
            wiki,
            "dddd4444-fixture-optout-example.md",
            uid="dddd4444",
            name="fixture-optout@example.com",
            extra_fm="tags:\n- blocked\n",
        )
        assert "do_not_email" not in page.read_text(encoding="utf-8")

        # A fresh statement carrying the migration's own provenance shape
        # (source: agent-observed:maecenas:migrate_exclude_opt_outs) and the
        # migration's exact wording — a new duplicate submission of the kind
        # maecenas#166 describes continuing to arrive.
        raw = _raw(
            tmp_path / "raw" / "maecenas-opt-out-migration",
            "---\nobserved_at: 2026-08-25\n"
            "source: agent-observed:maecenas:migrate_exclude_opt_outs\n---\n\n"
            "Do not email fixture-optout@example.com. This person is on the RSB "
            "campaign's manual opt-out list (rsb_campaign/exclude.json), which is "
            "a human-maintained list of people who asked not to be contacted or "
            "who an operator determined must not be contacted.\n\n"
            "Reported by maecenas/rsb_campaign/migrate_exclude_opt_outs.py so the "
            "opt-out survives deletion of that file. The list is currently the "
            "ONLY record of this person's opt-out.\n",
            filename="20260825T010715Z-fixture0001.md",
        )

        out = tier0_do_not_email_mark(raw, EntityIndex(wiki), wiki)
        assert out is not None
        entity, changed = out
        assert changed is True
        assert entity.uid == "dddd4444"

        after_meta, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
        assert after_meta.get("do_not_email") is True
        assert after_meta.get("do_not_email_reason", "").startswith(
            "Do not email fixture-optout@example.com."
        )
        assert after_meta.get("do_not_email_date") == "2026-08-25"
        # Pre-existing frontmatter (tags, uid, type, name) is preserved.
        assert after_meta.get("tags") == ["blocked"]
        assert after_meta.get("uid") == "dddd4444"


class TestProcessOneShortCircuits:
    """``process_one`` applies the mark and returns before the LLM tiers run."""

    def test_llm_client_never_called(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        raw = _raw(
            tmp_path / "raw" / "producer",
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "Do not email someone@example.com.\n",
        )

        client = MagicMock()
        client.messages.create.side_effect = AssertionError(
            "LLM tiers must not run for a deterministically-recognized "
            "do-not-email statement (athenaeum#1121)"
        )
        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            client,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
        )
        client.messages.create.assert_not_called()
        assert not result.created
        assert result.updated == ["aaaa1111"]
        assert not result.escalated

        meta, _ = parse_frontmatter(
            (wiki / "aaaa1111-someone-example-com.md").read_text(encoding="utf-8")
        )
        assert meta.get("do_not_email") is True
