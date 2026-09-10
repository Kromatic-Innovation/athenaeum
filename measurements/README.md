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

---

# North-star report (athenaeum#1523)

Output for `python -m tests.evals.north_star_cli` (issue athenaeum#1523):
the driver that runs the four-arm rollout grid (NONE/PUSH/ORACLE/PULL —
issue athenaeum#1522) over the probe corpus (issue athenaeum#1521) and
reports the dimensions that cost nothing to measure: PULL no-call rate,
query quality, cost per turn, efficiency, waste, and utilization (the free
portion — uid citation and distinctive n-gram overlap, never a judged
per-claim check). Same convention as the shadow-parity section above, not a
second one invented for this issue.

## What lands here

Each real (non-`--dry-run`) run writes one dated markdown report:

```
north-star-<YYYY-MM-DD>.md
```

The report is self-contained: the PULL no-call rate first (the issue's own
framing — the most direct evidence for the thesis under test), every
dimension broken out per probe class AND per corpus scale (never a single
aggregate), cost and quality framed as a frontier (never a single weighted
composite — the weights are a business judgement the report leaves to the
reader), a closing verdict on whether the free dimensions alone settle the
question, and a provenance stamp (athenaeum version, git SHA, generation
timestamp, a corpus digest per corpus scale in the run). A run that aborted
mid-grid still writes its partial report from whatever rows the append-only
result store already persisted, prefixed with a `PARTIAL` banner naming the
abort reason — nothing here is ever a silently-incomplete result.

**Filenames never clobber.** A second run on the same day gets a numeric
suffix (`north-star-<date>-2.md`, `-3.md`, ...) rather than silently
overwriting an earlier report, so a partial run's own artifact always
survives a same-day retry.

**No LLM judge is invoked anywhere on this path.** Every figure in the
report is a deterministic computation over already-captured rollout
transcripts (`tests/evals/north_star_report.py`). This is a measurement,
never a regression gate — nothing here ever fails a build.

## Who writes it

Only `python -m tests.evals.north_star_cli` (see
`tests/evals/north_star_cli.py` / `tests/evals/north_star_report.py`).
Nothing else in this repo writes to this directory, and nothing reads from
it automatically — each report is a point-in-time artifact for a human (or
an issue body) to cite directly.

## Status

This directory ships without a north-star report — issue athenaeum#1523
built the report generator and fixture-tested its shape end-to-end against
synthetic rollout data (`tests/evals/test_north_star_report.py`); it does
not run the live grid, because no lane container in this repo's build
pipeline can run the PULL arm's `claude -p` spawn under a real
credential. The run against the live probe corpus, with real model spend
under an operator-approved cap, is a separate `~operator` issue:
athenaeum#1547. That issue's report is what populates this directory with a
north-star report for the first time.
