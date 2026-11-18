# SPDX-License-Identifier: Apache-2.0
"""Decision-queue digest (athenaeum#2529, design athenaeum#2489 section 3)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from athenaeum import decision_digest
from athenaeum.cli import main as cli_main
from athenaeum.decision_budget import record_decision_answered
from athenaeum.decision_digest import queue_digest

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
SHAPE_KEYS = {
    "contract_version",
    "digest_id",
    "generated_at",
    "pending_total",
    "held_total",
    "unanswerable_total",
    "by_type",
    "by_routing",
    "backlog",
    "budget",
    "actionable_now",
    "oldest_age_days",
    "headline",
}


def _item(dtype: str, n: int, *, created: str = "2026-11-10", **extra: Any) -> dict:
    return {
        "type": dtype,
        "id": f"{dtype}-{n}",
        "created_at": created,
        "summary": f"{dtype} number {n}?",
        "confidence": None,
        "payload": {"n": n},
        **extra,
    }


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    w = tmp_path / "wiki"
    w.mkdir()
    return w


def _patch_queue(monkeypatch: pytest.MonkeyPatch, items: list[dict]) -> list[dict]:
    monkeypatch.setattr(decision_digest, "collect_unframed_decisions", lambda *a, **k: items)
    return items


def test_shape_core(wiki: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_queue(monkeypatch, [_item("merge", 1), _item("audit", 1)])
    d = queue_digest(wiki, now=NOW)
    assert set(d) == SHAPE_KEYS
    assert d["contract_version"] == 1
    assert d["digest_id"].startswith("sha256:")
    assert d["generated_at"] == "2026-11-18T12:00:00Z"
    assert d["pending_total"] == 2 and d["held_total"] == 0
    assert d["by_type"] == {"audit": 1, "merge": 1}
    assert set(d["by_routing"]) == {"authority", "competence", "scheduled-review"}
    assert set(d["budget"]) == {"per_type_daily", "answered_today"}
    assert d["oldest_age_days"] == 8
    assert {"id", "type", "summary", "age_days", "reversibility", "priority"} <= set(
        d["actionable_now"][0]
    )
    assert d["headline"].startswith("2 decisions need you")


def test_digest_id_stable_when_set_unchanged_and_changes_when_it_changes(
    wiki: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = _patch_queue(monkeypatch, [_item("merge", 1), _item("merge", 2)])
    first = queue_digest(wiki, now=NOW)
    later = queue_digest(wiki, now=datetime(2026, 11, 18, 18, 30, tzinfo=timezone.utc))
    assert first["generated_at"] != later["generated_at"]
    assert first["digest_id"] == later["digest_id"]

    items.append(_item("merge", 3))
    assert queue_digest(wiki, now=NOW)["digest_id"] != first["digest_id"]
    items.pop()
    items[0]["summary"] = "a changed question?"
    assert queue_digest(wiki, now=NOW)["digest_id"] != first["digest_id"]


def test_actionable_is_pending_answerable_and_bounded(
    wiki: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_queue(
        monkeypatch,
        [
            *[_item("merge", n) for n in range(6)],
            _item("question", 1, state="held", hold_reason="waiting on a rename"),
            _item("retraction", 1),
            _item("quarantine", 1),
        ],
    )
    d = queue_digest(wiki, now=NOW)
    offered = {i["id"] for i in d["actionable_now"]}
    assert len(d["actionable_now"]) == 4  # default batch size
    assert all(i.startswith("merge-") for i in offered)
    assert d["held_total"] == 1
    assert "question-1" not in offered
    # unanswerable: counted everywhere, offered nowhere
    assert d["unanswerable_total"] == 2
    assert d["pending_total"] == 8
    assert d["by_type"]["retraction"] == 1 and d["by_type"]["quarantine"] == 1
    assert not offered & {"retraction-1", "quarantine-1"}
    # the held item is not counted as pending
    assert "question" not in d["by_type"]


def test_batch_size_is_configurable(wiki: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_queue(monkeypatch, [_item("merge", n) for n in range(6)])
    cfg = {"decisions": {"digest_batch_size": 2}}
    assert len(queue_digest(wiki, config=cfg, now=NOW)["actionable_now"]) == 2


def test_surface_cap_limits_offering_default_audit_three(
    wiki: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_queue(
        monkeypatch,
        [*[_item("audit", n) for n in range(6)], _item("merge", 1)],
    )
    cfg = {"decisions": {"digest_batch_size": 10}}
    d = queue_digest(wiki, config=cfg, now=NOW)
    types = [i["type"] for i in d["actionable_now"]]
    assert types.count("audit") == 3 and types.count("merge") == 1
    assert d["budget"]["per_type_daily"]["audit"] == {
        "cap": 3,
        "surfaced_today": 0,
        "remaining": 3,
    }
    # the cap never changes what exists or what may be answered
    assert d["pending_total"] == 7 and d["by_type"]["audit"] == 6


def test_surface_cap_configurable_and_spent_by_todays_answers(
    wiki: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_queue(monkeypatch, [_item("audit", n) for n in range(5)])
    record_decision_answered(
        wiki, decision_id="audit-x", decision_type="audit", raised_at="2026-11-01",
        answered_at="2026-11-18T08:00:00Z",
    )
    record_decision_answered(  # yesterday: does not count
        wiki, decision_id="audit-y", decision_type="audit", raised_at="2026-11-01",
        answered_at="2026-11-17T23:00:00Z",
    )
    cfg = {"decisions": {"surface_caps": {"audit": 2}, "digest_batch_size": 10}}
    d = queue_digest(wiki, config=cfg, now=NOW)
    assert d["budget"]["answered_today"] == 1
    assert d["budget"]["per_type_daily"]["audit"] == {
        "cap": 2,
        "surfaced_today": 1,
        "remaining": 1,
    }
    assert len(d["actionable_now"]) == 1


def test_cap_zero_offers_none_but_still_counts(
    wiki: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_queue(monkeypatch, [_item("merge", 1)])
    cfg = {"decisions": {"surface_caps": {"merge": 0}}}
    d = queue_digest(wiki, config=cfg, now=NOW)
    assert d["actionable_now"] == [] and d["pending_total"] == 1
    assert d["headline"] == "No decisions need you"


def test_no_llm_or_framing_call(wiki: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import athenaeum.decision_framing as framing
    import athenaeum.decisions as decisions

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("digest must not frame or call a model")

    monkeypatch.setattr(framing, "frame_decision", boom)
    monkeypatch.setattr(decisions, "frame_decision", boom)
    monkeypatch.setattr(framing, "template_presentation", boom)
    monkeypatch.setattr(decisions, "list_pending_decisions", boom)
    (wiki / "_pending_questions.md").write_text(
        "# Pending Questions\n\n"
        '## [2026-07-01] Entity: "Acme Corp" (from sessions/x.md)\n'
        "- [ ] Is Acme still Series A?\n"
        "**Conflict type**: principled\n"
        "**Description**: two conflicting statements\n",
        encoding="utf-8",
    )
    d = queue_digest(wiki, now=NOW)  # real queue, real collector
    assert d["pending_total"] == 1 and len(d["actionable_now"]) == 1


def test_framed_flag_reads_the_stored_presentation(
    wiki: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum.decision_framing import state_digest_for
    from athenaeum.decision_presentations import save_presentations

    items = _patch_queue(monkeypatch, [_item("merge", 1), _item("merge", 2)])
    save_presentations(
        wiki,
        {},
        {"merge-1": {"state_digest": state_digest_for(items[0]), "presentation": {"job": "j"}}},
        prune=False,
        live_ids=set(),
    )
    framed = {i["id"]: i["framed"] for i in queue_digest(wiki, now=NOW)["actionable_now"]}
    assert framed == {"merge-1": True, "merge-2": False}


def test_page_recompile_in_by_type_and_backlog_slot(
    wiki: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_queue(monkeypatch, [_item("page-recompile", 1)])
    big = "x" * 400
    (wiki / "aa11bb22-big.md").write_text(
        f"---\nname: Big\nuid: aa11bb22\nneeds_split: true\n---\n{big}\n", encoding="utf-8"
    )
    d = queue_digest(wiki, now=NOW)
    assert d["by_type"] == {"page-recompile": 1}
    assert d["backlog"] == {"page_recompile": 1}
    # a restricted audience gets no page counts
    assert queue_digest(wiki, {"team"}, now=NOW)["backlog"] == {}


def test_headline_names_types(wiki: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_queue(monkeypatch, [_item("audit", 1), _item("audit", 2), _item("merge", 1)])
    assert queue_digest(wiki, now=NOW)["headline"] == "3 decisions need you — 2 audits, 1 merge"


def _knowledge(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "_pending_questions.md").write_text(
        "# Pending Questions\n\n"
        '## [2026-07-01] Entity: "Acme Corp" (from sessions/x.md)\n'
        "- [ ] Is Acme still Series A?\n"
        "**Conflict type**: principled\n"
        "**Description**: two conflicting statements\n",
        encoding="utf-8",
    )
    return tmp_path


def test_cli_digest_json_and_text(tmp_path: Path) -> None:
    root = _knowledge(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["decisions", "digest", "--path", str(root), "--json"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert set(payload) == SHAPE_KEYS
    assert payload["pending_total"] == 1
    assert payload["actionable_now"][0]["type"] in {"question", "confirmation"}

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["decisions", "digest", "--path", str(root)])
    assert rc == 0
    assert "1 decision needs you" in buf.getvalue()


def test_mcp_tool_queue_digest(tmp_path: Path) -> None:
    pytest.importorskip("fastmcp")
    import asyncio

    from athenaeum.mcp_server import create_server

    root = _knowledge(tmp_path)
    raw = root / "raw"
    raw.mkdir()
    server = create_server(raw_root=raw, wiki_root=root / "wiki")

    async def _run() -> dict:
        tool = await server.get_tool("queue_digest")
        return tool.fn()

    payload = asyncio.run(_run())
    assert set(payload) == SHAPE_KEYS
    assert payload["pending_total"] == 1 and len(payload["actionable_now"]) == 1
