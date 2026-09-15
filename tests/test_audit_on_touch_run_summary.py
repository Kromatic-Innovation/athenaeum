# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1627 plan step 6: the five ``audit_on_touch.*`` counters
flow into ``RunContext.emit_run_summary`` as an ``audit_on_touch``
``run_profile`` phase entry — and from there into both the prose
``librarian-run-summary`` line and the durable JSONL ledger record, with no
further wiring than ``emit_run_summary`` itself (mirrors
``tests/test_t1_screen_census.py``'s ``TestT1CensusIntegration`` shape for
the sibling ``t1-screen`` counter).
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.librarian import _render_run_summary
from athenaeum.run_summary_log import build_run_summary_ledger_record
from tests.test_librarian_run_phases import _make_ctx


class TestAuditOnTouchRunSummaryIntegration:
    def test_emit_run_summary_appends_audit_on_touch_phase_entry(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        # Two touches this run: 1 audited, 1 fresh (the issue's own AC5
        # worked example).
        ctx.audit_on_touch_counters.audited = 1
        ctx.audit_on_touch_counters.skipped_fresh = 1

        ctx.emit_run_summary()

        entries = [e for e in ctx.run_profile if e[0] == "audit_on_touch"]
        assert len(entries) == 1
        _name, secs, fields = entries[0]
        assert secs == 0.0
        assert fields == {
            "audited": 1,
            "skipped_fresh": 1,
            "skipped_unavailable": 0,
            "coordinates_filled": 0,
            "coordinates_undeterminable": 0,
        }

        # Same input flows into the durable ledger record unchanged.
        record = build_run_summary_ledger_record(ctx.run_profile)
        assert record["phases"]["audit_on_touch"]["audited"] == 1
        assert record["phases"]["audit_on_touch"]["skipped_fresh"] == 1

        # ...and into the greppable prose line — dotted names in the issue
        # body are the phase-qualified way to TALK about a field
        # (`audit_on_touch.audited`), not a literal `key=value` token: the
        # phase name is `audit_on_touch`, the bare field is `audited`.
        line = _render_run_summary(ctx.run_profile)
        assert "audit_on_touch secs=0.000" in line
        assert "audited=1" in line
        assert "skipped_fresh=1" in line

    def test_all_five_counters_round_trip(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.audit_on_touch_counters.audited = 3
        ctx.audit_on_touch_counters.skipped_fresh = 2
        ctx.audit_on_touch_counters.skipped_unavailable = 1
        ctx.audit_on_touch_counters.coordinates_filled = 4
        ctx.audit_on_touch_counters.coordinates_undeterminable = 5

        ctx.emit_run_summary()
        record = build_run_summary_ledger_record(ctx.run_profile)
        phase = record["phases"]["audit_on_touch"]
        assert phase["audited"] == 3
        assert phase["skipped_fresh"] == 2
        assert phase["skipped_unavailable"] == 1
        assert phase["coordinates_filled"] == 4
        assert phase["coordinates_undeterminable"] == 5

    def test_emit_run_summary_is_idempotent(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.audit_on_touch_counters.audited = 1
        ctx.emit_run_summary()
        ctx.emit_run_summary()
        entries = [e for e in ctx.run_profile if e[0] == "audit_on_touch"]
        assert len(entries) == 1

    def test_omits_entry_when_counters_are_all_zero(self, tmp_path: Path) -> None:
        """A run that never touched an existing page keeps an unchanged
        run_profile -- same 'omit, don't report a hollow zero' convention
        as the sibling t1-screen counter."""
        ctx = _make_ctx(tmp_path)
        ctx.emit_run_summary()
        assert ctx.run_profile == []
