# SPDX-License-Identifier: Apache-2.0
"""Tests for the page rename primitive (issue athenaeum#2488).

Three acceptance-critical shapes, matching the issue's own AC list:

- rename + alias (``TestBuildRenamePreview``/``TestApplyRename``): ``name:``
  changes, the previous name lands in ``aliases:`` (deduplicated).
- rename idempotence (``TestRenameIdempotence``): renaming to the page's
  CURRENT name is a clean no-op, at both the library and CLI layer, whether
  or not an earlier call already applied it.
- compile-does-not-revert (``TestCompileDoesNotRevertRenamedName``): the
  sharpest AC — a real, deterministic compile path
  (:func:`athenaeum.librarian.tier0_handle_upsert`) run against a page this
  module already renamed must not revert ``name:`` back to the pre-rename
  value, whether the compile path resolves the page by uid or by its
  now-aliased old name.

Also covers the ``--reslug`` path (file move + inbound-wikilink rewrite,
reusing :mod:`athenaeum.pending_merges`'s fold helpers — not reimplementing
them) and the provenance ledger (who/when/reason/old->new).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import cli
from athenaeum.entity_rename import (
    apply_rename,
    build_rename_preview,
    read_rename_provenance,
    record_rename_provenance,
)
from athenaeum.models import parse_frontmatter


def _write_page(wiki: Path, filename: str, frontmatter: str, body: str = "\nBody text.\n") -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / filename
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return path


class TestBuildRenamePreview:
    def test_unknown_uid_is_an_error(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        preview = build_rename_preview(wiki, "nope", "New Name")
        assert preview.error is not None
        assert "nope" in preview.error

    def test_empty_name_is_an_error(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")
        preview = build_rename_preview(wiki, "abc", "   ")
        assert preview.error is not None
        assert "non-empty" in preview.error

    def test_basic_rename_plan(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")
        preview = build_rename_preview(wiki, "abc", "Acme Project")
        assert preview.error is None
        assert preview.noop is False
        assert preview.old_name == "acme"
        assert preview.new_name == "Acme Project"
        assert preview.aliases_to_add == ["acme"]
        meta, _ = parse_frontmatter(preview.rewritten_text)
        assert meta["name"] == "Acme Project"
        assert meta["aliases"] == ["acme"]
        # Default (no --reslug): file path untouched.
        assert preview.new_page_path == preview.page_path

    def test_rename_to_current_name_is_noop(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")
        preview = build_rename_preview(wiki, "abc", "acme")
        assert preview.error is None
        assert preview.noop is True

    def test_reslug_collision_with_different_page_is_an_error(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")
        _write_page(wiki, "other-page.md", "uid: def\ntype: concept\nname: Other Page\n")
        preview = build_rename_preview(wiki, "abc", "Other Page", reslug=True)
        assert preview.error is not None
        assert "other-page.md" in preview.error

    def test_existing_aliases_are_preserved_and_deduped(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(
            wiki,
            "acme.md",
            "uid: abc\ntype: company\nname: acme\naliases:\n- ACME Inc\n",
        )
        preview = build_rename_preview(wiki, "abc", "Acme Project")
        meta, _ = parse_frontmatter(preview.rewritten_text)
        assert meta["aliases"] == ["ACME Inc", "acme"]


class TestApplyRename:
    def test_apply_writes_name_and_alias(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")
        preview = build_rename_preview(wiki, "abc", "Acme Project")
        result = apply_rename(wiki, preview, by="tristan", reason="cleanup")
        assert result.error is None
        assert result.aliases_added == ["acme"]
        assert result.reslugged is False
        meta, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
        assert meta["name"] == "Acme Project"
        assert meta["aliases"] == ["acme"]

    def test_apply_raises_on_error_preview(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        preview = build_rename_preview(wiki, "nope", "X")
        with pytest.raises(ValueError):
            apply_rename(wiki, preview, by="tristan")

    def test_apply_raises_on_noop_preview(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")
        preview = build_rename_preview(wiki, "abc", "acme")
        with pytest.raises(ValueError):
            apply_rename(wiki, preview, by="tristan")

    def test_apply_with_reslug_moves_file_and_rewrites_wikilinks(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")
        other = _write_page(
            wiki,
            "other-page.md",
            "uid: def\ntype: concept\nname: Other Page\n",
            body="\nSee [[acme]] and [[acme|Acme Co]] for details.\n",
        )
        preview = build_rename_preview(wiki, "abc", "Acme Project", reslug=True)
        result = apply_rename(wiki, preview, by="tristan", reason="cleanup")

        assert result.reslugged is True
        assert not (wiki / "acme.md").exists()
        new_page = wiki / "acme-project.md"
        assert new_page.is_file()
        meta, _ = parse_frontmatter(new_page.read_text(encoding="utf-8"))
        assert meta["name"] == "Acme Project"
        assert meta["aliases"] == ["acme"]

        assert result.links_rewritten == 1
        rewritten_other = other.read_text(encoding="utf-8")
        assert "[[acme-project]]" in rewritten_other
        assert "[[acme-project|Acme Co]]" in rewritten_other
        assert "[[acme]]" not in rewritten_other

    def test_apply_without_reslug_leaves_slug_and_links_unchanged(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")
        other = _write_page(
            wiki,
            "other-page.md",
            "uid: def\ntype: concept\nname: Other Page\n",
            body="\nSee [[acme]] for details.\n",
        )
        preview = build_rename_preview(wiki, "abc", "Acme Project", reslug=False)
        result = apply_rename(wiki, preview, by="tristan")

        assert result.reslugged is False
        assert result.links_rewritten == 0
        assert (wiki / "acme.md").is_file()
        assert "[[acme]]" in other.read_text(encoding="utf-8")


class TestRenameProvenance:
    def test_record_and_read_round_trip(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        ok = record_rename_provenance(
            wiki,
            uid="abc",
            old_name="acme",
            new_name="Acme Project",
            by="tristan",
            reason="operator cleanup",
        )
        assert ok is True
        records = read_rename_provenance(wiki)
        assert len(records) == 1
        rec = records[0]
        assert rec["uid"] == "abc"
        assert rec["old_name"] == "acme"
        assert rec["new_name"] == "Acme Project"
        assert rec["by"] == "tristan"
        assert rec["reason"] == "operator cleanup"
        assert "at" in rec and rec["at"]

    def test_apply_rename_writes_provenance(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")
        preview = build_rename_preview(wiki, "abc", "Acme Project")
        apply_rename(wiki, preview, by="tristan", reason="cleanup")
        records = read_rename_provenance(wiki)
        assert len(records) == 1
        assert records[0] == {
            "uid": "abc",
            "old_name": "acme",
            "new_name": "Acme Project",
            "by": "tristan",
            "reason": "cleanup",
            "at": records[0]["at"],
        }

    def test_no_records_file_reads_as_empty(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        assert read_rename_provenance(wiki) == []


class TestRenameIdempotence:
    def test_library_level_second_call_is_noop(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")

        first = build_rename_preview(wiki, "abc", "Acme Project")
        apply_rename(wiki, first, by="tristan", reason="cleanup")

        second = build_rename_preview(wiki, "abc", "Acme Project")
        assert second.noop is True
        # A no-op preview is never applied — a caller that checks `.noop`
        # first (as the CLI does) never calls apply_rename a second time,
        # so there is only ever one provenance record and one alias entry.
        records = read_rename_provenance(wiki)
        assert len(records) == 1
        meta, _ = parse_frontmatter((wiki / "acme.md").read_text(encoding="utf-8"))
        assert meta["aliases"] == ["acme"]

    def test_cli_apply_then_apply_again_same_name_is_clean_noop(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")

        first_exit = cli.main(
            [
                "entity",
                "rename",
                "--uid",
                "abc",
                "--name",
                "Acme Project",
                "--by",
                "tristan",
                "--apply",
                "--path",
                str(tmp_path),
                "--cache-dir",
                str(tmp_path / "_cache"),
            ]
        )
        assert first_exit == 0

        second_exit = cli.main(
            [
                "entity",
                "rename",
                "--uid",
                "abc",
                "--name",
                "Acme Project",
                "--apply",
                "--path",
                str(tmp_path),
                "--cache-dir",
                str(tmp_path / "_cache"),
            ]
        )
        assert second_exit == 0

        captured = capsys.readouterr()
        assert "NO-OP" in captured.out
        meta, _ = parse_frontmatter((wiki / "acme.md").read_text(encoding="utf-8"))
        assert meta["aliases"] == ["acme"]
        records = read_rename_provenance(wiki)
        assert len(records) == 1


class TestCliDryRunAndApply:
    def test_dry_run_default_writes_nothing_and_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", "uid: abc\ntype: company\nname: acme\n")
        before = (wiki / "acme.md").read_text(encoding="utf-8")

        exit_code = cli.main(
            [
                "entity",
                "rename",
                "--uid",
                "abc",
                "--name",
                "Acme Project",
                "--path",
                str(tmp_path),
                "--cache-dir",
                str(tmp_path / "_cache"),
            ]
        )
        assert exit_code == 2
        assert (wiki / "acme.md").read_text(encoding="utf-8") == before
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert read_rename_provenance(wiki) == []

    def test_unknown_uid_via_cli_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        exit_code = cli.main(
            [
                "entity",
                "rename",
                "--uid",
                "nope",
                "--name",
                "X",
                "--path",
                str(tmp_path),
                "--cache-dir",
                str(tmp_path / "_cache"),
            ]
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "nope" in captured.err

    def test_plain_entity_read_without_uid_still_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression guard: adding the nested `rename` sub-action moved
        `--uid`'s required-ness for the (default) read mode out of argparse
        and into cmd_entity's own code — this proves the externally
        observable behavior (exit 2, a `--uid` usage error) is unchanged."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        exit_code = cli.main(["entity", "--path", str(tmp_path)])
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "--uid" in captured.err


class TestCompileDoesNotRevertRenamedName:
    """The sharp AC: a later compile must not revert a renamed `name:`.

    Exercises a real, deterministic, LLM-free compile path
    (:func:`athenaeum.librarian.tier0_handle_upsert`) against a page THIS
    module's own rename primitive already renamed, feeding it a raw seed
    that still carries the page's PRE-rename name — the realistic shape of
    a stale seed/source-handle re-intake arriving after an operator has
    renamed the page. Covers both ways such a seed can resolve to the page:
    by its declared `uid:` (which never looks at `name:` at all) and, with
    no uid declared, by `index.lookup()` matching the OLD name against the
    page's new `aliases:` entry (added by the rename itself).
    """

    def _make_raw(self, tmp_path: Path, content: str):
        from athenaeum.models import RawFile

        raw_dir = tmp_path / "raw" / "contact-wiki"
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / "seed.md"
        path.write_text(content, encoding="utf-8")
        return RawFile(path=path, source="contact-wiki", timestamp="", uuid8="")

    def _renamed_page(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        _write_page(
            wiki,
            "company-x-acme.md",
            "uid: company-x\ntype: company\nname: acme\naccess: internal\n",
            body="\n# acme\n\nBody.\n",
        )
        preview = build_rename_preview(wiki, "company-x", "Acme Project")
        apply_rename(wiki, preview, by="tristan", reason="fix generic short name")
        return wiki

    def test_uid_resolved_seed_does_not_revert_name(self, tmp_path: Path) -> None:
        from athenaeum.librarian import tier0_handle_upsert
        from athenaeum.models import EntityIndex

        wiki = self._renamed_page(tmp_path)
        page_path = wiki / "company-x-acme.md"

        # The seed still names the entity by its OLD (pre-rename) name —
        # exactly what a stale source-handle re-intake would carry — but
        # it DOES declare the uid, so resolution is by uid and never
        # touches `name:` at all.
        raw = self._make_raw(
            tmp_path,
            "---\nuid: company-x\ntype: company\nname: acme\n"
            "domains:\n  - acme.example\n---\n\n# acme\n\nseed\n",
        )
        index = EntityIndex(wiki)
        out = tier0_handle_upsert(raw, index, wiki, ["company"])
        assert out is not None
        entity, changed = out
        assert changed is True

        meta, _ = parse_frontmatter(page_path.read_text(encoding="utf-8"))
        assert meta["name"] == "Acme Project"  # NOT reverted to "acme"
        assert meta["aliases"] == ["acme"]
        assert meta["domains"] == ["acme.example"]

    def test_alias_resolved_seed_does_not_revert_name(self, tmp_path: Path) -> None:
        """Same as above, but the seed declares NO uid — resolved instead by
        `index.lookup("acme")`, which only succeeds because the rename
        itself recorded "acme" as an alias of the renamed page."""
        from athenaeum.librarian import tier0_handle_upsert
        from athenaeum.models import EntityIndex

        wiki = self._renamed_page(tmp_path)
        page_path = wiki / "company-x-acme.md"

        raw = self._make_raw(
            tmp_path,
            "---\ntype: company\nname: acme\n"
            "domains:\n  - acme.example\n---\n\n# acme\n\nseed\n",
        )
        index = EntityIndex(wiki)
        assert index.lookup("acme") is not None  # sanity: resolves via the new alias

        out = tier0_handle_upsert(raw, index, wiki, ["company"])
        assert out is not None
        _, changed = out
        assert changed is True

        meta, _ = parse_frontmatter(page_path.read_text(encoding="utf-8"))
        assert meta["name"] == "Acme Project"  # NOT reverted to "acme"
        assert meta["domains"] == ["acme.example"]
