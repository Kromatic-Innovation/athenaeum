# Memory model measurements

Durable home for v6 (dimensional memory model) measurement artifacts.
Each `##` section is produced by one epic child issue and states, inline,
the reproducible command that generated it. This file is committed —
`docs/memory-model.md` (the design lock) is never touched by any command
that writes here.

## Push-precision and coverage baseline

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
