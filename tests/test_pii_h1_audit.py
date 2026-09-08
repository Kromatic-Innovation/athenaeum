# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.pii_h1_audit` (issue athenaeum#1461).

Fixture-only verification: every test builds a synthetic ``wiki/`` under
``tmp_path``; nothing touches a live corpus. Covers the issue's five
acceptance criteria:

- AC1/AC2 — ``TestClassification`` and ``TestFindH1MarkerPages``: the
  marker-leading (heading-subject-consumed) vs. marker-mid-heading
  (title-otherwise-intact) split, classified rather than reported flat.
- AC3 — ``TestNameFieldNeverGatesClassification``: an unredacted ``name:``
  on a class-(b) page must still classify as (b), never treated as a defect
  signal.
- AC5 — ``TestReadOnly``: the audit never writes.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.pii_h1_audit import (
    MARKER,
    MARKER_LEADING,
    MARKER_MID_HEADING,
    classify_h1_marker,
    find_h1_marker_pages,
    render_report,
)


def _write_page(wiki_root: Path, filename: str, frontmatter: str, body: str) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / filename
    path.write_text(f"---\n{frontmatter}---\n{body}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# AC1/AC2 -- classify_h1_marker: the discriminating signal
# --------------------------------------------------------------------------- #


class TestClassification:
    def test_marker_leading_with_residual_punctuation_only(self) -> None:
        # The marker followed only by residual punctuation -- the marker
        # consumed the entire heading subject.
        assert classify_h1_marker(f"{MARKER}???") == MARKER_LEADING

    def test_marker_leading_bare(self) -> None:
        assert classify_h1_marker(MARKER) == MARKER_LEADING

    def test_marker_leading_with_trailing_whitespace_and_punctuation(self) -> None:
        assert classify_h1_marker(f"{MARKER}  -- ") == MARKER_LEADING

    def test_marker_mid_heading_title_otherwise_intact(self) -> None:
        # A real title with the marker substituted for one inline token
        # mid-line -- the marker doing its documented job.
        heading = f"Notes from a call with {MARKER} about Q3 renewal"
        assert classify_h1_marker(heading) == MARKER_MID_HEADING

    def test_marker_mid_heading_marker_at_start_but_title_continues(self) -> None:
        # Marker-LEADING position is not itself the signal -- what survives
        # after stripping the marker is. A word character surviving means
        # the heading still reads as a title even though the marker sits
        # at the front.
        heading = f"{MARKER} project retrospective"
        assert classify_h1_marker(heading) == MARKER_MID_HEADING


# --------------------------------------------------------------------------- #
# AC1/AC2 -- find_h1_marker_pages: end-to-end over a fixture corpus
# --------------------------------------------------------------------------- #


class TestFindH1MarkerPages:
    def test_classifies_leading_and_mid_heading_separately(self, tmp_path: Path) -> None:
        wiki = tmp_path / "knowledge" / "wiki"
        _write_page(
            wiki,
            "leading.md",
            "uid: a1\nname: Leading Page\ntype: person\n",
            f"# {MARKER}???\n\nBody text.",
        )
        _write_page(
            wiki,
            "mid.md",
            "uid: a2\nname: Mid Page\ntype: person\n",
            f"# Notes from a call with {MARKER} about Q3\n\nBody text.",
        )

        findings = find_h1_marker_pages(wiki)

        assert len(findings) == 2
        by_file = {f.page_relpath.split("/")[-1]: f for f in findings}
        assert by_file["leading.md"].classification == MARKER_LEADING
        assert by_file["mid.md"].classification == MARKER_MID_HEADING

    def test_marker_mid_heading_not_reported_as_defect_in_report(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "knowledge" / "wiki"
        _write_page(
            wiki,
            "mid.md",
            "uid: a2\nname: Mid Page\ntype: person\n",
            f"# Notes from a call with {MARKER} about Q3\n\nBody text.",
        )

        findings = find_h1_marker_pages(wiki)
        report = render_report(findings)

        assert findings[0].classification == MARKER_MID_HEADING
        assert "[DEFECT] marker-leading" in report
        assert "marker-leading / heading-subject-consumed: 0 page(s)" in report
        assert "mid.md" not in report.split("[NOT A DEFECT]")[0]
        assert "mid.md" in report.split("[NOT A DEFECT]")[1]

    def test_page_with_no_h1_marker_is_not_reported(self, tmp_path: Path) -> None:
        wiki = tmp_path / "knowledge" / "wiki"
        # Marker present, but not on the H1 line.
        _write_page(
            wiki,
            "clean-heading.md",
            "uid: a3\nname: Clean Heading\ntype: person\n",
            f"# A perfectly normal title\n\nReach out at {MARKER} for intros.",
        )

        findings = find_h1_marker_pages(wiki)

        assert findings == []

    def test_page_with_no_marker_at_all_is_not_reported(self, tmp_path: Path) -> None:
        wiki = tmp_path / "knowledge" / "wiki"
        _write_page(
            wiki, "unaffected.md", "uid: a4\nname: Unaffected\ntype: person\n", "# A title\n\nBody."
        )

        findings = find_h1_marker_pages(wiki)

        assert findings == []

    def test_excluded_surface_page_never_a_candidate(self, tmp_path: Path) -> None:
        wiki = tmp_path / "knowledge" / "wiki"
        _write_page(
            wiki / "excluded",
            "decoy.md",
            "uid: a5\nname: Decoy\ntype: person\n",
            f"# {MARKER}???\n\nBody.",
        )

        findings = find_h1_marker_pages(wiki)

        assert findings == []


# --------------------------------------------------------------------------- #
# AC3 -- an unredacted name: field never gates classification
# --------------------------------------------------------------------------- #


class TestNameFieldNeverGatesClassification:
    def test_class_b_page_with_unredacted_name_field_stays_class_b(
        self, tmp_path: Path
    ) -> None:
        # A class-(b) fixture page whose frontmatter name: is unredacted
        # plain text is STILL classified (b) -- an unredacted name: is a
        # deliberate durable-identifier preservation (athenaeum#502), not
        # evidence of a broken redaction, and must never be treated as a
        # defect signal.
        wiki = tmp_path / "knowledge" / "wiki"
        _write_page(
            wiki,
            "priya.md",
            "uid: a6\nname: Priya Patel\ntype: person\n",  # unredacted plain-text name
            f"# Notes from a call with {MARKER} about renewal\n\nBody text.",
        )

        findings = find_h1_marker_pages(wiki)

        assert len(findings) == 1
        assert findings[0].classification == MARKER_MID_HEADING
        assert findings[0].name_field_unredacted is False  # a real name, not PII-shaped

    def test_name_field_pii_flag_is_informational_only_never_a_defect_signal(
        self, tmp_path: Path
    ) -> None:
        # Even when name: genuinely IS email/phone-shaped (the athenaeum#502
        # carve-out population), that fact rides along on the finding as
        # `name_field_unredacted` but never changes the classification --
        # the report must not read it as evidence of a defect either way.
        wiki = tmp_path / "knowledge" / "wiki"
        _write_page(
            wiki,
            "kim.md",
            "uid: a7\nname: kim@streak.example\ntype: person\n",
            f"# {MARKER}???\n\nBody text.",
        )

        findings = find_h1_marker_pages(wiki)

        assert len(findings) == 1
        assert findings[0].classification == MARKER_LEADING
        assert findings[0].name_field_unredacted is True
        report = render_report(findings)
        # The name-field note is attached only as an annotation on the
        # defect line, not as a second, separately-counted defect class.
        assert "kim.md" in report
        assert report.count("kim.md") == 1


# --------------------------------------------------------------------------- #
# AC5 -- read-only
# --------------------------------------------------------------------------- #


class TestReadOnly:
    def test_find_h1_marker_pages_never_writes(self, tmp_path: Path) -> None:
        wiki = tmp_path / "knowledge" / "wiki"
        page = _write_page(
            wiki,
            "leading.md",
            "uid: a1\nname: Leading Page\ntype: person\n",
            f"# {MARKER}???\n\nBody text.",
        )
        before_bytes = page.read_bytes()
        before_mtime = page.stat().st_mtime_ns

        find_h1_marker_pages(wiki)

        assert page.read_bytes() == before_bytes
        assert page.stat().st_mtime_ns == before_mtime

    def test_render_report_never_writes(self, tmp_path: Path) -> None:
        wiki = tmp_path / "knowledge" / "wiki"
        page = _write_page(
            wiki,
            "leading.md",
            "uid: a1\nname: Leading Page\ntype: person\n",
            f"# {MARKER}???\n\nBody text.",
        )
        before_bytes = page.read_bytes()

        findings = find_h1_marker_pages(wiki)
        render_report(findings)

        assert page.read_bytes() == before_bytes


def test_find_h1_marker_pages_missing_wiki_root_returns_empty(tmp_path: Path) -> None:
    findings = find_h1_marker_pages(tmp_path / "does-not-exist")
    assert findings == []


def test_find_h1_marker_pages_respects_limit(tmp_path: Path) -> None:
    wiki = tmp_path / "knowledge" / "wiki"
    for i in range(3):
        _write_page(
            wiki,
            f"page{i}.md",
            f"uid: b{i}\nname: Page {i}\ntype: person\n",
            f"# {MARKER}???\n\nBody.",
        )

    findings = find_h1_marker_pages(wiki, limit=2)

    assert len(findings) == 2
