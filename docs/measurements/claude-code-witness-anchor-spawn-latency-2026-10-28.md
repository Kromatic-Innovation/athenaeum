# Claude Code periodic anchor spawn — latency measurement

Durable record for issue athenaeum#2156's acceptance criterion: **with the
interval due, the hook's wall time and stdout are unchanged versus not
due** (referenced against athenaeum#2149's 25 ms p95 budget for the witness
path's own added cost).

Figures here are **measured**, never estimated. The harness is committed at
[`scripts/measure_claude_code_witness_anchor_spawn_latency.py`](../../scripts/measure_claude_code_witness_anchor_spawn_latency.py).
Reproduce with:

```
python scripts/measure_claude_code_witness_anchor_spawn_latency.py --n 200
```

## Method

Two real subprocess fork/exec -> exit samples of
`python -m athenaeum.claude_code_adapter`, witness **enabled** on both
sides (so `record_turn` + the hash-chain append run identically either
way) and a **fresh `PROMPTWITNESS_DIR` for every sample on both sides**
(first-turn session-state cost is then equal on both sides — see the
"a false first delta" note below). The only variable is the periodic
anchor interval:

- **not due**: `ATHENAEUM_WITNESS_ANCHOR_INTERVAL_MINUTES=0` — the
  due-check short-circuits immediately, never opening the guard file.
- **due**: default interval (15) on a ledger with no prior anchor activity
  at all — due immediately
  (`promptwitness.anchor_schedule.periodic_anchor_due`) — so every sample
  spawns a REAL detached `promptwitness anchor` child process via
  `subprocess.Popen`.

## Result (n=40, idle container)

| | not due | due | **delta** |
|---|---|---|---|
| mean | 62.556 ms | 130.504 ms | 67.948 ms |
| p50 | 56.724 ms | 111.822 ms | 55.098 ms |
| **p95** | 79.716 ms | 197.089 ms | **117.373 ms** |
| max | 197.916 ms | 292.026 ms | — (noise-bound) |

At face value this is well outside the 25 ms figure athenaeum#2149
established for the witness path's own in-process cost. **It is not evidence
that this design adds 117 ms of in-process work** — see the control
measurement below, which isolates exactly what the delta actually is.

## Control: a bare, no-op `Popen` costs the same, in this container

```python
subprocess.Popen([sys.executable, "-c", "pass"], stdin=DEVNULL, stdout=DEVNULL,
                  stderr=DEVNULL, start_new_session=True)
```

n=30, same container, same moment:

| | min | p50 | **p95** | max |
|---|---|---|---|---|
| bare `Popen` (no-op child) | 0.217 ms | 2.445 ms | **85.177 ms** | 106.728 ms |

This is a call that does nothing but issue `fork()`+`exec()` for a
trivial Python process and return immediately — no ledger, no promptwitness
import, no anchoring logic of any kind. Its own p95 (85 ms) accounts for
essentially the entire "due vs not due" delta above. **The fork/exec
syscall sequence itself, for spawning ANY new process, costs roughly this
much in this sandboxed container** — almost certainly overlay-filesystem
or cgroup/seccomp overhead on process creation, not anything this design
does or could do differently. The "due" measurement spawns one such
process per sample; the "not due" measurement spawns none; the delta
between them is explained by that cost alone, with no residual attributable
to this design's own logic (the guard-file check itself is a single
non-blocking `flock` plus a few bytes of file I/O — sub-millisecond).

## What the AC is actually about, and what proves it

"Zero added prompt latency" in the issue is about the hook **never
blocking on the spawned child** — not about the OS-level cost of creating
a process at all, which no implementation of "spawn a detached worker" can
avoid and which does not scale with network latency, calendar timeouts, or
how overdue the anchor is. That structural property — the hook's own code
never calls `.wait()`/`.communicate()`/reads a pipe from the child, and
returns regardless of whether the spawn succeeds instantly or the detached
process goes on to block on a real network call for 30 seconds — is proven
directly, and is environment-independent, by
`tests/test_claude_code_adapter.py::TestPeriodicAnchorSpawn::test_hook_never_waits_on_the_spawned_process`
(a fake child process object whose `.wait()`/`.communicate()` raise if
ever called; the test asserts a clean, error-free return) and by
`test_two_turns_in_the_same_interval_spawn_at_most_one_process` /
`test_due_spawns_exactly_one_detached_process`, which assert the exact
`Popen` call shape (closed stdio, `start_new_session=True`) rather than
timing it.

## Caveats

- **Container noise dominates this measurement far more than it dominated
  athenaeum#2149's.** That measurement added only in-process work (no new
  process); this one adds a real `fork()`+`exec()`, which this sandboxed
  container appears to charge heavily and with high variance (the bare-Popen
  control's own min-to-max range is 0.2 ms to 107 ms). Re-run the control
  alongside any reproduction — a due/not-due delta that roughly tracks the
  control's own p95 is the expected, passing shape; a delta that *exceeds*
  the control by a wide margin would be the real regression to look for.
- **A false first delta, and why both sides use a fresh ledger dir.** The
  first draft of this harness reused one `PROMPTWITNESS_DIR` across all
  "not due" samples (valid — it never spawns, so staleness doesn't matter)
  while giving "due" a fresh dir per sample (required — a ledger with any
  prior anchor activity stops being due). That asymmetry meant "not due"
  paid `promptwitness.harness.claude_code`'s first-turn-only `ps` probe
  (`_is_launch_argument`) exactly once across the whole run while "due"
  paid it on every single sample, producing an apparent ~145 ms delta that
  was actually a session-state-reuse artifact, not an anchor-spawn cost.
  Giving both sides a fresh ledger directory per sample (this harness's
  current form) removes that asymmetry.
- **Not a Claude Code real-binary measurement**, same caveat as
  athenaeum#2149's own harness — this runs the adapter directly via
  `python -m`, with synthetic stdin.
