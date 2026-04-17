"""Tests for the observation-filter tuning mechanism.

Issue #29: the filter file is the user-tunable authority for what gets
captured. These tests pin the contract between the filter schema file
(copied to wiki/_schema/observation-filter.md on init) and the
CLAUDE.md.example that instructs Claude to read and update it.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from athenaeum.init import init_knowledge_dir

CLAUDE_MD_EXAMPLE = (
    Path(__file__).parent.parent / "examples" / "claude-code" / "CLAUDE.md.example"
)


def test_filter_has_tuning_sections(tmp_path: Path) -> None:
    """The scaffolded filter includes the sections Claude needs to tune it."""
    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    filter_text = (target / "wiki" / "_schema" / "observation-filter.md").read_text(
        encoding="utf-8"
    )
    assert "## Always Capture" in filter_text
    assert "## Capture When Reinforced" in filter_text
    assert "## Never Capture" in filter_text
    assert "## Tuning" in filter_text


def test_claude_md_example_references_filter_path() -> None:
    """CLAUDE.md.example points Claude at the filter file as the authority."""
    text = CLAUDE_MD_EXAMPLE.read_text(encoding="utf-8")
    assert "~/knowledge/wiki/_schema/observation-filter.md" in text


def test_claude_md_example_has_tuning_instructions() -> None:
    """CLAUDE.md.example tells Claude how to update the filter on feedback."""
    text = CLAUDE_MD_EXAMPLE.read_text(encoding="utf-8")
    assert "Tuning the observation filter" in text
    assert "Stop saving X" in text or "stop saving" in text.lower()
    assert "Never Capture" in text


def test_bundled_filter_matches_init_output(tmp_path: Path) -> None:
    """The bundled schema is what gets copied (no divergence)."""
    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    schema_pkg = importlib.resources.files("athenaeum.schema")
    bundled = (schema_pkg / "observation-filter.md").read_text(encoding="utf-8")
    copied = (target / "wiki" / "_schema" / "observation-filter.md").read_text(
        encoding="utf-8"
    )
    assert copied == bundled
