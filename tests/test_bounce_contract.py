# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the Tier-0 bounce-note conformance check (issue athenaeum#854).

The point of these tests is not that the check works in isolation — it is that
the published contract, the check, and the shipped gate all say the same thing:

- ``TestConformingNote`` / ``TestSingleConditionDeclines`` — the check's own
  verdict, one unmet condition at a time, so a producer's failure report is
  actionable rather than a bare boolean.
- ``TestGateAgreement`` — the check's verdict matches what
  ``librarian.tier0_bounce_mark`` actually does, over the whole fixture
  matrix, asserted against the REAL gate run in ``dry_run`` (which writes
  nothing) rather than against a reimplementation of it. This is the seam that
  would catch someone re-inlining the eligibility logic into the gate.
- ``TestContractDocumentAgreement`` — ``docs/extending/tier0-bounce-note-contract.md``
  documents exactly the decline reasons the code can emit, no more and no
  fewer, so the prose cannot fall behind.
- ``TestCli`` — the ``athenaeum bounce-contract`` surface a producer in
  another language actually calls, including its exit codes.

Every fixture is synthetic: ``example.org`` / ``example.com`` addresses and
invented diagnostics. No real address and no diagnostic copied from any live
store appears here — this repository is public.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from athenaeum._cmd_bounce_contract import EXIT_NONCONFORMING
from athenaeum.bounce_contract import (
    DECLINE_REASONS,
    FRONTMATTER_NOT_A_MAPPING,
    MISSING_HARD_BOUNCE_CODE,
    MISSING_OBSERVED_AT,
    MISSING_SOURCE,
    NO_EMAIL_IDENTIFIER,
    SEVERAL_EMAIL_IDENTIFIERS,
    UNSUPPORTED_SOURCE_TYPE,
    WHERE_BODY,
    WHERE_FRONTMATTER,
    check_tier0_bounce_conformance,
)
from athenaeum.cli import main as cli_main
from athenaeum.librarian import tier0_bounce_mark
from athenaeum.models import RawFile

CONTRACT_DOC = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "extending"
    / "tier0-bounce-note-contract.md"
)

CONFORMING = (
    "---\nobserved_at: 2026-08-05\nsource: script:bounce-relay\n---\n\n"
    "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
)

NO_OBSERVED_AT = (
    "---\nsource: script:bounce-relay\n---\n\n"
    "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
)

NO_SOURCE = (
    "---\nobserved_at: 2026-08-05\n---\n\n"
    "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
)

BLANK_OBSERVED_AT = (
    "---\nobserved_at: '   '\nsource: script:bounce-relay\n---\n\n"
    "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
)

SOURCE_WRONG_TYPE = (
    "---\nobserved_at: 2026-08-05\nsource:\n  - one\n  - two\n---\n\n"
    "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
)

NO_ADDRESS = (
    "---\nobserved_at: 2026-08-05\nsource: script:bounce-relay\n---\n\n"
    "The address hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
)

SEVERAL_ADDRESSES = (
    "---\nobserved_at: 2026-08-05\nsource: script:bounce-relay\n---\n\n"
    "alex@example.org and blair@example.com both hard-bounced. "
    "Diagnostic: 550 5.1.1 user unknown.\n"
)

TRANSIENT_4XX = (
    "---\nobserved_at: 2026-08-05\nsource: script:bounce-relay\n---\n\n"
    "alex@example.org soft-bounced. Diagnostic: 421 4.4.62 routing issue.\n"
)

NO_DIAGNOSTIC = (
    "---\nobserved_at: 2026-08-05\nsource: script:bounce-relay\n---\n\n"
    "alex@example.org came back undeliverable, no code given.\n"
)

NO_FRONTMATTER = "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"

FRONTMATTER_IS_A_LIST = (
    "---\n- observed_at\n- source\n---\n\n"
    "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
)

ORDINARY_PROSE = "---\nobserved_at: 2026-08-05\nsource: manual\n---\n\nAcme raised a Series B.\n"

#: Every fixture above, for the gate-agreement sweep. Name → note text.
ALL_FIXTURES: dict[str, str] = {
    "conforming": CONFORMING,
    "no_observed_at": NO_OBSERVED_AT,
    "no_source": NO_SOURCE,
    "blank_observed_at": BLANK_OBSERVED_AT,
    "source_wrong_type": SOURCE_WRONG_TYPE,
    "no_address": NO_ADDRESS,
    "several_addresses": SEVERAL_ADDRESSES,
    "transient_4xx": TRANSIENT_4XX,
    "no_diagnostic": NO_DIAGNOSTIC,
    "no_frontmatter": NO_FRONTMATTER,
    "frontmatter_is_a_list": FRONTMATTER_IS_A_LIST,
    "ordinary_prose": ORDINARY_PROSE,
    "bare_code_no_smtp_reply": (
        "---\nobserved_at: 2026-08-05\nsource: script:bounce-relay\n---\n\n"
        "alex@example.org: 5.1.10 recipient not found.\n"
    ),
    "empty": "",
}


class TestConformingNote:
    """The documented note shape is recognized, and reports what it recovered."""

    def test_conforming_note_conforms(self) -> None:
        result = check_tier0_bounce_conformance(CONFORMING)

        assert result.conforms is True
        assert result.declines == ()
        assert result.reasons == ()

    def test_conforming_note_reports_the_recovered_fields(self) -> None:
        result = check_tier0_bounce_conformance(CONFORMING)

        assert result.identifier == "alex@example.org"
        assert result.diagnostic == (
            "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown."
        )
        assert result.observed_at == "2026-08-05"
        assert result.source == "script:bounce-relay"

    def test_mapping_shaped_source_is_accepted(self) -> None:
        note = (
            "---\nobserved_at: 2026-08-05\nsource:\n  handle: script:bounce-relay\n---\n\n"
            "alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.\n"
        )

        result = check_tier0_bounce_conformance(note)

        assert result.conforms is True
        assert result.source == {"handle": "script:bounce-relay"}

    def test_check_writes_nothing(self, tmp_path: Path) -> None:
        # The check is a pure function of the note text: it takes no store
        # path at all, and leaves the cwd untouched.
        before = sorted(p.name for p in tmp_path.iterdir())

        check_tier0_bounce_conformance(CONFORMING)

        assert sorted(p.name for p in tmp_path.iterdir()) == before


class TestSingleConditionDeclines:
    """Each condition failing IN ISOLATION reports that condition, specifically."""

    @pytest.mark.parametrize(
        ("note", "reason", "where"),
        [
            (NO_OBSERVED_AT, MISSING_OBSERVED_AT, WHERE_FRONTMATTER),
            (BLANK_OBSERVED_AT, MISSING_OBSERVED_AT, WHERE_FRONTMATTER),
            (NO_SOURCE, MISSING_SOURCE, WHERE_FRONTMATTER),
            (SOURCE_WRONG_TYPE, UNSUPPORTED_SOURCE_TYPE, WHERE_FRONTMATTER),
            (NO_ADDRESS, NO_EMAIL_IDENTIFIER, WHERE_BODY),
            (SEVERAL_ADDRESSES, SEVERAL_EMAIL_IDENTIFIERS, WHERE_BODY),
            (TRANSIENT_4XX, MISSING_HARD_BOUNCE_CODE, WHERE_BODY),
            (NO_DIAGNOSTIC, MISSING_HARD_BOUNCE_CODE, WHERE_BODY),
            (FRONTMATTER_IS_A_LIST, FRONTMATTER_NOT_A_MAPPING, WHERE_FRONTMATTER),
        ],
    )
    def test_reports_exactly_the_failed_condition(
        self, note: str, reason: str, where: str
    ) -> None:
        result = check_tier0_bounce_conformance(note)

        assert result.conforms is False
        assert result.reasons == (reason,), f"expected only {reason}, got {result.reasons}"
        assert result.declines[0].where == where
        assert result.declines[0].detail  # actionable text, not an empty string
        assert result.fact is None
        assert result.identifier is None

    def test_reports_every_unmet_condition_not_just_the_first(self) -> None:
        # A producer fixing a batch needs the whole list — learning about one
        # failure per round trip is the thing this check exists to avoid.
        result = check_tier0_bounce_conformance(NO_FRONTMATTER)

        assert result.conforms is False
        assert set(result.reasons) == {MISSING_OBSERVED_AT, MISSING_SOURCE}

    def test_a_wholly_unrelated_note_declines_on_the_body(self) -> None:
        result = check_tier0_bounce_conformance(ORDINARY_PROSE)

        assert result.conforms is False
        assert set(result.reasons) == {NO_EMAIL_IDENTIFIER, MISSING_HARD_BOUNCE_CODE}

    def test_every_reported_reason_is_a_documented_token(self) -> None:
        for note in ALL_FIXTURES.values():
            for reason in check_tier0_bounce_conformance(note).reasons:
                assert reason in DECLINE_REASONS


class TestGateAgreement:
    """The check answers exactly what ``tier0_bounce_mark`` does — same code path.

    Asserted against the REAL gate (in ``dry_run``, which detects and reports
    but never writes), not a reimplementation of it: a contract test that
    re-derives the gate's logic drifts from it, which is worse than no test.
    """

    @staticmethod
    def _raw(tmp_path: Path, name: str, content: str) -> RawFile:
        raw_dir = tmp_path / "raw" / "bounce-relay"
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        return RawFile(path=path, source="bounce-relay", timestamp="", uuid8="")

    @pytest.mark.parametrize("name", sorted(ALL_FIXTURES))
    def test_check_verdict_matches_the_gate(self, name: str, tmp_path: Path) -> None:
        note = ALL_FIXTURES[name]
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)

        check = check_tier0_bounce_conformance(note)
        fact = tier0_bounce_mark(self._raw(tmp_path, name, note), wiki, dry_run=True)

        assert check.conforms is (fact is not None), (
            f"{name}: check says conforms={check.conforms}, gate says {fact!r}"
        )
        if fact is not None:
            assert check.identifier == fact.identifier
            assert check.diagnostic == fact.diagnostic

    def test_the_gate_still_writes_the_mark_for_a_conforming_note(
        self, tmp_path: Path
    ) -> None:
        # Guards the other half of the refactor: the check decides, the gate
        # still marks. A check that agreed with a gate that had stopped
        # writing would be agreement about nothing.
        from athenaeum.pii import contacts_surface_root, default_bounce_record_path

        config = {"storage": {"mapping": {"pii": "excluded"}}}
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)

        fact = tier0_bounce_mark(
            self._raw(tmp_path, "conforming", CONFORMING), wiki, config=config
        )

        assert fact is not None
        record = default_bounce_record_path(
            contacts_surface_root(wiki.parent, config), "alex@example.org"
        )
        assert record.exists()


class TestContractDocumentAgreement:
    """The published contract documents exactly the reasons the code can emit."""

    def test_document_exists(self) -> None:
        assert CONTRACT_DOC.is_file()

    def test_documented_reason_table_matches_the_code_exactly(self) -> None:
        # The doc's decline-reason table rows: "| `reason` | where | means |".
        # Set equality both ways: a reason added to the code without being
        # documented fails here, and so does a documented reason the code can
        # no longer emit.
        text = CONTRACT_DOC.read_text(encoding="utf-8")
        tabled = set(re.findall(r"^\| `([a-z_]+)` \| (?:frontmatter|body) \|", text, re.M))

        assert tabled == set(DECLINE_REASONS), (
            "the decline-reason table and bounce_contract.DECLINE_REASONS disagree: "
            f"doc-only={sorted(tabled - set(DECLINE_REASONS))}, "
            f"code-only={sorted(set(DECLINE_REASONS) - tabled)}"
        )

    def test_document_states_the_nothing_is_rejected_property(self) -> None:
        # The single property a producer most needs and cannot infer from a
        # bare API reference: a non-conforming note is not an error.
        text = CONTRACT_DOC.read_text(encoding="utf-8").lower()

        assert "not rejected" in text
        assert "falls through" in text


class TestCli:
    """``athenaeum bounce-contract`` — the surface a foreign producer calls."""

    def test_conforming_note_exits_zero(self, tmp_path: Path, capsys) -> None:
        note = tmp_path / "note.md"
        note.write_text(CONFORMING, encoding="utf-8")

        assert cli_main(["bounce-contract", "--file", str(note)]) == 0
        assert "conforms" in capsys.readouterr().out

    def test_non_conforming_note_exits_nonconforming(self, capsys) -> None:
        rc = cli_main(["bounce-contract", "--text", TRANSIENT_4XX])

        assert rc == EXIT_NONCONFORMING
        out = capsys.readouterr().out
        assert "does NOT conform" in out
        assert MISSING_HARD_BOUNCE_CODE in out

    def test_json_verdict_is_machine_readable(self, capsys) -> None:
        rc = cli_main(["bounce-contract", "--text", CONFORMING, "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["conforms"] is True
        assert payload["identifier"] == "alex@example.org"
        assert payload["observed_at"] == "2026-08-05"
        assert payload["declines"] == []

    def test_json_decline_carries_every_reason(self, capsys) -> None:
        rc = cli_main(["bounce-contract", "--text", NO_FRONTMATTER, "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert rc == EXIT_NONCONFORMING
        assert payload["conforms"] is False
        assert {d["reason"] for d in payload["declines"]} == {
            MISSING_OBSERVED_AT,
            MISSING_SOURCE,
        }
        assert all(d["where"] in (WHERE_FRONTMATTER, WHERE_BODY) for d in payload["declines"])

    def test_reads_the_note_from_stdin_by_default(self, capsys, monkeypatch) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(CONFORMING))

        assert cli_main(["bounce-contract"]) == 0
        assert "alex@example.org" in capsys.readouterr().out

    def test_cli_writes_nothing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)

        cli_main(["bounce-contract", "--text", CONFORMING])
        capsys.readouterr()

        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Sensitivity-registry migration (issue athenaeum#992, S3 of athenaeum#910's design note)
# ---------------------------------------------------------------------------
#
# This module's email-identifier count now comes from
# athenaeum.sensitivity.classify() (config=None — this module has no config
# surface of its own) instead of a direct `find_inline_emails` import, which
# the design note originally omitted from its S3 call-site inventory. AC3.


class TestNoDirectPiiDetectorImport:
    def test_module_does_not_import_find_inline_emails_by_name(self) -> None:
        import athenaeum.bounce_contract as bc
        from athenaeum.pii import find_inline_emails

        assert not hasattr(bc, "find_inline_emails")
        # athenaeum.pii's own export is untouched (AC8).
        assert find_inline_emails("reach a@b.com") == ["a@b.com"]


class TestSensitivityRegistryEquivalence:
    """AC4: the migrated email-identifier count agrees with the pre-change
    ``athenaeum.pii.find_inline_emails`` result on every fixture this module's
    existing test suite already exercises — proven, not merely asserted.
    Dedup is preserved (a repeated address counts once, matching the
    pre-change contract exactly), which is what keeps a note repeating one
    address from flipping ``SEVERAL_EMAIL_IDENTIFIERS``.
    """

    @pytest.mark.parametrize("name,note_text", list(ALL_FIXTURES.items()))
    def test_email_identifier_count_matches_pre_change_function(
        self, name: str, note_text: str
    ) -> None:
        from athenaeum.bounce_contract import _conforming_emails
        from athenaeum.models import parse_frontmatter
        from athenaeum.pii import find_inline_emails

        _, body = parse_frontmatter(note_text or "")
        assert _conforming_emails(body) == find_inline_emails(body), name

    def test_repeated_address_in_body_still_counts_once(self) -> None:
        note = (
            "---\nobserved_at: 2026-08-05\nsource: script:bounce-relay\n---\n\n"
            "alex@example.org hard-bounced, per alex@example.org's own report. "
            "Diagnostic: 550 5.1.1 user unknown.\n"
        )
        result = check_tier0_bounce_conformance(note)
        assert result.conforms is True
        assert result.identifier == "alex@example.org"
