# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum push-metrics {baseline,coverage-audit}` (issue athenaeum#711)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from athenaeum import push_metrics
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


def _seed_valid_ledger(cache_dir: Path, *, session_id: str = "s1") -> None:
    """Seed one push + one fully-referenced record so a baseline computed
    over *cache_dir* has ``reference_record_count > 0`` — the CLI refuses to
    write a snapshot otherwise (issue athenaeum#795)."""
    push = push_metrics.build_push_record(
        session_id=session_id, query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
    )
    push_metrics.record_push(push, cache_dir=cache_dir)
    ref = push_metrics.ReferenceResult(
        session_id=session_id,
        ts="2026-01-01T00:00:00Z",
        pushed_ids=["u1"],
        referenced_ids=["u1"],
    )
    push_metrics.record_reference_result(ref, cache_dir=cache_dir)


def test_baseline_empty_ledger_is_honest(tmp_path: Path) -> None:
    """athenaeum#795: an empty ledger (zero reference records, precision not
    computable) must be REFUSED — the athenaeum#711 incident this issue
    fixes was exactly this case silently writing a placeholder snapshot.
    """
    docs_path = tmp_path / "docs" / "memory-model-measurements.md"
    rc, out, err = _run_capture_stderr(
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
    assert rc == 1
    assert not docs_path.exists()
    assert "reference_records" in err
    assert out == ""


def test_baseline_rerun_is_idempotent(tmp_path: Path) -> None:
    docs_path = tmp_path / "docs.md"
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
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
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(tmp_path / "docs.md"),
        ]
    )
    assert rc == 0
    assert "sessions: 1" in out
    assert "athenaeum_version:" in out


def test_baseline_exclude_session_flag(tmp_path: Path) -> None:
    """athenaeum#791 AC3/AC4: ``--exclude-session`` drops a known-synthetic
    session from the counts/precision and reports it as a distinct field.
    """
    cache_dir = tmp_path / "cache"
    clean = push_metrics.build_push_record(
        session_id="clean", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
    )
    push_metrics.record_push(clean, cache_dir=cache_dir)
    push_metrics.record_reference_result(
        push_metrics.ReferenceResult(
            session_id="clean",
            ts="2026-01-01T00:00:00Z",
            pushed_ids=["u1"],
            referenced_ids=["u1"],
        ),
        cache_dir=cache_dir,
    )
    synth = push_metrics.build_push_record(
        session_id="synth", query="q", backend="fts5", hits=[("test-page.md", None, "b")]
    )
    push_metrics.record_push(synth, cache_dir=cache_dir)
    push_metrics.record_reference_result(
        push_metrics.ReferenceResult(
            session_id="synth",
            ts="2026-01-01T00:00:00Z",
            pushed_ids=["test-page.md"],
            referenced_ids=[],
        ),
        cache_dir=cache_dir,
    )

    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(tmp_path / "docs.md"),
            "--exclude-session",
            "synth",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["sessions"] == 1
    assert payload["push_records"] == 1
    assert payload["excluded_sessions"] == ["synth"]
    assert payload["excluded_push_records"] == 1


def test_baseline_without_exclude_session_reports_honest_zero(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(tmp_path / "docs.md"),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["excluded_sessions"] == []
    assert payload["excluded_push_records"] == 0
    assert payload["excluded_reference_records"] == 0


def test_baseline_dry_run_does_not_write(tmp_path: Path) -> None:
    """AC1: ``--dry-run`` computes/displays the baseline without touching
    ``--docs-path``. Uses a VALID (writable) baseline so this test isolates
    the dry-run behavior from the separate zero-reference-records refusal.
    """
    docs_path = tmp_path / "docs.md"
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(docs_path),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    assert not docs_path.exists()
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["sessions"] == 1


def test_baseline_dry_run_inspects_invalid_baseline_without_writing(tmp_path: Path) -> None:
    """This is the exact athenaeum#711 incident scenario: check whether a
    baseline is computable, over an empty/dead-instrument ledger, without
    mutating ``docs/memory-model-measurements.md``. ``--dry-run --json`` is
    the safe way to do that — no refusal, no write, exit 0.
    """
    docs_path = tmp_path / "docs.md"
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--docs-path",
            str(docs_path),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    assert not docs_path.exists()
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["sessions"] == 0
    assert payload["precision"] is None


def test_baseline_json_alone_still_writes(tmp_path: Path) -> None:
    """States the chosen dry-run semantics (issue athenaeum#795): ``--json``
    is a stdout-format concern only and does NOT by itself suppress the
    write — ``--dry-run`` is the (separate, explicit) no-write flag.
    """
    docs_path = tmp_path / "docs.md"
    cache_dir = tmp_path / "cache"
    _seed_valid_ledger(cache_dir)
    rc, out = _run(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(cache_dir),
            "--docs-path",
            str(docs_path),
            "--json",
        ]
    )
    assert rc == 0
    assert docs_path.is_file()
    payload = json.loads(out)
    assert payload["dry_run"] is False


def test_baseline_default_docs_path_not_written_for_invalid_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC ("cover the default docs_path"): every other CLI test above passes
    an explicit ``--docs-path`` under ``tmp_path``. The default relative
    ``docs_path=Path("docs/memory-model-measurements.md")`` — resolved
    against cwd, and the thing that actually wrote into the repo during the
    athenaeum#711 incident — is exercised here instead, by chdir-ing into a
    tmp directory first so a bug in this test can never write into the real
    repo docs. The ledger is empty (the incident's own scenario: a
    zero-reference-record baseline), so the refusal added by athenaeum#795
    is what actually protects the default path in practice.
    """
    monkeypatch.chdir(tmp_path)
    rc, out, err = _run_capture_stderr(
        [
            "push-metrics",
            "baseline",
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert rc == 1
    assert "reference_records" in err
    assert not (tmp_path / "docs" / "memory-model-measurements.md").exists()


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


def test_coverage_audit_exclude_session_flag(tmp_path: Path) -> None:
    """athenaeum#986 AC2: ``--exclude-session`` on coverage-audit at the CLI
    layer — same semantics as ``baseline --exclude-session``: the excluded
    session is dropped from the sample and reported, never silently ignored.
    """
    cache_dir = tmp_path / "cache"
    clean = push_metrics.build_push_record(
        session_id="clean", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
    )
    push_metrics.record_push(clean, cache_dir=cache_dir)
    synth = push_metrics.build_push_record(
        session_id="synth", query="q", backend="fts5", hits=[("test-page.md", None, "b")]
    )
    push_metrics.record_push(synth, cache_dir=cache_dir)

    rc, out = _run(
        [
            "push-metrics",
            "coverage-audit",
            "--cache-dir",
            str(cache_dir),
            "--n",
            "5",
            "--seed",
            "1",
            "--output",
            str(tmp_path / "ws.json"),
            "--exclude-session",
            "synth",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["sampled_session_count"] == 1
    assert payload["sessions"][0]["session_id"] == "clean"
    assert payload["excluded_sessions"] == ["synth"]
    assert payload["excluded_push_records"] == 1


def test_no_subcommand_prints_usage(tmp_path: Path) -> None:
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = cli_main(["push-metrics"])
    assert rc == 2
    assert "usage" in buf.getvalue().lower()


def test_parser_tree_binds_func_for_push_metrics_subcommands() -> None:
    """Guards the athenaeum#553 dispatch invariant for the new subcommand tree.

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
