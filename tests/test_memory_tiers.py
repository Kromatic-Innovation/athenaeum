# SPDX-License-Identifier: Apache-2.0
"""Tests for retrieval-cost memory tiers + push budget/ranking (issue athenaeum#718).

Covers: tier resolution (explicit pin / class default / cold via
`storage.is_embedded` / fallback), coordinate-fit scope comparison, the
push-selection formula and its token-budget boundary enforcement, automatic
hot<->warm tier movement (class-default/age/precision triggers, the axiom
refusal, promote-on-use), frontmatter text-surgery round-tripping, the
`run_tier_sweep` end-to-end scan+write+ledger path, and the three new
`athenaeum.config` resolvers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from athenaeum import memory_tiers
from athenaeum.authority import AuthorityManifest, AuthoritySource
from athenaeum.axiom_governance import read_axiom_ledger
from athenaeum.config import (
    resolve_memory_tier_demote_after_days,
    resolve_memory_tier_sweep_enabled,
    resolve_push_token_budget,
)
from athenaeum.push_metrics import (
    ReferenceResult,
    build_push_record,
    record_push,
    record_reference_result,
)


def _page_text(
    *,
    uid: str = "abc123",
    name: str = "Test Page",
    page_type: str = "concept",
    memory_class: str | None = None,
    memory_tier: str | None = None,
    updated: str | None = None,
    superseded_by: str | None = None,
    deprecated: bool | None = None,
    claimed_scope: str | None = None,
) -> str:
    lines = ["---", f"uid: {uid}", f"name: {name}", f"type: {page_type}"]
    if memory_class is not None:
        lines.append(f"memory_class: {memory_class}")
    if memory_tier is not None:
        lines.append(f"memory_tier: {memory_tier}")
    if updated is not None:
        lines.append(f"updated: {updated}")
    if superseded_by is not None:
        lines.append(f"superseded_by: {superseded_by}")
    if deprecated is not None:
        lines.append(f"deprecated: {str(deprecated).lower()}")
    if claimed_scope is not None:
        lines.append(f"claimed_scope: {claimed_scope}")
    lines.append("---")
    lines.append("")
    lines.append("Body text.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# resolve_tier
# ---------------------------------------------------------------------------


class TestResolveTier:
    def test_explicit_pin_hot(self) -> None:
        assert memory_tiers.resolve_tier({"memory_tier": "hot"}) == "hot"

    def test_explicit_pin_warm(self) -> None:
        assert memory_tiers.resolve_tier({"memory_tier": "warm"}) == "warm"

    def test_explicit_invalid_value_falls_through_to_class_default(self) -> None:
        # "cold"/"refused" (and any garbage) are not settable per-page --
        # SETTABLE_TIERS is {hot, warm} only.
        fm = {"memory_tier": "cold", "memory_class": "guideline"}
        assert memory_tiers.resolve_tier(fm) == "hot"

    @pytest.mark.parametrize(
        "memory_class,expected",
        [
            ("axiom", "hot"),
            ("guideline", "hot"),
            ("decision", "hot"),
            ("fact", "warm"),
            ("reference", "warm"),
            ("entity", "warm"),
            ("procedure", "warm"),
        ],
    )
    def test_class_defaults(self, memory_class: str, expected: str) -> None:
        assert memory_tiers.resolve_tier({"memory_class": memory_class}) == expected

    def test_unrecognized_class_falls_back_to_warm(self) -> None:
        assert memory_tiers.resolve_tier({"memory_class": "not-a-real-class"}) == "warm"

    def test_derives_class_from_type_when_memory_class_absent(self) -> None:
        # person -> entity -> warm (memory_class.TYPE_TO_MEMORY_CLASS)
        assert memory_tiers.resolve_tier({"type": "person"}) == "warm"
        # principle -> guideline -> hot
        assert memory_tiers.resolve_tier({"type": "principle"}) == "hot"

    def test_empty_fm_never_raises(self) -> None:
        assert memory_tiers.resolve_tier({}) == "warm"
        assert memory_tiers.resolve_tier(None) == "warm"

    def test_cold_when_type_not_embedded(self) -> None:
        config = {"storage": {"mapping": {"pii": "excluded"}}}
        fm = {"type": "pii", "memory_class": "axiom", "memory_tier": "hot"}
        # Cold wins even over an explicit hot pin -- class+config authoritative.
        assert memory_tiers.resolve_tier(fm, config=config) == "cold"

    def test_default_config_never_cold(self) -> None:
        assert memory_tiers.resolve_tier({"type": "anything"}, config=None) != "cold"


# ---------------------------------------------------------------------------
# scope_relation / tier_scope_header_line
# ---------------------------------------------------------------------------


class TestScopeRelation:
    def test_none_session_scope_returns_none(self) -> None:
        assert memory_tiers.scope_relation({"claimed_scope": "org/team"}, None) is None

    def test_none_page_scope_returns_none(self) -> None:
        assert memory_tiers.scope_relation({}, "org/team") is None

    def test_equal(self) -> None:
        assert memory_tiers.scope_relation({"claimed_scope": "org/team"}, "org/team") == "equal"

    def test_contains(self) -> None:
        # page scope is an ancestor of the session scope.
        assert (
            memory_tiers.scope_relation({"claimed_scope": "org"}, "org/team/sub") == "contains"
        )

    def test_disjoint(self) -> None:
        assert (
            memory_tiers.scope_relation({"claimed_scope": "org/team-a"}, "org/team-b")
            == "disjoint"
        )


class TestTierScopeHeaderLine:
    def test_no_relation(self) -> None:
        assert memory_tiers.tier_scope_header_line("hot", None) == "**Tier:** hot"

    def test_with_relation(self) -> None:
        line = memory_tiers.tier_scope_header_line("warm", "contains")
        assert line == "**Tier:** warm · **Scope:** contains"


# ---------------------------------------------------------------------------
# is_refused
# ---------------------------------------------------------------------------


class TestIsRefused:
    def _manifest(
        self, *, classes: tuple[str, ...] = ("mirror-of-live-source",)
    ) -> AuthorityManifest:
        return AuthorityManifest(
            version=1,
            sources=(
                AuthoritySource(
                    slug="css-typeface-source",
                    location="assets/styles/cover.css",
                    topics=("webfont-embedding",),
                    kind="config",
                ),
            ),
            never_ingest_classes=classes,
        )

    def test_matching_topic_is_refused(self) -> None:
        meta = {"name": "Montserrat cover font", "topics": ["webfont-embedding"]}
        assert memory_tiers.is_refused(meta, "body", manifest=self._manifest()) is True

    def test_no_match_is_not_refused(self) -> None:
        meta = {"name": "unrelated page"}
        assert memory_tiers.is_refused(meta, "body", manifest=self._manifest()) is False

    def test_dark_by_default_when_no_classes_declared(self) -> None:
        meta = {"name": "Montserrat cover font", "topics": ["webfont-embedding"]}
        manifest = self._manifest(classes=())
        assert memory_tiers.is_refused(meta, "body", manifest=manifest) is False


# ---------------------------------------------------------------------------
# push_score / tier_weight / coordinate_fit_weight
# ---------------------------------------------------------------------------


class TestPushScore:
    def test_hot_full_weight(self) -> None:
        assert memory_tiers.push_score(2.0, "hot", None) == 2.0

    @pytest.mark.parametrize("tier", ["warm", "cold", "refused", "garbage"])
    def test_non_hot_is_zero(self, tier: str) -> None:
        assert memory_tiers.push_score(100.0, tier, "contains") == 0.0

    def test_contains_outranks_disjoint(self) -> None:
        contains_score = memory_tiers.push_score(1.0, "hot", "contains")
        disjoint_score = memory_tiers.push_score(1.0, "hot", "disjoint")
        assert contains_score > disjoint_score

    def test_none_relation_is_neutral(self) -> None:
        assert memory_tiers.push_score(1.0, "hot", None) == memory_tiers.push_score(
            1.0, "hot", "equal"
        )


# ---------------------------------------------------------------------------
# select_for_push -- the token-budget boundary
# ---------------------------------------------------------------------------


def _candidate(
    key: Any,
    *,
    relevance: float,
    tier: str,
    scope_relation: str | None = None,
    tokens: int,
) -> memory_tiers.PushCandidate:
    return memory_tiers.PushCandidate(
        key=key, relevance=relevance, tier=tier, scope_relation=scope_relation, tokens=tokens
    )


class TestSelectForPush:
    def test_only_hot_tier_selected(self) -> None:
        candidates = [
            _candidate("a", relevance=10.0, tier="warm", tokens=1),
            _candidate("b", relevance=1.0, tier="hot", tokens=1),
        ]
        selected = memory_tiers.select_for_push(candidates, token_budget=1000)
        assert selected == ["b"]

    def test_ranked_by_push_score_descending(self) -> None:
        candidates = [
            _candidate("low", relevance=1.0, tier="hot", scope_relation="disjoint", tokens=1),
            _candidate("high", relevance=1.0, tier="hot", scope_relation="contains", tokens=1),
        ]
        selected = memory_tiers.select_for_push(candidates, token_budget=1000)
        assert selected == ["high", "low"]

    def test_budget_boundary_excludes_the_item_that_would_exceed_it(self) -> None:
        # Budget exactly fits candidate "a" (100 tokens) alone; adding "b"
        # (50 more) would push the total to 150 > 100, so "b" is excluded --
        # this is the AC's "enforced, tested at the boundary" case.
        candidates = [
            _candidate("a", relevance=2.0, tier="hot", tokens=100),
            _candidate("b", relevance=1.0, tier="hot", tokens=50),
        ]
        selected = memory_tiers.select_for_push(candidates, token_budget=100)
        assert selected == ["a"]

    def test_budget_packs_a_smaller_later_candidate(self) -> None:
        # "big" alone would exceed budget and is skipped (not truncated);
        # the smaller "small" candidate, ranked below it, still fits and is
        # included -- packing, not a hard cutoff at the first miss.
        candidates = [
            _candidate("big", relevance=3.0, tier="hot", tokens=90),
            _candidate("small", relevance=2.0, tier="hot", tokens=10),
        ]
        selected = memory_tiers.select_for_push(candidates, token_budget=50)
        assert selected == ["small"]

    def test_exactly_at_budget_is_included(self) -> None:
        candidates = [_candidate("a", relevance=1.0, tier="hot", tokens=100)]
        assert memory_tiers.select_for_push(candidates, token_budget=100) == ["a"]

    def test_one_over_budget_is_excluded(self) -> None:
        candidates = [_candidate("a", relevance=1.0, tier="hot", tokens=101)]
        assert memory_tiers.select_for_push(candidates, token_budget=100) == []

    def test_empty_candidates(self) -> None:
        assert memory_tiers.select_for_push([], token_budget=100) == []


# ---------------------------------------------------------------------------
# evaluate_tier_movement
# ---------------------------------------------------------------------------


class TestEvaluateTierMovement:
    _NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)

    def test_axiom_never_moves_even_with_every_trigger_present(self) -> None:
        fm = {
            "memory_class": "axiom",
            "superseded_by": "winner",
            "updated": "2020-01-01T00:00:00Z",
        }
        result = memory_tiers.evaluate_tier_movement(
            fm, usage=None, now=self._NOW, demote_after_days=60
        )
        assert result == (None, None)

    def test_demote_by_superseded(self) -> None:
        fm = {"memory_class": "guideline", "superseded_by": "winner-slug"}
        new_tier, reason = memory_tiers.evaluate_tier_movement(
            fm, usage=None, now=self._NOW, demote_after_days=60
        )
        assert new_tier == "warm"
        assert reason is not None and "superseded" in reason

    def test_demote_by_deprecated(self) -> None:
        fm = {"memory_class": "decision", "deprecated": True}
        new_tier, reason = memory_tiers.evaluate_tier_movement(
            fm, usage=None, now=self._NOW, demote_after_days=60
        )
        assert new_tier == "warm"
        assert reason is not None and "deprecated" in reason

    def test_demote_by_age_without_use(self) -> None:
        fm = {"memory_class": "guideline", "updated": "2020-01-01T00:00:00Z"}
        new_tier, reason = memory_tiers.evaluate_tier_movement(
            fm, usage=None, now=self._NOW, demote_after_days=60
        )
        assert new_tier == "warm"
        assert reason == "age-without-use"

    def test_no_demote_when_age_under_threshold(self) -> None:
        recent = (self._NOW - timedelta(days=5)).isoformat().replace("+00:00", "Z")
        fm = {"memory_class": "guideline", "updated": recent}
        result = memory_tiers.evaluate_tier_movement(
            fm, usage=None, now=self._NOW, demote_after_days=60
        )
        assert result == (None, None)

    def test_no_demote_when_no_updated_timestamp_at_all(self) -> None:
        # Unknown age -- must not demote on absence of evidence.
        fm = {"memory_class": "guideline"}
        result = memory_tiers.evaluate_tier_movement(
            fm, usage=None, now=self._NOW, demote_after_days=60
        )
        assert result == (None, None)

    def test_demote_by_pushed_but_never_referenced(self) -> None:
        from athenaeum.usage_report import ClaimUsage

        usage = ClaimUsage(
            id="x",
            pushed_count=5,
            referenced_count=0,
            last_pushed="2020-01-01T00:00:00Z",
            last_referenced=None,
        )
        fm = {"memory_class": "guideline", "updated": "2026-08-20T00:00:00Z"}
        new_tier, reason = memory_tiers.evaluate_tier_movement(
            fm, usage=usage, now=self._NOW, demote_after_days=60
        )
        assert new_tier == "warm"
        assert reason == "pushed-but-never-used"

    def test_no_demote_when_pushed_and_referenced(self) -> None:
        from athenaeum.usage_report import ClaimUsage

        usage = ClaimUsage(
            id="x",
            pushed_count=5,
            referenced_count=2,
            last_pushed="2020-01-01T00:00:00Z",
            last_referenced="2020-06-01T00:00:00Z",
        )
        fm = {"memory_class": "guideline", "updated": "2026-08-20T00:00:00Z"}
        result = memory_tiers.evaluate_tier_movement(
            fm, usage=usage, now=self._NOW, demote_after_days=60
        )
        assert result == (None, None)

    def test_promote_warm_on_use(self) -> None:
        from athenaeum.usage_report import ClaimUsage

        usage = ClaimUsage(
            id="x", pushed_count=1, referenced_count=1, last_pushed="x", last_referenced="x"
        )
        fm = {"memory_class": "fact"}  # warm by class default
        new_tier, reason = memory_tiers.evaluate_tier_movement(
            fm, usage=usage, now=self._NOW, demote_after_days=60
        )
        assert new_tier == "hot"
        assert reason == "promote-on-use"

    def test_no_promote_when_never_referenced(self) -> None:
        fm = {"memory_class": "fact"}  # warm by class default
        result = memory_tiers.evaluate_tier_movement(
            fm, usage=None, now=self._NOW, demote_after_days=60
        )
        assert result == (None, None)

    def test_cold_page_never_moves(self) -> None:
        config = {"storage": {"mapping": {"pii": "excluded"}}}
        fm = {"type": "pii", "memory_class": "guideline", "updated": "2020-01-01T00:00:00Z"}
        result = memory_tiers.evaluate_tier_movement(
            fm, usage=None, now=self._NOW, demote_after_days=60, config=config
        )
        assert result == (None, None)


# ---------------------------------------------------------------------------
# set_memory_tier_text
# ---------------------------------------------------------------------------


class TestSetMemoryTierText:
    def test_no_frontmatter_returns_none(self) -> None:
        assert memory_tiers.set_memory_tier_text("no frontmatter here", "hot") is None

    def test_inserts_when_absent(self) -> None:
        text = "---\nname: Foo\ntype: concept\n---\n\nBody.\n"
        updated = memory_tiers.set_memory_tier_text(text, "hot")
        assert updated is not None
        assert "memory_tier: hot" in updated
        fm_block = updated.split("---")[1]
        assert "memory_tier: hot" in fm_block

    def test_updates_existing_value_in_place(self) -> None:
        text = "---\nname: Foo\nmemory_tier: hot\ntype: concept\n---\n\nBody.\n"
        updated = memory_tiers.set_memory_tier_text(text, "warm")
        assert updated is not None
        assert "memory_tier: warm" in updated
        assert "memory_tier: hot" not in updated
        # Only the memory_tier line changed -- name/type untouched.
        assert "name: Foo" in updated
        assert "type: concept" in updated

    def test_second_identical_write_is_a_byte_level_noop(self) -> None:
        text = "---\nname: Foo\nmemory_tier: hot\n---\n\nBody.\n"
        once = memory_tiers.set_memory_tier_text(text, "hot")
        twice = memory_tiers.set_memory_tier_text(once, "hot")
        assert once == twice


# ---------------------------------------------------------------------------
# run_tier_sweep (end-to-end)
# ---------------------------------------------------------------------------


class TestRunTierSweep:
    def test_scans_and_demotes_superseded_page(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "page1.md").write_text(
            _page_text(uid="p1", memory_class="guideline", superseded_by="winner"),
            encoding="utf-8",
        )
        report = memory_tiers.run_tier_sweep(wiki, cache_dir=tmp_path / "cache")
        assert report.scanned == 1
        assert len(report.changed) == 1
        change = report.changed[0]
        assert change.old_tier == "hot"
        assert change.new_tier == "warm"

        # Written to disk.
        text = (wiki / "page1.md").read_text(encoding="utf-8")
        assert "memory_tier: warm" in text

        # Ledgered.
        ledger = memory_tiers._tier_sweep_ledger_path(tmp_path / "cache")
        assert ledger.exists()
        assert "warm" in ledger.read_text(encoding="utf-8")

    def test_axiom_pages_are_skipped_not_demoted(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "axiom1.md").write_text(
            _page_text(uid="ax1", memory_class="axiom", superseded_by="winner"),
            encoding="utf-8",
        )
        report = memory_tiers.run_tier_sweep(wiki, cache_dir=tmp_path / "cache")
        assert report.scanned == 1
        assert report.changed == []
        assert report.skipped_axiom == 1
        text = (wiki / "axiom1.md").read_text(encoding="utf-8")
        assert "memory_tier:" not in text

    def test_dry_run_reports_without_writing(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        original = _page_text(uid="p1", memory_class="guideline", superseded_by="winner")
        (wiki / "page1.md").write_text(original, encoding="utf-8")
        report = memory_tiers.run_tier_sweep(wiki, cache_dir=tmp_path / "cache", dry_run=True)
        assert len(report.changed) == 1
        assert (wiki / "page1.md").read_text(encoding="utf-8") == original
        assert not memory_tiers._tier_sweep_ledger_path(tmp_path / "cache").exists()

    def test_second_run_is_a_no_op(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "page1.md").write_text(
            _page_text(uid="p1", memory_class="guideline", superseded_by="winner"),
            encoding="utf-8",
        )
        cache = tmp_path / "cache"
        first = memory_tiers.run_tier_sweep(wiki, cache_dir=cache)
        assert len(first.changed) == 1
        second = memory_tiers.run_tier_sweep(wiki, cache_dir=cache)
        assert second.changed == []

    def test_no_change_needed_leaves_page_untouched(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        recent = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
        text = _page_text(uid="p1", memory_class="guideline", updated=recent)
        (wiki / "page1.md").write_text(text, encoding="utf-8")
        report = memory_tiers.run_tier_sweep(wiki, cache_dir=tmp_path / "cache")
        assert report.changed == []
        assert (wiki / "page1.md").read_text(encoding="utf-8") == text

    def test_promotes_warm_page_that_was_referenced(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "page1.md").write_text(
            _page_text(uid="p1", memory_class="fact"),  # warm by class default
            encoding="utf-8",
        )
        cache = tmp_path / "cache"
        # Fabricate a usage record: pushed and referenced once.
        record = build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("page1.md", {"uid": "p1"}, "snip")]
        )
        record_push(record, cache_dir=cache)
        record_reference_result(
            ReferenceResult(
                session_id="s1", ts=record.ts, pushed_ids=["p1"], referenced_ids=["p1"]
            ),
            cache_dir=cache,
        )
        report = memory_tiers.run_tier_sweep(wiki, cache_dir=cache)
        assert len(report.changed) == 1
        assert report.changed[0].old_tier == "warm"
        assert report.changed[0].new_tier == "hot"


# ---------------------------------------------------------------------------
# demote_axiom_tier
# ---------------------------------------------------------------------------


class TestDemoteAxiomTier:
    def test_requires_axiom_class(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path = wiki / "p1.md"
        path.write_text(_page_text(uid="p1", memory_class="guideline"), encoding="utf-8")
        with pytest.raises(ValueError):
            memory_tiers.demote_axiom_tier(
                wiki, path, {"memory_class": "guideline"}, reason="r", by="human"
            )

    def test_writes_governance_ledger_and_tier(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path = wiki / "ax1.md"
        path.write_text(_page_text(uid="ax1", memory_class="axiom"), encoding="utf-8")
        fm = {"memory_class": "axiom", "uid": "ax1"}
        change = memory_tiers.demote_axiom_tier(
            wiki, path, fm, reason="operator walked it back", by="tristan"
        )
        assert change.old_tier == "hot"
        assert change.new_tier == "warm"

        text = path.read_text(encoding="utf-8")
        assert "memory_tier: warm" in text

        records = read_axiom_ledger(wiki, slug="ax1")
        assert len(records) == 1
        assert records[0]["action"] == "demote"
        assert records[0]["reason"] == "operator walked it back"
        assert records[0]["by"] == "tristan"

    def test_raises_when_not_currently_hot(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path = wiki / "ax1.md"
        path.write_text(
            _page_text(uid="ax1", memory_class="axiom", memory_tier="warm"), encoding="utf-8"
        )
        fm = {"memory_class": "axiom", "memory_tier": "warm", "uid": "ax1"}
        with pytest.raises(ValueError):
            memory_tiers.demote_axiom_tier(wiki, path, fm, reason="r", by="human")


# ---------------------------------------------------------------------------
# Config resolvers (issue athenaeum#718)
# ---------------------------------------------------------------------------


class TestResolvePushTokenBudget:
    def test_default(self) -> None:
        assert resolve_push_token_budget(None) == 1200

    def test_yaml(self) -> None:
        assert resolve_push_token_budget({"push_budget": {"tokens_per_turn": 500}}) == 500

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_PUSH_TOKEN_BUDGET", "42")
        assert resolve_push_token_budget({"push_budget": {"tokens_per_turn": 500}}) == 42

    def test_non_positive_yaml_falls_through(self) -> None:
        assert resolve_push_token_budget({"push_budget": {"tokens_per_turn": -5}}) == 1200

    def test_bool_yaml_falls_through(self) -> None:
        assert resolve_push_token_budget({"push_budget": {"tokens_per_turn": True}}) == 1200


class TestResolveMemoryTierSweepEnabled:
    def test_default_off(self) -> None:
        assert resolve_memory_tier_sweep_enabled(None) is False

    def test_yaml_true(self) -> None:
        config = {"librarian": {"memory_tier_sweep_enabled": True}}
        assert resolve_memory_tier_sweep_enabled(config) is True

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MEMORY_TIER_SWEEP_ENABLED", "0")
        config = {"librarian": {"memory_tier_sweep_enabled": True}}
        assert resolve_memory_tier_sweep_enabled(config) is False


class TestResolveMemoryTierDemoteAfterDays:
    def test_default(self) -> None:
        assert resolve_memory_tier_demote_after_days(None) == 60

    def test_yaml(self) -> None:
        config = {"memory_tiers": {"demote_after_days": 10}}
        assert resolve_memory_tier_demote_after_days(config) == 10

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MEMORY_TIER_DEMOTE_AFTER_DAYS", "5")
        config = {"memory_tiers": {"demote_after_days": 10}}
        assert resolve_memory_tier_demote_after_days(config) == 5
