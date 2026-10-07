# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.auto_apply` (issue athenaeum#716, lane 716-D).

Closes the gap lanes A/B/C surfaced and reported honestly: the ``duplicate``
auto-apply path in :mod:`athenaeum.verdict_effects` computed an
AUTHORIZATION (gate on + a fresh verdict basis) but never executed a fold —
:func:`athenaeum.auto_apply.enact_verdict_effect` is the composition layer
that actually performs the write, through :mod:`athenaeum.pending_merges`'s
real fold machinery, without either module importing the other.

Six scenarios, one class each, matching the issue's own enumeration:

1. ``TestGateOffNoFoldExecuted`` — the default-off regression test.
2. ``TestGateOnFreshDuplicateFoldExecutes`` — the fold really executes.
3. ``TestGateOnStaleVerdictNoFoldExecuted`` — stale blocks execution, not
   merely authorization.
4. ``TestAlreadyAppliedOperationsStand`` — marking the verdict stale
   afterwards does not un-apply anything.
5. ``TestAutoAppliedFoldIsUnfoldable`` — the round trip that is the whole
   argument for why auto-applying is safe.
6. ``TestNothingOutsideAllowlistReachesExecution`` — only a ``duplicate``
   verdict can ever reach the write path.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.auto_apply import AUTO_FOLD_EXECUTED_ACTION, enact_verdict_effect
from athenaeum.comparator import (
    VERDICT_CONTRADICTION,
    VERDICT_DISTINCT,
    VERDICT_DUPLICATE,
    VERDICT_SPECIALIZATION,
    VERDICT_UNDERDETERMINED,
    CompareOutcome,
    page_from_path,
)
from athenaeum.models import is_tombstone, parse_frontmatter, tombstone_target
from athenaeum.provenance import read_merge_provenance
from athenaeum.runlock import RunLock
from athenaeum.unfold import UNFOLD_DIRECT, unfold_page
from athenaeum.verdict_effects import _canonical_side
from athenaeum.verdicts import Basis, append_verdict, build_verdict_entry, mark_pairs_stale
from tests.conftest import init_git_repo

_AUTO_APPLY_CONFIG = {"librarian": {"reversible_verdict_auto_apply_enabled": True}}


def _write_page(path: Path, *, name: str, body: str, type_: str = "concept") -> None:
    path.write_text(
        f"---\nname: {name}\ntype: {type_}\n---\n{body}", encoding="utf-8"
    )


def _basis(**overrides) -> Basis:
    defaults = dict(
        content_hashes=["hash-a", "hash-b"],
        coords=[],
        coord_origins={},
        registry_epoch=1,
        tree_epoch=1,
        authority_basis="implicit-superuser",
        predicate_instrument=["status", "status"],
        comparator_version="v1.gate2",
    )
    defaults.update(overrides)
    return Basis(**defaults)


def _append_entry(wiki_root: Path, id_a: str, id_b: str, *, stale: bool = False) -> None:
    entry = build_verdict_entry(
        id_a, id_b, "duplicate", basis=_basis(), decided_by="comparator"
    )
    if stale:
        import dataclasses

        entry = dataclasses.replace(entry, stale=True, stale_reason="test-induced")
    lock = RunLock(wiki_root)
    lock.acquire()
    try:
        append_verdict(wiki_root, entry, lock=lock)
    finally:
        lock.release()


def _setup_pair(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Two equivalent-content pages plus a third page linking to BOTH
    slugs, under a fresh git repo. Returns ``(wiki_root, a_path, b_path,
    linker_path)``."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    a_path = wiki / "alpha.md"
    b_path = wiki / "beta.md"
    linker_path = wiki / "linker.md"
    _write_page(a_path, name="Alpha", body="Shared content about X.\n")
    _write_page(b_path, name="Beta", body="Shared content about X.\n")
    _write_page(linker_path, name="Linker", body="See [[alpha]] and [[beta]].\n")
    init_git_repo(wiki)
    return wiki, a_path, b_path, linker_path


class TestGateOffNoFoldExecuted:
    """1. gate off + fresh verdict => no fold executed, evidence/queue
    behaviour unchanged (the default-off regression test)."""

    def test_gate_off_falls_back_to_evidence_and_queue(self, tmp_path: Path) -> None:
        wiki, a_path, b_path, _linker = _setup_pair(tmp_path)
        _append_entry(wiki, "alpha", "beta", stale=False)
        page_a, page_b = page_from_path(a_path), page_from_path(b_path)
        outcome = CompareOutcome(verdict=VERDICT_DUPLICATE, widened_coords={})

        effect = enact_verdict_effect(
            page_a, page_b, outcome, wiki_root=wiki, path_a=a_path, path_b=b_path, config=None
        )

        assert effect.action == "fold-proposal"
        assert effect.details["auto_apply_authorized"] is False
        assert effect.details["auto_apply_reason"] == "auto_apply_disabled"
        assert len(effect.artifacts) == 1  # the fold-evidence file, unchanged
        assert not (wiki / "_pending_merges.md").exists()
        for p in (a_path, b_path):
            meta, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            assert not is_tombstone(meta)


class TestGateOnFreshDuplicateFoldExecutes:
    """2. gate on + fresh duplicate verdict => the fold really executed."""

    def test_fold_actually_writes_tombstone_aliases_links_and_provenance(
        self, tmp_path: Path
    ) -> None:
        wiki, a_path, b_path, linker_path = _setup_pair(tmp_path)
        _append_entry(wiki, "alpha", "beta", stale=False)
        page_a, page_b = page_from_path(a_path), page_from_path(b_path)
        outcome = CompareOutcome(verdict=VERDICT_DUPLICATE, widened_coords={})
        side, _rule, _rows = _canonical_side(page_a, page_b, outcome)
        canonical_path = a_path if side == "a" else b_path
        other_path = b_path if side == "a" else a_path

        effect = enact_verdict_effect(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki,
            path_a=a_path,
            path_b=b_path,
            config=_AUTO_APPLY_CONFIG,
        )

        assert effect.action == AUTO_FOLD_EXECUTED_ACTION
        assert effect.details["auto_apply_authorized"] is True
        assert effect.details["auto_apply_reason"] == "authorized"

        canonical_meta, _ = parse_frontmatter(canonical_path.read_text(encoding="utf-8"))
        other_meta, _ = parse_frontmatter(other_path.read_text(encoding="utf-8"))
        assert not is_tombstone(canonical_meta)  # canonical is live
        assert is_tombstone(other_meta)  # non-canonical is a tombstone
        assert tombstone_target(other_meta) == canonical_path.stem

        # aliases accumulated
        assert other_path.stem in (canonical_meta.get("aliases") or [])

        # inbound links rewritten: linker.md pointed at BOTH slugs; only the
        # folded-away one should now read the canonical slug.
        linker_text = linker_path.read_text(encoding="utf-8")
        assert f"[[{canonical_path.stem}]]" in linker_text
        assert f"[[{other_path.stem}]]" not in linker_text
        assert effect.details["links_rewritten"] == 1

        # provenance record: auto_applied True + the reversal-sufficient fields.
        records = read_merge_provenance(wiki)
        assert len(records) == 1
        record = records[0]
        assert record["auto_applied"] is True
        assert record["canonical_content_hash"]
        assert record["folded_sources"] == [str(other_path)]
        assert record["aliases_added"] == [other_path.stem]


class TestGateOnStaleVerdictNoFoldExecuted:
    """3. gate on + stale verdict => no fold executed (the stale-blocks-
    auto-apply path, executed this time, not merely authorized)."""

    def test_stale_verdict_blocks_execution(self, tmp_path: Path) -> None:
        wiki, a_path, b_path, _linker = _setup_pair(tmp_path)
        _append_entry(wiki, "alpha", "beta", stale=True)
        page_a, page_b = page_from_path(a_path), page_from_path(b_path)
        outcome = CompareOutcome(verdict=VERDICT_DUPLICATE, widened_coords={})

        effect = enact_verdict_effect(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki,
            path_a=a_path,
            path_b=b_path,
            config=_AUTO_APPLY_CONFIG,
        )

        assert effect.action == "fold-proposal"
        assert effect.details["auto_apply_authorized"] is False
        assert effect.details["auto_apply_reason"] == "stale_verdict"
        assert not (wiki / "_pending_merges.md").exists()
        for p in (a_path, b_path):
            meta, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            assert not is_tombstone(meta)


class TestEffortBudgetBreachBlocksAutoApply:
    """Issue athenaeum#1996 ratchet guard 1: an otherwise-authorized
    duplicate fold (gate on, fresh verdict basis) is refused while the
    decision queue's effort budget is in breach, and authorized again once
    it clears -- both directions, against the SAME pair/ledger state, so
    only ``effort_budget_breach`` itself can explain the different
    outcome."""

    def test_breach_refuses_then_clearing_reauthorizes(self, tmp_path: Path) -> None:
        wiki, a_path, b_path, _linker = _setup_pair(tmp_path)
        _append_entry(wiki, "alpha", "beta", stale=False)
        page_a, page_b = page_from_path(a_path), page_from_path(b_path)
        outcome = CompareOutcome(verdict=VERDICT_DUPLICATE, widened_coords={})

        # Breach reported: refused even though gate is on and the verdict
        # basis is fresh -- the SAME preconditions
        # TestGateOnFreshDuplicateFoldExecutes authorizes under.
        refused = enact_verdict_effect(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki,
            path_a=a_path,
            path_b=b_path,
            config=_AUTO_APPLY_CONFIG,
            effort_budget_breach=True,
        )
        assert refused.action == "fold-proposal"
        assert refused.details["auto_apply_authorized"] is False
        assert refused.details["auto_apply_reason"] == "effort_budget_breach"
        assert not (wiki / "_pending_merges.md").exists()
        for p in (a_path, b_path):
            meta, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            assert not is_tombstone(meta)

        # Breach clears: nothing else about the pair/ledger changed (the
        # refused call above wrote no fold), so this call is authorized and
        # the fold really executes.
        side, _rule, _rows = _canonical_side(page_a, page_b, outcome)
        other_path = b_path if side == "a" else a_path

        cleared = enact_verdict_effect(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki,
            path_a=a_path,
            path_b=b_path,
            config=_AUTO_APPLY_CONFIG,
            effort_budget_breach=False,
        )
        assert cleared.action == AUTO_FOLD_EXECUTED_ACTION
        assert cleared.details["auto_apply_authorized"] is True
        assert cleared.details["auto_apply_reason"] == "authorized"
        other_meta, _ = parse_frontmatter(other_path.read_text(encoding="utf-8"))
        assert is_tombstone(other_meta)


class TestAlreadyAppliedOperationsStand:
    """4. gate on, fold applied, THEN the verdict is marked stale =>
    the tombstone and the canonical are unchanged (operations already
    applied stand)."""

    def test_marking_stale_after_the_fold_does_not_unfold_anything(
        self, tmp_path: Path
    ) -> None:
        wiki, a_path, b_path, _linker = _setup_pair(tmp_path)
        _append_entry(wiki, "alpha", "beta", stale=False)
        page_a, page_b = page_from_path(a_path), page_from_path(b_path)
        outcome = CompareOutcome(verdict=VERDICT_DUPLICATE, widened_coords={})
        side, _rule, _rows = _canonical_side(page_a, page_b, outcome)
        canonical_path = a_path if side == "a" else b_path
        other_path = b_path if side == "a" else a_path

        effect = enact_verdict_effect(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki,
            path_a=a_path,
            path_b=b_path,
            config=_AUTO_APPLY_CONFIG,
        )
        assert effect.action == AUTO_FOLD_EXECUTED_ACTION
        canonical_before = canonical_path.read_text(encoding="utf-8")
        other_before = other_path.read_text(encoding="utf-8")

        pair_key = "|".join(sorted(("alpha", "beta")))
        lock = RunLock(wiki)
        lock.acquire()
        try:
            mark_pairs_stale(wiki, {pair_key: "test-induced-after-fold"}, lock=lock)
        finally:
            lock.release()

        assert canonical_path.read_text(encoding="utf-8") == canonical_before
        assert other_path.read_text(encoding="utf-8") == other_before
        other_meta, _ = parse_frontmatter(other_path.read_text(encoding="utf-8"))
        assert is_tombstone(other_meta)  # still a tombstone, not un-folded


class TestAutoAppliedFoldIsUnfoldable:
    """5. the round trip: an auto-applied fold is unfoldable by
    ``athenaeum.unfold`` directly, since the canonical is untouched since
    the fold -- the whole argument for why auto-applying is safe."""

    def test_unfold_page_restores_the_tombstone_directly(self, tmp_path: Path) -> None:
        wiki, a_path, b_path, _linker = _setup_pair(tmp_path)
        _append_entry(wiki, "alpha", "beta", stale=False)
        page_a, page_b = page_from_path(a_path), page_from_path(b_path)
        outcome = CompareOutcome(verdict=VERDICT_DUPLICATE, widened_coords={})
        side, _rule, _rows = _canonical_side(page_a, page_b, outcome)
        other_path = b_path if side == "a" else a_path

        effect = enact_verdict_effect(
            page_a,
            page_b,
            outcome,
            wiki_root=wiki,
            path_a=a_path,
            path_b=b_path,
            config=_AUTO_APPLY_CONFIG,
        )
        assert effect.action == AUTO_FOLD_EXECUTED_ACTION

        result = unfold_page(other_path, wiki_root=wiki)

        assert result.action == UNFOLD_DIRECT
        restored_meta, _ = parse_frontmatter(other_path.read_text(encoding="utf-8"))
        assert not is_tombstone(restored_meta)


class TestNothingOutsideAllowlistReachesExecution:
    """6. nothing outside AUTO_APPLY_OPERATIONS can reach the execution
    path -- every non-``duplicate`` verdict, even with the gate on and a
    fresh ``duplicate`` ledger entry sitting for the exact same pair, must
    never fold anything."""

    def test_non_duplicate_verdicts_never_fold(self, tmp_path: Path) -> None:
        wiki, a_path, b_path, _linker = _setup_pair(tmp_path)
        _append_entry(wiki, "alpha", "beta", stale=False)
        page_a, page_b = page_from_path(a_path), page_from_path(b_path)

        non_duplicate_outcomes = [
            CompareOutcome(verdict=VERDICT_DISTINCT, separator=["scope"]),
            CompareOutcome(verdict=VERDICT_UNDERDETERMINED, missing=["scope"]),
            CompareOutcome(verdict=VERDICT_CONTRADICTION),
            CompareOutcome(verdict=VERDICT_SPECIALIZATION, separator=["scope"], specific_side="a"),
        ]
        for outcome in non_duplicate_outcomes:
            effect = enact_verdict_effect(
                page_a,
                page_b,
                outcome,
                wiki_root=wiki,
                path_a=a_path,
                path_b=b_path,
                config=_AUTO_APPLY_CONFIG,
            )
            assert effect.action != AUTO_FOLD_EXECUTED_ACTION
            assert "folded_sources" not in effect.details

        assert not (wiki / "_pending_merges.md").exists()
        for p in (a_path, b_path):
            meta, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            assert not is_tombstone(meta)
