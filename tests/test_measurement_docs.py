# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared measurement-pack docs-append helper (issue athenaeum#713)."""

from __future__ import annotations

import argparse
from pathlib import Path

from athenaeum.measurement_docs import DOCS_HEADER, append_measurement_section

_REPO_DOCS_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "measurements" / "memory-model-measurements.md"
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


def _find_subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    """Return *parser*'s own ``_SubParsersAction``, or raise if it has none.

    Walking ``parser._actions`` (rather than the public-but-narrower
    ``add_subparsers()`` return value, which a caller several frames away
    does not have) is the standard way to recover a previously-registered
    subparsers action from an already-built ``ArgumentParser`` — used below
    to reach the REAL, currently-registered subcommand names instead of a
    second hand-copied literal.
    """
    for action in parser._actions:  # argparse has no public accessor for this
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError(f"no subparsers action found on {parser.prog!r}")


class TestReproducingSectionInvocations:
    """Issue athenaeum#1095 AC7(c): docs/measurements/memory-model-measurements.md's own
    'Reproducing the measurement pack' section must list all three exact
    invocations, pinned against each module's own REPRODUCE_COMMAND constant
    so the doc cannot silently drift out of sync with the CLI.

    Quine finding (athenaeum#1095 follow-up): the docs-contains assertion
    alone cannot catch CLI-vs-docs drift, because REPRODUCE_COMMAND is
    itself just a hardcoded string with no coupling to the argparse
    registration in ``_cmd_measure.py`` — renaming a subcommand there would
    leave this test green while the documented command 404s. Closing the
    loop requires BOTH links in the chain: CLI (real registered subparser
    choices, via ``athenaeum.cli.build_parser()``) -> REPRODUCE_COMMAND
    constant -> docs.
    """

    def test_reproduce_command_subcommand_is_actually_registered(self) -> None:
        import shlex

        from athenaeum import backlog_price_sheet, ordinary_night_table, shadow_linkage
        from athenaeum.cli import build_parser

        top_parser = build_parser()
        top_subparsers = _find_subparsers_action(top_parser)
        measure_parser = top_subparsers.choices["measure"]
        measure_subparsers = _find_subparsers_action(measure_parser)

        for module in (shadow_linkage, backlog_price_sheet, ordinary_night_table):
            tokens = shlex.split(module.REPRODUCE_COMMAND)
            assert tokens[:2] == ["athenaeum", "measure"], module.REPRODUCE_COMMAND
            assert tokens[2] in measure_subparsers.choices, (
                f"{module.__name__}.REPRODUCE_COMMAND names subcommand "
                f"{tokens[2]!r}, which is not among the actually-registered "
                f"`athenaeum measure` subparsers {sorted(measure_subparsers.choices)}"
            )

    def test_docs_lists_all_three_exact_invocations(self) -> None:
        from athenaeum import backlog_price_sheet, ordinary_night_table, shadow_linkage

        text = _REPO_DOCS_PATH.read_text(encoding="utf-8")
        assert shadow_linkage.REPRODUCE_COMMAND in text
        assert backlog_price_sheet.REPRODUCE_COMMAND in text
        assert ordinary_night_table.REPRODUCE_COMMAND in text
