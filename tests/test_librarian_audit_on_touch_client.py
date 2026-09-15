# SPDX-License-Identifier: Apache-2.0
"""``RunContext.resolve_audit_on_touch_client`` / ``build_audit_hook`` (issue
athenaeum#1627): lazy per-run client resolution, caching, and the
batch-mode short-circuit (``audit_page`` always makes a SYNCHRONOUS call,
so audit-on-touch stays out of the way of a batch run rather than
defeating its whole point). All fixtures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from athenaeum.audit_on_touch import AuditOnTouchCounters
from tests.test_librarian_run_phases import _make_ctx


class TestResolveAuditOnTouchClient:
    def test_resolved_once_and_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        def _fake_build(*args: Any, **kwargs: Any) -> object:
            calls["n"] += 1
            return object()

        monkeypatch.setattr("athenaeum.provider.build_llm_client", _fake_build)
        ctx = _make_ctx(tmp_path)

        first = ctx.resolve_audit_on_touch_client()
        second = ctx.resolve_audit_on_touch_client()

        assert first is second
        assert calls["n"] == 1

    def test_provider_error_degrades_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raises(*args: Any, **kwargs: Any) -> object:
            raise RuntimeError("no provider configured")

        monkeypatch.setattr("athenaeum.provider.build_llm_client", _raises)
        ctx = _make_ctx(tmp_path)

        assert ctx.resolve_audit_on_touch_client() is None

    def test_batch_mode_short_circuits_without_attempting_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue athenaeum#1627: a batch run never even ATTEMPTS to build a
        synchronous audit client -- audit_page's call would defeat the
        point of routing LLM traffic through the async Batch API."""
        calls = {"n": 0}

        def _fake_build(*args: Any, **kwargs: Any) -> object:
            calls["n"] += 1
            return object()

        monkeypatch.setattr("athenaeum.provider.build_llm_client", _fake_build)
        ctx = _make_ctx(tmp_path)
        ctx.batch_mode = True

        assert ctx.resolve_audit_on_touch_client() is None
        assert calls["n"] == 0


class TestBuildAuditHook:
    def test_hook_counts_skipped_unavailable_with_no_client(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.batch_mode = True  # forces client=None via the short-circuit above

        hook = ctx.build_audit_hook()
        meta: dict[str, Any] = {"uid": "u1", "type": "concept"}
        hook("u1", Path("/opt/example-knowledge/wiki/u1.md"), meta, "body")

        assert ctx.audit_on_touch_counters.skipped_unavailable == 1
        assert "last_audited" not in meta

    def test_hook_passes_frozen_now_through(self, tmp_path: Path) -> None:
        """``ctx.now`` (when set) reaches ``audit_on_touch`` as the ``now``
        callable -- the freshness check and the stamped timestamp both key
        off it, matching every other ``ctx.now``-driven stamp in this
        module (issue athenaeum#1064's own precedent)."""
        from datetime import datetime, timezone

        frozen = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ctx = _make_ctx(tmp_path, now=frozen)
        ctx.batch_mode = True  # keep this unit test client-free and offline

        hook = ctx.build_audit_hook()
        meta: dict[str, Any] = {"uid": "u1", "type": "concept"}
        hook("u1", Path("/opt/example-knowledge/wiki/u1.md"), meta, "body")

        # No client -> skipped_unavailable, meta untouched either way; this
        # just proves the hook builds and calls without raising when `now`
        # is set.
        assert ctx.audit_on_touch_counters.skipped_unavailable == 1

    def test_counters_accumulate_across_multiple_hook_calls(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.batch_mode = True
        hook = ctx.build_audit_hook()

        hook("u1", Path("/opt/example-knowledge/wiki/u1.md"), {"uid": "u1"}, "b1")
        hook("u2", Path("/opt/example-knowledge/wiki/u2.md"), {"uid": "u2"}, "b2")

        assert ctx.audit_on_touch_counters.skipped_unavailable == 2
        assert isinstance(ctx.audit_on_touch_counters, AuditOnTouchCounters)
