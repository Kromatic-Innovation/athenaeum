# SPDX-License-Identifier: Apache-2.0
"""Tests for the self-tuning loop's nightly phase wiring (issue athenaeum#2020
AC4/AC5): ``librarian._run_signal_mining_phase``, gated by ONE master key
(``librarian.signal_mining.enabled``) mirroring ``_run_rule_proposal_phase``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from athenaeum.dimension_proposals import read_dimension_proposals_ledger
from athenaeum.librarian import RunContext, _run_signal_mining_phase
from athenaeum.runlock import RunLock
from athenaeum.verdicts import Basis, append_verdict, build_verdict_entry

_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _basis() -> Basis:
    return Basis(
        content_hashes=["a", "b"],
        coords=[None, None],
        coord_origins={},
        registry_epoch=1,
        tree_epoch=1,
        authority_basis=None,
        predicate_instrument=[None, None],
        comparator_version="v1.gate2",
    )


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


def _seed_underdetermined(wiki_root: Path, *, id_a: str, id_b: str, missing: list[str]) -> None:
    entry = build_verdict_entry(
        id_a,
        id_b,
        "underdetermined",
        basis=_basis(),
        missing=missing,
        at="2026-09-01",
        decided_by="comparator",
    )
    with RunLock(wiki_root.parent) as lock:
        append_verdict(wiki_root, entry, lock=lock)


def _signal_mining_config(*, enabled: bool, dry_run: bool | None = None) -> dict:
    section: dict = {"enabled": enabled, "threshold": 1, "window_days": 3650}
    if dry_run is not None:
        section["dry_run"] = dry_run
    return {"librarian": {"signal_mining": section}}


class TestMasterGate:
    def test_disabled_by_default_leaves_summary_none(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config={})
        _run_signal_mining_phase(ctx)
        assert ctx.signal_mining_summary is None

    def test_deadline_tripped_skips_and_records_reason(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config=_signal_mining_config(enabled=True))
        ctx.deadline_tripped = True
        _run_signal_mining_phase(ctx)
        assert ctx.signal_mining_summary == {"skipped_deadline_tripped": True}


class TestDryRunDefault:
    def test_enabled_dry_run_lists_without_queueing(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "knowledge" / "wiki"
        ctx = _make_ctx(tmp_path, config=_signal_mining_config(enabled=True))
        _seed_underdetermined(ctx.wiki_root, id_a="a1", id_b="b1", missing=["jurisdiction"])

        _run_signal_mining_phase(ctx)

        assert ctx.signal_mining_summary is not None
        assert ctx.signal_mining_summary["dry_run"] is True
        assert ctx.signal_mining_summary["drafted_ids"]
        assert ctx.signal_mining_summary["proposed"] == 0
        # Dry-run must make no persistence call at all.
        assert read_dimension_proposals_ledger(wiki_root) == []


class TestLiveQueueing:
    def test_enabled_live_queues_proposals(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "knowledge" / "wiki"
        ctx = _make_ctx(tmp_path, config=_signal_mining_config(enabled=True, dry_run=False))
        _seed_underdetermined(ctx.wiki_root, id_a="a1", id_b="b1", missing=["jurisdiction"])

        _run_signal_mining_phase(ctx)

        assert ctx.signal_mining_summary["dry_run"] is False
        assert ctx.signal_mining_summary["proposed"] == 1
        records = read_dimension_proposals_ledger(wiki_root)
        assert len(records) == 1
        assert records[0]["name"] == "jurisdiction"

    def test_global_run_dry_run_forces_dry_run_even_if_knob_says_false(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "knowledge" / "wiki"
        ctx = _make_ctx(
            tmp_path,
            config=_signal_mining_config(enabled=True, dry_run=False),
            dry_run=True,
        )
        _seed_underdetermined(ctx.wiki_root, id_a="a1", id_b="b1", missing=["jurisdiction"])

        _run_signal_mining_phase(ctx)

        assert ctx.signal_mining_summary["dry_run"] is True
        assert read_dimension_proposals_ledger(wiki_root) == []
