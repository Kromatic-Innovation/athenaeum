# SPDX-License-Identifier: Apache-2.0
"""``athenaeum.page_recompile`` -- candidate scan and unit segmenter (issue
athenaeum#2534, slice S1 of the page-recompile design, athenaeum#2525).

No LLM, no network, no page write -- everything here is pure/read-only.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.models import render_frontmatter
from athenaeum.page_recompile import (
    _pack_at_boundaries,
    check_conservation,
    check_marker_resolution,
    find_recompile_candidates,
    segment_page,
)


def _write(wiki: Path, uid: str, body: str, **meta_extra: object) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {"uid": uid, "type": "reference", "name": uid}
    meta.update(meta_extra)
    path = wiki / f"{uid}.md"
    path.write_text(render_frontmatter(meta) + body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC1: candidate scan excludes inactive pages.
# ---------------------------------------------------------------------------


class TestFindRecompileCandidatesAC1:
    def test_tombstoned_retired_superseded_excluded_active_included(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        over_body = "x" * 50
        _write(wiki, "active01", over_body, needs_split=True)
        _write(
            wiki,
            "tomb0001",
            over_body,
            needs_split=True,
            status="folded",
            folded_into="active01",
        )
        _write(wiki, "retd0001", over_body, needs_split=True, retired=True)
        _write(wiki, "supr0001", over_body, needs_split=True, superseded_by="active01")

        scan = find_recompile_candidates(wiki, {"librarian": {"page_size_threshold_chars": 10}})

        assert [c.uid for c in scan.candidates] == ["active01"]
        assert scan.blocked == ()


# ---------------------------------------------------------------------------
# AC2: a page the pass cannot act on is blocked, never dropped.
# ---------------------------------------------------------------------------


class TestBlockedBucketAC2:
    def test_flagged_page_with_no_uid_is_blocked_no_uid(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "noUid.md").write_text(
            "---\nneeds_split: true\nname: NoUid\n---\n\nShort body.\n",
            encoding="utf-8",
        )

        scan = find_recompile_candidates(wiki)

        assert scan.candidates == ()
        assert len(scan.blocked) == 1
        assert scan.blocked[0].reason == "no_uid"

    def test_invalid_frontmatter_is_blocked(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # Frontmatter block present but parses to a YAML list, not a mapping.
        (wiki / "listfm.md").write_text("---\n- a\n- b\n---\n\nBody.\n", encoding="utf-8")

        scan = find_recompile_candidates(wiki)

        assert scan.candidates == ()
        assert [b.reason for b in scan.blocked] == ["invalid_frontmatter"]


# ---------------------------------------------------------------------------
# AC3: deterministic ordering -- discriminates from plain size order.
# ---------------------------------------------------------------------------


class TestOrderingAC3:
    def test_order_differs_from_size_order_alone(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        config = {
            "librarian": {
                "page_size_threshold_chars": 10,
                "oversize_hard_ceiling_chars": 100,
            }
        }
        # B: over the hard ceiling (120 chars) -- ranks first regardless of
        # its own recency/size relative to the others.
        _write(wiki, "pageB001", "x" * 120, updated="2020-01-01")
        # C: under ceiling, MOST recently updated, medium size.
        _write(wiki, "pageC001", "x" * 20, updated="2026-11-10")
        # D: under ceiling, mid-recency, LARGER than C -- a plain size sort
        # would put D ahead of C; the recency rule must not.
        _write(wiki, "pageD001", "x" * 80, updated="2026-06-01")
        # A: under ceiling, oldest update, smallest size.
        _write(wiki, "pageA001", "x" * 11, updated="2026-01-01")

        scan = find_recompile_candidates(wiki, config)

        assert [c.uid for c in scan.candidates] == [
            "pageB001",
            "pageC001",
            "pageD001",
            "pageA001",
        ]
        size_order = [
            c.uid for c in sorted(scan.candidates, key=lambda c: c.chars, reverse=True)
        ]
        assert size_order == ["pageB001", "pageD001", "pageC001", "pageA001"]
        assert size_order != [c.uid for c in scan.candidates]


# ---------------------------------------------------------------------------
# AC4: segmenter -- ordered units with id, heading path, text, footnotes.
# ---------------------------------------------------------------------------


class TestSegmentPageAC4:
    def test_units_carry_ordinal_hash_heading_path_text_footnotes(self) -> None:
        body = (
            "## Section One\n\n"
            "First para cites a fact.[^src-1]\n\n"
            "## Section Two\n\n"
            "Second para cites another.[^src-2]\n\n"
            "[^src-1]: **Source:** a\n"
            "[^src-2]: **Source:** b\n"
        )

        result = segment_page(body)

        assert [u.heading_path for u in result.units] == [
            ("Section One",),
            ("Section Two",),
        ]
        assert [u.ordinal for u in result.units] == [0, 1]
        assert result.units[0].unit_id.startswith("0000-")
        assert result.units[1].unit_id.startswith("0001-")
        assert result.units[0].footnote_labels == ("src-1",)
        assert result.units[1].footnote_labels == ("src-2",)
        # Footnote DEFINITIONS are parsed separately, not folded into a unit.
        assert result.footnote_block == "[^src-1]: **Source:** a\n[^src-2]: **Source:** b\n"


# ---------------------------------------------------------------------------
# AC5: title detection reuses tiers._split_into_atomic_sections.
# ---------------------------------------------------------------------------


class TestSegmentPageAC5TitleDetection:
    def test_lone_title_with_five_sections_yields_five_heading_groups(self) -> None:
        body = (
            "# Name\n\n"
            "## A\n\ntext a\n\n"
            "## B\n\ntext b\n\n"
            "## C\n\ntext c\n\n"
            "## D\n\ntext d\n\n"
            "## E\n\ntext e\n"
        )

        result = segment_page(body)

        heading_groups = {u.heading_path for u in result.units if u.heading_path}
        assert heading_groups == {("A",), ("B",), ("C",), ("D",), ("E",)}
        # The lone "# Name" heading folds into the leading no-heading group
        # (issue athenaeum#2524's title fix) -- it must NOT surface as a
        # sixth, same-named group of its own.
        assert not any(u.heading_path == ("Name",) for u in result.units)
        assert any(u.heading_path == () for u in result.units)


# ---------------------------------------------------------------------------
# AC6: oversize leaf cut at paragraph/list-item boundaries; byte-identity.
# ---------------------------------------------------------------------------


class TestSegmentPageAC6PropertyAndCounterExample:
    def test_units_plus_footnote_block_reproduce_body_byte_for_byte(self) -> None:
        body = (
            "## Section\n\n"
            + ("Paragraph one with enough words to pad it out. " * 60)
            + "\n\n"
            + "- item one\n- item two\n- item three\n"
            + ("- item four is long enough to matter here too. " * 40)
            + "\n\n"
            + "[^src-1]: **Source:** somewhere\n"
        )

        result = segment_page(body)

        reconstructed = "".join(u.text for u in result.units) + result.footnote_block
        assert reconstructed == body
        # The cap actually bit: more than one unit came out of the single
        # heading group, proving the paragraph/list-item cut fired.
        assert len(result.units) > 1

    def test_counter_example_dropping_one_chunk_fails_the_property(self) -> None:
        """A stub that drops a chunk must fail byte-identity -- otherwise
        the property test above would be proving nothing."""
        body = "para one.\n\npara two.\n\npara three.\n"

        chunks = _pack_at_boundaries(body, cap=1)

        assert len(chunks) >= 2, "need at least 2 chunks to have one to drop"
        broken = chunks[:1] + chunks[2:]  # drop the second chunk
        assert "".join(broken) != body


# ---------------------------------------------------------------------------
# AC7: pure checkers -- conservation and marker resolution.
# ---------------------------------------------------------------------------


class TestCheckConservationAC7:
    def test_passing_case_every_unit_accounted_for_exactly_once(self) -> None:
        result = check_conservation(["a", "b", "c"], {"dest1": ["a", "b"], "dest2": ["c"]})

        assert result.ok is True
        assert result.missing == ()
        assert result.duplicated == ()

    def test_failing_case_missing_and_duplicated(self) -> None:
        result = check_conservation(["a", "b", "c"], {"dest1": ["a", "a"], "dest2": []})

        assert result.ok is False
        assert result.missing == ("b", "c")
        assert result.duplicated == ("a",)


class TestCheckMarkerResolutionAC7:
    def test_passing_case_every_marker_has_a_definition(self) -> None:
        body = "Claim one.[^src-1]\n\n[^src-1]: **Source:** x\n"

        result = check_marker_resolution(body)

        assert result.ok is True
        assert result.dangling == ()

    def test_failing_case_dangling_marker(self) -> None:
        body = "Claim one.[^src-1]\n\nNo definitions here.\n"

        result = check_marker_resolution(body)

        assert result.ok is False
        assert result.dangling == ("src-1",)
