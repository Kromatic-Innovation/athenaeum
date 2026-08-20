# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum measure {shadow-linkage,backlog-price,ordinary-night}``
(issue athenaeum#713)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from athenaeum.cli import main as cli_main


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _run_capture_stderr(argv: list[str]) -> tuple[int, str, str]:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = cli_main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


def _write_page(wiki_root: Path, filename: str, body: str) -> None:
    wiki_root.mkdir(parents=True, exist_ok=True)
    (wiki_root / filename).write_text(
        f"---\nname: {filename[:-3]}\ntype: concept\n---\n{body}\n", encoding="utf-8"
    )


class TestMeasureDispatch:
    def test_bare_measure_prints_usage(self) -> None:
        rc, _out, err = _run_capture_stderr(["measure"])
        assert rc == 2
        assert "shadow-linkage" in err


class TestShadowLinkageCli:
    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        _write_page(knowledge_root / "wiki", "a.md", "hello world alpha")
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "shadow-linkage",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--dry-run",
            ]
        )
        assert rc == 0
        assert "dry run" in out
        assert not docs_path.exists()

    def test_writes_snapshot_and_json_output(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        _write_page(knowledge_root / "wiki", "a.md", "hello world alpha")
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "shadow-linkage",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["candidate_file_count"] == 1
        assert docs_path.is_file()

    def test_empty_wiki_refuses_write_and_exits_nonzero(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "wiki").mkdir(parents=True)
        docs_path = tmp_path / "measurements.md"
        rc, _out, err = _run_capture_stderr(
            [
                "measure",
                "shadow-linkage",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
            ]
        )
        assert rc == 1
        assert "error" in err
        assert not docs_path.exists()


class TestBacklogPriceCli:
    def test_dry_run_and_json(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        raw_dir = knowledge_root / "raw" / "s"
        raw_dir.mkdir(parents=True)
        (raw_dir / "20260801T000000Z-aaaaaaaa.md").write_text("x")
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "backlog-price",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--dry-run",
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["backlog_count"] == 1
        assert not docs_path.exists()

    def test_empty_backlog_refuses_write(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw").mkdir(parents=True)
        docs_path = tmp_path / "measurements.md"
        rc, _out, err = _run_capture_stderr(
            [
                "measure",
                "backlog-price",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--cache-dir",
                str(tmp_path / "cache"),
            ]
        )
        assert rc == 1
        assert "error" in err


class TestOrdinaryNightCli:
    def test_writes_snapshot_even_when_indeterminate(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw").mkdir(parents=True)
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "ordinary-night",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["verdict"] == "indeterminate"
        assert docs_path.is_file()

    def test_comparator_pair_count_amortization_flows_through(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "raw").mkdir(parents=True)
        docs_path = tmp_path / "measurements.md"
        rc, out = _run(
            [
                "measure",
                "ordinary-night",
                "--path",
                str(knowledge_root),
                "--docs-path",
                str(docs_path),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--comparator-pair-count",
                "70",
                "--comparator-amortization-nights",
                "7",
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["amortized"]["comparator_pairs_per_night"] == 10.0
