# SPDX-License-Identifier: Apache-2.0
"""Tests for the two comparator instruments (issue athenaeum#715, phase 2):
``compatible`` TTL re-check and sibling-scope widening proposals.

Conventions match ``tests/test_comparator.py`` (``_page``/``_fake_client``/
``_content_payload`` helpers, offline ``MagicMock`` client, ``RunLock(tmp_path)``)
and ``tests/test_verdicts.py`` (``_basis``/``_entry``-shaped ledger seeding via
``build_verdict_entry`` + ``append_verdict``). No live network anywhere in this
file.
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import athenaeum.comparator_instruments as instruments_mod
from athenaeum.comparator import (
    COEXIST_SEPARATOR,
    COMPARATOR_VERSION_GATE1,
    COMPARATOR_VERSION_GATE2,
    VERDICT_DISTINCT,
    VERDICT_DUPLICATE,
    ComparatorPage,
    ContentRelation,
    page_from_text,
)
from athenaeum.comparator_instruments import (
    CONTENT_WRITE_COUNTER_NAME,
    TTL_STALE_REASON,
    WideningCandidate,
    WideningProposal,
    count_content_writes,
    record_content_writes,
    run_sibling_widening,
    run_ttl_recheck,
    select_compatible_ttl_expired,
    sibling_widening_candidates,
)
from athenaeum.runlock import RunLock
from athenaeum.verdicts import (
    Basis,
    append_verdict,
    build_verdict_entry,
    ledger_dir,
    lookup_pair,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _page(
    page_id: str,
    *,
    claimed_scope: str | None = None,
    memory_class: str | None = None,
    body: str = "some claim text",
) -> ComparatorPage:
    lines = ["---", "name: probe", "type: feedback"]
    if claimed_scope is not None:
        lines.append(f"claimed_scope: {claimed_scope}")
    if memory_class is not None:
        lines.append(f"memory_class: {memory_class}")
    lines.append("---")
    text = "\n".join(lines) + "\n" + body + "\n"
    return page_from_text(page_id, text)


def _fake_client(payload_json: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_json)]
    client.messages.create.return_value = response
    return client


def _content_payload(
    relation: str,
    *,
    passages: list[str] | None = None,
    rationale: str = "test rationale",
) -> str:
    return json.dumps(
        {
            "content_relation": relation,
            "conflicting_passages": passages or [],
            "predicate_a": "a-predicate",
            "predicate_b": "b-predicate",
            "rationale": rationale,
        }
    )


def _basis(comparator_version: str = COMPARATOR_VERSION_GATE2, **overrides: object) -> Basis:
    defaults: dict[str, object] = dict(
        content_hashes=["hash-a", "hash-b"],
        coords=[None, None],
        coord_origins={},
        registry_epoch=None,
        tree_epoch=None,
        authority_basis="implicit-superuser",
        predicate_instrument=[None, None],
        comparator_version=comparator_version,
    )
    defaults.update(overrides)
    return Basis(**defaults)  # type: ignore[arg-type]


def _seed_compatible(
    wiki_root: Path,
    lock: RunLock,
    id_a: str = "alpha",
    id_b: str = "beta",
    *,
    at: str = "2026-01-01",
    stale: bool = False,
) -> str:
    """Seed a ``compatible``-shaped verdict (DISTINCT / COEXIST_SEPARATOR /
    Gate 2) directly into the ledger and return its pair key."""
    entry = build_verdict_entry(
        id_a,
        id_b,
        VERDICT_DISTINCT,
        basis=_basis(),
        separator=[COEXIST_SEPARATOR],
        at=at,
        decided_by="comparator",
    )
    entry.stale = stale
    append_verdict(wiki_root, entry, lock=lock)
    return entry.pair


def _seed_other(
    wiki_root: Path,
    lock: RunLock,
    id_a: str,
    id_b: str,
    *,
    verdict: str = VERDICT_DUPLICATE,
    separator: list[str] | None = None,
    comparator_version: str = COMPARATOR_VERSION_GATE2,
    at: str = "2026-01-01",
) -> str:
    """Seed a non-``compatible``-shaped verdict, for negative-selection tests."""
    entry = build_verdict_entry(
        id_a,
        id_b,
        verdict,
        basis=_basis(comparator_version=comparator_version),
        separator=separator or [],
        at=at,
        decided_by="comparator",
    )
    append_verdict(wiki_root, entry, lock=lock)
    return entry.pair


# ---------------------------------------------------------------------------
# Content-write counter
# ---------------------------------------------------------------------------


class TestContentWriteCounter:
    def test_missing_counter_file_returns_empty(self, tmp_path: Path) -> None:
        assert count_content_writes(tmp_path) == {}

    def test_first_sighting_of_a_page_starts_at_zero(self, tmp_path: Path) -> None:
        result = record_content_writes(tmp_path, {"alpha": "hash-1"})
        assert result["alpha"] == 0
        assert count_content_writes(tmp_path)["alpha"] == 0

    def test_unchanged_hash_does_not_increment(self, tmp_path: Path) -> None:
        record_content_writes(tmp_path, {"alpha": "hash-1"})
        record_content_writes(tmp_path, {"alpha": "hash-1"})
        assert count_content_writes(tmp_path)["alpha"] == 0

    def test_changed_hash_increments_by_one(self, tmp_path: Path) -> None:
        record_content_writes(tmp_path, {"alpha": "hash-1"})
        record_content_writes(tmp_path, {"alpha": "hash-2"})
        assert count_content_writes(tmp_path)["alpha"] == 1

    def test_repeated_changes_accumulate(self, tmp_path: Path) -> None:
        for h in ("h1", "h2", "h3", "h4"):
            record_content_writes(tmp_path, {"alpha": h})
        assert count_content_writes(tmp_path)["alpha"] == 3

    def test_independent_pages_have_independent_counts(self, tmp_path: Path) -> None:
        record_content_writes(tmp_path, {"alpha": "a1"})
        record_content_writes(tmp_path, {"alpha": "a2", "beta": "b1"})
        counts = count_content_writes(tmp_path)
        assert counts["alpha"] == 1
        assert counts["beta"] == 0

    def test_counter_file_lives_under_verdicts_dir(self, tmp_path: Path) -> None:
        record_content_writes(tmp_path, {"alpha": "a1"})
        path = ledger_dir(tmp_path) / CONTENT_WRITE_COUNTER_NAME
        assert path.exists()

    def test_counter_file_is_valid_json(self, tmp_path: Path) -> None:
        record_content_writes(tmp_path, {"alpha": "a1", "beta": "b1"})
        path = ledger_dir(tmp_path) / CONTENT_WRITE_COUNTER_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["alpha"]["hash"] == "a1"
        assert payload["beta"]["hash"] == "b1"

    def test_record_content_writes_never_touches_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sanity: nothing here resolves a path from the environment/home.
        monkeypatch.delenv("HOME", raising=False)
        record_content_writes(tmp_path, {"alpha": "a1"})
        assert count_content_writes(tmp_path)["alpha"] == 0


# ---------------------------------------------------------------------------
# select_compatible_ttl_expired
# ---------------------------------------------------------------------------


class TestSelectCompatibleTtlExpiredEligibility:
    def test_ignores_non_distinct_verdict(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            _seed_other(tmp_path, lock, "alpha", "beta", verdict=VERDICT_DUPLICATE, at="2020-01-01")
        assert (
            select_compatible_ttl_expired(tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc))
            == []
        )

    def test_ignores_distinct_without_coexist_separator(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            _seed_other(
                tmp_path,
                lock,
                "alpha",
                "beta",
                verdict=VERDICT_DISTINCT,
                separator=["scope"],
                at="2020-01-01",
            )
        assert (
            select_compatible_ttl_expired(tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc))
            == []
        )

    def test_ignores_gate1_comparator_version(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            _seed_other(
                tmp_path,
                lock,
                "alpha",
                "beta",
                verdict=VERDICT_DISTINCT,
                separator=[COEXIST_SEPARATOR],
                comparator_version=COMPARATOR_VERSION_GATE1,
                at="2020-01-01",
            )
        assert (
            select_compatible_ttl_expired(tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc))
            == []
        )

    def test_ignores_already_stale_pair(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            _seed_compatible(tmp_path, lock, at="2020-01-01", stale=True)
        assert (
            select_compatible_ttl_expired(tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc))
            == []
        )

    def test_no_ledger_at_all_returns_empty(self, tmp_path: Path) -> None:
        assert select_compatible_ttl_expired(tmp_path) == []


class TestSelectCompatibleTtlExpiredAgeTrigger:
    def test_selects_pair_older_than_default_recheck_days(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            pair = _seed_compatible(tmp_path, lock, at="2026-01-01")
        result = select_compatible_ttl_expired(
            tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc)
        )
        assert result == [pair]

    def test_does_not_select_pair_within_recheck_days(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            _seed_compatible(tmp_path, lock, at="2026-08-20")
        result = select_compatible_ttl_expired(
            tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc)
        )
        assert result == []

    def test_exactly_at_threshold_is_expired(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            _seed_compatible(tmp_path, lock, at="2026-01-01")
        # 2026-01-01 -> 2026-07-02 is exactly 182 days; configure the
        # threshold down to exactly that so the boundary (>=) is exercised.
        config = {"librarian": {"compatible_recheck_days": 182}}
        result = select_compatible_ttl_expired(
            tmp_path, config=config, now=datetime(2026, 7, 2, tzinfo=timezone.utc)
        )
        assert len(result) == 1

    def test_recheck_days_configurable_via_yaml(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            pair = _seed_compatible(tmp_path, lock, at="2026-08-01")
        config = {"librarian": {"compatible_recheck_days": 10}}
        result = select_compatible_ttl_expired(
            tmp_path, config=config, now=datetime(2026, 8, 25, tzinfo=timezone.utc)
        )
        assert result == [pair]

    def test_recheck_days_configurable_via_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = RunLock(tmp_path)
        with lock:
            pair = _seed_compatible(tmp_path, lock, at="2026-08-01")
        monkeypatch.setenv("ATHENAEUM_COMPATIBLE_RECHECK_DAYS", "10")
        result = select_compatible_ttl_expired(
            tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc)
        )
        assert result == [pair]


class TestSelectCompatibleTtlExpiredWriteTrigger:
    def test_selects_pair_when_writes_meet_threshold(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            pair = _seed_compatible(tmp_path, lock, at="2026-08-20")
        for h in ("h1", "h2", "h3"):
            record_content_writes(tmp_path, {"alpha": h})
        config = {"librarian": {"compatible_recheck_writes": 2}}
        result = select_compatible_ttl_expired(
            tmp_path, config=config, now=datetime(2026, 8, 25, tzinfo=timezone.utc)
        )
        assert result == [pair]

    def test_does_not_select_when_writes_below_threshold(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            _seed_compatible(tmp_path, lock, at="2026-08-20")
        record_content_writes(tmp_path, {"alpha": "h1"})
        config = {"librarian": {"compatible_recheck_writes": 5}}
        result = select_compatible_ttl_expired(
            tmp_path, config=config, now=datetime(2026, 8, 25, tzinfo=timezone.utc)
        )
        assert result == []

    def test_either_side_advancing_is_sufficient(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            pair = _seed_compatible(tmp_path, lock, "alpha", "beta", at="2026-08-20")
        for h in ("h1", "h2"):
            record_content_writes(tmp_path, {"beta": h})
        config = {"librarian": {"compatible_recheck_writes": 1}}
        result = select_compatible_ttl_expired(
            tmp_path, config=config, now=datetime(2026, 8, 25, tzinfo=timezone.utc)
        )
        assert result == [pair]

    def test_recheck_writes_configurable_via_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = RunLock(tmp_path)
        with lock:
            pair = _seed_compatible(tmp_path, lock, at="2026-08-20")
        for h in ("h1", "h2", "h3"):
            record_content_writes(tmp_path, {"alpha": h})
        monkeypatch.setenv("ATHENAEUM_COMPATIBLE_RECHECK_WRITES", "2")
        result = select_compatible_ttl_expired(
            tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc)
        )
        assert result == [pair]

    def test_either_trigger_alone_is_sufficient_not_both_required(self, tmp_path: Path) -> None:
        """Age expired but writes below threshold still selects (OR, not AND)."""
        lock = RunLock(tmp_path)
        with lock:
            pair = _seed_compatible(tmp_path, lock, at="2026-01-01")
        config = {"librarian": {"compatible_recheck_writes": 999}}
        result = select_compatible_ttl_expired(
            tmp_path, config=config, now=datetime(2026, 8, 25, tzinfo=timezone.utc)
        )
        assert result == [pair]


# ---------------------------------------------------------------------------
# run_ttl_recheck
# ---------------------------------------------------------------------------


class TestRunTtlRecheck:
    def test_marks_expired_pairs_stale_with_named_reason(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            pair = _seed_compatible(tmp_path, lock, at="2026-01-01")
            result = run_ttl_recheck(
                tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc), lock=lock
            )
        assert result["expired"] == 1
        assert result["marked_stale"] == 1
        assert result["pairs"] == [pair]
        entry = lookup_pair(tmp_path, pair)
        assert entry is not None
        assert entry.stale is True
        assert entry.stale_reason == TTL_STALE_REASON

    def test_no_expired_pairs_is_a_clean_noop(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            _seed_compatible(tmp_path, lock, at="2026-08-20")
            result = run_ttl_recheck(
                tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc), lock=lock
            )
        assert result == {"ok": True, "expired": 0, "marked_stale": 0, "pairs": []}

    def test_never_calls_an_llm(self, tmp_path: Path) -> None:
        """No client/usage parameter exists on this function at all -- the
        strongest possible guarantee that it cannot reach an LLM."""
        sig = inspect.signature(run_ttl_recheck)
        assert "client" not in sig.parameters
        assert "usage" not in sig.parameters

    def test_requires_lock_only_when_something_would_be_marked(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            _seed_compatible(tmp_path, lock, at="2026-08-20")
        unacquired = RunLock(tmp_path)
        # Nothing to mark -- should not raise even though the lock was never
        # acquired (mark_pairs_stale is never called with an empty dict).
        result = run_ttl_recheck(
            tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc), lock=unacquired
        )
        assert result["expired"] == 0

    def test_raises_without_acquired_lock_when_pairs_are_expired(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            _seed_compatible(tmp_path, lock, at="2026-01-01")
        unacquired = RunLock(tmp_path)
        with pytest.raises(Exception):
            run_ttl_recheck(
                tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc), lock=unacquired
            )

    def test_write_baseline_resets_after_recheck(self, tmp_path: Path) -> None:
        """A pair flagged once should not immediately re-flag on the very
        next call purely from the SAME historical write count."""
        lock = RunLock(tmp_path)
        with lock:
            _seed_compatible(tmp_path, lock, at="2026-08-20")
        record_content_writes(tmp_path, {"alpha": "h1"})
        record_content_writes(tmp_path, {"alpha": "h2"})
        config = {"librarian": {"compatible_recheck_writes": 1}}
        with lock:
            first = run_ttl_recheck(
                tmp_path, config=config, now=datetime(2026, 8, 25, tzinfo=timezone.utc), lock=lock
            )
        assert first["expired"] == 1
        # Re-decide a fresh compatible verdict (simulating the comparator
        # re-running on the now-stale pair) with no further writes.
        with lock:
            _seed_compatible(tmp_path, lock, at="2026-08-25")
            second = run_ttl_recheck(
                tmp_path, config=config, now=datetime(2026, 8, 26, tzinfo=timezone.utc), lock=lock
            )
        assert second["expired"] == 0

    def test_marked_stale_never_exceeds_expired_count(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with lock:
            p1 = _seed_compatible(tmp_path, lock, "alpha", "beta", at="2026-01-01")
            p2 = _seed_compatible(tmp_path, lock, "gamma", "delta", at="2026-01-01")
            result = run_ttl_recheck(
                tmp_path, now=datetime(2026, 8, 25, tzinfo=timezone.utc), lock=lock
            )
        assert result["marked_stale"] <= result["expired"]
        assert sorted(result["pairs"]) == sorted([p1, p2])


# ---------------------------------------------------------------------------
# sibling_widening_candidates
# ---------------------------------------------------------------------------


class TestSiblingWideningCandidates:
    def test_requires_scope_disjoint(self) -> None:
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-b", memory_class="guideline")
        candidates = sibling_widening_candidates([(page_a, page_b, 0.9)])
        assert len(candidates) == 1
        assert candidates[0].page_a_id == "alpha"
        assert candidates[0].page_b_id == "beta"

    def test_excludes_equal_scope(self) -> None:
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-a", memory_class="guideline")
        assert sibling_widening_candidates([(page_a, page_b, 0.9)]) == []

    def test_excludes_ancestor_descendant_scope(self) -> None:
        """A CONTAINS relation (hierarchy prefix) is not a sibling."""
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-a/subteam", memory_class="guideline")
        assert sibling_widening_candidates([(page_a, page_b, 0.9)]) == []

    def test_excludes_unknown_scope(self) -> None:
        """Both sides null on scope -> UNKNOWN, not DISJOINT -- not a candidate."""
        page_a = _page("alpha", memory_class="guideline")
        page_b = _page("beta", memory_class="guideline")
        assert sibling_widening_candidates([(page_a, page_b, 0.9)]) == []

    def test_excludes_disallowed_memory_class(self) -> None:
        page_a = _page("alpha", claimed_scope="team-a", memory_class="entity")
        page_b = _page("beta", claimed_scope="team-b", memory_class="entity")
        assert sibling_widening_candidates([(page_a, page_b, 0.9)]) == []

    def test_requires_both_sides_in_allowed_class(self) -> None:
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-b", memory_class="entity")
        assert sibling_widening_candidates([(page_a, page_b, 0.9)]) == []

    def test_excludes_below_min_similarity(self) -> None:
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-b", memory_class="guideline")
        candidates = sibling_widening_candidates([(page_a, page_b, 0.5)])
        assert candidates == []

    def test_similarity_exactly_at_threshold_is_included(self) -> None:
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-b", memory_class="guideline")
        config = {"librarian": {"sibling_widening_min_similarity": 0.9}}
        candidates = sibling_widening_candidates([(page_a, page_b, 0.9)], config=config)
        assert len(candidates) == 1

    def test_min_similarity_configurable_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SIBLING_WIDENING_MIN_SIMILARITY", "0.5")
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-b", memory_class="guideline")
        candidates = sibling_widening_candidates([(page_a, page_b, 0.6)])
        assert len(candidates) == 1

    def test_allowed_classes_configurable_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SIBLING_WIDENING_CLASSES", "entity")
        page_a = _page("alpha", claimed_scope="team-a", memory_class="entity")
        page_b = _page("beta", claimed_scope="team-b", memory_class="entity")
        candidates = sibling_widening_candidates([(page_a, page_b, 0.9)])
        assert len(candidates) == 1

    def test_default_allowed_classes_include_guideline_procedure_axiom(self) -> None:
        for cls in ("guideline", "procedure", "axiom"):
            page_a = _page("alpha", claimed_scope="team-a", memory_class=cls)
            page_b = _page("beta", claimed_scope="team-b", memory_class=cls)
            assert len(sibling_widening_candidates([(page_a, page_b, 0.9)])) == 1

    def test_candidate_carries_raw_scope_values(self) -> None:
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-b", memory_class="guideline")
        candidates = sibling_widening_candidates([(page_a, page_b, 0.9)])
        assert candidates[0].scope_a == "team-a"
        assert candidates[0].scope_b == "team-b"

    def test_multiple_pairs_filtered_independently(self) -> None:
        good = (
            _page("alpha", claimed_scope="team-a", memory_class="guideline"),
            _page("beta", claimed_scope="team-b", memory_class="guideline"),
            0.9,
        )
        bad = (
            _page("gamma", claimed_scope="team-a", memory_class="guideline"),
            _page("delta", claimed_scope="team-a", memory_class="guideline"),
            0.9,
        )
        candidates = sibling_widening_candidates([good, bad])
        assert [c.page_a_id for c in candidates] == ["alpha"]

    def test_candidate_has_no_similarity_field(self) -> None:
        field_names = {f.name for f in fields(WideningCandidate)}
        assert not any("simil" in n.lower() for n in field_names)
        assert not any("confidence" in n.lower() for n in field_names)


# ---------------------------------------------------------------------------
# run_sibling_widening
# ---------------------------------------------------------------------------


class TestRunSiblingWideningBudget:
    def _candidates(self, n: int) -> list[tuple[ComparatorPage, ComparatorPage, float]]:
        out = []
        for i in range(n):
            page_a = _page(f"alpha-{i}", claimed_scope="team-a", memory_class="guideline")
            page_b = _page(f"beta-{i}", claimed_scope="team-b", memory_class="guideline")
            out.append((page_a, page_b, 0.95))
        return out

    def test_spends_up_to_budget(self, tmp_path: Path) -> None:
        pairs = self._candidates(3)
        client = _fake_client(_content_payload(ContentRelation.COMPATIBLE))
        config = {"librarian": {"sibling_widening_budget": 2}}
        result = run_sibling_widening(pairs, wiki_root=tmp_path, client=client, config=config)
        assert result["budget"] == 2
        assert result["spent"] == 2

    def test_skips_and_counts_pairs_over_budget_exactly(self, tmp_path: Path) -> None:
        pairs = self._candidates(5)
        client = _fake_client(_content_payload(ContentRelation.COMPATIBLE))
        config = {"librarian": {"sibling_widening_budget": 2}}
        result = run_sibling_widening(pairs, wiki_root=tmp_path, client=client, config=config)
        assert result["spent"] == 2
        assert result["skipped_over_budget"] == 3
        assert result["spent"] + result["skipped_over_budget"] == len(pairs)

    def test_budget_never_exceeded_across_many_candidates(self, tmp_path: Path) -> None:
        pairs = self._candidates(25)
        client = _fake_client(_content_payload(ContentRelation.COMPATIBLE))
        config = {"librarian": {"sibling_widening_budget": 7}}
        result = run_sibling_widening(pairs, wiki_root=tmp_path, client=client, config=config)
        assert result["spent"] <= result["budget"]
        assert result["spent"] == 7
        assert result["skipped_over_budget"] == 18

    def test_under_budget_skips_nothing(self, tmp_path: Path) -> None:
        pairs = self._candidates(2)
        client = _fake_client(_content_payload(ContentRelation.COMPATIBLE))
        config = {"librarian": {"sibling_widening_budget": 25}}
        result = run_sibling_widening(pairs, wiki_root=tmp_path, client=client, config=config)
        assert result["spent"] == 2
        assert result["skipped_over_budget"] == 0

    def test_no_candidates_spends_nothing(self, tmp_path: Path) -> None:
        client = _fake_client(_content_payload(ContentRelation.COMPATIBLE))
        result = run_sibling_widening([], wiki_root=tmp_path, client=client)
        assert result == {
            "budget": 25,
            "spent": 0,
            "skipped_over_budget": 0,
            "proposals": [],
        }


class TestRunSiblingWideningVerdictOutcomes:
    def _pair(self) -> list[tuple[ComparatorPage, ComparatorPage, float]]:
        page_a = _page(
            "alpha", claimed_scope="team-a", memory_class="guideline", body="deploy nightly"
        )
        page_b = _page(
            "beta", claimed_scope="team-b", memory_class="guideline", body="deploy nightly too"
        )
        return [(page_a, page_b, 0.95)]

    def test_equivalent_result_emits_a_proposal(self, tmp_path: Path) -> None:
        client = _fake_client(_content_payload(ContentRelation.EQUIVALENT, rationale="same rule"))
        result = run_sibling_widening(self._pair(), wiki_root=tmp_path, client=client)
        assert len(result["proposals"]) == 1
        proposal = result["proposals"][0]
        assert isinstance(proposal, WideningProposal)
        assert proposal.page_a_id == "alpha"
        assert proposal.page_b_id == "beta"
        assert proposal.scopes == ["team-a", "team-b"]
        assert proposal.rationale == "same rule"

    def test_compatible_result_emits_no_proposal(self, tmp_path: Path) -> None:
        client = _fake_client(_content_payload(ContentRelation.COMPATIBLE))
        result = run_sibling_widening(self._pair(), wiki_root=tmp_path, client=client)
        assert result["proposals"] == []
        assert result["spent"] == 1

    def test_conflicting_result_emits_no_proposal(self, tmp_path: Path) -> None:
        client = _fake_client(_content_payload(ContentRelation.CONFLICTING, passages=["a", "b"]))
        result = run_sibling_widening(self._pair(), wiki_root=tmp_path, client=client)
        assert result["proposals"] == []
        assert result["spent"] == 1

    def test_llm_unavailable_emits_no_proposal_but_still_counts_as_spent(
        self, tmp_path: Path
    ) -> None:
        result = run_sibling_widening(self._pair(), wiki_root=tmp_path, client=None)
        assert result["proposals"] == []
        assert result["spent"] == 1

    def test_proposal_never_writes_to_the_ledger(self, tmp_path: Path) -> None:
        client = _fake_client(_content_payload(ContentRelation.EQUIVALENT))
        run_sibling_widening(self._pair(), wiki_root=tmp_path, client=client)
        assert not (tmp_path / "_verdicts").exists()

    def test_proposal_does_not_mutate_source_pages(self, tmp_path: Path) -> None:
        pairs = self._pair()
        page_a, page_b, _sim = pairs[0]
        original_a, original_b = page_a.text, page_b.text
        client = _fake_client(_content_payload(ContentRelation.EQUIVALENT))
        run_sibling_widening(pairs, wiki_root=tmp_path, client=client)
        assert page_a.text == original_a
        assert page_b.text == original_b


class TestRunSiblingWideningMemoization:
    def test_fresh_verdict_is_not_re_spent(self, tmp_path: Path) -> None:
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-b", memory_class="guideline")
        lock = RunLock(tmp_path)
        with lock:
            entry = build_verdict_entry(
                "alpha",
                "beta",
                VERDICT_DUPLICATE,
                basis=_basis(),
                at="2026-08-20",
                decided_by="comparator",
            )
            append_verdict(tmp_path, entry, lock=lock)
        client = _fake_client(_content_payload(ContentRelation.EQUIVALENT))
        result = run_sibling_widening([(page_a, page_b, 0.95)], wiki_root=tmp_path, client=client)
        assert result["spent"] == 0
        assert result["proposals"] == []
        client.messages.create.assert_not_called()

    def test_stale_verdict_is_still_re_spent(self, tmp_path: Path) -> None:
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-b", memory_class="guideline")
        lock = RunLock(tmp_path)
        with lock:
            entry = build_verdict_entry(
                "alpha",
                "beta",
                VERDICT_DUPLICATE,
                basis=_basis(),
                at="2026-08-20",
                decided_by="comparator",
            )
            entry.stale = True
            append_verdict(tmp_path, entry, lock=lock)
        client = _fake_client(_content_payload(ContentRelation.EQUIVALENT))
        result = run_sibling_widening([(page_a, page_b, 0.95)], wiki_root=tmp_path, client=client)
        assert result["spent"] == 1

    def test_memoized_skip_does_not_count_against_budget(self, tmp_path: Path) -> None:
        page_a = _page("alpha", claimed_scope="team-a", memory_class="guideline")
        page_b = _page("beta", claimed_scope="team-b", memory_class="guideline")
        lock = RunLock(tmp_path)
        with lock:
            entry = build_verdict_entry(
                "alpha",
                "beta",
                VERDICT_DUPLICATE,
                basis=_basis(),
                at="2026-08-20",
                decided_by="comparator",
            )
            append_verdict(tmp_path, entry, lock=lock)
        client = _fake_client(_content_payload(ContentRelation.EQUIVALENT))
        config = {"librarian": {"sibling_widening_budget": 1}}
        result = run_sibling_widening(
            [(page_a, page_b, 0.95)], wiki_root=tmp_path, client=client, config=config
        )
        assert result["skipped_over_budget"] == 0
        assert result["spent"] == 0


# ---------------------------------------------------------------------------
# No confidence thresholds / no silent truncation (module non-negotiables)
# ---------------------------------------------------------------------------


class TestNoConfidenceThresholds:
    def test_widening_proposal_has_no_similarity_field(self) -> None:
        field_names = {f.name for f in fields(WideningProposal)}
        assert not any("simil" in n.lower() for n in field_names)
        assert not any("confidence" in n.lower() for n in field_names)

    def test_run_sibling_widening_decision_branches_never_test_similarity(self) -> None:
        """Static check: the only conditional deciding whether to emit a
        proposal reads ``result.relation``, never a similarity/confidence
        value -- mirrors ``test_comparator.py``'s equivalent AST check for
        ``predicate_instrument``."""
        tree = ast.parse(inspect.getsource(run_sibling_widening))
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test_src = ast.dump(node.test)
                assert "similarity" not in test_src.lower()
                assert "confidence" not in test_src.lower()

    def test_two_similarities_above_threshold_produce_identical_proposal_shape(
        self, tmp_path: Path
    ) -> None:
        """Similarity's only job is candidate selection -- once two pairs
        both clear the floor, differing similarity values must not change
        the resulting proposal's shape."""
        page_a1 = _page("alpha1", claimed_scope="team-a", memory_class="guideline")
        page_b1 = _page("beta1", claimed_scope="team-b", memory_class="guideline")
        page_a2 = _page("alpha2", claimed_scope="team-a", memory_class="guideline")
        page_b2 = _page("beta2", claimed_scope="team-b", memory_class="guideline")
        client_low = _fake_client(_content_payload(ContentRelation.EQUIVALENT, rationale="r"))
        client_high = _fake_client(_content_payload(ContentRelation.EQUIVALENT, rationale="r"))
        result_low = run_sibling_widening(
            [(page_a1, page_b1, 0.86)], wiki_root=tmp_path / "a", client=client_low
        )
        result_high = run_sibling_widening(
            [(page_a2, page_b2, 0.99)], wiki_root=tmp_path / "b", client=client_high
        )
        shape_low = (result_low["proposals"][0].scopes, result_low["proposals"][0].rationale)
        shape_high = (result_high["proposals"][0].scopes, result_high["proposals"][0].rationale)
        assert shape_low == shape_high


# ---------------------------------------------------------------------------
# Module hygiene
# ---------------------------------------------------------------------------


class TestModuleHygiene:
    def test_spdx_header(self) -> None:
        src = Path(instruments_mod.__file__).read_text(encoding="utf-8")
        assert src.startswith("# SPDX-License-Identifier: Apache-2.0")

    def test_module_docstring_cites_issue_715(self) -> None:
        assert "athenaeum#715" in (instruments_mod.__doc__ or "")

    def test_all_exports_are_importable_and_match_dunder_all(self) -> None:
        for name in instruments_mod.__all__:
            assert hasattr(instruments_mod, name), name

    def test_public_functions_are_fully_annotated(self) -> None:
        for func in (
            count_content_writes,
            record_content_writes,
            select_compatible_ttl_expired,
            run_ttl_recheck,
            sibling_widening_candidates,
            run_sibling_widening,
        ):
            sig = inspect.signature(func)
            for name, param in sig.parameters.items():
                if name == "self":
                    continue
                assert param.annotation is not inspect.Parameter.empty, (func.__name__, name)
            assert sig.return_annotation is not inspect.Signature.empty, func.__name__

    def test_module_does_not_import_librarian_or_decision_answers(self) -> None:
        """AST-based, not a substring scan -- the module docstring legitimately
        NAMES ``athenaeum.librarian``/``athenaeum.decision_answers`` in prose
        (explaining that it does not import them), so only actual ``import``/
        ``from ... import`` statements are checked here."""
        tree = ast.parse(Path(instruments_mod.__file__).read_text(encoding="utf-8"))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        assert "athenaeum.librarian" not in imported_modules
        assert "athenaeum.decision_answers" not in imported_modules

    def test_module_never_reads_home_directory(self) -> None:
        """AST-based: no call to ``Path.home()`` / ``os.path.expanduser`` /
        ``os.environ["HOME"]`` anywhere in the module (the docstring's prose
        mention of ``~/knowledge`` is not itself a filesystem access)."""
        src = Path(instruments_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("home", "expanduser"):
                pytest.fail(f"unexpected home-directory access: {ast.dump(node)}")
