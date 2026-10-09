# SPDX-License-Identifier: Apache-2.0
"""Tests for the self-tuning loop's two sibling nightly phases (issue
athenaeum#719 AC11/AC15): ``librarian._run_tier_movement_proposals_phase``
(issue athenaeum#2019) and ``librarian._run_auto_apply_proposals_phase``
(issue athenaeum#2018), both gated by the SAME master key
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
