# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum storage audit-h1-redaction`` (issue athenaeum#1461).

Thin CLI-integration coverage over :mod:`athenaeum.pii_h1_audit`, whose own
tests (``tests/test_pii_h1_audit.py``) carry the full classification and
read-only coverage. This file only proves the wiring: argv -> exit code ->
report text, and that the CLI path itself never writes.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.cli import main
from athenaeum.pii_h1_audit import MARKER


def _write_page(wiki_root: Path, filename: str, frontmatter: str, body: str) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / filename
    path.write_text(f"---\n{frontmatter}---\n{body}\n", encoding="utf-8")
    return path


def test_clean_corpus_exits_zero(tmp_path: Path, capsys) -> None:
    root = tmp_path / "knowledge"
    _write_page(
        root / "wiki", "clean.md", "uid: c1\nname: Clean\ntype: person\n", "# A normal title\n"
    )

    rc = main(["storage", "audit-h1-redaction", "--path", str(root)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "0 page(s)" in out


def test_defect_population_exits_nonzero_and_reports_classification(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "knowledge"
    _write_page(
        root / "wiki",
        "leading.md",
        "uid: c2\nname: Leading\ntype: person\n",
        f"# {MARKER}???\n\nBody.",
    )
    _write_page(
        root / "wiki",
        "mid.md",
        "uid: c3\nname: Mid\ntype: person\n",
        f"# Notes with {MARKER} about Q3\n\nBody.",
    )

    rc = main(["storage", "audit-h1-redaction", "--path", str(root)])

    assert rc == 2  # EXIT_PII_FOUND
    out = capsys.readouterr().out
    assert "[DEFECT]" in out
    assert "leading.md" in out
    assert "[NOT A DEFECT]" in out
    assert "mid.md" in out


def test_mid_heading_only_population_exits_zero(tmp_path: Path, capsys) -> None:
    # A page whose marker is genuinely mid-heading (not a defect) must NOT
    # trip the non-zero exit code on its own.
    root = tmp_path / "knowledge"
    _write_page(
        root / "wiki",
        "mid.md",
        "uid: c4\nname: Mid\ntype: person\n",
        f"# Notes with {MARKER} about Q3\n\nBody.",
    )

    rc = main(["storage", "audit-h1-redaction", "--path", str(root)])

    assert rc == 0


def test_cli_path_never_writes(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    page = _write_page(
        root / "wiki",
        "leading.md",
        "uid: c5\nname: Leading\ntype: person\n",
        f"# {MARKER}???\n\nBody.",
    )
    before = page.read_bytes()

    main(["storage", "audit-h1-redaction", "--path", str(root)])

    assert page.read_bytes() == before


def test_missing_wiki_root_errors(tmp_path: Path, capsys) -> None:
    root = tmp_path / "knowledge"  # no wiki/ created
    root.mkdir()

    rc = main(["storage", "audit-h1-redaction", "--path", str(root)])

    assert rc == 1
    assert "Wiki root not found" in capsys.readouterr().err
