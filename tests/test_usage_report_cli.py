# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum usage-report` (issue athenaeum#968)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from athenaeum import push_metrics
from athenaeum.cli import main as cli_main


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _seed(cache_dir: Path) -> None:
    push = push_metrics.build_push_record(
        session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
    )
    push_metrics.record_push(push, cache_dir=cache_dir)
    ref = push_metrics.ReferenceResult(
        session_id="s1",
        ts="2026-01-01T00:00:00Z",
        pushed_ids=["u1"],
        referenced_ids=["u1"],
    )
    push_metrics.record_reference_result(ref, cache_dir=cache_dir)


def test_empty_ledger_json(tmp_path: Path) -> None:
    rc, out = _run(["usage-report", "--cache-dir", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(out) == []


def test_json_output_shape(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, out = _run(["usage-report", "--cache-dir", str(tmp_path), "--json"])
    assert rc == 0
    rows = json.loads(out)
    assert rows == [
        {
            "id": "u1",
            "pushed_count": 1,
            "referenced_count": 1,
            "last_pushed": rows[0]["last_pushed"],
            "last_referenced": "2026-01-01T00:00:00Z",
        }
    ]


def test_claim_id_filter(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, out = _run(
        ["usage-report", "--cache-dir", str(tmp_path), "--claim-id", "u1", "--json"]
    )
    assert rc == 0
    row = json.loads(out)
    assert row["id"] == "u1"


def test_claim_id_filter_unknown_id_returns_null(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, out = _run(
        ["usage-report", "--cache-dir", str(tmp_path), "--claim-id", "nope", "--json"]
    )
    assert rc == 0
    assert json.loads(out) is None


def test_text_output_is_ids_only(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, out = _run(["usage-report", "--cache-dir", str(tmp_path)])
    assert rc == 0
    assert "u1" in out
    assert "pushed=1" in out
    assert "referenced=1" in out
