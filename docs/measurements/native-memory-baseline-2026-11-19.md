<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native-memory baseline — re-run for the kill criterion, 2026-11-19: the oracle control failed, the decision rows are void

Issue athenaeum#2805. `docs/use-cases.md` §1 names the comparison against native
memory as the kill criterion, and `docs/design/native-memory-baseline.md` §7 is
its decision rule. athenaeum#2805 asks for a fresh grid on the current
`develop`, with the oracle positive control passing **before** any decision row
is read.

**It did not pass. Every decision row of this run is void and none is used as a
verdict here.** This document records the control, the provenance, and what the
failing oracle cells show, and then stops. It states no per-class comparison
against native memory, no cost ratio, no cutoff scale, and no go or no-go. It
does not place the grid beside the real-session numbers in
[`recall-metrics-real-sessions-2026-11-15.md`](recall-metrics-real-sessions-2026-11-15.md),
because there is no valid grid result to place them beside.

The full rendered report from the run is recorded verbatim beside this file:
[`north-star-grid-2026-11-19-run-44438673846.md`](north-star-grid-2026-11-19-run-44438673846.md).
That artifact expires from GitHub Actions 30 days after the run; the in-tree
copy does not. The rendered report contains a "Decision" block and per-class
tables. They are void for the reason in section 3 and are committed only so the
evidence outlives the artifact. Recording the render is not reading its rows.

athenaeum#2805 is satisfied by producing a dated report. This one reports that
the control failed, which is a result, not a gate.

## 1. Provenance

| | |
| --- | --- |
| Workflow run | [44438673846](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/44438673846) |
| Dispatched | 2026-11-19 01:40 UTC, `workflow_dispatch` on `develop`; the north-star job finished 02:46 UTC |
| `develop` SHA at dispatch | `8d047902794454407bc64e361df5cdbd928b9bb8` |
| athenaeum version stamped in the report | 0.26.10 |
| Inputs | `north_star=true`, `north_star_scale=full`, `north_star_search_backend=vector`, `north_star_max_spend=25.00`, `north_star_workers=4`, `north_star_phase2=true`, `north_star_phase2_scales=medium`, `north_star_phase2_systems=athenaeum,native` (corpus scales left blank, so the workflow's own vector default `core,medium` applied) |
| Job conclusions | North-star grid (API mode) `success`, Live-API eval suite `success`, MiniLM-dependent suite `success`, Calibration sweep `skipped` |
| Mode | API (Anthropic Messages tool-use loop), model `claude-haiku-4-5-20251001`; Phase 2 native prompt fidelity `reconstructed` |
| Planned cells | 1026 (`north-star-store-vector.jsonl.planned.json`) |
| Total rollout rows | 1026, the full planned grid; no torn rows and no duplicate rows (`load_rollout_rows_and_diagnostics`, run offline on the downloaded store) |
| Harness failures | 24, all `turn_cap`: `native_grep` core 2, `native_grep` medium 11, `native_index` medium 2, `pull` core 2, `pull` medium 2, `push_breadcrumb_pull` core 1, `push_breadcrumb_pull` medium 1, `push_sections_pull` core 1, `push_sections_pull` medium 2 |
| Pre-flight price | `scale=full cells=1026 estimated=$8.7723 model=claude-haiku-4-5-20251001 phase2_estimated=$1.9440 preflight_estimated=$0.0770`, which sums to $10.7933 against the `$25.00` ceiling |
| Search backend | vector, hybrid on, relevance floors off |
| Grader revision | `athenaeum#1935` |
| `corpus_digest[core]` / `[medium]` | `3c5ea876688b089e` / `42e6ce0dfdc53e02` |
| Rendered report sha256 | `9bdbb0f9a04b5201cb4285f663fe2f1973231c300b9abfbd69ef9f44678d63c3` (268355 bytes, as uploaded by the run) |

I re-rendered the report offline from the downloaded store with the in-tree
`build_report` and `render_report`. The output matched the uploaded report line
for line, apart from the three header lines the offline call does not carry
(version, Phase 2 summary, pre-flight summary). That confirms the stored rows
and the report agree. It says nothing about whether the control passes, which
section 3 reads from the oracle rows alone.

### Spend against the USD 25 ceiling

The run records tokens, not a billed amount. The figures below are **derived
from recorded token counts at the repo's own rate table** (haiku-4-5 at USD 1 /
5 per million input / output tokens; cache writes at 1.25x and cache reads at
0.10x, the multipliers the harness bills cache activity at). They are not taken
from an invoice.

| Part | Tokens (in / out) | Derived USD |
| --- | --- | --- |
| Grid rollouts, 1026 rows | 8,678,279 / 325,720 | 10.31 |
| Pre-flight probe rows, 9 rows | 50,047 / 1,339 | 0.06 |
| Phase 2 native writer, `medium` | 4,296,860 / 168,468 | 5.14 |
| Phase 2 librarian filing, `medium` | 118,451 / 6,463 | 0.15 |
| **Grid job subtotal** | | **15.65** |
| Live-API eval suite job (`eval-summary.json` `token_usage`, per-model rates, cache terms included) | 137,000 / 23,768 plus 4,636 cache-write and 37,088 cache-read | 0.67 |
| **Whole run, derived** | | **16.32** |

Read this as a lower bound: Phase 2 filing retries and any librarian compile
calls that were not written to the Phase 2 store are not in it. The grid job
alone, and the whole run, are under the USD 25 ceiling either way. Two
differences from the plan are worth knowing: the Phase 2 native writer came to
USD 5.14 against a Phase 2 estimate of USD 1.94, and the grid job came to USD
15.65 against a total pre-flight estimate of USD 10.79.

## 2. What differs from 2026-09-19, and why nothing pools

The previous grid is described in
[`native-memory-baseline-2026-09-19.md`](native-memory-baseline-2026-09-19.md).

| | 2026-09-19 (run 35443944736) | 2026-11-19 (run 44438673846) |
| --- | --- | --- |
| `develop` SHA | `f7c00a0c4fbe80938a555b009a6cf124d252efb5` | `8d047902794454407bc64e361df5cdbd928b9bb8` |
| `corpus_digest[core]` | `edcd8dd3286d0135` | `3c5ea876688b089e` |
| `corpus_digest[medium]` | `d00353d3d4b95644` | `42e6ce0dfdc53e02` |
| Grader revision | `athenaeum#1843` | `athenaeum#1935` |
| Planned cells / rows | 768 / 768 | 1026 / 1026 |
| Harness failures | 26 | 24 |
| Arms | 8 | 9: adds `push_sections_pull` (athenaeum#2310) |
| `unprompted_push` probes per scale | 3 | 12 |

**The corpus digests do not match, and the grader revision differs.** Nothing in
this run pools with the 2026-09-19 grid or with any earlier one. The grid also
grew by 258 cells, so the group sizes differ. Everything below is a reading of
this run alone.

## 3. The oracle positive control

The control is `oracle`: the ground-truth pages handed to the model verbatim.
`docs/design/native-memory-baseline.md` §5 states that it must score at or near
1.0 on every non-abstention class, and that when it does not, the grading
contract is not satisfiable and the run's verdict rows must not be read. The
2026-09-18 report applied the strict form: every gating class read 1.000 at
oracle before any other row was read
([`native-memory-baseline-2026-09-18.md`](native-memory-baseline-2026-09-18.md)).
I applied the same rule here. The counts are the number of graded oracle cells
that were correct, over the number graded; they come from
`grade_correctness` applied to each oracle row of the downloaded store, and they
agree with the rendered report's correctness table.

| probe_class | enrolled in §7 | oracle core | oracle medium | result |
| --- | --- | --- | --- | --- |
| `abstention` | yes | 3/3 | 3/3 | clears |
| `disambiguation` | yes | 4/4 | 4/4 | clears |
| `distractor_robustness` | yes | 2/2 | 2/2 | clears |
| `follow_through` | yes | 6/6 | 6/6 | clears |
| `multi_hop` | yes | 3/3 | 3/3 | clears |
| `redundancy` | yes | **2/4** | **2/4** | **fails** |
| `single_hop` | yes | **5/8** | **6/8** | **fails** |
| `temporal` | yes | 6/6 | 6/6 | clears |
| `aggregation` | report-only | 1/3 | 2/3 | fails |
| `contradiction` | report-only | 3/3 | 3/3 | clears |
| `negative_knowledge` | report-only | 2/3 | 2/3 | fails |
| `unprompted_push` | report-only | 5/12 | 3/12 | fails |

**The control fails.** Two enrolled classes, `single_hop` and `redundancy`, fall
well below 1.0 at both scales, and three report-only classes do too. `single_hop`
is part of the relationship use case that condition 1 reads, so the failure
reaches the rows the decision turns on. Under the rule in the design doc and in
the 2026-09-18 report, the decision rows of this run are void, and this document does
not use them.

### The 2026-09-19 control, for the record

The 2026-09-19 report does not record an oracle check. Applying the same rule to
its rendered correctness table gives:

| probe_class | oracle core | oracle medium |
| --- | --- | --- |
| `single_hop` | 5/8 | 6/8 |
| `redundancy` | 0/4 | 2/4 |
| `disambiguation` | 4/4 | 3/4 |
| `multi_hop` | 3/3 | 2/3 |
| `distractor_robustness` | 1/2 | 1/2 |

Under the same rule, the 2026-09-19 control would not have passed either. The
operator's GO ruling on athenaeum#1736 rested on that grid. This document does
not say what follows from that for the earlier ruling; it records the fact so the
ruling can be weighed with it in view.

## 4. What the failing oracle cells show

Every failing oracle cell is a `marker_miss_with_delivery` cell. By that
diagnostic's own definition (athenaeum#1842), the answer-bearing page **was
delivered** to the arm and the planted answer marker was **absent** from the
answer. So each failure is an answer that did not contain the planted phrase,
not a retrieval miss. In all 30 failing oracle cells across both scales
(single_hop 5, redundancy 4, aggregation 3, negative_knowledge 2,
unprompted_push 16, summing the two scales), `marker_miss_with_delivery` was
true.

| probe_class | probe_id | scales failed |
| --- | --- | --- |
| `single_hop` | `confidentiality_rule` | core |
| `single_hop` | `bramfield_retainer_renewal` | core, medium |
| `single_hop` | `triform_vendor_consolidation` | core, medium |
| `redundancy` | `keelbridge_programme_scope` | core, medium |
| `redundancy` | `mira_castellane_role` | core, medium |
| `aggregation` | `aggregation_delivery_roles` | core |
| `aggregation` | `aggregation_inhouse_tools` | core, medium |
| `negative_knowledge` | `vendor_migration_rerun_lesson` | core, medium |
| `unprompted_push` | `relay_sync_fix`, `invoiceref_rename`, `rate_limit_removal`, `cache_layer_merge`, `connection_pool_widen`, `export_flag_deletion`, `webhook_backoff_lower` | core, medium |
| `unprompted_push` | `tls_library_bump`, `migration_spot_check_cut` | medium |

The patterns below are observations for follow-on work. They are my reading of
the stored oracle answers and the in-tree probe definitions. I have not fixed
anything and I have not re-run anything.

- **The marker names a fact the question does not ask for.** `keelbridge_programme_scope`
  asks "what does the Keelbridge programme cover?" and its marker is a cost
  figure; the oracle answer lists the three things the programme covers and
  never mentions cost. `bramfield_retainer_renewal` asks for the renewal
  timeline and its marker is "monthly touchpoint"; the oracle answer states the
  renewal cycle. `triform_vendor_consolidation` asks why the firm consolidated
  and its marker is a contract clause the answer does not mention.
  `confidentiality_rule` asks whether client details can be shared and its marker
  is a person's name; the oracle answer says no and gives the policy. In each of
  these the oracle answer reads as responsive to the question that was asked.
- **A possible normalizer issue.** `mira_castellane_role` has the marker "since
  March 2019" and the oracle answer says "since **March 2019**", with the date
  in markdown bold. `_normalize_marker_for_match` in `tests/evals/corpus.py`
  lowercases and collapses whitespace and does nothing else, so the asterisks
  would break the substring match. This is a code read, not a confirmed cause;
  the cell has not been re-graded with the asterisks stripped.
- **Paraphrase of the planted phrase.** `vendor_migration_rerun_lesson` has the
  marker "did not checkpoint progress"; the oracle answer says there was "no
  checkpointing" and that the script "did not save progress checkpoints".
- **`unprompted_push` is the class with the lowest oracle score.** It grew from 3
  to 12 probes per scale in this run. In the 16 failing cells the stored oracle
  answers mostly decline or caution against the requested change and omit the
  planted phrase. I read only the first 350 characters of each answer, and a few
  answers go a different way (one says it does not know the procedure), so this
  is a pattern in the answers I read, not a finding about every cell.

## 5. What this document does not record

- No per-class comparison of Athenaeum against native memory, on cost or on
  relevance, and no read of the `follow_through` class that lost in September.
  The rows for it exist in the committed render and are void.
- No cutoff scale and no §7 condition readings.
- No side-by-side with the real-session numbers.
- No go or no-go. The ruling is the operator's, and the issue's own criterion
  asks for the control to pass before a ruling is recorded.

What a valid result would need is a grid whose oracle control passes on every
enrolled class at every scale, on a corpus and grader that are fixed for the
re-run. The failing probes and patterns are in section 4. Fixing them is
follow-on work and is out of scope for athenaeum#2805.
