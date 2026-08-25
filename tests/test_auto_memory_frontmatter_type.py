# SPDX-License-Identifier: Apache-2.0
"""Auto-memory discovery by declared type, and the audit false positives it
left behind.

Three defects, measured against the live store on 2026-08-25 and fixed
together because they are the same mistake seen from two sides -- discovery
and the audit that is supposed to backstop discovery disagreeing about what
counts as claimed:

- ``TestFrontmatterTypeFallback`` -- ``discover_auto_memory_files`` claimed a
  file only when its FILENAME carried a ``feedback_``/``project_``/... prefix.
  Claude Code's memory writer emits ``<kebab-slug>.md`` and puts the type in
  ``metadata.type``, so 186 of 188 real memories on the live store were
  invisible to every discovery path, and the count grew every session.
- ``TestOneLevelDescentNotFlagged`` -- athenaeum#974 gave ``discover_raw_files``
  a one-level walk below each source dir; the audit never modelled it, so
  2849 of 7622 flagged files were reported ``unrecognised shape`` WHILE BEING
  CLAIMED AND QUEUED.
- ``TestLibrarianOwnFilesNotFlagged`` -- ``raw/_librarian-clusters-*.jsonl`` is
  the librarian's own output; the audit raised operator decisions about it.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.intake import (
    auto_memory_type_from_frontmatter,
    discover_auto_memory_files,
)
from athenaeum.intake_audit import (
    REASON_MISSING_NAMING_CONVENTION,
    REASON_UNMATCHED_EXTENSION,
    REASON_UNRECOGNISED_SHAPE,
    find_unclaimed_raw_files,
)

#: The exact shape Claude Code's memory writer emits.
MEMORY_DOC = """\
---
name: absence-is-not-success
description: a verifier read a partially-populated state and reported success
metadata:
  type: feedback
---

A check that reads an empty state and concludes "fine" is a recurring defect.
"""


def _write(root: Path, rel: str, content: str = "hello\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _doc(memory_type: str, *, scalar: bool = False) -> str:
    block = (
        f"metadata: {memory_type}"
        if scalar
        else f"metadata:\n  type: {memory_type}"
    )
    return f"---\nname: x\n{block}\n---\n\nbody text\n"


class TestAutoMemoryTypeFromFrontmatter:
    """The shared predicate. Closed vocabulary, fails closed."""

    def test_reads_metadata_type(self) -> None:
        assert auto_memory_type_from_frontmatter({"metadata": {"type": "feedback"}}) == "feedback"

    def test_reads_scalar_metadata_block(self) -> None:
        # Observed on the live store: `metadata: feedback` with no `type:` key.
        assert auto_memory_type_from_frontmatter({"metadata": "reference"}) == "reference"

    def test_reads_top_level_memory_type(self) -> None:
        assert auto_memory_type_from_frontmatter({"memory_type": "Project"}) == "project"

    def test_rejects_type_outside_vocabulary(self) -> None:
        # `concept` is a real value on the live store. An arbitrary `type:`
        # must never turn an entity page into auto-memory intake.
        assert auto_memory_type_from_frontmatter({"metadata": {"type": "concept"}}) is None
        assert auto_memory_type_from_frontmatter({"type": "person"}) is None

    def test_fails_closed_on_junk(self) -> None:
        for meta in (None, {}, {"metadata": None}, {"metadata": {"type": 7}}, "nope"):
            assert auto_memory_type_from_frontmatter(meta) is None


class TestFrontmatterTypeFallback:
    def test_kebab_slug_file_is_discovered(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/repo-a/absence-is-not-success.md", MEMORY_DOC)
        found = discover_auto_memory_files(tmp_path, None)
        assert [f.path.name for f in found] == ["absence-is-not-success.md"]
        assert found[0].memory_type == "feedback"

    def test_conforming_filename_still_wins(self, tmp_path: Path) -> None:
        # The filename prefix remains authoritative when present, even if the
        # frontmatter disagrees -- this fix widens WHERE the type may be
        # declared, it does not change precedence.
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/repo-a/project_thing.md", _doc("feedback"))
        found = discover_auto_memory_files(tmp_path, None)
        assert [f.memory_type for f in found] == ["project"]

    def test_untyped_file_still_skipped(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/repo-a/no-frontmatter.md", "just prose\n")
        assert discover_auto_memory_files(tmp_path, None) == []

    def test_entity_schema_file_still_falls_through(self, tmp_path: Path) -> None:
        # <timestamp>-<uuid8>.md in a scope dir belongs to the entity tier.
        # It must not be claimed as auto-memory even if it declares a type.
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/repo-a/20260810T120000Z-abcdef01.md", _doc("project"))
        assert discover_auto_memory_files(tmp_path, None) == []

    def test_audit_no_longer_flags_a_now_claimed_file(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/repo-a/absence-is-not-success.md", MEMORY_DOC)
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_audit_still_flags_an_untyped_file(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/repo-a/mystery.md", "no frontmatter here\n")
        found = find_unclaimed_raw_files(raw, tmp_path)
        assert [f.reason for f in found] == [REASON_MISSING_NAMING_CONVENTION]
        assert found[0].group_key == "auto-memory/repo-a"


class TestOneLevelDescentNotFlagged:
    def test_md_in_source_subdir_is_claimed(self, tmp_path: Path) -> None:
        # The live-store shape: raw/drive/kromatic-intake/<uid>-<slug>.md
        raw = tmp_path / "raw"
        _write(raw, "drive/kromatic-intake/8d69fdc7-afce.md", "---\n---\nbody\n")
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_jsonl_in_source_subdir_is_claimed(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "drive/oep-intake/rows.jsonl", '{"a": 1}\n')
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_wrong_extension_in_source_subdir_still_flagged(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "drive/oep-intake/export.csv", "a,b\n")
        found = find_unclaimed_raw_files(raw, tmp_path)
        assert [f.reason for f in found] == [REASON_UNMATCHED_EXTENSION]
        assert found[0].group_key == "drive/oep-intake"

    def test_two_levels_deep_still_unrecognised(self, tmp_path: Path) -> None:
        # discover_raw_files descends EXACTLY one level, never deeper.
        raw = tmp_path / "raw"
        _write(raw, "drive/oep-intake/nested/deep.md", "---\n---\nbody\n")
        found = find_unclaimed_raw_files(raw, tmp_path)
        assert [f.reason for f in found] == [REASON_UNRECOGNISED_SHAPE]


class TestLibrarianOwnFilesNotFlagged:
    def test_cluster_sidecar_at_raw_root_ignored(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "_librarian-clusters-20260825T162443Z.jsonl", '{"c": 1}\n')
        _write(raw, "_librarian-clusters.jsonl", '{"c": 1}\n')
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_ds_store_ignored_anywhere(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, ".DS_Store", "\x00")
        _write(raw, "mural/.DS_Store", "\x00")
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_ordinary_loose_file_at_raw_root_still_flagged(self, tmp_path: Path) -> None:
        # Only leading-underscore names are the librarian's own; a genuine
        # stray must still raise.
        raw = tmp_path / "raw"
        _write(raw, "stray-note.md", "body\n")
        found = find_unclaimed_raw_files(raw, tmp_path)
        assert [f.reason for f in found] == [REASON_UNRECOGNISED_SHAPE]
        assert found[0].group_key == "(raw root)"
