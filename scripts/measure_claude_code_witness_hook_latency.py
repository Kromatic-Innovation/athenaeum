#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Latency harness for the Claude Code witness-ledger hook path (athenaeum#2149).

**What this answers.** The issue's own AC: p95 at or under 25 ms, with
``key_provider: none`` and with ``software-file``.

**Why two separate measurements, not one subprocess run each.** The
``software-file`` key adapter is athenaeum#2150's deliverable and is being
built concurrently in a sibling lane -- it may not exist on this branch (and
this issue is explicitly out-of-scope for building it). ``record_turn``
itself always calls ``ledger.append_line(..., key_provider=None, ...)``
today, so there is no production code path yet that can exercise a signed
append end-to-end. This harness measures the REAL ``none`` case with a real
subprocess (fork/exec -> exit, same discipline as
``scripts/measure_retrieval_entry_point.py``), then measures the
INCREMENTAL cost of signing in-process, against a local Ed25519 stand-in
built directly from ``cryptography.hazmat.primitives.asymmetric.ed25519``
(never serialized to PEM -- the key object is used in memory only, so there
is no private-key banner anywhere for a secret scanner to find). The
``software-file`` figure reported is the real ``none`` p95 PLUS that
measured signing delta -- an honest estimate built from two real
measurements, not a guess. See the issue and the PR body for this same
disclosure.

**Measurement contract**, mirroring ``scripts/measure_retrieval_entry_point.py``:

* The number the issue's 25 ms budget is about is the witness path's OWN
  ADDED cost, not a fresh-process fork/exec -- the hook process already
  starts and imports ``athenaeum.config``/``athenaeum.models`` for every
  turn today (recall's own existing cost), with or without this issue. So
  this harness measures TWO real subprocess fork/exec -> exit samples --
  ``ATHENAEUM_WITNESS_LEDGER_ENABLED=false`` (baseline, gate checked and
  short-circuited) and ``=true`` (witness path runs) -- both with
  ``AUTO_RECALL=false`` so recall itself contributes nothing to either
  number, and reports ``enabled - baseline`` as the witness path's added
  latency. That delta, not either raw absolute number, is what the AC's
  25 ms figure is measured against.
* The signing delta is a separate in-process timer around
  ``promptwitness.ledger.append_line`` directly (unsigned vs. signed, same
  arguments otherwise), because isolating the signing cost from fork/exec
  noise entirely is the whole point of that second delta.
* Reported per sample set: n, mean, p50, p95, min, max (milliseconds).

Usage::

    python scripts/measure_claude_code_witness_hook_latency.py --n 200
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
    n: int, ledger_dir: Path, cache_dir: Path, *, witness_enabled: bool
) -> list[float]:
    """Real subprocess fork/exec -> exit of the actual hook entry point.
    ``AUTO_RECALL=false`` on both sides so recall contributes nothing to
    either sample set -- only ``ATHENAEUM_WITNESS_LEDGER_ENABLED`` differs."""
    env = {
        "PYTHONPATH": SRC,
        "PATH": "/usr/bin:/bin",
        "ATHENAEUM_CACHE_DIR": str(cache_dir),
        "ATHENAEUM_WITNESS_LEDGER_ENABLED": "true" if witness_enabled else "false",
        "AUTO_RECALL": "false",
        "PROMPTWITNESS_DIR": str(ledger_dir),
    }
    stdin_payload = json.dumps(
        {"prompt": "measure the witness hook latency for this turn", "session_id": "lat-sess"}
    )
    samples: list[float] = []
    for _ in range(n):
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


class _LocalEd25519Provider:
    """Measurement-only ``KeyProviderLike`` stand-in (athenaeum#2149's
    reversible default -- NOT athenaeum#2150's shipped adapter). Generates
    an in-memory key pair and signs with it directly; the private key is
    never serialized to PEM or any other on-disk format, so no key
    material -- and no PEM banner -- ever exists outside process memory."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._key = Ed25519PrivateKey.generate()

    def key_id(self) -> str:
        return "measurement-stand-in"

    def algorithm(self) -> str:
        return "ed25519"

    def strength(self) -> str:
        return "software-file"

    def presence_required(self) -> bool:
        return False

    def sign(self, payload: bytes) -> bytes:
        return self._key.sign(payload)


def _measure_append_line(n: int, ledger_dir: Path, *, signed: bool) -> list[float]:
    """In-process timer around ``promptwitness.ledger.append_line`` alone,
    isolating the signing cost from fork/exec noise."""
    sys.path.insert(0, SRC)
    try:
        from promptwitness import ledger

        key_provider = _LocalEd25519Provider() if signed else None
        samples: list[float] = []
        for i in range(n):
            start = time.perf_counter()
            ledger.append_line(
                ledger_dir,
                kind="turn",
                harness="claude-code",
                harness_version="",
                hook_version="promptwitness measurement-harness",
                key_provider=key_provider,
                session="lat-sess",
                turn=i,
                prompt_id=f"p{i}",
                prompt_kind="human",
                channel="interactive",
            )
            samples.append((time.perf_counter() - start) * 1000.0)
        return samples
    finally:
        sys.path.remove(SRC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=200, help="Samples per measurement (default 200)")
    args = parser.parse_args(argv)

    report: dict[str, Any] = {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        baseline_dir = tmp_path / "baseline" / "witness"
        baseline_cache = tmp_path / "baseline" / "cache"
        baseline_dir.mkdir(parents=True)
        baseline_cache.mkdir(parents=True)
        baseline_samples = _measure_subprocess(
            args.n, baseline_dir, baseline_cache, witness_enabled=False
        )
        report["baseline_subprocess_ms"] = _stats(baseline_samples)

        enabled_dir = tmp_path / "enabled" / "witness"
        enabled_cache = tmp_path / "enabled" / "cache"
        enabled_dir.mkdir(parents=True)
        enabled_cache.mkdir(parents=True)
        enabled_samples = _measure_subprocess(
            args.n, enabled_dir, enabled_cache, witness_enabled=True
        )
        report["witness_enabled_subprocess_ms"] = _stats(enabled_samples)

        unsigned_dir = tmp_path / "append-line-unsigned"
        unsigned_dir.mkdir()
        unsigned_samples = _measure_append_line(args.n, unsigned_dir, signed=False)
        report["append_line_unsigned_ms"] = _stats(unsigned_samples)

        signed_dir = tmp_path / "append-line-signed"
        signed_dir.mkdir()
        signed_samples = _measure_append_line(args.n, signed_dir, signed=True)
        report["append_line_signed_ms"] = _stats(signed_samples)

    none_added_p95 = round(
        report["witness_enabled_subprocess_ms"]["p95"] - report["baseline_subprocess_ms"]["p95"],
        3,
    )
    signing_delta_p95 = round(
        report["append_line_signed_ms"]["p95"] - report["append_line_unsigned_ms"]["p95"], 3
    )
    report["witness_added_latency_p95_ms"] = {
        "none": none_added_p95,
        "software_file_estimate": round(none_added_p95 + signing_delta_p95, 3),
    }
    report["signing_delta_p95_ms"] = signing_delta_p95

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
