# measurements/

Output directory for `athenaeum measure shadow-parity` (issue athenaeum#1333):
the harness that runs the C4 contradiction detector
(`athenaeum.contradictions.detect_contradictions`) and the cluster-domain
comparator (`athenaeum.cluster_comparator.run_cluster_comparator`) over the
same cluster input and reports their verdict agreement matrix and the
comparator-call-to-detector-call multiplier.

## What lands here

Each real (non-`--dry-run`) run writes one dated markdown report:

```
shadow-parity-<YYYY-MM-DD>.md
```

The report is self-contained: agreement rate (with its formula and
denominator spelled out), the agreement matrix, the measured call
multiplier, a per-item table, the pre-run cost/call projection, and a
provenance stamp (athenaeum version, git SHA, generation timestamp, corpus
digest). A run that aborted (a `--max-usd` ceiling crossed, or a required
model client unavailable) still writes its partial report here, prefixed
with a `PARTIAL` banner naming the abort reason — nothing here is ever a
silently-incomplete result.

**Filenames never clobber.** A second run on the same day — the common
case is a retry after a `--max-usd` abort — gets a numeric suffix
(`shadow-parity-<date>-2.md`, `-3.md`, ...) rather than silently
overwriting an earlier report, so a partial run's own artifact always
survives its retry.

## Who writes it

Only `athenaeum measure shadow-parity` (see
`src/athenaeum/_cmd_measure.py` / `src/athenaeum/shadow_parity.py`). Nothing
else in this repo writes to this directory, and nothing reads from it
automatically — each report is a point-in-time artifact for a human (or an
issue body) to cite directly.

## Status

This directory ships empty of reports (only this README) — issue
athenaeum#1333 built the harness; it does not run it. The run against the
live `~/knowledge` auto-memory corpus, with real model spend under an
operator-approved cap, is a separate `~operator` issue: athenaeum#1258. That
issue's report is what populates this directory for the first time, and its
output is the input to the C4-retirement decision tracked in athenaeum#1256.
