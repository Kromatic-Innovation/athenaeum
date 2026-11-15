"""Queue items name the pages they concern by title plus path (athenaeum#2520)."""

from __future__ import annotations

from pathlib import Path

from athenaeum.answers import PendingQuestion
from athenaeum.calibration import ATTRIBUTION_TIER_NAME, sample_tier_decision
from athenaeum.decision_framing import frame_decision, subjects_for
from athenaeum.decisions import (
    SubjectResolver,
    audit_to_decision,
    list_pending_decisions,
    page_recompile_plan_to_decision,
    question_to_decision,
    retraction_to_decision,
)
from athenaeum.pending_merges import parse_pending_merges, write_pending_merge

CFG = {
    "librarian": {
        "audit_sample_rate_t1_rejects": 1.0,
        "audit_sample_rate_t2_approvals": 1.0,
        "audit_sample_rate_attribution": 1.0,
    }
}


def _page(wiki: Path, filename: str, name: str) -> Path:
    path = wiki / filename
    path.write_text(f"---\nname: {name}\nuid: {filename.split('-')[0]}\n---\nBody.\n")
    return path


def _wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir(exist_ok=True)
    return wiki


def test_audit_record_carries_sources_and_item_names_them(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    a = _page(wiki, "aaaa1111-roadmap.md", "Product Roadmap")
    b = _page(wiki, "bbbb2222-plan.md", "Roadmap Plan")
    rec = sample_tier_decision(
        wiki,
        tier="T1",
        verdict="reject",
        proposal_id="deadbeef0001",
        reason="different scope",
        config=CFG,
        sources=[str(a), str(b)],
    )
    assert rec is not None and rec["sources"] == [str(a), str(b)]
    items = list_pending_decisions(wiki)
    (audit,) = [i for i in items if i["type"] == "audit"]
    titles = [s["title"] for s in audit["subjects"]]
    assert titles == ["Product Roadmap", "Roadmap Plan"]
    assert [s["path"] for s in audit["subjects"]] == [str(a), str(b)]
    # Page pair and the tier's reason are both in the framing text.
    assert '"Product Roadmap"' in audit["summary"]
    assert str(b) in audit["summary"]
    assert "tier reason: different scope" in audit["summary"]
    # payload semantics unchanged
    assert audit["payload"]["proposal_id"] == "deadbeef0001"


def test_legacy_audit_record_recovers_pages_from_the_pending_merge(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    a = _page(wiki, "aaaa1111-roadmap.md", "Product Roadmap")
    b = _page(wiki, "bbbb2222-plan.md", "Roadmap Plan")
    write_pending_merge(
        wiki / "_pending_merges.md",
        merge_target_name="roadmap",
        sources=[str(a), str(b)],
        rationale="r",
        draft_merged_body="body",
        confidence=0.9,
    )
    merge_id = parse_pending_merges(wiki / "_pending_merges.md")[0].id
    # Pre-2520 record: proposal_id only.
    rec = {
        "id": "x",
        "tier": "T2",
        "verdict": "approve",
        "proposal_id": merge_id,
        "reason": "same thing",
    }
    item = audit_to_decision(rec, resolver=SubjectResolver(wiki))
    assert {s["title"] for s in item["payload"]["subjects"]} == {
        "Product Roadmap",
        "Roadmap Plan",
    }


def test_audit_unresolvable_falls_back_to_raw_id(tmp_path: Path) -> None:
    rec = {"id": "x", "tier": "T1", "verdict": "reject", "proposal_id": "abc", "reason": ""}
    item = audit_to_decision(rec, resolver=SubjectResolver(_wiki(tmp_path)))
    assert "subjects" not in item["payload"]
    assert "proposal abc" in item["summary"]
    # Missing page file: keeps the raw path as a (filename-derived) title.
    rec["sources"] = [str(tmp_path / "gone-page.md")]
    item = audit_to_decision(rec, resolver=SubjectResolver(_wiki(tmp_path)))
    (subject,) = item["payload"]["subjects"]
    assert subject["resolved"] is False
    assert subject["title"] == "gone-page"


def test_attribution_audit_resolves_uids_to_page_title_and_path(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    alice = _page(wiki, "12345678-alice-example.md", "Alice Example")
    rec = sample_tier_decision(
        wiki,
        tier=ATTRIBUTION_TIER_NAME,
        verdict="own",
        proposal_id="suspect-1",
        reason="placement -> 'own' on 12345678",
        config=CFG,
        uids=["12345678", "ffffffff"],
    )
    assert rec is not None and rec["uids"] == ["12345678", "ffffffff"]
    item = audit_to_decision(rec, resolver=SubjectResolver(wiki))
    first, second = item["payload"]["subjects"]
    assert (first["title"], first["path"]) == ("Alice Example", str(alice))
    # unknown uid keeps the raw id
    assert (second["title"], second["resolved"]) == ("ffffffff", False)
    assert '"Alice Example"' in item["summary"]


def test_fold_widen_question_names_canonical_and_folded_pages(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    canon = _page(wiki, "cccc3333-roadmap.md", "Product Roadmap")
    src = _page(wiki, "dddd4444-roadmap-old.md", "Old Roadmap Notes")
    write_pending_merge(
        wiki / "_pending_merges.md",
        merge_target_name="roadmap",
        sources=[str(canon), str(src)],
        rationale="r",
        draft_merged_body="body",
        confidence=0.9,
    )
    merge_id = parse_pending_merges(wiki / "_pending_merges.md")[0].id
    pq = _pq(
        entity="roadmap",
        source=str(canon),
        question="Fold of 'roadmap' cannot auto-widen its 'x' coordinate — value?",
        description=f"Human-approved fold (proposal {merge_id}) into 'roadmap'.",
    )
    item = question_to_decision(pq, resolver=SubjectResolver(wiki))
    assert [s["title"] for s in item["payload"]["subjects"]] == [
        "Product Roadmap",
        "Old Roadmap Notes",
    ]
    assert "Old Roadmap Notes" in item["summary"]
    assert item["payload"]["question"] == pq.question  # field semantics unchanged


def _pq(**kw: object) -> PendingQuestion:
    base: dict[str, object] = {
        "id": "q1",
        "created_at": "2026-01-01",
        "entity": "",
        "source": "",
        "question": "Q?",
        "conflict_type": "ambiguous",
        "description": "",
        "answered": False,
        "answer_lines": [],
        "raw_block": "",
    }
    base.update(kw)
    return PendingQuestion(**base)  # type: ignore[arg-type]


def test_plain_question_without_a_page_is_unchanged() -> None:
    pq = _pq(question="Plain?", source="(from nowhere)")
    item = question_to_decision(pq, resolver=SubjectResolver(None))
    assert item["summary"] == "Plain?"
    assert "subjects" not in item["payload"]


def test_retraction_names_the_canonical_page(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    page = _page(wiki, "eeee5555-auth.md", "Auth Design")
    item = retraction_to_decision(
        {"id": "r", "canonical_slug": "auth", "retracted_ref": "src", "reason": ""},
        resolver=SubjectResolver(wiki),
    )
    assert item["payload"]["subjects"][0]["path"] == str(page)
    assert '"Auth Design"' in item["summary"]


def test_page_recompile_resolves_uid_when_outline_has_no_name(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    page = _page(wiki, "ffff6666-big.md", "Big Page")
    item = page_recompile_plan_to_decision(
        {"id": "p", "uid": "ffff6666", "outline": {"group_count": 2, "page_chars": 9}},
        resolver=SubjectResolver(wiki),
    )
    assert "Big Page" in item["summary"] and str(page) in item["summary"]
    assert item["payload"]["subjects"][0]["title"] == "Big Page"


def test_frame_decision_surfaces_subjects_and_survives_the_context_cap() -> None:
    item = {
        "type": "audit",
        "id": "a",
        "created_at": "2026-01-01",
        "summary": "s",
        "confidence": None,
        "payload": {
            "subjects": [{"title": "T", "path": "/p.md", "ref": "/p.md", "resolved": True}],
            "blob": "x" * 20000,
        },
    }
    framed = frame_decision(item, max_context_tokens=50)
    assert framed["subjects"] == [
        {"title": "T", "path": "/p.md", "ref": "/p.md", "resolved": True}
    ]
    assert subjects_for({"payload": {}}) == []
    assert "subjects" not in frame_decision({"type": "audit", "payload": {}}, max_context_tokens=50)
