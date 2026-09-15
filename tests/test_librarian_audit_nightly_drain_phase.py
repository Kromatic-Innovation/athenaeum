# SPDX-License-Identifier: Apache-2.0
"""``librarian._run_audit_nightly_drain_phase`` wiring (issue athenaeum#1630).

Covers the LIBRARIAN-PHASE wiring only — the drain's own selection/budget/
write-back logic is covered exhaustively in ``tests/test_audit_queue.py``
against :func:`athenaeum.audit_queue.run_nightly_drain` directly. This file
proves: the phase is a no-op (config gate off) by default; it wires
``ctx.classify_client``/``ctx.knob_models["classify"]`` through unchanged;
it appends exactly one ``"audit-nightly-drain"`` entry to ``ctx.run_profile``
either way; it respects ``ctx.deadline_tripped``; and a raised exception
inside the drain is swallowed (run-profile records ``"failed"``) rather than
propagating and aborting the whole librarian run.

A deliberately self-contained, minimal :class:`~athenaeum.librarian.RunContext`
constructor lives in THIS file (not imported from
``tests/test_librarian_run_phases.py``) — issue athenaeum#1627 is editing that
shared file concurrently in a sibling lane; duplicating the few required
fields here avoids a merge collision on its helper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from athenaeum.librarian import RunContext, _run_audit_nightly_drain_phase


def _make_ctx(tmp_path: Path, **overrides: Any) -> RunContext:
    """Minimal RunContext, mirroring the constructor
    ``tests/test_librarian_run_phases.py::_make_ctx`` also builds (same
    field set ``run()`` itself always supplies) — duplicated per this
    file's own module docstring."""
    knowledge_root = overrides.pop("knowledge_root", tmp_path / "knowledge")
    wiki_root = overrides.pop("wiki_root", knowledge_root / "wiki")
    raw_root = overrides.pop("raw_root", knowledge_root / "raw")
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
        now=None,
        heartbeat=None,
        out_run_stats=None,
    )
    defaults.setdefault("config", {})
    defaults.update(overrides)
    ctx = RunContext(**defaults)
    ctx.knob_models = {"classify": "claude-haiku-4-5-20251001"}
    return ctx


def _page(root: Path, name: str, uid: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nuid: {uid}\ntype: concept\nname: {uid}\n---\nBody.\n", encoding="utf-8")


class TestGateOffByDefault:
    def test_noop_when_config_key_absent(self, tmp_path: Path) -> None:
        """The phase always DELEGATES the gate check to
        ``audit_queue.run_nightly_drain`` (see that function's own
        docstring: it returns ``None`` immediately, before any wiki scan or
        client use, when ``audit.nightly_max_pages`` is unset) rather than
        re-checking the config key itself — proven here against the REAL
        (unmocked) ``run_nightly_drain`` with a config that has no ``audit``
        key at all, so a stale page existing in the fixture wiki is never
        touched."""
        ctx = _make_ctx(tmp_path)
        (ctx.wiki_root).mkdir(parents=True)
        _page(ctx.wiki_root, "never.md", "never1")
        _run_audit_nightly_drain_phase(ctx)
        assert ctx.audit_nightly_drain_summary is None
        assert len(ctx.run_profile) == 1
        phase, _secs, fields = ctx.run_profile[0]
        assert phase == "audit-nightly-drain"
        assert fields == {"reason": "disabled"}
        from athenaeum.models import parse_frontmatter

        meta, _body = parse_frontmatter((ctx.wiki_root / "never.md").read_text())
        assert meta.get("last_audited") is None


class TestDeadlineTripped:
    def test_skips_phase_when_deadline_already_tripped(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.deadline_tripped = True
        with patch("athenaeum.audit_queue.run_nightly_drain") as mock_drain:
            _run_audit_nightly_drain_phase(ctx)
        mock_drain.assert_not_called()
        assert ctx.audit_nightly_drain_summary is None
        assert ctx.run_profile[-1][2] == {"reason": "deadline-tripped"}


class TestEnabledPath:
    def test_wires_classify_client_and_model_through(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config={"audit": {"nightly_max_pages": 3}})
        ctx.wiki_root.mkdir(parents=True)
        ctx.classify_client = object()

        captured: dict[str, Any] = {}

        def _fake_drain(wiki_root, *, client, model, config, now, run_usage):
            captured["client"] = client
            captured["model"] = model
            captured["config"] = config
            captured["run_usage"] = run_usage
            return None

        with patch("athenaeum.audit_queue.run_nightly_drain", side_effect=_fake_drain):
            _run_audit_nightly_drain_phase(ctx)

        assert captured["client"] is ctx.classify_client
        assert captured["model"] == "claude-haiku-4-5-20251001"
        assert captured["config"] is ctx.config
        assert captured["run_usage"] is ctx.usage

    def test_records_summary_and_profile_entry_on_success(self, tmp_path: Path) -> None:
        from athenaeum.audit_queue import NightlyDrainSummary

        ctx = _make_ctx(tmp_path, config={"audit": {"nightly_max_pages": 3}})
        summary = NightlyDrainSummary(
            stale_queue_size=5,
            candidates_considered=3,
            reaudited=2,
            skipped_budget=1,
            failed=0,
            stale_remaining=3,
            cost_usd=0.01,
            reason="completed",
        )
        with patch("athenaeum.audit_queue.run_nightly_drain", return_value=summary):
            _run_audit_nightly_drain_phase(ctx)

        assert ctx.audit_nightly_drain_summary == summary.to_dict()
        assert len(ctx.run_profile) == 1
        phase, _secs, fields = ctx.run_profile[0]
        assert phase == "audit-nightly-drain"
        assert fields == {
            "reason": "completed",
            "reaudited": 2,
            "skipped_budget": 1,
            "stale_remaining": 3,
        }

    def test_exception_is_swallowed_and_recorded_as_failed(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config={"audit": {"nightly_max_pages": 3}})
        with patch(
            "athenaeum.audit_queue.run_nightly_drain", side_effect=RuntimeError("boom")
        ):
            _run_audit_nightly_drain_phase(ctx)  # must not raise

        assert ctx.audit_nightly_drain_summary is None
        assert ctx.run_profile[-1][2] == {"reason": "failed"}


class TestRunSummaryFlowsIntoLedgerWithoutTouchingRunSummaryLog:
    """The phase-append convention (``ctx.run_profile.append((name, secs,
    fields))``) is documented (see e.g. ``_run_name_collision_phase``'s own
    docstring) to flow automatically into BOTH the prose
    ``librarian-run-summary`` line AND the durable JSONL ledger record via
    ``run_summary_log.build_run_summary_ledger_record`` — no per-phase-name
    registration needed in ``run_summary_log.py`` itself. This test proves
    that generic flow-through for this phase specifically, which is why
    this lane's diff does not touch ``run_summary_log.py`` at all."""

    def test_phase_entry_renders_in_prose_summary(self, tmp_path: Path) -> None:
        from athenaeum.audit_queue import NightlyDrainSummary
        from athenaeum.librarian import _render_run_summary

        ctx = _make_ctx(tmp_path, config={"audit": {"nightly_max_pages": 3}})
        summary = NightlyDrainSummary(reaudited=2, skipped_budget=1, stale_remaining=3)
        with patch("athenaeum.audit_queue.run_nightly_drain", return_value=summary):
            _run_audit_nightly_drain_phase(ctx)

        line = _render_run_summary(ctx.run_profile)
        assert "audit-nightly-drain" in line
        assert "reaudited=2" in line
        assert "skipped_budget=1" in line

    def test_phase_entry_reaches_the_durable_ledger_record(self, tmp_path: Path) -> None:
        from athenaeum.audit_queue import NightlyDrainSummary
        from athenaeum.run_summary_log import build_run_summary_ledger_record

        ctx = _make_ctx(tmp_path, config={"audit": {"nightly_max_pages": 3}})
        summary = NightlyDrainSummary(reaudited=2, skipped_budget=1, stale_remaining=3)
        with patch("athenaeum.audit_queue.run_nightly_drain", return_value=summary):
            _run_audit_nightly_drain_phase(ctx)

        record = build_run_summary_ledger_record(ctx.run_profile)
        assert record["phases"]["audit-nightly-drain"]["reaudited"] == 2


class TestEndToEndThroughRealDrain:
    """One integration test against the REAL (unmocked)
    ``audit_queue.run_nightly_drain`` -- everything else in this file mocks
    it to isolate the librarian-wiring contract, which
    ``tests/test_audit_queue.py`` already covers exhaustively at the module
    level; this proves the two actually compose."""

    def test_real_drain_stamps_pages_and_records_summary(self, tmp_path: Path) -> None:
        from tests.test_audit_queue import _FakeBatchClient

        ctx = _make_ctx(tmp_path, config={"audit": {"nightly_max_pages": 2}})
        ctx.wiki_root.mkdir(parents=True)
        for i in range(4):
            _page(ctx.wiki_root, f"p{i}.md", f"uid{i}")
        ctx.classify_client = _FakeBatchClient()

        _run_audit_nightly_drain_phase(ctx)

        assert ctx.audit_nightly_drain_summary is not None
        assert ctx.audit_nightly_drain_summary["reaudited"] == 2
        assert ctx.audit_nightly_drain_summary["stale_remaining"] == 2

        from athenaeum.models import parse_frontmatter

        stamped = 0
        for i in range(4):
            meta, _body = parse_frontmatter((ctx.wiki_root / f"p{i}.md").read_text())
            if meta.get("last_audited"):
                stamped += 1
        assert stamped == 2
