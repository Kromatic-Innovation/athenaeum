# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.fold_target_repair` (issue athenaeum#2545 AC — the
optional ``athenaeum repair --fold-targets`` dry-run/``--apply`` repair for
a tombstone's dangling bare-slug ``folded_into``).
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.fold_target_repair import repair_dangling_fold_targets
from athenaeum.models import parse_frontmatter, tombstone_target


def _write_page(path: Path, *, name: str, uid: str | None = None, body: str = "") -> None:
    fm = [f"name: {name}", "type: concept"]
    if uid:
        fm.append(f"uid: {uid}")
    path.write_text("---\n" + "\n".join(fm) + "\n---\n" + body, encoding="utf-8")


def _write_tombstone(path: Path, *, name: str, folded_into: str, body: str = "") -> None:
    path.write_text(
        "---\n"
        f"name: {name}\n"
        "type: concept\n"
        "status: folded\n"
        f"folded_into: {folded_into}\n"
        "embedded: false\n"
        "---\n" + body,
        encoding="utf-8",
    )


class TestDryRunDefault:
    def test_dry_run_reports_without_writing(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        canonical = wiki / "1a2b3c4d-release-checklist.md"
        _write_page(canonical, name="Release Checklist", uid="1a2b3c4d")
        tombstone = wiki / "old-checklist.md"
        _write_tombstone(
            tombstone, name="Old Checklist", folded_into="release-checklist"
        )

        report = repair_dangling_fold_targets(wiki, apply=False)

        assert report.dangling == 1
        assert len(report.changes) == 1
        path, old, new = report.changes[0]
        assert path == tombstone
        assert old == "release-checklist"
        assert new == "1a2b3c4d-release-checklist"
        assert report.ambiguous == []

        # Nothing written — dry-run.
        meta, _ = parse_frontmatter(tombstone.read_text(encoding="utf-8"))
        assert tombstone_target(meta) == "release-checklist"


class TestApplyRewritesDanglingValue:
    def test_apply_rewrites_exactly_one_live_candidate(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        canonical = wiki / "1a2b3c4d-release-checklist.md"
        _write_page(canonical, name="Release Checklist", uid="1a2b3c4d")
        tombstone = wiki / "old-checklist.md"
        _write_tombstone(
            tombstone,
            name="Old Checklist",
            folded_into="release-checklist",
            body="tombstoned body\n",
        )

        report = repair_dangling_fold_targets(wiki, apply=True)

        assert report.dangling == 1
        assert report.repaired == 1
        assert report.ambiguous == []

        meta, body = parse_frontmatter(tombstone.read_text(encoding="utf-8"))
        assert tombstone_target(meta) == "1a2b3c4d-release-checklist"
        # Every other key, and the body, survive untouched.
        assert meta["status"] == "folded"
        assert meta["embedded"] is False
        assert body == "tombstoned body\n"

    def test_already_resolvable_value_is_left_alone(self, tmp_path: Path) -> None:
        """A tombstone already stamped with the stem form (post-fix) is not
        'dangling' at all — nothing to repair."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        canonical = wiki / "1a2b3c4d-release-checklist.md"
        _write_page(canonical, name="Release Checklist", uid="1a2b3c4d")
        tombstone = wiki / "old-checklist.md"
        _write_tombstone(
            tombstone, name="Old Checklist", folded_into="1a2b3c4d-release-checklist"
        )

        report = repair_dangling_fold_targets(wiki, apply=True)

        assert report.dangling == 0
        assert report.changes == []

    def test_bare_slug_canonical_is_already_resolvable(self, tmp_path: Path) -> None:
        """A tombstone folded into a bare-slug canonical (<slug>.md) was never
        dangling in the first place."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        canonical = wiki / "canonical.md"
        _write_page(canonical, name="Canonical")
        tombstone = wiki / "old-a.md"
        _write_tombstone(tombstone, name="Old A", folded_into="canonical")

        report = repair_dangling_fold_targets(wiki, apply=True)

        assert report.dangling == 0
        assert report.changes == []


class TestAmbiguousIsReportedNotRewritten:
    def test_zero_candidates_is_ambiguous_and_untouched(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        tombstone = wiki / "orphan.md"
        _write_tombstone(tombstone, name="Orphan", folded_into="nowhere")

        report = repair_dangling_fold_targets(wiki, apply=True)

        assert report.dangling == 1
        assert report.changes == []
        assert len(report.ambiguous) == 1
        name, folded_into, candidates = report.ambiguous[0]
        assert name == "orphan.md"
        assert folded_into == "nowhere"
        assert candidates == []

        meta, _ = parse_frontmatter(tombstone.read_text(encoding="utf-8"))
        assert tombstone_target(meta) == "nowhere"

    def test_two_live_candidates_is_ambiguous_and_untouched(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # Two DIFFERENT live pages both end with "-shared.md" and both
        # identity-resolve to the "shared" slug via their own uid prefix +
        # name:, so the bare slug is genuinely ambiguous.
        page_a = wiki / "aaaa0000-shared.md"
        _write_page(page_a, name="Shared", uid="aaaa0000")
        page_b = wiki / "bbbb1111-shared.md"
        _write_page(page_b, name="Shared", uid="bbbb1111")
        tombstone = wiki / "old-shared.md"
        _write_tombstone(tombstone, name="Old Shared", folded_into="shared")

        report = repair_dangling_fold_targets(wiki, apply=True)

        assert report.dangling == 1
        assert report.changes == []
        assert len(report.ambiguous) == 1
        name, folded_into, candidates = report.ambiguous[0]
        assert name == "old-shared.md"
        assert folded_into == "shared"
        assert sorted(candidates) == ["aaaa0000-shared.md", "bbbb1111-shared.md"]

        meta, _ = parse_frontmatter(tombstone.read_text(encoding="utf-8"))
        assert tombstone_target(meta) == "shared"
