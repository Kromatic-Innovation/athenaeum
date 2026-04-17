"""Integration tests for athenaeum.librarian — discover_raw_files, rebuild_index,
process_one, and the run() pipeline with mocked LLM."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.models import RawFile

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    """Create a raw directory with sample intake files."""
    raw = tmp_path / "raw"
    raw.mkdir()
    sessions = raw / "sessions"
    sessions.mkdir()
    imports = raw / "imports"
    imports.mkdir()

    (sessions / "20240406T120000Z-aabb0011.md").write_text(
        "Met with Alice Zhang from Acme Corp about lean coaching.\n"
    )
    (sessions / "20240406T120100Z-ccdd2233.md").write_text(
        "Explored innovation accounting as a concept.\n"
    )
    (imports / "20240406T130000Z-eeff4455.md").write_text(
        "User mentioned preferring dark mode in all tools.\n"
    )
    # Non-standard filename (should still be discovered)
    (sessions / "random-notes.md").write_text("Some freeform notes.\n")
    # .gitkeep should be skipped
    (sessions / ".gitkeep").write_text("")

    return raw


# ---------------------------------------------------------------------------
# discover_raw_files
# ---------------------------------------------------------------------------


class TestDiscoverRawFiles:
    def test_finds_all_files(self, raw_dir: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(raw_dir)
        # 3 standard + 1 non-standard = 4 (skips .gitkeep)
        assert len(files) == 4

    def test_extracts_metadata(self, raw_dir: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(raw_dir)
        standard = [f for f in files if f.timestamp]
        assert len(standard) == 3
        session_files = [f for f in standard if f.source == "sessions"]
        assert len(session_files) == 2
        import_files = [f for f in standard if f.source == "imports"]
        assert len(import_files) == 1

    def test_non_standard_filename(self, raw_dir: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(raw_dir)
        non_standard = [f for f in files if not f.timestamp]
        assert len(non_standard) == 1
        assert non_standard[0].path.name == "random-notes.md"

    def test_skips_gitkeep(self, raw_dir: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(raw_dir)
        names = [f.path.name for f in files]
        assert ".gitkeep" not in names

    def test_empty_dir(self, tmp_path: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        empty = tmp_path / "empty_raw"
        empty.mkdir()
        files = discover_raw_files(empty)
        assert files == []

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        from athenaeum.librarian import discover_raw_files

        files = discover_raw_files(tmp_path / "does_not_exist")
        assert files == []


# ---------------------------------------------------------------------------
# RawFile content loading
# ---------------------------------------------------------------------------


class TestRawFileContent:
    def test_lazy_loading(self, raw_dir: Path) -> None:
        raw = RawFile(
            path=raw_dir / "sessions" / "20240406T120000Z-aabb0011.md",
            source="sessions",
            timestamp="20240406T120000Z",
            uuid8="aabb0011",
        )
        # _content is None before access
        assert raw._content is None
        content = raw.content
        assert "Alice Zhang" in content
        # Now cached
        assert raw._content is not None

    def test_ref_format(self) -> None:
        raw = RawFile(
            path=Path("/tmp/knowledge/raw/sessions/20240406T120000Z-aabb0011.md"),
            source="sessions",
            timestamp="20240406T120000Z",
            uuid8="aabb0011",
        )
        assert raw.ref == "sessions/20240406T120000Z-aabb0011.md"


# ---------------------------------------------------------------------------
# rebuild_index
# ---------------------------------------------------------------------------


class TestRebuildIndex:
    def test_creates_index(self, wiki_dir: Path) -> None:
        from athenaeum.librarian import rebuild_index

        rebuild_index(wiki_dir)
        index_path = wiki_dir / "_index.md"
        assert index_path.exists()
        content = index_path.read_text()
        assert "# Knowledge Wiki Index" in content
        assert "Acme Corp" in content

    def test_groups_by_type(self, wiki_dir: Path) -> None:
        from athenaeum.librarian import rebuild_index

        rebuild_index(wiki_dir)
        content = (wiki_dir / "_index.md").read_text()
        assert "## Company" in content
        assert "## Project" in content

    def test_empty_wiki(self, tmp_path: Path) -> None:
        from athenaeum.librarian import rebuild_index

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        rebuild_index(wiki)
        content = (wiki / "_index.md").read_text()
        assert "Total entities: 0" in content

    def test_skips_underscore_files(self, wiki_dir: Path) -> None:
        from athenaeum.librarian import rebuild_index

        # Add an underscore file that should not appear in index
        (wiki_dir / "_config.md").write_text("---\nname: Config\ntype: tool\n---\n")
        rebuild_index(wiki_dir)
        content = (wiki_dir / "_index.md").read_text()
        assert "Config" not in content


# ---------------------------------------------------------------------------
# run() integration — mocked LLM, real filesystem + git
# ---------------------------------------------------------------------------


class TestRunIntegration:
    """End-to-end integration test for the run() pipeline.

    Uses a real tmp_path-based knowledge root with a real git repo,
    but mocks anthropic.Anthropic at the module level so no HTTP calls
    are made and no API key is needed.
    """

    def _seed_knowledge_root(self, tmp_path: Path) -> Path:
        """Create a minimal knowledge/ tree with .git, wiki/_schema, raw/sessions."""
        root = tmp_path / "knowledge"
        root.mkdir()

        wiki = root / "wiki"
        (wiki / "_schema").mkdir(parents=True)
        (wiki / "_schema" / "types.md").write_text(
            "# Types\n\n| Type |\n|------|\n| person |\n"
        )
        (wiki / "_schema" / "tags.md").write_text(
            "# Tags\n\n| Tag |\n|-----|\n| active |\n"
        )
        (wiki / "_schema" / "access-levels.md").write_text(
            "# Access\n\n| Level |\n|-------|\n| internal |\n"
        )

        sessions = root / "raw" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / ".gitkeep").write_text("")

        subprocess.run(
            ["git", "init", "-q", "-b", "test-branch"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test Runner"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"],
            cwd=root,
            check=True,
        )

        # Drop the raw intake file post-commit so it is an uncommitted
        # change when run() takes its pre-processing snapshot.
        (sessions / "20240410T120000Z-aabbccdd.md").write_text(
            "Met with Alice Zhang about product strategy. "
            "She leads product at Acme Corp.\n"
        )
        return root

    def test_max_api_calls_stops_processing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issue #6: run() must stop processing when max_api_calls budget is exhausted."""
        import json
        import logging

        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)
        sessions = root / "raw" / "sessions"

        # Add a second raw file so there are 2 to process
        (sessions / "20240410T130000Z-11223344.md").write_text(
            "Discussed innovation accounting methodology in detail.\n"
        )

        # Mock client that returns valid classification + creation responses
        classify_response = MagicMock()
        classify_response.content = [MagicMock(text=json.dumps([{
            "name": "Alice Zhang",
            "entity_type": "person",
            "tags": ["active"],
            "access": "internal",
            "observations": "Product leader.",
        }]))]
        create_response = MagicMock()
        create_response.content = [MagicMock(text="# Alice Zhang\n\nProduct leader.")]

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            classify_response,
            create_response,
            # If budget is working, the second file should NOT be processed
            # and these would never be called
        ]
        monkeypatch.setattr(
            anthropic_mod, "Anthropic", lambda **kwargs: mock_client,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        caplog.set_level(logging.DEBUG, logger="athenaeum")

        # Set max_api_calls=2 — processing first file uses ~2 calls (1 classify + 1 create)
        # so the second file should be skipped
        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=2,
        )

        assert any(
            "budget exhausted" in rec.message.lower() or "API call budget" in rec.message
            for rec in caplog.records
        ), "Expected budget exhaustion log message"

    def test_max_retries_passed_to_client(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Issue #6: Anthropic client must be created with max_retries=3."""
        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)

        captured_kwargs: dict = {}

        def mock_anthropic(**kwargs):
            captured_kwargs.update(kwargs)
            client = MagicMock()
            client.messages.create.return_value = MagicMock(
                content=[MagicMock(text="[]")]
            )
            return client

        monkeypatch.setattr(anthropic_mod, "Anthropic", mock_anthropic)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key")

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        assert captured_kwargs.get("max_retries") == 3

    def test_keeps_raw_on_llm_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When the LLM fails, raw files must be preserved for retry."""
        import logging

        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)
        raw_file = root / "raw" / "sessions" / "20240410T120000Z-aabbccdd.md"
        assert raw_file.exists(), "test setup: raw file not seeded"

        # Patch anthropic.Anthropic to return a client that always raises
        failing_client = MagicMock()
        failing_client.messages.create.side_effect = anthropic_mod.APIError(
            message="Simulated server error",
            request=MagicMock(),
            body=None,
        )
        monkeypatch.setattr(
            anthropic_mod, "Anthropic", lambda **kwargs: failing_client,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        caplog.set_level(logging.DEBUG, logger="athenaeum")

        exit_code = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
        )

        # Contract 1: raw intake preserved for retry on next run
        assert raw_file.exists(), (
            "raw file was deleted despite LLM failure -- must keep raw files "
            "when an LLM call fails so the next run can retry."
        )

        # Contract 2: logged the failure through outer exception handler
        assert any(
            "Failed to process" in rec.message for rec in caplog.records
        ), "run() did not log the failure via its outer exception handler"

        # Contract 3: no wiki entity pages created
        wiki_entities = [
            p for p in (root / "wiki").rglob("*.md")
            if "_schema" not in p.parts and not p.name.startswith("_")
        ]
        assert wiki_entities == [], (
            f"Wiki pages were created despite LLM failure: {wiki_entities}"
        )

        # Contract 4: pre-processing git snapshot ran
        git_log = subprocess.run(
            ["git", "log", "--format=%s"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "librarian: pre-processing snapshot" in git_log.stdout, (
            "pre-processing snapshot was not taken"
        )

        # run() returns 1 when any files failed (partial failure)
        assert exit_code == 1
