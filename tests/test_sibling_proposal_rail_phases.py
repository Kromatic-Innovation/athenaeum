# SPDX-License-Identifier: Apache-2.0
"""Tests for the self-tuning loop's sibling nightly phases (issue
athenaeum#719 AC11/AC15): ``librarian._run_tier_movement_proposals_phase``
(issue athenaeum#2019) and ``librarian._run_auto_apply_proposals_phase``
(issue athenaeum#2018, now also hosting athenaeum#2219's
attribution-threshold drafter -- see ``TestAttributionThresholdPhase*``
below), both gated by the SAME master key
(``librarian.signal_mining.enabled``) ``_run_signal_mining_phase`` already
uses for ``dimension_proposals.py``.

``_make_ctx`` below reuses ``tests/test_signal_mining_phase.py``'s fixture
shape (read, not imported, per that file's own pattern).

**Driving the tier-movement rail from a test.** The phase deliberately
exposes no test seam -- it calls
``run_tier_movement_proposal_drafting`` with no ``usages=`` kwarg, so the
drafter performs its own push-metrics read
(:func:`athenaeum.usage_report.compute_usage_report`). To reach a REAL draft
through the phase's own call surface, ``_patch_usage_report`` below stands in
for that one read; everything downstream of it -- both config gates, the
threshold comparison, the idempotency check, the ledger append -- is the
production path unmodified. This matters for the dry-run claim specifically:
asserting "no ledger file" on a zero-candidate run proves nothing, so each
dry-run assertion below is paired with a ``dry_run=false`` run over the
IDENTICAL input that does write. The auto-apply sibling needs no such stand-in:
its trigger (:func:`athenaeum.calibration.calibration_summary`) is seeded by
writing directly under ``wiki_root``, which the fixture already provides.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from athenaeum.auto_apply_proposals import (
    AutoApplyProposalRunSummary,
    default_auto_apply_proposals_ledger_path,
    read_auto_apply_proposals_ledger,
)
from athenaeum.calibration import read_calibration_ledger, record_audit_review, sample_tier_decision
from athenaeum.librarian import (
    RunContext,
    _run_auto_apply_proposals_phase,
    _run_tier_movement_proposals_phase,
)
from athenaeum.resolutions import DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION
from athenaeum.tier_movement_proposals import (
    default_tier_movement_proposals_ledger_path,
    read_tier_movement_proposals_ledger,
)
from athenaeum.usage_report import ClaimUsage

_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)

_FULL_SAMPLE_CONFIG = {
    "librarian": {
        "audit_sample_rate_t1_rejects": 1.0,
        "audit_sample_rate_t2_approvals": 1.0,
    }
}


def _make_ctx(tmp_path: Path, **overrides) -> RunContext:
    knowledge_root = overrides.pop("knowledge_root", tmp_path / "knowledge")
    wiki_root = overrides.pop("wiki_root", knowledge_root / "wiki")
    raw_root = overrides.pop("raw_root", knowledge_root / "raw")
    wiki_root.mkdir(parents=True, exist_ok=True)
    defaults = dict(
        raw_root=raw_root,
        wiki_root=wiki_root,
        knowledge_root=knowledge_root,
        dry_run=False,
        max_files=None,
        max_api_calls=None,
        max_runtime=None,
        cluster_only=False,
        merge_only=False,
        strict_budget=False,
        batch_mode=None,
        retire=None,
        push_after_run=None,
        pull_before_run=None,
        projects_root=None,
        install_signal_handlers=False,
        changed_paths=None,
        full_compile=False,
        now=_NOW,
        heartbeat=None,
        out_run_stats=None,
    )
    defaults.update(overrides)
    ctx = RunContext(**defaults)
    ctx.skip_entity_tiers = ctx.cluster_only or ctx.merge_only
    ctx.config = overrides.get("config", {})
    return ctx


def _master_config(
    *, enabled: bool, dry_run: bool | None = None, extra: dict | None = None
) -> dict:
    section: dict = {"enabled": enabled}
    if dry_run is not None:
        section["dry_run"] = dry_run
    librarian_cfg: dict = {"signal_mining": section}
    if extra:
        librarian_cfg.update(extra)
    return {"librarian": librarian_cfg}


def _sample_and_review(wiki_root: Path, *, proposal_id: str, human_verdict: str) -> None:
    sample_tier_decision(
        wiki_root,
        tier="T1",
        verdict="reject",
        proposal_id=proposal_id,
        reason="test",
        config=_FULL_SAMPLE_CONFIG,
    )
    audit_id = next(
        r["id"]
        for r in read_calibration_ledger(wiki_root)
        if r.get("kind") == "audit" and r.get("proposal_id") == proposal_id
    )
    record_audit_review(wiki_root, audit_id=audit_id, human_verdict=human_verdict)


def _seed_t1_disagreement(wiki_root: Path, *, overturned: int, confirmed: int) -> None:
    """Sample + review N T1-reject audits so
    ``auto_apply_proposals.disagreement_rate`` reports a real, non-trivial
    rate -- same pattern as ``tests/test_auto_apply_proposals.py``'s own
    helper, copied (not imported) per this file's docstring."""
    idx = 0
    for _ in range(overturned):
        idx += 1
        _sample_and_review(wiki_root, proposal_id=f"prop-{idx}", human_verdict="approve")
    for _ in range(confirmed):
        idx += 1
        _sample_and_review(wiki_root, proposal_id=f"prop-{idx}", human_verdict="reject")


def _usage(id_: str = "low-value", *, pushed: int = 10, referenced: int = 0) -> ClaimUsage:
    """One push-metrics usage record, shaped exactly as
    ``tests/test_tier_movement_proposals.py``'s own ``_usage`` helper (copied,
    not imported, per this file's docstring)."""
    return ClaimUsage(
        id=id_,
        pushed_count=pushed,
        referenced_count=referenced,
        last_pushed="2026-01-01T00:00:00+00:00",
        last_referenced=None,
    )


def _patch_usage_report(monkeypatch, usages: dict[str, ClaimUsage]) -> None:
    """Make the drafter's own ``compute_usage_report`` return *usages*.

    The phase deliberately passes no ``usages=`` kwarg (it is production
    wiring, not a test seam), so the only honest way to drive a REAL draft
    through the phase's own call surface is to stand in for the push-metrics
    read the drafter performs itself -- ``compute_usage_report``, imported at
    module scope in :mod:`athenaeum.tier_movement_proposals` (its line 71).
    Without this, every tier-movement assertion below would be a zero-candidate
    no-op, and the "dry-run suppresses the write" claim would rest on a control
    that could not have written anything either way.
    """
    import athenaeum.tier_movement_proposals as tmp_mod

    monkeypatch.setattr(
        tmp_mod,
        "compute_usage_report",
        lambda **kwargs: dict(usages),
    )


class TestTierMovementPhaseMasterGate:
    def test_disabled_by_default_leaves_summary_none(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config={})
        _run_tier_movement_proposals_phase(ctx)
        assert ctx.tier_movement_proposals_summary is None

    def test_deadline_tripped_skips_and_records_reason(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config=_master_config(enabled=True))
        ctx.deadline_tripped = True
        _run_tier_movement_proposals_phase(ctx)
        assert ctx.tier_movement_proposals_summary == {"skipped_deadline_tripped": True}


class TestTierMovementPhaseDryRun:
    def test_enabled_dry_run_default_populates_summary_no_ledger(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config=_master_config(enabled=True))
        _run_tier_movement_proposals_phase(ctx)

        assert ctx.tier_movement_proposals_summary is not None
        assert ctx.tier_movement_proposals_summary["dry_run"] is True
        assert ctx.tier_movement_proposals_summary["drafted_ids"] == []
        assert not default_tier_movement_proposals_ledger_path(ctx.wiki_root).exists()

    def test_enabled_dry_run_lists_a_real_draft_without_writing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Dry-run with a candidate that genuinely crosses the threshold: the
        draft is listed in the summary and the ledger is still never written.
        Paired with ``test_enabled_live_writes_a_real_draft`` below, which runs
        the SAME input with ``dry_run=false`` and does write -- so the absence
        here is the dry-run suppressing a write that would otherwise happen,
        not a vacuous no-op.
        """
        _patch_usage_report(monkeypatch, {"low-value": _usage()})
        ctx = _make_ctx(
            tmp_path,
            config=_master_config(
                enabled=True,
                extra={
                    "tier_movement_proposals_enabled": True,
                    "tier_movement_pushed_min": 5,
                    "tier_movement_referenced_max": 0,
                },
            ),
        )
        _run_tier_movement_proposals_phase(ctx)

        summary = ctx.tier_movement_proposals_summary
        assert summary is not None
        assert summary["dry_run"] is True
        assert summary["candidates_seen"] == 1
        assert summary["proposed"] == 0
        assert len(summary["drafted_ids"]) == 1
        assert not default_tier_movement_proposals_ledger_path(ctx.wiki_root).exists()

    def test_enabled_live_writes_a_real_draft(self, tmp_path: Path, monkeypatch) -> None:
        """The negative control for the dry-run test above: identical input,
        ``dry_run=false``, and the proposal IS appended to the ledger."""
        _patch_usage_report(monkeypatch, {"low-value": _usage()})
        ctx = _make_ctx(
            tmp_path,
            config=_master_config(
                enabled=True,
                dry_run=False,
                extra={
                    "tier_movement_proposals_enabled": True,
                    "tier_movement_pushed_min": 5,
                    "tier_movement_referenced_max": 0,
                },
            ),
        )
        _run_tier_movement_proposals_phase(ctx)

        summary = ctx.tier_movement_proposals_summary
        assert summary["dry_run"] is False
        assert summary["candidates_seen"] == 1
        assert summary["proposed"] == 1
        ledger = default_tier_movement_proposals_ledger_path(ctx.wiki_root)
        assert ledger.exists()
        assert len(read_tier_movement_proposals_ledger(ctx.wiki_root)) == 1

    def test_drafters_own_sub_gate_still_governs_when_master_key_is_on(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The master key only decides whether the drafter is INVOKED; the
        drafter's own ``tier_movement_proposals_enabled`` (default OFF) still
        has to be on for anything to be drafted. Same crossing candidate as
        the two tests above, sub-gate left at its default."""
        _patch_usage_report(monkeypatch, {"low-value": _usage()})
        ctx = _make_ctx(tmp_path, config=_master_config(enabled=True, dry_run=False))
        _run_tier_movement_proposals_phase(ctx)

        summary = ctx.tier_movement_proposals_summary
        assert summary is not None
        assert summary["candidates_seen"] == 0
        assert summary["proposed"] == 0
        assert not default_tier_movement_proposals_ledger_path(ctx.wiki_root).exists()

    def test_global_run_dry_run_forces_dry_run_even_if_knob_says_false(
        self, tmp_path: Path
    ) -> None:
        ctx = _make_ctx(
            tmp_path,
            config=_master_config(enabled=True, dry_run=False),
            dry_run=True,
        )
        _run_tier_movement_proposals_phase(ctx)
        assert ctx.tier_movement_proposals_summary["dry_run"] is True


class TestAutoApplyPhaseMasterGate:
    def test_disabled_by_default_leaves_summary_none(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config={})
        _run_auto_apply_proposals_phase(ctx)
        assert ctx.auto_apply_proposals_summary is None

    def test_deadline_tripped_skips_and_records_reason(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config=_master_config(enabled=True))
        ctx.deadline_tripped = True
        _run_auto_apply_proposals_phase(ctx)
        assert ctx.auto_apply_proposals_summary == {"skipped_deadline_tripped": True}


class TestAutoApplyPhaseDryRun:
    def test_enabled_dry_run_default_populates_summary_no_ledger(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "knowledge" / "wiki"
        ctx = _make_ctx(tmp_path, wiki_root=wiki_root, config=_master_config(enabled=True))
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)  # 75% disagreement

        _run_auto_apply_proposals_phase(ctx)

        assert ctx.auto_apply_proposals_summary is not None
        assert ctx.auto_apply_proposals_summary["dry_run"] is True
        assert ctx.auto_apply_proposals_summary["proposed"] == 0
        assert ctx.auto_apply_proposals_summary["drafted_ids"], "expected real drafts in dry-run"
        assert not default_auto_apply_proposals_ledger_path(wiki_root).exists()

    def test_enabled_live_writes_ledger(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "knowledge" / "wiki"
        ctx = _make_ctx(
            tmp_path,
            wiki_root=wiki_root,
            config=_master_config(enabled=True, dry_run=False),
        )
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)  # 75% disagreement

        _run_auto_apply_proposals_phase(ctx)

        assert ctx.auto_apply_proposals_summary["dry_run"] is False
        assert ctx.auto_apply_proposals_summary["proposed"] == len(
            DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION
        )
        records = read_auto_apply_proposals_ledger(wiki_root)
        assert len(records) == len(DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION)

    def test_global_run_dry_run_forces_dry_run_even_if_knob_says_false(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "knowledge" / "wiki"
        ctx = _make_ctx(
            tmp_path,
            wiki_root=wiki_root,
            config=_master_config(enabled=True, dry_run=False),
            dry_run=True,
        )
        _seed_t1_disagreement(wiki_root, overturned=3, confirmed=1)

        _run_auto_apply_proposals_phase(ctx)

        assert ctx.auto_apply_proposals_summary["dry_run"] is True
        assert read_auto_apply_proposals_ledger(wiki_root) == []


class TestAutoApplyPhaseActionListDerivation:
    def test_actions_passed_are_derived_from_default_threshold_dict(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A hand-written action list must never drift back in -- spy on the
        drafter entry point and assert the *actions* positional arg is
        exactly ``sorted(DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION)``."""
        import athenaeum.auto_apply_proposals as auto_apply_mod

        captured: dict[str, list[str]] = {}

        def _spy(actions, **kwargs):
            captured["actions"] = list(actions)
            return AutoApplyProposalRunSummary(actions_seen=len(actions))

        monkeypatch.setattr(auto_apply_mod, "run_auto_apply_proposal_detection", _spy)

        ctx = _make_ctx(tmp_path, config=_master_config(enabled=True))
        _run_auto_apply_proposals_phase(ctx)

        assert captured["actions"] == sorted(DEFAULT_AUTO_APPLY_THRESHOLD_PER_ACTION)


# ---------------------------------------------------------------------------
# Issue athenaeum#2219, design §8.3 — the attribution-threshold drafter,
# wired into the SAME `_run_auto_apply_proposals_phase` (no second phase,
# no new config knob, no new gate). The round trip below is the production
# path: real sampler -> real phase entry point -> real decisions queue ->
# real approval -> real `threshold_for_class` read-back.
# ---------------------------------------------------------------------------


def _sample_and_review_attribution(
    wiki_root: Path,
    *,
    proposal_id: str,
    question_class: str,
    human_verdict: str,
    verdict: str = "own",
) -> None:
    """Same shape as ``_sample_and_review`` above, for the attribution
    tier -- copied, not imported, per this file's own docstring."""
    from athenaeum.calibration import ATTRIBUTION_TIER_NAME

    sample_tier_decision(
        wiki_root,
        tier=ATTRIBUTION_TIER_NAME,
        verdict=verdict,
        proposal_id=proposal_id,
        reason="test",
        config={"librarian": {"audit_sample_rate_attribution": 1.0}},
        question_class=question_class,
        applied=True,
    )
    audit_id = next(
        r["id"]
        for r in read_calibration_ledger(wiki_root)
        if r.get("kind") == "audit" and r.get("proposal_id") == proposal_id
    )
    record_audit_review(wiki_root, audit_id=audit_id, human_verdict=human_verdict)


def _seed_attribution_disagreement(
    wiki_root: Path, *, question_class: str, overturned: int, confirmed: int
) -> None:
    """Sample + review N attribution ``own`` audits for *question_class*:
    *overturned* reviewed as ``alias`` (differs -> overturned), *confirmed*
    reviewed as ``own`` (matches -> confirmed)."""
    idx = 0
    for _ in range(overturned):
        idx += 1
        _sample_and_review_attribution(
            wiki_root,
            proposal_id=f"attr-{question_class}-{idx}",
            question_class=question_class,
            human_verdict="alias",
        )
    for _ in range(confirmed):
        idx += 1
        _sample_and_review_attribution(
            wiki_root,
            proposal_id=f"attr-{question_class}-{idx}",
            question_class=question_class,
            human_verdict="own",
        )


def _attribution_threshold_config(question_class: str, threshold: float) -> dict:
    return {
        "librarian": {
            "signal_mining": {"enabled": True, "dry_run": False},
            "attribution": {
                "accuracy_floor": 0.95,
                "thresholds": {question_class: threshold},
            },
        }
    }


class TestAttributionThresholdPhaseMasterGate:
    def test_disabled_by_default_leaves_both_summaries_none(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config={})
        _run_auto_apply_proposals_phase(ctx)
        assert ctx.auto_apply_proposals_summary is None
        assert ctx.attribution_threshold_proposals_summary is None

    def test_deadline_tripped_skips_and_records_reason(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config=_master_config(enabled=True))
        ctx.deadline_tripped = True
        _run_auto_apply_proposals_phase(ctx)
        assert ctx.attribution_threshold_proposals_summary == {
            "skipped_deadline_tripped": True
        }


class TestAttributionThresholdPhaseQuestionClassDerivation:
    def test_question_classes_passed_are_derived_from_the_seam(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A hand-written class list must never drift back in -- spy on the
        drafter entry point and assert the *question_classes* positional
        arg is exactly ``sorted(pii_attribution.QUESTION_CLASSES)``."""
        import athenaeum.auto_apply_proposals as auto_apply_mod
        from athenaeum.pii_attribution import QUESTION_CLASSES

        captured: dict[str, list[str]] = {}

        def _spy(question_classes, **kwargs):
            captured["question_classes"] = list(question_classes)
            return AutoApplyProposalRunSummary(actions_seen=len(question_classes))

        monkeypatch.setattr(
            auto_apply_mod, "run_attribution_threshold_proposal_detection", _spy
        )

        ctx = _make_ctx(tmp_path, config=_master_config(enabled=True))
        _run_auto_apply_proposals_phase(ctx)

        assert captured["question_classes"] == sorted(QUESTION_CLASSES)


def _patch_calibration_record(monkeypatch, *, question_class: str, threshold: float) -> None:
    """Patch a single PASSING, synthetic calibration entry for
    *question_class* (threshold *threshold*), so ``threshold_for_class``
    resolves a real ``current`` with NO explicit
    ``librarian.attribution.thresholds.<class>`` config set -- the
    not-pinned happy path a round trip through approval needs. Copied
    (not imported) from ``tests/test_attribution_escalation.py``'s own
    ``_entry``/``_patch_record`` pair, per this file's "copied, not
    imported" convention."""
    from athenaeum import pii_attribution

    entry = pii_attribution.CalibrationEntry(
        question_class=question_class,
        model="synthetic-attribution-model",
        prompt_sha256="synthetic-prompt-sha",
        signal=pii_attribution.SIGNAL_SELF_REPORTED,
        bins=((0.0, 0.5, 0.20), (0.5, 0.9, 0.70), (0.9, 1.0, 0.98)),
        threshold=threshold,
        n_fit=60,
        n_holdout=40,
        holdout_accuracy_above=0.98,
        holdout_wilson_lb=0.95,
        abstain_share=0.18,
        dataset_sha256="synthetic-dataset-sha",
        measured_at="2026-10-31",
        passes=True,
        ece=0.03,
        auroc=0.81,
    )
    monkeypatch.setattr(
        pii_attribution, "load_calibration_record", lambda: {question_class: entry}
    )


class TestAttributionThresholdPhaseRoundTrip:
    """The production round trip the coordinator asked for: real sampler,
    real phase entry point (never the drafter directly), real decisions
    queue, real approval path, real ``threshold_for_class`` read-back."""

    def test_full_round_trip_through_the_production_phase_and_decisions_queue(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from athenaeum.auto_apply_proposals import approve_auto_apply_threshold_proposal
        from athenaeum.calibration import attribution_threshold_action_id
        from athenaeum.decisions import list_pending_decisions
        from athenaeum.pii_attribution import threshold_for_class

        wiki_root = tmp_path / "knowledge" / "wiki"
        question_class = "held-elsewhere"
        # NOT pinned by explicit config -- `current` comes from a shipped,
        # PASSING calibration entry instead, the happy path that actually
        # lets an approval take effect (issue athenaeum#2219 Seer finding
        # A / PR athenaeum#2269: a class PINNED by explicit config is
        # covered separately, in TestAttributionThresholdPhasePinnedByConfig
        # below, specifically BECAUSE an approval there never takes
        # effect).
        _patch_calibration_record(monkeypatch, question_class=question_class, threshold=0.9)
        config = {"librarian": {"signal_mining": {"enabled": True, "dry_run": False}}}

        # 0/10 overturned -> 0.0, strictly below the 0.05 target (1 -
        # accuracy_floor) -> the real sampler + the real phase should widen.
        _seed_attribution_disagreement(
            wiki_root, question_class=question_class, overturned=0, confirmed=10
        )

        ctx = _make_ctx(tmp_path, wiki_root=wiki_root, config=config)
        _run_auto_apply_proposals_phase(ctx)

        summary = ctx.attribution_threshold_proposals_summary
        assert summary is not None
        assert summary["dry_run"] is False
        assert summary["proposed"] == 1
        assert summary["skipped_pinned_by_config"] == 0
        assert len(summary["drafted_ids"]) == 1
        proposal_id = summary["drafted_ids"][0]

        # The draft is visible in the REAL decisions queue, as the SAME
        # `type: "auto-apply-threshold"` item the merge-dedupe rail uses.
        decisions = list_pending_decisions(wiki_root)
        drafted = next(d for d in decisions if d["id"] == proposal_id)
        assert drafted["type"] == "auto-apply-threshold"
        assert drafted["payload"]["direction"] == "widen"
        assert drafted["payload"]["question_class"] == question_class
        action = attribution_threshold_action_id(question_class)
        assert drafted["payload"]["action"] == action

        # Nothing changed yet -- AC5's "no threshold changes without
        # approval" -- the shipped entry's own threshold still reads back.
        assert threshold_for_class(
            question_class, config, wiki_root=wiki_root
        ) == pytest.approx(0.9)

        # Approve through the REAL approval path.
        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=proposal_id)

        # NOW the real reader honours the approved ledger override -- no
        # explicit config exists to outrank it.
        overridden = threshold_for_class(question_class, config, wiki_root=wiki_root)
        assert overridden == pytest.approx(0.85)  # 0.9 - the default widen step (0.05)
        assert overridden < 0.9

        # An operator's explicit config, even added AFTER approval, still
        # wins UNCONDITIONALLY (AC5's own precedence).
        config_with_explicit = _attribution_threshold_config(question_class, 0.9)
        assert threshold_for_class(
            question_class, config_with_explicit, wiki_root=wiki_root
        ) == pytest.approx(0.9)


class TestAttributionThresholdPhasePinnedByConfig:
    """Issue athenaeum#2219, Seer finding A (PR athenaeum#2269): an
    explicit per-class config threshold wins UNCONDITIONALLY over any
    ledger override, so an approved proposal for that class can never
    take effect. Confirmed EMPIRICALLY (not by reading) before this fix
    existed: running the real phase twice over one store with the SAME
    pinned config re-drafted and re-appended the IDENTICAL
    ``(action, current, proposed)`` id on every run, because the generic
    rail's suppression tracks PENDING and REJECTED ids, never APPROVED
    ones. The fix: a pinned class is never even handed to the drafter."""

    def test_pinned_class_never_drafts_at_all(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "knowledge" / "wiki"
        question_class = "held-elsewhere"
        config = _attribution_threshold_config(question_class, 0.9)
        _seed_attribution_disagreement(
            wiki_root, question_class=question_class, overturned=0, confirmed=10
        )

        ctx = _make_ctx(tmp_path, wiki_root=wiki_root, config=config)
        _run_auto_apply_proposals_phase(ctx)

        summary = ctx.attribution_threshold_proposals_summary
        assert summary is not None
        assert summary["proposed"] == 0
        assert summary["skipped_pinned_by_config"] == 1
        # The other 4 question classes have no calibration data seeded at
        # all (rate unavailable) -- "not warranted", a DIFFERENT reason
        # than "pinned by config", which is exactly why the two counters
        # are separate.
        assert summary["not_warranted"] == 4
        assert summary["drafted_ids"] == []

    def test_a_second_run_never_re_drafts_an_approval_made_before_pinning(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The exact scenario the coordinator asked to settle empirically:
        a store carries an APPROVED attribution-threshold proposal (from
        before an operator pinned an explicit config value for that
        class), and the explicit config is now ALSO pinned. Running the
        real phase again must draft nothing new and must not duplicate
        the ledger entry."""
        from athenaeum.auto_apply_proposals import (
            approve_auto_apply_threshold_proposal,
            read_auto_apply_proposals_ledger,
        )

        wiki_root = tmp_path / "knowledge" / "wiki"
        question_class = "held-elsewhere"
        _seed_attribution_disagreement(
            wiki_root, question_class=question_class, overturned=0, confirmed=10
        )

        # Run 1, unpinned: a real draft, which we approve for realism.
        _patch_calibration_record(monkeypatch, question_class=question_class, threshold=0.9)
        unpinned_config = {"librarian": {"signal_mining": {"enabled": True, "dry_run": False}}}
        ctx1 = _make_ctx(tmp_path, wiki_root=wiki_root, config=unpinned_config)
        _run_auto_apply_proposals_phase(ctx1)
        summary1 = ctx1.attribution_threshold_proposals_summary
        assert summary1["proposed"] == 1
        proposal_id = summary1["drafted_ids"][0]
        approve_auto_apply_threshold_proposal(wiki_root, proposal_id=proposal_id)
        ledger_after_approval = read_auto_apply_proposals_ledger(wiki_root)
        assert len(ledger_after_approval) == 2  # one proposal + one approve

        # The operator now ALSO pins an explicit config value for the
        # SAME class (a plausible real sequence -- approving a
        # recommendation, then hard-pinning it for clarity). Re-run the
        # real phase with that pinned config.
        pinned_config = _attribution_threshold_config(question_class, 0.9)
        ctx2 = _make_ctx(tmp_path, wiki_root=wiki_root, config=pinned_config)
        _run_auto_apply_proposals_phase(ctx2)

        summary2 = ctx2.attribution_threshold_proposals_summary
        assert summary2["proposed"] == 0
        assert summary2["skipped_pinned_by_config"] == 1
        assert summary2["drafted_ids"] == []
        # The ledger is byte-for-byte unchanged -- no duplicate proposal
        # record for the already-approved id.
        assert read_auto_apply_proposals_ledger(wiki_root) == ledger_after_approval
