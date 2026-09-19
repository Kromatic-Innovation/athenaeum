<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native-memory baseline — validating grid for the `follow_through` path-to-beat, 2026-09-19

Issue athenaeum#1854. This is the **validating grid** dispatched after the four
`follow_through` path-to-beat changes landed on `develop` — athenaeum#1842
(PR#1846), athenaeum#1843 (PR#1847), athenaeum#1844 (PR#1848) and
athenaeum#1845 (PR#1853) — and this document is the **reading** athenaeum#1854
asks for.

athenaeum#1854 is satisfied by *producing* this reading, including a null or a
failing one. It is not a gate: nothing below should ever fail a build, and this
document is not re-read on a later day waiting for a better number.

The full rendered report from the run is recorded verbatim beside this file:
[`north-star-grid-2026-09-19-run-35443944736.md`](north-star-grid-2026-09-19-run-35443944736.md).
That artifact expires from GitHub Actions 30 days after the run; the in-tree
copy does not.

## 1. Provenance

| | |
| --- | --- |
| Workflow run | [35443944736](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/35443944736) |
| Dispatched | 2026-09-19 12:49 UTC, completed 13:41 UTC, `workflow_dispatch` on `develop` |
| `develop` SHA at dispatch | `f7c00a0c4fbe80938a555b009a6cf124d252efb5` |
| Inputs | `north_star=true`, `north_star_scale=full`, `north_star_search_backend=vector`, `north_star_max_spend=25.00`, `north_star_workers=4`, `north_star_phase2=true`, `north_star_phase2_scales=medium`, `north_star_phase2_systems=athenaeum,native` (corpus scales left blank, so the workflow's own vector default `core,medium` applied) |
| Job conclusions | North-star grid `success`, Live-API eval suite `success`, MiniLM-dependent suite `success` |
| Planned cells | 768 (`north-star-store-vector.jsonl.planned.json`) |
| Total rollout rows | 768 — the full planned grid, no abort |
| Harness failures | 26, all `turn_cap` (see the recorded report's header for the per-arm breakdown) |
| Pre-flight price | `scale=full cells=768 estimated=$6.5664 model=claude-haiku-4-5-20251001 phase2_estimated=$1.6524`, under the `$25.00` ceiling |
| Search backend | vector (hybrid on), relevance floors off |
| `corpus_digest[core]` / `[medium]` | `edcd8dd3286d0135` / `d00353d3d4b95644` |
| Rendered report sha256 | `bb307bf5b15c146346ed0a25a92910ed03bf22b37e4ab6ccacc3ea5d82ad529c` (221279 bytes, as uploaded by the run) |

All four PRs are ancestors of the dispatched SHA — merge commits `18f7105`
(athenaeum#1846), `939352e` (athenaeum#1847), `4e4b799` (athenaeum#1848) and
`832158f` (athenaeum#1853) are all on `develop` at `f7c00a0c` — so
athenaeum#1854's first acceptance criterion is met by construction.

## 2. This run is NOT comparable to runs 35399179014 and 35407275511

**Neither prior grid pools with this one.** Two independent reasons, both named
in athenaeum#1854 and both verifiable from the reports' own headers:

1. **The bytes the model reads changed.** athenaeum#1845 (PR#1853) changed how
   the MCP surface renders outbound links (`uid — Name`) and added the
   follow-link sentence to the `recall`/`read_entity` contract. It is the first
   of the four to change bytes the model actually reads, so no run taken before
   it landed is comparable.
2. **`GENERATOR_VERSION` was bumped 3 → 4** by athenaeum#1843 (`tests/evals/corpus.py`:
   an alternative `answer_markers` value plus whitespace normalisation in the
   marker comparison, so a cell that graded `False` before can grade `True` now
   with no page byte different). That constant's own comment names runs
   35399179014 and 35407275511 explicitly as no longer pooling with anything
   recorded after the bump.

The reports' headers carry the mechanical proof: run 35407275511 recorded
`corpus_digest[core]: 72c896043be29a85` / `corpus_digest[medium]: 0fe552ab0deb4940`,
and this run records `edcd8dd3286d0135` / `d00353d3d4b95644`. Different corpus
fingerprints — the floor/ceiling tables do not pool.

A third, smaller difference points the same way: the grid itself grew from 720
to 768 planned cells (athenaeum#1839 added redundancy probes so that class has
n ≥ 4 per scale), and harness failures rose 18 → 26, so several groups' `n`
differs between the two runs.

Everything in §4 below is therefore a **reading placed side by side**, never a
pooled comparison, and never a pass condition.

## 3. The two new columns (athenaeum#1854 criterion 3)

Both columns athenaeum#1854 names are present in the rendered report.

### `follow_hop_rate` — "Follow-through hop delivered (issue athenaeum#1844, report_only)"

`n/a` for every non-`follow_through` group by construction: 175 of 191 rows are
`n/a`, and the 16 non-`n/a` rows — every `follow_through` group at both
dispatched scales — are recorded here in full.

| probe_class | corpus_scale | arm | n | follow_hop_rate |
| --- | --- | --- | --- | --- |
| `follow_through` | core | `native_grep` | 5 | 0.800 |
| `follow_through` | core | `native_index` | 6 | 1.000 |
| `follow_through` | core | `none` | 6 | 0.000 |
| `follow_through` | core | `oracle` | 6 | 1.000 |
| `follow_through` | core | `pull` | 6 | 1.000 |
| `follow_through` | core | `push_breadcrumb` | 6 | 0.000 |
| `follow_through` | core | `push_breadcrumb_pull` | 6 | 1.000 |
| `follow_through` | core | `push_pages_upper_bound` | 6 | 0.000 |
| `follow_through` | medium | `native_grep` | 3 | 1.000 |
| `follow_through` | medium | `native_index` | 5 | 1.000 |
| `follow_through` | medium | `none` | 6 | 0.000 |
| `follow_through` | medium | `oracle` | 6 | 1.000 |
| `follow_through` | medium | `pull` | 6 | 1.000 |
| `follow_through` | medium | `push_breadcrumb` | 6 | 0.000 |
| `follow_through` | medium | `push_breadcrumb_pull` | 6 | 1.000 |
| `follow_through` | medium | `push_pages_upper_bound` | 6 | 0.000 |

**Reading:** the verdict arm `push_breadcrumb_pull` delivers every
`corpus.deep_hop_uids` page on every `follow_through` cell at both scales
(1.000, n=6 each) — the same as `oracle`, `pull` and (at `medium`)
`native_index`. The three arms that cannot follow a body `[[wikilink]]` at all
(`none`, `push_breadcrumb`, `push_pages_upper_bound`) read 0.000, which is the
expected floor for them, not a finding.

### `marker_miss_with_delivery` — "Marker miss with delivery (issue athenaeum#1842, report_only)"

Of 191 rows: 16 `n/a`, 111 exactly `0`, and the 64 non-zero rows are recorded
below in full — so this table plus those two counts is a lossless record of the
column (every row not listed is `0` or `n/a`). Total miss-cells: 123.

Per class: `redundancy` 30, `single_hop` 30, `aggregation` 13, `disambiguation`
12, `temporal` 11, `negative_knowledge` 9, `unprompted_push` 7,
`distractor_robustness` 6, `contradiction` 2, `follow_through` 2, `multi_hop` 1.

| probe_class | corpus_scale | arm | n | marker_miss_with_delivery |
| --- | --- | --- | --- | --- |
| `aggregation` | core | `native_grep` | 2 | 1 |
| `aggregation` | core | `native_index` | 3 | 1 |
| `aggregation` | core | `oracle` | 3 | 2 |
| `aggregation` | core | `pull` | 3 | 1 |
| `aggregation` | core | `push_breadcrumb_pull` | 3 | 1 |
| `aggregation` | medium | `native_index` | 3 | 1 |
| `aggregation` | medium | `oracle` | 3 | 3 |
| `aggregation` | medium | `pull` | 3 | 2 |
| `aggregation` | medium | `push_breadcrumb_pull` | 3 | 1 |
| `contradiction` | core | `push_breadcrumb` | 3 | 1 |
| `contradiction` | medium | `push_breadcrumb` | 3 | 1 |
| `disambiguation` | core | `push_breadcrumb` | 4 | 4 |
| `disambiguation` | core | `push_pages_upper_bound` | 4 | 1 |
| `disambiguation` | medium | `native_grep` | 4 | 1 |
| `disambiguation` | medium | `native_index` | 3 | 1 |
| `disambiguation` | medium | `oracle` | 4 | 1 |
| `disambiguation` | medium | `push_breadcrumb` | 4 | 4 |
| `distractor_robustness` | core | `native_grep` | 2 | 1 |
| `distractor_robustness` | core | `oracle` | 2 | 1 |
| `distractor_robustness` | core | `push_breadcrumb` | 2 | 1 |
| `distractor_robustness` | core | `push_breadcrumb_pull` | 2 | 1 |
| `distractor_robustness` | medium | `oracle` | 2 | 1 |
| `distractor_robustness` | medium | `push_breadcrumb` | 2 | 1 |
| `follow_through` | core | `native_index` | 6 | 1 |
| `follow_through` | medium | `pull` | 6 | 1 |
| `multi_hop` | medium | `oracle` | 3 | 1 |
| `negative_knowledge` | core | `pull` | 3 | 1 |
| `negative_knowledge` | core | `push_breadcrumb` | 3 | 3 |
| `negative_knowledge` | medium | `native_index` | 2 | 1 |
| `negative_knowledge` | medium | `oracle` | 3 | 1 |
| `negative_knowledge` | medium | `push_breadcrumb` | 3 | 3 |
| `redundancy` | core | `native_grep` | 4 | 1 |
| `redundancy` | core | `native_index` | 4 | 1 |
| `redundancy` | core | `oracle` | 4 | 4 |
| `redundancy` | core | `pull` | 4 | 3 |
| `redundancy` | core | `push_breadcrumb` | 4 | 4 |
| `redundancy` | core | `push_breadcrumb_pull` | 4 | 1 |
| `redundancy` | core | `push_pages_upper_bound` | 4 | 2 |
| `redundancy` | medium | `native_grep` | 4 | 1 |
| `redundancy` | medium | `native_index` | 4 | 1 |
| `redundancy` | medium | `oracle` | 4 | 2 |
| `redundancy` | medium | `pull` | 4 | 2 |
| `redundancy` | medium | `push_breadcrumb` | 4 | 4 |
| `redundancy` | medium | `push_breadcrumb_pull` | 4 | 2 |
| `redundancy` | medium | `push_pages_upper_bound` | 4 | 2 |
| `single_hop` | core | `native_grep` | 8 | 1 |
| `single_hop` | core | `native_index` | 8 | 1 |
| `single_hop` | core | `oracle` | 8 | 3 |
| `single_hop` | core | `push_breadcrumb` | 8 | 7 |
| `single_hop` | core | `push_breadcrumb_pull` | 8 | 1 |
| `single_hop` | core | `push_pages_upper_bound` | 8 | 4 |
| `single_hop` | medium | `oracle` | 8 | 2 |
| `single_hop` | medium | `pull` | 8 | 1 |
| `single_hop` | medium | `push_breadcrumb` | 8 | 6 |
| `single_hop` | medium | `push_pages_upper_bound` | 8 | 4 |
| `temporal` | core | `pull` | 6 | 1 |
| `temporal` | core | `push_breadcrumb` | 6 | 5 |
| `temporal` | medium | `push_breadcrumb` | 6 | 5 |
| `unprompted_push` | core | `native_grep` | 3 | 1 |
| `unprompted_push` | core | `native_index` | 3 | 1 |
| `unprompted_push` | core | `oracle` | 3 | 1 |
| `unprompted_push` | core | `push_breadcrumb` | 3 | 2 |
| `unprompted_push` | core | `push_breadcrumb_pull` | 3 | 1 |
| `unprompted_push` | medium | `oracle` | 3 | 1 |

## 4. Per-`probe_class` regression reading (athenaeum#1854 criterion 4)

**Recorded as a reading, not as a pass condition.** The prior grid is run
35407275511 (the most recent before this one); §2 says why the two do not pool.

### 4.1 The verdict arm — `push_breadcrumb_pull`

`push_breadcrumb_pull` is the shipped configuration and the ONE arm the design
doc's §7 conditions read, so it is the per-class reading that answers
athenaeum#1845's "no class regresses on the next manually dispatched grid".

| probe_class | corpus_scale | prior n | prior | new n | new | delta | reading |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `abstention` | core | 3 | 1.000 | 3 | 1.000 | +0.000 | unchanged |
| `abstention` | medium | 3 | 1.000 | 2 | 1.000 | +0.000 | unchanged |
| `aggregation` | core | 3 | 0.333 | 3 | 0.333 | +0.000 | unchanged |
| `aggregation` | medium | 2 | 0.000 | 3 | 0.333 | +0.333 | improved |
| `contradiction` | core | 3 | 0.333 | 3 | 1.000 | +0.667 | improved |
| `contradiction` | medium | 3 | 0.333 | 3 | 1.000 | +0.667 | improved |
| `disambiguation` | core | 4 | 0.750 | 4 | 1.000 | +0.250 | improved |
| `disambiguation` | medium | 4 | 1.000 | 4 | 1.000 | +0.000 | unchanged |
| `distractor_robustness` | core | 2 | 0.500 | 2 | 0.500 | +0.000 | unchanged |
| `distractor_robustness` | medium | 2 | 0.500 | 2 | 1.000 | +0.500 | improved |
| `follow_through` | core | 6 | 0.333 | 6 | 1.000 | +0.667 | improved |
| `follow_through` | medium | 6 | 0.500 | 6 | 1.000 | +0.500 | improved |
| `multi_hop` | core | 3 | 0.667 | 3 | 0.667 | +0.000 | unchanged |
| `multi_hop` | medium | 3 | 0.667 | 2 | 1.000 | +0.333 | improved |
| `negative_knowledge` | core | 3 | 1.000 | 3 | 1.000 | +0.000 | unchanged |
| `negative_knowledge` | medium | 3 | 1.000 | 3 | 1.000 | +0.000 | unchanged |
| `redundancy` | core | 1 | 0.000 | 4 | 0.750 | +0.750 | improved |
| `redundancy` | medium | 1 | 0.000 | 4 | 0.500 | +0.500 | improved |
| `single_hop` | core | 8 | 0.875 | 8 | 0.875 | +0.000 | unchanged |
| `single_hop` | medium | 8 | 1.000 | 8 | 1.000 | +0.000 | unchanged |
| `temporal` | core | 6 | 1.000 | 6 | 1.000 | +0.000 | unchanged |
| `temporal` | medium | 6 | 1.000 | 6 | 1.000 | +0.000 | unchanged |
| `unprompted_push` | core | 3 | 0.000 | 3 | 0.333 | +0.333 | improved |
| `unprompted_push` | medium | 3 | 0.000 | 3 | 0.000 | +0.000 | unchanged |

**No `probe_class` regressed on the verdict arm at either scale.** Ten of
twenty-four class×scale groups improved, fourteen were unchanged, none moved
down. `follow_through` — the class these four PRs targeted — moved
0.333 → 1.000 at `core` and 0.500 → 1.000 at `medium`.

### 4.2 Every other arm, for completeness

Across all 191 class×scale×arm groups the side-by-side reads 50 improved, 125
unchanged and 16 down. The 16 that moved down are recorded here rather than
omitted:

| probe_class | corpus_scale | arm | prior n | prior | new n | new | delta |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `abstention` | medium | `pull` | 3 | 0.667 | 2 | 0.500 | -0.167 |
| `disambiguation` | medium | `native_index` | 4 | 0.250 | 3 | 0.000 | -0.250 |
| `distractor_robustness` | core | `native_grep` | 2 | 0.500 | 2 | 0.000 | -0.500 |
| `distractor_robustness` | medium | `native_grep` | 2 | 0.500 | 1 | 0.000 | -0.500 |
| `distractor_robustness` | medium | `oracle` | 2 | 1.000 | 2 | 0.500 | -0.500 |
| `distractor_robustness` | medium | `pull` | 2 | 1.000 | 2 | 0.500 | -0.500 |
| `follow_through` | core | `native_grep` | 6 | 0.833 | 5 | 0.800 | -0.033 |
| `multi_hop` | medium | `oracle` | 3 | 1.000 | 3 | 0.667 | -0.333 |
| `negative_knowledge` | medium | `native_index` | 3 | 1.000 | 2 | 0.500 | -0.500 |
| `redundancy` | core | `pull` | 1 | 1.000 | 4 | 0.250 | -0.750 |
| `single_hop` | core | `native_index` | 8 | 0.750 | 8 | 0.625 | -0.125 |
| `single_hop` | core | `oracle` | 8 | 0.750 | 8 | 0.625 | -0.125 |
| `single_hop` | medium | `pull` | 8 | 1.000 | 8 | 0.875 | -0.125 |
| `temporal` | core | `native_grep` | 6 | 1.000 | 6 | 0.833 | -0.167 |
| `temporal` | core | `pull` | 6 | 1.000 | 6 | 0.833 | -0.167 |
| `unprompted_push` | medium | `native_index` | 3 | 0.667 | 3 | 0.333 | -0.334 |

**Reading, with the basis stated so a reader can disagree:** no defect issue is
opened for these 16, for three reasons taken together.

1. **They do not pool** (§2): different corpus fingerprints, a different grader
   revision, a grid that grew by 48 cells, and 8 more harness failures changing
   several denominators. `redundancy`/`core`/`pull` is the clearest case —
   prior `n=1`, new `n=4`, which is precisely the defect athenaeum#1839 fixed.
2. **Every one is a single-cell flip at small n** (n ≤ 8; most n ≤ 4). At n=2 a
   one-cell flip is −0.500 by arithmetic alone.
3. **Six of the sixteen are on `native_grep`/`native_index`** — the *native*
   comparison arms, which do not go through athenaeum's MCP surface at all and
   therefore cannot have been moved by any of the four PRs under test.

The honest summary is that this run shows no regression attributable to
athenaeum#1842/#1843/#1844/#1845, and shows movement on non-verdict arms that
is not separable from run-to-run variance at these group sizes. A reader who
disagrees has the full table above and the recorded report to work from.

## 5. What else moved, recorded but not acted on

Acting on what the grid shows is explicitly out of scope for athenaeum#1854.
Two readings are recorded here because they are what a reader of this run will
want next, and neither is followed up in this document.

**The design-doc §7 decision table now passes at `medium`.** Side by side:

| run | scale | cond 1 | cond 2 | cond 3 | reading | all pass |
| --- | --- | --- | --- | --- | --- | --- |
| 35407275511 (prior) | core | fail | fail | fail | undefined | fail |
| 35407275511 (prior) | medium | pass | fail | fail | undefined | fail |
| **35443944736 (this run)** | core | fail | pass | fail | fail | fail |
| **35443944736 (this run)** | medium | **pass** | **pass** | **pass** | native-zero | **pass** |

The rendered report states **`Cutoff scale: medium`** — the first grid to
name a cutoff. Per §2 this does not pool with the prior grid, so it is a
reading on this corpus at this SHA, not a settled verdict; the go/no-go
decision it feeds is athenaeum#1736's, not this issue's.

**Phase 2 ran** (`scales=medium`, `systems=athenaeum,native`) and its write-path
and filing-loss tables are in the recorded report; this document does not
re-read them.
