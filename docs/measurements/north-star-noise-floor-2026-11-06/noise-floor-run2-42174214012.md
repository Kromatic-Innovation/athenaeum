<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
Noise-floor summary over run 42174214012's store ALONE -- kept for its per-dispatch spend receipt, same reason as the sibling file.
-->

# North-star grid noise floor (athenaeum#2309)

- run_id: 42174214012
- generated: 2026-11-06T07:00:46Z
- model: claude-haiku-4-5-20251001
- search_backend: vector
- grader_revision: athenaeum#1935 (code computing THIS report, not the rows)
- git_sha: 991fbfd641b8 (code computing THIS report, not the rows)
- athenaeum_version: 0.23.4
- corpus_digest[core]: 3c5ea876688b089e
- corpus_digest[medium]: 42e6ce0dfdc53e02
- rows: 480 total, 460 gradable, 20 harness failures, 0 torn, 0 duplicate

## 1. Replicate-to-replicate variance

| probe_class | scale | arm | n (rep0/rep1) | rate rep0 | rate rep1 | n paired | discordance | SD(class rate) | df | estimator |
|---|---|---|---|---|---|---|---|---|---|---|
| negative_knowledge | core | native_grep | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| negative_knowledge | core | native_index | 3/3 | 0.667 | 0.667 | 3 | 0.667 | 0.333 | 2 | paired discordance |
| negative_knowledge | core | none | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| negative_knowledge | core | oracle | 3/3 | 0.667 | 1.000 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| negative_knowledge | core | pull | 3/3 | 0.667 | 1.000 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| negative_knowledge | core | push_breadcrumb | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| negative_knowledge | core | push_breadcrumb_pull | 3/3 | 0.667 | 0.667 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| negative_knowledge | core | push_pages_upper_bound | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| negative_knowledge | medium | native_grep | 2/3 | 0.500 | 1.000 | 2 | 0.500 | 0.250 | 1 | paired discordance |
| negative_knowledge | medium | native_index | 3/3 | 1.000 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| negative_knowledge | medium | none | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| negative_knowledge | medium | oracle | 3/3 | 1.000 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| negative_knowledge | medium | pull | 3/3 | 0.667 | 1.000 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| negative_knowledge | medium | push_breadcrumb | 1/1 | 0.000 | 0.000 | 1 | 0.000 | 0.000 | 0 | paired discordance |
| negative_knowledge | medium | push_breadcrumb_pull | 1/1 | 1.000 | 1.000 | 1 | 0.000 | 0.000 | 0 | paired discordance |
| negative_knowledge | medium | push_pages_upper_bound | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| unprompted_push | core | native_grep | 12/12 | 0.000 | 0.000 | 12 | 0.000 | 0.000 | 11 | paired discordance |
| unprompted_push | core | native_index | 12/11 | 0.250 | 0.273 | 11 | 0.364 | 0.129 | 10 | paired discordance |
| unprompted_push | core | none | 12/12 | 0.000 | 0.000 | 12 | 0.000 | 0.000 | 11 | paired discordance |
| unprompted_push | core | oracle | 12/12 | 0.333 | 0.417 | 12 | 0.083 | 0.056 | 11 | paired discordance |
| unprompted_push | core | pull | 12/12 | 0.000 | 0.000 | 12 | 0.000 | 0.000 | 11 | paired discordance |
| unprompted_push | core | push_breadcrumb | 12/12 | 0.000 | 0.000 | 12 | 0.000 | 0.000 | 11 | paired discordance |
| unprompted_push | core | push_breadcrumb_pull | 12/12 | 0.083 | 0.167 | 12 | 0.083 | 0.056 | 11 | paired discordance |
| unprompted_push | core | push_pages_upper_bound | 12/12 | 0.000 | 0.000 | 12 | 0.000 | 0.000 | 11 | paired discordance |
| unprompted_push | medium | native_grep | 12/12 | 0.000 | 0.083 | 12 | 0.083 | 0.056 | 11 | paired discordance |
| unprompted_push | medium | native_index | 10/12 | 0.100 | 0.250 | 10 | 0.200 | 0.089 | 9 | paired discordance |
| unprompted_push | medium | none | 12/12 | 0.000 | 0.000 | 12 | 0.000 | 0.000 | 11 | paired discordance |
| unprompted_push | medium | oracle | 12/12 | 0.500 | 0.250 | 12 | 0.250 | 0.088 | 11 | paired discordance |
| unprompted_push | medium | pull | 12/12 | 0.083 | 0.167 | 12 | 0.250 | 0.101 | 11 | paired discordance |
| unprompted_push | medium | push_breadcrumb | 10/10 | 0.000 | 0.000 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| unprompted_push | medium | push_breadcrumb_pull | 10/10 | 0.100 | 0.000 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| unprompted_push | medium | push_pages_upper_bound | 12/12 | 0.000 | 0.000 | 12 | 0.000 | 0.000 | 11 | paired discordance |

## 2. Minimum detectable difference vs `push_breadcrumb_pull` (alpha=0.05, power=0.80)

| arm | probe_class | scale | n | rate(arm) | rate(verdict) | MDE paired | MDE unpaired | normal approx reliable |
|---|---|---|---|---|---|---|---|---|
| native_grep | negative_knowledge | core | 6 | 1.000 | 0.667 | 0.539 (insufficient n for normal approx -- use exact test) | 0.603 | False |
| native_index | negative_knowledge | core | 6 | 0.667 | 0.667 | 0.660 (insufficient n for normal approx -- use exact test) | 0.762 | False |
| none | negative_knowledge | core | 6 | 0.000 | 0.667 | 0.539 (insufficient n for normal approx -- use exact test) | 0.762 | False |
| oracle | negative_knowledge | core | 6 | 0.833 | 0.667 | 0.426 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| pull | negative_knowledge | core | 6 | 0.833 | 0.667 | 0.426 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| push_breadcrumb | negative_knowledge | core | 6 | 0.000 | 0.667 | 0.539 (insufficient n for normal approx -- use exact test) | 0.762 | False |
| push_pages_upper_bound | negative_knowledge | core | 6 | 1.000 | 0.667 | 0.539 (insufficient n for normal approx -- use exact test) | 0.603 | False |
| native_grep | negative_knowledge | medium | 1 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_index | negative_knowledge | medium | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| none | negative_knowledge | medium | 2 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 1.401 | False |
| oracle | negative_knowledge | medium | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | negative_knowledge | medium | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | negative_knowledge | medium | 2 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 1.401 | False |
| push_pages_upper_bound | negative_knowledge | medium | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | unprompted_push | core | 24 | 0.000 | 0.125 | 0.189 (insufficient n for normal approx -- use exact test) | 0.196 | False |
| native_index | unprompted_push | core | 23 | 0.261 | 0.130 | 0.313 | 0.328 | True |
| none | unprompted_push | core | 24 | 0.000 | 0.125 | 0.189 (insufficient n for normal approx -- use exact test) | 0.196 | False |
| oracle | unprompted_push | core | 24 | 0.375 | 0.125 | 0.340 | 0.350 | True |
| pull | unprompted_push | core | 24 | 0.000 | 0.125 | 0.189 (insufficient n for normal approx -- use exact test) | 0.196 | False |
| push_breadcrumb | unprompted_push | core | 24 | 0.000 | 0.125 | 0.189 (insufficient n for normal approx -- use exact test) | 0.196 | False |
| push_pages_upper_bound | unprompted_push | core | 24 | 0.000 | 0.125 | 0.189 (insufficient n for normal approx -- use exact test) | 0.196 | False |
| native_grep | unprompted_push | medium | 20 | 0.050 | 0.050 | 0.198 (insufficient n for normal approx -- use exact test) | 0.193 | False |
| native_index | unprompted_push | medium | 18 | 0.111 | 0.056 | 0.267 (insufficient n for normal approx -- use exact test) | 0.258 | False |
| none | unprompted_push | medium | 20 | 0.000 | 0.050 | 0.137 (insufficient n for normal approx -- use exact test) | 0.138 | False |
| oracle | unprompted_push | medium | 20 | 0.400 | 0.050 | 0.299 | 0.370 | True |
| pull | unprompted_push | medium | 20 | 0.150 | 0.050 | 0.273 (insufficient n for normal approx -- use exact test) | 0.266 | False |
| push_breadcrumb | unprompted_push | medium | 20 | 0.000 | 0.050 | 0.137 (insufficient n for normal approx -- use exact test) | 0.138 | False |
| push_pages_upper_bound | unprompted_push | medium | 20 | 0.000 | 0.050 | 0.137 (insufficient n for normal approx -- use exact test) | 0.138 | False |

Formulas: paired/McNemar: MDE = (z_a/2 + z_b) * sqrt(p_disc - (p10-p01)^2) / sqrt(n), alpha=0.05 two-sided, power=0.80

Formulas: unpaired two-proportion normal approximation (pooled variance at the mean rate, SAME n per side, pairing structure discarded): MDE = (z_a/2 + z_b) * sqrt(2 * pbar * (1-pbar)) / sqrt(n)

## 3. Required n to detect 0.05 / 0.10 / 0.15 / 0.20

| arm | probe_class | scale | n achieved | n@0.05 | n@0.10 | n@0.15 | n@0.20 |
|---|---|---|---|---|---|---|---|
| native_grep | negative_knowledge | core | 6 | 698 | 175 | 78 | 44 |
| native_index | negative_knowledge | core | 6 | 1047 | 262 | 117 | 66 |
| none | negative_knowledge | core | 6 | 698 | 175 | 78 | 44 |
| oracle | negative_knowledge | core | 6 | 437 | 110 | 49 | 28 |
| pull | negative_knowledge | core | 6 | 437 | 110 | 49 | 28 |
| push_breadcrumb | negative_knowledge | core | 6 | 698 | 175 | 78 | 44 |
| push_pages_upper_bound | negative_knowledge | core | 6 | 698 | 175 | 78 | 44 |
| native_grep | negative_knowledge | medium | 1 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | negative_knowledge | medium | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | negative_knowledge | medium | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | negative_knowledge | medium | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | negative_knowledge | medium | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | negative_knowledge | medium | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | negative_knowledge | medium | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | unprompted_push | core | 24 | 344 | 86 | 39 | 22 |
| native_index | unprompted_push | core | 23 | 903 | 226 | 101 | 57 |
| none | unprompted_push | core | 24 | 344 | 86 | 39 | 22 |
| oracle | unprompted_push | core | 24 | 1112 | 278 | 124 | 70 |
| pull | unprompted_push | core | 24 | 344 | 86 | 39 | 22 |
| push_breadcrumb | unprompted_push | core | 24 | 344 | 86 | 39 | 22 |
| push_pages_upper_bound | unprompted_push | core | 24 | 344 | 86 | 39 | 22 |
| native_grep | unprompted_push | medium | 20 | 314 | 79 | 35 | 20 |
| native_index | unprompted_push | medium | 18 | 514 | 129 | 58 | 33 |
| none | unprompted_push | medium | 20 | 150 | 38 | 17 | 10 |
| oracle | unprompted_push | medium | 20 | 715 | 179 | 80 | 45 |
| pull | unprompted_push | medium | 20 | 597 | 150 | 67 | 38 |
| push_breadcrumb | unprompted_push | medium | 20 | 150 | 38 | 17 | 10 |
| push_pages_upper_bound | unprompted_push | medium | 20 | 150 | 38 | 17 | 10 |

### `unprompted_push` specifically

- native_grep vs push_breadcrumb_pull @ core: n_achieved=24, required n = {0.05: '344', 0.1: '86', 0.15: '39', 0.2: '22'}
- native_index vs push_breadcrumb_pull @ core: n_achieved=23, required n = {0.05: '903', 0.1: '226', 0.15: '101', 0.2: '57'}
- none vs push_breadcrumb_pull @ core: n_achieved=24, required n = {0.05: '344', 0.1: '86', 0.15: '39', 0.2: '22'}
- oracle vs push_breadcrumb_pull @ core: n_achieved=24, required n = {0.05: '1112', 0.1: '278', 0.15: '124', 0.2: '70'}
- pull vs push_breadcrumb_pull @ core: n_achieved=24, required n = {0.05: '344', 0.1: '86', 0.15: '39', 0.2: '22'}
- push_breadcrumb vs push_breadcrumb_pull @ core: n_achieved=24, required n = {0.05: '344', 0.1: '86', 0.15: '39', 0.2: '22'}
- push_pages_upper_bound vs push_breadcrumb_pull @ core: n_achieved=24, required n = {0.05: '344', 0.1: '86', 0.15: '39', 0.2: '22'}
- native_grep vs push_breadcrumb_pull @ medium: n_achieved=20, required n = {0.05: '314', 0.1: '79', 0.15: '35', 0.2: '20'}
- native_index vs push_breadcrumb_pull @ medium: n_achieved=18, required n = {0.05: '514', 0.1: '129', 0.15: '58', 0.2: '33'}
- none vs push_breadcrumb_pull @ medium: n_achieved=20, required n = {0.05: '150', 0.1: '38', 0.15: '17', 0.2: '10'}
- oracle vs push_breadcrumb_pull @ medium: n_achieved=20, required n = {0.05: '715', 0.1: '179', 0.15: '80', 0.2: '45'}
- pull vs push_breadcrumb_pull @ medium: n_achieved=20, required n = {0.05: '597', 0.1: '150', 0.15: '67', 0.2: '38'}
- push_breadcrumb vs push_breadcrumb_pull @ medium: n_achieved=20, required n = {0.05: '150', 0.1: '38', 0.15: '17', 0.2: '10'}
- push_pages_upper_bound vs push_breadcrumb_pull @ medium: n_achieved=20, required n = {0.05: '150', 0.1: '38', 0.15: '17', 0.2: '10'}

## 4. Oracle counter-example

Pooled correctness per arm (n-weighted over every gradable row):

| arm | n | correct | rate | harness failures |
|---|---|---|---|---|
| native_grep | 59 | 11 | 0.186 | 1 |
| native_index | 57 | 19 | 0.333 | 3 |
| none | 60 | 0 | 0.000 | 0 |
| oracle | 60 | 28 | 0.467 | 0 |
| pull | 60 | 13 | 0.217 | 0 |
| push_breadcrumb | 52 | 0 | 0.000 | 8 |
| push_breadcrumb_pull | 52 | 10 | 0.192 | 8 |
| push_pages_upper_bound | 60 | 12 | 0.200 | 0 |

Correctness per (arm, probe_class, corpus_scale):

| arm | probe_class | scale | n | correct | rate | harness failures |
|---|---|---|---|---|---|---|
| native_grep | negative_knowledge | core | 6 | 6 | 1.000 | 0 |
| native_grep | negative_knowledge | medium | 5 | 4 | 0.800 | 1 |
| native_grep | unprompted_push | core | 24 | 0 | 0.000 | 0 |
| native_grep | unprompted_push | medium | 24 | 1 | 0.042 | 0 |
| native_index | negative_knowledge | core | 6 | 4 | 0.667 | 0 |
| native_index | negative_knowledge | medium | 6 | 5 | 0.833 | 0 |
| native_index | unprompted_push | core | 23 | 6 | 0.261 | 1 |
| native_index | unprompted_push | medium | 22 | 4 | 0.182 | 2 |
| none | negative_knowledge | core | 6 | 0 | 0.000 | 0 |
| none | negative_knowledge | medium | 6 | 0 | 0.000 | 0 |
| none | unprompted_push | core | 24 | 0 | 0.000 | 0 |
| none | unprompted_push | medium | 24 | 0 | 0.000 | 0 |
| oracle | negative_knowledge | core | 6 | 5 | 0.833 | 0 |
| oracle | negative_knowledge | medium | 6 | 5 | 0.833 | 0 |
| oracle | unprompted_push | core | 24 | 9 | 0.375 | 0 |
| oracle | unprompted_push | medium | 24 | 9 | 0.375 | 0 |
| pull | negative_knowledge | core | 6 | 5 | 0.833 | 0 |
| pull | negative_knowledge | medium | 6 | 5 | 0.833 | 0 |
| pull | unprompted_push | core | 24 | 0 | 0.000 | 0 |
| pull | unprompted_push | medium | 24 | 3 | 0.125 | 0 |
| push_breadcrumb | negative_knowledge | core | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | negative_knowledge | medium | 2 | 0 | 0.000 | 4 |
| push_breadcrumb | unprompted_push | core | 24 | 0 | 0.000 | 0 |
| push_breadcrumb | unprompted_push | medium | 20 | 0 | 0.000 | 4 |
| push_breadcrumb_pull | negative_knowledge | core | 6 | 4 | 0.667 | 0 |
| push_breadcrumb_pull | negative_knowledge | medium | 2 | 2 | 1.000 | 4 |
| push_breadcrumb_pull | unprompted_push | core | 24 | 3 | 0.125 | 0 |
| push_breadcrumb_pull | unprompted_push | medium | 20 | 1 | 0.050 | 4 |
| push_pages_upper_bound | negative_knowledge | core | 6 | 6 | 1.000 | 0 |
| push_pages_upper_bound | negative_knowledge | medium | 6 | 6 | 1.000 | 0 |
| push_pages_upper_bound | unprompted_push | core | 24 | 0 | 0.000 | 0 |
| push_pages_upper_bound | unprompted_push | medium | 24 | 0 | 0.000 | 0 |

Every `oracle` probe graded incorrect (32 probes):

| probe_class | scale | replicate | probe_id | push_breadcrumb_pull correct |
|---|---|---|---|---|
| negative_knowledge | core | 0 | vendor_migration_rerun_lesson | False |
| negative_knowledge | medium | 1 | vendor_migration_rerun_lesson | harness_failure |
| unprompted_push | core | 0 | cache_layer_merge | False |
| unprompted_push | core | 1 | cache_layer_merge | False |
| unprompted_push | core | 0 | connection_pool_widen | False |
| unprompted_push | core | 1 | connection_pool_widen | False |
| unprompted_push | core | 0 | export_flag_deletion | False |
| unprompted_push | core | 1 | export_flag_deletion | False |
| unprompted_push | core | 0 | invoice_role_merge | True |
| unprompted_push | core | 1 | invoice_role_merge | True |
| unprompted_push | core | 0 | invoiceref_rename | False |
| unprompted_push | core | 1 | invoiceref_rename | False |
| unprompted_push | core | 0 | rate_limit_removal | False |
| unprompted_push | core | 1 | rate_limit_removal | False |
| unprompted_push | core | 0 | tls_library_bump | False |
| unprompted_push | core | 0 | webhook_backoff_lower | False |
| unprompted_push | core | 1 | webhook_backoff_lower | False |
| unprompted_push | medium | 0 | cache_layer_merge | False |
| unprompted_push | medium | 1 | cache_layer_merge | False |
| unprompted_push | medium | 1 | connection_pool_widen | harness_failure |
| unprompted_push | medium | 0 | export_flag_deletion | False |
| unprompted_push | medium | 1 | export_flag_deletion | False |
| unprompted_push | medium | 0 | invoice_role_merge | harness_failure |
| unprompted_push | medium | 1 | invoice_role_merge | False |
| unprompted_push | medium | 0 | invoiceref_rename | False |
| unprompted_push | medium | 1 | invoiceref_rename | False |
| unprompted_push | medium | 1 | migration_spot_check_cut | harness_failure |
| unprompted_push | medium | 0 | rate_limit_removal | False |
| unprompted_push | medium | 1 | rate_limit_removal | False |
| unprompted_push | medium | 1 | tls_library_bump | False |
| unprompted_push | medium | 0 | webhook_backoff_lower | False |
| unprompted_push | medium | 1 | webhook_backoff_lower | False |

## 5. Positive control: `oracle` on `unprompted_push`

| scale | replicate | n | correct | rate | harness failures |
|---|---|---|---|---|---|
| core | 0 | 12 | 4 | 0.333 | 0 |
| core | 1 | 12 | 5 | 0.417 | 0 |
| medium | 0 | 12 | 6 | 0.500 | 0 |
| medium | 1 | 12 | 3 | 0.250 | 0 |

## 6. Grid coverage

15 probes have at least one row; 15 are complete across every (scale, replicate, arm) this store carries. 42 probes have no row at all.

Absent entirely (no row at all):

| probe_id |
|---|
| `pto_allowance` |
| `confidentiality_rule` |
| `bluewater_terms` |
| `onboarding_length` |
| `spend_approver_named` |
| `portal_design_reviewer` |
| `ratecard_tooling_owner` |
| `office_address_current` |
| `standup_time_current` |
| `budget_threshold_current` |
| `callum_drews_last_contact` |
| `thorncastle_first_contact` |
| `tamsin_ferro_role_change` |
| `person_not_repo` |
| `repo_not_person` |
| `surname_is_ambiguous` |
| `given_name_is_ambiguous` |
| `abstain_unknown_client` |
| `abstain_unknown_policy` |
| `abstain_unknown_person` |
| `acme_billing_exception` |
| `former_client_not_current` |
| `keelbridge_programme_scope` |
| `driftgate_migration_funding` |
| `dual_signoff_threshold` |
| `mira_castellane_role` |
| `fenwick_relationship_history` |
| `corvale_holdings_history` |
| `pinemarsh_goal_rationale` |
| `gilcrest_vendor_decision` |
| `anchorline_retirement_rationale` |
| `subsidiary_onboarding_lesson` |
| `bramfield_retainer_renewal` |
| `remote_equipment_stipend_cap` |
| `lighthouse_migration_rollback` |
| `triform_vendor_consolidation` |
| `aggregation_delivery_roles` |
| `aggregation_retainer_clients` |
| `aggregation_inhouse_tools` |
| `expense_approval_limit_manager` |
| `overtime_request_approver` |
| `invoicing_api_pagination_workaround` |

## 7. Actual spend for this run

Priced at `claude-haiku-4-5-20251001` rates with the cache multipliers `athenaeum.models` documents (1.25x on a cache write, 0.1x on a cache read), over EVERY persisted row -- a cell that tripped the turn cap was paid for exactly like one that answered.

| rows counted | input | cache write | cache read | output | billable input | USD |
|---|---|---|---|---|---|---|
| 480 | 3,925,190 | 0 | 0 | 193,534 | 3,925,190 | $4.89 |

Total tokens: 4,118,724.
