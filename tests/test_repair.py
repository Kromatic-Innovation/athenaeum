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


# ---------------------------------------------------------------------------
# Pass isolation + errors path

# A file whose tag-indent rewrite still fails to parse — used to drive the
# tag-indent "still_broken" error branch. Mixed indent + a stray unquoted
# bracket value that survives the indent normalization and keeps YAML broken.
TAG_INDENT_UNREPAIRABLE = """\
---
uid: person:eve
type: person
name: Eve Test
tags:
- person
  - apollo:enriched
current_title: [Founder] CEO
---

Body.
"""


def test_pass2_runs_after_pass1_errors_on_other_file(tmp_path: Path) -> None:
    """`repair --all` keeps running pass 2 on remaining files when pass 1
    logs an error on a malformed-beyond-repair file."""
    wiki = _make_wiki(
        tmp_path,
        {
            # pass 1 (tag-indent) will log an error on this one — its
            # post-rewrite frontmatter still fails yaml.safe_load because
            # of the unquoted `[Founder] CEO` value.
            "eve.md": TAG_INDENT_UNREPAIRABLE,
            # pass 2 (value-quoting) must still fix this one.
            "carol.md": VALUE_QUOTING_BROKEN,
        },
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["repair", "--all", "--apply", "--wiki-root", str(wiki)])

    # Errors present from pass 1 → exit 1
    assert rc == 1
    out = buf.getvalue()
    assert "tag-indent" in out
    assert "value-quoting" in out
    # carol.md got repaired by pass 2 despite pass 1's error
    text = (wiki / "carol.md").read_text()
    end = text.find("\n---\n", 4)
    meta = yaml.safe_load(text[4:end])
    assert meta["current_title"] == "[Founder] CEO"


# Truly unparseable YAML — unterminated double-quote inside frontmatter.
# Neither pass can repair this; both should populate `report.errors` for
# tag-indent's "still_broken" branch (no, actually only if a rewrite is
# attempted). For value-quoting we need a parse failure that the regex
# does not match, so the rewrite is a no-op and the file is just skipped.
# To force the errors-path test, we use a file that pass 1 will rewrite
# (mixed indent) but where the rewrite still fails to parse.
UNPARSEABLE_FRONTMATTER = """\
---
uid: person:frank
type: person
name: Frank Test
tags:
- person
  - apollo:enriched
title: "unterminated quote
---

Body.
"""


def test_unparseable_yaml_populates_errors_and_cli_exits_1(tmp_path: Path) -> None:
    """File with unterminated quote: tag-indent rewrites the indent but the
    result still fails ``yaml.safe_load`` → ``report.errors`` populated and
    CLI returns exit code 1."""
    wiki = _make_wiki(tmp_path, {"frank.md": UNPARSEABLE_FRONTMATTER})

    report = repair_tag_indent(wiki, apply=False)
    assert len(report.errors) == 1
    path, msg = report.errors[0]
    assert path.name == "frank.md"
    assert "still_broken" in msg

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["repair", "--tag-indent", "--wiki-root", str(wiki)])
    assert rc == 1


def test_atomic_write_no_tmp_left_behind(tmp_path: Path) -> None:
    """Apply pass leaves no ``*.tmp`` debris on disk."""
    wiki = _make_wiki(tmp_path, {"bob.md": TAG_INDENT_BROKEN})
    repair_tag_indent(wiki, apply=True)
    leftovers = list(wiki.glob("*.tmp")) + list(wiki.glob("*.md.tmp"))
    assert leftovers == []
