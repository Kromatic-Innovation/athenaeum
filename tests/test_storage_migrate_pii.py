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
