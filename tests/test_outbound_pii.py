# SPDX-License-Identifier: Apache-2.0
"""Tests for the outbound-draft PII lint (issue #455, split from #428).

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
            "see issue #455 and PR #460",
            "chapter 12 page 340",
            "version 1.2.3 released",
        ],
    )
    def test_short_digit_runs_are_not_phones(self, prose: str) -> None:
        assert scan_outbound_text(prose) == []


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
