"""Tests for the observation-filter tuning mechanism.

Issue athenaeum#29: the filter file is the user-tunable authority for what gets
captured. These tests pin the contract between the filter schema file
(copied to wiki/_schema/observation-filter.md on init) and the
CLAUDE.md.example that instructs Claude to read and update it.
"""

from __future__ import annotations

import importlib.resources
import inspect
from pathlib import Path

from athenaeum import librarian
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


def test_shipped_filter_does_not_claim_self_tuning() -> None:
    """The shipped default must not describe the librarian mutating this file.

    This encodes a *decision*, not a style rule (issue athenaeum#1423, "Option
    A"). The filter used to ship with a "Decay Rules" section, a "Pattern
    Detection" section, and an opening line claiming "the librarian updates
    it during consolidation" -- describing a self-tuning mechanism that was
    never built. Rather than build it (Option B: making the librarian a
    writer to wiki/_schema/, a new single-writer-boundary crossing plus a
    decay/reinforcement counter that doesn't exist), athenaeum#1423 chose
    Option A: stop shipping a doc that describes behavior the code doesn't
    have. Real tuning is human/agent-edit only (see the "## Tuning" section
    and its own test above).

    The banned-marker list below is deliberately small and explicit -- this
    is a drift guard, not a general prose linter. If a future change
    legitimately reintroduces one of these markers *because the described
    behavior now exists*, the companion assertion
    (test_librarian_does_not_write_observation_filter) must be updated in the
    same commit, or this test stays red as a forcing function.
    """
    schema_pkg = importlib.resources.files("athenaeum.schema")
    bundled = (schema_pkg / "observation-filter.md").read_text(encoding="utf-8")

    banned_markers = (
        "## Decay Rules",
        "## Pattern Detection",
        "the librarian updates it",
    )
    for marker in banned_markers:
        assert marker not in bundled, (
            f"shipped observation-filter.md claims self-tuning behavior "
            f"({marker!r}) that the librarian does not implement -- see "
            f"test_librarian_does_not_write_observation_filter"
        )


def test_librarian_does_not_write_observation_filter() -> None:
    """Paired code fact for the assertion above: librarian.py never writes
    wiki/_schema/observation-filter.md.

    If someone later BUILDS the self-tuning writer this issue declined to
    build (Option B), this test is the tripwire: it fails, and the failure
    forces the shipped default's wording (and the assertion above) to be
    updated in the same change instead of silently drifting out of sync
    again.

    The only legitimate occurrence of the string "observation-filter" in
    librarian.py today is the literal example text inside
    ``_render_run_summary``'s docstring (the `schema_fragments=` log-line
    format sample) -- not a read, and not a write. Tier-2's actual read of
    the live file lives in tiers.py, outside this lane's scope.
    """
    source_path = inspect.getsourcefile(librarian)
    assert source_path is not None
    source = Path(source_path).read_text(encoding="utf-8")

    hits = [line for line in source.splitlines() if "observation-filter" in line]
    assert len(hits) == 1, (
        "expected exactly one mention of 'observation-filter' in librarian.py "
        f"(the run-summary docstring example); found {len(hits)}: {hits!r}. "
        "If this is a new read/write of the live schema file, "
        "wiki/_schema/observation-filter.md's shipped wording (and "
        "test_shipped_filter_does_not_claim_self_tuning's banned markers) may "
        "need to change in the same commit."
    )
    assert "schema_fragments=observation-filter:default" in hits[0], (
        "the sole 'observation-filter' mention in librarian.py is expected to "
        f"be the docstring log-format example, got: {hits[0]!r}"
    )


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
