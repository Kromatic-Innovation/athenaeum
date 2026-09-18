<!-- SPDX-License-Identifier: Apache-2.0 -->

# Write-path retention — per-gate diagnosis, 2026-09-18

> **Note (issue athenaeum#1830, 2026-09-18 operator ruling on
> Kromatic-Innovation/athenaeum#1791 comment 5732689494):** the athenaeum row
> below measured the RETIRED raw-observation-compile shape
> (`tests.evals.write_path.compile_observation_stream` run directly against
> the observation stream) — a job the librarian never performs in
> production, since it files what Claude has already written rather than
> writing facts itself. That shape stays exported as a diagnostic, but the
> default Phase 2 athenaeum arm now compiles the NATIVE writer's own memory
> files instead (`compile_native_memory_files`), materialised as auto-memory
> intake under `raw/auto-memory/` the way production intake receives them.
> Retention is now measured on that compiled result against those same
> native files ("Claude's memories versus Claude's memories after filing"),
> plus a separate filing-loss row distinguishing the librarian's own loss
> from Claude's own write loss. The per-gate analysis below (the `max_files`
> window, session-bundling shape) is still the correct diagnosis of the RUN
> it describes; it just no longer describes the current default arm.

Issue athenaeum#1824. The Phase 2 smoke run
([35292686290](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/35292686290),
develop `831ce902`) compiled the medium observation stream through the real
librarian and retained **5 of 36** planted answer tokens, while the native
writer arm on the identical stream retained **34 of 36**. The compile exited
`0` and reported `partial: false`, so nothing on the record said the run was
anything but clean.

**Verdict: one gate, and it is not a librarian defect.** The librarian's
`max_files` window admitted 50 of the stream's 213 raw files and **deferred
the other 163 to a later run** — which a one-shot eval compile then scored as
loss. Inside the window the librarian lost nothing at all: 50 files in, 50
files processed, `32C 23U 0E 0F`, and every one of the 5 in-window planted
tokens present in the compiled wiki.

Classification of the 36 tokens: **(a) librarian defect 0 · (b) eval-shape
artefact 31 · (c) scanner miss 0.**

## 1. The gate

`max_files` — `athenaeum.librarian.DEFAULT_MAX_FILES = 50`
(`src/athenaeum/librarian.py`), resolved by `librarian_max_files()` from
`ATHENAEUM_MAX_FILES` > `librarian.max_files` > the default. It is a per-**run**
batch size counted in **files**, not a filter: files beyond the window are
deferred to the next run, and a nightly deployment drains a backlog over
successive nights. The run's own log says so in as many words:

```
backlog-drain-advisor: 163 deferred file(s) ≈ 4 night(s) to drain at current
caps/provider (ledger rate) — consider: athenaeum drain --max-usd 0.5 --yes
```

`213 − 50 = 163`. `tests/evals/write_path.py` passed no `max_files`, ran the
librarian exactly once, and read the wiki immediately — so it measured *what
one night compiles*, not *what the librarian retains*.

## 2. Per-gate retention table (smoke stream, medium, run 35292686290)

| gate | observations in | observations out | tokens lost here | evidence |
| --- | --- | --- | --- | --- |
| `ObservationStream.materialize` | 213 | 213 raw files under `raw/sessions/` | 0 | one file per observation; `run.log` git listing shows `raw/sessions/<ts>-<uuid8>.md` |
| `intake.discover_raw_files` | 213 files | 213 | 0 | no discovery warning in the log |
| **`max_files` window (50)** | **213 files** | **50 admitted, 163 deferred** | **31** | `backlog-drain-advisor: 163 deferred file(s)`; the 50 consumed files are the `delete mode 100644 raw/sessions/…` entries in the compile commit |
| ephemeral scopes / operational markers | 50 | 50 | 0 | `librarian: processed 50 file(s) (32C 23U 0E 0F)` — 0 errors, 0 failures, no drop class |
| tier-2 classification | 50 | 50 | 0 | same line; every admitted file produced an action |
| tier-3 create / merge | 50 files | 32 creates + 23 updates | 0 | same line |
| `wiki_dedupe` / merge collapse | 55 actions | 50 wiki pages (+ `_index.md`) | 0 | 50 `create mode 100644 wiki/…` entries in the compile commit |
| deadline (`max_runtime`) | — | not reached | 0 | `exit_code: 0`, `partial: false` on the phase-2 meta row |
| scanner (`compute_write_path_stats`) | 5 in-window tokens | 5 retained | 0 | see §3 |

The single largest gate is `max_files`, and it accounts for **every** lost
token. No other gate dropped anything.

## 3. Why (c) scanner miss is zero, provably

Every compiled page came from a file inside the 50-file window, so the
scanner's `corpus_text` **cannot** contain a token from a deferred file:
retained ⊆ in-window. Regenerating the stream at the run's own SHA
(`831ce902`) and applying the window in sorted discovery order yields exactly
five in-window tokens — `Cindervane`, `Fenmoor`, `Quorlinth`, `Trevanquil`,
`Voltmere` — and the run reported `answer_tokens_retained: 5`. The two sets
are therefore *equal*, not merely equal in size. The compile commit's own
page list confirms it byte-for-byte: `wiki/1bf12f5f-cindervane.md`,
`wiki/62690bcc-fenmoor.md`, `wiki/2fc93735-quorlinth.md`,
`wiki/ee042548-trevanquil.md`, `wiki/c2eb3a12-voltmere.md`.

In-window retention was **5 of 5**. Paraphrase was never plausible either:
the planted tokens are invented proper nouns, and the librarian carried each
one through verbatim into a page named after it.

## 4. Per-token classification

All 36 tokens, by whether their raw file fell inside the window
(rank = position in sorted discovery order, of 213):

| classification | count | tokens |
| --- | --- | --- |
| retained (in window) | 5 | `Cindervane` (11), `Fenmoor` (41), `Quorlinth` (22), `Trevanquil` (20), `Voltmere` (23) |
| **(b) eval-shape artefact** | **31** | `Ambervost` (118), `Ashcaldera` (62), `Brindlemoor` (159), `Brindlewake` (100), `Cinderquill` (187), `Copperfen` (181), `Driftwick` (165), `Emberlyn` (134), `Fendrickal` (146), `Fennorack` (70), `Glimmerhollow` (102), `Greywisp` (144), `Halvorwick` (140), `Harrowvex` (112), `Hollowmarsh` (150), `Kestrelgate` (77), `Larkspindle` (86), `Marrowfen` (129), `Oakenspire` (158), `Ossmarl` (139), `Pallowrift` (113), `Quenfrost` (67), `Quillbrook` (130), `Sablecroft` (172), `Sablethorn` (177), `Tanglewrought` (145), `Thistlemere` (124), `Thornmere` (132), `Verdantholt` (171), `Vulmarsh` (73), `Wrenholt` (180) |
| (a) librarian defect | 0 | — |
| (c) scanner miss | 0 | — |

The evidence line is the same for all 31 and is mechanical rather than
inferred: **the token's raw file ranked beyond the 50-file `max_files` window
in sorted discovery order, so it never reached the tier chain; the run's own
log reports `163 deferred file(s)`.** Every rank above is > 50 and every
retained rank is ≤ 50, with no exceptions in either direction.

This is (b) and not (a) because the shape that overflowed the window is one
production intake never produces. Production intake **is** Claude's own
auto-memory output: one file per *session*, carrying every observation that
session produced. A real day arrives as a few dozen multi-observation files.
The eval materialised 213 singletons under a synthetic `sessions/` source,
and `max_files` counts files.

## 5. The counter-example: the same stream in production shape

Gate admission is deterministic — the window is applied to files in sorted
discovery order — so it is computable from the stream alone, with no model
client. (The method is validated against the live run: applied at `831ce902`
it reproduces the consumed-file set and the 5-token result exactly.)
Re-measured on develop's stream, which now carries 285 observations
(62 durable planted tokens + 3 transient, §6):

| arm | `session_size` | `max_files` | raw files | admitted | deferred | answer tokens admitted | transient tokens admitted |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A — as shipped, one file per observation | 1 | 50 (default) | 285 | 50 | 235 | 5/62 | 0/3 |
| B — production shape only | 6 | 50 (default) | 48 | 48 | 0 | 62/62 | 3/3 |
| C — production shape + window sized to input | 6 | sized (48) | 48 | 48 | 0 | 62/62 | 3/3 |

One caveat on reading arm B: `session_bundles` groups observations in
*stream* order, which the generator has already shuffled, so a bundle's
contents are scattered across the date range and a bundle is named after
whichever of its observations the shuffle put first. Under bundling, *which*
tokens would be deferred by a partially-filled window is therefore a function
of the shuffle rather than of anything meaningful. That is exactly why arm C,
not arm B, is the default: with the window sized to the input nothing is ever
partially admitted, and the question does not arise.

Arm A loses 57 of 62 tokens before a single model call. Arm B recovers all of
them by changing only the intake shape. Arm C is the new default and is the
one that is robust: bundling alone clears the window only while the stream
stays under 50 bundles, which one more core page would erase, so
`compile_observation_stream` also sizes `max_files` to the materialised file
count — the single-run equivalent of letting the backlog drain.

**Both fixes are in this change**:
`ObservationStream.materialize(root, session_size=...)` bundles observations
into session files (`session_bundles()` exposes the grouping so the file
count is checkable without materialising a tree), and
`compile_observation_stream` defaults to
`session_size=DEFAULT_SESSION_SIZE` (6) with `max_files` sized to the input.
`CompileOutcome` now carries `deferred_raw_files`, surfaced on the phase-2
`meta` row: a nonzero value means the store was measured before its input
finished compiling and the retention number is a floor, not a result. That
field alone would have made this run's 5/36 self-explanatory on the day.

### What this measurement deliberately does not claim

An end-to-end offline compile through the real librarian against a canned
client was attempted and is **not** reported as a retention number. The
offline harness is lossless on the create path (a three-observation control
retained 3/3) but not faithful on the update path — it produced `33C 0U`
where the live run produced `32C 23U` — so its end-to-end retention is a
floor of the *harness*, not of the librarian, and is not comparable to the
live 5/36. Gate admission, which needs no model at all, is the honest offline
half; the per-gate attribution in §2 rests on the live run's own log.

## 6. The missing half: observations that should NOT be retained

Every number above rewards remembering. A system that retained 36/36 would
have scored perfectly while also hoarding an outage that cleared before
lunch. `Observation` therefore gains `retain: bool = True`, and the core and
medium streams (the generator is scale-invariant, so one change covers both)
carry three `retain=False` observations of the transient kind, each with its
own planted token that must appear in no compiled page:

| kind | token | observation |
| --- | --- | --- |
| temporary outage | `Zephrandil` | the client portal is returning 503s this morning, tracked under a temporary reference |
| one-off status | `Marrowglint` | the nightly export finished at 14:02 today and the queue is empty |
| mid-task instruction | `Ossivane` | for this task only, stage the working copy in a named scratch sheet |

They ride the same stream and the same seeded interleave as every durable
observation, so neither system can tell a should-drop observation from a
should-keep one by position or batch. `compute_write_path_stats` reports
`transient_total` / `transient_retained` beside `answer_tokens_retained`, and
`render_report` renders both — with `transient_retained` the one column where
**lower is better**. Transient observations are excluded from every retention
field: counted naively they would inflate `answer_tokens_total`,
`observations_measured` and `observations_dropped`, grow `pages_targeted`
with `transient-*` pages that were never meant to exist, and score a correct
discard as a lost fact.

**Read arm A's `0/3` as "never offered", not "correctly dropped".** No
transient file fell inside the 50-file window, so the as-shipped shape cannot
measure the transient side at all. Arm C admits all three, which is what makes
the measurement meaningful: from the next Phase 2 dispatch, a nonzero
`transient_retained` is a real finding about the store.

## 7. What is not settled here

The two arms no longer consume identical input shape. The native writer arm
runs one session per observation (`meta.sessions: 213`); the librarian arm now
receives bundled session files. That asymmetry touches the write-cost
comparison as well as retention and is a deliberate open item — see the PR
checklist on athenaeum#1824.

Related: athenaeum#1736 (decision record), athenaeum#1788, athenaeum#1791,
athenaeum#1775, athenaeum#1726.
