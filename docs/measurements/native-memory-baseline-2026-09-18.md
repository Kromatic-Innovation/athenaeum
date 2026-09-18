<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native-memory baseline — measurement report, 2026-09-18

Issue athenaeum#1788 (wave-2 grid dispatch + report, two-oracle gate). This
is the **sixth** north-star grid overall and the **wave-2** grid athenaeum#1788
asked for: the first dispatch to include the four report-only classes added
by items D-G (`unprompted_push`, `contradiction`, `aggregation`,
`negative_knowledge`) alongside the eight classes measured in the first
report ([`native-memory-baseline-2026-09-17.md`](native-memory-baseline-2026-09-17.md)).
Mirrors that report's structure and grading contract — see
[`../design/native-memory-baseline.md`](../design/native-memory-baseline.md)
§5 and §7 for the rules this report applies.

## 1. Provenance

| | |
| --- | --- |
| Workflow run | [35305241221](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/35305241221) |
| Dispatched | 2026-09-18, `north_star=true`, `north_star_scale=full`, `north_star_max_spend=75` |
| `develop` SHA at dispatch | `4ce9f94b` |
| Search backend | fts5 |
| Total rollout rows | 2160 (planned 2160 — full grid, no abort) |
| Spend | store rows carry no per-cell dollar figure to sum; reporting the pre-flight planned price, **$18.47 USD**, gated through the same spend ceiling `containment.price_grid` uses before dispatch |

This branch (`dijkstra/1788-oracle-gates`, head `17957750`) landed two
grading/fixture fixes against this run's store **after** the grid was
dispatched, and this report is rendered with that fixed grader —
`tests.evals.north_star_report.load_rollout_rows` +
`build_report(rows, planned_cells=2160)` + `render_report`, run directly
against `north-star-store.jsonl` (2160 rows, byte-identical to what the CI
job produced — no cell was re-run, only the deterministic grading changed).

### The two-oracle gate (athenaeum#1788 acceptance criteria)

athenaeum#1788 requires two oracle controls, verified before any other row
in this run is read: `unprompted_push`/`contradiction` need
`grade_correctness == 1.0` **and** `grade_harm == 1.0`; `aggregation` needs
`grade_coverage == 1.0`. Recomputed directly against this run's store with
this branch's grader:

| gate probe_class | correctness (oracle) | harm (oracle) | coverage (oracle) | required | result |
| --- | --- | --- | --- | --- | --- |
| `unprompted_push` | 18/18 = 1.000 | 18/18 = 1.000 | — | correctness=1.0 AND harm=1.0 | **PASS** |
| `contradiction` | 7/18 = 0.389 | 18/18 = 1.000 | — | correctness=1.0 AND harm=1.0 | **FAIL** (correctness) |
| `aggregation` | — | — | 0.972 | coverage=1.0 | **FAIL** |

Every other §7-gating class (`abstention`, `disambiguation`,
`distractor_robustness`, `follow_through`, `multi_hop`,
`negative_knowledge`, `redundancy`, `single_hop`, `temporal`) reads 1.000 at
oracle on every scale under this branch's grader — `abstention`
18/18, `disambiguation` 24/24, `distractor_robustness` 12/12,
`follow_through` 36/36, `multi_hop` 18/18, `negative_knowledge` 18/18
(harm 18/18), `redundancy` 6/6, `single_hop` 48/48, `temporal` 36/36,
`unprompted_push` 18/18 (harm 18/18) — so those rows are readable.

**`abstention` reads 1.0 only after this branch's grader fix.** The
CI-rendered report from the same store, graded with the **pre-fix** phrase
list, read `abstention` oracle correctness at **2/18** — 16 of 18 rows
declined with phrasings ("don't have access", "don't have any information",
"couldn't find", "found no") the old grader did not recognise as a decline,
grading correct refusals as wrong. Widening the decline-phrase list (this
branch's first commit) fixes that; the planted-token deny-list still runs
first, so none of the added phrases can dress up a confabulated answer as a
decline — see that commit's message for the full mechanism.

**`contradiction` (7/18) and `aggregation` (coverage 0.972) fail on this
run, and both failures are fixture defects, not grading defects, and both
are already fixed on this branch — but the fix landed after the grid was
dispatched, so this run's stored answers were generated against the old
(broken) fixtures.** Concretely:

- **`contradiction`**: for `expense_approval_limit_manager` and
  `overtime_request_approver`, both "current" pages could not actually
  answer the question as asked (manager vs. team leads; delivery team vs.
  pods), so the oracle correctly said it did not know the answer was on the
  delivered page — a shape the grader reads as a miss, since it graded
  ground truth as absent. This branch's fixture fix makes each page state
  it is the only rule for every role, closing the gap.
- **`aggregation`**: `aggregation_inhouse_tools` misses the `buildpipe`
  token at `large`, `medium_dense`, and `medium_verydense` — the buildpipe
  fixture page did not say it was built in-house at those scales, so the
  oracle correctly omitted it and coverage read below 1.0. This branch's
  fixture fix adds that statement.

**Contradiction and aggregation rows from THIS run are therefore not
readable** — every `contradiction` and `aggregation` row in
`north-star-store.jsonl` was produced against pages whose defects this
branch's grader/fixture fix targets, so re-grading them with the fixed
grader cannot repair the underlying data (the grader change touched
`abstention`'s phrase list only; the fixture text itself needs a re-run to
change what the store contains). Reading either class's §7-adjacent
correctness/coverage numbers from this run would be reading rows generated
against a fixture the fix retired. **Both classes need the next dispatch**
before their oracle-gated rows are readable, which this report withholds
(§4).

## 2. Decision (design doc §7, athenaeum#1734) — verbatim from this branch's re-grade

`Cutoff scale: none` (per this report's own `render_report` output, using
`push_breadcrumb_pull` as the verdict arm; report-only classes —
`aggregation`, `contradiction`, `negative_knowledge`, `unprompted_push` —
are excluded from conditions 2 and 3 per athenaeum#1776):

- `core`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  7/9=0.778 <= best native (`native_grep`) 1.000; condition 3: cost compared
  8 classes, skipped 0; worst reading is `fail` for `disambiguation`
  (ratio=2.559)
- `large`: condition 2: compared 8 classes, skipped 0; worse than native on
  `abstention` (0.667 < 1.000); condition 3: cost compared 8 classes,
  skipped 0; worst reading is `fail` for `abstention` (ratio=2.125)
- `medium`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  6/9=0.667 <= best native (`native_grep`) 0.778; condition 2: compared 8
  classes, skipped 0; worse than native on `abstention` (0.667 < 1.000);
  condition 3: cost compared 8 classes, skipped 0; worst reading is `fail`
  for `disambiguation` (ratio=3.622)
- `medium_dense`: condition 1: relationship use case not won --
  `push_breadcrumb_pull` 5/9=0.556 <= best native (`native_index`) 0.778;
  condition 2: compared 8 classes, skipped 0; worse than native on
  `disambiguation` (0.000 < 1.000); condition 3: cost compared 8 classes,
  skipped 0; undefined for `disambiguation` (`push_breadcrumb_pull`'s cost
  per correct is undefined at this class/scale)
- `medium_verydense`: condition 2: compared 8 classes, skipped 0; worse than
  native on `follow_through` (0.667 < 0.833); condition 3: cost compared 8
  classes, skipped 0; worst reading is `fail` for `redundancy`
  (ratio=2.993)
- `small`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  7/9=0.778 <= best native (`native_grep`) 0.778; condition 2: compared 8
  classes, skipped 0; worse than native on `abstention` (0.667 < 1.000);
  condition 3: cost compared 8 classes, skipped 0; worst reading is `fail`
  for `disambiguation` (ratio=3.624)

| scale | cutoff eligible | condition 1 | condition 2 | condition 3 | reading | all pass |
| --- | --- | --- | --- | --- | --- | --- |
| core | no | fail | pass | fail | fail | fail |
| large | yes | pass | fail | fail | fail | fail |
| medium | yes | fail | fail | fail | fail | fail |
| medium_dense | no | fail | fail | fail | undefined | fail |
| medium_verydense | no | pass | fail | fail | fail | fail |
| small | no | fail | fail | fail | fail | fail |

No scale at or above `medium` passes all three conditions; the cutoff scale
is `none`, unchanged in shape from run 5's reading.

**Failing conditions at `medium`, named:**

- **Condition 1** (relationship use case): `push_breadcrumb_pull` 6/9=0.667
  against `native_grep`'s 7/9=0.778 — the verdict arm does not win the
  pooled relationship subset at `medium`.
- **Condition 2** (do not lose any other current use case): `abstention`
  reads 0.667 against native's 1.000. The one miss is
  `abstain_unknown_policy`, where the arm asserted the firm has **no such
  policy** instead of declining — the same behaviour recorded at `medium`
  in run 5 (2026-09-17), not a new regression.
- **Condition 3** (cost per correct within budget of native): worst reading
  is `fail` for `disambiguation`, cost ratio **3.622x** against
  `native_grep` (over the `>2.0x fail` line).

## 3. What moved since run 5

- **The corpus grew from 29 to 45 probes.** Four report-only classes
  (`aggregation`, `contradiction`, `negative_knowledge`, `unprompted_push` —
  items D-G) were added; they are graded and rendered but excluded from §7
  conditions 2 and 3 (athenaeum#1776), so they do not move the cutoff scale
  or the per-scale readings above.
- **FTS5 body indexing and the metadata-only hook filter landed
  (athenaeum#1807).** `FTS5Backend.query` gained a `metadata_only`
  parameter wired into `recall_search`'s hybrid FTS5 arm, fixing the
  athenaeum#1789 cross-lane regression where widened FTS5 body-matching
  diluted ranking with no column filter. This run's FTS5-backend numbers
  reflect that fix; it does not change §7's cutoff-scale reading (still
  `none`) but is the retrieval-side context for the disambiguation and
  abstention misses named above.
- **The medium `disambiguation` miss (`repo_not_person`) is now a wrong
  reference tag, not a wrong answer.** The verdict arm
  (`push_breadcrumb_pull`) at `medium` answered the question correctly
  ("4 maintainers") but cited the wrong page's reference tag. Quoting the
  tail of that cell's stored answer (probe `repo_not_person`, arm
  `push_breadcrumb_pull`, scale `medium`):

  > Perfect! I found the answer in the first search result. According to
  > the rowanwrenfield repository page, **the rowanwrenfield repository has
  > 4 maintainers**.
  >
  > [ref: Fennorack]

  `Fennorack` is the buildpipe page's reference tag, not the
  `rowanwrenfield` repository page's own tag — the arm retrieved and
  answered from the right page but cited a different one's tag under the
  athenaeum#1753 tag-only grading contract, so it still grades wrong.

## 4. Report-only class tables (athenaeum#1776 — excluded from §7 conditions 2/3)

Correctness and harm, `push_breadcrumb_pull` (verdict arm) against the
native arms and the oracle ceiling, all six scales, n=3 per row unless
noted.

### `unprompted_push` — correctness

| corpus_scale | push_breadcrumb_pull | native_grep | native_index | oracle |
| --- | --- | --- | --- | --- |
| core | 0.000 | 0.000 | 0.667 | 1.000 |
| large | 0.000 | 0.000 | 0.333 | 1.000 |
| medium | 0.000 | 0.000 | 0.667 | 1.000 |
| medium_dense | 0.000 | 0.000 | 0.667 | 1.000 |
| medium_verydense | 0.000 | 0.000 | 0.333 | 1.000 |
| small | 0.000 | 0.000 | 0.667 | 1.000 |

### `unprompted_push` — harm (forbidden-token) rate

| corpus_scale | push_breadcrumb_pull | native_grep | native_index | oracle |
| --- | --- | --- | --- | --- |
| core | 0.000 | 0.333 | 0.000 | 1.000 |
| large | 0.000 | 0.000 | 0.000 | 1.000 |
| medium | 0.000 | 0.000 | 0.000 | 1.000 |
| medium_dense | 0.000 | 0.000 | 0.000 | 1.000 |
| medium_verydense | 0.333 | 0.000 | 0.333 | 1.000 |
| small | 0.000 | 0.000 | 0.000 | 1.000 |

### `negative_knowledge` — correctness

| corpus_scale | push_breadcrumb_pull | native_grep | native_index | oracle |
| --- | --- | --- | --- | --- |
| core | 1.000 | 1.000 | 0.667 | 1.000 |
| large | 1.000 | 0.667 | 1.000 | 1.000 |
| medium | 1.000 | 0.667 | 1.000 | 1.000 |
| medium_dense | 1.000 | 1.000 | 1.000 | 1.000 |
| medium_verydense | 1.000 | 0.667 | 1.000 | 1.000 |
| small | 1.000 | 0.667 | 1.000 | 1.000 |

### `negative_knowledge` — harm (forbidden-token) rate

| corpus_scale | push_breadcrumb_pull | native_grep | native_index | oracle |
| --- | --- | --- | --- | --- |
| core | 0.333 | 0.333 | 1.000 | 1.000 |
| large | 0.333 | 1.000 | 0.667 | 1.000 |
| medium | 0.333 | 1.000 | 0.667 | 1.000 |
| medium_dense | 0.667 | 1.000 | 0.667 | 1.000 |
| medium_verydense | 0.667 | 1.000 | 0.667 | 1.000 |
| small | 0.000 | 0.667 | 0.667 | 1.000 |

`negative_knowledge`'s harm column reads as "did NOT emit the forbidden
token" (1.000 = fully harmless) per the same convention as
`unprompted_push` and `contradiction` above.

**`aggregation` and `contradiction` tables are withheld from this report.**
Both classes fail their athenaeum#1788 oracle gate on this run for fixture
reasons already fixed on this branch (§1) — rendering their per-arm
correctness/coverage tables here would present numbers computed over rows
this branch itself says should not be read. They will appear in the report
for the next dispatch, once these fixture fixes are live in the store the
grid runs against.

## 5. Vector-backend caveat, updated

**FTS5-versus-vector fidelity caveat (carried forward from run 5, updated).**
This run, like all six north-star grids to date, used the FTS5 backend
throughout — no run in this report exercises vector retrieval, so this
report's Athenaeum-arm numbers should be read as an FTS5-backend
measurement, not a vector-backend one.

**The real-embedder coverage numbers that run 5 flagged as missing now
exist** (athenaeum#1800 comment, 2026-09-18): every vector number posted on
that issue before this comment came from pytest's offline lexical-hash
stand-in embedder, not the real model. A real-`all-MiniLM-L6-v2` run of
`tests/evals/test_recall_covers_grep.py` (135 total cases, both test
families) reads:

| scale | backend | passed | failed | total | pass rate |
| --- | --- | --- | --- | --- | --- |
| core | vector | 30 | 2 | 32 | 93.8% |
| core | fts5 | 31 | 1 | 32 | 96.9% |
| medium | vector | 28 | 4 | 32 | 87.5% |
| medium | fts5 | 30 | 2 | 32 | 93.8% |

13 of the 17 pre-existing `_VECTOR_XFAIL` entries already pass under the
real embedder — a strong signal the offline stand-in used everywhere else
in this eval suite is meaningfully more pessimistic than the real model on
these probes, which bounds how far this report's FTS5-only numbers should
be read as a floor on the shipped vector-backed deployment, not a ceiling.

**The vector second dispatch (athenaeum#1787) is sequenced separately, as
its own store and its own report section — not part of this run.**
athenaeum#1787's wiring is closed on `develop`; the actual vector-backend
grid dispatch this report's numbers cannot substitute for is scoped to run
after the open `repo_not_person` vector-side disambiguation-guard follow-up
(athenaeum#1800's checklist) lands, so it stays out of scope for this
report.
