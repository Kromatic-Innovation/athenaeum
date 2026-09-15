# SPDX-License-Identifier: Apache-2.0
"""``write_pending_merge``'s audit-on-touch hook (issue athenaeum#1627 plan
step 3): every page in ``sources`` is re-audited before the proposal block
is written. All fixtures — no live client, no live knowledge store.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum.audit import AUDIT_VERSION
from athenaeum.audit_on_touch import AuditOnTouchCounters
from athenaeum.models import parse_frontmatter
from athenaeum.pending_merges import write_pending_merge
from tests.conftest import FakeLLMClient


def _write_source(path: Path, *, uid: str, name: str, body: str = "body text.\n") -> None:
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: feedback\n---\n{body}",
        encoding="utf-8",
    )


def _responder(**kwargs: Any) -> str:
    return json.dumps(
        {
            "valid_from": {"value": "2026-01-01"},
            "retirement_candidate": False,
            "retirement_reason": "",
        }
    )


def test_audits_each_source_before_writing_the_block(tmp_path: Path) -> None:
    merges = tmp_path / "_pending_merges.md"
    src_a = tmp_path / "u1-alpha.md"
    src_b = tmp_path / "u2-beta.md"
    _write_source(src_a, uid="u1", name="alpha")
    _write_source(src_b, uid="u2", name="beta")

    client = FakeLLMClient(responder=_responder)
    counters = AuditOnTouchCounters()

    write_pending_merge(
        merges,
        merge_target_name="alpha",
        sources=[str(src_a), str(src_b)],
        rationale="test merge",
        draft_merged_body="merged draft",
        confidence=0.9,
        audit_client=client,
        audit_model="claude-haiku-4-5-20251001",
        audit_counters=counters,
    )

    assert counters.audited == 2
    assert counters.coordinates_filled == 2

    meta_a, _ = parse_frontmatter(src_a.read_text(encoding="utf-8"))
    meta_b, _ = parse_frontmatter(src_b.read_text(encoding="utf-8"))
    assert meta_a["audit_version"] == AUDIT_VERSION
    assert meta_a["valid_from"] == "2026-01-01"
    assert meta_b["audit_version"] == AUDIT_VERSION
    assert meta_b["valid_from"] == "2026-01-01"

    # The proposal block was still written normally.
    assert merges.exists()
    assert "alpha" in merges.read_text(encoding="utf-8")


def test_fresh_source_is_skipped_and_counted(tmp_path: Path) -> None:
    merges = tmp_path / "_pending_merges.md"
    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    src_a = tmp_path / "u1-alpha.md"
    src_a.write_text(
        f"---\nuid: u1\nname: alpha\ntype: feedback\nlast_audited: '{recent}'\n---\nbody\n",
        encoding="utf-8",
    )

    client = FakeLLMClient(responder=_responder)
    counters = AuditOnTouchCounters()

    write_pending_merge(
        merges,
        merge_target_name="alpha",
        sources=[str(src_a)],
        rationale="test merge",
        draft_merged_body="merged draft",
        confidence=0.9,
        audit_client=client,
        audit_model="claude-haiku-4-5-20251001",
        audit_counters=counters,
        audit_now=lambda: now,
    )

    assert client.calls == []
    assert counters.skipped_fresh == 1
    assert counters.audited == 0


def test_no_audit_client_still_writes_the_block(tmp_path: Path) -> None:
    merges = tmp_path / "_pending_merges.md"
    src_a = tmp_path / "u1-alpha.md"
    _write_source(src_a, uid="u1", name="alpha")
    counters = AuditOnTouchCounters()

    block = write_pending_merge(
        merges,
        merge_target_name="alpha",
        sources=[str(src_a)],
        rationale="test merge",
        draft_merged_body="merged draft",
        confidence=0.9,
        audit_client=None,
        audit_counters=counters,
    )

    assert block  # the proposal still wrote normally
    assert counters.skipped_unavailable == 1
    meta_a, _ = parse_frontmatter(src_a.read_text(encoding="utf-8"))
    assert "last_audited" not in meta_a


def test_audit_counters_none_disables_the_hook_entirely(tmp_path: Path) -> None:
    """Every pre-athenaeum#1627 caller (``merge.py``, ``reasoning_screens.py``,
    ``name_structure.py``, ``_cmd_merges.py``) passes no audit_* kwargs at
    all -- must be byte-identical to before this issue."""
    merges = tmp_path / "_pending_merges.md"
    src_a = tmp_path / "u1-alpha.md"
    _write_source(src_a, uid="u1", name="alpha")
    before = src_a.read_text(encoding="utf-8")

    write_pending_merge(
        merges,
        merge_target_name="alpha",
        sources=[str(src_a)],
        rationale="test merge",
        draft_merged_body="merged draft",
        confidence=0.9,
    )

    assert src_a.read_text(encoding="utf-8") == before


def test_unreadable_source_is_skipped_not_raised(tmp_path: Path) -> None:
    merges = tmp_path / "_pending_merges.md"
    missing = tmp_path / "does-not-exist.md"
    client = FakeLLMClient(responder=_responder)
    counters = AuditOnTouchCounters()

    block = write_pending_merge(
        merges,
        merge_target_name="alpha",
        sources=[str(missing)],
        rationale="test merge",
        draft_merged_body="merged draft",
        confidence=0.9,
        audit_client=client,
        audit_model="claude-haiku-4-5-20251001",
        audit_counters=counters,
    )

    assert block
    assert counters.is_zero
    assert client.calls == []
