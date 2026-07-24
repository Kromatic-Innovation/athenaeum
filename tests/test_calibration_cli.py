# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum calibration {summary,review}` (issue #438)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from athenaeum.calibration import sample_tier_decision
from athenaeum.cli import main as cli_main


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _seed_audit(knowledge_root: Path, *, tier: str, verdict: str, pid: str) -> str:
    wiki = knowledge_root / "wiki"
    wiki.mkdir(exist_ok=True)
    rec = sample_tier_decision(
        wiki,
        tier=tier,
        verdict=verdict,
        proposal_id=pid,
        reason="r",
        config={
            "librarian": {
                "audit_sample_rate_t1_rejects": 1.0,
                "audit_sample_rate_t2_approvals": 1.0,
            }
        },
    )
    assert rec is not None
    return rec["id"]


def test_summary_empty(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(out) == {
        "T1": {"sampled": 0, "reviewed": 0, "overturned": 0},
        "T2": {"sampled": 0, "reviewed": 0, "overturned": 0},
    }


def test_summary_after_sampling(tmp_path: Path) -> None:
    _seed_audit(tmp_path, tier="T2", verdict="approve", pid="p1")
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(out)["T2"]["sampled"] == 1


def test_review_overturn_flow(tmp_path: Path) -> None:
    audit_id = _seed_audit(tmp_path, tier="T2", verdict="approve", pid="p2")
    rc, out = _run(
        [
            "calibration",
            "review",
            "--path",
            str(tmp_path),
            "--id",
            audit_id,
            "--verdict",
            "reject",
            "--json",
        ]
    )
    assert rc == 0
    assert json.loads(out)["overturned"] is True

    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    assert json.loads(out)["T2"] == {"sampled": 1, "reviewed": 1, "overturned": 1}


def test_review_unknown_id_exits_nonzero(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    rc, _ = _run(
        [
            "calibration",
            "review",
            "--path",
            str(tmp_path),
            "--id",
            "nope",
            "--verdict",
            "reject",
        ]
    )
    assert rc == 1


def test_summary_text_output(tmp_path: Path) -> None:
    _seed_audit(tmp_path, tier="T1", verdict="reject", pid="p3")
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path)])
    assert rc == 0
    assert "T1: sampled 1" in out
