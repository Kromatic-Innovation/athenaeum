# SPDX-License-Identifier: Apache-2.0
"""Machine-generated audit log exclusion for the PII scan (issue athenaeum#1273).

``storage lint-pii`` walked ``_shape_rule_dispositions.jsonl`` — a 341+ MB,
~1.49M-record log the shape-rule engine regenerates every nightly run — and
found 0 emails / 100,533 phone-axis matches, every one an epoch-millisecond
timestamp. An allowlist entry (athenaeum#936) can never absorb these: the
file regenerates nightly with fresh timestamps, so the distinct-value set
never stabilises. These tests pin the fix: a filename-based, operator-
configurable exclusion (default: the confirmed offender only) that the CLI
reports rather than silently applies.

Mirrors ``test_corpus_pii_lint.py``'s ``TestSelfScanExclusion`` /
``TestLintPiiCLI`` in-process ``cli.main([...])`` + ``capsys`` style.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.cli import main
from athenaeum.config import load_config, resolve_pii_scan_exclude
from athenaeum.pii import (
    DEFAULT_PII_SCAN_EXCLUDE_FILENAMES,
    iter_corpus_files,
    resolve_pii_scan_exclude_filenames,
    scan_corpus_pii,
    scan_excluded_by_name,
)

#: Exit code ``storage lint-pii`` returns when it finds inline PII.
EXIT_PII_FOUND = 2

# A machine-log-shaped fixture: an epoch-millisecond timestamp is exactly
# what the live corpus's _shape_rule_dispositions.jsonl false-positives on
# the phone axis (issue athenaeum#1273 measured 100,533 such matches).
_MACHINE_LOG_FIXTURE = (
    '{"at": "2026-08-23T23:16:08Z", "disposition": "no-match", '
    '"key_fingerprint": "aaaaaaaaaaaaaaaa", "tier": null}\n'
    '{"note": "contact 17884567890123 for details"}\n'
)


def _wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    return root


class TestDefaultExcludeFilenames:
    def test_confirmed_offender_is_the_default(self) -> None:
        assert DEFAULT_PII_SCAN_EXCLUDE_FILENAMES == {"_shape_rule_dispositions.jsonl"}


class TestIterCorpusFilesExcludeNames:
    def test_excluded_filename_is_skipped(self, tmp_path: Path) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_shape_rule_dispositions.jsonl").write_text(
            _MACHINE_LOG_FIXTURE, encoding="utf-8"
        )
        (root / "wiki" / "_keep.md").write_text("hello\n", encoding="utf-8")

        files = iter_corpus_files(
            root / "wiki", exclude_names={"_shape_rule_dispositions.jsonl"}
        )

        names = {p.name for p in files}
        assert "_shape_rule_dispositions.jsonl" not in names
        assert "_keep.md" in names

    def test_no_exclude_names_scans_everything(self, tmp_path: Path) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_shape_rule_dispositions.jsonl").write_text(
            _MACHINE_LOG_FIXTURE, encoding="utf-8"
        )

        files = iter_corpus_files(root / "wiki")

        assert "_shape_rule_dispositions.jsonl" in {p.name for p in files}


class TestScanCorpusPiiExcludeNames:
    def test_excluded_machine_log_contributes_zero_findings(
        self, tmp_path: Path
    ) -> None:
        # THE required regression (AC #4): an excluded machine log's
        # phone-shaped timestamp must not show up as a finding at all.
        root = _wiki(tmp_path)
        (root / "wiki" / "_shape_rule_dispositions.jsonl").write_text(
            _MACHINE_LOG_FIXTURE, encoding="utf-8"
        )

        findings = scan_corpus_pii(
            root / "wiki", exclude_names={"_shape_rule_dispositions.jsonl"}
        )

        assert findings == []

    def test_without_exclusion_the_fixture_would_have_been_a_finding(
        self, tmp_path: Path
    ) -> None:
        # Sanity check that the fixture is genuinely phone-shaped and the
        # exclusion above is doing real work, not vacuously passing.
        root = _wiki(tmp_path)
        (root / "wiki" / "_shape_rule_dispositions.jsonl").write_text(
            _MACHINE_LOG_FIXTURE, encoding="utf-8"
        )

        findings = scan_corpus_pii(root / "wiki")

        assert len(findings) == 1
        assert findings[0].phones


class TestResolvePiiScanExclude:
    def test_unset_is_empty(self) -> None:
        assert resolve_pii_scan_exclude(None) == []
        assert resolve_pii_scan_exclude({}) == []

    def test_operator_additions_are_returned(self) -> None:
        config = {"storage": {"pii_scan_exclude": ["_my_other_machine_log.jsonl"]}}
        assert resolve_pii_scan_exclude(config) == ["_my_other_machine_log.jsonl"]

    def test_blank_and_non_string_entries_are_dropped(self) -> None:
        config = {"storage": {"pii_scan_exclude": ["  ", 42, "_ok.jsonl", None]}}
        assert resolve_pii_scan_exclude(config) == ["_ok.jsonl"]


class TestResolvePiiScanExcludeFilenames:
    def test_default_only_when_unconfigured(self) -> None:
        assert (
            resolve_pii_scan_exclude_filenames(None)
            == DEFAULT_PII_SCAN_EXCLUDE_FILENAMES
        )

    def test_operator_addition_is_additive_not_a_replacement(self) -> None:
        config = {"storage": {"pii_scan_exclude": ["_extra_log.jsonl"]}}
        result = resolve_pii_scan_exclude_filenames(config)
        assert "_shape_rule_dispositions.jsonl" in result  # default still present
        assert "_extra_log.jsonl" in result  # operator addition present too


class TestScanExcludedByName:
    def test_reports_matching_paths(self, tmp_path: Path) -> None:
        root = _wiki(tmp_path)
        target = root / "wiki" / "_shape_rule_dispositions.jsonl"
        target.write_text(_MACHINE_LOG_FIXTURE, encoding="utf-8")
        (root / "wiki" / "_keep.md").write_text("hello\n", encoding="utf-8")

        excluded = scan_excluded_by_name(
            root / "wiki", {"_shape_rule_dispositions.jsonl"}
        )

        assert excluded == [target]

    def test_missing_root_is_empty(self, tmp_path: Path) -> None:
        assert scan_excluded_by_name(tmp_path / "nope", {"x.jsonl"}) == []

    def test_no_names_is_empty(self, tmp_path: Path) -> None:
        root = _wiki(tmp_path)
        assert scan_excluded_by_name(root / "wiki", set()) == []


class TestLintPiiCLIReportsExclusion:
    def test_default_offender_is_excluded_and_reported(
        self, tmp_path: Path, capsys
    ) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_shape_rule_dispositions.jsonl").write_text(
            _MACHINE_LOG_FIXTURE, encoding="utf-8"
        )

        rc = main(["storage", "lint-pii", "--path", str(root)])

        # The machine log's timestamp-shaped phone would otherwise have made
        # this a non-clean run — the exclusion is what keeps it at 0.
        assert rc == 0
        captured = capsys.readouterr()
        assert "_shape_rule_dispositions.jsonl" in captured.err
        assert "athenaeum#1273" in captured.err

    def test_json_payload_lists_excluded_paths(self, tmp_path: Path, capsys) -> None:
        import json

        root = _wiki(tmp_path)
        (root / "wiki" / "_shape_rule_dispositions.jsonl").write_text(
            _MACHINE_LOG_FIXTURE, encoding="utf-8"
        )

        rc = main(["storage", "lint-pii", "--path", str(root), "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert any(
            p.endswith("_shape_rule_dispositions.jsonl") for p in payload["excluded"]
        )

    def test_operator_configured_extra_exclusion_is_honored(
        self, tmp_path: Path, capsys
    ) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_my_machine_log.jsonl").write_text(
            "reach 15551230100 at ext 9\n", encoding="utf-8"
        )
        (root / "athenaeum.yaml").write_text(
            "storage:\n  pii_scan_exclude:\n    - _my_machine_log.jsonl\n",
            encoding="utf-8",
        )

        rc = main(["storage", "lint-pii", "--path", str(root)])

        assert rc == 0
        assert "_my_machine_log.jsonl" in capsys.readouterr().err

    def test_no_config_file_behaves_as_the_shipped_default(
        self, tmp_path: Path
    ) -> None:
        root = _wiki(tmp_path)
        config = load_config(root)
        assert (
            resolve_pii_scan_exclude_filenames(config)
            == DEFAULT_PII_SCAN_EXCLUDE_FILENAMES
        )
