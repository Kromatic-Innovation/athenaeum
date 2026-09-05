# Retrieval entry point — import-budget measurements

Durable record of the spike commissioned by issue athenaeum#1357: **can a Python
retrieval entry point serve the FTS5-only recall path inside a ≤127 ms
wall-clock budget, measured `fork/exec → exit`?**

Figures here are **measured**, never estimated. Every section records the exact
command that produced it so a later agent with no session memory can reproduce a
comparable number. The harness is committed at
[`scripts/measure_retrieval_entry_point.py`](../scripts/measure_retrieval_entry_point.py).

**Why 127 ms.** It is the cost of today's *live* shell hook path with the LLM
extractor disabled — the budget is "do not regress what the operator already
has", not an aspiration. The `<50 ms` contract stated in
`examples/claude-code/user-prompt-recall.sh`'s header is missed by every live
configuration today and is retired by this measurement.

---

## Verdict

> **GO — conditional.** A minimal retrieval entry point serves the FTS5-only path
> at **16.8–18.8 ms warm p50** and **33.3–35.3 ms cold p50** against the **127 ms**
> budget. The worst cold p95 across three 200-run samples is **54.2 ms**, a
> **+72.8 ms** margin. The condition: those numbers hold only for an entry point
> that imports **no athenaeum module**. Every path that reaches `athenaeum.search`
> as the tree stands today costs **≈440–475 ms warm p50 / ≈615–770 ms cold p50**
> and is a hard NO-GO by roughly an order of magnitude — including the file-path
> bypass the live hook already uses, which does not work (see
> [Finding 2](#finding-2--the-live-hooks-__init__py-bypass-does-not-bypass-anything)).

So the A2–A7 design is **not** abandoned; the fallback plan recorded on
athenaeum#1347 is not triggered. But the design may not assume it can `import
athenaeum.search` until athenaeum#1360 lands, and athenaeum#1360's fix must reach
**the retrieval module**, not merely the package root — which is exactly what
that issue's second acceptance criterion already demands.

> **Update — athenaeum#1360 has since landed and the condition is discharged.**
> `import athenaeum.search` fell from ~460 ms to ~62 ms and a
> `package-import` entry point now fits the budget on warm and cold p50. See
> [Re-measured after athenaeum#1360](#re-measured-after-athenaeum1360--the-condition-is-discharged)
> for what that does and does not license.

---

## Provenance

| | |
|---|---|
| Measured | 2026-09-03 |
| Issue | athenaeum#1357 |
| Tree | `develop` at `8d9390d` |
| Harness | `scripts/measure_retrieval_entry_point.py` |
| Interpreter | CPython 3.13.15 |
| Platform | Linux aarch64 (container; see [Portability](#portability-of-these-numbers)) |
| Index | `wiki-index.db`, **25,005 pages**, 8.0 MB, built from the live `~/knowledge/wiki` corpus (29,701 markdown files on disk) |
| Runs | three-candidate sweep: warm n=50, cold n=20 per candidate. `stdlib` characterisation: warm n=200, cold n=200, three independent samples. 3 discarded warm-up runs each |
| Budget under test | 127 ms, `fork/exec → exit` |
| Writes | none — read-only harness; no wiki page, ledger, or index was written |

---

## Method

**Wall clock is `fork/exec → exit` of a real subprocess.** Never an in-process
timer around the query. This distinction is the whole point: an in-process timer
reports ~4 ms for the FTS5 query while the real per-turn cost of the same work
through the package root is ~650 ms. A benchmark that excludes interpreter and
import start would have answered this spike with a number two orders of
magnitude wrong, in the optimistic direction.

**Three candidates, one workload.** All three parse a hook-shaped JSON envelope
on stdin, extract search terms through the stopword filter, run the FTS5 `MATCH`,
look up each hit's `description`, and emit the hook's `additionalContext`
envelope — the same work `knowledge-recall-on-turn.sh` does. Each returned 3 hits
on every run, so none is timing an empty query. Only the import path differs:

| candidate | how it reaches the FTS5 query |
|---|---|
| `stdlib` | `sqlite3` + `json` + `re`; no athenaeum import at all |
| `direct-load` | `athenaeum/search.py` loaded by file path via `importlib.util.spec_from_file_location` — the bypass the live hook's vector leg already uses |
| `package-import` | `from athenaeum.search import query_fts5_index` |

The candidates are **measurement fixtures**, written to a temp directory by the
harness at run time. Deliberately not modules under `src/athenaeum/`: building the
retrieval core is athenaeum#1357's explicit non-goal.

**Cold is measured, not asserted.** Before each cold run the harness removes the
entry point's `__pycache__`, then calls `posix_fadvise(POSIX_FADV_DONTNEED)` over
the index database, the Python executable, and the interpreter's stdlib and
site-packages trees. `--cold-check` reports the achieved effect so an inert
filesystem is visible rather than silently reported as cold:

```
cold_effect: warm_read_ms 0.69, cold_read_ms 2.35, ratio 3.43
```

A 3.4× read-time penalty after eviction confirms the page cache is genuinely
being dropped on this filesystem. (`posix_fadvise` is inert on a virtiofs/9p bind
mount — the corpus mount here is one, which is why eviction targets the index
copy on local storage rather than the corpus.)

**The index is realistic.** FTS5 query cost is a function of corpus size, so the
harness *refuses* to report against an index below `--min-pages` (default 1000).
The index measured here holds 25,005 pages built from the operator's live
`~/knowledge` corpus — the same corpus the production hook queries, not a fixture.

---

## Results

Every figure is milliseconds, `fork/exec → exit`.

### The three-candidate sweep

Three independent runs of the same command (warm n=50, cold n=20 per candidate).

| candidate | | warm p50 | warm p95 | cold p50 | cold p95 |
|---|---|---|---|---|---|
| `stdlib` | run 1 | **16.3** | 18.3 | **30.1** | 32.7 |
| | run 2 | **16.7** | 19.1 | **31.3** | 49.9 |
| | run 3 | **17.2** | 26.0 | **35.7** | 64.7 |
| `direct-load` | run 1 | 442.1 | 488.5 | 617.6 | 702.8 |
| | run 2 | 468.8 | 508.0 | 615.0 | 944.7 |
| | run 3 | 475.0 | 544.5 | 738.5 | 1036.9 |
| `package-import` | run 1 | 447.1 | 619.6 | 715.4 | 983.6 |
| | run 2 | 433.5 | 471.8 | 630.8 | 1170.2 |
| | run 3 | 456.2 | 552.1 | 768.9 | 1169.5 |

`stdlib` is a GO on every statistic of every run. `direct-load` and
`package-import` are a NO-GO on every statistic of every run, by 3.4× to 9.2×.
No reading of this data puts them near the budget.

### `stdlib` characterised at n=200

The three-candidate sweep's cold p95 for `stdlib` ranged 32.7–64.7 ms, which is
n=20 sampling noise rather than a real spread. Re-measured at **n=200 warm and
n=200 cold**, three independent samples:

| sample | warm p50 | warm p95 | warm max | cold p50 | cold p95 | cold max |
|---|---|---|---|---|---|---|
| A | 16.8 | 21.7 | 24.8 | 33.8 | 54.2 | 358.5 |
| B | 18.8 | 30.2 | 132.5 | 35.3 | 49.3 | 245.7 |
| C | 18.4 | 47.0 | 153.2 | 33.3 | 45.8 | 73.5 |

The stable figures are **warm p50 16.8–18.8 ms** and **cold p50 33.3–35.3 ms**;
cold p95 settles at **45.8–54.2 ms**. The `max` column is the honest caveat: this
box is a shared container and occasionally stalls a process for a few hundred
milliseconds. Those outliers are scheduling noise on the measuring host, not a
property of the entry point — they appear in the `pass` reference too.

**The verdict is stated on the worst cold p95 observed at n=200: 54.2 ms,
+72.8 ms of margin.**

### Reference points

Measured on the same box in the same runs, so the candidate figures above can be
attributed to a component rather than reported as a bare total.

| reference | run 1 p50 | run 2 p50 | run 3 p50 | what it is |
|---|---|---|---|---|
| `pass` | 11.4 | 10.3 | 11.6 | bare interpreter start |
| `import sqlite3, json, re` | 15.8 | 14.1 | 16.0 | interpreter + the stdlib the query needs |
| `import anthropic` | 406.4 | 344.6 | 371.0 | the SDK the package root drags in |
| `import athenaeum` | 471.2 | 429.9 | 509.8 | the package root |
| `import athenaeum.search` | 460.5 | 433.2 | 551.1 | the retrieval module, via the package root |

---

## Findings

### Finding 1 — the interpreter is not the problem; the package root is

`python3 -c pass` costs **10–11 ms** here. The stdlib the FTS5 query actually
needs adds **~4 ms**. The complete retrieval entry point — argv, stdin parse,
term extraction, FTS5 `MATCH` over 25,005 pages, three description lookups,
render, JSON emit — costs **16.8–18.8 ms warm p50** at n=200, i.e. about **6 ms
of work on top of a bare interpreter**.

Against that, `import athenaeum` costs **430–510 ms**, of which `import
anthropic` alone is **345–406 ms**. This is the component that blows the budget,
and it is filed as athenaeum#1360.

This confirms the misattribution recorded on athenaeum#1360 and athenaeum#1347
with an independent measurement: the shell sidecar's justifying claim that
"Python interpreter start costs 360–450 ms warm" is off by roughly **35×** for
what it names. Python starts in ~11 ms here. What costs ~440 ms is *this import
chain* — and note the sidecar's figure is close to the chain's real cost, which
is consistent with the cost having been measured correctly and attributed to the
wrong component.

### Finding 2 — the live hook's `__init__.py` bypass does not bypass anything

`examples/claude-code/user-prompt-recall.sh` loads `athenaeum/search.py` by file
path with `importlib.util.spec_from_file_location`, carrying the comment *"Import
search module directly to avoid `__init__.py` pulling in heavy deps."*

**It does not avoid them.** Measured directly:

```
$ PYTHONPATH=src python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('probe', 'src/athenaeum/search.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print('anthropic', 'anthropic' in sys.modules)
print('athenaeum', 'athenaeum' in sys.modules)
print('athenaeum.librarian', 'athenaeum.librarian' in sys.modules)"
anthropic True
athenaeum True
athenaeum.librarian True
```

The reason is structural, not incidental: `search.py`'s own module-scope imports
are **absolute** — `from athenaeum.models import ...`, `from athenaeum.authority
import ...`, `from athenaeum.pii import ...`, `from athenaeum.storage import
...`, `from athenaeum.store import ...`. Importing any of them executes
`athenaeum/__init__.py`, which eagerly imports `athenaeum.librarian`, which
imports `anthropic`. Loading the file under a different module name changes
nothing about what its own import statements do.

The measurement agrees with the mechanism: `direct-load` at 442–469 ms warm p50
is statistically indistinguishable from `package-import` at 434–447 ms. The
bypass buys nothing.

Two consequences:

1. The comment in `user-prompt-recall.sh` is wrong and should be corrected when
   that file is next touched. It is currently load-bearing misinformation — it
   tells a reader the vector leg is cheap when it costs the full chain.
2. **athenaeum#1360 cannot be discharged by moving the `anthropic` import into
   `librarian` alone.** So long as `search.py` imports `athenaeum.models` at
   module scope and the package root eagerly imports `librarian`, reaching the
   retrieval module still pays for the SDK. This is precisely the counter-example
   athenaeum#1360's second acceptance criterion names; this spike confirms it is
   the live behaviour and not a hypothetical.

### Finding 3 — the budget is met with room, but only by the stdlib path

The `stdlib` candidate clears 127 ms by **2.3×** on its worst n=200 cold p95, by
**3.6×** on cold p50, and by **~7×** on warm p50. Nothing in the FTS5-only
retrieval path is intrinsically expensive: the query itself is ~4 ms against a
25,005-page index, and the whole entry point is ~6 ms of work on top of a bare
interpreter.

The margin is real but it is not unconditional. It belongs to an entry point that
imports nothing from athenaeum. Any design that wants to *reuse*
`athenaeum.search` — rather than reimplement the query — needs athenaeum#1360
resolved at the retrieval-module level first.

---

## Re-measured after athenaeum#1360 — the condition is discharged

The verdict above was **GO, conditional**: the budget was met only by an entry
point importing no athenaeum module, because reaching `athenaeum.search` cost
~440 ms. athenaeum#1360 made the package root's re-exports lazy (PEP 562) and
moved `athenaeum.tiers`' `anthropic` import — annotation-only, and therefore
never evaluated at runtime — under `TYPE_CHECKING`. Re-running the same harness
on the same box, same index, immediately after:

| candidate | warm p50 | | cold p50 | | cold p95 |
|---|---|---|---|---|---|
| | **before** | **after** | **before** | **after** | **after** |
| `stdlib` | 16.3 | 16.6 | 30.1 | 30.3 | 37.9 |
| `direct-load` | 442.1 | **62.6** | 617.6 | **91.8** | 181.8 |
| `package-import` | 447.1 | **61.8** | 715.4 | **101.3** | 214.8 |

Reference points move the same way: `import athenaeum` **471.2 → 12.4 ms**,
`import athenaeum.search` **460.5 → 61.9 ms**.

**What this changes.** A retrieval entry point may now *reuse* `athenaeum.search`
rather than reimplement the FTS5 query, and stay inside 127 ms on warm p50
(61.8 ms) and cold p50 (101.3 ms). Before the fix that was a NO-GO by 5–6×.

**What it does not change.** `package-import`'s **cold p95 of 214.8 ms is still
over budget**, where `stdlib`'s is 37.9 ms. A per-turn hook is felt at its tail,
so a design that reuses `athenaeum.search` is buying a ~45 ms warm cost and a
cold tail that needs watching, in exchange for not maintaining a second copy of
the query. That is a real trade, not a free one, and it belongs to whoever builds
the core. The unconditional headroom still belongs to the stdlib path.

Finding 2 below is also now stale in a specific, good way: the file-path bypass
still does not bypass the package root — that is structural — but the package
root it re-enters no longer costs anything, so the bypass is pointless rather
than harmful. The comment in `examples/claude-code/user-prompt-recall.sh` is
still wrong about *why* it is there.

---

## Portability of these numbers

These figures were taken on a Linux aarch64 container, not on the operator's
macOS box where the 127 ms budget was originally measured. The absolute numbers
are therefore not directly comparable to that budget, and this section states
what does and does not carry across — including where the projection gets
uncomfortable.

**What carries across.** The *structure*: which component costs what, and the
size of the entry point's own work relative to interpreter start. Those are
properties of the import graph, not of the hardware. Finding 2 in particular is a
fact about `search.py`'s import statements and holds on any machine.

**What does not.** Absolute milliseconds. This box's interpreter floor is
**10.3–11.6 ms**; the operator's box measured **25 ms** for the same
`python3 -c pass` (recorded on athenaeum#1360), so it is roughly **2.3× slower**
on process start.

**Two ways to project, and they disagree — so both are stated.**

*Uniform scaling (pessimistic, and the wrong model).* Multiplying the worst
n=200 cold p95 by 2.3 gives **≈125 ms** — inside 127 ms, but only just. Read
naively, that is a warning.

*Component-wise (the defensible model).* Uniform scaling is the wrong transform
here, because it scales I/O and page-fault cost by a ratio derived from CPU-bound
interpreter start. Decomposed:

| component | measured here | on the operator's box |
|---|---|---|
| interpreter start | 10.3–11.6 ms | 25 ms (measured, athenaeum#1360) |
| entry point's own work (warm p50 − floor) | ~6 ms | ~14 ms at 2.3× |
| cold penalty (cold p50 − warm p50) | ~16 ms | ~37 ms at 2.3× |
| **cold p50 total** | **33–35 ms** | **≈76 ms** |

≈76 ms cold p50 against a 127 ms budget, on a machine whose storage is a local
SSD rather than a container over a VM — where the cold penalty should be *better*
than measured here, not worse.

**Why the two disagree, and which to believe.** The uniform projection is driven
by the p95 tail, and this box's tail is contaminated by container scheduling
stalls (see the `max` column at n=200 — the same stalls appear in the bare `pass`
reference, which cannot be an entry-point property). Scaling contaminated tail
noise by 2.3 propagates the contamination. The component-wise projection uses the
stable p50 statistics and is the one to act on.

**This is a projection, not a measurement, and it should be replaced by one.**
The harness is committed and takes one command; re-running it on the operator's
box turns the projection above into a measured figure:

```bash
python scripts/measure_retrieval_entry_point.py \
    --index ~/.cache/athenaeum/wiki-index.db --cold-check
```

The GO does not hinge on it: the candidates that fail do so by 3.4–9.2×, and both
projections of the passing candidate land inside the budget. But the operator's
box's own number should replace the projection in this document once that run
exists, and if it comes back near 125 ms rather than near 76 ms, the design
should be re-examined for tail behaviour before A2–A7 commits to a per-turn hook.

---

## Reproducing

```bash
# 1. Build a realistic index (the harness refuses anything under 1000 pages).
python -c "from athenaeum.search import build_fts5_index; \
    build_fts5_index('~/knowledge/wiki', '~/.cache/athenaeum')"

# 2. Measure. Exit status is 0 for GO, 1 for NO-GO.
python scripts/measure_retrieval_entry_point.py \
    --index ~/.cache/athenaeum/wiki-index.db \
    --cold-check --json /tmp/retrieval-budget.json
```

Useful flags: `--budget-ms` to state the verdict against a different budget,
`--warm-runs` / `--cold-runs` to change sample size, `--candidates` to measure a
subset (`--candidates stdlib --references none` is the fast path used to produce
the n=200 table above), `--prompt` to measure a different query shape, `--python`
to measure a different interpreter.

---

## Related

- athenaeum#1347 — the epic this spike gates. A no-go here would have triggered
  its recorded fallback plan; it did not.
- athenaeum#1360 — the eager-import defect. Finding 2 sharpens its second
  acceptance criterion into a confirmed live behaviour.
- `docs/design/recall-architecture.md` — what the retrieval path is for.
- `examples/claude-code/user-prompt-recall.sh` — the shell sidecar whose
  justifying performance claim Finding 1 retires.
