<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
Noise-floor summary over run 42169666857's store ALONE -- kept for its per-dispatch spend receipt (section 7), which the pooled figure cannot give because de-duplication counts a twice-paid cell once.
-->

# North-star grid noise floor (athenaeum#2309)

- run_id: 42169666857
- generated: 2026-11-06T06:14:37Z
- model: n/a (unstamped store, no recoverable model)
  - model NOT independently verified for arms: native_grep, native_index, none, oracle, pull, push_breadcrumb, push_breadcrumb_pull, push_pages_upper_bound (pre-athenaeum#2309 store rows with no RolloutRecord.model and no cli-mode transcript to fall back to)
- search_backend: vector
- grader_revision: athenaeum#1935 (code computing THIS report, not the rows)
- git_sha: 447fcc0fb5bf (code computing THIS report, not the rows)
- athenaeum_version: 0.23.4
- corpus_digest[core]: 3c5ea876688b089e
- rows: 8 total, 8 gradable, 0 harness failures, 0 torn, 0 duplicate

## 1. Replicate-to-replicate variance

| probe_class | scale | arm | n (rep0/rep1) | rate rep0 | rate rep1 | n paired | discordance | SD(class rate) | df | estimator |
|---|---|---|---|---|---|---|---|---|---|---|
| single_hop | core | native_grep | 1/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | native_index | 1/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | none | 1/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | oracle | 1/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | pull | 1/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | push_breadcrumb | 1/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | push_breadcrumb_pull | 1/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | push_pages_upper_bound | 1/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |

## 2. Minimum detectable difference vs `push_breadcrumb_pull` (alpha=0.05, power=0.80)

| arm | probe_class | scale | n | rate(arm) | rate(verdict) | MDE paired | MDE unpaired | normal approx reliable |
|---|---|---|---|---|---|---|---|---|
| native_grep | single_hop | core | 1 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 1.981 | False |
| native_index | single_hop | core | 1 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 1.981 | False |
| none | single_hop | core | 1 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 1.981 | False |
| oracle | single_hop | core | 1 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | single_hop | core | 1 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | single_hop | core | 1 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 1.981 | False |
| push_pages_upper_bound | single_hop | core | 1 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |

Formulas: paired/McNemar: MDE = (z_a/2 + z_b) * sqrt(p_disc - (p10-p01)^2) / sqrt(n), alpha=0.05 two-sided, power=0.80

Formulas: unpaired two-proportion normal approximation (pooled variance at the mean rate, SAME n per side, pairing structure discarded): MDE = (z_a/2 + z_b) * sqrt(2 * pbar * (1-pbar)) / sqrt(n)

## 3. Required n to detect 0.05 / 0.10 / 0.15 / 0.20

| arm | probe_class | scale | n achieved | n@0.05 | n@0.10 | n@0.15 | n@0.20 |
|---|---|---|---|---|---|---|---|
| native_grep | single_hop | core | 1 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | single_hop | core | 1 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | single_hop | core | 1 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | single_hop | core | 1 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | single_hop | core | 1 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | single_hop | core | 1 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | single_hop | core | 1 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |

## 4. Oracle counter-example

Pooled correctness per arm (n-weighted over every gradable row):

| arm | n | correct | rate | harness failures |
|---|---|---|---|---|
| native_grep | 1 | 0 | 0.000 | 0 |
| native_index | 1 | 0 | 0.000 | 0 |
| none | 1 | 0 | 0.000 | 0 |
| oracle | 1 | 1 | 1.000 | 0 |
| pull | 1 | 1 | 1.000 | 0 |
| push_breadcrumb | 1 | 0 | 0.000 | 0 |
| push_breadcrumb_pull | 1 | 1 | 1.000 | 0 |
| push_pages_upper_bound | 1 | 1 | 1.000 | 0 |

Correctness per (arm, probe_class, corpus_scale):

| arm | probe_class | scale | n | correct | rate | harness failures |
|---|---|---|---|---|---|---|
| native_grep | single_hop | core | 1 | 0 | 0.000 | 0 |
| native_index | single_hop | core | 1 | 0 | 0.000 | 0 |
| none | single_hop | core | 1 | 0 | 0.000 | 0 |
| oracle | single_hop | core | 1 | 1 | 1.000 | 0 |
| pull | single_hop | core | 1 | 1 | 1.000 | 0 |
| push_breadcrumb | single_hop | core | 1 | 0 | 0.000 | 0 |
| push_breadcrumb_pull | single_hop | core | 1 | 1 | 1.000 | 0 |
| push_pages_upper_bound | single_hop | core | 1 | 1 | 1.000 | 0 |

Every `oracle` probe graded incorrect (0 probes):

| probe_class | scale | replicate | probe_id | push_breadcrumb_pull correct |
|---|---|---|---|---|

## 5. Positive control: `oracle` on `unprompted_push`

| scale | replicate | n | correct | rate | harness failures |
|---|---|---|---|---|---|
