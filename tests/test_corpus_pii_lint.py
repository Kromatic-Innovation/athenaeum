# SPDX-License-Identifier: Apache-2.0
"""Corpus-wide PII lint (issue #495).

#427's entity-page lint only ever opens entity pages, so a body-text email in
a ``_``-prefixed queue/index/archive file — or a stray ``.bak`` — sits inside
``wiki/`` (and is therefore recallable) forever without the lint noticing. #495
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
from athenaeum.pii import iter_corpus_files, scan_corpus_pii

#: Exit code ``storage lint-pii`` returns when it finds inline PII (mirrors the
#: outbound-lint convention; a "found something" signal distinct from 1).
EXIT_PII_FOUND = 2

# The queue-file shape #495 calls out: full draft bodies embedded in a merge
# archive, every contact datum copied verbatim into a file that lives inside
# ``wiki/`` with NO ``emails:`` frontmatter for the entity lint to catch.
_QUEUE_FILE_FIXTURE = """\
# _pending_merges_archive.md

## Proposed merge #412: Bob Roberts -> Robert Roberts

Draft body (verbatim):
  Bob leads partnerships. Reach him at bob.roberts@example.com or +1-555-0142.

## Proposed merge #418: Carol Vance -> Caroline Vance

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
            "Jane leads widgets. See issue #495 and the 2026 plan.\n",
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
        assert payload[0]["emails"] == ["grace@example.com"]
        assert payload[0]["path"].endswith("_queue.md")
