# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#663 — a reliably-failing file must not be a silent permanent no-progress loop.

``tier3_write`` defers a raw file's disk writes until *every* action for that
file has succeeded (the all-or-nothing boundary, deliberately preserved here —
see the invariant argued in ``tier3_write``'s docstring). A single reliably-
failing LLM call (e.g. an entity page large enough to time out every night)
therefore discards the file's other ~17 successful merges and the file is
retried WHOLE on the next run — forever, silently.

This suite covers the fix, which does NOT weaken the boundary (that cannot be
done safely: actions are re-derived non-deterministically each run, so partial
application + whole-file retry would double-apply). Instead:

- ``tier3_write`` annotates the propagating exception with the failing action's
  ``kind:name``, and applies NO writes on the failure path (invariant preserved).
- the entity loop keeps a persistent per-file consecutive-failure ledger; a file
  over the threshold (on unchanged content) is SKIPPED and surfaced as
  machine-detectable run state (``out_run_stats["stuck_files"]`` + a greppable
  WARNING), so the permanent loop is loud and bounded rather than silent.

All Anthropic calls are mocked; no live API, no network.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.librarian import (
    DEFAULT_STUCK_FILE_THRESHOLD,
    STUCK_FILE_PREFIX,
    STUCK_MANIFEST_NAME,
    _load_stuck_ledger,
    _record_stuck_failure,
    _write_stuck_ledger,
    librarian_stuck_file_threshold,
    run,
)
from athenaeum.models import EntityAction, RawFile
from athenaeum.tiers import tier3_write

# Reuse the deadline suite's run harness — the stuck-file loop lives in the same
# per-file entity phase and must be exercised against the same run scaffold.
from tests.test_librarian_deadline import _seed_knowledge_root


def _make_raw(content: str) -> RawFile:
    return RawFile(
        path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


# ---------------------------------------------------------------------------
# Resolver — env > yaml > default, with the same guards as the sibling knobs
# ---------------------------------------------------------------------------


class TestResolveStuckFileThreshold:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_STUCK_FILE_THRESHOLD", raising=False)
        assert librarian_stuck_file_threshold(None) == DEFAULT_STUCK_FILE_THRESHOLD
        assert librarian_stuck_file_threshold({}) == DEFAULT_STUCK_FILE_THRESHOLD

    def test_yaml_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_STUCK_FILE_THRESHOLD", raising=False)
        assert librarian_stuck_file_threshold({"librarian": {"stuck_file_threshold": 5}}) == 5

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_THRESHOLD", "7")
        assert librarian_stuck_file_threshold({"librarian": {"stuck_file_threshold": 5}}) == 7

    def test_below_one_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A threshold < 1 would quarantine a file on its very first transient
        # failure, defeating the "N nights running" contract.
        monkeypatch.delenv("ATHENAEUM_STUCK_FILE_THRESHOLD", raising=False)
        assert librarian_stuck_file_threshold({"librarian": {"stuck_file_threshold": 0}}) == (
            DEFAULT_STUCK_FILE_THRESHOLD
        )

    def test_bool_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `stuck_file_threshold: yes` parses as True (int subclass) — must NOT
        # become a threshold of 1.
        monkeypatch.delenv("ATHENAEUM_STUCK_FILE_THRESHOLD", raising=False)
        assert librarian_stuck_file_threshold({"librarian": {"stuck_file_threshold": True}}) == (
            DEFAULT_STUCK_FILE_THRESHOLD
        )

    def test_non_numeric_env_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_STUCK_FILE_THRESHOLD", "not-a-number")
        assert librarian_stuck_file_threshold(None) == DEFAULT_STUCK_FILE_THRESHOLD


# ---------------------------------------------------------------------------
# The all-or-nothing boundary is PRESERVED and the failing action is named
# ---------------------------------------------------------------------------


def _write_page(path: Path, uid: str, name: str) -> None:
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: person\n---\nBody of {name}.\n",
        encoding="utf-8",
    )


def test_tier3_write_mid_file_failure_applies_no_writes_and_names_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing invariant test (AC 2/3/4).

    Two update actions: the first merge SUCCEEDS, the second RAISES. The
    boundary must hold — because the flush is deferred until after every action
    succeeds, the first (successful) merge must NOT be written to disk, so no
    partial/corrupt state is reachable. And the propagating exception must name
    the failing action so the caller can record which entity is stuck.

    Fails against the pre-athenaeum#663 code: it never set ``athenaeum_failing_action``.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page_a = wiki / "a.md"
    page_b = wiki / "b.md"
    _write_page(page_a, "uid-a", "Alice")
    _write_page(page_b, "uid-b", "Bob")
    before_a = page_a.read_text(encoding="utf-8")
    before_b = page_b.read_text(encoding="utf-8")

    index = MagicMock()
    index.get_by_uid = lambda uid: {"uid-a": page_a, "uid-b": page_b}[uid]

    def fake_merge(action, existing_body, ref, client, **kwargs):
        if action.name == "Bob":
            raise RuntimeError("boom: Bob's page times out")
        return ("MERGED " + existing_body, None)

    monkeypatch.setattr("athenaeum.tiers.tier3_merge", fake_merge)

    actions = [
        EntityAction(
            kind="update", name="Alice", entity_type="", tags=[], access="",
            existing_uid="uid-a", observations="obs a",
        ),
        EntityAction(
            kind="update", name="Bob", entity_type="", tags=[], access="",
            existing_uid="uid-b", observations="obs b",
        ),
    ]

    raw = _make_raw("Notes about Alice and Bob.")
    with pytest.raises(RuntimeError) as excinfo:
        tier3_write(raw, actions, index, wiki, MagicMock())

    # The failing action is named on the exception.
    assert getattr(excinfo.value, "athenaeum_failing_action", None) == "update:Bob"

    # Invariant preserved: NEITHER page changed on disk. Alice's merge succeeded
    # in-memory but the deferred flush never ran, so no partial write landed.
    assert page_a.read_text(encoding="utf-8") == before_a
    assert page_b.read_text(encoding="utf-8") == before_b


# ---------------------------------------------------------------------------
# Ledger primitives
# ---------------------------------------------------------------------------


def test_ledger_roundtrip_and_empty_removes_file(tmp_path: Path) -> None:
    entry = {"hash": "abc123", "failures": 2, "escalated": True, "last_action": "update:X"}
    _write_stuck_ledger(tmp_path, {"sessions/x.md": entry})
    assert (tmp_path / STUCK_MANIFEST_NAME).exists()
    assert _load_stuck_ledger(tmp_path) == {"sessions/x.md": entry}

    # An empty ledger removes the file so a recovered corpus leaves no stale record.
    _write_stuck_ledger(tmp_path, {})
    assert not (tmp_path / STUCK_MANIFEST_NAME).exists()


def test_corrupt_ledger_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / STUCK_MANIFEST_NAME).write_text("{ not valid json", encoding="utf-8")
    # A corrupt ledger must never wedge a run — fail-open to "nothing stuck".
    assert _load_stuck_ledger(tmp_path) == {}


def test_record_crosses_threshold_exactly_once(tmp_path: Path) -> None:
    ledger: dict = {}
    raw = _make_raw("payload")
    assert _record_stuck_failure(ledger, raw, error="E", action="update:X", threshold=2) is None
    crossed = _record_stuck_failure(ledger, raw, error="E", action="update:X", threshold=2)
    assert crossed is not None
    assert crossed["failures"] == 2
    assert crossed["escalated"] is True
    assert crossed["last_action"] == "update:X"
    # A file that STAYS stuck is not re-surfaced as "newly stuck" every night.
    assert _record_stuck_failure(ledger, raw, error="E", action="update:X", threshold=2) is None
    assert ledger[raw.ref]["failures"] == 3


def test_content_change_resets_consecutive_count(tmp_path: Path) -> None:
    ledger: dict = {}
    raw1 = _make_raw("original content")
    _record_stuck_failure(ledger, raw1, error="E", action="update:X", threshold=3)
    _record_stuck_failure(ledger, raw1, error="E", action="update:X", threshold=3)
    assert ledger[raw1.ref]["failures"] == 2

    # Same ref (same path) but the author edited the file — a fresh attempt.
    raw2 = _make_raw("EDITED content, hopefully no longer times out")
    _record_stuck_failure(ledger, raw2, error="E", action="update:X", threshold=3)
    assert ledger[raw1.ref]["failures"] == 1


# ---------------------------------------------------------------------------
# End-to-end: a file failing N runs crosses, is surfaced, then skipped
# ---------------------------------------------------------------------------


def _raising_process_one(action: str = "update:Foo"):
    """A ``process_one`` stand-in that fails every file, naming the action the
    way ``tier3_write`` annotates a real mid-file timeout."""
    calls = {"n": 0}

    def fake_process_one(raw, *args, **kwargs):
        calls["n"] += 1
        exc = RuntimeError("simulated reliably-failing action")
        # Mirror tier3_write's annotation so the ledger records the failing action.
        exc.athenaeum_failing_action = action  # type: ignore[attr-defined]
        raise exc

    return fake_process_one, calls


def test_reliably_failing_file_becomes_stuck_and_is_then_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The regression this issue is about — verified against the run harness.

    Threshold 2: run 1 fails (count 1, not yet stuck); run 2 fails (count 2 —
    crosses, surfaced); run 3 SKIPS the file entirely (process_one is never
    called for it) and still surfaces it. Against the pre-athenaeum#663 code
    ``out_run_stats`` had no ``stuck_files`` key and process_one was re-invoked
    forever — this test fails there.
    """
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_STUCK_FILE_THRESHOLD", "2")

    fake_process_one, calls = _raising_process_one(action="update:BigPage")
    monkeypatch.setattr("athenaeum.librarian.process_one", fake_process_one)

    def _run() -> dict:
        stats: dict = {}
        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=0,  # deadline disabled — isolate the stuck-file behavior
            out_run_stats=stats,
        )
        return stats

    # Run 1: first failure — not yet stuck.
    stats1 = _run()
    assert stats1["stuck_files"] == []
    assert calls["n"] == 1
    ledger = _load_stuck_ledger(root / "wiki")
    ref = next(iter(ledger))
    assert ledger[ref]["failures"] == 1

    # Run 2: second failure crosses the threshold — surfaced as run state.
    caplog.clear()
    stats2 = _run()
    assert calls["n"] == 2  # process_one WAS called again (still retrying)
    assert len(stats2["stuck_files"]) == 1
    surfaced = stats2["stuck_files"][0]
    assert surfaced["ref"] == ref
    assert surfaced["failures"] == 2
    assert surfaced["action"] == "update:BigPage"
    assert "RuntimeError" in surfaced["error"]
    assert STUCK_FILE_PREFIX in caplog.text

    # Run 3: the file is now known-stuck — skipped, NOT re-attempted, still surfaced.
    caplog.clear()
    stats3 = _run()
    assert calls["n"] == 2, "a stuck file must NOT consume another process_one call"
    assert len(stats3["stuck_files"]) == 1
    assert stats3["stuck_files"][0]["ref"] == ref
    assert STUCK_FILE_PREFIX in caplog.text
