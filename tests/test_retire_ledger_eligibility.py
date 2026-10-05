# SPDX-License-Identifier: Apache-2.0
"""Tests for the comparator-ledger-based move-eligibility rewrite (athenaeum#1256).

The C4 contradiction detector is retired; ``athenaeum.retire._move_eligibility``
no longer reads ``entry.contradiction`` and instead reads the comparator's
recorded verdicts straight from the verdict ledger
(:func:`athenaeum.verdicts.get_verdict_status`) for every candidate pair among
a cluster's resolved members. This file pins the operator-specified six-way
mapping from ``measurements/c4-retirement-preconditions-2026-09-15.md``
("What the chosen option requires in code", step 4):

- no ledger row (never attempted)           -> HOLD
- decided but stale                         -> HOLD
- verdict == "contradiction"                -> HOLD
- verdict == "underdetermined"              -> HOLD
- Gate 2 unavailable (never ledgered)       -> HOLD (same "no row" code path)
- T1-screened-out (never ledgered)          -> HOLD (same "no row" code path)
- verdict in {duplicate, distinct, specialization} -> eligible

It also pins the load-bearing vacuous-quantifier guard: "none of the pairs is
contradiction" must not be checked only over the SUBSET of pairs that happen
to carry a verdict -- a multi-member cluster with zero ledgered pairs must
HOLD, not pass vacuously. Only a genuine singleton (fewer than two resolved
members) may pass without consulting the ledger at all.

Ledger state is built through the real ``athenaeum.verdicts`` writer API
(``build_verdict_entry`` + ``append_verdict`` under an acquired ``RunLock``),
the same fixture idiom ``tests/test_verdicts.py`` and
``tests/test_cluster_comparator.py`` use, rather than hand-writing JSONL --
so pair-key derivation is exercised for real.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from athenaeum.merge import MergedWikiEntry
from athenaeum.retire import _move_eligibility
from athenaeum.runlock import RunLock
from athenaeum.verdicts import (
    Basis,
    VerdictEntry,
    append_verdict,
    build_verdict_entry,
    make_pair_key,
    page_id_for_path,
)


def _entry(*, contradictions_detected: bool = False) -> MergedWikiEntry:
    """A minimal cluster entry -- only ``contradictions_detected`` is read."""
    return MergedWikiEntry(
        topic_slug="t",
        cluster_id="c-1",
        cluster_centroid_score=1.0,
        contradictions_detected=contradictions_detected,
    )


def _member(tmp_path: Path, name: str) -> Path:
    scope = tmp_path / "raw" / "auto-memory" / "scope-x"
    scope.mkdir(parents=True, exist_ok=True)
    path = scope / f"{name}.md"
    path.write_text(f"---\nname: {name}\ntype: feedback\n---\nbody {name}\n", encoding="utf-8")
    return path


def _write_verdict(
    wiki_root: Path,
    lock: RunLock,
    id_a: str,
    id_b: str,
    verdict: str,
    *,
    at: str = "2026-09-01",
    stale: bool = False,
    stale_reason: str | None = None,
) -> None:
    entry: VerdictEntry = build_verdict_entry(
        id_a, id_b, verdict, basis=Basis(), at=at, decided_by="comparator"
    )
    if stale:
        entry = replace(entry, stale=True, stale_reason=stale_reason)
    append_verdict(wiki_root, entry, lock=lock)


def _pair_key(wiki_root: Path, a: Path, b: Path) -> str:
    return make_pair_key(
        page_id_for_path(a, root=wiki_root), page_id_for_path(b, root=wiki_root)
    )


# ---------------------------------------------------------------------------
# Genuine vacuous passes -- zero candidate pairs, checked BEFORE the ledger
# ---------------------------------------------------------------------------


class TestGenuineVacuousPass:
    def test_zero_members_eligible(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        eligible, reason = _move_eligibility(_entry(), wiki_root, [])
        assert eligible is True
        assert reason == ""

    def test_single_member_eligible(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        a = _member(tmp_path, "a")
        eligible, reason = _move_eligibility(_entry(), wiki_root, [a])
        assert eligible is True
        assert reason == ""


# ---------------------------------------------------------------------------
# The six-way HOLD mapping
# ---------------------------------------------------------------------------


class TestHoldCases:
    def test_hold_no_ledger_row_never_attempted(self, tmp_path: Path) -> None:
        """A pair that was simply never compared -- no row exists at all."""
        wiki_root = tmp_path / "wiki"
        a, b = _member(tmp_path, "a"), _member(tmp_path, "b")

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b])

        assert eligible is False
        pair_key = _pair_key(wiki_root, a, b)
        assert pair_key in reason
        assert "no comparator verdict ledgered" in reason

    def test_hold_gate2_unavailable(self, tmp_path: Path) -> None:
        """Gate 2 unavailable never ledgers a row -- indistinguishable, from a
        pure ``get_verdict_status`` read, from "never attempted". Both map
        onto the same HOLD code path and reason (documented hazard in
        ``cluster_comparator.py``'s own docstring)."""
        wiki_root = tmp_path / "wiki"
        a, b = _member(tmp_path, "a"), _member(tmp_path, "b")

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b])

        assert eligible is False
        assert "no comparator verdict ledgered" in reason

    def test_hold_t1_screened_out(self, tmp_path: Path) -> None:
        """A T1-screened-out pair also never reaches the ledger -- same HOLD
        code path as "never attempted" / "Gate 2 unavailable"; retire.py
        cannot and does not need to distinguish them."""
        wiki_root = tmp_path / "wiki"
        a, b = _member(tmp_path, "a"), _member(tmp_path, "b")

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b])

        assert eligible is False
        assert "no comparator verdict ledgered" in reason

    def test_hold_stale_row(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        a, b = _member(tmp_path, "a"), _member(tmp_path, "b")
        id_a = page_id_for_path(a, root=wiki_root)
        id_b = page_id_for_path(b, root=wiki_root)

        with RunLock(tmp_path) as lock:
            _write_verdict(
                wiki_root, lock, id_a, id_b, "duplicate",
                stale=True, stale_reason="page-changed",
            )

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b])

        assert eligible is False
        assert "stale" in reason
        assert "page-changed" in reason

    def test_hold_contradiction_verdict(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        a, b = _member(tmp_path, "a"), _member(tmp_path, "b")
        id_a = page_id_for_path(a, root=wiki_root)
        id_b = page_id_for_path(b, root=wiki_root)

        with RunLock(tmp_path) as lock:
            _write_verdict(wiki_root, lock, id_a, id_b, "contradiction")

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b])

        assert eligible is False
        assert "contradiction" in reason

    def test_hold_underdetermined_verdict(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        a, b = _member(tmp_path, "a"), _member(tmp_path, "b")
        id_a = page_id_for_path(a, root=wiki_root)
        id_b = page_id_for_path(b, root=wiki_root)

        with RunLock(tmp_path) as lock:
            _write_verdict(wiki_root, lock, id_a, id_b, "underdetermined")

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b])

        assert eligible is False
        assert "underdetermined" in reason

    def test_hold_contradictions_detected_flag_short_circuits(self, tmp_path: Path) -> None:
        """Belt-and-suspenders: a (now-residual, no longer set by merge.py)
        ``contradictions_detected`` flag still holds regardless of ledger
        state -- it can only ever force a HOLD, never override one."""
        wiki_root = tmp_path / "wiki"
        a, b = _member(tmp_path, "a"), _member(tmp_path, "b")
        id_a = page_id_for_path(a, root=wiki_root)
        id_b = page_id_for_path(b, root=wiki_root)

        with RunLock(tmp_path) as lock:
            _write_verdict(wiki_root, lock, id_a, id_b, "duplicate")

        eligible, reason = _move_eligibility(
            _entry(contradictions_detected=True), wiki_root, [a, b]
        )

        assert eligible is False
        assert reason == "contradiction flagged — queued for human confirmation"


# ---------------------------------------------------------------------------
# Eligible verdicts
# ---------------------------------------------------------------------------


class TestEligibleVerdicts:
    def test_duplicate_is_eligible(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        a, b = _member(tmp_path, "a"), _member(tmp_path, "b")
        id_a = page_id_for_path(a, root=wiki_root)
        id_b = page_id_for_path(b, root=wiki_root)

        with RunLock(tmp_path) as lock:
            _write_verdict(wiki_root, lock, id_a, id_b, "duplicate")

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b])
        assert (eligible, reason) == (True, "")

    def test_distinct_is_eligible(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        a, b = _member(tmp_path, "a"), _member(tmp_path, "b")
        id_a = page_id_for_path(a, root=wiki_root)
        id_b = page_id_for_path(b, root=wiki_root)

        with RunLock(tmp_path) as lock:
            _write_verdict(wiki_root, lock, id_a, id_b, "distinct")

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b])
        assert (eligible, reason) == (True, "")

    def test_specialization_is_eligible(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        a, b = _member(tmp_path, "a"), _member(tmp_path, "b")
        id_a = page_id_for_path(a, root=wiki_root)
        id_b = page_id_for_path(b, root=wiki_root)

        with RunLock(tmp_path) as lock:
            _write_verdict(wiki_root, lock, id_a, id_b, "specialization")

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b])
        assert (eligible, reason) == (True, "")


# ---------------------------------------------------------------------------
# The vacuous-quantifier regression guard (load-bearing, per the design doc)
# ---------------------------------------------------------------------------


class TestVacuousQuantifierRegressionGuard:
    def test_multi_member_cluster_with_no_rows_holds_not_passes(
        self, tmp_path: Path
    ) -> None:
        """A 3-member cluster (3 candidate pairs) with NO ledger rows at all
        must HOLD. The bug this guards against: filtering down to "the pairs
        that happen to have a verdict" (here, the empty set) and folding
        ``all(v != "contradiction" for v in that subset)`` over it, which is
        vacuously True and would wrongly pass this cluster."""
        wiki_root = tmp_path / "wiki"
        a, b, c = _member(tmp_path, "a"), _member(tmp_path, "b"), _member(tmp_path, "c")

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b, c])

        assert eligible is False
        assert "no comparator verdict ledgered" in reason

    def test_mixed_cluster_one_clean_pair_one_missing_row_holds(
        self, tmp_path: Path
    ) -> None:
        """3 members -> 3 candidate pairs. (a, b) has a clean ``distinct``
        verdict; (a, c) and (b, c) have no row at all. The cluster must HOLD
        -- a clean verdict on ONE pair must never authorize retiring raw
        whose OTHER pairs were never examined."""
        wiki_root = tmp_path / "wiki"
        a, b, c = _member(tmp_path, "a"), _member(tmp_path, "b"), _member(tmp_path, "c")
        id_a = page_id_for_path(a, root=wiki_root)
        id_b = page_id_for_path(b, root=wiki_root)

        with RunLock(tmp_path) as lock:
            _write_verdict(wiki_root, lock, id_a, id_b, "distinct")

        eligible, reason = _move_eligibility(_entry(), wiki_root, [a, b, c])

        assert eligible is False
        assert "no comparator verdict ledgered" in reason
