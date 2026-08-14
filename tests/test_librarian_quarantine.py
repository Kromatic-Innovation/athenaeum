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

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import anthropic as anthropic_mod
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
from athenaeum.quarantine import list_pending_quarantine, quarantine_file, release_quarantine

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


def test_llm_calls_bound_uses_content_hash_not_stat_so_mtime_change_alone_does_not_reset(
    tmp_path: Path,
) -> None:
    """Code-review finding (athenaeum#898): for ``llm_calls``/``wall_clock`` the
    fingerprint must be a CONTENT hash, not the ``bytes`` bound's stat-based
    one. A file that crosses either of these two bounds was, by
    construction, already read in full by ``process_one`` to spend those
    calls / that time — so hashing it here costs nothing additional, and
    doing so is what makes the ledger key survive anything that
    re-provisions the raw checkout WITHOUT preserving mtimes (a fresh
    clone, ``rsync`` without ``-t``, a tar extract, a backup restore, an
    in-place editor rewrite that touches mtime but not bytes). A stat-based
    fingerprint would silently reset the violation count on unchanged
    content in exactly that cron-style redeploy, and a genuinely
    pathological file would then sit at ``violations: 1`` forever.
    """
    ledger: dict = {}
    fpath = tmp_path / "20240407T120000Z-aabb0011.md"
    fpath.write_text("a slow-to-classify file", encoding="utf-8")
    raw1 = RawFile(path=fpath, source="sessions", timestamp="", uuid8="")
    _record_bound_violation(ledger, raw1, bound="llm_calls", detail="d", threshold=3)

    # Touch ONLY the mtime — byte-identical content, as a redeploy that
    # doesn't preserve timestamps would produce.
    st = fpath.stat()
    os.utime(fpath, (st.st_atime + 10_000, st.st_mtime + 10_000))
    raw2 = RawFile(path=fpath, source="sessions", timestamp="", uuid8="")
    crossed = _record_bound_violation(ledger, raw2, bound="llm_calls", detail="d", threshold=3)

    # The streak CONTINUED (2, not reset to 1) — proves the fingerprint is
    # content-based, immune to the mtime-only change.
    assert crossed is None  # threshold is 3; this is only violation #2
    assert ledger[raw1.ref]["violations"] == 2

    # A GENUINE content edit still resets it, same as the bytes bound.
    fpath.write_text("a DIFFERENT slow-to-classify file", encoding="utf-8")
    raw3 = RawFile(path=fpath, source="sessions", timestamp="", uuid8="")
    _record_bound_violation(ledger, raw3, bound="llm_calls", detail="d", threshold=3)
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


def test_dry_run_byte_bound_hit_records_nothing_and_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COULD-FIX (code review): ``RawFile.content`` is read unconditionally
    near the top of ``process_one`` — even in dry-run, BEFORE the
    ``if dry_run:`` early return — so a dry run over an oversized file still
    observes ``RawFileTooLargeError``. The code already gates the
    consecutive-violation ledger write and the quarantine action itself on
    ``if not ctx.dry_run:``; this pins that a dry run touches neither.
    """
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_BYTES", "50")

    raw_file = next((root / "raw" / "sessions").glob("*.md"))
    raw_file.write_text("x" * 500, encoding="utf-8")

    stats: dict = {}
    run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        dry_run=True,
        max_api_calls=100,
        max_runtime=0,
        out_run_stats=stats,
    )

    assert stats["quarantined_files"] == []
    assert raw_file.exists()
    assert _load_quarantine_candidates(root / "wiki") == {}
    assert list_pending_quarantine(root / "wiki") == []


# ---------------------------------------------------------------------------
# SHOULD-FIX (code review): a failure DURING the quarantine action itself
# (disk-full, permission error, SIGTERM) must not crash the run and must
# not lose track of the file.
# ---------------------------------------------------------------------------


def test_quarantine_action_failure_does_not_crash_run_and_stays_retry_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The entity loop wraps the physical quarantine attempt — a failure
    there (simulated here as the underlying quarantine_file call raising)
    must log and leave the file's candidate-ledger entry retry-eligible,
    not crash the nightly run and not silently drop the file's violation
    history. The NEXT run, with the transient failure gone, completes the
    quarantine using the SAME violation streak (not restarted from zero).
    """
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_QUARANTINE_THRESHOLD", "2")
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_BYTES", "50")

    raw_file = next((root / "raw" / "sessions").glob("*.md"))
    raw_file.write_text("x" * 500, encoding="utf-8")

    def _run(out_stats: dict | None = None) -> int:
        return run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=0,
            out_run_stats=out_stats,
        )

    # Run 1: first violation, not yet crossing.
    _run()
    assert raw_file.exists()

    # Run 2 would cross the threshold and quarantine — simulate the
    # quarantine action itself failing (disk-full, permission error, or a
    # SIGTERM landing mid-move).
    def _boom(*args, **kwargs):
        raise OSError("simulated disk-full during quarantine")

    monkeypatch.setattr("athenaeum.librarian._quarantine_file", _boom)

    caplog.clear()
    stats2: dict = {}
    rc = _run(stats2)

    # Fail-open: the run completes normally rather than crashing.
    assert rc == 0
    assert stats2["quarantined_files"] == []
    assert "failed to quarantine" in caplog.text.lower()

    # Not moved (the failure happened inside the quarantine attempt) and
    # the candidate ledger entry survives, retry-eligible.
    assert raw_file.exists()
    candidates = _load_quarantine_candidates(root / "wiki")
    assert len(candidates) == 1
    entry = candidates[next(iter(candidates))]
    assert entry["violations"] == 2
    assert entry["escalated"] is False
    assert list_pending_quarantine(root / "wiki") == []

    # Run 3: the transient failure is gone — restore the real quarantine
    # action. The SAME violation streak (still >= threshold, now
    # un-escalated) retries and succeeds, rather than needing to
    # re-accumulate from zero.
    monkeypatch.setattr("athenaeum.librarian._quarantine_file", quarantine_file)
    stats3: dict = {}
    _run(stats3)
    assert len(stats3["quarantined_files"]) == 1
    assert not raw_file.exists()
    moved = root / "wiki" / "_quarantine" / "sessions" / raw_file.name
    assert moved.exists()


# ---------------------------------------------------------------------------
# End-to-end: AC 7 — a synthetic BUDGET-LOOPING file is quarantined after N
# runs
# ---------------------------------------------------------------------------


def _make_over_budget_client(n_creates: int) -> MagicMock:
    """A REAL (mocked only at the Anthropic HTTP boundary) LLM client whose
    classify response proposes *n_creates* new entities — enough Tier-3
    create calls that ONE file's total LLM-call count exceeds a small
    per-file bound.

    Code-review finding (athenaeum#898): the original version of this test
    monkeypatched ``librarian.process_one`` WHOLESALE with a stand-in that
    never wrote anything, so it could not distinguish "the over-bound result
    is truly discarded" from "the over-bound result is merely uncounted
    while its writes already landed on disk" — exactly the bug that shipped
    (`tier3_derive_actions`'s update flush and `process_one`'s own
    create-write loop both ran to completion before the old post-hoc check
    ever fired). Driving the REAL `tier2_classify` -> `tier3_derive_actions`
    -> (bound check) -> write path, with only the network boundary mocked,
    is what makes a regression in that ordering fail this test.
    """
    classify_response = MagicMock()
    classify_response.content = [
        MagicMock(
            text=json.dumps(
                [
                    {
                        "name": f"Budget Blower {i}",
                        "entity_type": "concept",
                        "tags": [],
                        "access": "internal",
                        "observations": f"Observation {i}.",
                    }
                    for i in range(n_creates)
                ]
            )
        )
    ]
    create_responses = []
    for i in range(n_creates):
        r = MagicMock()
        r.content = [MagicMock(text=f"# Budget Blower {i}\n\nObservation {i}.\n")]
        create_responses.append(r)
    client = MagicMock()
    client.messages.create.side_effect = [classify_response, *create_responses]
    return client


def _entity_pages(wiki_root: Path) -> list[Path]:
    return [p for p in wiki_root.glob("*.md") if not p.name.startswith("_")]


def test_budget_looping_file_writes_no_pages_and_is_quarantined_after_n_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC 2/3/4/7, and the regression test for the "discard is not honoured"
    code-review finding: a file whose classification proposes enough new
    entities to blow the per-file LLM-call bound must have NONE of those
    entity pages written to wiki_root — on the run below threshold AND on
    the crossing run — not merely be left uncounted while pages accumulate
    underneath the accounting. The raw file itself is left on disk (not
    deleted) below threshold, then quarantined on the run that crosses it.
    """
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_QUARANTINE_THRESHOLD", "2")
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_API_CALLS", "2")

    raw_file = next((root / "raw" / "sessions").glob("*.md"))
    # 1 classify call + 4 create calls = 5 calls for this one file — well
    # over the 2-call bound configured above. A fresh client (fresh
    # side_effect queue) is built on every `anthropic.Anthropic(...)` call,
    # so this works unchanged across both runs below.
    monkeypatch.setattr(
        anthropic_mod, "Anthropic", lambda **kwargs: _make_over_budget_client(4)
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

    # Run 1: over-bound — the LLM calls happened (all 4 "Budget Blower"
    # entities were classified and drafted), but the result must be
    # discarded BEFORE any of them is written.
    stats1 = _run()
    assert stats1["quarantined_files"] == []
    assert raw_file.exists()
    assert _entity_pages(root / "wiki") == [], (
        "an over-bound process_one wrote an entity page — the bug this test "
        "regresses: 'discarded' must mean never written, not merely uncounted"
    )

    # Run 2: crosses the threshold — quarantined. Still zero pages written.
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
    assert _entity_pages(root / "wiki") == []


def test_wall_clock_bound_full_cycle_to_quarantine_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC 2/3/4/5/6/7: the wall-clock bound (independent of the call-count
    bound above) drives the SAME full crossing -> quarantine -> release
    cycle the byte and llm_calls bounds get — closing the loop the earlier
    version of this test left open (it asserted only one recorded
    violation, never a full crossing)."""
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_QUARANTINE_THRESHOLD", "2")
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS", "10")

    raw_file = next((root / "raw" / "sessions").glob("*.md"))

    # Issue athenaeum#898 code review: the wall-clock check now lives INSIDE the
    # real process_one (checked right after tier3_derive_actions, before any
    # write), not in the entity loop after process_one returns — so a
    # wholesale process_one stand-in would bypass it entirely (the entity
    # loop no longer performs its own post-hoc check). This drives the REAL
    # process_one, mocked only at the Anthropic HTTP boundary, same as the
    # llm_calls test above; the clock advances as a side effect of each
    # mocked LLM call, simulating real wall-clock elapsing during a slow
    # call.
    clock = {"n": 0.0}
    monkeypatch.setattr("athenaeum.librarian.time.monotonic", lambda: clock["n"])

    classify_response = MagicMock()
    classify_response.content = [
        MagicMock(
            text=json.dumps(
                [
                    {
                        "name": "Slow Entity",
                        "entity_type": "concept",
                        "tags": [],
                        "access": "internal",
                        "observations": "Observation.",
                    }
                ]
            )
        )
    ]
    create_response = MagicMock()
    create_response.content = [MagicMock(text="# Slow Entity\n\nObservation.\n")]

    def _slow_client() -> MagicMock:
        responses = iter([classify_response, create_response])

        def _side_effect(**kwargs):
            clock["n"] += 20.0  # exceeds the 10s bound on the very first call
            return next(responses)

        client = MagicMock()
        client.messages.create.side_effect = _side_effect
        return client

    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: _slow_client())

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

    # Run 1: one recorded violation, not yet quarantined — and, same
    # regression proof as the llm_calls test, "Slow Entity" is never written.
    stats1 = _run()
    assert stats1["quarantined_files"] == []
    candidates = _load_quarantine_candidates(root / "wiki")
    entry = candidates[next(iter(candidates))]
    assert entry["last_bound"] == "wall_clock"
    assert entry["violations"] == 1
    assert raw_file.exists()
    assert _entity_pages(root / "wiki") == []

    # Run 2: crosses the threshold — quarantined. Still zero pages written.
    caplog.clear()
    stats2 = _run()
    assert len(stats2["quarantined_files"]) == 1
    quarantined = stats2["quarantined_files"][0]
    assert quarantined["bound"] == "wall_clock"
    assert quarantined["violations"] == 2
    assert QUARANTINE_FILE_PREFIX in caplog.text
    assert not raw_file.exists()
    assert _load_quarantine_candidates(root / "wiki") == {}
    assert _entity_pages(root / "wiki") == []

    # AC 6: released -> back in the discovery set.
    pending = list_pending_quarantine(root / "wiki")
    assert len(pending) == 1
    release_quarantine(
        root / "wiki", root / "raw", quarantine_id=pending[0]["id"], note="reviewed"
    )
    assert raw_file.exists()
    refs = {f.ref for f in discover_raw_files(root / "raw")}
    assert "sessions/" + raw_file.name in refs
    assert list_pending_quarantine(root / "wiki") == []


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
