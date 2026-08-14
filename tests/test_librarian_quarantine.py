# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#898 — per-file size/cost bound + quarantine for poison files.

Mirrors ``tests/test_librarian_stuck_files.py`` (the athenaeum#663 precedent this
issue explicitly builds on) in structure: resolver tests, ledger-primitive
tests, then end-to-end coverage against the real ``run()`` harness.

Acceptance criteria under test:

- AC 1: the per-file byte bound, enforced at ``RawFile.content`` (see also
  ``tests/test_librarian.py::TestRawFileContent`` for the property-level
  unit tests; this file covers it through the full entity-loop integration).
- AC 2: the per-file LLM-call and wall-clock bounds, enforced by the entity
  phase runner after each file's ``process_one`` call.
- AC 3/7: N-consecutive-run quarantine for both a synthetic oversized file
  and a synthetic budget-looping file.
- AC 4/5: the quarantine ledger + pending-decisions listing surface.
- AC 6: reversibility via ``release_quarantine``.
- The consecutive-count reset on content change (mirrors athenaeum#663's
  ``test_content_change_resets_consecutive_count``).

All Anthropic calls are mocked/never reached; no live API, no network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.decisions import list_pending_decisions
from athenaeum.librarian import (
    DEFAULT_QUARANTINE_THRESHOLD,
    QUARANTINE_CANDIDATE_MANIFEST_NAME,
    QUARANTINE_FILE_PREFIX,
    _load_quarantine_candidates,
    _record_bound_violation,
    _write_quarantine_candidates,
    discover_raw_files,
    librarian_quarantine_threshold,
    run,
)
from athenaeum.models import RawFile
from athenaeum.quarantine import list_pending_quarantine, release_quarantine

# Reuse the deadline suite's run harness — the quarantine loop lives in the
# same per-file entity phase and must be exercised against the same scaffold.
from tests.test_librarian_deadline import _seed_knowledge_root


def _make_raw(path: Path = Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md")) -> RawFile:
    return RawFile(path=path, source="sessions", timestamp="20240407T120000Z", uuid8="aabb0011")


# ---------------------------------------------------------------------------
# Resolver — env > yaml > default, with the same guards as the sibling knob
# ---------------------------------------------------------------------------


class TestResolveQuarantineThreshold:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_QUARANTINE_THRESHOLD", raising=False)
        assert librarian_quarantine_threshold(None) == DEFAULT_QUARANTINE_THRESHOLD
        assert librarian_quarantine_threshold({}) == DEFAULT_QUARANTINE_THRESHOLD
        assert DEFAULT_QUARANTINE_THRESHOLD == 2

    def test_yaml_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_QUARANTINE_THRESHOLD", raising=False)
        assert librarian_quarantine_threshold({"librarian": {"quarantine_threshold": 5}}) == 5

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_QUARANTINE_THRESHOLD", "7")
        assert librarian_quarantine_threshold({"librarian": {"quarantine_threshold": 5}}) == 7

    def test_below_one_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_QUARANTINE_THRESHOLD", raising=False)
        assert librarian_quarantine_threshold({"librarian": {"quarantine_threshold": 0}}) == (
            DEFAULT_QUARANTINE_THRESHOLD
        )

    def test_bool_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_QUARANTINE_THRESHOLD", raising=False)
        assert librarian_quarantine_threshold({"librarian": {"quarantine_threshold": True}}) == (
            DEFAULT_QUARANTINE_THRESHOLD
        )

    def test_non_numeric_env_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_QUARANTINE_THRESHOLD", "not-a-number")
        assert librarian_quarantine_threshold(None) == DEFAULT_QUARANTINE_THRESHOLD


# ---------------------------------------------------------------------------
# Ledger primitives
# ---------------------------------------------------------------------------


def test_ledger_roundtrip_and_empty_removes_file(tmp_path: Path) -> None:
    entry = {"hash": "abc123", "violations": 2, "escalated": True, "last_bound": "bytes"}
    _write_quarantine_candidates(tmp_path, {"sessions/x.md": entry})
    assert (tmp_path / QUARANTINE_CANDIDATE_MANIFEST_NAME).exists()
    assert _load_quarantine_candidates(tmp_path) == {"sessions/x.md": entry}

    _write_quarantine_candidates(tmp_path, {})
    assert not (tmp_path / QUARANTINE_CANDIDATE_MANIFEST_NAME).exists()


def test_corrupt_ledger_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / QUARANTINE_CANDIDATE_MANIFEST_NAME).write_text(
        "{ not valid json", encoding="utf-8"
    )
    assert _load_quarantine_candidates(tmp_path) == {}


def test_record_crosses_threshold_exactly_once(tmp_path: Path) -> None:
    ledger: dict = {}
    raw = _make_raw()
    assert (
        _record_bound_violation(ledger, raw, bound="bytes", detail="d1", threshold=2) is None
    )
    crossed = _record_bound_violation(ledger, raw, bound="bytes", detail="d2", threshold=2)
    assert crossed is not None
    assert crossed["violations"] == 2
    assert crossed["escalated"] is True
    assert crossed["last_bound"] == "bytes"
    assert crossed["last_detail"] == "d2"
    # A candidate that stays over-bound is not re-surfaced as "newly crossed".
    assert (
        _record_bound_violation(ledger, raw, bound="bytes", detail="d3", threshold=2) is None
    )
    assert ledger[raw.ref]["violations"] == 3


def test_content_change_resets_consecutive_count(tmp_path: Path) -> None:
    """Mirrors athenaeum#663's identical test for the stuck-file ledger."""
    ledger: dict = {}
    fpath = tmp_path / "20240407T120000Z-aabb0011.md"
    fpath.write_text("original content", encoding="utf-8")
    raw1 = RawFile(path=fpath, source="sessions", timestamp="", uuid8="")
    _record_bound_violation(ledger, raw1, bound="bytes", detail="d", threshold=3)
    _record_bound_violation(ledger, raw1, bound="bytes", detail="d", threshold=3)
    assert ledger[raw1.ref]["violations"] == 2

    # Same ref (same path), but the file's mtime/size changed underneath it
    # (an edit) — a fresh attempt.
    fpath.write_text("EDITED, hopefully no longer oversized", encoding="utf-8")
    raw2 = RawFile(path=fpath, source="sessions", timestamp="", uuid8="")
    _record_bound_violation(ledger, raw2, bound="bytes", detail="d", threshold=3)
    assert ledger[raw1.ref]["violations"] == 1


# ---------------------------------------------------------------------------
# End-to-end: AC 7 — a synthetic OVERSIZED file is quarantined after N runs
# ---------------------------------------------------------------------------


def test_oversized_file_is_quarantined_after_n_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC 1/3/4/7: a file over the byte bound never reaches process_one (zero
    LLM cost), and after the (default 2) consecutive-run threshold is moved
    out of the discovery set with an audit-ledger record.
    """
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_QUARANTINE_THRESHOLD", "2")
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_BYTES", "50")

    raw_file = next((root / "raw" / "sessions").glob("*.md"))
    raw_file.write_text("x" * 500, encoding="utf-8")

    # process_one is never invoked for an oversized file (the byte bound
    # trips at raw.content, the very first line of the real process_one) —
    # deliberately NOT stubbed, so this exercises the real enforcement path.

    def _run() -> dict:
        stats: dict = {}
        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=0,
            out_run_stats=stats,
        )
        return stats

    # Run 1: first violation — not yet quarantined; file stays on disk.
    stats1 = _run()
    assert stats1["quarantined_files"] == []
    assert raw_file.exists()
    candidates = _load_quarantine_candidates(root / "wiki")
    assert candidates[next(iter(candidates))]["violations"] == 1

    # Run 2: second violation crosses the threshold — quarantined.
    caplog.clear()
    stats2 = _run()
    assert len(stats2["quarantined_files"]) == 1
    quarantined = stats2["quarantined_files"][0]
    assert quarantined["ref"] == "sessions/" + raw_file.name
    assert quarantined["bound"] == "bytes"
    assert quarantined["violations"] == 2
    assert QUARANTINE_FILE_PREFIX in caplog.text

    # Moved out of the discovery set (AC 4).
    assert not raw_file.exists()
    assert discover_raw_files(root / "raw") == []
    moved = root / "wiki" / "_quarantine" / "sessions" / raw_file.name
    assert moved.exists()

    # The candidate ledger no longer tracks it (terminal disposition).
    assert _load_quarantine_candidates(root / "wiki") == {}

    # Pending-decision listing surface (AC 5).
    pending = list_pending_quarantine(root / "wiki")
    assert len(pending) == 1
    decisions = list_pending_decisions(root / "wiki")
    assert any(d["type"] == "quarantine" for d in decisions)


# ---------------------------------------------------------------------------
# End-to-end: AC 7 — a synthetic BUDGET-LOOPING file is quarantined after N
# runs
# ---------------------------------------------------------------------------


def _looping_process_one_factory(calls_per_file: int):
    """A ``process_one`` stand-in that "succeeds" but blows the per-file LLM-
    call bound every run — never creates/updates anything, so the vanilla
    success path (delete raw, count created/updated) would otherwise apply."""

    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        usage = kwargs.get("usage")
        if usage is not None:
            usage.api_calls += calls_per_file
        return SimpleNamespace(created=[], updated=[], escalated=[], skipped=[])

    return fake_process_one


def test_budget_looping_file_is_quarantined_after_n_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC 2/3/4/7: a file whose compilation reliably blows its per-file
    LLM-call bound is left on disk (not deleted, not counted) on the runs
    below threshold, then quarantined on the run that crosses it.
    """
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_QUARANTINE_THRESHOLD", "2")
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_API_CALLS", "5")

    raw_file = next((root / "raw" / "sessions").glob("*.md"))
    monkeypatch.setattr(
        "athenaeum.librarian.process_one", _looping_process_one_factory(calls_per_file=50)
    )

    def _run() -> dict:
        stats: dict = {}
        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=1000,
            max_runtime=0,
            out_run_stats=stats,
        )
        return stats

    # Run 1: over-bound — result NOT applied; file stays on disk.
    stats1 = _run()
    assert stats1["quarantined_files"] == []
    assert raw_file.exists()

    # Run 2: crosses the threshold — quarantined.
    caplog.clear()
    stats2 = _run()
    assert len(stats2["quarantined_files"]) == 1
    quarantined = stats2["quarantined_files"][0]
    assert quarantined["bound"] == "llm_calls"
    assert quarantined["violations"] == 2
    assert QUARANTINE_FILE_PREFIX in caplog.text
    assert not raw_file.exists()
    moved = root / "wiki" / "_quarantine" / "sessions" / raw_file.name
    assert moved.exists()


def test_wall_clock_bound_violation_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2: the wall-clock bound (independent of the call-count bound
    above) is also checked and recorded — a file whose process_one takes too
    long, even at zero LLM calls, is flagged on the wall-clock bound."""
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS", "10")

    clock = {"n": 0.0}

    def _fake_monotonic() -> float:
        return clock["n"]

    monkeypatch.setattr("athenaeum.librarian.time.monotonic", _fake_monotonic)

    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        clock["n"] += 20.0  # exceeds the 10s bound
        return SimpleNamespace(created=[], updated=[], escalated=[], skipped=[])

    monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)

    stats: dict = {}
    run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=0,
        out_run_stats=stats,
    )
    candidates = _load_quarantine_candidates(root / "wiki")
    assert len(candidates) == 1
    entry = candidates[next(iter(candidates))]
    assert entry["last_bound"] == "wall_clock"
    assert entry["violations"] == 1


# ---------------------------------------------------------------------------
# AC 6 — reversibility: an operator decision returns the file to the
# discovery set
# ---------------------------------------------------------------------------


def test_release_quarantine_returns_file_to_discovery_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_QUARANTINE_THRESHOLD", "1")
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_BYTES", "50")

    raw_file = next((root / "raw" / "sessions").glob("*.md"))
    raw_file.write_text("x" * 500, encoding="utf-8")

    run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=0,
    )
    assert not raw_file.exists()
    assert discover_raw_files(root / "raw") == []

    pending = list_pending_quarantine(root / "wiki")
    assert len(pending) == 1
    quarantine_id = pending[0]["id"]

    release_quarantine(
        root / "wiki",
        root / "raw",
        quarantine_id=quarantine_id,
        note="operator reviewed: legitimate large import, raising the bound",
    )

    # Back in the discovery set (AC 6) — no code change, no automatic
    # un-quarantine, purely the operator decision.
    assert raw_file.exists()
    refs = {f.ref for f in discover_raw_files(root / "raw")}
    assert "sessions/" + raw_file.name in refs
    assert list_pending_quarantine(root / "wiki") == []
