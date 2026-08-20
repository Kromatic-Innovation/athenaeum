# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared measurement-pack docs-append helper (issue athenaeum#713)."""

from __future__ import annotations

from pathlib import Path

from athenaeum.measurement_docs import DOCS_HEADER, append_measurement_section


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
