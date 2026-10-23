# Transcript-receipts writer: ledger scans and transcript reads (athenaeum#2048)

Follow-up to athenaeum#753. Measures the receipts phase of the nightly intake
(`transcript_receipts.write_receipts_for_files`) before and after indexing the
ledger once per run and reading each transcript once per memory.

## Method

Synthetic, reproducible, no live store: `scripts/measure_transcript_receipts.py N`
builds N memories (10 scopes, one session each, ~150-line transcripts) in a
temp directory and runs the writer three times: a cold night (empty ledger), an
unchanged night, and a resumed night (every transcript grew by one unrelated
turn). Opens are counted by wrapping `Path.read_bytes` / `Path.read_text` for
paths under the ledger directory and the projects root. Before = `develop` at
`4ec873f7`; after = this change. One machine, one run each, so wall-clock
figures are indicative; the open counts are exact.

## Results (N = 1000)

| night | metric | before | after |
|---|---|---|---|
| cold | ledger opens | 2998 (3.00/memory) | 0 (no partition yet) |
| cold | transcript reads | 3000 (3.00/memory) | 1000 (1.00/memory) |
| cold | wall time | 4.25 s | 0.48 s |
| unchanged | ledger opens | 2000 (2.00/memory) | 1 (one monthly partition) |
| unchanged | transcript reads | 4000 (4.00/memory) | 1000 (1.00/memory) |
| unchanged | wall time | 7.06 s | 0.38 s |
| resumed | ledger opens | 3000 (3.00/memory) | 1 |
| resumed | transcript reads | 4000 (4.00/memory) | 1000 (1.00/memory) |
| resumed | wall time | 10.93 s | 0.38 s |
| resumed | ledger lines after | 2000 (doubled) | 1000 (no new receipts) |

N = 300 gave the same per-memory counts (before: 2.99 / 2.00 / 3.00 ledger
opens, 3.00 / 4.00 / 4.00 transcript reads; after: 0 / 1 / 1 ledger opens,
1.00 transcript reads per memory) with wall time 0.52 / 0.75 / 1.13 s before
and 0.16 / 0.11 / 0.11 s after. Before, the unchanged and resumed nights grew
roughly with memories x ledger lines (quadratic); after, they are flat in
ledger size.

"One ledger read" means one read of each monthly partition (the ledger is
partitioned by month), so the count is the number of partitions, independent
of memory count.

## Not measured here

A run-profile of the receipts phase against the live knowledge store is
operator-side: run the nightly intake with `librarian.transcript_receipts_enabled`
on and compare the receipts-phase duration in the run profile against a
pre-change night. This file records the synthetic numbers only.
