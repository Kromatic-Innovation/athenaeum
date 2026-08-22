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
