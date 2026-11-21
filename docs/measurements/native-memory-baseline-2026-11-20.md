<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native-memory baseline — re-run for the kill criterion, 2026-11-20: the oracle control failed again, on fewer cells, and the decision rows are void

Issue athenaeum#2805. `docs/use-cases.md` §1 names the comparison against native
memory as the kill criterion, and `docs/design/native-memory-baseline.md` §7 is
its decision rule. athenaeum#2805 asks for a fresh grid on the current
`develop`, with the oracle positive control passing **before** any decision row
is read. This is the second re-run, after
[`native-memory-baseline-2026-11-19.md`](native-memory-baseline-2026-11-19.md)
and the grader repair in
[`north-star-oracle-control-regrade-2026-11-19.md`](north-star-oracle-control-regrade-2026-11-19.md)
(athenaeum#2918).

**It did not pass. Every decision row of this run is void and none is used as a
verdict here.** The repairs from athenaeum#2918 did what the offline re-grade
said they would: `single_hop` and `redundancy`, the two enrolled classes that
failed on 2026-11-19, now read 1.000 at oracle at both scales. Three other
enrolled groups fell below 1.000 in their place, and a strict control reads one
missed answer as a failure. This document records the control, the provenance,
and what the failing oracle cells show, and then stops. It states no per-class
comparison against native memory, no cost ratio, no cutoff scale, and no go or
no-go. It does not place the grid beside the real-session numbers in
[`recall-metrics-real-sessions-2026-11-15.md`](recall-metrics-real-sessions-2026-11-15.md),
because there is no valid grid result to place them beside.

The full rendered report from the run is recorded verbatim beside this file:
[`north-star-grid-2026-11-20-run-44783114473.md`](north-star-grid-2026-11-20-run-44783114473.md).
That artifact expires from GitHub Actions 30 days after the run; the in-tree
copy does not. The rendered report prints its "Decision" block as void, and its
per-class tables are evidence only. They are committed so the evidence outlives
the artifact. Recording the render is not reading its rows.

athenaeum#2805 is satisfied by producing a dated report. This one reports that
the control failed, which is a result, not a gate.

## 1. Provenance

| | |
| --- | --- |
| Workflow run | [44783114473](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/44783114473) |
| Dispatched | 2026-11-20 17:25 UTC, `workflow_dispatch` on `develop`; the north-star job finished 18:32 UTC |
| `develop` SHA at dispatch | `71d17ac248a796b87c0aff852dedbda4cb4eb806` |
| athenaeum version stamped in the report | 0.27.5 |
| Inputs | `north_star=true`, `north_star_scale=full`, `north_star_search_backend=vector`, `north_star_max_spend=25.00`, `north_star_workers=4`, `north_star_phase2=true`, `north_star_phase2_scales=medium`, `north_star_phase2_systems=athenaeum,native` (corpus scales left blank, so the workflow's own vector default `core,medium` applied); identical to the 2026-11-19 run |
| Job conclusions | North-star grid (API mode) `success`, Live-API eval suite `success`, MiniLM-dependent suite `success`, Calibration sweep `skipped` |
| Mode | API (Anthropic Messages tool-use loop), model `claude-haiku-4-5-20251001`; Phase 2 native prompt fidelity `reconstructed` |
| Planned cells | 1026 (`north-star-store-vector.jsonl.planned.json`) |
| Total rollout rows | 1026, the full planned grid; no torn rows and no duplicate rows (`load_rollout_rows_and_diagnostics`, run offline on the downloaded store) |
| Harness failures | 23, all `turn_cap`: `native_grep` core 3, `native_grep` medium 6, `native_index` core 1, `native_index` medium 1, `pull` core 2, `pull` medium 5, `push_breadcrumb_pull` core 1, `push_breadcrumb_pull` medium 2, `push_sections_pull` core 1, `push_sections_pull` medium 1 |
| Pre-flight price | `scale=full cells=1026 estimated=$8.7723 model=claude-haiku-4-5-20251001 phase2_estimated=$1.9440 preflight_estimated=$0.0770`, which sums to $10.7933 against the `$25.00` ceiling (the same figures as 2026-11-19) |
| Search backend | vector, hybrid on, relevance floors off |
| Grader revision | `athenaeum#2918` |
| `corpus_digest[core]` / `[medium]` | `3c5ea876688b089e` / `42e6ce0dfdc53e02` |
| Rendered report sha256 | `66e6bcbba764bda29423ae9a1996e1b37c551e0e6143b6fa321bf3378a920a69` (266702 bytes, as uploaded by the run) |

I re-graded every `oracle` row of the downloaded store with the in-tree
`grade_correctness` and the in-tree probe corpus (`load_probes`, `build_corpus`).
`GRADER_REVISION` read `athenaeum#2918` and both corpus fingerprints matched the
stamped digests. The per-class counts in section 3 are that re-grade, and they
agree with the rendered report's correctness table and with its three-row void
table.

### Spend against the USD 25 ceiling

The run records tokens, not a billed amount. The figures below are **derived
from recorded token counts at the repo's own rate table**, by the same method as
the 2026-11-19 report: haiku-4-5 at USD 1 / 5 per million input / output tokens;
cache writes at 1.25x and cache reads at 0.10x, the multipliers the harness bills
cache activity at. They are not taken from an invoice.

| Part | Tokens (in / out) | Derived USD |
| --- | --- | --- |
| Grid rollouts, 1026 rows | 8,762,490 / 326,860 | 10.40 |
| Pre-flight probe rows, 9 rows | 49,602 / 1,454 | 0.06 |
| Phase 2 native writer, `medium` | 4,159,144 / 170,073 | 5.01 |
| Phase 2 librarian filing, `medium` | 110,972 / 6,411 | 0.14 |
| **Grid job subtotal** | | **15.61** |
| Live-API eval suite job (`eval-summary.json` `token_usage`, per-model rates, cache terms included) | 139,327 / 24,928 plus 4,636 cache-write and 37,088 cache-read | 0.70 |
| **Whole run, derived** | | **16.30** |

The arithmetic for the largest line is 8.762490 + 5 x 0.326860 = 10.397, and for
the native writer 4.159144 + 5 x 0.170073 = 5.010. No grid or pre-flight row
recorded any cache-write or cache-read tokens, so those multipliers apply only
to the eval suite job. Its per-model parts are `claude-opus-5` at USD 5 / 25
(0.12), `claude-sonnet-5` at USD 3 / 15 (0.49), and haiku-4-5 under both its
dated and undated names at USD 1 / 5 (0.07 and 0.01).

Read this as a lower bound: Phase 2 filing retries and any librarian compile
calls that were not written to the Phase 2 store are not in it. The grid job
alone, and the whole run, are under the USD 25 ceiling either way. As on
2026-11-19, the Phase 2 native writer came to USD 5.01 against a Phase 2
estimate of USD 1.94, and the grid job to USD 15.61 against a total pre-flight
estimate of USD 10.79. The two paid runs together come to about USD 32.6 derived
(16.32 and 16.30).

## 2. What differs from 2026-11-19, and why nothing pools

| | 2026-11-19 (run 44438673846) | 2026-11-20 (run 44783114473) |
| --- | --- | --- |
| `develop` SHA | `8d047902794454407bc64e361df5cdbd928b9bb8` | `71d17ac248a796b87c0aff852dedbda4cb4eb806` |
| athenaeum version | 0.26.10 | 0.27.5 |
| `corpus_digest[core]` | `3c5ea876688b089e` | `3c5ea876688b089e` |
| `corpus_digest[medium]` | `42e6ce0dfdc53e02` | `42e6ce0dfdc53e02` |
| Grader revision | `athenaeum#1935` | `athenaeum#2918` |
| Planned cells / rows | 1026 / 1026 | 1026 / 1026 |
| Harness failures | 24 | 23 |
| Arms | 9 | 9 |
| `unprompted_push` probes per scale | 12 | 12 |

**The grader differs; the pages do not.** athenaeum#2918 made two changes to the
grading side: `_normalize_marker_for_match` now strips markdown emphasis
delimiters before the substring match, and four probe markers were repaired
(`confidentiality_rule`, `keelbridge_programme_scope`, `bramfield_retainer_renewal`,
`triform_vendor_consolidation`). The corpus digests are unchanged because
`Corpus.fingerprint()` covers pages only, and neither change touches a page, so
the corpus pages are identical to the 2026-11-19 run. The digest cannot show a
change to `probes.yaml` or to the matcher, which is why the grader revision
moved. `git diff 8d047902 71d17ac2 -- tests/evals` touches the matcher, the four
markers, and the report renderer and its tests, and nothing under the rollout
code. A report stamped `athenaeum#2918` and one stamped `athenaeum#1935` are not
comparable, so **nothing in this run pools with 2026-11-19**, or with any earlier
grid. Everything below is a reading of this run alone, set beside the earlier
one only as a reference for what moved.

## 3. The oracle positive control

The control is `oracle`: the ground-truth pages handed to the model verbatim.
`docs/design/native-memory-baseline.md` §5 states that it must score at or near
1.0 on every non-abstention class, and that when it does not, the grading
contract is not satisfiable and the run's verdict rows must not be read. The
renderer now applies that rule strictly: any enrolled class below 1.000 at oracle
at any scale prints the decision block as void
(`ORACLE_CONTROL_FLOOR`, athenaeum#2918). The counts are the number of graded
oracle cells that were correct, over the number graded. They come from
`grade_correctness` applied to each oracle row of the downloaded store, and they
agree with the rendered report's correctness table.

| probe_class | enrolled in §7 | oracle core | oracle medium | result |
| --- | --- | --- | --- | --- |
| `abstention` | yes | 3/3 | 3/3 | clears |
| `disambiguation` | yes | 4/4 | 4/4 | clears |
| `distractor_robustness` | yes | 2/2 | 2/2 | clears |
| `follow_through` | yes | **5/6** | 6/6 | **fails at core** |
| `multi_hop` | yes | **2/3** | **2/3** | **fails at both** |
| `redundancy` | yes | 4/4 | 4/4 | clears |
| `single_hop` | yes | 8/8 | 8/8 | clears |
| `temporal` | yes | 6/6 | 6/6 | clears |
| `aggregation` | report-only | 1/3 | 2/3 | fails |
| `contradiction` | report-only | 3/3 | 3/3 | clears |
| `negative_knowledge` | report-only | 2/3 | 3/3 | fails at core |
| `unprompted_push` | report-only | 5/12 | 5/12 | fails |

**The control fails.** The render's void table lists the same three enrolled
groups, `follow_through` core at 0.833 and `multi_hop` at 0.667 at both scales.
Every other enrolled class and scale reads 1.000. Under the rule in the design
doc and the renderer, the decision rows of this run are void, and this document
does not use them. The report-only classes also read below 1.000; they do not
trigger the void, and I have not classified their failing cells here.

### Against 2026-11-19

On 2026-11-19 the enrolled failures were `single_hop` (5/8 and 6/8) and
`redundancy` (2/4 and 2/4): nine failing cells in four groups. Now it is three
cells in three groups, and across all twelve classes 21 failing oracle cells
against 30. The two classes that failed then pass now, which is what the
athenaeum#2918 re-grade of the stored answers predicted.

The enrolled groups that fail now **passed on 2026-11-19**. That run's render
reads `follow_through` 6/6 and `multi_hop` 3/3 at oracle at both scales
(Correctness table, `oracle` rows). So these are new failures, on the same pages
(identical digests), with markers and matcher behaviour unchanged for these two
probes, in a harness whose rollout code did not change between the two commits.
That points to run-to-run variance in the model's sampled answers. It is one run
each, so it is a reading and not a measurement of the variance.

## 4. What the failing oracle cells show

There are three failing enrolled cells, in two probes. In each, the render's
`marker_miss_with_delivery` count is 1 for that oracle group: by that
diagnostic's own definition (athenaeum#1842) every answer-bearing page **was
delivered** to the arm and the planted marker was **absent** from the answer. The
oracle arm hands the ground-truth pages over verbatim, so delivery is by
construction here, and the answers themselves carry the planted reference tags
of the pages they were given (`Voltmere` for `spend_approver_named`; `Wrenholt`
and `Copperfen` for `subsidiary_onboarding_lesson`). None of the three is a
retrieval miss. Each is an answer that did not contain the planted phrase.

| probe_class | probe_id | scales failed | reading |
| --- | --- | --- | --- |
| `multi_hop` | `spend_approver_named` | core, medium | probe defect |
| `follow_through` | `subsidiary_onboarding_lesson` | core | model failure, figure omitted |

The patterns below are my reading of the stored oracle answers and the in-tree
probe and page definitions. I have not fixed anything and I have not re-run
anything. The buckets are the ones used in
[`north-star-oracle-control-regrade-2026-11-19.md`](north-star-oracle-control-regrade-2026-11-19.md).

- **`spend_approver_named`: a probe defect, at the boundary of the paraphrase
  bucket.** The question is "who signs off discretionary spend over 500 GBP, and
  will he clear it by email alone or does he insist on a direct conversation
  first?" The marker, on the finance partner's page, is "from a written note by
  itself"; the page reads "He never approves this kind of request from a written
  note by itself; he waits to hear the requester explain it in person". The
  question never uses the page's wording, so a correct answer cannot be expected
  to contain it. Both stored answers name the right person and answer the
  question correctly: the core answer says he "will not clear it by email alone.
  He insists on a direct conversation first" and waits to hear the request
  "in person"; the medium answer says he will not clear it by email alone and
  "insists on hearing the requester explain the request in person". Neither
  contains the marker. This is the same shape as the four markers repaired in
  athenaeum#2918: a correct, responsive answer graded against page wording the
  question does not reach. It differs from those four in one respect. They graded
  a different fact from the one asked; this marker is the fact asked, in the
  page's words, which is why it sits next to the paraphrase bucket and why the
  paraphrase ruling in section 5 bears on it. The probe passed at oracle on
  2026-11-19 (3/3 for the class at both scales), but the stored answers from that
  run are not in the tree, so I cannot say what that answer said or how it
  contained the marker.
- **`subsidiary_onboarding_lesson`: a model failure, figure omitted.** The
  probe has two answer-bearing pages and needs both markers. The answer contains
  the first ("week three") and not the second ("single combined invoice"). The
  question asks whether the incident exposed a gap in how payment paperwork is
  handled. The billing split policy page, which the arm was handed, says an
  unconfirmed split defaults to a single combined invoice. The answer says only
  that the incident "exposed issues with how billing is handled for clients with
  multiple subsidiaries, specifically related to the cost-center billing split
  process". It never says what the gap is. The same probe passed at `medium` on
  this run, on the same pages.

## 5. What this document does not record

- No per-class comparison of Athenaeum against native memory, on cost or on
  relevance. The rows for it exist in the committed render and are void.
- No cutoff scale and no §7 condition readings.
- No side-by-side with the real-session numbers.
- No go or no-go. The ruling is the operator's, and the issue's own criterion
  asks for the control to pass before a ruling is recorded.

Three rulings are open, and they are the operator's:

- **(a) The `spend_approver_named` marker.** Whether to repair it, as the
  four markers were in athenaeum#2918, so that it names the fact the question
  asks for in words a correct answer can reach.
- **(b) How the control treats a single model omission when delivery is
  confirmed.** The strict 1.000 floor has now voided two consecutive paid runs,
  at about USD 32.6 derived between them. The second run failed on three cells
  that the first run passed, none of them a retrieval miss. The choice is
  between keeping the strict floor, and some other rule for a missed answer in a
  cell whose pages were handed over.
- **(c) The paraphrase ruling left open in the regrade note.** Whether a close
  paraphrase of the planted phrase should be accepted. Accepting it changes what
  every arm is graded on. It bears on the report-only classes that still read
  below 1.000 here, and on the boundary reading of `spend_approver_named` above.

What a valid result would need is a grid whose oracle control passes under
whichever rule those rulings settle on, on a corpus and grader that are fixed for
the re-run. Settling them is out of scope for athenaeum#2805.
