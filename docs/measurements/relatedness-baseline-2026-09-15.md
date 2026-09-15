# Relatedness baseline — 2026-09-15

Issues athenaeum#1576 (compile-time `related:` edge writer) and athenaeum#1570
(the offline relatedness measure and its Cluster A ground truth). First dated
baseline of the relatedness layer under `docs/measurements/`, measured
offline against a named `develop` commit — no live model call, no network,
no chromadb ONNX download.

- Corpus: `tests/evals/corpus.build_corpus(scale="core")` (96 pages), scored
  against Cluster A (`quiet-handover`,
  `tests/evals/data/corpus/ground_truth/relatedness.yaml`, via
  `tests.evals.corpus.load_unlinked_clusters()`).
- Writer under test: `athenaeum.relatedness.stamp_related_edges`, replayed
  over the corpus by `tests/evals/relatedness_writer.py::compile_corpus`
  (same entry point `librarian._apply_tier3_results` calls in production).
- Measure: `tests/evals/relatedness.py` (`score_cluster` /
  `RelatednessScore`; edges scored corpus-wide, not just within the cluster —
  see that module's docstring for why link-everything must fail on
  precision).
- Graded by: `tests/evals/test_relatedness_writer_eval.py`. Deliberately
  **unmarked** — no `eval`/`embedding` marker — so it runs in the DEFAULT CI
  selection (`-m 'not eval and not embedding and not rollout'`); all 8 tests
  in it passed at the commit below.
- Commit measured: `develop` @ `0148c7f9`
  (`0148c7f995d5fbb9b8d086386397bca74ce5673a`).
- Corpus fingerprint (`tests.evals.corpus.Corpus.fingerprint()`,
  `tests/evals/corpus.py:230`): **`ff882e71388a8ddf`**.
- Config in force (`src/athenaeum/relatedness.py`): `DEFAULT_NEIGHBOURS = 4`,
  `DEFAULT_FLOOR = 0.08`, `DEFAULT_MAX_EDGES = 4`,
  `DEFAULT_MIN_INDEX_PAGES = 50`.

Numbers below were read by running
`pytest tests/evals/test_relatedness_writer_eval.py -v` (all 8 tests green;
the recall/precision/f1 assertions inside those tests do not print the
underlying floats) and then, per this issue's plan, computing the same
`score_cluster(...)` calls the test module itself uses
(`tests.evals.relatedness.score_cluster`,
`tests.evals.relatedness_writer.compile_corpus`) in a throwaway,
**uncommitted** script against the identical corpus/cluster fixtures — no
new measure, no new fixture, just reading out the numbers the test only
asserts thresholds on.

## Per-corpus result

| Corpus | Recall | Precision | F1 | Ground-truth edges found | Spurious edges | Notes |
|---|---|---|---|---|---|---|
| **athenaeum#1576 writer** (`stamp_related_edges`) | 1.000 | 1.000 | 1.000 | 3 / 3 | 0 | Roles agreed 0/3 — role is `term-overlap` for every written edge; roles are reported, never graded (`tests/evals/relatedness.py` docstring). |
| **Link-nothing control** (`link_nothing`) | 0.000 | 0.000 | 0.000 | 0 / 3 | 0 | Every page stripped of edges — the "no librarian ran" corpus; matches the live wiki's own state (~7.9% of pages carry any edge, athenaeum#1568). |
| **Link-everything control** (`link_everything`) | 1.000 | 0.011 | 0.021 | 3 / 3 | 282 | Every page linked to every other page in the 96-page corpus; recall alone would score this perfectly, which is exactly what the corpus-wide spurious count exists to reject. |
| Ground-truth positive control (`link_ground_truth`) | 1.000 | 1.000 | 1.000 | 3 / 3 | 0 | Sanity check that the measure can score a perfect librarian at 1.000, not just fail an imperfect one. |

Writer edge count over the whole corpus: 81 edges written across 96 pages
(`0.84` edges/page, `WriterRun.edges_per_page`) — the same `0.84 edges/page`
density `src/athenaeum/relatedness.py`'s own docstring cites for the `core`
scale.

## MiniLM vs. corpus-weighted term overlap (quoted, not re-measured)

Per this issue's plan, the MiniLM-embedding figures are **quoted from
`src/athenaeum/relatedness.py:29-50`** with that file as their provenance,
not re-run here:

> **MiniLM embedding neighbourhood** (the rung athenaeum#1576 named next).
> Measured with `athenaeum.search.embed_texts` over the same text fields
> this module uses. It reaches full recall at k>=3 but drags two spurious
> edges in with it (`model-handover-ladder -> ops-escalation-path` at
> cosine 0.488, ABOVE its 0.481 to the correct target, and
> `process-shadow-fortnight -> ops-client-reporting` at 0.484, likewise
> above): precision 0.600, f1 0.750.
>
> — `src/athenaeum/relatedness.py:38-46`

The shipped writer (corpus-weighted distinctive-term overlap, the rung below
MiniLM) scores 1.000 / 1.000 / 1.000 on the same Cluster A fixture — the
comparison the module was written to make, restated here as a dated
measurement rather than only a code comment.

## What the numbers say

**The writer matches the positive control exactly.** `1.000 / 1.000 / 1.000`
for both `stamp_related_edges`'s replay and `link_ground_truth` means the
writer recovers Cluster A's three ground-truth edges and nothing else — not
merely a good score, the same score a hand-authored perfect corpus gets
(`test_writer_matches_the_positive_control`).

**Both adversaries fail from opposite sides, by construction.**
Link-nothing drives recall to 0 (nothing to find); link-everything reaches
recall 1.0 but precision collapses to 0.011 because spurious edges are
counted corpus-wide (282 of them, against 96 pages), not just within the
cluster — the scoping the measure's own docstring says is load-bearing
(a cluster-scoped count would cap link-everything at precision 0.5 instead
of ~1/N, and the floor would then have to be hand-tuned to beat it).

**The writer is a cheaper rung than MiniLM and scores higher on this
fixture.** Corpus-weighted term overlap (1.000/1.000/1.000, no model, no
network) beats the MiniLM neighbourhood (0.600/0.750 precision/f1, quoted
above) on the exact fixture that motivated trying MiniLM in the first place
— which is the whole reason the shipped writer uses the lower rung
(`docs/north-star.md` §2.2's deterministic-before-cheap-before-expensive
ladder).

**This measurement makes no live-API call and is reproducible at will.**
Unlike the attachment baseline
(`docs/measurements/attachment-baseline-2026-09-15.md`), every number here
comes from an offline pytest run plus a pure-function replay against a
committed fixture — nothing here can go stale the way an artifact retention
window does.

## What would change this baseline

- Any edit to `tests/evals/data/corpus/core/*.yaml` or
  `tests/evals/data/corpus/ground_truth/relatedness.yaml` → the corpus
  fingerprint (`ff882e71388a8ddf`) changes and every number in this file
  needs re-measuring, not just re-dating.
- A change to `DEFAULT_NEIGHBOURS`, `DEFAULT_FLOOR`, `DEFAULT_MAX_EDGES`, or
  `DEFAULT_MIN_INDEX_PAGES` in `src/athenaeum/relatedness.py` → the writer's
  edge count and density move together, and the writer's score against
  Cluster A could change even though the corpus itself did not.
- A change to `GENERATOR_VERSION` (folded into the fingerprint) → the
  fingerprint changes even if no fixture file's content does.
- Re-measuring the MiniLM rung and updating
  `src/athenaeum/relatedness.py:29-50`'s own figures → the quoted comparison
  in this file's second section needs re-quoting from the new lines, per
  this issue's own instruction not to re-run MiniLM independently here.
- A future rung (or a change to which rung ships) landing in
  `athenaeum.relatedness` → this baseline stops describing what
  `stamp_related_edges` computes and needs re-measuring against the new
  implementation.
