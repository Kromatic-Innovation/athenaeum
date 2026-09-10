# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum calibration {summary,review}` (issue athenaeum#438)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from athenaeum.calibration import sample_tier_decision
from athenaeum.cli import main as cli_main


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


@pytest.fixture
def _tiers_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the reasoning-tier surface (issue athenaeum#518 — default OFF)."""
    monkeypatch.setenv("ATHENAEUM_REASONING_TIER_AUDITING_ENABLED", "1")


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


def test_summary_gated_off_by_default(tmp_path: Path) -> None:
    """Issue athenaeum#518: with the tiers disabled (the default), the summary reports
    an explicit not-enabled state — not a 0/0/0 all-clear that lies."""
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert payload["enabled"] is False
    assert "not enabled" in payload["error"]


def test_summary_empty(tmp_path: Path, _tiers_enabled: None) -> None:
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    assert rc == 0
    # Issue athenaeum#1487: the summary now also carries the UNSAMPLED decision-log
    # activity per tier. `_tiers_enabled` arms T1 only (via
    # ATHENAEUM_REASONING_TIER_AUDITING_ENABLED) — T2's own flag is untouched
    # — so with zero decisions ever logged, T1 is the "armed but silent"
    # case (exactly the athenaeum#1487 state) and T2 is simply unarmed.
    empty_bucket_t1 = {
        "sampled": 0, "reviewed": 0, "overturned": 0,
        "applied": 0, "overturned_applied": 0,
        "decisions_logged": 0, "last_decision_at": None,
        "armed": True, "armed_but_silent": True,
    }
    empty_bucket_t2 = {
        "sampled": 0, "reviewed": 0, "overturned": 0,
        "applied": 0, "overturned_applied": 0,
        "decisions_logged": 0, "last_decision_at": None,
        "armed": False, "armed_but_silent": False,
    }
    assert json.loads(out) == {"T1": empty_bucket_t1, "T2": empty_bucket_t2}


def test_summary_after_sampling(tmp_path: Path, _tiers_enabled: None) -> None:
    _seed_audit(tmp_path, tier="T2", verdict="approve", pid="p1")
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(out)["T2"]["sampled"] == 1


def test_review_overturn_flow(tmp_path: Path, _tiers_enabled: None) -> None:
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
    t2 = json.loads(out)["T2"]
    sampled_keys = ("sampled", "reviewed", "overturned", "applied", "overturned_applied")
    assert {k: t2[k] for k in sampled_keys} == {
        "sampled": 1, "reviewed": 1, "overturned": 1,
        "applied": 0, "overturned_applied": 0,
    }
    # Issue athenaeum#1487: `_seed_audit` writes only to the SAMPLED calibration
    # ledger, never to the unsampled decision log — so the raw
    # `decisions_logged` count stays 0 here even though `sampled` is 1. This
    # is precisely the distinction the fix makes visible.
    assert t2["decisions_logged"] == 0


def test_review_unknown_id_exits_nonzero(tmp_path: Path, _tiers_enabled: None) -> None:
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


def test_summary_text_output(tmp_path: Path, _tiers_enabled: None) -> None:
    _seed_audit(tmp_path, tier="T1", verdict="reject", pid="p3")
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path)])
    assert rc == 0
    assert "T1: sampled 1" in out


# ---------------------------------------------------------------------------
# Issue athenaeum#1487: raw (unsampled) decision-log activity, layered onto the
# sampled calibration summary above. `_seed_audit` only ever writes to the
# SAMPLED ledger (`sample_tier_decision`) — these tests seed the separate,
# unconditional `_reasoning_tier_decisions.jsonl` log directly via
# `record_reasoning_tier_decision`, the same call every real T1/T2 decision
# goes through in `reasoning_tiers.run_reasoning_pipeline`.
# ---------------------------------------------------------------------------


def _seed_decision_log(
    knowledge_root: Path, *, tier: str, verdict: str, proposal_id: str
) -> None:
    from athenaeum.reasoning_tiers import ReasoningTierDecision, record_reasoning_tier_decision

    wiki = knowledge_root / "wiki"
    wiki.mkdir(exist_ok=True)
    decision = ReasoningTierDecision(
        tier=tier,
        verdict=verdict,
        reason="test reason",
        reason_code=None,
        model=None,
        proposal_id=proposal_id,
    )
    ok = record_reasoning_tier_decision(wiki, decision)
    assert ok


def test_summary_reports_armed_but_silent_when_no_decisions_logged(
    tmp_path: Path, _tiers_enabled: None
) -> None:
    """Issue athenaeum#1487 AC5 (regression gate): T1 armed, zero decisions ever
    logged -> the summary must say so explicitly, both in JSON
    (``armed_but_silent: true``) and in the text rendering (an "ARMED BUT
    SILENT" line) — not a bare, indistinguishable 0/0/0."""
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert payload["T1"]["armed"] is True
    assert payload["T1"]["decisions_logged"] == 0
    assert payload["T1"]["armed_but_silent"] is True

    rc, text_out = _run(["calibration", "summary", "--path", str(tmp_path)])
    assert rc == 0
    assert "T1" in text_out and "ARMED but has recorded ZERO decisions" in text_out


def test_summary_not_armed_but_silent_once_a_decision_is_logged(
    tmp_path: Path, _tiers_enabled: None
) -> None:
    """The flip side of the guard above: once even one decision has been
    logged (reject OR pass-up — the unsampled log records both), the tier is
    no longer reported as silent, and the count/timestamp are visible."""
    _seed_decision_log(tmp_path, tier="T1", verdict="pass_up", proposal_id="prop-1")

    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert payload["T1"]["decisions_logged"] == 1
    assert payload["T1"]["last_decision_at"] is not None
    assert payload["T1"]["armed_but_silent"] is False

    rc, text_out = _run(["calibration", "summary", "--path", str(tmp_path)])
    assert rc == 0
    assert "1 decision(s) logged" in text_out
    assert "ARMED but has recorded ZERO decisions" not in text_out
