# SPDX-License-Identifier: Apache-2.0
"""Tests for the verdict ledger with justification basis (issue athenaeum#712)."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.runlock import RunLock
from athenaeum.verdicts import (
    Basis,
    BranchEpochState,
    EpochWaveInProgress,
    LockNotHeld,
    VerdictEntry,
    append_verdict,
    build_verdict_entry,
    can_authorize_auto_operation,
    close_wave,
    compact,
    content_hash,
    duty_cycle,
    ensure_ledger_initialized,
    get_verdict_status,
    ledger_count,
    list_by_verdict,
    lookup_pair,
    make_pair_key,
    mark_pairs_stale,
    note_run_night,
    open_epoch,
    record_pair_decision,
    refuse_if_erasure_class,
    select_stale_for_authority_revoked,
    select_stale_for_changed_page,
    select_stale_for_comparator_epoch_bump,
    select_stale_for_coordinate_challenged,
    select_stale_for_dimension_change,
    select_stale_for_tree_epoch_bump,
    show_one_pair,
    show_stale,
)


def _basis(**overrides) -> Basis:
    defaults = dict(
        content_hashes=["hash-a", "hash-b"],
        coords=["engagement:1", "engagement:2"],
        coord_origins={"engagement": "answer:q_1"},
        registry_epoch=1,
        tree_epoch=1,
        authority_basis="implicit-superuser",
        predicate_instrument=["status", "status"],
        comparator_version="v1.gate2",
    )
    defaults.update(overrides)
    return Basis(**defaults)


def _entry(
    id_a: str = "alpha",
    id_b: str = "beta",
    *,
    verdict: str = "duplicate",
    at: str = "2026-08-01",
    **basis_overrides,
) -> VerdictEntry:
    return build_verdict_entry(
        id_a,
        id_b,
        verdict,
        basis=_basis(**basis_overrides),
        at=at,
        decided_by="comparator",
    )


def _write_page(path: Path, *, name: str, body: str = "body text\n", extra: str = "") -> None:
    path.write_text(
        f"---\nname: {name}\ntype: feedback\n{extra}---\n{body}",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Content hashing — claim content only
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_content_hash_excludes_system_metadata(self) -> None:
        """Issue athenaeum#712 AC: writing system metadata leaves the hash unchanged."""
        before = "---\nname: alpha\ntype: feedback\n---\nsome claim text\n"
        after = (
            "---\n"
            "name: alpha\n"
            "type: feedback\n"
            "coords: [1, 2]\n"
            "breadcrumbs: [a, b]\n"
            "predicate: is-a\n"
            "tier: 3\n"
            "---\n"
            "some claim text\n"
        )
        assert content_hash(before) == content_hash(after)

    def test_content_hash_changes_on_body_change(self) -> None:
        a = "---\nname: alpha\n---\nbody one\n"
        b = "---\nname: alpha\n---\nbody two\n"
        assert content_hash(a) != content_hash(b)

    def test_content_hash_changes_on_claim_metadata_change(self) -> None:
        a = "---\nname: alpha\ntype: feedback\n---\nbody\n"
        b = "---\nname: alpha\ntype: reference\n---\nbody\n"
        assert content_hash(a) != content_hash(b)


class TestMakePairKey:
    def test_order_independent(self) -> None:
        assert make_pair_key("alpha", "beta") == make_pair_key("beta", "alpha")

    def test_stable_shape(self) -> None:
        assert make_pair_key("beta", "alpha") == "alpha+beta"


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------


class TestSchemaRoundTrip:
    def test_verdict_entry_round_trips(self) -> None:
        entry = _entry()
        restored = VerdictEntry.from_dict(entry.to_dict())
        assert restored == entry

    def test_build_verdict_entry_rejects_bad_verdict(self) -> None:
        with pytest.raises(ValueError):
            build_verdict_entry(
                "a", "b", "not-a-real-verdict", basis=_basis(), decided_by="comparator"
            )

    def test_build_verdict_entry_requires_decided_by(self) -> None:
        with pytest.raises(ValueError):
            build_verdict_entry("a", "b", "duplicate", basis=_basis(), decided_by="")


# ---------------------------------------------------------------------------
# Single-appender: enforced via RunLock
# ---------------------------------------------------------------------------


class TestSingleAppenderEnforced:
    def test_append_without_acquired_lock_raises(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)  # never acquired
        with pytest.raises(LockNotHeld):
            append_verdict(tmp_path / "wiki", _entry(), lock=lock)

    def test_append_with_acquired_lock_succeeds(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            path = append_verdict(wiki_root, _entry(), lock=lock)
        assert path.exists()

    def test_second_lock_on_same_root_is_held(self, tmp_path: Path) -> None:
        """End-to-end proof this reuses runlock's real mutual exclusion."""
        from athenaeum.runlock import LockHeld

        lock1 = RunLock(tmp_path)
        lock1.acquire()
        try:
            lock2 = RunLock(tmp_path)
            with pytest.raises(LockHeld):
                lock2.acquire()
        finally:
            lock1.release()

    def test_compact_and_mark_stale_also_require_lock(self, tmp_path: Path) -> None:
        lock = RunLock(tmp_path)
        with pytest.raises(LockNotHeld):
            compact(tmp_path / "wiki", lock=lock)
        with pytest.raises(LockNotHeld):
            mark_pairs_stale(tmp_path / "wiki", {"a+b": "reason"}, lock=lock)


# ---------------------------------------------------------------------------
# Writer / reader / memoization
# ---------------------------------------------------------------------------


class TestWriteAndLookup:
    def test_lookup_pair_missing_is_none(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        assert lookup_pair(wiki_root, "alpha+beta") is None

    def test_lookup_pair_after_append(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        entry = _entry()
        with lock:
            append_verdict(wiki_root, entry, lock=lock)
        found = lookup_pair(wiki_root, entry.pair)
        assert found is not None
        assert found.verdict == "duplicate"

    def test_get_verdict_status_one_call(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        entry = _entry()
        with lock:
            append_verdict(wiki_root, entry, lock=lock)
        status = get_verdict_status(wiki_root, entry.pair)
        assert status == {
            "decided": True,
            "fresh": True,
            "verdict": "duplicate",
            "at": "2026-08-01",
            "stale_reason": None,
        }
        assert get_verdict_status(wiki_root, "nope+nothing")["decided"] is False

    def test_ledger_count_and_list_by_verdict(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            append_verdict(wiki_root, _entry("a", "b", verdict="duplicate"), lock=lock)
            append_verdict(wiki_root, _entry("c", "d", verdict="distinct"), lock=lock)
        assert ledger_count(wiki_root) == 2
        dup = list_by_verdict(wiki_root, verdict="duplicate")
        assert len(dup) == 1
        assert dup[0]["pair"] == "a+b"

    def test_show_one_pair_cli_helper(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        entry = _entry()
        with lock:
            append_verdict(wiki_root, entry, lock=lock)
        assert show_one_pair(wiki_root, entry.pair)["verdict"] == "duplicate"
        assert show_one_pair(wiki_root, "missing+pair") is None

    def test_show_stale_cli_helper(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            append_verdict(wiki_root, _entry("a", "b", verdict="duplicate"), lock=lock)
            append_verdict(wiki_root, _entry("c", "d", verdict="distinct"), lock=lock)
            mark_pairs_stale(wiki_root, {"a+b": "changed_page"}, lock=lock)
        stale = show_stale(wiki_root)
        assert [e["pair"] for e in stale] == ["a+b"]
        assert stale[0]["stale"] is True
        assert stale[0]["stale_reason"] == "changed_page"


# ---------------------------------------------------------------------------
# Ledger growth: linear in cluster-level dispositions, never quadratic
# ---------------------------------------------------------------------------


class TestLedgerGrowthLinear:
    def test_record_pair_decision_writes_exactly_one_verdict(self, tmp_path: Path) -> None:
        """Issue athenaeum#712 AC: no code path writes a verdict for a pair that
        was never a clustering candidate — a corpus of N pages with exactly
        ONE real proposal (2 sources) must produce exactly ONE verdict, never
        C(N, 2)."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True)
        pages = []
        for i in range(6):
            p = wiki_root / f"page-{i}.md"
            _write_page(p, name=f"page-{i}")
            pages.append(p)

        lock = RunLock(tmp_path)
        with lock:
            result = record_pair_decision(
                wiki_root,
                source_a=str(pages[0]),
                source_b=str(pages[1]),
                verdict="duplicate",
                decided_by="pipeline:merge-approve",
                lock=lock,
            )
        assert result["ok"] is True
        # 6 pages -> C(6,2) = 15 possible pairs; only the ONE real
        # disposition may ever be recorded.
        assert ledger_count(wiki_root) == 1


# ---------------------------------------------------------------------------
# Compaction — live/history split
# ---------------------------------------------------------------------------


class TestCompaction:
    def test_compact_keeps_latest_moves_rest_to_history(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        older = _entry("a", "b", verdict="underdetermined", at="2026-06-01")
        newer = _entry("a", "b", verdict="duplicate", at="2026-08-01")
        with lock:
            append_verdict(wiki_root, older, lock=lock)
            append_verdict(wiki_root, newer, lock=lock)
            result = compact(wiki_root, lock=lock)
        assert result.moved_to_history == 1
        assert result.kept_live == 1
        live = lookup_pair(wiki_root, "a+b")
        assert live is not None
        assert live.verdict == "duplicate"
        from athenaeum.verdicts import read_history_entries

        history = read_history_entries(wiki_root)
        assert len(history) == 1
        assert history[0].verdict == "underdetermined"

    def test_compact_is_noop_on_already_compacted_corpus(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            append_verdict(wiki_root, _entry("a", "b"), lock=lock)
            first = compact(wiki_root, lock=lock)
            second = compact(wiki_root, lock=lock)
        assert first.partitions_rewritten == []  # single entry, nothing superseded
        assert second.moved_to_history == 0
        assert second.partitions_rewritten == []


# ---------------------------------------------------------------------------
# Targeted stale-marking — one test per rule
# ---------------------------------------------------------------------------


class TestStaleMarkingRules:
    def test_changed_content_hash_stale_marks_pair(self) -> None:
        entries = [_entry("alpha", "beta")]
        selected = select_stale_for_changed_page(
            entries, "alpha", new_content_hash="a-new-hash"
        )
        assert entries[0].pair in selected

    def test_unchanged_content_hash_does_not_stale_mark(self) -> None:
        entries = [_entry("alpha", "beta")]
        selected = select_stale_for_changed_page(
            entries, "alpha", new_content_hash="hash-a"  # matches basis.content_hashes[0]
        )
        assert selected == {}

    def test_dimension_retired_stale_marks_named_pairs(self) -> None:
        e1 = _entry("alpha", "beta")
        e1.assumed = ["engagement"]
        e2 = _entry("gamma", "delta")
        selected = select_stale_for_dimension_change(
            [e1, e2], "engagement", changed_ids=set()
        )
        assert e1.pair in selected
        assert e2.pair not in selected

    def test_dimension_change_stale_marks_by_changed_side(self) -> None:
        e1 = _entry("alpha", "beta")
        selected = select_stale_for_dimension_change(
            [e1], "engagement", changed_ids={"alpha"}
        )
        assert e1.pair in selected

    def test_coordinate_challenged_stale_marks_by_answer_id(self) -> None:
        e1 = _entry("alpha", "beta", coord_origins={"engagement": "answer:q_88"})
        e2 = _entry("gamma", "delta", coord_origins={"engagement": "answer:q_99"})
        selected = select_stale_for_coordinate_challenged([e1, e2], "answer:q_88")
        assert e1.pair in selected
        assert e2.pair not in selected

    def test_tree_epoch_bump_stale_marks_only_renamed_subtree(self) -> None:
        e1 = _entry("alpha", "beta", tree_epoch=1, coords=["scope:team-a/x", None])
        e2 = _entry("gamma", "delta", tree_epoch=1, coords=["scope:team-b/x", None])
        selected = select_stale_for_tree_epoch_bump([e1, e2], 2, ["scope:team-a"])
        assert e1.pair in selected
        assert e2.pair not in selected

    def test_authority_revoked_stale_marks_matching_basis(self) -> None:
        e1 = _entry("alpha", "beta", authority_basis="implicit-superuser")
        e2 = _entry("gamma", "delta", authority_basis="grant:someone-else")
        selected = select_stale_for_authority_revoked([e1, e2], "implicit-superuser")
        assert e1.pair in selected
        assert e2.pair not in selected

    def test_comparator_epoch_bump_stale_marks_only_its_branch(self) -> None:
        gate1 = _entry("alpha", "beta", comparator_version="v1.gate1")
        gate2 = _entry("gamma", "delta", comparator_version="v1.gate2")
        selected = select_stale_for_comparator_epoch_bump(
            [gate1, gate2], "v1.gate2", "v1.gate2-fix"
        )
        assert gate2.pair in selected
        assert gate1.pair not in selected

    def test_mark_pairs_stale_persists_and_is_side_effect_free_elsewhere(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True)
        # A provenance-like sentinel file elsewhere under wiki_root, to prove
        # stale-marking never touches anything outside the ledger dir.
        sentinel = wiki_root / "_merge_provenance.jsonl"
        sentinel.write_text("untouched\n", encoding="utf-8")

        lock = RunLock(tmp_path)
        entry = _entry("alpha", "beta")
        with lock:
            append_verdict(wiki_root, entry, lock=lock)
            marked = mark_pairs_stale(wiki_root, {entry.pair: "content changed"}, lock=lock)
        assert marked == 1
        found = lookup_pair(wiki_root, entry.pair)
        assert found.stale is True
        assert found.stale_reason == "content changed"
        assert sentinel.read_text(encoding="utf-8") == "untouched\n"


class TestStaleAuthorization:
    """Issue athenaeum#712 AC: stale cannot authorize a NEW auto op; applied ops stand."""

    def test_fresh_verdict_can_authorize(self) -> None:
        assert can_authorize_auto_operation(_entry()) is True

    def test_stale_verdict_cannot_authorize(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        entry = _entry()
        with lock:
            append_verdict(wiki_root, entry, lock=lock)
            mark_pairs_stale(wiki_root, {entry.pair: "reason"}, lock=lock)
        stale_entry = lookup_pair(wiki_root, entry.pair)
        assert can_authorize_auto_operation(stale_entry) is False

    def test_marking_stale_does_not_touch_already_applied_state(self, tmp_path: Path) -> None:
        """Half 2: an already-applied operation (modeled here as an
        untouched sentinel file elsewhere in the wiki tree) is unaffected
        by a stale-mark call — it stands until a real re-comparison."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True)
        applied_page = wiki_root / "merged-target.md"
        _write_page(applied_page, name="merged-target")
        before = applied_page.read_text(encoding="utf-8")

        lock = RunLock(tmp_path)
        entry = _entry()
        with lock:
            append_verdict(wiki_root, entry, lock=lock)
            mark_pairs_stale(wiki_root, {entry.pair: "reason"}, lock=lock)
        assert applied_page.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Epoch registry — no-overlapping-wave guard + duty cycle
# ---------------------------------------------------------------------------


class TestEpochRegistry:
    def test_open_epoch_then_reopen_without_close_raises(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            open_epoch(wiki_root, "gate2", "v1.gate2", lock=lock)
            with pytest.raises(EpochWaveInProgress):
                open_epoch(wiki_root, "gate2", "v1.gate2-fix", lock=lock)

    def test_close_wave_then_reopen_succeeds(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            open_epoch(wiki_root, "gate2", "v1.gate2", lock=lock)
            close_wave(wiki_root, "gate2", lock=lock)
            state = open_epoch(wiki_root, "gate2", "v1.gate2-fix", lock=lock)
        assert state.version == "v1.gate2-fix"
        assert state.wave_open is True

    def test_independent_branches_do_not_conflict(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            open_epoch(wiki_root, "gate1", "v1.gate1", lock=lock)
            # gate2 has no open wave yet, so this must NOT raise.
            open_epoch(wiki_root, "gate2", "v1.gate2", lock=lock)

    def test_duty_cycle_computation(self) -> None:
        state = BranchEpochState(version="v1", wave_open=True, nights_in_wave=3, nights_total=12)
        assert duty_cycle(state) == pytest.approx(0.25)

    def test_duty_cycle_zero_nights_is_zero(self) -> None:
        state = BranchEpochState(version="v1")
        assert duty_cycle(state) == 0.0

    def test_note_run_night_increments_counters(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            open_epoch(wiki_root, "gate2", "v1.gate2", lock=lock)
            note_run_night(wiki_root, lock=lock)
            report = note_run_night(wiki_root, lock=lock)
        assert report["gate2"] == pytest.approx(1.0)  # both nights inside the open wave


# ---------------------------------------------------------------------------
# Erasure-class refusal guard
# ---------------------------------------------------------------------------


class TestErasureClassGuard:
    def test_refuses_pii_flagged_page(self, tmp_path: Path) -> None:
        page = tmp_path / "person.md"
        page.write_text(
            "---\nname: Jane\ntype: person\npii: true\n---\nsome fact\n",
            encoding="utf-8",
        )
        reason = refuse_if_erasure_class(page)
        assert reason is not None
        assert "athenaeum#712" in reason

    def test_allows_ordinary_page(self, tmp_path: Path) -> None:
        page = tmp_path / "topic.md"
        _write_page(page, name="topic")
        assert refuse_if_erasure_class(page) is None

    def test_record_pair_decision_refuses_erasure_class_pair(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True)
        pii_page = wiki_root / "jane.md"
        pii_page.write_text(
            "---\nname: Jane\ntype: person\npii: true\n---\nfact\n", encoding="utf-8"
        )
        ordinary = wiki_root / "topic.md"
        _write_page(ordinary, name="topic")

        lock = RunLock(tmp_path)
        with lock:
            result = record_pair_decision(
                wiki_root,
                source_a=str(pii_page),
                source_b=str(ordinary),
                verdict="duplicate",
                decided_by="pipeline:merge-approve",
                lock=lock,
            )
        assert result["ok"] is False
        assert result["error_code"] == "erasure_class_refused"
        assert ledger_count(wiki_root) == 0


# ---------------------------------------------------------------------------
# Off-corpus ledger-shard routing (issue athenaeum#984 AC3)
# ---------------------------------------------------------------------------


class TestOffCorpusLedgerRouting:
    """A pair with an erasure-class side must land on the off-corpus ledger
    shard, in the SAME purgeable store as the off-corpus index — never the
    in-git ledger above. This is deliberately the SAME fixture shape as
    ``TestErasureClassGuard.test_record_pair_decision_refuses_erasure_class_pair``
    (a cross-boundary pair: one erasure-class source, one ordinary corpus
    source) so the only variable between "refuse" and "route off-corpus" is
    whether *config*/*knowledge_root* are supplied — proving the off state
    is unchanged and the on state is additive, not a behavior swap."""

    @staticmethod
    def _off_corpus_config(off_corpus_dir: Path) -> dict:
        return {
            "off_corpus": {"enabled": True, "adapter": "off-corpus-test"},
            "storage": {
                "adapters": {
                    "off-corpus-test": {
                        "backing_store": "markdown",
                        "surface_root": str(off_corpus_dir),
                        "corpus_policy": {
                            "embedded": False,
                            "recallable": True,
                            "merge_eligible": False,
                        },
                    },
                },
            },
        }

    def test_cross_boundary_pair_routes_to_off_corpus_and_not_git(
        self, tmp_path: Path
    ) -> None:
        """AC3's adversarial test: EXACTLY ONE side of the pair is
        erasure-class (the other is an ordinary corpus page) — a
        cross-boundary pair, not a fully-off-corpus one — and it must still
        route off-git in its entirety, never split or partially hashed into
        the in-git ledger."""
        from athenaeum import off_corpus

        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        pii_page = wiki_root / "jane.md"
        pii_page.write_text(
            "---\nname: Jane\ntype: person\npii: true\n---\nfact\n", encoding="utf-8"
        )
        ordinary = wiki_root / "topic.md"
        _write_page(ordinary, name="topic")

        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        config = self._off_corpus_config(off_corpus_dir)

        lock = RunLock(knowledge_root)
        with lock:
            result = record_pair_decision(
                wiki_root,
                source_a=str(pii_page),
                source_b=str(ordinary),
                verdict="duplicate",
                decided_by="pipeline:merge-approve",
                lock=lock,
                config=config,
                knowledge_root=knowledge_root,
            )

        assert result["ok"] is True
        assert result["ledger"] == "off-corpus"

        # AC3's core assertion: the pair must NOT appear in git (the in-git
        # ledger stays empty).
        assert ledger_count(wiki_root) == 0

        # ...and MUST appear in the off-corpus ledger shard, physically
        # outside the git working tree (off_corpus_dir is a sibling of
        # knowledge_root, never a subdirectory of it).
        store = off_corpus.off_corpus_store(config, knowledge_root)
        assert store is not None
        assert knowledge_root not in off_corpus_dir.parents

        import json as _json

        ledger_dir = off_corpus_dir / off_corpus.LEDGER_DIRNAME
        assert ledger_dir.is_dir()
        partitions = list(ledger_dir.glob("*.jsonl"))
        assert len(partitions) == 1
        lines = [
            _json.loads(line)
            for line in partitions[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        assert lines[0]["pair"] == result["pair"]

    def test_fully_off_corpus_pair_also_routes_off_git(self, tmp_path: Path) -> None:
        """Both sides erasure-class — the simpler, non-adversarial case,
        checked too so the adversarial cross-boundary test above is not the
        only coverage."""
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        jane = wiki_root / "jane.md"
        jane.write_text(
            "---\nname: Jane\ntype: person\npii: true\n---\nfact one\n", encoding="utf-8"
        )
        jane2 = wiki_root / "jane2.md"
        jane2.write_text(
            "---\nname: Jane 2\ntype: person\npii: true\n---\nfact two\n", encoding="utf-8"
        )

        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        config = self._off_corpus_config(off_corpus_dir)

        lock = RunLock(knowledge_root)
        with lock:
            result = record_pair_decision(
                wiki_root,
                source_a=str(jane),
                source_b=str(jane2),
                verdict="duplicate",
                decided_by="pipeline:merge-approve",
                lock=lock,
                config=config,
                knowledge_root=knowledge_root,
            )
        assert result["ok"] is True
        assert result["ledger"] == "off-corpus"
        assert ledger_count(wiki_root) == 0

    def test_without_config_still_refuses_exactly_as_before(self, tmp_path: Path) -> None:
        """The pre-athenaeum#984 default: config/knowledge_root omitted ->
        refuse-and-drop, byte-identical to before this issue."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True)
        pii_page = wiki_root / "jane.md"
        pii_page.write_text(
            "---\nname: Jane\ntype: person\npii: true\n---\nfact\n", encoding="utf-8"
        )
        ordinary = wiki_root / "topic.md"
        _write_page(ordinary, name="topic")

        lock = RunLock(tmp_path)
        with lock:
            result = record_pair_decision(
                wiki_root,
                source_a=str(pii_page),
                source_b=str(ordinary),
                verdict="duplicate",
                decided_by="pipeline:merge-approve",
                lock=lock,
            )
        assert result["ok"] is False
        assert result["error_code"] == "erasure_class_refused"
        assert ledger_count(wiki_root) == 0


# ---------------------------------------------------------------------------
# ensure_ledger_initialized — the "flag on" run-finalize contract
# ---------------------------------------------------------------------------


class TestEnsureLedgerInitialized:
    def test_creates_well_formed_empty_ledger(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            d = ensure_ledger_initialized(wiki_root, lock=lock)
        assert d.is_dir()
        assert (d / "_verdicts_epochs.json").exists()
        assert ledger_count(wiki_root) == 0  # well-formed, comparator-empty

    def test_idempotent(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        lock = RunLock(tmp_path)
        with lock:
            ensure_ledger_initialized(wiki_root, lock=lock)
            epath = wiki_root / "_verdicts" / "_verdicts_epochs.json"
            before = epath.read_text(encoding="utf-8")
            ensure_ledger_initialized(wiki_root, lock=lock)
            after = epath.read_text(encoding="utf-8")
        assert before == after
