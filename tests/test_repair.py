# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.repair (tag-indent + value-quoting passes)."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from athenaeum import cli
from athenaeum.repair import repair_tag_indent, repair_value_quoting

# ---------------------------------------------------------------------------
# Fixture builders


# A wiki where Apollo enricher spliced "  - apollo:enriched" into a 0-space
# tag block — fails yaml.safe_load until tag-indent normalization runs.
TAG_INDENT_BROKEN = """\
---
uid: person:bob
type: person
name: Bob Test
tags:
- person
  - apollo:enriched
- warm
emails:
- bob@example.com
- bob@other.com
---

# Bob Test

Body content.
"""

# Already-clean wiki with 2-space tag indent — must be a no-op.
TAG_INDENT_CLEAN = """\
---
uid: person:alice
type: person
name: Alice Test
tags:
  - person
  - warm
---

Body.
"""

# A wiki where current_title is unquoted and starts with "[" — breaks YAML.
VALUE_QUOTING_BROKEN = """\
---
uid: person:carol
type: person
name: Carol Test
current_title: [Founder] CEO
current_company: Acme
---

Body.
"""

# Already-clean — must be a no-op.
VALUE_QUOTING_CLEAN = """\
---
uid: person:dan
type: person
name: Dan Test
current_title: "VP Engineering"
---

Body.
"""


def _make_wiki(tmp_path: Path, files: dict[str, str]) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    for name, content in files.items():
        (wiki / name).write_text(content, encoding="utf-8")
    return wiki


# ---------------------------------------------------------------------------
# repair_tag_indent


def test_tag_indent_dry_run_does_not_write(tmp_path: Path) -> None:
    wiki = _make_wiki(
        tmp_path, {"bob.md": TAG_INDENT_BROKEN, "alice.md": TAG_INDENT_CLEAN}
    )
    before = (wiki / "bob.md").read_text()

    report = repair_tag_indent(wiki, apply=False)

    assert report.files_scanned == 2
    assert report.files_changed == 1
    assert (wiki / "bob.md").read_text() == before  # untouched
    assert any("bob.md" == p.name for p, _ in report.changes)


def test_tag_indent_apply_writes_and_parses(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"bob.md": TAG_INDENT_BROKEN})

    report = repair_tag_indent(wiki, apply=True)

    assert report.files_changed == 1
    text = (wiki / "bob.md").read_text()
    # Frontmatter parses cleanly now
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    fm = text[4:end]
    meta = yaml.safe_load(fm)
    assert meta["tags"] == ["person", "apollo:enriched", "warm"]
    assert meta["emails"] == ["bob@example.com", "bob@other.com"]


def test_tag_indent_idempotent(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"bob.md": TAG_INDENT_BROKEN})
    repair_tag_indent(wiki, apply=True)
    after_first = (wiki / "bob.md").read_text()

    report2 = repair_tag_indent(wiki, apply=True)

    assert report2.files_changed == 0
    assert (wiki / "bob.md").read_text() == after_first


def test_tag_indent_skips_underscore_files(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"_pending_questions.md": TAG_INDENT_BROKEN})
    report = repair_tag_indent(wiki, apply=False)
    assert report.files_scanned == 0


# ---------------------------------------------------------------------------
# repair_value_quoting


def test_value_quoting_dry_run(tmp_path: Path) -> None:
    wiki = _make_wiki(
        tmp_path,
        {"carol.md": VALUE_QUOTING_BROKEN, "dan.md": VALUE_QUOTING_CLEAN},
    )
    before = (wiki / "carol.md").read_text()

    report = repair_value_quoting(wiki, apply=False)

    assert report.files_scanned == 2
    assert report.files_changed == 1
    assert (wiki / "carol.md").read_text() == before


def test_value_quoting_apply_writes_and_parses(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"carol.md": VALUE_QUOTING_BROKEN})

    report = repair_value_quoting(wiki, apply=True)

    assert report.files_changed == 1
    text = (wiki / "carol.md").read_text()
    end = text.find("\n---\n", 4)
    meta = yaml.safe_load(text[4:end])
    assert meta["current_title"] == "[Founder] CEO"
    assert meta["current_company"] == "Acme"


def test_value_quoting_idempotent(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"carol.md": VALUE_QUOTING_BROKEN})
    repair_value_quoting(wiki, apply=True)
    after_first = (wiki / "carol.md").read_text()

    report2 = repair_value_quoting(wiki, apply=True)
    assert report2.files_changed == 0
    assert (wiki / "carol.md").read_text() == after_first


# ---------------------------------------------------------------------------
# CLI integration


def test_cli_dry_run_returns_2_when_fixes_pending(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"bob.md": TAG_INDENT_BROKEN})
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["repair", "--tag-indent", "--wiki-root", str(wiki)])
    assert rc == 2
    out = buf.getvalue()
    assert "tag-indent" in out
    assert "DRY RUN" in out
    assert "files_changed: 1" in out
    # File untouched
    assert (wiki / "bob.md").read_text() == TAG_INDENT_BROKEN


def test_cli_apply_returns_0(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"bob.md": TAG_INDENT_BROKEN})
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["repair", "--tag-indent", "--apply", "--wiki-root", str(wiki)])
    assert rc == 0
    assert "APPLY" in buf.getvalue()


def test_cli_clean_tree_returns_0(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"alice.md": TAG_INDENT_CLEAN})
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["repair", "--tag-indent", "--wiki-root", str(wiki)])
    assert rc == 0


def test_cli_all_runs_both_passes(tmp_path: Path) -> None:
    wiki = _make_wiki(
        tmp_path,
        {"bob.md": TAG_INDENT_BROKEN, "carol.md": VALUE_QUOTING_BROKEN},
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["repair", "--all", "--apply", "--wiki-root", str(wiki)])
    assert rc == 0
    out = buf.getvalue()
    assert "tag-indent" in out
    assert "value-quoting" in out


def test_cli_mutex_flags_rejected(tmp_path: Path, capsys) -> None:
    wiki = _make_wiki(tmp_path, {})
    try:
        cli.main(["repair", "--tag-indent", "--all", "--wiki-root", str(wiki)])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("expected SystemExit from mutex flag conflict")


def test_cli_missing_wiki_returns_1(tmp_path: Path) -> None:
    rc = cli.main(["repair", "--tag-indent", "--wiki-root", str(tmp_path / "nope")])
    assert rc == 1
