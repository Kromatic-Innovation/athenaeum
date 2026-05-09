# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.repair (tag-indent + value-quoting passes)."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from athenaeum import cli
from athenaeum.repair import (
    LEGACY_SLUG_MAP,
    migrate_legacy_source_slugs,
    repair_tag_indent,
    repair_value_quoting,
)

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


# ---------------------------------------------------------------------------
# 3. Legacy bare-slug source: migration  (issue #97 / design-lock §5)

LEGACY_EXTENDED = """\
---
uid: person:gail
type: person
name: Gail Test
source: extended-tier-build
---

Body referencing extended-tier-build literally in prose.
"""

LEGACY_WARM = """\
---
uid: person:hank
type: person
name: Hank Test
source: warm-network-detect
---

Body.
"""

ALREADY_TYPED = """\
---
uid: person:ivy
type: person
name: Ivy Test
source: api:apollo:2026-04-29
---

Body.
"""

UNKNOWN_SLUG = """\
---
uid: person:jay
type: person
name: Jay Test
source: unknown-slug-xyz
---

Body.
"""


def test_legacy_slugs_dry_run_does_not_write(tmp_path: Path) -> None:
    wiki = _make_wiki(
        tmp_path,
        {
            "gail.md": LEGACY_EXTENDED,
            "hank.md": LEGACY_WARM,
            "ivy.md": ALREADY_TYPED,
        },
    )
    before = {p.name: p.read_text() for p in wiki.glob("*.md")}

    report = migrate_legacy_source_slugs(wiki, apply=False)

    assert report.files_scanned == 3
    assert report.would_rewrite == 2
    assert report.rewrites_applied == 0
    assert report.unknown_slugs == {}
    assert report.per_slug_counts == {
        "extended-tier-build": 1,
        "warm-network-detect": 1,
    }
    # No file mutated on disk.
    for p in wiki.glob("*.md"):
        assert p.read_text() == before[p.name]


def test_legacy_slugs_apply_rewrites_to_typed_form(tmp_path: Path) -> None:
    wiki = _make_wiki(
        tmp_path,
        {
            "gail.md": LEGACY_EXTENDED,
            "hank.md": LEGACY_WARM,
            "ivy.md": ALREADY_TYPED,
        },
    )

    report = migrate_legacy_source_slugs(wiki, apply=True)

    assert report.rewrites_applied == 2
    assert report.skipped_validation_fail == 0
    assert "source: script:extended-tier-build" in (wiki / "gail.md").read_text()
    assert "source: script:warm-network-detect" in (wiki / "hank.md").read_text()
    # Already-typed wiki untouched.
    assert (wiki / "ivy.md").read_text() == ALREADY_TYPED


def test_legacy_slugs_idempotent(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"gail.md": LEGACY_EXTENDED})
    migrate_legacy_source_slugs(wiki, apply=True)
    after_first = (wiki / "gail.md").read_text()

    report2 = migrate_legacy_source_slugs(wiki, apply=True)

    assert report2.would_rewrite == 0
    assert report2.rewrites_applied == 0
    assert (wiki / "gail.md").read_text() == after_first


def test_legacy_slugs_unknown_slug_aborts(tmp_path: Path) -> None:
    wiki = _make_wiki(
        tmp_path,
        {
            "gail.md": LEGACY_EXTENDED,
            "jay.md": UNKNOWN_SLUG,
        },
    )
    before = {p.name: p.read_text() for p in wiki.glob("*.md")}

    report = migrate_legacy_source_slugs(wiki, apply=True)

    assert report.unknown_slugs == {"unknown-slug-xyz": 1}
    # Neither legacy nor typed wikis are written.
    assert report.rewrites_applied == 0
    for p in wiki.glob("*.md"):
        assert p.read_text() == before[p.name]


def test_legacy_slugs_byte_for_byte_body_preservation(tmp_path: Path) -> None:
    """Body containing the literal slug string is not touched — only the
    frontmatter ``source:`` line is rewritten."""
    wiki = _make_wiki(tmp_path, {"gail.md": LEGACY_EXTENDED})
    migrate_legacy_source_slugs(wiki, apply=True)

    new = (wiki / "gail.md").read_text()
    # Body retained verbatim.
    assert "Body referencing extended-tier-build literally in prose." in new
    # Frontmatter source line rewritten to typed form.
    assert "source: script:extended-tier-build\n" in new
    # The bare legacy slug no longer appears as a `source:` value.
    assert "source: extended-tier-build\n" not in new
    # Difference between old and new is ONLY the source line — the rest
    # of the file is byte-identical.
    old_lines = LEGACY_EXTENDED.splitlines()
    new_lines = new.splitlines()
    assert len(old_lines) == len(new_lines)
    diffs = [(i, o, n) for i, (o, n) in enumerate(zip(old_lines, new_lines)) if o != n]
    assert len(diffs) == 1
    assert diffs[0][1].startswith("source: ")
    assert diffs[0][2] == "source: script:extended-tier-build"


def test_legacy_slugs_validation_failure_skips_file(
    tmp_path: Path, monkeypatch
) -> None:
    """If ``validate_wiki_meta`` rejects the rewritten frontmatter, that
    file is skipped (recorded in ``skipped_validation_fail``) and the
    other files still proceed."""
    wiki = _make_wiki(
        tmp_path,
        {"gail.md": LEGACY_EXTENDED, "hank.md": LEGACY_WARM},
    )

    from athenaeum import schemas as schemas_mod

    real_validate = schemas_mod.validate_wiki_meta
    fail_target = "person:gail"

    def fake_validate(meta):
        if meta.get("uid") == fail_target:
            raise ValueError("synthetic validation failure")
        return real_validate(meta)

    monkeypatch.setattr(schemas_mod, "validate_wiki_meta", fake_validate)

    report = migrate_legacy_source_slugs(wiki, apply=True)

    assert report.skipped_validation_fail == 1
    assert report.rewrites_applied == 1
    # gail.md untouched (validation failed).
    assert (wiki / "gail.md").read_text() == LEGACY_EXTENDED
    # hank.md migrated.
    assert "source: script:warm-network-detect" in (wiki / "hank.md").read_text()


def test_legacy_slug_map_is_exactly_the_design_locked_two() -> None:
    """Guard against accidental expansion of the mapping in PRs other
    than a deliberate design-doc revision."""
    assert LEGACY_SLUG_MAP == {
        "extended-tier-build": "script:extended-tier-build",
        "warm-network-detect": "script:warm-network-detect",
    }


# ---------------------------------------------------------------------------
# CLI integration for legacy-source-slugs


def test_cli_legacy_slugs_dry_run_returns_2(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"gail.md": LEGACY_EXTENDED})
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["repair", "--legacy-source-slugs", "--wiki-root", str(wiki)])
    assert rc == 2
    out = buf.getvalue()
    assert "legacy-source-slugs" in out
    assert "DRY RUN" in out
    assert "would_rewrite:           1" in out
    # Untouched on dry-run.
    assert (wiki / "gail.md").read_text() == LEGACY_EXTENDED


def test_cli_legacy_slugs_apply_returns_0(tmp_path: Path) -> None:
    wiki = _make_wiki(tmp_path, {"gail.md": LEGACY_EXTENDED})
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(
            [
                "repair",
                "--legacy-source-slugs",
                "--apply",
                "--wiki-root",
                str(wiki),
            ]
        )
    assert rc == 0
    assert "APPLY" in buf.getvalue()
    assert "source: script:extended-tier-build" in (wiki / "gail.md").read_text()


def test_cli_legacy_slugs_unknown_slug_returns_1(tmp_path: Path, capsys) -> None:
    wiki = _make_wiki(tmp_path, {"jay.md": UNKNOWN_SLUG})
    rc = cli.main(
        [
            "repair",
            "--legacy-source-slugs",
            "--apply",
            "--wiki-root",
            str(wiki),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "unknown-slug-xyz" in captured.err
    assert "ABORTED" in captured.err


def test_legacy_scalar_re_still_present_in_provenance() -> None:
    """Quine gate: the legacy bare-slug regex MUST still be present in
    ``provenance.py``. This PR ships the migration tool ONLY; the regex
    retires in a separate follow-up after the live tree is migrated."""
    from athenaeum import provenance

    assert hasattr(provenance, "_LEGACY_SCALAR_RE")
