<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
Rendered north-star grid report, Actions run 42174214012 (2026-11-06), the 480-cell second dispatch that bought exactly the cells run 42169666857 did not reach (issue athenaeum#2309). Recorded in-tree for the same 30-day artifact expiry. Byte-identical to the artifact's own render apart from this comment.
-->

# North-star report: free dimensions only (athenaeum#1523)

- generated: 2026-11-06T06:55:44Z
- athenaeum_version: 0.23.4
- git_sha: 1dbd9c7963c9
- search_backend: vector
- hybrid: on
- relevance_floor_vector: off
- relevance_floor_fts5: off
- preflight: ok provider=api probe=pto_allowance arm=push_breadcrumb_pull recall_called=true tool_calls=1
- corpus_digest[core]: 3c5ea876688b089e
- corpus_digest[medium]: 42e6ce0dfdc53e02
- grader_revision: athenaeum#1935
- model: claude-haiku-4-5-20251001
- total rollout rows: 480
- harness failures: 20
  - turn_cap[arm=native_grep, scale=medium]: 1
  - turn_cap[arm=native_index, scale=core]: 1
  - turn_cap[arm=native_index, scale=medium]: 2

No LLM judge is invoked anywhere on this path — every figure below is a deterministic computation over already-captured rollout transcripts. This is a **measurement, not a regression gate**; nothing here should ever fail a build.

## Decision (design doc §7, athenaeum#1734)

**Verdict arm:** `push_breadcrumb_pull` (ruling R1) -- the shipped configuration is the ONE Athenaeum arm every condition below reads. Other Athenaeum arms still appear in the per-dimension tables further down, but never in these verdicts -- no picking the most-correct arm for conditions 1-2 and the cheapest for condition 3.

Three conditions, evaluated per scale, all against the verdict arm above: **(1)** win the relationship use case (single_hop/multi_hop/disambiguation/temporal probes targeting person/company pages), one POOLED correctness rate over the whole subset, against the better native arm's own pooled rate on the same rows; **(2)** do not lose any other current use case, class by class, skipping (and naming) any class where either side has no gradable rows; **(3)** cost per correct answer within budget of the better native arm, per class, skipping (and naming) any class with no native cost data at all (ruling R5) -- `>2.0x fail`, `<=2.0x limit`, `<=1.0x target`, `<=0.5x aspirational`, or `native-zero` (a pass) when the better native arm scored zero correct answers while the verdict arm scored at least one.

report-only classes excluded: aggregation, contradiction, negative_knowledge, unprompted_push -- excluded from conditions 2 and 3 entirely (issue athenaeum#1776); promotion is an explicit operator ruling on athenaeum#1736's thread, recorded by editing `tests.evals.corpus.CONDITION_2_ENROLLED`, never a side effect of this report.

**"Better native arm"** means two different things across these conditions, both the harshest reading available to Athenaeum: for condition 1, the native arm with the HIGHER pooled correctness rate; for condition 3, the native arm with the CHEAPER DEFINED cost per correct (a native arm that scored zero correct answers has no defined cost and is never picked as "cheaper" by that alone -- see `native-zero`).

**Cutoff scale:** `none` -- no scale at or above `medium` was evaluated in this run (see the eligibility column below), so there is nothing to certify a cutoff against.

| scale | cutoff eligible | condition 1 | condition 2 | condition 3 | reading | all pass |
| --- | --- | --- | --- | --- | --- | --- |

## Arms in this report (athenaeum#1574)

**PUSH means breadcrumbs** — `push_breadcrumb` and `push_breadcrumb_pull` are the arms that match what `examples/claude-code/user-prompt-recall.sh` actually ships: at most three 200-character-clamped `name — description` bullets, assembled by running that hook itself, never reimplemented. `push_pages_upper_bound` is the ORIGINAL five-full-page PUSH arm, kept and renamed — read it as an explicit **upper bound** ("what if the model always got the whole page"), never as the shipped configuration.

| arm | delivery | reads as |
| --- | --- | --- |
| `none` | nothing | floor |
| `push_pages_upper_bound` | 5 full pages via `recall_search` | **upper bound**, NOT the shipped hook |
| `push_breadcrumb` | <=3 breadcrumbs, via the real shipped hook | **matches production PUSH** |
| `push_breadcrumb_pull` | breadcrumbs injected + `recall` tool available | matches production PUSH, agent may still PULL |
| `oracle` | ground-truth pages verbatim | ceiling |
| `pull` | nothing injected, `recall` tool available | agent-initiated only |
| `native_index` | Claude Code auto-memory: topic files + a full `MEMORY.md` index, loaded and truncated by Claude Code itself | native memory, WITH an index (honest past the documented cap) |
| `native_grep` | Claude Code auto-memory: topic files, no index at all | native memory, file search only |

**Issue athenaeum#1725:** `native_index`/`native_grep` read a Claude Code auto-memory store instead of an Athenaeum-compiled corpus (design lock: `docs/design/native-memory-baseline.md`) -- the read-path comparison the project's continuation decision turns on. Neither is scored against a `pushed_context`/pulled-recall basis the way the other arms are: there is no Athenaeum retrieval step to attribute utilization to.

## Mode per cell (athenaeum#1733)

Which execution path produced each rollout cell -- `api` (Anthropic Messages API tool-use loop, the primary path per `docs/design/native-memory-baseline.md` §4) or `cli` (`claude -p`, the fidelity spot-check). A direct render over `report.rows` itself, never folded into `GroupStats`/`compute_group_stats`.

| probe_id | arm | corpus_scale | replicate | mode | tag_followed |
| --- | --- | --- | --- | --- | --- |
| cache_layer_merge | native_grep | core | 0 | api | no |
| cache_layer_merge | native_grep | core | 1 | api | no |
| cache_layer_merge | native_grep | medium | 0 | api | no |
| cache_layer_merge | native_grep | medium | 1 | api | no |
| cache_layer_merge | native_index | core | 0 | api | yes |
| cache_layer_merge | native_index | core | 1 | api | no |
| cache_layer_merge | native_index | medium | 0 | api | yes |
| cache_layer_merge | native_index | medium | 1 | api | yes |
| cache_layer_merge | none | core | 0 | api | no |
| cache_layer_merge | none | core | 1 | api | no |
| cache_layer_merge | none | medium | 0 | api | no |
| cache_layer_merge | none | medium | 1 | api | no |
| cache_layer_merge | oracle | core | 0 | api | yes |
| cache_layer_merge | oracle | core | 1 | api | yes |
| cache_layer_merge | oracle | medium | 0 | api | yes |
| cache_layer_merge | oracle | medium | 1 | api | yes |
| cache_layer_merge | pull | core | 0 | api | yes |
| cache_layer_merge | pull | core | 1 | api | yes |
| cache_layer_merge | pull | medium | 0 | api | yes |
| cache_layer_merge | pull | medium | 1 | api | yes |
| cache_layer_merge | push_breadcrumb | core | 0 | api | no |
| cache_layer_merge | push_breadcrumb | core | 1 | api | no |
| cache_layer_merge | push_breadcrumb | medium | 0 | api | no |
| cache_layer_merge | push_breadcrumb | medium | 1 | api | no |
| cache_layer_merge | push_breadcrumb_pull | core | 0 | api | no |
| cache_layer_merge | push_breadcrumb_pull | core | 1 | api | yes |
| cache_layer_merge | push_breadcrumb_pull | medium | 0 | api | no |
| cache_layer_merge | push_breadcrumb_pull | medium | 1 | api | no |
| cache_layer_merge | push_pages_upper_bound | core | 0 | api | no |
| cache_layer_merge | push_pages_upper_bound | core | 1 | api | no |
| cache_layer_merge | push_pages_upper_bound | medium | 0 | api | no |
| cache_layer_merge | push_pages_upper_bound | medium | 1 | api | no |
| claims_logging_off | native_grep | core | 0 | api | no |
| claims_logging_off | native_grep | core | 1 | api | yes |
| claims_logging_off | native_grep | medium | 0 | api | no |
| claims_logging_off | native_grep | medium | 1 | api | yes |
| claims_logging_off | native_index | core | 0 | api | yes |
| claims_logging_off | native_index | core | 1 | api | yes |
| claims_logging_off | native_index | medium | 0 | api | yes |
| claims_logging_off | native_index | medium | 1 | api | yes |
| claims_logging_off | none | core | 0 | api | no |
| claims_logging_off | none | core | 1 | api | no |
| claims_logging_off | none | medium | 0 | api | no |
| claims_logging_off | none | medium | 1 | api | no |
| claims_logging_off | oracle | core | 0 | api | yes |
| claims_logging_off | oracle | core | 1 | api | yes |
| claims_logging_off | oracle | medium | 0 | api | yes |
| claims_logging_off | oracle | medium | 1 | api | yes |
| claims_logging_off | pull | core | 0 | api | yes |
| claims_logging_off | pull | core | 1 | api | no |
| claims_logging_off | pull | medium | 0 | api | yes |
| claims_logging_off | pull | medium | 1 | api | yes |
| claims_logging_off | push_breadcrumb | core | 0 | api | no |
| claims_logging_off | push_breadcrumb | core | 1 | api | no |
| claims_logging_off | push_breadcrumb | medium | 0 | api | no |
| claims_logging_off | push_breadcrumb | medium | 1 | api | no |
| claims_logging_off | push_breadcrumb_pull | core | 0 | api | no |
| claims_logging_off | push_breadcrumb_pull | core | 1 | api | yes |
| claims_logging_off | push_breadcrumb_pull | medium | 0 | api | yes |
| claims_logging_off | push_breadcrumb_pull | medium | 1 | api | no |
| claims_logging_off | push_pages_upper_bound | core | 0 | api | no |
| claims_logging_off | push_pages_upper_bound | core | 1 | api | no |
| claims_logging_off | push_pages_upper_bound | medium | 0 | api | no |
| claims_logging_off | push_pages_upper_bound | medium | 1 | api | no |
| connection_pool_widen | native_grep | core | 0 | api | no |
| connection_pool_widen | native_grep | core | 1 | api | no |
| connection_pool_widen | native_grep | medium | 0 | api | no |
| connection_pool_widen | native_grep | medium | 1 | api | no |
| connection_pool_widen | native_index | core | 0 | api | yes |
| connection_pool_widen | native_index | core | 1 | api | yes |
| connection_pool_widen | native_index | medium | 0 | api | yes |
| connection_pool_widen | native_index | medium | 1 | api | yes |
| connection_pool_widen | none | core | 0 | api | no |
| connection_pool_widen | none | core | 1 | api | no |
| connection_pool_widen | none | medium | 0 | api | no |
| connection_pool_widen | none | medium | 1 | api | no |
| connection_pool_widen | oracle | core | 0 | api | yes |
| connection_pool_widen | oracle | core | 1 | api | yes |
| connection_pool_widen | oracle | medium | 0 | api | yes |
| connection_pool_widen | oracle | medium | 1 | api | yes |
| connection_pool_widen | pull | core | 0 | api | no |
| connection_pool_widen | pull | core | 1 | api | yes |
| connection_pool_widen | pull | medium | 0 | api | no |
| connection_pool_widen | pull | medium | 1 | api | no |
| connection_pool_widen | push_breadcrumb | core | 0 | api | no |
| connection_pool_widen | push_breadcrumb | core | 1 | api | no |
| connection_pool_widen | push_breadcrumb | medium | 0 | api | no |
| connection_pool_widen | push_breadcrumb | medium | 1 | api | no |
| connection_pool_widen | push_breadcrumb_pull | core | 0 | api | no |
| connection_pool_widen | push_breadcrumb_pull | core | 1 | api | no |
| connection_pool_widen | push_breadcrumb_pull | medium | 0 | api | no |
| connection_pool_widen | push_breadcrumb_pull | medium | 1 | api | no |
| connection_pool_widen | push_pages_upper_bound | core | 0 | api | no |
| connection_pool_widen | push_pages_upper_bound | core | 1 | api | no |
| connection_pool_widen | push_pages_upper_bound | medium | 0 | api | no |
| connection_pool_widen | push_pages_upper_bound | medium | 1 | api | no |
| export_flag_deletion | native_grep | core | 0 | api | no |
| export_flag_deletion | native_grep | core | 1 | api | no |
| export_flag_deletion | native_grep | medium | 0 | api | no |
| export_flag_deletion | native_grep | medium | 1 | api | no |
| export_flag_deletion | native_index | core | 0 | api | yes |
| export_flag_deletion | native_index | core | 1 | api | no |
| export_flag_deletion | native_index | medium | 0 | api | no |
| export_flag_deletion | native_index | medium | 1 | api | yes |
| export_flag_deletion | none | core | 0 | api | no |
| export_flag_deletion | none | core | 1 | api | no |
| export_flag_deletion | none | medium | 0 | api | no |
| export_flag_deletion | none | medium | 1 | api | no |
| export_flag_deletion | oracle | core | 0 | api | yes |
| export_flag_deletion | oracle | core | 1 | api | yes |
| export_flag_deletion | oracle | medium | 0 | api | yes |
| export_flag_deletion | oracle | medium | 1 | api | yes |
| export_flag_deletion | pull | core | 0 | api | no |
| export_flag_deletion | pull | core | 1 | api | no |
| export_flag_deletion | pull | medium | 0 | api | yes |
| export_flag_deletion | pull | medium | 1 | api | yes |
| export_flag_deletion | push_breadcrumb | core | 0 | api | no |
| export_flag_deletion | push_breadcrumb | core | 1 | api | no |
| export_flag_deletion | push_breadcrumb | medium | 0 | api | no |
| export_flag_deletion | push_breadcrumb | medium | 1 | api | no |
| export_flag_deletion | push_breadcrumb_pull | core | 0 | api | no |
| export_flag_deletion | push_breadcrumb_pull | core | 1 | api | yes |
| export_flag_deletion | push_breadcrumb_pull | medium | 0 | api | no |
| export_flag_deletion | push_breadcrumb_pull | medium | 1 | api | yes |
| export_flag_deletion | push_pages_upper_bound | core | 0 | api | no |
| export_flag_deletion | push_pages_upper_bound | core | 1 | api | no |
| export_flag_deletion | push_pages_upper_bound | medium | 0 | api | no |
| export_flag_deletion | push_pages_upper_bound | medium | 1 | api | no |
| invoice_role_merge | native_grep | core | 0 | api | no |
| invoice_role_merge | native_grep | core | 1 | api | no |
| invoice_role_merge | native_grep | medium | 0 | api | no |
| invoice_role_merge | native_grep | medium | 1 | api | yes |
| invoice_role_merge | native_index | core | 0 | api | yes |
| invoice_role_merge | native_index | core | 1 | api | yes |
| invoice_role_merge | native_index | medium | 0 | api | yes |
| invoice_role_merge | native_index | medium | 1 | api | yes |
| invoice_role_merge | none | core | 0 | api | no |
| invoice_role_merge | none | core | 1 | api | no |
| invoice_role_merge | none | medium | 0 | api | no |
| invoice_role_merge | none | medium | 1 | api | no |
| invoice_role_merge | oracle | core | 0 | api | yes |
| invoice_role_merge | oracle | core | 1 | api | yes |
| invoice_role_merge | oracle | medium | 0 | api | yes |
| invoice_role_merge | oracle | medium | 1 | api | yes |
| invoice_role_merge | pull | core | 0 | api | no |
| invoice_role_merge | pull | core | 1 | api | yes |
| invoice_role_merge | pull | medium | 0 | api | yes |
| invoice_role_merge | pull | medium | 1 | api | yes |
| invoice_role_merge | push_breadcrumb | core | 0 | api | no |
| invoice_role_merge | push_breadcrumb | core | 1 | api | no |
| invoice_role_merge | push_breadcrumb | medium | 0 | api | no |
| invoice_role_merge | push_breadcrumb | medium | 1 | api | no |
| invoice_role_merge | push_breadcrumb_pull | core | 0 | api | yes |
| invoice_role_merge | push_breadcrumb_pull | core | 1 | api | yes |
| invoice_role_merge | push_breadcrumb_pull | medium | 0 | api | no |
| invoice_role_merge | push_breadcrumb_pull | medium | 1 | api | yes |
| invoice_role_merge | push_pages_upper_bound | core | 0 | api | no |
| invoice_role_merge | push_pages_upper_bound | core | 1 | api | no |
| invoice_role_merge | push_pages_upper_bound | medium | 0 | api | no |
| invoice_role_merge | push_pages_upper_bound | medium | 1 | api | no |
| invoiceref_rename | native_grep | core | 0 | api | no |
| invoiceref_rename | native_grep | core | 1 | api | no |
| invoiceref_rename | native_grep | medium | 0 | api | no |
| invoiceref_rename | native_grep | medium | 1 | api | no |
| invoiceref_rename | native_index | core | 0 | api | yes |
| invoiceref_rename | native_index | core | 1 | api | no |
| invoiceref_rename | native_index | medium | 0 | api | yes |
| invoiceref_rename | native_index | medium | 1 | api | yes |
| invoiceref_rename | none | core | 0 | api | no |
| invoiceref_rename | none | core | 1 | api | no |
| invoiceref_rename | none | medium | 0 | api | no |
| invoiceref_rename | none | medium | 1 | api | no |
| invoiceref_rename | oracle | core | 0 | api | yes |
| invoiceref_rename | oracle | core | 1 | api | yes |
| invoiceref_rename | oracle | medium | 0 | api | yes |
| invoiceref_rename | oracle | medium | 1 | api | yes |
| invoiceref_rename | pull | core | 0 | api | no |
| invoiceref_rename | pull | core | 1 | api | no |
| invoiceref_rename | pull | medium | 0 | api | no |
| invoiceref_rename | pull | medium | 1 | api | no |
| invoiceref_rename | push_breadcrumb | core | 0 | api | no |
| invoiceref_rename | push_breadcrumb | core | 1 | api | no |
| invoiceref_rename | push_breadcrumb | medium | 0 | api | no |
| invoiceref_rename | push_breadcrumb | medium | 1 | api | no |
| invoiceref_rename | push_breadcrumb_pull | core | 0 | api | no |
| invoiceref_rename | push_breadcrumb_pull | core | 1 | api | no |
| invoiceref_rename | push_breadcrumb_pull | medium | 0 | api | no |
| invoiceref_rename | push_breadcrumb_pull | medium | 1 | api | no |
| invoiceref_rename | push_pages_upper_bound | core | 0 | api | no |
| invoiceref_rename | push_pages_upper_bound | core | 1 | api | no |
| invoiceref_rename | push_pages_upper_bound | medium | 0 | api | no |
| invoiceref_rename | push_pages_upper_bound | medium | 1 | api | no |
| junior_analyst_ramp_lesson | native_grep | core | 0 | api | yes |
| junior_analyst_ramp_lesson | native_grep | core | 1 | api | yes |
| junior_analyst_ramp_lesson | native_grep | medium | 0 | api | no |
| junior_analyst_ramp_lesson | native_grep | medium | 1 | api | yes |
| junior_analyst_ramp_lesson | native_index | core | 0 | api | yes |
| junior_analyst_ramp_lesson | native_index | core | 1 | api | yes |
| junior_analyst_ramp_lesson | native_index | medium | 0 | api | yes |
| junior_analyst_ramp_lesson | native_index | medium | 1 | api | yes |
| junior_analyst_ramp_lesson | none | core | 0 | api | no |
| junior_analyst_ramp_lesson | none | core | 1 | api | no |
| junior_analyst_ramp_lesson | none | medium | 0 | api | no |
| junior_analyst_ramp_lesson | none | medium | 1 | api | no |
| junior_analyst_ramp_lesson | oracle | core | 0 | api | yes |
| junior_analyst_ramp_lesson | oracle | core | 1 | api | yes |
| junior_analyst_ramp_lesson | oracle | medium | 0 | api | yes |
| junior_analyst_ramp_lesson | oracle | medium | 1 | api | yes |
| junior_analyst_ramp_lesson | pull | core | 0 | api | yes |
| junior_analyst_ramp_lesson | pull | core | 1 | api | yes |
| junior_analyst_ramp_lesson | pull | medium | 0 | api | yes |
| junior_analyst_ramp_lesson | pull | medium | 1 | api | yes |
| junior_analyst_ramp_lesson | push_breadcrumb | core | 0 | api | no |
| junior_analyst_ramp_lesson | push_breadcrumb | core | 1 | api | no |
| junior_analyst_ramp_lesson | push_breadcrumb | medium | 0 | api | no |
| junior_analyst_ramp_lesson | push_breadcrumb | medium | 1 | api | no |
| junior_analyst_ramp_lesson | push_breadcrumb_pull | core | 0 | api | yes |
| junior_analyst_ramp_lesson | push_breadcrumb_pull | core | 1 | api | yes |
| junior_analyst_ramp_lesson | push_breadcrumb_pull | medium | 0 | api | yes |
| junior_analyst_ramp_lesson | push_breadcrumb_pull | medium | 1 | api | yes |
| junior_analyst_ramp_lesson | push_pages_upper_bound | core | 0 | api | yes |
| junior_analyst_ramp_lesson | push_pages_upper_bound | core | 1 | api | yes |
| junior_analyst_ramp_lesson | push_pages_upper_bound | medium | 0 | api | yes |
| junior_analyst_ramp_lesson | push_pages_upper_bound | medium | 1 | api | yes |
| migration_spot_check_cut | native_grep | core | 0 | api | no |
| migration_spot_check_cut | native_grep | core | 1 | api | no |
| migration_spot_check_cut | native_grep | medium | 0 | api | no |
| migration_spot_check_cut | native_grep | medium | 1 | api | no |
| migration_spot_check_cut | native_index | core | 0 | api | yes |
| migration_spot_check_cut | native_index | core | 1 | api | no |
| migration_spot_check_cut | native_index | medium | 0 | api | yes |
| migration_spot_check_cut | native_index | medium | 1 | api | yes |
| migration_spot_check_cut | none | core | 0 | api | no |
| migration_spot_check_cut | none | core | 1 | api | no |
| migration_spot_check_cut | none | medium | 0 | api | no |
| migration_spot_check_cut | none | medium | 1 | api | no |
| migration_spot_check_cut | oracle | core | 0 | api | yes |
| migration_spot_check_cut | oracle | core | 1 | api | yes |
| migration_spot_check_cut | oracle | medium | 0 | api | yes |
| migration_spot_check_cut | oracle | medium | 1 | api | yes |
| migration_spot_check_cut | pull | core | 0 | api | no |
| migration_spot_check_cut | pull | core | 1 | api | no |
| migration_spot_check_cut | pull | medium | 0 | api | no |
| migration_spot_check_cut | pull | medium | 1 | api | no |
| migration_spot_check_cut | push_breadcrumb | core | 0 | api | no |
| migration_spot_check_cut | push_breadcrumb | core | 1 | api | no |
| migration_spot_check_cut | push_breadcrumb | medium | 0 | api | no |
| migration_spot_check_cut | push_breadcrumb | medium | 1 | api | no |
| migration_spot_check_cut | push_breadcrumb_pull | core | 0 | api | no |
| migration_spot_check_cut | push_breadcrumb_pull | core | 1 | api | no |
| migration_spot_check_cut | push_breadcrumb_pull | medium | 0 | api | yes |
| migration_spot_check_cut | push_breadcrumb_pull | medium | 1 | api | yes |
| migration_spot_check_cut | push_pages_upper_bound | core | 0 | api | no |
| migration_spot_check_cut | push_pages_upper_bound | core | 1 | api | no |
| migration_spot_check_cut | push_pages_upper_bound | medium | 0 | api | no |
| migration_spot_check_cut | push_pages_upper_bound | medium | 1 | api | no |
| nightly_job_drop | native_grep | core | 0 | api | no |
| nightly_job_drop | native_grep | core | 1 | api | no |
| nightly_job_drop | native_grep | medium | 0 | api | no |
| nightly_job_drop | native_grep | medium | 1 | api | no |
| nightly_job_drop | native_index | core | 0 | api | yes |
| nightly_job_drop | native_index | core | 1 | api | yes |
| nightly_job_drop | native_index | medium | 0 | api | no |
| nightly_job_drop | native_index | medium | 1 | api | yes |
| nightly_job_drop | none | core | 0 | api | no |
| nightly_job_drop | none | core | 1 | api | no |
| nightly_job_drop | none | medium | 0 | api | no |
| nightly_job_drop | none | medium | 1 | api | no |
| nightly_job_drop | oracle | core | 0 | api | yes |
| nightly_job_drop | oracle | core | 1 | api | yes |
| nightly_job_drop | oracle | medium | 0 | api | yes |
| nightly_job_drop | oracle | medium | 1 | api | yes |
| nightly_job_drop | pull | core | 0 | api | no |
| nightly_job_drop | pull | core | 1 | api | no |
| nightly_job_drop | pull | medium | 0 | api | no |
| nightly_job_drop | pull | medium | 1 | api | no |
| nightly_job_drop | push_breadcrumb | core | 0 | api | no |
| nightly_job_drop | push_breadcrumb | core | 1 | api | no |
| nightly_job_drop | push_breadcrumb | medium | 0 | api | no |
| nightly_job_drop | push_breadcrumb | medium | 1 | api | no |
| nightly_job_drop | push_breadcrumb_pull | core | 0 | api | no |
| nightly_job_drop | push_breadcrumb_pull | core | 1 | api | no |
| nightly_job_drop | push_breadcrumb_pull | medium | 0 | api | no |
| nightly_job_drop | push_breadcrumb_pull | medium | 1 | api | no |
| nightly_job_drop | push_pages_upper_bound | core | 0 | api | no |
| nightly_job_drop | push_pages_upper_bound | core | 1 | api | no |
| nightly_job_drop | push_pages_upper_bound | medium | 0 | api | no |
| nightly_job_drop | push_pages_upper_bound | medium | 1 | api | no |
| rate_limit_removal | native_grep | core | 0 | api | yes |
| rate_limit_removal | native_grep | core | 1 | api | yes |
| rate_limit_removal | native_grep | medium | 0 | api | yes |
| rate_limit_removal | native_grep | medium | 1 | api | yes |
| rate_limit_removal | native_index | core | 0 | api | yes |
| rate_limit_removal | native_index | core | 1 | api | yes |
| rate_limit_removal | native_index | medium | 0 | api | yes |
| rate_limit_removal | native_index | medium | 1 | api | yes |
| rate_limit_removal | none | core | 0 | api | no |
| rate_limit_removal | none | core | 1 | api | no |
| rate_limit_removal | none | medium | 0 | api | no |
| rate_limit_removal | none | medium | 1 | api | no |
| rate_limit_removal | oracle | core | 0 | api | yes |
| rate_limit_removal | oracle | core | 1 | api | yes |
| rate_limit_removal | oracle | medium | 0 | api | yes |
| rate_limit_removal | oracle | medium | 1 | api | yes |
| rate_limit_removal | pull | core | 0 | api | yes |
| rate_limit_removal | pull | core | 1 | api | no |
| rate_limit_removal | pull | medium | 0 | api | yes |
| rate_limit_removal | pull | medium | 1 | api | no |
| rate_limit_removal | push_breadcrumb | core | 0 | api | no |
| rate_limit_removal | push_breadcrumb | core | 1 | api | no |
| rate_limit_removal | push_breadcrumb | medium | 0 | api | no |
| rate_limit_removal | push_breadcrumb | medium | 1 | api | no |
| rate_limit_removal | push_breadcrumb_pull | core | 0 | api | yes |
| rate_limit_removal | push_breadcrumb_pull | core | 1 | api | yes |
| rate_limit_removal | push_breadcrumb_pull | medium | 0 | api | yes |
| rate_limit_removal | push_breadcrumb_pull | medium | 1 | api | no |
| rate_limit_removal | push_pages_upper_bound | core | 0 | api | no |
| rate_limit_removal | push_pages_upper_bound | core | 1 | api | no |
| rate_limit_removal | push_pages_upper_bound | medium | 0 | api | no |
| rate_limit_removal | push_pages_upper_bound | medium | 1 | api | no |
| regional_office_onboarding_lesson | native_grep | core | 0 | api | yes |
| regional_office_onboarding_lesson | native_grep | core | 1 | api | yes |
| regional_office_onboarding_lesson | native_grep | medium | 0 | api | yes |
| regional_office_onboarding_lesson | native_grep | medium | 1 | api | yes |
| regional_office_onboarding_lesson | native_index | core | 0 | api | yes |
| regional_office_onboarding_lesson | native_index | core | 1 | api | yes |
| regional_office_onboarding_lesson | native_index | medium | 0 | api | yes |
| regional_office_onboarding_lesson | native_index | medium | 1 | api | yes |
| regional_office_onboarding_lesson | none | core | 0 | api | no |
| regional_office_onboarding_lesson | none | core | 1 | api | no |
| regional_office_onboarding_lesson | none | medium | 0 | api | no |
| regional_office_onboarding_lesson | none | medium | 1 | api | no |
| regional_office_onboarding_lesson | oracle | core | 0 | api | yes |
| regional_office_onboarding_lesson | oracle | core | 1 | api | yes |
| regional_office_onboarding_lesson | oracle | medium | 0 | api | yes |
| regional_office_onboarding_lesson | oracle | medium | 1 | api | yes |
| regional_office_onboarding_lesson | pull | core | 0 | api | yes |
| regional_office_onboarding_lesson | pull | core | 1 | api | yes |
| regional_office_onboarding_lesson | pull | medium | 0 | api | yes |
| regional_office_onboarding_lesson | pull | medium | 1 | api | yes |
| regional_office_onboarding_lesson | push_breadcrumb | core | 0 | api | no |
| regional_office_onboarding_lesson | push_breadcrumb | core | 1 | api | no |
| regional_office_onboarding_lesson | push_breadcrumb | medium | 0 | api | no |
| regional_office_onboarding_lesson | push_breadcrumb | medium | 1 | api | no |
| regional_office_onboarding_lesson | push_breadcrumb_pull | core | 0 | api | yes |
| regional_office_onboarding_lesson | push_breadcrumb_pull | core | 1 | api | yes |
| regional_office_onboarding_lesson | push_breadcrumb_pull | medium | 0 | api | yes |
| regional_office_onboarding_lesson | push_breadcrumb_pull | medium | 1 | api | yes |
| regional_office_onboarding_lesson | push_pages_upper_bound | core | 0 | api | yes |
| regional_office_onboarding_lesson | push_pages_upper_bound | core | 1 | api | yes |
| regional_office_onboarding_lesson | push_pages_upper_bound | medium | 0 | api | yes |
| regional_office_onboarding_lesson | push_pages_upper_bound | medium | 1 | api | yes |
| relay_sync_fix | native_grep | core | 0 | api | no |
| relay_sync_fix | native_grep | core | 1 | api | no |
| relay_sync_fix | native_grep | medium | 0 | api | no |
| relay_sync_fix | native_grep | medium | 1 | api | no |
| relay_sync_fix | native_index | core | 0 | api | yes |
| relay_sync_fix | native_index | core | 1 | api | yes |
| relay_sync_fix | native_index | medium | 0 | api | no |
| relay_sync_fix | native_index | medium | 1 | api | no |
| relay_sync_fix | none | core | 0 | api | no |
| relay_sync_fix | none | core | 1 | api | no |
| relay_sync_fix | none | medium | 0 | api | no |
| relay_sync_fix | none | medium | 1 | api | no |
| relay_sync_fix | oracle | core | 0 | api | yes |
| relay_sync_fix | oracle | core | 1 | api | yes |
| relay_sync_fix | oracle | medium | 0 | api | yes |
| relay_sync_fix | oracle | medium | 1 | api | yes |
| relay_sync_fix | pull | core | 0 | api | no |
| relay_sync_fix | pull | core | 1 | api | no |
| relay_sync_fix | pull | medium | 0 | api | no |
| relay_sync_fix | pull | medium | 1 | api | no |
| relay_sync_fix | push_breadcrumb | core | 0 | api | no |
| relay_sync_fix | push_breadcrumb | core | 1 | api | no |
| relay_sync_fix | push_breadcrumb | medium | 0 | api | no |
| relay_sync_fix | push_breadcrumb | medium | 1 | api | no |
| relay_sync_fix | push_breadcrumb_pull | core | 0 | api | no |
| relay_sync_fix | push_breadcrumb_pull | core | 1 | api | no |
| relay_sync_fix | push_breadcrumb_pull | medium | 0 | api | no |
| relay_sync_fix | push_breadcrumb_pull | medium | 1 | api | no |
| relay_sync_fix | push_pages_upper_bound | core | 0 | api | no |
| relay_sync_fix | push_pages_upper_bound | core | 1 | api | no |
| relay_sync_fix | push_pages_upper_bound | medium | 0 | api | no |
| relay_sync_fix | push_pages_upper_bound | medium | 1 | api | no |
| tls_library_bump | native_grep | core | 0 | api | no |
| tls_library_bump | native_grep | core | 1 | api | no |
| tls_library_bump | native_grep | medium | 0 | api | no |
| tls_library_bump | native_grep | medium | 1 | api | no |
| tls_library_bump | native_index | core | 0 | api | no |
| tls_library_bump | native_index | core | 1 | api | yes |
| tls_library_bump | native_index | medium | 0 | api | no |
| tls_library_bump | native_index | medium | 1 | api | no |
| tls_library_bump | none | core | 0 | api | no |
| tls_library_bump | none | core | 1 | api | no |
| tls_library_bump | none | medium | 0 | api | no |
| tls_library_bump | none | medium | 1 | api | no |
| tls_library_bump | oracle | core | 0 | api | yes |
| tls_library_bump | oracle | core | 1 | api | yes |
| tls_library_bump | oracle | medium | 0 | api | yes |
| tls_library_bump | oracle | medium | 1 | api | yes |
| tls_library_bump | pull | core | 0 | api | no |
| tls_library_bump | pull | core | 1 | api | no |
| tls_library_bump | pull | medium | 0 | api | yes |
| tls_library_bump | pull | medium | 1 | api | no |
| tls_library_bump | push_breadcrumb | core | 0 | api | no |
| tls_library_bump | push_breadcrumb | core | 1 | api | no |
| tls_library_bump | push_breadcrumb | medium | 0 | api | no |
| tls_library_bump | push_breadcrumb | medium | 1 | api | no |
| tls_library_bump | push_breadcrumb_pull | core | 0 | api | yes |
| tls_library_bump | push_breadcrumb_pull | core | 1 | api | yes |
| tls_library_bump | push_breadcrumb_pull | medium | 0 | api | yes |
| tls_library_bump | push_breadcrumb_pull | medium | 1 | api | yes |
| tls_library_bump | push_pages_upper_bound | core | 0 | api | no |
| tls_library_bump | push_pages_upper_bound | core | 1 | api | no |
| tls_library_bump | push_pages_upper_bound | medium | 0 | api | no |
| tls_library_bump | push_pages_upper_bound | medium | 1 | api | no |
| vendor_migration_rerun_lesson | native_grep | core | 0 | api | yes |
| vendor_migration_rerun_lesson | native_grep | core | 1 | api | yes |
| vendor_migration_rerun_lesson | native_grep | medium | 0 | api | yes |
| vendor_migration_rerun_lesson | native_grep | medium | 1 | api | yes |
| vendor_migration_rerun_lesson | native_index | core | 0 | api | yes |
| vendor_migration_rerun_lesson | native_index | core | 1 | api | yes |
| vendor_migration_rerun_lesson | native_index | medium | 0 | api | yes |
| vendor_migration_rerun_lesson | native_index | medium | 1 | api | yes |
| vendor_migration_rerun_lesson | none | core | 0 | api | no |
| vendor_migration_rerun_lesson | none | core | 1 | api | no |
| vendor_migration_rerun_lesson | none | medium | 0 | api | no |
| vendor_migration_rerun_lesson | none | medium | 1 | api | no |
| vendor_migration_rerun_lesson | oracle | core | 0 | api | yes |
| vendor_migration_rerun_lesson | oracle | core | 1 | api | yes |
| vendor_migration_rerun_lesson | oracle | medium | 0 | api | yes |
| vendor_migration_rerun_lesson | oracle | medium | 1 | api | yes |
| vendor_migration_rerun_lesson | pull | core | 0 | api | yes |
| vendor_migration_rerun_lesson | pull | core | 1 | api | yes |
| vendor_migration_rerun_lesson | pull | medium | 0 | api | yes |
| vendor_migration_rerun_lesson | pull | medium | 1 | api | yes |
| vendor_migration_rerun_lesson | push_breadcrumb | core | 0 | api | no |
| vendor_migration_rerun_lesson | push_breadcrumb | core | 1 | api | no |
| vendor_migration_rerun_lesson | push_breadcrumb | medium | 0 | api | no |
| vendor_migration_rerun_lesson | push_breadcrumb | medium | 1 | api | no |
| vendor_migration_rerun_lesson | push_breadcrumb_pull | core | 0 | api | yes |
| vendor_migration_rerun_lesson | push_breadcrumb_pull | core | 1 | api | yes |
| vendor_migration_rerun_lesson | push_breadcrumb_pull | medium | 0 | api | yes |
| vendor_migration_rerun_lesson | push_breadcrumb_pull | medium | 1 | api | yes |
| vendor_migration_rerun_lesson | push_pages_upper_bound | core | 0 | api | yes |
| vendor_migration_rerun_lesson | push_pages_upper_bound | core | 1 | api | yes |
| vendor_migration_rerun_lesson | push_pages_upper_bound | medium | 0 | api | yes |
| vendor_migration_rerun_lesson | push_pages_upper_bound | medium | 1 | api | yes |
| webhook_backoff_lower | native_grep | core | 0 | api | no |
| webhook_backoff_lower | native_grep | core | 1 | api | no |
| webhook_backoff_lower | native_grep | medium | 0 | api | no |
| webhook_backoff_lower | native_grep | medium | 1 | api | no |
| webhook_backoff_lower | native_index | core | 0 | api | yes |
| webhook_backoff_lower | native_index | core | 1 | api | yes |
| webhook_backoff_lower | native_index | medium | 0 | api | no |
| webhook_backoff_lower | native_index | medium | 1 | api | yes |
| webhook_backoff_lower | none | core | 0 | api | no |
| webhook_backoff_lower | none | core | 1 | api | no |
| webhook_backoff_lower | none | medium | 0 | api | no |
| webhook_backoff_lower | none | medium | 1 | api | no |
| webhook_backoff_lower | oracle | core | 0 | api | yes |
| webhook_backoff_lower | oracle | core | 1 | api | yes |
| webhook_backoff_lower | oracle | medium | 0 | api | yes |
| webhook_backoff_lower | oracle | medium | 1 | api | yes |
| webhook_backoff_lower | pull | core | 0 | api | no |
| webhook_backoff_lower | pull | core | 1 | api | yes |
| webhook_backoff_lower | pull | medium | 0 | api | no |
| webhook_backoff_lower | pull | medium | 1 | api | yes |
| webhook_backoff_lower | push_breadcrumb | core | 0 | api | no |
| webhook_backoff_lower | push_breadcrumb | core | 1 | api | no |
| webhook_backoff_lower | push_breadcrumb | medium | 0 | api | no |
| webhook_backoff_lower | push_breadcrumb | medium | 1 | api | no |
| webhook_backoff_lower | push_breadcrumb_pull | core | 0 | api | no |
| webhook_backoff_lower | push_breadcrumb_pull | core | 1 | api | yes |
| webhook_backoff_lower | push_breadcrumb_pull | medium | 0 | api | yes |
| webhook_backoff_lower | push_breadcrumb_pull | medium | 1 | api | yes |
| webhook_backoff_lower | push_pages_upper_bound | core | 0 | api | no |
| webhook_backoff_lower | push_pages_upper_bound | core | 1 | api | no |
| webhook_backoff_lower | push_pages_upper_bound | medium | 0 | api | no |
| webhook_backoff_lower | push_pages_upper_bound | medium | 1 | api | no |

## PULL no-call rate (first-class result)

`no_call_rate = count(recall_called=False) / count(PULL rollouts)`, per (probe_class, corpus_scale). The thesis under test predicts a non-trivial rate; a rate near 0.0 across every group means the free data do NOT evidence it.

| probe_class | corpus_scale | n | no_call_rate |
| --- | --- | --- | --- |
| negative_knowledge | core | 6 | 0.000 |
| negative_knowledge | medium | 6 | 0.000 |
| unprompted_push | core | 24 | 0.125 |
| unprompted_push | medium | 24 | 0.125 |

## Cost per correct answer (athenaeum#1734)

`cost_per_correct = (read_input_tokens + read_output_tokens + write_tokens_amortized) / correct_n` -- read cost only in Phase 1. Phase 2 adds write cost amortised over the full probe set at that scale (design doc §6); for a non-native arm `write_tokens_amortized` is the native writer's own amortised share PLUS the librarian's filing share (athenaeum#1870, ruling athenaeum#1830), never the filing share alone -- `write_native_share_amortized` prints that native portion separately so neither component is hidden, and the raw (unamortised) sum is printed alongside. `n/a` in the native-share columns means either no write data at all, or (for a native arm) that the whole combined figure already is native, so there is no separate share to print; `not-captured` means an Athenaeum arm's native share specifically was never captured, so the figure shown is filing-only -- see the design doc. `undefined` (never `inf`/`0`) means zero correct answers in the cell -- there is no denominator.

`cache_weighted_tokens` (athenaeum#2305) folds cache activity into `cost_per_correct`'s numerator at the same multipliers athenaeum#239 already bills real cache activity at (a cache write = 1.25x a plain input token, a cache read = 0.10x) -- counting a PUSH/ORACLE arm's injected context on EVERY later turn it stays resident in the model's context, which a plain input+output-tokens view would otherwise price at zero past the first turn. `n/a` means no row in this group ever recorded cache usage (a pre-athenaeum#2305 row), never a measured zero.

`injected_bytes` (athenaeum#2305) is the summed UTF-8 byte length of the content actually injected for this cell's rows (PUSH/ORACLE arms only) -- reported alongside cost, never folded into `cost_per_correct`'s arithmetic. `n/a` means no row in this group recorded it.

| probe_class | corpus_scale | arm | n | correct_n | read_tokens | cache_weighted_tokens | cost_per_correct | injected_bytes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | 6 | 32000 | 0.0 | 5333.3 | n/a |
| negative_knowledge | core | native_index | 6 | 4 | 147387 | 0.0 | 36846.8 | n/a |
| negative_knowledge | core | none | 6 | 0 | 2209 | 0.0 | undefined | n/a |
| negative_knowledge | core | oracle | 6 | 5 | 3157 | 0.0 | 631.4 | 3026 |
| negative_knowledge | core | pull | 6 | 5 | 64127 | 0.0 | 12825.4 | n/a |
| negative_knowledge | core | push_breadcrumb | 6 | 0 | 3525 | 0.0 | undefined | 3844 |
| negative_knowledge | core | push_breadcrumb_pull | 6 | 4 | 78441 | 0.0 | 19610.2 | 3844 |
| negative_knowledge | core | push_pages_upper_bound | 6 | 6 | 7344 | 0.0 | 1224.0 | 17146 |
| negative_knowledge | medium | native_grep | 5 | 4 | 36834 | 0.0 | 9208.5 | n/a |
| negative_knowledge | medium | native_index | 6 | 5 | 169153 | 0.0 | 33830.6 | n/a |
| negative_knowledge | medium | none | 6 | 0 | 2205 | 0.0 | undefined | n/a |
| negative_knowledge | medium | oracle | 6 | 5 | 3215 | 0.0 | 643.0 | 3026 |
| negative_knowledge | medium | pull | 6 | 5 | 65896 | 0.0 | 13179.2 | n/a |
| negative_knowledge | medium | push_breadcrumb | 2 | 0 | 1269 | 0.0 | undefined | 1338 |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | 2 | 26269 | 0.0 | 13134.5 | 1338 |
| negative_knowledge | medium | push_pages_upper_bound | 6 | 6 | 7246 | 0.0 | 1207.7 | 17026 |
| unprompted_push | core | native_grep | 24 | 0 | 139251 | 0.0 | undefined | n/a |
| unprompted_push | core | native_index | 23 | 6 | 699241 | 0.0 | 116540.2 | n/a |
| unprompted_push | core | none | 24 | 0 | 9032 | 0.0 | undefined | n/a |
| unprompted_push | core | oracle | 24 | 9 | 20847 | 0.0 | 2316.3 | 37340 |
| unprompted_push | core | pull | 24 | 0 | 298362 | 0.0 | undefined | n/a |
| unprompted_push | core | push_breadcrumb | 24 | 0 | 15505 | 0.0 | undefined | 16456 |
| unprompted_push | core | push_breadcrumb_pull | 24 | 3 | 396773 | 0.0 | 132257.7 | 16456 |
| unprompted_push | core | push_pages_upper_bound | 24 | 0 | 34470 | 0.0 | undefined | 81406 |
| unprompted_push | medium | native_grep | 24 | 1 | 170348 | 0.0 | 170348.0 | n/a |
| unprompted_push | medium | native_index | 22 | 4 | 663521 | 0.0 | 165880.2 | n/a |
| unprompted_push | medium | none | 24 | 0 | 9376 | 0.0 | undefined | n/a |
| unprompted_push | medium | oracle | 24 | 9 | 21039 | 0.0 | 2337.7 | 37340 |
| unprompted_push | medium | pull | 24 | 3 | 328352 | 0.0 | 109450.7 | n/a |
| unprompted_push | medium | push_breadcrumb | 20 | 0 | 12885 | 0.0 | undefined | 13368 |
| unprompted_push | medium | push_breadcrumb_pull | 20 | 1 | 375330 | 0.0 | 375330.0 | 13368 |
| unprompted_push | medium | push_pages_upper_bound | 24 | 0 | 34097 | 0.0 | undefined | 79582 |

## Query quality

`lexical_overlap(a, b) = |terms(a) ∩ terms(b)| / |terms(a) ∪ terms(b)|` (Jaccard) — `terms()` is lowercased alphanumeric tokens of length >= 3, minus a short stopword list; denominator is the union of both term sets, 0.0 when either side is empty. `self_query_overlap` is measured between the agent's own recall-tool query and the target (ground-truth) page text; `topic_query_overlap` is the SAME formula against the same target, but using the probe's original query text verbatim as a zero-cost baseline (no extraction step is run to produce it — it already exists).

| probe_class | corpus_scale | n | self_query_overlap | topic_query_overlap |
| --- | --- | --- | --- | --- |
| negative_knowledge | core | 6 | 0.101 | 0.094 |
| negative_knowledge | medium | 6 | 0.107 | 0.094 |
| unprompted_push | core | 24 | 0.046 | 0.127 |
| unprompted_push | medium | 24 | 0.047 | 0.129 |

## Cost (per turn, not per task)

`mean_injected_context_tokens` (PUSH/ORACLE) is counted whether the delivered pages were used or not — that is the asymmetry against PULL, which pays only on a turn where it actually calls recall. Read the two columns side by side, never in isolation.

| probe_class | corpus_scale | arm | n | mean_input_tokens/turn | mean_output_tokens/turn | mean_injected_context_tokens |
| --- | --- | --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | 1476.1 | 123.9 | n/a |
| negative_knowledge | core | native_index | 6 | 6047.9 | 93.2 | n/a |
| negative_knowledge | core | none | 6 | 223.7 | 144.5 | n/a |
| negative_knowledge | core | oracle | 6 | 380.3 | 145.8 | 125.7 |
| negative_knowledge | core | pull | 6 | 3402.1 | 160.6 | n/a |
| negative_knowledge | core | push_breadcrumb | 6 | 407.0 | 180.5 | 159.7 |
| negative_knowledge | core | push_breadcrumb_pull | 6 | 4190.9 | 166.9 | 159.7 |
| negative_knowledge | core | push_pages_upper_bound | 6 | 1067.7 | 156.3 | 711.7 |
| negative_knowledge | medium | native_grep | 5 | 2030.5 | 136.2 | n/a |
| negative_knowledge | medium | native_index | 6 | 6416.0 | 89.9 | n/a |
| negative_knowledge | medium | none | 6 | 223.7 | 143.8 | n/a |
| negative_knowledge | medium | oracle | 6 | 380.3 | 155.5 | 125.7 |
| negative_knowledge | medium | pull | 6 | 3502.4 | 158.5 | n/a |
| negative_knowledge | medium | push_breadcrumb | 2 | 418.0 | 216.5 | 166.0 |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | 4187.2 | 191.0 | 166.0 |
| negative_knowledge | medium | push_pages_upper_bound | 6 | 1051.3 | 156.3 | 706.7 |
| unprompted_push | core | native_grep | 24 | 1597.0 | 165.7 | n/a |
| unprompted_push | core | native_index | 23 | 6495.6 | 163.8 | n/a |
| unprompted_push | core | none | 24 | 232.7 | 143.7 | n/a |
| unprompted_push | core | oracle | 24 | 663.2 | 205.4 | 388.5 |
| unprompted_push | core | pull | 24 | 4308.5 | 212.1 | n/a |
| unprompted_push | core | push_breadcrumb | 24 | 421.2 | 224.8 | 170.4 |
| unprompted_push | core | push_breadcrumb_pull | 24 | 5144.9 | 216.9 | 170.4 |
| unprompted_push | core | push_pages_upper_bound | 24 | 1179.9 | 256.3 | 843.8 |
| unprompted_push | medium | native_grep | 24 | 1907.7 | 169.7 | n/a |
| unprompted_push | medium | native_index | 22 | 6889.7 | 169.1 | n/a |
| unprompted_push | medium | none | 24 | 232.7 | 158.0 | n/a |
| unprompted_push | medium | oracle | 24 | 663.2 | 213.4 | 388.5 |
| unprompted_push | medium | pull | 24 | 4538.1 | 220.6 | n/a |
| unprompted_push | medium | push_breadcrumb | 20 | 415.6 | 228.7 | 165.8 |
| unprompted_push | medium | push_breadcrumb_pull | 20 | 5553.8 | 220.5 | 165.8 |
| unprompted_push | medium | push_pages_upper_bound | 24 | 1155.9 | 264.8 | 825.1 |

## Efficiency

| probe_class | corpus_scale | arm | n | mean_turn_count | mean_tool_call_count |
| --- | --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | 3.33 | 3.67 |
| negative_knowledge | core | native_index | 6 | 4.00 | 3.50 |
| negative_knowledge | core | none | 6 | 1.00 | 0.00 |
| negative_knowledge | core | oracle | 6 | 1.00 | 0.00 |
| negative_knowledge | core | pull | 6 | 3.00 | 2.50 |
| negative_knowledge | core | push_breadcrumb | 6 | 1.00 | 0.00 |
| negative_knowledge | core | push_breadcrumb_pull | 6 | 3.00 | 2.83 |
| negative_knowledge | core | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| negative_knowledge | medium | native_grep | 5 | 3.40 | 5.20 |
| negative_knowledge | medium | native_index | 6 | 4.33 | 3.83 |
| negative_knowledge | medium | none | 6 | 1.00 | 0.00 |
| negative_knowledge | medium | oracle | 6 | 1.00 | 0.00 |
| negative_knowledge | medium | pull | 6 | 3.00 | 2.17 |
| negative_knowledge | medium | push_breadcrumb | 2 | 1.00 | 0.00 |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | 3.00 | 3.50 |
| negative_knowledge | medium | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| unprompted_push | core | native_grep | 24 | 3.29 | 5.62 |
| unprompted_push | core | native_index | 23 | 4.57 | 7.65 |
| unprompted_push | core | none | 24 | 1.00 | 0.00 |
| unprompted_push | core | oracle | 24 | 1.00 | 0.00 |
| unprompted_push | core | pull | 24 | 2.75 | 3.71 |
| unprompted_push | core | push_breadcrumb | 24 | 1.00 | 0.00 |
| unprompted_push | core | push_breadcrumb_pull | 24 | 3.08 | 4.29 |
| unprompted_push | core | push_pages_upper_bound | 24 | 1.00 | 0.00 |
| unprompted_push | medium | native_grep | 24 | 3.42 | 6.25 |
| unprompted_push | medium | native_index | 22 | 4.27 | 7.41 |
| unprompted_push | medium | none | 24 | 1.00 | 0.00 |
| unprompted_push | medium | oracle | 24 | 1.00 | 0.00 |
| unprompted_push | medium | pull | 24 | 2.88 | 4.25 |
| unprompted_push | medium | push_breadcrumb | 20 | 1.00 | 0.00 |
| unprompted_push | medium | push_breadcrumb_pull | 20 | 3.25 | 4.65 |
| unprompted_push | medium | push_pages_upper_bound | 24 | 1.00 | 0.00 |

## Waste

`wasted_page_fraction = 1 - uid_citation_rate` — the fraction of delivered pages whose uid never appears in the answer text, i.e. never referenced anywhere later in the rollout. `wasted_tokens_estimate` distributes the delivered content's estimated token count evenly across delivered uids and sums the wasted share. `n/a` means nothing was delivered for that group (no basis to call anything wasted).

| probe_class | corpus_scale | arm | n | wasted_page_fraction | wasted_tokens_estimate |
| --- | --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | n/a | n/a |
| negative_knowledge | core | native_index | 6 | n/a | n/a |
| negative_knowledge | core | none | 6 | n/a | n/a |
| negative_knowledge | core | oracle | 6 | 1.000 | 125.7 |
| negative_knowledge | core | pull | 6 | 1.000 | 1474.2 |
| negative_knowledge | core | push_breadcrumb | 6 | n/a | n/a |
| negative_knowledge | core | push_breadcrumb_pull | 6 | 0.983 | 2288.9 |
| negative_knowledge | core | push_pages_upper_bound | 6 | 1.000 | 711.7 |
| negative_knowledge | medium | native_grep | 5 | n/a | n/a |
| negative_knowledge | medium | native_index | 6 | n/a | n/a |
| negative_knowledge | medium | none | 6 | n/a | n/a |
| negative_knowledge | medium | oracle | 6 | 1.000 | 125.7 |
| negative_knowledge | medium | pull | 6 | 1.000 | 1569.8 |
| negative_knowledge | medium | push_breadcrumb | 2 | n/a | n/a |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | 1.000 | 2367.5 |
| negative_knowledge | medium | push_pages_upper_bound | 6 | 1.000 | 706.7 |
| unprompted_push | core | native_grep | 24 | n/a | n/a |
| unprompted_push | core | native_index | 23 | n/a | n/a |
| unprompted_push | core | none | 24 | n/a | n/a |
| unprompted_push | core | oracle | 24 | 1.000 | 388.5 |
| unprompted_push | core | pull | 24 | 0.871 | 2476.8 |
| unprompted_push | core | push_breadcrumb | 24 | n/a | n/a |
| unprompted_push | core | push_breadcrumb_pull | 24 | 0.923 | 3382.9 |
| unprompted_push | core | push_pages_upper_bound | 24 | 0.708 | 596.0 |
| unprompted_push | medium | native_grep | 24 | n/a | n/a |
| unprompted_push | medium | native_index | 22 | n/a | n/a |
| unprompted_push | medium | none | 24 | n/a | n/a |
| unprompted_push | medium | oracle | 24 | 0.979 | 380.0 |
| unprompted_push | medium | pull | 24 | 0.917 | 2869.7 |
| unprompted_push | medium | push_breadcrumb | 20 | n/a | n/a |
| unprompted_push | medium | push_breadcrumb_pull | 20 | 0.929 | 3882.5 |
| unprompted_push | medium | push_pages_upper_bound | 24 | 0.708 | 583.9 |

## Utilization (free portion only)

`uid_citation_rate` = fraction of delivered uids whose literal uid string appears in the answer text. `distinctive_ngram_overlap` = `|ngrams(delivered) ∩ ngrams(answer)| / |ngrams(delivered)|` over 4-word shingles — denominator is the delivered text's own shingle count. Both are coarse, zero-judgment proxies for "did the answer draw on what was delivered" — the judged per-claim support check is explicitly out of scope for this report.

| probe_class | corpus_scale | arm | n | uid_citation_rate | distinctive_ngram_overlap |
| --- | --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | n/a | n/a |
| negative_knowledge | core | native_index | 6 | n/a | n/a |
| negative_knowledge | core | none | 6 | n/a | n/a |
| negative_knowledge | core | oracle | 6 | 0.000 | 0.129 |
| negative_knowledge | core | pull | 6 | 0.000 | 0.035 |
| negative_knowledge | core | push_breadcrumb | 6 | n/a | 0.032 |
| negative_knowledge | core | push_breadcrumb_pull | 6 | 0.017 | 0.018 |
| negative_knowledge | core | push_pages_upper_bound | 6 | 0.000 | 0.065 |
| negative_knowledge | medium | native_grep | 5 | n/a | n/a |
| negative_knowledge | medium | native_index | 6 | n/a | n/a |
| negative_knowledge | medium | none | 6 | n/a | n/a |
| negative_knowledge | medium | oracle | 6 | 0.000 | 0.117 |
| negative_knowledge | medium | pull | 6 | 0.000 | 0.030 |
| negative_knowledge | medium | push_breadcrumb | 2 | n/a | 0.085 |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | 0.000 | 0.023 |
| negative_knowledge | medium | push_pages_upper_bound | 6 | 0.000 | 0.054 |
| unprompted_push | core | native_grep | 24 | n/a | n/a |
| unprompted_push | core | native_index | 23 | n/a | n/a |
| unprompted_push | core | none | 24 | n/a | n/a |
| unprompted_push | core | oracle | 24 | 0.000 | 0.104 |
| unprompted_push | core | pull | 24 | 0.129 | 0.038 |
| unprompted_push | core | push_breadcrumb | 24 | n/a | 0.093 |
| unprompted_push | core | push_breadcrumb_pull | 24 | 0.077 | 0.028 |
| unprompted_push | core | push_pages_upper_bound | 24 | 0.292 | 0.083 |
| unprompted_push | medium | native_grep | 24 | n/a | n/a |
| unprompted_push | medium | native_index | 22 | n/a | n/a |
| unprompted_push | medium | none | 24 | n/a | n/a |
| unprompted_push | medium | oracle | 24 | 0.021 | 0.124 |
| unprompted_push | medium | pull | 24 | 0.083 | 0.045 |
| unprompted_push | medium | push_breadcrumb | 20 | n/a | 0.096 |
| unprompted_push | medium | push_breadcrumb_pull | 20 | 0.071 | 0.027 |
| unprompted_push | medium | push_pages_upper_bound | 24 | 0.292 | 0.100 |

## Correctness (answer ground truth, issue athenaeum#1573)

`correctness_rate` grades the ANSWER, not retrieval: normalized substring match against each probe's planted `answer_tokens` (non-abstention), or the declining-language rule for abstention probes — see `grade_correctness`. No LLM judge. This is what makes NONE (floor) and ORACLE (ceiling) readable as numbers for the first time — every other dimension above describes retrieval or cost, never whether the final answer was actually right. `n/a` means no probe in that group carries ground truth tokens to grade against. Those tokens are the corpus pages' own internal reference tags, so every arm's system prompt carries one identical instruction (issue athenaeum#1753) to end the answer with `[ref: TAG]` for each page relied on, or `[ref: none]` for none — correctness therefore reads as “did the arm reach the right page and say so”. Rows recorded before that contract landed carry no tags and grade at or near 0 for every arm, including ORACLE.

| probe_class | corpus_scale | arm | n | correctness_rate |
| --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | 1.000 |
| negative_knowledge | core | native_index | 6 | 0.667 |
| negative_knowledge | core | none | 6 | 0.000 |
| negative_knowledge | core | oracle | 6 | 0.833 |
| negative_knowledge | core | pull | 6 | 0.833 |
| negative_knowledge | core | push_breadcrumb | 6 | 0.000 |
| negative_knowledge | core | push_breadcrumb_pull | 6 | 0.667 |
| negative_knowledge | core | push_pages_upper_bound | 6 | 1.000 |
| negative_knowledge | medium | native_grep | 5 | 0.800 |
| negative_knowledge | medium | native_index | 6 | 0.833 |
| negative_knowledge | medium | none | 6 | 0.000 |
| negative_knowledge | medium | oracle | 6 | 0.833 |
| negative_knowledge | medium | pull | 6 | 0.833 |
| negative_knowledge | medium | push_breadcrumb | 2 | 0.000 |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | 1.000 |
| negative_knowledge | medium | push_pages_upper_bound | 6 | 1.000 |
| unprompted_push | core | native_grep | 24 | 0.000 |
| unprompted_push | core | native_index | 23 | 0.261 |
| unprompted_push | core | none | 24 | 0.000 |
| unprompted_push | core | oracle | 24 | 0.375 |
| unprompted_push | core | pull | 24 | 0.000 |
| unprompted_push | core | push_breadcrumb | 24 | 0.000 |
| unprompted_push | core | push_breadcrumb_pull | 24 | 0.125 |
| unprompted_push | core | push_pages_upper_bound | 24 | 0.000 |
| unprompted_push | medium | native_grep | 24 | 0.042 |
| unprompted_push | medium | native_index | 22 | 0.182 |
| unprompted_push | medium | none | 24 | 0.000 |
| unprompted_push | medium | oracle | 24 | 0.375 |
| unprompted_push | medium | pull | 24 | 0.125 |
| unprompted_push | medium | push_breadcrumb | 20 | 0.000 |
| unprompted_push | medium | push_breadcrumb_pull | 20 | 0.050 |
| unprompted_push | medium | push_pages_upper_bound | 24 | 0.000 |

## Harm (forbidden-token) rate (issue athenaeum#1772, report_only)

`harm_free_rate` grades whether the answer avoided every one of a probe's planted `forbidden_tokens` -- see `grade_harm`. Same normalizer and substring instrument as `correctness_rate`, no LLM judge. `report_only` (issue athenaeum#1791 §2.1): this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no probe in that group carries `forbidden_tokens` to grade against -- true for every probe class shipped so far, so this section reads `n/a` throughout the current corpus.

| probe_class | corpus_scale | arm | n | harm_free_rate |
| --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | 0.833 |
| negative_knowledge | core | native_index | 6 | 0.667 |
| negative_knowledge | core | none | 6 | 1.000 |
| negative_knowledge | core | oracle | 6 | 1.000 |
| negative_knowledge | core | pull | 6 | 0.667 |
| negative_knowledge | core | push_breadcrumb | 6 | 1.000 |
| negative_knowledge | core | push_breadcrumb_pull | 6 | 0.167 |
| negative_knowledge | core | push_pages_upper_bound | 6 | 0.500 |
| negative_knowledge | medium | native_grep | 5 | 0.800 |
| negative_knowledge | medium | native_index | 6 | 0.667 |
| negative_knowledge | medium | none | 6 | 1.000 |
| negative_knowledge | medium | oracle | 6 | 1.000 |
| negative_knowledge | medium | pull | 6 | 0.667 |
| negative_knowledge | medium | push_breadcrumb | 2 | 1.000 |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | 0.000 |
| negative_knowledge | medium | push_pages_upper_bound | 6 | 0.833 |
| unprompted_push | core | native_grep | 24 | 0.042 |
| unprompted_push | core | native_index | 23 | 0.261 |
| unprompted_push | core | none | 24 | 1.000 |
| unprompted_push | core | oracle | 24 | 1.000 |
| unprompted_push | core | pull | 24 | 0.125 |
| unprompted_push | core | push_breadcrumb | 24 | 1.000 |
| unprompted_push | core | push_breadcrumb_pull | 24 | 0.083 |
| unprompted_push | core | push_pages_upper_bound | 24 | 0.208 |
| unprompted_push | medium | native_grep | 24 | 0.083 |
| unprompted_push | medium | native_index | 22 | 0.273 |
| unprompted_push | medium | none | 24 | 1.000 |
| unprompted_push | medium | oracle | 24 | 1.000 |
| unprompted_push | medium | pull | 24 | 0.125 |
| unprompted_push | medium | push_breadcrumb | 20 | 1.000 |
| unprompted_push | medium | push_breadcrumb_pull | 20 | 0.000 |
| unprompted_push | medium | push_pages_upper_bound | 24 | 0.292 |

## Coverage (fraction of planted tokens) (issue athenaeum#1773, report_only)

`coverage_rate` is the mean fraction of a probe's `answer_tokens` present in the answer -- see `grade_coverage`. Same normalizer and substring instrument as `correctness_rate`, but a ratio rather than an all-or-nothing match, so a many-correct-answer probe (the `aggregation` class) can grade partial credit instead of a forced zero. `report_only` (issue athenaeum#1791 §2.1): this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no probe in that group carries `answer_tokens` to grade against. For a single-`answer_tokens` probe, `coverage_rate` and `correctness_rate` are the same number by construction.

| probe_class | corpus_scale | arm | n | coverage_rate |
| --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | 1.000 |
| negative_knowledge | core | native_index | 6 | 1.000 |
| negative_knowledge | core | none | 6 | 0.000 |
| negative_knowledge | core | oracle | 6 | 1.000 |
| negative_knowledge | core | pull | 6 | 1.000 |
| negative_knowledge | core | push_breadcrumb | 6 | 0.000 |
| negative_knowledge | core | push_breadcrumb_pull | 6 | 1.000 |
| negative_knowledge | core | push_pages_upper_bound | 6 | 1.000 |
| negative_knowledge | medium | native_grep | 5 | 1.000 |
| negative_knowledge | medium | native_index | 6 | 1.000 |
| negative_knowledge | medium | none | 6 | 0.000 |
| negative_knowledge | medium | oracle | 6 | 1.000 |
| negative_knowledge | medium | pull | 6 | 1.000 |
| negative_knowledge | medium | push_breadcrumb | 2 | 0.000 |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | 1.000 |
| negative_knowledge | medium | push_pages_upper_bound | 6 | 1.000 |
| unprompted_push | core | native_grep | 24 | 0.125 |
| unprompted_push | core | native_index | 23 | 0.826 |
| unprompted_push | core | none | 24 | 0.000 |
| unprompted_push | core | oracle | 24 | 1.000 |
| unprompted_push | core | pull | 24 | 0.292 |
| unprompted_push | core | push_breadcrumb | 24 | 0.000 |
| unprompted_push | core | push_breadcrumb_pull | 24 | 0.417 |
| unprompted_push | core | push_pages_upper_bound | 24 | 0.000 |
| unprompted_push | medium | native_grep | 24 | 0.167 |
| unprompted_push | medium | native_index | 22 | 0.773 |
| unprompted_push | medium | none | 24 | 0.000 |
| unprompted_push | medium | oracle | 24 | 1.000 |
| unprompted_push | medium | pull | 24 | 0.458 |
| unprompted_push | medium | push_breadcrumb | 20 | 0.000 |
| unprompted_push | medium | push_breadcrumb_pull | 20 | 0.400 |
| unprompted_push | medium | push_pages_upper_bound | 24 | 0.000 |

## Marker resolution (issue athenaeum#1730, report_only)

`marker_resolution_rate` is the share of answers whose CITED `[^src-N]` footnote markers all resolve to a footnote definition on a page that plants one of the probe's `answer_tokens` -- see `grade_marker_resolution`. It asks whether an answer that quotes a citation quoted a real one, now that compiled pages carry inline per-claim markers and `recall` resolves them on the hit. `report_only`: this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no answer in that group cited a marker, or no token-bearing expected page defines one -- both are 'nothing to grade', never a graded zero.

| probe_class | corpus_scale | arm | n | marker_resolution_rate |
| --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | n/a |
| negative_knowledge | core | native_index | 6 | n/a |
| negative_knowledge | core | none | 6 | n/a |
| negative_knowledge | core | oracle | 6 | n/a |
| negative_knowledge | core | pull | 6 | n/a |
| negative_knowledge | core | push_breadcrumb | 6 | n/a |
| negative_knowledge | core | push_breadcrumb_pull | 6 | n/a |
| negative_knowledge | core | push_pages_upper_bound | 6 | n/a |
| negative_knowledge | medium | native_grep | 5 | n/a |
| negative_knowledge | medium | native_index | 6 | n/a |
| negative_knowledge | medium | none | 6 | n/a |
| negative_knowledge | medium | oracle | 6 | n/a |
| negative_knowledge | medium | pull | 6 | n/a |
| negative_knowledge | medium | push_breadcrumb | 2 | n/a |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | n/a |
| negative_knowledge | medium | push_pages_upper_bound | 6 | n/a |
| unprompted_push | core | native_grep | 24 | n/a |
| unprompted_push | core | native_index | 23 | n/a |
| unprompted_push | core | none | 24 | n/a |
| unprompted_push | core | oracle | 24 | n/a |
| unprompted_push | core | pull | 24 | n/a |
| unprompted_push | core | push_breadcrumb | 24 | n/a |
| unprompted_push | core | push_breadcrumb_pull | 24 | n/a |
| unprompted_push | core | push_pages_upper_bound | 24 | n/a |
| unprompted_push | medium | native_grep | 24 | n/a |
| unprompted_push | medium | native_index | 22 | n/a |
| unprompted_push | medium | none | 24 | n/a |
| unprompted_push | medium | oracle | 24 | n/a |
| unprompted_push | medium | pull | 24 | n/a |
| unprompted_push | medium | push_breadcrumb | 20 | n/a |
| unprompted_push | medium | push_breadcrumb_pull | 20 | n/a |
| unprompted_push | medium | push_pages_upper_bound | 24 | n/a |

## Reference tag followed (issue athenaeum#1831, report_only)

`tag_followed_rate` is the share of answers that quoted the `[ref: TAG]` `REFERENCE_TAG_INSTRUCTION` asks for, for every one of the probe's planted tags -- see `tag_followed`. This is the athenaeum#1753 correctness rule, DEMOTED by the operator ruling on athenaeum#1791 comment 5732689494: it feeds no §7 condition and no `correctness_rate` above, which is now graded on `answer_markers` (the planted fact) plus delivered-page evidence, never this tag. `n/a` means no probe in that group carries `answer_tokens` to grade against (including every abstention group).

| probe_class | corpus_scale | arm | n | tag_followed_rate |
| --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | 1.000 |
| negative_knowledge | core | native_index | 6 | 1.000 |
| negative_knowledge | core | none | 6 | 0.000 |
| negative_knowledge | core | oracle | 6 | 1.000 |
| negative_knowledge | core | pull | 6 | 1.000 |
| negative_knowledge | core | push_breadcrumb | 6 | 0.000 |
| negative_knowledge | core | push_breadcrumb_pull | 6 | 1.000 |
| negative_knowledge | core | push_pages_upper_bound | 6 | 1.000 |
| negative_knowledge | medium | native_grep | 5 | 1.000 |
| negative_knowledge | medium | native_index | 6 | 1.000 |
| negative_knowledge | medium | none | 6 | 0.000 |
| negative_knowledge | medium | oracle | 6 | 1.000 |
| negative_knowledge | medium | pull | 6 | 1.000 |
| negative_knowledge | medium | push_breadcrumb | 2 | 0.000 |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | 1.000 |
| negative_knowledge | medium | push_pages_upper_bound | 6 | 1.000 |
| unprompted_push | core | native_grep | 24 | 0.125 |
| unprompted_push | core | native_index | 23 | 0.826 |
| unprompted_push | core | none | 24 | 0.000 |
| unprompted_push | core | oracle | 24 | 1.000 |
| unprompted_push | core | pull | 24 | 0.292 |
| unprompted_push | core | push_breadcrumb | 24 | 0.000 |
| unprompted_push | core | push_breadcrumb_pull | 24 | 0.417 |
| unprompted_push | core | push_pages_upper_bound | 24 | 0.000 |
| unprompted_push | medium | native_grep | 24 | 0.167 |
| unprompted_push | medium | native_index | 22 | 0.773 |
| unprompted_push | medium | none | 24 | 0.000 |
| unprompted_push | medium | oracle | 24 | 1.000 |
| unprompted_push | medium | pull | 24 | 0.458 |
| unprompted_push | medium | push_breadcrumb | 20 | 0.000 |
| unprompted_push | medium | push_breadcrumb_pull | 20 | 0.400 |
| unprompted_push | medium | push_pages_upper_bound | 24 | 0.000 |

## Marker miss with delivery (issue athenaeum#1842, report_only)

`marker_miss_with_delivery` is the COUNT of gradable cells in that group where every answer-bearing page WAS delivered to the arm (`correctness_rate`'s delivered-page-evidence clause held) and at least one of those pages still contributed no matching `answer_markers` value to the answer -- see `marker_miss_with_delivery`. It splits a `False` cell into its two causes: a delivery gap (counted here as 0) versus a model miss or a marker-matching gap (counted here as 1). `report_only`: this column feeds no §7 condition, no `compute_verdicts` input and no win/loss field, exactly as `tag_followed_rate` above does not. `n/a` means no row in that group is gradable at all (including every abstention group) -- never a counted zero.

| probe_class | corpus_scale | arm | n | marker_miss_with_delivery |
| --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | 0 |
| negative_knowledge | core | native_index | 6 | 2 |
| negative_knowledge | core | none | 6 | 0 |
| negative_knowledge | core | oracle | 6 | 1 |
| negative_knowledge | core | pull | 6 | 1 |
| negative_knowledge | core | push_breadcrumb | 6 | 6 |
| negative_knowledge | core | push_breadcrumb_pull | 6 | 2 |
| negative_knowledge | core | push_pages_upper_bound | 6 | 0 |
| negative_knowledge | medium | native_grep | 5 | 1 |
| negative_knowledge | medium | native_index | 6 | 1 |
| negative_knowledge | medium | none | 6 | 0 |
| negative_knowledge | medium | oracle | 6 | 1 |
| negative_knowledge | medium | pull | 6 | 1 |
| negative_knowledge | medium | push_breadcrumb | 2 | 2 |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | 0 |
| negative_knowledge | medium | push_pages_upper_bound | 6 | 0 |
| unprompted_push | core | native_grep | 24 | 3 |
| unprompted_push | core | native_index | 23 | 13 |
| unprompted_push | core | none | 24 | 0 |
| unprompted_push | core | oracle | 24 | 15 |
| unprompted_push | core | pull | 24 | 10 |
| unprompted_push | core | push_breadcrumb | 24 | 4 |
| unprompted_push | core | push_breadcrumb_pull | 24 | 11 |
| unprompted_push | core | push_pages_upper_bound | 24 | 2 |
| unprompted_push | medium | native_grep | 24 | 3 |
| unprompted_push | medium | native_index | 22 | 13 |
| unprompted_push | medium | none | 24 | 0 |
| unprompted_push | medium | oracle | 24 | 15 |
| unprompted_push | medium | pull | 24 | 9 |
| unprompted_push | medium | push_breadcrumb | 20 | 2 |
| unprompted_push | medium | push_breadcrumb_pull | 20 | 9 |
| unprompted_push | medium | push_pages_upper_bound | 24 | 2 |

## Follow-through hop delivered (issue athenaeum#1844, report_only)

`follow_hop_rate` is the share of `follow_through` cells in that group where every page named by `corpus.deep_hop_uids` -- the page an arm can only have landed on by FOLLOWING the body `[[wikilink]]`, never by a lexical match on the query -- was delivered to the arm, read from `_delivered_uids` (the grader's own dispatch). It separates the two causes a `False` `correctness_rate` conflates on this class: the arm never reached the second page (counted here as 0) versus it reached the page and the answer still did not carry the fact (counted here as 1, alongside a `marker_miss_with_delivery` of 1). The deep page is DERIVED from the link graph, never read off a fixed `expected_uids` position. `report_only`: this column feeds no §7 condition, no cutoff, no `compute_verdicts` input and no win/loss field, exactly as `marker_miss_with_delivery` above does not. `n/a` for every non-`follow_through` group -- no hop to have followed is a different fact from a rate of zero.

| probe_class | corpus_scale | arm | n | follow_hop_rate |
| --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 6 | n/a |
| negative_knowledge | core | native_index | 6 | n/a |
| negative_knowledge | core | none | 6 | n/a |
| negative_knowledge | core | oracle | 6 | n/a |
| negative_knowledge | core | pull | 6 | n/a |
| negative_knowledge | core | push_breadcrumb | 6 | n/a |
| negative_knowledge | core | push_breadcrumb_pull | 6 | n/a |
| negative_knowledge | core | push_pages_upper_bound | 6 | n/a |
| negative_knowledge | medium | native_grep | 5 | n/a |
| negative_knowledge | medium | native_index | 6 | n/a |
| negative_knowledge | medium | none | 6 | n/a |
| negative_knowledge | medium | oracle | 6 | n/a |
| negative_knowledge | medium | pull | 6 | n/a |
| negative_knowledge | medium | push_breadcrumb | 2 | n/a |
| negative_knowledge | medium | push_breadcrumb_pull | 2 | n/a |
| negative_knowledge | medium | push_pages_upper_bound | 6 | n/a |
| unprompted_push | core | native_grep | 24 | n/a |
| unprompted_push | core | native_index | 23 | n/a |
| unprompted_push | core | none | 24 | n/a |
| unprompted_push | core | oracle | 24 | n/a |
| unprompted_push | core | pull | 24 | n/a |
| unprompted_push | core | push_breadcrumb | 24 | n/a |
| unprompted_push | core | push_breadcrumb_pull | 24 | n/a |
| unprompted_push | core | push_pages_upper_bound | 24 | n/a |
| unprompted_push | medium | native_grep | 24 | n/a |
| unprompted_push | medium | native_index | 22 | n/a |
| unprompted_push | medium | none | 24 | n/a |
| unprompted_push | medium | oracle | 24 | n/a |
| unprompted_push | medium | pull | 24 | n/a |
| unprompted_push | medium | push_breadcrumb | 20 | n/a |
| unprompted_push | medium | push_breadcrumb_pull | 20 | n/a |
| unprompted_push | medium | push_pages_upper_bound | 24 | n/a |

### Weak probes (NONE arm already answers correctly)

Probes where the floor (no context at all) already grades correct — a signal the ground truth leaked into the model's own prior knowledge, or the token is otherwise guessable, not that any retrieval arm helped. Read alongside the ORACLE ceiling: a probe listed here needs a harder token or a different question, not a better arm.

_none observed in this run_

## Index coverage (NATIVE_INDEX only, athenaeum#1725)

`index_coverage = index_lines_loaded / pages` -- the fraction of the corpus the TRUNCATED index still names. `index_lines_loaded` is read back from what Claude Code actually loaded into the session (the `attachment` event in the session transcript), never recomputed from the 200-line/25KB cap: Claude Code performs the truncation, this report only observes its result. `n/a` means no NATIVE_INDEX row in that group carried a readable coverage figure (e.g. auto memory did not activate).

| probe_class | corpus_scale | n | index_coverage |
| --- | --- | --- | --- |
| negative_knowledge | core | 6 | 1.000 |
| negative_knowledge | medium | 6 | 0.200 |
| unprompted_push | core | 23 | 1.000 |
| unprompted_push | medium | 22 | 0.200 |

## Write path (Phase 2, athenaeum#1726) -- native memories filed by librarian

Issue athenaeum#1830 (operator ruling, Kromatic-Innovation/athenaeum#1791 comment 5732689494): the librarian is not a fact writer and never acts as one in production -- it files what Claude has already written. The `athenaeum` row below is the NATIVE writer's own memory files, materialised as auto-memory intake and compiled by the librarian -- "Claude's memories versus Claude's memories after filing," not raw observations compiled directly (`tests.evals.write_path.compile_observation_stream` still exists as a diagnostic; it feeds no row here). Scoped to observations carrying at least one planted token (`observations_measured` of `observations_total`); an observation with no token plants nothing this scanner can check. `n/a` means no token-bearing observation was present for that system/scale, never a fabricated 0.

_no Phase 2 write-path data in this run_

### Filing loss (issue athenaeum#1830)

A second, SEPARATE retention row: the compiled store measured against the tokens the NATIVE writer itself already retained, not against the full observation stream -- so filing loss is distinguishable from Claude's own write loss (see `compute_filing_loss_stats`). A token Claude never wrote down at all is excluded from `native_tokens_total` entirely; it is a native write loss, already visible on the ordinary table above, not a filing loss.

_no filing-loss data in this run_

## Crossover scale (athenaeum#1725)

The smallest scale, per probe class, at which Athenaeum's correctness exceeds native's -- "the number the decision turns on" (design doc `docs/design/native-memory-baseline.md` §6/§7). Walks the SIZE axis only (`core` < `small` < `medium` < `large` < `xlarge`); the `medium_dense`/`medium_verydense` confusability scales are a different axis and are never considered here. **Athenaeum's correctness** = the best `correctness_rate` among its DELIVERY arms only (`push_pages_upper_bound`, `push_breadcrumb`, `push_breadcrumb_pull`, `pull`) -- `none` (the floor) and `oracle` (the ceiling) are explicitly excluded, because neither is a shippable configuration. **Native's correctness** = the best `correctness_rate` among `native_index`/`native_grep`. `n/a` means insufficient data at every scale for that probe class to establish a crossover -- never a fabricated scale.

| probe_class | crossover_scale |
| --- | --- |
| negative_knowledge | medium |
| unprompted_push | n/a |

## Frontier (cost vs. quality — never a single composite)

This report intentionally emits no weighted score combining cost and quality: the weights are a business judgement, not a measurement (issue athenaeum#1523). Read the Cost and Query-quality/Utilization sections above side by side, per (probe_class, corpus_scale), and decide the tradeoff from there — the numbers below are restated unweighted, one arm's cost beside its utilization, nothing summed across them.

| probe_class | corpus_scale | arm | mean_input_tokens/turn | mean_output_tokens/turn | uid_citation_rate | distinctive_ngram_overlap |
| --- | --- | --- | --- | --- | --- | --- |
| negative_knowledge | core | native_grep | 1476.1 | 123.9 | n/a | n/a |
| negative_knowledge | core | native_index | 6047.9 | 93.2 | n/a | n/a |
| negative_knowledge | core | none | 223.7 | 144.5 | n/a | n/a |
| negative_knowledge | core | oracle | 380.3 | 145.8 | 0.000 | 0.129 |
| negative_knowledge | core | pull | 3402.1 | 160.6 | 0.000 | 0.035 |
| negative_knowledge | core | push_breadcrumb | 407.0 | 180.5 | n/a | 0.032 |
| negative_knowledge | core | push_breadcrumb_pull | 4190.9 | 166.9 | 0.017 | 0.018 |
| negative_knowledge | core | push_pages_upper_bound | 1067.7 | 156.3 | 0.000 | 0.065 |
| negative_knowledge | medium | native_grep | 2030.5 | 136.2 | n/a | n/a |
| negative_knowledge | medium | native_index | 6416.0 | 89.9 | n/a | n/a |
| negative_knowledge | medium | none | 223.7 | 143.8 | n/a | n/a |
| negative_knowledge | medium | oracle | 380.3 | 155.5 | 0.000 | 0.117 |
| negative_knowledge | medium | pull | 3502.4 | 158.5 | 0.000 | 0.030 |
| negative_knowledge | medium | push_breadcrumb | 418.0 | 216.5 | n/a | 0.085 |
| negative_knowledge | medium | push_breadcrumb_pull | 4187.2 | 191.0 | 0.000 | 0.023 |
| negative_knowledge | medium | push_pages_upper_bound | 1051.3 | 156.3 | 0.000 | 0.054 |
| unprompted_push | core | native_grep | 1597.0 | 165.7 | n/a | n/a |
| unprompted_push | core | native_index | 6495.6 | 163.8 | n/a | n/a |
| unprompted_push | core | none | 232.7 | 143.7 | n/a | n/a |
| unprompted_push | core | oracle | 663.2 | 205.4 | 0.000 | 0.104 |
| unprompted_push | core | pull | 4308.5 | 212.1 | 0.129 | 0.038 |
| unprompted_push | core | push_breadcrumb | 421.2 | 224.8 | n/a | 0.093 |
| unprompted_push | core | push_breadcrumb_pull | 5144.9 | 216.9 | 0.077 | 0.028 |
| unprompted_push | core | push_pages_upper_bound | 1179.9 | 256.3 | 0.292 | 0.083 |
| unprompted_push | medium | native_grep | 1907.7 | 169.7 | n/a | n/a |
| unprompted_push | medium | native_index | 6889.7 | 169.1 | n/a | n/a |
| unprompted_push | medium | none | 232.7 | 158.0 | n/a | n/a |
| unprompted_push | medium | oracle | 663.2 | 213.4 | 0.021 | 0.124 |
| unprompted_push | medium | pull | 4538.1 | 220.6 | 0.083 | 0.045 |
| unprompted_push | medium | push_breadcrumb | 415.6 | 228.7 | n/a | 0.096 |
| unprompted_push | medium | push_breadcrumb_pull | 5553.8 | 220.5 | 0.071 | 0.027 |
| unprompted_push | medium | push_pages_upper_bound | 1155.9 | 264.8 | 0.292 | 0.100 |

## Verdict: do the free dimensions settle the question?

Observed no_call_rate (0.062) and/or the self-vs-topic query-overlap gap (0.045) are non-trivial. The free dimensions show a directional signal on their own — read the per-(probe_class, corpus_scale) tables above for where it concentrates before deciding whether judged scoring is still needed to establish magnitude.
