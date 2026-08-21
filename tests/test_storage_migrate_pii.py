# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum storage migrate-pii`` (issue athenaeum#479).

Covers the pure transform (:mod:`athenaeum.storage_migrate`) and the CLI
(:mod:`athenaeum._cmd_storage`), mirroring:
- ``test_pii_off_corpus.py``'s ``EXCLUDED_CONFIG`` + minimal-page conventions,
- ``test_authority_manifest.py``'s dry-run-does-not-mutate / apply-writes pair,
- ``test_outbound_pii.py``'s in-process ``cli.main([...])`` + ``capsys`` style.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.config import load_config
from athenaeum.librarian import reindex
from athenaeum.models import parse_frontmatter
from athenaeum.pii import is_service_address
from athenaeum.search import query_fts5_index
from athenaeum.storage import surface_root_for_class
from athenaeum.storage_migrate import (
    INLINE_REDACTION_MARKER,
    apply_name_email_rename,
    bulk_rename_name_email_pages,
    iter_entity_pages,
    iter_glob_pages,
    plan_name_email_rename,
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
        # Issue athenaeum#500: migrate-pii's phone detector matched CRM-timeline ISO
        # dates and the page's own uid, so a dry-run "found phones" and --apply
        # would strip real dates into the excluded surface as if they were
        # contact PII. Reproduces the two live pages named in athenaeum#500
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
        # lint-pii is now advertised in the usage line too (issue athenaeum#495).
        rc = main(["storage"])
        assert "lint-pii" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Bulk migration target-set resolution (issue athenaeum#495)
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
# Bulk migration CLI — athenaeum storage migrate-pii --all / --glob (issue athenaeum#495)
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


# ---------------------------------------------------------------------------
# Detector-driven key coverage — the athenaeum#502 residual key shapes
# ---------------------------------------------------------------------------
#
# athenaeum#479 read only ``emails:`` / ``phones:``; the live sweep left 690 pages whose
# PII lives in OTHER frontmatter keys (``aliases:`` dominant, then ``source:``,
# ``former_emails:``, ``alt_emails:``) and in body prose. The migrator now
# detector-scans every non-durable frontmatter value; these pin each residual
# shape athenaeum#502 measured, plus the durable-identifier preservation contract (athenaeum#427)
# and the name-is-an-email carve-out.


def _write_page(wiki_root: Path, filename: str, frontmatter: str, body: str) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / filename
    path.write_text(f"---\n{frontmatter}---\n{body}\n", encoding="utf-8")
    return path


class TestDetectorDrivenKeyCoverage:
    def test_migrates_email_in_aliases_preserving_real_aliases(
        self, tmp_path: Path
    ) -> None:
        # aliases:86 is the dominant residual — an email recorded AS an alias
        # (a matching key, so directly reachable by name resolution/recall).
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "dana.md",
            "uid: d1\n"
            "name: Dana Example\n"
            "type: person\n"
            "aliases:\n"
            "  - Dana E.\n"
            "  - dana.example@corp.example\n",
            "Dana leads sales.",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.rewritten_page_text or "")

        assert plan.changed is True
        assert plan.emails == ["dana.example@corp.example"]
        # The real, non-PII alias survives; the email alias is gone.
        assert meta["aliases"] == ["Dana E."]
        assert "dana.example@corp.example" not in (plan.rewritten_page_text or "")
        # ...and it is archived on the excluded record.
        assert "dana.example@corp.example" in (plan.excluded_page_text or "")

    def test_migrates_former_emails_and_alt_emails(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "eve.md",
            "uid: e1\n"
            "name: Eve Example\n"
            "type: person\n"
            "former_emails:\n"
            "  - old@acme.example\n"
            "alt_emails:\n"
            "  - alt@acme.example\n",
            "Eve.",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.rewritten_page_text or "")

        assert plan.emails == ["old@acme.example", "alt@acme.example"]
        # Pure-contact keys are dropped from the origin entirely.
        assert "former_emails" not in meta
        assert "alt_emails" not in meta
        record_meta, _ = parse_frontmatter(plan.excluded_page_text or "")
        assert record_meta["emails"] == ["old@acme.example", "alt@acme.example"]

    def test_migrates_email_in_source_redacts_in_place(self, tmp_path: Path) -> None:
        # source:6 — a provenance STRING that embeds an address. The non-PII
        # context must survive (redact in place), not drop the whole field.
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "finn.md",
            "uid: f1\n"
            "name: Finn Example\n"
            "type: person\n"
            'source: "Streak import 2016 via founder@acme.example"\n',
            "Finn.",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.rewritten_page_text or "")

        assert plan.emails == ["founder@acme.example"]
        # Field kept, email redacted in place, provenance context preserved.
        assert "source" in meta
        assert "founder@acme.example" not in meta["source"]
        assert INLINE_REDACTION_MARKER in meta["source"]
        assert "Streak import 2016" in meta["source"]

    def test_preserves_durable_identifiers_even_when_email_shaped(
        self, tmp_path: Path
    ) -> None:
        # athenaeum#427: durable identifiers (linkedin_url, handles_verified, record IDs,
        # google_contact*) are PRESERVED verbatim even if a value is email-
        # shaped, and are never pulled onto the excluded contact record.
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "gwen.md",
            "uid: g1\n"
            "name: Gwen Example\n"
            "type: person\n"
            "linkedin_url: https://linkedin.com/in/gwen\n"
            "google_contact_kromatic: people/c42\n"
            "handles_verified:\n"
            "  - handle@social.example\n"
            "aliases:\n"
            "  - real.migrate@corp.example\n",
            "Gwen.",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.rewritten_page_text or "")

        # Only the alias email is migrated; the durable-field email is NOT.
        assert plan.emails == ["real.migrate@corp.example"]
        assert "handle@social.example" not in plan.emails
        # Durable fields preserved verbatim on the origin page.
        assert meta["linkedin_url"] == "https://linkedin.com/in/gwen"
        assert meta["google_contact_kromatic"] == "people/c42"
        assert meta["handles_verified"] == ["handle@social.example"]
        # ...and the durable-field email never leaks onto the excluded record.
        assert "handle@social.example" not in (plan.excluded_page_text or "")

    def test_name_is_email_page_excluded_from_automatic_path(
        self, tmp_path: Path
    ) -> None:
        # ~80 pages are NAMED after an email (Streak email-only import). Renaming
        # breaks slugs/edges — so a page whose ONLY PII is in name: is a NO-OP
        # here, flagged for the separate slice, never silently renamed.
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "person-at-streak.md",
            "uid: h1\nname: person@streak.example\ntype: person\n",
            "Contact-only record.",
        )
        before = page.read_text(encoding="utf-8")

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)

        assert plan.changed is False  # not migrated
        assert plan.name_field_pii is True  # but flagged for the follow-up slice
        assert plan.rewritten_page_text is None
        assert page.read_text(encoding="utf-8") == before  # never renamed

    def test_name_is_email_with_alias_keeps_name_migrates_alias(
        self, tmp_path: Path
    ) -> None:
        # A page can be BOTH named after an email AND carry a migratable alias.
        # The alias migrates; the name: is preserved untouched (not renamed).
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "ivy.md",
            "uid: i1\n"
            "name: ivy@streak.example\n"
            "type: person\n"
            "aliases:\n"
            "  - ivy.real@corp.example\n",
            "Ivy.",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.rewritten_page_text or "")

        assert plan.changed is True
        assert plan.name_field_pii is True
        # The alias is migrated; the name-email is NOT treated as migrated PII.
        assert plan.emails == ["ivy.real@corp.example"]
        assert "ivy@streak.example" not in plan.emails
        # name: preserved verbatim (no rename), aliases scrubbed.
        assert meta["name"] == "ivy@streak.example"
        assert "aliases" not in meta

    def test_body_prose_email_on_entity_page_is_redacted(self, tmp_path: Path) -> None:
        # AC: body-text redaction covers entity pages. ~113/300 sampled pages
        # carry the address in prose only.
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "jack.md",
            "uid: j1\nname: Jack Example\ntype: person\n",
            "Reach Jack at jack.prose@corp.example for intros.",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)

        assert plan.changed is True
        assert plan.emails == ["jack.prose@corp.example"]
        assert "jack.prose@corp.example" not in (plan.rewritten_page_text or "")
        assert INLINE_REDACTION_MARKER in (plan.rewritten_page_text or "")


class TestNestedFrontmatterCoverage:
    """Issue athenaeum#507 — recurse into nested lists/dicts, targeting the exact leaf.

    The athenaeum#502 sweep scanned only the top level of each frontmatter value, so PII
    inside a *list of dicts* (``sources[].claim`` provenance blocks,
    ``apollo_employment_history[].title`` enrichment payloads) was invisible to
    the migrator. These pin the recursive walk, the leaf-precise rewrite, and
    the service-address carve-out.
    """

    def test_migrates_email_in_sources_claim_preserving_provenance_block(
        self, tmp_path: Path
    ) -> None:
        # sources[].claim — an auto-memory provenance block. The address is
        # redacted IN PLACE; the block's session/scope/date survive byte-identical.
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "auto-calendar-priya.md",
            "uid: n1\n"
            "name: Auto Calendar\n"
            "type: memory\n"
            "sources:\n"
            "  - session: sess-2026-07-01\n"
            "    scope: work\n"
            "    claim: Reached Priya at priya@example.com and p.raman@example.org\n",
            "Auto-memory page.",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.rewritten_page_text or "")

        assert plan.changed is True
        assert plan.emails == ["priya@example.com", "p.raman@example.org"]
        block = meta["sources"][0]
        # Surrounding provenance fields survive untouched...
        assert block["session"] == "sess-2026-07-01"
        assert block["scope"] == "work"
        # ...only the address in `claim` is replaced (redacted in place).
        assert "priya@example.com" not in block["claim"]
        assert "p.raman@example.org" not in block["claim"]
        assert INLINE_REDACTION_MARKER in block["claim"]
        assert "Reached Priya at" in block["claim"]
        # ...and both addresses are archived on the excluded record.
        assert "priya@example.com" in (plan.excluded_page_text or "")
        assert "p.raman@example.org" in (plan.excluded_page_text or "")

    def test_migrates_email_in_apollo_employment_history_title(
        self, tmp_path: Path
    ) -> None:
        # apollo_employment_history[].title — an address pasted into a job-title
        # field upstream. The leaf is scrubbed; the rest of the entry survives.
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "ad1b80ad-jordan-reyes.md",
            "uid: n2\n"
            "name: Jordan Reyes\n"
            "type: person\n"
            "apollo_employment_history:\n"
            "  - organization: Acme Corp\n"
            "    title: Jordan@example.com\n"
            "    start_date: 2015\n",
            "Enriched via Apollo.",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.rewritten_page_text or "")

        assert plan.changed is True
        assert plan.emails == ["Jordan@example.com"]
        entry = meta["apollo_employment_history"][0]
        # The rest of the employment entry is byte-identical...
        assert entry["organization"] == "Acme Corp"
        assert entry["start_date"] == 2015
        # ...and the address no longer appears anywhere on the origin page.
        assert "Jordan@example.com" not in (plan.rewritten_page_text or "")
        # ...but is archived on the excluded record.
        assert "Jordan@example.com" in (plan.excluded_page_text or "")

    def test_service_address_is_not_migrated(self, tmp_path: Path) -> None:
        # AC: a service identifier (git@github.com) is email-shaped but NOT
        # contact data — migrating it would break a clone URL. It is left in
        # place, and a page carrying ONLY service addresses is a no-op.
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "auto-cwc-git-ssh.md",
            "uid: n3\n"
            "name: CWC Git SSH\n"
            "type: memory\n"
            "sources:\n"
            "  - session: sess-cwc\n"
            "    claim: clone with git@github.com over SSH\n"
            "calendar: standup@group.calendar.google.com\n",
            "Repo access note.",
        )
        before = page.read_text(encoding="utf-8")

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)

        assert plan.changed is False  # nothing migratable
        assert plan.emails == []
        assert plan.rewritten_page_text is None
        assert page.read_text(encoding="utf-8") == before  # untouched

    def test_service_and_real_address_side_by_side_migrates_only_real(
        self, tmp_path: Path
    ) -> None:
        # A claim carrying BOTH a service address and a real one: only the real
        # address is redacted; the service identifier survives byte-identical.
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "mixed.md",
            "uid: n4\n"
            "name: Mixed\n"
            "type: memory\n"
            "sources:\n"
            "  - claim: mailed founder@acme.example after cloning git@github.com\n",
            "Mixed note.",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.rewritten_page_text or "")
        claim = meta["sources"][0]["claim"]

        assert plan.emails == ["founder@acme.example"]
        assert "founder@acme.example" not in claim
        assert "git@github.com" in claim  # service address preserved in place
        assert INLINE_REDACTION_MARKER in claim

    def test_arbitrary_nesting_depth_is_reached(self, tmp_path: Path) -> None:
        # Not just one-level list-of-dicts: an address buried several levels deep
        # (list > dict > list > dict > string) must still be detected/redacted.
        root = tmp_path / "knowledge"
        page = _write_page(
            root / "wiki",
            "deep.md",
            "uid: n5\n"
            "name: Deep Nest\n"
            "type: memory\n"
            "layers:\n"
            "  - inner:\n"
            "      - leaf:\n"
            "          claim: buried deep@corp.example far down\n"
            "          label: keep-me\n",
            "Deeply nested.",
        )

        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        meta, _body = parse_frontmatter(plan.rewritten_page_text or "")

        assert plan.changed is True
        assert plan.emails == ["deep@corp.example"]
        leaf = meta["layers"][0]["inner"][0]["leaf"]
        # The sibling leaf at the same depth is untouched...
        assert leaf["label"] == "keep-me"
        # ...the buried address is redacted in place...
        assert "deep@corp.example" not in leaf["claim"]
        assert INLINE_REDACTION_MARKER in leaf["claim"]
        assert "buried" in leaf["claim"] and "far down" in leaf["claim"]
        # ...and archived on the excluded record.
        assert "deep@corp.example" in (plan.excluded_page_text or "")


class TestServiceAddressPredicate:
    """Issue athenaeum#507 — the explicit, named service-address carve-out."""

    def test_git_ssh_pseudo_user_is_a_service_address(self) -> None:
        assert is_service_address("git@github.com") is True
        assert is_service_address("git@gitlab.com") is True
        # case-insensitive on the whole address
        assert is_service_address("GIT@GitHub.com") is True

    def test_calendar_group_domain_is_a_service_address(self) -> None:
        assert (
            is_service_address("abc123@group.calendar.google.com") is True
        )

    def test_a_real_contact_address_is_not_a_service_address(self) -> None:
        assert is_service_address("founder@acme.example") is False
        assert is_service_address("realbook@kromatic.com") is False


class TestBulkSurfacesNameIsEmailPopulation:
    def test_bulk_dry_run_reports_excluded_name_population(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "knowledge"
        _write_page(
            root / "wiki",
            "named-email.md",
            "uid: k1\nname: k@streak.example\ntype: person\n",
            "Contact-only.",
        )
        (root / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "1 page(s) are named after an email" in out


# ---------------------------------------------------------------------------
# Search-index invalidation after --apply (issue athenaeum#502 comment scope addition)
# ---------------------------------------------------------------------------
#
# --apply rewrites the markdown but does NOT itself touch the search index, so
# the pre-migration text stays recallable until a reindex runs. These pin the
# AC that would have caught the hole: a migrated page's contact data is
# unreachable THROUGH the configured search backend (fts5 indexes the aliases:
# column — the dominant residual), not merely absent from the markdown.


def _seed_indexable_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    (root / "athenaeum.yaml").write_text(
        "storage:\n  mapping:\n    pii: excluded\nsearch_backend: fts5\n",
        encoding="utf-8",
    )
    # A distinctive local-part in aliases: (an INDEXED fts5 column) so a MATCH
    # on the token is a clean reachable/unreachable probe. Flow style — the
    # fts5 frontmatter scanner reads inline ``aliases: [...]`` into its indexed
    # column (block-style list items land on separate lines it doesn't parse).
    _write_page(
        root / "wiki",
        "luna.md",
        "uid: l1\nname: Luna Example\ntype: person\n"
        "aliases: [zzuniquehandle@corp.example]\n",
        "Luna leads research.",
    )
    return root


class TestMigratePiiSearchIndex:
    def test_migrated_alias_unreachable_through_search_after_reindex(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        root = _seed_indexable_root(tmp_path)
        cache = tmp_path / "cache"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))

        # Baseline: build the index, confirm the alias email IS reachable.
        reindex(root, config=load_config(root))
        assert query_fts5_index("zzuniquehandle", cache, n=5)  # reachable

        # Migrate + reindex in one shot.
        rc = main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--all", "--apply", "--reindex",
            ]
        )
        assert rc == 0

        # Now unreachable through the backend — not merely absent from markdown.
        assert query_fts5_index("zzuniquehandle", cache, n=5) == []
        # ...but archived off-corpus on the excluded (never-indexed) surface.
        excluded = surface_root_for_class("pii", EXCLUDED_CONFIG, root) / "luna.md"
        assert "zzuniquehandle@corp.example" in excluded.read_text(encoding="utf-8")

    def test_apply_without_reindex_warns_index_still_dirty(
        self, tmp_path: Path, capsys
    ) -> None:
        # AC: --apply must NOT print an unqualified success while the data is
        # still recallable — it must instruct a reindex.
        root = _seed_indexable_root(tmp_path)

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all", "--apply"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "still carries the pre-migration page text" in err
        assert "athenaeum reindex" in err


# ---------------------------------------------------------------------------
# Name-is-an-email rename migration (issue athenaeum#505 — the athenaeum#502 carve-out's slice)
# ---------------------------------------------------------------------------
#
# APPROACH 1 (operator decision): derive a display name from the local-part
# with a confidence gate, rename the page, move the address to the excluded
# contact record, and rewrite inbound [[wikilink]] edges. Ambiguous
# local-parts are DEFERRED (left unrenamed), never guessed at.


def _write_name_email_page(
    wiki_root: Path,
    filename: str,
    *,
    uid: str,
    email: str,
    name_field: str = "name",
    body: str = "Contact-only record from the Streak import.",
    extra_frontmatter: str = "",
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / filename
    path.write_text(
        "---\n"
        f"uid: {uid}\n"
        f"{name_field}: {email}\n"
        "type: person\n"
        f"{extra_frontmatter}"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


class TestPlanNameEmailRename:
    def test_confident_local_part_derives_display_name(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="z1", email="jane.doe@acme.example"
        )

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)

        assert plan.confident is True
        assert plan.email == "jane.doe@acme.example"
        assert plan.display_name == "Jane Doe"
        assert plan.new_slug == "jane-doe"
        assert plan.new_filename == "jane-doe.md"
        assert plan.new_page_path == page.with_name("jane-doe.md")

    def test_ambiguous_role_address_is_deferred(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki", "info-at-acme.md", uid="z2", email="info@acme.example"
        )

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)

        assert plan.confident is False
        assert plan.email == "info@acme.example"
        assert plan.new_page_path is None
        assert plan.deferred_reason  # a human-readable reason is populated

    def test_ambiguous_initial_blob_is_deferred(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki", "jdoe-at-acme.md", uid="z3", email="jdoe@acme.example"
        )

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)

        assert plan.confident is False

    def test_ambiguous_plus_tag_is_deferred(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki",
            "tagged.md",
            uid="z4",
            email="first.last+tag@acme.example",
        )

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)

        assert plan.confident is False

    def test_ambiguous_numeric_local_part_is_deferred(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki", "numeric.md", uid="z5", email="12345@acme.example"
        )

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)

        assert plan.confident is False

    def test_preferred_name_is_email_handled_same_as_name(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki",
            "pref.md",
            uid="z6",
            email="mary.jane@acme.example",
            name_field="preferred_name",
            extra_frontmatter="name: Legacy Record\n",
        )

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)

        assert plan.confident is True
        assert plan.display_name == "Mary Jane"
        meta, _ = parse_frontmatter(plan.rewritten_page_text or "")
        assert meta["preferred_name"] == "Mary Jane"
        # The other name field is untouched.
        assert meta["name"] == "Legacy Record"

    def test_non_name_email_page_is_unplanned(self, tmp_path: Path) -> None:
        root = _seed_knowledge_root(tmp_path)
        page = root / "wiki" / "jane.md"

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)

        assert plan.email == ""
        assert plan.confident is False

    def test_excluded_record_carries_the_address(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="z7", email="jane.doe@acme.example"
        )

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)
        meta, _ = parse_frontmatter(plan.excluded_page_text or "")

        assert meta["pii"] is True
        assert meta["emails"] == ["jane.doe@acme.example"]
        assert plan.excluded_page_path == surface_root_for_class(
            "pii", EXCLUDED_CONFIG, root
        ) / "jane-doe.md"

    def test_renamed_page_carries_old_slug_and_local_part_as_aliases(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="z8", email="jane.doe@acme.example"
        )

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)
        meta, _ = parse_frontmatter(plan.rewritten_page_text or "")

        # The old slug (filename stem, slugified) and the raw local-part are
        # both recorded so a `[[jane.doe-at-acme]]` wikilink (slugified to
        # match) or an `[[jane.doe]]` alias lookup keep resolving post-rename.
        assert "jane-doe-at-acme" in meta["aliases"]
        assert "jane.doe" in meta["aliases"]


class TestApplyNameEmailRename:
    def test_confident_rename_writes_new_file_and_removes_old(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="z9", email="jane.doe@acme.example"
        )

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)
        apply_name_email_rename(plan, root / "wiki")

        assert not page.exists()
        new_path = root / "wiki" / "jane-doe.md"
        assert new_path.is_file()
        meta, _ = parse_frontmatter(new_path.read_text(encoding="utf-8"))
        assert meta["name"] == "Jane Doe"
        assert "jane.doe@acme.example" not in new_path.read_text(encoding="utf-8")

        excluded = surface_root_for_class("pii", EXCLUDED_CONFIG, root) / "jane-doe.md"
        assert excluded.is_file()
        assert "jane.doe@acme.example" in excluded.read_text(encoding="utf-8")

    def test_inbound_wikilink_from_another_page_is_rewritten_no_dangling_ref(
        self, tmp_path: Path
    ) -> None:
        # AC: an inbound [[related:]] edge from another page must be rewritten
        # to the new slug — no dangling reference remains.
        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="z10", email="jane.doe@acme.example"
        )
        referrer = root / "wiki" / "referrer.md"
        referrer.write_text(
            "---\nuid: r1\nname: Referrer\ntype: person\n---\n"
            "See [[jane.doe-at-acme]] for details.\n",
            encoding="utf-8",
        )

        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)
        links_rewritten = apply_name_email_rename(plan, root / "wiki")

        assert links_rewritten == 1
        referrer_text = referrer.read_text(encoding="utf-8")
        assert "[[jane-doe]]" in referrer_text
        # No dangling reference to the old slug remains anywhere.
        assert "[[jane.doe-at-acme]]" not in referrer_text
        assert not (root / "wiki" / "jane.doe-at-acme.md").exists()

    def test_apply_raises_on_deferred_plan(self, tmp_path: Path) -> None:
        import pytest

        root = tmp_path / "knowledge"
        page = _write_name_email_page(
            root / "wiki", "info-at-acme.md", uid="z11", email="info@acme.example"
        )
        plan = plan_name_email_rename(page, EXCLUDED_CONFIG, root)

        with pytest.raises(ValueError):
            apply_name_email_rename(plan, root / "wiki")


class TestBulkRenameNameEmailPages:
    def test_dry_run_reports_renamed_and_residual_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "knowledge"
        _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="b1", email="jane.doe@acme.example"
        )
        _write_name_email_page(
            root / "wiki", "info-at-acme.md", uid="b2", email="info@acme.example"
        )
        before = (root / "wiki" / "jane.doe-at-acme.md").read_text(encoding="utf-8")

        report = bulk_rename_name_email_pages(
            root / "wiki", EXCLUDED_CONFIG, root, apply=False
        )

        assert report.renamed == 1
        assert report.residual == 1
        assert (root / "wiki" / "jane.doe-at-acme.md").read_text(encoding="utf-8") == before
        assert not (root / "wiki" / "jane-doe.md").exists()

    def test_apply_renames_confident_pages_and_leaves_residual_untouched(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "knowledge"
        _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="b3", email="jane.doe@acme.example"
        )
        info_page = _write_name_email_page(
            root / "wiki", "info-at-acme.md", uid="b4", email="info@acme.example"
        )
        before_info = info_page.read_text(encoding="utf-8")

        report = bulk_rename_name_email_pages(
            root / "wiki", EXCLUDED_CONFIG, root, apply=True
        )

        assert report.renamed == 1
        assert report.residual == 1
        assert (root / "wiki" / "jane-doe.md").is_file()
        assert not (root / "wiki" / "jane.doe-at-acme.md").exists()
        # The ambiguous page is left exactly as-is.
        assert info_page.read_text(encoding="utf-8") == before_info

    def test_idempotent_rerun_skips_already_renamed_pages(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="b5", email="jane.doe@acme.example"
        )

        first = bulk_rename_name_email_pages(root / "wiki", EXCLUDED_CONFIG, root, apply=True)
        second = bulk_rename_name_email_pages(root / "wiki", EXCLUDED_CONFIG, root, apply=True)

        assert first.renamed == 1
        assert second.renamed == 0  # already renamed; name: no longer email-shaped
        assert second.residual == 0


class TestMigratePiiCliRenameNameEmail:
    def test_cli_all_apply_rename_name_email_migrates_confident_and_defers_residual(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "knowledge"
        root.mkdir(parents=True, exist_ok=True)
        (root / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
        _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="c1", email="jane.doe@acme.example"
        )
        _write_name_email_page(
            root / "wiki", "info-at-acme.md", uid="c2", email="info@acme.example"
        )
        referrer = root / "wiki" / "referrer.md"
        referrer.parent.mkdir(parents=True, exist_ok=True)
        referrer.write_text(
            "---\nuid: r1\nname: Referrer\ntype: person\n---\n"
            "See [[jane.doe-at-acme]].\n",
            encoding="utf-8",
        )

        rc = main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--all", "--apply", "--rename-name-email",
            ]
        )

        assert rc == 0
        assert (root / "wiki" / "jane-doe.md").is_file()
        assert not (root / "wiki" / "jane.doe-at-acme.md").exists()
        assert "[[jane-doe]]" in referrer.read_text(encoding="utf-8")
        assert "[[jane.doe-at-acme]]" not in referrer.read_text(encoding="utf-8")
        # The ambiguous page is untouched.
        assert (root / "wiki" / "info-at-acme.md").is_file()
        out = capsys.readouterr().out
        assert "renamed 1 name-is-an-email page" in out
        assert "1 page(s) have an ambiguous local-part" in out

    def test_cli_without_rename_flag_still_reports_note_for_both_populations(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "knowledge"
        root.mkdir(parents=True, exist_ok=True)
        (root / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
        _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="c3", email="jane.doe@acme.example"
        )

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "page(s) are named after an email" in out
        # Without --rename-name-email nothing is written.
        assert (root / "wiki" / "jane.doe-at-acme.md").is_file()


class TestRenameSliceScopingAndRenameOnly:
    """Issue athenaeum#745 — the rename slice must be reachable without the body migration.

    Before athenaeum#745 ``--rename-name-email`` ran only under ``--all`` (silently
    skipped under ``--glob``, never reached under ``--page``), and ``--all``
    always ran the body-text migration in the same pass. On the live corpus
    that made a safe rename unreachable: applying it required accepting a body
    migration that would have redacted real prose flagged by phone-axis
    detector false positives — the athenaeum#691 failure mode.
    """

    @staticmethod
    def _seed(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
        # A renameable name-is-an-email page.
        _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="s1", email="jane.doe@acme.example"
        )
        # An ambiguous one (deferred, never guessed).
        _write_name_email_page(
            root / "wiki", "info-at-acme.md", uid="s2", email="info@acme.example"
        )
        # A page whose PII is in the BODY, not the name — the population the
        # body migration would act on and --rename-only must leave alone.
        _write_page(
            root / "wiki",
            "bodyonly.md",
            "uid: s3\nname: Body Only\ntype: person\n",
            "Reach them at body.person@acme.example any time.",
        )

    def test_rename_only_skips_the_body_migration_entirely(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "knowledge"
        self._seed(root)
        before = (root / "wiki" / "bodyonly.md").read_text(encoding="utf-8")

        rc = main(
            ["storage", "migrate-pii", "--path", str(root), "--all", "--apply", "--rename-only"]
        )

        assert rc == 0
        # The rename happened.
        assert (root / "wiki" / "jane-doe.md").is_file()
        # The body-PII page is byte-for-byte untouched — the whole point.
        assert (root / "wiki" / "bodyonly.md").read_text(encoding="utf-8") == before
        assert "body.person@acme.example" in before
        out = capsys.readouterr().out
        assert "renamed 1 name-is-an-email page" in out
        # No body-migration summary line was emitted.
        assert "excluded contact record(s) to create" not in out

    def test_page_rename_plus_body_migration_both_run(
        self, tmp_path: Path, capsys
    ) -> None:
        """--page --rename-name-email --apply must do BOTH, not stop at the rename.

        Raised in review of athenaeum#745: an early return after a successful rename
        silently left body-text PII in place on the renamed page. The rename
        moves the file, so the body pass has to be retargeted at the NEW path
        rather than skipped.
        """
        root = tmp_path / "knowledge"
        root.mkdir(parents=True, exist_ok=True)
        (root / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
        # One page that is BOTH name-is-an-email AND carries a body address.
        page = _write_name_email_page(
            root / "wiki",
            "jane.doe-at-acme.md",
            uid="b1",
            email="jane.doe@acme.example",
            body="Backup contact: other.person@acme.example on weekends.",
        )

        rc = main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--page", str(page), "--apply", "--rename-name-email",
            ]
        )

        assert rc == 0
        renamed = root / "wiki" / "jane-doe.md"
        assert renamed.is_file()
        assert not page.exists()
        # The body address must be gone from the renamed page too.
        assert "other.person@acme.example" not in renamed.read_text(encoding="utf-8")

    def test_rename_to_names_a_page_athenaeum505_refuses_to_guess(
        self, tmp_path: Path, capsys
    ) -> None:
        """athenaeum#745: an operator-supplied name is athenaeum#505's missing half.

        athenaeum#505 is right to refuse to GUESS from an ambiguous local-part, but it
        left that population with no route through the tool at all — hand-editing
        the frontmatter skips the excluded record, the slug rename and the
        inbound-link rewrite.
        """
        root = tmp_path / "knowledge"
        self._seed(root)
        referrer = root / "wiki" / "ref.md"
        referrer.write_text(
            "---\nuid: r9\nname: Ref\ntype: person\n---\nSee [[info-at-acme]].\n",
            encoding="utf-8",
        )

        rc = main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--page", str(root / "wiki" / "info-at-acme.md"),
                "--rename-only", "--apply", "--rename-to", "Ada Lovelace",
            ]
        )

        assert rc == 0
        renamed = root / "wiki" / "ada-lovelace.md"
        assert renamed.is_file()
        assert not (root / "wiki" / "info-at-acme.md").exists()
        # name: is the operator's name, and the address is off the page.
        text = renamed.read_text(encoding="utf-8")
        assert "Ada Lovelace" in text
        assert "info@acme.example" not in text
        # The address landed on the excluded surface, not nowhere.
        assert (root / "excluded" / "ada-lovelace.md").is_file()
        # Inbound wikilinks follow the rename.
        assert "[[ada-lovelace]]" in referrer.read_text(encoding="utf-8")

    def test_rename_to_requires_page(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "knowledge"
        self._seed(root)

        rc = main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--all", "--rename-only", "--rename-to", "Ada Lovelace",
            ]
        )

        # Stamping one name across a bulk target set would rename every match
        # to the same thing.
        assert rc == 2
        assert "--rename-to requires --page" in capsys.readouterr().err

    def test_rename_only_implies_rename_name_email(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "knowledge"
        self._seed(root)

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all", "--rename-only"])

        assert rc == 0
        out = capsys.readouterr().out
        # The rename slice ran without --rename-name-email being passed.
        assert "would rename 1 name-is-an-email page" in out

    def test_page_scopes_the_slice_to_one_page(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "knowledge"
        self._seed(root)

        rc = main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--page", str(root / "wiki" / "jane.doe-at-acme.md"),
                "--rename-only",
            ]
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "of 1 scanned" in out  # not the whole corpus

    def test_glob_scopes_the_slice_instead_of_skipping_it(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "knowledge"
        self._seed(root)

        rc = main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--glob", "jane.doe-at-acme.md", "--rename-only",
            ]
        )

        assert rc == 0
        out = capsys.readouterr().out
        # Previously `and args.glob is None` suppressed the slice outright.
        assert "would rename 1 name-is-an-email page" in out
        assert "of 1 scanned" in out

    def test_all_still_scans_every_entity_page(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "knowledge"
        self._seed(root)

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all", "--rename-only"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "of 3 scanned" in out  # default target set unchanged

    def test_list_deferred_enumerates_the_manual_naming_worklist(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "knowledge"
        self._seed(root)

        rc = main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--all", "--rename-only", "--list-deferred",
            ]
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "deferred: manual naming required" in out
        assert "info-at-acme.md" in out

    def test_without_list_deferred_only_the_count_is_printed(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "knowledge"
        self._seed(root)

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all", "--rename-only"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "ambiguous local-part" in out
        assert "deferred: manual naming required" not in out
        assert "info-at-acme.md" not in out


class TestLintPiiCleanAfterRename:
    """AC: after migration + reindex, lint-pii no longer reports migrated
    pages, except the deliberately-deferred ambiguous residual."""

    def test_lint_pii_clean_except_deferred_residual(self, tmp_path: Path, capsys) -> None:
        from athenaeum.pii import name_field_holds_pii, scan_corpus_pii

        root = tmp_path / "knowledge"
        root.mkdir(parents=True, exist_ok=True)
        (root / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
        _write_name_email_page(
            root / "wiki", "jane.doe-at-acme.md", uid="d1", email="jane.doe@acme.example"
        )
        _write_name_email_page(
            root / "wiki", "info-at-acme.md", uid="d2", email="info@acme.example"
        )

        rc = main(
            [
                "storage", "migrate-pii", "--path", str(root),
                "--all", "--apply", "--rename-name-email",
            ]
        )
        assert rc == 0

        # The confident page no longer trips name_field_holds_pii (renamed to
        # a human-readable name) nor the corpus-wide lint (the raw address no
        # longer appears anywhere under wiki/).
        renamed_text = (root / "wiki" / "jane-doe.md").read_text(encoding="utf-8")
        renamed_meta, _ = parse_frontmatter(renamed_text)
        assert name_field_holds_pii(renamed_meta) is False

        findings = scan_corpus_pii(root / "wiki")
        finding_paths = {f.path.name for f in findings}
        # The deferred ambiguous page is still surfaced (deliberately) —
        # its address is still on the page as its name.
        assert "info-at-acme.md" in finding_paths
        # The renamed page's own findings must not include the migrated
        # address (it was moved off-corpus, not merely relocated on-page).
        assert "jane-doe.md" not in finding_paths

        # The deferred page still trips name_field_holds_pii — the residual
        # AC's "reported as a residual count" surfaces through the bulk
        # driver's report, not a silent disappearance.
        deferred_meta, _ = parse_frontmatter(
            (root / "wiki" / "info-at-acme.md").read_text(encoding="utf-8")
        )
        assert name_field_holds_pii(deferred_meta) is True


# ---------------------------------------------------------------------------
# Sensitivity-registry migration (issue athenaeum#992, S3 of athenaeum#910's design note)
# ---------------------------------------------------------------------------
#
# storage_migrate.py's detector call sites (the module-scope
# `from athenaeum.pii import find_inline_emails, find_inline_phones` and the
# function-local re-import inside `plan_name_email_rename`) now obtain
# findings through `athenaeum.sensitivity.classify()` instead. The classes
# below prove: (AC2) the module-scope import is gone and the sweep still
# works; (AC4) the migrated path agrees with the pre-change
# `find_inline_emails`/`find_inline_phones` result on representative fixture
# shapes; (AC5) the athenaeum#500/#732 phone false-positive exclusions still hold;
# (AC6) a purely test-defined recogniser travels the identical code path as
# the shipped `email` recogniser; (AC7) with no `sensitivity:` config block,
# the migrated sweep's output is unchanged.


class TestNoDirectPiiDetectorImport:
    def test_module_scope_import_does_not_name_find_inline_functions(self) -> None:
        # AC2/AC8: storage_migrate no longer imports the detector functions by
        # name at module scope, but athenaeum.pii still exports and works.
        import athenaeum.storage_migrate as sm
        from athenaeum.pii import find_inline_emails, find_inline_phones

        assert not hasattr(sm, "find_inline_emails")
        assert not hasattr(sm, "find_inline_phones")
        assert find_inline_emails("reach a@b.com") == ["a@b.com"]
        assert find_inline_phones("call 555-010-0100") == ["555-010-0100"]


class TestSensitivityRegistryEquivalence:
    """AC4: the migrated detector path agrees with the pre-change
    ``athenaeum.pii.find_inline_emails``/``find_inline_phones`` result on
    this module's existing fixture shapes — proven, not merely asserted.
    """

    EMAIL_FIXTURES: list[str] = [
        "",
        "no contact info here",
        "reach jane@example.com for details",
        "jane@example.com, then jane@example.com again",  # repeat -> dedup
        "git@github.com is a clone url",  # service address (see below)
        'source: "Streak import 2016 via founder@acme.example"',
    ]

    PHONE_FIXTURES: list[str] = [
        "",
        "call 555-010-0100 or (555) 010-0100",
        "+1 555 010 0100 and +15550100100",
        "555-010-0100, then 555-010-0100 again",  # repeat -> dedup
        "logged (2026-07-29) in the CRM",  # athenaeum#500: parenthesized ISO date
        "season 2019-2020 recap",  # athenaeum#500: year range
        "QBO realm 1008563730 for this account",  # athenaeum#732: labeled record id
        "GA4 stream 5139685489",  # athenaeum#732: labeled record id
        "ISBN 9781234567897 first edition",  # athenaeum#732: bare ISBN-13
        "issue list 256-257-280 filed",  # athenaeum#720: short unprefixed grouped run
    ]

    def test_email_detection_matches_pre_change_function_on_fixtures(self) -> None:
        from athenaeum.pii import find_inline_emails
        from athenaeum.storage_migrate import _classified_values

        for text in self.EMAIL_FIXTURES:
            assert _classified_values(text, "email", None) == find_inline_emails(text), text

    def test_phone_detection_matches_pre_change_function_on_fixtures(self) -> None:
        from athenaeum.pii import find_inline_phones
        from athenaeum.storage_migrate import _classified_values

        for text in self.PHONE_FIXTURES:
            assert _classified_values(text, "phone", None) == find_inline_phones(text), text

    def test_migratable_emails_still_excludes_service_addresses(self) -> None:
        # _migratable_emails' is_service_address filter (issue athenaeum#507) is
        # applied AFTER the registry lookup, unchanged.
        from athenaeum.storage_migrate import _migratable_emails

        assert _migratable_emails("git@github.com is the clone url", None) == []
        assert _migratable_emails("reach jane@example.com", None) == ["jane@example.com"]


class TestPhoneFalsePositiveSuppressionPreserved:
    """AC5: the migrated storage_migrate sweep still excludes the athenaeum#500/#732
    shapes. A regression here would silently re-open both closed issues.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "logged (2026-07-29) in the CRM",  # athenaeum#500: parenthesized ISO date
            "First contact: 2026-07-29 per CRM",  # athenaeum#500: bare ISO date
            "see (2019-2020) season stats",  # athenaeum#500: parenthesized year range
            "page uid (52785095) in the index",  # athenaeum#500: parenthesized uid prefix
            "QBO realm 1008563730 for this account",  # athenaeum#732: labeled record id
            "GA4 stream 5139685489",  # athenaeum#732: labeled record id
            "ISBN 9781234567897 first edition",  # athenaeum#732: bare ISBN-13
        ],
    )
    def test_excluded_shape_produces_no_phone_match(self, text: str) -> None:
        from athenaeum.storage_migrate import _classified_values

        assert _classified_values(text, "phone", None) == []

    def test_genuine_phone_beside_an_excluded_shape_still_matches(self) -> None:
        from athenaeum.storage_migrate import _classified_values

        assert _classified_values(
            "met 2026-07-29, call (555) 010-0100", "phone", None
        ) == ["(555) 010-0100"]


class TestSensitivityRegistryEndToEnd:
    """AC6: a purely test-defined recogniser, registered through the public
    :func:`athenaeum.sensitivity.register_recognizer` and bound to a
    test-defined class via config, travels a migrated call site's sweep
    through the SAME generic helper (:func:`athenaeum.storage_migrate._classified_values`)
    the shipped ``email`` recogniser uses — with no built-in-specific branch
    anywhere in the traversed path (the helper is parameterized by recogniser
    name; ``"email"``/``"phone"`` are not special-cased).
    """

    @pytest.fixture
    def _isolate_registered_recognizers(self):
        from athenaeum import sensitivity

        snapshot = dict(sensitivity._REGISTERED_RECOGNIZERS)
        try:
            yield
        finally:
            sensitivity._REGISTERED_RECOGNIZERS.clear()
            sensitivity._REGISTERED_RECOGNIZERS.update(snapshot)

    def test_custom_recognizer_matches_travel_the_same_path_as_email(
        self, _isolate_registered_recognizers
    ) -> None:
        from athenaeum.sensitivity import SensitivityMatch, register_recognizer
        from athenaeum.storage_migrate import _classified_values

        class _WidgetIdRecognizer:
            name = "widget-id"

            def detect(self, *, text, frontmatter=None):
                return [
                    SensitivityMatch(recognizer=self.name, value=tok)
                    for tok in text.split()
                    if tok.startswith("WID-")
                ]

        register_recognizer(_WidgetIdRecognizer())
        config = {
            "sensitivity": {
                "classes": {
                    "widget": {
                        "recognizers": ["widget-id"],
                        "read_policy": {"access": "internal"},
                    }
                }
            }
        }
        text = "contact alice@example.com re WID-42 and WID-42 again"

        custom = _classified_values(text, "widget-id", config)
        email = _classified_values(text, "email", config)

        assert custom == ["WID-42"]  # order-preserving dedup, same as email's
        assert email == ["alice@example.com"]
        assert type(custom) is type(email) is list


class TestSensitivityRegistryDeploymentDefaultUnchanged:
    """AC7: with no ``sensitivity:`` config block, the migrated sweep produces
    the same findings it produced before this PR on the existing fixture
    corpus — the full ``TestPlanPiiMigration``/``TestDetectorDrivenKeyCoverage``/
    ``TestNestedFrontmatterCoverage`` suites above (unmodified by athenaeum#992 and
    still green) are the corpus-wide proof; this test is the direct,
    explicit one for the default (no ``sensitivity`` key) config shape.
    """

    def test_default_config_migration_matches_pre_change_shape(
        self, tmp_path: Path
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        page = _write_entity_page(root / "wiki")
        # EXCLUDED_CONFIG carries no `sensitivity:` key at all.
        plan = plan_pii_migration(page, EXCLUDED_CONFIG, root)
        assert plan.emails == ["jane@example.com"]
        assert plan.phones == ["+1-555-0100"]
