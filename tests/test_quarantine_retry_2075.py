# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2075: size-aware LLM-call bound and quarantine retries over days.

AC1 size-aware bound, AC2 backoff schedule with a relaxed bound, AC3 transient
first wall-clock trip, AC4 attempt history on the decision item, AC5 re-queue
CLI. No live API; the Anthropic boundary is mocked as in
``tests/test_librarian_quarantine.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic as anthropic_mod
import pytest

from athenaeum.cli import main
from athenaeum.config import (
    effective_raw_file_llm_call_limit,
    effective_raw_file_runtime_limit,
    resolve_quarantine_bound_relax_factor,
    resolve_quarantine_max_attempts,
    resolve_quarantine_retry_delays_days,
    resolve_raw_file_max_api_calls_by_source,
)
from athenaeum.decisions import list_pending_decisions
from athenaeum.librarian import (
    QUARANTINE_CANDIDATE_MANIFEST_NAME,
    _load_quarantine_candidates,
    _record_bound_violation,
    run,
)
from athenaeum.models import RawFile
from athenaeum.quarantine import (
    list_pending_quarantine,
    quarantine_file,
    read_quarantine_ledger,
    requeue_legacy_quarantine,
)
from tests.test_librarian_deadline import _seed_knowledge_root
from tests.test_librarian_quarantine import _entity_pages, _make_over_budget_client

_ENV = (
    "ATHENAEUM_QUARANTINE_MAX_ATTEMPTS",
    "ATHENAEUM_QUARANTINE_RETRY_DELAYS_DAYS",
    "ATHENAEUM_QUARANTINE_BOUND_RELAX_FACTOR",
    "ATHENAEUM_RAW_FILE_MAX_API_CALLS",
    "ATHENAEUM_RAW_FILE_LLM_CALLS_CHUNK_CHARS",
    "ATHENAEUM_RAW_FILE_LLM_CALLS_PER_CHUNK",
    "ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def _raw(tmp_path: Path, body: str = "hello") -> RawFile:
    d = tmp_path / "raw" / "retros"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "20240407T120000Z-aabb0011.md"
    path.write_text(body, encoding="utf-8")
    return RawFile(path=path, source="retros", timestamp="20240407T120000Z", uuid8="aabb0011")


# --------------------------------------------------------------------- AC1


class TestSizeAwareBound:
    def test_bound_scales_with_size(self) -> None:
        small = effective_raw_file_llm_call_limit(None, source="retros", chars=100)
        big = effective_raw_file_llm_call_limit(None, source="retros", chars=40_000)
        assert small == 60
        assert big == 60 + 20  # 40_000 // 2000 chunks * 1 call

    def test_per_source_base(self) -> None:
        cfg = {"librarian": {"raw_file_max_api_calls_by_source": {"retros": 120, "bad": True}}}
        assert resolve_raw_file_max_api_calls_by_source(cfg) == {"retros": 120}
        assert effective_raw_file_llm_call_limit(cfg, source="retros", chars=0) == 120
        assert effective_raw_file_llm_call_limit(cfg, source="sessions", chars=0) == 60

    def test_chunk_knobs_from_yaml_and_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = {
            "librarian": {"raw_file_llm_calls_chunk_chars": 1000, "raw_file_llm_calls_per_chunk": 2}
        }
        assert effective_raw_file_llm_call_limit(cfg, source="x", chars=3000) == 60 + 6
        monkeypatch.setenv("ATHENAEUM_RAW_FILE_LLM_CALLS_PER_CHUNK", "3")
        assert effective_raw_file_llm_call_limit(cfg, source="x", chars=3000) == 60 + 9

    def test_relaxation_per_attempt(self) -> None:
        assert effective_raw_file_llm_call_limit(None, source="x", chars=0, attempts=1) == 90
        assert effective_raw_file_llm_call_limit(None, source="x", chars=0, attempts=2) == 135
        assert effective_raw_file_runtime_limit(None, attempts=0) == 900
        assert effective_raw_file_runtime_limit(None, attempts=2) == 2025


class TestScheduleResolvers:
    def test_defaults(self) -> None:
        assert resolve_quarantine_max_attempts(None) == 3
        assert resolve_quarantine_retry_delays_days(None) == [3, 4]
        assert resolve_quarantine_bound_relax_factor(None) == 1.5

    def test_overrides_and_rejections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = {
            "librarian": {
                "quarantine_retry_delays_days": [1, 2, 3],
                "quarantine_bound_relax_factor": 0.5,
            }
        }
        assert resolve_quarantine_retry_delays_days(cfg) == [1, 2, 3]
        assert resolve_quarantine_bound_relax_factor(cfg) == 1.5  # < 1.0 rejected
        monkeypatch.setenv("ATHENAEUM_QUARANTINE_RETRY_DELAYS_DAYS", "5,6")
        assert resolve_quarantine_retry_delays_days(cfg) == [5, 6]
        monkeypatch.setenv("ATHENAEUM_QUARANTINE_RETRY_DELAYS_DAYS", "x")
        assert resolve_quarantine_retry_delays_days(cfg) == [1, 2, 3]
        assert (
            resolve_quarantine_max_attempts({"librarian": {"quarantine_max_attempts": True}}) == 3
        )


# ------------------------------------------------------------ AC2 / AC3 unit


class TestRecordBoundViolationSchedule:
    def _violate(self, ledger, raw, bound="llm_calls", now=None, used=50, limit=24):
        return _record_bound_violation(
            ledger,
            raw,
            bound=bound,
            detail="d",
            threshold=2,
            max_attempts=3,
            retry_delays_days=[3, 4],
            now=now,
            used=used,
            limit=limit,
        )

    def test_three_attempts_over_a_week(self, tmp_path: Path) -> None:
        raw, ledger = _raw(tmp_path), {}
        t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
        assert self._violate(ledger, raw, now=t0) is None
        e = ledger[raw.ref]
        assert e["violations"] == 1 and e["retry_after"] == "2026-10-04T00:00:00Z"
        assert self._violate(ledger, raw, now=t0 + timedelta(days=3)) is None
        assert ledger[raw.ref]["retry_after"] == "2026-10-08T00:00:00Z"
        crossed = self._violate(ledger, raw, now=t0 + timedelta(days=7))
        assert crossed is not None and crossed["violations"] == 3
        assert [h["outcome"] for h in crossed["history"]] == [
            "retry_scheduled",
            "retry_scheduled",
            "parked",
        ]

    def test_first_wall_clock_is_transient_repeat_counts(self, tmp_path: Path) -> None:
        raw, ledger = _raw(tmp_path), {}
        assert self._violate(ledger, raw, bound="wall_clock") is None
        e = ledger[raw.ref]
        assert e["violations"] == 0 and "retry_after" not in e
        assert e["history"][0]["outcome"] == "transient"
        assert self._violate(ledger, raw, bound="wall_clock") is None
        assert ledger[raw.ref]["violations"] == 1
        assert "retry_after" in ledger[raw.ref]

    def test_content_change_resets_attempts(self, tmp_path: Path) -> None:
        raw, ledger = _raw(tmp_path), {}
        self._violate(ledger, raw)
        raw.path.write_text("different content", encoding="utf-8")
        raw = RawFile(path=raw.path, source=raw.source, timestamp=raw.timestamp, uuid8=raw.uuid8)
        self._violate(ledger, raw)
        assert ledger[raw.ref]["violations"] == 1

    def test_bytes_bound_keeps_plain_threshold(self, tmp_path: Path) -> None:
        raw, ledger = _raw(tmp_path), {}
        kw = dict(bound="bytes", detail="d", threshold=2, max_attempts=3, retry_delays_days=[3, 4])
        assert _record_bound_violation(ledger, raw, **kw) is None
        assert "retry_after" not in ledger[raw.ref]
        assert _record_bound_violation(ledger, raw, **kw) is not None


# ------------------------------------------------- end to end (AC2, AC4)


def test_retries_over_days_relax_bound_then_park_with_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_knowledge_root(tmp_path, n_files=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
    monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_API_CALLS", "2")
    clients: list[object] = []

    def _factory(**kwargs):
        c = _make_over_budget_client(8)
        clients.append(c)
        return c

    monkeypatch.setattr(anthropic_mod, "Anthropic", _factory)
    raw_file = next((root / "raw" / "sessions").glob("*.md"))
    wiki = root / "wiki"

    def _run() -> dict:
        stats: dict = {}
        run(
            raw_root=root / "raw",
            wiki_root=wiki,
            knowledge_root=root,
            max_api_calls=1000,
            max_runtime=0,
            out_run_stats=stats,
        )
        return stats

    def _make_due() -> None:
        path = wiki / QUARANTINE_CANDIDATE_MANIFEST_NAME
        data = json.loads(path.read_text())
        for entry in data["files"].values():
            entry["retry_after"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(data))

    # Attempt 1: bound 2 trips, retry scheduled, file stays in raw/.
    assert _run()["quarantined_files"] == []
    assert raw_file.exists()
    entry = next(iter(_load_quarantine_candidates(wiki).values()))
    assert entry["violations"] == 1 and entry["retry_after"] > "2026"

    # Inside the retry window: skipped, no LLM client call at all.
    calls_before = sum(c.messages.create.call_count for c in clients)
    stats = _run()
    assert stats["quarantined_files"] == []
    assert sum(c.messages.create.call_count for c in clients) == calls_before

    # Attempt 2: bound relaxed 2 -> 3.
    _make_due()
    assert _run()["quarantined_files"] == []
    # Attempt 3: relaxed again (2 * 1.5**2 = 4.5 -> 5); schedule spent -> parked.
    _make_due()
    parked = _run()["quarantined_files"]
    assert len(parked) == 1 and parked[0]["violations"] == 3
    assert not raw_file.exists()

    limits = [a["limit"] for a in parked[0]["attempts"]]
    assert limits == [2, 3, 5]
    assert all(a["used"] > a["limit"] for a in parked[0]["attempts"])

    # AC2: every attempt is in the ledger (3 attempt rows + the quarantine row).
    kinds = [r["kind"] for r in read_quarantine_ledger(wiki)]
    assert kinds.count("attempt") == 3 and kinds.count("quarantine") == 1

    # AC4: the decision item states the history and the bound it finally failed.
    (item,) = [d for d in list_pending_decisions(wiki) if d["type"] == "quarantine"]
    assert "3 attempt(s)" in item["summary"]
    assert "last 6 call(s) vs 5 call(s) allowed (llm_calls)" in item["summary"]
    assert len(item["payload"]["attempts"]) == 3
    assert len(_entity_pages(wiki)) >= 2  # partial progress landed throughout


# --------------------------------------------------------------------- AC5


def _legacy_park(tmp_path: Path, name: str, bound: str, attempts=None) -> dict:
    wiki, rawroot = tmp_path / "wiki", tmp_path / "raw"
    wiki.mkdir(exist_ok=True)
    d = rawroot / "retros"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("x", encoding="utf-8")
    raw = RawFile(path=p, source="retros", timestamp=name[:16], uuid8=name[17:25])
    return quarantine_file(
        raw,
        wiki_root=wiki,
        raw_root=rawroot,
        bound=bound,
        detail="d",
        violations=2,
        attempts=attempts,
    )


class TestRequeue:
    def test_only_legacy_schedule_bounds_requeue_once(self, tmp_path: Path) -> None:
        a = _legacy_park(tmp_path, "20240407T120000Z-aaaaaaaa.md", "llm_calls")
        b = _legacy_park(tmp_path, "20240407T120000Z-bbbbbbbb.md", "wall_clock")
        c = _legacy_park(tmp_path, "20240407T120000Z-cccccccc.md", "bytes")
        d = _legacy_park(
            tmp_path,
            "20240407T120000Z-dddddddd.md",
            "llm_calls",
            attempts=[
                {
                    "attempt": 1,
                    "at": "2026-10-01T00:00:00Z",
                    "bound": "llm_calls",
                    "used": 5,
                    "limit": 3,
                    "outcome": "parked",
                }
            ],
        )
        wiki, rawroot = tmp_path / "wiki", tmp_path / "raw"
        dry = requeue_legacy_quarantine(wiki, rawroot, apply=False)
        assert sorted(r["status"] for r in dry) == ["eligible", "eligible", "skipped", "skipped"]
        assert len(list_pending_quarantine(wiki)) == 4  # dry run changed nothing

        done = requeue_legacy_quarantine(wiki, rawroot, apply=True)
        assert sorted(r["id"] for r in done if r["status"] == "requeued") == sorted(
            [a["id"], b["id"]]
        )
        assert (rawroot / "retros" / "20240407T120000Z-aaaaaaaa.md").exists()
        assert {r["id"] for r in list_pending_quarantine(wiki)} == {c["id"], d["id"]}

        again = requeue_legacy_quarantine(wiki, rawroot, apply=True)
        assert not [r for r in again if r["status"] == "requeued"]

    def test_cli_dry_run_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        a = _legacy_park(tmp_path, "20240407T120000Z-aaaaaaaa.md", "llm_calls")
        assert main(["quarantine", "requeue", "--path", str(tmp_path)]) == 0
        assert "DRY RUN" in capsys.readouterr().out
        assert [r["id"] for r in list_pending_quarantine(tmp_path / "wiki")] == [a["id"]]
        assert main(["quarantine", "requeue", "--path", str(tmp_path), "--apply"]) == 0
        assert "APPLY" in capsys.readouterr().out
        assert list_pending_quarantine(tmp_path / "wiki") == []
        assert (tmp_path / "raw" / "retros" / "20240407T120000Z-aaaaaaaa.md").exists()
