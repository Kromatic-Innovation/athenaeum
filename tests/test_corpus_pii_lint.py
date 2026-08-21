# SPDX-License-Identifier: Apache-2.0
"""Corpus-wide PII lint (issue athenaeum#495).

athenaeum#427's entity-page lint only ever opens entity pages, so a body-text email in
a ``_``-prefixed queue/index/archive file — or a stray ``.bak`` — sits inside
``wiki/`` (and is therefore recallable) forever without the lint noticing. athenaeum#495
measured 790 such pages, dominated by the corpus's own queue/archive files.
These tests pin the corpus-wide gate that closes that: :func:`scan_corpus_pii`
(the library) and ``athenaeum storage lint-pii`` (the CLI, non-zero exit on any
finding), with the **required** queue-file-shaped fixture regression.

Mirrors ``test_storage_migrate_pii.py``'s in-process ``cli.main([...])`` +
``capsys`` style.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.cli import main
from athenaeum.pii import (
    PII_ALLOWLIST_FILENAME,
    PiiAllowlistEntry,
    adjudicate_corpus_pii,
    iter_corpus_files,
    load_pii_allowlist,
    scan_corpus_pii,
)

#: Exit code ``storage lint-pii`` returns when it finds inline PII (mirrors the
#: outbound-lint convention; a "found something" signal distinct from 1).
EXIT_PII_FOUND = 2

# The queue-file shape athenaeum#495 calls out: full draft bodies embedded in a merge
# archive, every contact datum copied verbatim into a file that lives inside
# ``wiki/`` with NO ``emails:`` frontmatter for the entity lint to catch.
_QUEUE_FILE_FIXTURE = """\
# _pending_merges_archive.md

## Proposed merge athenaeum#412: Bob Roberts -> Robert Roberts

Draft body (verbatim):
  Bob leads partnerships. Reach him at bob.roberts@example.com or +1-555-0142.

## Proposed merge athenaeum#418: Carol Vance -> Caroline Vance

Draft body (verbatim):
  Carol runs the East region. Cell: (555) 010-9988.
"""


def _wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    return root


class TestScanCorpusPii:
    def test_flags_body_text_email_in_queue_file(self, tmp_path: Path) -> None:
        # THE required regression (AC #4): a queue-file-shaped fixture with an
        # email in prose and no emails: frontmatter must be flagged.
        root = _wiki(tmp_path)
        (root / "wiki" / "_pending_merges_archive.md").write_text(
            _QUEUE_FILE_FIXTURE, encoding="utf-8"
        )

        findings = scan_corpus_pii(root / "wiki")

        assert len(findings) == 1
        f = findings[0]
        assert f.path.name == "_pending_merges_archive.md"
        assert "bob.roberts@example.com" in f.emails
        assert "+1-555-0142" in f.phones

    def test_flags_stale_bak_file(self, tmp_path: Path) -> None:
        # A .bak with a timestamp suffix (not even a .md) must be scanned.
        root = _wiki(tmp_path)
        (root / "wiki" / "_pending_questions.md.bak.20260523_102517").write_text(
            "old draft: carol@example.com\n", encoding="utf-8"
        )

        findings = scan_corpus_pii(root / "wiki")

        assert [f.path.name for f in findings] == [
            "_pending_questions.md.bak.20260523_102517"
        ]
        assert findings[0].emails == ["carol@example.com"]

    def test_scans_nested_subdirectories(self, tmp_path: Path) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_followups").mkdir()
        (root / "wiki" / "_followups" / "skill-change-proposals.md").write_text(
            "contact eve@example.com about this.\n", encoding="utf-8"
        )

        findings = scan_corpus_pii(root / "wiki")

        assert len(findings) == 1
        assert findings[0].emails == ["eve@example.com"]

    def test_clean_corpus_returns_no_findings(self, tmp_path: Path) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "jane.md").write_text(
            "---\nuid: '1'\nname: Jane\ntype: person\n"
            "linkedin_url: https://linkedin.com/in/jane\n---\n"
            "Jane leads widgets. See issue athenaeum#495 and the 2026 plan.\n",
            encoding="utf-8",
        )
        # Durable identifiers (LinkedIn URL, uid, issue/year numbers) must not
        # false-positive as email/phone.
        assert scan_corpus_pii(root / "wiki") == []

    def test_missing_wiki_root_is_empty(self, tmp_path: Path) -> None:
        assert scan_corpus_pii(tmp_path / "nope") == []
        assert iter_corpus_files(tmp_path / "nope") == []

    def test_skips_unreadable_binary_files(self, tmp_path: Path) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "logo.png").write_bytes(b"\x89PNG\r\n\x00\xff\xfe")
        (root / "wiki" / "_queue.md").write_text(
            "frank@example.com\n", encoding="utf-8"
        )

        findings = scan_corpus_pii(root / "wiki")

        # Binary asset skipped; the text file still flagged.
        assert [f.path.name for f in findings] == ["_queue.md"]


class TestLintPiiCLI:
    def test_exits_nonzero_on_finding(self, tmp_path: Path, capsys) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_pending_merges_archive.md").write_text(
            _QUEUE_FILE_FIXTURE, encoding="utf-8"
        )

        rc = main(["storage", "lint-pii", "--path", str(root)])

        assert rc == EXIT_PII_FOUND
        out = capsys.readouterr().out
        assert "_pending_merges_archive.md" in out
        assert "bob.roberts@example.com" in out

    def test_exits_zero_on_clean_corpus(self, tmp_path: Path, capsys) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "jane.md").write_text(
            "---\nuid: '1'\nname: Jane\ntype: person\n---\nJane leads widgets.\n",
            encoding="utf-8",
        )

        rc = main(["storage", "lint-pii", "--path", str(root)])

        assert rc == 0
        assert "0 inline PII findings" in capsys.readouterr().out

    def test_json_output(self, tmp_path: Path, capsys) -> None:
        import json

        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "reach grace@example.com\n", encoding="utf-8"
        )

        rc = main(["storage", "lint-pii", "--path", str(root), "--json"])

        assert rc == EXIT_PII_FOUND
        payload = json.loads(capsys.readouterr().out)
        assert payload["wiki"][0]["emails"] == ["grace@example.com"]
        assert payload["wiki"][0]["path"].endswith("_queue.md")
        # athenaeum#1049: raw/ is always present in the payload, empty when
        # absent/clean — a consumer never has to guess whether the key exists.
        assert payload["raw"] == []


# ---------------------------------------------------------------------------
# raw/ observability (issue athenaeum#1049)
# ---------------------------------------------------------------------------
#
# `_cmd_storage_lint_pii` resolved its scan root as `knowledge_root / "wiki"`
# only. `raw/` is a SIBLING of `wiki/`, not a descendant, and was never
# scanned — `docs/sensitivity-value-routing.md` §5 confirms this premise
# directly against the code. Once athenaeum#1025's standing filter ships, an
# original value stays in `raw/` in the clear (append-only by contract) while
# only a pointer reaches `wiki/`, so a clean `lint-pii` would read as "no
# retained values anywhere" when every original is still sitting in `raw/`,
# unmeasured. These tests pin the fix: `raw/` is scanned and reported as a
# SEPARATE, non-gating surface — never summed into the wiki finding count,
# never flipping the exit code — so wiki cleanliness and raw retention stay
# distinguishable instead of collapsing into one number.


def _wiki_and_raw(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    (root / "raw").mkdir(parents=True)
    return root


class TestLintPiiRawTree:
    def test_raw_finding_is_reported_but_does_not_fail_the_gate(
        self, tmp_path: Path, capsys
    ) -> None:
        # A clean wiki + a dirty raw/ must still exit 0 (issue athenaeum#1049):
        # raw retention is today's normal, unavoidable state, not a regression
        # this gate could ever clear -- gating on it would fail permanently.
        root = _wiki_and_raw(tmp_path)
        (root / "wiki" / "jane.md").write_text(
            "---\nuid: '1'\nname: Jane\ntype: person\n---\nJane leads widgets.\n",
            encoding="utf-8",
        )
        (root / "raw" / "intake-1.md").write_text(
            "Reach Bob at bob.roberts@example.com or +1-555-0142.\n",
            encoding="utf-8",
        )

        rc = main(["storage", "lint-pii", "--path", str(root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "0 inline PII findings under" in out  # the wiki line
        assert "2 inline PII finding(s) in 1 file(s) under" in out  # the raw line
        assert "bob.roberts@example.com" in out
        assert "not gated" in out

    def test_raw_dirty_and_wiki_dirty_exit_code_reflects_wiki_only(
        self, tmp_path: Path, capsys
    ) -> None:
        # Both surfaces carrying findings still exits EXIT_PII_FOUND (from the
        # wiki finding) -- never EXIT_PII_FOUND * 2 or some summed code, and
        # the raw count is not added into the wiki finding count.
        root = _wiki_and_raw(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "reach grace@example.com\n", encoding="utf-8"
        )
        (root / "raw" / "intake-1.md").write_text(
            "carol@example.com and dave@example.com\n", encoding="utf-8"
        )

        rc = main(["storage", "lint-pii", "--path", str(root)])

        assert rc == EXIT_PII_FOUND
        out = capsys.readouterr().out
        assert "1 inline PII finding(s) in 1 file(s) under" in out  # wiki count
        assert "2 inline PII finding(s) in 1 file(s) under" in out  # raw count, distinct

    def test_missing_raw_root_reports_clean_not_error(
        self, tmp_path: Path, capsys
    ) -> None:
        # _wiki() (not _wiki_and_raw()) never creates raw/ -- mirrors
        # test_missing_wiki_root_is_empty's "missing means empty, never raises".
        root = _wiki(tmp_path)
        (root / "wiki" / "jane.md").write_text(
            "---\nuid: '1'\nname: Jane\ntype: person\n---\nJane leads widgets.\n",
            encoding="utf-8",
        )

        rc = main(["storage", "lint-pii", "--path", str(root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "0 inline PII findings under" in out
        assert str(root / "raw") in out

    def test_clean_raw_tree_is_reported_as_zero(
        self, tmp_path: Path, capsys
    ) -> None:
        root = _wiki_and_raw(tmp_path)
        (root / "wiki" / "jane.md").write_text(
            "---\nuid: '1'\nname: Jane\ntype: person\n---\nJane leads widgets.\n",
            encoding="utf-8",
        )
        (root / "raw" / "intake-1.md").write_text(
            "Jane leads widgets, no contact data here.\n", encoding="utf-8"
        )

        rc = main(["storage", "lint-pii", "--path", str(root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert f"0 inline PII findings under {root / 'raw'}" in out

    def test_raw_finding_json_is_a_separate_key_never_summed(
        self, tmp_path: Path, capsys
    ) -> None:
        import json

        root = _wiki_and_raw(tmp_path)
        (root / "wiki" / "jane.md").write_text(
            "---\nuid: '1'\nname: Jane\ntype: person\n---\nJane leads widgets.\n",
            encoding="utf-8",
        )
        (root / "raw" / "intake-1.md").write_text(
            "carol@example.com\n", encoding="utf-8"
        )

        rc = main(["storage", "lint-pii", "--path", str(root), "--json"])

        assert rc == 0  # raw findings never flip the exit code
        payload = json.loads(capsys.readouterr().out)
        assert payload["wiki"] == []
        assert payload["raw"][0]["emails"] == ["carol@example.com"]
        assert payload["raw"][0]["path"].endswith("intake-1.md")

    def test_wiki_only_behavior_is_unchanged_by_raw_scanning(
        self, tmp_path: Path, capsys
    ) -> None:
        # Regression guard: adding the raw/ scan must not alter wiki/'s own
        # finding count, adjudication, or exit code when raw/ does not exist
        # at all (the pre-athenaeum#1049 shape every existing test in this file
        # already exercises via `_wiki()`).
        root = _wiki(tmp_path)
        (root / "wiki" / "_pending_merges_archive.md").write_text(
            _QUEUE_FILE_FIXTURE, encoding="utf-8"
        )

        rc = main(["storage", "lint-pii", "--path", str(root)])

        assert rc == EXIT_PII_FOUND
        out = capsys.readouterr().out
        assert "3 inline PII finding(s) in 1 file(s) under" in out


# ---------------------------------------------------------------------------
# Adjudicated allowlist (issue athenaeum#936, unblocking athenaeum#437)
# ---------------------------------------------------------------------------
#
# athenaeum#437's criterion is "exit 0, OR every remaining finding appears in a
# committed allowlist, one entry per distinct value, each carrying a one-line
# reason". Both branches were unreachable: nothing read an allowlist, and the
# corpus-wide sweep above scans EVERY file under wiki/ — so the allowlist, a
# file of verbatim contact values by construction, would have been scanned like
# any other and RAISED the count. These pin both halves.


def _allowlist(root: Path, body: str) -> Path:
    """Write the conventional allowlist inside wiki/ (where it self-scans)."""
    path = root / "wiki" / PII_ALLOWLIST_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


class TestLoadPiiAllowlist:
    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        # "Nothing adjudicated" — behaviour identical to before athenaeum#936.
        entries, errors = load_pii_allowlist(tmp_path / "nope.yml")
        assert entries == []
        assert errors == []

    def test_loads_value_and_reason(self, tmp_path: Path) -> None:
        p = tmp_path / "a.yml"
        p.write_text(
            "- value: noreply@example.com\n"
            "  reason: service account, not a person\n",
            encoding="utf-8",
        )
        entries, errors = load_pii_allowlist(p)
        assert errors == []
        assert entries == [
            PiiAllowlistEntry(
                value="noreply@example.com", reason="service account, not a person"
            )
        ]

    def test_entry_missing_its_reason_is_reported_and_skipped(
        self, tmp_path: Path
    ) -> None:
        # A value is never tolerated by OMISSION: no reason -> adjudicates
        # nothing, so whatever it would have covered stays unexplained.
        p = tmp_path / "a.yml"
        p.write_text(
            "- value: noreply@example.com\n"
            "- value: ok@example.com\n  reason: tagged test address\n",
            encoding="utf-8",
        )
        entries, errors = load_pii_allowlist(p)
        assert [e.value for e in entries] == ["ok@example.com"]
        assert len(errors) == 1
        assert "noreply@example.com" in errors[0]
        assert "reason" in errors[0]

    def test_empty_reason_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "a.yml"
        p.write_text("- value: a@example.com\n  reason: '   '\n", encoding="utf-8")
        entries, errors = load_pii_allowlist(p)
        assert entries == []
        assert len(errors) == 1

    def test_malformed_yaml_fails_closed(self, tmp_path: Path) -> None:
        p = tmp_path / "a.yml"
        p.write_text("- value: [unclosed\n", encoding="utf-8")
        entries, errors = load_pii_allowlist(p)
        assert entries == []
        assert len(errors) == 1

    def test_non_list_top_level_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "a.yml"
        p.write_text("value: a@example.com\n", encoding="utf-8")
        entries, errors = load_pii_allowlist(p)
        assert entries == []
        assert "list" in errors[0]

    def test_empty_file_loads_nothing_without_error(self, tmp_path: Path) -> None:
        p = tmp_path / "a.yml"
        p.write_text("", encoding="utf-8")
        assert load_pii_allowlist(p) == ([], [])


class TestSelfScanExclusion:
    def test_allowlist_is_excluded_from_its_own_scan(self, tmp_path: Path) -> None:
        # THE load-bearing case: without this, authoring the artifact athenaeum#437
        # demands would RAISE the finding count and exit 0 stays unreachable.
        root = _wiki(tmp_path)
        path = _allowlist(
            root,
            "- value: noreply@example.com\n  reason: service account\n",
        )

        assert scan_corpus_pii(root / "wiki") != []  # scanned like any file...
        assert scan_corpus_pii(root / "wiki", exclude=[path]) == []  # ...unless excluded
        assert path not in iter_corpus_files(root / "wiki", exclude=[path])

    def test_exclusion_matches_an_unresolved_spelling(self, tmp_path: Path) -> None:
        # Same file named via a `..` hop must still be excluded.
        root = _wiki(tmp_path)
        _allowlist(root, "- value: a@example.com\n  reason: test address\n")
        spelled = root / "wiki" / "_x" / ".." / PII_ALLOWLIST_FILENAME
        (root / "wiki" / "_x").mkdir()

        assert scan_corpus_pii(root / "wiki", exclude=[spelled]) == []


class TestAdjudication:
    def test_full_coverage_leaves_nothing_unexplained(self, tmp_path: Path) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "ping noreply@example.com or 555-010-9988\n", encoding="utf-8"
        )
        findings = scan_corpus_pii(root / "wiki")
        entries = [
            PiiAllowlistEntry(value="noreply@example.com", reason="service account"),
            PiiAllowlistEntry(value="555-010-9988", reason="ticket id, not a phone"),
        ]

        result = adjudicate_corpus_pii(findings, entries)

        assert result.is_clean
        assert result.unexplained_count == 0
        assert result.adjudicated_count == 2
        assert result.stale == []
        assert result.findings[0].is_adjudicated

    def test_partial_coverage_still_fails(self, tmp_path: Path) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "noreply@example.com and real.person@example.com\n", encoding="utf-8"
        )
        findings = scan_corpus_pii(root / "wiki")
        entries = [
            PiiAllowlistEntry(value="noreply@example.com", reason="service account")
        ]

        result = adjudicate_corpus_pii(findings, entries)

        assert not result.is_clean
        assert result.unexplained_count == 1
        assert result.adjudicated_count == 1
        assert result.findings[0].unexplained_emails == ["real.person@example.com"]
        assert not result.findings[0].is_adjudicated

    def test_entry_matching_nothing_is_stale(self, tmp_path: Path) -> None:
        # The artifact must not rot into a permanent blanket over values that
        # have since left the corpus.
        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "noreply@example.com\n", encoding="utf-8"
        )
        findings = scan_corpus_pii(root / "wiki")
        entries = [
            PiiAllowlistEntry(value="noreply@example.com", reason="service account"),
            PiiAllowlistEntry(value="gone@example.com", reason="migrated off-corpus"),
        ]

        result = adjudicate_corpus_pii(findings, entries)

        assert result.is_clean
        assert [e.value for e in result.stale] == ["gone@example.com"]

    def test_no_entries_is_todays_behaviour(self, tmp_path: Path) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "grace@example.com\n", encoding="utf-8"
        )
        result = adjudicate_corpus_pii(scan_corpus_pii(root / "wiki"), [])
        assert not result.is_clean
        assert result.unexplained_count == 1
        assert result.adjudicated_count == 0


class TestLintPiiAllowlistCLI:
    def test_no_allowlist_file_behaves_as_before(
        self, tmp_path: Path, capsys
    ) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "grace@example.com\n", encoding="utf-8"
        )

        rc = main(["storage", "lint-pii", "--path", str(root)])

        assert rc == EXIT_PII_FOUND
        assert "grace@example.com" in capsys.readouterr().out

    def test_full_coverage_exits_zero(self, tmp_path: Path, capsys) -> None:
        # athenaeum#437's second branch, reachable for the first time.
        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "ping noreply@example.com\n", encoding="utf-8"
        )
        _allowlist(root, "- value: noreply@example.com\n  reason: service account\n")

        rc = main(["storage", "lint-pii", "--path", str(root)])

        out = capsys.readouterr().out
        assert rc == 0
        assert "0 unexplained" in out
        assert "1 adjudicated residue" in out

    def test_partial_coverage_exits_two(self, tmp_path: Path, capsys) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "noreply@example.com and real.person@example.com\n", encoding="utf-8"
        )
        _allowlist(root, "- value: noreply@example.com\n  reason: service account\n")

        rc = main(["storage", "lint-pii", "--path", str(root)])

        out = capsys.readouterr().out
        assert rc == EXIT_PII_FOUND
        assert "real.person@example.com" in out
        # The adjudicated value is residue, not a failure line.
        assert "1 unexplained" in out

    def test_stale_entry_is_surfaced_on_stderr(self, tmp_path: Path, capsys) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "jane.md").write_text("Jane leads widgets.\n", encoding="utf-8")
        _allowlist(root, "- value: gone@example.com\n  reason: migrated off-corpus\n")

        rc = main(["storage", "lint-pii", "--path", str(root)])

        captured = capsys.readouterr()
        assert rc == 0
        assert "stale allowlist entry" in captured.err
        assert "gone@example.com" in captured.err

    def test_entry_missing_reason_is_warned_and_still_fails(
        self, tmp_path: Path, capsys
    ) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "noreply@example.com\n", encoding="utf-8"
        )
        _allowlist(root, "- value: noreply@example.com\n")  # no reason

        rc = main(["storage", "lint-pii", "--path", str(root)])

        captured = capsys.readouterr()
        assert rc == EXIT_PII_FOUND  # fails closed
        assert "reason" in captured.err
        assert "noreply@example.com" in captured.out

    def test_allowlist_path_is_overridable(self, tmp_path: Path, capsys) -> None:
        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "noreply@example.com\n", encoding="utf-8"
        )
        elsewhere = tmp_path / "adjudicated.yml"
        elsewhere.write_text(
            "- value: noreply@example.com\n  reason: service account\n",
            encoding="utf-8",
        )

        rc = main(
            [
                "storage",
                "lint-pii",
                "--path",
                str(root),
                "--allowlist",
                str(elsewhere),
            ]
        )

        assert rc == 0

    def test_json_carries_adjudication_status(self, tmp_path: Path, capsys) -> None:
        import json

        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "noreply@example.com and real.person@example.com\n", encoding="utf-8"
        )
        _allowlist(root, "- value: noreply@example.com\n  reason: service account\n")

        rc = main(["storage", "lint-pii", "--path", str(root), "--json"])

        assert rc == EXIT_PII_FOUND
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["wiki"]) == 1
        assert payload["wiki"][0]["emails"] == ["real.person@example.com"]
        assert payload["wiki"][0]["allowlisted"] == ["noreply@example.com"]
        assert payload["wiki"][0]["adjudicated"] is False

    def test_json_exits_zero_when_fully_adjudicated(
        self, tmp_path: Path, capsys
    ) -> None:
        import json

        root = _wiki(tmp_path)
        (root / "wiki" / "_queue.md").write_text(
            "noreply@example.com\n", encoding="utf-8"
        )
        _allowlist(root, "- value: noreply@example.com\n  reason: service account\n")

        rc = main(["storage", "lint-pii", "--path", str(root), "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["wiki"][0]["adjudicated"] is True
        assert payload["wiki"][0]["emails"] == []
