"""Shared test fixtures for athenaeum test suite."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure git identity is available for tests that run git commit."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test Runner")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test Runner")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


@pytest.fixture
def wiki_dir(tmp_path: Path) -> Path:
    """Create a minimal wiki directory with sample entity pages."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    # Old-format page (no uid field)
    (wiki / "feedback_keychain_auth.md").write_text(textwrap.dedent("""\
        ---
        name: Auth tokens must use system keychain
        description: Never store auth tokens as plaintext env vars.
        type: feedback
        ---

        Always use the system keychain for storing auth tokens.
    """))

    # Entity-template format page
    (wiki / "a1b2c3d4-acme-corp.md").write_text(textwrap.dedent("""\
        ---
        uid: a1b2c3d4
        type: company
        name: Acme Corp
        aliases:
          - Acme
          - Acme Corporation
        access: confidential
        tags:
          - client
          - fintech
        created: '2024-03-15'
        updated: '2024-04-06'
        ---

        # Acme Corp

        Fintech startup, Series B.
    """))

    # Another old-format page
    (wiki / "project_knowledge_architecture.md").write_text(textwrap.dedent("""\
        ---
        name: Knowledge architecture project
        description: Unified knowledge system.
        type: project
        ---

        The knowledge architecture unifies fragmented memory scopes.
    """))

    # Files that should be skipped by EntityIndex
    (wiki / "_index.md").write_text("# Index\n")
    (wiki / "MEMORY.md").write_text("# Memory Index\n")

    return wiki
