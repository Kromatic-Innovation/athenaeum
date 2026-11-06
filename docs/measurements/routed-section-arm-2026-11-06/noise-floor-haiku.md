<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
Noise-floor and coverage summary over run 42177905668's store. Its single replicate yields no floor of its own, so the Haiku comparison is read against athenaeum#2309's floor, which matches on model, corpus digest and grader revision. Kept for its coverage table and its spend receipt.
-->

# North-star grid noise floor (athenaeum#2309)

- run_id: 42177905668
- generated: 2026-11-06T07:23:23Z
- model: claude-haiku-4-5-20251001
- search_backend: vector
- grader_revision: athenaeum#1935 (code computing THIS report, not the rows)
- git_sha: e6f4cc2a150b (code computing THIS report, not the rows)
- athenaeum_version: 0.23.4
- corpus_digest[core]: 3c5ea876688b089e
- rows: 432 total, 427 gradable, 5 harness failures, 0 torn, 0 duplicate

## 1. Replicate-to-replicate variance

| probe_class | scale | arm | n (rep0/rep1) | rate rep0 | rate rep1 | n paired | discordance | SD(class rate) | df | estimator |
|---|---|---|---|---|---|---|---|---|---|---|
| abstention | core | native_grep | 1/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| abstention | core | native_index | 3/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| abstention | core | none | 3/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| abstention | core | oracle | 3/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| abstention | core | pull | 3/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| abstention | core | push_breadcrumb | 3/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| abstention | core | push_breadcrumb_pull | 2/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| abstention | core | push_pages_upper_bound | 3/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| abstention | core | push_sections_pull | 2/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| disambiguation | core | native_grep | 4/0 | 0.750 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| disambiguation | core | native_index | 4/0 | 0.500 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| disambiguation | core | none | 4/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| disambiguation | core | oracle | 4/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| disambiguation | core | pull | 4/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| disambiguation | core | push_breadcrumb | 4/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| disambiguation | core | push_breadcrumb_pull | 4/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| disambiguation | core | push_pages_upper_bound | 4/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| disambiguation | core | push_sections_pull | 4/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| distractor_robustness | core | native_grep | 2/0 | 0.500 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| distractor_robustness | core | native_index | 2/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| distractor_robustness | core | none | 2/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| distractor_robustness | core | oracle | 2/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| distractor_robustness | core | pull | 2/0 | 0.500 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| distractor_robustness | core | push_breadcrumb | 2/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| distractor_robustness | core | push_breadcrumb_pull | 2/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| distractor_robustness | core | push_pages_upper_bound | 2/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| distractor_robustness | core | push_sections_pull | 2/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| follow_through | core | native_grep | 6/0 | 0.833 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| follow_through | core | native_index | 6/0 | 0.833 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| follow_through | core | none | 6/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| follow_through | core | oracle | 6/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| follow_through | core | pull | 6/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| follow_through | core | push_breadcrumb | 6/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| follow_through | core | push_breadcrumb_pull | 6/0 | 0.833 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| follow_through | core | push_pages_upper_bound | 6/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| follow_through | core | push_sections_pull | 6/0 | 0.667 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| multi_hop | core | native_grep | 3/0 | 0.667 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| multi_hop | core | native_index | 3/0 | 0.667 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| multi_hop | core | none | 3/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| multi_hop | core | oracle | 3/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| multi_hop | core | pull | 2/0 | 0.500 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| multi_hop | core | push_breadcrumb | 3/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| multi_hop | core | push_breadcrumb_pull | 3/0 | 0.667 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| multi_hop | core | push_pages_upper_bound | 3/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| multi_hop | core | push_sections_pull | 3/0 | 0.667 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| redundancy | core | native_grep | 4/0 | 0.250 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| redundancy | core | native_index | 4/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| redundancy | core | none | 4/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| redundancy | core | oracle | 4/0 | 0.750 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| redundancy | core | pull | 4/0 | 0.500 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| redundancy | core | push_breadcrumb | 4/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| redundancy | core | push_breadcrumb_pull | 4/0 | 0.750 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| redundancy | core | push_pages_upper_bound | 4/0 | 0.500 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| redundancy | core | push_sections_pull | 4/0 | 0.500 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | native_grep | 8/0 | 0.875 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | native_index | 8/0 | 0.875 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | none | 8/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | oracle | 8/0 | 0.625 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | pull | 8/0 | 0.875 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | push_breadcrumb | 8/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | push_breadcrumb_pull | 8/0 | 0.875 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | push_pages_upper_bound | 8/0 | 0.375 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| single_hop | core | push_sections_pull | 8/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| temporal | core | native_grep | 6/0 | 0.833 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| temporal | core | native_index | 6/0 | 0.500 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| temporal | core | none | 6/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| temporal | core | oracle | 6/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| temporal | core | pull | 6/0 | 0.833 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| temporal | core | push_breadcrumb | 6/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| temporal | core | push_breadcrumb_pull | 6/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| temporal | core | push_pages_upper_bound | 6/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| temporal | core | push_sections_pull | 6/0 | 1.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| unprompted_push | core | native_grep | 12/0 | 0.083 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| unprompted_push | core | native_index | 12/0 | 0.333 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| unprompted_push | core | none | 12/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| unprompted_push | core | oracle | 12/0 | 0.333 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| unprompted_push | core | pull | 12/0 | 0.083 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| unprompted_push | core | push_breadcrumb | 12/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| unprompted_push | core | push_breadcrumb_pull | 12/0 | 0.167 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| unprompted_push | core | push_pages_upper_bound | 12/0 | 0.000 | n/a | 0 | n/a | n/a | -1 | paired discordance |
| unprompted_push | core | push_sections_pull | 12/0 | 0.167 | n/a | 0 | n/a | n/a | -1 | paired discordance |

## 2. Minimum detectable difference vs `push_breadcrumb_pull` (alpha=0.05, power=0.80)

| arm | probe_class | scale | n | rate(arm) | rate(verdict) | MDE paired | MDE unpaired | normal approx reliable |
|---|---|---|---|---|---|---|---|---|
| native_grep | abstention | core | 1 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_index | abstention | core | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| none | abstention | core | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| oracle | abstention | core | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | abstention | core | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | abstention | core | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_pages_upper_bound | abstention | core | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_sections_pull | abstention | core | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | disambiguation | core | 4 | 0.750 | 1.000 | 0.607 (insufficient n for normal approx -- use exact test) | 0.655 | False |
| native_index | disambiguation | core | 4 | 0.500 | 1.000 | 0.700 (insufficient n for normal approx -- use exact test) | 0.858 | False |
| none | disambiguation | core | 4 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.991 | False |
| oracle | disambiguation | core | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | disambiguation | core | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | disambiguation | core | 4 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.991 | False |
| push_pages_upper_bound | disambiguation | core | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_sections_pull | disambiguation | core | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | distractor_robustness | core | 2 | 0.500 | 1.000 | 0.991 (insufficient n for normal approx -- use exact test) | 1.213 | False |
| native_index | distractor_robustness | core | 2 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 1.401 | False |
| none | distractor_robustness | core | 2 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 1.401 | False |
| oracle | distractor_robustness | core | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | distractor_robustness | core | 2 | 0.500 | 1.000 | 0.991 (insufficient n for normal approx -- use exact test) | 1.213 | False |
| push_breadcrumb | distractor_robustness | core | 2 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 1.401 | False |
| push_pages_upper_bound | distractor_robustness | core | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_sections_pull | distractor_robustness | core | 2 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | follow_through | core | 6 | 0.833 | 0.833 | 0.660 (insufficient n for normal approx -- use exact test) | 0.603 | False |
| native_index | follow_through | core | 6 | 0.833 | 0.833 | 0.660 (insufficient n for normal approx -- use exact test) | 0.603 | False |
| none | follow_through | core | 6 | 0.000 | 0.833 | 0.426 (insufficient n for normal approx -- use exact test) | 0.797 | False |
| oracle | follow_through | core | 6 | 1.000 | 0.833 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| pull | follow_through | core | 6 | 1.000 | 0.833 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| push_breadcrumb | follow_through | core | 6 | 0.000 | 0.833 | 0.426 (insufficient n for normal approx -- use exact test) | 0.797 | False |
| push_pages_upper_bound | follow_through | core | 6 | 0.000 | 0.833 | 0.426 (insufficient n for normal approx -- use exact test) | 0.797 | False |
| push_sections_pull | follow_through | core | 6 | 0.667 | 0.833 | 0.786 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| native_grep | multi_hop | core | 3 | 0.667 | 0.667 | 0.000 (insufficient n for normal approx -- use exact test) | 1.078 | False |
| native_index | multi_hop | core | 3 | 0.667 | 0.667 | 0.000 (insufficient n for normal approx -- use exact test) | 1.078 | False |
| none | multi_hop | core | 3 | 0.000 | 0.667 | 0.762 (insufficient n for normal approx -- use exact test) | 1.078 | False |
| oracle | multi_hop | core | 3 | 1.000 | 0.667 | 0.762 (insufficient n for normal approx -- use exact test) | 0.852 | False |
| pull | multi_hop | core | 2 | 0.500 | 1.000 | 0.991 (insufficient n for normal approx -- use exact test) | 1.213 | False |
| push_breadcrumb | multi_hop | core | 3 | 0.000 | 0.667 | 0.762 (insufficient n for normal approx -- use exact test) | 1.078 | False |
| push_pages_upper_bound | multi_hop | core | 3 | 0.000 | 0.667 | 0.762 (insufficient n for normal approx -- use exact test) | 1.078 | False |
| push_sections_pull | multi_hop | core | 3 | 0.667 | 0.667 | 0.000 (insufficient n for normal approx -- use exact test) | 1.078 | False |
| native_grep | redundancy | core | 4 | 0.250 | 0.750 | 0.700 (insufficient n for normal approx -- use exact test) | 0.991 | False |
| native_index | redundancy | core | 4 | 0.000 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.959 | False |
| none | redundancy | core | 4 | 0.000 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.959 | False |
| oracle | redundancy | core | 4 | 0.750 | 0.750 | 0.991 (insufficient n for normal approx -- use exact test) | 0.858 | False |
| pull | redundancy | core | 4 | 0.500 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.959 | False |
| push_breadcrumb | redundancy | core | 4 | 0.000 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.959 | False |
| push_pages_upper_bound | redundancy | core | 4 | 0.500 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.959 | False |
| push_sections_pull | redundancy | core | 4 | 0.500 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.959 | False |
| native_grep | single_hop | core | 8 | 0.875 | 0.875 | 0.495 (insufficient n for normal approx -- use exact test) | 0.463 | False |
| native_index | single_hop | core | 8 | 0.875 | 0.875 | 0.495 (insufficient n for normal approx -- use exact test) | 0.463 | False |
| none | single_hop | core | 8 | 0.000 | 0.875 | 0.328 (insufficient n for normal approx -- use exact test) | 0.695 | False |
| oracle | single_hop | core | 8 | 0.625 | 0.875 | 0.429 (insufficient n for normal approx -- use exact test) | 0.607 | False |
| pull | single_hop | core | 8 | 0.875 | 0.875 | 0.495 (insufficient n for normal approx -- use exact test) | 0.463 | False |
| push_breadcrumb | single_hop | core | 8 | 0.000 | 0.875 | 0.328 (insufficient n for normal approx -- use exact test) | 0.695 | False |
| push_pages_upper_bound | single_hop | core | 8 | 0.375 | 0.875 | 0.495 (insufficient n for normal approx -- use exact test) | 0.678 | False |
| push_sections_pull | single_hop | core | 8 | 1.000 | 0.875 | 0.328 (insufficient n for normal approx -- use exact test) | 0.339 | False |
| native_grep | temporal | core | 6 | 0.833 | 1.000 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| native_index | temporal | core | 6 | 0.500 | 1.000 | 0.572 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| none | temporal | core | 6 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.809 | False |
| oracle | temporal | core | 6 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | temporal | core | 6 | 0.833 | 1.000 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| push_breadcrumb | temporal | core | 6 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.809 | False |
| push_pages_upper_bound | temporal | core | 6 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_sections_pull | temporal | core | 6 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | unprompted_push | core | 12 | 0.083 | 0.167 | 0.224 (insufficient n for normal approx -- use exact test) | 0.378 | False |
| native_index | unprompted_push | core | 12 | 0.333 | 0.167 | 0.447 (insufficient n for normal approx -- use exact test) | 0.495 | False |
| none | unprompted_push | core | 12 | 0.000 | 0.167 | 0.301 (insufficient n for normal approx -- use exact test) | 0.316 | False |
| oracle | unprompted_push | core | 12 | 0.333 | 0.167 | 0.556 | 0.495 | True |
| pull | unprompted_push | core | 12 | 0.083 | 0.167 | 0.399 (insufficient n for normal approx -- use exact test) | 0.378 | False |
| push_breadcrumb | unprompted_push | core | 12 | 0.000 | 0.167 | 0.301 (insufficient n for normal approx -- use exact test) | 0.316 | False |
| push_pages_upper_bound | unprompted_push | core | 12 | 0.000 | 0.167 | 0.301 (insufficient n for normal approx -- use exact test) | 0.316 | False |
| push_sections_pull | unprompted_push | core | 12 | 0.167 | 0.167 | 0.467 (insufficient n for normal approx -- use exact test) | 0.426 | False |

Formulas: paired/McNemar: MDE = (z_a/2 + z_b) * sqrt(p_disc - (p10-p01)^2) / sqrt(n), alpha=0.05 two-sided, power=0.80

Formulas: unpaired two-proportion normal approximation (pooled variance at the mean rate, SAME n per side, pairing structure discarded): MDE = (z_a/2 + z_b) * sqrt(2 * pbar * (1-pbar)) / sqrt(n)

## 3. Required n to detect 0.05 / 0.10 / 0.15 / 0.20

| arm | probe_class | scale | n achieved | n@0.05 | n@0.10 | n@0.15 | n@0.20 |
|---|---|---|---|---|---|---|---|
| native_grep | abstention | core | 1 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | abstention | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | abstention | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | abstention | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | abstention | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | abstention | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | abstention | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_sections_pull | abstention | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | disambiguation | core | 4 | 589 | 148 | 66 | 37 |
| native_index | disambiguation | core | 4 | 785 | 197 | 88 | 50 |
| none | disambiguation | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | disambiguation | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | disambiguation | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | disambiguation | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | disambiguation | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_sections_pull | disambiguation | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | distractor_robustness | core | 2 | 785 | 197 | 88 | 50 |
| native_index | distractor_robustness | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | distractor_robustness | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | distractor_robustness | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | distractor_robustness | core | 2 | 785 | 197 | 88 | 50 |
| push_breadcrumb | distractor_robustness | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | distractor_robustness | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_sections_pull | distractor_robustness | core | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | follow_through | core | 6 | 1047 | 262 | 117 | 66 |
| native_index | follow_through | core | 6 | 1047 | 262 | 117 | 66 |
| none | follow_through | core | 6 | 437 | 110 | 49 | 28 |
| oracle | follow_through | core | 6 | 437 | 110 | 49 | 28 |
| pull | follow_through | core | 6 | 437 | 110 | 49 | 28 |
| push_breadcrumb | follow_through | core | 6 | 437 | 110 | 49 | 28 |
| push_pages_upper_bound | follow_through | core | 6 | 437 | 110 | 49 | 28 |
| push_sections_pull | follow_through | core | 6 | 1483 | 371 | 165 | 93 |
| native_grep | multi_hop | core | 3 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | multi_hop | core | 3 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | multi_hop | core | 3 | 698 | 175 | 78 | 44 |
| oracle | multi_hop | core | 3 | 698 | 175 | 78 | 44 |
| pull | multi_hop | core | 2 | 785 | 197 | 88 | 50 |
| push_breadcrumb | multi_hop | core | 3 | 698 | 175 | 78 | 44 |
| push_pages_upper_bound | multi_hop | core | 3 | 698 | 175 | 78 | 44 |
| push_sections_pull | multi_hop | core | 3 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | redundancy | core | 4 | 785 | 197 | 88 | 50 |
| native_index | redundancy | core | 4 | 589 | 148 | 66 | 37 |
| none | redundancy | core | 4 | 589 | 148 | 66 | 37 |
| oracle | redundancy | core | 4 | 1570 | 393 | 175 | 99 |
| pull | redundancy | core | 4 | 589 | 148 | 66 | 37 |
| push_breadcrumb | redundancy | core | 4 | 589 | 148 | 66 | 37 |
| push_pages_upper_bound | redundancy | core | 4 | 589 | 148 | 66 | 37 |
| push_sections_pull | redundancy | core | 4 | 589 | 148 | 66 | 37 |
| native_grep | single_hop | core | 8 | 785 | 197 | 88 | 50 |
| native_index | single_hop | core | 8 | 785 | 197 | 88 | 50 |
| none | single_hop | core | 8 | 344 | 86 | 39 | 22 |
| oracle | single_hop | core | 8 | 589 | 148 | 66 | 37 |
| pull | single_hop | core | 8 | 785 | 197 | 88 | 50 |
| push_breadcrumb | single_hop | core | 8 | 344 | 86 | 39 | 22 |
| push_pages_upper_bound | single_hop | core | 8 | 785 | 197 | 88 | 50 |
| push_sections_pull | single_hop | core | 8 | 344 | 86 | 39 | 22 |
| native_grep | temporal | core | 6 | 437 | 110 | 49 | 28 |
| native_index | temporal | core | 6 | 785 | 197 | 88 | 50 |
| none | temporal | core | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | temporal | core | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | temporal | core | 6 | 437 | 110 | 49 | 28 |
| push_breadcrumb | temporal | core | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | temporal | core | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_sections_pull | temporal | core | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | unprompted_push | core | 12 | 240 | 60 | 27 | 15 |
| native_index | unprompted_push | core | 12 | 960 | 240 | 107 | 60 |
| none | unprompted_push | core | 12 | 437 | 110 | 49 | 28 |
| oracle | unprompted_push | core | 12 | 1483 | 371 | 165 | 93 |
| pull | unprompted_push | core | 12 | 764 | 191 | 85 | 48 |
| push_breadcrumb | unprompted_push | core | 12 | 437 | 110 | 49 | 28 |
| push_pages_upper_bound | unprompted_push | core | 12 | 437 | 110 | 49 | 28 |
| push_sections_pull | unprompted_push | core | 12 | 1047 | 262 | 117 | 66 |

### `unprompted_push` specifically

- native_grep vs push_breadcrumb_pull @ core: n_achieved=12, required n = {0.05: '240', 0.1: '60', 0.15: '27', 0.2: '15'}
- native_index vs push_breadcrumb_pull @ core: n_achieved=12, required n = {0.05: '960', 0.1: '240', 0.15: '107', 0.2: '60'}
- none vs push_breadcrumb_pull @ core: n_achieved=12, required n = {0.05: '437', 0.1: '110', 0.15: '49', 0.2: '28'}
- oracle vs push_breadcrumb_pull @ core: n_achieved=12, required n = {0.05: '1483', 0.1: '371', 0.15: '165', 0.2: '93'}
- pull vs push_breadcrumb_pull @ core: n_achieved=12, required n = {0.05: '764', 0.1: '191', 0.15: '85', 0.2: '48'}
- push_breadcrumb vs push_breadcrumb_pull @ core: n_achieved=12, required n = {0.05: '437', 0.1: '110', 0.15: '49', 0.2: '28'}
- push_pages_upper_bound vs push_breadcrumb_pull @ core: n_achieved=12, required n = {0.05: '437', 0.1: '110', 0.15: '49', 0.2: '28'}
- push_sections_pull vs push_breadcrumb_pull @ core: n_achieved=12, required n = {0.05: '1047', 0.1: '262', 0.15: '117', 0.2: '66'}

## 4. Oracle counter-example

Pooled correctness per arm (n-weighted over every gradable row):

| arm | n | correct | rate | harness failures |
|---|---|---|---|---|
| native_grep | 46 | 26 | 0.565 | 2 |
| native_index | 48 | 26 | 0.542 | 0 |
| none | 48 | 3 | 0.062 | 0 |
| oracle | 48 | 36 | 0.750 | 0 |
| pull | 47 | 30 | 0.638 | 1 |
| push_breadcrumb | 48 | 3 | 0.062 | 0 |
| push_breadcrumb_pull | 47 | 33 | 0.702 | 1 |
| push_pages_upper_bound | 48 | 20 | 0.417 | 0 |
| push_sections_pull | 47 | 32 | 0.681 | 1 |

Correctness per (arm, probe_class, corpus_scale):

| arm | probe_class | scale | n | correct | rate | harness failures |
|---|---|---|---|---|---|---|
| native_grep | abstention | core | 1 | 1 | 1.000 | 2 |
| native_grep | disambiguation | core | 4 | 3 | 0.750 | 0 |
| native_grep | distractor_robustness | core | 2 | 1 | 0.500 | 0 |
| native_grep | follow_through | core | 6 | 5 | 0.833 | 0 |
| native_grep | multi_hop | core | 3 | 2 | 0.667 | 0 |
| native_grep | redundancy | core | 4 | 1 | 0.250 | 0 |
| native_grep | single_hop | core | 8 | 7 | 0.875 | 0 |
| native_grep | temporal | core | 6 | 5 | 0.833 | 0 |
| native_grep | unprompted_push | core | 12 | 1 | 0.083 | 0 |
| native_index | abstention | core | 3 | 3 | 1.000 | 0 |
| native_index | disambiguation | core | 4 | 2 | 0.500 | 0 |
| native_index | distractor_robustness | core | 2 | 0 | 0.000 | 0 |
| native_index | follow_through | core | 6 | 5 | 0.833 | 0 |
| native_index | multi_hop | core | 3 | 2 | 0.667 | 0 |
| native_index | redundancy | core | 4 | 0 | 0.000 | 0 |
| native_index | single_hop | core | 8 | 7 | 0.875 | 0 |
| native_index | temporal | core | 6 | 3 | 0.500 | 0 |
| native_index | unprompted_push | core | 12 | 4 | 0.333 | 0 |
| none | abstention | core | 3 | 3 | 1.000 | 0 |
| none | disambiguation | core | 4 | 0 | 0.000 | 0 |
| none | distractor_robustness | core | 2 | 0 | 0.000 | 0 |
| none | follow_through | core | 6 | 0 | 0.000 | 0 |
| none | multi_hop | core | 3 | 0 | 0.000 | 0 |
| none | redundancy | core | 4 | 0 | 0.000 | 0 |
| none | single_hop | core | 8 | 0 | 0.000 | 0 |
| none | temporal | core | 6 | 0 | 0.000 | 0 |
| none | unprompted_push | core | 12 | 0 | 0.000 | 0 |
| oracle | abstention | core | 3 | 3 | 1.000 | 0 |
| oracle | disambiguation | core | 4 | 4 | 1.000 | 0 |
| oracle | distractor_robustness | core | 2 | 2 | 1.000 | 0 |
| oracle | follow_through | core | 6 | 6 | 1.000 | 0 |
| oracle | multi_hop | core | 3 | 3 | 1.000 | 0 |
| oracle | redundancy | core | 4 | 3 | 0.750 | 0 |
| oracle | single_hop | core | 8 | 5 | 0.625 | 0 |
| oracle | temporal | core | 6 | 6 | 1.000 | 0 |
| oracle | unprompted_push | core | 12 | 4 | 0.333 | 0 |
| pull | abstention | core | 3 | 3 | 1.000 | 0 |
| pull | disambiguation | core | 4 | 4 | 1.000 | 0 |
| pull | distractor_robustness | core | 2 | 1 | 0.500 | 0 |
| pull | follow_through | core | 6 | 6 | 1.000 | 0 |
| pull | multi_hop | core | 2 | 1 | 0.500 | 1 |
| pull | redundancy | core | 4 | 2 | 0.500 | 0 |
| pull | single_hop | core | 8 | 7 | 0.875 | 0 |
| pull | temporal | core | 6 | 5 | 0.833 | 0 |
| pull | unprompted_push | core | 12 | 1 | 0.083 | 0 |
| push_breadcrumb | abstention | core | 3 | 3 | 1.000 | 0 |
| push_breadcrumb | disambiguation | core | 4 | 0 | 0.000 | 0 |
| push_breadcrumb | distractor_robustness | core | 2 | 0 | 0.000 | 0 |
| push_breadcrumb | follow_through | core | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | multi_hop | core | 3 | 0 | 0.000 | 0 |
| push_breadcrumb | redundancy | core | 4 | 0 | 0.000 | 0 |
| push_breadcrumb | single_hop | core | 8 | 0 | 0.000 | 0 |
| push_breadcrumb | temporal | core | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | unprompted_push | core | 12 | 0 | 0.000 | 0 |
| push_breadcrumb_pull | abstention | core | 2 | 2 | 1.000 | 1 |
| push_breadcrumb_pull | disambiguation | core | 4 | 4 | 1.000 | 0 |
| push_breadcrumb_pull | distractor_robustness | core | 2 | 2 | 1.000 | 0 |
| push_breadcrumb_pull | follow_through | core | 6 | 5 | 0.833 | 0 |
| push_breadcrumb_pull | multi_hop | core | 3 | 2 | 0.667 | 0 |
| push_breadcrumb_pull | redundancy | core | 4 | 3 | 0.750 | 0 |
| push_breadcrumb_pull | single_hop | core | 8 | 7 | 0.875 | 0 |
| push_breadcrumb_pull | temporal | core | 6 | 6 | 1.000 | 0 |
| push_breadcrumb_pull | unprompted_push | core | 12 | 2 | 0.167 | 0 |
| push_pages_upper_bound | abstention | core | 3 | 3 | 1.000 | 0 |
| push_pages_upper_bound | disambiguation | core | 4 | 4 | 1.000 | 0 |
| push_pages_upper_bound | distractor_robustness | core | 2 | 2 | 1.000 | 0 |
| push_pages_upper_bound | follow_through | core | 6 | 0 | 0.000 | 0 |
| push_pages_upper_bound | multi_hop | core | 3 | 0 | 0.000 | 0 |
| push_pages_upper_bound | redundancy | core | 4 | 2 | 0.500 | 0 |
| push_pages_upper_bound | single_hop | core | 8 | 3 | 0.375 | 0 |
| push_pages_upper_bound | temporal | core | 6 | 6 | 1.000 | 0 |
| push_pages_upper_bound | unprompted_push | core | 12 | 0 | 0.000 | 0 |
| push_sections_pull | abstention | core | 2 | 2 | 1.000 | 1 |
| push_sections_pull | disambiguation | core | 4 | 4 | 1.000 | 0 |
| push_sections_pull | distractor_robustness | core | 2 | 2 | 1.000 | 0 |
| push_sections_pull | follow_through | core | 6 | 4 | 0.667 | 0 |
| push_sections_pull | multi_hop | core | 3 | 2 | 0.667 | 0 |
| push_sections_pull | redundancy | core | 4 | 2 | 0.500 | 0 |
| push_sections_pull | single_hop | core | 8 | 8 | 1.000 | 0 |
| push_sections_pull | temporal | core | 6 | 6 | 1.000 | 0 |
| push_sections_pull | unprompted_push | core | 12 | 2 | 0.167 | 0 |

Every `oracle` probe graded incorrect (12 probes):

| probe_class | scale | replicate | probe_id | push_breadcrumb_pull correct |
|---|---|---|---|---|
| redundancy | core | 0 | keelbridge_programme_scope | True |
| single_hop | core | 0 | bramfield_retainer_renewal | True |
| single_hop | core | 0 | remote_equipment_stipend_cap | False |
| single_hop | core | 0 | triform_vendor_consolidation | True |
| unprompted_push | core | 0 | cache_layer_merge | False |
| unprompted_push | core | 0 | connection_pool_widen | False |
| unprompted_push | core | 0 | export_flag_deletion | False |
| unprompted_push | core | 0 | invoice_role_merge | True |
| unprompted_push | core | 0 | invoiceref_rename | False |
| unprompted_push | core | 0 | rate_limit_removal | True |
| unprompted_push | core | 0 | tls_library_bump | False |
| unprompted_push | core | 0 | webhook_backoff_lower | False |

## 5. Positive control: `oracle` on `unprompted_push`

| scale | replicate | n | correct | rate | harness failures |
|---|---|---|---|---|---|
| core | 0 | 12 | 4 | 0.333 | 0 |

## 6. Grid coverage

48 probes have at least one row; 48 are complete across every (scale, replicate, arm) this store carries. 9 probes have no row at all.

Absent entirely (no row at all):

| probe_id |
|---|
| `aggregation_delivery_roles` |
| `aggregation_retainer_clients` |
| `aggregation_inhouse_tools` |
| `expense_approval_limit_manager` |
| `overtime_request_approver` |
| `invoicing_api_pagination_workaround` |
| `regional_office_onboarding_lesson` |
| `vendor_migration_rerun_lesson` |
| `junior_analyst_ramp_lesson` |

## 7. Actual spend for this run

Priced at `claude-haiku-4-5-20251001` rates with the cache multipliers `athenaeum.models` documents (1.25x on a cache write, 0.1x on a cache read), over EVERY persisted row -- a cell that tripped the turn cap was paid for exactly like one that answered.

| rows counted | input | cache write | cache read | output | billable input | USD |
|---|---|---|---|---|---|---|
| 432 | 3,318,600 | 0 | 0 | 129,390 | 3,318,600 | $3.97 |

Total tokens: 3,447,990.
