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

Issue athenaeum#1840 (transient decay 2 of 3) extends the same file with its
own AC-keyed classes at the bottom, following the organisation above:
deriving a page-level ``valid_until`` from a non-durable ``bucket``, and
replacing athenaeum#904's first-non-empty-member-wins cluster fold with
most-durable-wins.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from athenaeum.config import resolve_decay_horizon_days
from athenaeum.intake import discover_auto_memory_files
from athenaeum.mcp_server import (
    _is_deprioritized_for_currency,
    _reorder_hits_by_currency,
    recall_search,
    remember_write,
)
from athenaeum.merge import (
    _decay_anchor_date,
    _derive_page_valid_until,
    merge_cluster_row,
    render_merged_entry,
)
from athenaeum.models import (
    MEMORY_BUCKETS,
    AutoMemoryFile,
    coerce_bucket,
    parse_bucket,
    parse_frontmatter,
    valid_until_expired,
    validity_bound_str,
)
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

    def test_most_durable_active_member_wins_on_disagreement(
        self, tmp_path: Path
    ) -> None:
        """Issue athenaeum#1840 replaced athenaeum#904's first-non-empty-member-
        wins fold with most-durable-wins. ``weekly`` beats ``daily`` under
        BOTH rules when the weekly member happens to come first, so the
        order-reversed case below is what actually distinguishes them (the
        full counter-example battery is in ``TestMostDurableWinsFold``).
        """
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

        reversed_row = dict(row, member_paths=[str(m2), str(m1)])
        reversed_entry = merge_cluster_row(
            reversed_row, extra_roots=[tmp_path], am_by_path={}
        )
        assert reversed_entry is not None
        assert reversed_entry.bucket == "weekly"


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


# ---------------------------------------------------------------------------
# Issue athenaeum#1840: page-level `valid_until` DERIVED from `bucket`, and a
# most-durable-wins cluster fold. Keyed to that issue's acceptance criteria.
# ---------------------------------------------------------------------------


def _write_member(
    root: Path,
    filename: str,
    *,
    bucket: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    sources: str = "",
    body: str = "A claim.",
) -> Path:
    """Write one raw member file under *root* and return its path.

    Every path is derived from the caller's ``tmp_path`` — never an absolute
    literal — so the fixtures stay portable (and pass ``public-safe-lint.sh``).
    """
    lines = ["---", "name: " + filename.split(".")[0], "type: feedback"]
    if bucket is not None:
        lines.append(f"bucket: {bucket}")
    if valid_from is not None:
        lines.append(f"valid_from: {valid_from}")
    if valid_until is not None:
        lines.append(f"valid_until: {valid_until}")
    if sources:
        lines.append(sources.rstrip("\n"))
    lines += ["---", "", body, ""]
    path = root / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _row(cluster_id: str, *paths: Path) -> dict[str, object]:
    return {
        "cluster_id": cluster_id,
        "member_paths": [str(p) for p in paths],
        "centroid_score": 1.0,
    }


class TestDecayHorizonResolution:
    """AC1: ``resolve_decay_horizon_days(bucket, config)`` — env > yaml >
    default, ``daily`` 1 and ``weekly`` 7.

    The full precedence battery (both channels, both families, the
    malformed-value fallback, and the no-horizon branch every other bucket
    takes) lives in
    ``tests/test_config_resolver_parity_generic.py::TestDecayHorizonResolverDirect``,
    which is where this repo covers a resolver the generic parity prober
    cannot call. Asserted here too because it is this issue's AC1 and this
    file is the AC-keyed home for the slice.
    """

    def test_daily_and_weekly_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_DECAY_DAILY_HORIZON_DAYS", raising=False)
        monkeypatch.delenv("ATHENAEUM_DECAY_WEEKLY_HORIZON_DAYS", raising=False)
        assert resolve_decay_horizon_days("daily", None) == 1
        assert resolve_decay_horizon_days("weekly", None) == 7

    def test_precedence_env_over_yaml_over_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_DECAY_DAILY_HORIZON_DAYS", raising=False)
        cfg = {"decay": {"daily_horizon_days": 3}}
        assert resolve_decay_horizon_days("daily", cfg) == 3
        monkeypatch.setenv("ATHENAEUM_DECAY_DAILY_HORIZON_DAYS", "5")
        assert resolve_decay_horizon_days("daily", cfg) == 5

    def test_durable_has_no_horizon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_DECAY_DAILY_HORIZON_DAYS", "99")
        cfg = {"decay": {"daily_horizon_days": 99, "weekly_horizon_days": 99}}
        assert resolve_decay_horizon_days("durable", cfg) == 0
        assert resolve_decay_horizon_days("", cfg) == 0


class TestDecayAnchorPrecedence:
    """AC2: anchor precedence ``valid_from`` -> raw-filename ``RAW_FILE_RE``
    stamp -> ``date.today()``, and the horizon is measured from THAT date."""

    def test_counter_example_old_file_does_not_get_today_plus_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file dated 2026-05-10, classified ``daily``, expires 2026-05-11
        — not tomorrow.

        This is the whole point of anchoring on the memory's OWN date: a
        today-anchored horizon would renew every transient page on every
        nightly compile and it could never expire.
        """
        monkeypatch.delenv("ATHENAEUM_DECAY_DAILY_HORIZON_DAYS", raising=False)
        member = _write_member(tmp_path, "20260510T120000Z-deadbeef.md", bucket="daily")
        entry = merge_cluster_row(
            _row("c-1840-a", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None

        anchor = date(2026, 5, 10)
        horizon = resolve_decay_horizon_days("daily", None)
        # Asserted as anchor + resolved_horizon, not as a hardcoded literal:
        # an operator who widens the daily horizon must not silently break
        # this test into asserting the wrong property.
        assert entry.valid_until == (anchor + timedelta(days=horizon)).isoformat()
        # ...and, concretely, that is 2026-05-11 under the shipped default.
        assert entry.valid_until == "2026-05-11"
        # The counter-example the criterion names: NOT today+1.
        assert entry.valid_until != (date.today() + timedelta(days=1)).isoformat()

    def test_daily_default_horizon_is_at_most_two_days(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Separate from the anchor assertion above, per AC2: a "daily"
        bucket whose default horizon drifted to a week would make the
        anchor test above pass against a nonsense window."""
        monkeypatch.delenv("ATHENAEUM_DECAY_DAILY_HORIZON_DAYS", raising=False)
        assert 1 <= resolve_decay_horizon_days("daily", None) <= 2

    def test_valid_from_outranks_the_filename_stamp(self, tmp_path: Path) -> None:
        member = _write_member(
            tmp_path,
            "20260510T120000Z-deadbeef.md",
            bucket="daily",
            valid_from="2026-07-01",
        )
        entry = merge_cluster_row(
            _row("c-1840-b", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        assert entry.valid_until == "2026-07-02"

    def test_falls_back_to_today_when_no_valid_from_and_no_stamp(
        self, tmp_path: Path
    ) -> None:
        member = _write_member(tmp_path, "project_current_focus.md", bucket="daily")
        entry = merge_cluster_row(
            _row("c-1840-c", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        assert entry.valid_until == (date.today() + timedelta(days=1)).isoformat()

    def test_as_of_rewinds_the_today_fallback(self, tmp_path: Path) -> None:
        """``as_of`` (compile-as-of) is the "today" the fallback anchor uses,
        so a rewound compile derives the window it WOULD have derived then."""
        member = _write_member(tmp_path, "project_current_focus.md", bucket="daily")
        entry = merge_cluster_row(
            _row("c-1840-d", member),
            extra_roots=[tmp_path],
            am_by_path={},
            as_of=date(2026, 3, 1),
        )
        assert entry is not None
        assert entry.valid_until == "2026-03-02"

    def test_anchor_helper_tolerates_a_malformed_valid_from(
        self, tmp_path: Path
    ) -> None:
        """``AutoMemoryFile.valid_from`` is normalized to ``YYYY-MM-DD`` (or
        ``""``) at construction by ``validity_bound_str``, so the compile
        path never hands the anchor helper a malformed bound — but the
        dataclass is plain and a caller can build one directly, and an
        anchor helper that RAISED would take down a whole nightly compile
        over one bad field. Exercised at the helper, where the defect would
        actually live.
        """
        am = AutoMemoryFile(
            path=tmp_path / "20260510T120000Z-deadbeef.md",
            origin_scope=tmp_path.name,
            memory_type="feedback",
            bucket="daily",
            valid_from="2026-13-99",
        )
        assert _decay_anchor_date(am) == date(2026, 5, 10)
        assert _derive_page_valid_until(am) == "2026-05-11"

    def test_impossible_calendar_stamp_falls_through_to_today(
        self, tmp_path: Path
    ) -> None:
        """``RAW_FILE_RE`` pins eight DIGITS, not a valid calendar date, so
        a hand-renamed file can match the pattern and still not be a date."""
        member = _write_member(tmp_path, "20261399T120000Z-deadbeef.md", bucket="daily")
        entry = merge_cluster_row(
            _row("c-1840-e2", member),
            extra_roots=[tmp_path],
            am_by_path={},
            as_of=date(2026, 3, 1),
        )
        assert entry is not None
        assert entry.valid_until == "2026-03-02"

    def test_malformed_valid_from_falls_through_to_the_stamp(
        self, tmp_path: Path
    ) -> None:
        member = _write_member(
            tmp_path,
            "20260510T120000Z-deadbeef.md",
            bucket="daily",
            valid_from="not-a-date",
        )
        entry = merge_cluster_row(
            _row("c-1840-e", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        assert entry.valid_until == "2026-05-11"

    def test_operator_horizon_override_moves_the_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_DECAY_WEEKLY_HORIZON_DAYS", "10")
        member = _write_member(tmp_path, "20260510T120000Z-deadbeef.md", bucket="weekly")
        entry = merge_cluster_row(
            _row("c-1840-f", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        assert entry.valid_until == "2026-05-20"

    def test_config_yaml_horizon_threads_through_merge_cluster_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_DECAY_DAILY_HORIZON_DAYS", raising=False)
        member = _write_member(tmp_path, "20260510T120000Z-deadbeef.md", bucket="daily")
        entry = merge_cluster_row(
            _row("c-1840-g", member),
            extra_roots=[tmp_path],
            am_by_path={},
            config={"decay": {"daily_horizon_days": 4}},
        )
        assert entry is not None
        assert entry.valid_until == "2026-05-14"


class TestDurableWritesNoValidUntil:
    """AC3: ``bucket: durable`` writes no ``valid_until`` key at ANY layer."""

    def test_durable_page_has_no_valid_until_anywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_DECAY_DAILY_HORIZON_DAYS", "99")
        monkeypatch.setenv("ATHENAEUM_DECAY_WEEKLY_HORIZON_DAYS", "99")
        member = _write_member(
            tmp_path, "20260510T120000Z-deadbeef.md", bucket="durable"
        )
        entry = merge_cluster_row(
            _row("c-1840-h", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        assert entry.bucket == "durable"
        assert entry.valid_until == ""
        rendered = render_merged_entry(entry)
        assert "bucket: durable" in rendered
        # "at any layer": not in frontmatter, not on a compiled source
        # record, not in a rendered footnote.
        assert "valid_until" not in rendered
        assert all("valid_until" not in src for src in entry.sources)

    def test_unbucketed_page_has_no_valid_until(self, tmp_path: Path) -> None:
        member = _write_member(tmp_path, "20260510T120000Z-deadbeef.md")
        entry = merge_cluster_row(
            _row("c-1840-i", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        assert entry.bucket == ""
        assert entry.valid_until == ""
        assert "valid_until" not in render_merged_entry(entry)


class TestDeclaredValidUntilIsNeverOverridden:
    """AC4 counter-example: a source already carrying ``valid_until:`` is
    left byte-identical — the derivation FILLS an absent bound and never
    rewrites a declared one, at either layer."""

    _SOURCES = (
        "sources:\n"
        "  - session: 11111111-2222-3333-4444-555555555555\n"
        "    turn: 3\n"
        "    source_type: user-stated\n"
        "    source_ref: 11111111-2222-3333-4444-555555555555#3\n"
        "    valid_until: '2027-06-30'\n"
    )

    def test_member_declared_bound_wins_over_the_derived_one(
        self, tmp_path: Path
    ) -> None:
        member = _write_member(
            tmp_path,
            "20260510T120000Z-deadbeef.md",
            bucket="daily",
            valid_until="2026-12-31",
        )
        entry = merge_cluster_row(
            _row("c-1840-j", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        assert entry.valid_until == "2026-12-31"
        assert entry.valid_until != "2026-05-11"

    def test_per_source_bound_is_byte_identical_with_and_without_a_bucket(
        self, tmp_path: Path
    ) -> None:
        """The derivation touches only the PAGE layer: compiling the same
        member with and without ``bucket:`` yields identical ``sources``."""
        # Same directory and same date stamp for both, so the ONLY
        # difference between the two compiles is the ``bucket:`` line (a
        # differing parent directory would also shift each source's
        # ``origin_scope``).
        bucketed = _write_member(
            tmp_path,
            "20260510T120000Z-deadbeef.md",
            bucket="daily",
            sources=self._SOURCES,
        )
        plain = _write_member(
            tmp_path, "20260510T120000Z-deadbeee.md", sources=self._SOURCES
        )

        with_bucket = merge_cluster_row(
            _row("c-1840-k", bucketed), extra_roots=[tmp_path], am_by_path={}
        )
        without_bucket = merge_cluster_row(
            _row("c-1840-k", plain), extra_roots=[tmp_path], am_by_path={}
        )
        assert with_bucket is not None and without_bucket is not None
        assert with_bucket.sources == without_bucket.sources
        assert with_bucket.sources[0]["valid_until"] == "2027-06-30"
        # The page-level key is the ONLY thing the bucket added.
        assert with_bucket.valid_until == "2026-05-11"
        assert without_bucket.valid_until == ""


class TestPageValidUntilRendering:
    """AC5: ``MergedWikiEntry.valid_until`` renders next to ``bucket``, and
    is omitted entirely at its default."""

    def test_rendered_next_to_bucket(self, tmp_path: Path) -> None:
        member = _write_member(tmp_path, "20260510T120000Z-deadbeef.md", bucket="daily")
        entry = merge_cluster_row(
            _row("c-1840-l", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        rendered = render_merged_entry(entry)
        meta, _body = parse_frontmatter(rendered)
        assert parse_bucket(meta) == "daily"
        assert validity_bound_str(meta, "valid_until") == "2026-05-11"
        keys = list(meta)
        assert keys.index("valid_until") == keys.index("bucket") + 1

    def test_round_trips_through_the_shared_expiry_predicate(
        self, tmp_path: Path
    ) -> None:
        """Not a parallel validity concept: the rendered key is the one
        ``models.valid_until_expired`` has always read."""
        member = _write_member(tmp_path, "20260510T120000Z-deadbeef.md", bucket="daily")
        entry = merge_cluster_row(
            _row("c-1840-m", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        meta, _body = parse_frontmatter(render_merged_entry(entry))
        assert valid_until_expired(meta, date(2026, 5, 12)) is True
        assert valid_until_expired(meta, date(2026, 5, 11)) is False

    def test_omitted_at_default(self, tmp_path: Path) -> None:
        member = _write_member(tmp_path, "project_notes.md")
        entry = merge_cluster_row(
            _row("c-1840-n", member), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        meta, _body = parse_frontmatter(render_merged_entry(entry))
        assert "valid_until" not in meta
        assert "bucket" not in meta


class TestMostDurableWinsFold:
    """AC6 counter-example folds: ``{daily, durable}`` -> ``durable`` with no
    ``valid_until``; ``{daily, weekly}`` -> ``weekly`` plus a 7-day horizon;
    ``{daily, daily}`` -> ``daily``."""

    def _fold(
        self, tmp_path: Path, buckets: list[str], cluster_id: str
    ) -> tuple[str, str]:
        paths = [
            _write_member(
                tmp_path, f"2026051{i}T120000Z-deadbee{i}.md", bucket=b
            )
            for i, b in enumerate(buckets)
        ]
        entry = merge_cluster_row(
            _row(cluster_id, *paths), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        return entry.bucket, entry.valid_until

    def test_daily_then_durable_folds_to_durable(self, tmp_path: Path) -> None:
        bucket, valid_until = self._fold(tmp_path, ["daily", "durable"], "c-1840-o")
        assert bucket == "durable"
        assert valid_until == ""

    def test_durable_then_daily_folds_to_durable(self, tmp_path: Path) -> None:
        """Order-independent — the athenaeum#904 fold returned whichever
        bucket happened to come first in the cluster row."""
        bucket, valid_until = self._fold(tmp_path, ["durable", "daily"], "c-1840-p")
        assert bucket == "durable"
        assert valid_until == ""

    def test_daily_then_weekly_folds_to_weekly_with_a_seven_day_horizon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_DECAY_WEEKLY_HORIZON_DAYS", raising=False)
        bucket, valid_until = self._fold(tmp_path, ["daily", "weekly"], "c-1840-q")
        assert bucket == "weekly"
        # The weekly member is the second one, dated 2026-05-11; the horizon
        # is anchored on the SAME member that won the fold, so bucket and
        # horizon can never come from two different members.
        assert valid_until == "2026-05-18"
        assert (
            date.fromisoformat(valid_until) - date(2026, 5, 11)
        ).days == resolve_decay_horizon_days("weekly", None) == 7

    def test_daily_and_daily_folds_to_daily(self, tmp_path: Path) -> None:
        bucket, valid_until = self._fold(tmp_path, ["daily", "daily"], "c-1840-r")
        assert bucket == "daily"
        # Ties keep the FIRST member at the winning level (2026-05-10).
        assert valid_until == "2026-05-11"

    def test_unbucketed_members_are_skipped_not_treated_as_durable(
        self, tmp_path: Path
    ) -> None:
        m1 = _write_member(tmp_path, "project_no_bucket.md")
        m2 = _write_member(tmp_path, "20260510T120000Z-deadbeef.md", bucket="daily")
        entry = merge_cluster_row(
            _row("c-1840-s", m1, m2), extra_roots=[tmp_path], am_by_path={}
        )
        assert entry is not None
        assert entry.bucket == "daily"
        assert entry.valid_until == "2026-05-11"


class TestDownstreamSwitchOn:
    """AC7: an expired compiled page reaches the deterministic sweep's kill
    list AND is deprioritized by recall's currency ranking — the two
    downstream consumers this slice exists to switch on.

    Both already keyed on ``models.valid_until_expired``; before this issue
    a compiled page carried a ``bucket`` but never a page-level
    ``valid_until``, so neither could ever fire on one.
    """

    def _compile_into_wiki(
        self, tmp_path: Path, *, bucket: str, filename: str
    ) -> tuple[Path, Path]:
        members_dir = tmp_path / "members"
        wiki_root = tmp_path / "wiki"
        members_dir.mkdir(exist_ok=True)
        wiki_root.mkdir(exist_ok=True)
        member = _write_member(members_dir, filename, bucket=bucket)
        entry = merge_cluster_row(
            _row(f"c-{filename}", member), extra_roots=[members_dir], am_by_path={}
        )
        assert entry is not None
        page = wiki_root / entry.filename
        page.write_text(render_merged_entry(entry), encoding="utf-8")
        return wiki_root, page

    def test_expired_compiled_page_is_on_the_sweep_kill_list(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.decay_sweep import build_sweep_report

        wiki_root, page = self._compile_into_wiki(
            tmp_path, bucket="daily", filename="20260510T120000Z-deadbeef.md"
        )
        report = build_sweep_report(wiki_root, as_of=date(2026, 6, 1))
        assert report.scanned == 1
        assert [c.path for c in report.kill] == [page]
        assert report.kill[0].valid_until == "2026-05-11"
        assert report.retained == []

    def test_unexpired_compiled_page_is_retained(self, tmp_path: Path) -> None:
        from athenaeum.decay_sweep import build_sweep_report

        wiki_root, page = self._compile_into_wiki(
            tmp_path, bucket="daily", filename="20260510T120000Z-deadbeef.md"
        )
        report = build_sweep_report(wiki_root, as_of=date(2026, 5, 11))
        assert report.kill == []
        assert [p for p, _reason in report.retained] == [page]

    def test_durable_compiled_page_is_never_even_scanned(
        self, tmp_path: Path
    ) -> None:
        """Out of scope, asserted: this issue does not widen the sweep's
        daily-only retirement scope."""
        from athenaeum.decay_sweep import build_sweep_report

        wiki_root, _page = self._compile_into_wiki(
            tmp_path, bucket="durable", filename="20260510T120000Z-deadbeef.md"
        )
        report = build_sweep_report(wiki_root, as_of=date(2027, 1, 1))
        assert report.scanned == 0
        assert report.kill == []

    def test_weekly_compiled_page_is_not_swept(self, tmp_path: Path) -> None:
        from athenaeum.decay_sweep import build_sweep_report

        wiki_root, _page = self._compile_into_wiki(
            tmp_path, bucket="weekly", filename="20260510T120000Z-deadbeef.md"
        )
        report = build_sweep_report(wiki_root, as_of=date(2027, 1, 1))
        assert report.scanned == 0

    def test_expired_compiled_page_is_deprioritized_for_currency(
        self, tmp_path: Path
    ) -> None:
        _wiki_root, page = self._compile_into_wiki(
            tmp_path, bucket="daily", filename="20260510T120000Z-deadbeef.md"
        )
        meta, _body = parse_frontmatter(page.read_text(encoding="utf-8"))
        assert _is_deprioritized_for_currency(meta, as_of=date(2026, 6, 1)) is True
        assert _is_deprioritized_for_currency(meta, as_of=date(2026, 5, 11)) is False

    def test_durable_compiled_page_is_never_deprioritized(
        self, tmp_path: Path
    ) -> None:
        _wiki_root, page = self._compile_into_wiki(
            tmp_path, bucket="durable", filename="20260510T120000Z-deadbeef.md"
        )
        meta, _body = parse_frontmatter(page.read_text(encoding="utf-8"))
        assert _is_deprioritized_for_currency(meta, as_of=date(2030, 1, 1)) is False
