# SPDX-License-Identifier: Apache-2.0
"""Tests for the retrieval entry-point import-budget harness (issue athenaeum#1357).

The spike's deliverable is a *number*, so what needs pinning here is the set of
properties that decide whether that number means anything:

* it refuses a toy index, because FTS5 cost scales with corpus size and a small
  fixture makes any implementation look fast;
* it times a real ``fork/exec -> exit``, not an in-process query — the exact way
  this measurement has been got wrong before (~5 ms reported against a ~650 ms
  real per-turn cost);
* the candidate entry points genuinely differ in import path and genuinely
  return hits, so the comparison between them is a comparison of imports;
* the verdict is stated against an explicit budget with the measured number.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "measure_retrieval_entry_point.py"

_spec = importlib.util.spec_from_file_location("measure_retrieval_entry_point", _SCRIPT)
assert _spec and _spec.loader
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)


def _build_index(path: Path, pages: int) -> Path:
    """A minimal FTS5 index with the live schema's columns."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5("
        "filename, name, tags, aliases, description, "
        'audience UNINDEXED, type UNINDEXED, memory_tier UNINDEXED, '
        'tokenize="porter unicode61")'
    )
    conn.executemany(
        "INSERT INTO wiki (filename, name, tags, aliases, description, "
        "audience, type, memory_tier) VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                f"page-{i}.md",
                f"Recall Architecture Note {i}",
                "recall",
                "",
                f"description number {i}",
                "public",
                "reference",
                "hot",
            )
            for i in range(pages)
        ],
    )
    conn.commit()
    conn.close()
    return path


class TestToyIndexRefusal:
    """athenaeum#1357 AC3: a number taken against a toy corpus is not a number."""

    def test_refuses_an_index_below_min_pages(self, tmp_path, capsys):
        index = _build_index(tmp_path / "wiki-index.db", pages=10)
        with pytest.raises(SystemExit) as excinfo:
            harness.main(["--index", str(index), "--min-pages", "1000"])
        assert excinfo.value.code == 2
        assert "below --min-pages" in capsys.readouterr().err

    def test_refusal_names_the_page_count_it_measured(self, tmp_path, capsys):
        index = _build_index(tmp_path / "wiki-index.db", pages=7)
        with pytest.raises(SystemExit):
            harness.main(["--index", str(index), "--min-pages", "1000"])
        assert "7 pages" in capsys.readouterr().err

    def test_accepts_an_index_at_the_threshold(self, tmp_path):
        """The gate is a floor, not a moving target: exactly --min-pages passes."""
        index = _build_index(tmp_path / "wiki-index.db", pages=25)
        assert harness._index_pages(index) == 25
        # Exercised through the real entry point with the floor lowered to match.
        rc = harness.main(
            [
                "--index", str(index), "--min-pages", "25",
                "--warm-runs", "2", "--cold-runs", "1",
                "--candidates", "stdlib", "--references", "none",
            ]
        )
        assert rc in (0, 1)  # a verdict was reached, either way


class TestMeasuresRealProcesses:
    """athenaeum#1357 AC1: the measurement is fork/exec -> exit, not in-process."""

    def test_time_once_measures_a_whole_process(self):
        """A process that sleeps 150 ms must be timed at >=150 ms.

        An in-process timer around the query would report ~0 ms here. This is the
        counter-example the acceptance criterion names, made executable.
        """
        elapsed = harness._time_once(
            [sys.executable, "-c", "import time; time.sleep(0.15)"], "", dict(**_env())
        )
        assert elapsed >= 150.0

    def test_includes_interpreter_start_not_just_the_body(self):
        """Timing a no-op process still costs interpreter start, so it is nonzero."""
        elapsed = harness._time_once([sys.executable, "-c", "pass"], "", dict(**_env()))
        assert elapsed > 1.0


def _env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["ATHENAEUM_SRC_ROOT"] = str(_REPO / "src")
    return env


class TestCandidates:
    def test_every_candidate_reaches_the_query_differently(self):
        """The three candidates must differ in import path, or the comparison is empty."""
        stdlib, direct, package = (
            harness.CANDIDATES[k][0] for k in ("stdlib", "direct-load", "package-import")
        )
        stdlib_prelude = stdlib.split(harness._COMMON_BODY)[0]
        assert not any(
            line.startswith(("import athenaeum", "from athenaeum"))
            for line in stdlib_prelude.splitlines()
        )
        assert "spec_from_file_location" in direct
        assert "from athenaeum.search import" in package

    def test_candidates_share_the_same_workload(self):
        """Only the import path may differ — otherwise the timings are not comparable."""
        for source, _blurb in harness.CANDIDATES.values():
            assert source.endswith(harness._COMMON_BODY)

    def test_a_candidate_actually_returns_hits(self, tmp_path):
        """A candidate timing an empty query is timing nothing."""
        index = _build_index(tmp_path / "wiki-index.db", pages=50)
        script = tmp_path / "entry.py"
        script.write_text(harness.CANDIDATES["stdlib"][0], encoding="utf-8")
        payload = json.dumps(
            {"prompt": "recall architecture note", "session_id": "test-session"}
        )
        hits = harness._verify_output(
            [sys.executable, str(script), str(index)], payload, _env()
        )
        assert hits == 3

    def test_candidate_emits_the_hook_envelope(self, tmp_path):
        index = _build_index(tmp_path / "wiki-index.db", pages=50)
        script = tmp_path / "entry.py"
        script.write_text(harness.CANDIDATES["stdlib"][0], encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(script), str(index)],
            input=json.dumps(
                {"prompt": "recall architecture note", "session_id": "s"}
            ).encode(),
            capture_output=True,
            env=_env(),
            check=True,
        )
        emitted = json.loads(proc.stdout.decode())
        assert emitted["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "[Knowledge context]" in (
            emitted["hookSpecificOutput"]["additionalContext"]
        )


class TestVerdict:
    """athenaeum#1357 AC2: go/no-go stated against the threshold, with the number."""

    def test_verdict_names_the_budget_and_the_measurement(self, tmp_path, capsys):
        index = _build_index(tmp_path / "wiki-index.db", pages=30)
        harness.main(
            [
                "--index", str(index), "--min-pages", "30",
                "--warm-runs", "2", "--cold-runs", "1",
                "--candidates", "stdlib", "--references", "none",
                "--json", str(tmp_path / "out.json"),
            ]
        )
        out = capsys.readouterr().out
        assert "VERDICT:" in out
        assert "127 ms budget" in out

        record = json.loads((tmp_path / "out.json").read_text())
        verdict = record["verdict"]
        assert verdict["result"] in ("GO", "NO-GO")
        assert verdict["budget_ms"] == harness.BUDGET_MS
        assert isinstance(verdict["measured_ms"], float)
        assert verdict["basis"] == "cold p95 of the cheapest candidate"

    def test_budget_is_overridable_and_the_verdict_follows_it(self, tmp_path):
        """A verdict that ignored --budget-ms would be stated against nothing."""
        index = _build_index(tmp_path / "wiki-index.db", pages=30)
        out = tmp_path / "out.json"
        rc = harness.main(
            [
                "--index", str(index), "--min-pages", "30",
                "--warm-runs", "2", "--cold-runs", "1",
                "--candidates", "stdlib", "--references", "none",
                "--budget-ms", "0.001", "--json", str(out),
            ]
        )
        record = json.loads(out.read_text())
        assert record["verdict"]["result"] == "NO-GO"
        assert record["verdict"]["budget_ms"] == 0.001
        assert rc == 1

    def test_record_carries_the_index_provenance(self, tmp_path):
        """A number with no stated corpus cannot be judged for realism."""
        index = _build_index(tmp_path / "wiki-index.db", pages=30)
        out = tmp_path / "out.json"
        harness.main(
            [
                "--index", str(index), "--min-pages", "30",
                "--warm-runs", "2", "--cold-runs", "1",
                "--candidates", "stdlib", "--references", "none", "--json", str(out),
            ]
        )
        record = json.loads(out.read_text())
        assert record["index"]["pages"] == 30
        assert record["index"]["size_bytes"] > 0
        assert record["python_version"]
        assert record["warm_runs"] == 2


class TestCandidateSelection:
    def test_rejects_an_unknown_candidate(self, tmp_path, capsys):
        index = _build_index(tmp_path / "wiki-index.db", pages=30)
        with pytest.raises(SystemExit) as excinfo:
            harness.main(
                ["--index", str(index), "--min-pages", "30", "--candidates", "nope"]
            )
        assert excinfo.value.code == 2
        assert "unknown candidate" in capsys.readouterr().err


class TestStats:
    def test_p95_is_the_upper_tail_not_the_mean(self):
        stats = harness._stats([1.0] * 95 + [100.0] * 5)
        assert stats["p50"] == 1.0
        assert stats["p95"] >= 1.0
        assert stats["max"] == 100.0
        assert stats["n"] == 100

    def test_single_sample_does_not_raise(self):
        """statistics.quantiles needs n>1; a one-run cold pass must still report."""
        stats = harness._stats([42.0])
        assert stats["p50"] == 42.0
        assert stats["p95"] == 42.0


class TestColdStart:
    def test_go_cold_drops_bytecode(self, tmp_path):
        script = tmp_path / "entry.py"
        script.write_text("print(1)\n", encoding="utf-8")
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "entry.cpython-313.pyc").write_bytes(b"stale")
        harness._go_cold(script, [])
        assert not cache.exists()

    def test_cold_effect_reports_a_ratio(self, tmp_path):
        """The self-check must be able to say eviction was inert, not assume it worked."""
        index = _build_index(tmp_path / "wiki-index.db", pages=30)
        effect = harness._cold_effect(index, [index])
        assert set(effect) == {"warm_read_ms", "cold_read_ms", "ratio"}
        assert effect["ratio"] >= 0.0
