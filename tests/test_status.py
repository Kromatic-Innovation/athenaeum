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
        (wiki / "a1b2c3d4-acme-corp.md").write_text(
            textwrap.dedent(
                """\
            ---
            uid: a1b2c3d4
            type: company
            name: Acme Corp
            access: internal
            ---

            # Acme Corp
        """
            )
        )

        (wiki / "b2c3d4e5-alice.md").write_text(
            textwrap.dedent(
                """\
            ---
            uid: b2c3d4e5
            type: person
            name: Alice Zhang
            access: internal
            ---

            # Alice Zhang
        """
            )
        )

        # Pending questions
        (wiki / "_pending_questions.md").write_text(
            "# Pending Questions\n\n"
            '## [2024-04-06] Entity: "Acme" (from ref)\n\nConflict.\n\n'
            "---\n\n"
            '## [2024-04-07] Entity: "Bob" (from ref2)\n\nAnother.\n'
        )

        # Raw file pending
        (raw / "20240410T120000Z-aabbccdd.md").write_text("Some raw.\n")

        # Init git
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"],
            cwd=root,
            check=True,
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


def _entity_page(name: str, uid: str, target_bytes: int) -> str:
    """Build an entity page whose UTF-8 body is ~``target_bytes`` long."""
    header = textwrap.dedent(
        f"""\
        ---
        uid: {uid}
        type: concept
        name: {name}
        access: internal
        ---

        # {name}

    """
    )
    pad = "x" * max(0, target_bytes - len(header.encode("utf-8")))
    return header + pad


class TestPageSizeGuardrails:
    """Issue #310 — warn-only oversized wiki-page reporting."""

    def _seed(self, tmp_path: Path) -> Path:
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        wiki.mkdir(parents=True)
        (root / "raw").mkdir()

        # Under warn (default 8192).
        (wiki / "small.md").write_text(_entity_page("Small", "u1", 500))
        # Over warn, at/under flag (default 16384).
        (wiki / "warnpage.md").write_text(_entity_page("Warn Page", "u2", 10000))
        # Over flag.
        (wiki / "flagpage.md").write_text(_entity_page("Flag Page", "u3", 20000))
        # Big but NOT an entity (no frontmatter name) — must be skipped.
        (wiki / "notes.md").write_text("y" * 30000)
        # Big but _-prefixed — must be skipped.
        (wiki / "_scratch.md").write_text("z" * 30000)
        return root

    def test_scan_buckets_by_threshold(self, tmp_path: Path) -> None:
        from athenaeum.status import scan_page_sizes

        root = self._seed(tmp_path)
        warn, flag = scan_page_sizes(root / "wiki", 8192, 16384)
        warn_names = {n for n, _ in warn}
        flag_names = {n for n, _ in flag}
        assert warn_names == {"warnpage.md"}
        assert flag_names == {"flagpage.md"}
        # Disjoint: a flagged page is not double-counted as a warn.
        assert not (warn_names & flag_names)

    def test_scan_skips_nonentity_and_underscore(self, tmp_path: Path) -> None:
        from athenaeum.status import scan_page_sizes

        root = self._seed(tmp_path)
        warn, flag = scan_page_sizes(root / "wiki", 8192, 16384)
        seen = {n for n, _ in warn} | {n for n, _ in flag}
        assert "notes.md" not in seen  # non-entity (no name) skipped
        assert "_scratch.md" not in seen  # underscore-prefixed skipped
        assert "small.md" not in seen  # under warn threshold

    def test_status_reports_pages(self, tmp_path: Path) -> None:
        root = self._seed(tmp_path)
        info = status(root)
        assert [n for n, _ in info["pages_warn"]] == ["warnpage.md"]
        assert [n for n, _ in info["pages_flag"]] == ["flagpage.md"]

    def test_format_status_includes_oversized_summary(self, tmp_path: Path) -> None:
        root = self._seed(tmp_path)
        out = format_status(status(root))
        assert "Oversized pages (warn/flag): 1/1" in out
        assert "[flag] flagpage.md" in out
        assert "[warn] warnpage.md" in out

    def test_format_status_backward_compatible(self) -> None:
        # A pre-#310 status dict (no pages_* keys) must still format.
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
        }
        out = format_status(info)  # type: ignore[arg-type]
        assert "Oversized pages (warn/flag): 0/0" in out

    def test_thresholds_honor_config(self, tmp_path: Path) -> None:
        # A tiny yaml warn threshold pulls the small page into the warn bucket.
        root = self._seed(tmp_path)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  page_warn_bytes: 100\n  page_flag_bytes: 15000\n"
        )
        info = status(root)
        warn_names = {n for n, _ in info["pages_warn"]}
        flag_names = {n for n, _ in info["pages_flag"]}
        # small (500B) and warn (10000B) now exceed the 100B warn floor but
        # stay under the 15000B flag; only flagpage (20000B) is flagged.
        assert warn_names == {"small.md", "warnpage.md"}
        assert flag_names == {"flagpage.md"}
