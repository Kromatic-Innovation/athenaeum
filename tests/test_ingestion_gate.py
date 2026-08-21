# SPDX-License-Identifier: Apache-2.0
"""Tests for the ingestion gate (issue athenaeum#968, part 3).

Covers: the gate is off (and therefore trivially "healthy") by default; when
enabled, it is unhealthy while push-metrics instrumentation is disabled OR
has zero reference-determination records, and healthy once at least one
exists; the config resolver's env/yaml precedence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import push_metrics
from athenaeum.config import resolve_ingestion_gate_enabled
from athenaeum.ingestion_gate import check_ingestion_gate


class TestResolveIngestionGateEnabled:
    def test_default_off(self) -> None:
        assert resolve_ingestion_gate_enabled(None) is False

    def test_yaml_true(self) -> None:
        config = {"librarian": {"ingestion_gate_enabled": True}}
        assert resolve_ingestion_gate_enabled(config) is True

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_INGESTION_GATE_ENABLED", "0")
        config = {"librarian": {"ingestion_gate_enabled": True}}
        assert resolve_ingestion_gate_enabled(config) is False

    def test_non_bool_yaml_falls_through_to_default(self) -> None:
        config = {"librarian": {"ingestion_gate_enabled": "yes"}}
        assert resolve_ingestion_gate_enabled(config) is False


class TestCheckIngestionGateDisabled:
    def test_disabled_is_trivially_healthy_and_unblocked(self, tmp_path: Path) -> None:
        status = check_ingestion_gate(config=None, cache_dir=tmp_path)
        assert status.enabled is False
        assert status.healthy is True
        assert status.blocked is False


class TestCheckIngestionGateEnabled:
    _CONFIG = {"librarian": {"ingestion_gate_enabled": True}}

    def test_push_metrics_disabled_is_unhealthy_and_blocked(self, tmp_path: Path) -> None:
        config = {**self._CONFIG, "push_metrics": {"enabled": False}}
        status = check_ingestion_gate(config=config, cache_dir=tmp_path)
        assert status.enabled is True
        assert status.healthy is False
        assert status.blocked is True
        assert status.push_metrics_enabled is False

    def test_zero_reference_records_is_unhealthy_and_blocked(self, tmp_path: Path) -> None:
        status = check_ingestion_gate(config=self._CONFIG, cache_dir=tmp_path)
        assert status.enabled is True
        assert status.push_metrics_enabled is True
        assert status.reference_record_count == 0
        assert status.healthy is False
        assert status.blocked is True

    def test_at_least_one_reference_record_is_healthy_and_unblocked(
        self, tmp_path: Path
    ) -> None:
        ref = push_metrics.ReferenceResult(
            session_id="s1",
            ts="2026-01-01T00:00:00Z",
            pushed_ids=["a"],
            referenced_ids=["a"],
        )
        push_metrics.record_reference_result(ref, cache_dir=tmp_path)

        status = check_ingestion_gate(config=self._CONFIG, cache_dir=tmp_path)
        assert status.healthy is True
        assert status.blocked is False
        assert status.reference_record_count == 1

    def test_to_dict_carries_blocked(self, tmp_path: Path) -> None:
        status = check_ingestion_gate(config=self._CONFIG, cache_dir=tmp_path)
        d = status.to_dict()
        assert d["blocked"] is True
        assert d["enabled"] is True


class TestLibrarianWiring:
    """Issue athenaeum#968: `_run_auto_memory_phase` checks the ingestion gate
    BEFORE discovery and skips the whole phase when blocked."""

    def test_blocked_gate_skips_auto_memory_phase(self, tmp_path: Path) -> None:
        from datetime import datetime, timezone

        from athenaeum.librarian import RunContext, TokenUsage, _run_auto_memory_phase

        knowledge_root = tmp_path / "knowledge"
        auto = knowledge_root / "raw" / "auto-memory" / "-Users-alice-Code-projectx"
        auto.mkdir(parents=True)
        (auto / "reference_recall_architecture.md").write_text(
            "---\nname: Recall architecture\ntype: reference\n"
            "originSessionId: sess-legit\n---\nSome durable knowledge.\n",
            encoding="utf-8",
        )
        (knowledge_root / "athenaeum.yaml").write_text(
            "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
            encoding="utf-8",
        )
        (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)

        ctx = RunContext(
            raw_root=knowledge_root / "raw",
            wiki_root=knowledge_root / "wiki",
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
            now=datetime.now(timezone.utc),
            heartbeat=None,
            out_run_stats=None,
        )
        ctx.config = {
            "recall": {"extra_intake_roots": ["raw/auto-memory"]},
            "librarian": {"ingestion_gate_enabled": True},
        }
        ctx.usage = TokenUsage()
        ctx.run_deadline = None

        result = _run_auto_memory_phase(ctx)

        assert result is None
        assert ctx.ingestion_gate_status is not None
        assert ctx.ingestion_gate_status["blocked"] is True
        # Never reached discovery/filtering -- the never-ingest summary stays
        # unset, proving the gate short-circuits BEFORE that phase runs.
        assert ctx.never_ingest_summary is None
        # Nothing on disk touched.
        assert (auto / "reference_recall_architecture.md").exists()
