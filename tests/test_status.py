"""Tests for athenaeum status command."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from athenaeum.status import format_status, status


class TestStatus:
    def _seed_knowledge(self, tmp_path: Path) -> Path:
        """Create a minimal knowledge directory for status tests."""
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        (wiki / "_schema").mkdir(parents=True)
        raw = root / "raw" / "sessions"
        raw.mkdir(parents=True)

        # Entity page
        (wiki / "a1b2c3d4-acme-corp.md").write_text(textwrap.dedent("""\
            ---
            uid: a1b2c3d4
            type: company
            name: Acme Corp
            access: internal
            ---

            # Acme Corp
        """))

        (wiki / "b2c3d4e5-alice.md").write_text(textwrap.dedent("""\
            ---
            uid: b2c3d4e5
            type: person
            name: Alice Zhang
            access: internal
            ---

            # Alice Zhang
        """))

        # Pending questions
        (wiki / "_pending_questions.md").write_text(
            "# Pending Questions\n\n"
            "## [2024-04-06] Entity: \"Acme\" (from ref)\n\nConflict.\n\n"
            "---\n\n"
            "## [2024-04-07] Entity: \"Bob\" (from ref2)\n\nAnother.\n"
        )

        # Raw file pending
        (raw / "20240410T120000Z-aabbccdd.md").write_text("Some raw.\n")

        # Init git
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"],
            cwd=root, check=True,
        )

        return root

    def test_status_counts(self, tmp_path: Path) -> None:
        root = self._seed_knowledge(tmp_path)
        info = status(root)
        assert info["raw_pending"] == 1
        assert info["entity_count"] == 2
        assert info["entities_by_type"]["company"] == 1
        assert info["entities_by_type"]["person"] == 1
        assert info["pending_questions"] == 2
        assert info["last_commit_date"] != ""

    def test_status_empty_knowledge(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        info = status(root)
        assert info["raw_pending"] == 0
        assert info["entity_count"] == 0
        assert info["pending_questions"] == 0

    def test_format_status(self) -> None:
        info = {
            "raw_pending": 3,
            "entity_count": 10,
            "entities_by_type": {"person": 5, "company": 3, "concept": 2},
            "last_commit_date": "2024-04-06 12:00:00 -0700",
            "last_commit_message": "librarian: processed 5 file(s)",
            "pending_questions": 1,
        }
        output = format_status(info)
        assert "Raw files pending:    3" in output
        assert "Wiki entities:        10" in output
        assert "person: 5" in output
        assert "Pending questions:    1" in output

    def test_cli_status(self, tmp_path: Path) -> None:
        from athenaeum.cli import main

        root = self._seed_knowledge(tmp_path)
        exit_code = main(["status", "--path", str(root)])
        assert exit_code == 0

    def test_cli_status_missing_dir(self, tmp_path: Path) -> None:
        from athenaeum.cli import main

        exit_code = main(["status", "--path", str(tmp_path / "nope")])
        assert exit_code == 1
