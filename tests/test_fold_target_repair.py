# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.fold_target_repair` (issue athenaeum#2545 AC — the
optional ``athenaeum repair --fold-targets`` dry-run/``--apply`` repair for
a tombstone's dangling bare-slug ``folded_into``).
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.fold_target_repair import repair_dangling_fold_targets
from athenaeum.models import parse_frontmatter, tombstone_target
from athenaeum.provenance import default_merge_provenance_path, read_merge_provenance
from athenaeum.store import now_iso
from athenaeum.unfold import _path_for_slug, can_unfold_directly, find_fold_record
from athenaeum.verdicts import content_hash


def _write_page(path: Path, *, name: str, uid: str | None = None, body: str = "") -> None:
    fm = [f"name: {name}", "type: concept"]
    if uid:
        fm.append(f"uid: {uid}")
    path.write_text("---\n" + "\n".join(fm) + "\n---\n" + body, encoding="utf-8")


def _append_ledger_record(wiki_root: Path, record: dict) -> None:
    """Append one merge-provenance record directly, the same shape
    :func:`athenaeum.provenance.record_merge_provenance` writes — used here
    to fixture a PRE-athenaeum#2545 ledger entry (``canonical_slug`` ==
    the bare slug, matching what the fold write path stamped on
    ``folded_into`` before the fix) without going through a real fold."""
    path = default_merge_provenance_path(wiki_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


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


class TestRepairKeepsLedgerInSyncWithFoldedInto:
    """Review follow-up on issue athenaeum#2545: the repair must not patch
    ``folded_into`` while leaving the merge-provenance ledger's
    ``canonical_slug`` on the old bare slug -- that is the exact
    write/ledger desync the fold path itself was fixed to avoid, and
    leaving it unfixed here would re-break `find_fold_record`'s exact-match
    lookup for every tombstone this repair touches."""

    def _fixture(self, tmp_path: Path) -> tuple[Path, Path, Path, str]:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        canonical = wiki / "1a2b3c4d-release-checklist.md"
        _write_page(
            canonical, name="Release Checklist", uid="1a2b3c4d", body="canonical\n"
        )
        tombstone = wiki / "old-checklist.md"
        _write_tombstone(
            tombstone,
            name="Old Checklist",
            folded_into="release-checklist",
            body="tombstoned body\n",
        )
        canonical_hash = content_hash(canonical.read_text(encoding="utf-8"))
        # A ledger entry in the SHAPE the pre-athenaeum#2545 fold path wrote:
        # canonical_slug is the same dangling bare slug folded_into carries.
        _append_ledger_record(
            wiki,
            {
                "v": 2,
                "ts": now_iso(),
                "merge_id": "m-pre-fix",
                "write_kind": "fold-into-existing",
                "canonical_slug": "release-checklist",
                "source_paths": [str(tombstone)],
                "auto_applied": False,
                "folded_sources": [str(tombstone)],
                "aliases_added": [],
                "links_rewritten": [],
                "canonical_content_hash": canonical_hash,
                "coordinates_widened": {},
            },
        )
        return wiki, canonical, tombstone, canonical_hash

    def test_apply_rewrites_ledger_canonical_slug_to_match(self, tmp_path: Path) -> None:
        wiki, _canonical, tombstone, _hash = self._fixture(tmp_path)

        report = repair_dangling_fold_targets(wiki, apply=True)

        assert report.repaired == 1
        assert report.ledger_records_updated == 1
        meta, _ = parse_frontmatter(tombstone.read_text(encoding="utf-8"))
        new_value = tombstone_target(meta)
        assert new_value == "1a2b3c4d-release-checklist"

        records = read_merge_provenance(wiki)
        assert len(records) == 1
        assert records[0]["canonical_slug"] == new_value

    def test_dry_run_reports_ledger_count_without_writing(self, tmp_path: Path) -> None:
        wiki, _canonical, _tombstone, _hash = self._fixture(tmp_path)

        report = repair_dangling_fold_targets(wiki, apply=False)

        assert report.ledger_records_updated == 1
        records = read_merge_provenance(wiki)
        assert records[0]["canonical_slug"] == "release-checklist"  # unwritten

    def test_can_unfold_directly_holds_after_repair(self, tmp_path: Path) -> None:
        """End-to-end: after the repair, unfold's own lookup chain --
        tombstone_target -> _path_for_slug -> find_fold_record ->
        can_unfold_directly -- must still say yes for an untouched
        canonical, exactly as it would have for a bare-slug one."""
        wiki, canonical, tombstone, _hash = self._fixture(tmp_path)

        report = repair_dangling_fold_targets(wiki, apply=True)
        assert report.repaired == 1
        assert report.ledger_records_updated == 1

        meta, _ = parse_frontmatter(tombstone.read_text(encoding="utf-8"))
        canonical_slug = tombstone_target(meta)
        canonical_path = _path_for_slug(wiki, canonical_slug)
        assert canonical_path == canonical

        record = find_fold_record(wiki, tombstone, canonical_slug)
        assert record is not None
        ok, _reason = can_unfold_directly(record, canonical_path)
        assert ok is True

    def test_malformed_ledger_line_is_preserved_verbatim(self, tmp_path: Path) -> None:
        """A torn trailing write / hand-edit next to the record being
        rewritten must survive byte-for-byte, same tolerance
        read_merge_provenance already extends it."""
        wiki, _canonical, _tombstone, _hash = self._fixture(tmp_path)
        ledger_path = default_merge_provenance_path(wiki)
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write("{not valid json\n")

        report = repair_dangling_fold_targets(wiki, apply=True)
        assert report.ledger_records_updated == 1

        lines = ledger_path.read_text(encoding="utf-8").splitlines()
        assert lines[-1] == "{not valid json"
