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
    from athenaeum._cmd_calibration import T1_UNWIRED_NOTE

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
        "armed_but_silent_reason": T1_UNWIRED_NOTE,
    }
    empty_bucket_t2 = {
        "sampled": 0, "reviewed": 0, "overturned": 0,
        "applied": 0, "overturned_applied": 0,
        "decisions_logged": 0, "last_decision_at": None,
        "armed": False, "armed_but_silent": False,
    }
    payload = json.loads(out)
    # athenaeum#2079 AC2: the reach check is environment-dependent (API key);
    # its content is pinned in the dedicated tests below.
    assert payload["T1"].pop("armed_but_silent_check")["state"] in {
        "tier_not_callable",
        "no_decisions_reached_tier",
    }
    assert payload == {"T1": empty_bucket_t1, "T2": empty_bucket_t2}


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


def test_armed_but_silent_t1_names_the_wiring_defect(
    tmp_path: Path, _tiers_enabled: None
) -> None:
    """athenaeum#2080 AC4: an armed T1 with zero decisions names the structural
    wiring defect (T1's only call site is unwired) instead of only blaming the
    LLM client, in both JSON and text."""
    from athenaeum._cmd_calibration import T1_UNWIRED_NOTE

    (tmp_path / "wiki").mkdir()
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    assert rc == 0
    assert json.loads(out)["T1"]["armed_but_silent_reason"] == T1_UNWIRED_NOTE

    rc, text_out = _run(["calibration", "summary", "--path", str(tmp_path)])
    assert "known wiring defect" in text_out
    assert "run_cluster_comparator" in text_out


def test_t1_unwired_note_premise_still_holds() -> None:
    """The note is only true while no production code constructs the
    ``ClusterScreenContext`` that arms T1 in the cluster driver. When this
    fails, T1 has been wired in: retire ``T1_UNWIRED_NOTE`` (and this test)
    rather than weakening the assertion."""
    src = Path(__file__).resolve().parents[1] / "src" / "athenaeum"
    constructors = []
    for p in src.rglob("*.py"):
        text = p.read_text("utf-8")
        if "ClusterScreenContext(" in text and "class ClusterScreenContext" not in text:
            constructors.append(p.name)
    assert constructors == []


def test_armed_but_silent_not_callable_names_provider_and_model(
    tmp_path: Path, _tiers_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """athenaeum#2079 AC2: no API key => the tier cannot have been called; the
    checked condition names the provider and model it tried."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ATHENAEUM_REASONING_T1_LLM_PROVIDER", raising=False)
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    check = json.loads(out)["T1"]["armed_but_silent_check"]
    assert rc == 0
    assert check["state"] == "tier_not_callable"
    assert check["provider"] == "api"
    assert check["model"]

    rc, text_out = _run(["calibration", "summary", "--path", str(tmp_path)])
    assert "TIER NOT CALLABLE" in text_out
    assert "provider 'api'" in text_out


def test_armed_but_silent_callable_means_no_decisions_reached(
    tmp_path: Path, _tiers_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """athenaeum#2079 AC2: a buildable client => the tier is callable and the
    zero count means nothing reached it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ATHENAEUM_REASONING_T1_LLM_PROVIDER", raising=False)
    (tmp_path / "wiki").mkdir()
    rc, out = _run(["calibration", "summary", "--path", str(tmp_path), "--json"])
    check = json.loads(out)["T1"]["armed_but_silent_check"]
    assert rc == 0
    assert check["state"] == "no_decisions_reached_tier"
    assert check["provider"] == "api"

    rc, text_out = _run(["calibration", "summary", "--path", str(tmp_path)])
    assert "NO DECISIONS REACHED IT" in text_out


def _seed_default_acceptance_audit(wiki: Path, pid: str) -> str:
    from athenaeum.calibration import sample_default_acceptance

    wiki.mkdir(exist_ok=True)
    rec = sample_default_acceptance(
        wiki,
        decision_id=pid,
        answer_source="rule",
        config={"librarian": {"default_acceptance_audit_sample_rate": 1.0}},
    )
    assert rec is not None
    return rec["id"]


def test_close_not_applicable_dry_run_then_apply(tmp_path: Path) -> None:
    """athenaeum#2079 AC3: dry-run by default; --apply closes the items so they
    count toward no rate and leave the pending queue. Works with the tier gate
    OFF (ledger hygiene is not a tier surface)."""
    from athenaeum.calibration import (
        calibration_summary,
        list_pending_audit,
        read_calibration_ledger,
    )

    wiki = tmp_path / "wiki"
    ids = [_seed_default_acceptance_audit(wiki, f"merge-{i}") for i in range(3)]
    t1_id = _seed_audit(tmp_path, tier="T1", verdict="reject", pid="keep-me")

    rc, out = _run(["calibration", "close-not-applicable", "--path", str(tmp_path)])
    assert rc == 0
    assert out.count("would close") == 3
    assert len(list_pending_audit(wiki)) == 4  # nothing written

    rc, out = _run(
        ["calibration", "close-not-applicable", "--path", str(tmp_path), "--apply"]
    )
    assert rc == 0
    assert out.count("closed ") == 3
    pending = {r["id"] for r in list_pending_audit(wiki)}
    assert pending == {t1_id}  # T1 item untouched, circular ones resolved
    summary = calibration_summary(wiki)
    assert summary["default_acceptance"] == {
        "sampled": 0, "reviewed": 0, "overturned": 0,
        "applied": 0, "overturned_applied": 0,
    }
    assert summary["T1"]["sampled"] == 1
    ledger_notes = [
        r["note"]
        for r in read_calibration_ledger(wiki)
        if r.get("not_applicable")
    ]
    assert len(ledger_notes) == 3 and all("athenaeum#2079" in n for n in ledger_notes)
    assert {r["id"] for r in read_calibration_ledger(wiki) if r.get("not_applicable")} == set(ids)


def test_close_not_applicable_rejects_non_default_acceptance_id(tmp_path: Path) -> None:
    t1_id = _seed_audit(tmp_path, tier="T1", verdict="reject", pid="p")
    rc, _ = _run(
        ["calibration", "close-not-applicable", "--path", str(tmp_path),
         "--id", t1_id, "--apply"]
    )
    assert rc == 1
