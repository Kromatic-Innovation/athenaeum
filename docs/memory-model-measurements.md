# Memory model measurements

Durable home for v6 (dimensional memory model) measurement artifacts.
Each `##` section is produced by one epic child issue and states, inline,
the reproducible command that generated it. This file is committed —
`docs/memory-model.md` (the design lock) is never touched by any command
that writes here.

## Reproducing the measurement pack

Issue athenaeum#713's three read-only measurement-pack artifacts, and the
operator-supplied override flags issue athenaeum#1095 added so artifact 1's
measured figures can feed artifacts 2 and 3 directly:

- `athenaeum measure shadow-linkage` — shadow-mode complete-linkage cluster
  population over the live wiki store (zero LLM calls).
- `athenaeum measure backlog-price` — backlog price sheet + decision-inflow
  sensitivity table. Optional overrides (issue athenaeum#1095 AC3):
  `--backlog-count INT`, `--calls-per-file FLOAT`,
  `--wall-clock-per-file-seconds FLOAT`. Each defaults to the
  measured/derived value; passing one records `operator-supplied` provenance
  in the written snapshot.
- `athenaeum measure ordinary-night` — ordinary-night steady-state table
  (closes/does-not-close verdict against the nightly call/wall-clock
  budgets). Optional overrides (issue athenaeum#1095 AC5):
  `--calls-per-file FLOAT`, `--files-per-day FLOAT`,
  `--wall-clock-per-file-seconds FLOAT`. Same operator-supplied provenance
  rule as `backlog-price`.

All three write/append a dated snapshot into this file unless `--dry-run` is
passed, and support `--json` for machine-readable output; see each
subcommand's own `--help` for the full flag list.

## Push-precision and coverage baseline

### Snapshot 2026-08-20T00:09:51.592236Z

Reproduce with: `athenaeum push-metrics baseline`

- window_start: (instrument-enabled)
- window_end: 2026-08-20T00:09:51.592236Z
- sessions: 65
- push_records: 221
- reference_records: 3
- precision: 0.8710
- coverage_miss_rate: n/a — awaits operator review of the coverage worksheet (`athenaeum push-metrics coverage-audit`); see that command's output file for the sampled sessions and candidate misses a human must mark
- excluded_sessions: none
- excluded_push_records: 0
- excluded_reference_records: 0
- athenaeum_version: 0.19.0
- git_sha: ca038f5bfa58

### Snapshot 2026-08-02T15:33:55.304120Z

Reproduce with: `athenaeum push-metrics baseline`

- window_start: (instrument-enabled)
- window_end: 2026-08-02T15:33:55.304120Z
- sessions: 0
- push_records: 0
- reference_records: 0
- precision: n/a — accrues as sessions run
- coverage_miss_rate: n/a — awaits operator review of the coverage worksheet (`athenaeum push-metrics coverage-audit`); see that command's output file for the sampled sessions and candidate misses a human must mark
- athenaeum_version: 0.16.4
- git_sha: 5513d80f0188

## Interpreting the measurement-pack snapshots (shadow-linkage, backlog-price, ordinary-night)

The three sections below (issue athenaeum#713) are a single, dated snapshot
of one operator's live wiki, not a benchmark suite. Read them with four
things in mind:

1. **Single-sample.** Each section is one measurement, against one
   operator's corpus, on one date. It is not a statistically representative
   sample of "wikis in general," and it should not be treated as one until
   more snapshots from other corpora exist alongside it.
2. **Calibration, not configuration.** No code reads these numbers. They
   exist so a human can sanity-check whether a design's assumptions (comparator
   load, backlog drain rate, nightly budget headroom) are plausible at real
   scale — not to parameterize any runtime behavior.
3. **Corpus-dependent.** Every figure here scales with corpus size, intake
   rate, and page shape. A wiki an order of magnitude larger or smaller,
   or with a different intake cadence, should produce materially different
   numbers than these — that divergence is expected, not a defect in either
   corpus or measurement.
4. **Timestamped and reproducible.** Each snapshot below carries its own
   `athenaeum_version`, `git_sha`, and `Reproduce with:` command exactly as
   the renderer emitted them — keep those fields verbatim if you copy or
   quote a snapshot elsewhere.

**Contribute your own snapshot.** If you run a wiki of meaningfully
different size or shape, you're invited to run the same `athenaeum measure
shadow-linkage`, `athenaeum measure backlog-price`, and `athenaeum measure
ordinary-night` commands (see "Reproducing the measurement pack" above)
against your own corpus and open a PR appending your snapshot alongside
this one. Before publishing, you can verify for yourself that these
commands are aggregate-only by construction, not by policy: cluster
membership is reduced to a bare count (`len(c.member_paths)`) before the
result object is ever built (`shadow_linkage.py:241`), and the `to_dict`
surfaces that ultimately get written or printed
(`shadow_linkage.py:184-196`, `backlog_price_sheet.py:217-236`,
`ordinary_night_table.py:287-319`) carry only counts, rates, thresholds,
digests, and timings — no page names, uids, titles, paths, or query text.

## Shadow-mode complete-linkage population

### Snapshot 2026-08-24T00:24:36.397827Z

Reproduce with: `athenaeum measure shadow-linkage`

- candidate_file_count: 2389
- cluster_threshold: 0.5500
- corpus_digest: 9f2b4a86adfec7eb
- athenaeum_version: 0.19.25
- git_sha: b9e6d8dbe237

- complete-linkage (post-athenaeum#681, current formation):
  - clusters formed: 1344 (476 multi-member)
  - size distribution (size:cluster_count): 1:868, 2:269, 3:97, 4:45, 5:18, 6:15, 7:8, 8:5, 9:6, 10:1, 11:2, 12:3, 13:2, 15:1, 17:2, 18:1, 21:1
  - pairs reaching content-comparison stage: 3008

- single-linkage (pre-athenaeum#681, historical anchor's regime):
  - clusters formed: 423 (8 multi-member)
  - size distribution (size:cluster_count): 1:415, 2:6, 3:1, 1959:1
  - pairs reaching content-comparison stage: 1917870

## Backlog price sheet

### Snapshot 2026-08-24T00:24:51.046285Z

Reproduce with: `athenaeum measure backlog-price`

- raw_backlog_count: 2986 (re-counted, not copied)
- calls_per_file: 5.987249544626594 [ledger]
- avg_input_tokens_per_file: 25951 [ledger]
- avg_output_tokens_per_file: 2063 [ledger]
- wall_clock_per_file_seconds: 662.774 [run-summary log (entity phase)]
- write_model (priced against): claude-sonnet-5
- cost_without_prefilter_usd: $162.44 (1979043.16s)
- cost_with_prefilter: n/a — pre-filter fraction not supplied (the write-refusal/retention-pack classifier does not exist yet; pass --prefilter-excluded-fraction once it does)
- athenaeum_version: 0.19.25
- git_sha: b9e6d8dbe237

The burst is paced by QUEUE CAPACITY, not by dollars: compile batches are throttled so the human decision budget stays true throughout the drain — never more compiled-and-awaiting-decision inventory than the decision-inflow rate can absorb at the stated daily budget.

Triage valve: the raw tail beyond the decision budget is cold-tiered UNCOMPILED (retrievable but never compiled), with one floor — every raw file matching a hot/warm recall hit or session reference from the trailing 6 months must compile or be individually human-waived.

Sensitivity table (decision-inflow rate -> days to terminal disposition):

| rate/100 compiled | decisions | days | breaches 6mo |
|---|---|---|---|
| 5 | 149 | 7 | no |
| 10 | 299 | 15 | no |
| 15 | 448 | 22 | no |
| 20 | 597 | 30 | no |
| 25 | 746 | 37 | no |
| 30 | 896 | 45 | no |
| 35 | 1045 | 52 | no |
| 40 | 1194 | 60 | no |
| 45 | 1344 | 67 | no |
| 50 | 1493 | 75 | no |

## Ordinary-night steady state

### Snapshot 2026-08-24T00:25:03.931349Z

Reproduce with: `athenaeum measure ordinary-night`

**The ordinary night CLOSES: total call and wall-clock load fits inside both budgets.**

*Conditionality note (derived, not measured — arithmetic on top of the figures above, not a re-run of them).* The close is dominated by the comparator assumption, not by measured intake: 429.71 of the 440.41 nightly calls are artifact 1's 3,008 complete-linkage pairs amortized over an assumed 7 nights at 1 call/pair, against only 10.69 calls/night of actually-measured ordinary intake. Two break-evens bound how far that assumption can move before the table stops closing. **Calls:** below an amortization window of ~3.81 nights, the comparator alone (at 1 call/pair) would exceed the remaining 789.31-call headroom under the 800-call budget — so the close requires amortizing over **at least 4 nights**. **Wall clock:** `--comparator-seconds-per-pair` was left at its 0 default in this run, so the wall-clock column currently attributes zero seconds to ~430 LLM calls a night, which is honest about what has not been measured but is not physical. Against the 3,600s nightly window and the 1,183.53s already spent on measured intake, the remaining 2,416.47s of headroom is exhausted above **~5.62 seconds per comparator pair** (at the 7-night amortization and 3,008-pair figures above) — past that, the night stops closing on wall clock even though it still closes on calls. So: the verdict is `closes`, conditional on (a) the amortization window staying at ≥4 nights, (b) the eventual comparator call costing under ~5.62s/pair, and (c) the TTL-recheck, invalidation-wave, and audit-sampling terms genuinely staying at zero — true today only because those subsystems don't exist yet, not because their load has been measured and found to be zero. The nightly totals in this table are therefore a **lower bound** on the comparator regime's real load; the first real comparator run should re-measure seconds/pair and re-check both break-evens against this table before the numbers here are trusted for capacity planning.

- files_per_day (ordinary intake, lower bound over trailing 14d, n=25): 1.786
- calls_per_file: 5.99 [ledger]
- wall_clock_per_file_seconds: 662.77 [run-summary log (entity phase)]
- ordinary_calls_total: 10.69
- ordinary_seconds_total: 1183.53
- amortized_calls_per_night: 429.71 (comparator/TTL/invalidation-wave/audit-sampling subsystems are not yet built (out of athenaeum#713 scope) — every figure above is an explicit operator-supplied ASSUMPTION, not a measurement)
- amortized_seconds_per_night: 0.00
- nightly_calls_total: 440.41 vs nightly_call_budget: 800
- nightly_seconds_total: 1183.53 vs nightly_window_seconds: 3600
- wave_duty_cycle: n/a (wave cadence not yet defined) vs target: 25%
- athenaeum_version: 0.19.25
- git_sha: b9e6d8dbe237
