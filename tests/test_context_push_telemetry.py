# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#1362 — sidecar push telemetry through
``push_metrics.record_push``.

Each acceptance criterion in the issue gets its own test, with the
counter-example the issue names as a comment on the test it defeats.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from athenaeum import push_metrics
from athenaeum.context import record_context_push

SRC = str(Path(__file__).resolve().parent.parent / "src")


def _build_index(path: Path, rows: list[tuple]) -> Path:
    """One FTS5 ``wiki`` table, schema v4 shape (``memory_tier`` column
    present) — the shape ``athenaeum.context`` queries against.

    Each row: ``(filename, name, tags, description, audience, memory_tier)``.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE VIRTUAL TABLE wiki USING fts5(filename, name, tags, aliases, '
        'description, audience UNINDEXED, type UNINDEXED, '
        'memory_tier UNINDEXED, tokenize="porter unicode61")'
    )
    conn.executemany(
        "INSERT INTO wiki (filename, name, tags, aliases, description, audience, "
        "type, memory_tier) VALUES (?, ?, ?, '', ?, ?, 'reference', ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def _run_context_cli(
    cache_dir: Path, *, prompt: str = "recall architecture note", session_id: str = "sess-1"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "athenaeum.cli",
            "context",
            prompt,
            "--session-id",
            session_id,
            "--cache-dir",
            str(cache_dir),
            "--no-llm",
        ],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": SRC, "PATH": "/usr/bin:/bin"},
        timeout=30,
    )


# ---------------------------------------------------------------------------
# AC1 — the deployed adapter, end to end, exactly one sidecar row
# ---------------------------------------------------------------------------


def test_cli_end_to_end_writes_exactly_one_sidecar_row(tmp_path: Path) -> None:
    """Counter-example that must fail: a row written by a code path that
    never runs — exactly issue athenaeum#1343's shipped-but-dead state (a
    module tested in isolation, never exercised by any real invocation).
    This drives the REAL ``athenaeum context`` subprocess — the deployed
    adapter — against a fixture index and a temp cache dir, and reads back
    the ledger file that run actually produced."""
    _build_index(
        tmp_path / "wiki-index.db",
        [
            (
                "abc12345-recall-architecture-note.md",
                "Recall Architecture Note",
                "recall",
                "description number 0",
                "|opsadmin|",
                "hot",
            )
        ],
    )

    result = _run_context_cli(tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    envelope = json.loads(result.stdout)
    assert envelope["candidates"], "fixture query must actually match a candidate"
    assert envelope["candidates"][0]["filename"] == "abc12345-recall-architecture-note.md"

    ledger_path = tmp_path / "_push_records.jsonl"
    lines = [ln for ln in ledger_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one ledger row, got {len(lines)}: {lines}"

    row = json.loads(lines[0])
    assert row["source"] == "sidecar"
    assert row["session_id"] == "sess-1"
    assert row["pushed_count"] == 1
    assert len(row["items"]) == 1

    item = row["items"][0]
    # AC ("PII hazard"): the entity-shaped filename's name-derived slug must
    # never reach the ledger — only the 8-hex uid prefix.
    assert item["id"] == "abc12345"
    assert "recall-architecture-note" not in item["id"]
    # AC4: memory_tier carried per item, from the index's own column.
    assert item["memory_tier"] == "hot"
    # Scope derived from the stored audience string (issue athenaeum#1362,
    # docs/extending/sidecar-adapter-contract.md §2.4's inverse-parsing helper).
    assert item["scope"] == "opsadmin"
    assert item["tier"] == "internal"


def test_cli_run_with_no_matching_candidates_writes_no_row(tmp_path: Path) -> None:
    """A turn that pushes nothing must never write an empty/placeholder row
    (mirrors ``record_push``'s own no-items no-op contract)."""
    _build_index(tmp_path / "wiki-index.db", [])

    result = _run_context_cli(tmp_path, prompt="entirely unrelated gibberish query zzz")
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    ledger_path = tmp_path / "_push_records.jsonl"
    assert not ledger_path.exists()


# ---------------------------------------------------------------------------
# AC2 — byte-compatible with the MCP `recall` path: same serializer
# ---------------------------------------------------------------------------


def test_sidecar_and_mcp_records_share_key_structure() -> None:
    """Counter-example that must fail: two independent serializers drifting
    apart. Builds one sidecar-shaped record (via the same
    :class:`push_metrics.PushRecord`/:class:`PushedItem` construction
    :func:`athenaeum.context.record_context_push` uses) and one MCP-path
    record (via :func:`push_metrics.build_push_record`, the ``recall``
    tool's own call), and compares their ``to_dict()`` key sets — top level
    and per item — so they can never silently diverge in shape again."""
    sidecar_record = push_metrics.PushRecord(
        session_id="s1",
        ts="2026-01-01T00:00:00Z",
        query_hash="deadbeef00000000",
        backend="fts5",
        items=[
            push_metrics.PushedItem(
                id="abc12345", tier="internal", scope="opsadmin", token_cost=10, memory_tier="hot"
            )
        ],
        source="sidecar",
    )
    mcp_record = push_metrics.build_push_record(
        session_id="s1",
        query="some raw prompt text",
        backend="fts5",
        hits=[("abc12345-page.md", {"uid": "abc12345", "audience": ["opsadmin"]}, "body text")],
        memory_tier_by_filename={"abc12345-page.md": "hot"},
    )

    sidecar_dict = sidecar_record.to_dict()
    mcp_dict = mcp_record.to_dict()

    # The ONE intentional discriminator: sidecar rows carry `source`, MCP
    # rows omit it entirely (see PushRecord.source's own docstring / the
    # reader-rule tests in test_push_metrics.py). No other top-level key
    # may differ between the two serializers.
    assert sidecar_dict.keys() - mcp_dict.keys() == {"source"}
    assert mcp_dict.keys() - sidecar_dict.keys() == set()

    # Per-item shape must be IDENTICAL — this is the field-level drift the
    # issue's counter-example names.
    assert sidecar_dict["items"][0].keys() == mcp_dict["items"][0].keys()


# ---------------------------------------------------------------------------
# AC3 — telemetry failure never fails or delays the turn
# ---------------------------------------------------------------------------


def test_cli_survives_an_unwritable_ledger_path(tmp_path: Path) -> None:
    """Counter-example that must fail: an unwritable ledger path taking down
    the push. The ledger path is replaced with a DIRECTORY (deterministic
    across users/containers, unlike a permission-bit test that a root-run
    container could silently bypass) so the append write raises
    ``IsADirectoryError`` — and the CLI must still exit 0 and still print
    its envelope."""
    _build_index(
        tmp_path / "wiki-index.db",
        [
            (
                "abc12345-recall-architecture-note.md",
                "Recall Architecture Note",
                "recall",
                "description number 0",
                "|__access_open__|",
                "hot",
            )
        ],
    )
    (tmp_path / "_push_records.jsonl").mkdir()

    result = _run_context_cli(tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    envelope = json.loads(result.stdout)
    assert envelope["candidates"], "the push itself must still succeed"

    # The unwritable path is untouched (still a directory) — the write
    # attempt failed closed, it did not clobber or crash into something else.
    assert (tmp_path / "_push_records.jsonl").is_dir()


def test_record_context_push_swallows_write_failure_directly(tmp_path: Path) -> None:
    """Same counter-example, exercised directly against
    :func:`record_context_push` rather than through a subprocess — pins the
    no-raise contract at the unit level too."""
    (tmp_path / "_push_records.jsonl").mkdir()
    envelope = {
        "session_id": "s1",
        "query": "q",
        "backend": "fts5",
        "candidates": [
            {
                "filename": "abc12345-page.md",
                "audience": "|__access_open__|",
                "memory_tier": "hot",
                "token_cost": 5,
            }
        ],
    }
    result = record_context_push(envelope, cache_dir=tmp_path)
    assert result is False  # honest: the write did not happen


# ---------------------------------------------------------------------------
# AC4 — memory_tier is carried per item, not just globally
# ---------------------------------------------------------------------------


def test_memory_tier_is_per_item_not_a_single_shared_value(tmp_path: Path) -> None:
    envelope = {
        "session_id": "s1",
        "query": "q",
        "backend": "fts5",
        "candidates": [
            {
                "filename": "aaaaaaaa-hot-page.md",
                "audience": "|",
                "memory_tier": "hot",
                "token_cost": 3,
            },
            {
                "filename": "bbbbbbbb-cold-page.md",
                "audience": "|",
                "memory_tier": "cold",
                "token_cost": 4,
            },
        ],
    }
    assert record_context_push(envelope, cache_dir=tmp_path) is True

    ledger_path = tmp_path / "_push_records.jsonl"
    row = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    tiers_by_id = {it["id"]: it["memory_tier"] for it in row["items"]}
    assert tiers_by_id == {"aaaaaaaa": "hot", "bbbbbbbb": "cold"}
