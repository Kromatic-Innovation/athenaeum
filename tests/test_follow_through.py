# SPDX-License-Identifier: Apache-2.0
"""Tests for the follow-through analyzer (issue athenaeum#2301).

All ledger/transcript rows here are HAND-AUTHORED SYNTHETIC fixtures — no
real transcript content enters this file, matching the issue's own hard
safety constraint.
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum import follow_through, push_metrics


def _seed_push(
    cache_dir: Path,
    *,
    session_id: str,
    pushed_id: str,
    ts: str,
) -> None:
    rec = push_metrics.build_push_record(
        session_id=session_id,
        query="q",
        backend="fts5",
        hits=[("f.md", {"uid": pushed_id}, push_metrics.estimate_tokens("body"))],
    )
    rec.ts = ts
    push_metrics.record_push(rec, cache_dir=cache_dir)


def _write_transcript(projects_root: Path, session_id: str, records: list[dict]) -> None:
    scope_dir = projects_root / "-scope-hash"
    scope_dir.mkdir(parents=True, exist_ok=True)
    (scope_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )


def _read_entity_block(tool_use_id: str, uid: str) -> dict:
    return {
        "type": "tool_use",
        "id": tool_use_id,
        "name": "mcp__athenaeum__read_entity",
        "input": {"uid": uid, "entity_class": "project"},
    }


def _recall_use_block(tool_use_id: str) -> dict:
    return {
        "type": "tool_use",
        "id": tool_use_id,
        "name": "mcp__athenaeum__recall",
        "input": {"query": "unrelated-query"},
    }


def _recall_result_block(tool_use_id: str, text: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}


def _read_file_block(tool_use_id: str, file_path: str) -> dict:
    return {
        "type": "tool_use",
        "id": tool_use_id,
        "name": "Read",
        "input": {"file_path": file_path},
    }


class TestOpenedAfterPush:
    def test_read_entity_after_push_counts_as_opened(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        projects = tmp_path / "projects"
        _seed_push(cache, session_id="sess-open", pushed_id="aaa11111", ts="2026-01-01T00:00:00Z")
        _write_transcript(
            projects,
            "sess-open",
            [
                {
                    "type": "assistant",
                    "sessionId": "sess-open",
                    "timestamp": "2026-01-01T00:05:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [_read_entity_block("tu1", "aaa11111")],
                    },
                },
            ],
        )

        report = follow_through.build_follow_through_report(
            cache_dir=cache, projects_root=projects
        )
        assert report.sessions_considered == 1
        assert report.class_counts[follow_through.CLASS_OPENED_AFTER_PUSH] == 1
        assert report.class_counts[follow_through.CLASS_CITED_WITHOUT_OPENING] == 0
        assert report.class_counts[follow_through.CLASS_NEITHER] == 0

    def test_file_read_after_push_counts_as_opened(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        projects = tmp_path / "projects"
        _seed_push(
            cache, session_id="sess-file", pushed_id="bbb22222", ts="2026-01-01T00:00:00Z"
        )
        _write_transcript(
            projects,
            "sess-file",
            [
                {
                    "type": "assistant",
                    "sessionId": "sess-file",
                    "timestamp": "2026-01-01T00:05:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            _read_file_block(
                                "tu1", "/knowledge/wiki/bbb22222-some-project.md"
                            )
                        ],
                    },
                },
            ],
        )

        report = follow_through.build_follow_through_report(
            cache_dir=cache, projects_root=projects
        )
        assert report.class_counts[follow_through.CLASS_OPENED_AFTER_PUSH] == 1

    def test_recall_resurfacing_the_pushed_id_counts_as_opened(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        projects = tmp_path / "projects"
        _seed_push(
            cache, session_id="sess-recall", pushed_id="ccc33333", ts="2026-01-01T00:00:00Z"
        )
        _write_transcript(
            projects,
            "sess-recall",
            [
                {
                    "type": "assistant",
                    "sessionId": "sess-recall",
                    "timestamp": "2026-01-01T00:05:00Z",
                    "message": {"role": "assistant", "content": [_recall_use_block("tu1")]},
                },
                {
                    "type": "user",
                    "sessionId": "sess-recall",
                    "timestamp": "2026-01-01T00:05:01Z",
                    "message": {
                        "role": "user",
                        "content": [_recall_result_block("tu1", "hit: ccc33333 — some project")],
                    },
                },
            ],
        )

        report = follow_through.build_follow_through_report(
            cache_dir=cache, projects_root=projects
        )
        assert report.class_counts[follow_through.CLASS_OPENED_AFTER_PUSH] == 1


class TestOpenPrecedesPush:
    def test_open_before_push_does_not_count_as_following(self, tmp_path: Path) -> None:
        """issue athenaeum#2301's explicit AC: an open that PRECEDES the push
        must not count as following it. The read_entity call at
        00:00:00Z happens strictly before the push at 00:10:00Z, so this
        page must land as 'neither' (no citation either), never
        'opened_after_push'."""
        cache = tmp_path / "cache"
        projects = tmp_path / "projects"
        _write_transcript(
            projects,
            "sess-precede",
            [
                {
                    "type": "assistant",
                    "sessionId": "sess-precede",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [_read_entity_block("tu1", "ddd44444")],
                    },
                },
            ],
        )
        _seed_push(
            cache, session_id="sess-precede", pushed_id="ddd44444", ts="2026-01-01T00:10:00Z"
        )

        report = follow_through.build_follow_through_report(
            cache_dir=cache, projects_root=projects
        )
        assert report.class_counts[follow_through.CLASS_OPENED_AFTER_PUSH] == 0
        assert report.class_counts[follow_through.CLASS_NEITHER] == 1


class TestCitedWithoutOpening:
    def test_cited_in_assistant_text_without_any_open_tool_call(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        projects = tmp_path / "projects"
        _seed_push(
            cache, session_id="sess-cited", pushed_id="eee55555", ts="2026-01-01T00:00:00Z"
        )
        _write_transcript(
            projects,
            "sess-cited",
            [
                {
                    "type": "assistant",
                    "sessionId": "sess-cited",
                    "timestamp": "2026-01-01T00:05:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Per eee55555, this is settled."}],
                    },
                },
            ],
        )

        report = follow_through.build_follow_through_report(
            cache_dir=cache, projects_root=projects
        )
        assert report.class_counts[follow_through.CLASS_CITED_WITHOUT_OPENING] == 1
        assert report.class_counts[follow_through.CLASS_OPENED_AFTER_PUSH] == 0


class TestNeither:
    def test_pushed_never_opened_never_cited(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        projects = tmp_path / "projects"
        _seed_push(
            cache, session_id="sess-neither", pushed_id="fff66666", ts="2026-01-01T00:00:00Z"
        )
        _write_transcript(
            projects,
            "sess-neither",
            [
                {
                    "type": "user",
                    "sessionId": "sess-neither",
                    "timestamp": "2026-01-01T00:05:00Z",
                    "message": {"role": "user", "content": "totally unrelated turn"},
                },
            ],
        )

        report = follow_through.build_follow_through_report(
            cache_dir=cache, projects_root=projects
        )
        assert report.class_counts[follow_through.CLASS_NEITHER] == 1
        assert report.class_counts[follow_through.CLASS_OPENED_AFTER_PUSH] == 0
        assert report.class_counts[follow_through.CLASS_CITED_WITHOUT_OPENING] == 0


class TestOpenedWithoutPush:
    def test_read_entity_for_an_id_never_pushed_in_this_session(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        projects = tmp_path / "projects"
        # This session pushed one id, so it is in scope (build_follow_through_report
        # only analyzes sessions that appear in the push ledger).
        _seed_push(
            cache, session_id="sess-extra", pushed_id="ggg77777", ts="2026-01-01T00:00:00Z"
        )
        _write_transcript(
            projects,
            "sess-extra",
            [
                {
                    "type": "assistant",
                    "sessionId": "sess-extra",
                    "timestamp": "2026-01-01T00:05:00Z",
                    "message": {
                        "role": "assistant",
                        # Opens a DIFFERENT id than the one pushed.
                        "content": [_read_entity_block("tu1", "hhh88888")],
                    },
                },
            ],
        )

        report = follow_through.build_follow_through_report(
            cache_dir=cache, projects_root=projects
        )
        assert report.opened_without_push_count == 1
        # The pushed id itself is still classified normally (neither, here).
        assert report.class_counts[follow_through.CLASS_NEITHER] == 1


class TestSessionLengthBucketing:
    def test_short_medium_long_boundaries(self) -> None:
        assert (
            follow_through._session_length_bucket(0) == follow_through.LENGTH_SHORT
        )
        assert (
            follow_through._session_length_bucket(
                follow_through.SHORT_SESSION_MAX_RECORDS - 1
            )
            == follow_through.LENGTH_SHORT
        )
        assert (
            follow_through._session_length_bucket(follow_through.SHORT_SESSION_MAX_RECORDS)
            == follow_through.LENGTH_MEDIUM
        )
        assert (
            follow_through._session_length_bucket(
                follow_through.MEDIUM_SESSION_MAX_RECORDS - 1
            )
            == follow_through.LENGTH_MEDIUM
        )
        assert (
            follow_through._session_length_bucket(follow_through.MEDIUM_SESSION_MAX_RECORDS)
            == follow_through.LENGTH_LONG
        )


class TestChannelNeverInferredFromLength:
    def test_channel_bucket_always_unknown_regardless_of_length(self) -> None:
        """issue athenaeum#2301: neither the push ledger nor the transcript
        carries an interactive-vs-headless field, and the bucket must NEVER
        be guessed from session length. Two record lists of very different
        lengths must both resolve to CHANNEL_UNKNOWN."""
        short_records: list[object] = [{"type": "user"}]
        long_records: list[object] = [{"type": "user"} for _ in range(500)]
        assert (
            follow_through._session_channel_bucket(short_records)
            == follow_through.CHANNEL_UNKNOWN
        )
        assert (
            follow_through._session_channel_bucket(long_records)
            == follow_through.CHANNEL_UNKNOWN
        )

    def test_report_segments_only_ever_carry_the_unknown_channel_bucket(
        self, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        projects = tmp_path / "projects"
        _seed_push(cache, session_id="sess-ch", pushed_id="iii99999", ts="2026-01-01T00:00:00Z")
        _write_transcript(
            projects,
            "sess-ch",
            [
                {
                    "type": "user",
                    "sessionId": "sess-ch",
                    "timestamp": "2026-01-01T00:05:00Z",
                    "message": {"role": "user", "content": "x"},
                }
            ],
        )
        report = follow_through.build_follow_through_report(
            cache_dir=cache, projects_root=projects
        )
        channel_buckets = {seg["channel_bucket"] for seg in report.to_dict()["segments"]}
        assert channel_buckets == {follow_through.CHANNEL_UNKNOWN}


class TestNoIdentifiersInOutput:
    def test_serialized_report_never_carries_session_or_page_identifiers(
        self, tmp_path: Path
    ) -> None:
        """issue athenaeum#2301 AC: output contains rates and counts only —
        no session IDs, no page text, no query text. Build a report over a
        ledger/transcript pair carrying distinctive synthetic identifiers,
        then scan the serialized JSON for every one of them."""
        cache = tmp_path / "cache"
        projects = tmp_path / "projects"
        session_id = "sess-forbidden-identifier-zzz"
        pushed_id = "zzzforbiddenpageid00"
        opened_only_id = "yyyforbiddenopenonly0"
        query_text = "forbidden-query-text-marker"

        rec = push_metrics.build_push_record(
            session_id=session_id,
            query=query_text,
            backend="fts5",
            hits=[("f.md", {"uid": pushed_id}, push_metrics.estimate_tokens("body"))],
        )
        rec.ts = "2026-01-01T00:00:00Z"
        push_metrics.record_push(rec, cache_dir=cache)

        _write_transcript(
            projects,
            session_id,
            [
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "timestamp": "2026-01-01T00:05:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": f"Per {pushed_id}, noted."},
                            _read_entity_block("tu1", opened_only_id),
                        ],
                    },
                },
            ],
        )

        report = follow_through.build_follow_through_report(
            cache_dir=cache, projects_root=projects
        )
        serialized = json.dumps(report.to_dict())
        for forbidden in (session_id, pushed_id, opened_only_id, query_text):
            assert forbidden not in serialized, f"{forbidden!r} leaked into the report"


class TestScopeIsPushLedgerSessionsOnly:
    def test_session_missing_transcript_is_reported_not_guessed(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        projects = tmp_path / "projects"
        projects.mkdir()
        _seed_push(
            cache, session_id="sess-no-transcript", pushed_id="jjj00000", ts="2026-01-01T00:00:00Z"
        )

        report = follow_through.build_follow_through_report(
            cache_dir=cache, projects_root=projects
        )
        assert report.sessions_considered == 0
        assert report.sessions_skipped_no_transcript == 1
        assert report.pushed_page_total == 0
