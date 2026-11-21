# SPDX-License-Identifier: Apache-2.0
"""Offline pins for the native-baseline harness (issue athenaeum#3061)."""

from __future__ import annotations

from pathlib import Path

from tests.evals import native_baseline_cli as nb


def test_project_dir_encoding_replaces_every_non_alphanumeric(tmp_path: Path) -> None:
    cwd = tmp_path / "a.b_c" / "cwd"
    cwd.mkdir(parents=True)
    enc = nb.encode_project_dir(cwd)
    assert "/" not in enc and "." not in enc and "_" not in enc
    assert enc.endswith("a-b-c-cwd")
    assert (
        nb.native_memory_dir(tmp_path / "cfg", cwd)
        == tmp_path / "cfg" / "projects" / enc / "memory"
    )


def test_render_auto_memory_writes_index_line_and_topic_file(tmp_path: Path) -> None:
    notes = [
        nb.MemoryNote("a.md", "Alpha", "first line", "# Alpha\nfirst line\nmore"),
        nb.MemoryNote("b.md", "Beta", "other", "# Beta\nother"),
    ]
    nb.render_auto_memory(notes, tmp_path / "memory")
    index = (tmp_path / "memory" / "MEMORY.md").read_text().splitlines()
    assert index == ["- Alpha — first line", "- Beta — other"]
    assert (tmp_path / "memory" / "a.md").read_text().endswith("more")


def test_topic_reads_excludes_the_index_and_outside_files(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()

    def read(path: Path) -> dict:
        return {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(path)}}]
            },
        }

    events = [read(mem / "MEMORY.md"), read(mem / "a.md"), read(tmp_path / "other.md")]
    assert nb.topic_reads(events, mem) == [str((mem / "a.md").resolve())]


def test_strip_degradation_notice_keeps_the_breadcrumb() -> None:
    text = "[athenaeum] degraded\n[Knowledge context] pages:\n  - A"
    assert nb.strip_degradation_notice(text) == "[Knowledge context] pages:\n  - A"


def test_aggregate_splits_cost_per_correct_and_skips_failures() -> None:
    def row(correct: bool, reached: bool, cost: float, failure=None) -> nb.RunRow:
        return nb.RunRow(
            "N", "haiku", "p", "single_hop", correct, reached, cost, failure, None, None, correct
        )

    cell = nb.aggregate(
        "N", "haiku", [row(True, True, 1.0), row(False, False, 1.0), row(True, True, 9.0, "x")]
    )
    assert (cell.n, cell.correct_rate, cell.reach_rate, cell.cost_per_correct) == (2, 0.5, 0.5, 2.0)
