# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared measurement-pack docs-append helper (issue athenaeum#713)."""

from __future__ import annotations

from pathlib import Path

from athenaeum.measurement_docs import DOCS_HEADER, append_measurement_section

_REPO_DOCS_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "memory-model-measurements.md"
)


class TestAppendMeasurementSection:
    def test_creates_file_with_header_when_absent(self, tmp_path: Path) -> None:
        docs_path = tmp_path / "docs" / "measurements.md"
        append_measurement_section(
            docs_path,
            section_heading="## Artifact A",
            entry_markdown="### Snapshot 2026-01-01\n\n- x: 1\n",
        )
        text = docs_path.read_text(encoding="utf-8")
        assert text.startswith(DOCS_HEADER)
        assert "## Artifact A" in text
        assert "### Snapshot 2026-01-01" in text
        assert "- x: 1" in text

    def test_appends_new_section_when_file_exists_without_it(self, tmp_path: Path) -> None:
        docs_path = tmp_path / "measurements.md"
        docs_path.write_text(DOCS_HEADER + "\n## Other Artifact\n\nsomething\n")
        append_measurement_section(
            docs_path,
            section_heading="## Artifact A",
            entry_markdown="### Snapshot 2026-01-01\n\n- x: 1\n",
        )
        text = docs_path.read_text(encoding="utf-8")
        assert "## Other Artifact" in text
        assert "## Artifact A" in text
        assert text.index("## Other Artifact") < text.index("## Artifact A")

    def test_inserts_dated_entry_inside_existing_section_without_duplicating_heading(
        self, tmp_path: Path
    ) -> None:
        docs_path = tmp_path / "measurements.md"
        append_measurement_section(
            docs_path,
            section_heading="## Artifact A",
            entry_markdown="### Snapshot 2026-01-01\n\n- x: 1\n",
        )
        append_measurement_section(
            docs_path,
            section_heading="## Artifact A",
            entry_markdown="### Snapshot 2026-01-02\n\n- x: 2\n",
        )
        text = docs_path.read_text(encoding="utf-8")
        assert text.count("## Artifact A") == 1
        assert "### Snapshot 2026-01-01" in text
        assert "### Snapshot 2026-01-02" in text
        # Earlier entry is preserved untouched, not overwritten.
        assert "- x: 1" in text
        assert "- x: 2" in text

    def test_never_replaces_an_earlier_entry(self, tmp_path: Path) -> None:
        docs_path = tmp_path / "measurements.md"
        for i in range(3):
            append_measurement_section(
                docs_path,
                section_heading="## Artifact A",
                entry_markdown=f"### Snapshot 2026-01-0{i}\n\n- n: {i}\n",
            )
        text = docs_path.read_text(encoding="utf-8")
        for i in range(3):
            assert f"- n: {i}" in text

    def test_does_not_touch_a_different_sibling_section(self, tmp_path: Path) -> None:
        docs_path = tmp_path / "measurements.md"
        append_measurement_section(
            docs_path,
            section_heading="## Artifact A",
            entry_markdown="### Snapshot 2026-01-01\n\n- x: 1\n",
        )
        append_measurement_section(
            docs_path,
            section_heading="## Artifact B",
            entry_markdown="### Snapshot 2026-01-01\n\n- y: 9\n",
        )
        append_measurement_section(
            docs_path,
            section_heading="## Artifact A",
            entry_markdown="### Snapshot 2026-01-02\n\n- x: 2\n",
        )
        text = docs_path.read_text(encoding="utf-8")
        assert text.count("## Artifact A") == 1
        assert text.count("## Artifact B") == 1
        assert "- y: 9" in text


class TestGeneratorWriterReplacesOnlyItsOwnSection:
    """Issue athenaeum#1095 AC6: a real generator's ``write_snapshot`` (not
    just the shared ``append_measurement_section`` primitive exercised
    above) must replace/update only its OWN section on a repeat run —
    never a sibling section, and never by duplicating its own heading."""

    def test_backlog_price_sheet_write_snapshot_preserves_sibling_section(
        self, tmp_path: Path
    ) -> None:
        from athenaeum import backlog_price_sheet as bps

        docs_path = tmp_path / "measurements.md"

        # Seed the doc with an unrelated sibling section FIRST.
        append_measurement_section(
            docs_path,
            section_heading="## Some Other Artifact",
            entry_markdown="### Snapshot 2026-01-01\n\n- z: 1\n",
        )
        before = docs_path.read_text(encoding="utf-8")

        raw_dir = tmp_path / "knowledge" / "raw" / "s"
        raw_dir.mkdir(parents=True)
        (raw_dir / "20260801T000000Z-aaaaaaaa.md").write_text("x")

        # Run the generator's OWN writer twice.
        result1 = bps.build_price_sheet(tmp_path / "knowledge")
        bps.write_snapshot(result1, docs_path=docs_path)
        result2 = bps.build_price_sheet(tmp_path / "knowledge")
        bps.write_snapshot(result2, docs_path=docs_path)

        after = docs_path.read_text(encoding="utf-8")

        # The sibling section (and everything before it) is byte-identical —
        # both generator runs only ever append after it / insert inside their
        # own later section.
        assert after.startswith(before)
        assert after.count("## Some Other Artifact") == 1
        assert after.count(bps.SECTION_HEADING) == 1


class TestReproducingSectionInvocations:
    """Issue athenaeum#1095 AC7(c): docs/memory-model-measurements.md's own
    'Reproducing the measurement pack' section must list all three exact
    invocations, pinned against each module's own REPRODUCE_COMMAND constant
    so the doc cannot silently drift out of sync with the CLI."""

    def test_docs_lists_all_three_exact_invocations(self) -> None:
        from athenaeum import backlog_price_sheet, ordinary_night_table, shadow_linkage

        text = _REPO_DOCS_PATH.read_text(encoding="utf-8")
        assert shadow_linkage.REPRODUCE_COMMAND in text
        assert backlog_price_sheet.REPRODUCE_COMMAND in text
        assert ordinary_night_table.REPRODUCE_COMMAND in text
