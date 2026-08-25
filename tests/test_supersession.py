# SPDX-License-Identifier: Apache-2.0
"""Tests for auto-supersession (issue athenaeum#715, phase 2).

Test-class names map to a named precondition from ``athenaeum.supersession``'s
module docstring / the dispatch brief's numbered list (AC1..AC10, plus the
route-(a)/(b)/(c) sub-conditions of AC8 and the two rate limits under AC10).
Offline throughout: no LLM client, no network -- every ``CompareOutcome`` is
constructed directly rather than produced by a live :func:`compare_pages`
call, matching this repo's existing ``tests/test_comparator.py`` convention.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import athenaeum.supersession as supersession_mod
from athenaeum.comparator import (
    VERDICT_CONTRADICTION,
    VERDICT_DUPLICATE,
    ComparatorPage,
    CompareOutcome,
    page_from_text,
)
from athenaeum.dimensions import (
    DEFAULT_REGISTRY,
    Dimension,
    DimensionKind,
    DimensionRegistry,
    LifecycleState,
    NullMeans,
)
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.supersession import (
    SUPERSESSION_APPLIED,
    SUPERSESSION_LEDGER_NAME,
    SUPERSESSION_QUEUE,
    SupersessionDecision,
    SupersessionNotEnactable,
    append_supersession_record,
    decide_supersession,
    enact_supersession,
    read_supersession_records,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

ALICE: dict[str, object] = {"type": "person", "iss": "https://accounts.example.com", "sub": "alice"}
BOB: dict[str, object] = {"type": "person", "iss": "https://accounts.example.com", "sub": "bob"}
CAROL: dict[str, object] = {"type": "person", "iss": "https://accounts.example.com", "sub": "carol"}

WINNER_OBSERVED = date(2026, 6, 10)
LOSER_OBSERVED = date(2026, 6, 1)
WINNER_RECORDED = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
LOSER_RECORDED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

ENABLED_CONFIG: dict[str, Any] = {"librarian": {"auto_supersession_enabled": True}}
DISABLED_CONFIG: dict[str, Any] = {"librarian": {"auto_supersession_enabled": False}}


def _meta(**overrides: object) -> dict[str, object]:
    meta: dict[str, object] = {"name": "probe"}
    meta.update(overrides)
    return meta


def _page(page_id: str, meta: dict[str, object], body: str = "some claim text") -> ComparatorPage:
    """Build a :class:`ComparatorPage` via ``render_frontmatter`` + ``page_from_text``.

    Using real ``date``/``datetime`` Python objects for date-ish fields (never
    plain strings) sidesteps PyYAML's implicit-timestamp-resolver round-trip
    ambiguity entirely -- every parser this module calls
    (``parse_observed_at``, this module's own ``_parse_recorded_at``) already
    accepts ``date``/``datetime`` objects directly.
    """
    text = render_frontmatter(meta) + body + "\n"
    return page_from_text(page_id, text)


def _golden_pair(
    *,
    winner_id: str = "winner-page",
    loser_id: str = "loser-page",
    winner_asserter: dict[str, object] | None = ALICE,
    loser_asserter: dict[str, object] | None = ALICE,
    winner_claim_kind: str | None = "fact",
    loser_claim_kind: str | None = "fact",
    winner_observed_at: date | None = WINNER_OBSERVED,
    loser_observed_at: date | None = LOSER_OBSERVED,
    winner_recorded_at: datetime | None = WINNER_RECORDED,
    loser_recorded_at: datetime | None = LOSER_RECORDED,
    winner_extra: dict[str, object] | None = None,
    loser_extra: dict[str, object] | None = None,
) -> tuple[ComparatorPage, ComparatorPage]:
    """A pair that decides APPLIED via route (a) with every default left alone."""
    winner_meta = _meta(name=winner_id, asserter=winner_asserter)
    if winner_claim_kind is not None:
        winner_meta["claim_kind"] = winner_claim_kind
    if winner_observed_at is not None:
        winner_meta["observed_at"] = winner_observed_at
    if winner_recorded_at is not None:
        winner_meta["recorded_at"] = winner_recorded_at
    if winner_extra:
        winner_meta.update(winner_extra)

    loser_meta = _meta(name=loser_id, asserter=loser_asserter)
    if loser_claim_kind is not None:
        loser_meta["claim_kind"] = loser_claim_kind
    if loser_observed_at is not None:
        loser_meta["observed_at"] = loser_observed_at
    if loser_recorded_at is not None:
        loser_meta["recorded_at"] = loser_recorded_at
    if loser_extra:
        loser_meta.update(loser_extra)

    return _page(winner_id, winner_meta), _page(loser_id, loser_meta)


def _contradiction(passages: list[str] | None = None) -> CompareOutcome:
    return CompareOutcome(
        verdict=VERDICT_CONTRADICTION,
        conflicting_passages=list(passages) if passages is not None else ["the loser's claim"],
    )


def _decide(
    page_a: ComparatorPage,
    page_b: ComparatorPage,
    outcome: CompareOutcome | None = None,
    *,
    wiki_root: Path,
    config: dict[str, Any] | None = ENABLED_CONFIG,
    registry: DimensionRegistry = DEFAULT_REGISTRY,
    now: datetime | None = None,
    live_claims: tuple[ComparatorPage, ...] = (),
) -> SupersessionDecision:
    return decide_supersession(
        page_a,
        page_b,
        outcome if outcome is not None else _contradiction(),
        wiki_root=wiki_root,
        config=config,
        registry=registry,
        now=now,
        live_claims=live_claims,
    )


def _assert_only_blocked(decision: SupersessionDecision, *names: str) -> None:
    """Assert *decision* queued with EXACTLY *names* as the failing conditions."""
    assert decision.action == SUPERSESSION_QUEUE
    assert decision.blocked_by == sorted(names), decision.conditions


# ---------------------------------------------------------------------------
# Golden path sanity (used as the baseline every isolation test perturbs)
# ---------------------------------------------------------------------------


class TestGoldenPathAppliesViaRouteA:
    def test_all_conditions_pass_and_applies(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.action == SUPERSESSION_APPLIED
        assert decision.winner_id == "winner-page"
        assert decision.loser_id == "loser-page"
        assert decision.located_passages == ["the loser's claim"]
        assert decision.blocked_by == []
        assert decision.rate_limited is None
        assert "route (a)" in decision.reason
        assert all(decision.conditions[name] for name in supersession_mod._DRIVING_CONDITIONS)

    def test_order_independent_a_b_swap(self, tmp_path: Path) -> None:
        """Passing (loser, winner) instead of (winner, loser) reaches the same verdict."""
        winner, loser = _golden_pair()
        decision = _decide(loser, winner, wiki_root=tmp_path)
        assert decision.action == SUPERSESSION_APPLIED
        assert decision.winner_id == "winner-page"
        assert decision.loser_id == "loser-page"


# ---------------------------------------------------------------------------
# AC1 -- auto_supersession_enabled
# ---------------------------------------------------------------------------


class TestConditionAutoSupersessionEnabled:
    def test_disabled_config_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, config=DISABLED_CONFIG)
        assert decision.conditions["auto_supersession_enabled"] is False
        _assert_only_blocked(decision, "auto_supersession_enabled")

    def test_none_config_defaults_off_and_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, config=None)
        assert decision.conditions["auto_supersession_enabled"] is False
        _assert_only_blocked(decision, "auto_supersession_enabled")

    def test_enabled_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_AUTO_SUPERSESSION_ENABLED", "1")
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, config=None)
        assert decision.conditions["auto_supersession_enabled"] is True
        assert decision.action == SUPERSESSION_APPLIED


# ---------------------------------------------------------------------------
# AC2 -- verdict_is_contradiction
# ---------------------------------------------------------------------------


class TestConditionVerdictIsContradiction:
    def test_non_contradiction_verdict_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        outcome = CompareOutcome(verdict=VERDICT_DUPLICATE, conflicting_passages=["irrelevant"])
        decision = _decide(winner, loser, outcome, wiki_root=tmp_path)
        _assert_only_blocked(decision, "verdict_is_contradiction")

    def test_none_verdict_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        outcome = CompareOutcome(verdict=None, conflicting_passages=["irrelevant"])
        decision = _decide(winner, loser, outcome, wiki_root=tmp_path)
        _assert_only_blocked(decision, "verdict_is_contradiction")


# ---------------------------------------------------------------------------
# AC3 -- located (never a page-global retirement)
# ---------------------------------------------------------------------------


class TestConditionLocated:
    def test_empty_conflicting_passages_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        outcome = _contradiction(passages=[])
        decision = _decide(winner, loser, outcome, wiki_root=tmp_path)
        _assert_only_blocked(decision, "located")
        assert decision.located_passages == []

    def test_nonempty_passages_pass(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, _contradiction(["a", "b"]), wiki_root=tmp_path)
        assert decision.conditions["located"] is True
        assert decision.located_passages == ["a", "b"]


# ---------------------------------------------------------------------------
# AC4 -- standing_state (both sides' claim_kind)
# ---------------------------------------------------------------------------


class TestConditionStandingState:
    def test_loser_observation_claim_kind_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(loser_claim_kind="observation")
        decision = _decide(winner, loser, wiki_root=tmp_path)
        _assert_only_blocked(decision, "standing_state")

    def test_winner_opinion_claim_kind_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_claim_kind="opinion")
        decision = _decide(winner, loser, wiki_root=tmp_path)
        _assert_only_blocked(decision, "standing_state")

    def test_unclassified_claim_kind_fails_closed(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(loser_claim_kind=None)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        _assert_only_blocked(decision, "standing_state")

    def test_decision_and_policy_kinds_pass(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_claim_kind="decision", loser_claim_kind="policy")
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.conditions["standing_state"] is True
        assert decision.action == SUPERSESSION_APPLIED

    def test_operator_widened_claim_kinds_via_config(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(loser_claim_kind="observation")
        config = dict(ENABLED_CONFIG)
        config["librarian"] = dict(config["librarian"])
        config["librarian"]["standing_state_claim_kinds"] = ["fact", "observation"]
        decision = _decide(winner, loser, wiki_root=tmp_path, config=config)
        assert decision.conditions["standing_state"] is True


# ---------------------------------------------------------------------------
# AC5 -- no_overlaps (partial-overlap conflicts always queue)
# ---------------------------------------------------------------------------


class TestConditionNoOverlaps:
    def test_overlapping_valid_time_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(
            winner_extra={"valid_from": date(2026, 1, 1), "valid_until": date(2026, 1, 31)},
            loser_extra={"valid_from": date(2026, 1, 15), "valid_until": date(2026, 2, 15)},
        )
        decision = _decide(winner, loser, wiki_root=tmp_path)
        _assert_only_blocked(decision, "no_overlaps")

    def test_disjoint_valid_time_passes_no_overlaps(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(
            winner_extra={"valid_from": date(2026, 2, 1), "valid_until": date(2026, 2, 28)},
            loser_extra={"valid_from": date(2026, 1, 1), "valid_until": date(2026, 1, 31)},
        )
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.conditions["no_overlaps"] is True

    def test_identical_coordinates_pass_no_overlaps(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(
            winner_extra={"subject": "acme-headcount"},
            loser_extra={"subject": "acme-headcount"},
        )
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.conditions["no_overlaps"] is True
        assert decision.action == SUPERSESSION_APPLIED


# ---------------------------------------------------------------------------
# AC6 -- observed_time_strictly_later / winner-loser determination
# ---------------------------------------------------------------------------


class TestConditionObservedTimeStrictlyLater:
    def test_equal_observed_time_queues_and_no_winner(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(loser_observed_at=WINNER_OBSERVED)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        _assert_only_blocked(decision, "observed_time_strictly_later")
        assert decision.winner_id is None
        assert decision.loser_id is None

    def test_missing_winner_observed_at_queues_and_no_winner(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_observed_at=None)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        _assert_only_blocked(decision, "observed_time_strictly_later")
        assert decision.winner_id is None

    def test_missing_both_observed_at_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_observed_at=None, loser_observed_at=None)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        _assert_only_blocked(decision, "observed_time_strictly_later")

    def test_earlier_side_never_wins(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        # Swap call order; the LATER observed_at must still win regardless of
        # which positional argument it arrives as.
        decision = _decide(loser, winner, wiki_root=tmp_path)
        assert decision.winner_id == "winner-page"


# ---------------------------------------------------------------------------
# AC7 -- recorded_time_not_earlier
# ---------------------------------------------------------------------------


class TestConditionRecordedTimeNotEarlier:
    def test_winner_recorded_before_loser_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(
            winner_recorded_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            loser_recorded_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        decision = _decide(winner, loser, wiki_root=tmp_path)
        _assert_only_blocked(decision, "recorded_time_not_earlier")

    def test_missing_winner_recorded_at_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_recorded_at=None)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        _assert_only_blocked(decision, "recorded_time_not_earlier")

    def test_missing_loser_recorded_at_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(loser_recorded_at=None)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        _assert_only_blocked(decision, "recorded_time_not_earlier")

    def test_equal_recorded_at_passes(self, tmp_path: Path) -> None:
        same = datetime(2026, 6, 1, tzinfo=timezone.utc)
        winner, loser = _golden_pair(winner_recorded_at=same, loser_recorded_at=same)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.conditions["recorded_time_not_earlier"] is True
        assert decision.action == SUPERSESSION_APPLIED


# ---------------------------------------------------------------------------
# AC8 -- asserter_route, and its three sub-routes
# ---------------------------------------------------------------------------


class TestConditionAsserterRouteA:
    def test_same_asserter_passes_route_a(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_asserter=ALICE, loser_asserter=ALICE)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.conditions["route_a_same_asserter"] is True
        assert decision.action == SUPERSESSION_APPLIED

    def test_different_asserter_fails_route_a_only(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_asserter=ALICE, loser_asserter=BOB)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.conditions["route_a_same_asserter"] is False
        # No grants on either side -> EQUAL authority -> route (b) also fails,
        # and no live_claims -> route (c) also fails -> the whole precondition
        # (AC8) fails, isolated from every other precondition.
        _assert_only_blocked(decision, "asserter_route")


class TestConditionAsserterRouteB:
    def test_strictly_greater_authority_passes_route_b(self, tmp_path: Path) -> None:
        winner_asserter = {**ALICE, "grants": ["reader", "editor"]}
        loser_asserter = {**BOB, "grants": ["reader"]}
        winner, loser = _golden_pair(winner_asserter=winner_asserter, loser_asserter=loser_asserter)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.conditions["route_a_same_asserter"] is False
        assert decision.conditions["route_b_greater_authority"] is True
        assert decision.action == SUPERSESSION_APPLIED
        assert "route (b)" in decision.reason

    def test_lesser_authority_does_not_grant_route_b(self, tmp_path: Path) -> None:
        winner_asserter = {**ALICE, "grants": ["reader"]}
        loser_asserter = {**BOB, "grants": ["reader", "editor"]}
        winner, loser = _golden_pair(winner_asserter=winner_asserter, loser_asserter=loser_asserter)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.conditions["route_b_greater_authority"] is False
        _assert_only_blocked(decision, "asserter_route")

    def test_incomparable_grants_do_not_grant_route_b(self, tmp_path: Path) -> None:
        winner_asserter = {**ALICE, "grants": ["billing"]}
        loser_asserter = {**BOB, "grants": ["deploy"]}
        winner, loser = _golden_pair(winner_asserter=winner_asserter, loser_asserter=loser_asserter)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.conditions["route_b_greater_authority"] is False
        _assert_only_blocked(decision, "asserter_route")


class TestConditionAsserterRouteC:
    def test_equal_authority_with_independent_corroboration_passes_route_c(
        self, tmp_path: Path
    ) -> None:
        winner, loser = _golden_pair(winner_asserter=ALICE, loser_asserter=BOB)
        corroborator = _page("third-page", _meta(name="third-page", asserter=CAROL))
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(corroborator,))
        assert decision.conditions["route_a_same_asserter"] is False
        assert decision.conditions["route_b_greater_authority"] is False
        assert decision.conditions["route_c_corroborated"] is True
        assert decision.conditions["no_third_conflicting_live_claim"] is True
        assert decision.action == SUPERSESSION_APPLIED
        assert "route (c)" in decision.reason

    def test_incomparable_authority_with_corroboration_also_passes_route_c(
        self, tmp_path: Path
    ) -> None:
        winner_asserter = {**ALICE, "grants": ["billing"]}
        loser_asserter = {**BOB, "grants": ["deploy"]}
        winner, loser = _golden_pair(winner_asserter=winner_asserter, loser_asserter=loser_asserter)
        corroborator = _page("third-page", _meta(name="third-page", asserter=CAROL))
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(corroborator,))
        assert decision.conditions["route_c_corroborated"] is True
        assert decision.action == SUPERSESSION_APPLIED

    def test_no_corroborating_claim_fails_route_c(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_asserter=ALICE, loser_asserter=BOB)
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=())
        assert decision.conditions["route_c_corroborated"] is False
        _assert_only_blocked(decision, "asserter_route")

    def test_corroborator_from_winner_asserter_does_not_count(self, tmp_path: Path) -> None:
        """A live claim from the WINNER's own identity is not "independent"."""
        winner, loser = _golden_pair(winner_asserter=ALICE, loser_asserter=BOB)
        not_independent = _page("third-page", _meta(name="third-page", asserter=ALICE))
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(not_independent,))
        assert decision.conditions["route_c_corroborated"] is False

    def test_corroborator_at_different_coordinates_does_not_count(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(
            winner_asserter=ALICE,
            loser_asserter=BOB,
            winner_extra={"subject": "acme-headcount"},
        )
        elsewhere = _page(
            "third-page", _meta(name="third-page", asserter=CAROL, subject="unrelated-topic")
        )
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(elsewhere,))
        assert decision.conditions["route_c_corroborated"] is False

    def test_superseded_corroborator_does_not_count(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_asserter=ALICE, loser_asserter=BOB)
        retired = _page(
            "third-page", _meta(name="third-page", asserter=CAROL, superseded_by="someone-else")
        )
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(retired,))
        assert decision.conditions["route_c_corroborated"] is False

    def test_unknown_asserter_identity_never_grants_route_c(self, tmp_path: Path) -> None:
        """Doubt never grants the permissive route -- an identity-less
        candidate compares "unknown", never "different", to either side."""
        winner, loser = _golden_pair(winner_asserter=ALICE, loser_asserter=BOB)
        no_identity = _page("third-page", _meta(name="third-page"))
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(no_identity,))
        assert decision.conditions["route_c_corroborated"] is False


# ---------------------------------------------------------------------------
# AC9 -- no_third_conflicting_live_claim
# ---------------------------------------------------------------------------


class TestConditionNoThirdConflictingLiveClaim:
    def test_third_claim_from_losers_asserter_queues(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        echo_of_loser = _page("third-page", _meta(name="third-page", asserter=ALICE))
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(echo_of_loser,))
        assert decision.conditions["no_third_conflicting_live_claim"] is False
        _assert_only_blocked(decision, "no_third_conflicting_live_claim")

    def test_third_claim_with_unknown_identity_queues_on_doubt(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        no_identity = _page("third-page", _meta(name="third-page"))
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(no_identity,))
        assert decision.conditions["no_third_conflicting_live_claim"] is False

    def test_third_claim_excluded_when_superseded(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        retired_echo = _page(
            "third-page", _meta(name="third-page", asserter=ALICE, superseded_by="somebody")
        )
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(retired_echo,))
        assert decision.conditions["no_third_conflicting_live_claim"] is True
        assert decision.action == SUPERSESSION_APPLIED

    def test_third_claim_excluded_when_not_at_same_coordinates(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_extra={"subject": "acme-headcount"})
        elsewhere = _page(
            "third-page", _meta(name="third-page", asserter=ALICE, subject="other-topic")
        )
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(elsewhere,))
        assert decision.conditions["no_third_conflicting_live_claim"] is True

    def test_the_two_pages_under_comparison_are_excluded_from_the_pool(
        self, tmp_path: Path
    ) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, live_claims=(winner, loser))
        assert decision.conditions["no_third_conflicting_live_claim"] is True
        assert decision.action == SUPERSESSION_APPLIED


# ---------------------------------------------------------------------------
# AC10 -- rate limits (route (a) only)
# ---------------------------------------------------------------------------


class TestRateLimitPerClaim:
    def test_one_prior_self_revision_still_applies(self, tmp_path: Path) -> None:
        """The first two same-asserter self-revisions auto-apply -- this
        call is the SECOND (one prior record already in the ledger)."""
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        append_supersession_record(
            tmp_path,
            {
                "action": SUPERSESSION_APPLIED,
                "loser_id": "loser-page",
                "winner_asserter_key": ["https://accounts.example.com", "alice"],
                "at": (now - timedelta(days=1)).isoformat(timespec="seconds"),
            },
        )
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now)
        assert decision.conditions["rate_limit_per_claim"] is True
        assert decision.action == SUPERSESSION_APPLIED

    def test_third_self_revision_within_window_queues(self, tmp_path: Path) -> None:
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for i in range(2):
            append_supersession_record(
                tmp_path,
                {
                    "action": SUPERSESSION_APPLIED,
                    "loser_id": "loser-page",
                    "winner_asserter_key": ["https://accounts.example.com", "alice"],
                    "at": (now - timedelta(days=1 + i)).isoformat(timespec="seconds"),
                },
            )
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now)
        assert decision.rate_limited == "per-claim"
        assert decision.conditions["rate_limit_per_claim"] is False
        assert decision.action == SUPERSESSION_QUEUE

    def test_prior_revision_outside_window_does_not_count(self, tmp_path: Path) -> None:
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for _ in range(3):
            append_supersession_record(
                tmp_path,
                {
                    "action": SUPERSESSION_APPLIED,
                    "loser_id": "loser-page",
                    "winner_asserter_key": ["https://accounts.example.com", "alice"],
                    "at": (now - timedelta(days=200)).isoformat(timespec="seconds"),
                },
            )
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now)
        assert decision.conditions["rate_limit_per_claim"] is True
        assert decision.action == SUPERSESSION_APPLIED

    def test_prior_revision_of_a_different_loser_does_not_count(self, tmp_path: Path) -> None:
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for _ in range(3):
            append_supersession_record(
                tmp_path,
                {
                    "action": SUPERSESSION_APPLIED,
                    "loser_id": "some-other-page",
                    "winner_asserter_key": ["https://accounts.example.com", "alice"],
                    "at": (now - timedelta(days=1)).isoformat(timespec="seconds"),
                },
            )
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now)
        assert decision.conditions["rate_limit_per_claim"] is True

    def test_queued_records_never_counted(self, tmp_path: Path) -> None:
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for _ in range(5):
            append_supersession_record(
                tmp_path,
                {
                    "action": SUPERSESSION_QUEUE,
                    "loser_id": "loser-page",
                    "winner_asserter_key": ["https://accounts.example.com", "alice"],
                    "at": (now - timedelta(days=1)).isoformat(timespec="seconds"),
                },
            )
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now)
        assert decision.conditions["rate_limit_per_claim"] is True
        assert decision.action == SUPERSESSION_APPLIED

    def test_configurable_window_and_max(self, tmp_path: Path) -> None:
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        append_supersession_record(
            tmp_path,
            {
                "action": SUPERSESSION_APPLIED,
                "loser_id": "loser-page",
                "winner_asserter_key": ["https://accounts.example.com", "alice"],
                "at": (now - timedelta(days=1)).isoformat(timespec="seconds"),
            },
        )
        config = {
            "librarian": {
                "auto_supersession_enabled": True,
                "supersession_claim_window_max": 2,
            }
        }
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, config=config, now=now)
        assert decision.conditions["rate_limit_per_claim"] is False
        assert decision.rate_limited == "per-claim"

    def test_route_a_not_attempted_never_computes_rate_limits(self, tmp_path: Path) -> None:
        """Different asserters -> route (a) inapplicable -> both caps ``True``
        (not applicable), even with a saturated ledger for a DIFFERENT pair."""
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for _ in range(20):
            append_supersession_record(
                tmp_path,
                {
                    "action": SUPERSESSION_APPLIED,
                    "loser_id": "loser-page",
                    "winner_asserter_key": ["https://accounts.example.com", "alice"],
                    "at": (now - timedelta(days=1)).isoformat(timespec="seconds"),
                },
            )
        winner, loser = _golden_pair(winner_asserter=ALICE, loser_asserter=BOB)
        corroborator = _page("third-page", _meta(name="third-page", asserter=CAROL))
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now, live_claims=(corroborator,))
        assert decision.conditions["rate_limit_per_claim"] is True
        assert decision.conditions["rate_limit_per_asserter"] is True
        assert decision.rate_limited is None


class TestRateLimitPerAsserter:
    def test_nine_prior_applies_this_week_still_applies(self, tmp_path: Path) -> None:
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for i in range(9):
            append_supersession_record(
                tmp_path,
                {
                    "action": SUPERSESSION_APPLIED,
                    "loser_id": f"other-loser-{i}",
                    "winner_asserter_key": ["https://accounts.example.com", "alice"],
                    "at": (now - timedelta(hours=i + 1)).isoformat(timespec="seconds"),
                },
            )
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now)
        assert decision.conditions["rate_limit_per_asserter"] is True
        assert decision.action == SUPERSESSION_APPLIED

    def test_tenth_prior_apply_this_week_suspends_route_a(self, tmp_path: Path) -> None:
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for i in range(10):
            append_supersession_record(
                tmp_path,
                {
                    "action": SUPERSESSION_APPLIED,
                    "loser_id": f"other-loser-{i}",
                    "winner_asserter_key": ["https://accounts.example.com", "alice"],
                    "at": (now - timedelta(hours=i + 1)).isoformat(timespec="seconds"),
                },
            )
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now)
        assert decision.conditions["rate_limit_per_asserter"] is False
        assert decision.rate_limited == "per-asserter"
        assert decision.action == SUPERSESSION_QUEUE

    def test_prior_apply_outside_trailing_week_does_not_count(self, tmp_path: Path) -> None:
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for i in range(20):
            append_supersession_record(
                tmp_path,
                {
                    "action": SUPERSESSION_APPLIED,
                    "loser_id": f"other-loser-{i}",
                    "winner_asserter_key": ["https://accounts.example.com", "alice"],
                    "at": (now - timedelta(days=8)).isoformat(timespec="seconds"),
                },
            )
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now)
        assert decision.conditions["rate_limit_per_asserter"] is True

    def test_prior_apply_from_a_different_asserter_does_not_count(self, tmp_path: Path) -> None:
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for i in range(20):
            append_supersession_record(
                tmp_path,
                {
                    "action": SUPERSESSION_APPLIED,
                    "loser_id": f"other-loser-{i}",
                    "winner_asserter_key": ["https://accounts.example.com", "bob"],
                    "at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                },
            )
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now)
        assert decision.conditions["rate_limit_per_asserter"] is True

    def test_per_claim_pass_does_not_imply_per_asserter_pass(self, tmp_path: Path) -> None:
        """Ten distinct losers, same asserter, all this week -- per-claim is
        fine (no repeats of THIS loser) but per-asserter trips."""
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for i in range(10):
            append_supersession_record(
                tmp_path,
                {
                    "action": SUPERSESSION_APPLIED,
                    "loser_id": f"other-loser-{i}",
                    "winner_asserter_key": ["https://accounts.example.com", "alice"],
                    "at": (now - timedelta(hours=i + 1)).isoformat(timespec="seconds"),
                },
            )
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, now=now)
        assert decision.conditions["rate_limit_per_claim"] is True
        assert decision.conditions["rate_limit_per_asserter"] is False


# ---------------------------------------------------------------------------
# conditions dict is ALWAYS fully populated; blocked_by / reason shape
# ---------------------------------------------------------------------------


class TestConditionsAlwaysFullyPopulated:
    _EXPECTED_KEYS = frozenset(
        {
            "auto_supersession_enabled",
            "verdict_is_contradiction",
            "located",
            "standing_state",
            "no_overlaps",
            "observed_time_strictly_later",
            "recorded_time_not_earlier",
            "asserter_route",
            "no_third_conflicting_live_claim",
            "rate_limit_per_claim",
            "rate_limit_per_asserter",
            "route_a_same_asserter",
            "route_b_greater_authority",
            "route_c_corroborated",
        }
    )

    def test_applied_decision_has_every_key(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert set(decision.conditions) == self._EXPECTED_KEYS
        assert all(isinstance(v, bool) for v in decision.conditions.values())

    def test_queued_decision_has_every_key_too(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, config=DISABLED_CONFIG)
        assert set(decision.conditions) == self._EXPECTED_KEYS
        assert all(isinstance(v, bool) for v in decision.conditions.values())

    def test_maximally_broken_pair_still_fully_populates(self, tmp_path: Path) -> None:
        """Every precondition fails simultaneously -- conditions must still be
        a complete, all-bool dict, and blocked_by must list every failure."""
        winner, loser = _golden_pair(
            winner_claim_kind="opinion",
            loser_claim_kind="observation",
            winner_asserter=BOB,
            loser_asserter=None,
            winner_observed_at=None,
            loser_observed_at=None,
            winner_recorded_at=None,
            loser_recorded_at=None,
        )
        decision = _decide(
            winner,
            loser,
            CompareOutcome(verdict=VERDICT_DUPLICATE, conflicting_passages=[]),
            wiki_root=tmp_path,
            config=DISABLED_CONFIG,
        )
        assert set(decision.conditions) == self._EXPECTED_KEYS
        assert decision.action == SUPERSESSION_QUEUE
        assert "auto_supersession_enabled" in decision.blocked_by
        assert "verdict_is_contradiction" in decision.blocked_by
        assert "located" in decision.blocked_by
        assert "standing_state" in decision.blocked_by
        assert "observed_time_strictly_later" in decision.blocked_by
        assert "recorded_time_not_earlier" in decision.blocked_by


class TestBlockedByAndReason:
    def test_applied_blocked_by_is_empty(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.blocked_by == []

    def test_blocked_by_is_sorted(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(loser_claim_kind="observation")
        decision = _decide(winner, loser, wiki_root=tmp_path, config=DISABLED_CONFIG)
        assert decision.blocked_by == sorted(decision.blocked_by)
        assert decision.blocked_by == ["auto_supersession_enabled", "standing_state"]

    def test_route_sub_booleans_never_appear_in_blocked_by(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair(winner_asserter=ALICE, loser_asserter=BOB)
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert "route_a_same_asserter" not in decision.blocked_by
        assert "route_b_greater_authority" not in decision.blocked_by
        assert "route_c_corroborated" not in decision.blocked_by
        assert "asserter_route" in decision.blocked_by

    def test_queued_reason_names_blocked_conditions(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, config=DISABLED_CONFIG)
        assert "auto_supersession_enabled" in decision.reason


# ---------------------------------------------------------------------------
# No confidence thresholds / scalars anywhere, no LLM client parameter
# ---------------------------------------------------------------------------


class TestNoConfidenceThresholdsAnywhere:
    def test_module_source_names_no_threshold_or_confidence_constant(self) -> None:
        names = [
            name
            for name in dir(supersession_mod)
            if ("THRESHOLD" in name.upper() or "CONFIDENCE" in name.upper())
        ]
        assert names == []

    def test_decision_dataclass_has_no_confidence_or_score_field(self) -> None:
        field_names = {f.name for f in fields(SupersessionDecision)}
        assert not any("confidence" in n.lower() for n in field_names)
        assert not any("score" in n.lower() for n in field_names)

    def test_decide_supersession_takes_no_llm_client(self) -> None:
        sig = inspect.signature(decide_supersession)
        assert "client" not in sig.parameters

    def test_module_never_imports_a_provider_or_llm_module(self) -> None:
        src = inspect.getsource(supersession_mod)
        assert "athenaeum.provider" not in src
        assert "anthropic" not in src.lower()


# ---------------------------------------------------------------------------
# The silent-no-op trap
# ---------------------------------------------------------------------------


class TestSilentNoOpTrap:
    def test_enactment_can_never_silently_no_op(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.action == SUPERSESSION_APPLIED
        assert decision.located_passages != []
        assert decision.winner_id is not None
        assert decision.loser_id is not None

        # A well-formed applied decision, but the loser path does not exist:
        # enact_supersession must RAISE, never return None.
        with pytest.raises(SupersessionNotEnactable):
            enact_supersession(
                decision,
                loser_path=tmp_path / "does-not-exist.md",
                winner_id=decision.winner_id,
                wiki_root=tmp_path,
            )

    def test_no_observed_time_never_reaches_enactment_default_registry(
        self, tmp_path: Path
    ) -> None:
        winner, loser = _golden_pair(winner_observed_at=None, loser_observed_at=None)
        decision = _decide(winner, loser, wiki_root=tmp_path, registry=DEFAULT_REGISTRY)
        assert decision.action == SUPERSESSION_QUEUE
        assert decision.conditions["observed_time_strictly_later"] is False

    def test_no_observed_time_never_reaches_enactment_with_a_backfill_dimension(
        self, tmp_path: Path
    ) -> None:
        backfill_dim = Dimension(
            name="engagement-tier",
            kind=DimensionKind.ENUM,
            values=("low", "high"),
            null_means=NullMeans.UNKNOWN,
            separates=True,
            state=LifecycleState.BACKFILL,
            origin="operator",
        )
        registry = DimensionRegistry(dimensions=DEFAULT_REGISTRY.dimensions + (backfill_dim,))
        winner, loser = _golden_pair(winner_observed_at=None, loser_observed_at=None)
        decision = _decide(winner, loser, wiki_root=tmp_path, registry=registry)
        assert decision.action == SUPERSESSION_QUEUE
        assert decision.conditions["observed_time_strictly_later"] is False

    def test_manually_constructed_broken_applied_decision_raises(self, tmp_path: Path) -> None:
        """Guards against a caller hand-building a malformed decision, not
        just against decide_supersession's own outputs."""
        broken = SupersessionDecision(
            action=SUPERSESSION_APPLIED,
            winner_id="winner-page",
            loser_id="loser-page",
            located_passages=[],
            conditions={},
            blocked_by=[],
            reason="hand-built",
        )
        loser_file = tmp_path / "loser.md"
        loser_file.write_text(render_frontmatter({"name": "loser-page"}) + "body\n")
        with pytest.raises(SupersessionNotEnactable):
            enact_supersession(
                broken, loser_path=loser_file, winner_id="winner-page", wiki_root=tmp_path
            )

    def test_enact_never_returns_none_on_the_queue_path(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, config=DISABLED_CONFIG)
        assert decision.action == SUPERSESSION_QUEUE
        loser_file = tmp_path / "loser.md"
        loser_file.write_text(render_frontmatter({"name": "loser-page"}) + "body\n")
        with pytest.raises(SupersessionNotEnactable):
            enact_supersession(
                decision, loser_path=loser_file, winner_id="winner-page", wiki_root=tmp_path
            )


# ---------------------------------------------------------------------------
# enact_supersession -- success path, frontmatter shape, ledger write
# ---------------------------------------------------------------------------


class TestEnactSupersessionSuccess:
    def _write_loser_file(self, tmp_path: Path) -> Path:
        loser_file = tmp_path / "loser.md"
        meta = {"name": "loser-page", "claim_kind": "fact", "other_field": "preserved"}
        loser_file.write_text(render_frontmatter(meta) + "the loser's claim body\n")
        return loser_file

    def test_writes_superseded_fields_and_preserves_body(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        assert decision.action == SUPERSESSION_APPLIED
        loser_file = self._write_loser_file(tmp_path)

        result_path = enact_supersession(
            decision,
            loser_path=loser_file,
            winner_id=decision.winner_id,
            wiki_root=tmp_path,
            now=datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc),
            winner_meta=winner.meta,
        )
        assert result_path == loser_file

        text = loser_file.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        assert meta["superseded_by"] == "winner-page"
        assert meta["superseded_claim"] == decision.located_passages
        assert meta["superseded_at"] == "2026-07-04T12:00:00+00:00"
        assert meta["other_field"] == "preserved"
        assert meta["name"] == "loser-page"
        assert "the loser's claim body" in body

    def test_never_returns_none(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        loser_file = self._write_loser_file(tmp_path)
        result = enact_supersession(
            decision, loser_path=loser_file, winner_id=decision.winner_id, wiki_root=tmp_path
        )
        assert result is not None
        assert isinstance(result, Path)

    def test_appends_applied_record_to_ledger(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        loser_file = self._write_loser_file(tmp_path)
        enact_supersession(
            decision,
            loser_path=loser_file,
            winner_id=decision.winner_id,
            wiki_root=tmp_path,
            winner_meta=winner.meta,
        )
        records = read_supersession_records(tmp_path)
        assert len(records) == 1
        assert records[0]["action"] == SUPERSESSION_APPLIED
        assert records[0]["winner_id"] == "winner-page"
        assert records[0]["loser_id"] == "loser-page"
        assert records[0]["winner_asserter_key"] == ["https://accounts.example.com", "alice"]
        assert records[0]["located_passages"] == decision.located_passages

    def test_omitted_winner_meta_records_empty_asserter_key(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        loser_file = self._write_loser_file(tmp_path)
        enact_supersession(
            decision, loser_path=loser_file, winner_id=decision.winner_id, wiki_root=tmp_path
        )
        records = read_supersession_records(tmp_path)
        assert records[0]["winner_asserter_key"] == []

    def test_never_touches_the_winner_file(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        winner_file = tmp_path / "winner.md"
        winner_file.write_text(render_frontmatter({"name": "winner-page"}) + "winner body\n")
        original = winner_file.read_text(encoding="utf-8")
        loser_file = self._write_loser_file(tmp_path)

        enact_supersession(
            decision, loser_path=loser_file, winner_id=decision.winner_id, wiki_root=tmp_path
        )

        assert winner_file.read_text(encoding="utf-8") == original

    def test_ledger_file_is_named_correctly(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        loser_file = self._write_loser_file(tmp_path)
        enact_supersession(
            decision, loser_path=loser_file, winner_id=decision.winner_id, wiki_root=tmp_path
        )
        assert (tmp_path / SUPERSESSION_LEDGER_NAME).exists()
        assert SUPERSESSION_LEDGER_NAME == "_supersessions.jsonl"


# ---------------------------------------------------------------------------
# enact_supersession -- failure paths
# ---------------------------------------------------------------------------


class TestEnactSupersessionRaises:
    def test_raises_on_queue_decision(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path, config=DISABLED_CONFIG)
        loser_file = tmp_path / "loser.md"
        loser_file.write_text(render_frontmatter({"name": "loser-page"}) + "body\n")
        with pytest.raises(SupersessionNotEnactable):
            enact_supersession(
                decision, loser_path=loser_file, winner_id="winner-page", wiki_root=tmp_path
            )

    def test_raises_when_loser_path_missing(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        with pytest.raises(SupersessionNotEnactable):
            enact_supersession(
                decision,
                loser_path=tmp_path / "missing.md",
                winner_id=decision.winner_id,
                wiki_root=tmp_path,
            )

    def test_raises_when_write_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        loser_file = tmp_path / "loser.md"
        loser_file.write_text(render_frontmatter({"name": "loser-page"}) + "body\n")

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(supersession_mod, "atomic_write_text", _boom)
        with pytest.raises(SupersessionNotEnactable):
            enact_supersession(
                decision, loser_path=loser_file, winner_id=decision.winner_id, wiki_root=tmp_path
            )
        # And no ledger record was appended for a write that never landed.
        assert read_supersession_records(tmp_path) == []

    def test_raises_when_located_passages_empty(self, tmp_path: Path) -> None:
        broken = SupersessionDecision(
            action=SUPERSESSION_APPLIED,
            winner_id="w",
            loser_id="l",
            located_passages=[],
            conditions={},
            blocked_by=[],
            reason="hand-built",
        )
        loser_file = tmp_path / "loser.md"
        loser_file.write_text(render_frontmatter({"name": "l"}) + "body\n")
        with pytest.raises(SupersessionNotEnactable):
            enact_supersession(broken, loser_path=loser_file, winner_id="w", wiki_root=tmp_path)

    def test_raises_when_winner_id_none_on_decision(self, tmp_path: Path) -> None:
        broken = SupersessionDecision(
            action=SUPERSESSION_APPLIED,
            winner_id=None,
            loser_id="l",
            located_passages=["x"],
            conditions={},
            blocked_by=[],
            reason="hand-built",
        )
        loser_file = tmp_path / "loser.md"
        loser_file.write_text(render_frontmatter({"name": "l"}) + "body\n")
        with pytest.raises(SupersessionNotEnactable):
            enact_supersession(broken, loser_path=loser_file, winner_id="w", wiki_root=tmp_path)

    def test_raises_when_loser_id_none_on_decision(self, tmp_path: Path) -> None:
        broken = SupersessionDecision(
            action=SUPERSESSION_APPLIED,
            winner_id="w",
            loser_id=None,
            located_passages=["x"],
            conditions={},
            blocked_by=[],
            reason="hand-built",
        )
        loser_file = tmp_path / "loser.md"
        loser_file.write_text(render_frontmatter({"name": "l"}) + "body\n")
        with pytest.raises(SupersessionNotEnactable):
            enact_supersession(broken, loser_path=loser_file, winner_id="w", wiki_root=tmp_path)


# ---------------------------------------------------------------------------
# append_supersession_record / read_supersession_records
# ---------------------------------------------------------------------------


class TestAppendReadSupersessionRecords:
    def test_read_empty_when_absent(self, tmp_path: Path) -> None:
        assert read_supersession_records(tmp_path) == []

    def test_round_trip(self, tmp_path: Path) -> None:
        record = {"action": "queue", "winner_id": None, "loser_id": None, "at": "2026-01-01"}
        path = append_supersession_record(tmp_path, record)
        assert path == tmp_path / SUPERSESSION_LEDGER_NAME
        records = read_supersession_records(tmp_path)
        assert records == [record]

    def test_multiple_appends_preserve_order(self, tmp_path: Path) -> None:
        for i in range(5):
            append_supersession_record(tmp_path, {"action": "applied", "n": i})
        records = read_supersession_records(tmp_path)
        assert [r["n"] for r in records] == [0, 1, 2, 3, 4]

    def test_tolerant_of_torn_or_malformed_lines(self, tmp_path: Path) -> None:
        path = tmp_path / SUPERSESSION_LEDGER_NAME
        path.write_text(
            json.dumps({"action": "applied", "n": 1})
            + "\n"
            + "{not valid json\n"
            + "\n"
            + json.dumps({"action": "applied", "n": 2})
            + "\n"
        )
        records = read_supersession_records(tmp_path)
        assert [r["n"] for r in records] == [1, 2]

    def test_all_io_stays_under_wiki_root(self, tmp_path: Path) -> None:
        sub_root = tmp_path / "wiki"
        sub_root.mkdir()
        append_supersession_record(sub_root, {"action": "applied"})
        assert (sub_root / SUPERSESSION_LEDGER_NAME).exists()
        assert not (tmp_path / SUPERSESSION_LEDGER_NAME).exists()


# ---------------------------------------------------------------------------
# Public API shape -- the exact names other lanes code against
# ---------------------------------------------------------------------------


class TestPublicAPIShape:
    def test_exported_names(self) -> None:
        assert set(supersession_mod.__all__) == {
            "SUPERSESSION_APPLIED",
            "SUPERSESSION_LEDGER_NAME",
            "SUPERSESSION_QUEUE",
            "SupersessionDecision",
            "SupersessionNotEnactable",
            "append_supersession_record",
            "decide_supersession",
            "enact_supersession",
            "read_supersession_records",
        }

    def test_constants(self) -> None:
        assert SUPERSESSION_APPLIED == "applied"
        assert SUPERSESSION_QUEUE == "queue"
        assert SUPERSESSION_LEDGER_NAME == "_supersessions.jsonl"

    def test_decide_supersession_signature(self) -> None:
        sig = inspect.signature(decide_supersession)
        assert set(sig.parameters) == {
            "page_a",
            "page_b",
            "outcome",
            "wiki_root",
            "config",
            "registry",
            "now",
            "live_claims",
        }
        assert sig.parameters["config"].default is None
        assert sig.parameters["now"].default is None
        assert sig.parameters["live_claims"].default == ()

    def test_enact_supersession_documented_five_argument_shape_still_works(
        self, tmp_path: Path
    ) -> None:
        """The exact five-argument call shape from the dispatch brief (no
        winner_meta) must keep working -- winner_meta is additive."""
        winner, loser = _golden_pair()
        decision = _decide(winner, loser, wiki_root=tmp_path)
        loser_file = tmp_path / "loser.md"
        loser_file.write_text(render_frontmatter({"name": "loser-page"}) + "body\n")
        result = enact_supersession(
            decision,
            loser_path=loser_file,
            winner_id=decision.winner_id,
            wiki_root=tmp_path,
            now=None,
        )
        assert result == loser_file

    def test_supersession_decision_dataclass_fields(self) -> None:
        field_names = {f.name for f in fields(SupersessionDecision)}
        assert field_names == {
            "action",
            "winner_id",
            "loser_id",
            "located_passages",
            "conditions",
            "blocked_by",
            "reason",
            "rate_limited",
        }

    def test_supersession_decision_is_frozen(self, tmp_path: Path) -> None:
        winner, loser = _golden_pair()
        decision = decide_supersession(
            winner, loser, _contradiction(), wiki_root=tmp_path, config=ENABLED_CONFIG
        )
        with pytest.raises(Exception):
            decision.action = SUPERSESSION_QUEUE  # type: ignore[misc]

    def test_not_enactable_is_an_exception(self) -> None:
        assert issubclass(SupersessionNotEnactable, Exception)
