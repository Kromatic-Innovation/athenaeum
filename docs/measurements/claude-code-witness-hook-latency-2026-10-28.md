# Claude Code witness hook — latency measurement

Durable record for issue athenaeum#2149's acceptance criterion: **p95 at or
under 25 ms, with `key_provider: none` and with `software-file`.**

Figures here are **measured**, never estimated. The harness is committed at
[`scripts/measure_claude_code_witness_hook_latency.py`](../../scripts/measure_claude_code_witness_hook_latency.py).
Reproduce with:

```
python scripts/measure_claude_code_witness_hook_latency.py --n 500
```

## What the 25 ms budget is measured against

The hook process already starts a fresh Python interpreter and imports
`athenaeum.config`/`athenaeum.models` for every turn **today**, for recall's
own purposes, independent of this issue. That fork/exec + import cost is
real (p95 ≈ 41–49 ms in this container — see both subprocess figures below)
but is not what athenaeum#2149 adds: it is the pre-existing cost of invoking
the hook at all. So this harness measures the witness path's **own added
latency** as a delta: the same subprocess invocation with
`ATHENAEUM_WITNESS_LEDGER_ENABLED=false` (baseline) vs. `=true` (witness
path runs), both with `AUTO_RECALL=false` so recall itself contributes
nothing to either side. That delta — not either absolute subprocess number —
is what is compared against the 25 ms figure below.

## `key_provider: none` — real measurement

**Method:** two real subprocess fork/exec → exit samples (n=500 each) of
`python -m athenaeum.claude_code_adapter`, differing only in
`ATHENAEUM_WITNESS_LEDGER_ENABLED`. `record_turn` calls
`promptwitness.ledger.append_line(..., key_provider=None, ...)` today — this
is the real, shipped code path.

| | baseline (gate off) | witness enabled | **added (witness path)** |
|---|---|---|---|
| mean | 37.210 ms | 45.348 ms | 8.138 ms |
| p50 | 36.428 ms | 44.793 ms | 8.365 ms |
| **p95** | 41.346 ms | 49.239 ms | **7.893 ms** |
| max | 68.872 ms | 70.936 ms | — (subprocess-noise bound, not meaningful as a delta) |

**p95 added latency: 7.893 ms — within the 25 ms budget.**

## `software-file` — measured against athenaeum#2150's shipped adapter

`record_turn` always calls
`promptwitness.ledger.append_line(..., key_provider=None, ...)`: signing is
wired in by athenaeum#2150 and selected by configuration, not by this issue,
so there is no production code path that signs a turn end-to-end for a
subprocess measurement to exercise. So this harness measures the **signing
cost in isolation**: an in-process timer directly around
`promptwitness.ledger.append_line`, once with `key_provider=None` and once
with athenaeum#2150's shipped `signing.SoftwareFileKeyProvider`, bound to a
throwaway Ed25519 key that `signing.generate_and_write_key` writes into a
`TemporaryDirectory` and that dies with it. No key material and no PEM
banner is ever written into the repository or to stdout/stderr.

That adapter is the stricter of the two things that could be measured here:
it re-reads and re-parses the PEM from disk on **every** `sign` call rather
than holding a key object in memory, so the figure below is an upper bound
on what a cached-key implementation would cost.

| | unsigned | signed (`SoftwareFileKeyProvider`) | delta |
|---|---|---|---|
| mean | 0.452 ms | 0.699 ms | 0.247 ms |
| p50 | 0.457 ms | 0.695 ms | 0.238 ms |
| **p95** | 0.536 ms | 0.872 ms | **0.336 ms** |

**`software-file` added latency: 7.893 ms (measured `none` delta) + 0.336 ms
(measured signing delta) = 8.229 ms — within the 25 ms budget.**

## Caveats

- **Container noise.** The absolute subprocess p95 (≈41–49 ms) reflects this
  measurement container's fork/exec + Python-interpreter-startup cost, which
  varies run to run. A `--n 300` repeat taken while two full `pytest` suites
  were running concurrently in the same container put the deltas at
  **15.125 ms (`none`) / 15.574 ms (`software-file`)** — roughly double, and
  still inside the 25 ms budget, which is the useful thing that stress run
  shows. The figures in the tables above are from an idle-container `--n 500`
  run; re-run the harness for a fresh sample if reproducing.
- **Sub-millisecond `append_line` timings are jitter-prone.** The signing
  delta is a difference of two sub-millisecond p95s; a short run can show a
  noisier or even negative delta purely from timing jitter. Use `--n 500` or
  more.
- **Not a Claude Code real-binary measurement.** This runs the adapter
  directly via `python -m`, with synthetic stdin — the real-binary,
  real-hook-wiring integration test is athenaeum#2161's explicit job, not
  this issue's.
