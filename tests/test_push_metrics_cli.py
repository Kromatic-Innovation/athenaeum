# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum push-metrics {baseline,coverage-audit}` (issue #711)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from athenaeum import push_metrics
from athenaeum.cli import main as cli_main


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def test_baseline_empty_ledger_is_honest(tmp_path: Path) -> None:
    docs_path = tmp_path / "docs" / "memory-model-measurements.md"
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--docs-path",
            str(docs_path),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["sessions"] == 0
    assert payload["precision"] is None
    assert docs_path.is_file()
    content = docs_path.read_text()
    assert "n/a — accrues as sessions run" in content


def test_baseline_rerun_is_idempotent(tmp_path: Path) -> None:
    docs_path = tmp_path / "docs.md"
    cache_dir = tmp_path / "cache"
    for _ in range(3):
        rc, _ = _run(
            [
                "push-metrics",
                "baseline",
                "--cache-dir",
                str(cache_dir),
                "--docs-path",
                str(docs_path),
            ]
        )
        assert rc == 0
    content = docs_path.read_text()
    assert content.count("## Push-precision and coverage baseline") == 1
    assert content.count("### Snapshot") == 3


def test_baseline_text_output(tmp_path: Path) -> None:
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--docs-path",
            str(tmp_path / "docs.md"),
        ]
    )
    assert rc == 0
    assert "sessions: 0" in out
    assert "athenaeum_version:" in out


def test_coverage_audit_writes_worksheet_file(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    rec = push_metrics.build_push_record(
        session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "body")]
    )
    push_metrics.record_push(rec, cache_dir=cache_dir)

    output = tmp_path / "worksheet.json"
    rc, out = _run(
        [
            "push-metrics",
            "coverage-audit",
            "--cache-dir",
            str(cache_dir),
            "--n",
            "1",
            "--seed",
            "1",
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    assert output.is_file()
    payload = json.loads(output.read_text())
    assert payload["sampled_session_count"] == 1
    assert str(output) in out


def test_coverage_audit_json_stdout(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    rec = push_metrics.build_push_record(
        session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "body")]
    )
    push_metrics.record_push(rec, cache_dir=cache_dir)
    rc, out = _run(
        [
            "push-metrics",
            "coverage-audit",
            "--cache-dir",
            str(cache_dir),
            "--n",
            "1",
            "--seed",
            "1",
            "--output",
            str(tmp_path / "ws.json"),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["sampled_session_count"] == 1


def test_no_subcommand_prints_usage(tmp_path: Path) -> None:
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = cli_main(["push-metrics"])
    assert rc == 2
    assert "usage" in buf.getvalue().lower()


def test_parser_tree_binds_func_for_push_metrics_subcommands() -> None:
    """Guards the #553 dispatch invariant for the new subcommand tree.

    The generic parser-tree walk in ``test_cli.py`` already asserts this for
    every registered subcommand; this test additionally pins the two leaves
    ``push-metrics`` actually adds so a future refactor of this module alone
    fails fast and locally.
    """
    import argparse

    from athenaeum.cli import build_parser

    parser = build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    push_metrics_parser = subparsers_action.choices["push-metrics"]
    assert push_metrics_parser.get_default("func") is not None
    inner = next(
        a
        for a in push_metrics_parser._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    assert set(inner.choices) == {"baseline", "coverage-audit"}
    for name, sub in inner.choices.items():
        assert (
            sub.get_default("func") is not None
            or push_metrics_parser.get_default("func") is not None
        ), f"push-metrics {name} has no resolvable func"
