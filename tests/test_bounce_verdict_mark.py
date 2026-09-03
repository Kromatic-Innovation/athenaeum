# SPDX-License-Identifier: Apache-2.0
"""Tests for the librarian-side non-RFC bounce-verdict mark (issue athenaeum#1341).

athenaeum#1341 is a narrow, scoped reversal of athenaeum#852's read-only stance on
the wiki ``bounced:`` frontmatter field: a bare SMTP 550-559 reply (or an
allowlisted verified list-verification verdict token) previously had no
structured home at all and compiled as ordinary LLM-reasoned prose. This
module covers:

- ``TestDetectBounceVerdictFact`` — the recognizer in ``athenaeum.pii``.
- ``TestTier0BounceVerdictMarkEligibility`` — the deterministic gate in
  ``librarian.tier0_bounce_verdict_mark`` declines (falls through) unless
  every required signal is present, mirroring ``tier0_do_not_email_mark``'s
  shape.
- ``TestDispatchOrder`` — a note carrying an RFC 3463 ``5.x.x`` code is
  claimed by ``tier0_bounce_mark`` first, never by this branch — both via
  dispatch order in ``process_one`` AND via the standalone conformance check.
- ``TestPiiSurfaceUntouched`` — this function never writes to the PII/
  contacts surface, only the wiki page's ``bounced:`` field.
- ``TestProcessOneShortCircuits`` — ``process_one`` applies the mark and
  returns before the LLM tiers ever run.

All fixtures are synthetic — no client data, no real addresses or names,
lives in this public repo.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.bounce_contract import (
    FRONTMATTER_NOT_A_MAPPING,
    MISSING_VERDICT_SIGNAL,
    NO_EMAIL_IDENTIFIER,
    RFC_CODE_ALREADY_PRESENT,
    SEVERAL_EMAIL_IDENTIFIERS,
    UNSUPPORTED_SOURCE_TYPE,
    check_tier0_bounce_verdict_conformance,
)
from athenaeum.librarian import (
    process_one,
    tier0_bounce_mark,
    tier0_bounce_verdict_mark,
)
from athenaeum.models import EntityIndex, RawFile, parse_frontmatter
from athenaeum.pii import contacts_surface_root, detect_bounce_verdict_fact

EXCLUDED_CONFIG = {"storage": {"mapping": {"pii": "excluded"}}}


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


_BARE_550_NOTE = (
    "---\nobserved_at: 2026-08-25\nsource: script:voltaire-bounce-relay\n---\n\n"
    "Email bounce observed: someone@example.com — hard bounce on 2026-08-25, "
    "reported by voltaire from its triage path.\n\n"
    "Delivery diagnostic, verbatim: 550 user unknown\n"
)

_BARE_552_NOTE = (
    "---\nobserved_at: 2026-08-25\nsource: script:voltaire-bounce-relay\n---\n\n"
    "Email bounce observed: someone@example.com — hard bounce on 2026-08-25, "
    "reported by voltaire from its triage path.\n\n"
    "Delivery diagnostic, verbatim: 552 mailbox full\n"
)

_VERDICT_TOKEN_NOTE = (
    "---\nobserved_at: 2026-08-25\nsource: script:voltaire-bounce-relay\n---\n\n"
    "Email bounce observed: someone@example.com — hard bounce on 2026-08-25.\n\n"
    "Delivery diagnostic, verbatim: MailboxDoesNotExist\n"
)


class TestDetectBounceVerdictFact:
    """Unit-level coverage of the recognizer itself."""

    def test_bare_550_recognized(self) -> None:
        fact = detect_bounce_verdict_fact(
            "someone@example.com: delivery failed. Diagnostic: 550 user unknown."
        )
        assert fact is not None
        assert fact.identifier == "someone@example.com"
        assert fact.verdict == "550"

    def test_bare_552_recognized(self) -> None:
        fact = detect_bounce_verdict_fact(
            "someone@example.com: delivery failed. Diagnostic: 552 mailbox full."
        )
        assert fact is not None
        assert fact.verdict == "552"

    def test_allowlisted_verdict_token_recognized(self) -> None:
        fact = detect_bounce_verdict_fact(
            "someone@example.com: list-verification verdict MailboxDoesNotExist."
        )
        assert fact is not None
        assert fact.identifier == "someone@example.com"
        assert fact.verdict == "MailboxDoesNotExist"

    def test_domain_does_not_exist_token_recognized(self) -> None:
        fact = detect_bounce_verdict_fact(
            "someone@example.com: list-verification verdict DomainDoesNotExist."
        )
        assert fact is not None
        assert fact.verdict == "DomainDoesNotExist"

    def test_domain_has_null_mx_token_recognized(self) -> None:
        fact = detect_bounce_verdict_fact(
            "someone@example.com: list-verification verdict DomainHasNullMx."
        )
        assert fact is not None
        assert fact.verdict == "DomainHasNullMx"

    def test_rfc_conforming_note_declines(self) -> None:
        # This shape belongs to detect_hard_bounce_fact exclusively.
        assert (
            detect_bounce_verdict_fact(
                "someone@example.com hard-bounced. Diagnostic: 550 5.1.1 user unknown."
            )
            is None
        )

    def test_smtp_connection_timeout_declines(self) -> None:
        # Transient/connectivity, not a verified permanent failure — must
        # never gain any bounce mark of any kind.
        assert (
            detect_bounce_verdict_fact(
                "someone@example.com: verdict SmtpConnectionTimeout."
            )
            is None
        )

    def test_ordinary_prose_declines(self) -> None:
        assert detect_bounce_verdict_fact("Acme just raised a Series B.") is None

    def test_multiple_addresses_declines(self) -> None:
        assert (
            detect_bounce_verdict_fact(
                "a@example.com and b@example.com both got 550 user unknown."
            )
            is None
        )

    def test_no_verdict_signal_declines(self) -> None:
        assert (
            detect_bounce_verdict_fact("someone@example.com: unspecified failure.")
            is None
        )

    # -- bare-code false positives (issue athenaeum#1341 QA must-fix) -------
    #
    # A bare 550-559 number with zero SMTP/diagnostic context must never
    # match, even with a lone email-shaped token nearby — that combination
    # would otherwise write a bogus `bounced:` mark onto a real wiki page.

    def test_dollar_amount_near_email_declines(self) -> None:
        assert (
            detect_bounce_verdict_fact(
                "We closed a $550 round with someone@example.com cc'd on the "
                "announcement."
            )
            is None
        )

    def test_ticket_number_near_email_declines(self) -> None:
        assert (
            detect_bounce_verdict_fact(
                "Please reference ticket #552 when you email someone@example.com."
            )
            is None
        )

    def test_booth_number_near_email_declines(self) -> None:
        assert (
            detect_bounce_verdict_fact(
                "Meet us at booth 550 — email someone@example.com to schedule."
            )
            is None
        )

    def test_invoice_number_near_email_declines(self) -> None:
        assert (
            detect_bounce_verdict_fact(
                "See invoice #559 for details; contact someone@example.com with "
                "questions."
            )
            is None
        )

    def test_voltaire_shaped_bare_code_note_matches(self) -> None:
        # The actual buildBounceIntakeBody framing (voltaire
        # src/tiers/bounce-intake.ts): a "Delivery diagnostic, verbatim:"
        # intro line ahead of the bare code. Confirms the false-positive
        # fix above did not also swallow the legitimate case.
        note = (
            "Email bounce observed: someone@example.com — hard bounce on "
            "2026-08-25, reported by voltaire from its triage path.\n\n"
            "Delivery diagnostic, verbatim: 550 No such user here\n"
        )
        fact = detect_bounce_verdict_fact(note)
        assert fact is not None
        assert fact.identifier == "someone@example.com"
        assert fact.verdict == "550"


class TestTier0BounceVerdictConformance:
    """The published contract check, mirroring TestDetectBounceVerdictFact
    at the frontmatter+body level."""

    def test_bare_550_conforms(self) -> None:
        check = check_tier0_bounce_verdict_conformance(_BARE_550_NOTE)
        assert check.conforms is True
        assert check.identifier == "someone@example.com"

    def test_rfc_code_present_declines_with_reason(self) -> None:
        note = (
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "someone@example.com hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
        )
        check = check_tier0_bounce_verdict_conformance(note)
        assert check.conforms is False
        assert RFC_CODE_ALREADY_PRESENT in check.reasons

    def test_transient_declines_with_missing_verdict_signal(self) -> None:
        note = (
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "someone@example.com: verdict SmtpConnectionTimeout.\n"
        )
        check = check_tier0_bounce_verdict_conformance(note)
        assert check.conforms is False
        assert MISSING_VERDICT_SIGNAL in check.reasons

    # -- decline reasons not otherwise covered at the contract level --------
    # (issue athenaeum#1341 QA should-fix 2). check_tier0_bounce_verdict_
    # conformance re-implements its own email-counting logic rather than
    # delegating to detect_bounce_verdict_fact, so it can drift independently
    # — worth a dedicated test per reason code.

    def test_no_email_identifier_declines_with_reason(self) -> None:
        note = (
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "The address hard-bounced. Delivery diagnostic, verbatim: 550 "
            "user unknown.\n"
        )
        check = check_tier0_bounce_verdict_conformance(note)
        assert check.conforms is False
        assert NO_EMAIL_IDENTIFIER in check.reasons

    def test_several_email_identifiers_declines_with_reason(self) -> None:
        note = (
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "a@example.com and b@example.com both got a Delivery diagnostic, "
            "verbatim: 550 user unknown.\n"
        )
        check = check_tier0_bounce_verdict_conformance(note)
        assert check.conforms is False
        assert SEVERAL_EMAIL_IDENTIFIERS in check.reasons

    def test_frontmatter_not_a_mapping_declines_with_reason(self) -> None:
        note = (
            "---\n- observed_at\n- source\n---\n\n"
            "someone@example.com: Delivery diagnostic, verbatim: 550 user "
            "unknown.\n"
        )
        check = check_tier0_bounce_verdict_conformance(note)
        assert check.conforms is False
        assert FRONTMATTER_NOT_A_MAPPING in check.reasons

    def test_unsupported_source_type_declines_with_reason(self) -> None:
        note = (
            "---\nobserved_at: 2026-08-25\nsource:\n  - one\n  - two\n---\n\n"
            "someone@example.com: Delivery diagnostic, verbatim: 550 user "
            "unknown.\n"
        )
        check = check_tier0_bounce_verdict_conformance(note)
        assert check.conforms is False
        assert UNSUPPORTED_SOURCE_TYPE in check.reasons


class TestTier0BounceVerdictMarkEligibility:
    """Every required signal must be present, else ``None`` — falls through."""

    def test_bare_550_stamps_field_onto_existing_page(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", _BARE_550_NOTE)

        out = tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki)
        assert out is not None
        entity, changed = out
        assert changed is True
        assert entity.uid == "aaaa1111"

        after_meta, _ = parse_frontmatter(
            (wiki / "aaaa1111-someone-example-com.md").read_text(encoding="utf-8")
        )
        assert "550" in after_meta.get("bounced", "")

    def test_bare_552_stamps_field(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", _BARE_552_NOTE)
        out = tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki)
        assert out is not None
        assert out[1] is True

    def test_allowlisted_verdict_token_stamps_field(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", _VERDICT_TOKEN_NOTE)
        out = tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki)
        assert out is not None
        entity, changed = out
        assert changed is True
        after_meta, _ = parse_frontmatter(
            (wiki / "aaaa1111-someone-example-com.md").read_text(encoding="utf-8")
        )
        assert "MailboxDoesNotExist" in after_meta.get("bounced", "")

    def test_transient_smtp_connection_timeout_falls_through_untouched(
        self, tmp_path: Path
    ) -> None:
        # Transient signals must never get any bounce mark, on either surface.
        wiki = tmp_path / "wiki"
        page = _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        before = page.read_text(encoding="utf-8")
        note = (
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "someone@example.com: verdict SmtpConnectionTimeout.\n"
        )
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", note)
        assert tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki) is None
        assert page.read_text(encoding="utf-8") == before

    def test_missing_observed_at_falls_through(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        note = (
            "---\nsource: test\n---\n\n"
            "someone@example.com: delivery failed. Diagnostic: 550 user unknown.\n"
        )
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", note)
        assert tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki) is None

    def test_missing_source_falls_through(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        note = (
            "---\nobserved_at: 2026-08-25\n---\n\n"
            "someone@example.com: delivery failed. Diagnostic: 550 user unknown.\n"
        )
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", note)
        assert tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki) is None

    def test_ordinary_prose_falls_through(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        note = "---\nobserved_at: 2026-08-25\nsource: test\n---\n\nAcme raised a Series B.\n"
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", note)
        assert tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki) is None

    def test_unmatched_address_declines_and_falls_through(self, tmp_path: Path) -> None:
        # No existing page for this address — this deterministic path only
        # upserts onto an EXISTING page; a brand-new address is left to the
        # LLM tiers (it does not create pages).
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", _BARE_550_NOTE)
        assert tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki) is None

    def test_reseed_already_verdicted_is_noop(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
            extra_fm="bounced: MailboxDoesNotExist\n",
        )
        before = page.read_text(encoding="utf-8")
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", _BARE_550_NOTE)
        out = tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki)
        assert out is not None
        _, changed = out
        assert changed is False
        # Byte-for-byte stable — existing evidence, from any producer, is
        # never overwritten.
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
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", _BARE_550_NOTE)
        out = tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki, dry_run=True)
        assert out is not None
        assert out[1] is True
        assert page.read_text(encoding="utf-8") == before


class TestDispatchOrder:
    """An RFC-conforming note is claimed by ``tier0_bounce_mark`` first,
    never by this branch — both structurally (this function declines it
    directly) and end-to-end (``process_one`` dispatches bounce_mark first)."""

    def test_standalone_check_declines_an_rfc_conforming_note(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        note = (
            "---\nobserved_at: 2026-08-25\nsource: script:voltaire-bounce-relay\n---\n\n"
            "someone@example.com hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
        )
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", note)

        # tier0_bounce_mark claims it (writes to the PII surface)...
        pii_fact = tier0_bounce_mark(raw, wiki, config=EXCLUDED_CONFIG)
        assert pii_fact is not None

        # ...and this branch, called directly on the SAME note, declines it
        # rather than also claiming it — correct in isolation, not merely by
        # dispatch order.
        wiki_upsert = tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki)
        assert wiki_upsert is None

    def test_process_one_claims_rfc_conforming_note_via_pii_surface_only(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        page = _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        before = page.read_text(encoding="utf-8")
        note = (
            "---\nobserved_at: 2026-08-25\nsource: script:voltaire-bounce-relay\n---\n\n"
            "someone@example.com hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
        )
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", note)

        client = MagicMock()
        client.messages.create.side_effect = AssertionError(
            "must not reach the LLM tiers"
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
        # The wiki page is untouched — the RFC-conforming note landed on the
        # PII surface via tier0_bounce_mark, not on bounced: via this branch.
        assert page.read_text(encoding="utf-8") == before
        assert not result.updated


class TestPiiSurfaceUntouched:
    """Hard constraint: the wiki page is the sole authoring surface for this
    branch — never the PII/excluded contacts surface."""

    def test_never_writes_to_a_non_wiki_path(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _existing_page(
            wiki,
            "aaaa1111-someone-example-com.md",
            uid="aaaa1111",
            name="someone@example.com",
        )
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", _BARE_550_NOTE)
        tier0_bounce_verdict_mark(raw, EntityIndex(wiki), wiki)
        assert list(wiki.glob("*.md")) == [wiki / "aaaa1111-someone-example-com.md"]
        assert not (wiki.parent / "excluded").exists()
        contacts_root = contacts_surface_root(wiki.parent, EXCLUDED_CONFIG)
        assert not contacts_root.exists()


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
        raw = _raw(tmp_path / "raw" / "voltaire-bounce-relay", _BARE_550_NOTE)

        client = MagicMock()
        client.messages.create.side_effect = AssertionError(
            "LLM tiers must not run for a deterministically-recognized "
            "bounce verdict (athenaeum#1341)"
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
        assert "550" in meta.get("bounced", "")
