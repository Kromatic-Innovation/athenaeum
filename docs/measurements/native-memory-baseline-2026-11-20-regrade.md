<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native-memory baseline, 2026-11-20 regrade: the decision block of run 44783114473 read under the oracle-tolerance rule

Issue athenaeum#3029, applying the operator ruling recorded on athenaeum#2805 on
2026-11-20. This document re-reads the stored rows of the grid reported in
[`native-memory-baseline-2026-11-20.md`](native-memory-baseline-2026-11-20.md),
which printed its decision block as void because the `oracle` positive control
fell below 1.000 on three enrolled groups. Nothing was run and nothing was
spent: the renderer was pointed at that run's own stored rows. The full render
is recorded verbatim beside this file:
[`north-star-grid-2026-11-20-run-44783114473-regrade-3029.md`](north-star-grid-2026-11-20-run-44783114473-regrade-3029.md).

**This document states no go and no-go.** It reads the decision block and sets
it beside the real-session numbers. The ruling is the operator's.

## 1. The rule, and when it was set

**The rule was set after the run was seen.** The grid of 2026-11-20 ran, voided
and was written up first
([`native-memory-baseline-2026-11-20.md`](native-memory-baseline-2026-11-20.md)),
with its three failing cells described. The operator's ruling was recorded on
athenaeum#2805 on 2026-11-20, after that, and before any re-grade was run or any
decision row was read. So the rule was not blind to the run: its authors knew
which cells had failed and why. What the order does give is that the marker and
the tolerance were fixed before the decision rows were read, and neither was
adjusted afterwards. Read the result below with that in mind. It is a regrade
under a rule written with knowledge of the run, not a pre-registered test.

The ruling, as implemented:

1. **Marker repair.** `spend_approver_named` gains the alternative marker
   `requester explain` on `person-amir-osei`; the original marker stays. (The
   ruling first proposed `in person`, which the corpus validator rejects for
   carrying fewer than two content terms, so the amendment on athenaeum#2805
   replaced it with the page's own phrase, which has two and appears on no other
   page.)
2. **Single-omission tolerance.** When an enrolled `oracle` row grades wrong
   while its answer-bearing pages were delivered (`marker_miss_with_delivery`),
   that (probe, scale) pair is excluded from **every** arm, native arms included,
   for conditions 1, 2 and 3, and is named in the report. The cap is one excluded
   probe per enrolled class and scale; past it, that class and scale stay void.
   An oracle miss without delivery still voids.
3. **No re-run, no pooling.** Both rules apply offline to the stored rows, under
   `GRADER_REVISION` `athenaeum#3029`. This reading does not pool with the
   `athenaeum#2918` reading of the same run, or with any earlier grid.

## 2. What was excluded

Exactly one pair:

| scale | probe class | probe |
| --- | --- | --- |
| core | `follow_through` | `subsidiary_onboarding_lesson` |

The oracle answer was handed both answer-bearing pages and gave the first
planted fact but not the second (the default of a single combined invoice). The
same probe passed at `medium`.

The marker amendment does the rest. With `requester explain` added, the two
stored `oracle` answers to `spend_approver_named` (both state the finance
partner's insistence on hearing the requester in person) now grade correct, and
`multi_hop` reads 1.000 at oracle at both scales (3/3 and 3/3). The rows of
the five other arms that grade correct on `spend_approver_named` (`pull`,
`push_breadcrumb_pull`, `push_sections_pull` and both native arms) are unchanged,
and the `none`, `push_breadcrumb` and `push_pages_upper_bound` rows still grade
wrong at both scales. The two stored `none` answers contain neither `requester
explain` nor `in person`.

After the exclusion, the oracle control reads 1.000 on every enrolled class at
both scales, so the block is not void.

## 3. The decision block, per scale

Verdict arm `push_breadcrumb_pull`, the shipped configuration. The three
conditions are those of `docs/design/native-memory-baseline.md` §7, and a cutoff
scale must be at or above `medium` and pass all three. The block's own text:
**cutoff scale `none`**.

Rows below are counts of correct over graded cells after the exclusion. Cells
that hit the turn cap are not graded, which is why some classes show fewer rows
than the corpus has probes. The per-class cost ratios are the verdict arm's cost
per correct answer over the cheaper defined native arm, from the report's own
functions; the render prints only the worst class per scale, which these match
(6.785 and 2.500).

### Core

| condition | result | reading |
| --- | --- | --- |
| 1, win the relationship use case | **fail** | `push_breadcrumb_pull` 9/9 = 1.000, best native (`native_grep`) 1.000. The rule is strictly greater, so a tie fails. |
| 2, lose no other use case | **pass** | 7 classes compared, 1 skipped (`multi_hop`, no gradable rows outside the relationship subset); not worse than native on any compared class. |
| 3, cost per correct within budget | **fail** | worst reading `fail` for `abstention` (6.785x). |

Condition 3 by class: `abstention` 6.785x, `follow_through` 2.452x and
`disambiguation` 2.094x are above the 2.0x limit and fail. `single_hop` (1.966x),
`multi_hop` (1.582x), `temporal` (1.531x) and `distractor_robustness` (1.129x)
are within the limit. `redundancy` (0.977x) is at the target.

### Medium

| condition | result | reading |
| --- | --- | --- |
| 1, win the relationship use case | **pass** | `push_breadcrumb_pull` 9/9 = 1.000 against best native (`native_grep`) 0.750. |
| 2, lose no other use case | **fail** | 8 classes compared, 0 skipped; worse than native on `abstention` (0.500 against 1.000). |
| 3, cost per correct within budget | **fail** | worst reading `fail` for `abstention` (2.500x). |

Condition 3 by class: only `abstention` (2.500x) is above the limit.
`multi_hop` (1.983x), `follow_through` (1.218x), `single_hop` (1.111x) are
within it; `temporal` (0.863x) is at the target; `redundancy` (0.540x),
`disambiguation` (0.524x) and `distractor_robustness` (0.518x) are between the
target and the aspirational 0.5x line.

### Where the verdict arm wins and loses

On correctness, after the exclusion:

- **Wins on relationship classes and on redundancy.** At medium, `disambiguation`
  1.000 against 0.500 (both native arms), `redundancy` 1.000 against 0.500 and
  0.250, `temporal` 1.000 against 0.833 and 0.500, `distractor_robustness` 1.000
  against 0.500. At core, `redundancy` 1.000 against 0.500, `temporal` 1.000
  against 0.833, `single_hop` 1.000 against 0.875.
- **Ties.** `single_hop` at medium (0.875 for the verdict arm and both native
  arms), `follow_through` at both scales (1.000), and `abstention` and
  `disambiguation` at core against `native_grep` (1.000).
- **Loses.** `abstention` at medium, 0.500 against 1.000. It is a graded 1 of 2:
  the third row was a turn-cap harness failure and is not graded. `multi_hop` at
  medium is 0.667 against `native_grep` 1.000, but `native_grep` has one graded
  row there, and condition 2 does not flag the class.

Every class has between 2 and 8 graded rows, so one row moves a rate by 12 to 50
points. The `abstention` loss that fails condition 2 at medium is a single wrong
answer in two. The same class drives condition 3 at both scales. Nothing here is a
measurement of variance: this is one run, graded once.

### The arm that ships the push

Pooled over the enrolled classes after the exclusion, correct over graded:

| arm | core | medium |
| --- | --- | --- |
| `none` (floor) | 3/35 | 3/36 |
| `push_breadcrumb` (the shipped push, no pull) | 3/35 | 3/36 |
| `push_breadcrumb_pull` | 34/34 | 32/35 |
| `native_grep` | 28/33 | 25/32 |
| `native_index` | 27/35 | 24/35 |
| `oracle` (ceiling) | 35/35 | 36/36 |

The breadcrumb push on its own scores what the floor scores. The correct answers
come from the arms that go on to call `recall` or `read_entity`.

## 4. Beside the real-session numbers

[`recall-metrics-real-sessions-2026-11-15.md`](recall-metrics-real-sessions-2026-11-15.md)
measures the shipped push on real tasks from one deployment's sessions, with an
LLM labelling relevance and the operator's spot-check still pending. The grid
measures synthetic questions over a synthetic corpus, graded by planted-marker
match. They share no tasks and are not poolable. The reading side by side:

**Where they agree.**

- **The push alone does not carry the answer.** The grid's breadcrumb-only arm
  scores the floor (3/35 and 3/36). The real sessions find the same shape from
  the other side: about half of the relevant pages in interactive turns had been
  pushed (0.527 push coverage), but the agent went on to recall or open one in
  only 14 of 385 cases (recall-through 0.036), and no pushed or recalled page
  was used under the shipped citation rule.
- **The value is in the pull.** The arm that wins the grid is the one that calls
  `recall`. Both documents put the weight on what happens after the push, not on
  the push.

**Where they do not.**

- **Whether the agent pulls.** In the grid the model called `recall` on essentially
  every pull-arm task (no-call rate 0.000 on every enrolled class at both scales;
  0.083 on `unprompted_push`). In the real sessions it almost never did: recall-through
  0.036 interactive and 0.000 headless, with no `recall` call at all in 120
  headless windows. The grid's `push_breadcrumb_pull` win rests on a behaviour the
  real sessions do not show. A question that invites lookup, and a harness that
  gives the model the tool, produce it; ordinary work did not.
- **Push relevance.** The real-session labels judged 91.7% of pushed pages
  irrelevant for interactive turns and 96.3% for headless. The grid reads
  correctness of the answer, not the relevance of the pushed set, so it does not
  speak to this.
- **Cost.** The grid prices a cell that ran a recall loop. The real sessions
  mostly did not, so the cost ratios above are not what a typical session pays.

The grid therefore says: when the model does pull, Athenaeum beats native memory
on relationship-style questions at `medium` and ties or loses elsewhere, at a
cost that fails the budget on `abstention`. The real sessions say the model
rarely pulls. Neither document settles how often the grid's condition holds in
practice.

## 5. What this document does not record

- No go or no-go, and no recommendation on the kill criterion. The block's reading
  is cutoff `none`: the cutoff needs all three conditions at a scale at or above
  `medium`, and at `medium` conditions 2 and 3 fail. That is what the rendered
  block prints, not a ruling on it.
- No pooling with the `athenaeum#2918` reading. The two differ in grader revision;
  run 44783114473 under `athenaeum#2918` is void.
- No paraphrase ruling. The 19 paraphrase cells noted in
  [`north-star-oracle-control-regrade-2026-11-19.md`](north-star-oracle-control-regrade-2026-11-19.md)
  touch report-only classes only and are outside this ruling.
- No variance estimate. One run, one replicate per cell.
