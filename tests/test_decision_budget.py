# SPDX-License-Identifier: Apache-2.0
"""Decision-queue budget instrumentation (issue athenaeum#1990).

Fixtures throughout are synthetic — no operator-corpus content, per this
repo's public-deployment-boundary rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from athenaeum.answers import raise_pending_question
from athenaeum.decision_answers import apply_decision_answers, write_decision_answer
from athenaeum.decision_budget import (
    budget_report,
    context_size_distribution,
    format_budget_report,
    item_age_p95_days,
    items_per_day,
    queue_depth_trend,
    read_decision_budget_events,
    record_decision_answered,
    record_overflow_shapes,
)
from athenaeum.decisions import decision_time_minutes
from athenaeum.pending_merges import parse_pending_merges, write_pending_merge

_NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def _event(decision_id: str, decision_type: str, raised_at: str, answered_at: str) -> dict:
    return {
        "decision_id": decision_id,
        "decision_type": decision_type,
        "raised_at": raised_at,
        "answered_at": answered_at,
    }


def test_record_and_read_roundtrip(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    record_decision_answered(
        wiki_root,
        decision_id="x1",
        decision_type="question",
        raised_at="2026-10-01T00:00:00Z",
        answered_at="2026-10-01T00:30:00Z",
    )
    events = read_decision_budget_events(wiki_root)
    assert len(events) == 1
    assert events[0]["decision_id"] == "x1"
    assert events[0]["answered_at"] == "2026-10-01T00:30:00Z"


def test_read_tolerates_torn_trailing_line(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    events_path = wiki_root / "_decision_budget_events.jsonl"
    events_path.write_text(
        '{"decision_id": "a", "decision_type": "question", '
        '"raised_at": "2026-10-01T00:00:00Z", "answered_at": "2026-10-01T00:10:00Z"}\n'
        '{"decision_id": "b", "decision_typ'  # torn trailing line
    )
    events = read_decision_budget_events(wiki_root)
    assert len(events) == 1
    assert events[0]["decision_id"] == "a"


def test_items_per_day_rolling_mean() -> None:
    events = [
        _event("a", "question", "2026-09-20T00:00:00Z", f"2026-10-0{d}T00:00:00Z")
        for d in range(1, 6)
    ]
    # 5 events inside a 10-day window -> mean 0.5/day.
    assert items_per_day(events, window_days=10, now=_NOW) == 0.5


def test_items_per_day_excludes_unparseable_answered_at() -> None:
    events = [_event("a", "question", "2026-10-01T00:00:00Z", "garbage")]
    assert items_per_day(events, window_days=30, now=_NOW) == 0.0


def test_decision_time_percentiles_excludes_missing_answered_at() -> None:
    from athenaeum.decision_budget import decision_time_percentiles

    events = [
        _event("a", "question", "2026-10-01T00:00:00Z", "2026-10-01T00:30:00Z"),
        _event("b", "merge", "2026-10-02T00:00:00Z", ""),  # unanswered -> excluded
    ]
    pct = decision_time_percentiles(events)
    assert pct["p50"] == 30.0
    assert pct["p95"] == 30.0


def test_decision_time_percentiles_empty_when_no_usable_pair() -> None:
    from athenaeum.decision_budget import decision_time_percentiles

    pct = decision_time_percentiles([_event("a", "question", "", "")])
    assert pct == {"p50": None, "p95": None}


def test_context_size_distribution_over_live_items() -> None:
    items = [{"context_tokens": v} for v in (100, 200, 300, 1500)]
    dist = context_size_distribution(items)
    assert dist is not None
    assert dist["count"] == 4
    assert dist["max"] == 1500


def test_context_size_distribution_empty_queue() -> None:
    assert context_size_distribution([]) is None


def test_item_age_p95_days() -> None:
    from datetime import date

    items = [{"created_at": "2026-09-01"}, {"created_at": "2026-10-01"}]
    p95 = item_age_p95_days(items, today=date(2026, 10, 7))
    assert p95 is not None
    assert p95 > 0


def test_queue_depth_trend_counts_open_items() -> None:
    pending = [{"created_at": "2026-10-01"}, {"created_at": "2026-10-05"}]
    events = [_event("a", "question", "2026-09-25T00:00:00Z", "2026-10-03T00:00:00Z")]
    trend = queue_depth_trend(pending, events, window_days=7, now=_NOW)
    assert len(trend) == 7
    by_date = {row["date"]: row["depth"] for row in trend}
    # On 2026-10-02 both still-open pending items plus the already-raised,
    # not-yet-answered ledger item are open: the two live items were raised
    # 10-01/10-05 (10-05 not yet raised by 10-02) and the ledger item is
    # open (raised 09-25, answered 10-03) -> depth 2 (the 10-01 pending item
    # + the ledger item; 10-05 hasn't been raised yet by this day).
    assert by_date["2026-10-02"] == 2
    # By 2026-10-06 the ledger item has long been answered (10-03) and both
    # pending items are raised (still open live) -> depth 2.
    assert by_date["2026-10-06"] == 2


def test_record_overflow_shapes_writes_on_breach(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    pending = [{"type": "question"}, {"type": "question"}, {"type": "merge"}]
    rec = record_overflow_shapes(
        wiki_root, breach_dims=["items_per_day"], pending_items=pending, now=_NOW
    )
    assert rec is not None
    assert rec["breach_dims"] == ["items_per_day"]
    assert rec["type_counts"] == {"merge": 1, "question": 2}
    shapes_path = wiki_root / "_decision_budget_shapes.jsonl"
    assert shapes_path.exists()


def test_record_overflow_shapes_noop_without_breach(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    rec = record_overflow_shapes(wiki_root, breach_dims=[], pending_items=[], now=_NOW)
    assert rec is None
    assert not (wiki_root / "_decision_budget_shapes.jsonl").exists()


def test_budget_report_flags_breach_and_records_shape(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    # 25 answered-today events exceeds a max of 1/day.
    for i in range(25):
        record_decision_answered(
            wiki_root,
            decision_id=f"q{i}",
            decision_type="question",
            raised_at="2026-10-07T00:00:00Z",
            answered_at="2026-10-07T00:05:00Z",
        )
    report = budget_report(
        wiki_root,
        pending_items=[],
        items_per_day_max=1,
        decision_minutes_p50_max=30,
        decision_minutes_p95_max=60,
        item_age_p95_days_max=7,
        window_days=1,
        now=_NOW,
    )
    assert report["breach"] is True
    assert "items_per_day" in report["breach_dims"]
    assert report["recorded_shape"] is not None


def test_budget_report_within_budget_on_empty_queue(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    report = budget_report(
        wiki_root,
        pending_items=[],
        items_per_day_max=20,
        decision_minutes_p50_max=30,
        decision_minutes_p95_max=60,
        item_age_p95_days_max=7,
        window_days=30,
        now=_NOW,
    )
    assert report["breach"] is False
    assert report["context_size_tokens"] is None
    assert report["item_age_p95_days"] is None


def test_raised_at_answered_at_pairing_through_question_apply(tmp_path: Path) -> None:
    """End-to-end: raising, answering, and applying a question produces a
    budget event whose raised_at/answered_at pairs correctly."""
    wiki_root = tmp_path / "wiki"
    raw_root = tmp_path / "raw"
    wiki_root.mkdir()
    raw_root.mkdir()
    pending_path = wiki_root / "_pending_questions.md"

    raised = raise_pending_question(pending_path, "Is X true?", "ctx", entity="X")
    qid = raised["decision_id"]
    write_decision_answer(raw_root, decision_id=qid, decision_type="question", verdict="yes")
    report = apply_decision_answers(wiki_root, raw_root)
    assert report.applied == 1

    events = read_decision_budget_events(wiki_root)
    assert len(events) == 1
    event = events[0]
    assert event["decision_id"] == qid
    assert event["raised_at"]
    assert event["answered_at"]
    minutes = decision_time_minutes(event["raised_at"], event["answered_at"])
    assert minutes is not None
    assert minutes >= 0


def test_raised_at_answered_at_pairing_through_merge_apply(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    raw_root = tmp_path / "raw"
    wiki_root.mkdir()
    raw_root.mkdir()
    merges_path = wiki_root / "_pending_merges.md"

    write_pending_merge(
        merges_path,
        merge_target_name="Foo Bar",
        sources=["a.md", "b.md"],
        rationale="r",
        draft_merged_body="body",
        confidence=0.9,
    )
    pm = parse_pending_merges(merges_path)[0]
    write_decision_answer(raw_root, decision_id=pm.id, decision_type="merge", verdict="reject")
    report = apply_decision_answers(wiki_root, raw_root)
    assert report.applied == 1

    events = read_decision_budget_events(wiki_root)
    assert len(events) == 1
    assert events[0]["decision_id"] == pm.id
    assert events[0]["decision_type"] == "merge"
    assert events[0]["raised_at"] == pm.created_at


class _FakeRaw:
    """Minimal RawFile double — path/source/ref (mirrors test_quarantine.py)."""

    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source

    @property
    def ref(self) -> str:
        return f"{self.source}/{self.path.name}"


def test_quarantine_release_records_budget_event(tmp_path: Path) -> None:
    from athenaeum.quarantine import (
        list_pending_quarantine,
        quarantine_file,
        release_quarantine,
    )

    wiki_root = tmp_path / "wiki"
    raw_root = tmp_path / "raw"
    sessions = raw_root / "sessions"
    sessions.mkdir(parents=True)
    wiki_root.mkdir()
    fpath = sessions / "20260101T000000Z-aabbccdd.md"
    fpath.write_text("content\n", encoding="utf-8")
    raw = _FakeRaw(fpath, "sessions")

    quarantine_file(
        raw,
        wiki_root=wiki_root,
        raw_root=raw_root,
        bound="byte_size",
        detail="too big",
        violations=1,
    )
    pending = list_pending_quarantine(wiki_root)
    assert len(pending) == 1
    qid = pending[0]["id"]

    release_quarantine(wiki_root, raw_root, quarantine_id=qid)
    events = read_decision_budget_events(wiki_root)
    assert len(events) == 1
    assert events[0]["decision_type"] == "quarantine"
    assert events[0]["decision_id"] == qid


# ---------------------------------------------------------------------------
# Issue athenaeum#1996 ratchet guard 2: format_budget_report's extension to
# carry the measured default-acceptance rubber-stamp rate -- OPTIONAL key,
# same shared rendering, never a third surface. budget_report() itself is
# untouched by athenaeum#1996 (its five figures + breach logic are out of
# scope); these tests only exercise the rendering extension.
# ---------------------------------------------------------------------------


def _minimal_report(**overrides: object) -> dict:
    report = budget_report(
        overrides.pop("wiki_root"),
        pending_items=[],
        items_per_day_max=20,
        decision_minutes_p50_max=30,
        decision_minutes_p95_max=60,
        item_age_p95_days_max=7,
        window_days=30,
        now=_NOW,
    )
    report.update(overrides)
    return report


def test_format_budget_report_omits_rubber_stamp_line_when_key_absent(
    tmp_path: Path,
) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    report = _minimal_report(wiki_root=wiki_root)
    assert "default_acceptance_rubber_stamp" not in report
    rendered = format_budget_report(report)
    assert "rubber-stamp" not in rendered.lower()


def test_format_budget_report_renders_rubber_stamp_when_present(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    report = _minimal_report(
        wiki_root=wiki_root,
        default_acceptance_rubber_stamp={
            "sampled": 10,
            "reviewed": 10,
            "overturned": 3,
            "rate": 0.7,
        },
    )
    rendered = format_budget_report(report)
    assert "Default-acceptance rubber-stamp rate: 70%" in rendered
    assert "sampled=10" in rendered
    assert "reviewed=10" in rendered
    assert "overturned=3" in rendered


def test_format_budget_report_rate_none_renders_as_not_yet_reviewed(
    tmp_path: Path,
) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    report = _minimal_report(
        wiki_root=wiki_root,
        default_acceptance_rubber_stamp={
            "sampled": 2,
            "reviewed": 0,
            "overturned": 0,
            "rate": None,
        },
    )
    rendered = format_budget_report(report)
    assert "n/a (none reviewed yet)" in rendered


def test_cmd_decisions_budget_surfaces_the_rate_via_the_shared_rendering(
    tmp_path: Path,
) -> None:
    """The real `athenaeum decisions budget` surface (issue athenaeum#1996):
    extends the SAME shared format_budget_report call with the measured
    rate, never a second rendering."""
    from athenaeum.calibration import sample_default_acceptance

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    sample_default_acceptance(
        wiki_root,
        decision_id="merge-1",
        config={"librarian": {"default_acceptance_audit_sample_rate": 1.0}},
    )
    report = _minimal_report(wiki_root=wiki_root)

    from athenaeum.calibration import default_acceptance_rubber_stamp_rate

    report["default_acceptance_rubber_stamp"] = default_acceptance_rubber_stamp_rate(
        wiki_root
    )
    rendered = format_budget_report(report)
    assert "Default-acceptance rubber-stamp rate: n/a (none reviewed yet)" in rendered
    assert "sampled=1" in rendered
