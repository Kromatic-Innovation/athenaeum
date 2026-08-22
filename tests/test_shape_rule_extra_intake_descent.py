# SPDX-License-Identifier: Apache-2.0
"""Tests for shape-rule evaluation reaching one level below a configured
`recall.extra_intake_roots` entry (issue athenaeum#1096).

`discover_raw_files` deliberately never descends into a source directory
that is itself an extra-intake root (default `raw/auto-memory`) — correct
for INTAKE (see that function's docstring), but it also meant the
shape-rule phase could never see a tree like
`raw/auto-memory/hestia-lanes/`, so a `preserve` rule targeting hestia's
lane logs could load cleanly and still match zero candidates forever.

Design-note option 1 (the issue's stated preference): let shape-rule
evaluation reach one level below an `extra_intake_roots` entry, while
leaving *intake* discovery exactly as #974 left it. This file proves each
acceptance criterion:

- AC1: a file at `raw/auto-memory/<scope>/<name>.md` is evaluated by the
  shape-rule phase and a matching rule records a real disposition.
- AC2: intake discovery (`discover_raw_files`) is unchanged over the same
  tree — this repo's pre-existing `test_intake_nested_subdirs.py::
  test_auto_memory_extra_intake_root_not_double_discovered` already pins
  this, and is unmodified by athenaeum#1096; this file adds one more
  direct assertion for locality.
- AC3: `RawFile.source` for such a file stays the top-level source
  directory name (`auto-memory`), not `auto-memory/<scope>`.
- AC4: a `preserve` rule in `mode: observe` targeting the lane-log tree
  matches against fixtures, without moving anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from athenaeum.intake import discover_raw_files, discover_shape_rule_extra_intake_files
from athenaeum.rules import run_shape_rule_phase


def _write_rule(rules_dir: Path, filename: str, rule: dict) -> Path:
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / filename
    path.write_text(yaml.safe_dump(rule), encoding="utf-8")
    return path


def _write_raw_jsonl(raw_root: Path, rel_dir: str, name: str, record: dict) -> Path:
    d = raw_root / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _observe_preserve_rule(**overrides) -> dict:
    d = {
        "version": 1,
        "name": "hestia-lane-log-observe",
        "mode": "observe",
        "match": {"source": "auto-memory", "format": "jsonl"},
        "disposition": "preserve",
    }
    d.update(overrides)
    return d


def _run(tmp_path: Path, *, config: dict | None = None):
    if config is None:
        config = {}
    return run_shape_rule_phase(
        raw_root=tmp_path / "raw",
        wiki_root=tmp_path / "wiki",
        knowledge_root=tmp_path,
        config=config,
    )


class TestExtraIntakeCandidateDiscovery:
    """AC1 + AC3: `discover_shape_rule_extra_intake_files` finds the nested
    file and tags it with the top-level source directory name."""

    def test_finds_file_one_level_below_extra_intake_root(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        nested = raw_root / "auto-memory" / "hestia-lanes" / "20260821T000000Z-aaaaaaaa.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("---\nname: x\ntype: auto-memory\n---\nbody\n", encoding="utf-8")

        found = discover_shape_rule_extra_intake_files(raw_root)
        assert [f.path for f in found] == [nested]

    def test_source_is_the_top_level_extra_intake_root_name(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        nested = raw_root / "auto-memory" / "hestia-lanes" / "20260821T000000Z-aaaaaaaa.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("body\n", encoding="utf-8")

        found = discover_shape_rule_extra_intake_files(raw_root)
        assert len(found) == 1
        assert found[0].source == "auto-memory"
        assert found[0].ref == "auto-memory/20260821T000000Z-aaaaaaaa.md"

    def test_top_level_of_extra_intake_root_itself_is_not_rescanned(
        self, tmp_path: Path
    ) -> None:
        # A file directly at raw/auto-memory/ (no scope subdir) is not this
        # function's concern -- `discover_raw_files` already scans that
        # level (and finds nothing there today by construction).
        raw_root = tmp_path / "raw"
        top = raw_root / "auto-memory" / "20260821T000000Z-aaaaaaaa.md"
        top.parent.mkdir(parents=True)
        top.write_text("body\n", encoding="utf-8")

        assert discover_shape_rule_extra_intake_files(raw_root) == []

    def test_non_extra_intake_source_is_not_visited(self, tmp_path: Path) -> None:
        # An ordinary nested source (e.g. raw/hestia/lane-a/...) is already
        # reachable via `discover_raw_files` -- this function must not
        # duplicate it.
        raw_root = tmp_path / "raw"
        nested = raw_root / "hestia" / "lane-a" / "20260821T000000Z-aaaaaaaa.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("body\n", encoding="utf-8")

        assert discover_shape_rule_extra_intake_files(raw_root) == []

    def test_recursion_bounded_to_one_level(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        too_deep = (
            raw_root
            / "auto-memory"
            / "hestia-lanes"
            / "too-deep"
            / "20260821T000000Z-aaaaaaaa.md"
        )
        too_deep.parent.mkdir(parents=True)
        too_deep.write_text("body\n", encoding="utf-8")

        assert discover_shape_rule_extra_intake_files(raw_root) == []


class TestIntakeDiscoveryUnchanged:
    """AC2: `discover_raw_files` over the same extra-intake-root tree is
    byte-for-byte what #974 left it — this issue adds a SEPARATE candidate
    source for shape rules, it does not touch intake discovery itself."""

    def test_discover_raw_files_still_finds_nothing_under_extra_intake_root(
        self, tmp_path: Path
    ) -> None:
        raw_root = tmp_path / "raw"
        nested = raw_root / "auto-memory" / "hestia-lanes" / "20260821T000000Z-aaaaaaaa.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("---\nname: x\ntype: auto-memory\n---\nbody\n", encoding="utf-8")

        config = {"recall": {"extra_intake_roots": ["raw/auto-memory"]}}
        assert discover_raw_files(raw_root, config) == []


class TestShapeRulePhaseReachesAutoMemory:
    """AC4: a `preserve` rule in `mode: observe` targeting the lane-log
    tree matches against fixtures, without moving anything."""

    def test_observe_mode_preserve_rule_matches_instead_of_no_match(
        self, tmp_path: Path
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _observe_preserve_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw",
            "auto-memory/hestia-lanes",
            "20260821T000000Z-aaaaaaaa.jsonl",
            {"lane": "1096"},
        )

        summary = _run(tmp_path)

        assert summary["files_evaluated"] == 1
        assert summary["files_matched"] == 1
        assert summary["dispositions"] == {"observed-preserve": 1}
        # observe mode: nothing moved.
        assert raw_path.exists()

    def test_without_this_issues_fix_it_would_have_been_no_match(
        self, tmp_path: Path
    ) -> None:
        # Documents the bug this issue fixes: a rule targeting the
        # auto-memory tree, run against ONLY `discover_raw_files`'
        # candidates (i.e. what the phase saw before #1096), never sees the
        # file at all.
        raw_root = tmp_path / "raw"
        _write_raw_jsonl(
            raw_root,
            "auto-memory/hestia-lanes",
            "20260821T000000Z-aaaaaaaa.jsonl",
            {"lane": "1096"},
        )
        assert discover_raw_files(raw_root) == []
