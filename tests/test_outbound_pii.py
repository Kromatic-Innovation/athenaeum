# SPDX-License-Identifier: Apache-2.0
"""Tests for the outbound-draft PII lint (issue athenaeum#455, split from athenaeum#428).

Structure mirrors the issue's acceptance criteria:

- ``TestDetection`` — emails and phone numbers in several formats are found,
  each with its class and location.
- ``TestNoFalsePositives`` — ordinary prose with ``@`` handles/decorators and
  digit runs that are not phone numbers produce no findings, and clean text
  produces no findings.
- ``TestAllowlist`` — addresses already known to the recipient pass; the
  fail-safe default (no allowlist) flags everything.
- ``TestRedactMode`` — strip mode replaces findings with a placeholder and is
  distinct from flag-only mode.
- ``TestCli`` — the ``athenaeum outbound-lint`` entry point over --text/--file/
  stdin, --allow, --redact, and --json, with the found/clean exit codes.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from athenaeum._cmd_outbound import EXIT_PII_FOUND
from athenaeum.cli import main
from athenaeum.outbound_pii import (
    PII_KIND_EMAIL,
    PII_KIND_PHONE,
    Allowlist,
    lint_outbound_text,
    redact_outbound_text,
    scan_outbound_text,
)

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetection:
    def test_finds_a_plain_email(self) -> None:
        findings = scan_outbound_text("write to jane.doe@example.com please")
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == PII_KIND_EMAIL
        assert f.value == "jane.doe@example.com"

    @pytest.mark.parametrize(
        "phone",
        [
            "555-010-0100",
            "(555) 010-0100",
            "+1 555 010 0100",
            "+15550100100",
            "555.010.0100",
            "5550100100",
        ],
    )
    def test_finds_phone_in_several_formats(self, phone: str) -> None:
        findings = scan_outbound_text(f"call me at {phone} tomorrow")
        assert [f.kind for f in findings] == [PII_KIND_PHONE]
        assert findings[0].value.strip() == phone

    def test_reports_class_and_location(self) -> None:
        text = "line one\nreach jane@example.com now"
        findings = scan_outbound_text(text)
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == PII_KIND_EMAIL
        # 2nd line, column where the email begins (1-based).
        assert f.line == 2
        assert f.column == text.split("\n")[1].index("jane@example.com") + 1
        assert text[f.start : f.end] == f.value

    def test_location_on_an_earlier_line_of_multiline_text(self) -> None:
        # Finding on line 1 of a 3-line block exercises the line-lookup for a
        # non-final line (column is measured within that line, not the whole
        # document).
        text = "reach jane@example.com\nsecond line\nthird line"
        f = scan_outbound_text(text)[0]
        assert (f.line, f.column) == (1, len("reach ") + 1)

    def test_multiple_findings_are_in_document_order(self) -> None:
        text = "first a@b.com then call 555-010-0100 then c@d.org"
        findings = scan_outbound_text(text)
        assert [f.kind for f in findings] == [
            PII_KIND_EMAIL,
            PII_KIND_PHONE,
            PII_KIND_EMAIL,
        ]
        assert [f.start for f in findings] == sorted(f.start for f in findings)

    def test_email_containing_digits_is_not_double_counted_as_phone(self) -> None:
        # The digit run lives inside the email span, so it is reported once
        # (as the email), never twice.
        findings = scan_outbound_text("ping jo.5551234567@example.com")
        assert len(findings) == 1
        assert findings[0].kind == PII_KIND_EMAIL


# ---------------------------------------------------------------------------
# No false positives
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    def test_clean_text_produces_no_findings(self) -> None:
        assert scan_outbound_text("a perfectly ordinary sentence.") == []

    def test_empty_and_none_text(self) -> None:
        assert scan_outbound_text("") == []
        assert scan_outbound_text(None) == []  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "prose",
        [
            "mention @janedoe on the thread",
            "the @property decorator wraps it",
            "use @pytest.mark.parametrize here",
            "email me @ the office",
        ],
    )
    def test_at_signs_without_a_tld_are_not_emails(self, prose: str) -> None:
        assert scan_outbound_text(prose) == []

    @pytest.mark.parametrize(
        "prose",
        [
            "shipped in 2026 after 3 revisions",
            "see issue athenaeum#455 and PR athenaeum#460",
            "chapter 12 page 340",
            "version 1.2.3 released",
        ],
    )
    def test_short_digit_runs_are_not_phones(self, prose: str) -> None:
        assert scan_outbound_text(prose) == []

    @pytest.mark.parametrize(
        "prose",
        [
            "logged (2026-07-29) in the CRM",  # parenthesized date
            "see (2019-2020) season stats",  # parenthesized year range
            "page uid (52785095) in the index",  # parenthesized uid prefix
            "First contact: 2026-07-29 per CRM",  # bare date, 8 digits
        ],
    )
    def test_parenthesized_dates_and_ids_are_not_phones(self, prose: str) -> None:
        # scan_outbound_text shares _PHONE_RE / _has_enough_digits with
        # find_inline_phones and inherited the same leading-paren bug (athenaeum#683):
        # a parenthesized date/uid was over-flagged on the egress path. It now
        # applies the same provably-not-a-phone exclusion.
        assert scan_outbound_text(prose) == []

    def test_genuine_phone_beside_a_date_still_flags(self) -> None:
        # The exclusion drops only the date; a real phone in the same text is
        # still reported (the fix never suppresses a genuine number).
        findings = scan_outbound_text("met 2026-07-29, call (555) 010-0100")
        assert [f.value for f in findings] == ["(555) 010-0100"]


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_allowlisted_email_passes(self) -> None:
        text = "reach jane@example.com or john@other.com"
        findings = scan_outbound_text(text, allowlist=["jane@example.com"])
        assert [f.value for f in findings] == ["john@other.com"]

    def test_allowlist_email_is_case_insensitive(self) -> None:
        findings = scan_outbound_text(
            "reach Jane@Example.COM", allowlist=["jane@example.com"]
        )
        assert findings == []

    def test_allowlisted_phone_matches_regardless_of_separators(self) -> None:
        findings = scan_outbound_text(
            "call (555) 010-0100 now", allowlist=["555-010-0100"]
        )
        assert findings == []

    def test_no_allowlist_flags_everything_fail_safe(self) -> None:
        text = "reach jane@example.com at 555-010-0100"
        assert len(scan_outbound_text(text)) == 2
        assert len(scan_outbound_text(text, allowlist=None)) == 2
        assert len(scan_outbound_text(text, allowlist=[])) == 2

    def test_accepts_a_prebuilt_allowlist_object(self) -> None:
        allow = Allowlist.from_entries(["jane@example.com"])
        findings = scan_outbound_text("reach jane@example.com", allowlist=allow)
        assert findings == []

    def test_blank_allowlist_entries_are_ignored(self) -> None:
        allow = Allowlist.from_entries(["", "  ", "jane@example.com"])
        assert allow.emails == frozenset({"jane@example.com"})
        assert allow.phones == frozenset()


# ---------------------------------------------------------------------------
# Redact / strip mode (distinct from flag-only)
# ---------------------------------------------------------------------------


class TestRedactMode:
    def test_redacts_email_and_phone(self) -> None:
        cleaned, findings = redact_outbound_text(
            "reach jane@example.com at 555-010-0100"
        )
        assert "jane@example.com" not in cleaned
        assert "555-010-0100" not in cleaned
        assert "[redacted-email]" in cleaned
        assert "[redacted-phone]" in cleaned
        assert len(findings) == 2

    def test_redaction_preserves_surrounding_text(self) -> None:
        cleaned, _ = redact_outbound_text("before jane@example.com after")
        assert cleaned == "before [redacted-email] after"

    def test_findings_offsets_point_at_original_text(self) -> None:
        text = "before jane@example.com after"
        _, findings = redact_outbound_text(text)
        f = findings[0]
        assert text[f.start : f.end] == "jane@example.com"

    def test_allowlisted_address_is_not_redacted(self) -> None:
        cleaned, findings = redact_outbound_text(
            "reach jane@example.com", allowlist=["jane@example.com"]
        )
        assert cleaned == "reach jane@example.com"
        assert findings == []

    def test_custom_placeholder(self) -> None:
        cleaned, _ = redact_outbound_text(
            "reach jane@example.com", placeholder="<{kind}>"
        )
        assert cleaned == "reach <email>"

    def test_flag_only_mode_does_not_redact(self) -> None:
        result = lint_outbound_text("reach jane@example.com")
        assert result.redacted is None
        assert result.has_findings

    def test_lint_wrapper_redact_mode(self) -> None:
        result = lint_outbound_text("reach jane@example.com", redact=True)
        assert result.redacted == "reach [redacted-email]"
        assert result.has_findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_text_flag_reports_findings_and_exit_code(self, capsys) -> None:
        rc = main(["outbound-lint", "--text", "reach jane@example.com"])
        assert rc == EXIT_PII_FOUND
        out = capsys.readouterr().out
        assert "1 PII finding" in out
        assert "jane@example.com" in out

    def test_clean_text_exits_zero(self, capsys) -> None:
        rc = main(["outbound-lint", "--text", "nothing to see here"])
        assert rc == 0
        assert "0 PII findings" in capsys.readouterr().out

    def test_json_output(self, capsys) -> None:
        import json

        rc = main(["outbound-lint", "--text", "reach jane@example.com", "--json"])
        assert rc == EXIT_PII_FOUND
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["kind"] == PII_KIND_EMAIL
        assert payload[0]["value"] == "jane@example.com"

    def test_allow_flag_drops_finding(self, capsys) -> None:
        rc = main(
            [
                "outbound-lint",
                "--text",
                "reach jane@example.com",
                "--allow",
                "jane@example.com",
            ]
        )
        assert rc == 0
        assert "0 PII findings" in capsys.readouterr().out

    def test_redact_flag_emits_sanitized_stdout(self, capsys) -> None:
        rc = main(["outbound-lint", "--text", "reach jane@example.com", "--redact"])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == "reach [redacted-email]"
        assert "redacted 1 PII finding" in captured.err

    def test_file_input(self, tmp_path: Path, capsys) -> None:
        draft = tmp_path / "draft.txt"
        draft.write_text("call 555-010-0100", encoding="utf-8")
        rc = main(["outbound-lint", "--file", str(draft)])
        assert rc == EXIT_PII_FOUND
        assert "phone" in capsys.readouterr().out

    def test_stdin_input(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("reach jane@example.com"))
        rc = main(["outbound-lint"])
        assert rc == EXIT_PII_FOUND
        assert "jane@example.com" in capsys.readouterr().out

    def test_allowlist_file(self, tmp_path: Path, capsys) -> None:
        allow = tmp_path / "known.txt"
        allow.write_text("jane@example.com\n", encoding="utf-8")
        rc = main(
            [
                "outbound-lint",
                "--text",
                "reach jane@example.com",
                "--allowlist-file",
                str(allow),
            ]
        )
        assert rc == 0
        assert "0 PII findings" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Sensitivity-registry migration (issue athenaeum#992, S3 of athenaeum#910's design note)
# ---------------------------------------------------------------------------
#
# outbound_pii never imported `find_inline_emails`/`find_inline_phones` (the
# design note originally claimed it did — corrected in this PR). It imported
# `athenaeum.pii`'s private `_EMAIL_RE`/`_PHONE_RE`/`_has_enough_digits`/
# `_is_excluded_phone_shape` directly; those are now obtained through
# `athenaeum.sensitivity.classify()` instead (AC1, disposition (a): migrated,
# viable because the built-in recognisers populate `SensitivityMatch.span`,
# which redaction needs).


class TestNoDirectPiiPrivateImport:
    def test_module_does_not_import_pii_private_symbols_by_name(self) -> None:
        import athenaeum.outbound_pii as op

        for name in ("_EMAIL_RE", "_PHONE_RE", "_has_enough_digits", "_is_excluded_phone_shape"):
            assert not hasattr(op, name), name


class TestOutboundPiiUnchangedOnFixtures:
    """AC1(a): ``redact_outbound_text`` output is byte-identical to pre-change
    on this module's existing fixtures — the whole ``TestDetection`` /
    ``TestNoFalsePositives`` / ``TestAllowlist`` / ``TestRedactMode`` suites
    above are unmodified by athenaeum#992 and still green, which is the corpus-wide
    proof; this class is the direct, explicit ``redact_outbound_text`` check
    the AC names.
    """

    FIXTURES: list[str] = [
        "write to jane.doe@example.com please",
        "call me at (555) 010-0100 tomorrow",
        "call me at +1 555 010 0100 tomorrow",
        "first a@b.com then call 555-010-0100 then c@d.org",
        "ping jo.5551234567@example.com",
        "a perfectly ordinary sentence.",
        "mention @janedoe on the thread",
        "shipped in 2026 after 3 revisions",
        "logged (2026-07-29) in the CRM",
        "see (2019-2020) season stats",
        "page uid (52785095) in the index",
        "met 2026-07-29, call (555) 010-0100",
        "reach jane@example.com at 555-010-0100",
    ]

    def test_redact_outbound_text_byte_identical_on_fixtures(self) -> None:
        # Re-derive what pre-change scan_outbound_text would have found,
        # directly off the private detection primitives (the pre-change
        # implementation), and assert the migrated path matches exactly.
        from athenaeum.pii import (
            _EMAIL_RE,
            _PHONE_RE,
            _has_enough_digits,
            _is_excluded_phone_shape,
        )

        for text in self.FIXTURES:
            cleaned, findings = redact_outbound_text(text)
            email_spans: list[tuple[int, int]] = []
            expected: list[tuple[str, str, int, int]] = []
            for m in _EMAIL_RE.finditer(text):
                email_spans.append((m.start(), m.end()))
                expected.append((PII_KIND_EMAIL, m.group(0), m.start(), m.end()))
            for m in _PHONE_RE.finditer(text):
                token = m.group(1)
                if not _has_enough_digits(token):
                    continue
                if _is_excluded_phone_shape(token):
                    continue
                start, end = m.start(1), m.end(1)
                if any(start < e and s < end for s, e in email_spans):
                    continue
                expected.append((PII_KIND_PHONE, token, start, end))
            expected.sort(key=lambda f: f[2])
            expected_cleaned = text
            for kind, value, start, end in sorted(expected, key=lambda f: f[2], reverse=True):
                expected_cleaned = (
                    expected_cleaned[:start]
                    + f"[redacted-{kind}]"
                    + expected_cleaned[end:]
                )
            assert cleaned == expected_cleaned, text
            actual_kv = [(f.kind, f.value) for f in findings]
            expected_kv = [(k, v) for k, v, _, _ in expected]
            assert actual_kv == expected_kv, text


class TestLabeledIdentifierPrefixDeliberateDifference:
    """Enumerated, deliberate difference (AC1/AC4): the registry's built-in
    ``phone`` recogniser additionally suppresses a digit run the surrounding
    prose already types as a labeled record id (issue athenaeum#732), which
    ``scan_outbound_text`` did not previously apply. No existing fixture
    exercises this shape (see ``TestOutboundPiiUnchangedOnFixtures`` above,
    which stays byte-identical), so this is documented as new coverage, not a
    regression: outbound text carrying ``"QBO realm 1008563730"`` now reports
    no phone finding, where the pre-change implementation would have flagged
    it as a false-positive phone number.
    """

    def test_labeled_record_id_no_longer_flagged_as_a_phone(self) -> None:
        findings = scan_outbound_text("QBO realm 1008563730 was reconciled")
        assert findings == []
