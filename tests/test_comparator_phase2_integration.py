# SPDX-License-Identifier: Apache-2.0
"""Integration across the athenaeum#715 phase-2 seams.

Each phase-2 module has its own unit suite, and each stubs its neighbours --
``tests/test_verdict_effects.py`` fakes :mod:`athenaeum.supersession`,
``tests/test_supersession.py`` never goes through
:func:`athenaeum.verdict_effects.apply_verdict_effect`. That is the right
shape for unit tests and it leaves exactly one thing unchecked: whether the
two modules actually fit together. These tests use the REAL modules on both
sides of every seam.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from athenaeum.comparator import (
    VERDICT_CONTRADICTION,
    ComparatorPage,
    CompareOutcome,
    page_from_text,
)
from athenaeum.supersession import (
    SUPERSESSION_APPLIED,
    SUPERSESSION_QUEUE,
    decide_supersession,
    read_supersession_records,
)
from athenaeum.verdict_effects import apply_verdict_effect

ALICE: dict[str, Any] = {
    "type": "person",
    "iss": "https://accounts.example.com",
    "sub": "alice",
}
BOB: dict[str, Any] = {
    "type": "person",
    "iss": "https://accounts.example.com",
    "sub": "bob",
}

_AUTO_ON: dict[str, Any] = {
    "librarian": {"comparator_enabled": True, "auto_supersession_enabled": True}
}


def _page(
    page_id: str,
    *,
    asserter: dict[str, Any] | None,
    observed_at: date,
    recorded_at: str,
    claim_kind: str = "fact",
    body: str = "the standing state is X",
) -> ComparatorPage:
    lines = ["---", f"name: {page_id}", f"claim_kind: {claim_kind}"]
    lines.append(f'observed_at: "{observed_at.isoformat()}"')
    lines.append(f'recorded_at: "{recorded_at}"')
    if asserter is not None:
        lines.append("asserter:")
        for key, value in asserter.items():
            lines.append(f"  {key}: {value}")
    lines += ["---", "", body, ""]
    return page_from_text(page_id, "\n".join(lines))


def _pair(
    *,
    winner_asserter: dict[str, Any] | None = ALICE,
    loser_asserter: dict[str, Any] | None = ALICE,
) -> tuple[ComparatorPage, ComparatorPage]:
    winner = _page(
        "winner-page",
        asserter=winner_asserter,
        observed_at=date(2026, 6, 1),
        recorded_at="2026-06-01T00:00:00+00:00",
        body="the standing state is Y",
    )
    loser = _page(
        "loser-page",
        asserter=loser_asserter,
        observed_at=date(2026, 1, 1),
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    return winner, loser


def _contradiction(passages: list[str] | None = None) -> CompareOutcome:
    return CompareOutcome(
        verdict=VERDICT_CONTRADICTION,
        conflicting_passages=passages if passages is not None else ["says Y", "says X"],
        comparator_version="v1.gate2",
    )


NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


class TestVerdictEffectsReachesTheRealSupersession:
    """The seam the two unit suites both stub out."""

    def test_an_auto_supersedable_contradiction_reports_superseded(self, tmp_path: Path) -> None:
        winner, loser = _pair()
        result = apply_verdict_effect(
            winner,
            loser,
            _contradiction(),
            wiki_root=tmp_path,
            config=_AUTO_ON,
            now=NOW,
        )
        assert result.action == "superseded"
        assert result.details["supersession_available"] is True
        assert result.details["winner_id"] == "winner-page"
        assert result.details["loser_id"] == "loser-page"
        assert result.details["located_passages"] == ["says Y", "says X"]

    def test_the_same_pair_queues_when_auto_supersession_is_off(self, tmp_path: Path) -> None:
        # The ONLY difference from the test above is the config switch --
        # which is the whole point of it being a separate switch.
        winner, loser = _pair()
        result = apply_verdict_effect(
            winner,
            loser,
            _contradiction(),
            wiki_root=tmp_path,
            config={"librarian": {"comparator_enabled": True}},
            now=NOW,
        )
        assert result.action == "queued"
        assert result.details["supersession_available"] is True
        assert "auto_supersession_enabled" in result.details["blocked_by"]

    def test_a_cross_asserter_peer_conflict_queues_rather_than_picking_a_winner(
        self, tmp_path: Path
    ) -> None:
        # Neither side declares grants, so authority is EQUAL and route (a)
        # is unavailable -- athenaeum#715: "Equal-authority, uncorroborated,
        # cross-asserter conflicts always queue. Between peers, recency alone
        # is never truth."
        winner, loser = _pair(winner_asserter=ALICE, loser_asserter=BOB)
        result = apply_verdict_effect(
            winner, loser, _contradiction(), wiki_root=tmp_path, config=_AUTO_ON, now=NOW
        )
        assert result.action == "queued"
        assert result.details["blocked_by"]

    def test_an_unlocated_contradiction_queues_through_the_real_module(
        self, tmp_path: Path
    ) -> None:
        # The silent-no-op trap, end to end: no located passage means there
        # is no claim to retire, so nothing may be enacted.
        winner, loser = _pair()
        result = apply_verdict_effect(
            winner, loser, _contradiction([]), wiki_root=tmp_path, config=_AUTO_ON, now=NOW
        )
        assert result.action == "queued"
        assert "located" in result.details["blocked_by"]

    def test_deciding_writes_nothing_to_the_supersession_ledger(self, tmp_path: Path) -> None:
        # decide_supersession is read-only; only enact_supersession writes,
        # and apply_verdict_effect never enacts. An "applied" ledger row must
        # always be proof a retirement happened, never a projection of one.
        winner, loser = _pair()
        apply_verdict_effect(
            winner, loser, _contradiction(), wiki_root=tmp_path, config=_AUTO_ON, now=NOW
        )
        assert read_supersession_records(tmp_path) == []


class TestDecisionAgreesWithTheEffectLayer:
    @pytest.mark.parametrize(
        ("winner_asserter", "loser_asserter", "expected_action"),
        [
            (ALICE, ALICE, SUPERSESSION_APPLIED),
            (ALICE, BOB, SUPERSESSION_QUEUE),
        ],
    )
    def test_effect_action_tracks_the_decision(
        self,
        tmp_path: Path,
        winner_asserter: dict[str, Any],
        loser_asserter: dict[str, Any],
        expected_action: str,
    ) -> None:
        winner, loser = _pair(winner_asserter=winner_asserter, loser_asserter=loser_asserter)
        outcome = _contradiction()
        decision = decide_supersession(
            winner, loser, outcome, wiki_root=tmp_path, config=_AUTO_ON, now=NOW
        )
        assert decision.action == expected_action
        result = apply_verdict_effect(
            winner, loser, outcome, wiki_root=tmp_path, config=_AUTO_ON, now=NOW
        )
        assert result.action == (
            "superseded" if expected_action == SUPERSESSION_APPLIED else "queued"
        )


class TestPhase2StaysDark:
    """The invariant ``tests/test_comparator.py::TestAC1LandedDark`` pins for
    the comparator core, extended to every phase-2 module.

    Each of these is reachable only from the comparator subsystem or from the
    explicit, opt-in ``athenaeum merges recompare`` command. A future edit
    that wires one into the nightly path without the cut-over -- which
    athenaeum#715 requires to REPLACE the old paths rather than run beside
    them -- must break this test rather than quietly double the night's work.
    """

    PIPELINE_ENTRY_POINTS = (
        "src/athenaeum/librarian.py",
        "src/athenaeum/decision_answers.py",
        "src/athenaeum/merge.py",
        "src/athenaeum/contradictions.py",
    )

    DARK_MODULES = (
        "comparator",
        "verdict_effects",
        "comparator_instruments",
        "supersession",
        "asserter_authority",
        "recompare",
    )

    @pytest.mark.parametrize("entry_point", PIPELINE_ENTRY_POINTS)
    def test_no_pipeline_entry_point_imports_a_phase_2_module(self, entry_point: str) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / entry_point).read_text(encoding="utf-8")
        for module in self.DARK_MODULES:
            assert f"athenaeum.{module}" not in source, (
                f"{entry_point} imports athenaeum.{module} -- athenaeum#715's "
                "cut-over must REPLACE the old split paths, not run beside them"
            )

    def test_the_probe_can_actually_fail(self) -> None:
        # Positive control: the substring check above must be able to detect a
        # real import, or it is a test that cannot fail.
        repo_root = Path(__file__).resolve().parents[1]
        effects = (repo_root / "src/athenaeum/verdict_effects.py").read_text(encoding="utf-8")
        assert "athenaeum.supersession" in effects

    def test_recompare_is_the_only_live_reader_of_the_comparator_gate(self) -> None:
        """``resolve_comparator_enabled`` has exactly ONE caller in ``src/``.

        Matches a CALL (``resolve_comparator_enabled(``), not any textual
        mention -- ``comparator.py`` names the resolver in its docstring
        without reading it, which is the documented gate-belongs-to-the-caller
        split and must not register as wiring.
        """
        repo_root = Path(__file__).resolve().parents[1] / "src" / "athenaeum"
        callers = sorted(
            path.name
            for path in repo_root.glob("*.py")
            if path.name != "config.py"
            and "resolve_comparator_enabled(" in path.read_text(encoding="utf-8")
        )
        assert callers == ["_cmd_merges.py"]
