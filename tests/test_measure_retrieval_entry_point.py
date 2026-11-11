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

Cost note (athenaeum#2233). Every test that drives ``harness.main()`` end to
end used to perform the harness's REAL cold-start sweep: ``_evictable_paths``
walks the whole stdlib plus ``site-packages`` (about 21,000 files here) and
``_go_cold`` then issues ``posix_fadvise(POSIX_FADV_DONTNEED)`` over every
one of them,
twice per call (one cold warm-up plus one cold run). None of those tests
asserts anything about cold measurement -- they assert the verdict's shape,
the record's provenance and the usage errors -- so the sweep was pure cost,
and on a GitHub runner (where ``posix_fadvise`` is NOT inert, unlike this
container's overlay mount) it also evicted the page cache out from under the
other xdist workers sharing the shard. ``narrow_eviction`` below scopes the
sweep to the two paths the run is actually about. The breadth of the real
sweep is pinned directly, and cheaply, by
``TestColdStart::test_evictable_paths_covers_the_interpreter_trees``; the
real ``_go_cold`` and ``_cold_effect`` are still exercised by their own
sibling tests there.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import sysconfig
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


@pytest.fixture
def narrow_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scope the harness's cold-start sweep to the paths under measurement.

    ``_evictable_paths`` deliberately returns the whole interpreter: stdlib,
    ``purelib`` and ``platlib``, every ``.py``/``.pyc``/``.so`` in them. That
    breadth is the measurement contract for a real run (see the module
    docstring), and it is asserted directly by
    ``test_evictable_paths_covers_the_interpreter_trees``. A test that only
    needs ``main()`` to reach its verdict does not need tens of thousands of
    ``posix_fadvise`` calls, nor the cold interpreter starts they cause in
    this process and in every sibling xdist worker on the same runner.

    The sweep itself still runs for real, on a real list -- ``_go_cold`` is
    called, bytecode is still dropped, ``posix_fadvise`` is still issued --
    just over the index and the interpreter binary rather than the whole
    tree. No test using this fixture asserts a cold-vs-warm magnitude, so the
    narrower list cannot change a verdict (athenaeum#2233).
    """
    real = harness._evictable_paths

    def _narrow(index_db: Path, python: Path) -> list[Path]:
        return [index_db, Path(python).resolve()]

    monkeypatch.setattr(harness, "_evictable_paths", _narrow)
    assert real is not harness._evictable_paths  # the patch took


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

    def test_accepts_an_index_at_the_threshold(self, tmp_path, narrow_eviction):
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

    def test_verdict_names_the_budget_and_the_measurement(
        self, tmp_path, capsys, narrow_eviction
    ):
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

    def test_budget_is_overridable_and_the_verdict_follows_it(self, tmp_path, narrow_eviction):
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

    def test_record_carries_the_index_provenance(self, tmp_path, narrow_eviction):
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

    @pytest.mark.parametrize("value", [",", "", "  ,  ,"])
    def test_rejects_an_empty_selection_as_usage_not_as_no_go(
        self, tmp_path, capsys, value
    ):
        """Exit 1 means NO-GO. A mistyped argument must not borrow that meaning.

        Before this guard, `--candidates ","` reached the verdict block with no
        candidates measured, raised ValueError out of `min()`, and exited 1 --
        which a caller reading the documented exit codes would read as "the
        entry point missed the budget".
        """
        index = _build_index(tmp_path / "wiki-index.db", pages=30)
        with pytest.raises(SystemExit) as excinfo:
            harness.main(
                [
                    "--index", str(index), "--min-pages", "30",
                    "--candidates", value, "--references", "none",
                ]
            )
        assert excinfo.value.code == 2
        assert "selected nothing to measure" in capsys.readouterr().err

    def test_rejects_an_unknown_reference(self, tmp_path, capsys):
        index = _build_index(tmp_path / "wiki-index.db", pages=30)
        with pytest.raises(SystemExit) as excinfo:
            harness.main(
                ["--index", str(index), "--min-pages", "30", "--references", "nope"]
            )
        assert excinfo.value.code == 2
        assert "unknown reference" in capsys.readouterr().err

    def test_references_none_is_not_an_error(self, tmp_path, narrow_eviction):
        """'none' skips the reference sweep; it is not an unknown reference name."""
        index = _build_index(tmp_path / "wiki-index.db", pages=30)
        rc = harness.main(
            [
                "--index", str(index), "--min-pages", "30",
                "--candidates", "stdlib", "--references", "none",
                "--warm-runs", "2", "--cold-runs", "1",
            ]
        )
        assert rc in (0, 1)


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
    def test_evictable_paths_covers_the_interpreter_trees(self, tmp_path):
        """The sweep's BREADTH is the cold-start contract, so assert it here.

        Everything a cold run should have to fault back in: the index, the
        interpreter binary, and the stdlib/purelib/platlib trees. This used
        to be covered only incidentally -- by five unrelated tests paying for
        the full sweep as a side effect -- which made it both expensive and
        invisible. Asserting the returned LIST is cheap; performing the
        eviction is what cost (athenaeum#2233).
        """
        index = _build_index(tmp_path / "wiki-index.db", pages=5)
        paths = harness._evictable_paths(index, Path(sys.executable))

        assert index in paths
        assert Path(sys.executable).resolve() in paths

        roots = {
            key: sysconfig.get_paths()[key]
            for key in ("stdlib", "purelib", "platlib")
            if sysconfig.get_paths().get(key)
            and os.path.isdir(sysconfig.get_paths()[key])
        }
        assert roots, "no interpreter tree to evict -- the sweep would be vacuous"
        for key, root in roots.items():
            assert any(
                str(path).startswith(root) for path in paths
            ), f"{key} tree ({root}) contributed nothing to the eviction sweep"

        # What the trees contribute is loadable code -- that is where a cold
        # interpreter start pays. (The index and the interpreter binary are
        # named explicitly above and are not suffix-filtered.)
        tree_paths = [
            path
            for path in paths
            if any(str(path).startswith(root) for root in roots.values())
        ]
        assert tree_paths
        assert all(
            str(path).endswith((".py", ".pyc", ".so")) for path in tree_paths
        )

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
