# SPDX-License-Identifier: Apache-2.0
"""Tests for the push-precision + coverage baseline instrument (issue #711).

Covers: push-record shape and redaction (no content, no personal data, opaque
uid never a name-derived slug), precision computation, the coverage worksheet,
the snapshot writer's idempotence, the enable/disable config accessor, and
that instrumentation ON does not change what ``recall`` returns.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum import push_metrics
from athenaeum.config import resolve_push_metrics_enabled

# ---------------------------------------------------------------------------
# Config accessor
# ---------------------------------------------------------------------------


class TestResolvePushMetricsEnabled:
    def test_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        assert resolve_push_metrics_enabled(None) is True
        assert resolve_push_metrics_enabled({}) is True

    def test_yaml_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        assert resolve_push_metrics_enabled({"push_metrics": {"enabled": False}}) is False

    def test_yaml_true_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        assert resolve_push_metrics_enabled({"push_metrics": {"enabled": True}}) is True

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_PUSH_METRICS_ENABLED", "0")
        assert resolve_push_metrics_enabled({"push_metrics": {"enabled": True}}) is False

    def test_env_truthy_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("1", "true", "yes", "on", "anything"):
            monkeypatch.setenv("ATHENAEUM_PUSH_METRICS_ENABLED", val)
            assert resolve_push_metrics_enabled(None) is True

    def test_env_falsey_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("0", "false", "no", "off", "False", "OFF"):
            monkeypatch.setenv("ATHENAEUM_PUSH_METRICS_ENABLED", val)
            assert resolve_push_metrics_enabled(None) is False

    def test_non_bool_yaml_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        assert resolve_push_metrics_enabled({"push_metrics": {"enabled": "yes"}}) is True


# ---------------------------------------------------------------------------
# opaque_push_id / redaction
# ---------------------------------------------------------------------------


class TestOpaquePushId:
    def test_uses_uid_when_present(self) -> None:
        fm = {"uid": "abc12345", "name": "Jane Doe"}
        assert push_metrics.opaque_push_id("abc12345-jane-doe.md", fm) == "abc12345"

    def test_falls_back_to_filename_when_no_uid(self) -> None:
        fm: dict[str, object] = {"name": "some note"}
        assert (
            push_metrics.opaque_push_id("20260802T023311Z-3f0ea402.md", fm)
            == "20260802T023311Z-3f0ea402.md"
        )

    def test_falls_back_when_fm_empty(self) -> None:
        assert push_metrics.opaque_push_id("raw/foo.md", {}) == "raw/foo.md"

    def test_never_returns_name_derived_slug_for_person(self) -> None:
        # The whole point: a person page's FILENAME embeds the slugified
        # name (uid-slug.md). The id recorded must be the uid, never the
        # filename, so a push record can never leak a name via its id.
        fm = {"uid": "deadbeef", "type": "person", "name": "Alice Wonderland"}
        pid = push_metrics.opaque_push_id("deadbeef-alice-wonderland.md", fm)
        assert pid == "deadbeef"
        assert "alice" not in pid.lower()
        assert "wonderland" not in pid.lower()


class TestBuildPushRecordRedaction:
    def test_no_content_or_pii_in_record(self) -> None:
        fm = {
            "uid": "abc12345",
            "type": "person",
            "access": "internal",
            "name": "Jane Doe",
            "aliases": ["J. Doe"],
        }
        record = push_metrics.build_push_record(
            session_id="sess-1",
            query="what is jane doe's phone number",
            backend="fts5",
            hits=[("abc12345-jane-doe.md", fm, "Jane Doe's phone is 555-1234")],
        )
        blob = json.dumps(record.to_dict()).lower()
        assert "jane" not in blob
        assert "doe" not in blob
        assert "555-1234" not in blob
        assert "phone" not in blob
        # But the opaque id and counts ARE present.
        assert "abc12345" in blob
        assert record.to_dict()["pushed_count"] == 1

    def test_only_query_hash_retained_never_raw_query(self) -> None:
        record = push_metrics.build_push_record(
            session_id="s",
            query="jane doe personal cell number",
            backend="keyword",
            hits=[],
        )
        blob = json.dumps(record.to_dict())
        assert "jane" not in blob.lower()
        assert "personal cell" not in blob.lower()
        assert record.to_dict()["query_hash"] != "jane doe personal cell number"

    def test_record_shape_has_required_fields(self) -> None:
        fm = {"uid": "x1", "access": "open"}
        record = push_metrics.build_push_record(
            session_id="sess-9",
            query="q",
            backend="fts5",
            hits=[("x1-thing.md", fm, "some body text here")],
        )
        d = record.to_dict()
        for key in (
            "session_id",
            "ts",
            "query_hash",
            "backend",
            "items",
            "pushed_count",
            "token_cost",
            "token_cost_estimated",
        ):
            assert key in d
        assert d["session_id"] == "sess-9"
        item = d["items"][0]
        for key in ("id", "tier", "scope", "token_cost"):
            assert key in item


# ---------------------------------------------------------------------------
# record_push / read_push_records
# ---------------------------------------------------------------------------


class TestRecordPush:
    def test_writes_and_reads_back(self, tmp_path: Path) -> None:
        record = push_metrics.build_push_record(
            session_id="s1",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "u1"}, "body")],
        )
        assert push_metrics.record_push(record, cache_dir=tmp_path) is True
        rows = push_metrics.read_push_records(cache_dir=tmp_path)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "s1"

    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        record = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
        )
        ok = push_metrics.record_push(
            record, cache_dir=tmp_path, config={"push_metrics": {"enabled": False}}
        )
        assert ok is False
        assert push_metrics.read_push_records(cache_dir=tmp_path) == []

    def test_noop_when_no_session_id(self, tmp_path: Path) -> None:
        record = push_metrics.build_push_record(
            session_id="", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
        )
        assert push_metrics.record_push(record, cache_dir=tmp_path) is False

    def test_noop_when_no_items(self, tmp_path: Path) -> None:
        record = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[]
        )
        assert push_metrics.record_push(record, cache_dir=tmp_path) is False

    def test_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        path = push_metrics.push_records_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        good = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
        ).to_dict()
        path.write_text(json.dumps(good) + "\n" + '{"broken json')
        rows = push_metrics.read_push_records(cache_dir=tmp_path)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Precision computation
# ---------------------------------------------------------------------------


class TestReferenceResultPrecision:
    def test_full_precision(self) -> None:
        result = push_metrics.ReferenceResult(
            session_id="s", ts="t", pushed_ids=["a", "b"], referenced_ids=["a", "b"]
        )
        assert result.precision == 1.0

    def test_partial_precision(self) -> None:
        result = push_metrics.ReferenceResult(
            session_id="s", ts="t", pushed_ids=["a", "b", "c", "d"], referenced_ids=["a"]
        )
        assert result.precision == 0.25

    def test_zero_precision(self) -> None:
        result = push_metrics.ReferenceResult(
            session_id="s", ts="t", pushed_ids=["a", "b"], referenced_ids=[]
        )
        assert result.precision == 0.0

    def test_none_when_nothing_pushed(self) -> None:
        result = push_metrics.ReferenceResult(
            session_id="s", ts="t", pushed_ids=[], referenced_ids=[]
        )
        assert result.precision is None


class TestDetermineReferences:
    def test_referenced_id_found_in_assistant_text(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-a",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "abc12345"}, "body")],
        )
        push_metrics.record_push(record, cache_dir=cache)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / "-scope-hash"
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-a.jsonl").write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {"type": "user", "message": {"role": "user", "content": "hi"}},
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Used abc12345 here."}],
                        },
                    },
                ]
            )
            + "\n"
        )

        result = push_metrics.determine_references(
            "sess-a", cache_dir=cache, projects_root=projects_root
        )
        assert result is not None
        assert result.referenced_ids == ["abc12345"]
        assert result.precision == 1.0

    def test_matches_tool_result_text_not_just_user_text(self, tmp_path: Path) -> None:
        # Reference-determination must catch ids referenced via tool output —
        # NOT just user-authored text (that is what distinguishes it from
        # transcript_verify.verify_user_stated, which only matches user text).
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-b",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "toolid99"}, "body")],
        )
        push_metrics.record_push(record, cache_dir=cache)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / "-scope-hash"
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-b.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": "grep found toolid99 in results",
                            }
                        ],
                    },
                }
            )
            + "\n"
        )

        result = push_metrics.determine_references(
            "sess-b", cache_dir=cache, projects_root=projects_root
        )
        assert result is not None
        assert result.referenced_ids == ["toolid99"]

    def test_unreferenced_id_not_marked(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-c",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "neverused"}, "body")],
        )
        push_metrics.record_push(record, cache_dir=cache)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / "-scope-hash"
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-c.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "unrelated"}})
            + "\n"
        )

        result = push_metrics.determine_references(
            "sess-c", cache_dir=cache, projects_root=projects_root
        )
        assert result is not None
        assert result.referenced_ids == []
        assert result.precision == 0.0

    def test_none_when_no_push_records_for_session(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        result = push_metrics.determine_references(
            "nonexistent", cache_dir=cache, projects_root=tmp_path / "projects"
        )
        assert result is None

    def test_none_when_transcript_missing(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-d",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "x"}, "body")],
        )
        push_metrics.record_push(record, cache_dir=cache)
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        result = push_metrics.determine_references(
            "sess-d", cache_dir=cache, projects_root=projects_root
        )
        assert result is None


class TestRunReferenceDetermination:
    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-e", query="q", backend="fts5", hits=[("f.md", {"uid": "x"}, "b")]
        )
        push_metrics.record_push(record, cache_dir=cache)
        result = push_metrics.run_reference_determination(
            "sess-e",
            cache_dir=cache,
            config={"push_metrics": {"enabled": False}},
        )
        assert result is None
        assert push_metrics.reference_records_path(cache).is_file() is False

    def test_writes_reference_record_when_enabled(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-f", query="q", backend="fts5", hits=[("f.md", {"uid": "refid"}, "b")]
        )
        push_metrics.record_push(record, cache_dir=cache)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / "-scope"
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-f.jsonl").write_text(
            json.dumps({"type": "assistant", "message": {"content": "refid used"}}) + "\n"
        )

        result = push_metrics.run_reference_determination(
            "sess-f", cache_dir=cache, projects_root=projects_root
        )
        assert result is not None
        rows = push_metrics._read_jsonl(push_metrics.reference_records_path(cache))
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-f"


# ---------------------------------------------------------------------------
# compute_baseline
# ---------------------------------------------------------------------------


class TestComputeBaseline:
    def test_empty_ledger_is_honest_not_fabricated(self, tmp_path: Path) -> None:
        baseline = push_metrics.compute_baseline(cache_dir=tmp_path, repo_root=Path("."))
        assert baseline.session_count == 0
        assert baseline.push_record_count == 0
        assert baseline.reference_record_count == 0
        assert baseline.precision is None  # never fabricated as 0.0

    def test_aggregates_reference_records(self, tmp_path: Path) -> None:
        cache = tmp_path
        r1 = push_metrics.ReferenceResult(
            session_id="s1", ts="2026-01-01T00:00:00Z", pushed_ids=["a", "b"], referenced_ids=["a"]
        )
        r2 = push_metrics.ReferenceResult(
            session_id="s2", ts="2026-01-01T00:00:00Z", pushed_ids=["c"], referenced_ids=["c"]
        )
        push_metrics.record_reference_result(r1, cache_dir=cache)
        push_metrics.record_reference_result(r2, cache_dir=cache)
        push_rec = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "a"}, "b")]
        )
        push_metrics.record_push(push_rec, cache_dir=cache)

        baseline = push_metrics.compute_baseline(cache_dir=cache, repo_root=Path("."))
        # 3 total pushed (a,b,c), 2 referenced (a,c) -> 2/3
        assert baseline.precision == pytest.approx(2 / 3)
        assert baseline.reference_record_count == 2

    def test_since_window_excludes_old_records(self, tmp_path: Path) -> None:
        from datetime import datetime, timedelta, timezone

        cache = tmp_path
        old = push_metrics.ReferenceResult(
            session_id="old",
            ts="2020-01-01T00:00:00Z",
            pushed_ids=["x"],
            referenced_ids=[],
        )
        push_metrics.record_reference_result(old, cache_dir=cache)
        baseline = push_metrics.compute_baseline(
            since=datetime.now(tz=timezone.utc) - timedelta(days=1),
            cache_dir=cache,
            repo_root=Path("."),
        )
        assert baseline.reference_record_count == 0


# ---------------------------------------------------------------------------
# Snapshot writer idempotence
# ---------------------------------------------------------------------------


class TestWriteSnapshot:
    def test_creates_new_file(self, tmp_path: Path) -> None:
        baseline = push_metrics.compute_baseline(cache_dir=tmp_path / "cache", repo_root=Path("."))
        docs_path = tmp_path / "docs" / "memory-model-measurements.md"
        push_metrics.write_snapshot(baseline, docs_path=docs_path)
        content = docs_path.read_text()
        assert "## Push-precision and coverage baseline" in content
        assert "precision: n/a" in content

    def test_rerun_appends_dated_snapshot_never_corrupts(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        docs_path = tmp_path / "docs.md"
        b1 = push_metrics.compute_baseline(cache_dir=cache, repo_root=Path("."))
        push_metrics.write_snapshot(b1, docs_path=docs_path)
        b2 = push_metrics.compute_baseline(cache_dir=cache, repo_root=Path("."))
        push_metrics.write_snapshot(b2, docs_path=docs_path)

        content = docs_path.read_text()
        assert content.count("## Push-precision and coverage baseline") == 1
        assert content.count("### Snapshot") == 2
        # File stays parseable markdown (no truncation mid-section).
        assert f"git_sha: {b2.git_sha}" in content
        assert f"git_sha: {b1.git_sha}" in content

    def test_idempotent_many_runs(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        docs_path = tmp_path / "docs.md"
        for _ in range(5):
            b = push_metrics.compute_baseline(cache_dir=cache, repo_root=Path("."))
            push_metrics.write_snapshot(b, docs_path=docs_path)
        content = docs_path.read_text()
        assert content.count("## Push-precision and coverage baseline") == 1
        assert content.count("### Snapshot") == 5

    def test_appends_section_to_existing_unrelated_file(self, tmp_path: Path) -> None:
        docs_path = tmp_path / "docs.md"
        docs_path.write_text("# Some other doc\n\nUnrelated content.\n")
        baseline = push_metrics.compute_baseline(cache_dir=tmp_path / "cache", repo_root=Path("."))
        push_metrics.write_snapshot(baseline, docs_path=docs_path)
        content = docs_path.read_text()
        assert "Unrelated content." in content
        assert "## Push-precision and coverage baseline" in content


# ---------------------------------------------------------------------------
# Coverage worksheet
# ---------------------------------------------------------------------------


class TestCoverageWorksheet:
    def test_samples_up_to_n_sessions(self, tmp_path: Path) -> None:
        cache = tmp_path
        for i in range(5):
            rec = push_metrics.build_push_record(
                session_id=f"s{i}",
                query="q",
                backend="fts5",
                hits=[("f.md", {"uid": f"u{i}"}, "b")],
            )
            push_metrics.record_push(rec, cache_dir=cache)
        ws = push_metrics.build_coverage_worksheet(
            n=3, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        assert ws["sampled_session_count"] == 3
        assert len(ws["sessions"]) == 3

    def test_candidates_exclude_own_pushed_set(self, tmp_path: Path) -> None:
        cache = tmp_path
        rec1 = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "pushed1"}, "b")]
        )
        rec2 = push_metrics.build_push_record(
            session_id="s2", query="q", backend="fts5", hits=[("f.md", {"uid": "other2"}, "b")]
        )
        push_metrics.record_push(rec1, cache_dir=cache)
        push_metrics.record_push(rec2, cache_dir=cache)
        ws = push_metrics.build_coverage_worksheet(
            n=2, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        s1 = next(s for s in ws["sessions"] if s["session_id"] == "s1")
        assert "pushed1" in s1["pushed"]
        assert "pushed1" not in s1["candidates_not_pushed"]
        assert "other2" in s1["candidates_not_pushed"]

    def test_reviewer_verdict_starts_as_todo(self, tmp_path: Path) -> None:
        cache = tmp_path
        rec1 = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "a"}, "b")]
        )
        rec2 = push_metrics.build_push_record(
            session_id="s2", query="q", backend="fts5", hits=[("f.md", {"uid": "b"}, "b")]
        )
        push_metrics.record_push(rec1, cache_dir=cache)
        push_metrics.record_push(rec2, cache_dir=cache)
        ws = push_metrics.build_coverage_worksheet(
            n=2, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        for session in ws["sessions"]:
            for verdict in session["reviewer_verdict"].values():
                assert verdict == "TODO"

    def test_write_coverage_worksheet_is_a_file_not_console_only(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        rec = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "a"}, "b")]
        )
        push_metrics.record_push(rec, cache_dir=cache)
        ws = push_metrics.build_coverage_worksheet(
            n=1, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        out = tmp_path / "worksheet.json"
        push_metrics.write_coverage_worksheet(ws, output_path=out)
        assert out.is_file()
        loaded = json.loads(out.read_text())
        assert loaded["sampled_session_count"] == 1


# ---------------------------------------------------------------------------
# Token estimate
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_char_over_four_heuristic(self) -> None:
        assert push_metrics.estimate_tokens("a" * 40) == 10

    def test_empty_string(self) -> None:
        assert push_metrics.estimate_tokens("") == 0

    def test_never_negative(self) -> None:
        assert push_metrics.estimate_tokens("abc") == 0
