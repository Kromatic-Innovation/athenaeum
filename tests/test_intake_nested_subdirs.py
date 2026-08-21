# SPDX-License-Identifier: Apache-2.0
"""Tests for nested-subdirectory discovery (issue athenaeum#974 AC2).

``discover_raw_files`` previously only globbed *directly* inside
``raw/<source>/`` — a record living one level below (``raw/<source>/<subdir>/``)
was never enumerated, even though nothing about the intake format or the
shape-rule engine required that restriction. This is one of the two gaps
that made the intended ``log_group: hestia-lanes-*`` rule (issue athenaeum#940)
inexpressible: an expressible field predicate still would not see files a
source organises into subdirectories.

Covers:
- a record directly inside a nested source subdirectory is now discovered;
- discovery is bounded to exactly ONE level below the source directory (a
  file two levels down is still not discovered);
- a nested file's ``RawFile.source`` is still the top-level source
  directory name (not ``<source>/<subdir>``), so ``match.source`` and
  ``non_intake_sources`` keep meaning exactly what they meant before this
  issue;
- every existing top-level-only exclusion/skip rule (``.gitkeep``,
  ``non_intake_sources``, the hardcoded ``answers`` skip, a claimed
  correction batch) still applies identically at the nested level;
- backward compatibility: a source directory with NO subdirectories
  discovers exactly what it always did (no new files appear, no reordering
  of the existing top-level files for that source).
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.intake import discover_raw_files


def _write(path: Path, content: str = "---\nuid: x\n---\nbody\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestNestedSourceSubdirDiscovery:
    def test_file_in_nested_subdir_is_discovered(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        nested = _write(
            raw_root / "hestia" / "hestia-lanes-974" / "20260821T000000Z-aaaaaaaa.md"
        )
        files = discover_raw_files(raw_root)
        assert [f.path for f in files] == [nested]

    def test_nested_file_keeps_top_level_source_name(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        _write(raw_root / "hestia" / "lane-a" / "20260821T000000Z-aaaaaaaa.md")
        files = discover_raw_files(raw_root)
        assert len(files) == 1
        assert files[0].source == "hestia"
        assert files[0].ref == "hestia/20260821T000000Z-aaaaaaaa.md"

    def test_recursion_bounded_to_one_level(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        _write(
            raw_root
            / "hestia"
            / "lane-a"
            / "too-deep"
            / "20260821T000000Z-aaaaaaaa.md"
        )
        files = discover_raw_files(raw_root)
        assert files == []

    def test_top_level_and_nested_files_both_discovered(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        top = _write(raw_root / "hestia" / "20260821T000000Z-aaaaaaaa.md")
        nested = _write(
            raw_root / "hestia" / "lane-a" / "20260821T000100Z-bbbbbbbb.md"
        )
        files = discover_raw_files(raw_root)
        assert {f.path for f in files} == {top, nested}

    def test_existing_flat_source_unchanged(self, tmp_path: Path) -> None:
        """Backward compat: a source with no subdirectories at all discovers
        exactly the same set it always did."""
        raw_root = tmp_path / "raw"
        a = _write(raw_root / "delivery-monitor" / "20260821T000000Z-aaaaaaaa.md")
        b = _write(raw_root / "delivery-monitor" / "20260821T000100Z-bbbbbbbb.md")
        files = discover_raw_files(raw_root)
        assert {f.path for f in files} == {a, b}

    def test_non_intake_sources_still_excludes_nested_files(
        self, tmp_path: Path
    ) -> None:
        raw_root = tmp_path / "raw"
        _write(raw_root / "noisy-tool" / "sub" / "20260821T000000Z-aaaaaaaa.md")
        config = {"librarian": {"non_intake_sources": ["noisy-tool"]}}
        assert discover_raw_files(raw_root, config) == []

    def test_answers_source_still_excluded_when_nested(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        _write(raw_root / "answers" / "sub" / "20260821T000000Z-aaaaaaaa.md")
        assert discover_raw_files(raw_root) == []

    def test_gitkeep_still_skipped_in_nested_subdir(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        _write(raw_root / "hestia" / "lane-a" / ".gitkeep", content="")
        assert discover_raw_files(raw_root) == []

    def test_auto_memory_extra_intake_root_not_double_discovered(
        self, tmp_path: Path
    ) -> None:
        """The dedicated `raw/auto-memory/<scope>/` tree (discovered by
        ``discover_auto_memory_files``, a completely separate function with
        a different frontmatter schema) must NOT also surface through
        ``discover_raw_files``'s new one-level-below descent -- that would
        double-discover every auto-memory file as if it were an ordinary
        entity raw file."""
        raw_root = tmp_path / "raw"
        _write(
            raw_root / "auto-memory" / "alpha" / "project_x0.md",
            content="---\nname: project_x0\ntype: auto-memory\n---\nbody\n",
        )
        config = {"recall": {"extra_intake_roots": ["raw/auto-memory"]}}
        assert discover_raw_files(raw_root, config) == []

    def test_claimed_correction_batch_still_skipped_when_nested(
        self, tmp_path: Path
    ) -> None:
        import json

        raw_root = tmp_path / "raw"
        envelope = json.dumps(
            {
                "record": "batch",
                "schema_version": 1,
                "submitter": "script:test",
                "batch_id": "x",
                "created_at": "2026-08-21T00:00:00Z",
            }
        )
        _write(
            raw_root / "hestia" / "lane-a" / "20260821T000000Z-aaaaaaaa.jsonl",
            content=envelope + "\n",
        )
        assert discover_raw_files(raw_root) == []
