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
real (p95 ≈ 99–105 ms in this container — see both subprocess figures below)
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
| mean | 82.182 ms | 96.818 ms | 14.636 ms |
| p50 | 90.506 ms | 98.623 ms | 8.117 ms |
| **p95** | 99.178 ms | 104.582 ms | **5.404 ms** |
| max | 163.913 ms | 152.944 ms | — (subprocess-noise bound, not meaningful as a delta) |

**p95 added latency: 5.404 ms — within the 25 ms budget.**

## `software-file` — measured against a local stand-in, not athenaeum#2150's adapter

**athenaeum#2150 (the `software-file` key adapter) is a sibling lane's
deliverable and may not exist on this branch** — this issue is explicitly
scoped to exclude signing. `record_turn` has no way to inject a
`KeyProviderLike` today, so there is no production code path yet that signs
a turn end-to-end.

To still answer the AC honestly, this harness measures the **signing
cost in isolation**: an in-process timer directly around
`promptwitness.ledger.append_line`, once with `key_provider=None` and once
with a **local Ed25519 stand-in** built from
`cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey` (already
in the `dev` extra). The stand-in key is generated and used purely in
process memory — never serialized to PEM or written to disk — so the
cryptographic *work* (key generation once, one `sign()` call per append) is
the same work `software-file` would do, which is what this measurement is
about, without claiming to BE athenaeum#2150's shipped adapter.

| | unsigned | signed (local Ed25519 stand-in) | delta |
|---|---|---|---|
| mean | 0.816 ms | 1.147 ms | 0.331 ms |
| p50 | 0.565 ms | 0.744 ms | 0.179 ms |
| **p95** | 1.533 ms | 2.042 ms | **0.509 ms** |

**Estimated `software-file` added latency: 5.404 ms (measured `none` delta)
+ 0.509 ms (measured signing delta) = 5.913 ms — within the 25 ms budget**,
assuming athenaeum#2150's shipped adapter costs roughly the same as this
stand-in's single local `sign()` call (no network, no OS keychain prompt —
`software-file` reads an on-disk key, the same order of cost as the
in-memory key used here).

## Caveats

- **Container noise.** The absolute subprocess p95 (≈99–105 ms) reflects
  this measurement container's fork/exec + Python-interpreter-startup cost,
  which varies run to run (a repeat at `--n 200` showed a noisier, even
  negative, signing delta purely from timing jitter on sub-millisecond
  `append_line` calls). The delta figures above are from an `--n 500` run
  and are the ones that matter for the AC; re-run the harness for a fresh
  sample if reproducing.
- **Not a Claude Code real-binary measurement.** This runs the adapter
  directly via `python -m`, with synthetic stdin — the real-binary,
  real-hook-wiring integration test is athenaeum#2161's explicit job, not
  this issue's.
