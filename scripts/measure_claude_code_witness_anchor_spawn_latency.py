#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Latency harness for the periodic in-session anchor spawn check (issue
athenaeum#2156).

**What this answers.** The issue's own AC: with the interval due, the
hook's wall time is unchanged versus not due, within the athenaeum#2149
p95 budget (25 ms) for the witness path's own added cost.

**Method**, mirroring
``scripts/measure_claude_code_witness_hook_latency.py`` exactly (real
subprocess fork/exec -> exit, witness enabled on both sides so only the
periodic-anchor variable differs):

* **not due**: ``ATHENAEUM_WITNESS_ANCHOR_INTERVAL_MINUTES=0`` — the
  due-check short-circuits immediately (disabled), never even opening the
  guard file.
* **due**: default interval (15) — a ledger with no prior anchor activity
  at all is due immediately (see
  ``promptwitness.anchor_schedule.periodic_anchor_due``), so every sample
  in this set spawns a REAL detached ``promptwitness anchor`` child
  process. The measured subprocess never waits on that child
  (``subprocess.Popen`` with closed stdio, discarded immediately — the
  structural property
  ``tests/test_claude_code_adapter.py::TestPeriodicAnchorSpawn`` proves
  directly), so this harness's own timing is exactly what it needs to be:
  proof that spawning does not block the parent, not a measurement of the
  child's own (unbounded, network-dependent) runtime.

  **Both sides use a FRESH ``PROMPTWITNESS_DIR`` for every sample** —
  not just the 'due' set. A ledger directory that has never seen a turn
  before pays `record_turn`'s own first-turn
  (`promptwitness.harness.claude_code._is_launch_argument`) ``ps`` probe
  subprocess cost; reusing one dir across samples would let the 'not due'
  set skip that cost from sample 2 onward while the 'due' set (which must
  stay fresh every sample to stay due) kept paying it on every single
  one — a measurement artifact of session-state reuse, not of the anchor
  feature, and the first draft of this harness fell into exactly that
  trap (a ~145 ms apparent delta that vanished once both sides were put
  on equal footing).

Reported: ``due_subprocess_ms - not_due_subprocess_ms`` at p95, compared
against the 25 ms budget.

Usage::

    python scripts/measure_claude_code_witness_anchor_spawn_latency.py --n 200
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = str(REPO_ROOT / "src")


def _stats(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    n = len(ordered)
    p95_idx = min(n - 1, int(round(0.95 * (n - 1))))
    return {
        "n": n,
        "mean": round(statistics.fmean(ordered), 3),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[p95_idx], 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


def _measure_subprocess(
    n: int, cache_dir: Path, ledger_root: Path, *, anchor_interval_minutes: int, fresh_ledger_each_sample: bool
) -> list[float]:
    """Real subprocess fork/exec -> exit of the actual hook entry point,
    witness ENABLED on every sample -- only the anchor-interval variable
    (and, for the 'due' set, a fresh ledger dir per sample) differs."""
    stdin_payload = json.dumps(
        {"prompt": "measure the anchor spawn latency for this turn", "session_id": "lat-sess"}
    )
    samples: list[float] = []
    for i in range(n):
        ledger_dir = ledger_root / str(i) if fresh_ledger_each_sample else ledger_root
        ledger_dir.mkdir(parents=True, exist_ok=True)
        env = {
            "PYTHONPATH": SRC,
            "PATH": "/usr/bin:/bin",
            "ATHENAEUM_CACHE_DIR": str(cache_dir),
            "ATHENAEUM_WITNESS_LEDGER_ENABLED": "true",
            "AUTO_RECALL": "false",
            "PROMPTWITNESS_DIR": str(ledger_dir),
            "ATHENAEUM_WITNESS_ANCHOR_INTERVAL_MINUTES": str(anchor_interval_minutes),
        }
        start = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-m", "athenaeum.claude_code_adapter"],
            input=stdin_payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if result.returncode != 0:
            raise RuntimeError(f"hook subprocess failed: {result.stderr}")
        samples.append(elapsed_ms)
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=200, help="Samples per measurement (default 200)")
    args = parser.parse_args(argv)

    report: dict[str, Any] = {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        not_due_cache = tmp_path / "not-due" / "cache"
        not_due_ledger = tmp_path / "not-due" / "witness"
        not_due_cache.mkdir(parents=True)
        not_due_ledger.mkdir(parents=True)
        not_due_samples = _measure_subprocess(
            args.n,
            not_due_cache,
            not_due_ledger,
            anchor_interval_minutes=0,
            fresh_ledger_each_sample=True,
        )
        report["not_due_subprocess_ms"] = _stats(not_due_samples)

        due_cache = tmp_path / "due" / "cache"
        due_ledger_root = tmp_path / "due" / "witness"
        due_cache.mkdir(parents=True)
        due_ledger_root.mkdir(parents=True)
        due_samples = _measure_subprocess(
            args.n,
            due_cache,
            due_ledger_root,
            anchor_interval_minutes=15,
            fresh_ledger_each_sample=True,
        )
        report["due_subprocess_ms"] = _stats(due_samples)

    added_p95 = round(
        report["due_subprocess_ms"]["p95"] - report["not_due_subprocess_ms"]["p95"], 3
    )
    report["anchor_spawn_added_latency_p95_ms"] = added_p95

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
