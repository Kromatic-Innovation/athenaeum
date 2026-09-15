# SPDX-License-Identifier: Apache-2.0
"""``athenaeum.audit_on_touch`` — the librarian-side re-audit hook (issue athenaeum#1627).

Covers the pure ``audit_on_touch()`` function in isolation (freshness skip,
unavailable-client skip, a successful audit stamping ``meta`` in place and
incrementing the right counters, an errored audit leaving ``meta``
untouched) plus :class:`AuditOnTouchCounters`'s own shape. The three
librarian touch-point integrations (tier-3 merge, pending-merge proposals,
name collisions) are covered in their own test modules
(``test_tiers.py``, ``test_pending_merges_pii.py``-adjacent,
``test_name_collisions_1170.py``) and ``test_librarian_run_summary.py``
covers the run-summary counters. All fixtures — no live client, no live
knowledge store.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum.audit import AUDIT_VERSION
from athenaeum.audit_on_touch import DEFAULT_FRESHNESS_HOURS, AuditOnTouchCounters, audit_on_touch
from tests.conftest import FakeLLMClient


def _verdict_json(**fields: Any) -> str:
    return json.dumps(
        {
            "valid_from": {"value": "2026-01-01"},
            "retirement_candidate": False,
            "retirement_reason": "",
            **fields,
        }
    )


class TestFreshnessSkip:
    def test_fresh_last_audited_skips_and_makes_no_call(self) -> None:
        now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta: dict[str, Any] = {"uid": "u1", "type": "concept", "last_audited": recent}
        client = FakeLLMClient(text=_verdict_json())
        counters = AuditOnTouchCounters()

        verdict = audit_on_touch(
            client,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=meta,
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters,
            now=lambda: now,
        )

        assert verdict is None
        assert client.calls == []
        assert counters.skipped_fresh == 1
        assert counters.audited == 0
        assert meta["last_audited"] == recent  # untouched

    def test_stale_last_audited_is_audited(self) -> None:
        now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
        stale = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta: dict[str, Any] = {"uid": "u1", "type": "concept", "last_audited": stale}
        client = FakeLLMClient(text=_verdict_json())
        counters = AuditOnTouchCounters()

        verdict = audit_on_touch(
            client,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=meta,
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters,
            now=lambda: now,
        )

        assert verdict is not None
        assert len(client.calls) == 1
        assert counters.audited == 1
        assert counters.skipped_fresh == 0

    def test_never_audited_page_is_audited(self) -> None:
        meta: dict[str, Any] = {"uid": "u1", "type": "concept"}
        client = FakeLLMClient(text=_verdict_json())
        counters = AuditOnTouchCounters()

        verdict = audit_on_touch(
            client,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=meta,
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters,
        )

        assert verdict is not None
        assert counters.audited == 1

    def test_custom_freshness_window_is_honored(self) -> None:
        now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
        two_hours_ago = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta: dict[str, Any] = {"uid": "u1", "type": "concept", "last_audited": two_hours_ago}
        client = FakeLLMClient(text=_verdict_json())
        counters = AuditOnTouchCounters()

        # Default (24h) window: fresh, skipped.
        audit_on_touch(
            client,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=dict(meta),
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters,
            now=lambda: now,
        )
        assert counters.skipped_fresh == 1

        # A 1h window: two hours ago is stale under THIS window.
        counters2 = AuditOnTouchCounters()
        audit_on_touch(
            client,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=dict(meta),
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters2,
            freshness_hours=1.0,
            now=lambda: now,
        )
        assert counters2.audited == 1
        assert counters2.skipped_fresh == 0


class TestUnavailableSkip:
    def test_no_client_skips_and_counts_unavailable(self) -> None:
        meta: dict[str, Any] = {"uid": "u1", "type": "concept"}
        counters = AuditOnTouchCounters()

        verdict = audit_on_touch(
            None,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=meta,
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters,
        )

        assert verdict is None
        assert counters.skipped_unavailable == 1
        assert counters.audited == 0
        assert meta == {"uid": "u1", "type": "concept"}  # untouched


class TestSuccessfulAudit:
    def test_stamps_last_audited_and_audit_version_onto_meta(self) -> None:
        meta: dict[str, Any] = {"uid": "u1", "type": "concept"}
        client = FakeLLMClient(text=_verdict_json())
        counters = AuditOnTouchCounters()

        verdict = audit_on_touch(
            client,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=meta,
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters,
        )

        assert verdict is not None and verdict.error is None
        assert meta["audit_version"] == AUDIT_VERSION
        datetime.strptime(meta["last_audited"], "%Y-%m-%dT%H:%M:%SZ")

    def test_determinable_coordinate_is_filled_and_counted(self) -> None:
        meta: dict[str, Any] = {"uid": "u1", "type": "concept"}
        client = FakeLLMClient(text=_verdict_json())
        counters = AuditOnTouchCounters()

        audit_on_touch(
            client,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=meta,
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters,
        )

        assert meta["valid_from"] == "2026-01-01"
        assert counters.coordinates_filled == 1
        assert counters.coordinates_undeterminable == 0

    def test_undeterminable_coordinate_is_recorded_and_counted(self) -> None:
        meta: dict[str, Any] = {"uid": "u1", "type": "concept"}
        client = FakeLLMClient(
            text=json.dumps(
                {
                    "valid_from": {"undeterminable": "no date stated"},
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )
        )
        counters = AuditOnTouchCounters()

        audit_on_touch(
            client,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=meta,
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters,
        )

        assert "valid_from" not in meta
        assert meta["audit_findings"]["valid_from"].startswith("undeterminable:")
        assert counters.coordinates_filled == 0
        assert counters.coordinates_undeterminable == 1

    def test_never_overwrites_a_populated_coordinate(self) -> None:
        meta: dict[str, Any] = {
            "uid": "u1",
            "type": "concept",
            "claimed_scope": "team:pre-existing",
        }
        # empty_fields excludes claimed_scope (already populated), so the
        # model is never even asked about it — but assert the invariant at
        # this layer too, mirroring apply_audit_report's own defensive
        # re-check.
        client = FakeLLMClient(text=_verdict_json())
        counters = AuditOnTouchCounters()

        audit_on_touch(
            client,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=meta,
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters,
        )

        assert meta["claimed_scope"] == "team:pre-existing"


class TestErroredAudit:
    def test_transport_failure_leaves_meta_untouched_but_counts_audited(self) -> None:
        meta: dict[str, Any] = {"uid": "u1", "type": "concept"}
        client = FakeLLMClient(raises=ConnectionError("boom"))
        counters = AuditOnTouchCounters()

        verdict = audit_on_touch(
            client,
            uid="u1",
            path=Path("/opt/example-knowledge/wiki/u1.md"),
            meta=meta,
            body="body",
            model="claude-haiku-4-5-20251001",
            counters=counters,
        )

        assert verdict is not None and verdict.error is not None
        assert counters.audited == 1
        assert "last_audited" not in meta
        assert counters.coordinates_filled == 0
        assert counters.coordinates_undeterminable == 0


class TestCounters:
    def test_as_profile_fields_uses_bare_keys(self) -> None:
        counters = AuditOnTouchCounters(
            audited=1,
            skipped_fresh=2,
            skipped_unavailable=3,
            coordinates_filled=4,
            coordinates_undeterminable=5,
        )
        assert counters.as_profile_fields() == {
            "audited": 1,
            "skipped_fresh": 2,
            "skipped_unavailable": 3,
            "coordinates_filled": 4,
            "coordinates_undeterminable": 5,
        }

    def test_is_zero(self) -> None:
        assert AuditOnTouchCounters().is_zero
        assert not AuditOnTouchCounters(audited=1).is_zero
        assert not AuditOnTouchCounters(skipped_fresh=1).is_zero

    def test_default_freshness_hours_matches_issue_suggestion(self) -> None:
        assert DEFAULT_FRESHNESS_HOURS == 24.0
