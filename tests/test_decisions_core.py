"""Unit tests for the `athenaeum.decisions` core helpers (issue athenaeum#401)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from athenaeum.answers import PendingQuestion
from athenaeum.decisions import (
    _fallback_title,
    _first_body_line,
    _one_line,
    age_days,
    confirmation_to_decision,
    list_pending_decisions,
    quarantine_to_decision,
    source_info,
)
from athenaeum.quarantine import quarantine_file


def test_age_days_basic() -> None:
    assert age_days("2026-06-20", today=date(2026, 7, 20)) == 30


def test_age_days_datetime_form() -> None:
    assert age_days("2026-07-01T09:15:00Z", today=date(2026, 7, 20)) == 19


def test_age_days_unparseable() -> None:
    assert age_days("not-a-date", today=date(2026, 7, 20)) is None
    assert age_days("", today=date(2026, 7, 20)) is None


def test_fallback_title_strips_uid_prefix() -> None:
    assert _fallback_title("/k/wiki/34f82884-auth-authentication.md") == "auth-authentication"


def test_fallback_title_strips_memory_prefix() -> None:
    assert _fallback_title("/k/user/user_alice_a.md") == "alice_a"


def test_fallback_title_plain() -> None:
    assert _fallback_title("/k/wiki/plainname.md") == "plainname"


def test_one_line_truncates() -> None:
    out = _one_line("a " * 200, limit=20)
    assert len(out) == 20
    assert out.endswith("…")


def test_first_body_line_skips_headings() -> None:
    assert _first_body_line("# Title\n\n## Sub\nReal content here.") == "Real content here."


def test_source_info_prefers_name_and_description(tmp_path: Path) -> None:
    page = tmp_path / "abc12345-jane.md"
    page.write_text(
        "---\nname: Jane Doe\ndescription: CEO of Acme.\n---\nbody line.\n",
        encoding="utf-8",
    )
    info = source_info(str(page))
    assert info == {"path": str(page), "title": "Jane Doe", "gist": "CEO of Acme."}


def test_source_info_gist_falls_back_to_body(tmp_path: Path) -> None:
    page = tmp_path / "abc12345-jane.md"
    page.write_text("---\nname: Jane Doe\n---\nFounder and CEO.\n", encoding="utf-8")
    info = source_info(str(page))
    assert info["title"] == "Jane Doe"
    assert info["gist"] == "Founder and CEO."


def test_source_info_missing_file(tmp_path: Path) -> None:
    info = source_info(str(tmp_path / "aa11bb22-lean-startup.md"))
    assert info["title"] == "lean-startup"
    assert info["gist"] == ""


def test_list_pending_decisions_empty(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    assert list_pending_decisions(tmp_path / "wiki") == []


# ---------------------------------------------------------------------------
# confirmation_to_decision (issue athenaeum#1290) — mirrors
# test_quarantine_to_decision_shape below.
# ---------------------------------------------------------------------------


def test_confirmation_to_decision_shape() -> None:
    pq = PendingQuestion(
        id="abc123",
        entity="(confirmation: o/r#42)",
        source="agent-raised",
        question="Confirm: implemented X instead of Y on o/r#42?",
        conflict_type="",
        description="Raised by lane-42 on o/r#42. ...",
        created_at="2026-09-02",
        answered=False,
        answer_lines=[],
        raw_block="",
        raised_by="agent",
        decision_kind="confirmation",
        raiser="lane-42",
        repo="o/r",
        issue_ref="42",
        narrowed_scope="scalar fields only",
        implemented_behavior="extended the existing tool",
        alternative="a parallel tool",
        raised_at="2026-09-02T09:50:00Z",
    )
    decision = confirmation_to_decision(pq)
    assert decision["type"] == "confirmation"
    assert decision["id"] == "abc123"
    assert decision["confidence"] is None
    assert decision["created_at"] == "2026-09-02"
    assert "lane-42" in decision["summary"]
    assert "o/r#42" in decision["summary"]
    assert "extended the existing tool" in decision["summary"]
    assert "a parallel tool" in decision["summary"]
    payload = decision["payload"]
    assert payload == {
        "raiser": "lane-42",
        "repo": "o/r",
        "issue_ref": "42",
        "narrowed_scope": "scalar fields only",
        "implemented_behavior": "extended the existing tool",
        "alternative": "a parallel tool",
        "raised_at": "2026-09-02T09:50:00Z",
        "question": pq.question,
        "context": pq.description,
        "raised_by": "agent",
    }


def test_plain_question_payload_shape_unchanged_by_confirmation_field(
    tmp_path: Path,
) -> None:
    """An ordinary (decision_kind="question") item's dict is UNAFFECTED by
    the new PendingQuestion fields athenaeum#1290 added — same keys as
    before, no confirmation fields leaking in."""
    from athenaeum.answers import raise_pending_question

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    pending = wiki / "_pending_questions.md"
    raise_pending_question(pending, "Plain Q?", "plain context")

    decisions = list_pending_decisions(wiki)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["type"] == "question"
    assert set(decision["payload"]) == {
        "entity",
        "source",
        "question",
        "conflict_type",
        "description",
        "raised_by",
    }


# ---------------------------------------------------------------------------
# quarantine_to_decision / list_pending_decisions surfacing (issue athenaeum#898,
# AC 4/5) — mirrors the audit/retraction coverage above for the sibling
# owner-only review-item types.
# ---------------------------------------------------------------------------


class _FakeRaw:
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source

    @property
    def ref(self) -> str:
        return f"{self.source}/{self.path.name}"


def test_quarantine_to_decision_shape() -> None:
    rec = {
        "id": "abc123",
        "created_at": "2026-08-14T00:00:00Z",
        "ref": "sessions/20260101T000000Z-aabbccdd.md",
        "source": "sessions",
        "bound": "bytes",
        "detail": "9,700,000 bytes exceeds the 5,242,880-byte limit",
        "violations": 2,
        "quarantine_path": "_quarantine/sessions/20260101T000000Z-aabbccdd.md",
        "original_path": "sessions/20260101T000000Z-aabbccdd.md",
    }
    decision = quarantine_to_decision(rec)
    assert decision["type"] == "quarantine"
    assert decision["id"] == "abc123"
    assert decision["confidence"] is None
    assert "sessions/20260101T000000Z-aabbccdd.md" in decision["summary"]
    assert "bytes" in decision["summary"]
    assert "2 consecutive run(s)" in decision["summary"]
    assert decision["payload"]["ref"] == rec["ref"]
    assert decision["payload"]["bound"] == "bytes"
    assert decision["payload"]["violations"] == 2


def test_quarantine_item_surfaces_in_decisions_distinct_from_escalations(
    tmp_path: Path,
) -> None:
    """A quarantined file appears in list_pending_decisions alongside an
    ordinary escalation, distinguishable by type — same shape as the
    audit/retraction coverage in test_calibration.py /
    test_retraction_cascade.py."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "_pending_questions.md").write_text(
        "# Pending Questions\n\n"
        '## [2026-07-01] Entity: "Acme" (from s/x.md)\n'
        "- [ ] Is Acme Series A?\n"
        "**Conflict type**: principled\n"
        "**Description**: two conflicting statements\n",
        encoding="utf-8",
    )

    raw_root = tmp_path / "raw"
    (raw_root / "sessions").mkdir(parents=True)
    fpath = raw_root / "sessions" / "20260101T000000Z-aabbccdd.md"
    fpath.write_text("poison content\n", encoding="utf-8")
    quarantine_file(
        _FakeRaw(fpath, "sessions"),
        wiki_root=wiki,
        raw_root=raw_root,
        bound="bytes",
        detail="9.7MB > 5MB limit",
        violations=2,
    )

    decisions = list_pending_decisions(wiki)
    types = {d["type"] for d in decisions}
    assert "question" in types and "quarantine" in types

    quarantine_item = next(d for d in decisions if d["type"] == "quarantine")
    assert quarantine_item["payload"]["ref"] == "sessions/20260101T000000Z-aabbccdd.md"
    assert quarantine_item["payload"]["bound"] == "bytes"
    assert quarantine_item["confidence"] is None


def test_quarantine_item_withheld_from_restricted_caller(tmp_path: Path) -> None:
    """Issue athenaeum#538 parity: quarantine items reference raw-intake files by
    ref, not a readable compiled-wiki source path, so they are owner-only —
    mirroring retraction/audit."""
    wiki = tmp_path / "wiki"
    raw_root = tmp_path / "raw"
    (raw_root / "sessions").mkdir(parents=True)
    fpath = raw_root / "sessions" / "20260101T000000Z-aabbccdd.md"
    wiki.mkdir()
    fpath.write_text("poison content\n", encoding="utf-8")
    quarantine_file(
        _FakeRaw(fpath, "sessions"),
        wiki_root=wiki,
        raw_root=raw_root,
        bound="bytes",
        detail="d",
        violations=2,
    )

    decisions = list_pending_decisions(wiki, caller_audience={"Operations"})
    assert not any(d["type"] == "quarantine" for d in decisions)
