# SPDX-License-Identifier: Apache-2.0
"""Tests for the memory-decay bucket / suggested valid_until slice (issue athenaeum#904).

Organized by acceptance criterion:

- AC1: intake records (``AutoMemoryFile``) and ``remember()`` accept an
  optional ``bucket`` + suggested ``valid_until``.
- AC2: a shape rule may set the same two fields on the correction records it
  emits, and ``corrections.process_correction_record`` applies them.
- AC3: compiled wiki pages (the auto-memory cluster-merge path) carry
  ``bucket`` in frontmatter alongside the existing validity fields.
- AC4/AC5: recall's currency-aware ranking deprioritizes an expired
  ``daily``-bucket page by default, and does NOT when the caller opts into
  ``history=True``.

AC6/AC7 (the deterministic sweep) are covered separately in
``tests/test_decay_sweep.py``, mirroring ``test_auto_memory_prune.py``'s own
split.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from athenaeum.intake import discover_auto_memory_files
from athenaeum.mcp_server import (
    _is_deprioritized_for_currency,
    _reorder_hits_by_currency,
    recall_search,
    remember_write,
)
from athenaeum.models import MEMORY_BUCKETS, coerce_bucket, parse_bucket
from athenaeum.rules import CorrectionSpec, build_correction_record

# ---------------------------------------------------------------------------
# AC1 (boundary primitives): coerce_bucket / parse_bucket
# ---------------------------------------------------------------------------


class TestBucketPrimitives:
    def test_memory_buckets_enum(self) -> None:
        assert MEMORY_BUCKETS == {"daily", "weekly", "durable"}

    @pytest.mark.parametrize("value", sorted(MEMORY_BUCKETS))
    def test_coerce_bucket_accepts_enum_members(self, value: str) -> None:
        assert coerce_bucket(value) == value

    def test_coerce_bucket_unset_is_empty(self) -> None:
        assert coerce_bucket(None) == ""
        assert coerce_bucket("") == ""

    def test_coerce_bucket_rejects_invalid(self) -> None:
        with pytest.raises(ValueError, match="monthly"):
            coerce_bucket("monthly")

    def test_parse_bucket_fail_open_on_invalid(self) -> None:
        # Read-side: a corrupted on-disk value degrades to "" rather than
        # raising (unlike the write-time coerce_bucket boundary).
        assert parse_bucket({"bucket": "monthly"}) == ""
        assert parse_bucket({}) == ""
        assert parse_bucket(None) == ""

    def test_parse_bucket_reads_valid_value(self) -> None:
        assert parse_bucket({"bucket": "daily"}) == "daily"


# ---------------------------------------------------------------------------
# AC1: intake records (AutoMemoryFile) carry bucket
# ---------------------------------------------------------------------------


class TestAutoMemoryFileBucket:
    def _write_member(
        self, root: Path, *, bucket: str | None, valid_until: str | None = None
    ) -> None:
        scope_dir = root / "raw" / "auto-memory" / "-Users-alice-Code-projectx"
        scope_dir.mkdir(parents=True, exist_ok=True)
        lines = ["---", "name: current focus", "type: feedback"]
        if bucket is not None:
            lines.append(f"bucket: {bucket}")
        if valid_until is not None:
            lines.append(f"valid_until: '{valid_until}'")
        lines += ["---", "", "Working on the athenaeum#904 slice today.", ""]
        (scope_dir / "feedback_current_focus.md").write_text(
            "\n".join(lines), encoding="utf-8"
        )

    def test_bucket_propagates_from_frontmatter(self, tmp_path: Path) -> None:
        self._write_member(tmp_path, bucket="daily")
        files = discover_auto_memory_files(tmp_path)
        assert len(files) == 1
        assert files[0].bucket == "daily"

    def test_unset_bucket_is_empty_string(self, tmp_path: Path) -> None:
        self._write_member(tmp_path, bucket=None)
        files = discover_auto_memory_files(tmp_path)
        assert len(files) == 1
        assert files[0].bucket == ""

    def test_invalid_on_disk_bucket_fails_open(self, tmp_path: Path) -> None:
        # A hand-edited/corrupted bucket must not crash discovery.
        self._write_member(tmp_path, bucket="monthly")
        files = discover_auto_memory_files(tmp_path)
        assert len(files) == 1
        assert files[0].bucket == ""


# ---------------------------------------------------------------------------
# AC1: remember() / remember_write() accept bucket + suggested valid_until
# ---------------------------------------------------------------------------


class TestRememberBucket:
    def test_bucket_written_to_frontmatter(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(raw, "Daily status note", bucket="daily")
        assert result.startswith("Saved to")
        files = list((raw / "claude-session").glob("*.md"))
        assert len(files) == 1
        assert "bucket: daily" in files[0].read_text()

    def test_invalid_bucket_rejected_at_boundary(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(raw, "content", bucket="monthly")
        assert result.startswith("Error")
        assert "bucket" in result
        # Nothing written -- rejection happens before any filesystem write.
        assert not (raw / "claude-session").exists()

    def test_unset_bucket_writes_nothing(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        remember_write(raw, "content")
        files = list((raw / "claude-session").glob("*.md"))
        assert "bucket:" not in files[0].read_text()

    def test_suggested_valid_until_written(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        remember_write(raw, "content", valid_until="2026-08-20")
        files = list((raw / "claude-session").glob("*.md"))
        assert "valid_until: '2026-08-20'" in files[0].read_text()

    def test_malformed_valid_until_silently_dropped(self, tmp_path: Path) -> None:
        # Fail-open, matching every other valid_until write path -- not an
        # Error string (that posture is reserved for `bucket`).
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(raw, "content", valid_until="not-a-date")
        assert result.startswith("Saved to")
        files = list((raw / "claude-session").glob("*.md"))
        assert "valid_until" not in files[0].read_text()


# ---------------------------------------------------------------------------
# AC2: shape-rule-emitted correction records carry bucket/valid_until
# ---------------------------------------------------------------------------


class TestShapeRuleCorrectionSpec:
    def _spec(self, **overrides) -> CorrectionSpec:
        base = dict(
            target={"uid": "person-alex"},
            op="set",
            field="bounced",
            value="2026-08-06",
            source="script:test-rule",
        )
        base.update(overrides)
        return CorrectionSpec(**base)

    def test_bucket_carried_into_emitted_record(self) -> None:
        spec = self._spec(bucket="daily")
        out = build_correction_record(spec, {}, rule_tag="test-rule")
        assert out["bucket"] == "daily"

    def test_omitted_by_default(self) -> None:
        spec = self._spec()
        out = build_correction_record(spec, {}, rule_tag="test-rule")
        assert "bucket" not in out
        assert "valid_until" not in out

    def test_invalid_bucket_rejected_at_rule_load_time(self) -> None:
        with pytest.raises(PydanticValidationError, match="monthly"):
            self._spec(bucket="monthly")

    def test_valid_until_interpolates_from_record(self) -> None:
        spec = self._spec(valid_until="$expiry")
        out = build_correction_record(spec, {"expiry": "2026-08-20"}, rule_tag="test-rule")
        assert out["valid_until"] == "2026-08-20"


# ---------------------------------------------------------------------------
# AC2 (apply side): corrections.process_correction_record applies bucket /
# valid_until onto the target entity's page-level frontmatter.
# ---------------------------------------------------------------------------


def _write_wiki_page(wiki: Path, filename: str, meta: dict, body: str = "Body.\n") -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    page = wiki / filename
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    page.write_text("\n".join(lines) + f"\n\n{body}", encoding="utf-8")
    return page


def _correction_envelope(**overrides) -> dict:
    env = {
        "record": "batch",
        "schema_version": 1,
        "submitter": "delivery-monitor",
        "batch_id": "20260806T140211Z-9f3ac1d2",
        "created_at": "2026-08-06T14:02:11Z",
        "defaults": {},
    }
    env.update(overrides)
    return env


class TestCorrectionsBucketApply:
    def _fields_config(self) -> dict:
        return {
            "librarian": {
                "corrections": {
                    "fields": {
                        "current_title": {
                            "shape": "scalar",
                            "writers": ["delivery-monitor"],
                        }
                    }
                }
            }
        }

    def _record(self, **overrides) -> dict:
        rec = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "set",
            "field": "current_title",
            "value": "VP Engineering",
            "source": "api:delivery-monitor",
            "observed_at": "2026-08-06T05:58:40Z",
        }
        rec.update(overrides)
        return rec

    def test_bucket_applied_on_correction(self, tmp_path: Path) -> None:
        from athenaeum.corrections import process_correction_record
        from athenaeum.models import EntityIndex

        wiki = tmp_path / "wiki"
        page = _write_wiki_page(
            wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"}
        )
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(bucket="daily"),
            _correction_envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._fields_config(),
        )
        assert result.disposition == "applied"
        assert "bucket: daily" in page.read_text()

    def test_invalid_bucket_raises_tier(self, tmp_path: Path) -> None:
        from athenaeum.corrections import process_correction_record
        from athenaeum.models import EntityIndex

        wiki = tmp_path / "wiki"
        _write_wiki_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(bucket="monthly"),
            _correction_envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._fields_config(),
        )
        assert result.disposition == "raised-tier"
        assert "bucket" in result.reason

    def test_valid_until_suggestion_fills_absent_bound(self, tmp_path: Path) -> None:
        from athenaeum.corrections import process_correction_record
        from athenaeum.models import EntityIndex

        wiki = tmp_path / "wiki"
        page = _write_wiki_page(
            wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"}
        )
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(valid_until="2026-08-20"),
            _correction_envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._fields_config(),
        )
        assert result.disposition == "applied"
        assert "valid_until: '2026-08-20'" in page.read_text()

    def test_valid_until_suggestion_never_overrides_explicit(self, tmp_path: Path) -> None:
        from athenaeum.corrections import process_correction_record
        from athenaeum.models import EntityIndex

        wiki = tmp_path / "wiki"
        page = _write_wiki_page(
            wiki,
            "p.md",
            {
                "uid": "person-a",
                "type": "person",
                "name": "A",
                "valid_until": "2026-01-01",
            },
        )
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(valid_until="2026-08-20"),
            _correction_envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._fields_config(),
        )
        assert result.disposition == "applied"
        # The explicit, pre-existing bound survives untouched (round-tripped
        # through YAML as a native date, hence unquoted on disk).
        assert "valid_until: 2026-01-01" in page.read_text()
        # Key-scoped, not a bare substring: the write path also stamps
        # ``updated: <today>``, so an unqualified ``"2026-08-20" not in ...``
        # fires on any run whose UTC date happens to equal this literal.
        assert "valid_until: 2026-08-20" not in page.read_text()


# ---------------------------------------------------------------------------
# AC3: compiled wiki pages (auto-memory cluster-merge path) carry `bucket`
# ---------------------------------------------------------------------------


class TestMergeCompileBucket:
    def test_bucket_stamped_on_compiled_page(self, tmp_path: Path) -> None:
        from athenaeum.merge import merge_cluster_row, render_merged_entry

        member = tmp_path / "current_focus.md"
        member.write_text(
            "---\nname: Current focus\ntype: feedback\nbucket: daily\n---\n\n"
            "Working the athenaeum#904 slice.\n",
            encoding="utf-8",
        )
        row = {
            "cluster_id": "c-0001",
            "member_paths": [str(member)],
            "centroid_score": 1.0,
        }
        entry = merge_cluster_row(row, extra_roots=[tmp_path], am_by_path={})
        assert entry is not None
        assert entry.bucket == "daily"

        rendered = render_merged_entry(entry)
        assert "bucket: daily" in rendered

    def test_unset_bucket_omitted_from_compiled_page(self, tmp_path: Path) -> None:
        from athenaeum.merge import merge_cluster_row, render_merged_entry

        member = tmp_path / "current_focus.md"
        member.write_text(
            "---\nname: Current focus\ntype: feedback\n---\n\nNo bucket here.\n",
            encoding="utf-8",
        )
        row = {
            "cluster_id": "c-0002",
            "member_paths": [str(member)],
            "centroid_score": 1.0,
        }
        entry = merge_cluster_row(row, extra_roots=[tmp_path], am_by_path={})
        assert entry is not None
        assert entry.bucket == ""
        assert "bucket:" not in render_merged_entry(entry)

    def test_first_active_member_wins_on_disagreement(self, tmp_path: Path) -> None:
        from athenaeum.merge import merge_cluster_row

        m1 = tmp_path / "m1.md"
        m1.write_text(
            "---\nname: M1\ntype: feedback\nbucket: weekly\n---\n\nFirst.\n",
            encoding="utf-8",
        )
        m2 = tmp_path / "m2.md"
        m2.write_text(
            "---\nname: M2\ntype: feedback\nbucket: daily\n---\n\nSecond.\n",
            encoding="utf-8",
        )
        row = {
            "cluster_id": "c-0003",
            "member_paths": [str(m1), str(m2)],
            "centroid_score": 1.0,
        }
        entry = merge_cluster_row(row, extra_roots=[tmp_path], am_by_path={})
        assert entry is not None
        assert entry.bucket == "weekly"


# ---------------------------------------------------------------------------
# AC4/AC5: currency-aware recall ranking
# ---------------------------------------------------------------------------


class TestCurrencyDeprioritization:
    def test_expired_daily_page_deprioritized(self) -> None:
        expired_daily = {"bucket": "daily", "valid_until": "2020-01-01"}
        assert _is_deprioritized_for_currency(expired_daily) is True

    def test_unexpired_daily_page_not_deprioritized(self) -> None:
        future_daily = {"bucket": "daily", "valid_until": "2099-01-01"}
        assert _is_deprioritized_for_currency(future_daily) is False

    def test_no_valid_until_daily_page_not_deprioritized(self) -> None:
        # Fail-open per athenaeum#308: absent valid_until => open => still valid.
        assert _is_deprioritized_for_currency({"bucket": "daily"}) is False

    def test_expired_weekly_page_not_deprioritized(self) -> None:
        # AC6/design constraint: only `daily` is ever touched.
        expired_weekly = {"bucket": "weekly", "valid_until": "2020-01-01"}
        assert _is_deprioritized_for_currency(expired_weekly) is False

    def test_expired_durable_page_not_deprioritized(self) -> None:
        expired_durable = {"bucket": "durable", "valid_until": "2020-01-01"}
        assert _is_deprioritized_for_currency(expired_durable) is False

    def test_expired_unbucketed_page_not_deprioritized(self) -> None:
        expired_unbucketed = {"valid_until": "2020-01-01"}
        assert _is_deprioritized_for_currency(expired_unbucketed) is False


class TestReorderHitsByCurrency:
    def _wiki(self, tmp_path: Path, *, bucket: str | None, valid_until: str | None) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        return wiki

    def _write(self, wiki: Path, name: str, *, bucket: str | None, valid_until: str | None) -> None:
        lines = ["---", f"name: {name}"]
        if bucket:
            lines.append(f"bucket: {bucket}")
        if valid_until:
            lines.append(f"valid_until: '{valid_until}'")
        lines += ["---", "", f"Body for {name}."]
        (wiki / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")

    def test_reorders_expired_daily_to_the_end(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._write(wiki, "stale", bucket="daily", valid_until="2020-01-01")
        self._write(wiki, "fresh", bucket=None, valid_until=None)
        hits = [("stale.md", "stale", 5.0), ("fresh.md", "fresh", 1.0)]
        reordered = _reorder_hits_by_currency(hits, wiki_root=wiki, extra_roots=[])
        assert [h[0] for h in reordered] == ["fresh.md", "stale.md"]

    def test_no_bucket_anywhere_is_a_no_op(self, tmp_path: Path) -> None:
        # Compatibility constraint: a corpus with no buckets anywhere must
        # be completely unaffected by the reorder.
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._write(wiki, "a", bucket=None, valid_until=None)
        self._write(wiki, "b", bucket=None, valid_until=None)
        hits = [("a.md", "a", 5.0), ("b.md", "b", 1.0)]
        reordered = _reorder_hits_by_currency(hits, wiki_root=wiki, extra_roots=[])
        assert reordered == hits


class TestRecallCurrencyIntegration:
    def _wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        return wiki

    def test_expired_daily_page_ranks_below_current_one(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        (wiki / "status_stale.md").write_text(
            "---\nname: Status stale\nbucket: daily\n"
            "valid_until: '2020-01-01'\n---\n\n"
            "widget pipeline status widget pipeline widget pipeline widget\n"
        )
        (wiki / "status_fresh.md").write_text(
            "---\nname: Status fresh\n---\n\nwidget pipeline\n"
        )
        result = recall_search(wiki, "widget pipeline", top_k=2)
        fresh_pos = result.index("Status fresh")
        stale_pos = result.index("Status stale")
        assert fresh_pos < stale_pos

    def test_history_flag_disables_currency_reorder(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        (wiki / "status_stale.md").write_text(
            "---\nname: Status stale\nbucket: daily\n"
            "valid_until: '2020-01-01'\n---\n\n"
            "widget pipeline status widget pipeline widget pipeline widget widget widget\n"
        )
        (wiki / "status_fresh.md").write_text(
            "---\nname: Status fresh\n---\n\nwidget\n"
        )
        # With the higher-relevance page being the stale one (more keyword
        # hits), history=True must return it FIRST -- currency ranking must
        # not apply when the caller explicitly asked for history.
        result = recall_search(wiki, "widget pipeline", top_k=2, history=True)
        stale_pos = result.index("Status stale")
        fresh_pos = result.index("Status fresh")
        assert stale_pos < fresh_pos
