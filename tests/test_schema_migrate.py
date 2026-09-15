# SPDX-License-Identifier: Apache-2.0
"""``athenaeum schema migrate`` — issue athenaeum#1628 Plan item 4.

Covers: dry-run (the default) leaves a tmp wiki byte-identical; --apply
stamps pending eager rule-based migrations and is idempotent on a second
run; a page whose next pending migration is model-derivation is left
alone; frontmatter-less/empty/unparseable pages are skipped and counted,
never given synthetic frontmatter; and the CLI wiring (dry-run default,
--apply, --dry-run overrides --apply, --json). No test here touches a
live knowledge store.
"""

from __future__ import annotations

import json as json_module
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.models import parse_frontmatter
from athenaeum.schema_migrate import (
    apply_migrations,
    build_migrate_report,
    discover_wiki_pages,
    insert_schema_fields,
)


def _page(root: Path, name: str, frontmatter: str, body: str = "Body text.\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "wiki"
    root.mkdir(parents=True)
    return root


# --- discover_wiki_pages -----------------------------------------------------


class TestDiscoverWikiPages:
    def test_skips_underscore_prefixed_infra_files(self, wiki: Path) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        (wiki / "_pending_questions.md").write_text("infra\n", encoding="utf-8")
        found = discover_wiki_pages(wiki)
        assert [p.name for p in found] == ["a.md"]


# --- build_migrate_report / apply_migrations ---------------------------------


class TestBuildAndApply:
    def test_dry_run_leaves_the_tree_byte_identical(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        before = path.read_text(encoding="utf-8")

        report = build_migrate_report(wiki)
        assert len(report.migrations) == 1

        after = path.read_text(encoding="utf-8")
        assert after == before

    def test_apply_stamps_schema_version_one(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        report = build_migrate_report(wiki)
        changed = apply_migrations(report)
        assert changed == 1

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["schema_version"] == 1

    def test_apply_touches_only_the_frontmatter_block(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A", body="Untouched body.\n")
        report = build_migrate_report(wiki)
        apply_migrations(report)

        text = path.read_text(encoding="utf-8")
        assert text.endswith("---\nUntouched body.\n")
        assert "schema_version: 1" in text

    def test_second_apply_is_idempotent_and_byte_identical(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        report1 = build_migrate_report(wiki)
        apply_migrations(report1)
        after_first = path.read_text(encoding="utf-8")

        report2 = build_migrate_report(wiki)
        # Nothing eager left to apply -- schema_version is now 1, and the
        # only pending migration (v1->v2) is model-derivation.
        assert report2.migrations == []
        changed = apply_migrations(report2)
        assert changed == 0

        after_second = path.read_text(encoding="utf-8")
        assert after_second == after_first

    def test_page_whose_next_pending_migration_is_model_is_left_alone(self, wiki: Path) -> None:
        path = _page(
            wiki, "b.md", "uid: '2'\ntype: concept\nname: B\nschema_version: 1\n"
        )
        before = path.read_text(encoding="utf-8")
        report = build_migrate_report(wiki)
        [outcome] = report.outcomes
        assert outcome.reason == "next-pending-is-model"
        assert outcome.migrated is False

        changed = apply_migrations(report)
        assert changed == 0
        assert path.read_text(encoding="utf-8") == before

    def test_fully_current_page_reports_already_current(self, wiki: Path) -> None:
        _page(wiki, "c.md", "uid: '3'\ntype: concept\nname: C\nschema_version: 2\n")
        report = build_migrate_report(wiki)
        [outcome] = report.outcomes
        assert outcome.reason == "already-current"

    def test_no_frontmatter_page_is_skipped_never_synthesized(self, wiki: Path) -> None:
        path = wiki / "plain.md"
        path.write_text("Just a plain markdown file, no frontmatter.\n", encoding="utf-8")
        report = build_migrate_report(wiki)
        [outcome] = report.outcomes
        assert outcome.reason == "no-frontmatter"

        changed = apply_migrations(report)
        assert changed == 0
        assert path.read_text(encoding="utf-8") == "Just a plain markdown file, no frontmatter.\n"

    def test_concurrent_advance_between_scan_and_apply_is_not_clobbered(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        report = build_migrate_report(wiki)
        # Simulate a concurrent writer landing schema_version: 1 first.
        path.write_text(
            "---\nuid: '1'\ntype: concept\nname: A\nschema_version: 1\n---\nBody text.\n",
            encoding="utf-8",
        )
        changed = apply_migrations(report)
        assert changed == 0


# --- insert_schema_fields -----------------------------------------------------


class TestInsertSchemaFields:
    def test_no_frontmatter_returns_none(self) -> None:
        assert insert_schema_fields("no frontmatter here\n", 1, {}) is None

    def test_inserts_schema_version_line(self) -> None:
        text = "---\nuid: '1'\n---\nBody.\n"
        updated = insert_schema_fields(text, 1, {})
        assert updated == "---\nuid: '1'\nschema_version: 1\n---\nBody.\n"

    def test_inserts_produced_fields_too(self) -> None:
        text = "---\nuid: '1'\n---\nBody.\n"
        updated = insert_schema_fields(text, 1, {"extra": "value"})
        assert updated is not None
        assert "schema_version: 1" in updated
        assert "extra: value" in updated


# --- CLI -----------------------------------------------------------------------


class TestCLI:
    def test_dry_run_default(self, wiki: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        before = (wiki / "a.md").read_text(encoding="utf-8")

        rc = main(["schema", "migrate", "--path", str(wiki.parent)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "dry run: nothing written" in out
        assert (wiki / "a.md").read_text(encoding="utf-8") == before

    def test_apply_writes(self, wiki: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        rc = main(["schema", "migrate", "--path", str(wiki.parent), "--apply"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "applied: 1 file(s) written" in out
        meta, _ = parse_frontmatter((wiki / "a.md").read_text(encoding="utf-8"))
        assert meta["schema_version"] == 1

    def test_apply_is_idempotent_through_the_cli_too(
        self, wiki: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        main(["schema", "migrate", "--path", str(wiki.parent), "--apply"])
        capsys.readouterr()
        after_first = (wiki / "a.md").read_text(encoding="utf-8")

        rc = main(["schema", "migrate", "--path", str(wiki.parent), "--apply"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "applied: 0 file(s) written" in out
        assert (wiki / "a.md").read_text(encoding="utf-8") == after_first

    def test_dry_run_overrides_apply(self, wiki: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        rc = main(["schema", "migrate", "--path", str(wiki.parent), "--apply", "--dry-run"])
        assert rc == 0
        meta, _ = parse_frontmatter((wiki / "a.md").read_text(encoding="utf-8"))
        assert "schema_version" not in meta

    def test_json_output(self, wiki: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        rc = main(["schema", "migrate", "--path", str(wiki.parent), "--json"])
        assert rc == 0
        payload = json_module.loads(capsys.readouterr().out)
        assert payload["scanned"] == 1
        assert payload["migrated"] == 1
        assert payload["applied"] is False

    def test_missing_wiki_dir_errors(self, tmp_path: Path) -> None:
        rc = main(["schema", "migrate", "--path", str(tmp_path / "nope")])
        assert rc == 1
