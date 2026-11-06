<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
Rendered north-star grid report, Actions run 42177905668 (2026-11-06, `claude-haiku-4-5-20251001`, the same 48 probes x 9 arms x core at one replicate), recorded in-tree for the same 30-day artifact expiry. Complete: 432 of 432 cells.
-->

# North-star report: free dimensions only (athenaeum#1523)

- generated: 2026-11-06T07:08:03Z
- athenaeum_version: 0.23.4
- git_sha: 5c78210c8c1c
- search_backend: vector
- hybrid: on
- relevance_floor_vector: off
- relevance_floor_fts5: off
- preflight: ok provider=api probe=pto_allowance arm=push_breadcrumb_pull recall_called=true tool_calls=1
- corpus_digest[core]: 3c5ea876688b089e
- grader_revision: athenaeum#1935
- model: claude-haiku-4-5-20251001
- total rollout rows: 432
- harness failures: 5
  - turn_cap[arm=native_grep, scale=core]: 2
  - turn_cap[arm=pull, scale=core]: 1
  - turn_cap[arm=push_breadcrumb_pull, scale=core]: 1
  - turn_cap[arm=push_sections_pull, scale=core]: 1

No LLM judge is invoked anywhere on this path — every figure below is a deterministic computation over already-captured rollout transcripts. This is a **measurement, not a regression gate**; nothing here should ever fail a build.

## Decision (design doc §7, athenaeum#1734)

**Verdict arm:** `push_breadcrumb_pull` (ruling R1) -- the shipped configuration is the ONE Athenaeum arm every condition below reads. Other Athenaeum arms still appear in the per-dimension tables further down, but never in these verdicts -- no picking the most-correct arm for conditions 1-2 and the cheapest for condition 3.

Three conditions, evaluated per scale, all against the verdict arm above: **(1)** win the relationship use case (single_hop/multi_hop/disambiguation/temporal probes targeting person/company pages), one POOLED correctness rate over the whole subset, against the better native arm's own pooled rate on the same rows; **(2)** do not lose any other current use case, class by class, skipping (and naming) any class where either side has no gradable rows; **(3)** cost per correct answer within budget of the better native arm, per class, skipping (and naming) any class with no native cost data at all (ruling R5) -- `>2.0x fail`, `<=2.0x limit`, `<=1.0x target`, `<=0.5x aspirational`, or `native-zero` (a pass) when the better native arm scored zero correct answers while the verdict arm scored at least one.

report-only classes excluded: aggregation, contradiction, negative_knowledge, unprompted_push -- excluded from conditions 2 and 3 entirely (issue athenaeum#1776); promotion is an explicit operator ruling on athenaeum#1736's thread, recorded by editing `tests.evals.corpus.CONDITION_2_ENROLLED`, never a side effect of this report.

**"Better native arm"** means two different things across these conditions, both the harshest reading available to Athenaeum: for condition 1, the native arm with the HIGHER pooled correctness rate; for condition 3, the native arm with the CHEAPER DEFINED cost per correct (a native arm that scored zero correct answers has no defined cost and is never picked as "cheaper" by that alone -- see `native-zero`).

**Cutoff scale:** `none` -- no scale at or above `medium` passed all three conditions. Failing condition per scale:

- `core`: condition 3: cost compared 8 classes, skipped 0; worst reading is 'fail' for 'abstention' (ratio=5.828)

| scale | cutoff eligible | condition 1 | condition 2 | condition 3 | reading | all pass |
| --- | --- | --- | --- | --- | --- | --- |
| core | no | pass | pass | fail | fail | fail |

## Arms in this report (athenaeum#1574)

**PUSH means breadcrumbs** — `push_breadcrumb` and `push_breadcrumb_pull` are the arms that match what `examples/claude-code/user-prompt-recall.sh` actually ships: at most three 200-character-clamped `name — description` bullets, assembled by running that hook itself, never reimplemented. `push_pages_upper_bound` is the ORIGINAL five-full-page PUSH arm, kept and renamed — read it as an explicit **upper bound** ("what if the model always got the whole page"), never as the shipped configuration.

| arm | delivery | reads as |
| --- | --- | --- |
| `none` | nothing | floor |
| `push_pages_upper_bound` | 5 full pages via `recall_search` | **upper bound**, NOT the shipped hook |
| `push_breadcrumb` | <=3 breadcrumbs, via the real shipped hook | **matches production PUSH** |
| `push_breadcrumb_pull` | breadcrumbs injected + `recall` tool available | matches production PUSH, agent may still PULL |
| `push_sections_pull` | routed, section-level page content under a byte budget (`tests/evals/rollout.py`'s `PUSH_SECTIONS_BYTE_BUDGET`) + `recall`/`read_entity` tools available | **a competing delivery design, not an upper bound** -- issue athenaeum#2310, between `push_breadcrumb_pull` and `push_pages_upper_bound` |
| `oracle` | ground-truth pages verbatim | ceiling |
| `pull` | nothing injected, `recall` tool available | agent-initiated only |
| `native_index` | Claude Code auto-memory: topic files + a full `MEMORY.md` index, loaded and truncated by Claude Code itself | native memory, WITH an index (honest past the documented cap) |
| `native_grep` | Claude Code auto-memory: topic files, no index at all | native memory, file search only |

**Issue athenaeum#1725:** `native_index`/`native_grep` read a Claude Code auto-memory store instead of an Athenaeum-compiled corpus (design lock: `docs/design/native-memory-baseline.md`) -- the read-path comparison the project's continuation decision turns on. Neither is scored against a `pushed_context`/pulled-recall basis the way the other arms are: there is no Athenaeum retrieval step to attribute utilization to.

**Issue athenaeum#2310:** `push_sections_pull` is NOT an upper bound and NOT a shipped configuration -- it is a candidate design point this comparison measures against `push_breadcrumb_pull`. Unlike the breadcrumb, its pushed text carries a real `**Uid:**` marker per page, so it is scored the same way `push_pages_upper_bound` and `pull` are (`uids_from_recall_output`), not via the breadcrumb's name-matching fallback.

## Mode per cell (athenaeum#1733)

Which execution path produced each rollout cell -- `api` (Anthropic Messages API tool-use loop, the primary path per `docs/design/native-memory-baseline.md` §4) or `cli` (`claude -p`, the fidelity spot-check). A direct render over `report.rows` itself, never folded into `GroupStats`/`compute_group_stats`.

| probe_id | arm | corpus_scale | replicate | mode | tag_followed |
| --- | --- | --- | --- | --- | --- |
| abstain_unknown_client | native_grep | core | 0 | api | n/a |
| abstain_unknown_client | native_index | core | 0 | api | n/a |
| abstain_unknown_client | none | core | 0 | api | n/a |
| abstain_unknown_client | oracle | core | 0 | api | n/a |
| abstain_unknown_client | pull | core | 0 | api | n/a |
| abstain_unknown_client | push_breadcrumb | core | 0 | api | n/a |
| abstain_unknown_client | push_breadcrumb_pull | core | 0 | api | n/a |
| abstain_unknown_client | push_pages_upper_bound | core | 0 | api | n/a |
| abstain_unknown_client | push_sections_pull | core | 0 | api | n/a |
| abstain_unknown_person | native_grep | core | 0 | api | n/a |
| abstain_unknown_person | native_index | core | 0 | api | n/a |
| abstain_unknown_person | none | core | 0 | api | n/a |
| abstain_unknown_person | oracle | core | 0 | api | n/a |
| abstain_unknown_person | pull | core | 0 | api | n/a |
| abstain_unknown_person | push_breadcrumb | core | 0 | api | n/a |
| abstain_unknown_person | push_breadcrumb_pull | core | 0 | api | n/a |
| abstain_unknown_person | push_pages_upper_bound | core | 0 | api | n/a |
| abstain_unknown_person | push_sections_pull | core | 0 | api | n/a |
| abstain_unknown_policy | native_grep | core | 0 | api | n/a |
| abstain_unknown_policy | native_index | core | 0 | api | n/a |
| abstain_unknown_policy | none | core | 0 | api | n/a |
| abstain_unknown_policy | oracle | core | 0 | api | n/a |
| abstain_unknown_policy | pull | core | 0 | api | n/a |
| abstain_unknown_policy | push_breadcrumb | core | 0 | api | n/a |
| abstain_unknown_policy | push_breadcrumb_pull | core | 0 | api | n/a |
| abstain_unknown_policy | push_pages_upper_bound | core | 0 | api | n/a |
| abstain_unknown_policy | push_sections_pull | core | 0 | api | n/a |
| acme_billing_exception | native_grep | core | 0 | api | no |
| acme_billing_exception | native_index | core | 0 | api | no |
| acme_billing_exception | none | core | 0 | api | no |
| acme_billing_exception | oracle | core | 0 | api | yes |
| acme_billing_exception | pull | core | 0 | api | yes |
| acme_billing_exception | push_breadcrumb | core | 0 | api | no |
| acme_billing_exception | push_breadcrumb_pull | core | 0 | api | yes |
| acme_billing_exception | push_pages_upper_bound | core | 0 | api | yes |
| acme_billing_exception | push_sections_pull | core | 0 | api | yes |
| anchorline_retirement_rationale | native_grep | core | 0 | api | yes |
| anchorline_retirement_rationale | native_index | core | 0 | api | yes |
| anchorline_retirement_rationale | none | core | 0 | api | no |
| anchorline_retirement_rationale | oracle | core | 0 | api | yes |
| anchorline_retirement_rationale | pull | core | 0 | api | yes |
| anchorline_retirement_rationale | push_breadcrumb | core | 0 | api | no |
| anchorline_retirement_rationale | push_breadcrumb_pull | core | 0 | api | yes |
| anchorline_retirement_rationale | push_pages_upper_bound | core | 0 | api | no |
| anchorline_retirement_rationale | push_sections_pull | core | 0 | api | yes |
| bluewater_terms | native_grep | core | 0 | api | yes |
| bluewater_terms | native_index | core | 0 | api | yes |
| bluewater_terms | none | core | 0 | api | no |
| bluewater_terms | oracle | core | 0 | api | yes |
| bluewater_terms | pull | core | 0 | api | yes |
| bluewater_terms | push_breadcrumb | core | 0 | api | no |
| bluewater_terms | push_breadcrumb_pull | core | 0 | api | yes |
| bluewater_terms | push_pages_upper_bound | core | 0 | api | yes |
| bluewater_terms | push_sections_pull | core | 0 | api | yes |
| bramfield_retainer_renewal | native_grep | core | 0 | api | yes |
| bramfield_retainer_renewal | native_index | core | 0 | api | yes |
| bramfield_retainer_renewal | none | core | 0 | api | no |
| bramfield_retainer_renewal | oracle | core | 0 | api | yes |
| bramfield_retainer_renewal | pull | core | 0 | api | yes |
| bramfield_retainer_renewal | push_breadcrumb | core | 0 | api | no |
| bramfield_retainer_renewal | push_breadcrumb_pull | core | 0 | api | yes |
| bramfield_retainer_renewal | push_pages_upper_bound | core | 0 | api | no |
| bramfield_retainer_renewal | push_sections_pull | core | 0 | api | yes |
| budget_threshold_current | native_grep | core | 0 | api | yes |
| budget_threshold_current | native_index | core | 0 | api | no |
| budget_threshold_current | none | core | 0 | api | no |
| budget_threshold_current | oracle | core | 0 | api | yes |
| budget_threshold_current | pull | core | 0 | api | yes |
| budget_threshold_current | push_breadcrumb | core | 0 | api | no |
| budget_threshold_current | push_breadcrumb_pull | core | 0 | api | yes |
| budget_threshold_current | push_pages_upper_bound | core | 0 | api | yes |
| budget_threshold_current | push_sections_pull | core | 0 | api | yes |
| cache_layer_merge | native_grep | core | 0 | api | no |
| cache_layer_merge | native_index | core | 0 | api | yes |
| cache_layer_merge | none | core | 0 | api | no |
| cache_layer_merge | oracle | core | 0 | api | yes |
| cache_layer_merge | pull | core | 0 | api | yes |
| cache_layer_merge | push_breadcrumb | core | 0 | api | no |
| cache_layer_merge | push_breadcrumb_pull | core | 0 | api | no |
| cache_layer_merge | push_pages_upper_bound | core | 0 | api | no |
| cache_layer_merge | push_sections_pull | core | 0 | api | no |
| callum_drews_last_contact | native_grep | core | 0 | api | yes |
| callum_drews_last_contact | native_index | core | 0 | api | yes |
| callum_drews_last_contact | none | core | 0 | api | no |
| callum_drews_last_contact | oracle | core | 0 | api | yes |
| callum_drews_last_contact | pull | core | 0 | api | yes |
| callum_drews_last_contact | push_breadcrumb | core | 0 | api | no |
| callum_drews_last_contact | push_breadcrumb_pull | core | 0 | api | yes |
| callum_drews_last_contact | push_pages_upper_bound | core | 0 | api | yes |
| callum_drews_last_contact | push_sections_pull | core | 0 | api | yes |
| claims_logging_off | native_grep | core | 0 | api | no |
| claims_logging_off | native_index | core | 0 | api | yes |
| claims_logging_off | none | core | 0 | api | no |
| claims_logging_off | oracle | core | 0 | api | yes |
| claims_logging_off | pull | core | 0 | api | yes |
| claims_logging_off | push_breadcrumb | core | 0 | api | no |
| claims_logging_off | push_breadcrumb_pull | core | 0 | api | no |
| claims_logging_off | push_pages_upper_bound | core | 0 | api | no |
| claims_logging_off | push_sections_pull | core | 0 | api | yes |
| confidentiality_rule | native_grep | core | 0 | api | yes |
| confidentiality_rule | native_index | core | 0 | api | yes |
| confidentiality_rule | none | core | 0 | api | no |
| confidentiality_rule | oracle | core | 0 | api | yes |
| confidentiality_rule | pull | core | 0 | api | yes |
| confidentiality_rule | push_breadcrumb | core | 0 | api | no |
| confidentiality_rule | push_breadcrumb_pull | core | 0 | api | yes |
| confidentiality_rule | push_pages_upper_bound | core | 0 | api | yes |
| confidentiality_rule | push_sections_pull | core | 0 | api | yes |
| connection_pool_widen | native_grep | core | 0 | api | no |
| connection_pool_widen | native_index | core | 0 | api | yes |
| connection_pool_widen | none | core | 0 | api | no |
| connection_pool_widen | oracle | core | 0 | api | yes |
| connection_pool_widen | pull | core | 0 | api | no |
| connection_pool_widen | push_breadcrumb | core | 0 | api | no |
| connection_pool_widen | push_breadcrumb_pull | core | 0 | api | yes |
| connection_pool_widen | push_pages_upper_bound | core | 0 | api | no |
| connection_pool_widen | push_sections_pull | core | 0 | api | no |
| corvale_holdings_history | native_grep | core | 0 | api | yes |
| corvale_holdings_history | native_index | core | 0 | api | yes |
| corvale_holdings_history | none | core | 0 | api | no |
| corvale_holdings_history | oracle | core | 0 | api | yes |
| corvale_holdings_history | pull | core | 0 | api | yes |
| corvale_holdings_history | push_breadcrumb | core | 0 | api | no |
| corvale_holdings_history | push_breadcrumb_pull | core | 0 | api | yes |
| corvale_holdings_history | push_pages_upper_bound | core | 0 | api | no |
| corvale_holdings_history | push_sections_pull | core | 0 | api | yes |
| driftgate_migration_funding | native_grep | core | 0 | api | yes |
| driftgate_migration_funding | native_index | core | 0 | api | no |
| driftgate_migration_funding | none | core | 0 | api | no |
| driftgate_migration_funding | oracle | core | 0 | api | yes |
| driftgate_migration_funding | pull | core | 0 | api | no |
| driftgate_migration_funding | push_breadcrumb | core | 0 | api | no |
| driftgate_migration_funding | push_breadcrumb_pull | core | 0 | api | no |
| driftgate_migration_funding | push_pages_upper_bound | core | 0 | api | no |
| driftgate_migration_funding | push_sections_pull | core | 0 | api | no |
| dual_signoff_threshold | native_grep | core | 0 | api | no |
| dual_signoff_threshold | native_index | core | 0 | api | no |
| dual_signoff_threshold | none | core | 0 | api | no |
| dual_signoff_threshold | oracle | core | 0 | api | yes |
| dual_signoff_threshold | pull | core | 0 | api | no |
| dual_signoff_threshold | push_breadcrumb | core | 0 | api | no |
| dual_signoff_threshold | push_breadcrumb_pull | core | 0 | api | no |
| dual_signoff_threshold | push_pages_upper_bound | core | 0 | api | no |
| dual_signoff_threshold | push_sections_pull | core | 0 | api | no |
| export_flag_deletion | native_grep | core | 0 | api | yes |
| export_flag_deletion | native_index | core | 0 | api | yes |
| export_flag_deletion | none | core | 0 | api | no |
| export_flag_deletion | oracle | core | 0 | api | yes |
| export_flag_deletion | pull | core | 0 | api | yes |
| export_flag_deletion | push_breadcrumb | core | 0 | api | no |
| export_flag_deletion | push_breadcrumb_pull | core | 0 | api | yes |
| export_flag_deletion | push_pages_upper_bound | core | 0 | api | no |
| export_flag_deletion | push_sections_pull | core | 0 | api | yes |
| fenwick_relationship_history | native_grep | core | 0 | api | yes |
| fenwick_relationship_history | native_index | core | 0 | api | yes |
| fenwick_relationship_history | none | core | 0 | api | no |
| fenwick_relationship_history | oracle | core | 0 | api | yes |
| fenwick_relationship_history | pull | core | 0 | api | yes |
| fenwick_relationship_history | push_breadcrumb | core | 0 | api | no |
| fenwick_relationship_history | push_breadcrumb_pull | core | 0 | api | yes |
| fenwick_relationship_history | push_pages_upper_bound | core | 0 | api | no |
| fenwick_relationship_history | push_sections_pull | core | 0 | api | yes |
| former_client_not_current | native_grep | core | 0 | api | yes |
| former_client_not_current | native_index | core | 0 | api | no |
| former_client_not_current | none | core | 0 | api | no |
| former_client_not_current | oracle | core | 0 | api | yes |
| former_client_not_current | pull | core | 0 | api | no |
| former_client_not_current | push_breadcrumb | core | 0 | api | no |
| former_client_not_current | push_breadcrumb_pull | core | 0 | api | yes |
| former_client_not_current | push_pages_upper_bound | core | 0 | api | yes |
| former_client_not_current | push_sections_pull | core | 0 | api | yes |
| gilcrest_vendor_decision | native_grep | core | 0 | api | no |
| gilcrest_vendor_decision | native_index | core | 0 | api | no |
| gilcrest_vendor_decision | none | core | 0 | api | no |
| gilcrest_vendor_decision | oracle | core | 0 | api | yes |
| gilcrest_vendor_decision | pull | core | 0 | api | yes |
| gilcrest_vendor_decision | push_breadcrumb | core | 0 | api | no |
| gilcrest_vendor_decision | push_breadcrumb_pull | core | 0 | api | yes |
| gilcrest_vendor_decision | push_pages_upper_bound | core | 0 | api | no |
| gilcrest_vendor_decision | push_sections_pull | core | 0 | api | no |
| given_name_is_ambiguous | native_grep | core | 0 | api | yes |
| given_name_is_ambiguous | native_index | core | 0 | api | no |
| given_name_is_ambiguous | none | core | 0 | api | no |
| given_name_is_ambiguous | oracle | core | 0 | api | yes |
| given_name_is_ambiguous | pull | core | 0 | api | yes |
| given_name_is_ambiguous | push_breadcrumb | core | 0 | api | no |
| given_name_is_ambiguous | push_breadcrumb_pull | core | 0 | api | yes |
| given_name_is_ambiguous | push_pages_upper_bound | core | 0 | api | yes |
| given_name_is_ambiguous | push_sections_pull | core | 0 | api | yes |
| invoice_role_merge | native_grep | core | 0 | api | no |
| invoice_role_merge | native_index | core | 0 | api | yes |
| invoice_role_merge | none | core | 0 | api | no |
| invoice_role_merge | oracle | core | 0 | api | yes |
| invoice_role_merge | pull | core | 0 | api | yes |
| invoice_role_merge | push_breadcrumb | core | 0 | api | no |
| invoice_role_merge | push_breadcrumb_pull | core | 0 | api | yes |
| invoice_role_merge | push_pages_upper_bound | core | 0 | api | no |
| invoice_role_merge | push_sections_pull | core | 0 | api | yes |
| invoiceref_rename | native_grep | core | 0 | api | no |
| invoiceref_rename | native_index | core | 0 | api | no |
| invoiceref_rename | none | core | 0 | api | no |
| invoiceref_rename | oracle | core | 0 | api | yes |
| invoiceref_rename | pull | core | 0 | api | no |
| invoiceref_rename | push_breadcrumb | core | 0 | api | no |
| invoiceref_rename | push_breadcrumb_pull | core | 0 | api | no |
| invoiceref_rename | push_pages_upper_bound | core | 0 | api | no |
| invoiceref_rename | push_sections_pull | core | 0 | api | no |
| keelbridge_programme_scope | native_grep | core | 0 | api | yes |
| keelbridge_programme_scope | native_index | core | 0 | api | yes |
| keelbridge_programme_scope | none | core | 0 | api | no |
| keelbridge_programme_scope | oracle | core | 0 | api | yes |
| keelbridge_programme_scope | pull | core | 0 | api | yes |
| keelbridge_programme_scope | push_breadcrumb | core | 0 | api | no |
| keelbridge_programme_scope | push_breadcrumb_pull | core | 0 | api | no |
| keelbridge_programme_scope | push_pages_upper_bound | core | 0 | api | no |
| keelbridge_programme_scope | push_sections_pull | core | 0 | api | yes |
| lighthouse_migration_rollback | native_grep | core | 0 | api | yes |
| lighthouse_migration_rollback | native_index | core | 0 | api | yes |
| lighthouse_migration_rollback | none | core | 0 | api | no |
| lighthouse_migration_rollback | oracle | core | 0 | api | yes |
| lighthouse_migration_rollback | pull | core | 0 | api | yes |
| lighthouse_migration_rollback | push_breadcrumb | core | 0 | api | no |
| lighthouse_migration_rollback | push_breadcrumb_pull | core | 0 | api | yes |
| lighthouse_migration_rollback | push_pages_upper_bound | core | 0 | api | no |
| lighthouse_migration_rollback | push_sections_pull | core | 0 | api | yes |
| migration_spot_check_cut | native_grep | core | 0 | api | no |
| migration_spot_check_cut | native_index | core | 0 | api | yes |
| migration_spot_check_cut | none | core | 0 | api | no |
| migration_spot_check_cut | oracle | core | 0 | api | yes |
| migration_spot_check_cut | pull | core | 0 | api | yes |
| migration_spot_check_cut | push_breadcrumb | core | 0 | api | no |
| migration_spot_check_cut | push_breadcrumb_pull | core | 0 | api | no |
| migration_spot_check_cut | push_pages_upper_bound | core | 0 | api | no |
| migration_spot_check_cut | push_sections_pull | core | 0 | api | yes |
| mira_castellane_role | native_grep | core | 0 | api | no |
| mira_castellane_role | native_index | core | 0 | api | no |
| mira_castellane_role | none | core | 0 | api | no |
| mira_castellane_role | oracle | core | 0 | api | no |
| mira_castellane_role | pull | core | 0 | api | no |
| mira_castellane_role | push_breadcrumb | core | 0 | api | no |
| mira_castellane_role | push_breadcrumb_pull | core | 0 | api | no |
| mira_castellane_role | push_pages_upper_bound | core | 0 | api | yes |
| mira_castellane_role | push_sections_pull | core | 0 | api | no |
| nightly_job_drop | native_grep | core | 0 | api | no |
| nightly_job_drop | native_index | core | 0 | api | yes |
| nightly_job_drop | none | core | 0 | api | no |
| nightly_job_drop | oracle | core | 0 | api | yes |
| nightly_job_drop | pull | core | 0 | api | no |
| nightly_job_drop | push_breadcrumb | core | 0 | api | no |
| nightly_job_drop | push_breadcrumb_pull | core | 0 | api | no |
| nightly_job_drop | push_pages_upper_bound | core | 0 | api | no |
| nightly_job_drop | push_sections_pull | core | 0 | api | yes |
| office_address_current | native_grep | core | 0 | api | yes |
| office_address_current | native_index | core | 0 | api | no |
| office_address_current | none | core | 0 | api | no |
| office_address_current | oracle | core | 0 | api | yes |
| office_address_current | pull | core | 0 | api | yes |
| office_address_current | push_breadcrumb | core | 0 | api | no |
| office_address_current | push_breadcrumb_pull | core | 0 | api | yes |
| office_address_current | push_pages_upper_bound | core | 0 | api | yes |
| office_address_current | push_sections_pull | core | 0 | api | yes |
| onboarding_length | native_grep | core | 0 | api | yes |
| onboarding_length | native_index | core | 0 | api | yes |
| onboarding_length | none | core | 0 | api | no |
| onboarding_length | oracle | core | 0 | api | yes |
| onboarding_length | pull | core | 0 | api | yes |
| onboarding_length | push_breadcrumb | core | 0 | api | no |
| onboarding_length | push_breadcrumb_pull | core | 0 | api | yes |
| onboarding_length | push_pages_upper_bound | core | 0 | api | yes |
| onboarding_length | push_sections_pull | core | 0 | api | yes |
| person_not_repo | native_grep | core | 0 | api | yes |
| person_not_repo | native_index | core | 0 | api | no |
| person_not_repo | none | core | 0 | api | no |
| person_not_repo | oracle | core | 0 | api | yes |
| person_not_repo | pull | core | 0 | api | no |
| person_not_repo | push_breadcrumb | core | 0 | api | no |
| person_not_repo | push_breadcrumb_pull | core | 0 | api | no |
| person_not_repo | push_pages_upper_bound | core | 0 | api | no |
| person_not_repo | push_sections_pull | core | 0 | api | yes |
| pinemarsh_goal_rationale | native_grep | core | 0 | api | yes |
| pinemarsh_goal_rationale | native_index | core | 0 | api | yes |
| pinemarsh_goal_rationale | none | core | 0 | api | no |
| pinemarsh_goal_rationale | oracle | core | 0 | api | yes |
| pinemarsh_goal_rationale | pull | core | 0 | api | yes |
| pinemarsh_goal_rationale | push_breadcrumb | core | 0 | api | no |
| pinemarsh_goal_rationale | push_breadcrumb_pull | core | 0 | api | yes |
| pinemarsh_goal_rationale | push_pages_upper_bound | core | 0 | api | no |
| pinemarsh_goal_rationale | push_sections_pull | core | 0 | api | yes |
| portal_design_reviewer | native_grep | core | 0 | api | yes |
| portal_design_reviewer | native_index | core | 0 | api | yes |
| portal_design_reviewer | none | core | 0 | api | no |
| portal_design_reviewer | oracle | core | 0 | api | yes |
| portal_design_reviewer | pull | core | 0 | api | yes |
| portal_design_reviewer | push_breadcrumb | core | 0 | api | no |
| portal_design_reviewer | push_breadcrumb_pull | core | 0 | api | yes |
| portal_design_reviewer | push_pages_upper_bound | core | 0 | api | no |
| portal_design_reviewer | push_sections_pull | core | 0 | api | yes |
| pto_allowance | native_grep | core | 0 | api | yes |
| pto_allowance | native_index | core | 0 | api | no |
| pto_allowance | none | core | 0 | api | no |
| pto_allowance | oracle | core | 0 | api | yes |
| pto_allowance | pull | core | 0 | api | yes |
| pto_allowance | push_breadcrumb | core | 0 | api | no |
| pto_allowance | push_breadcrumb_pull | core | 0 | api | yes |
| pto_allowance | push_pages_upper_bound | core | 0 | api | yes |
| pto_allowance | push_sections_pull | core | 0 | api | yes |
| rate_limit_removal | native_grep | core | 0 | api | yes |
| rate_limit_removal | native_index | core | 0 | api | yes |
| rate_limit_removal | none | core | 0 | api | no |
| rate_limit_removal | oracle | core | 0 | api | yes |
| rate_limit_removal | pull | core | 0 | api | no |
| rate_limit_removal | push_breadcrumb | core | 0 | api | no |
| rate_limit_removal | push_breadcrumb_pull | core | 0 | api | yes |
| rate_limit_removal | push_pages_upper_bound | core | 0 | api | no |
| rate_limit_removal | push_sections_pull | core | 0 | api | yes |
| ratecard_tooling_owner | native_grep | core | 0 | api | no |
| ratecard_tooling_owner | native_index | core | 0 | api | no |
| ratecard_tooling_owner | none | core | 0 | api | no |
| ratecard_tooling_owner | oracle | core | 0 | api | yes |
| ratecard_tooling_owner | pull | core | 0 | api | no |
| ratecard_tooling_owner | push_breadcrumb | core | 0 | api | no |
| ratecard_tooling_owner | push_breadcrumb_pull | core | 0 | api | no |
| ratecard_tooling_owner | push_pages_upper_bound | core | 0 | api | no |
| ratecard_tooling_owner | push_sections_pull | core | 0 | api | no |
| relay_sync_fix | native_grep | core | 0 | api | no |
| relay_sync_fix | native_index | core | 0 | api | yes |
| relay_sync_fix | none | core | 0 | api | no |
| relay_sync_fix | oracle | core | 0 | api | yes |
| relay_sync_fix | pull | core | 0 | api | no |
| relay_sync_fix | push_breadcrumb | core | 0 | api | no |
| relay_sync_fix | push_breadcrumb_pull | core | 0 | api | no |
| relay_sync_fix | push_pages_upper_bound | core | 0 | api | no |
| relay_sync_fix | push_sections_pull | core | 0 | api | no |
| remote_equipment_stipend_cap | native_grep | core | 0 | api | yes |
| remote_equipment_stipend_cap | native_index | core | 0 | api | yes |
| remote_equipment_stipend_cap | none | core | 0 | api | no |
| remote_equipment_stipend_cap | oracle | core | 0 | api | yes |
| remote_equipment_stipend_cap | pull | core | 0 | api | yes |
| remote_equipment_stipend_cap | push_breadcrumb | core | 0 | api | no |
| remote_equipment_stipend_cap | push_breadcrumb_pull | core | 0 | api | yes |
| remote_equipment_stipend_cap | push_pages_upper_bound | core | 0 | api | no |
| remote_equipment_stipend_cap | push_sections_pull | core | 0 | api | yes |
| repo_not_person | native_grep | core | 0 | api | yes |
| repo_not_person | native_index | core | 0 | api | yes |
| repo_not_person | none | core | 0 | api | no |
| repo_not_person | oracle | core | 0 | api | yes |
| repo_not_person | pull | core | 0 | api | no |
| repo_not_person | push_breadcrumb | core | 0 | api | no |
| repo_not_person | push_breadcrumb_pull | core | 0 | api | no |
| repo_not_person | push_pages_upper_bound | core | 0 | api | no |
| repo_not_person | push_sections_pull | core | 0 | api | yes |
| spend_approver_named | native_grep | core | 0 | api | yes |
| spend_approver_named | native_index | core | 0 | api | yes |
| spend_approver_named | none | core | 0 | api | no |
| spend_approver_named | oracle | core | 0 | api | yes |
| spend_approver_named | pull | core | 0 | api | yes |
| spend_approver_named | push_breadcrumb | core | 0 | api | no |
| spend_approver_named | push_breadcrumb_pull | core | 0 | api | yes |
| spend_approver_named | push_pages_upper_bound | core | 0 | api | no |
| spend_approver_named | push_sections_pull | core | 0 | api | yes |
| standup_time_current | native_grep | core | 0 | api | no |
| standup_time_current | native_index | core | 0 | api | no |
| standup_time_current | none | core | 0 | api | no |
| standup_time_current | oracle | core | 0 | api | yes |
| standup_time_current | pull | core | 0 | api | yes |
| standup_time_current | push_breadcrumb | core | 0 | api | no |
| standup_time_current | push_breadcrumb_pull | core | 0 | api | yes |
| standup_time_current | push_pages_upper_bound | core | 0 | api | yes |
| standup_time_current | push_sections_pull | core | 0 | api | yes |
| subsidiary_onboarding_lesson | native_grep | core | 0 | api | yes |
| subsidiary_onboarding_lesson | native_index | core | 0 | api | yes |
| subsidiary_onboarding_lesson | none | core | 0 | api | no |
| subsidiary_onboarding_lesson | oracle | core | 0 | api | yes |
| subsidiary_onboarding_lesson | pull | core | 0 | api | yes |
| subsidiary_onboarding_lesson | push_breadcrumb | core | 0 | api | no |
| subsidiary_onboarding_lesson | push_breadcrumb_pull | core | 0 | api | yes |
| subsidiary_onboarding_lesson | push_pages_upper_bound | core | 0 | api | no |
| subsidiary_onboarding_lesson | push_sections_pull | core | 0 | api | yes |
| surname_is_ambiguous | native_grep | core | 0 | api | no |
| surname_is_ambiguous | native_index | core | 0 | api | yes |
| surname_is_ambiguous | none | core | 0 | api | no |
| surname_is_ambiguous | oracle | core | 0 | api | yes |
| surname_is_ambiguous | pull | core | 0 | api | yes |
| surname_is_ambiguous | push_breadcrumb | core | 0 | api | no |
| surname_is_ambiguous | push_breadcrumb_pull | core | 0 | api | no |
| surname_is_ambiguous | push_pages_upper_bound | core | 0 | api | no |
| surname_is_ambiguous | push_sections_pull | core | 0 | api | yes |
| tamsin_ferro_role_change | native_grep | core | 0 | api | yes |
| tamsin_ferro_role_change | native_index | core | 0 | api | yes |
| tamsin_ferro_role_change | none | core | 0 | api | no |
| tamsin_ferro_role_change | oracle | core | 0 | api | yes |
| tamsin_ferro_role_change | pull | core | 0 | api | yes |
| tamsin_ferro_role_change | push_breadcrumb | core | 0 | api | no |
| tamsin_ferro_role_change | push_breadcrumb_pull | core | 0 | api | yes |
| tamsin_ferro_role_change | push_pages_upper_bound | core | 0 | api | yes |
| tamsin_ferro_role_change | push_sections_pull | core | 0 | api | yes |
| thorncastle_first_contact | native_grep | core | 0 | api | yes |
| thorncastle_first_contact | native_index | core | 0 | api | yes |
| thorncastle_first_contact | none | core | 0 | api | no |
| thorncastle_first_contact | oracle | core | 0 | api | yes |
| thorncastle_first_contact | pull | core | 0 | api | yes |
| thorncastle_first_contact | push_breadcrumb | core | 0 | api | no |
| thorncastle_first_contact | push_breadcrumb_pull | core | 0 | api | yes |
| thorncastle_first_contact | push_pages_upper_bound | core | 0 | api | yes |
| thorncastle_first_contact | push_sections_pull | core | 0 | api | yes |
| tls_library_bump | native_grep | core | 0 | api | no |
| tls_library_bump | native_index | core | 0 | api | yes |
| tls_library_bump | none | core | 0 | api | no |
| tls_library_bump | oracle | core | 0 | api | yes |
| tls_library_bump | pull | core | 0 | api | no |
| tls_library_bump | push_breadcrumb | core | 0 | api | no |
| tls_library_bump | push_breadcrumb_pull | core | 0 | api | yes |
| tls_library_bump | push_pages_upper_bound | core | 0 | api | no |
| tls_library_bump | push_sections_pull | core | 0 | api | no |
| triform_vendor_consolidation | native_grep | core | 0 | api | yes |
| triform_vendor_consolidation | native_index | core | 0 | api | yes |
| triform_vendor_consolidation | none | core | 0 | api | no |
| triform_vendor_consolidation | oracle | core | 0 | api | yes |
| triform_vendor_consolidation | pull | core | 0 | api | yes |
| triform_vendor_consolidation | push_breadcrumb | core | 0 | api | no |
| triform_vendor_consolidation | push_breadcrumb_pull | core | 0 | api | yes |
| triform_vendor_consolidation | push_pages_upper_bound | core | 0 | api | no |
| triform_vendor_consolidation | push_sections_pull | core | 0 | api | yes |
| webhook_backoff_lower | native_grep | core | 0 | api | no |
| webhook_backoff_lower | native_index | core | 0 | api | yes |
| webhook_backoff_lower | none | core | 0 | api | no |
| webhook_backoff_lower | oracle | core | 0 | api | yes |
| webhook_backoff_lower | pull | core | 0 | api | no |
| webhook_backoff_lower | push_breadcrumb | core | 0 | api | no |
| webhook_backoff_lower | push_breadcrumb_pull | core | 0 | api | yes |
| webhook_backoff_lower | push_pages_upper_bound | core | 0 | api | no |
| webhook_backoff_lower | push_sections_pull | core | 0 | api | no |

## PULL no-call rate (first-class result)

`no_call_rate = count(recall_called=False) / count(PULL rollouts)`, per (probe_class, corpus_scale). The thesis under test predicts a non-trivial rate; a rate near 0.0 across every group means the free data do NOT evidence it.

| probe_class | corpus_scale | n | no_call_rate |
| --- | --- | --- | --- |
| abstention | core | 3 | 0.000 |
| disambiguation | core | 4 | 0.000 |
| distractor_robustness | core | 2 | 0.000 |
| follow_through | core | 6 | 0.000 |
| multi_hop | core | 2 | 0.000 |
| redundancy | core | 4 | 0.000 |
| single_hop | core | 8 | 0.000 |
| temporal | core | 6 | 0.000 |
| unprompted_push | core | 12 | 0.083 |

## Cost per correct answer (athenaeum#1734)

`cost_per_correct = (read_input_tokens + read_output_tokens + write_tokens_amortized) / correct_n` -- read cost only in Phase 1. Phase 2 adds write cost amortised over the full probe set at that scale (design doc §6); for a non-native arm `write_tokens_amortized` is the native writer's own amortised share PLUS the librarian's filing share (athenaeum#1870, ruling athenaeum#1830), never the filing share alone -- `write_native_share_amortized` prints that native portion separately so neither component is hidden, and the raw (unamortised) sum is printed alongside. `n/a` in the native-share columns means either no write data at all, or (for a native arm) that the whole combined figure already is native, so there is no separate share to print; `not-captured` means an Athenaeum arm's native share specifically was never captured, so the figure shown is filing-only -- see the design doc. `undefined` (never `inf`/`0`) means zero correct answers in the cell -- there is no denominator.

`cache_weighted_tokens` (athenaeum#2305) folds cache activity into `cost_per_correct`'s numerator at the same multipliers athenaeum#239 already bills real cache activity at (a cache write = 1.25x a plain input token, a cache read = 0.10x) -- counting a PUSH/ORACLE arm's injected context on EVERY later turn it stays resident in the model's context, which a plain input+output-tokens view would otherwise price at zero past the first turn. `n/a` means no row in this group ever recorded cache usage (a pre-athenaeum#2305 row), never a measured zero.

`injected_bytes` (athenaeum#2305) is the summed UTF-8 byte length of the content actually injected for this cell's rows (PUSH/ORACLE arms only) -- reported alongside cost, never folded into `cost_per_correct`'s arithmetic. `n/a` means no row in this group recorded it.

| probe_class | corpus_scale | arm | n | correct_n | read_tokens | cache_weighted_tokens | cost_per_correct | injected_bytes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | 1 | 4531 | 0.0 | 4531.0 | n/a |
| abstention | core | native_index | 3 | 3 | 35475 | 0.0 | 11825.0 | n/a |
| abstention | core | none | 3 | 3 | 787 | 0.0 | 262.3 | n/a |
| abstention | core | oracle | 3 | 3 | 883 | 0.0 | 294.3 | 0 |
| abstention | core | pull | 3 | 3 | 76153 | 0.0 | 25384.3 | n/a |
| abstention | core | push_breadcrumb | 3 | 3 | 1499 | 0.0 | 499.7 | 1731 |
| abstention | core | push_breadcrumb_pull | 2 | 2 | 52813 | 0.0 | 26406.5 | 1057 |
| abstention | core | push_pages_upper_bound | 3 | 3 | 3491 | 0.0 | 1163.7 | 8818 |
| abstention | core | push_sections_pull | 2 | 2 | 42079 | 0.0 | 21039.5 | 4050 |
| disambiguation | core | native_grep | 4 | 3 | 22394 | 0.0 | 7464.7 | n/a |
| disambiguation | core | native_index | 4 | 2 | 99712 | 0.0 | 49856.0 | n/a |
| disambiguation | core | none | 4 | 0 | 1136 | 0.0 | undefined | n/a |
| disambiguation | core | oracle | 4 | 4 | 3757 | 0.0 | 939.2 | 6794 |
| disambiguation | core | pull | 4 | 4 | 23549 | 0.0 | 5887.2 | n/a |
| disambiguation | core | push_breadcrumb | 4 | 0 | 2192 | 0.0 | undefined | 2214 |
| disambiguation | core | push_breadcrumb_pull | 4 | 4 | 33387 | 0.0 | 8346.8 | 2214 |
| disambiguation | core | push_pages_upper_bound | 4 | 4 | 5431 | 0.0 | 1357.8 | 11997 |
| disambiguation | core | push_sections_pull | 4 | 4 | 20786 | 0.0 | 5196.5 | 9488 |
| distractor_robustness | core | native_grep | 2 | 1 | 13111 | 0.0 | 13111.0 | n/a |
| distractor_robustness | core | native_index | 2 | 0 | 37992 | 0.0 | undefined | n/a |
| distractor_robustness | core | none | 2 | 0 | 599 | 0.0 | undefined | n/a |
| distractor_robustness | core | oracle | 2 | 2 | 1423 | 0.0 | 711.5 | 2376 |
| distractor_robustness | core | pull | 2 | 1 | 11200 | 0.0 | 11200.0 | n/a |
| distractor_robustness | core | push_breadcrumb | 2 | 0 | 1075 | 0.0 | undefined | 1160 |
| distractor_robustness | core | push_breadcrumb_pull | 2 | 2 | 13756 | 0.0 | 6878.0 | 1160 |
| distractor_robustness | core | push_pages_upper_bound | 2 | 2 | 2478 | 0.0 | 1239.0 | 5725 |
| distractor_robustness | core | push_sections_pull | 2 | 2 | 9811 | 0.0 | 4905.5 | 5054 |
| follow_through | core | native_grep | 6 | 5 | 39175 | 0.0 | 7835.0 | n/a |
| follow_through | core | native_index | 6 | 5 | 164523 | 0.0 | 32904.6 | n/a |
| follow_through | core | none | 6 | 0 | 2012 | 0.0 | undefined | n/a |
| follow_through | core | oracle | 6 | 6 | 4202 | 0.0 | 700.3 | 6273 |
| follow_through | core | pull | 6 | 6 | 110467 | 0.0 | 18411.2 | n/a |
| follow_through | core | push_breadcrumb | 6 | 0 | 3447 | 0.0 | undefined | 4004 |
| follow_through | core | push_breadcrumb_pull | 6 | 5 | 134025 | 0.0 | 26805.0 | 4004 |
| follow_through | core | push_pages_upper_bound | 6 | 0 | 7427 | 0.0 | undefined | 17676 |
| follow_through | core | push_sections_pull | 6 | 4 | 58580 | 0.0 | 14645.0 | 16105 |
| multi_hop | core | native_grep | 3 | 2 | 32850 | 0.0 | 16425.0 | n/a |
| multi_hop | core | native_index | 3 | 2 | 80486 | 0.0 | 40243.0 | n/a |
| multi_hop | core | none | 3 | 0 | 902 | 0.0 | undefined | n/a |
| multi_hop | core | oracle | 3 | 3 | 2547 | 0.0 | 849.0 | 4079 |
| multi_hop | core | pull | 2 | 1 | 47972 | 0.0 | 47972.0 | n/a |
| multi_hop | core | push_breadcrumb | 3 | 0 | 1576 | 0.0 | undefined | 1746 |
| multi_hop | core | push_breadcrumb_pull | 3 | 2 | 89994 | 0.0 | 44997.0 | 1746 |
| multi_hop | core | push_pages_upper_bound | 3 | 0 | 3525 | 0.0 | undefined | 8465 |
| multi_hop | core | push_sections_pull | 3 | 2 | 43215 | 0.0 | 21607.5 | 6439 |
| redundancy | core | native_grep | 4 | 1 | 16422 | 0.0 | 16422.0 | n/a |
| redundancy | core | native_index | 4 | 0 | 98507 | 0.0 | undefined | n/a |
| redundancy | core | none | 4 | 0 | 1139 | 0.0 | undefined | n/a |
| redundancy | core | oracle | 4 | 3 | 2795 | 0.0 | 931.7 | 5303 |
| redundancy | core | pull | 4 | 2 | 31483 | 0.0 | 15741.5 | n/a |
| redundancy | core | push_breadcrumb | 4 | 0 | 2146 | 0.0 | undefined | 2577 |
| redundancy | core | push_breadcrumb_pull | 4 | 3 | 33007 | 0.0 | 11002.3 | 2577 |
| redundancy | core | push_pages_upper_bound | 4 | 2 | 4675 | 0.0 | 2337.5 | 11853 |
| redundancy | core | push_sections_pull | 4 | 2 | 19266 | 0.0 | 9633.0 | 8577 |
| single_hop | core | native_grep | 8 | 7 | 35709 | 0.0 | 5101.3 | n/a |
| single_hop | core | native_index | 8 | 7 | 190867 | 0.0 | 27266.7 | n/a |
| single_hop | core | none | 8 | 0 | 2348 | 0.0 | undefined | n/a |
| single_hop | core | oracle | 8 | 5 | 5764 | 0.0 | 1152.8 | 10854 |
| single_hop | core | pull | 8 | 7 | 65292 | 0.0 | 9327.4 | n/a |
| single_hop | core | push_breadcrumb | 8 | 0 | 4246 | 0.0 | undefined | 4934 |
| single_hop | core | push_breadcrumb_pull | 8 | 7 | 75048 | 0.0 | 10721.1 | 4934 |
| single_hop | core | push_pages_upper_bound | 8 | 3 | 9005 | 0.0 | 3001.7 | 21580 |
| single_hop | core | push_sections_pull | 8 | 8 | 41305 | 0.0 | 5163.1 | 22793 |
| temporal | core | native_grep | 6 | 5 | 25005 | 0.0 | 5001.0 | n/a |
| temporal | core | native_index | 6 | 3 | 148776 | 0.0 | 49592.0 | n/a |
| temporal | core | none | 6 | 0 | 1617 | 0.0 | undefined | n/a |
| temporal | core | oracle | 6 | 6 | 2725 | 0.0 | 454.2 | 3232 |
| temporal | core | pull | 6 | 5 | 34184 | 0.0 | 6836.8 | n/a |
| temporal | core | push_breadcrumb | 6 | 0 | 3156 | 0.0 | undefined | 3657 |
| temporal | core | push_breadcrumb_pull | 6 | 6 | 43049 | 0.0 | 7174.8 | 3657 |
| temporal | core | push_pages_upper_bound | 6 | 6 | 6612 | 0.0 | 1102.0 | 15489 |
| temporal | core | push_sections_pull | 6 | 6 | 27723 | 0.0 | 4620.5 | 8977 |
| unprompted_push | core | native_grep | 12 | 1 | 78744 | 0.0 | 78744.0 | n/a |
| unprompted_push | core | native_index | 12 | 4 | 383567 | 0.0 | 95891.8 | n/a |
| unprompted_push | core | none | 12 | 0 | 4514 | 0.0 | undefined | n/a |
| unprompted_push | core | oracle | 12 | 4 | 10468 | 0.0 | 2617.0 | 18670 |
| unprompted_push | core | pull | 12 | 1 | 183317 | 0.0 | 183317.0 | n/a |
| unprompted_push | core | push_breadcrumb | 12 | 0 | 7731 | 0.0 | undefined | 8228 |
| unprompted_push | core | push_breadcrumb_pull | 12 | 2 | 222094 | 0.0 | 111047.0 | 8228 |
| unprompted_push | core | push_pages_upper_bound | 12 | 0 | 17029 | 0.0 | undefined | 40703 |
| unprompted_push | core | push_sections_pull | 12 | 2 | 126934 | 0.0 | 63467.0 | 27837 |

## Query quality

`lexical_overlap(a, b) = |terms(a) ∩ terms(b)| / |terms(a) ∪ terms(b)|` (Jaccard) — `terms()` is lowercased alphanumeric tokens of length >= 3, minus a short stopword list; denominator is the union of both term sets, 0.0 when either side is empty. `self_query_overlap` is measured between the agent's own recall-tool query and the target (ground-truth) page text; `topic_query_overlap` is the SAME formula against the same target, but using the probe's original query text verbatim as a zero-cost baseline (no extraction step is run to produce it — it already exists).

| probe_class | corpus_scale | n | self_query_overlap | topic_query_overlap |
| --- | --- | --- | --- | --- |
| abstention | core | 3 | 0.000 | 0.000 |
| disambiguation | core | 4 | 0.028 | 0.025 |
| distractor_robustness | core | 2 | 0.029 | 0.030 |
| follow_through | core | 6 | 0.059 | 0.054 |
| multi_hop | core | 2 | 0.048 | 0.053 |
| redundancy | core | 4 | 0.043 | 0.051 |
| single_hop | core | 8 | 0.042 | 0.057 |
| temporal | core | 6 | 0.072 | 0.068 |
| unprompted_push | core | 12 | 0.047 | 0.130 |

## Cost (per turn, not per task)

`mean_injected_context_tokens` (PUSH/ORACLE) is counted whether the delivered pages were used or not — that is the asymmetry against PULL, which pays only on a turn where it actually calls recall. Read the two columns side by side, never in isolation.

| probe_class | corpus_scale | arm | n | mean_input_tokens/turn | mean_output_tokens/turn | mean_injected_context_tokens |
| --- | --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | 1056.2 | 76.5 | n/a |
| abstention | core | native_index | 3 | 5829.0 | 83.5 | n/a |
| abstention | core | none | 3 | 213.7 | 48.7 | n/a |
| abstention | core | oracle | 3 | 213.7 | 80.7 | 0.0 |
| abstention | core | pull | 3 | 4966.5 | 110.4 | n/a |
| abstention | core | push_breadcrumb | 3 | 388.3 | 111.3 | 143.3 |
| abstention | core | push_breadcrumb_pull | 2 | 5753.3 | 114.8 | 131.0 |
| abstention | core | push_pages_upper_bound | 3 | 1092.7 | 71.0 | 731.0 |
| abstention | core | push_sections_pull | 2 | 5159.9 | 100.0 | 506.0 |
| disambiguation | core | native_grep | 4 | 1743.8 | 122.4 | n/a |
| disambiguation | core | native_index | 4 | 6515.7 | 131.8 | n/a |
| disambiguation | core | none | 4 | 212.2 | 71.8 | n/a |
| disambiguation | core | oracle | 4 | 779.0 | 160.2 | 424.2 |
| disambiguation | core | pull | 4 | 2814.2 | 129.4 | n/a |
| disambiguation | core | push_breadcrumb | 4 | 395.5 | 152.5 | 137.5 |
| disambiguation | core | push_breadcrumb_pull | 4 | 3562.9 | 146.8 | 137.5 |
| disambiguation | core | push_pages_upper_bound | 4 | 1151.5 | 206.2 | 746.5 |
| disambiguation | core | push_sections_pull | 4 | 3302.5 | 161.8 | 592.5 |
| distractor_robustness | core | native_grep | 2 | 1968.5 | 216.7 | n/a |
| distractor_robustness | core | native_index | 2 | 6187.7 | 144.3 | n/a |
| distractor_robustness | core | none | 2 | 210.0 | 89.5 | n/a |
| distractor_robustness | core | oracle | 2 | 621.0 | 90.5 | 296.5 |
| distractor_robustness | core | pull | 2 | 2690.8 | 109.2 | n/a |
| distractor_robustness | core | push_breadcrumb | 2 | 393.5 | 144.0 | 144.0 |
| distractor_robustness | core | push_breadcrumb_pull | 2 | 3272.2 | 166.8 | 144.0 |
| distractor_robustness | core | push_pages_upper_bound | 2 | 1068.0 | 171.0 | 711.5 |
| distractor_robustness | core | push_sections_pull | 2 | 3136.7 | 133.7 | 631.5 |
| follow_through | core | native_grep | 6 | 1448.0 | 119.0 | n/a |
| follow_through | core | native_index | 6 | 6220.4 | 107.4 | n/a |
| follow_through | core | none | 6 | 229.0 | 106.3 | n/a |
| follow_through | core | oracle | 6 | 567.2 | 133.2 | 261.3 |
| follow_through | core | pull | 6 | 4453.1 | 149.7 | n/a |
| follow_through | core | push_breadcrumb | 6 | 417.0 | 157.5 | 165.8 |
| follow_through | core | push_breadcrumb_pull | 6 | 5434.1 | 150.3 | 165.8 |
| follow_through | core | push_pages_upper_bound | 6 | 1087.5 | 150.3 | 733.0 |
| follow_through | core | push_sections_pull | 6 | 4032.4 | 151.9 | 670.5 |
| multi_hop | core | native_grep | 3 | 2211.8 | 134.6 | n/a |
| multi_hop | core | native_index | 3 | 6576.6 | 130.6 | n/a |
| multi_hop | core | none | 3 | 231.7 | 69.0 | n/a |
| multi_hop | core | oracle | 3 | 690.7 | 158.3 | 339.3 |
| multi_hop | core | pull | 2 | 5197.6 | 132.7 | n/a |
| multi_hop | core | push_breadcrumb | 3 | 404.0 | 121.3 | 144.7 |
| multi_hop | core | push_breadcrumb_pull | 3 | 6278.0 | 150.1 | 144.7 |
| multi_hop | core | push_pages_upper_bound | 3 | 1061.0 | 114.0 | 702.3 |
| multi_hop | core | push_sections_pull | 3 | 4662.6 | 139.1 | 536.3 |
| redundancy | core | native_grep | 4 | 1278.8 | 89.7 | n/a |
| redundancy | core | native_index | 4 | 6085.2 | 71.5 | n/a |
| redundancy | core | none | 4 | 214.8 | 70.0 | n/a |
| redundancy | core | oracle | 4 | 628.2 | 70.5 | 330.8 |
| redundancy | core | pull | 4 | 3060.5 | 87.8 | n/a |
| redundancy | core | push_breadcrumb | 4 | 403.8 | 132.8 | 160.2 |
| redundancy | core | push_breadcrumb_pull | 4 | 3557.9 | 109.6 | 160.2 |
| redundancy | core | push_pages_upper_bound | 4 | 1083.5 | 85.2 | 737.8 |
| redundancy | core | push_sections_pull | 4 | 3134.8 | 76.2 | 535.8 |
| single_hop | core | native_grep | 8 | 1303.3 | 125.1 | n/a |
| single_hop | core | native_index | 8 | 6065.7 | 91.3 | n/a |
| single_hop | core | none | 8 | 215.0 | 78.5 | n/a |
| single_hop | core | oracle | 8 | 566.5 | 154.0 | 338.8 |
| single_hop | core | pull | 8 | 3119.7 | 144.9 | n/a |
| single_hop | core | push_breadcrumb | 8 | 397.6 | 133.1 | 153.2 |
| single_hop | core | push_breadcrumb_pull | 8 | 3596.3 | 156.1 | 153.2 |
| single_hop | core | push_pages_upper_bound | 8 | 1027.4 | 98.2 | 671.0 |
| single_hop | core | push_sections_pull | 8 | 3294.3 | 147.8 | 711.9 |
| temporal | core | native_grep | 6 | 1313.7 | 75.4 | n/a |
| temporal | core | native_index | 6 | 6126.6 | 72.4 | n/a |
| temporal | core | none | 6 | 212.3 | 57.2 | n/a |
| temporal | core | oracle | 6 | 399.2 | 55.0 | 134.3 |
| temporal | core | pull | 6 | 2765.2 | 83.4 | n/a |
| temporal | core | push_breadcrumb | 6 | 399.2 | 126.8 | 151.7 |
| temporal | core | push_breadcrumb_pull | 6 | 3210.9 | 100.5 | 151.7 |
| temporal | core | push_pages_upper_bound | 6 | 1028.8 | 73.2 | 642.3 |
| temporal | core | push_sections_pull | 6 | 3005.3 | 75.0 | 373.5 |
| unprompted_push | core | native_grep | 12 | 1630.1 | 159.6 | n/a |
| unprompted_push | core | native_index | 12 | 6574.0 | 155.3 | n/a |
| unprompted_push | core | none | 12 | 232.7 | 143.5 | n/a |
| unprompted_push | core | oracle | 12 | 663.2 | 209.1 | 388.5 |
| unprompted_push | core | pull | 12 | 4875.4 | 216.7 | n/a |
| unprompted_push | core | push_breadcrumb | 12 | 421.2 | 223.0 | 170.4 |
| unprompted_push | core | push_breadcrumb_pull | 12 | 5627.1 | 217.5 | 170.4 |
| unprompted_push | core | push_pages_upper_bound | 12 | 1179.9 | 239.2 | 843.8 |
| unprompted_push | core | push_sections_pull | 12 | 4469.5 | 231.8 | 579.6 |

## Efficiency

| probe_class | corpus_scale | arm | n | mean_turn_count | mean_tool_call_count |
| --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | 4.00 | 3.00 |
| abstention | core | native_index | 3 | 2.00 | 1.33 |
| abstention | core | none | 3 | 1.00 | 0.00 |
| abstention | core | oracle | 3 | 1.00 | 0.00 |
| abstention | core | pull | 3 | 5.00 | 4.00 |
| abstention | core | push_breadcrumb | 3 | 1.00 | 0.00 |
| abstention | core | push_breadcrumb_pull | 2 | 4.50 | 3.50 |
| abstention | core | push_pages_upper_bound | 3 | 1.00 | 0.00 |
| abstention | core | push_sections_pull | 2 | 4.00 | 3.00 |
| disambiguation | core | native_grep | 4 | 3.00 | 3.25 |
| disambiguation | core | native_index | 4 | 3.75 | 5.75 |
| disambiguation | core | none | 4 | 1.00 | 0.00 |
| disambiguation | core | oracle | 4 | 1.00 | 0.00 |
| disambiguation | core | pull | 4 | 2.00 | 1.00 |
| disambiguation | core | push_breadcrumb | 4 | 1.00 | 0.00 |
| disambiguation | core | push_breadcrumb_pull | 4 | 2.25 | 1.25 |
| disambiguation | core | push_pages_upper_bound | 4 | 1.00 | 0.00 |
| disambiguation | core | push_sections_pull | 4 | 1.50 | 0.50 |
| distractor_robustness | core | native_grep | 2 | 3.00 | 8.50 |
| distractor_robustness | core | native_index | 2 | 3.00 | 3.00 |
| distractor_robustness | core | none | 2 | 1.00 | 0.00 |
| distractor_robustness | core | oracle | 2 | 1.00 | 0.00 |
| distractor_robustness | core | pull | 2 | 2.00 | 1.00 |
| distractor_robustness | core | push_breadcrumb | 2 | 1.00 | 0.00 |
| distractor_robustness | core | push_breadcrumb_pull | 2 | 2.00 | 1.00 |
| distractor_robustness | core | push_pages_upper_bound | 2 | 1.00 | 0.00 |
| distractor_robustness | core | push_sections_pull | 2 | 1.50 | 0.50 |
| follow_through | core | native_grep | 6 | 4.17 | 5.33 |
| follow_through | core | native_index | 6 | 4.33 | 5.00 |
| follow_through | core | none | 6 | 1.00 | 0.00 |
| follow_through | core | oracle | 6 | 1.00 | 0.00 |
| follow_through | core | pull | 6 | 4.00 | 4.33 |
| follow_through | core | push_breadcrumb | 6 | 1.00 | 0.00 |
| follow_through | core | push_breadcrumb_pull | 6 | 4.00 | 4.17 |
| follow_through | core | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| follow_through | core | push_sections_pull | 6 | 2.33 | 2.33 |
| multi_hop | core | native_grep | 3 | 4.67 | 7.33 |
| multi_hop | core | native_index | 3 | 4.00 | 6.33 |
| multi_hop | core | none | 3 | 1.00 | 0.00 |
| multi_hop | core | oracle | 3 | 1.00 | 0.00 |
| multi_hop | core | pull | 2 | 4.50 | 6.00 |
| multi_hop | core | push_breadcrumb | 3 | 1.00 | 0.00 |
| multi_hop | core | push_breadcrumb_pull | 3 | 4.67 | 5.33 |
| multi_hop | core | push_pages_upper_bound | 3 | 1.00 | 0.00 |
| multi_hop | core | push_sections_pull | 3 | 3.00 | 3.00 |
| redundancy | core | native_grep | 4 | 3.00 | 2.75 |
| redundancy | core | native_index | 4 | 4.00 | 3.25 |
| redundancy | core | none | 4 | 1.00 | 0.00 |
| redundancy | core | oracle | 4 | 1.00 | 0.00 |
| redundancy | core | pull | 4 | 2.50 | 1.50 |
| redundancy | core | push_breadcrumb | 4 | 1.00 | 0.00 |
| redundancy | core | push_breadcrumb_pull | 4 | 2.25 | 1.25 |
| redundancy | core | push_pages_upper_bound | 4 | 1.00 | 0.00 |
| redundancy | core | push_sections_pull | 4 | 1.50 | 0.50 |
| single_hop | core | native_grep | 8 | 3.12 | 3.50 |
| single_hop | core | native_index | 8 | 3.88 | 3.12 |
| single_hop | core | none | 8 | 1.00 | 0.00 |
| single_hop | core | oracle | 8 | 1.00 | 0.00 |
| single_hop | core | pull | 8 | 2.50 | 1.62 |
| single_hop | core | push_breadcrumb | 8 | 1.00 | 0.00 |
| single_hop | core | push_breadcrumb_pull | 8 | 2.50 | 1.62 |
| single_hop | core | push_pages_upper_bound | 8 | 1.00 | 0.00 |
| single_hop | core | push_sections_pull | 8 | 1.50 | 0.50 |
| temporal | core | native_grep | 6 | 3.00 | 2.50 |
| temporal | core | native_index | 6 | 4.00 | 3.50 |
| temporal | core | none | 6 | 1.00 | 0.00 |
| temporal | core | oracle | 6 | 1.00 | 0.00 |
| temporal | core | pull | 6 | 2.00 | 1.00 |
| temporal | core | push_breadcrumb | 6 | 1.00 | 0.00 |
| temporal | core | push_breadcrumb_pull | 6 | 2.17 | 1.50 |
| temporal | core | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| temporal | core | push_sections_pull | 6 | 1.50 | 0.50 |
| unprompted_push | core | native_grep | 12 | 3.67 | 6.08 |
| unprompted_push | core | native_index | 12 | 4.75 | 7.83 |
| unprompted_push | core | none | 12 | 1.00 | 0.00 |
| unprompted_push | core | oracle | 12 | 1.00 | 0.00 |
| unprompted_push | core | pull | 12 | 3.00 | 4.58 |
| unprompted_push | core | push_breadcrumb | 12 | 1.00 | 0.00 |
| unprompted_push | core | push_breadcrumb_pull | 12 | 3.17 | 4.58 |
| unprompted_push | core | push_pages_upper_bound | 12 | 1.00 | 0.00 |
| unprompted_push | core | push_sections_pull | 12 | 2.25 | 2.67 |

## Waste

`wasted_page_fraction = 1 - uid_citation_rate` — the fraction of delivered pages whose uid never appears in the answer text, i.e. never referenced anywhere later in the rollout. `wasted_tokens_estimate` distributes the delivered content's estimated token count evenly across delivered uids and sums the wasted share. `n/a` means nothing was delivered for that group (no basis to call anything wasted).

| probe_class | corpus_scale | arm | n | wasted_page_fraction | wasted_tokens_estimate |
| --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | n/a | n/a |
| abstention | core | native_index | 3 | n/a | n/a |
| abstention | core | none | 3 | n/a | n/a |
| abstention | core | oracle | 3 | n/a | n/a |
| abstention | core | pull | 3 | 1.000 | 4165.0 |
| abstention | core | push_breadcrumb | 3 | n/a | n/a |
| abstention | core | push_breadcrumb_pull | 2 | 1.000 | 5416.0 |
| abstention | core | push_pages_upper_bound | 3 | 1.000 | 731.0 |
| abstention | core | push_sections_pull | 2 | 1.000 | 3957.0 |
| disambiguation | core | native_grep | 4 | n/a | n/a |
| disambiguation | core | native_index | 4 | n/a | n/a |
| disambiguation | core | none | 4 | n/a | n/a |
| disambiguation | core | oracle | 4 | 1.000 | 424.2 |
| disambiguation | core | pull | 4 | 1.000 | 734.5 |
| disambiguation | core | push_breadcrumb | 4 | n/a | n/a |
| disambiguation | core | push_breadcrumb_pull | 4 | 1.000 | 1602.0 |
| disambiguation | core | push_pages_upper_bound | 4 | 0.750 | 556.5 |
| disambiguation | core | push_sections_pull | 4 | 1.000 | 966.0 |
| distractor_robustness | core | native_grep | 2 | n/a | n/a |
| distractor_robustness | core | native_index | 2 | n/a | n/a |
| distractor_robustness | core | none | 2 | n/a | n/a |
| distractor_robustness | core | oracle | 2 | 1.000 | 296.5 |
| distractor_robustness | core | pull | 2 | 0.833 | 495.7 |
| distractor_robustness | core | push_breadcrumb | 2 | n/a | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 2 | 1.000 | 1333.0 |
| distractor_robustness | core | push_pages_upper_bound | 2 | 1.000 | 711.5 |
| distractor_robustness | core | push_sections_pull | 2 | 1.000 | 875.5 |
| follow_through | core | native_grep | 6 | n/a | n/a |
| follow_through | core | native_index | 6 | n/a | n/a |
| follow_through | core | none | 6 | n/a | n/a |
| follow_through | core | oracle | 6 | 1.000 | 261.3 |
| follow_through | core | pull | 6 | 1.000 | 2428.7 |
| follow_through | core | push_breadcrumb | 6 | n/a | n/a |
| follow_through | core | push_breadcrumb_pull | 6 | 1.000 | 3689.3 |
| follow_through | core | push_pages_upper_bound | 6 | 1.000 | 733.0 |
| follow_through | core | push_sections_pull | 6 | 1.000 | 1917.0 |
| multi_hop | core | native_grep | 3 | n/a | n/a |
| multi_hop | core | native_index | 3 | n/a | n/a |
| multi_hop | core | none | 3 | n/a | n/a |
| multi_hop | core | oracle | 3 | 1.000 | 339.3 |
| multi_hop | core | pull | 2 | 1.000 | 3453.5 |
| multi_hop | core | push_breadcrumb | 3 | n/a | n/a |
| multi_hop | core | push_breadcrumb_pull | 3 | 1.000 | 5081.3 |
| multi_hop | core | push_pages_upper_bound | 3 | 1.000 | 702.3 |
| multi_hop | core | push_sections_pull | 3 | 1.000 | 2518.0 |
| redundancy | core | native_grep | 4 | n/a | n/a |
| redundancy | core | native_index | 4 | n/a | n/a |
| redundancy | core | none | 4 | n/a | n/a |
| redundancy | core | oracle | 4 | 1.000 | 330.8 |
| redundancy | core | pull | 4 | 1.000 | 993.0 |
| redundancy | core | push_breadcrumb | 4 | n/a | n/a |
| redundancy | core | push_breadcrumb_pull | 4 | 0.975 | 1618.5 |
| redundancy | core | push_pages_upper_bound | 4 | 1.000 | 737.8 |
| redundancy | core | push_sections_pull | 4 | 1.000 | 786.5 |
| single_hop | core | native_grep | 8 | n/a | n/a |
| single_hop | core | native_index | 8 | n/a | n/a |
| single_hop | core | none | 8 | n/a | n/a |
| single_hop | core | oracle | 8 | 1.000 | 338.8 |
| single_hop | core | pull | 8 | 1.000 | 1165.1 |
| single_hop | core | push_breadcrumb | 8 | n/a | n/a |
| single_hop | core | push_breadcrumb_pull | 8 | 1.000 | 1675.2 |
| single_hop | core | push_pages_upper_bound | 8 | 1.000 | 671.0 |
| single_hop | core | push_sections_pull | 8 | 1.000 | 1065.4 |
| temporal | core | native_grep | 6 | n/a | n/a |
| temporal | core | native_index | 6 | n/a | n/a |
| temporal | core | none | 6 | n/a | n/a |
| temporal | core | oracle | 6 | 1.000 | 134.3 |
| temporal | core | pull | 6 | 0.967 | 637.0 |
| temporal | core | push_breadcrumb | 6 | n/a | n/a |
| temporal | core | push_breadcrumb_pull | 6 | 1.000 | 1100.2 |
| temporal | core | push_pages_upper_bound | 6 | 1.000 | 642.3 |
| temporal | core | push_sections_pull | 6 | 1.000 | 641.0 |
| unprompted_push | core | native_grep | 12 | n/a | n/a |
| unprompted_push | core | native_index | 12 | n/a | n/a |
| unprompted_push | core | none | 12 | n/a | n/a |
| unprompted_push | core | oracle | 12 | 1.000 | 388.5 |
| unprompted_push | core | pull | 12 | 0.873 | 3073.0 |
| unprompted_push | core | push_breadcrumb | 12 | n/a | n/a |
| unprompted_push | core | push_breadcrumb_pull | 12 | 0.929 | 3790.0 |
| unprompted_push | core | push_pages_upper_bound | 12 | 0.750 | 630.2 |
| unprompted_push | core | push_sections_pull | 12 | 0.893 | 2200.7 |

## Utilization (free portion only)

`uid_citation_rate` = fraction of delivered uids whose literal uid string appears in the answer text. `distinctive_ngram_overlap` = `|ngrams(delivered) ∩ ngrams(answer)| / |ngrams(delivered)|` over 4-word shingles — denominator is the delivered text's own shingle count. Both are coarse, zero-judgment proxies for "did the answer draw on what was delivered" — the judged per-claim support check is explicitly out of scope for this report.

| probe_class | corpus_scale | arm | n | uid_citation_rate | distinctive_ngram_overlap |
| --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | n/a | n/a |
| abstention | core | native_index | 3 | n/a | n/a |
| abstention | core | none | 3 | n/a | n/a |
| abstention | core | oracle | 3 | n/a | n/a |
| abstention | core | pull | 3 | 0.000 | 0.002 |
| abstention | core | push_breadcrumb | 3 | n/a | 0.069 |
| abstention | core | push_breadcrumb_pull | 2 | 0.000 | 0.001 |
| abstention | core | push_pages_upper_bound | 3 | 0.000 | 0.000 |
| abstention | core | push_sections_pull | 2 | 0.000 | 0.002 |
| disambiguation | core | native_grep | 4 | n/a | n/a |
| disambiguation | core | native_index | 4 | n/a | n/a |
| disambiguation | core | none | 4 | n/a | n/a |
| disambiguation | core | oracle | 4 | 0.000 | 0.114 |
| disambiguation | core | pull | 4 | 0.000 | 0.077 |
| disambiguation | core | push_breadcrumb | 4 | n/a | 0.016 |
| disambiguation | core | push_breadcrumb_pull | 4 | 0.000 | 0.040 |
| disambiguation | core | push_pages_upper_bound | 4 | 0.250 | 0.078 |
| disambiguation | core | push_sections_pull | 4 | 0.000 | 0.092 |
| distractor_robustness | core | native_grep | 2 | n/a | n/a |
| distractor_robustness | core | native_index | 2 | n/a | n/a |
| distractor_robustness | core | none | 2 | n/a | n/a |
| distractor_robustness | core | oracle | 2 | 0.000 | 0.089 |
| distractor_robustness | core | pull | 2 | 0.167 | 0.050 |
| distractor_robustness | core | push_breadcrumb | 2 | n/a | 0.109 |
| distractor_robustness | core | push_breadcrumb_pull | 2 | 0.000 | 0.045 |
| distractor_robustness | core | push_pages_upper_bound | 2 | 0.000 | 0.073 |
| distractor_robustness | core | push_sections_pull | 2 | 0.000 | 0.027 |
| follow_through | core | native_grep | 6 | n/a | n/a |
| follow_through | core | native_index | 6 | n/a | n/a |
| follow_through | core | none | 6 | n/a | n/a |
| follow_through | core | oracle | 6 | 0.000 | 0.186 |
| follow_through | core | pull | 6 | 0.000 | 0.034 |
| follow_through | core | push_breadcrumb | 6 | n/a | 0.021 |
| follow_through | core | push_breadcrumb_pull | 6 | 0.000 | 0.019 |
| follow_through | core | push_pages_upper_bound | 6 | 0.000 | 0.037 |
| follow_through | core | push_sections_pull | 6 | 0.000 | 0.033 |
| multi_hop | core | native_grep | 3 | n/a | n/a |
| multi_hop | core | native_index | 3 | n/a | n/a |
| multi_hop | core | none | 3 | n/a | n/a |
| multi_hop | core | oracle | 3 | 0.000 | 0.130 |
| multi_hop | core | pull | 2 | 0.000 | 0.012 |
| multi_hop | core | push_breadcrumb | 3 | n/a | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 3 | 0.000 | 0.013 |
| multi_hop | core | push_pages_upper_bound | 3 | 0.000 | 0.004 |
| multi_hop | core | push_sections_pull | 3 | 0.000 | 0.026 |
| redundancy | core | native_grep | 4 | n/a | n/a |
| redundancy | core | native_index | 4 | n/a | n/a |
| redundancy | core | none | 4 | n/a | n/a |
| redundancy | core | oracle | 4 | 0.000 | 0.070 |
| redundancy | core | pull | 4 | 0.000 | 0.041 |
| redundancy | core | push_breadcrumb | 4 | n/a | 0.006 |
| redundancy | core | push_breadcrumb_pull | 4 | 0.025 | 0.018 |
| redundancy | core | push_pages_upper_bound | 4 | 0.000 | 0.032 |
| redundancy | core | push_sections_pull | 4 | 0.000 | 0.038 |
| single_hop | core | native_grep | 8 | n/a | n/a |
| single_hop | core | native_index | 8 | n/a | n/a |
| single_hop | core | none | 8 | n/a | n/a |
| single_hop | core | oracle | 8 | 0.000 | 0.190 |
| single_hop | core | pull | 8 | 0.000 | 0.071 |
| single_hop | core | push_breadcrumb | 8 | n/a | 0.013 |
| single_hop | core | push_breadcrumb_pull | 8 | 0.000 | 0.042 |
| single_hop | core | push_pages_upper_bound | 8 | 0.000 | 0.071 |
| single_hop | core | push_sections_pull | 8 | 0.000 | 0.087 |
| temporal | core | native_grep | 6 | n/a | n/a |
| temporal | core | native_index | 6 | n/a | n/a |
| temporal | core | none | 6 | n/a | n/a |
| temporal | core | oracle | 6 | 0.000 | 0.083 |
| temporal | core | pull | 6 | 0.033 | 0.035 |
| temporal | core | push_breadcrumb | 6 | n/a | 0.029 |
| temporal | core | push_breadcrumb_pull | 6 | 0.000 | 0.020 |
| temporal | core | push_pages_upper_bound | 6 | 0.000 | 0.035 |
| temporal | core | push_sections_pull | 6 | 0.000 | 0.056 |
| unprompted_push | core | native_grep | 12 | n/a | n/a |
| unprompted_push | core | native_index | 12 | n/a | n/a |
| unprompted_push | core | none | 12 | n/a | n/a |
| unprompted_push | core | oracle | 12 | 0.000 | 0.107 |
| unprompted_push | core | pull | 12 | 0.127 | 0.031 |
| unprompted_push | core | push_breadcrumb | 12 | n/a | 0.095 |
| unprompted_push | core | push_breadcrumb_pull | 12 | 0.071 | 0.030 |
| unprompted_push | core | push_pages_upper_bound | 12 | 0.250 | 0.087 |
| unprompted_push | core | push_sections_pull | 12 | 0.107 | 0.049 |

## Correctness (answer ground truth, issue athenaeum#1573)

`correctness_rate` grades the ANSWER, not retrieval: normalized substring match against each probe's planted `answer_tokens` (non-abstention), or the declining-language rule for abstention probes — see `grade_correctness`. No LLM judge. This is what makes NONE (floor) and ORACLE (ceiling) readable as numbers for the first time — every other dimension above describes retrieval or cost, never whether the final answer was actually right. `n/a` means no probe in that group carries ground truth tokens to grade against. Those tokens are the corpus pages' own internal reference tags, so every arm's system prompt carries one identical instruction (issue athenaeum#1753) to end the answer with `[ref: TAG]` for each page relied on, or `[ref: none]` for none — correctness therefore reads as “did the arm reach the right page and say so”. Rows recorded before that contract landed carry no tags and grade at or near 0 for every arm, including ORACLE.

| probe_class | corpus_scale | arm | n | correctness_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | 1.000 |
| abstention | core | native_index | 3 | 1.000 |
| abstention | core | none | 3 | 1.000 |
| abstention | core | oracle | 3 | 1.000 |
| abstention | core | pull | 3 | 1.000 |
| abstention | core | push_breadcrumb | 3 | 1.000 |
| abstention | core | push_breadcrumb_pull | 2 | 1.000 |
| abstention | core | push_pages_upper_bound | 3 | 1.000 |
| abstention | core | push_sections_pull | 2 | 1.000 |
| disambiguation | core | native_grep | 4 | 0.750 |
| disambiguation | core | native_index | 4 | 0.500 |
| disambiguation | core | none | 4 | 0.000 |
| disambiguation | core | oracle | 4 | 1.000 |
| disambiguation | core | pull | 4 | 1.000 |
| disambiguation | core | push_breadcrumb | 4 | 0.000 |
| disambiguation | core | push_breadcrumb_pull | 4 | 1.000 |
| disambiguation | core | push_pages_upper_bound | 4 | 1.000 |
| disambiguation | core | push_sections_pull | 4 | 1.000 |
| distractor_robustness | core | native_grep | 2 | 0.500 |
| distractor_robustness | core | native_index | 2 | 0.000 |
| distractor_robustness | core | none | 2 | 0.000 |
| distractor_robustness | core | oracle | 2 | 1.000 |
| distractor_robustness | core | pull | 2 | 0.500 |
| distractor_robustness | core | push_breadcrumb | 2 | 0.000 |
| distractor_robustness | core | push_breadcrumb_pull | 2 | 1.000 |
| distractor_robustness | core | push_pages_upper_bound | 2 | 1.000 |
| distractor_robustness | core | push_sections_pull | 2 | 1.000 |
| follow_through | core | native_grep | 6 | 0.833 |
| follow_through | core | native_index | 6 | 0.833 |
| follow_through | core | none | 6 | 0.000 |
| follow_through | core | oracle | 6 | 1.000 |
| follow_through | core | pull | 6 | 1.000 |
| follow_through | core | push_breadcrumb | 6 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 6 | 0.833 |
| follow_through | core | push_pages_upper_bound | 6 | 0.000 |
| follow_through | core | push_sections_pull | 6 | 0.667 |
| multi_hop | core | native_grep | 3 | 0.667 |
| multi_hop | core | native_index | 3 | 0.667 |
| multi_hop | core | none | 3 | 0.000 |
| multi_hop | core | oracle | 3 | 1.000 |
| multi_hop | core | pull | 2 | 0.500 |
| multi_hop | core | push_breadcrumb | 3 | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 3 | 0.667 |
| multi_hop | core | push_pages_upper_bound | 3 | 0.000 |
| multi_hop | core | push_sections_pull | 3 | 0.667 |
| redundancy | core | native_grep | 4 | 0.250 |
| redundancy | core | native_index | 4 | 0.000 |
| redundancy | core | none | 4 | 0.000 |
| redundancy | core | oracle | 4 | 0.750 |
| redundancy | core | pull | 4 | 0.500 |
| redundancy | core | push_breadcrumb | 4 | 0.000 |
| redundancy | core | push_breadcrumb_pull | 4 | 0.750 |
| redundancy | core | push_pages_upper_bound | 4 | 0.500 |
| redundancy | core | push_sections_pull | 4 | 0.500 |
| single_hop | core | native_grep | 8 | 0.875 |
| single_hop | core | native_index | 8 | 0.875 |
| single_hop | core | none | 8 | 0.000 |
| single_hop | core | oracle | 8 | 0.625 |
| single_hop | core | pull | 8 | 0.875 |
| single_hop | core | push_breadcrumb | 8 | 0.000 |
| single_hop | core | push_breadcrumb_pull | 8 | 0.875 |
| single_hop | core | push_pages_upper_bound | 8 | 0.375 |
| single_hop | core | push_sections_pull | 8 | 1.000 |
| temporal | core | native_grep | 6 | 0.833 |
| temporal | core | native_index | 6 | 0.500 |
| temporal | core | none | 6 | 0.000 |
| temporal | core | oracle | 6 | 1.000 |
| temporal | core | pull | 6 | 0.833 |
| temporal | core | push_breadcrumb | 6 | 0.000 |
| temporal | core | push_breadcrumb_pull | 6 | 1.000 |
| temporal | core | push_pages_upper_bound | 6 | 1.000 |
| temporal | core | push_sections_pull | 6 | 1.000 |
| unprompted_push | core | native_grep | 12 | 0.083 |
| unprompted_push | core | native_index | 12 | 0.333 |
| unprompted_push | core | none | 12 | 0.000 |
| unprompted_push | core | oracle | 12 | 0.333 |
| unprompted_push | core | pull | 12 | 0.083 |
| unprompted_push | core | push_breadcrumb | 12 | 0.000 |
| unprompted_push | core | push_breadcrumb_pull | 12 | 0.167 |
| unprompted_push | core | push_pages_upper_bound | 12 | 0.000 |
| unprompted_push | core | push_sections_pull | 12 | 0.167 |

## Harm (forbidden-token) rate (issue athenaeum#1772, report_only)

`harm_free_rate` grades whether the answer avoided every one of a probe's planted `forbidden_tokens` -- see `grade_harm`. Same normalizer and substring instrument as `correctness_rate`, no LLM judge. `report_only` (issue athenaeum#1791 §2.1): this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no probe in that group carries `forbidden_tokens` to grade against -- true for every probe class shipped so far, so this section reads `n/a` throughout the current corpus.

| probe_class | corpus_scale | arm | n | harm_free_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | n/a |
| abstention | core | native_index | 3 | n/a |
| abstention | core | none | 3 | n/a |
| abstention | core | oracle | 3 | n/a |
| abstention | core | pull | 3 | n/a |
| abstention | core | push_breadcrumb | 3 | n/a |
| abstention | core | push_breadcrumb_pull | 2 | n/a |
| abstention | core | push_pages_upper_bound | 3 | n/a |
| abstention | core | push_sections_pull | 2 | n/a |
| disambiguation | core | native_grep | 4 | n/a |
| disambiguation | core | native_index | 4 | n/a |
| disambiguation | core | none | 4 | n/a |
| disambiguation | core | oracle | 4 | n/a |
| disambiguation | core | pull | 4 | n/a |
| disambiguation | core | push_breadcrumb | 4 | n/a |
| disambiguation | core | push_breadcrumb_pull | 4 | n/a |
| disambiguation | core | push_pages_upper_bound | 4 | n/a |
| disambiguation | core | push_sections_pull | 4 | n/a |
| distractor_robustness | core | native_grep | 2 | n/a |
| distractor_robustness | core | native_index | 2 | n/a |
| distractor_robustness | core | none | 2 | n/a |
| distractor_robustness | core | oracle | 2 | n/a |
| distractor_robustness | core | pull | 2 | n/a |
| distractor_robustness | core | push_breadcrumb | 2 | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 2 | n/a |
| distractor_robustness | core | push_pages_upper_bound | 2 | n/a |
| distractor_robustness | core | push_sections_pull | 2 | n/a |
| follow_through | core | native_grep | 6 | n/a |
| follow_through | core | native_index | 6 | n/a |
| follow_through | core | none | 6 | n/a |
| follow_through | core | oracle | 6 | n/a |
| follow_through | core | pull | 6 | n/a |
| follow_through | core | push_breadcrumb | 6 | n/a |
| follow_through | core | push_breadcrumb_pull | 6 | n/a |
| follow_through | core | push_pages_upper_bound | 6 | n/a |
| follow_through | core | push_sections_pull | 6 | n/a |
| multi_hop | core | native_grep | 3 | n/a |
| multi_hop | core | native_index | 3 | n/a |
| multi_hop | core | none | 3 | n/a |
| multi_hop | core | oracle | 3 | n/a |
| multi_hop | core | pull | 2 | n/a |
| multi_hop | core | push_breadcrumb | 3 | n/a |
| multi_hop | core | push_breadcrumb_pull | 3 | n/a |
| multi_hop | core | push_pages_upper_bound | 3 | n/a |
| multi_hop | core | push_sections_pull | 3 | n/a |
| redundancy | core | native_grep | 4 | n/a |
| redundancy | core | native_index | 4 | n/a |
| redundancy | core | none | 4 | n/a |
| redundancy | core | oracle | 4 | n/a |
| redundancy | core | pull | 4 | n/a |
| redundancy | core | push_breadcrumb | 4 | n/a |
| redundancy | core | push_breadcrumb_pull | 4 | n/a |
| redundancy | core | push_pages_upper_bound | 4 | n/a |
| redundancy | core | push_sections_pull | 4 | n/a |
| single_hop | core | native_grep | 8 | n/a |
| single_hop | core | native_index | 8 | n/a |
| single_hop | core | none | 8 | n/a |
| single_hop | core | oracle | 8 | n/a |
| single_hop | core | pull | 8 | n/a |
| single_hop | core | push_breadcrumb | 8 | n/a |
| single_hop | core | push_breadcrumb_pull | 8 | n/a |
| single_hop | core | push_pages_upper_bound | 8 | n/a |
| single_hop | core | push_sections_pull | 8 | n/a |
| temporal | core | native_grep | 6 | n/a |
| temporal | core | native_index | 6 | n/a |
| temporal | core | none | 6 | n/a |
| temporal | core | oracle | 6 | n/a |
| temporal | core | pull | 6 | n/a |
| temporal | core | push_breadcrumb | 6 | n/a |
| temporal | core | push_breadcrumb_pull | 6 | n/a |
| temporal | core | push_pages_upper_bound | 6 | n/a |
| temporal | core | push_sections_pull | 6 | n/a |
| unprompted_push | core | native_grep | 12 | 0.167 |
| unprompted_push | core | native_index | 12 | 0.333 |
| unprompted_push | core | none | 12 | 1.000 |
| unprompted_push | core | oracle | 12 | 1.000 |
| unprompted_push | core | pull | 12 | 0.083 |
| unprompted_push | core | push_breadcrumb | 12 | 1.000 |
| unprompted_push | core | push_breadcrumb_pull | 12 | 0.083 |
| unprompted_push | core | push_pages_upper_bound | 12 | 0.167 |
| unprompted_push | core | push_sections_pull | 12 | 0.250 |

## Coverage (fraction of planted tokens) (issue athenaeum#1773, report_only)

`coverage_rate` is the mean fraction of a probe's `answer_tokens` present in the answer -- see `grade_coverage`. Same normalizer and substring instrument as `correctness_rate`, but a ratio rather than an all-or-nothing match, so a many-correct-answer probe (the `aggregation` class) can grade partial credit instead of a forced zero. `report_only` (issue athenaeum#1791 §2.1): this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no probe in that group carries `answer_tokens` to grade against. For a single-`answer_tokens` probe, `coverage_rate` and `correctness_rate` are the same number by construction.

| probe_class | corpus_scale | arm | n | coverage_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | n/a |
| abstention | core | native_index | 3 | n/a |
| abstention | core | none | 3 | n/a |
| abstention | core | oracle | 3 | n/a |
| abstention | core | pull | 3 | n/a |
| abstention | core | push_breadcrumb | 3 | n/a |
| abstention | core | push_breadcrumb_pull | 2 | n/a |
| abstention | core | push_pages_upper_bound | 3 | n/a |
| abstention | core | push_sections_pull | 2 | n/a |
| disambiguation | core | native_grep | 4 | 0.750 |
| disambiguation | core | native_index | 4 | 0.500 |
| disambiguation | core | none | 4 | 0.000 |
| disambiguation | core | oracle | 4 | 1.000 |
| disambiguation | core | pull | 4 | 0.500 |
| disambiguation | core | push_breadcrumb | 4 | 0.000 |
| disambiguation | core | push_breadcrumb_pull | 4 | 0.250 |
| disambiguation | core | push_pages_upper_bound | 4 | 0.250 |
| disambiguation | core | push_sections_pull | 4 | 1.000 |
| distractor_robustness | core | native_grep | 2 | 0.500 |
| distractor_robustness | core | native_index | 2 | 0.000 |
| distractor_robustness | core | none | 2 | 0.000 |
| distractor_robustness | core | oracle | 2 | 1.000 |
| distractor_robustness | core | pull | 2 | 0.500 |
| distractor_robustness | core | push_breadcrumb | 2 | 0.000 |
| distractor_robustness | core | push_breadcrumb_pull | 2 | 1.000 |
| distractor_robustness | core | push_pages_upper_bound | 2 | 1.000 |
| distractor_robustness | core | push_sections_pull | 2 | 1.000 |
| follow_through | core | native_grep | 6 | 0.917 |
| follow_through | core | native_index | 6 | 0.917 |
| follow_through | core | none | 6 | 0.000 |
| follow_through | core | oracle | 6 | 1.000 |
| follow_through | core | pull | 6 | 1.000 |
| follow_through | core | push_breadcrumb | 6 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 6 | 1.000 |
| follow_through | core | push_pages_upper_bound | 6 | 0.500 |
| follow_through | core | push_sections_pull | 6 | 0.917 |
| multi_hop | core | native_grep | 3 | 0.667 |
| multi_hop | core | native_index | 3 | 0.667 |
| multi_hop | core | none | 3 | 0.000 |
| multi_hop | core | oracle | 3 | 1.000 |
| multi_hop | core | pull | 2 | 1.000 |
| multi_hop | core | push_breadcrumb | 3 | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 3 | 0.667 |
| multi_hop | core | push_pages_upper_bound | 3 | 0.000 |
| multi_hop | core | push_sections_pull | 3 | 0.667 |
| redundancy | core | native_grep | 4 | 0.750 |
| redundancy | core | native_index | 4 | 0.625 |
| redundancy | core | none | 4 | 0.000 |
| redundancy | core | oracle | 4 | 0.875 |
| redundancy | core | pull | 4 | 0.625 |
| redundancy | core | push_breadcrumb | 4 | 0.000 |
| redundancy | core | push_breadcrumb_pull | 4 | 0.375 |
| redundancy | core | push_pages_upper_bound | 4 | 0.375 |
| redundancy | core | push_sections_pull | 4 | 0.625 |
| single_hop | core | native_grep | 8 | 1.000 |
| single_hop | core | native_index | 8 | 0.875 |
| single_hop | core | none | 8 | 0.000 |
| single_hop | core | oracle | 8 | 1.000 |
| single_hop | core | pull | 8 | 1.000 |
| single_hop | core | push_breadcrumb | 8 | 0.000 |
| single_hop | core | push_breadcrumb_pull | 8 | 1.000 |
| single_hop | core | push_pages_upper_bound | 8 | 0.500 |
| single_hop | core | push_sections_pull | 8 | 1.000 |
| temporal | core | native_grep | 6 | 0.833 |
| temporal | core | native_index | 6 | 0.500 |
| temporal | core | none | 6 | 0.000 |
| temporal | core | oracle | 6 | 1.000 |
| temporal | core | pull | 6 | 1.000 |
| temporal | core | push_breadcrumb | 6 | 0.000 |
| temporal | core | push_breadcrumb_pull | 6 | 1.000 |
| temporal | core | push_pages_upper_bound | 6 | 1.000 |
| temporal | core | push_sections_pull | 6 | 1.000 |
| unprompted_push | core | native_grep | 12 | 0.167 |
| unprompted_push | core | native_index | 12 | 0.917 |
| unprompted_push | core | none | 12 | 0.000 |
| unprompted_push | core | oracle | 12 | 1.000 |
| unprompted_push | core | pull | 12 | 0.417 |
| unprompted_push | core | push_breadcrumb | 12 | 0.000 |
| unprompted_push | core | push_breadcrumb_pull | 12 | 0.500 |
| unprompted_push | core | push_pages_upper_bound | 12 | 0.000 |
| unprompted_push | core | push_sections_pull | 12 | 0.500 |

## Marker resolution (issue athenaeum#1730, report_only)

`marker_resolution_rate` is the share of answers whose CITED `[^src-N]` footnote markers all resolve to a footnote definition on a page that plants one of the probe's `answer_tokens` -- see `grade_marker_resolution`. It asks whether an answer that quotes a citation quoted a real one, now that compiled pages carry inline per-claim markers and `recall` resolves them on the hit. `report_only`: this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no answer in that group cited a marker, or no token-bearing expected page defines one -- both are 'nothing to grade', never a graded zero.

| probe_class | corpus_scale | arm | n | marker_resolution_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | n/a |
| abstention | core | native_index | 3 | n/a |
| abstention | core | none | 3 | n/a |
| abstention | core | oracle | 3 | n/a |
| abstention | core | pull | 3 | n/a |
| abstention | core | push_breadcrumb | 3 | n/a |
| abstention | core | push_breadcrumb_pull | 2 | n/a |
| abstention | core | push_pages_upper_bound | 3 | n/a |
| abstention | core | push_sections_pull | 2 | n/a |
| disambiguation | core | native_grep | 4 | n/a |
| disambiguation | core | native_index | 4 | n/a |
| disambiguation | core | none | 4 | n/a |
| disambiguation | core | oracle | 4 | n/a |
| disambiguation | core | pull | 4 | n/a |
| disambiguation | core | push_breadcrumb | 4 | n/a |
| disambiguation | core | push_breadcrumb_pull | 4 | n/a |
| disambiguation | core | push_pages_upper_bound | 4 | n/a |
| disambiguation | core | push_sections_pull | 4 | n/a |
| distractor_robustness | core | native_grep | 2 | n/a |
| distractor_robustness | core | native_index | 2 | n/a |
| distractor_robustness | core | none | 2 | n/a |
| distractor_robustness | core | oracle | 2 | n/a |
| distractor_robustness | core | pull | 2 | n/a |
| distractor_robustness | core | push_breadcrumb | 2 | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 2 | n/a |
| distractor_robustness | core | push_pages_upper_bound | 2 | n/a |
| distractor_robustness | core | push_sections_pull | 2 | n/a |
| follow_through | core | native_grep | 6 | n/a |
| follow_through | core | native_index | 6 | n/a |
| follow_through | core | none | 6 | n/a |
| follow_through | core | oracle | 6 | n/a |
| follow_through | core | pull | 6 | n/a |
| follow_through | core | push_breadcrumb | 6 | n/a |
| follow_through | core | push_breadcrumb_pull | 6 | n/a |
| follow_through | core | push_pages_upper_bound | 6 | n/a |
| follow_through | core | push_sections_pull | 6 | n/a |
| multi_hop | core | native_grep | 3 | n/a |
| multi_hop | core | native_index | 3 | n/a |
| multi_hop | core | none | 3 | n/a |
| multi_hop | core | oracle | 3 | n/a |
| multi_hop | core | pull | 2 | n/a |
| multi_hop | core | push_breadcrumb | 3 | n/a |
| multi_hop | core | push_breadcrumb_pull | 3 | n/a |
| multi_hop | core | push_pages_upper_bound | 3 | n/a |
| multi_hop | core | push_sections_pull | 3 | n/a |
| redundancy | core | native_grep | 4 | n/a |
| redundancy | core | native_index | 4 | n/a |
| redundancy | core | none | 4 | n/a |
| redundancy | core | oracle | 4 | n/a |
| redundancy | core | pull | 4 | n/a |
| redundancy | core | push_breadcrumb | 4 | n/a |
| redundancy | core | push_breadcrumb_pull | 4 | n/a |
| redundancy | core | push_pages_upper_bound | 4 | n/a |
| redundancy | core | push_sections_pull | 4 | n/a |
| single_hop | core | native_grep | 8 | n/a |
| single_hop | core | native_index | 8 | n/a |
| single_hop | core | none | 8 | n/a |
| single_hop | core | oracle | 8 | n/a |
| single_hop | core | pull | 8 | n/a |
| single_hop | core | push_breadcrumb | 8 | n/a |
| single_hop | core | push_breadcrumb_pull | 8 | n/a |
| single_hop | core | push_pages_upper_bound | 8 | n/a |
| single_hop | core | push_sections_pull | 8 | n/a |
| temporal | core | native_grep | 6 | n/a |
| temporal | core | native_index | 6 | n/a |
| temporal | core | none | 6 | n/a |
| temporal | core | oracle | 6 | n/a |
| temporal | core | pull | 6 | n/a |
| temporal | core | push_breadcrumb | 6 | n/a |
| temporal | core | push_breadcrumb_pull | 6 | n/a |
| temporal | core | push_pages_upper_bound | 6 | n/a |
| temporal | core | push_sections_pull | 6 | n/a |
| unprompted_push | core | native_grep | 12 | n/a |
| unprompted_push | core | native_index | 12 | n/a |
| unprompted_push | core | none | 12 | n/a |
| unprompted_push | core | oracle | 12 | n/a |
| unprompted_push | core | pull | 12 | n/a |
| unprompted_push | core | push_breadcrumb | 12 | n/a |
| unprompted_push | core | push_breadcrumb_pull | 12 | n/a |
| unprompted_push | core | push_pages_upper_bound | 12 | n/a |
| unprompted_push | core | push_sections_pull | 12 | n/a |

## Reference tag followed (issue athenaeum#1831, report_only)

`tag_followed_rate` is the share of answers that quoted the `[ref: TAG]` `REFERENCE_TAG_INSTRUCTION` asks for, for every one of the probe's planted tags -- see `tag_followed`. This is the athenaeum#1753 correctness rule, DEMOTED by the operator ruling on athenaeum#1791 comment 5732689494: it feeds no §7 condition and no `correctness_rate` above, which is now graded on `answer_markers` (the planted fact) plus delivered-page evidence, never this tag. `n/a` means no probe in that group carries `answer_tokens` to grade against (including every abstention group).

| probe_class | corpus_scale | arm | n | tag_followed_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | n/a |
| abstention | core | native_index | 3 | n/a |
| abstention | core | none | 3 | n/a |
| abstention | core | oracle | 3 | n/a |
| abstention | core | pull | 3 | n/a |
| abstention | core | push_breadcrumb | 3 | n/a |
| abstention | core | push_breadcrumb_pull | 2 | n/a |
| abstention | core | push_pages_upper_bound | 3 | n/a |
| abstention | core | push_sections_pull | 2 | n/a |
| disambiguation | core | native_grep | 4 | 0.750 |
| disambiguation | core | native_index | 4 | 0.500 |
| disambiguation | core | none | 4 | 0.000 |
| disambiguation | core | oracle | 4 | 1.000 |
| disambiguation | core | pull | 4 | 0.500 |
| disambiguation | core | push_breadcrumb | 4 | 0.000 |
| disambiguation | core | push_breadcrumb_pull | 4 | 0.250 |
| disambiguation | core | push_pages_upper_bound | 4 | 0.250 |
| disambiguation | core | push_sections_pull | 4 | 1.000 |
| distractor_robustness | core | native_grep | 2 | 0.500 |
| distractor_robustness | core | native_index | 2 | 0.000 |
| distractor_robustness | core | none | 2 | 0.000 |
| distractor_robustness | core | oracle | 2 | 1.000 |
| distractor_robustness | core | pull | 2 | 0.500 |
| distractor_robustness | core | push_breadcrumb | 2 | 0.000 |
| distractor_robustness | core | push_breadcrumb_pull | 2 | 1.000 |
| distractor_robustness | core | push_pages_upper_bound | 2 | 1.000 |
| distractor_robustness | core | push_sections_pull | 2 | 1.000 |
| follow_through | core | native_grep | 6 | 0.833 |
| follow_through | core | native_index | 6 | 0.833 |
| follow_through | core | none | 6 | 0.000 |
| follow_through | core | oracle | 6 | 1.000 |
| follow_through | core | pull | 6 | 1.000 |
| follow_through | core | push_breadcrumb | 6 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 6 | 1.000 |
| follow_through | core | push_pages_upper_bound | 6 | 0.000 |
| follow_through | core | push_sections_pull | 6 | 0.833 |
| multi_hop | core | native_grep | 3 | 0.667 |
| multi_hop | core | native_index | 3 | 0.667 |
| multi_hop | core | none | 3 | 0.000 |
| multi_hop | core | oracle | 3 | 1.000 |
| multi_hop | core | pull | 2 | 1.000 |
| multi_hop | core | push_breadcrumb | 3 | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 3 | 0.667 |
| multi_hop | core | push_pages_upper_bound | 3 | 0.000 |
| multi_hop | core | push_sections_pull | 3 | 0.667 |
| redundancy | core | native_grep | 4 | 0.500 |
| redundancy | core | native_index | 4 | 0.250 |
| redundancy | core | none | 4 | 0.000 |
| redundancy | core | oracle | 4 | 0.750 |
| redundancy | core | pull | 4 | 0.250 |
| redundancy | core | push_breadcrumb | 4 | 0.000 |
| redundancy | core | push_breadcrumb_pull | 4 | 0.000 |
| redundancy | core | push_pages_upper_bound | 4 | 0.250 |
| redundancy | core | push_sections_pull | 4 | 0.250 |
| single_hop | core | native_grep | 8 | 1.000 |
| single_hop | core | native_index | 8 | 0.875 |
| single_hop | core | none | 8 | 0.000 |
| single_hop | core | oracle | 8 | 1.000 |
| single_hop | core | pull | 8 | 1.000 |
| single_hop | core | push_breadcrumb | 8 | 0.000 |
| single_hop | core | push_breadcrumb_pull | 8 | 1.000 |
| single_hop | core | push_pages_upper_bound | 8 | 0.500 |
| single_hop | core | push_sections_pull | 8 | 1.000 |
| temporal | core | native_grep | 6 | 0.833 |
| temporal | core | native_index | 6 | 0.500 |
| temporal | core | none | 6 | 0.000 |
| temporal | core | oracle | 6 | 1.000 |
| temporal | core | pull | 6 | 1.000 |
| temporal | core | push_breadcrumb | 6 | 0.000 |
| temporal | core | push_breadcrumb_pull | 6 | 1.000 |
| temporal | core | push_pages_upper_bound | 6 | 1.000 |
| temporal | core | push_sections_pull | 6 | 1.000 |
| unprompted_push | core | native_grep | 12 | 0.167 |
| unprompted_push | core | native_index | 12 | 0.917 |
| unprompted_push | core | none | 12 | 0.000 |
| unprompted_push | core | oracle | 12 | 1.000 |
| unprompted_push | core | pull | 12 | 0.417 |
| unprompted_push | core | push_breadcrumb | 12 | 0.000 |
| unprompted_push | core | push_breadcrumb_pull | 12 | 0.500 |
| unprompted_push | core | push_pages_upper_bound | 12 | 0.000 |
| unprompted_push | core | push_sections_pull | 12 | 0.500 |

## Marker miss with delivery (issue athenaeum#1842, report_only)

`marker_miss_with_delivery` is the COUNT of gradable cells in that group where every answer-bearing page WAS delivered to the arm (`correctness_rate`'s delivered-page-evidence clause held) and at least one of those pages still contributed no matching `answer_markers` value to the answer -- see `marker_miss_with_delivery`. It splits a `False` cell into its two causes: a delivery gap (counted here as 0) versus a model miss or a marker-matching gap (counted here as 1). `report_only`: this column feeds no §7 condition, no `compute_verdicts` input and no win/loss field, exactly as `tag_followed_rate` above does not. `n/a` means no row in that group is gradable at all (including every abstention group) -- never a counted zero.

| probe_class | corpus_scale | arm | n | marker_miss_with_delivery |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | n/a |
| abstention | core | native_index | 3 | n/a |
| abstention | core | none | 3 | n/a |
| abstention | core | oracle | 3 | n/a |
| abstention | core | pull | 3 | n/a |
| abstention | core | push_breadcrumb | 3 | n/a |
| abstention | core | push_breadcrumb_pull | 2 | n/a |
| abstention | core | push_pages_upper_bound | 3 | n/a |
| abstention | core | push_sections_pull | 2 | n/a |
| disambiguation | core | native_grep | 4 | 0 |
| disambiguation | core | native_index | 4 | 0 |
| disambiguation | core | none | 4 | 0 |
| disambiguation | core | oracle | 4 | 0 |
| disambiguation | core | pull | 4 | 0 |
| disambiguation | core | push_breadcrumb | 4 | 4 |
| disambiguation | core | push_breadcrumb_pull | 4 | 0 |
| disambiguation | core | push_pages_upper_bound | 4 | 0 |
| disambiguation | core | push_sections_pull | 4 | 0 |
| distractor_robustness | core | native_grep | 2 | 0 |
| distractor_robustness | core | native_index | 2 | 0 |
| distractor_robustness | core | none | 2 | 0 |
| distractor_robustness | core | oracle | 2 | 0 |
| distractor_robustness | core | pull | 2 | 0 |
| distractor_robustness | core | push_breadcrumb | 2 | 1 |
| distractor_robustness | core | push_breadcrumb_pull | 2 | 0 |
| distractor_robustness | core | push_pages_upper_bound | 2 | 0 |
| distractor_robustness | core | push_sections_pull | 2 | 0 |
| follow_through | core | native_grep | 6 | 0 |
| follow_through | core | native_index | 6 | 0 |
| follow_through | core | none | 6 | 0 |
| follow_through | core | oracle | 6 | 0 |
| follow_through | core | pull | 6 | 0 |
| follow_through | core | push_breadcrumb | 6 | 0 |
| follow_through | core | push_breadcrumb_pull | 6 | 1 |
| follow_through | core | push_pages_upper_bound | 6 | 0 |
| follow_through | core | push_sections_pull | 6 | 1 |
| multi_hop | core | native_grep | 3 | 0 |
| multi_hop | core | native_index | 3 | 0 |
| multi_hop | core | none | 3 | 0 |
| multi_hop | core | oracle | 3 | 0 |
| multi_hop | core | pull | 2 | 1 |
| multi_hop | core | push_breadcrumb | 3 | 0 |
| multi_hop | core | push_breadcrumb_pull | 3 | 0 |
| multi_hop | core | push_pages_upper_bound | 3 | 0 |
| multi_hop | core | push_sections_pull | 3 | 0 |
| redundancy | core | native_grep | 4 | 1 |
| redundancy | core | native_index | 4 | 1 |
| redundancy | core | none | 4 | 0 |
| redundancy | core | oracle | 4 | 1 |
| redundancy | core | pull | 4 | 2 |
| redundancy | core | push_breadcrumb | 4 | 4 |
| redundancy | core | push_breadcrumb_pull | 4 | 1 |
| redundancy | core | push_pages_upper_bound | 4 | 2 |
| redundancy | core | push_sections_pull | 4 | 2 |
| single_hop | core | native_grep | 8 | 1 |
| single_hop | core | native_index | 8 | 0 |
| single_hop | core | none | 8 | 0 |
| single_hop | core | oracle | 8 | 3 |
| single_hop | core | pull | 8 | 1 |
| single_hop | core | push_breadcrumb | 8 | 7 |
| single_hop | core | push_breadcrumb_pull | 8 | 1 |
| single_hop | core | push_pages_upper_bound | 8 | 5 |
| single_hop | core | push_sections_pull | 8 | 0 |
| temporal | core | native_grep | 6 | 0 |
| temporal | core | native_index | 6 | 0 |
| temporal | core | none | 6 | 0 |
| temporal | core | oracle | 6 | 0 |
| temporal | core | pull | 6 | 1 |
| temporal | core | push_breadcrumb | 6 | 5 |
| temporal | core | push_breadcrumb_pull | 6 | 0 |
| temporal | core | push_pages_upper_bound | 6 | 0 |
| temporal | core | push_sections_pull | 6 | 0 |
| unprompted_push | core | native_grep | 12 | 2 |
| unprompted_push | core | native_index | 12 | 8 |
| unprompted_push | core | none | 12 | 0 |
| unprompted_push | core | oracle | 12 | 8 |
| unprompted_push | core | pull | 12 | 7 |
| unprompted_push | core | push_breadcrumb | 12 | 2 |
| unprompted_push | core | push_breadcrumb_pull | 12 | 5 |
| unprompted_push | core | push_pages_upper_bound | 12 | 1 |
| unprompted_push | core | push_sections_pull | 12 | 7 |

## Follow-through hop delivered (issue athenaeum#1844, report_only)

`follow_hop_rate` is the share of `follow_through` cells in that group where every page named by `corpus.deep_hop_uids` -- the page an arm can only have landed on by FOLLOWING the body `[[wikilink]]`, never by a lexical match on the query -- was delivered to the arm, read from `_delivered_uids` (the grader's own dispatch). It separates the two causes a `False` `correctness_rate` conflates on this class: the arm never reached the second page (counted here as 0) versus it reached the page and the answer still did not carry the fact (counted here as 1, alongside a `marker_miss_with_delivery` of 1). The deep page is DERIVED from the link graph, never read off a fixed `expected_uids` position. `report_only`: this column feeds no §7 condition, no cutoff, no `compute_verdicts` input and no win/loss field, exactly as `marker_miss_with_delivery` above does not. `n/a` for every non-`follow_through` group -- no hop to have followed is a different fact from a rate of zero.

| probe_class | corpus_scale | arm | n | follow_hop_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1 | n/a |
| abstention | core | native_index | 3 | n/a |
| abstention | core | none | 3 | n/a |
| abstention | core | oracle | 3 | n/a |
| abstention | core | pull | 3 | n/a |
| abstention | core | push_breadcrumb | 3 | n/a |
| abstention | core | push_breadcrumb_pull | 2 | n/a |
| abstention | core | push_pages_upper_bound | 3 | n/a |
| abstention | core | push_sections_pull | 2 | n/a |
| disambiguation | core | native_grep | 4 | n/a |
| disambiguation | core | native_index | 4 | n/a |
| disambiguation | core | none | 4 | n/a |
| disambiguation | core | oracle | 4 | n/a |
| disambiguation | core | pull | 4 | n/a |
| disambiguation | core | push_breadcrumb | 4 | n/a |
| disambiguation | core | push_breadcrumb_pull | 4 | n/a |
| disambiguation | core | push_pages_upper_bound | 4 | n/a |
| disambiguation | core | push_sections_pull | 4 | n/a |
| distractor_robustness | core | native_grep | 2 | n/a |
| distractor_robustness | core | native_index | 2 | n/a |
| distractor_robustness | core | none | 2 | n/a |
| distractor_robustness | core | oracle | 2 | n/a |
| distractor_robustness | core | pull | 2 | n/a |
| distractor_robustness | core | push_breadcrumb | 2 | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 2 | n/a |
| distractor_robustness | core | push_pages_upper_bound | 2 | n/a |
| distractor_robustness | core | push_sections_pull | 2 | n/a |
| follow_through | core | native_grep | 6 | 0.833 |
| follow_through | core | native_index | 6 | 0.833 |
| follow_through | core | none | 6 | 0.000 |
| follow_through | core | oracle | 6 | 1.000 |
| follow_through | core | pull | 6 | 1.000 |
| follow_through | core | push_breadcrumb | 6 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 6 | 1.000 |
| follow_through | core | push_pages_upper_bound | 6 | 0.000 |
| follow_through | core | push_sections_pull | 6 | 0.833 |
| multi_hop | core | native_grep | 3 | n/a |
| multi_hop | core | native_index | 3 | n/a |
| multi_hop | core | none | 3 | n/a |
| multi_hop | core | oracle | 3 | n/a |
| multi_hop | core | pull | 2 | n/a |
| multi_hop | core | push_breadcrumb | 3 | n/a |
| multi_hop | core | push_breadcrumb_pull | 3 | n/a |
| multi_hop | core | push_pages_upper_bound | 3 | n/a |
| multi_hop | core | push_sections_pull | 3 | n/a |
| redundancy | core | native_grep | 4 | n/a |
| redundancy | core | native_index | 4 | n/a |
| redundancy | core | none | 4 | n/a |
| redundancy | core | oracle | 4 | n/a |
| redundancy | core | pull | 4 | n/a |
| redundancy | core | push_breadcrumb | 4 | n/a |
| redundancy | core | push_breadcrumb_pull | 4 | n/a |
| redundancy | core | push_pages_upper_bound | 4 | n/a |
| redundancy | core | push_sections_pull | 4 | n/a |
| single_hop | core | native_grep | 8 | n/a |
| single_hop | core | native_index | 8 | n/a |
| single_hop | core | none | 8 | n/a |
| single_hop | core | oracle | 8 | n/a |
| single_hop | core | pull | 8 | n/a |
| single_hop | core | push_breadcrumb | 8 | n/a |
| single_hop | core | push_breadcrumb_pull | 8 | n/a |
| single_hop | core | push_pages_upper_bound | 8 | n/a |
| single_hop | core | push_sections_pull | 8 | n/a |
| temporal | core | native_grep | 6 | n/a |
| temporal | core | native_index | 6 | n/a |
| temporal | core | none | 6 | n/a |
| temporal | core | oracle | 6 | n/a |
| temporal | core | pull | 6 | n/a |
| temporal | core | push_breadcrumb | 6 | n/a |
| temporal | core | push_breadcrumb_pull | 6 | n/a |
| temporal | core | push_pages_upper_bound | 6 | n/a |
| temporal | core | push_sections_pull | 6 | n/a |
| unprompted_push | core | native_grep | 12 | n/a |
| unprompted_push | core | native_index | 12 | n/a |
| unprompted_push | core | none | 12 | n/a |
| unprompted_push | core | oracle | 12 | n/a |
| unprompted_push | core | pull | 12 | n/a |
| unprompted_push | core | push_breadcrumb | 12 | n/a |
| unprompted_push | core | push_breadcrumb_pull | 12 | n/a |
| unprompted_push | core | push_pages_upper_bound | 12 | n/a |
| unprompted_push | core | push_sections_pull | 12 | n/a |

### Weak probes (NONE arm already answers correctly)

Probes where the floor (no context at all) already grades correct — a signal the ground truth leaked into the model's own prior knowledge, or the token is otherwise guessable, not that any retrieval arm helped. Read alongside the ORACLE ceiling: a probe listed here needs a harder token or a different question, not a better arm.

_none observed in this run_

## Index coverage (NATIVE_INDEX only, athenaeum#1725)

`index_coverage = index_lines_loaded / pages` -- the fraction of the corpus the TRUNCATED index still names. `index_lines_loaded` is read back from what Claude Code actually loaded into the session (the `attachment` event in the session transcript), never recomputed from the 200-line/25KB cap: Claude Code performs the truncation, this report only observes its result. `n/a` means no NATIVE_INDEX row in that group carried a readable coverage figure (e.g. auto memory did not activate).

| probe_class | corpus_scale | n | index_coverage |
| --- | --- | --- | --- |
| abstention | core | 3 | 1.000 |
| disambiguation | core | 4 | 1.000 |
| distractor_robustness | core | 2 | 1.000 |
| follow_through | core | 6 | 1.000 |
| multi_hop | core | 3 | 1.000 |
| redundancy | core | 4 | 1.000 |
| single_hop | core | 8 | 1.000 |
| temporal | core | 6 | 1.000 |
| unprompted_push | core | 12 | 1.000 |

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
| abstention | n/a |
| disambiguation | core |
| distractor_robustness | core |
| follow_through | core |
| multi_hop | n/a |
| redundancy | core |
| single_hop | n/a |
| temporal | core |
| unprompted_push | n/a |

## Frontier (cost vs. quality — never a single composite)

This report intentionally emits no weighted score combining cost and quality: the weights are a business judgement, not a measurement (issue athenaeum#1523). Read the Cost and Query-quality/Utilization sections above side by side, per (probe_class, corpus_scale), and decide the tradeoff from there — the numbers below are restated unweighted, one arm's cost beside its utilization, nothing summed across them.

| probe_class | corpus_scale | arm | mean_input_tokens/turn | mean_output_tokens/turn | uid_citation_rate | distinctive_ngram_overlap |
| --- | --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1056.2 | 76.5 | n/a | n/a |
| abstention | core | native_index | 5829.0 | 83.5 | n/a | n/a |
| abstention | core | none | 213.7 | 48.7 | n/a | n/a |
| abstention | core | oracle | 213.7 | 80.7 | n/a | n/a |
| abstention | core | pull | 4966.5 | 110.4 | 0.000 | 0.002 |
| abstention | core | push_breadcrumb | 388.3 | 111.3 | n/a | 0.069 |
| abstention | core | push_breadcrumb_pull | 5753.3 | 114.8 | 0.000 | 0.001 |
| abstention | core | push_pages_upper_bound | 1092.7 | 71.0 | 0.000 | 0.000 |
| abstention | core | push_sections_pull | 5159.9 | 100.0 | 0.000 | 0.002 |
| disambiguation | core | native_grep | 1743.8 | 122.4 | n/a | n/a |
| disambiguation | core | native_index | 6515.7 | 131.8 | n/a | n/a |
| disambiguation | core | none | 212.2 | 71.8 | n/a | n/a |
| disambiguation | core | oracle | 779.0 | 160.2 | 0.000 | 0.114 |
| disambiguation | core | pull | 2814.2 | 129.4 | 0.000 | 0.077 |
| disambiguation | core | push_breadcrumb | 395.5 | 152.5 | n/a | 0.016 |
| disambiguation | core | push_breadcrumb_pull | 3562.9 | 146.8 | 0.000 | 0.040 |
| disambiguation | core | push_pages_upper_bound | 1151.5 | 206.2 | 0.250 | 0.078 |
| disambiguation | core | push_sections_pull | 3302.5 | 161.8 | 0.000 | 0.092 |
| distractor_robustness | core | native_grep | 1968.5 | 216.7 | n/a | n/a |
| distractor_robustness | core | native_index | 6187.7 | 144.3 | n/a | n/a |
| distractor_robustness | core | none | 210.0 | 89.5 | n/a | n/a |
| distractor_robustness | core | oracle | 621.0 | 90.5 | 0.000 | 0.089 |
| distractor_robustness | core | pull | 2690.8 | 109.2 | 0.167 | 0.050 |
| distractor_robustness | core | push_breadcrumb | 393.5 | 144.0 | n/a | 0.109 |
| distractor_robustness | core | push_breadcrumb_pull | 3272.2 | 166.8 | 0.000 | 0.045 |
| distractor_robustness | core | push_pages_upper_bound | 1068.0 | 171.0 | 0.000 | 0.073 |
| distractor_robustness | core | push_sections_pull | 3136.7 | 133.7 | 0.000 | 0.027 |
| follow_through | core | native_grep | 1448.0 | 119.0 | n/a | n/a |
| follow_through | core | native_index | 6220.4 | 107.4 | n/a | n/a |
| follow_through | core | none | 229.0 | 106.3 | n/a | n/a |
| follow_through | core | oracle | 567.2 | 133.2 | 0.000 | 0.186 |
| follow_through | core | pull | 4453.1 | 149.7 | 0.000 | 0.034 |
| follow_through | core | push_breadcrumb | 417.0 | 157.5 | n/a | 0.021 |
| follow_through | core | push_breadcrumb_pull | 5434.1 | 150.3 | 0.000 | 0.019 |
| follow_through | core | push_pages_upper_bound | 1087.5 | 150.3 | 0.000 | 0.037 |
| follow_through | core | push_sections_pull | 4032.4 | 151.9 | 0.000 | 0.033 |
| multi_hop | core | native_grep | 2211.8 | 134.6 | n/a | n/a |
| multi_hop | core | native_index | 6576.6 | 130.6 | n/a | n/a |
| multi_hop | core | none | 231.7 | 69.0 | n/a | n/a |
| multi_hop | core | oracle | 690.7 | 158.3 | 0.000 | 0.130 |
| multi_hop | core | pull | 5197.6 | 132.7 | 0.000 | 0.012 |
| multi_hop | core | push_breadcrumb | 404.0 | 121.3 | n/a | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 6278.0 | 150.1 | 0.000 | 0.013 |
| multi_hop | core | push_pages_upper_bound | 1061.0 | 114.0 | 0.000 | 0.004 |
| multi_hop | core | push_sections_pull | 4662.6 | 139.1 | 0.000 | 0.026 |
| redundancy | core | native_grep | 1278.8 | 89.7 | n/a | n/a |
| redundancy | core | native_index | 6085.2 | 71.5 | n/a | n/a |
| redundancy | core | none | 214.8 | 70.0 | n/a | n/a |
| redundancy | core | oracle | 628.2 | 70.5 | 0.000 | 0.070 |
| redundancy | core | pull | 3060.5 | 87.8 | 0.000 | 0.041 |
| redundancy | core | push_breadcrumb | 403.8 | 132.8 | n/a | 0.006 |
| redundancy | core | push_breadcrumb_pull | 3557.9 | 109.6 | 0.025 | 0.018 |
| redundancy | core | push_pages_upper_bound | 1083.5 | 85.2 | 0.000 | 0.032 |
| redundancy | core | push_sections_pull | 3134.8 | 76.2 | 0.000 | 0.038 |
| single_hop | core | native_grep | 1303.3 | 125.1 | n/a | n/a |
| single_hop | core | native_index | 6065.7 | 91.3 | n/a | n/a |
| single_hop | core | none | 215.0 | 78.5 | n/a | n/a |
| single_hop | core | oracle | 566.5 | 154.0 | 0.000 | 0.190 |
| single_hop | core | pull | 3119.7 | 144.9 | 0.000 | 0.071 |
| single_hop | core | push_breadcrumb | 397.6 | 133.1 | n/a | 0.013 |
| single_hop | core | push_breadcrumb_pull | 3596.3 | 156.1 | 0.000 | 0.042 |
| single_hop | core | push_pages_upper_bound | 1027.4 | 98.2 | 0.000 | 0.071 |
| single_hop | core | push_sections_pull | 3294.3 | 147.8 | 0.000 | 0.087 |
| temporal | core | native_grep | 1313.7 | 75.4 | n/a | n/a |
| temporal | core | native_index | 6126.6 | 72.4 | n/a | n/a |
| temporal | core | none | 212.3 | 57.2 | n/a | n/a |
| temporal | core | oracle | 399.2 | 55.0 | 0.000 | 0.083 |
| temporal | core | pull | 2765.2 | 83.4 | 0.033 | 0.035 |
| temporal | core | push_breadcrumb | 399.2 | 126.8 | n/a | 0.029 |
| temporal | core | push_breadcrumb_pull | 3210.9 | 100.5 | 0.000 | 0.020 |
| temporal | core | push_pages_upper_bound | 1028.8 | 73.2 | 0.000 | 0.035 |
| temporal | core | push_sections_pull | 3005.3 | 75.0 | 0.000 | 0.056 |
| unprompted_push | core | native_grep | 1630.1 | 159.6 | n/a | n/a |
| unprompted_push | core | native_index | 6574.0 | 155.3 | n/a | n/a |
| unprompted_push | core | none | 232.7 | 143.5 | n/a | n/a |
| unprompted_push | core | oracle | 663.2 | 209.1 | 0.000 | 0.107 |
| unprompted_push | core | pull | 4875.4 | 216.7 | 0.127 | 0.031 |
| unprompted_push | core | push_breadcrumb | 421.2 | 223.0 | n/a | 0.095 |
| unprompted_push | core | push_breadcrumb_pull | 5627.1 | 217.5 | 0.071 | 0.030 |
| unprompted_push | core | push_pages_upper_bound | 1179.9 | 239.2 | 0.250 | 0.087 |
| unprompted_push | core | push_sections_pull | 4469.5 | 231.8 | 0.107 | 0.049 |

## Verdict: do the free dimensions settle the question?

Observed no_call_rate (0.009) and/or the self-vs-topic query-overlap gap (0.014) are non-trivial. The free dimensions show a directional signal on their own — read the per-(probe_class, corpus_scale) tables above for where it concentrates before deciding whether judged scoring is still needed to establish magnitude.
