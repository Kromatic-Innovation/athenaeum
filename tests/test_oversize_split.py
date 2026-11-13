# SPDX-License-Identifier: Apache-2.0
"""``athenaeum.oversize_split.find_split_candidates`` — the walk/filter pass
behind the operator-facing ``athenaeum split-oversize`` command (issue
athenaeum#2078 AC3).

This is the walk ``athenaeum.tiers.enumerate_oversize_pages`` used to
duplicate (same ``wiki_root.glob("*.md")``, same ``_``-prefix skip, same
frontmatter-stripped body measurement) before issue athenaeum#2436 removed
that dead read-only AC3 helper (0 src callers; see that issue's evidence
table). The two functions select on different predicates --
``find_split_candidates`` returns a page that is EITHER flagged
``needs_split: true`` (the athenaeum#2078 gate's in-place flag) OR over the
hard ceiling (:func:`~athenaeum.config.resolve_oversize_hard_ceiling_chars`),
not merely over the ordinary page-size threshold -- so this suite is a
fresh unit suite for the real predicate, not a renamed copy of the removed
one.

No LLM, no network.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.oversize_split import find_split_candidates


def _low_ceiling_config(ceiling: int) -> dict:
    return {"librarian": {"oversize_hard_ceiling_chars": ceiling}}


class TestFindSplitCandidates:
    def test_flagged_pages_are_returned_regardless_of_size(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "flagged.md").write_text(
            "---\nuid: aaaa1111\nneeds_split: true\nname: Flagged\n---\n\nShort body.\n"
        )

        results = find_split_candidates(wiki)

        assert [c.path.name for c in results] == ["flagged.md"]
        assert results[0].flagged is True
        assert results[0].over_ceiling is False
        assert results[0].uid == "aaaa1111"

    def test_pages_over_hard_ceiling_are_returned_even_unflagged(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        big_body = "x" * 200
        (wiki / "big.md").write_text(
            f"---\nuid: bbbb2222\nname: Big\n---\n\n{big_body}\n"
        )

        results = find_split_candidates(wiki, _low_ceiling_config(50))

        assert [c.path.name for c in results] == ["big.md"]
        assert results[0].flagged is False
        assert results[0].over_ceiling is True

    def test_pages_neither_flagged_nor_over_ceiling_are_excluded(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "ordinary.md").write_text(
            "---\nuid: cccc3333\nname: Ordinary\n---\n\nShort body.\n"
        )

        assert find_split_candidates(wiki) == []

    def test_underscore_prefixed_files_are_skipped(self, tmp_path: Path) -> None:
        """Mirrors EntityIndex._load's _-prefix exclusion -- the genuinely
        huge _-prefixed files are already correctly excluded from the
        entity index and never merged into, so they must never surface as
        split candidates either."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_pending_merges_archive.md").write_text(
            "needs_split: true\n" + ("x" * 200)
        )

        assert find_split_candidates(wiki) == []

    def test_sorted_largest_first(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "a-small.md").write_text(
            "---\nuid: dddd4444\nneeds_split: true\nname: A\n---\n\n" + ("x" * 50) + "\n"
        )
        (wiki / "b-large.md").write_text(
            "---\nuid: eeee5555\nneeds_split: true\nname: B\n---\n\n" + ("x" * 5000) + "\n"
        )

        results = find_split_candidates(wiki)

        assert [c.path.name for c in results] == ["b-large.md", "a-small.md"]

    def test_missing_uid_reports_empty_string(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "no-uid.md").write_text(
            "---\nneeds_split: true\nname: No Uid\n---\n\nShort body.\n"
        )

        results = find_split_candidates(wiki)

        assert len(results) == 1
        assert results[0].uid == ""

    def test_empty_wiki_returns_empty_list(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        assert find_split_candidates(wiki) == []
