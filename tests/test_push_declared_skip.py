# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#2976: a declared-off per-turn push writes a skip row.

A caller that knows its run is self-contained sets ``AUTO_RECALL=false``. The
hook still skips the push and still runs the witness call, and now writes one
push-ledger skip row with reason ``declared`` so the turn stays in the
push-metrics denominator. Fixtures are invented.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum import push_metrics
from athenaeum.context import SKIP_REASON_DECLARED, record_declared_skip

SRC = str(Path(__file__).resolve().parent.parent / "src")

PROMPT = "tell me about the zebrafish migration plan"

#: The only keys a declared skip row may carry. Everything else in the row
#: (``v``, the empty ``items`` list and the zero counts) is the ledger's fixed
#: row shape, not content; nothing here is derived from the prompt.
_ALLOWED_KEYS = {
    "v",
    "session_id",
    "ts",
    "query_hash",
    "backend",
    "items",
    "pushed_count",
    "token_cost",
    "token_cost_estimated",
    "source",
    "skip_reason",
}


def _build_index(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5(filename, name, tags, aliases, "
        "description, audience UNINDEXED, type UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    conn.executemany(
        "INSERT INTO wiki (filename, name, tags, aliases, description, audience, type) "
        "VALUES (?, ?, ?, '', ?, '|__access_open__|', 'reference')",
        [("aaaa1111-zebrafish.md", "Zebrafish Migration", "zebrafish", "zebrafish notes")],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def cache(tmp_path: Path) -> Path:
    _build_index(tmp_path / "wiki-index.db")
    return tmp_path


def _run_adapter(cache: Path, session_id: str, extra_env: dict[str, str]):  # type: ignore[no-untyped-def]
    env = {
        "PYTHONPATH": SRC,
        "PATH": "/usr/bin:/bin",
        "ATHENAEUM_CACHE_DIR": str(cache),
        "HOME": str(cache),
        "SEARCH_BACKEND": "fts5",
    }
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "athenaeum.claude_code_adapter"],
        input=json.dumps({"prompt": PROMPT, "session_id": session_id}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _ledger(cache: Path) -> list[dict]:
    return push_metrics._read_jsonl(push_metrics.push_records_path(cache))


def test_declared_off_writes_one_declared_skip_row(cache: Path) -> None:
    r = _run_adapter(cache, "sess-declared", {"AUTO_RECALL": "false"})
    assert r.returncode == 0
    assert r.stdout == ""
    assert r.stderr == ""
    rows = _ledger(cache)
    assert len(rows) == 1
    row = rows[0]
    assert row["skip_reason"] == SKIP_REASON_DECLARED == "declared"
    assert row["session_id"] == "sess-declared"
    assert row["ts"]
    assert row["items"] == []
    assert row["pushed_count"] == 0
    assert not (cache / "context-seen-sess-declared.txt").exists()


def test_declared_skip_row_carries_no_prompt_query_or_page_names(cache: Path) -> None:
    _run_adapter(cache, "sess-content", {"AUTO_RECALL": "false"})
    (row,) = _ledger(cache)
    assert set(row) <= _ALLOWED_KEYS
    assert row["query_hash"] == ""
    assert row["backend"] == ""
    assert "query_terms" not in row and "extractor" not in row
    raw = push_metrics.push_records_path(cache).read_text(encoding="utf-8")
    for fragment in ("zebrafish", "migration", "Zebrafish Migration", "aaaa1111"):
        assert fragment not in raw
    assert push_metrics._query_hash(PROMPT) not in raw


def test_any_non_true_value_is_declared_off(cache: Path) -> None:
    _run_adapter(cache, "sess-zero", {"AUTO_RECALL": "0"})
    assert [r.get("skip_reason") for r in _ledger(cache)] == ["declared"]


def test_auto_recall_unset_writes_no_skip_row(cache: Path) -> None:
    """Negative test: with ``AUTO_RECALL`` unset the turn pushes as before and
    no skip row is written. The push row is the positive control proving the
    fixture matches, so the empty skip list is not vacuous."""
    r = _run_adapter(cache, "sess-unset", {})
    assert r.returncode == 0
    assert r.stdout != "", "fixture never matched; the negative test would be vacuous"
    assert push_metrics.read_skip_records(cache) == []
    assert len(push_metrics.read_push_records(cache)) == 1


def test_declared_off_still_runs_the_witness_call(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.claude_code_adapter as adapter

    calls: list[str] = []
    monkeypatch.setattr(adapter, "_witness_turn", lambda _cache, raw: calls.append(raw))
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
    monkeypatch.setenv("AUTO_RECALL", "false")
    payload = json.dumps({"prompt": PROMPT, "session_id": "sess-witness"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert adapter.main() == 0

    assert calls == [payload]
    assert buf.getvalue() == ""
    assert [r.get("skip_reason") for r in push_metrics.read_skip_records(cache)] == ["declared"]


def test_declared_skip_failure_never_breaks_the_turn(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.claude_code_adapter as adapter
    import athenaeum.context as context_mod

    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(context_mod, "record_declared_skip", boom)
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
    monkeypatch.setenv("AUTO_RECALL", "false")
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"prompt": PROMPT, "session_id": "s"}))
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert adapter.main() == 0
    assert buf.getvalue() == ""


def test_kill_switch_writes_no_declared_skip_row(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATHENAEUM_DISABLED", "all")
    assert record_declared_skip("sess-k", cache_dir=cache) is False
    assert _ledger(cache) == []


def test_no_session_id_writes_nothing(cache: Path) -> None:
    assert record_declared_skip("", cache_dir=cache) is False
    assert _ledger(cache) == []


# ---------------------------------------------------------------------------
# push-metrics: skips are their own figure, and a skip-only session is not
# reported as "no push"
# ---------------------------------------------------------------------------


def test_baseline_counts_skipped_turns_separately(cache: Path) -> None:
    _run_adapter(cache, "sess-pushed", {})
    _run_adapter(cache, "sess-skip-only", {"AUTO_RECALL": "false"})
    _run_adapter(cache, "sess-skip-only", {"AUTO_RECALL": "false"})
    _run_adapter(cache, "sess-pushed", {"AUTO_RECALL": "false"})

    baseline = push_metrics.compute_baseline(cache_dir=cache)
    assert baseline.push_record_count == 1
    assert baseline.session_count == 1
    assert baseline.skipped_turn_count == 3
    assert baseline.skipped_by_reason == {"declared": 3}
    assert baseline.skip_only_session_count == 1
    d = baseline.to_dict()
    assert d["push_records"] == 1
    assert d["skipped_turns"] == 3
    assert d["skipped_by_reason"] == {"declared": 3}
    assert d["skip_only_sessions"] == 1


def test_baseline_exclude_session_drops_its_skips(cache: Path) -> None:
    _run_adapter(cache, "sess-pushed", {})
    _run_adapter(cache, "sess-skip-only", {"AUTO_RECALL": "false"})
    baseline = push_metrics.compute_baseline(cache_dir=cache, exclude_sessions=["sess-skip-only"])
    assert baseline.skipped_turn_count == 0
    assert baseline.skip_only_session_count == 0


def test_baseline_with_no_skips_reports_zero(cache: Path) -> None:
    _run_adapter(cache, "sess-pushed", {})
    d = push_metrics.compute_baseline(cache_dir=cache).to_dict()
    assert d["skipped_turns"] == 0
    assert d["skipped_by_reason"] == {}
    assert d["skip_only_sessions"] == 0


def test_push_metrics_cli_prints_skipped_figure(cache: Path) -> None:
    _run_adapter(cache, "sess-pushed", {})
    _run_adapter(cache, "sess-skip-only", {"AUTO_RECALL": "false"})
    env = {
        "PYTHONPATH": SRC,
        "PATH": "/usr/bin:/bin",
        "HOME": str(cache),
        "ATHENAEUM_CACHE_DIR": str(cache),
    }
    base = [sys.executable, "-m", "athenaeum.cli", "push-metrics", "baseline", "--dry-run"]
    base += ["--cache-dir", str(cache)]
    text = subprocess.run(base, capture_output=True, text=True, env=env, timeout=60)
    assert text.returncode == 0, text.stderr
    assert "push_records: 1\n" in text.stdout
    assert "skipped_turns: 1 (declared=1)\n" in text.stdout
    assert "skip_only_sessions: 1\n" in text.stdout
    js = subprocess.run(base + ["--json"], capture_output=True, text=True, env=env, timeout=60)
    payload = json.loads(js.stdout)
    assert payload["skipped_turns"] == 1
    assert payload["skip_only_sessions"] == 1


def test_skip_only_session_is_not_reported_as_no_push(cache: Path) -> None:
    _run_adapter(cache, "sess-skip-only", {"AUTO_RECALL": "false"})
    result, reason = push_metrics._determine_references_with_reason(
        "sess-skip-only", cache_dir=cache
    )
    assert result is None
    assert reason == push_metrics.REFERENCE_REASON_PUSH_SKIPPED
    assert reason != push_metrics.REFERENCE_REASON_NO_PUSH_RECORDS
    # Counter-example: a session with no rows at all is still "no push".
    assert push_metrics._determine_references_with_reason("sess-never", cache_dir=cache) == (
        None,
        push_metrics.REFERENCE_REASON_NO_PUSH_RECORDS,
    )
