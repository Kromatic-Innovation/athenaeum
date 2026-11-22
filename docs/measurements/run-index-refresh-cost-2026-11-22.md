<!-- SPDX-License-Identifier: Apache-2.0 -->

# `athenaeum run` search-index refresh cost, 2026-11-22

Issue athenaeum#3124. Since 0.30.0, `athenaeum run` finishes by bringing the
search index up to date with the pages it wrote and the raw notes it deleted
(athenaeum#3090). This report measures what that adds to a scheduled run.

**Result.** When nothing changed, the refresh is a manifest diff and costs
well under a second: about 0.02 s at 1,000 pages and 0.6 s at 10,000 pages.
When a run changes a handful of pages, the refresh embeds only those pages.
That took about 4 s for 5 changed pages on the default `vector` backend, and
under 0.2 s on `fts5`. The refresh makes no model calls and spends no API
tokens: embeddings are computed locally on the CPU.

## Method

[`scripts/bench_run_index_refresh.py`](../../scripts/bench_run_index_refresh.py)
builds a synthetic store of invented pages (about 180 words each), indexes it
once with `reindex`, and then times `_refresh_index_after_run`, the step
`run` calls after its compile. It runs three times with nothing changed, then
three times after rewriting a given number of pages. The compile itself is not
timed, and no model is called.

| Item | Value |
| --- | --- |
| athenaeum | 0.30.0 (`develop` after the release) |
| Python | 3.13.18 |
| Machine | Linux container, aarch64, 12 vCPUs |
| `vector` backend | chromadb 1.5.9 with its default MiniLM-L6-v2 ONNX embedder |

## Results

Median of three runs; changed-page runs are listed individually.

| Backend | Pages | Full `reindex` | Refresh, nothing changed | Refresh, 5 pages changed |
| --- | ---: | ---: | ---: | ---: |
| `fts5` | 1,000 | 0.2 to 0.3 s | 0.02 to 0.07 s | 0.06 to 0.18 s |
| `fts5` | 10,000 | 2.0 s | 0.58 s | 0.93, 1.21, 1.31 s |
| `vector` | 1,000 | 353 s | 0.02 s | 3.79, 4.08, 4.71 s |

The two `fts5` rows at 1,000 pages are ranges over two separate runs.

## Reading

- The refresh cost grows with the number of pages a run changed. It does not
  grow with the size of the store, apart from the manifest diff. A full
  `vector` reindex cost about 0.35 s per page here, so a run that rewrites
  50 pages would add roughly 20 s. That figure is an estimate from the
  per-page rate, not a measurement.
- A failed refresh does not fail `run`. It prints a warning naming
  `athenaeum reindex` on stderr, and the compile's exit code stands. The next
  `run`, `session-end` or `reindex` catches the index up.
- A store on a slower machine, or with much longer pages, will take longer per
  page. Re-run the script on your own host to get your own numbers.
