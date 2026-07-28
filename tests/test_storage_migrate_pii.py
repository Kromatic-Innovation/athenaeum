# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum storage migrate-pii`` (issue #479).

Covers the pure transform (:mod:`athenaeum.storage_migrate`) and the CLI
(:mod:`athenaeum._cmd_storage`), mirroring:
- ``test_pii_off_corpus.py``'s ``EXCLUDED_CONFIG`` + minimal-page conventions,
- ``test_authority_manifest.py``'s dry-run-does-not-mutate / apply-writes pair,
- ``test_outbound_pii.py``'s in-process ``cli.main([...])`` + ``capsys`` style.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.cli import main
from athenaeum.models import parse_frontmatter
from athenaeum.storage import surface_root_for_class
from athenaeum.storage_migrate import (
    INLINE_REDACTION_MARKER,
    iter_entity_pages,
    iter_glob_pages,
    plan_pii_migration,
)

EXCLUDED_CONFIG = {"storage": {"mapping": {"pii": "excluded"}}}


def _write_entity_page(
    wiki_root: Path,
    filename: str = "jane.md",
    *,
    body: str = "Jane leads widgets.",
    extra_frontmatter: str = "",
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / filename
    path.write_text(
        "---\n"
        'uid: "12345"\n'
        "name: Jane Springer\n"
        "type: person\n"
        "linkedin_url: https://linkedin.com/in/janespringer\n"
        "google_contact: people/c99\n"
        "emails:\n"
        "  - jane@example.com\n"
        "phones:\n"
        '  - "+1-555-0100"\n'
        "tags:\n"
        "  - active\n"
        f"{extra_frontmatter}"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def _seed_knowledge_root(tmp_path: Path, *, mapped: bool = True) -> Path:
    """A knowledge root with a live entity page and (optionally) pii->excluded."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    if mapped:
        (root / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
    _write_entity_page(root / "wiki")
    return root


# ---------------------------------------------------------------------------
# Pure transform — plan_pii_migration
# ---------------------------------------------------------------------------


class TestPlanPiiMigration:
    def test_extracts_frontmatter_and_inline_contact_data(self, tmp_path: Path) -> None:
        root = _seed_knowledge_root(tmp_path)
        page = root / "wiki" / "jane.md"
        # Add inline PII (a second email + the phone in prose).
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "Jane leads widgets.",
                "Reach her at jane@example.com or +1-555-0100. Asst: bob@example.com.",
            ),
            encoding="utf-8",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)

        assert plan.changed is True
        # Frontmatter + inline emails, deduped, order-preserving.
        assert plan.emails == ["jane@example.com", "bob@example.com"]
        assert plan.phones == ["+1-555-0100"]
        assert plan.excluded_page_path == surface_root_for_class(
            "pii", EXCLUDED_CONFIG, root
        ) / "jane.md"

    def test_origin_keeps_durable_identifiers_drops_contact_fields(
        self, tmp_path: Path
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        page = root / "wiki" / "jane.md"

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.rewritten_page_text or "")

        # Archival contact fields gone...
        assert "emails" not in meta
        assert "phones" not in meta
        # ...durable identifiers untouched.
        assert meta["uid"] == "12345"
        assert meta["name"] == "Jane Springer"
        assert meta["type"] == "person"
        assert meta["linkedin_url"] == "https://linkedin.com/in/janespringer"
        assert meta["google_contact"] == "people/c99"
        assert meta["tags"] == ["active"]

    def test_inline_tokens_redacted_in_origin_body(self, tmp_path: Path) -> None:
        root = _seed_knowledge_root(tmp_path)
        page = root / "wiki" / "jane.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "Jane leads widgets.", "Email jane@example.com now."
            ),
            encoding="utf-8",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)

        assert "jane@example.com" not in (plan.rewritten_page_text or "")
        assert INLINE_REDACTION_MARKER in (plan.rewritten_page_text or "")

    def test_excluded_record_carries_pii_and_linkage(self, tmp_path: Path) -> None:
        root = _seed_knowledge_root(tmp_path)
        page = root / "wiki" / "jane.md"

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.excluded_page_text or "")

        assert meta["pii"] is True
        assert meta["uid"] == "12345"
        assert meta["contact_of"] == "Jane Springer"
        assert meta["emails"] == ["jane@example.com"]
        assert meta["phones"] == ["+1-555-0100"]

    def test_page_without_contact_data_is_a_noop(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        page = root / "wiki" / "clean.md"
        page.write_text(
            "---\nuid: c1\nname: Clean\ntype: person\n---\nNo contact data here.\n",
            encoding="utf-8",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)

        assert plan.changed is False
        assert plan.emails == []
        assert plan.phones == []
        assert plan.rewritten_page_text is None
        assert plan.excluded_page_text is None

    def test_crm_timeline_dates_are_not_phone_migrations(self, tmp_path: Path) -> None:
        # Issue #500: migrate-pii's phone detector matched CRM-timeline ISO
        # dates and the page's own uid, so a dry-run "found phones" and --apply
        # would strip real dates into the excluded surface as if they were
        # contact PII. Reproduces the two live pages named in #500
        # (`00075741-blekinge-business-incubator-2016.md`, `000a36e4-dawn-b.md`):
        # a page whose only digit runs are timeline dates + an id prefix must
        # report ZERO phone hits and be a no-op migration.
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        page = root / "wiki" / "00075741-blekinge-business-incubator-2016.md"
        page.write_text(
            "---\n"
            "uid: '00075741'\n"
            "name: Blekinge Business Incubator\n"
            "type: organization\n"
            "created: 2015-12-03\n"
            "updated: 2026-04-16\n"
            "ga4_property_id: '387473359'\n"
            "---\n"
            "## CRM Timeline\n"
            "- First contact: 2015-12-03\n"
            "- Last CRM update: 2021-07-16\n"
            "- Last email: 2016-03-14\n",
            encoding="utf-8",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)

        assert plan.phones == []  # no dates/ids misread as phones
        assert plan.changed is False  # nothing to migrate — a true no-op
        assert plan.rewritten_page_text is None
        assert plan.excluded_page_text is None


# ---------------------------------------------------------------------------
# CLI — athenaeum storage migrate-pii
# ---------------------------------------------------------------------------


class TestStorageMigratePiiCLI:
    def test_dry_run_does_not_mutate(self, tmp_path: Path, capsys) -> None:
        root = _seed_knowledge_root(tmp_path)
        page = root / "wiki" / "jane.md"
        before = page.read_text(encoding="utf-8")

        rc = main(["storage", "migrate-pii", "--path", str(root), "--page", str(page)])

        assert rc == 0
        assert page.read_text(encoding="utf-8") == before  # unchanged
        assert not (root / "excluded").exists()  # nothing written
        out = capsys.readouterr().out
        assert "jane@example.com" in out  # preview shows the extracted data

    def test_apply_writes_both_pages_and_scrubs_origin(
        self, tmp_path: Path
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        page = root / "wiki" / "jane.md"

        rc = main(
            ["storage", "migrate-pii", "--path", str(root), "--page", str(page), "--apply"]
        )

        assert rc == 0
        origin = page.read_text(encoding="utf-8")
        assert "jane@example.com" not in origin
        assert "+1-555-0100" not in origin

        excluded_page = surface_root_for_class("pii", EXCLUDED_CONFIG, root) / "jane.md"
        assert excluded_page.is_file()
        record = excluded_page.read_text(encoding="utf-8")
        assert "jane@example.com" in record

    def test_apply_is_idempotent(self, tmp_path: Path, capsys) -> None:
        root = _seed_knowledge_root(tmp_path)
        page = root / "wiki" / "jane.md"
        argv = ["storage", "migrate-pii", "--path", str(root), "--page", str(page), "--apply"]

        assert main(argv) == 0
        capsys.readouterr()
        assert main(argv) == 0  # second run: no contact data left
        assert "nothing to migrate" in capsys.readouterr().out

    def test_apply_refused_when_pii_not_mapped_to_excluded(
        self, tmp_path: Path, capsys
    ) -> None:
        root = _seed_knowledge_root(tmp_path, mapped=False)  # no storage.mapping.pii
        page = root / "wiki" / "jane.md"
        before = page.read_text(encoding="utf-8")

        rc = main(
            ["storage", "migrate-pii", "--path", str(root), "--page", str(page), "--apply"]
        )

        assert rc == 1  # refused, not a silent leak
        assert page.read_text(encoding="utf-8") == before  # untouched
        err = capsys.readouterr().err
        assert "excluded surface" in err

    def test_missing_page_errors(self, tmp_path: Path) -> None:
        root = _seed_knowledge_root(tmp_path)
        rc = main(
            [
                "storage",
                "migrate-pii",
                "--path",
                str(root),
                "--page",
                str(root / "wiki" / "nope.md"),
            ]
        )
        assert rc == 1

    def test_bare_storage_command_prints_usage(self, capsys) -> None:
        rc = main(["storage"])
        assert rc == 2
        assert "migrate-pii" in capsys.readouterr().err
        # lint-pii is now advertised in the usage line too (issue #495).
        rc = main(["storage"])
        assert "lint-pii" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Bulk migration target-set resolution (issue #495)
# ---------------------------------------------------------------------------


class TestBulkTargetSet:
    def test_iter_entity_pages_skips_underscore_and_subdirs(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        (wiki / "sub").mkdir(parents=True)
        (wiki / "alice.md").write_text("x", encoding="utf-8")
        (wiki / "bob.md").write_text("x", encoding="utf-8")
        (wiki / "_index.md").write_text("x", encoding="utf-8")  # queue/index — skip
        (wiki / "_pending_merges.md").write_text("x", encoding="utf-8")  # skip
        (wiki / "notes.txt").write_text("x", encoding="utf-8")  # non-md — skip
        (wiki / "sub" / "nested.md").write_text("x", encoding="utf-8")  # not top-level

        names = [p.name for p in iter_entity_pages(wiki)]

        assert names == ["alice.md", "bob.md"]  # sorted, top-level, non-underscore

    def test_iter_entity_pages_missing_root_is_empty(self, tmp_path: Path) -> None:
        assert list(iter_entity_pages(tmp_path / "nope")) == []

    def test_iter_glob_pages_matches_named_files_only(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_pending_merges_archive.md").write_text("x", encoding="utf-8")
        (wiki / "_pending_questions_archive.md").write_text("x", encoding="utf-8")
        (wiki / "jane.md").write_text("x", encoding="utf-8")

        names = [p.name for p in iter_glob_pages(wiki, "_*_archive.md")]

        assert names == ["_pending_merges_archive.md", "_pending_questions_archive.md"]


# ---------------------------------------------------------------------------
# Bulk migration CLI — athenaeum storage migrate-pii --all / --glob (issue #495)
# ---------------------------------------------------------------------------


def _seed_bulk_root(tmp_path: Path, *, mapped: bool = True, n_dirty: int = 3) -> Path:
    """A knowledge root with *n_dirty* PII-carrying entity pages + clean pages."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    if mapped:
        (root / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
    for i in range(n_dirty):
        _write_entity_page(root / "wiki", filename=f"person{i}.md")
    # A clean entity page (no contact data) and a _-prefixed queue file that the
    # entity-page bulk migration must NOT touch.
    (root / "wiki" / "clean.md").write_text(
        "---\nuid: c\nname: Clean\ntype: person\n---\nNothing here.\n", encoding="utf-8"
    )
    (root / "wiki" / "_pending_merges.md").write_text(
        "Draft body with dave@example.com inline.\n", encoding="utf-8"
    )
    return root


class TestBulkMigrateCLI:
    def test_all_dry_run_summarizes_and_writes_nothing(
        self, tmp_path: Path, capsys
    ) -> None:
        root = _seed_bulk_root(tmp_path, n_dirty=3)
        before = (root / "wiki" / "person0.md").read_text(encoding="utf-8")

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all"])

        assert rc == 0
        # Nothing written: origin unchanged, no excluded surface.
        assert (root / "wiki" / "person0.md").read_text(encoding="utf-8") == before
        assert not (root / "excluded").exists()
        out = capsys.readouterr().out
        assert "would migrate 3 page(s)" in out  # summary, not per-page diffs

    def test_all_apply_migrates_every_dirty_entity_page(self, tmp_path: Path) -> None:
        root = _seed_bulk_root(tmp_path, n_dirty=3)

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all", "--apply"])

        assert rc == 0
        excluded_root = surface_root_for_class("pii", EXCLUDED_CONFIG, root)
        for i in range(3):
            origin = (root / "wiki" / f"person{i}.md").read_text(encoding="utf-8")
            assert "jane@example.com" not in origin
            assert "+1-555-0100" not in origin
            assert (excluded_root / f"person{i}.md").is_file()

    def test_all_apply_leaves_underscore_queue_file_untouched(
        self, tmp_path: Path
    ) -> None:
        # The entity-page bulk path must NOT rewrite _-prefixed queue/index
        # files — those need per-file-kind operator decisions and are covered
        # by the corpus-wide lint, not the entity-page transform.
        root = _seed_bulk_root(tmp_path, n_dirty=1)
        queue = root / "wiki" / "_pending_merges.md"
        before = queue.read_text(encoding="utf-8")

        main(["storage", "migrate-pii", "--path", str(root), "--all", "--apply"])

        assert queue.read_text(encoding="utf-8") == before

    def test_all_apply_is_idempotent_and_resumable(
        self, tmp_path: Path, capsys
    ) -> None:
        root = _seed_bulk_root(tmp_path, n_dirty=3)
        argv = ["storage", "migrate-pii", "--path", str(root), "--all", "--apply"]

        assert main(argv) == 0
        capsys.readouterr()
        # Re-run (models the resume-after-crash case): already-migrated pages
        # carry no contact data, so the second run migrates nothing and writes
        # no duplicate records.
        assert main(argv) == 0
        assert "migrated 0 page(s)" in capsys.readouterr().out

    def test_resume_after_partial_completes_remaining_pages(
        self, tmp_path: Path
    ) -> None:
        root = _seed_bulk_root(tmp_path, n_dirty=3)
        # Simulate a run that only reached person0 before dying: migrate it
        # via the single-page path, then let bulk pick up the rest.
        main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--page", str(root / "wiki" / "person0.md"), "--apply",
            ]
        )

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all", "--apply"])

        assert rc == 0
        excluded_root = surface_root_for_class("pii", EXCLUDED_CONFIG, root)
        for i in range(3):
            origin = (root / "wiki" / f"person{i}.md").read_text(encoding="utf-8")
            assert "jane@example.com" not in origin
            assert (excluded_root / f"person{i}.md").is_file()

    def test_all_apply_refused_when_pii_not_mapped(
        self, tmp_path: Path, capsys
    ) -> None:
        root = _seed_bulk_root(tmp_path, mapped=False, n_dirty=2)
        before = (root / "wiki" / "person0.md").read_text(encoding="utf-8")

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all", "--apply"])

        assert rc == 1
        assert (root / "wiki" / "person0.md").read_text(encoding="utf-8") == before
        assert "excluded surface" in capsys.readouterr().err

    def test_all_dry_run_on_unmapped_base_warns_but_previews(
        self, tmp_path: Path, capsys
    ) -> None:
        root = _seed_bulk_root(tmp_path, mapped=False, n_dirty=2)

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all"])

        assert rc == 0  # dry-run still previews on an unconfigured base
        err = capsys.readouterr().err
        assert "not mapped to an excluded surface" in err

    def test_glob_targets_named_file(self, tmp_path: Path) -> None:
        root = _seed_bulk_root(tmp_path, n_dirty=1)
        # An operator explicitly redacting one queue file in place.
        rc = main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--glob", "_pending_merges.md", "--apply",
            ]
        )
        assert rc == 0
        assert "dave@example.com" not in (
            root / "wiki" / "_pending_merges.md"
        ).read_text(encoding="utf-8")

    def test_target_selector_is_required(self, capsys) -> None:
        # argparse mutually-exclusive required group: none of --page/--all/--glob
        # is a usage error (exit 2 via SystemExit).
        import pytest

        with pytest.raises(SystemExit):
            main(["storage", "migrate-pii", "--path", "/tmp/x"])
